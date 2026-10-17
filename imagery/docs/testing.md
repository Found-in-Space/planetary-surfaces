# Testing and validation

Tests are split between producer correctness and Pas de Géant integration. A
viewer screenshot cannot substitute for pixel-level producer validation, and
producer tests cannot prove that the viewer requests the correct tiles.

## Synthetic fixture

Generate the fixture from code so its expected values and spatial locations are
known. It should include:

- labeled hemispheres and cardinal directions;
- longitude and latitude gradients;
- distinct patterns touching each side of the antimeridian;
- features at both poles;
- constant-value regions crossing tile boundaries;
- exact byte values 0 and 255;
- masked or missing regions;
- a second optional source with partial coverage and overlap.

The fixture must begin in a projected planetary CRS so the test exercises real
reprojection, not just window splitting.

## Producer tests

Project and source contracts:

- reject unsupported format versions and unknown semantic fields;
- resolve relative paths from the project file;
- reject environment interpolation and committed absolute run paths;
- detect changed source bytes and metadata mismatches;
- prove deterministic source ordering and compositing.

Tile structure:

- exactly `4^z` tiles at level `z` and the declared total count;
- every new PNG decodes to the declared square size, at most 256 × 256, with the declared bit depth and channels;
- no alpha channel, palette transparency, or PNG transparency chunk for opaque
  products;
- path coordinates are in range and no expected coordinate is absent;
- tile bytes and manifests are stable across identical clean runs.

Pixel and spatial correctness:

- each decoded tile equals the corresponding level-raster window exactly;
- assembling all tiles reproduces the complete level raster exactly;
- coordinate tests prove eastward `x` and southward `y`;
- known synthetic features land at expected longitudes, latitudes, poles, and
  antimeridian sides;
- missing data becomes the declared opaque value;
- constant regions remain constant across tiles and levels;
- parents match the declared area-average and byte-rounding rule.

Operational behavior:

- interruption never promotes a partial stage;
- valid completed stages resume without rewriting outputs;
- changed configuration or input invalidates the correct downstream stages;
- work is bounded under an instrumented memory limit;
- free-space preflight and overlapping-root checks fail safely;
- reports contain complete tool versions, timings, counts, sizes, and hashes.

## Full-product validation

The WAC build additionally requires:

- source metadata comparison against the reviewed acquisition specification;
- per-level tile count and byte-size reconciliation;
- complete decoding/structure validation for all 87,381 tiles;
- exact pixel-window checks for all tiles, or a documented equivalent streaming
  comparison that covers every pixel;
- selected inverse-coordinate checks against the original source;
- manual review of global brightness consistency and source coverage;
- preservation of all generated logs and reports for the `catalogs` handoff.

## Pas de Géant integration

Tests in the application repository should cover:

- imagery zoom 8 while elevation remains independently limited to zoom 5;
- intended behavior for imagery zoom 9 and elevation zoom 6;
- at most 256-pixel square tile dimensions and one-tile level-zero matrix;
- XYZ orientation and proxy URL construction;
- proxy operation against a local static tile server;
- interactive inspection at poles, antimeridian, boundaries, and zoom changes;
- no unexpected missing-tile requests during ordinary navigation.

## Grid migration checks

`test_system_gdal.py` builds a real small projected source through both legacy
geographic and explicit Mercator contracts, emits/validates the full pyramid,
checks the metre geotransform and authenticates a full-latitude polar base.
`test_grid.py` checks inverse coordinates, exact-edge ranges, pixel centres and
masks. `test_preview_tiles.py` samples a registered archive row gradient onto
nonlinear Mercator rows, preserving valid black and masked holes.

The sampling fixture is generated from the producer grid implementation:

```sh
UV_CACHE_DIR=.cache/uv uv run python scripts/write-grid-contract-fixture.py \
  tests/fixtures/surface-grid.json
```

The producer test verifies that this file is reproducible. The application
keeps the same fixture at `tests/surface-grid.fixture.json` and checks bounds,
orientation, pixel-centre positions, ancestor crops and antimeridian wrapping
for both body radii and both projections. Copy a regenerated fixture to the
application repository when the grid contract changes; never hand-edit it.
Application tests also exercise real GDAL numeric height encoding, source
admission, grid/cache incompatibility, mesh elevation UVs, and measured crop
bounds with both Terrarium and RG16 heights. These checks use only tiny local
synthetic sources. Full payload and Quest acceptance remain separate steps.

## Earth RGB pyramids

`test_earth_marbles.py` parses both real recipes and builds an eight-sheet,
synthetic WGS 84 RGB globe through explicit admission and the full `run` command
path. It checks the independent base against analytically averaged source
pixels (including valid black), RGB channel order, opaque encoding, exact
tile reconstruction, unchanged completed raster reuse and overlap rejection.
No large NASA source files or network access are required. These tests do not
establish visual acceptance of the full historical NASA composites.

## Matched overlay validation

`test_photometry.py` checks robust gain/offset recovery with outliers, rejection
of degenerate overlap, unchanged fallback inside holes, invalid-source exclusion
from blend bands, and globally consistent field sampling across tiles/zooms.
`test_overlay.py` builds real GDAL float rasters through the complete command path,
checks repeatable product identity and opaque output, authenticates reference
tiles and matching reports, and exercises explicit quality-mask fallback.
All six Apollo recipes are parsed and checked against the calibration budget.
See [matched overlays](matched-overlays.md) for reduced-zoom review and production
acceptance boundaries.
