# Display-texture tile contract

The local [sparse detail preview](sparse-preview-contract.md) has its own kind
and version discriminator and uses masks. The opaque baseline below remains the
production display-texture contract.

This contract is intentionally independent of any particular source or target
body. Individual products may choose different zoom ranges or colour encodings,
but deviations must be explicit and supported by the Pas de Géant consumer.

## Quest consumer performance decision

**New tile products use square tiles no larger than 256 × 256 across every body
and provider, including scientific terrain.** Earlier physical Quest testing
found performance problems with 512-pixel tiles; the owner reconfirmed that
finding on 13 October 2026. The consumer records the rationale in
`pas-de-geant/docs/quest-imagery-tile-size.md`.

The 512-pixel baseline below describes historical producer products and their
structural contract. The current producer rejects tile sizes above 256 pixels.
A successful producer validation proves the declared payload and registration,
not headset performance. Scientific elevation retains its separate numeric
contract and follows the same tile-size limit.

Prepare a genuine 256-pixel runtime derivative. Review the grid, coverage, zoom caps and metres per pixel:
reducing dimensions at unchanged zoom reduces sampling resolution. Generate
new descriptors, identities, manifests and checksums through the build path;
do not hand-edit existing 512-pixel metadata or relabel payloads. Historical
published products retain their original contract.

## Historical format 1 contract

- Geographic longitude/latitude target grid covering the declared body extent.
- XYZ addressing: `x` increases eastward and `y` increases southward.
- Square `2^z × 2^z` matrix with one whole-body tile at zoom 0.
- Pixel centers sample a uniform linear-longitude/linear-latitude grid.
- 512 × 512 pixels per tile.
- Opaque PNG for display imagery.
- Path `imagery/{z}/{x}/{y}.png`.
- No independent per-tile stretch, normalisation, or colour adjustment.
- Missing coverage is represented by the product's declared opaque fallback
  pixel unless a future, separately versioned consumer contract says otherwise.

For bounds `[west, south, east, north]`, a tile at `(z, x, y)` covers:

```text
n = 2^z
tile_west  = west  + (east - west) * x       / n
tile_east  = west  + (east - west) * (x + 1) / n
tile_north = north - (north - south) * y       / n
tile_south = north - (north - south) * (y + 1) / n
```

The antimeridian is a wrap boundary between the western edge of `x = 0` and the
eastern edge of `x = n - 1`; it is not duplicated as an extra column.

## Pyramid construction

The highest product level is built on the declared target grid. Lower levels
are generated using a declared area-averaging rule. The first implementation
should derive every lower level directly from the highest-resolution base so
that repeated byte rounding does not accumulate between levels.

Each level is materialized as a tiled intermediate whose block boundaries align
with output tiles. Emission copies exact 512-pixel windows without another
resampling step.

## Required product metadata

The output root should include generated, machine-readable metadata recording:

- product and target IDs;
- bounds, CRS resource checksum, and axis conventions;
- tile size, addressing, path template, zoom range, pixel type, channels, and
  opacity;
- nodata/missing-coverage rendering;
- resampling and rounding rules;
- source identities and checksums;
- per-level dimensions, tile counts, byte sizes, and manifest checksums;
- producer Git SHA and tool versions.

Metadata filenames and their schema must be fixed during implementation before
the first publication. Format version 1 uses:

- `product.json` for the product identity and per-level summaries;
- `manifests/tiles-z{z}.sha256` for canonical per-tile payload checksums;
- `tiles.validation.json` in the run evidence root for complete structural and
  pixel-reconstruction validation.

## Topography exclusion

This contract is for visual colour/intensity samples. It must not be reused for
elevation simply because both products can be tiled. Topography requires a
separate contract for units, vertical datum, numeric precision, nodata,
interpolation, quantization, range, and potentially mesh continuity.

## Explicit-grid contract (format version 2)

The baseline formulas above describe immutable format 1 geographic products.
Format 2 retains the square opaque XYZ payload, orientation, window emission
and direct-from-base pyramid rules, and declares this additional grid identity:

```json
{"formatVersion": 1, "projection": "web-mercator", "radiusMetres": 1737400}
```

`grid` is required in `product.json`. For tile `(z, x, y)`, `n = 2^z`:

```text
longitude(x) = -180 + 360*x/n
latitude(y)  = atan(sinh(pi*(1 - 2*y/n))) * 180/pi
```

Sample centres use fractional indices `x + (column + 0.5)/tile_size` and
`y + (row + 0.5)/tile_size`. There is no extra antimeridian column. GDAL target
extent is ±πR in metres and the sampled latitude limit is ±85.0511287798066°.
Using the same addresses does not make geographic and Mercator bytes equivalent.

The output also includes an independently generated full-latitude `base.png`.
`baseTexture` records `path`, `projection: "equirectangular"`, `width`, `height`
and `sha256`. Product identity authenticates the base descriptor as well as the
grid and tile payload. Full validation checks the base against its build and
reports grid coverage separately from base coverage of the poles.

The shared consumer checks the grid against the body's sphere/equatorial radius
and any companion elevation source. Mercator zoom 0 is never used as an
equirectangular base. Its caps use the full-latitude base and zero reference
height outside the streamed latitude extent. New products need new immutable
prefixes and cache identities; neither descriptor edits nor filename changes
constitute reprojection. Existing format 1 releases remain geographic.

Format 2 display products also support `tiles.channels: "rgb"`: opaque 8-bit
RGB PNG (colour type 2), with Red/Green/Blue channel order. The independent base
uses the same channels. Grayscale remains colour type 0. Alpha, palette and
`tRNS` transparency remain prohibited. Valid black samples, including the
declared opaque missing-coverage value, participate in pyramid averaging;
the produced levels and PNGs do not mark black as nodata. These are display
samples: an RGB night composite does not declare physical radiance or emission.
