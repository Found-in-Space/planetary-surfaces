from __future__ import annotations

import math
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .grid import TileGrid
from .project import AcquisitionSpec, SourceRef, TextureProject
from .provenance import producer_identity
from .toolchain import (
    CommandResult,
    gdalinfo,
    require_tools,
    run_command,
    spatial_reference_proj4,
    tool_versions,
)
from .util import atomic_write_json, identity_sha256, sha256_file, utc_now


class ProcessingError(RuntimeError):
    """Raised when a controlled processing contract cannot be satisfied."""


def _relative_or_absolute(path: Path) -> str:
    return str(path.resolve())


def _source_observation(
    spec: AcquisitionSpec,
    source_path: Path,
    metadata: dict[str, Any],
    *,
    size: int,
    checksum: str,
) -> tuple[dict[str, Any], list[str], list[list[str]]]:
    errors: list[str] = []
    expected = spec.expected
    if expected.size_bytes is not None and size != expected.size_bytes:
        errors.append(f"size_bytes: expected {expected.size_bytes}, got {size}")
    if expected.sha256 is not None and checksum != expected.sha256:
        errors.append(f"sha256: expected {expected.sha256}, got {checksum}")
    raster_size = metadata.get("size")
    if not isinstance(raster_size, list) or len(raster_size) != 2:
        errors.append("gdalinfo did not return a two-dimensional raster size")
        width = height = None
    else:
        width, height = raster_size
    if expected.width is not None and width != expected.width:
        errors.append(f"width: expected {expected.width}, got {width}")
    if expected.height is not None and height != expected.height:
        errors.append(f"height: expected {expected.height}, got {height}")
    bands = metadata.get("bands")
    band_count = len(bands) if isinstance(bands, list) else None
    if expected.band_count is not None and band_count != expected.band_count:
        errors.append(f"band_count: expected {expected.band_count}, got {band_count}")
    data_types = (
        sorted({band.get("type") for band in bands if isinstance(band, dict)})
        if isinstance(bands, list)
        else []
    )
    if expected.data_type is not None and data_types != [expected.data_type]:
        errors.append(f"data_type: expected {expected.data_type}, got {data_types}")
    nodata_values = (
        [band.get("noDataValue", "absent") for band in bands if isinstance(band, dict)]
        if isinstance(bands, list)
        else []
    )
    mask_flags = (
        [
            sorted(band.get("mask", {}).get("flags", ["ALL_VALID"]))
            for band in bands
            if isinstance(band, dict)
        ]
        if isinstance(bands, list)
        else []
    )
    scales = (
        [band.get("scale", "absent") for band in bands if isinstance(band, dict)]
        if isinstance(bands, list)
        else []
    )
    offsets = (
        [band.get("offset", "absent") for band in bands if isinstance(band, dict)]
        if isinstance(bands, list)
        else []
    )
    geo_transform = metadata.get("geoTransform")
    if expected.geo_transform is not None and geo_transform != list(
        expected.geo_transform
    ):
        errors.append(
            f"geo_transform: expected {list(expected.geo_transform)}, got {geo_transform}"
        )
    if expected.nodata is not None and nodata_values != [expected.nodata] * (
        band_count or 0
    ):
        errors.append(f"nodata: expected {expected.nodata}, got {nodata_values}")
    expected_mask = (
        list(expected.mask_flags) if expected.mask_flags is not None else None
    )
    if expected_mask is not None and mask_flags != [expected_mask] * (band_count or 0):
        errors.append(f"mask_flags: expected {expected_mask}, got {mask_flags}")
    if expected.scale is not None and scales != [expected.scale] * (band_count or 0):
        errors.append(f"scale: expected {expected.scale}, got {scales}")
    if expected.offset is not None and offsets != [expected.offset] * (band_count or 0):
        errors.append(f"offset: expected {expected.offset}, got {offsets}")

    srs_commands: list[list[str]] = []
    source_srs = None
    declared_srs = None
    if spec.source_crs is not None:
        declared_srs, declared_command = spatial_reference_proj4(spec.source_crs)
        source_srs, source_command = spatial_reference_proj4(source_path)
        srs_commands = [list(declared_command.argv), list(source_command.argv)]
        if source_srs != declared_srs:
            errors.append(
                "source_crs: embedded source CRS does not match the reviewed resource"
            )

    observed = {
        "width": width,
        "height": height,
        "band_count": band_count,
        "data_types": data_types,
        "geo_transform": geo_transform,
        "nodata_values": nodata_values,
        "mask_flags": mask_flags,
        "scales": scales,
        "offsets": offsets,
        "source_crs_proj4": source_srs,
        "declared_source_crs_proj4": declared_srs,
    }
    return observed, errors, srs_commands


def inspect_source(
    spec: AcquisitionSpec, source_root: Path, evidence_root: Path
) -> dict[str, Any]:
    source_path = source_root.resolve() / spec.filename
    if not source_path.is_file():
        raise ProcessingError(f"source file does not exist: {source_path}")
    metadata, command = gdalinfo(source_path)
    size = source_path.stat().st_size
    checksum = sha256_file(source_path)
    expected = spec.expected
    observed, errors, srs_commands = _source_observation(
        spec, source_path, metadata, size=size, checksum=checksum
    )

    report = {
        "format_version": 1,
        "source_id": spec.source_id,
        "source_status": spec.status,
        "inspected_at": utc_now(),
        "source": {
            "filename": spec.filename,
            "size_bytes": size,
            "sha256": checksum,
        },
        "observed": observed,
        "contract_complete": expected.complete,
        "contract_matches": not errors,
        "errors": errors,
        "gdalinfo_command": list(command.argv),
        "spatial_reference_commands": srs_commands,
        "gdalinfo": metadata,
        "tool_versions": tool_versions(),
        "producer": producer_identity(),
        "source_crs_resource_sha256": (
            sha256_file(spec.source_crs) if spec.source_crs is not None else None
        ),
    }
    output = evidence_root.resolve() / "sources" / f"{spec.source_id}.inspection.json"
    atomic_write_json(output, report)
    if errors:
        raise ProcessingError(
            "source metadata does not match its contract: " + "; ".join(errors)
        )
    return report


def _roots_overlap(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left in right.parents or right in left.parents


def preflight(
    project: TextureProject,
    *,
    source_root: Path,
    work_root: Path,
    output_root: Path,
    evidence_root: Path,
) -> dict[str, Any]:
    roots = {
        "source": source_root.resolve(),
        "work": work_root.resolve(),
        "output": output_root.resolve(),
        "evidence": evidence_root.resolve(),
    }
    pairs = list(roots.items())
    for index, (left_name, left_path) in enumerate(pairs):
        if left_path == Path(left_path.anchor):
            raise ProcessingError(f"{left_name} root must not be a filesystem root")
        for right_name, right_path in pairs[index + 1 :]:
            if _roots_overlap(left_path, right_path):
                raise ProcessingError(
                    f"{left_name} and {right_name} roots must not overlap: "
                    f"{left_path} / {right_path}"
                )
    repository_root = Path(__file__).resolve().parents[3]
    for name, root in roots.items():
        if (
            root == repository_root
            or repository_root in root.parents
            or root in repository_root.parents
        ):
            raise ProcessingError(
                f"{name} root must be outside the Git repository: {root}"
            )

    require_tools(("gdalinfo", "gdalwarp", "gdal_translate", "gdal_retile.py", "proj"))
    if len(project.sources) > 1:
        require_tools(("gdalbuildvrt",))
    validate_grid_crs(project)
    source_checks: list[dict[str, Any]] = []
    for source in project.sources:
        path = roots["source"] / source.acquisition.filename
        exists = path.is_file()
        size = path.stat().st_size if exists else None
        checksum = sha256_file(path) if exists else None
        expected = source.acquisition.expected
        identity_matches = (
            exists
            and (expected.size_bytes is None or size == expected.size_bytes)
            and (expected.sha256 is None or checksum == expected.sha256)
        )
        source_checks.append(
            {
                "id": source.id,
                "path": _relative_or_absolute(path),
                "exists": exists,
                "size_bytes": size,
                "sha256": checksum,
                "admitted": source.acquisition.status == "admitted",
                "contract_complete": source.acquisition.expected.complete,
                "identity_matches": identity_matches,
            }
        )
    dimension = project.product.maximum_dimension
    bands = project.product.band_count
    base_bytes = dimension * dimension * bands
    pyramid_bytes = sum(
        (project.product.tile_size * (1 << zoom)) ** 2 * bands
        for zoom in range(project.product.maximum_zoom + 1)
    )
    overview_bytes = (
        project.base_texture.width * project.base_texture.height * bands
        if project.base_texture
        else 0
    )
    # Work levels, encoded tiles and one base-sized temporary. Atomic raster
    # construction, retile resume and validation exports run sequentially, so
    # their temporary allocations do not need to be reserved simultaneously.
    minimum_budget = base_bytes + 2 * pyramid_bytes + 2 * overview_bytes
    work_root.parent.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(work_root.parent)
    producer = producer_identity()
    project_semantic = project.semantic_dict()
    report = {
        "format_version": 1,
        "product_id": project.product_id,
        "project": project_semantic,
        "project_identity_sha256": identity_sha256(project_semantic),
        "target_crs_sha256": sha256_file(project.target.crs_path),
        "checked_at": utc_now(),
        "roots": {name: str(path) for name, path in roots.items()},
        "sources": source_checks,
        "tool_versions": tool_versions(),
        "producer": producer,
        "estimate": {
            "maximum_dimension": dimension,
            "uncompressed_base_bytes": base_bytes,
            "uncompressed_pyramid_bytes": pyramid_bytes,
            "minimum_working_budget_bytes": minimum_budget,
            "available_bytes": disk.free,
            "disk_budget_satisfied": disk.free >= minimum_budget,
            "tile_count": project.product.total_tile_count,
        },
        "ready_for_processing": all(
            source["exists"]
            and source["admitted"]
            and source["contract_complete"]
            and source["identity_matches"]
            for source in source_checks
        )
        and disk.free >= minimum_budget,
        "release_eligible": producer.get("git_sha") is not None
        and producer.get("git_dirty") is False,
    }
    if project.grid:
        report["grid"] = project.grid.metadata()
        report["geographic_crs_sha256"] = sha256_file(
            project.target.geographic_crs_path
        )
        report["estimate"]["uncompressed_base_texture_bytes"] = overview_bytes
    atomic_write_json(roots["evidence"] / "preflight.json", report)
    return report


def _stage_state_path(work_root: Path, name: str) -> Path:
    return work_root / "state" / f"{name}.json"


def _run_atomic_file_stage(
    *,
    stage_name: str,
    identity: dict[str, Any],
    argv: list[str],
    output_path: Path,
    work_root: Path,
    environment: dict[str, str],
    validate_output: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    state_path = _stage_state_path(work_root, stage_name)
    identity_hash = identity_sha256(identity)
    if state_path.exists() and output_path.is_file():
        import json

        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            state.get("status") == "complete"
            and state.get("identity_sha256") == identity_hash
            and sha256_file(output_path) == state.get("output", {}).get("sha256")
        ):
            return state

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    effective_argv = [str(temporary) if item == "{output}" else item for item in argv]
    state: dict[str, Any] = {
        "format_version": 1,
        "stage": stage_name,
        "status": "running",
        "identity": identity,
        "identity_sha256": identity_hash,
        "started_at": utc_now(),
        "command": effective_argv,
    }
    atomic_write_json(state_path, state)
    started_monotonic = time.monotonic()
    try:
        result: CommandResult = run_command(effective_argv, env=environment)
        if not temporary.is_file():
            raise ProcessingError(f"{stage_name} did not produce {temporary}")
        output_metadata, inspect_command = gdalinfo(temporary)
        validate_output(output_metadata)
        temporary.replace(output_path)
        state.update(
            status="complete",
            completed_at=utc_now(),
            elapsed_seconds=time.monotonic() - started_monotonic,
            stdout=result.stdout,
            stderr=result.stderr,
            inspect_command=list(inspect_command.argv),
            output={
                "path": str(output_path),
                "size_bytes": output_path.stat().st_size,
                "sha256": sha256_file(output_path),
                "gdalinfo": output_metadata,
            },
        )
        atomic_write_json(state_path, state)
        return state
    except Exception as exc:
        state.update(
            status="failed",
            completed_at=utc_now(),
            elapsed_seconds=time.monotonic() - started_monotonic,
            error={"type": type(exc).__name__, "message": str(exc)},
            temporary_output=str(temporary),
        )
        atomic_write_json(state_path, state)
        temporary.unlink(missing_ok=True)
        raise


def _processing_contract(project: TextureProject) -> tuple[SourceRef, ...]:
    if project.composite_policy not in {"priority", "mosaic"}:
        raise ProcessingError(
            "the processor supports priority or compatible-raster mosaic compositing"
        )
    for source in project.sources:
        spec = source.acquisition
        if spec.status != "admitted" or not spec.expected.complete:
            raise ProcessingError(f"source {spec.source_id} has not been admitted")
        if spec.source_crs is None or not spec.source_crs.is_file():
            raise ProcessingError(
                f"source {spec.source_id} needs a reviewed source_crs file"
            )
        if (
            spec.expected.band_count != project.product.band_count
            or spec.expected.data_type != "Byte"
        ):
            raise ProcessingError(
                "Source bands must match the declared display channels"
            )
    # GDAL's mosaic precedence is last valid source wins, independent of TOML order.
    return tuple(
        sorted(project.sources, key=lambda source: (source.priority, source.id))
    )


def _validate_display_bands(metadata: dict[str, Any], channels: str) -> None:
    expected = ["Red", "Green", "Blue"] if channels == "rgb" else ["Gray"]
    bands = metadata.get("bands", [])
    if len(bands) != len(expected) or any(
        band.get("type") != "Byte"
        or band.get("colorInterpretation") not in {None, colour}
        for band, colour in zip(bands, expected, strict=True)
    ):
        raise ProcessingError(f"Raster must contain opaque Byte {channels} bands")


def _validate_level_output(
    metadata: dict[str, Any], *, dimension: int, bounds=None, channels="grayscale"
) -> None:
    if metadata.get("size") != [dimension, dimension]:
        raise ProcessingError(
            f"generated level must be {dimension} x {dimension} pixels"
        )
    _validate_display_bands(metadata, channels)
    if bounds is not None:
        west, south, east, north = bounds
        expected = [
            west,
            (east - west) / dimension,
            0,
            north,
            0,
            (south - north) / dimension,
        ]
        actual = metadata.get("geoTransform", [])
        if len(actual) != 6 or any(
            not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)
            for a, b in zip(actual, expected, strict=True)
        ):
            raise ProcessingError(
                "Generated raster geotransform does not match its grid"
            )


def validate_grid_reference(path: Path, grid: TileGrid, projection=None) -> None:
    projection = projection or (
        "merc" if grid.projection == "web-mercator" else "longlat"
    )
    value, _ = spatial_reference_proj4(path)
    parameters = dict(
        part[1:].split("=", 1)
        for part in value.split()
        if part.startswith("+") and "=" in part
    )
    a = float(parameters.get("R", parameters.get("a", "nan")))
    b = float(parameters.get("R", parameters.get("b", "nan")))
    if (
        parameters.get("proj") != projection
        or not math.isclose(a, grid.radius_metres, rel_tol=1e-12)
        or not math.isclose(b, a, rel_tol=1e-12)
    ):
        raise ProcessingError("Declared grid projection/radius does not match its CRS")
    if parameters.get("pm", "0") not in {"0", "greenwich"}:
        raise ProcessingError("XYZ requires the Greenwich prime meridian")
    if projection == "merc" and (
        parameters.get("units") != "m"
        or any(
            float(parameters.get(key, "0")) != 0
            for key in ("lon_0", "x_0", "y_0", "lat_ts")
        )
        or float(parameters.get("k", parameters.get("k_0", "1"))) != 1
    ):
        raise ProcessingError(
            "XYZ Mercator requires unit scale, metre units and zero offsets"
        )


def validate_grid_crs(project: TextureProject) -> None:
    if not project.grid:
        return
    for path, projection in [
        (
            project.target.crs_path,
            "merc" if project.grid.projection == "web-mercator" else "longlat",
        ),
        (project.target.geographic_crs_path, "longlat"),
    ]:
        validate_grid_reference(path, project.grid, projection)


def _build_base_texture(project, source_path, spec, identity, work_root, environment):
    config = project.base_texture
    geographic = project.target.geographic_crs_path
    overview = work_root / "base-equirectangular.tif"
    base_identity = {**identity, "geographic_crs_sha256": sha256_file(geographic)}
    argv = [
        "gdalwarp",
        "-overwrite",
        "-of",
        "GTiff",
        "-ot",
        "Byte",
        "-s_srs",
        str(spec.source_crs),
        "-t_srs",
        str(geographic),
        "-te",
        "-180",
        "-90",
        "180",
        "90",
        "-ts",
        str(config.width),
        str(config.height),
        "-r",
        config.resampling,
        "-dstnodata",
        "None",
        "-wo",
        f"INIT_DEST={project.missing_pixel}",
    ]
    if spec.expected.nodata != "absent":
        argv.extend(("-srcnodata", str(spec.expected.nodata)))
    argv.extend((str(source_path), "{output}"))
    _run_atomic_file_stage(
        stage_name="build-base-overview",
        identity=base_identity,
        argv=argv,
        output_path=overview,
        work_root=work_root,
        environment=environment,
        validate_output=lambda metadata: _validate_base_texture(
            metadata, config.width, config.height, project.product.channels
        ),
    )
    _run_atomic_file_stage(
        stage_name="encode-base-texture",
        identity={**base_identity, "overview_sha256": sha256_file(overview)},
        argv=[
            "gdal_translate",
            "-of",
            "PNG",
            "-a_nodata",
            "none",
            str(overview),
            "{output}",
        ],
        output_path=work_root / config.path,
        work_root=work_root,
        environment=environment,
        validate_output=lambda metadata: _validate_base_texture(
            metadata, config.width, config.height, project.product.channels
        ),
    )


def _validate_base_texture(metadata, width, height, channels="grayscale"):
    if metadata.get("size") != [width, height]:
        raise ProcessingError("Base texture dimensions changed")
    _validate_display_bands(metadata, channels)


def _prepare_sources(project, source_root, work_root, environment):
    sources = _processing_contract(project)
    records = []
    paths = []
    for source in sources:
        spec = source.acquisition
        path = source_root.resolve() / spec.filename
        metadata, command = gdalinfo(path)
        checksum = sha256_file(path)
        observed, errors, srs_commands = _source_observation(
            spec, path, metadata, size=path.stat().st_size, checksum=checksum
        )
        if errors:
            raise ProcessingError(
                "source does not match the admitted contract: " + "; ".join(errors)
            )
        _validate_display_bands(metadata, project.product.channels)
        records.append(
            {
                "id": source.id,
                "sha256": checksum,
                "observed": observed,
                "inspect_command": list(command.argv),
                "spatial_reference_commands": srs_commands,
                "source_crs_sha256": sha256_file(spec.source_crs),
            }
        )
        paths.append(path)
    first = sources[0].acquisition
    if len(paths) == 1:
        return paths[0], first, records

    reference = records[0]["observed"]
    for record in records[1:]:
        observed = record["observed"]
        if any(
            observed[key] != reference[key]
            for key in ("source_crs_proj4", "nodata_values", "scales", "offsets")
        ):
            raise ProcessingError(
                "Mosaic sources must share CRS, nodata, scale and offset"
            )
        if any(
            not math.isclose(
                observed["geo_transform"][index],
                reference["geo_transform"][index],
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
            for index in (1, 2, 4, 5)
        ):
            raise ProcessingError(
                "Mosaic sources must share pixel size and orientation"
            )
    if any(
        record["observed"]["geo_transform"][index] != 0
        for record in records
        for index in (2, 4)
    ):
        raise ProcessingError("Mosaics require north-up, unrotated source rasters")
    if project.composite_policy == "mosaic":
        # Adjacent NASA sheets may have sub-pixel georeferencing roundoff.
        # Real overlap must use the explicitly ordered priority policy.
        bounds = []
        for record in records:
            observed = record["observed"]
            west, dx, _, north, _, dy = observed["geo_transform"]
            if dx <= 0 or dy >= 0:
                raise ProcessingError(
                    "Mosaics require eastward columns and southward rows"
                )
            current = (
                west,
                north + dy * observed["height"],
                west + dx * observed["width"],
                north,
            )
            for other in bounds:
                if (
                    min(current[2], other[2]) - max(current[0], other[0]) > dx * 1e-4
                    and min(current[3], other[3]) - max(current[1], other[1])
                    > -dy * 1e-4
                ):
                    raise ProcessingError(
                        "Mosaic sources overlap; declare priority compositing"
                    )
            bounds.append(current)
    mosaic = work_root.resolve() / "sources.vrt"
    argv = ["gdalbuildvrt", "-overwrite", "-resolution", "highest"]
    if first.expected.nodata == "absent":
        argv.extend(("-srcnodata", "None", "-vrtnodata", "None"))
    argv.extend(("{output}", *(str(path) for path in paths)))

    def validate_mosaic(metadata):
        _validate_display_bands(metadata, project.product.channels)
        dependencies = {Path(path).resolve() for path in metadata.get("files", [])}
        if not {path.resolve() for path in paths} <= dependencies:
            raise ProcessingError("GDAL mosaic omitted a declared source")

    _run_atomic_file_stage(
        stage_name="mosaic-sources",
        identity={
            "project": project.semantic_dict(),
            "sources": records,
            "tools": tool_versions(),
            "producer": producer_identity(),
        },
        argv=argv,
        output_path=mosaic,
        work_root=work_root.resolve(),
        environment=environment,
        validate_output=validate_mosaic,
    )
    return mosaic, first, records


def build_base(
    project: TextureProject,
    *,
    source_root: Path,
    work_root: Path,
    threads: int = 1,
    warp_memory_mb: int = 512,
) -> dict[str, Any]:
    if threads <= 0 or warp_memory_mb <= 0:
        raise ValueError("threads and warp_memory_mb must be > 0")
    temporary_root = work_root.resolve() / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    environment = {
        "CPL_TMPDIR": str(temporary_root),
        "GDAL_CACHEMAX": str(warp_memory_mb),
    }
    source_path, spec, source_records = _prepare_sources(
        project, source_root, work_root, environment
    )
    target_crs_checksum = sha256_file(project.target.crs_path)
    source_crs_checksum = sha256_file(spec.source_crs)
    validate_grid_crs(project)
    dimension = project.product.maximum_dimension
    west, south, east, north = project.projected_bounds
    output = work_root.resolve() / "levels" / f"{project.product.maximum_zoom}.tif"
    argv = [
        "gdalwarp",
        "-overwrite",
        "-of",
        "GTiff",
        "-ot",
        "Byte",
        "-s_srs",
        str(spec.source_crs),
        "-t_srs",
        str(project.target.crs_path),
        "-te_srs",
        str(project.target.crs_path),
        "-te",
        str(west),
        str(south),
        str(east),
        str(north),
        "-ts",
        str(dimension),
        str(dimension),
        "-r",
        project.product.reprojection_resampling,
        "-dstnodata",
        "None",
        "-wo",
        f"INIT_DEST={project.missing_pixel}",
        "-wm",
        str(warp_memory_mb),
        "-wo",
        f"NUM_THREADS={threads}",
        "-co",
        "BIGTIFF=YES",
        "-co",
        "TILED=YES",
        "-co",
        f"BLOCKXSIZE={project.product.tile_size}",
        "-co",
        f"BLOCKYSIZE={project.product.tile_size}",
        "-co",
        "COMPRESS=DEFLATE",
        "-co",
        "PREDICTOR=2",
    ]
    if spec.expected.nodata != "absent":
        argv.extend(("-srcnodata", str(spec.expected.nodata)))
    argv.extend((str(source_path), "{output}"))
    identity = {
        "project": project.semantic_dict(),
        "sources": source_records,
        "source_crs_sha256": source_crs_checksum,
        "target_crs_sha256": target_crs_checksum,
        "tools": tool_versions(),
        "producer": producer_identity(),
        "threads": threads,
        "warp_memory_mb": warp_memory_mb,
    }
    state = _run_atomic_file_stage(
        stage_name="build-base",
        identity=identity,
        argv=argv,
        output_path=output,
        work_root=work_root.resolve(),
        environment=environment,
        validate_output=lambda metadata: _validate_level_output(
            metadata,
            dimension=dimension,
            bounds=project.projected_bounds if project.grid else None,
            channels=project.product.channels,
        ),
    )

    if project.base_texture:
        _build_base_texture(
            project,
            source_path,
            spec,
            identity,
            work_root.resolve(),
            {**environment, "GDAL_PAM_ENABLED": "NO"},
        )
    return state


def build_levels(project: TextureProject, *, work_root: Path) -> list[dict[str, Any]]:
    work_root = work_root.resolve()
    maximum_zoom = project.product.maximum_zoom
    base = work_root / "levels" / f"{maximum_zoom}.tif"
    if not base.is_file():
        raise ProcessingError(f"base raster does not exist: {base}")
    base_checksum = sha256_file(base)
    versions = tool_versions()
    producer = producer_identity()
    states: list[dict[str, Any]] = []
    for zoom in range(maximum_zoom - 1, -1, -1):
        dimension = project.product.tile_size * (1 << zoom)
        output = work_root / "levels" / f"{zoom}.tif"
        argv = [
            "gdal_translate",
            "-of",
            "GTiff",
            "-ot",
            "Byte",
            "-outsize",
            str(dimension),
            str(dimension),
            "-r",
            project.product.pyramid_resampling,
            "-co",
            "BIGTIFF=IF_SAFER",
            "-co",
            "TILED=YES",
            "-co",
            f"BLOCKXSIZE={project.product.tile_size}",
            "-co",
            f"BLOCKYSIZE={project.product.tile_size}",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "PREDICTOR=2",
            str(base),
            "{output}",
        ]
        states.append(
            _run_atomic_file_stage(
                stage_name=f"build-level-{zoom}",
                identity={
                    "project": project.semantic_dict(),
                    "base_sha256": base_checksum,
                    "zoom": zoom,
                    "tools": versions,
                    "producer": producer,
                },
                argv=argv,
                output_path=output,
                work_root=work_root,
                environment={"CPL_TMPDIR": str(work_root / "tmp")},
                validate_output=lambda metadata, dimension=dimension: (
                    _validate_level_output(
                        metadata,
                        dimension=dimension,
                        bounds=project.projected_bounds if project.grid else None,
                        channels=project.product.channels,
                    )
                ),
            )
        )
    return states
