"""
canopy_cover_data.py

Fetches NLCD Tree Canopy Cover (TCC) -- USDA Forest Service, 30 m,
percent cover 0-100, conterminous US -- for a property boundary, and
provides a network-free helper that turns that raster into the same
dilated tree-root-zone cell mask canopy_height_data.tree_root_zone_mask()
produces from lidar HAG.

WHY THIS EXISTS: THE ONE LAYER WITH REAL NATIONAL GAPS.
`3dep-lidar-hag` on Planetary Computer is a DERIVED product keyed per
lidar acquisition project, built from whatever snapshot Microsoft
processed -- its coverage is NARROWER than 3DEP's own, which is itself
near-complete nationally. Confirmed on a real Maryland property: no
`3dep-lidar-hag` item, and no `3dep-lidar-dsm` either (so deriving HAG
from DSM-DTM does not help) -- the whole 3DEP lidar group is absent for
that project area. Canopy is a MANDATORY layer (parcel_data.py's
HARD-FAIL CONTRACT; production_area.get_required_tree_root_zone_mask_
utm()), so before this module a parcel like that could not be used at
all: the user could not draw a boundary, let alone reach a step, and no
retry would ever help because the absence is permanent for that parcel.

FALLBACK ONLY -- NEVER A SECOND GATE. TCC is used ONLY where HAG is
absent. It does NOT union with HAG where both exist. HAG is the better
measurement (a direct height, at 1 m posting, of what is actually
standing on the ground); mixing the two would mean no parcel is analysed
by a single consistent rule. A parcel gets ONE canopy source and the
record says which -- see canopy_height_data.canopy_source() and the
`canopy_data_source` flag it feeds.

THE RULE: ANY NONZERO COVER IS CANOPY. No threshold, no tuning, no
per-consumer calibration. From the user's own inspection of the
reference area: the darkest cells read 80-90; a cell reading 3 sits over
ground that is plainly canopy (a 30 m pixel is 900 m^2, so 3% is roughly
two mature crowns, and a thin hedgerow clipping one corner reads the
same as two isolated trees); and TCC MISSES some real canopy entirely --
thin strips and single trees are not picked up at all. The error is
therefore already toward UNDER-detection. A threshold above zero would
miss the strips on top of that. Treating any nonzero value as canopy is
the conservative direction, and conservative is correct here: over-
including means the tool avoids siting into trees, under-including means
it sites a tree crop into standing forest.

254 AND 255 ARE NOT COVER VALUES. 254 is non-processing area, 255 is
background. Under a nonzero rule both would read as canopy, and 255
could blanket ground that is simply not in the dataset. They are
excluded EXPLICITLY (TCC_NON_PROCESSING_VALUE / TCC_BACKGROUND_VALUE
below, checked in _classify_cover()), not left to a nodata tag that may
or may not be set -- this is a specific check, not a detail. Cells
carrying either value are NaN in the returned array: not canopy, and not
"checked and clear" either.

RESAMPLING ONTO THE 5 m DEM GRID. The ImageServer is asked for the DEM's
OWN window: the DEM's exact bbox, at the DEM's exact pixel dimensions, in
the DEM's own UTM CRS, with NEAREST-NEIGHBOUR interpolation -- the same
ArcGIS REST `exportImage` pattern dem_data.py already uses against
3DEPElevation, just parameterised from an existing grid rather than from
a boundary. So there is NO second reprojection step here at all (unlike
canopy_height_data.py, which clips a STAC tile in its own CRS and then
warps it onto the DEM grid): the returned array is cell-for-cell aligned
with dem['array'] by construction.

NEAREST-NEIGHBOUR IS NOT INTERCHANGEABLE WITH BILINEAR HERE, for two
independent reasons. (1) 254 and 255 are CODES, not magnitudes --
blending 255 with a neighbouring 0 produces e.g. 127, a plausible-looking
percent cover that is pure fabrication, and under the nonzero rule every
such blended cell reads as canopy. (2) Under "any nonzero is canopy",
bilinear would smear a nonzero value one interpolation kernel outward
from every canopy edge, silently growing the mask beyond what the buffer
already does deliberately.

THE MASK IS BLOCKY, AND THAT IS INHERENT. A 30 m TCC pixel covers
(30/5)^2 = ~36 cells of a ~5 m DEM grid, so every canopy edge in the
resulting mask is a 30 m staircase. HAG's mask, derived from a 1 m
product, is not. A parcel running on this fallback is being analysed by
a coarser rule, which is exactly why the source is surfaced as a flag
rather than folded in silently.

COVERAGE CONVENTION: returns None (not an exception) when the fetch
comes back with no usable cover values on-parcel -- outside the
conterminous US, or an all-background window. Same "nothing usable was
found" convention canopy_height_data.get_canopy_height_for_boundary()
and imagery_data.py already use. A caller that has already exhausted HAG
and then gets None here has no canopy source at all, and the layer
hard-fails (parcel_data.py).

WHICH SERVICE, AND WHY NOT THE OTHER ONE. The default endpoint is the
NLCD TCC service on IIPP (imagery.geoplatform.gov) -- see DEFAULT_TCC_
IMAGESERVER, which also records the dead apps.fs.usda.gov host it
replaced and the migration notice that host now answers with. Its
sibling `USFS_EDW_Science_TCC_CONUS` is the RAW MODEL OUTPUT and must
NOT be substituted: the NLCD version is masked to remove canopy over
water and non-tree crops and smoothed across years, and under the
any-nonzero rule above that masking is load-bearing -- unmasked model
noise over a crop field is a small nonzero percentage, which this module
would read as trees.

Docs: https://imagery.geoplatform.gov/iipp/rest/services (IIPP catalog)
      https://www.mrlc.gov/data (NLCD Tree Canopy Cover, CONUS)
      (ArcGIS REST "exportImage" operation reference:
      https://developers.arcgis.com/rest/services-reference/enterprise/export-image/)

Like every other network-backed module in this repo, this requires real
internet access and will not run in a fully offline sandbox.
"""

import math
import os
from typing import Optional

import numpy as np
import requests
from rasterio.io import MemoryFile
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.prepared import prep

import fetch_attempts
# canopy_height_data does NOT import this module at module scope (it reaches
# the fallback through a function-local import), so this direction is safe
# and there is no cycle. Imported for two things: the root-zone buffer, so
# the fallback dilates by EXACTLY the distance the HAG path dilates by --
# two canopy sources, one buffer, or the two masks would differ by something
# other than the measurement -- and the source identifier, whose single
# definition lives with the dispatch in canopy_height_data.
from canopy_height_data import CANOPY_SOURCE_NLCD_TCC, TREE_ROOT_ZONE_BUFFER_METERS
from raster_grid import binary_dilate, pixel_center_xy

# The ImageServer publishing NLCD Tree Canopy Cover for the conterminous
# US, on the Imagery and Image Products Platform (IIPP). Same kind of
# ArcGIS REST image service dem_data.py queries against 3DEPElevation, so
# the request shape below is dem_data.py's, not a new integration
# pattern. CONFIRMED LIVE against the reference parcels.
#
# THE OLD FOREST SERVICE HOST IS GONE. apps.fs.usda.gov/fsgisx01 now
# answers 403 with a migration notice -- "The service being requested has
# been migrated to IIPP. Please visit https://imagery.geoplatform.gov/
# iipp/rest/services" -- so the previous default could never have worked,
# whatever service name followed it.
#
# USFS_EDW_Science_TCC_CONUS IS THE SIBLING SERVICE AND MUST NOT BE
# SUBSTITUTED FOR THIS ONE. It is the RAW MODEL OUTPUT. The NLCD version
# named here is masked to remove canopy over water and non-tree crops,
# and smoothed across years. That difference is not cosmetic under this
# module's any-nonzero-is-canopy rule (see the module docstring): an
# unmasked model's noise over a crop field is a small nonzero percentage,
# which this module would read as trees and exclude as canopy root zone.
# The masking is doing load-bearing work that the threshold deliberately
# does not do.
#
# OVERRIDABLE BY ENVIRONMENT, deliberately -- and this migration is
# exactly why. A service's host and NAME are the parts of an ArcGIS
# endpoint a publisher re-versions or relocates without notice, and this
# repo has no way to discover the change except by a request failing.
# KEYLINE_TCC_IMAGESERVER lets an operator repoint this without a code
# change; unset (the default) uses the constant below, which is the
# working one. Either way a wrong path fails LOUDLY -- a non-2xx or a
# non-TIFF body raises -- the same "fail loudly on a schema surprise"
# stance dem_data.py's own docstring takes on its endpoint.
DEFAULT_TCC_IMAGESERVER = (
    "https://imagery.geoplatform.gov/iipp/rest/services/Vegetation/"
    "USFS_EDW_NLCD_TCC_CONUS/ImageServer"
)


def tcc_export_endpoint() -> str:
    """The exportImage endpoint this module posts to, honouring
    KEYLINE_TCC_IMAGESERVER. A function rather than a module constant so
    the environment is read at call time -- a test (or an operator) can
    repoint it without reimporting."""
    base = os.environ.get("KEYLINE_TCC_IMAGESERVER", DEFAULT_TCC_IMAGESERVER).rstrip("/")
    return f"{base}/exportImage"


# THE TWO NON-COVER CODES, NAMED. See the module docstring: 254 is
# non-processing area, 255 is background. Both are excluded explicitly in
# _classify_cover() rather than being left to the raster's nodata tag,
# which the service may or may not set on an exported window.
TCC_NON_PROCESSING_VALUE = 254
TCC_BACKGROUND_VALUE = 255

# The inclusive range of REAL percent-cover values. Anything outside it
# (including the two codes above) is not cover.
TCC_MIN_COVER_PCT = 0
TCC_MAX_COVER_PCT = 100

# THE RULE, AS A CONSTANT SO IT CAN BE ASSERTED AGAINST RATHER THAN
# RE-STATED. Any cover STRICTLY GREATER than this is canopy. It is zero
# and is not a tuning knob -- see the module docstring for why a
# threshold above zero would make an already under-detecting product
# under-detect further, in the unsafe direction.
CANOPY_COVER_THRESHOLD_PCT = 0

# The native cell size of NLCD TCC, used only to REPORT how coarse the
# resampled mask is (how many DEM cells one TCC pixel spans). Nothing
# computed here depends on it.
TCC_NATIVE_RESOLUTION_METERS = 30.0

# Max percent of ON-PARCEL cells that may carry no usable cover value
# (254, 255, or the service's own nodata) before this module treats the
# fetch as unusable and returns None rather than a mostly-unchecked
# array that would read as "mostly no trees". Deliberately the same
# 10% figure and the same reasoning as canopy_height_data.MAX_ACCEPTABLE_
# CANOPY_NODATA_PCT, kept as a SEPARATE constant because the two
# products' gap behaviour is unrelated and either may need retuning
# alone. CONFIGURABLE, unvalidated against a real property yet, same
# caveat every other threshold in this pipeline carries.
MAX_ACCEPTABLE_TCC_NODATA_PCT = 10.0


def __getattr__(name):
    """PEP 562 -- see canopy_height_data.py's own __getattr__ for the
    reasoning. Every read of LAST_FETCH_* lands here and gets the calling
    thread's totals from the last published call."""
    return fetch_attempts.published(__name__, name)


def _retry(operation, max_retries: int = 2):
    """The same progressive-timeout retry helper every network-backed
    module in this pipeline keeps its own private copy of -- see
    imagery_data._retry()'s docstring. Attempts and slept milliseconds go
    through fetch_attempts so they are published, not swallowed; since
    this module is called from inside canopy_height_data.get_canopy_
    height_for_boundary()'s own @fetch_attempts.publishes entry point,
    they land in the canopy layer's ledger, which is correct -- a
    fallback fetch is part of what fetching the canopy layer cost."""
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        timeout = 30 + (attempt * 30)
        try:
            return operation(timeout)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def dem_grid_window(dem: dict) -> dict:
    """
    The DEM's own window, in the form an ArcGIS exportImage request wants:
    its exact bbox, its exact pixel dimensions and its own CRS. Pure
    arithmetic off the dem dict -- no request, no rasterio.

    dem['origin_x']/['origin_y'] are the UPPER-LEFT corner (dem_data.py
    sets origin_y to the window's max_y because row 0 is the north edge),
    so the bbox's min_y is origin_y MINUS the full grid height.

    Returns {'bbox': (min_x, min_y, max_x, max_y), 'size': (width,
    height), 'crs': str, 'epsg': int}.
    """
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    min_x = float(dem["origin_x"])
    max_y = float(dem["origin_y"])
    max_x = min_x + cols * float(px)
    min_y = max_y - rows * float(py)
    crs = str(dem["crs"])
    epsg = int(crs.split(":")[-1])
    return {"bbox": (min_x, min_y, max_x, max_y), "size": (cols, rows), "crs": crs, "epsg": epsg}


def _export_tcc_on_dem_grid(dem: dict, timeout: float) -> tuple[np.ndarray, Optional[float]]:
    """
    One exportImage request for the DEM's exact window, returning the raw
    band (unclassified, still carrying 254/255) and whatever nodata value
    the returned TIFF declares, if any.

    `interpolation=RSP_NearestNeighbor` is REQUIRED, not a preference --
    see the module docstring. `pixelType=U8` matches TCC's own storage, so
    a real 0-100 value round-trips exactly and the two codes arrive as 254
    and 255 rather than as floats near them. `noData` is deliberately NOT
    sent: this module classifies 254/255 itself and wants to SEE them, and
    a client-supplied noData would fold both into one indistinguishable
    value before they ever reach _classify_cover().
    """
    window = dem_grid_window(dem)
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
        "f": "image",
    }

    response = requests.get(tcc_export_endpoint(), params=params, timeout=timeout)
    response.raise_for_status()

    with MemoryFile(response.content) as memfile:
        with memfile.open() as src:
            band = src.read(1)
            nodata = src.nodata
    return band, nodata


def _classify_cover(band: np.ndarray, service_nodata: Optional[float]) -> tuple[np.ndarray, dict]:
    """
    THE 254/255 CHECK. Turns a raw TCC band into a float32 percent-cover
    array where every cell is either a real 0-100 percentage or NaN, and
    reports how many cells each non-cover code accounted for.

    Excluded EXPLICITLY, in this order and by value, not by any nodata
    tag:

      254 -- non-processing area.
      255 -- background.
      the service's own declared nodata, if the exported TIFF set one
             (belt-and-braces; on a window fully inside CONUS there
             usually is none).
      anything else outside 0-100 -- cannot be a percentage.

    Why by value: under "any nonzero cover is canopy" BOTH codes would
    otherwise read as canopy, and 255 -- which is what an ImageServer
    returns for ground simply not in the dataset -- could blanket a whole
    parcel as wooded. That is the single worst failure this module could
    have, so it is checked here rather than trusted to metadata.

    Returns (cover_pct_float32_with_nan, counts) where counts carries
    'non_processing_254', 'background_255', 'service_nodata',
    'out_of_range' and 'valid_cover'.
    """
    raw = band.astype("float32")
    cover = raw.copy()

    is_254 = raw == float(TCC_NON_PROCESSING_VALUE)
    is_255 = raw == float(TCC_BACKGROUND_VALUE)
    is_service_nodata = (
        np.isclose(raw, float(service_nodata)) if service_nodata is not None else np.zeros(raw.shape, dtype=bool)
    )
    # Counted as its own category only where it is not already one of the
    # two named codes, so the four counts partition the grid.
    is_service_nodata = is_service_nodata & (~is_254) & (~is_255)

    out_of_range = (
        (raw < float(TCC_MIN_COVER_PCT)) | (raw > float(TCC_MAX_COVER_PCT))
    ) & (~is_254) & (~is_255) & (~is_service_nodata)

    invalid = is_254 | is_255 | is_service_nodata | out_of_range
    cover[invalid] = np.nan

    counts = {
        "non_processing_254": int(is_254.sum()),
        "background_255": int(is_255.sum()),
        "service_nodata": int(is_service_nodata.sum()),
        "out_of_range": int(out_of_range.sum()),
        "valid_cover": int((~invalid).sum()),
    }
    return cover, counts


def _boundary_to_polygon(boundary_coordinates: list) -> Polygon:
    """Same ring-closing convention as canopy_height_data._boundary_to_
    polygon()."""
    coords = list(boundary_coordinates)
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return Polygon(coords)


def _on_parcel_nan_fraction(cover: np.ndarray, boundary_coordinates: list, dem: dict) -> tuple[float, int]:
    """Fraction of ON-PARCEL cells that carry no usable cover value --
    the same cell-center-containment on-parcel test canopy_height_data.
    _on_parcel_nan_fraction() applies, for the same reason (dem_data.py
    fetches a buffer past the drawn boundary; counting that off-parcel
    ground as missing would understate real on-parcel coverage)."""
    boundary_polygon = _boundary_to_polygon(boundary_coordinates)
    boundary_polygon_utm = shape(transform_geom("EPSG:4326", dem["crs"], mapping(boundary_polygon)))
    boundary_prepared = prep(boundary_polygon_utm)

    rows, cols = cover.shape
    on_parcel_count = 0
    on_parcel_nan_count = 0
    for r in range(rows):
        for c in range(cols):
            if boundary_prepared.contains(Point(pixel_center_xy(dem, r, c))):
                on_parcel_count += 1
                if np.isnan(cover[r, c]):
                    on_parcel_nan_count += 1

    if on_parcel_count == 0:
        return 0.0, 0
    return on_parcel_nan_count / on_parcel_count, on_parcel_count


def get_tree_canopy_cover_for_boundary(
    boundary_coordinates: list,
    dem: dict,
    max_retries: int = 5,
) -> Optional[dict]:
    """
    Fetches NLCD Tree Canopy Cover for this boundary, ALREADY ON dem's own
    grid (see the module docstring's RESAMPLING section -- the ImageServer
    is asked for the DEM's exact window, so nothing is reprojected here).

    Returns:
        {
            'array': np.ndarray, shape matching dem['array'], float32
                     PERCENT COVER 0-100, np.nan where the cell carries no
                     usable cover value (254, 255, service nodata, or out
                     of range),
            'units': 'percent_cover'   -- NOT metres. The HAG dict's
                     'array' is height above ground; this one is a
                     percentage, which is why the mask derivation
                     dispatches on 'source' rather than thresholding
                     'array' the same way for both.
            'resolution_meters' / 'origin_x' / 'origin_y' / 'crs':
                     dem's own, verbatim,
            'source': canopy_height_data.CANOPY_SOURCE_NLCD_TCC,
            'source_item_id': the service endpoint the data came from,
            'native_resolution_meters': 30.0,
            'dem_cells_per_source_pixel': how many DEM cells one TCC pixel
                     spans (~36 on a 5 m grid) -- the blockiness, as a
                     number,
            'value_counts': _classify_cover()'s own counts, including how
                     many cells carried 254 and how many carried 255,
            'on_parcel_nodata_pct': percent of ON-PARCEL cells with no
                     usable cover value,
        }

    Returns None when the window comes back with no usable cover on-parcel
    at all (outside the conterminous US, an all-background window), or
    when more than MAX_ACCEPTABLE_TCC_NODATA_PCT of the ON-PARCEL grid
    carries no usable value -- a mostly-unchecked array must not be handed
    back as "mostly no trees". Same "nothing usable found" convention as
    canopy_height_data.get_canopy_height_for_boundary(); a caller that has
    already exhausted HAG and gets None here has no canopy source at all.

    max_retries defaults to 5, not the 2 most network layers in this
    pipeline use, matching canopy_height_data.get_canopy_height_for_
    boundary()'s own budget and for the same reason: by the time this runs
    it is the LAST canopy source there is for the parcel, and a failure
    here fails the whole session. canopy_height_data._tree_canopy_cover_
    fallback() threads ITS caller's budget through rather than relying on
    this default, so the two never drift apart.

    Raises (does not return None) on an actual request failure once
    retries are exhausted -- "the service did not answer" is a different
    outcome from "the service answered and has nothing here", and the two
    must not collapse into one value.
    """
    band, service_nodata = _retry(
        lambda timeout: _export_tcc_on_dem_grid(dem, timeout), max_retries=max_retries
    )

    cover, counts = _classify_cover(band, service_nodata)
    if counts["valid_cover"] == 0:
        return None

    nan_fraction, on_parcel_count = _on_parcel_nan_fraction(cover, boundary_coordinates, dem)
    if on_parcel_count > 0 and nan_fraction * 100 > MAX_ACCEPTABLE_TCC_NODATA_PCT:
        return None

    px, py = dem["resolution_meters"]
    cell_size = (float(px) + float(py)) / 2.0
    cells_per_source_pixel = (TCC_NATIVE_RESOLUTION_METERS / cell_size) ** 2

    return {
        "array": cover,
        "units": "percent_cover",
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source": CANOPY_SOURCE_NLCD_TCC,
        "source_item_id": tcc_export_endpoint(),
        "native_resolution_meters": TCC_NATIVE_RESOLUTION_METERS,
        "dem_cells_per_source_pixel": round(cells_per_source_pixel, 1),
        "value_counts": counts,
        "on_parcel_nodata_pct": round(nan_fraction * 100, 2),
    }


def canopy_cover_cell_mask(cover_array: np.ndarray) -> np.ndarray:
    """
    THE RULE, AS ONE LINE: any nonzero cover is canopy.

    Network-free. True where the cell carries a real percent-cover value
    STRICTLY GREATER than CANOPY_COVER_THRESHOLD_PCT (which is 0). NaN
    cells -- 254, 255, service nodata, out of range -- are never canopy,
    the same way canopy_height_data.tree_root_zone_mask() never counts a
    NaN HAG cell as a tree.

    Separated from canopy_cover_root_zone_mask() below so the un-dilated
    canopy cells can be measured on their own (the HAG-vs-TCC calibration
    in test_canopy_cover_fallback.py compares both).
    """
    return (~np.isnan(cover_array)) & (cover_array > float(CANOPY_COVER_THRESHOLD_PCT))


def canopy_cover_root_zone_mask(
    cover_array: np.ndarray,
    resolution_meters: tuple[float, float],
    buffer_meters: float = TREE_ROOT_ZONE_BUFFER_METERS,
) -> np.ndarray:
    """
    The TCC counterpart of canopy_height_data.tree_root_zone_mask(), and
    deliberately the SAME two-step raster operation -- only the first step
    differs, because the two products measure different things:

      1. Canopy test: any nonzero percent cover (canopy_cover_cell_mask()
         above), where HAG's is `>= CANOPY_HEIGHT_THRESHOLD_METERS`.
      2. Dilate: raster_grid.binary_dilate() by buffer_meters converted to
         a whole-cell radius with the same ceil()-for-safety rounding
         tree_root_zone_mask() uses, so a root zone genuinely as wide as
         buffer_meters is never under-covered.

    Step 2 is character-for-character the HAG path's, on purpose: the two
    masks must differ by the MEASUREMENT and by nothing else, or a
    calibration between them is measuring this function instead.

    Stays a RASTER operation throughout, never vectorized into a polygon
    buffer/difference -- see canopy_height_data.py's CELL MASK LOGIC
    section for the sliver-fragmentation bug that path causes.
    """
    canopy_cells = canopy_cover_cell_mask(cover_array)

    if buffer_meters <= 0:
        return canopy_cells

    px, py = resolution_meters
    cell_size = (px + py) / 2.0
    radius_cells = max(1, math.ceil(buffer_meters / cell_size))

    return binary_dilate(canopy_cells, radius_cells)


def summarize_tree_canopy_cover(cover: Optional[dict]) -> str:
    """Plain-language summary, same purpose as canopy_height_data.
    summarize_canopy_height() and dem_data.summarize_dem()."""
    if not cover:
        return "No NLCD Tree Canopy Cover data was found for this property."

    array = cover["array"]
    valid = array[~np.isnan(array)]
    if valid.size == 0:
        return "No NLCD Tree Canopy Cover data was found for this property."

    mask = canopy_cover_root_zone_mask(array, cover["resolution_meters"])
    pct_root_zone = round(100 * float(np.count_nonzero(mask)) / mask.size, 1)
    counts = cover["value_counts"]

    return (
        f"NLCD Tree Canopy Cover (FALLBACK -- no lidar HAG coverage here). "
        f"Percent cover range: {valid.min():.0f}% to {valid.max():.0f}%. "
        f"{valid.size}/{array.size} cells carry a usable cover value "
        f"({counts['non_processing_254']} cells were 254/non-processing, "
        f"{counts['background_255']} were 255/background, "
        f"{counts['service_nodata']} were service nodata). "
        f"Any nonzero cover counts as canopy; with the "
        f"+{TREE_ROOT_ZONE_BUFFER_METERS:.1f}m root-zone buffer that is "
        f"{pct_root_zone}% of the grid. One 30m source pixel spans "
        f"~{cover['dem_cells_per_source_pixel']:.0f} DEM cells, so this mask "
        f"is blocky in a way a HAG mask is not."
    )


if __name__ == "__main__":
    from dem_data import get_dem_for_boundary

    # THE USER'S OWN FAILING PARCEL -- Frederick County, Maryland.
    # Confirmed live at 0 `3dep-lidar-hag` items, and confirmed falling
    # back to NLCD TCC. test_canopy_cover_fallback.py uses these exact
    # coordinates; they are the reason this module exists.
    maryland_boundary = [
        (-77.52875829843366, 39.37068357456659),
        (-77.5244882217246, 39.37103192232765),
        (-77.52519632490174, 39.37488851360338),
        (-77.52955223229841, 39.37377717368003),
        (-77.53033091081994, 39.36936307851048),
    ]

    print("Fetching DEM grid + NLCD Tree Canopy Cover for the Maryland boundary...\n")

    try:
        dem = get_dem_for_boundary(maryland_boundary)
        cover = get_tree_canopy_cover_for_boundary(maryland_boundary, dem)
        print(summarize_tree_canopy_cover(cover))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer and the IIPP ImageServer -- not a fully "
            "sandboxed environment."
        )
