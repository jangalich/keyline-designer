"""
fetch_contour_dem.py

Fetches the 3DEP elevation window behind the marketing page's contour
background and writes it to ~/keyline-scratch/contour-source.tif, as the
input to scripts/generate_contour_background.py.

THIS IS THE ONE SCRIPT HERE THAT NEEDS NETWORK. Its counterpart,
generate_contour_background.py, is fully offline and runs anywhere; this
one talks to USGS's National Map ImageServer and must be run from a normal
terminal with real internet access. That split is the whole reason there
are two scripts instead of one.

Run from the repo root:
    python3 scripts/fetch_contour_dem.py

Do NOT commit the GeoTIFF it writes. It is a build input, not an artifact
-- roughly a megabyte, regenerable at any time from this script, and
unwelcome in git history. Only the SVG that generate_contour_background.py
produces from it gets committed, and that goes to the frontend repo.

---------------------------------------------------------------------------
Why this script raises dem_data.MAX_GRID_DIMENSION
---------------------------------------------------------------------------

Three things a reader coming to this cold will not be able to reconstruct,
and without which `MAX_GRID_DIMENSION = 900` below reads like either a fix
or a landmine:

1. THIS IS A BUILD-TIME ASSET FETCH, NOT A PIPELINE RUN. Nothing imports
   this script. It is not reachable from api.py or main.py, it runs by
   hand roughly once ever, and its output feeds a static decorative asset
   -- not a report, not an analysis, not a recommendation shown to a user.
   Whatever budget the pipeline has to respect at request time does not
   apply to a script someone runs once at a terminal.

2. THE CAP EXISTS FOR FLOW ROUTING THIS SCRIPT NEVER TOUCHES. dem_data.py
   caps fetched grids at 300x300 because valley_delineation.py's
   depression fill, D8 flow direction and flow accumulation are pure
   numpy/Python, and their cost grows with cell count -- see that module's
   docstring and dem_data.py's own comment on the constant. This script
   fetches a DEM and writes it to disk. It runs no flow routing, imports
   nothing that does, and the contour generator downstream of it doesn't
   either (contourpy walks the grid once). The constraint the cap encodes
   is simply not present on this path.

3. LIFTING IT COSTS NO CORRECTNESS. The cap does not protect geometry --
   it only decides how many cells the window is divided into.
   get_dem_for_boundary() asks the ImageServer for output already
   projected into the local UTM zone (imageSR), then derives
   resolution_meters from the grid size it actually got, so every cell is
   true meters in both directions whatever the cap is set to. Clamped, the
   only consequence is a coarser grid with non-square pixels (this window
   would come back 300x300 at 7.68 x 9.35 m instead of 421x521 at 5 x 5 m).
   Nothing is stretched, squashed or misplaced either way. Raising the cap
   buys resolution; it does not trade away accuracy for it.

The assignment stays HERE, at the call site, and deliberately never in
dem_data.py: every pipeline caller keeps the 300-cell cap it was written
against, and this one script opts out of it for its own run only.
"""

import os
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

# Python puts THIS file's directory on sys.path, not the repo root, so a
# bare `import dem_data` fails however you invoke the script. Same line
# generate_contour_background.py carries, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dem_data  # noqa: E402

# See the module docstring above before changing this. Build-time asset
# fetch; the 300-cell cap guards valley_delineation.py's numpy flow
# routing, which nothing on this path runs; and the grid is true UTM
# meters at any cap, so this buys resolution without distorting anything.
dem_data.MAX_GRID_DIMENSION = 900

# The marketing background window: ~2.1 x 2.6 km of dissected Allegheny
# Plateau, which lands in UTM 17N (EPSG:32617).
MIN_LON, MIN_LAT, MAX_LON, MAX_LAT = -79.99781, 40.63363, -79.97257, 40.65688

OUT = os.path.expanduser("~/keyline-scratch/contour-source.tif")


def main() -> None:
    dem = dem_data.get_dem_for_boundary(
        [
            (MIN_LON, MIN_LAT),
            (MAX_LON, MIN_LAT),
            (MAX_LON, MAX_LAT),
            (MIN_LON, MAX_LAT),
        ],
        resolution_meters=5.0,
        # No buffer: the GeoTIFF should BE the requested window, so the
        # SVG's viewBox is the extent's own aspect ratio with no cropping
        # or recomputation downstream.
        buffer_meters=0.0,
    )
    print(dem_data.summarize_dem(dem))

    array = dem["array"]
    pixel_size_x, pixel_size_y = dem["resolution_meters"]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)

    # get_dem_for_boundary() returns a plain dict (array + origin +
    # resolution + crs), not a rasterio dataset -- that's deliberate on its
    # side, so every terrain module downstream stays rasterio-free and
    # unit-testable. Reconstructing the transform here is the cost of that,
    # and it's two lines.
    with rasterio.open(
        OUT,
        "w",
        driver="GTiff",
        height=array.shape[0],
        width=array.shape[1],
        count=1,
        dtype="float32",
        crs=dem["crs"],
        transform=from_origin(dem["origin_x"], dem["origin_y"], pixel_size_x, pixel_size_y),
        # dem_data.py hands back np.nan for nodata; GeoTIFF consumers are
        # better served by the same sentinel dem_data.py itself requests
        # from the ImageServer.
        nodata=dem_data.NODATA_VALUE,
        compress="deflate",
    ) as dst:
        dst.write(np.where(np.isnan(array), dem_data.NODATA_VALUE, array).astype("float32"), 1)

    print(f"Wrote {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)")
    print("Next: python3 scripts/generate_contour_background.py")


if __name__ == "__main__":
    main()
