from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, BinaryIO

from PIL import Image

from .processing import ProcessingError, _validate_display_bands
from .project import TextureProject
from .provenance import producer_identity
from .toolchain import gdalinfo, require_tools, run_command, tool_versions
from .util import atomic_write_json, identity_sha256, sha256_file, utc_now

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TILE_NAME = re.compile(r"_(\d+)_(\d+)\.png$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PNG_ZLEVEL = 6


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProcessingError(f"JSON object expected in {path}")
    return value


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _tile_path(project: TextureProject, zoom: int, x: int, y: int) -> Path:
    return Path(project.product.path_template.format(z=zoom, x=x, y=y))


def _inspect_png(path: Path, *, tile_size: int, channels="grayscale") -> dict[str, Any]:
    payload = path.read_bytes()
    if not payload.startswith(_PNG_SIGNATURE):
        raise ProcessingError(f"tile is not a PNG: {path}")
    position = len(_PNG_SIGNATURE)
    chunks: list[str] = []
    width = height = bit_depth = color_type = interlace = None
    saw_iend = False
    while position < len(payload):
        if position + 12 > len(payload):
            raise ProcessingError(f"truncated PNG chunk in {path}")
        length = struct.unpack(">I", payload[position : position + 4])[0]
        chunk_type = payload[position + 4 : position + 8]
        data_start = position + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if crc_end > len(payload):
            raise ProcessingError(f"truncated PNG chunk data in {path}")
        chunk_data = payload[data_start:data_end]
        expected_crc = struct.unpack(">I", payload[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ProcessingError(f"PNG chunk CRC mismatch in {path}")
        try:
            chunk_name = chunk_type.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ProcessingError(f"invalid PNG chunk type in {path}") from exc
        chunks.append(chunk_name)
        if chunk_name == "IHDR":
            if width is not None or length != 13:
                raise ProcessingError(f"invalid PNG IHDR in {path}")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", chunk_data)
            if compression != 0 or filter_method != 0:
                raise ProcessingError(f"unsupported PNG encoding in {path}")
        elif chunk_name == "tRNS":
            raise ProcessingError(f"opaque tile contains PNG transparency: {path}")
        elif chunk_name == "IEND":
            if length != 0:
                raise ProcessingError(f"invalid PNG IEND in {path}")
            saw_iend = True
            position = crc_end
            break
        position = crc_end
    if position != len(payload) or not saw_iend:
        raise ProcessingError(f"invalid PNG termination in {path}")
    if not chunks or chunks[0] != "IHDR" or "IDAT" not in chunks:
        raise ProcessingError(f"PNG is missing required chunks: {path}")
    if (width, height) != (tile_size, tile_size):
        raise ProcessingError(f"tile must be {tile_size} x {tile_size} pixels: {path}")
    expected_color_type = 2 if channels == "rgb" else 0
    if (bit_depth, color_type, interlace) != (8, expected_color_type, 0):
        raise ProcessingError(
            f"tile must be non-interlaced opaque 8-bit {channels}: {path}"
        )
    if "PLTE" in chunks:
        raise ProcessingError(f"display tile must not contain a palette: {path}")
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "chunks": chunks,
    }


def _validate_level_raster(
    metadata: dict[str, Any], dimension: int, path: Path, channels="grayscale"
) -> None:
    if metadata.get("size") != [dimension, dimension]:
        raise ProcessingError(f"level raster has unexpected dimensions: {path}")
    _validate_display_bands(metadata, channels)


def _level_inputs(project: TextureProject, work_root: Path) -> list[dict[str, Any]]:
    levels: list[dict[str, Any]] = []
    for zoom in range(project.product.maximum_zoom + 1):
        path = work_root / "levels" / f"{zoom}.tif"
        if not path.is_file():
            raise ProcessingError(f"level raster does not exist: {path}")
        dimension = project.product.tile_size * (1 << zoom)
        metadata, _ = gdalinfo(path)
        _validate_level_raster(metadata, dimension, path, project.product.channels)
        levels.append(
            {
                "zoom": zoom,
                "path": path,
                "dimension": dimension,
                "tile_count": 4**zoom,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return levels


def _completed_emission(
    state: dict[str, Any] | None,
    *,
    identity_hash: str,
    output_root: Path,
) -> bool:
    if not state or state.get("status") != "complete":
        return False
    if state.get("identity_sha256") != identity_hash:
        return False
    product = output_root / "product.json"
    expected = state.get("output", {}).get("product_json_sha256")
    return product.is_file() and sha256_file(product) == expected


def _marker_matches(path: Path, marker_identity: str) -> dict[str, Any] | None:
    marker = _read_json(path)
    if (
        marker
        and marker.get("status") == "complete"
        and marker.get("identity_sha256") == marker_identity
    ):
        return marker
    return None


def _retile_level(
    project: TextureProject,
    *,
    level: dict[str, Any],
    staging: Path,
    environment: dict[str, str],
    stage_identity_hash: str,
) -> dict[str, Any]:
    zoom = level["zoom"]
    matrix = 1 << zoom
    marker_identity = identity_sha256(
        {
            "emit_identity_sha256": stage_identity_hash,
            "zoom": zoom,
            "level_sha256": level["sha256"],
        }
    )
    marker_path = staging / ".state" / f"zoom-{zoom}.json"
    marker = _marker_matches(marker_path, marker_identity)
    if marker is not None:
        manifest = staging / marker["manifest_path"]
        if manifest.is_file() and sha256_file(manifest) == marker["manifest_sha256"]:
            return marker

    local_work = staging / ".work"
    local_work.mkdir(parents=True, exist_ok=True)
    vrt = local_work / f"level-{zoom}.vrt"
    raw_tiles = local_work / f"retile-{zoom}"
    raw_tiles.mkdir(parents=True, exist_ok=True)
    vrt_command = (
        "gdal_translate",
        "-q",
        "-of",
        "VRT",
        "-a_nodata",
        "none",
        str(level["path"]),
        str(vrt),
    )
    run_command(vrt_command, env=environment)
    retile_command = (
        "gdal_retile.py",
        "-q",
        "-resume",
        "-of",
        "PNG",
        "-ot",
        "Byte",
        "-ps",
        str(project.product.tile_size),
        str(project.product.tile_size),
        "-overlap",
        "0",
        "-co",
        f"ZLEVEL={_PNG_ZLEVEL}",
        "-useDirForEachRow",
        "-targetDir",
        str(raw_tiles),
        str(vrt),
    )
    run_command(retile_command, env=environment)

    discovered: dict[tuple[int, int], Path] = {}
    for path in raw_tiles.rglob("*.png"):
        match = _TILE_NAME.search(path.name)
        if match is None:
            raise ProcessingError(f"unrecognized gdal_retile output name: {path}")
        y = int(match.group(1)) - 1
        x = int(match.group(2)) - 1
        if not (0 <= x < matrix and 0 <= y < matrix):
            raise ProcessingError(f"tile coordinates out of range: {path}")
        if (x, y) in discovered:
            raise ProcessingError(f"duplicate tile coordinates at z{zoom}: {x}/{y}")
        discovered[(x, y)] = path
    if len(discovered) != level["tile_count"]:
        raise ProcessingError(
            f"z{zoom} retile count mismatch: expected {level['tile_count']}, "
            f"got {len(discovered)}"
        )

    manifest_lines: list[str] = []
    tile_bytes = 0
    for x in range(matrix):
        for y in range(matrix):
            source = discovered[(x, y)]
            details = _inspect_png(
                source,
                tile_size=project.product.tile_size,
                channels=project.product.channels,
            )
            relative = _tile_path(project, zoom, x, y)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                existing = _inspect_png(
                    destination,
                    tile_size=project.product.tile_size,
                    channels=project.product.channels,
                )
                if existing["sha256"] != details["sha256"]:
                    raise ProcessingError(
                        f"resumed tile differs from generated tile: {relative}"
                    )
                source.unlink()
                details = existing
            else:
                source.replace(destination)
            tile_bytes += details["size_bytes"]
            manifest_lines.append(f"{details['sha256']}  {relative.as_posix()}\n")

    manifest_path = staging / "manifests" / f"tiles-z{zoom}.sha256"
    _atomic_write_text(manifest_path, "".join(manifest_lines))
    shutil.rmtree(raw_tiles)
    vrt.unlink(missing_ok=True)
    marker = {
        "format_version": 1,
        "stage": f"emit-tiles-z{zoom}",
        "status": "complete",
        "completed_at": utc_now(),
        "identity_sha256": marker_identity,
        "zoom": zoom,
        "dimension": level["dimension"],
        "level_raster_sha256": level["sha256"],
        "tile_count": level["tile_count"],
        "tile_bytes": tile_bytes,
        "manifest_path": manifest_path.relative_to(staging).as_posix(),
        "manifest_sha256": sha256_file(manifest_path),
        "commands": [list(vrt_command), list(retile_command)],
    }
    atomic_write_json(marker_path, marker)
    return marker


def emit_tiles(
    project: TextureProject, *, work_root: Path, output_root: Path
) -> dict[str, Any]:
    work_root = work_root.resolve()
    output_root = output_root.resolve()
    tools = require_tools(("gdal_translate", "gdal_retile.py", "gdalinfo"))
    versions = tool_versions()
    versions["gdal_retile.py_sha256"] = sha256_file(Path(tools["gdal_retile.py"]))
    levels = _level_inputs(project, work_root)
    producer = producer_identity()
    identity = {
        "project": project.semantic_dict(),
        "levels": [
            {
                key: value
                for key, value in level.items()
                if key in {"zoom", "dimension", "tile_count", "size_bytes", "sha256"}
            }
            for level in levels
        ],
        "png": {
            "bit_depth": 8,
            "color_type": 2 if project.product.channels == "rgb" else 0,
            "zlevel": _PNG_ZLEVEL,
            "opaque": True,
        },
        "producer": producer,
        "tools": versions,
    }
    identity_hash = identity_sha256(identity)
    state_path = work_root / "state" / "emit-tiles.json"
    prior_state = _read_json(state_path)
    if _completed_emission(
        prior_state, identity_hash=identity_hash, output_root=output_root
    ):
        return prior_state  # type: ignore[return-value]
    if output_root.exists() and any(output_root.iterdir()):
        raise ProcessingError(
            f"output root is not empty and has no matching completed stage: {output_root}"
        )

    staging = output_root.parent / (
        f".{output_root.name}.emit-tiles-{identity_hash[:16]}.tmp"
    )
    staging.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "format_version": 1,
        "stage": "emit-tiles",
        "status": "running",
        "started_at": utc_now(),
        "identity": identity,
        "identity_sha256": identity_hash,
        "staging": str(staging),
        "levels": [],
    }
    atomic_write_json(state_path, state)
    started = time.monotonic()
    environment = {
        "CPL_TMPDIR": str(work_root / "tmp"),
        "GDAL_PAM_ENABLED": "NO",
    }
    try:
        level_outputs: list[dict[str, Any]] = []
        for level in levels:
            summary = _retile_level(
                project,
                level=level,
                staging=staging,
                environment=environment,
                stage_identity_hash=identity_hash,
            )
            level_outputs.append(summary)
            state["levels"] = level_outputs
            atomic_write_json(state_path, state)

        tile_files = list((staging / "imagery").rglob("*.png"))
        if len(tile_files) != project.product.total_tile_count:
            raise ProcessingError(
                "complete tile tree count mismatch: "
                f"expected {project.product.total_tile_count}, got {len(tile_files)}"
            )
        manifest_set_sha256 = identity_sha256(
            [
                {
                    "zoom": item["zoom"],
                    "manifest_sha256": item["manifest_sha256"],
                }
                for item in level_outputs
            ]
        )
        base_texture = None
        if project.base_texture:
            config = project.base_texture
            base = work_root / config.path
            with Image.open(base) as image:
                if (
                    image.mode != project.product.image_mode
                    or image.size != (config.width, config.height)
                    or "transparency" in image.info
                ):
                    raise ProcessingError(
                        "Base texture dimensions or opaque display encoding changed"
                    )
            shutil.copyfile(base, staging / config.path)
            base_texture = {
                "path": config.path,
                "projection": "equirectangular",
                "width": config.width,
                "height": config.height,
                "sha256": sha256_file(base),
            }
        product_identity = identity_sha256(
            {
                "project": project.semantic_dict(),
                "source_sha256": [
                    source.acquisition.expected.sha256 for source in project.sources
                ],
                "target_crs_sha256": sha256_file(project.target.crs_path),
                "producer": producer,
                "tools": versions,
                "manifest_set_sha256": manifest_set_sha256,
                **({"base_texture": base_texture} if base_texture else {}),
            }
        )
        product = {
            "format_version": project.format_version,
            "product_id": project.product_id,
            "product_identity_sha256": product_identity,
            "created_at": utc_now(),
            "project_identity_sha256": identity_sha256(project.semantic_dict()),
            "target": {
                "id": project.target.id,
                "bounds": project.product.bounds,
                "crs_sha256": sha256_file(project.target.crs_path),
                "axis_conventions": {
                    "x": "eastward longitude",
                    "y": "southward latitude",
                },
            },
            "tiles": {
                "addressing": "XYZ",
                "matrix": "2^z x 2^z",
                "minimum_zoom": 0,
                "maximum_zoom": project.product.maximum_zoom,
                "tile_size": project.product.tile_size,
                "path_template": project.product.path_template,
                "pixel_type": project.product.pixel_type,
                "channels": project.product.channels,
                "format": project.product.format,
                "opaque": project.product.opaque,
                "missing_pixel": project.missing_pixel,
                "total_count": project.product.total_tile_count,
                "total_bytes": sum(item["tile_bytes"] for item in level_outputs),
            },
            "processing": {
                "reprojection_resampling": project.product.reprojection_resampling,
                "pyramid_resampling": project.product.pyramid_resampling,
                "lower_levels_derived_directly_from_maximum": True,
                "tile_emission_resampling": "none",
                "png_zlevel": _PNG_ZLEVEL,
            },
            "sources": [
                {
                    "id": source.id,
                    "sha256": source.acquisition.expected.sha256,
                    "size_bytes": source.acquisition.expected.size_bytes,
                    "title": source.acquisition.title,
                    "landing_page": source.acquisition.landing_page,
                    "url": source.acquisition.url,
                    "citation": source.acquisition.citation,
                    **(
                        {"license": source.acquisition.license}
                        if source.acquisition.license
                        else {}
                    ),
                    **(
                        {"license_url": source.acquisition.license_url}
                        if source.acquisition.license_url
                        else {}
                    ),
                }
                for source in project.sources
            ],
            "levels": [
                {
                    "zoom": item["zoom"],
                    "dimension": item["dimension"],
                    "tile_count": item["tile_count"],
                    "tile_bytes": item["tile_bytes"],
                    "level_raster_sha256": item["level_raster_sha256"],
                    "manifest": item["manifest_path"],
                    "manifest_sha256": item["manifest_sha256"],
                }
                for item in level_outputs
            ],
            "manifest_set_sha256": manifest_set_sha256,
            "producer": producer,
            "tools": versions,
        }
        if project.grid:
            product["grid"] = project.grid.metadata()
            product["baseTexture"] = base_texture
        product_path = staging / "product.json"
        atomic_write_json(product_path, product)
        shutil.rmtree(staging / ".state")
        shutil.rmtree(staging / ".work", ignore_errors=True)
        if output_root.exists():
            output_root.rmdir()
        staging.replace(output_root)
        state.update(
            status="complete",
            completed_at=utc_now(),
            elapsed_seconds=time.monotonic() - started,
            output={
                "root": str(output_root),
                "product_json_sha256": sha256_file(output_root / "product.json"),
                "product_identity_sha256": product_identity,
                "tile_count": project.product.total_tile_count,
                "tile_bytes": product["tiles"]["total_bytes"],
                "manifest_set_sha256": manifest_set_sha256,
            },
        )
        state.pop("staging", None)
        atomic_write_json(state_path, state)
        return state
    except Exception as exc:
        state.update(
            status="failed",
            completed_at=utc_now(),
            elapsed_seconds=time.monotonic() - started,
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        atomic_write_json(state_path, state)
        raise


def _manifest_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        parts = line.split("  ", 1)
        if len(parts) != 2 or not _SHA256.fullmatch(parts[0]):
            raise ProcessingError(f"invalid manifest line {line_number} in {path}")
        checksum, relative = parts
        if relative in entries:
            raise ProcessingError(f"duplicate manifest path in {path}: {relative}")
        entries[relative] = checksum
    return entries


def _read_pgm_token(stream: BinaryIO) -> bytes:
    token = bytearray()
    while True:
        byte = stream.read(1)
        if not byte:
            raise ProcessingError("unexpected end of PGM header")
        if byte == b"#":
            while byte not in {b"", b"\n"}:
                byte = stream.read(1)
            continue
        if not byte.isspace():
            token.extend(byte)
            break
    while True:
        byte = stream.read(1)
        if not byte or byte.isspace():
            return bytes(token)
        token.extend(byte)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProcessingError("unexpected end of PGM pixel data")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _decode_opaque_tile(
    path: Path, tile_size: int, channels="grayscale"
) -> tuple[bytes, str, int]:
    details = _inspect_png(path, tile_size=tile_size, channels=channels)
    bands = 3 if channels == "rgb" else 1
    mode = "RGB" if channels == "rgb" else "L"
    with Image.open(path) as image:
        if image.format != "PNG" or image.mode != mode:
            raise ProcessingError(f"tile must decode as {channels} PNG: {path}")
        if image.size != (tile_size, tile_size):
            raise ProcessingError(f"decoded tile dimensions are invalid: {path}")
        if "transparency" in image.info:
            raise ProcessingError(f"decoded tile declares transparency: {path}")
        image.load()
        pixels = image.tobytes()
    if len(pixels) != tile_size * tile_size * bands:
        raise ProcessingError(f"decoded tile byte count is invalid: {path}")
    return pixels, details["sha256"], details["size_bytes"]


def _validate_level_tiles(
    project: TextureProject,
    *,
    level: dict[str, Any],
    output_root: Path,
    work_root: Path,
    product_level: dict[str, Any],
) -> dict[str, Any]:
    zoom = level["zoom"]
    matrix = 1 << zoom
    tile_size = project.product.tile_size
    manifest_path = output_root / product_level["manifest"]
    if sha256_file(manifest_path) != product_level["manifest_sha256"]:
        raise ProcessingError(f"z{zoom} manifest checksum mismatch")
    manifest = _manifest_entries(manifest_path)
    expected_paths = {
        _tile_path(project, zoom, x, y).as_posix()
        for x in range(matrix)
        for y in range(matrix)
    }
    if set(manifest) != expected_paths:
        missing = len(expected_paths - set(manifest))
        extra = len(set(manifest) - expected_paths)
        raise ProcessingError(
            f"z{zoom} manifest paths mismatch: missing={missing}, extra={extra}"
        )
    level_root = output_root / "imagery" / str(zoom)
    actual_paths = {
        path.relative_to(output_root).as_posix()
        for path in level_root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        missing = len(expected_paths - actual_paths)
        extra = len(actual_paths - expected_paths)
        raise ProcessingError(
            f"z{zoom} tile paths mismatch: missing={missing}, extra={extra}"
        )

    bands = project.product.band_count
    required_space = level["dimension"] ** 2 * bands + 1_000_000_000
    available = shutil.disk_usage(work_root).free
    if available < required_space:
        raise ProcessingError(
            f"insufficient temporary space to validate z{zoom}: "
            f"need {required_space}, have {available}"
        )
    pgm = work_root / "tmp" / f"validate-z{zoom}-{uuid.uuid4().hex}.pgm"
    pgm.parent.mkdir(parents=True, exist_ok=True)
    command = (
        "gdal_translate",
        "-q",
        "-of",
        "PNM",
        "-ot",
        "Byte",
        "-a_nodata",
        "none",
        str(level["path"]),
        str(pgm),
    )
    try:
        run_command(
            command,
            env={"CPL_TMPDIR": str(work_root / "tmp"), "GDAL_PAM_ENABLED": "NO"},
        )
        expected_digest = hashlib.sha256()
        actual_digest = hashlib.sha256()
        tile_bytes = 0
        with pgm.open("rb") as source_pixels:
            if _read_pgm_token(source_pixels) != (b"P6" if bands == 3 else b"P5"):
                raise ProcessingError(
                    f"validation raster has the wrong binary PNM encoding: {pgm}"
                )
            width = int(_read_pgm_token(source_pixels))
            height = int(_read_pgm_token(source_pixels))
            maximum = int(_read_pgm_token(source_pixels))
            if (width, height, maximum) != (
                level["dimension"],
                level["dimension"],
                255,
            ):
                raise ProcessingError(f"validation PGM header mismatch at z{zoom}")
            for tile_y in range(matrix):
                tile_row: list[bytes] = []
                for tile_x in range(matrix):
                    relative = _tile_path(project, zoom, tile_x, tile_y)
                    tile = output_root / relative
                    pixels, checksum, size_bytes = _decode_opaque_tile(
                        tile, tile_size, project.product.channels
                    )
                    if checksum != manifest[relative.as_posix()]:
                        raise ProcessingError(f"tile checksum mismatch: {relative}")
                    tile_row.append(pixels)
                    tile_bytes += size_bytes
                for local_y in range(tile_size):
                    start = local_y * tile_size * bands
                    actual = b"".join(
                        pixels[start : start + tile_size * bands] for pixels in tile_row
                    )
                    expected = _read_exact(source_pixels, level["dimension"] * bands)
                    actual_digest.update(actual)
                    expected_digest.update(expected)
                    if actual != expected:
                        raise ProcessingError(
                            f"tile reconstruction differs from z{zoom} raster at "
                            f"pixel row {tile_y * tile_size + local_y}"
                        )
            if source_pixels.read(1):
                raise ProcessingError(f"validation PGM has trailing pixel data: {pgm}")
        if tile_bytes != product_level["tile_bytes"]:
            raise ProcessingError(f"z{zoom} tile byte total mismatch")
        if actual_digest.hexdigest() != expected_digest.hexdigest():
            raise ProcessingError(f"z{zoom} reconstructed pixel checksum mismatch")
        return {
            "zoom": zoom,
            "dimension": level["dimension"],
            "tile_count": level["tile_count"],
            "tile_bytes": tile_bytes,
            "manifest_sha256": product_level["manifest_sha256"],
            "pixel_sha256": actual_digest.hexdigest(),
            f"all_tiles_opaque_{project.product.channels}": True,
            "exact_streaming_reconstruction": True,
            "horizontal_adjacencies_covered": matrix * (matrix - 1),
            "vertical_adjacencies_covered": matrix * (matrix - 1),
            "antimeridian_wrap_rows_covered": matrix,
            "north_pole_tiles_covered": matrix,
            "south_pole_tiles_covered": matrix,
            "source_export_command": list(command),
        }
    finally:
        pgm.unlink(missing_ok=True)


def validate_tiles(
    project: TextureProject,
    *,
    work_root: Path,
    output_root: Path,
    evidence_root: Path,
) -> dict[str, Any]:
    work_root = work_root.resolve()
    output_root = output_root.resolve()
    evidence_root = evidence_root.resolve()
    require_tools(("gdal_translate", "gdalinfo"))
    product_path = output_root / "product.json"
    product = _read_json(product_path)
    if product is None:
        raise ProcessingError(f"product metadata does not exist: {product_path}")
    if product.get("product_id") != project.product_id:
        raise ProcessingError("product metadata does not match the project")
    if product.get("project_identity_sha256") != identity_sha256(
        project.semantic_dict()
    ):
        raise ProcessingError("product project identity does not match")
    levels = _level_inputs(project, work_root)
    identity = {
        "project": project.semantic_dict(),
        "product_json_sha256": sha256_file(product_path),
        "levels": [
            {"zoom": level["zoom"], "sha256": level["sha256"]} for level in levels
        ],
        "producer": producer_identity(),
        "tools": tool_versions(),
    }
    if project.base_texture:
        identity["base_texture_sha256"] = sha256_file(
            output_root / project.base_texture.path
        )
    identity_hash = identity_sha256(identity)
    state_path = work_root / "state" / "validate-tiles.json"
    report_path = evidence_root / "tiles.validation.json"
    # A matching descriptor and old report cannot authenticate current tile
    # bytes. Explicit validation always reads the payload, including on resume.

    state: dict[str, Any] = {
        "format_version": 1,
        "stage": "validate-tiles",
        "status": "running",
        "started_at": utc_now(),
        "identity": identity,
        "identity_sha256": identity_hash,
        "levels": [],
    }
    atomic_write_json(state_path, state)
    started = time.monotonic()
    try:
        if product.get("format_version") != project.format_version:
            raise ProcessingError("Product format version does not match its project")
        if project.grid:
            if product.get("grid") != project.grid.metadata():
                raise ProcessingError("Product grid does not match its project")
            base = product.get("baseTexture", {})
            config = project.base_texture
            path = output_root / config.path
            if (
                base.get("projection") != "equirectangular"
                or base.get("path") != config.path
                or base.get("width") != config.width
                or base.get("height") != config.height
                or base.get("sha256") != sha256_file(path)
            ):
                raise ProcessingError("Base texture metadata or checksum changed")
            if sha256_file(path) != sha256_file(work_root / config.path):
                raise ProcessingError("Base texture differs from its recorded build")
            with Image.open(path) as image:
                if (
                    image.mode != project.product.image_mode
                    or image.size != (config.width, config.height)
                    or "transparency" in image.info
                ):
                    raise ProcessingError("Base texture dimensions or encoding changed")
        product_levels_raw = product.get("levels")
        if not isinstance(product_levels_raw, list):
            raise ProcessingError("product metadata levels must be a list")
        product_levels = {
            item.get("zoom"): item
            for item in product_levels_raw
            if isinstance(item, dict)
        }
        level_reports: list[dict[str, Any]] = []
        for level in levels:
            product_level = product_levels.get(level["zoom"])
            if not isinstance(product_level, dict):
                raise ProcessingError(f"product metadata is missing z{level['zoom']}")
            report = _validate_level_tiles(
                project,
                level=level,
                output_root=output_root,
                work_root=work_root,
                product_level=product_level,
            )
            level_reports.append(report)
            state["levels"] = level_reports
            atomic_write_json(state_path, state)

        zoom_directories = {
            path.name for path in (output_root / "imagery").iterdir() if path.is_dir()
        }
        expected_zooms = {str(zoom) for zoom in range(project.product.maximum_zoom + 1)}
        if zoom_directories != expected_zooms:
            raise ProcessingError("imagery zoom directory set is invalid")
        report = {
            "format_version": 1,
            "product_id": project.product_id,
            "validated_at": utc_now(),
            "valid": True,
            "product_identity_sha256": product["product_identity_sha256"],
            "product_json_sha256": sha256_file(product_path),
            "tile_count": sum(item["tile_count"] for item in level_reports),
            "tile_bytes": sum(item["tile_bytes"] for item in level_reports),
            "levels": level_reports,
            "contract": {
                "addressing": "XYZ",
                "x_increases_eastward": True,
                "y_increases_southward": True,
                "tile_size": project.product.tile_size,
                "opaque": True,
                "pixel_type": "uint8",
                "channels": project.product.channels,
                "missing_pixel": project.missing_pixel,
                "bounds": project.product.bounds,
            },
            "checks": {
                "all_expected_paths_present": True,
                "no_unexpected_tile_files": True,
                "all_png_chunks_crc_valid": True,
                "no_alpha_palette_or_transparency_chunks": True,
                "all_tile_checksums_match_manifests": True,
                "every_level_reconstructs_exactly": True,
                "poles_antimeridian_and_adjacent_edges_covered": True,
            },
            "producer": identity["producer"],
            "tools": identity["tools"],
        }
        if project.grid:
            report["grid"] = project.grid.metadata()
            report["baseTexture"] = product["baseTexture"]
            report["checks"].pop("poles_antimeridian_and_adjacent_edges_covered")
            report["checks"]["grid_antimeridian_and_adjacent_edges_covered"] = True
            report["checks"]["base_texture_covers_poles"] = True
        atomic_write_json(report_path, report)
        state.update(
            status="complete",
            completed_at=utc_now(),
            elapsed_seconds=time.monotonic() - started,
            report=str(report_path),
            report_sha256=sha256_file(report_path),
        )
        atomic_write_json(state_path, state)
        return report
    except Exception as exc:
        state.update(
            status="failed",
            completed_at=utc_now(),
            elapsed_seconds=time.monotonic() - started,
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        atomic_write_json(state_path, state)
        raise
