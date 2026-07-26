"""
canopy_height_data.py

Fetches USGS 3DEP Lidar Height-Above-Ground (the `3dep-lidar-hag`
collection: per-pixel height of the first lidar return above bare earth,
i.e. canopy/vegetation height, not terrain elevation) from Microsoft's
Planetary Computer STAC catalog for a property boundary, and provides a
network-free helper that turns that raster into a dilated tree-root-zone
cell mask for production_area.py's eligibility gate.

FETCH: follows imagery_data.py's exact fetch/clip/retry pattern --
`planetary_computer.sign_inplace` for signed asset access, a STAC search
against the boundary, `rasterio.open()` + `rasterio.mask.mask()` clipped
against the boundary polygon (reprojected into the source raster's own
CRS first, same as imagery_data._read_clipped_band()), and the same
`_retry()` progressive-timeout helper imagery_data.py/soil_data.py/
hydrology_data.py already use. Unlike imagery_data.py's Sentinel-2 search
(most-recent-first, cloud-cover filtered), 3DEP lidar HAG isn't a
recurring time series -- there can be more than one lidar-project tile
covering a boundary near a tile seam, so items are instead ranked by how
much of the boundary they actually cover, and the best-covering tile is
used.

GRID ALIGNMENT: after clipping, the raster is reprojected/resampled onto
the SAME grid dem_data.get_dem_for_boundary() already produced for this
property (same resolution, same UTM CRS, same origin) via
`rasterio.warp.reproject()` -- a second, separate step from the clip
above, not folded into it, so the clip stays a straight copy of
imagery_data.py's pattern. This is what makes the returned array
cell-for-cell aligned with production_area.py's slope/hydric masks:
`tree_root_zone_mask()` below and the hydric mask compute_step1_eligible_
cells() already builds can be OR'd together index-for-index with no
further alignment work.

NO-COVERAGE CONVENTION: returns None (not an exception) if no `3dep-
lidar-hag` tile intersects this boundary, or if every clipped/reprojected
pixel is nodata -- 3DEP lidar coverage is real but not yet nationwide, so
"no HAG data here" is a genuine outcome, the same "no scene met the
filter" / "no API key" convention imagery_data.py and irradiance_data.py
already use. Callers should surface that as a confidence_notes caveat
(the woody-vegetation gate couldn't be checked, not that the check found
no trees), not fail the pipeline.

CELL MASK LOGIC: tree_root_zone_mask() is deliberately network-free and
takes a plain HAG array, the same offline/network split raster_grid.py's
own helpers (binary_erode(), connected_components()) follow relative to
dem_data.py's fetch -- concept-specific numpy logic lives with the
concept module, generic raster morphology primitives
(raster_grid.binary_dilate(), added alongside binary_erode() for this)
live in raster_grid.py. It stays a RASTER operation throughout (threshold
-> raster dilate), never vectorized into a polygon buffer/difference --
that path is exactly what caused the real sliver-fragmentation bug
production_area.py's own module docstring documents for hydric-soil
carving (a thin buffered polygon differenced against real cell squares
slices ground into slivers a cell-level raster test never would).

Docs: https://planetarycomputer.microsoft.com/dataset/3dep-lidar-hag
      https://planetarycomputer.microsoft.com/docs/quickstarts/reading-stac-data/

Like every other network-backed module in this repo, this requires real
internet access and will not run in a fully offline sandbox.
"""

import math
import time
from typing import Optional

import numpy as np
import planetary_computer
import rasterio
from pystac_client import Client
from rasterio.mask import mask
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject, transform_geom
from shapely.geometry import Polygon, mapping, shape

from raster_grid import binary_dilate

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "3dep-lidar-hag"

# The single-band COG asset key 3dep-lidar-hag (and its sibling 3DEP lidar
# derived-product collections on Planetary Computer) publishes its data
# under. Confirmed against Planetary Computer's published collection docs
# as of this writing; if that ever changes, item.assets[HAG_ASSET_KEY]
# below raises a loud KeyError rather than silently reading the wrong
# thing -- same "fail loudly on a schema surprise" stance dem_data.py's
# own docstring takes on its ImageServer endpoint.
HAG_ASSET_KEY = "data"

# Fallback nodata value if a fetched tile's own nodata metadata is
# missing -- mirrors dem_data.py's NODATA_VALUE convention.
HAG_NODATA_FALLBACK = -9999.0

METERS_PER_FOOT = 0.3048

# Height above ground at/above which a cell reads as "tree", not
# shrub/tall-grass/brush. 15ft is a conventional canopy-vs-understory
# split for lidar HAG products -- distinguishing genuine trees (whose
# root systems are the actual production-zone concern below) from
# shorter woody/herbaceous vegetation. CONFIGURABLE, unvalidated against
# a real property yet, same caveat every other threshold in this
# pipeline (MAX_PRODUCTION_SLOPE_PCT, MIN_PRODUCTION_AREA_ACRES) carries.
CANOPY_HEIGHT_THRESHOLD_METERS = 4.5  # 15 ft

# How far past a tree cell's own footprint its root zone is presumed to
# extend, for the buffer this module dilates the thresholded tree mask
# by. 10ft, CONFIGURABLE -- a starting value, not a measured root-radius
# figure.
TREE_ROOT_ZONE_BUFFER_METERS = 10 * METERS_PER_FOOT  # ~3.05m


def _boundary_to_polygon(boundary_coordinates: list) -> Polygon:
    """Same convention as imagery_data._boundary_to_polygon()/soil_data.
    coordinates_to_wkt_polygon(): closes the ring if the input isn't
    already closed."""
    coords = list(boundary_coordinates)
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return Polygon(coords)


def _retry(operation, max_retries: int = 2):
    """Identical progressive-timeout retry helper to imagery_data._retry()
    -- see that module's docstring for the reasoning. Duplicated here
    rather than imported since every other network-backed module in this
    pipeline (soil_data.py, hydrology_data.py, imagery_data.py) keeps its
    own private copy rather than sharing one across modules."""
    last_error = None

    for attempt in range(max_retries + 1):
        timeout = 30 + (attempt * 30)
        try:
            return operation(timeout)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise last_error


def _search_hag_items(polygon: Polygon, max_retries: int = 2) -> list:
    """
    Searches the Planetary Computer STAC catalog for 3dep-lidar-hag items
    intersecting the boundary. Unlike imagery_data._search_scenes()
    (sorted most-recent-first, since a newer cloud-free scene is strictly
    better), HAG tiles aren't interchangeable by date -- near a lidar-
    project tile seam more than one item can intersect a boundary's
    bounding box while each only covering PART of it, so items are
    instead ranked by how much of the boundary they actually cover (their
    own WGS84 footprint intersected with the boundary), best-covering
    first. Empty list if nothing intersects.
    """

    def _do_search(timeout):
        catalog = Client.open(
            STAC_API_URL,
            modifier=planetary_computer.sign_inplace,
            timeout=timeout,
        )
        search = catalog.search(
            collections=[COLLECTION],
            intersects=mapping(polygon),
            limit=10,
        )
        return list(search.items())

    items = _retry(_do_search, max_retries=max_retries)
    if not items:
        return items

    def _overlap_area(item) -> float:
        try:
            return shape(item.geometry).intersection(polygon).area
        except Exception:
            return 0.0

    return sorted(items, key=_overlap_area, reverse=True)


def _read_clipped_hag(href: str, polygon: Polygon, timeout: float):
    """
    Opens the HAG raster asset (over HTTPS, via a Planetary-Computer-
    signed URL) and clips it to the property boundary -- same open +
    reproject-geometry-into-source-CRS + rasterio.mask.mask() pattern as
    imagery_data._read_clipped_band(), just returning the clip's own
    transform/CRS/nodata alongside the array too, since (unlike
    imagery_data.py, which only needs pixel values for an NDVI ratio)
    this array still has to be reprojected onto the DEM's own grid
    afterward.
    """
    with rasterio.Env(GDAL_HTTP_TIMEOUT=str(int(timeout)), GDAL_HTTP_CONNECTTIMEOUT=str(int(timeout))):
        with rasterio.open(href) as src:
            geom_in_raster_crs = transform_geom("EPSG:4326", src.crs, mapping(polygon))
            nodata = src.nodata if src.nodata is not None else HAG_NODATA_FALLBACK
            clipped, clipped_transform = mask(src, [geom_in_raster_crs], crop=True, filled=True, nodata=nodata)
            return clipped[0].astype("float32"), clipped_transform, src.crs, nodata


def _reproject_to_dem_grid(
    clipped_array: np.ndarray,
    clipped_transform,
    clipped_crs,
    nodata_value: float,
    dem: dict,
) -> np.ndarray:
    """
    Resamples the clipped HAG array (still in the source tile's own CRS
    and pixel grid) onto dem's exact grid -- same resolution, same UTM
    CRS, same origin -- via rasterio.warp.reproject(), so the result is
    cell-for-cell aligned with every other mask compute_step1_eligible_
    cells() builds against this DEM. NaN fills cells the source raster
    has no data for (including everywhere outside its own clipped
    extent), same nodata-as-NaN convention dem_data.py's own array uses.
    """
    px, py = dem["resolution_meters"]
    dst_transform = Affine(px, 0.0, dem["origin_x"], 0.0, -py, dem["origin_y"])
    rows, cols = dem["array"].shape

    destination = np.full((rows, cols), np.nan, dtype="float32")
    reproject(
        source=clipped_array,
        destination=destination,
        src_transform=clipped_transform,
        src_crs=clipped_crs,
        src_nodata=nodata_value,
        dst_transform=dst_transform,
        dst_crs=dem["crs"],
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return destination


def get_canopy_height_for_boundary(
    boundary_coordinates: list,
    dem: dict,
    max_retries: int = 2,
) -> Optional[dict]:
    """
    Given a property boundary (list of (longitude, latitude) points, same
    convention as every other module in this pipeline) and the DEM dict
    dem_data.get_dem_for_boundary() already produced for this same
    property (reused here, not re-fetched -- this module needs it only as
    the target grid definition), finds the best-covering 3dep-lidar-hag
    tile, clips it to the boundary, and reprojects the result onto the
    DEM's own grid.

    Returns:
        {
            'array': np.ndarray, shape matching dem['array'], float32
                     meters height-above-ground, np.nan where no HAG
                     coverage exists for that cell,
            'resolution_meters': dem['resolution_meters'],
            'origin_x': dem['origin_x'],
            'origin_y': dem['origin_y'],
            'crs': dem['crs'],
            'source_item_id': the STAC item id the data came from,
        }

    Returns None if no 3dep-lidar-hag tile intersects this boundary at
    all, or if the best-covering tile's clipped/reprojected result has no
    valid (non-nodata) pixels -- a genuine "no lidar HAG coverage here"
    outcome (3DEP lidar coverage is real but not yet nationwide) rather
    than a failed request, same convention imagery_data.get_imagery_
    summary_for_boundary() and irradiance_data.get_regional_irradiance_
    baseline() already use for their own "nothing usable was found"
    cases. Callers should treat this the same way: skip/caveat the
    woody-vegetation gate rather than fail the whole pipeline.
    """
    polygon = _boundary_to_polygon(boundary_coordinates)

    items = _search_hag_items(polygon, max_retries=max_retries)
    if not items:
        return None

    item = items[0]  # already ranked best-covering-first
    href = item.assets[HAG_ASSET_KEY].href

    clipped, clipped_transform, clipped_crs, nodata = _retry(
        lambda timeout: _read_clipped_hag(href, polygon, timeout), max_retries=max_retries
    )

    valid = (~np.isnan(clipped)) & (~np.isclose(clipped, nodata))
    if not np.any(valid):
        return None

    hag_on_grid = _reproject_to_dem_grid(clipped, clipped_transform, clipped_crs, nodata, dem)
    if not np.any(~np.isnan(hag_on_grid)):
        return None

    return {
        "array": hag_on_grid,
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": item.id,
    }


def tree_root_zone_mask(
    hag_array: np.ndarray,
    resolution_meters: tuple[float, float],
    height_threshold_meters: float = CANOPY_HEIGHT_THRESHOLD_METERS,
    buffer_meters: float = TREE_ROOT_ZONE_BUFFER_METERS,
) -> np.ndarray:
    """
    Network-free: turns a HAG array (get_canopy_height_for_boundary()'s
    own 'array', or any same-shape synthetic array in tests) into a
    boolean tree-ROOT-ZONE mask -- True where a cell is either itself a
    tree cell or within buffer_meters of one.

    Two-step raster operation, deliberately never vectorized into polygon
    geometry (see module docstring's CELL MASK LOGIC section for why):

      1. Threshold: hag_array >= height_threshold_meters (NaN/no-data
         cells never count as trees) -> boolean tree-cell mask.
      2. Dilate: raster_grid.binary_dilate() grows that mask outward by
         buffer_meters, converted to a whole-cell radius via this array's
         own resolution_meters (rounded UP via ceil, so a real root zone
         genuinely as wide as buffer_meters is never under-covered by a
         too-small radius -- same round-up-for-safety convention
         production_area._waist_erosion_radius_cells() already uses for
         its own meters-to-cells conversion).
    """
    tree_cell_mask = (~np.isnan(hag_array)) & (hag_array >= height_threshold_meters)

    if buffer_meters <= 0:
        return tree_cell_mask

    px, py = resolution_meters
    cell_size = (px + py) / 2.0
    radius_cells = max(1, math.ceil(buffer_meters / cell_size))

    return binary_dilate(tree_cell_mask, radius_cells)


def summarize_canopy_height(canopy: Optional[dict]) -> str:
    """Plain-language summary, same purpose as dem_data.summarize_dem()."""
    if not canopy:
        return "No USGS 3DEP lidar height-above-ground coverage was found for this property."

    array = canopy["array"]
    valid = array[~np.isnan(array)]
    if valid.size == 0:
        return "No USGS 3DEP lidar height-above-ground coverage was found for this property."

    tree_mask = tree_root_zone_mask(array, canopy["resolution_meters"])
    pct_tree_root_zone = round(100 * float(np.count_nonzero(tree_mask)) / tree_mask.size, 1)

    return (
        f"HAG source tile: {canopy['source_item_id']}. "
        f"Height-above-ground range: {valid.min():.1f}m to {valid.max():.1f}m. "
        f"{valid.size}/{array.size} cells have HAG data. "
        f"Tree root-zone (>= {CANOPY_HEIGHT_THRESHOLD_METERS}m canopy, "
        f"+{TREE_ROOT_ZONE_BUFFER_METERS:.1f}m buffer): {pct_tree_root_zone}% of the grid."
    )


if __name__ == "__main__":
    from dem_data import get_dem_for_boundary

    # Test case: the user's own drawn property boundary (Richland Township, PA)
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Fetching DEM grid + canopy height (HAG) summary for property boundary...\n")

    try:
        dem = get_dem_for_boundary(property_boundary)
        canopy = get_canopy_height_for_boundary(property_boundary, dem)
        print(summarize_canopy_height(canopy))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer and Planetary Computer's STAC API/blob storage "
            "-- not a fully sandboxed environment."
        )
