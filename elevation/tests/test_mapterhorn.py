import io
import json
import multiprocessing
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pmtiles.tile import TileType, zxy_to_tileid

from foundinspace.terrain.acquisition import digest, write_json
from foundinspace.terrain.encoding import decode_terrarium, encode_terrarium
from foundinspace.terrain.mapterhorn import (
    acquire,
    build,
    converted_tiles,
    execution_settings,
    split_tile,
    validate,
    zoom_zero,
)
from foundinspace.terrain.pmtiles_archive import (
    DiskPacker,
    RangeSource,
    check_structure,
    local_archive,
    tile_entries,
)


def webp(values):
    stream = io.BytesIO()
    Image.fromarray(encode_terrarium(values)).save(stream, format="WEBP", lossless=True)
    return stream.getvalue()


def image_values(payload):
    with Image.open(io.BytesIO(payload)) as image:
        assert image.mode == "RGB"
        return decode_terrarium(np.asarray(image))


def source(tmp_path):
    path = tmp_path / "source.pmtiles"
    rows, cols = np.indices((512, 512))
    # Integer Terrarium codes, byte carries and opposite signs in each axis.
    values = (cols * 301 - rows * 409 + 37) / 256
    payloads = {
        (0, 0, 0): webp(values),
        (1, 0, 0): webp(np.zeros((512, 512))),
        (1, 1, 1): webp(values - 409),
        (12, 2390, 1248): webp(values + 2300),
    }
    packer = DiskPacker(path, {"synthetic": True})
    for zxy, payload in payloads.items():
        packer.add(zxy_to_tileid(*zxy), payload)
    packer.finish(
        {
            "min_zoom": 0,
            "max_zoom": 12,
            "tile_type": TileType.WEBP,
            "min_lat_e7": -850511287,
            "max_lat_e7": 850511287,
        },
        {"attribution": "Synthetic test fixture"},
    )
    manifest = tmp_path / "source.json"
    write_json(
        manifest,
        {
            "bytes": path.stat().st_size,
            "sha256": digest(path),
            "kind": "synthetic",
            "file": path.name,
            "completeSnapshot": False,
        },
    )
    return path, manifest, values


def test_split_preserves_all_quadrants_negative_heights_and_orientation():
    rows, cols = np.indices((512, 512))
    values = (cols * 301 - rows * 409 + 37) / 256
    pieces = dict(split_tile(3, 5, 2, webp(values)))
    for dy in range(2):
        for dx in range(2):
            np.testing.assert_array_equal(
                image_values(pieces[zxy_to_tileid(4, 10 + dx, 4 + dy)]),
                values[dy * 256 : (dy + 1) * 256, dx * 256 : (dx + 1) * 256],
            )


def test_zoom_zero_filters_scalar_values_across_encoded_channel_carries():
    values = np.tile(
        np.array([[-1 / 256, 1 / 256], [255 + 255 / 256, -256]]), (256, 256)
    )
    result = image_values(zoom_zero(webp(values)))
    expected = round(float(values[:2, :2].mean()) * 256) / 256
    np.testing.assert_array_equal(result, expected)
    assert abs(expected - values[:2, :2].mean()) <= 1 / 512


def test_final_archive_sparse_coverage_zero_and_independent_numeric_validation(
    tmp_path,
):
    path, manifest, _ = source(tmp_path)
    output = tmp_path / "product"
    product = build(path, manifest, output)
    assert product["format"] == "earth-elevation-pmtiles"
    assert product["verticalReference"]["kind"] == "upstream-unresolved"
    assert product["uploadReady"] is False
    report = validate(path, output / "earth.pmtiles")
    assert report["structure"]["addressedTiles"] == 17
    assert report["zoom0"]["maximumEncodingErrorMetres"] <= 1 / 512
    assert sum(s["negativePixels"] for s in report["samples"]) > 0
    with local_archive(output / "earth.pmtiles") as reader:
        assert reader.get(2, 2, 0) is None  # Absent source (1,1,0).
        assert np.all(image_values(reader.get(2, 0, 0)) == 0)  # Present zero source.


def test_input_manifest_rejects_source_corruption(tmp_path):
    path, manifest, _ = source(tmp_path)
    with path.open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ValueError, match="immutable manifest"):
        build(path, manifest, tmp_path / "output")


def test_resuming_discards_uncommitted_payloads_and_rejects_recipe_changes(tmp_path):
    path = tmp_path / "archive.pmtiles"
    packer = DiskPacker(path, {"input": "a"})
    payload = b"a payload"
    packer.add(0, payload)
    packer.checkpoint(10)
    packer.add(1, b"orphan bytes")
    packer.close()
    with pytest.raises(ValueError, match="different input"):
        DiskPacker(path, {"input": "b"})
    resumed = DiskPacker(path, {"input": "a"})
    assert resumed.get_state("progress") == "10"
    resumed.add(1, payload)
    resumed.checkpoint(11)
    resumed.finish({"min_zoom": 0, "max_zoom": 1}, {})
    with local_archive(path) as reader:
        assert check_structure(reader, path.stat().st_size)["addressedTiles"] == 2
        assert reader.get(0, 0, 0) == reader.get(1, 0, 0) == payload
        assert reader.header()["tile_data_length"] == len(payload) * 2


def test_packer_directory_chunks_and_payload_bytes_are_deterministic(tmp_path):
    paths = [tmp_path / "first.pmtiles", tmp_path / "second.pmtiles"]
    for path in paths:
        packer = DiskPacker(path, {"same": True})
        for tile_id in range(85):
            packer.add(tile_id, f"payload {tile_id}".encode())
        packer.finish({"min_zoom": 0, "max_zoom": 3}, {}, leaf_size=5)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    with local_archive(paths[0]) as reader:
        assert check_structure(reader, paths[0].stat().st_size)["addressedTiles"] == 85
        assert reader.get(3, 7, 7) == f"payload {zxy_to_tileid(3, 7, 7)}".encode()


def test_acquisition_capacity_gate_precedes_network_or_partial_creation(
    tmp_path, monkeypatch
):
    import foundinspace.terrain.mapterhorn as module

    inspection = tmp_path / "inspection.json"
    write_json(
        inspection,
        {
            "url": "https://example.invalid/planet.pmtiles",
            "bytes": 355579263191,
            "etag": '"snapshot"',
            "lastModified": None,
        },
    )

    class Disk:
        free = 1

    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: Disk())
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_a, **_k: pytest.fail("Unexpected network request"),
    )
    with pytest.raises(ValueError, match="Insufficient durable capacity"):
        acquire(inspection, tmp_path / "incoming")
    assert not (tmp_path / "incoming/planet.pmtiles.part").exists()


def test_masked_or_wrong_size_tiles_cannot_be_silently_admitted():
    stream = io.BytesIO()
    Image.new("RGBA", (512, 512), (128, 0, 0, 0)).save(
        stream, format="WEBP", lossless=True
    )
    with pytest.raises(ValueError, match="explicit admission"):
        list(split_tile(0, 0, 0, stream.getvalue()))


def test_wrong_final_coverage_cannot_pass_on_counts_alone(tmp_path):
    path, manifest, _ = source(tmp_path)
    output = tmp_path / "product"
    build(path, manifest, output)
    # Header counts are authenticated against the full directory scan.
    archive = output / "earth.pmtiles"
    data = bytearray(archive.read_bytes())
    data[72:80] = (18).to_bytes(8, "little")
    archive.write_bytes(data)
    with pytest.raises(ValueError, match="counts disagree"):
        validate(path, archive)
    assert json.loads((output / "product.json").read_text())["status"] == "scratch"


def test_build_resumes_after_archive_completion_before_descriptor(
    tmp_path, monkeypatch
):
    import foundinspace.terrain.mapterhorn as module

    path, manifest, _ = source(tmp_path)
    output = tmp_path / "product"
    original_validate = module.validate

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("Interrupted before descriptor")

    monkeypatch.setattr(module, "validate", interrupted)
    with pytest.raises(RuntimeError, match="Interrupted"):
        build(path, manifest, output)
    assert (output / "earth.pmtiles").exists()
    assert not (output / "product.json").exists()
    with pytest.raises(ValueError, match="different build"):
        build(path, manifest, output, workers=1)
    assert not (output / "product.json").exists()
    monkeypatch.setattr(module, "validate", original_validate)
    product = build(path, manifest, output)
    assert product["transport"]["sha256"] == digest(output / "earth.pmtiles")


def archive_payloads(path):
    with local_archive(path) as reader:
        h = reader.header()
        return [
            (
                entry.tile_id,
                reader.get_bytes(h["tile_data_offset"] + entry.offset, entry.length),
            )
            for entry in tile_entries(reader)
        ]


def test_worker_counts_preserve_payload_bytes_order_and_execution_receipts(tmp_path):
    path, manifest, _ = source(tmp_path)
    expected = None
    for workers, limit in [(1, 1), (2, 1), (2, 4), (3, 5)]:
        output = tmp_path / f"product-{workers}-{limit}"
        build(path, manifest, output, workers=workers, max_in_flight=limit)
        payloads = archive_payloads(output / "earth.pmtiles")
        if expected is None:
            expected = payloads
        assert payloads == expected
        # Payload section order, not just directory sorting, is deterministic.
        with local_archive(output / "earth.pmtiles") as reader:
            entries = list(tile_entries(reader))
            assert [e.offset for e in entries] == sorted(e.offset for e in entries)
            identity = reader.metadata()["buildIdentity"]
        execution = execution_settings(workers, limit)
        assert identity["execution"] == execution
        for name in ("recipe.json", "provenance.json"):
            assert json.loads((output / name).read_text())["execution"] == execution


@pytest.mark.parametrize("limit", [1, 3, 5])
def test_slow_first_tile_bounds_inputs_and_completed_results(monkeypatch, limit):
    """Gate completion deterministically; real spawn is covered by builds above."""
    import foundinspace.terrain.mapterhorn as module

    release_first = threading.Event()
    later_finished = threading.Event()
    consumed = []
    written = []
    completion_order = []
    state = {"completed": 0}

    def inputs():
        for tile_id in range(11):
            assert len(consumed) - len(written) < limit
            consumed.append(tile_id)
            yield tile_id, b"payload"

    def conversion(tile_id, _payload):
        if tile_id == 0:
            assert release_first.wait(10)
        completion_order.append(tile_id)
        state["completed"] += 1
        if state["completed"] == limit - 1:
            later_finished.set()
        return [(tile_id, b"converted")]

    def pool(*, max_workers, mp_context, initializer):
        assert mp_context.get_start_method() == "spawn"
        return ThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr(module, "ProcessPoolExecutor", pool)
    monkeypatch.setattr(module, "convert_tile", conversion)

    def consume():
        with closing(
            converted_tiles(inputs(), workers=5, max_in_flight=limit)
        ) as tiles:
            for tile_id, children in tiles:
                assert children == [(tile_id, b"converted")]
                written.append(tile_id)

    with ThreadPoolExecutor(max_workers=1) as consumer:
        future = consumer.submit(consume)
        try:
            if limit > 1:
                assert later_finished.wait(10)
                assert consumed == list(range(limit))
                assert written == []
                assert len(completion_order) == limit - 1
        finally:
            release_first.set()
        future.result(timeout=10)
    assert written == list(range(11))
    if limit > 1:
        assert completion_order.index(0) == limit - 1


@pytest.mark.parametrize("workers", [1, 2])
def test_single_in_flight_does_not_prefetch_and_one_worker_is_inline(
    monkeypatch, workers
):
    import foundinspace.terrain.mapterhorn as module

    if workers == 1:
        monkeypatch.setattr(
            module,
            "ProcessPoolExecutor",
            lambda **_kwargs: pytest.fail("Inline mode created a pool"),
        )
    consumed = []

    def inputs():
        for tile_id in range(4):
            consumed.append(tile_id)
            yield tile_id, webp(np.zeros((512, 512)))

    with closing(converted_tiles(inputs(), workers=workers, max_in_flight=1)) as tiles:
        assert next(tiles)[0] == 0
        assert consumed == [0]
    assert consumed == [0]


@pytest.mark.parametrize(
    "workers,limit",
    [(0, 1), (-1, 1), (True, 1), (1.5, 1), (1, 0), (2, -1), (2, False), (2, 1.5)],
)
def test_invalid_execution_settings_fail_before_input_or_output(
    tmp_path, workers, limit
):
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="positive integer"):
        build(
            "missing-source",
            "missing-manifest",
            output,
            workers=workers,
            max_in_flight=limit,
        )
    assert not output.exists()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_partial_child_write_rolls_back_to_contiguous_checkpoint(
    tmp_path, monkeypatch, failure
):
    import foundinspace.terrain.mapterhorn as module

    path, manifest, _ = source(tmp_path)
    output = tmp_path / "product"
    monkeypatch.setattr(module, "CHECKPOINT_SOURCE_TILES", 2)
    original_add = DiskPacker.add
    initial_children = set(multiprocessing.active_children())
    writes = []

    def interrupted_add(packer, tile_id, payload):
        original_add(packer, tile_id, payload)
        writes.append(tile_id)
        # z0 and two complete source tiles are durable; the next tile is partial.
        if len(writes) == 11:
            raise failure("Interrupted child write")

    monkeypatch.setattr(DiskPacker, "add", interrupted_add)
    with pytest.raises(failure, match="Interrupted child write"):
        build(path, manifest, output, workers=2, max_in_flight=3)
    assert set(multiprocessing.active_children()) == initial_children
    assert not (output / "earth.pmtiles").exists()
    assert not (output / "product.json").exists()
    with sqlite3.connect(output / "earth.work.sqlite") as db:
        assert db.execute(
            "SELECT value FROM state WHERE key='progress'"
        ).fetchone() == ("1",)
        assert db.execute("SELECT count(*) FROM tiles").fetchone() == (9,)
        end = int(db.execute("SELECT value FROM state WHERE key='end'").fetchone()[0])
    partial = output / "earth.pmtiles.part"
    assert partial.stat().st_size > end
    with pytest.raises(ValueError, match="different input or recipe"):
        build(path, manifest, output, workers=3, max_in_flight=3)
    assert partial.stat().st_size > end
    monkeypatch.setattr(DiskPacker, "add", original_add)
    build(path, manifest, output, workers=2, max_in_flight=3)
    fresh = tmp_path / "fresh"
    build(path, manifest, fresh, workers=2, max_in_flight=3)
    assert (output / "earth.pmtiles").read_bytes() == (
        fresh / "earth.pmtiles"
    ).read_bytes()


def test_worker_decode_error_does_not_publish_or_advance_checkpoint(tmp_path):
    path, manifest, _ = source(tmp_path)
    # Replace one stored WebP with invalid data, retaining the source index and
    # refreshing its manifest so admission succeeds and a spawned worker fails.
    with local_archive(path) as reader:
        h = reader.header()
        broken = list(tile_entries(reader))[2]
    with path.open("r+b") as stream:
        stream.seek(h["tile_data_offset"] + broken.offset)
        stream.write(b"X" * broken.length)
    receipt = json.loads(manifest.read_text())
    receipt["sha256"] = digest(path)
    write_json(manifest, receipt)
    output = tmp_path / "product"
    from PIL import UnidentifiedImageError

    for _ in range(2):
        with pytest.raises(UnidentifiedImageError):
            build(path, manifest, output, workers=2, max_in_flight=4)
        assert not (output / "product.json").exists()
        with sqlite3.connect(output / "earth.work.sqlite") as db:
            assert db.execute("SELECT count(*) FROM tiles").fetchone() == (0,)
            assert (
                db.execute("SELECT value FROM state WHERE key='progress'").fetchone()
                is None
            )


def fail_once_conversion(tile_id, payload):
    """Importable spawned-worker fault; its marker is test scratch, never input."""
    from foundinspace.terrain.mapterhorn import convert_tile

    marker = Path(os.environ["FIS_TEST_WORKER_FAULT"])
    if tile_id == 3 and not marker.exists():
        marker.write_text("fault injected")
        raise RuntimeError("Transient worker failure")
    return convert_tile(tile_id, payload)


def test_spawned_worker_failure_resumes_from_last_complete_source_tile(
    tmp_path, monkeypatch
):
    import foundinspace.terrain.mapterhorn as module

    path, manifest, _ = source(tmp_path)
    output = tmp_path / "product"
    monkeypatch.setattr(module, "CHECKPOINT_SOURCE_TILES", 2)
    monkeypatch.setenv("FIS_TEST_WORKER_FAULT", str(tmp_path / "worker-fault"))
    monkeypatch.setattr(module, "convert_tile", fail_once_conversion)
    with pytest.raises(RuntimeError, match="Transient worker failure"):
        build(path, manifest, output, workers=2, max_in_flight=3)
    assert not (output / "earth.pmtiles").exists()
    assert not (output / "product.json").exists()
    with sqlite3.connect(output / "earth.work.sqlite") as db:
        assert db.execute(
            "SELECT value FROM state WHERE key='progress'"
        ).fetchone() == ("1",)
        assert db.execute("SELECT count(*) FROM tiles").fetchone() == (9,)
    original_inputs = module.source_tiles
    resumed_ids = []

    def observed_inputs(*args):
        for tile_id, payload in original_inputs(*args):
            resumed_ids.append(tile_id)
            yield tile_id, payload

    monkeypatch.setattr(module, "source_tiles", observed_inputs)
    build(path, manifest, output, workers=2, max_in_flight=3)
    with local_archive(path) as reader:
        assert resumed_ids == [e.tile_id for e in tile_entries(reader) if e.tile_id > 1]
    fresh = tmp_path / "fresh"
    build(path, manifest, fresh, workers=2, max_in_flight=3)
    assert (output / "earth.pmtiles").read_bytes() == (
        fresh / "earth.pmtiles"
    ).read_bytes()


def test_source_iterator_expands_runs_lazily_and_resumes_inside_them(
    tmp_path, monkeypatch
):
    from pmtiles.tile import Entry

    import foundinspace.terrain.mapterhorn as module

    reads = []

    class Reader:
        def get_bytes(self, offset, length):
            reads.append((offset, length))
            return b"webp"

    def entries(_reader):
        yield Entry(10, 3, 4, 1_000_000)
        pytest.fail("Prefetched a later directory entry")

    monkeypatch.setattr(module, "tile_entries", entries)
    with closing(
        module.source_tiles(Reader(), {"tile_data_offset": 100}, 12, tmp_path)
    ) as tiles:
        assert next(tiles) == (13, b"webp")
        assert next(tiles) == (14, b"webp")
        assert reads == [(103, 4), (103, 4)]


def test_http_ranges_reject_full_responses_changed_etags_and_cached_corruption(
    tmp_path, monkeypatch
):
    import foundinspace.terrain.pmtiles_archive as module

    identity = {"Content-Length": "1024", "ETag": '"original"'}
    responses = []

    class Response(io.BytesIO):
        def __init__(self, payload, status, headers):
            super().__init__(payload)
            self.status, self.headers = status, headers

    def request(req, **_kwargs):
        if req.get_method() == "HEAD":
            return Response(b"", 200, identity)
        assert req.get_header("If-match") == '"original"'
        return responses.pop(0)

    monkeypatch.setattr(module.urllib.request, "urlopen", request)
    ranges = RangeSource("https://example.invalid/archive.pmtiles", tmp_path / "ranges")
    responses.append(Response(b"bad", 200, {"ETag": '"original"'}))
    with pytest.raises(ValueError, match="snapshot-bound"):
        ranges(0, 3)
    responses.append(
        Response(b"abc", 206, {"ETag": '"changed"', "Content-Range": "bytes 0-2/1024"})
    )
    with pytest.raises(ValueError, match="snapshot-bound"):
        ranges(0, 3)
    responses.append(
        Response(b"abc", 206, {"ETag": '"original"', "Content-Range": "bytes 0-2/1024"})
    )
    assert ranges(0, 3) == b"abc"
    assert ranges(0, 3) == b"abc"
    (tmp_path / "ranges/0-3.bin").write_bytes(b"xyz")
    with pytest.raises(ValueError, match="Cached range changed"):
        ranges(0, 3)
    identity["ETag"] = '"changed"'
    with pytest.raises(ValueError, match="Remote source changed"):
        RangeSource("https://example.invalid/archive.pmtiles", tmp_path / "ranges")
