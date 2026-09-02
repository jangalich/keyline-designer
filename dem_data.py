"""
dem_data.py

Fetches a DEM (digital elevation model) raster grid covering a property
boundary from USGS's 3D Elevation Program (3DEP) — the same underlying
LiDAR-derived elevation source elevation_data.py already samples point-by-
point via EPQS, fetched here as a full raster grid instead of scattered
points.

A raster grid, not sampled points, is what valley/ridge delineation
(valley_delineation.py) actually needs: flow-direction and flow-
accumulation analysis require every cell's elevation relative to its
neighbors, not a handful of independent samples.

Source: USGS's 3DEPElevation dynamic ImageServer (part of The National
Map), queried via its ArcGIS REST "exportImage" operation. Free, no API
key, and backed by the same nationwide coverage documented in
elevation_data.py — 1-meter LiDAR-derived data where it's been flown,
1/3 arc-second (~10m) elsewhere. This is the raster counterpart of the
EPQS point service elevation_data.py already uses, from the same USGS
National Map system — the natural fit for the "same source" this project
already relies on. Endpoint path confirmed against USGS's published REST
docs as of this writing; if USGS ever changes it, requests here will start
failing loudly (non-2xx / non-TIFF response) rather than silently
returning bad data.

Docs: https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer
      (ArcGIS REST "exportImage" operation reference:
      https://developers.arcgis.com/rest/services-reference/enterprise/export-image/)

Like every other network-backed module in this repo, this requires real
internet access and will not run in a fully offline sandbox.
"""

import math
from typing import Optional

import numpy as np
import requests
from rasterio.io import MemoryFile
from rasterio.warp import transform as warp_transform

from raster_grid import pixel_center_xy  # noqa: F401 (re-exported for callers)

DEM_EXPORT_ENDPOINT = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
)

# Target pixel size for the fetched grid. 3DEP's best available resolution
# varies by county (1m LiDAR where flown, ~10m 1/3 arc-second elsewhere);
# requesting 5m asks the ImageServer to resample to a consistent grid
# regardless of which source resolution underlies a given property, which
# is what flow-routing needs (a uniform grid), while still being fine
# enough to resolve a small farm's swales and drainage lines.
DEFAULT_RESOLUTION_METERS = 5.0

# How far past the drawn boundary to fetch DEM coverage. Flow direction/
# accumulation for a valley near the property edge depends on terrain just
# outside the line someone drew — without a buffer, a valley could look
# like it "starts" right at the boundary when it actually continues
# upslope beyond it. Deliberately larger than hydrology_data.py's 150m
# feature-search buffer, since here it's shaping the actual terrain
# analysis, not just catching nearby features.
DEFAULT_BUFFER_METERS = 100.0

# Safety cap on grid dimensions. A pure-Python/numpy flow-routing pass
# (valley_delineation.py) over a few hundred thousand cells is fast enough
# for a "small regenerative farm property" (this tool's stated target);
# nothing here is built to scale to e.g. a full watershed.
MAX_GRID_DIMENSION = 300

NODATA_VALUE = -9999.0


def _utm_epsg_for_lonlat(longitude: float, latitude: float) -> int:
    """
    Returns the EPSG code for the UTM zone containing (longitude, latitude).

    DEM analysis (slope, flow direction, distances, gradients) needs a
    projected CRS where one unit is genuinely one meter in both directions
    — WGS84 lon/lat doesn't have that property (a degree of longitude
    shrinks as latitude increases). UTM is the standard, low-distortion
    choice for a single small property that won't straddle zone
    boundaries.
    """
    zone = int((longitude + 180) // 6) + 1
    return (32600 if latitude >= 0 else 32700) + zone


def _boundary_bbox_wgs84(
    boundary_coordinates: list[tuple[float, float]],
) -> tuple[float, float, float, float]:
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    return min(lons), min(lats), max(lons), max(lats)


def dem_window_bounds(
    boundary_coordinates: list[tuple[float, float]],
    resolution_meters: float = DEFAULT_RESOLUTION_METERS,
    buffer_meters: float = DEFAULT_BUFFER_METERS,
) -> dict:
    """
    THE WINDOW GEOMETRY, WITHOUT THE FETCH: everything
    get_dem_for_boundary() computes before it touches the network — the
    UTM zone, the buffered bbox, the (clamped) grid size and the actual
    pixel size. get_dem_for_boundary() calls this and then requests
    exactly this window, so the two can never disagree.

    Extracted so a caller can ask "what window WOULD this boundary
    fetch?" without fetching one. diagnose_water_survey_areas.py needs
    exactly that: it runs two boundaries over ONE shared DEM (so the
    per-criterion deltas are attributable to the boundary rather than to
    a different raster), and still has to report what each boundary's
    own window would have been, because the TWI curve is now referenced
    to that window. Pure arithmetic and one coordinate transform — no
    request, no contract change, and the returned dict is deliberately
    NOT the DEM dict (no 'array', no 'origin_*'; it is the request
    geometry, not a raster).

    Returns:
        {'crs', 'epsg', 'bbox': (min_x, min_y, max_x, max_y),
         'size': (width, height), 'resolution_meters': (px, py)}
    """
    min_lon, min_lat, max_lon, max_lat = _boundary_bbox_wgs84(boundary_coordinates)
    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2

    epsg = _utm_epsg_for_lonlat(center_lon, center_lat)
    dst_crs = f"EPSG:{epsg}"

    # Project just the bbox corners to UTM meters, to size the request grid
    # and apply the buffer in real ground distance rather than degrees.
    xs, ys = warp_transform(
        "EPSG:4326", dst_crs, [min_lon, max_lon], [min_lat, max_lat]
    )
    min_x, max_x = min(xs) - buffer_meters, max(xs) + buffer_meters
    min_y, max_y = min(ys) - buffer_meters, max(ys) + buffer_meters

    width = max(2, min(MAX_GRID_DIMENSION, round((max_x - min_x) / resolution_meters)))
    height = max(2, min(MAX_GRID_DIMENSION, round((max_y - min_y) / resolution_meters)))

    # Actual pixel size given the (possibly clamped) integer grid size —
    # kept separate from the requested resolution_meters so downstream
    # distance/gradient math uses what the grid really is, not what was asked for.
    pixel_size_x = (max_x - min_x) / width
    pixel_size_y = (max_y - min_y) / height

    return {
        "crs": dst_crs,
        "epsg": epsg,
        "bbox": (min_x, min_y, max_x, max_y),
        "size": (width, height),
        "resolution_meters": (pixel_size_x, pixel_size_y),
    }


def get_dem_for_boundary(
    boundary_coordinates: list[tuple[float, float]],
    resolution_meters: float = DEFAULT_RESOLUTION_METERS,
    buffer_meters: float = DEFAULT_BUFFER_METERS,
) -> dict:
    """
    Fetches a DEM raster grid covering the given property boundary (list of
    (longitude, latitude) points, same convention as every other module in
    this pipeline), reprojected to the property's local UTM zone so pixels
    are true meters.

    Returns a dict:
        {
            'array': np.ndarray, shape (height, width), float32 meters,
                     np.nan where 3DEP has no data,
            'resolution_meters': (pixel_size_x, pixel_size_y) — usually
                     equal but computed independently in case grid-size
                     rounding/clamping made them diverge slightly,
            'origin_x': x-coordinate (in `crs` units) of the upper-left
                     pixel's upper-left corner,
            'origin_y': y-coordinate of the same corner,
            'crs': EPSG code string for the UTM zone used, e.g. 'EPSG:32617',
        }

    origin_x/origin_y plus resolution_meters is deliberately a plain
    (non-rasterio) representation — every other module in this pipeline
    (valley_delineation.py, production_area.py, water_candidate_zones.py)
    works on this dict with plain numpy, so they can be unit-tested with a
    synthetic DEM and no rasterio/network dependency at all. Only this
    module talks to rasterio and the network.
    """
    window = dem_window_bounds(boundary_coordinates, resolution_meters, buffer_meters)
    epsg = window["epsg"]
    dst_crs = window["crs"]
    min_x, min_y, max_x, max_y = window["bbox"]
    width, height = window["size"]
    pixel_size_x, pixel_size_y = window["resolution_meters"]

    params = {
        "bbox": f"{min_x},{min_y},{max_x},{max_y}",
        "bboxSR": epsg,
        "imageSR": epsg,
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "F32",
        "noData": NODATA_VALUE,
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }

    response = requests.get(DEM_EXPORT_ENDPOINT, params=params, timeout=60)
    response.raise_for_status()

    with MemoryFile(response.content) as memfile:
        with memfile.open() as src:
            array = src.read(1).astype("float32")
            nodata = src.nodata if src.nodata is not None else NODATA_VALUE

    # Exact nodata matches, plus a broad sanity floor: bilinear resampling
    # (needed to reproject onto the UTM grid) blends nodata (-9999) into
    # any real value near a data-gap edge, producing large-but-not-quite-
    # exactly-nodata negative artifacts. No real US land elevation is
    # anywhere near this negative (Death Valley is about -86m), so this
    # floor catches those blended edge pixels without needing to detect
    # every possible blend ratio individually.
    array[np.isclose(array, nodata) | (array <= -1000.0)] = np.nan

    return {
        "array": array,
        "resolution_meters": (pixel_size_x, pixel_size_y),
        "origin_x": min_x,
        "origin_y": max_y,  # upper-left corner: max_y, since row 0 is the north edge
        "crs": dst_crs,
    }


def summarize_dem(dem: dict) -> str:
    """Plain-language summary — min/max/relief and grid size — mirroring
    elevation_data.py's summarize_elevation_grid()."""
    array = dem["array"]
    valid = array[~np.isnan(array)]

    if valid.size == 0:
        return "No elevation data found for this area."

    height, width = array.shape
    px, py = dem["resolution_meters"]

    return (
        f"DEM grid: {width}x{height} cells at ~{px:.1f}m x {py:.1f}m resolution "
        f"({dem['crs']}). Elevation range: {valid.min():.1f}m to {valid.max():.1f}m "
        f"(relief: {valid.max() - valid.min():.1f}m). "
        f"{valid.size}/{array.size} cells have data."
    )


if __name__ == "__main__":
    # Test case: the user's own drawn property boundary.
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Fetching DEM for property boundary...\n")

    try:
        dem = get_dem_for_boundary(property_boundary)
        print(summarize_dem(dem))
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer — not a fully sandboxed environment."
        )
