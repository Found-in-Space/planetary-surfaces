"""Explicit Earth PMTiles preparation, separate from the lunar XYZ contract.

All outputs remain scratch until a complete immutable acquisition, clean code
and the independent final validation and source-rights review are available.
"""

import argparse
import hashlib
import importlib.metadata
import io
import json
import math
import multiprocessing
import platform
import shutil
import signal
import sqlite3
import subprocess
import time
import urllib.request
import zlib
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, features
from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType, tileid_to_zxy, zxy_to_tileid

from .acquisition import digest, write_json
from .encoding import decode_terrarium, encode_terrarium
from .pmtiles_archive import (
    DiskPacker,
    RangeSource,
    check_structure,
    local_archive,
    tile_entries,
)

URL = "https://download.mapterhorn.com/planet.pmtiles"
UPSTREAM_SHA = "775663e9ca58c7fe741dbd53fbe4461cb73a9bb9"
DEFAULT_WORKERS = 2
CHECKPOINT_SOURCE_TILES = 128
LOCATIONS = {
    "everest": (86.925, 27.988),
    "alps": (7.96, 46.58),
    "dutch-coast": (4.1, 51.99),
    "dead-sea": (35.5, 31.5),
    "open-pacific": (-140, 0),
    "north-mercator-edge": (15, 85.04),
    "south-mercator-edge": (0, -85.04),
}


def now():
    return datetime.now(UTC).isoformat()


def json_header(header):
    return {key: getattr(value, "name", value) for key, value in header.items()}


def code_identity():
    root = Path(__file__).resolve().parents[4]
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root, text=True
    ).splitlines()
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "elevation"],
        cwd=root,
        text=True,
    ).splitlines()
    hashes = {
        name: digest(root / name)
        for name in sorted(set(files))
        if (root / name).is_file()
    }
    tree = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {
        "repository": "https://github.com/Found-in-Space/planetary-surfaces",
        "commit": revision,
        "dirty": bool(status),
        "status": status,
        "elevationTreeSha256": tree,
        "lockSha256": digest(root / "elevation/uv.lock"),
        "python": platform.python_version(),
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("pmtiles", "pillow", "numpy")
        },
        "webp": features.version("webp"),
        "zlib": features.version("zlib"),
        "stdlibZlib": zlib.ZLIB_RUNTIME_VERSION,
        "sqlite": sqlite3.sqlite_version,
    }


def rgb_image(payload, *, size, image_format):
    with Image.open(io.BytesIO(payload)) as image:
        if (
            image.format != image_format
            or image.size != (size, size)
            or image.mode != "RGB"
        ):
            raise ValueError(
                "Unexpected tile encoding, dimensions or mask; explicit admission required"
            )
        if any(
            key in image.info
            for key in ("icc_profile", "gamma", "srgb", "transparency")
        ):
            raise ValueError(
                "Colour-managed or masked height payload requires explicit admission"
            )
        return np.asarray(image).copy()


def png(rgb):
    stream = io.BytesIO()
    Image.fromarray(rgb).save(stream, format="PNG", compress_level=6)
    return stream.getvalue()


def split_tile(z, x, y, payload):
    rgb = rgb_image(payload, size=512, image_format="WEBP")
    # Decode and encode through the shared scalar-height contract. No filtering
    # is performed here; all 24-bit codes round-trip exactly, including negatives.
    heights = decode_terrarium(rgb)
    for dy in range(2):
        for dx in range(2):
            tile_id = zxy_to_tileid(z + 1, x * 2 + dx, y * 2 + dy)
            quadrant = heights[dy * 256 : (dy + 1) * 256, dx * 256 : (dx + 1) * 256]
            yield tile_id, png(encode_terrarium(quadrant))


def zoom_zero(payload):
    heights = decode_terrarium(rgb_image(payload, size=512, image_format="WEBP"))
    # Equal weights in the projected pixel grid, not a surface-area average.
    reduced = heights.reshape(256, 2, 256, 2).mean(axis=(1, 3), dtype=np.float64)
    return png(encode_terrarium(reduced))


def execution_settings(workers, max_in_flight):
    """Execution is part of restart identity, even though tile bytes are unchanged."""
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    if max_in_flight is None:
        max_in_flight = 2 * workers if workers > 1 else 1
    if type(max_in_flight) is not int or max_in_flight < 1:
        raise ValueError("max_in_flight must be a positive integer")
    return {
        "workers": workers,
        "maxInFlight": max_in_flight,
        "startMethod": "spawn" if workers > 1 else "inline",
        "checkpointSourceTiles": CHECKPOINT_SOURCE_TILES,
    }


def source_tiles(reader, header, progress, output):
    """Read one payload at a time in the parent, including expanded sparse runs."""
    for entry in tile_entries(reader):
        for tile_id in range(
            max(entry.tile_id, progress + 1), entry.tile_id + entry.run_length
        ):
            if shutil.disk_usage(output).free < 2 * 1024**3:
                raise ValueError(
                    "Stopped before exhausting durable storage; resume after adding capacity"
                )
            yield (
                tile_id,
                reader.get_bytes(
                    header["tile_data_offset"] + entry.offset, entry.length
                ),
            )


def convert_tile(tile_id, payload):
    """Picklable CPU-only task: no archive handles, paths or SQLite connections."""
    return sorted(split_tile(*tileid_to_zxy(tile_id), payload))


def worker_signals():
    # The parent handles Ctrl-C and rolls back its uncommitted writes. Workers
    # finish at most the bounded window instead of also raising KeyboardInterrupt.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def converted_tiles(inputs, *, workers, max_in_flight):
    """Ordered sliding window bounded across queued AND completed conversions.

    Refill only after the caller consumes a result. In particular, a slow first
    tile holds the window closed even if every later task has already finished.
    Use closing() at the call site so writer errors also cancel pending work.
    """
    settings = execution_settings(workers, max_in_flight)
    if workers == 1:
        for tile_id, payload in inputs:
            yield tile_id, convert_tile(tile_id, payload)
        return
    pool = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=worker_signals,
    )
    pending = deque()
    try:
        inputs = iter(inputs)
        exhausted = False
        while True:
            while not exhausted and len(pending) < settings["maxInFlight"]:
                item = next(inputs, None)
                if item is None:
                    exhausted = True
                    break
                tile_id, payload = item
                pending.append((tile_id, pool.submit(convert_tile, tile_id, payload)))
                del payload, item
            if not pending:
                break
            tile_id, future = pending.popleft()
            yield tile_id, future.result()
            # Drop the consumed result before submitting another task; futures
            # retain their returned PNGs even after result() has been called.
            del future
    finally:
        for _, future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)


def admit_source(reader):
    h = reader.header()
    if (h["min_zoom"], h["max_zoom"]) != (0, 12):
        raise ValueError("Expected the global source zoom contract 0–12")
    if h["tile_type"] != TileType.WEBP or h["tile_compression"] != Compression.NONE:
        raise ValueError("Expected uncompressed WebP payloads")
    if [h[k] for k in ("min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7")] != [
        -1800000000,
        -850511287,
        1800000000,
        850511287,
    ]:
        raise ValueError("Source bounds changed")
    return h


def descriptor(source, header, identity):
    return {
        "format": "earth-elevation-pmtiles",
        "formatVersion": 1,
        "status": "scratch",
        "uploadReady": False,
        "body": "earth",
        "grid": {
            "projection": "EPSG:3857",
            "scheme": "xyz",
            "radiusMetres": 6378137,
            "pixelRegistration": "area; pixel centres at half-pixel offsets",
            "radiusMeaning": "Web Mercator horizontal projection parameter; not a height datum",
        },
        "units": "metres",
        "verticalReference": {
            "kind": "upstream-unresolved",
            "transformation": "none",
            "description": "Source-specific heights preserved; no uniform geoid, ellipsoid or reference-sphere datum established by the archive metadata.",
        },
        "transport": {
            "kind": "pmtiles",
            "version": 3,
            "file": "earth.pmtiles",
            "httpRangeRequired": True,
        },
        "encoding": {
            "kind": "terrarium",
            "payload": "png",
            "channels": "RGB",
            "tileSize": 256,
            "quantumMetres": 1 / 256,
        },
        "zooms": {"minimum": 0, "maximum": 13},
        "coverage": {
            "bounds": [-180, -85.0511287, 180, 85.0511287],
            "addressedTiles": header["addressed_tiles_count"],
            "availability": "PMTiles directory is authoritative; sparse",
            "missingTiles": "absent, never implicitly flat or measured ocean",
            "zeroValues": "valid encoded upstream zeros; may include upstream nodata fill and ocean; no recoverable measurement mask",
            "polarCoverage": "No coverage outside Web Mercator bounds; bounds within them do not establish measurements",
        },
        "derivation": {
            "sourceZooms": [0, 12],
            "outputSplitZooms": [1, 13],
            "split": "z+1, 2x+dx, 2y+dy; dy=0 north, dx=0 west; exact scalar samples",
            "zoom0": "2 by 2 arithmetic mean of decoded source z0 heights in projected pixel space; float64; nearest-even Terrarium rounding",
            "maximumAdditionalEncodingErrorMetres": 1 / 512,
            "bathymetryAdded": False,
        },
        "source": source,
        "producer": identity,
        "consumerCompatibility": "Separate experimental contract. elevation-xyz v2 requires reference-sphere heights and XYZ URLs; do not relabel this archive as that contract.",
    }


def build(
    source_path, manifest_path, output, *, workers=DEFAULT_WORKERS, max_in_flight=None
):
    """Build from a SHA-bound local source, with per-batch durable restart state."""
    execution = execution_settings(workers, max_in_flight)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    source = json.loads(Path(manifest_path).read_text())
    if (
        digest(source_path) != source["sha256"]
        or Path(source_path).stat().st_size != source["bytes"]
    ):
        raise ValueError("Source does not match its immutable manifest")
    identity = code_identity()
    build_identity = {
        "sourceSha256": source["sha256"],
        "producer": identity,
        "recipeVersion": 2,
        "execution": execution,
    }
    archive = output / "earth.pmtiles"
    # A process may have completed the archive rename and stopped before
    # validation or the descriptor placement. Recover only the same build.
    if archive.exists():
        with local_archive(archive) as previous:
            if previous.metadata().get("buildIdentity") != build_identity:
                raise ValueError("Existing archive belongs to a different build")
        return finish_build(source_path, source, output, identity, build_identity)
    with local_archive(source_path) as reader:
        source_header = admit_source(reader)
        packer = DiskPacker(archive, build_identity)
        try:
            progress = packer.get_state("progress")
            progress = int(progress) if progress is not None else -1
            if progress < 0:
                base = reader.get(0, 0, 0)
                if base is None:
                    raise ValueError("Cannot generate output z0 without source z0")
                packer.add(0, zoom_zero(base))
                del base
            batch = 0
            last_tile_id = progress
            inputs = source_tiles(reader, source_header, progress, output)
            with closing(
                converted_tiles(
                    inputs, workers=workers, max_in_flight=execution["maxInFlight"]
                )
            ) as converted:
                for tile_id, children in converted:
                    for child_id, child in children:
                        packer.add(child_id, child)
                    last_tile_id = tile_id
                    batch += 1
                    if batch % CHECKPOINT_SOURCE_TILES == 0:
                        packer.checkpoint(tile_id)
                    del children, child
            packer.checkpoint(last_tile_id)
            packer.finish(
                {**source_header, "tile_type": TileType.PNG, "max_zoom": 13},
                {
                    "name": "Earth elevation 256; scratch",
                    "format": "png",
                    "encoding": "terrarium",
                    "tileSize": 256,
                    "attribution": reader.metadata().get(
                        "attribution", "Mapterhorn; see attribution.json"
                    ),
                    "sourceSha256": source["sha256"],
                    "verticalReference": "upstream-unresolved",
                    "buildIdentity": build_identity,
                },
            )
        except BaseException:
            packer.close()
            raise
    return finish_build(source_path, source, output, identity, build_identity)


def finish_build(source_path, source, output, identity, build_identity):
    archive = output / "earth.pmtiles"
    with local_archive(archive) as reader:
        header = reader.header()
    with local_archive(source_path) as reader:
        source_header = admit_source(reader)
    product = descriptor(source, header, identity)
    write_json(output / "source-manifest.json", source)
    write_json(
        output / "provenance.json",
        {
            "createdUtc": now(),
            "buildIdentity": build_identity,
            "execution": build_identity["execution"],
            "sourceHeader": json_header(source_header),
        },
    )
    report = validate(source_path, archive)
    write_json(output / "validation.json", report)
    write_json(
        output / "recipe.json",
        {
            "formatVersion": 1,
            "sourceFile": source["file"],
            "sourceSha256": source["sha256"],
            "sourceManifest": "source-manifest.json",
            "outputArchive": "earth.pmtiles",
            "command": "UV_CACHE_DIR=.cache/uv uv run python -m foundinspace.terrain.mapterhorn build <source> <source-manifest> --output <output> --workers <execution.workers> --max-in-flight <execution.maxInFlight>",
            "execution": build_identity["execution"],
            "conversion": product["derivation"],
            "producer": identity,
            "sourceScope": source["kind"],
        },
    )
    product["transport"]["bytes"] = archive.stat().st_size
    product["transport"]["sha256"] = digest(archive)
    # Descriptor is exposed last, after reading and checking the final archive.
    write_json(output / "product.json", product)
    return product


def validate(source_path, archive, *, samples_per_zoom=8):
    """All index entries; deterministic sampled pixels independently reconstructed."""
    samples = []
    with local_archive(source_path) as source, local_archive(archive) as result:
        h = admit_source(source)
        structure = check_structure(result, Path(archive).stat().st_size)
        oh = result.header()
        if (
            oh["tile_type"] != TileType.PNG
            or oh["tile_compression"] != Compression.NONE
        ):
            raise ValueError("Incorrect output tile type or compression")
        expected_count = 4 * h["addressed_tiles_count"] + 1
        if structure["addressedTiles"] != expected_count or (
            oh["min_zoom"],
            oh["max_zoom"],
        ) != (0, 13):
            raise ValueError("Output count or zoom contract mismatch")
        # A source Hilbert run [a,b) maps to [4a+1,4b+1) at the next
        # zoom, irrespective of quadrant orientation. Compare all coverage IDs
        # as intervals, without payload reads, a global index or gap filling.
        output_entries = iter(tile_entries(result))
        base_entry = next(output_entries)
        if base_entry.tile_id != 0 or base_entry.run_length != 1:
            raise ValueError("Missing or invalid output zoom-zero coverage")
        current = next(output_entries, None)
        for entry in tile_entries(source):
            start, stop = (
                4 * entry.tile_id + 1,
                4 * (entry.tile_id + entry.run_length) + 1,
            )
            while start < stop:
                if (
                    current is None
                    or current.tile_id != start
                    or start + current.run_length > stop
                ):
                    raise ValueError(
                        "Output coverage differs from source child coverage"
                    )
                start += current.run_length
                current = next(output_entries, None)
        if current is not None:
            raise ValueError("Extra output coverage")
        seen = {}
        for entry in tile_entries(source):
            z, x, y = tileid_to_zxy(entry.tile_id)
            if seen.get(z, 0) >= samples_per_zoom:
                continue
            seen[z] = seen.get(z, 0) + 1
            original = rgb_image(
                source.get(z, x, y), size=512, image_format="WEBP"
            ).astype(np.int32)
            # Independent integer decoding: do not use the production decoder.
            expected_codes = (
                original[..., 0] * 65536 + original[..., 1] * 256 + original[..., 2]
            )
            for dy in range(2):
                for dx in range(2):
                    actual = rgb_image(
                        result.get(z + 1, x * 2 + dx, y * 2 + dy),
                        size=256,
                        image_format="PNG",
                    ).astype(np.int32)
                    codes = (
                        actual[..., 0] * 65536 + actual[..., 1] * 256 + actual[..., 2]
                    )
                    expected = expected_codes[
                        dy * 256 : (dy + 1) * 256, dx * 256 : (dx + 1) * 256
                    ]
                    error = float(np.max(np.abs(codes - expected))) / 256
                    if error:
                        raise ValueError("Child quadrant sample mismatch")
            samples.append(
                {
                    "sourceZxy": [z, x, y],
                    "pixels": 512**2,
                    "maxErrorMetres": 0,
                    "negativePixels": int(
                        np.count_nonzero(expected_codes < 32768 * 256)
                    ),
                }
            )
        base = rgb_image(source.get(0, 0, 0), size=512, image_format="WEBP").astype(
            np.int32
        )
        raw_codes = base[..., 0] * 65536 + base[..., 1] * 256 + base[..., 2]
        # Independent slicing and addition, rather than the production reshape.
        mean_codes = (
            raw_codes[::2, ::2].astype(np.int64)
            + raw_codes[1::2, ::2]
            + raw_codes[::2, 1::2]
            + raw_codes[1::2, 1::2]
        ) / 4
        base_result = rgb_image(
            result.get(0, 0, 0), size=256, image_format="PNG"
        ).astype(np.int32)
        actual_codes = (
            base_result[..., 0] * 65536
            + base_result[..., 1] * 256
            + base_result[..., 2]
        )
        zero_error = float(np.max(np.abs(actual_codes - mean_codes))) / 256
        if zero_error > 1 / 512 or not np.array_equal(
            actual_codes, np.rint(mean_codes)
        ):
            raise ValueError("Zoom-zero scalar reduction mismatch")
    return {
        "status": "passed",
        "structure": structure,
        "comparison": "All directory entries and coverage IDs; deterministic first source entries per represented zoom; all pixels within sampled quadrants",
        "samples": samples,
        "samplePixels": sum(s["pixels"] for s in samples),
        "zoom0": {"pixels": 256**2, "maximumEncodingErrorMetres": zero_error},
        "limitations": [
            "Sampled pixels do not validate every pixel",
            "No independent geodetic or measured-height accuracy claim",
        ],
    }


def coordinate(lon, lat, z):
    n = 1 << z
    x = min(n - 1, max(0, int((lon + 180) / 360 * n)))
    y = min(
        n - 1,
        max(0, int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)),
    )
    return z, x, y


def probe(output):
    """Bounded remote inspection; never represent an extract as a full snapshot."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    remote = RangeSource(URL, output / "ranges")
    reader = Reader(remote)
    header = admit_source(reader)
    source_info = {
        **remote.identity,
        "retrievedUtc": now(),
        "header": json_header(header),
        "metadata": reader.metadata(),
        "completeSnapshot": False,
    }
    urls = "https://download.mapterhorn.com/download_urls.json"
    with urllib.request.urlopen(
        urllib.request.Request(
            urls, headers={"User-Agent": "foundinspace-terrain/0.1"}
        ),
        timeout=60,
    ) as response:
        manifest_bytes = response.read(4 * 1024**2)
    (output / "download_urls.json").write_bytes(manifest_bytes)
    upstream = next(
        item
        for item in json.loads(manifest_bytes)["items"]
        if item["name"] == "planet.pmtiles"
    )
    if upstream["size"] != remote.identity["bytes"]:
        raise ValueError("Source size and upstream manifest disagree")
    source_info["published"] = upstream
    write_json(output / "upstream-source.json", source_info)
    requests = {0: {"zxy": (0, 0, 0), "labels": ["global-z0"]}}
    for name, (lon, lat) in LOCATIONS.items():
        for z in (4, 8, 12):
            zxy = coordinate(lon, lat, z)
            item = requests.setdefault(zxy_to_tileid(*zxy), {"zxy": zxy, "labels": []})
            item["labels"].append(name)
    sample = output / "sample-source.pmtiles"
    if not sample.exists():
        packer = DiskPacker(sample, remote.identity)
        records = []
        progress = int(packer.get_state("progress") or -1)
        try:
            for tile_id, request in sorted(requests.items()):
                zxy = request["zxy"]
                payload = reader.get(*zxy)
                record = {
                    "zxy": zxy,
                    "labels": request["labels"],
                    "present": payload is not None,
                }
                if payload is not None:
                    values = decode_terrarium(
                        rgb_image(payload, size=512, image_format="WEBP")
                    )
                    children = list(split_tile(*zxy, payload))
                    record.update(
                        {
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "bytes": len(payload),
                            "minimumMetres": float(values.min()),
                            "maximumMetres": float(values.max()),
                            "negativePixels": int((values < 0).sum()),
                            "zeroPixels": int((values == 0).sum()),
                            "derivativeBytes": sum(len(b) for _, b in children),
                        }
                    )
                    if tile_id > progress:
                        packer.add(tile_id, payload)
                records.append(record)
                if tile_id > progress:
                    packer.checkpoint(tile_id)
            packer.finish(header, reader.metadata())
        except BaseException:
            packer.close()
            raise
        write_json(output / "samples.json", records)
    source_manifest = {
        "kind": "remote-range-sample",
        "completeSnapshot": False,
        "file": sample.name,
        "sha256": digest(sample),
        "bytes": sample.stat().st_size,
        "upstream": source_info,
        "selection": "global z0 plus fixed mountain, coast, negative-height, ocean and Mercator-edge locations at source z4, z8, z12",
    }
    write_json(output / "sample-source-manifest.json", source_manifest)
    records = json.loads((output / "samples.json").read_text())
    present = [s for s in records if s["present"]]
    estimate = round(
        sum(s["derivativeBytes"] for s in present)
        / len(present)
        * header["addressed_tiles_count"]
    )
    capacity = {
        "checkedUtc": now(),
        "durableFreeBytes": shutil.disk_usage(output).free,
        "sourceBytes": remote.identity["bytes"],
        "sourceFits": shutil.disk_usage(output).free > remote.identity["bytes"],
        "outputTilesExpected": 4 * header["addressed_tiles_count"] + 1,
        "sampleExtrapolatedPngBytes": estimate,
        "estimateCaveat": "Geographically chosen small sample; not a sizing guarantee. Reserve index, metadata, safety margin and source plus output space. No archive-sized intermediate copy is required.",
        "bulkAcquisitionStarted": False,
    }
    write_json(output / "capacity.json", capacity)
    derivative = output / "sample-derivative"
    if not (derivative / "product.json").exists():
        build(sample, output / "sample-source-manifest.json", derivative)
    return {
        "upstream": source_info,
        "capacity": capacity,
        "presentSampleTiles": len(present),
        "absentSampleTiles": len(records) - len(present),
    }


def acquire(inspection, output):
    """Resume an ETag-bound full source; promote only after published MD5 passes."""
    source = json.loads(Path(inspection).read_text())
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    partial = output / "planet.pmtiles.part"
    final = output / "planet.pmtiles"
    identity_path = output / "download-identity.json"
    identity = {key: source[key] for key in ("url", "bytes", "etag", "lastModified")}
    if final.exists():
        raise FileExistsError(
            "An immutable source already exists; verify its receipt rather than overwrite"
        )
    if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
        raise ValueError("Source identity changed since download began")
    if partial.exists() and not identity_path.exists():
        raise ValueError("Unidentified partial source")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > source["bytes"]:
        raise ValueError("Partial source too long")
    required = source["bytes"] - offset + 8 * 1024**3
    if shutil.disk_usage(output).free < required:
        raise ValueError(
            f"Insufficient durable capacity: acquisition alone requires {required} free bytes"
        )
    write_json(identity_path, identity)
    # Separate bounded requests make long downloads resumable after interruption.
    while offset < source["bytes"]:
        stop = min(source["bytes"], offset + 64 * 1024**2)
        request = urllib.request.Request(
            source["url"],
            headers={
                "Range": f"bytes={offset}-{stop - 1}",
                "If-Match": source["etag"],
                "Accept-Encoding": "identity",
                "User-Agent": "foundinspace-terrain/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            if (
                response.status != 206
                or response.headers.get("ETag") != source["etag"]
                or response.headers.get("Content-Range")
                != f"bytes {offset}-{stop - 1}/{source['bytes']}"
            ):
                raise ValueError(
                    "Server changed the snapshot or ignored the resume range"
                )
            with partial.open("ab") as stream:
                remaining = stop - offset
                while remaining:
                    block = response.read(min(1024**2, remaining))
                    if not block:
                        raise ValueError("Interrupted acquisition; rerun to resume")
                    stream.write(block)
                    remaining -= len(block)
                stream.flush()
                os_fsync(stream)
        offset = stop
    md5 = hashlib.md5(usedforsecurity=False)
    sha = hashlib.sha256()
    with partial.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            md5.update(block)
            sha.update(block)
    if md5.hexdigest() != source["published"]["md5sum"]:
        raise ValueError(
            "Downloaded source differs from the published MD5; not promoted"
        )
    with local_archive(partial) as reader:
        admit_source(reader)
        structure = check_structure(reader, source["bytes"])
    manifest = {
        "kind": "complete-snapshot",
        "completeSnapshot": True,
        "file": final.name,
        "bytes": source["bytes"],
        "sha256": sha.hexdigest(),
        "md5": md5.hexdigest(),
        "retrievedUtc": now(),
        "upstream": source,
        "structure": structure,
    }
    write_json(output / "source-manifest.json", manifest)
    partial.replace(final)
    final.chmod(0o444)
    return manifest


def os_fsync(stream):
    import os

    os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspection = commands.add_parser("probe")
    inspection.add_argument("--output", type=Path, required=True)
    acquisition = commands.add_parser("acquire")
    acquisition.add_argument("inspection", type=Path)
    acquisition.add_argument("--output", type=Path, required=True)
    creation = commands.add_parser("build")
    creation.add_argument("source", type=Path)
    creation.add_argument("manifest", type=Path)
    creation.add_argument("--output", type=Path, required=True)
    creation.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="CPU conversion processes (default: 2); 1 runs inline without a pool",
    )
    creation.add_argument(
        "--max-in-flight",
        type=int,
        help="Maximum source tiles submitted but not written, including completed results (default: 2 * workers, or 1 inline)",
    )
    checking = commands.add_parser("validate")
    checking.add_argument("source", type=Path)
    checking.add_argument("archive", type=Path)
    checking.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    start = time.monotonic()
    if args.command == "probe":
        result = probe(args.output)
    elif args.command == "acquire":
        result = acquire(args.inspection, args.output)
    elif args.command == "build":
        result = build(
            args.source,
            args.manifest,
            args.output,
            workers=args.workers,
            max_in_flight=args.max_in_flight,
        )
    else:
        result = validate(args.source, args.archive)
        write_json(args.output, result)
    print(
        json.dumps(
            {"result": result, "elapsedSeconds": round(time.monotonic() - start, 3)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
