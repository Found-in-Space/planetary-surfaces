from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from foundinspace.textures.processing import ProcessingError, build_base
from foundinspace.textures.project import load_acquisition, load_project
from foundinspace.textures.tiles import validate_tiles
from foundinspace.textures.toolchain import gdalinfo, run_command
from foundinspace.textures.workflow import admit_source, run_project

ROOT = Path(__file__).resolve().parents[1]
TOOLS = (
    "gdal_create",
    "gdalinfo",
    "gdalwarp",
    "gdal_translate",
    "gdal_retile.py",
    "gdalbuildvrt",
    "gdalsrsinfo",
    "proj",
)


@pytest.mark.parametrize("name", ["blue-marble-200407-500m", "black-marble-2016-500m"])
def test_full_earth_recipes_pin_compatible_sources(name):
    project = load_project(ROOT / "projects/earth" / name / "project.toml")
    assert project.product.channels == "rgb"
    assert project.product.tile_size == 256
    assert project.product.maximum_zoom == 9
    assert project.product.total_tile_count == 349525
    assert project.grid.radius_metres == 6378137
    assert len(project.sources) == 8
    assert len({source.acquisition.filename for source in project.sources}) == 8
    for source in project.sources:
        assert source.acquisition.status in {"candidate", "admitted"}
        assert source.acquisition.expected.width == 21600
        assert source.acquisition.expected.band_count == 3
        assert source.acquisition.expected.nodata == "absent"
        assert source.acquisition.license_url.startswith("https://www.nasa.gov/")


@pytest.fixture
def earth_sheets(tmp_path):
    if not all(shutil.which(tool) for tool in TOOLS):
        pytest.skip("system GDAL/PROJ tools are required")
    definition = tmp_path / "definition"
    specs = definition / "sources"
    specs.mkdir(parents=True)
    crs = definition / "crs"
    crs.mkdir()
    for name in (
        "earth-wgs84.wkt",
        "earth-mercator-6378137.wkt",
        "earth-geographic-6378137.wkt",
    ):
        shutil.copyfile(ROOT / "resources/crs" / name, crs / name)
    source_root = tmp_path / "raw"
    source_root.mkdir()
    y, x = np.indices((32, 64))
    pixels = np.stack((40 + x * 2, 20 + y * 4, 10 + x + y), axis=-1).astype(np.uint8)
    pixels[:, ::2] = 0  # Valid dark samples must contribute to every average.
    source_refs = []
    for row in range(2):
        for column in range(4):
            source_id = f"sheet-{row}-{column}"
            image = source_root / f"{source_id}.png"
            Image.fromarray(
                pixels[row * 16 : (row + 1) * 16, column * 16 : (column + 1) * 16]
            ).save(image)
            target = source_root / f"{source_id}.tif"
            west, north = -180 + column * 90, 90 - row * 90
            run_command(
                (
                    "gdal_translate",
                    "-q",
                    "-of",
                    "GTiff",
                    "-a_srs",
                    str(crs / "earth-wgs84.wkt"),
                    "-a_ullr",
                    str(west),
                    str(north),
                    str(west + 90),
                    str(north - 90),
                    str(image),
                    str(target),
                )
            )
            spec = specs / f"{source_id}.toml"
            spec.write_text(f'''format_version = 1
status = "candidate"
source_id = "{source_id}"
title = "Synthetic RGB sheet"
landing_page = "https://example.test/earth"
url = "https://example.test/{source_id}.tif"
filename = "{target.name}"
citation = "Synthetic data"
source_crs = "../crs/earth-wgs84.wkt"
[expected]
width = 16
height = 16
band_count = 3
data_type = "Byte"
nodata = "absent"
''')
            source_refs.append(
                f'[[sources]]\nid = "{source_id}"\nacquisition = "sources/{spec.name}"\npriority = 100\n'
            )
    # Reverse the references: geography, not enumeration order, places the sheets.
    project_path = definition / "project.toml"
    project_path.write_text(
        """format_version = 2
product_id = "earth-synthetic"
[target]
id = "earth"
crs = "crs/earth-mercator-6378137.wkt"
geographic_crs = "crs/earth-geographic-6378137.wkt"
longitude_range = [-180.0, 180.0]
latitude_range = [-85.0511287798066, 85.0511287798066]
"""
        + "\n".join(reversed(source_refs))
        + """
[composite]
policy = "mosaic"
missing_pixel = 0
[product]
kind = "display-texture"
bounds = [-180.0, -85.0511287798066, 180.0, 85.0511287798066]
maximum_zoom = 2
tile_size = 16
reprojection_resampling = "bilinear"
pyramid_resampling = "average"
pixel_type = "uint8"
channels = "rgb"
format = "png"
opaque = true
path_template = "imagery/{z}/{x}/{y}.png"
[grid]
projection = "web-mercator"
radius_metres = 6378137
[base_texture]
width = 8
height = 4
path = "base.png"
resampling = "average"
"""
    )
    return project_path, source_root, pixels


def test_rgb_sheets_build_complete_pyramid_and_preserve_dark_pixels(
    earth_sheets, tmp_path
):
    project_path, sources, pixels = earth_sheets
    with pytest.raises(ProcessingError, match="not been admitted"):
        build_base(
            load_project(project_path),
            source_root=sources,
            work_root=tmp_path / "rejected",
        )
    for source in load_project(project_path).sources:
        admit_source(source.acquisition, sources, tmp_path / "acquisition")
    project = load_project(project_path)
    roots = {
        "source_root": sources,
        "work_root": tmp_path / "work",
        "output_root": tmp_path / "product",
        "evidence_root": tmp_path / "evidence",
    }
    result = run_project(project, **roots)
    assert result["status"] == "complete"
    report = json.loads((roots["evidence_root"] / "tiles.validation.json").read_text())
    descriptor = json.loads((roots["output_root"] / "product.json").read_text())
    assert report["tile_count"] == 21
    assert descriptor["tiles"]["channels"] == "rgb"
    assert len(descriptor["sources"]) == 8
    assert report["checks"]["every_level_reconstructs_exactly"]
    with Image.open(roots["output_root"] / "base.png") as image:
        assert image.mode == "RGB"
        assert "transparency" not in image.info
        expected = pixels.reshape(4, 8, 8, 8, 3).mean(axis=(1, 3))
        assert np.asarray(image) == pytest.approx(expected, abs=1)
    for zoom in range(3):
        metadata, _ = gdalinfo(roots["work_root"] / f"levels/{zoom}.tif")
        assert [band["colorInterpretation"] for band in metadata["bands"]] == [
            "Red",
            "Green",
            "Blue",
        ]
        assert all("noDataValue" not in band for band in metadata["bands"])
    original_mtime = (roots["work_root"] / "levels/2.tif").stat().st_mtime_ns
    run_project(project, **roots)
    assert (roots["work_root"] / "levels/2.tif").stat().st_mtime_ns == original_mtime
    tile = roots["output_root"] / "imagery/0/0/0.png"
    Image.new("RGB", (16, 16), (200, 200, 200)).save(tile)
    with pytest.raises(ProcessingError, match="tile checksum mismatch"):
        validate_tiles(
            project,
            work_root=roots["work_root"],
            output_root=roots["output_root"],
            evidence_root=roots["evidence_root"],
        )
    # A contract cannot silently bless new bytes after admission.
    first = project.sources[0].acquisition
    with (sources / first.filename).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(ProcessingError, match="size_bytes"):
        admit_source(
            load_acquisition(first.path), sources, tmp_path / "changed-inspection"
        )


def test_mosaic_refuses_undeclared_overlap(earth_sheets, tmp_path):
    project_path, sources, _ = earth_sheets
    project = load_project(project_path)
    first, second = sorted(project.sources, key=lambda source: source.id)[:2]
    shutil.copyfile(
        sources / first.acquisition.filename, sources / second.acquisition.filename
    )
    for source in project.sources:
        admit_source(source.acquisition, sources, tmp_path / "acquisition")
    with pytest.raises(ProcessingError, match="overlap"):
        build_base(
            load_project(project_path), source_root=sources, work_root=tmp_path / "work"
        )


def test_priority_overlap_uses_numeric_priority_not_toml_order(earth_sheets, tmp_path):
    project_path, sources, pixels = earth_sheets
    project_path.write_text(
        project_path.read_text()
        .replace('policy = "mosaic"', 'policy = "priority"')
        .replace(
            'acquisition = "sources/sheet-0-0.toml"\npriority = 100',
            'acquisition = "sources/sheet-0-0.toml"\npriority = 200',
        )
    )
    run_command(
        (
            "gdal_create",
            "-of",
            "GTiff",
            "-outsize",
            "16",
            "16",
            "-bands",
            "3",
            "-burn",
            "20",
            "-burn",
            "40",
            "-burn",
            "80",
            "-a_srs",
            "EPSG:4326",
            "-a_ullr",
            "-180",
            "90",
            "-90",
            "0",
            str(sources / "sheet-0-1.tif"),
        )
    )
    for source in load_project(project_path).sources:
        admit_source(source.acquisition, sources, tmp_path / "acquisition")
    work = tmp_path / "work"
    build_base(load_project(project_path), source_root=sources, work_root=work)
    with Image.open(work / "base.png") as image:
        result = np.asarray(image)
        expected = pixels.reshape(4, 8, 8, 8, 3).mean(axis=(1, 3))
        assert result[:2, :2] == pytest.approx(expected[:2, :2], abs=1)
        assert np.all(result[:2, 2:4] == 0)
