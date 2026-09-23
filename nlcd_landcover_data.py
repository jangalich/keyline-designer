"""
nlcd_landcover_data.py

NLCD LAND COVER on the DEM grid for the site data report's Water &
hydrology section: the cover class of every cell in the parcel's window,
so the land cover of the parcel and of its contributing area can be
tabled as shares.

    get_land_cover_for_boundary(boundary)  -> the raw TIFF bytes (a fetch)
    parse_land_cover(raw)                  -> the parsed block

THE SERVICE is the Annual NLCD land cover ImageServer on the same IIPP
host, with the same exportImage pattern, as canopy_cover_data.py's Tree
Canopy Cover: the DEM's own window (dem_data.dem_window_bounds() at the
DEM's buffer and resolution), in the DEM's UTM zone, at the DEM's pixel
dimensions, nearest-neighbour, so the returned array is cell-for-cell
aligned with the DEM and every mask this pipeline builds. The native
pixel is 30 m; a 5 m cell here is a nearest-neighbour sample of it, and
any share computed from cells is a share of 30 m pixels.

THE YEAR IS PINNED. The service is a mosaic of one item per year
(1985-2024 in Annual NLCD Collection 1.1; step 0 queried the catalogue)
and the default mosaic happened to return the 2024 item -- nothing
guarantees it will tomorrow. Every export carries a mosaic rule selecting
NLCD_YEAR, and the block carries the year so the page prints it. MRLC now
lists Collection 1.2 (to 2025) as current; the service is one collection
behind, and the citation says which it served.

NO CLASS TABLE IS SERVED (hasRasterAttributeTable is false), so the NLCD
legend is bundled below. Any pixel value outside it -- including 250,
the product's own no-data value -- is nodata (NaN in the parsed grid),
never a class.

TERMS. A USGS product ("created by the Annual NLCD team at USGS EROS");
USGS-produced data are in the U.S. public domain by the USGS's own
copyright statement. Credit line: U.S. Geological Survey.
"""

import json
from typing import Optional

import numpy as np
import requests
from rasterio.io import MemoryFile

import fetch_attempts
from dem_data import dem_window_bounds

NLCD_IMAGESERVER = (
    "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/"
    "USFS_EDW_NLCD_Landcover_CONUS/ImageServer"
)
NLCD_YEAR = 2024
NLCD_COLLECTION = "Annual NLCD Collection 1.1"
NLCD_NATIVE_RESOLUTION_METERS = 30.0

# The NLCD legend: value -> (class name, group). Groups are the section's
# coarser rows; the class is what the methods note lists.
NLCD_CLASSES = {
    11: ("Open water", "water"),
    12: ("Perennial ice/snow", "water"),
    21: ("Developed, open space", "developed"),
    22: ("Developed, low intensity", "developed"),
    23: ("Developed, medium intensity", "developed"),
    24: ("Developed, high intensity", "developed"),
    31: ("Barren land", "barren"),
    41: ("Deciduous forest", "forest"),
    42: ("Evergreen forest", "forest"),
    43: ("Mixed forest", "forest"),
    52: ("Shrub/scrub", "shrub"),
    71: ("Grassland/herbaceous", "grassland"),
    81: ("Pasture/hay", "pasture"),
    82: ("Cultivated crops", "crops"),
    90: ("Woody wetlands", "wetland"),
    95: ("Emergent herbaceous wetlands", "wetland"),
}
NLCD_GROUP_LABELS = {
    "forest": "Forest",
    "pasture": "Pasture and hay",
    "crops": "Cultivated crops",
    "grassland": "Grassland",
    "shrub": "Shrub",
    "developed": "Developed",
    "wetland": "Wetland",
    "water": "Open water",
    "barren": "Barren",
}
NLCD_GROUP_ORDER = ("forest", "pasture", "crops", "grassland", "shrub", "developed", "wetland", "water", "barren")

NLCD_CITATION = (
    "U.S. Geological Survey, Annual National Land Cover Database (Annual NLCD) Collection 1.1 land cover, "
    "30 m, served by the USDA Forest Service's Image Services (IIPP) NLCD Landcover CONUS image service."
)


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


def mosaic_rule(year: int = NLCD_YEAR) -> str:
    return json.dumps({"mosaicMethod": "esriMosaicAttribute", "where": f"beginyear={int(year)}",
                       "sortField": "beginyear", "ascending": True})


@fetch_attempts.publishes
def get_land_cover_for_boundary(boundary_coordinates: list, year: int = NLCD_YEAR) -> dict:
    """
    ONE exportImage request for the boundary's DEM window -- the raw TIFF
    bytes plus the window and the year asked for (what a fixture stores):

        {'window': dem_window_bounds() dict, 'year': int, 'tiff': bytes}
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
        "pixelType": "U8",
        "interpolation": "RSP_NearestNeighbor",
        "mosaicRule": mosaic_rule(year),
        "f": "image",
    }
    last_error = None
    for attempt in fetch_attempts.attempts(2):
        timeout = 30 + attempt * 30
        try:
            response = requests.get(f"{NLCD_IMAGESERVER}/exportImage", params=params, timeout=timeout)
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image"):
                raise requests.exceptions.HTTPError(f"exportImage did not return an image: {response.text[:200]}")
            return {"window": window, "year": int(year), "tiff": response.content}
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < 2:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def parse_land_cover(raw: dict) -> dict:
    """
    The TIFF -> the block:

        {'array': float32 (rows, cols) of class values, NaN where the
                  pixel is not an NLCD class,
         'year', 'collection', 'native_resolution_m', 'window',
         'nodata_cells': int}

    The array's shape is the DEM's (rows = window height, cols = width).
    """
    with MemoryFile(raw["tiff"]) as memfile:
        with memfile.open() as src:
            band = src.read(1)
    values = np.asarray(band)
    known = np.isin(values, list(NLCD_CLASSES))
    array = np.where(known, values, np.nan).astype(np.float32)
    return {
        "array": array,
        "year": int(raw["year"]),
        "collection": NLCD_COLLECTION,
        "native_resolution_m": NLCD_NATIVE_RESOLUTION_METERS,
        "window": raw["window"],
        "nodata_cells": int(np.count_nonzero(~known)),
    }


def class_counts(array: np.ndarray, mask: Optional[np.ndarray] = None) -> dict:
    """{class value: cell count} over `mask` (or the whole grid), nodata
    excluded; every legend class present, in legend order."""
    values = array if mask is None else array[mask]
    values = values[~np.isnan(values)].astype(int)
    counts = {}
    for value in NLCD_CLASSES:
        n = int(np.count_nonzero(values == value))
        if n:
            counts[value] = n
    return counts


def group_counts(counts: dict) -> dict:
    """Class counts rolled up to the section's groups, in NLCD_GROUP_ORDER,
    groups with any cell only."""
    grouped = {group: 0 for group in NLCD_GROUP_ORDER}
    for value, n in counts.items():
        grouped[NLCD_CLASSES[value][1]] += n
    return {group: n for group, n in grouped.items() if n}


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    block = parse_land_cover(get_land_cover_for_boundary(REAL_BOUNDARY))
    print(block["year"], block["array"].shape, "nodata", block["nodata_cells"])
    counts = class_counts(block["array"])
    print({NLCD_CLASSES[v][0]: n for v, n in counts.items()})
    print(group_counts(counts))
