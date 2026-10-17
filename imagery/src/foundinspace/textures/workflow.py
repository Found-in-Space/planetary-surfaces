"""Acquisition, explicit source admission, and the existing production stages."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .acquisition import download_source
from .processing import (
    ProcessingError,
    build_base,
    build_levels,
    inspect_source,
    preflight,
)
from .project import AcquisitionSpec, TextureProject, load_acquisition
from .tiles import emit_tiles, validate_tiles
from .util import atomic_write_json, sha256_file, utc_now


def acquire_project(project: TextureProject, source_root: Path, evidence_root: Path):
    """Download and inspect every declared sheet, without admitting candidates."""
    records = []
    for source in project.sources:
        receipt = download_source(source.acquisition, source_root)
        inspection = inspect_source(source.acquisition, source_root, evidence_root)
        records.append({"id": source.id, "receipt": receipt, "inspection": inspection})
    report = {
        "status": "complete",
        "project": project.semantic_dict(),
        "sources": records,
        "completed_at": utc_now(),
    }
    atomic_write_json(evidence_root / "acquisition.json", report)
    return report


def admit_source(spec: AcquisitionSpec, source_root: Path, evidence_root: Path):
    """Explicitly record a reviewed source; never called by acquisition or builds."""
    inspection = inspect_source(spec, source_root, evidence_root)
    observed = inspection["observed"]
    expected = {
        "size_bytes": inspection["source"]["size_bytes"],
        "sha256": inspection["source"]["sha256"],
        "width": observed["width"],
        "height": observed["height"],
        "band_count": observed["band_count"],
        "geo_transform": observed["geo_transform"],
    }
    for key, values in (
        ("data_type", observed["data_types"]),
        ("nodata", observed["nodata_values"]),
        ("mask_flags", observed["mask_flags"]),
        ("scale", observed["scales"]),
        ("offset", observed["offsets"]),
    ):
        if not values or any(value != values[0] for value in values):
            raise ProcessingError(
                f"Source needs per-band {key} contracts before admission"
            )
        expected[key] = values[0]
    if spec.source_crs is None or not spec.source_crs.is_file():
        raise ProcessingError("Admission requires a reviewed source_crs resource")
    # All source fields are serialized explicitly, retaining source terms and
    # public retrieval identity. No machine-local payload paths enter the recipe.
    fields = {
        "format_version": 1,
        "status": "admitted",
        "source_id": spec.source_id,
        "title": spec.title,
        "landing_page": spec.landing_page,
        "url": spec.url,
        "filename": spec.filename,
        "citation": spec.citation,
        "source_crs": spec.source_crs.relative_to(spec.path.parent).as_posix(),
    }
    if spec.license:
        fields["license"] = spec.license
    if spec.license_url:
        fields["license_url"] = spec.license_url
    lines = [f"{key} = {json.dumps(value)}\n" for key, value in fields.items()]
    for name, values in (("reported", spec.reported), ("expected", expected)):
        if values:
            lines.append(f"\n[{name}]\n")
            lines.extend(
                f"{key} = {json.dumps(value)}\n" for key, value in values.items()
            )
    temporary = spec.path.with_name(f".{spec.path.name}.admission.tmp")
    try:
        temporary.write_text("".join(lines), encoding="utf-8")
        admitted = load_acquisition(temporary)
        if not admitted.expected.complete:
            raise ProcessingError(
                "Inspection did not produce a complete source contract"
            )
        temporary.replace(spec.path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "source_id": spec.source_id,
        "status": "admitted",
        "expected": asdict(admitted.expected),
        "spec_sha256": sha256_file(spec.path),
    }


def _snapshot_definition(project, evidence_root):
    files = {project.path, project.target.crs_path.resolve()}
    if project.target.geographic_crs_path:
        files.add(project.target.geographic_crs_path.resolve())
    for source in project.sources:
        files.add(source.acquisition.path)
        if source.acquisition.source_crs:
            files.add(source.acquisition.source_crs.resolve())
    common = Path(os.path.commonpath([path.parent for path in files]))
    manifest = []
    for path in sorted(files):
        relative = path.relative_to(common)
        destination = evidence_root / "definition" / relative
        payload = path.read_bytes()
        if destination.exists() and destination.read_bytes() != payload:
            raise ProcessingError("Run definition changed; use a new evidence root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        manifest.append({"path": relative.as_posix(), "sha256": sha256_file(path)})
    repository = Path(__file__).resolve().parents[3]
    for name in ("pyproject.toml", "uv.lock"):
        (evidence_root / name).write_bytes((repository / name).read_bytes())
    atomic_write_json(
        evidence_root / "definition.json",
        {
            "project": project.path.relative_to(common).as_posix(),
            "files": manifest,
        },
    )


def run_project(
    project: TextureProject,
    *,
    source_root: Path,
    work_root: Path,
    output_root: Path,
    evidence_root: Path,
    threads=1,
    warp_memory_mb=512,
):
    """Run the same independently restartable stages, then validate every pixel."""
    report = preflight(
        project,
        source_root=source_root,
        work_root=work_root,
        output_root=output_root,
        evidence_root=evidence_root,
    )
    if not report["ready_for_processing"]:
        raise ProcessingError(
            "Preflight is not ready: review sources, admission and disk budget"
        )
    _snapshot_definition(project, evidence_root)
    state = {
        "status": "running",
        "product_id": project.product_id,
        "started_at": utc_now(),
        "stages": [],
    }
    path = evidence_root / "run.json"
    atomic_write_json(path, state)
    try:
        for source in project.sources:
            inspect_source(source.acquisition, source_root, evidence_root)
        for name, stage in (
            (
                "build-base",
                lambda: build_base(
                    project,
                    source_root=source_root,
                    work_root=work_root,
                    threads=threads,
                    warp_memory_mb=warp_memory_mb,
                ),
            ),
            ("build-levels", lambda: build_levels(project, work_root=work_root)),
            (
                "emit-tiles",
                lambda: emit_tiles(
                    project, work_root=work_root, output_root=output_root
                ),
            ),
            (
                "validate-tiles",
                lambda: validate_tiles(
                    project,
                    work_root=work_root,
                    output_root=output_root,
                    evidence_root=evidence_root,
                ),
            ),
        ):
            stage()
            state["stages"].append({"stage": name, "completed_at": utc_now()})
            atomic_write_json(path, state)
        state.update(
            status="complete",
            completed_at=utc_now(),
            product_json_sha256=sha256_file(output_root / "product.json"),
        )
        atomic_write_json(path, state)
        return state
    except Exception as error:
        state.update(status="failed", failed_at=utc_now(), error=str(error))
        atomic_write_json(path, state)
        raise
