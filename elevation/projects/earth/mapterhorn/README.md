# Earth elevation from the Mapterhorn global archive

This workflow prepares 256 × 256 lossless Terrarium PNG tiles in a PMTiles v3
archive. It belongs to the elevation producer; no Geant code or live dataset is
changed. All outputs currently remain **scratch**, including a successful probe.
A sample archive is never the global product or an upload candidate.

Use Python 3.13+, run from `elevation/`, and set `UV_CACHE_DIR=.cache/uv` for
every uv command. The lock pins `pmtiles==3.7.0`, Pillow, NumPy and their build
environment. The independent HTTP validation recipe requires go-pmtiles 1.28.2
and records its full version, commit and binary hash.

## Source and scientific meaning

- Global source: <https://download.mapterhorn.com/planet.pmtiles>.
- [Data access](https://mapterhorn.com/data-access/) and
  [distribution documentation](https://github.com/mapterhorn/mapterhorn/blob/main/distribution/README.md).
- Inspect the actual archive header and metadata before acquisition. The
  admitted contract is gzip directories, uncompressed WebP payloads, zooms
  0–12 and Web Mercator bounds. Every admitted image must be RGB, 512 × 512,
  without transparency or colour-management metadata.
- A strong HTTP ETag binds each inspection range and resumable download.
  ETags, including multipart ETags, are **not** treated as payload checksums.
  Retain the upstream download manifest and published MD5. Full acquisition
  verifies that MD5, scans the archive structure and calculates SHA-256 before
  promotion. The downloaded original is made read-only and retained.
- EPSG:3857 uses a 6,378,137 m projection radius. That horizontal parameter says
  nothing about the height datum. Archive metadata does not establish a uniform
  ellipsoid, geoid or reference sphere. Preserve upstream values; apply no
  datum offset and claim no uniform vertical reference.
- At upstream revision
  [`775663e9ca58c7fe741dbd53fbe4461cb73a9bb9`](https://github.com/mapterhorn/mapterhorn/tree/775663e9ca58c7fe741dbd53fbe4461cb73a9bb9),
  [`aggregation_tile.py`](https://github.com/mapterhorn/mapterhorn/blob/775663e9ca58c7fe741dbd53fbe4461cb73a9bb9/pipelines/aggregation_tile.py)
  replaces missing scalar pixels with zero before encoding.
  [`downsampling_run.py`](https://github.com/mapterhorn/mapterhorn/blob/775663e9ca58c7fe741dbd53fbe4461cb73a9bb9/pipelines/downsampling_run.py)
  initializes absent child regions to zero. An encoded zero therefore cannot
  establish measured sea level or a valid observation. Preserve it, document
  this ambiguity, and retain absence for missing PMTiles entries. Do not create
  ocean terrain, bathymetry or a synthetic measurement mask.
- The inspected upstream code is supporting evidence, not proof of the exact
  producer revision used to build the remote archive. Metadata does not declare
  that revision. Likewise, an attribution JSON retrieved today is evidence of
  current notices; the archive does not bind it to its original build.
- Upstream documents zoom-dependent height rounding. Its 1/256 m Terrarium
  storage quantum does not imply millimetre measurement accuracy. Source z12
  has about 19.1 m projected sampling at the equator; output z13 preserves it.
  Neither Mercator bounds nor nonzero tile counts establish polar measurements.

Preserve [attribution.json](https://download.mapterhorn.com/attribution.json)
and the upstream documentation with retrieval times and SHA-256 checksums.
Source licences vary. The upstream BSD code licence does not license all terrain
data. Retain the individual attribution/licence references and resolve rights
evidence before approving a final distributable product.

## Inspect, acquire, prepare

Choose explicit durable run and incoming directories according to
`/data/fis/README.md`. These operational paths are supplied on the command line;
portable product metadata contains no machine-specific paths.

```sh
UV_CACHE_DIR=.cache/uv uv sync --frozen

# Bounded public inspection, a retained range sample and a validated scratch
# sample derivative. No global acquisition is started by this command.
UV_CACHE_DIR=.cache/uv uv run python -m foundinspace.terrain.mapterhorn probe \
  --output <durable-run>/probe

# Review capacity.json, samples.json, upstream-source.json, the retained
# download manifest, source evidence and licence notices before bulk work.
UV_CACHE_DIR=.cache/uv uv run python -m foundinspace.terrain.mapterhorn acquire \
  <durable-run>/probe/upstream-source.json --output <durable-incoming>

# Move the fully authenticated source and manifest to its immutable raw
# acquisition identity before selecting it as a controlled input.
UV_CACHE_DIR=.cache/uv uv run python -m foundinspace.terrain.mapterhorn build \
  <immutable-source>/planet.pmtiles <immutable-source>/source-manifest.json \
  --output <durable-run>/prepared --workers 2 --max-in-flight 4

UV_CACHE_DIR=.cache/uv uv run python -m foundinspace.terrain.mapterhorn validate \
  <immutable-source>/planet.pmtiles <durable-run>/prepared/earth.pmtiles \
  --output <durable-run>/prepared/independent-recheck.json

UV_CACHE_DIR=.cache/uv uv run python -m foundinspace.terrain.range_validation \
  <durable-run>/prepared/earth.pmtiles --output <durable-run>/http-check
pmtiles verify <durable-run>/prepared/earth.pmtiles
```

Acquisition requests bounded chunks, validates Content-Range and If-Match,
resumes the same partial source, and refuses insufficient disk capacity before
network IO. It reserves 8 GiB beyond the source download; that is an acquisition
guard, **not** enough space for the derivative. The probe's geographical PNG
sample extrapolation is not a statistically representative capacity forecast.
Budget source + derivative + SQLite index + safety margin before a full run.

The packer appends payloads directly into its eventual archive. SQLite stores
only working tile offsets and restart state; there is no giant unpacked tile
tree, whole-archive RAM buffer or second archive-sized packaging copy. Directory
leaves are emitted in bounded groups. Payloads are not deduplicated; their disk
size may exceed a deduplicated source significantly. The archive is valid v3
with an ordered index and an explicitly false `clustered` flag.

`build --workers N --max-in-flight M` controls CPU conversion. Defaults are two
workers and twice the worker count in flight (four with the default workers).
`--workers 1` converts inline without process startup or prefetch; its default
in-flight limit is one. Both options require positive integers. An explicit
limit smaller than the worker count is valid and may leave workers idle.

Workers receive only a source tile ID and its WebP bytes and return four sorted
PNG children. Processes use `spawn` on every platform, including macOS and Linux;
source readers, the archive stream and SQLite stay in the parent. No additional
system or container dependency is required beyond the locked elevation runtime.
Programmatic multiprocess callers must use an importable main module with the
usual `if __name__ == "__main__":` guard; the module CLI already does this.

The window counts all submitted source tiles until the parent has written their
children, including results completed out of order. A slow early tile closes
the window; later completions cannot trigger more reads. Source entries/runs
remain lazy, and tile payloads are identical across worker counts. Memory scales
with the selected window and workers, not archive length. This is a tile-count
bound, not a configurable byte or total-process-RAM ceiling. Conversion workers
may be underused when an early tile is slow or parent IO is limiting throughput.

Source `(z,x,y)` splits into `(z+1,2x+dx,2y+dy)`. `dx=0` is west and `dy=0`
north. Every scalar sample and negative value is preserved. Output z0 uses an
independent 2 × 2 arithmetic mean of decoded source-z0 heights in float64,
followed by nearest-even quantization at 1/256 m. This is equal weighting in the
projected pixel grid, not a geodetic surface-area average. Filtering encoded RGB
channels is forbidden. Maximum additional encoding error is 1/512 m.

Working checkpoints flush and fsync payloads before committing the matching
SQLite transaction. Resume discards only uncommitted partial bytes and rejects
changed source or producer identities. Recovery also handles interruption after
archive completion but before validation and descriptor placement. Do not
modify the code or lock while resuming. A fresh implementation needs a fresh
run directory.

Execution settings (`workers`, resolved `maxInFlight`, `startMethod` and the
128-source-tile checkpoint interval) are recorded in `recipe.json`,
`provenance.json` and the archive's build identity. Recipe version 2 binds them
to both partial and completed-archive recovery. Resume with the same settings;
changing them or resuming a version-1 build requires a fresh output directory.
Different settings preserve tile bytes and payload ordering but deliberately
change archive metadata and therefore the whole-archive hash.

Only complete source tiles advance the contiguous checkpoint. A worker failure,
Ctrl-C or parent write failure cancels queued tasks and waits for bounded running
work before closing the packer. Resume truncates bytes after the last durable
checkpoint and redoes that uncommitted batch, including any partial child group.
`product.json` is still placed only after final archive validation.

## Small local conversion benchmark

Use an authenticated small sample, such as the probe's read-only 16-source-tile
extract. Keep the sample, its manifest, reports and machine details outside Git:

```sh
UV_CACHE_DIR=.cache/uv uv run python examples/benchmark_mapterhorn.py \
  <sample>/sample-source.pmtiles <sample>/sample-source-manifest.json \
  --output <scratch>/benchmark.json --hardware '<CPU and RAM>' \
  --sample-tiles 16 --passes 16 --trials 3 --workers 1 2 4
```

Each trial repeats at most 16 source tiles through one fresh conversion pool
and verifies an identical ordered payload digest across trials/worker counts.
Use `--passes 1` separately to expose startup overhead for a tiny invocation.
The report records each duration, throughput, code/source identity and actual
execution settings. Timings include parent reads, IPC, PNG encoding, startup,
shutdown and ordered hashing. They exclude archive writing, fsync, z0 generation
and final validation. Warm repeated geographic samples do not predict global
runtime, cold-source IO, peak memory or output capacity.

## Validation and review bundle

Validation reads the **finished archive**. It scans every index entry, section,
count and coverage ID, and independently decodes deterministic tile samples
through integer Terrarium codes. It checks all pixels of selected quadrants and
calculates z0 independently by slicing and adding scalar codes. It does not
claim sampled comparisons validate every pixel or establish geodetic accuracy.
The probe selects mountain, coastal, negative-height, open-ocean and Mercator-edge
locations at source zooms 4, 8 and 12, plus z0; absent tiles remain absent.

HTTP validation starts a temporary loopback server for one static archive,
allows only range GETs, requires 206 responses, compares Python reader bytes
against local reads and repeats present-tile reads with the independent pinned
Go reader. The server stops on completion. This establishes local Range access,
not S3/CDN deployment, browser integration or headset acceptance.

Keep `earth.pmtiles`, `product.json`, `recipe.json`, `source-manifest.json`,
`provenance.json`, numeric/structural/HTTP reports, attribution/licence evidence,
the frozen source recipe/code and lock, run log and generated SHA-256 manifest
together. Keep operational logs and machine-specific paths separately.
`product.json` is written last after final archive validation.

Final candidates require a clean recorded producer commit, a complete source
snapshot, full provenance and rights evidence, and final archive validation.
The current implementation intentionally labels outputs scratch and
`uploadReady=false`; it does not promote samples or dirty builds. Prepare a
separately reviewed candidate from clean recorded code after these gates pass.
No catalog/Zenodo release, upload or runtime change is part of this workflow.

Proposed future object prefix, subject to final candidate identity:

```text
elevation/earth/mapterhorn-terrarium-256/20261018.1/
  earth.pmtiles
  product.json
  recipe.json
  source-manifest.json
  provenance.json
  validation.json
  http-validation.json
  NOTICE.md
  LICENSE.txt
  checksums.sha256
```

Dry-run template for the future reviewed bundle (never point this at a probe):

```sh
aws s3 sync <reviewed-upload-bundle>/ \
  s3://foundinspace/elevation/earth/mapterhorn-terrarium-256/20261018.1/ \
  --dryrun
```

Before Geant can use this product, reusable runtime code needs PMTiles transport,
256-pixel Terrarium decoding, sparse absence handling and explicit admission of
the unresolved Earth height reference. The old `elevation-xyz` v2 descriptor
cannot express these semantics. This task leaves that consumer work separate.
