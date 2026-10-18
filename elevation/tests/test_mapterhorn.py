import io
import json

import numpy as np
import pytest
from PIL import Image
from pmtiles.tile import TileType, zxy_to_tileid

from foundinspace.terrain.acquisition import digest, write_json
from foundinspace.terrain.encoding import decode_terrarium, encode_terrarium
from foundinspace.terrain.mapterhorn import (
    acquire,
    build,
    split_tile,
    validate,
    zoom_zero,
)
from foundinspace.terrain.pmtiles_archive import (
    DiskPacker,
    RangeSource,
    check_structure,
    local_archive,
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
    monkeypatch.setattr(module, "validate", original_validate)
    product = build(path, manifest, output)
    assert product["transport"]["sha256"] == digest(output / "earth.pmtiles")


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
