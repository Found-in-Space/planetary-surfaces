# Implementation plan

This is the implementation handoff. Complete the work in small, reviewable
phases; do not begin with the full 5.5 GB lunar source.

## Phase 1: contracts and toolchain spike

Status: initial implementation complete. Publication preserves the uv lockfile
and records the external GDAL/PROJ versions and representative display checks.

1. Choose the implementation boundary between Python and GDAL command-line
   tools. Prefer explicit subprocess commands when they make tool versions and
   exact operations easier to record; do not hide critical defaults.
2. Confirm Python 3.13 compatibility for the selected raster metadata and PNG
   libraries.
3. Define JSON schemas (or equivalently strict typed models) for project files,
   stage state, product metadata, validation reports, and manifests.
4. Add the versioned Moon target CRS with radius exactly `1,737,400 m`, then
   validate it with the chosen GDAL/PROJ versions.
5. Implement strict project parsing, canonical serialization, relative-path
   rules, and rejection of environment expansion.
6. Add CI, Ruff, pre-commit, and tests only after the dependency choices are
   proven locally.

Decision gate: record the chosen GDAL installation strategy. Python wheels that
quietly bundle a different GDAL/PROJ stack from the system tools can undermine
reproducibility.

The initial boundary uses Python for strict contracts, hashing, receipts,
restart state, and orchestration, and invokes system GDAL/PROJ executables for
raster operations. State records the complete command and versions. This is
sufficient for a traceable display-texture process when combined with the
recorded source, project, uv lockfile, and representative checks. Record native
tool versions separately. A hermetic system image, byte-identical regeneration,
and a full pyramid rebuild are not publication gates for display textures.

## Phase 2: synthetic end-to-end product

Status: source inspection, preflight, maximum-level reprojection,
direct-from-base lower-level construction, opaque tile emission, and exact
streaming validation are implemented. The `run` orchestrator now snapshots the
definition and lockfile and invokes these same stages through validation.
Rich publication reporting remains.

Build a small projected lunar source and a zoom 0–2 product exercising the real
pipeline. The fixture must contain unmistakable north/south/east/west markers,
longitude and latitude gradients, antimeridian features, polar features,
constant regions, black and white samples, and masked holes.

Implement restartable stages:

1. `inspect-source`: capture complete source metadata and enforce expectations.
2. `preflight`: resolve identities, estimate disk, verify tools and free space,
   and refuse unsafe overlap between source/work/output roots.
3. `build-base`: normalize sources and create the highest target-grid level.
4. `build-levels`: create lower levels with the declared area rule.
5. `emit-tiles`: copy aligned windows into opaque PNGs using atomic writes.
6. `validate`: run structural, pixel, spatial, and pyramid checks.
7. `report`: generate manifests, checksums, timings, versions, and publication
   evidence.
8. `run`: orchestrate the same stages without bypassing their contracts.

Every stage needs a durable state record containing configuration identity,
input checksums, output checksums, start/end times, status, and diagnostic
paths. State must become complete only after atomic output promotion.

## Phase 3: multiple-source proof

Status: compatible-raster `mosaic` and `priority` composition are implemented,
including RGB production and exact streaming RGB validation. The eight-sheet
synthetic Earth case checks sheet registration, valid black, polar fallback,
admission, resume and overlap rejection. The Blue/Black Marble recipes declare
full 500 m source sheets, acquired and admitted by exact SHA-256 and reviewed
metadata on 17 October 2026. Their production builds use those pinned inputs.

Add a second synthetic source with partial coverage and deliberate overlap.
Prove at least `priority` compositing and one explicit regional/cutline case.
Tests must demonstrate stable results regardless of filesystem enumeration
order and must catch undeclared overlap behavior.

Do not implement sophisticated blending until a real product requires it. The
format should reserve the concept without guessing a scientifically acceptable
algorithm.

## Phase 4: admit the LROC WAC source

1. Recheck available space on `/data` and define separate source, work, output,
   and evidence directories.
2. Download through an explicit acquisition command or document the exact
   external retrieval. Record URL, time, response metadata, byte size, and
   SHA-256. Never package credentials.
3. Save complete `gdalinfo -json` output before conversion.
4. Compare actual dimensions, CRS, band, datatype, mask, nodata, scale, and
   offset with the current assumptions. Update the source specification only
   after review.
5. Confirm whether byte values are already display-ready. Do not add `-scale`
   or histogram normalisation by default.
6. Run a representative regional build and measure compression, speed, memory,
   and temporary-space behavior.
7. Recalculate the full-run disk budget with safety margin.
8. Build the complete product only after the source contract and budget are
   accepted.

The intended maximum base is 131,072 × 131,072 pixels (zoom 8 with 512-pixel
tiles) over `[-180, -90, 180, 90]`. The complete zoom 0–8 pyramid contains
87,381 tiles. These are planned choices until the source inspection and Pas de
Géant integration confirm them.
The current 256-pixel Mercator recipe retains the 131,072-pixel maximum raster
at zoom 9 and emits 349,525 tiles.

## Phase 5: consumer integration and publication

In `pas-de-geant`, separate imagery maximum zoom from elevation maximum zoom
through configuration, proxy validation, viewer/provider construction, cache
keys, and fallbacks. Describe existing 512-pixel products and their one-tile
level-zero geographic matrix accurately for structural and archive checks.
Their dimensions do not establish Quest runtime acceptance: new imagery and
terrain products use at most 256-pixel square tiles because prior physical
testing found performance problems with 512-pixel tiles. Prepare
and validate actual runtime derivatives under new identities; see the
[Quest consumer decision](tile-contract.md#quest-consumer-performance-decision).
Keep the application's local tile builder only for fixtures.

Validate the actual WAC product in the viewer at both poles, the antimeridian,
tile boundaries, and zoom transitions. Once accepted, create a `catalogs`
publication under the next `YYYYMMDD.N` release ID. It must pin this repository
by public URL and exact Git SHA, authenticate the externally hosted payload, and
declare whether the release is a standalone work or a new immutable version of
an evolving series.

## Open decisions

- Future GDAL/PROJ upgrades and their representative display checks.
- Raster I/O library used for exact-window validation and PNG emission.
- Manifest granularity for tens of thousands of objects.
- Canonical JSON and configuration identity rules.
- Published object-store prefix and immutability controls.
- Future colour encodings beyond the format 2 opaque RGB PNG contract.
