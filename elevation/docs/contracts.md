# Production and consumer contracts

Projects declare one spherical body, one Mercator XYZ grid and ordered scalar
height sources with exact hashes and raster metadata. Paths resolve from the
project file. Source admission checks band count/type, dimensions, transform,
CRS, nodata and band scale/offset. Explicit conversion to metres and reference
radius are mandatory. Embedded georeferencing is used; a source without it
requires declared registration. No Earth CRS is assigned to another body.
New terrain products emit square tiles no larger than 256 × 256 pixels. Raising
the zoom cap by one when changing from 512 to 256 preserves the finest ground
sampling while reducing each tile's geographic footprint.
Native rotated rasters use conservative bounds from all four corners. Float
NaN nodata is represented explicitly in JSON specifications and remains masked
during scalar sampling. Heights require real scalar bands. Unregistered inputs
with an external mask need a registered GeoTIFF preparation step; registration
must not silently discard that mask.

Builds sample physical scalar heights using bilinear reprojection, exact
transforms and fixed unit kernel scale. Lower zooms are independently sampled
from the admitted sources, never averaged as RGB bytes. The project specifies
where and at which zooms detail tiles exist. Every emitted tile has complete
height coverage from the fallback and overlays; nodata never becomes zero.

The `elevation-xyz` format 2 descriptor uses the existing shared grid identity
and adds Terrarium encoding and sparse availability. Terrarium stores
`height = R*256 + G + B/256 - 32768` metres. Round to the nearest 1/256 m;
maximum encoding error is 1/512 m. Reject nonfinite and out-of-range heights
before encoding. RGB PNGs carry data bytes without colour correction.

Availability declares a complete global pyramid through `globalMaximumZoom`
and inclusive `[z, minX, minY, maxX, maxY]` detail ranges. Consumers select the
finest declared ancestor of a requested tile and crop it using ordinary XYZ
ancestor UVs. Missing declared tiles are errors, not evidence that a region is
flat. Imagery and elevation must share grid projection/radius; source identity
scopes persistent caches.

Transitions may feather local heights into fallback heights inside a declared
mask-edge distance. That is a representation choice, not a corrected scientific
measurement. Preserve original source values, record overlap height differences
and report the blend width. No automatic datum offsets or terrain exaggeration.

Build provenance includes the exact portable project, source/dependency hashes,
producer tree digest/Git state, Python/library/GDAL/PROJ versions, tile hashes,
availability and numeric reconstruction errors. Output is restartable per tile;
a descriptor is placed only after full validation. A dirty/local build remains
scratch, and cannot serve as citable release evidence. Raw sources and tile
payloads remain outside Git. Catalog publication requires pinned public code,
source rights/references and independently recorded consumer acceptance.

## Experimental Earth PMTiles contract

`earth-elevation-pmtiles` version 1 is a separate descriptor produced by the
[Mapterhorn workflow](../projects/earth/mapterhorn/README.md). It declares
PMTiles v3 transport, PNG Terrarium payloads, 256-pixel tiles, sparse directory
availability and an unresolved upstream vertical reference. EPSG:3857's
6,378,137 m projection radius is a horizontal grid parameter. It is **not** a
declaration that the source heights are above that sphere or the WGS84 ellipsoid.

This descriptor does not replace `elevation-xyz` version 2. Existing consumers
need explicit PMTiles loading, version admission and vertical-reference policy.
Consumers must distinguish absent archive entries from present zero pixels;
upstream's zero-filled missing pixels cannot be recovered as a measurement mask.
The current workflow labels every product scratch and does not approve uploads.
