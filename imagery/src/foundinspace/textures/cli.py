from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from .acquisition import AcquisitionError, download_source
from .processing import (
    ProcessingError,
    build_base,
    build_levels,
    inspect_source,
    preflight,
)
from .project import load_acquisition, load_project
from .tiles import emit_tiles, validate_tiles
from .toolchain import ToolError


def _emit(value: Any) -> None:
    click.echo(json.dumps(value, indent=2, sort_keys=True))


def _command_errors(function):
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (
            AcquisitionError,
            ProcessingError,
            ToolError,
            ValueError,
            OSError,
        ) as exc:
            raise click.ClickException(str(exc)) from exc

    wrapped.__name__ = function.__name__
    wrapped.__doc__ = function.__doc__
    return wrapped


@click.group()
@click.version_option(package_name="found-in-space-pas-de-geant-texture-pipeline")
def cli() -> None:
    """Build controlled planetary display-texture products."""


@cli.command("build-overlay")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--source-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--archive-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--reference-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--work-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--output-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--maximum-zoom",
    type=click.IntRange(min=0, max=20),
    help="Recorded cap for a review build; must reach the calibration zoom.",
)
@_command_errors
def build_overlay_command(
    project_path,
    source_root,
    archive_root,
    reference_root,
    work_root,
    output_root,
    maximum_zoom,
):
    """Match and blend admitted sources against a pinned base tile product."""
    from .overlay import build_overlay

    _emit(
        build_overlay(
            project_path.resolve(),
            source_root=source_root.resolve(),
            archive_root=archive_root.resolve(),
            reference_root=reference_root.resolve(),
            work_root=work_root.resolve(),
            output_root=output_root.resolve(),
            maximum_zoom=maximum_zoom,
        )
    )


@cli.command("build-preview")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--source-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--detail-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--work-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--output-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@_command_errors
def build_preview_command(
    project_path: Path,
    source_root: Path,
    detail_root: Path,
    work_root: Path,
    output_root: Path,
) -> None:
    """Build sparse Kaguya context and reference inner NAC preview pyramids."""
    from .preview import build_preview

    _emit(
        build_preview(
            project_path.resolve(),
            source_root.resolve(),
            detail_root.resolve(),
            work_root.resolve(),
            output_root.resolve(),
        )
    )


@cli.command("prepare-preview-tiles")
@click.argument(
    "preview_root", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--source-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--output-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--allow-download", is_flag=True, help="Acquire missing archive JPEG tiles."
)
@click.option(
    "--download-concurrency", type=click.IntRange(min=1), default=8, show_default=True
)
@_command_errors
def prepare_preview_tiles_command(
    preview_root: Path,
    source_root: Path,
    output_root: Path,
    allow_download: bool,
    download_concurrency: int,
) -> None:
    """Prepare opaque sparse XYZ tiles for the existing 3D imagery loader."""
    from .preview_tiles import prepare_preview_tiles

    _emit(
        prepare_preview_tiles(
            preview_root.resolve(),
            source_root.resolve(),
            output_root.resolve(),
            allow_download=allow_download,
            download_concurrency=download_concurrency,
        )
    )


@cli.command("validate-preview-tiles")
@click.argument(
    "output_root", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@_command_errors
def validate_preview_tiles_command(output_root: Path) -> None:
    """Authenticate and decode every prepared opaque XYZ preview tile."""
    from .preview_tiles import validate_preview_tiles

    _emit(validate_preview_tiles(output_root.resolve()))


@cli.command("collect-preview-tiles")
@click.option(
    "--input",
    "inputs",
    multiple=True,
    required=True,
    type=(str, click.Path(exists=True, file_okay=False, path_type=Path)),
    help="Immutable input namespace and product root; repeat for each product.",
)
@click.option(
    "--output-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@_command_errors
def collect_preview_tiles_command(inputs, output_root: Path) -> None:
    """Collect matching sparse products on one filesystem and validate all tiles."""
    from .preview_collection import collect_preview_tiles

    _emit(collect_preview_tiles(list(inputs), output_root))


@cli.command("validate-project")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@_command_errors
def validate_project_command(project_path: Path) -> None:
    """Parse and validate a versioned texture project."""
    project = load_project(project_path)
    _emit(
        {
            "product_id": project.product_id,
            "sources": [source.id for source in project.sources],
            "maximum_dimension": project.product.maximum_dimension,
            "tile_count": project.product.total_tile_count,
        }
    )


@cli.command("download")
@click.argument(
    "acquisition_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--source-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--timeout", default=60.0, show_default=True, type=click.FloatRange(min=1.0)
)
@click.option(
    "--minimum-free-bytes",
    default=5_000_000_000,
    show_default=True,
    type=click.IntRange(min=0),
)
@_command_errors
def download_command(
    acquisition_path: Path,
    source_root: Path,
    timeout: float,
    minimum_free_bytes: int,
) -> None:
    """Download or resume one declared source asset."""
    _emit(
        download_source(
            load_acquisition(acquisition_path),
            source_root,
            timeout=timeout,
            minimum_free_bytes=minimum_free_bytes,
        )
    )


@cli.command("inspect-source")
@click.argument(
    "acquisition_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--source-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--evidence-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@_command_errors
def inspect_source_command(
    acquisition_path: Path, source_root: Path, evidence_root: Path
) -> None:
    """Capture GDAL metadata and compare it with a source contract."""
    _emit(
        inspect_source(load_acquisition(acquisition_path), source_root, evidence_root)
    )


def _root_options(function):
    function = click.option(
        "--evidence-root",
        required=True,
        type=click.Path(file_okay=False, path_type=Path),
    )(function)
    function = click.option(
        "--output-root", required=True, type=click.Path(file_okay=False, path_type=Path)
    )(function)
    function = click.option(
        "--work-root", required=True, type=click.Path(file_okay=False, path_type=Path)
    )(function)
    function = click.option(
        "--source-root", required=True, type=click.Path(file_okay=False, path_type=Path)
    )(function)
    return function


@cli.command("preflight")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@_root_options
@_command_errors
def preflight_command(
    project_path: Path,
    source_root: Path,
    work_root: Path,
    output_root: Path,
    evidence_root: Path,
) -> None:
    """Validate tools, roots, source readiness, and initial disk estimates."""
    _emit(
        preflight(
            load_project(project_path),
            source_root=source_root,
            work_root=work_root,
            output_root=output_root,
            evidence_root=evidence_root,
        )
    )


@cli.command("build-base")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--source-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--work-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option("--threads", default=1, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--warp-memory-mb", default=512, show_default=True, type=click.IntRange(min=1)
)
@_command_errors
def build_base_command(
    project_path: Path,
    source_root: Path,
    work_root: Path,
    threads: int,
    warp_memory_mb: int,
) -> None:
    """Reproject admitted source imagery once onto the maximum target grid."""
    _emit(
        build_base(
            load_project(project_path),
            source_root=source_root,
            work_root=work_root,
            threads=threads,
            warp_memory_mb=warp_memory_mb,
        )
    )


@cli.command("build-levels")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--work-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@_command_errors
def build_levels_command(project_path: Path, work_root: Path) -> None:
    """Area-average every lower level directly from the maximum-level base."""
    _emit(build_levels(load_project(project_path), work_root=work_root))


@cli.command("emit-tiles")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--work-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--output-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@_command_errors
def emit_tiles_command(project_path: Path, work_root: Path, output_root: Path) -> None:
    """Copy every aligned level window into opaque display PNG tiles."""
    _emit(
        emit_tiles(
            load_project(project_path),
            work_root=work_root,
            output_root=output_root,
        )
    )


@cli.command("validate-tiles")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--work-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--output-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--evidence-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@_command_errors
def validate_tiles_command(
    project_path: Path,
    work_root: Path,
    output_root: Path,
    evidence_root: Path,
) -> None:
    """Validate every tile structurally and reconstruct every level exactly."""
    _emit(
        validate_tiles(
            load_project(project_path),
            work_root=work_root,
            output_root=output_root,
            evidence_root=evidence_root,
        )
    )


@cli.command("acquire-project")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--source-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@click.option(
    "--evidence-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@_command_errors
def acquire_project_command(project_path: Path, source_root: Path, evidence_root: Path):
    """Download and inspect every declared source; candidates remain unadmitted."""
    from .workflow import acquire_project

    _emit(acquire_project(load_project(project_path), source_root, evidence_root))


@cli.command("admit-source")
@click.argument(
    "acquisition_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--source-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--evidence-root", required=True, type=click.Path(file_okay=False, path_type=Path)
)
@_command_errors
def admit_source_command(
    acquisition_path: Path, source_root: Path, evidence_root: Path
):
    """After reviewing inspection evidence, update this source's versioned contract."""
    from .workflow import admit_source

    _emit(admit_source(load_acquisition(acquisition_path), source_root, evidence_root))


@cli.command("run")
@click.argument(
    "project_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@_root_options
@click.option("--threads", default=1, show_default=True, type=click.IntRange(min=1))
@click.option(
    "--warp-memory-mb", default=512, show_default=True, type=click.IntRange(min=1)
)
@_command_errors
def run_project_command(
    project_path: Path,
    source_root: Path,
    work_root: Path,
    output_root: Path,
    evidence_root: Path,
    threads: int,
    warp_memory_mb: int,
):
    """Preflight, build the full pyramid, emit tiles and validate every pixel."""
    from .workflow import run_project

    _emit(
        run_project(
            load_project(project_path),
            source_root=source_root,
            work_root=work_root,
            output_root=output_root,
            evidence_root=evidence_root,
            threads=threads,
            warp_memory_mb=warp_memory_mb,
        )
    )
