from __future__ import annotations

import json
from pathlib import Path

import pytest

import foundinspace.textures.processing as processing
from foundinspace.textures.processing import ProcessingError
from foundinspace.textures.project import load_acquisition, load_project
from foundinspace.textures.toolchain import CommandResult


def _metadata(width: int, height: int) -> dict:
    return {
        "size": [width, height],
        "geoTransform": [-4.0, 1.0, 0.0, 2.0, 0.0, -1.0],
        "bands": [{"band": 1, "type": "Byte", "colorInterpretation": "Gray"}],
    }


def test_inspect_source_writes_full_candidate_evidence(
    tmp_path: Path, project_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, acquisition_path = project_factory()
    spec = load_acquisition(acquisition_path)
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / spec.filename).write_bytes(b"source-pixels")
    monkeypatch.setattr(
        processing,
        "gdalinfo",
        lambda path: (
            _metadata(8, 4),
            CommandResult(("gdalinfo", str(path)), "{}", ""),
        ),
    )
    monkeypatch.setattr(
        processing,
        "spatial_reference_proj4",
        lambda path: (
            "+proj=eqc +R=1737400",
            CommandResult(("gdalsrsinfo", str(path)), "", ""),
        ),
    )
    monkeypatch.setattr(
        processing, "tool_versions", lambda: {"gdal": "test", "proj": "test"}
    )

    report = processing.inspect_source(spec, source_root, tmp_path / "evidence")

    assert report["contract_complete"] is False
    assert report["contract_matches"] is True
    saved = json.loads(
        (tmp_path / "evidence/sources/source-a.inspection.json").read_text()
    )
    assert saved["observed"]["data_types"] == ["Byte"]


def test_preflight_rejects_overlapping_roots(
    tmp_path: Path, project_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, _ = project_factory()
    monkeypatch.setattr(processing, "require_tools", lambda names: {})
    monkeypatch.setattr(processing, "tool_versions", lambda: {})

    with pytest.raises(ProcessingError, match="must not overlap"):
        processing.preflight(
            load_project(project_path),
            source_root=tmp_path / "runtime/source",
            work_root=tmp_path / "runtime",
            output_root=tmp_path / "output",
            evidence_root=tmp_path / "evidence",
        )


def test_preflight_reports_candidate_as_not_ready(
    tmp_path: Path, project_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, _ = project_factory()
    project = load_project(project_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    (source_root / "source.tif").write_bytes(b"source-pixels")
    monkeypatch.setattr(processing, "require_tools", lambda names: {})
    monkeypatch.setattr(processing, "tool_versions", lambda: {"gdal": "test"})

    report = processing.preflight(
        project,
        source_root=source_root,
        work_root=tmp_path / "work-root",
        output_root=tmp_path / "output-root",
        evidence_root=tmp_path / "evidence-root",
    )

    assert report["ready_for_processing"] is False
    assert report["estimate"]["tile_count"] == 21
    assert report["estimate"]["minimum_working_budget_bytes"] == (
        1024**2 + 2 * (256**2 + 512**2 + 1024**2)
    )
    assert (tmp_path / "evidence-root/preflight.json").is_file()


def test_build_base_records_explicit_crs_and_atomic_output(
    tmp_path: Path, project_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, _ = project_factory(admitted=True)
    project = load_project(project_path)
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    (source_root / "source.tif").write_bytes(b"source-pixels")
    commands: list[tuple[str, ...]] = []

    def fake_run(argv, *, env=None, timeout=None):
        command = tuple(argv)
        commands.append(command)
        Path(command[-1]).write_bytes(b"base-raster")
        return CommandResult(command, "", "")

    monkeypatch.setattr(processing, "run_command", fake_run)
    monkeypatch.setattr(
        processing,
        "gdalinfo",
        lambda path: (
            _metadata(8, 4)
            if Path(path).name == "source.tif"
            else _metadata(1024, 1024),
            CommandResult(("gdalinfo", str(path)), "{}", ""),
        ),
    )
    monkeypatch.setattr(
        processing,
        "spatial_reference_proj4",
        lambda path: (
            "+proj=eqc +R=1737400",
            CommandResult(("gdalsrsinfo", str(path)), "", ""),
        ),
    )
    monkeypatch.setattr(
        processing, "tool_versions", lambda: {"gdal": "test", "proj": "test"}
    )

    state = processing.build_base(
        project, source_root=source_root, work_root=tmp_path / "work", threads=2
    )

    command = commands[0]
    assert command[command.index("-s_srs") + 1].endswith("source.wkt")
    assert command[command.index("-t_srs") + 1].endswith("moon.wkt")
    assert command[command.index("-ts") + 1 : command.index("-ts") + 3] == (
        "1024",
        "1024",
    )
    assert state["status"] == "complete"
    assert (tmp_path / "work/levels/2.tif").read_bytes() == b"base-raster"


def test_build_levels_averages_each_level_directly_from_base(
    tmp_path: Path, project_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, _ = project_factory(maximum_zoom=2)
    project = load_project(project_path)
    work_root = tmp_path / "work"
    (work_root / "levels").mkdir(parents=True)
    base = work_root / "levels/2.tif"
    base.write_bytes(b"base")
    commands: list[tuple[str, ...]] = []

    def fake_run(argv, *, env=None, timeout=None):
        command = tuple(argv)
        commands.append(command)
        Path(command[-1]).write_bytes(f"level-{len(commands)}".encode())
        return CommandResult(command, "", "")

    monkeypatch.setattr(processing, "run_command", fake_run)
    monkeypatch.setattr(
        processing,
        "gdalinfo",
        lambda path: (
            _metadata(512, 512) if len(commands) == 1 else _metadata(256, 256),
            CommandResult(("gdalinfo", str(path)), "{}", ""),
        ),
    )
    monkeypatch.setattr(processing, "tool_versions", lambda: {"gdal": "test"})

    states = processing.build_levels(project, work_root=work_root)

    assert len(states) == 2
    assert [command[command.index("-outsize") + 1] for command in commands] == [
        "512",
        "256",
    ]
    assert all(command[-2] == str(base) for command in commands)
    assert all(command[command.index("-r") + 1] == "average" for command in commands)
