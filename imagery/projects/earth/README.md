# Earth: Blue Marble and Black Marble

These independent products prepare full opaque RGB pyramids for static object
storage, using the existing format 2 display-imagery contract:

| Recipe | Source | Display role |
| --- | --- | --- |
| [Blue Marble](blue-marble-200407-500m/project.toml) | NASA Blue Marble: Next Generation, July 2004, **Base Map** | Cloudless daytime colour, without added topographic shading or bathymetry |
| [Black Marble](black-marble-2016-500m/project.toml) | NASA Black Marble 2016, **colour maps** | NASA's rendered nighttime colour composite |

Each recipe mosaics eight 21,600 × 21,600 GeoTIFF sheets (A1–D1 north,
A2–D2 south) into the 86,400 × 43,200 source grid. Source coordinates are
WGS 84 geographic longitude/latitude. The two observation periods differ;
these are historical composites, not a contemporaneous or live Earth view.
The nominal 500 m grid does not imply additional sensor detail from resampling.

## Output and representation

Both products have identical coverage and addressing:

- EPSG:3857 Web Mercator, radius 6,378,137 m, XYZ addressing.
- Complete zooms **0–9**, **256 × 256**, opaque 8-bit **RGB PNG** tiles.
- **349,525 tiles per product** under `imagery/{z}/{x}/{y}.png`.
- Maximum raster 131,072 × 131,072: about 306 m/pixel at the equator. This
  retains the source detail with some oversampling; it does not create 306 m
  observations. Zoom 8 alone would sample at about 611 m/pixel.
- Independent **4096 × 2048** equirectangular `base.png`, covering both poles.
  Mercator tiles end at ±85.0511287798066°.
- Generated `product.json`, per-level SHA-256 manifests, complete pixel
  reconstruction validation, exact configuration snapshots and run evidence.

The producer mosaics the sheets before reprojection so interpolation can cross
sheet boundaries. Non-overlapping `mosaic` sources must share CRS, pixel size,
band encoding, nodata, scale and offset. Real overlap requires explicit
`priority` compositing; higher priority wins, with source ID breaking ties.
No per-sheet or per-tile colour adjustment, gamma, lighting or normalisation is
applied. Each lower level is averaged directly from the maximum-resolution
level. Valid black pixels remain opaque and participate in averaging.

Black Marble's colour composite includes NASA's presentation choices and
background. It is **not a lights-only emission mask or calibrated radiance**.
The producer preserves that colour composite. Any nighttime blending,
terminator, emission treatment and Sun-direction calculation belong to the
consumer; they are not baked into either pyramid. A future lights-only product
would need an explicit source and display-transfer recipe.

## Source status and storage budget

The checked-in sources are **admitted**. Acquisition and inspection on
17 October 2026 verified the full payload SHA-256 checksums, exact URLs, byte
sizes, RGB bands, WGS 84 CRS, geotransforms and absence of nodata. Initial HTTP
header and GDAL range checks were followed by complete downloads and metadata
review. Reacquisition authenticates against the pinned checksums. Neither
acquisition nor production automatically admits a new candidate.

Reported compressed source totals are approximately **2.41 GB for Blue Marble**
and **2.43 GB for Black Marble**. One maximum RGB raster holds 48 GiB of
uncompressed samples; all raster levels hold about 64 GiB. PNGs, temporary
validation exports and atomic stage outputs require additional space. Preflight
uses channel-aware, conservative working estimates (about 189 GB per product
before compression), and reports actual free space. Review that report before
acquisition or a full build. Build the products sequentially unless the combined
disk and memory budget supports both.

Sequential stages share one base-sized temporary allowance: an atomic raster
replacement, a resumed tile-emission batch and a validation export never run
simultaneously. On a constrained host, a fresh in-progress product and its
reproducible work rasters may use bounded SSD scratch. Keep sources and evidence
durable, enforce a free-space reserve while building, then verify and promote
the completed payload to durable storage before clearing scratch. The scratch
copy must never become a live service's only dataset.

Use the persistent data volume. Keep active acquisition in `incoming/`, promote
verified sources to an immutable `raw/` acquisition directory, and keep work,
output and evidence roots distinct. No source payloads or generated tiles go
into Git. A small `/tmp` synthetic test is not a production source or product.

## Acquire, review, admit, build

Run from `imagery/`. Set `FIS_DATA_ROOT` to the durable data root and choose
unique acquisition/run IDs. This example selects Blue Marble; repeat it for
`black-marble-2016-500m` with separate acquisition, run and output roots.

```sh
UV_CACHE_DIR=.cache/uv uv sync --frozen
export MARBLE_RECIPE=blue-marble-200407-500m
export MARBLE_PROJECT="projects/earth/$MARBLE_RECIPE/project.toml"
export MARBLE_ACQUISITION_ID=20261017-blue-marble-200407
export MARBLE_RUN_ID=20261017-blue-marble-200407-mercator-256
export MARBLE_INCOMING="$FIS_DATA_ROOT/incoming/surfaces/earth/$MARBLE_ACQUISITION_ID"
export MARBLE_RAW="$FIS_DATA_ROOT/raw/surfaces/earth/$MARBLE_ACQUISITION_ID"
export MARBLE_RUN="$FIS_DATA_ROOT/runs/surfaces/earth/$MARBLE_RUN_ID"

UV_CACHE_DIR=.cache/uv uv run pas-de-geant-textures preflight "$MARBLE_PROJECT" \
  --source-root "$MARBLE_INCOMING" --work-root "$MARBLE_RUN/work" \
  --output-root "$MARBLE_RUN/output" --evidence-root "$MARBLE_RUN/evidence"
```

Without local source files, `ready_for_processing` is false. Review the source expectations, roots,
tools and disk estimate, then acquire the source files:

```sh
UV_CACHE_DIR=.cache/uv uv run pas-de-geant-textures acquire-project "$MARBLE_PROJECT" \
  --source-root "$MARBLE_INCOMING" --evidence-root "$MARBLE_RUN/acquisition"
```

This downloads sequentially, retains resumable HTTP state and receipts, hashes
every file, and writes complete GDAL inspection evidence. Rerun the same command
to resume. Inspect `acquisition.json` and `sources/*.inspection.json`, including
hashes, dimensions, colour bands, georeferencing, masks and source-wide metadata.
For a new candidate revision, after that review explicitly record the admitted
contracts (the current pinned revision has already completed this step):

```sh
for MARBLE_SOURCE in "projects/earth/$MARBLE_RECIPE/sources/"*.toml; do
  UV_CACHE_DIR=.cache/uv uv run pas-de-geant-textures admit-source "$MARBLE_SOURCE" \
    --source-root "$MARBLE_INCOMING" --evidence-root "$MARBLE_RUN/admission"
done
```

`admit-source` updates the selected **versioned source specification** with the
verified SHA-256 and complete raster expectations. It re-inspects each local
file and refuses mismatches with existing expectations. Review and commit those
specifications. Preserve the acquisition evidence with the immutable source
acquisition. Promote the verified source directory on the same data filesystem
only when the destination does not already exist:

```sh
mkdir -p "$(dirname "$MARBLE_RAW")"
test ! -e "$MARBLE_RAW" && mv -T "$MARBLE_INCOMING" "$MARBLE_RAW"
```

Commit and push the producer implementation and admitted sources before
generating release evidence. For a controlled local build:

```sh
UV_CACHE_DIR=.cache/uv uv run pas-de-geant-textures run "$MARBLE_PROJECT" \
  --source-root "$MARBLE_RAW" --work-root "$MARBLE_RUN/work" \
  --output-root "$MARBLE_RUN/output" --evidence-root "$MARBLE_RUN/evidence" \
  --threads 2 --warp-memory-mb 512
```

`run` requires successful preflight, snapshots the exact project, acquisitions,
CRS files, project metadata and lockfile, and invokes the existing restartable
build stages followed by full tile validation. It records progress and failures
in `run.json`; the work root retains detailed stage commands, versions and
hashes. Rerun with identical definitions and roots to reuse completed stages.
A changed definition requires a new evidence root and immutable product prefix.

The individual `build-base`, `build-levels`, `emit-tiles` and `validate-tiles`
commands remain available for diagnosis. Neither the recipes nor `run` upload
to S3 or modify a viewer. Catalog publication must pin a clean public producer
SHA and authenticate both payloads before upload to distinct immutable prefixes.

## Sources, credits and reuse

- [Blue Marble base maps](https://science.nasa.gov/earth/earth-observatory/blue-marble-next-generation/base-map/):
  NASA Earth Observatory; Reto Stöckli (NASA/GSFC), July 2004.
- [Black Marble flat maps](https://science.nasa.gov/earth/earth-observatory/earth-at-night/maps/):
  NASA Earth Observatory; Joshua Stevens, using Suomi NPP VIIRS data from
  Miguel Román (NASA/GSFC), 2016.
- [NASA media usage guidelines](https://www.nasa.gov/nasa-brand-center/images-and-media/)
  and [SVS reuse guidance](https://svs.gsfc.nasa.gov/help/).

Keep each dataset's supplied credits and source terms in the catalog publication
and consumer attribution. Acknowledge the NASA source and identify our derived
processing without implying NASA endorsement. Source citations and terms are
carried in `product.json`; the repository's MIT licence does not replace them.
