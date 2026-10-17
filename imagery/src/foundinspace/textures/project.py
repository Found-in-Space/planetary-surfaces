from __future__ import annotations

import math
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .grid import TileGrid
from .util import reject_unportable_path

FORMAT_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_KEYS = {
    "format_version",
    "product_id",
    "target",
    "sources",
    "composite",
    "product",
    "grid",
    "base_texture",
}
_TARGET_KEYS = {"id", "crs", "geographic_crs", "longitude_range", "latitude_range"}
_SOURCE_REF_KEYS = {"id", "acquisition", "priority"}
_COMPOSITE_KEYS = {"policy", "missing_pixel"}
_PRODUCT_KEYS = {
    "kind",
    "bounds",
    "maximum_zoom",
    "tile_size",
    "reprojection_resampling",
    "pyramid_resampling",
    "pixel_type",
    "channels",
    "format",
    "opaque",
    "path_template",
}
_ACQUISITION_KEYS = {
    "format_version",
    "status",
    "source_id",
    "title",
    "landing_page",
    "url",
    "filename",
    "citation",
    "license",
    "license_url",
    "source_crs",
    "expected",
    "reported",
}
_EXPECTED_KEYS = {
    "size_bytes",
    "sha256",
    "width",
    "height",
    "band_count",
    "data_type",
    "geo_transform",
    "nodata",
    "mask_flags",
    "scale",
    "offset",
}
_REPORTED_KEYS = {
    "approximate_size",
    "width",
    "height",
    "band_count",
    "data_type",
    "projection",
}


@dataclass(frozen=True, slots=True)
class ExpectedSource:
    size_bytes: int | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    band_count: int | None = None
    data_type: str | None = None
    geo_transform: tuple[float, ...] | None = None
    nodata: str | int | float | None = None
    mask_flags: tuple[str, ...] | None = None
    scale: str | int | float | None = None
    offset: str | int | float | None = None

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.size_bytes,
                self.sha256,
                self.width,
                self.height,
                self.band_count,
                self.data_type,
                self.geo_transform,
                self.nodata,
                self.mask_flags,
                self.scale,
                self.offset,
            )
        )


@dataclass(frozen=True, slots=True)
class AcquisitionSpec:
    path: Path
    status: str
    source_id: str
    title: str
    landing_page: str
    url: str
    filename: str
    citation: str
    source_crs: Path | None
    expected: ExpectedSource
    reported: dict[str, Any]
    license: str | None = None
    license_url: str | None = None

    def semantic_dict(self) -> dict[str, Any]:
        result = {
            "format_version": FORMAT_VERSION,
            "status": self.status,
            "source_id": self.source_id,
            "title": self.title,
            "landing_page": self.landing_page,
            "url": self.url,
            "filename": self.filename,
            "citation": self.citation,
            "source_crs": (
                self.source_crs.relative_to(self.path.parent).as_posix()
                if self.source_crs
                else None
            ),
            "expected": asdict(self.expected),
            "reported": self.reported,
        }
        if self.license is not None:
            result["license"] = self.license
        if self.license_url is not None:
            result["license_url"] = self.license_url
        return result


@dataclass(frozen=True, slots=True)
class SourceRef:
    id: str
    acquisition_path: Path
    priority: int
    acquisition: AcquisitionSpec


@dataclass(frozen=True, slots=True)
class TargetConfig:
    id: str
    crs_path: Path
    longitude_range: tuple[float, float]
    latitude_range: tuple[float, float]
    geographic_crs_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ProductConfig:
    kind: str
    bounds: tuple[float, float, float, float]
    maximum_zoom: int
    tile_size: int
    reprojection_resampling: str
    pyramid_resampling: str
    pixel_type: str
    channels: str
    format: str
    opaque: bool
    path_template: str

    @property
    def band_count(self) -> int:
        return 3 if self.channels == "rgb" else 1

    @property
    def image_mode(self) -> str:
        return "RGB" if self.channels == "rgb" else "L"

    @property
    def maximum_dimension(self) -> int:
        return self.tile_size * (1 << self.maximum_zoom)

    @property
    def total_tile_count(self) -> int:
        return sum(4**zoom for zoom in range(self.maximum_zoom + 1))


@dataclass(frozen=True, slots=True)
class BaseTextureConfig:
    width: int
    height: int
    path: str
    resampling: str


@dataclass(frozen=True, slots=True)
class TextureProject:
    path: Path
    product_id: str
    target: TargetConfig
    sources: tuple[SourceRef, ...]
    composite_policy: str
    missing_pixel: int
    product: ProductConfig
    format_version: int = 1
    grid: TileGrid | None = None
    base_texture: BaseTextureConfig | None = None

    @property
    def projected_bounds(self):
        return self.grid.projected_bounds if self.grid else self.product.bounds

    def semantic_dict(self) -> dict[str, Any]:
        result = {
            "format_version": self.format_version,
            "product_id": self.product_id,
            "target": {
                "id": self.target.id,
                "crs": self.target.crs_path.relative_to(self.path.parent).as_posix(),
                "longitude_range": self.target.longitude_range,
                "latitude_range": self.target.latitude_range,
            },
            "sources": [
                {
                    "id": source.id,
                    "acquisition": source.acquisition.semantic_dict(),
                    "priority": source.priority,
                }
                for source in self.sources
            ],
            "composite": {
                "policy": self.composite_policy,
                "missing_pixel": self.missing_pixel,
            },
            "product": asdict(self.product),
        }

        if self.grid:
            result["grid"] = {
                "projection": self.grid.projection,
                "radius_metres": self.grid.radius_metres,
            }
        if self.base_texture:
            result["base_texture"] = asdict(self.base_texture)
            result["target"]["geographic_crs"] = (
                self.target.geographic_crs_path.relative_to(self.path.parent).as_posix()
            )
        return result


def _reject_unknown(raw: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"Unknown key(s) in {context}: {', '.join(unknown)}")


def _table(raw: dict[str, Any], key: str, context: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{context}.{key} must be a table")
    return value


def _string(raw: dict[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _integer(raw: dict[str, Any], key: str, context: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context}.{key} must be an integer")
    return value


def _number_tuple(
    raw: dict[str, Any], key: str, length: int, context: str
) -> tuple[float, ...]:
    value = raw.get(key)
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in value
        )
    ):
        raise ValueError(f"{context}.{key} must contain {length} finite numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{context}.{key} must contain {length} finite numbers")
    return result


def _https_url(raw: dict[str, Any], key: str, context: str) -> str:
    value = _string(raw, key, context)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{context}.{key} must be an HTTPS URL")
    return value


def load_acquisition(path: Path) -> AcquisitionSpec:
    resolved = path.resolve()
    with resolved.open("rb") as stream:
        raw = tomllib.load(stream)
    _reject_unknown(raw, _ACQUISITION_KEYS, "source acquisition")
    if raw.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"source acquisition format_version must be {FORMAT_VERSION}")
    status = _string(raw, "status", "source acquisition")
    if status not in {"candidate", "admitted"}:
        raise ValueError("source acquisition.status must be candidate or admitted")
    filename = _string(raw, "filename", "source acquisition")
    if Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("source acquisition.filename must be a plain filename")

    expected_raw = raw.get("expected", {})
    if not isinstance(expected_raw, dict):
        raise ValueError("source acquisition.expected must be a table")
    _reject_unknown(expected_raw, _EXPECTED_KEYS, "[expected]")
    geo_transform_raw = expected_raw.get("geo_transform")
    if geo_transform_raw is not None:
        if (
            not isinstance(geo_transform_raw, list)
            or len(geo_transform_raw) != 6
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in geo_transform_raw
            )
        ):
            raise ValueError("expected.geo_transform must contain 6 finite numbers")
        geo_transform = tuple(float(value) for value in geo_transform_raw)
        if not all(math.isfinite(value) for value in geo_transform):
            raise ValueError("expected.geo_transform must contain 6 finite numbers")
    else:
        geo_transform = None
    mask_flags_raw = expected_raw.get("mask_flags")
    if mask_flags_raw is not None:
        if (
            not isinstance(mask_flags_raw, list)
            or not mask_flags_raw
            or any(
                not isinstance(value, str) or not value.strip()
                for value in mask_flags_raw
            )
        ):
            raise ValueError("expected.mask_flags must be a non-empty list of strings")
        mask_flags = tuple(sorted(value.strip() for value in mask_flags_raw))
    else:
        mask_flags = None
    scalar_values: dict[str, str | int | float | None] = {}
    for field_name in ("nodata", "scale", "offset"):
        value = expected_raw.get(field_name)
        if (
            value is not None
            and value != "absent"
            and (isinstance(value, bool) or not isinstance(value, (int, float)))
        ):
            raise ValueError(f"expected.{field_name} must be numeric or 'absent'")
        scalar_values[field_name] = value
    expected = ExpectedSource(
        size_bytes=expected_raw.get("size_bytes"),
        sha256=expected_raw.get("sha256"),
        width=expected_raw.get("width"),
        height=expected_raw.get("height"),
        band_count=expected_raw.get("band_count"),
        data_type=expected_raw.get("data_type"),
        geo_transform=geo_transform,
        mask_flags=mask_flags,
        **scalar_values,
    )
    for field_name in ("size_bytes", "width", "height", "band_count"):
        value = getattr(expected, field_name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"expected.{field_name} must be a positive integer")
    if expected.sha256 is not None and not _SHA256_RE.fullmatch(expected.sha256):
        raise ValueError("expected.sha256 must be 64 lowercase hexadecimal characters")
    if expected.data_type is not None and not expected.data_type.strip():
        raise ValueError("expected.data_type must be a non-empty string")
    reported = raw.get("reported", {})
    if not isinstance(reported, dict):
        raise ValueError("source acquisition.reported must be a table")
    _reject_unknown(reported, _REPORTED_KEYS, "[reported]")
    source_crs_value = raw.get("source_crs")
    source_crs = None
    if source_crs_value is not None:
        if not isinstance(source_crs_value, str):
            raise ValueError("source acquisition.source_crs must be a relative path")
        source_crs = resolved.parent / reject_unportable_path(
            source_crs_value, field_name="source acquisition.source_crs"
        )
    if status == "admitted" and (not expected.complete or source_crs is None):
        raise ValueError(
            "an admitted source requires source_crs and complete expected identity "
            "and raster metadata"
        )

    return AcquisitionSpec(
        path=resolved,
        status=status,
        source_id=_string(raw, "source_id", "source acquisition"),
        title=_string(raw, "title", "source acquisition"),
        landing_page=_https_url(raw, "landing_page", "source acquisition"),
        url=_https_url(raw, "url", "source acquisition"),
        filename=filename,
        citation=_string(raw, "citation", "source acquisition"),
        source_crs=source_crs,
        expected=expected,
        reported=reported,
        license=_string(raw, "license", "source acquisition")
        if "license" in raw
        else None,
        license_url=_https_url(raw, "license_url", "source acquisition")
        if "license_url" in raw
        else None,
    )


def load_project(path: Path) -> TextureProject:
    resolved = path.resolve()
    with resolved.open("rb") as stream:
        raw = tomllib.load(stream)
    _reject_unknown(raw, _PROJECT_KEYS, "project")
    format_version = raw.get("format_version")
    if format_version not in {1, 2}:
        raise ValueError("project format_version must be 1 or 2")
    grid = None
    base_texture = None
    if format_version == 1 and ("grid" in raw or "base_texture" in raw):
        raise ValueError("Explicit grids and base textures require format_version 2")
    if format_version == 2:
        grid_raw = _table(raw, "grid", "project")
        _reject_unknown(grid_raw, {"projection", "radius_metres"}, "[grid]")
        grid = TileGrid(
            _string(grid_raw, "projection", "grid"), float(grid_raw["radius_metres"])
        )
        base_raw = _table(raw, "base_texture", "project")
        _reject_unknown(
            base_raw, {"width", "height", "path", "resampling"}, "[base_texture]"
        )
        base_texture = BaseTextureConfig(
            _integer(base_raw, "width", "base_texture"),
            _integer(base_raw, "height", "base_texture"),
            _string(base_raw, "path", "base_texture"),
            _string(base_raw, "resampling", "base_texture"),
        )
        if (
            base_texture.width <= 0
            or base_texture.height <= 0
            or base_texture.path != "base.png"
            or base_texture.resampling != "average"
        ):
            raise ValueError(
                "Base texture requires positive dimensions, base.png and average resampling"
            )

    target_raw = _table(raw, "target", "project")
    _reject_unknown(target_raw, _TARGET_KEYS, "[target]")
    longitude_range = _number_tuple(target_raw, "longitude_range", 2, "target")
    latitude_range = _number_tuple(target_raw, "latitude_range", 2, "target")
    if (
        longitude_range[0] >= longitude_range[1]
        or latitude_range[0] >= latitude_range[1]
    ):
        raise ValueError("target longitude and latitude ranges must increase")
    crs_path = resolved.parent / reject_unportable_path(
        _string(target_raw, "crs", "target"), field_name="target.crs"
    )
    if not crs_path.is_file():
        raise ValueError(f"target.crs does not exist: {crs_path}")

    geographic_crs = None
    if format_version == 2:
        geographic_crs = resolved.parent / reject_unportable_path(
            _string(target_raw, "geographic_crs", "target"),
            field_name="target.geographic_crs",
        )
        if not geographic_crs.is_file():
            raise ValueError("target.geographic_crs does not exist")

    sources_raw = raw.get("sources")
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ValueError("project.sources must be a non-empty array of tables")
    sources: list[SourceRef] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(sources_raw):
        context = f"sources[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{context} must be a table")
        _reject_unknown(item, _SOURCE_REF_KEYS, context)
        source_id = _string(item, "id", context)
        if source_id in seen_ids:
            raise ValueError(f"duplicate source id: {source_id}")
        seen_ids.add(source_id)
        acquisition_path = resolved.parent / reject_unportable_path(
            _string(item, "acquisition", context), field_name=f"{context}.acquisition"
        )
        acquisition = load_acquisition(acquisition_path)
        if acquisition.source_id != source_id:
            raise ValueError(
                f"{context}.id does not match acquisition source_id {acquisition.source_id!r}"
            )
        sources.append(
            SourceRef(
                id=source_id,
                acquisition_path=acquisition_path,
                priority=_integer(item, "priority", context),
                acquisition=acquisition,
            )
        )

    composite_raw = _table(raw, "composite", "project")
    _reject_unknown(composite_raw, _COMPOSITE_KEYS, "[composite]")
    composite_policy = _string(composite_raw, "policy", "composite")
    if composite_policy not in {"priority", "cutline", "blend", "mosaic"}:
        raise ValueError("composite.policy is not supported")
    missing_pixel = _integer(composite_raw, "missing_pixel", "composite")
    if not 0 <= missing_pixel <= 255:
        raise ValueError("composite.missing_pixel must be in 0..255")

    product_raw = _table(raw, "product", "project")
    _reject_unknown(product_raw, _PRODUCT_KEYS, "[product]")
    product = ProductConfig(
        kind=_string(product_raw, "kind", "product"),
        bounds=_number_tuple(product_raw, "bounds", 4, "product"),
        maximum_zoom=_integer(product_raw, "maximum_zoom", "product"),
        tile_size=_integer(product_raw, "tile_size", "product"),
        reprojection_resampling=_string(
            product_raw, "reprojection_resampling", "product"
        ),
        pyramid_resampling=_string(product_raw, "pyramid_resampling", "product"),
        pixel_type=_string(product_raw, "pixel_type", "product"),
        channels=_string(product_raw, "channels", "product"),
        format=_string(product_raw, "format", "product"),
        opaque=product_raw.get("opaque") is True,
        path_template=_string(product_raw, "path_template", "product"),
    )
    if product.kind != "display-texture":
        raise ValueError("product.kind must be display-texture")
    if product.maximum_zoom < 0 or product.maximum_zoom > 20:
        raise ValueError("product.maximum_zoom must be in 0..20")
    if (
        product.tile_size <= 0
        or product.tile_size > 256
        or product.tile_size & (product.tile_size - 1)
    ):
        raise ValueError("product.tile_size must be a power of two no larger than 256")
    if product.reprojection_resampling not in {
        "nearest",
        "bilinear",
        "cubic",
        "lanczos",
    }:
        raise ValueError("product.reprojection_resampling is not supported")
    if product.pyramid_resampling != "average":
        raise ValueError("product.pyramid_resampling must be average")
    allowed_channels = {"grayscale", "rgb"} if format_version == 2 else {"grayscale"}
    if (
        product.pixel_type != "uint8"
        or product.channels not in allowed_channels
        or product.format != "png"
        or not product.opaque
    ):
        raise ValueError(
            "products require opaque uint8 grayscale PNG; format 2 also supports RGB"
        )
    if product.path_template != "imagery/{z}/{x}/{y}.png":
        raise ValueError("the v1 path template must be imagery/{z}/{x}/{y}.png")
    expected_bounds = (
        longitude_range[0],
        latitude_range[0],
        longitude_range[1],
        latitude_range[1],
    )
    if grid and any(
        not math.isclose(a, b, abs_tol=1e-10)
        for a, b in zip(expected_bounds, grid.geographic_bounds, strict=True)
    ):
        raise ValueError("Target bounds do not match the declared tile grid")
    if product.bounds != expected_bounds:
        raise ValueError(
            "product.bounds must match target longitude and latitude ranges"
        )

    return TextureProject(
        path=resolved,
        format_version=format_version,
        grid=grid,
        base_texture=base_texture,
        product_id=_string(raw, "product_id", "project"),
        target=TargetConfig(
            id=_string(target_raw, "id", "target"),
            crs_path=crs_path,
            longitude_range=longitude_range,
            latitude_range=latitude_range,
            geographic_crs_path=geographic_crs,
        ),
        sources=tuple(
            sorted(sources, key=lambda source: (-source.priority, source.id))
        ),
        composite_policy=composite_policy,
        missing_pixel=missing_pixel,
        product=product,
    )
