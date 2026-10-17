# Project format

The parser accepts `format_version = 1` and `2` TOML project files. A project
describes semantic inputs and outputs; it must not
contain credentials or machine-specific run paths.

Relative paths resolve from the project file. Environment-variable expansion
inside TOML path values should be rejected so that the same file means the same
thing on every host. Local source, work, output, and evidence roots are supplied
as explicit CLI arguments or a separate ignored run file.

## Legacy geographic model

Version 1 retains its published geographic interpretation. The example below
records the historical 512-pixel recipe; the current producer rejects that tile
size for new builds. New products use 256 pixels or fewer.

```toml
format_version = 1
product_id = "moon-lroc-wac-global-morphology-100m-v1"

[target]
id = "moon"
crs = "../../../resources/crs/moon-geographic-1737400.wkt"
longitude_range = [-180.0, 180.0]
latitude_range = [-90.0, 90.0]

[[sources]]
id = "lroc-wac-global-morphology-100m-june-2013"
acquisition = "sources/lroc-wac-global-morphology-100m.toml"
priority = 100

[composite]
policy = "priority"
missing_pixel = 0

[product]
kind = "display-texture"
bounds = [-180.0, -90.0, 180.0, 90.0]
maximum_zoom = 8
tile_size = 512
reprojection_resampling = "bilinear"
pyramid_resampling = "average"
pixel_type = "uint8"
channels = "grayscale"
format = "png"
opaque = true
path_template = "imagery/{z}/{x}/{y}.png"
```

## Source acquisition specifications

Each source specification should declare:

- stable source ID and human-readable title;
- public landing page and exact payload URL or a documented manual retrieval
  method;
- expected filename, byte size, and SHA-256 once known;
- licence/citation and retrieval notes;
- expected raster dimensions, band count, type, CRS, geotransform, mask,
  nodata, scale, offset, and relevant metadata;
- coverage geometry or bounds;
- source-wide preprocessing, if any;
- whether network acquisition is allowed by the command being run.

Unknown values discovered during an explicit source-admission workflow may be
captured in a candidate report, but a production build must not silently bless
them. A human-reviewed update to the source specification comes first.

Source specifications therefore have an explicit `status` of `candidate` or
`admitted`. Discovery-page claims belong in `[reported]`. Only reviewed object
identity and raster metadata belong in `[expected]`; changing to `admitted`
requires a complete expected byte size, SHA-256, dimensions, band count, and
data type. The initial processor also requires a reviewed `source_crs` resource
before reprojection.

The two resampling choices are separate by design. `reprojection_resampling`
is used only while mapping admitted source pixels onto the maximum-resolution
target grid. `pyramid_resampling` is used to derive each lower level directly
from that base, and is fixed to area averaging (`average`) in format version 1.

Optional `license` and `license_url` fields record source terms alongside the
required citation; the URL must be public HTTPS. These fields participate in
the semantic identity and are carried into generated product metadata.
`acquire-project` downloads and inspects every source without admitting it.
After reviewing the inspection evidence, `admit-source` rechecks the local file
and updates the selected source specification with complete expected metadata
and SHA-256. Changed existing expectations are rejected.

## Target definitions

Targets are reusable across products, but a product pins the exact checked-in
definition. A target definition must make these choices explicit:

- datum and body-fixed frame;
- sphere radius or ellipsoid axes;
- prime meridian;
- longitude direction and range;
- latitude type;
- target geographic CRS and any projected CRS needed during processing.

Do not infer a lunar or planetary CRS from the source filename or from an EPSG
fallback.

## Product identity

The final report should compute a canonical product identity from at least:

- normalized project configuration;
- all source object checksums;
- checked-in CRS resource checksums;
- producer Git SHA and dirty-state assertion;
- material external tool versions;
- generated payload manifest checksum.

The Found in Space `catalogs` publication can then pin this identity and the
immutable storage prefix without committing the tile tree.

## Explicit grid and base texture (version 2)

Version 2 adds required `[grid]` and `[base_texture]` tables and
`target.geographic_crs`. The new WAC recipe is the complete runnable example:
`projects/moon/lroc-wac-global-100m-mercator-v2/project.toml`.

```toml
[grid]
projection = "web-mercator"
radius_metres = 1737400

[base_texture]
width = 2048
height = 1024
path = "base.png"
resampling = "average"
```

The supported projections are `web-mercator` and `geographic`; their normalized
XYZ matrix is always global. Target/product longitude and latitude bounds must
match that grid. Mercator bounds are ±180°, ±85.0511287798066°; warp extents are
in projected metres (±πR), not latitude degrees. GDAL validates the declared
sphere, projection, prime meridian, scale, offsets and units before building.
Generated level geotransforms must match the projected extent and dimensions.
The old format rejects these new tables rather than silently ignoring them.

The independent equirectangular base spans ±180°, ±90°, and is averaged from
the original admitted source, rather than from a clipped Mercator tile. Its CRS
resource hash, dimensions, resampling and payload hash participate in generated
build/product identity. Its opaque channels match the product's declared
`channels`: `grayscale` or, in format 2, `rgb`. The
base and polar display do not represent scientific heights.

Format 1 remains grayscale. Format 2 RGB sources contain exactly three Byte
bands in Red/Green/Blue order, and tiles use PNG colour type 2 with no alpha,
palette or transparency chunk. No colour transform is implied by `rgb`.

The processor supports single-source and compatible multi-source `priority`
and `mosaic` products. Multiple sources must share CRS, pixel size/orientation,
nodata, scale and offset; they are assembled into a VRT before reprojection.
`mosaic` rejects real overlap (sub-pixel georeferencing roundoff is tolerated).
For `priority`, the highest numeric priority wins; source ID breaks ties
lexicographically, with the last valid source winning. A changed source order
in the TOML does not change this precedence. See the eight-sheet Earth examples
at [`projects/earth/`](../projects/earth/README.md).

## Matched overlay projects

The opt-in `kind = "matched-overlay"`, `format_version = 1` project uses
source-wide display matching against an authenticated base product. It supports
admitted single-band rasters and registered archive imagery, fixed calibration
settings, source priorities, and hash-bound quality masks. Its complete schema,
algorithm and build instructions are documented in [matched overlays](matched-overlays.md).
It produces the existing format 2 sparse opaque tile contract.
