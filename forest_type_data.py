"""
forest_type_data.py

USFS FIA FOREST TYPE GROUP on the DEM grid for the site data report's
Trees & forestry section: the forest type group the Forest Inventory and
Analysis program's BIGMAP model assigns to every 30 m pixel in the
parcel's window, so the parcel's modelled forest can be tabled by area
beside the canopy the lidar measures.

    get_forest_type_for_boundary(boundary)  -> the raw TIFF bytes (a fetch)
    parse_forest_type(raw)                  -> the parsed block
    class_counts(array, mask)               -> {code: cells}

THE SERVICE is `USFS_FIA_BIGMAP_CONUS_ForestTypeGroup_2018` on the same
IIPP host, with the same exportImage pattern, as nlcd_landcover_data.py
and canopy_cover_data.py: the DEM's own window (dem_data.dem_window_
bounds() at the DEM's buffer and resolution), in the DEM's UTM zone, at
the DEM's pixel dimensions, nearest-neighbour, so the returned array is
cell-for-cell aligned with the DEM and every mask this pipeline builds.
The native pixel is 30 m (Web Mercator); a 5 m cell here is a nearest-
neighbour sample of it, and any share computed from cells is a share of
30 m pixels. Verified live for the reference parcel (branch 11, step 0,
2026-09-23): one catalogue item, CONUS_forest_type_group_2018, U16, no
authentication, 1 s.

WHAT IT IS, AND WHAT IT IS NOT. BIGMAP imputes FIA inventory plots
(213,000 of them, measured 2014-2018) to Landsat 8 pixels of the same
years through an ecological ordination model, then assigns each pixel the
forest type group of the plots imputed to it. It says what TYPE of forest
occupies an area, at the grain of the model; it does not say what stands
on any given acre, and it is not a canopy measurement: on the reference
parcel it calls 4.9% of the ground forest where the lidar measures 12.0%
canopy at 15 ft. The section reports both, each labelled by what it
measures. Code 0 is non-forest (a real class, not nodata); 999 is
nonstocked forest land.

NO RASTER ATTRIBUTE TABLE IS SERVED (hasRasterAttributeTable is false),
so the code table is bundled below from the service's own description
and legend: FIA's forest type group codes (COND.FORTYPCD joined to
REF_FOREST_TYPE.TYPGRPCD). Any pixel value outside it is nodata (NaN in
the parsed grid), never a class.

TERMS. Credit line on the service: "USDA Forest Service, Forest Inventory
and Analysis (FIA)". No licence text is published on the service or its
item; a U.S. federal work, in the public domain, so commercial use is
permitted with attribution. Citation: Wilson, B.T., Knight, J.F.,
McRoberts, R.E. 2018, Harmonic regression of Landsat time series for
modeling attributes from national forest inventory data, ISPRS Journal of
Photogrammetry and Remote Sensing 137: 29-46.
"""

from typing import Optional

import numpy as np
import requests
from rasterio.io import MemoryFile

import fetch_attempts
from dem_data import dem_window_bounds

FOREST_TYPE_IMAGESERVER = (
    "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/"
    "USFS_FIA_BIGMAP_CONUS_ForestTypeGroup_2018/ImageServer"
)
FOREST_TYPE_VINTAGE = "2014–2018"
FOREST_TYPE_PRODUCT = "FIA BIGMAP forest type group, 2018"
FOREST_TYPE_NATIVE_RESOLUTION_METERS = 30.0

NON_FOREST = 0
NONSTOCKED = 999

# The FIA forest type group codes the service publishes, value -> name,
# from its description and legend (step 0). Non-forest and nonstocked
# are classes: a parcel with no forest reads as non-forest, not as nodata.
FOREST_TYPE_GROUPS = {
    0: "Non-forest",
    100: "White / red / jack pine",
    120: "Spruce / fir",
    140: "Longleaf / slash pine",
    150: "Tropical softwoods",
    160: "Loblolly / shortleaf pine",
    170: "Other eastern softwoods",
    180: "Pinyon / juniper",
    200: "Douglas-fir",
    220: "Ponderosa pine",
    240: "Western white pine",
    260: "Fir / spruce / mountain hemlock",
    280: "Lodgepole pine",
    300: "Hemlock / Sitka spruce",
    320: "Western larch",
    340: "Redwood",
    360: "Other western softwoods",
    370: "California mixed conifer",
    380: "Exotic softwoods",
    390: "Other softwoods",
    400: "Oak / pine",
    500: "Oak / hickory",
    600: "Oak / gum / cypress",
    700: "Elm / ash / cottonwood",
    800: "Maple / beech / birch",
    900: "Aspen / birch",
    910: "Alder / maple",
    920: "Western oak",
    940: "Tanoak / laurel",
    950: "Other western hardwoods",
    960: "Other hardwoods",
    970: "Woodland hardwoods",
    980: "Tropical hardwoods",
    990: "Exotic hardwoods",
    999: "Nonstocked",
}

FOREST_TYPE_CITATION = (
    "USDA Forest Service, Forest Inventory and Analysis, BIGMAP forest type group 2018 (FIA plots 2014–2018 imputed "
    "to Landsat 8 pixels), 30 m, served by the USDA Forest Service's Image Services (IIPP) "
    "USFS_FIA_BIGMAP_CONUS_ForestTypeGroup_2018 image service."
)
FOREST_TYPE_TERMS = "U.S. federal work; public domain. Credit: USDA Forest Service, Forest Inventory and Analysis."


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


@fetch_attempts.publishes
def get_forest_type_for_boundary(boundary_coordinates: list) -> dict:
    """
    ONE exportImage request for the boundary's DEM window -- the raw TIFF
    bytes plus the window asked for (what a fixture stores):

        {'window': dem_window_bounds() dict, 'tiff': bytes}
    """
    window = dem_window_bounds(boundary_coordinates)
    min_x, min_y, max_x, max_y = window["bbox"]
    width, height = window["size"]
    params = {
        "bbox": f"{min_x},{min_y},{max_x},{max_y}",
        "bboxSR": window["epsg"],
        "imageSR": window["epsg"],
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "U16",
        "interpolation": "RSP_NearestNeighbor",
        "f": "image",
    }
    last_error = None
    for attempt in fetch_attempts.attempts(2):
        timeout = 30 + attempt * 30
        try:
            response = requests.get(f"{FOREST_TYPE_IMAGESERVER}/exportImage", params=params, timeout=timeout)
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image"):
                raise requests.exceptions.HTTPError(f"exportImage did not return an image: {response.text[:200]}")
            return {"window": window, "tiff": response.content}
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < 2:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def parse_forest_type(raw: dict) -> dict:
    """
    The TIFF -> the block:

        {'array': float32 (rows, cols) of group codes, NaN where the pixel
                  is not a published code,
         'vintage', 'product', 'native_resolution_m', 'window',
         'nodata_cells': int}

    The array's shape is the DEM's (rows = window height, cols = width).
    """
    with MemoryFile(raw["tiff"]) as memfile:
        with memfile.open() as src:
            band = src.read(1)
    values = np.asarray(band)
    known = np.isin(values, list(FOREST_TYPE_GROUPS))
    array = np.where(known, values, np.nan).astype(np.float32)
    return {
        "array": array,
        "vintage": FOREST_TYPE_VINTAGE,
        "product": FOREST_TYPE_PRODUCT,
        "native_resolution_m": FOREST_TYPE_NATIVE_RESOLUTION_METERS,
        "window": raw["window"],
        "nodata_cells": int(np.count_nonzero(~known)),
    }


def class_counts(array: np.ndarray, mask: Optional[np.ndarray] = None) -> dict:
    """{group code: cell count} over `mask` (or the whole grid), nodata
    excluded; every code present, in code order."""
    values = array if mask is None else array[mask]
    values = values[~np.isnan(values)].astype(int)
    counts = {}
    for code in FOREST_TYPE_GROUPS:
        n = int(np.count_nonzero(values == code))
        if n:
            counts[code] = n
    return counts


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    block = parse_forest_type(get_forest_type_for_boundary(REAL_BOUNDARY))
    print(block["product"], block["array"].shape, "nodata", block["nodata_cells"])
    print({FOREST_TYPE_GROUPS[v]: n for v, n in class_counts(block["array"]).items()})
