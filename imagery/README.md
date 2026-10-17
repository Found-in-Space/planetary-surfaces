# Planetary surfaces: imagery

Reproducible planetary display textures, consumed by Pas de Géant and reusable
by other applications. This is the independent imagery project in
[Planetary surfaces](../README.md). Run the commands below from `imagery/`.

Part of [Found in Space](https://foundin.space/), which makes scientific data
and models into materials people can explore, question, and build with.

This repository owns acquisition verification, source inspection, coordinate
conversion, multi-source compositing, pyramid generation, tile emission, and
validation. It is designed for more than one upstream image and more than one
target body. The first planned product is the Moon's LROC WAC global morphology
mosaic, but the architecture must also accommodate later lunar detail layers,
Mars, Venus, and deliberately constructed Earth imagery.

The project model is target-neutral. This workflow does not contain viewer code
and does not publish large tile trees to Git. The stage-oriented production CLI
implements controlled acquisition, source inspection, raster construction,
opaque PNG emission, and exact full-product tile validation.

## Ownership boundary

| Location | Responsibility |
| --- | --- |
| This workflow | Deterministic texture production and validation |
| Reusable runtime packages | Loading, querying and rendering capabilities |
| [Pas de Géant](https://github.com/Found-in-Space/pas-de-geant) | Experience, interaction and composition of runtime capabilities |
| [Found in Space Catalogs](https://github.com/Found-in-Space/catalogs) | Citable publication metadata, evidence, checksums, and producer SHA |
| Object storage/CDN | Immutable generated tile payloads and optional source archives |

Scientific topography uses the independent [elevation workflow](../elevation/README.md)
in this repository. Elevation samples must preserve physical values, datums,
units, nodata semantics and encodings; its processing and validation contracts
remain separate from imagery.

## Imagery project layout

```text
docs/                    Architecture, contracts, implementation, and QA plans
projects/                Versioned product definitions and product notes
resources/crs/           Reviewed target-body CRS definitions
src/foundinspace/textures/
                         Producer implementation
tests/                   Synthetic fixtures and contract tests
```

Raw downloads, working rasters, generated tiles, and run evidence live outside
Git. Their locations will be supplied to the CLI or project-local run config;
they must never be inferred from machine-specific absolute paths committed to a
product definition.

## Small example and checks

```sh
UV_CACHE_DIR=.cache/uv uv sync --frozen
UV_CACHE_DIR=.cache/uv uv run python examples/synthetic.py /tmp/surface-imagery-example
UV_CACHE_DIR=.cache/uv uv run pytest tests/test_system_gdal.py
```

The example generates a projected gradient, emits five Mercator tiles and an
independent full-latitude base, then checks exact pixel reconstruction. It
requires system GDAL/PROJ and no source downloads. Change the generated gradient
in `examples/synthetic.py` to explore the transformation. Use a fresh output
directory. See [contribution checks](../CONTRIBUTING.md).

## CLI

The initial implementation exposes a stage-oriented `pas-de-geant-textures`
command. Acquisition and raster construction are intentionally separate so a
downloaded candidate cannot silently become a production input.

```text
download
inspect-source
preflight
build-base
build-levels
emit-tiles
validate-tiles
validate-project
acquire-project
admit-source
run
```

`run` preflights and orchestrates the same restartable production stages,
snapshots the exact definition and lockfile, and validates the complete tile
tree. `acquire-project` downloads and inspects all declared sources without
admitting candidates; `admit-source` records a source only after explicit review.
Richer publication reporting remains separate. See
[`docs/implementation-plan.md`](docs/implementation-plan.md) for the phased
handoff.

Run the CLI through the locked environment:

```console
UV_CACHE_DIR=.cache/uv uv run pas-de-geant-textures --help
```

`build-overlay` adds opt-in source brightness matching, multiscale boundary
blending, and explicit quality exclusions against a pinned base tile product.
See [matched overlays](docs/matched-overlays.md) and the
[six Apollo recipes](projects/moon/matched-overlays/README.md). It emits the
existing opaque sparse tile contract and records fitting/validation evidence;
headset viewing gamma stays in the consumer.

The processor currently delegates raster operations to the system
`gdalinfo`, `gdalwarp`, `gdal_translate`, and `proj` executables. Their exact
versions and commands are recorded in evidence and stage state. Preserve the
matching uv lockfile and record the native tool versions. For display textures,
publication requires a documented process and representative spatial and visual
checks; byte-identical regeneration, a pinned OS image, and a full pyramid
rebuild are not release requirements. Published-file checksums still protect
payload identity and transfer integrity.

## First product

The initial product is documented at
[`projects/moon/lroc-wac-global-100m/README.md`](projects/moon/lroc-wac-global-100m/README.md).
Its historical release targets opaque 8-bit grayscale PNG tiles at zoom levels
0–8 using a square geographic XYZ matrix with 512-pixel tiles. New production
is capped at 256-pixel square tiles.

Its checked-in source specification is admitted and pins the reviewed object
identity, raster metadata, and lunar source CRS. `emit-tiles` removes nodata
metadata before PNG creation so black remains an opaque display value;
`validate-tiles` decodes every PNG and exactly reconstructs every level raster.

The [Mercator migration candidate](projects/moon/lroc-wac-global-100m-mercator-v2/README.md)
adds an explicit format 2 grid and independent full-latitude base, preserving
existing geographic recipes/releases. The
[Apollo Mercator recipe](projects/moon/apollo11-preview-mercator-v2/README.md)
prepares matching sparse context/detail for the same 3D runtime. Native sampling,
polar display, source admission and producer/consumer checks are documented in
those recipes and the tile contract. No scientific elevation processing is
introduced here.

The [six-site Apollo recipes](projects/moon/APOLLO_IMAGERY.md) add matching
context and registered NAC detail for Apollo 12, 14, 15, 16 and 17, and collect
them with the existing Apollo 11 tiles behind one validated runtime descriptor.

## Earth day and night products

The [Blue Marble and Black Marble workflow](projects/earth/README.md) provides
matching format 2 RGB products: NASA's unshaded July 2004 Blue Marble base map
and Black Marble 2016 colour composite. Each recipe declares all eight 500 m
source sheets, a complete 256-pixel zoom 0–9 Mercator pyramid and a separate
4096 × 2048 full-latitude base. The source specifications pin complete payload
hashes admitted after acquisition and metadata review. The workflow includes
source admission, disk planning, reproducible build commands and publication
handoff; it does not upload payloads or alter runtime lighting.

## Licence

Repository software and Found in Space-authored documentation are available
under the [MIT License](LICENSE). Upstream imagery and generated products retain
their own source terms; each publication must record those terms separately.
