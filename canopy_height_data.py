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

NO-COVERAGE CONVENTION, AND THE FALLBACK BELOW IT: where this module
used to return None -- no `3dep-lidar-hag` tile intersecting the
boundary, or every clipped/reprojected pixel nodata -- it now falls back
to NLCD Tree Canopy Cover (canopy_cover_data.py) and returns THAT dict
instead, tagged `source: CANOPY_SOURCE_NLCD_TCC`. None is still returned,
with the same meaning and the same hard-fail consequence upstream, when
the fallback ALSO has nothing for this parcel.

WHY A FALLBACK AT ALL. 3DEP lidar coverage is near-complete nationally,
but Planetary Computer's HAG collection is a DERIVED product keyed per
acquisition project, built from whatever snapshot Microsoft processed --
its coverage is narrower than 3DEP's own, and that is the gap. Confirmed
on a real Maryland property: no `3dep-lidar-hag` item, and no
`3dep-lidar-dsm` either, so deriving HAG from DSM-DTM does not help. The
whole 3DEP lidar group is absent for that project area. Canopy is a
MANDATORY layer, so before the fallback such a parcel could not be used
at all -- the user could not draw a boundary, let alone reach a step --
and no retry would ever help, because the absence is permanent for that
parcel.

ABSENT IS NOT UNAVAILABLE. Only ABSENCE falls back. A HAG fetch that
RAISES (retries exhausted, an asset that would not open) is a source that
did not answer; that exception still propagates uncaught, and no fallback
runs for it. See _tree_canopy_cover_fallback().

FALLBACK ONLY, NEVER A SECOND GATE. TCC is used only where HAG is
absent. It does NOT union with HAG where both exist -- HAG is the better
measurement, and mixing them would mean no parcel is analysed by a single
consistent rule. A parcel gets ONE canopy source and the record says
which: canopy_source() reads it off the dict, and it surfaces as the
`canopy_data_source` flag.

Coverage that DOES exist but is too sparse to trust is a separate, real
exception (CanopyCoverageIncompleteError below), NOT folded into either
the None case or the fallback: a tile that genuinely covers this parcel
and reads mostly-nodata is a HAG measurement that went wrong, not an
absent one, and swapping in a different product would hide it. That case
is unchanged by this branch.

RETRY BUDGET: max_retries defaults to 5 (not the 2 most other network
layers in this pipeline use) on both _search_hag_items() and
get_canopy_height_for_boundary() -- since a fetch failure here is no
longer something the pipeline quietly shrugs off, it's worth spending
more attempts to distinguish "genuinely unreachable" from "flaky this
one time" before giving up and failing the whole production-zone call.
Neither of those two functions owns a retry LOOP -- both hand their
budget to _retry(), which is where the attempts are actually made and
counted -- so a run's published count for this layer covers the STAC
search and the raster read together, under _retry. See fetch_attempts.py
and the __getattr__ below it.

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
from typing import Optional

import numpy as np
import planetary_computer
import rasterio
from pystac_client import Client
from rasterio.mask import mask
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject, transform_geom
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.prepared import prep

import fetch_attempts
from raster_grid import binary_dilate, pixel_center_xy

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
TREE_ROOT_ZONE_BUFFER_METERS = 10 * METERS_PER_FOOT  # ~3.048m

# Max percent of ON-PARCEL cells (see _on_parcel_nan_fraction()) that may
# be nodata in the clipped/reprojected HAG array before this module treats
# the fetch as unreliable rather than usable. CONFIGURABLE, unvalidated
# against a real property yet, same caveat every other threshold in this
# pipeline carries. This is deliberately NOT the same thing as "zero
# coverage" (get_canopy_height_for_boundary() already returns None for
# that, unchanged) -- this catches the in-between case where a tile
# technically intersects the boundary but only covers a sliver of it, which
# would otherwise silently produce an array that reads as "mostly no
# trees" when it's actually "mostly unchecked".
MAX_ACCEPTABLE_CANOPY_NODATA_PCT = 10.0


class CanopyCoverageIncompleteError(Exception):
    """
    Raised by get_canopy_height_for_boundary() when HAG coverage exists
    for this boundary (so it isn't the "no coverage at all" -> None case)
    but leaves more than MAX_ACCEPTABLE_CANOPY_NODATA_PCT of the ON-PARCEL
    grid as nodata after clipping/reprojection -- too sparse to trust as a
    real "no trees here" signal. Deliberately a real exception, not a
    return value: production_area.py's woody-vegetation gate treats
    "can't verify" the same as "found trees" would be treated -- as a hard
    stop, not a lower-confidence result to hand back with a caveat.
    """


def _boundary_to_polygon(boundary_coordinates: list) -> Polygon:
    """Same convention as imagery_data._boundary_to_polygon()/soil_data.
    coordinates_to_wkt_polygon(): closes the ring if the input isn't
    already closed."""
    coords = list(boundary_coordinates)
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return Polygon(coords)


# --- what a fetch of this module's layers cost, published ---------------
#
# PEP 562. Python calls a module's __getattr__ only when a normal
# attribute lookup fails, and nothing here ever sets the three
# LAST_FETCH_* names -- so every read of them lands here and gets the
# CALLING THREAD's totals from the last @fetch_attempts.publishes call it
# completed in this module. Per-thread and not process-global because two
# sessions created concurrently on different boundaries would otherwise
# overwrite each other's counts between the call returning and the
# diagnostic reading. run_diagnostics.py's contract is unchanged and does
# not know: it does getattr(module, "LAST_FETCH_ATTEMPTS") and gets an
# int. See fetch_attempts.py.
def __getattr__(name):
    return fetch_attempts.published(__name__, name)


def _retry(operation, max_retries: int = 2):
    """Identical progressive-timeout retry helper to imagery_data._retry()
    -- see that module's docstring for the reasoning. Duplicated here
    rather than imported since every other network-backed module in this
    pipeline (soil_data.py, hydrology_data.py, imagery_data.py) keeps its
    own private copy rather than sharing one across modules."""
    last_error = None

    # ATTEMPTS ARE PUBLISHED, NOT SWALLOWED -- fetch_attempts.attempts()
    # yields exactly what range(max_retries + 1) yielded and counts each
    # pass into the ledger the calling layer entry point opened, and
    # fetch_attempts.sleep() pauses for exactly as long as time.sleep(2)
    # did while recording how long that was. Neither changes the budget,
    # the backoff or the progressive timeout. See fetch_attempts.py.
    for attempt in fetch_attempts.attempts(max_retries):
        timeout = 30 + (attempt * 30)
        try:
            return operation(timeout)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                fetch_attempts.sleep(2)
                continue
            raise last_error


def _search_hag_items(polygon: Polygon, max_retries: int = 5) -> list:
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


def _on_parcel_nan_fraction(hag_on_grid: np.ndarray, boundary_coordinates: list, dem: dict) -> tuple[float, int]:
    """
    Fraction of ON-PARCEL cells (cell center inside the real property
    boundary, reprojected into dem['crs']) that are NaN in hag_on_grid --
    same cell-center-containment on-parcel test compute_step1_eligible_
    cells() applies against the real parcel boundary, not the full
    rectangular DEM grid (dem_data.py deliberately fetches a buffer past
    the drawn boundary for terrain analysis -- counting THAT off-parcel
    buffer ground as "missing" would understate real on-parcel coverage,
    since nobody needs HAG data out there).

    Returns (nan_fraction, on_parcel_cell_count) -- nan_fraction is 0.0
    when there are no on-parcel cells at all (degenerate boundary/DEM
    mismatch; nothing to be incomplete about).
    """
    boundary_polygon = _boundary_to_polygon(boundary_coordinates)
    boundary_polygon_utm = shape(transform_geom("EPSG:4326", dem["crs"], mapping(boundary_polygon)))
    boundary_prepared = prep(boundary_polygon_utm)

    rows, cols = hag_on_grid.shape
    on_parcel_count = 0
    on_parcel_nan_count = 0
    for r in range(rows):
        for c in range(cols):
            if boundary_prepared.contains(Point(pixel_center_xy(dem, r, c))):
                on_parcel_count += 1
                if np.isnan(hag_on_grid[r, c]):
                    on_parcel_nan_count += 1

    if on_parcel_count == 0:
        return 0.0, 0
    return on_parcel_nan_count / on_parcel_count, on_parcel_count


def _tree_canopy_cover_fallback(
    boundary_coordinates: list, dem: dict, max_retries: int = 5
) -> Optional[dict]:
    """
    THE FALLBACK, AND THE ONLY PLACE IT IS REACHED FROM. Called by
    get_canopy_height_for_boundary() at each of its three HAG-IS-ABSENT
    points and nowhere else.

    ABSENT, NOT UNAVAILABLE -- the distinction this whole function turns
    on. A HAG fetch that RAISES (retries exhausted, a signed URL that
    would not open) is a source that did not answer, which may answer on
    the next try; that exception propagates uncaught and no fallback runs
    for it. Absence -- no item for this project area, or an item whose
    every pixel over this boundary is nodata -- is permanent for this
    parcel, and no number of retries changes it. Only absence falls back.

    NOT A SECOND GATE. This runs only where HAG produced nothing; where
    HAG produced anything at all, this is never called and the two are
    never unioned (see the module docstring's FALLBACK section, and
    canopy_cover_data.py's).

    Returns the TCC canopy dict, or None when TCC has nothing usable here
    either -- in which case the caller has no canopy source at all and
    the layer hard-fails upstream (parcel_data.py, production_zone_
    payload.py). Deliberately does NOT swallow a TCC request failure: a
    raise from the fallback is still a real failure and is reported as
    one.

    THE CALLER'S OWN RETRY BUDGET is threaded through, not a second one
    declared here. By the time this runs, TCC is the LAST canopy source
    there is for this parcel, so it is worth exactly what the HAG search
    was worth -- see the module docstring's RETRY BUDGET section for why
    that is 5 and not the 2 most layers use. The attempts land in this
    layer's own ledger (fetch_attempts.py), which is correct: a fallback
    fetch is part of what fetching the canopy layer cost.
    """
    from canopy_cover_data import get_tree_canopy_cover_for_boundary

    return get_tree_canopy_cover_for_boundary(
        boundary_coordinates, dem, max_retries=max_retries
    )


@fetch_attempts.publishes
def get_canopy_height_for_boundary(
    boundary_coordinates: list,
    dem: dict,
    max_retries: int = 5,
) -> Optional[dict]:
    """
    Given a property boundary (list of (longitude, latitude) points, same
    convention as every other module in this pipeline) and the DEM dict
    dem_data.get_dem_for_boundary() already produced for this same
    property (reused here, not re-fetched -- this module needs it only as
    the target grid definition), finds the best-covering 3dep-lidar-hag
    tile, clips it to the boundary, and reprojects the result onto the
    DEM's own grid. WHERE THERE IS NO SUCH TILE, falls back to NLCD Tree
    Canopy Cover -- see _tree_canopy_cover_fallback() and the module
    docstring.

    THE RETURN IS ONE OF TWO SHAPES, and 'source' is what tells them
    apart. Never read 'array' without reading 'source' first: the two
    arrays are not the same kind of number, and thresholding one the
    other's way is a silent unit error. Use canopy_source() to read it
    and root_zone_mask_from_canopy() to derive a mask, rather than
    branching at each consumer.

    On the lidar HAG path:
        {
            'array': np.ndarray, shape matching dem['array'], float32
                     METERS HEIGHT-ABOVE-GROUND, np.nan where no HAG
                     coverage exists for that cell,
            'units': 'meters_above_ground',
            'source': CANOPY_SOURCE_LIDAR_HAG,
            'resolution_meters': dem['resolution_meters'],
            'origin_x': dem['origin_x'],
            'origin_y': dem['origin_y'],
            'crs': dem['crs'],
            'source_item_id': the STAC item id the data came from,
        }

    On the NLCD TCC fallback path: canopy_cover_data.get_tree_canopy_
    cover_for_boundary()'s own dict, verbatim -- same grid keys, but
    'array' is PERCENT COVER 0-100, 'units' is 'percent_cover' and
    'source' is CANOPY_SOURCE_NLCD_TCC. See that function for the rest.

    Returns None ONLY when BOTH sources have nothing for this boundary:
    no 3dep-lidar-hag tile intersects it (or the best-covering tile's
    clipped/reprojected result has no valid pixels anywhere) AND the TCC
    fallback has no usable cover here either. A genuine "no canopy data
    exists for this land" outcome rather than a failed request -- same
    convention imagery_data.get_imagery_summary_for_boundary() and
    irradiance_data.get_regional_irradiance_baseline() already use for
    their own "nothing usable was found" cases, and still a hard failure
    upstream (parcel_data.py), because canopy did not become optional.

    Raises CanopyCoverageIncompleteError if coverage exists (so it isn't
    the None case above) but leaves more than MAX_ACCEPTABLE_CANOPY_
    NODATA_PCT of the ON-PARCEL grid (see _on_parcel_nan_fraction()) as
    nodata -- a tile that only grazes the boundary can technically
    "intersect" it while covering almost none of the actual parcel, which
    would otherwise silently read as "checked, mostly no trees" rather
    than "barely checked at all". Unlike the None case, this is
    deliberately a hard failure, not a value for callers to degrade
    gracefully on -- see production_area.py's woody-vegetation gate,
    which does not catch it.
    """
    polygon = _boundary_to_polygon(boundary_coordinates)

    items = _search_hag_items(polygon, max_retries=max_retries)
    if not items:
        # No HAG ITEM AT ALL for this project area -- the Maryland case.
        # Permanent for this parcel, not a transient outage. Fall back.
        return _tree_canopy_cover_fallback(boundary_coordinates, dem, max_retries)

    item = items[0]  # already ranked best-covering-first
    href = item.assets[HAG_ASSET_KEY].href

    clipped, clipped_transform, clipped_crs, nodata = _retry(
        lambda timeout: _read_clipped_hag(href, polygon, timeout), max_retries=max_retries
    )

    valid = (~np.isnan(clipped)) & (~np.isclose(clipped, nodata))
    if not np.any(valid):
        # A tile intersects, but every pixel over this boundary is nodata:
        # HAG is ABSENT here in the only sense that matters. Fall back.
        return _tree_canopy_cover_fallback(boundary_coordinates, dem, max_retries)

    hag_on_grid = _reproject_to_dem_grid(clipped, clipped_transform, clipped_crs, nodata, dem)
    if not np.any(~np.isnan(hag_on_grid)):
        return _tree_canopy_cover_fallback(boundary_coordinates, dem, max_retries)

    nan_fraction, on_parcel_count = _on_parcel_nan_fraction(hag_on_grid, boundary_coordinates, dem)
    if on_parcel_count > 0 and nan_fraction * 100 > MAX_ACCEPTABLE_CANOPY_NODATA_PCT:
        raise CanopyCoverageIncompleteError(
            f"USGS 3DEP lidar HAG coverage for this property is only "
            f"{100 - nan_fraction * 100:.1f}% complete on-parcel ({nan_fraction * 100:.1f}% "
            f"no-data), exceeding the {MAX_ACCEPTABLE_CANOPY_NODATA_PCT}% max acceptable "
            f"no-data threshold (source tile: {item.id})."
        )

    return {
        "array": hag_on_grid,
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        # THE SOURCE, ON THE DICT ITSELF. Every consumer that needs to know
        # a parcel is running on the coarser fallback reads it from here
        # (via canopy_source()); nothing infers it from which keys are
        # present. 'units' names what 'array' holds, because the fallback's
        # array holds something else entirely.
        "source": CANOPY_SOURCE_LIDAR_HAG,
        "units": "meters_above_ground",
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


# --- WHICH SOURCE A PARCEL'S CANOPY CAME FROM --------------------------
#
# Two values, and there will not quietly be a third: a parcel gets ONE
# canopy source (see the module docstring's FALLBACK section -- TCC does
# NOT union with HAG where both exist) and the record says which.
#
# STABLE IDENTIFIERS, not display prose. These are what the
# `canopy_data_source` flag carries onto the wire, into the diagnostics
# sweep (run_diagnostics.py records any key ending `_source`) and into
# exclusion_zones' canopy layer. A consumer branches on these strings;
# the wording a user reads is the frontend's, and is not written in this
# branch.
CANOPY_SOURCE_LIDAR_HAG = "lidar_hag"
CANOPY_SOURCE_NLCD_TCC = "nlcd_tcc"


def canopy_source(canopy: Optional[dict]) -> Optional[str]:
    """
    Which source a canopy dict came from: CANOPY_SOURCE_LIDAR_HAG or
    CANOPY_SOURCE_NLCD_TCC. None for a None canopy (no source at all).

    A dict with NO 'source' key reads as lidar HAG. That is not leniency
    -- it is the only correct reading: every canopy dict predating the
    fallback is a HAG dict, including the fixtures a dozen offline tests
    build by hand, and the TCC path always sets the key explicitly.
    """
    if not canopy:
        return None
    return canopy.get("source", CANOPY_SOURCE_LIDAR_HAG)


def root_zone_mask_from_canopy(
    canopy: dict,
    buffer_meters: float = TREE_ROOT_ZONE_BUFFER_METERS,
) -> np.ndarray:
    """
    THE ONE PLACE A CANOPY DICT BECOMES A ROOT-ZONE MASK, dispatching on
    which source it came from. Network-free.

    This exists because the two sources' 'array' values are not the same
    KIND of number: HAG's is metres of height above ground (thresholded
    at CANOPY_HEIGHT_THRESHOLD_METERS), TCC's is percent cover 0-100
    (any nonzero value is canopy). Thresholding one the other's way is a
    unit error that would produce a plausible-looking mask -- 4.5 read as
    a percentage marks almost everything wooded; 15 read as metres marks
    almost nothing. Dispatching here, once, is what keeps that impossible
    for every consumer.

    Both branches apply the SAME buffer through the SAME dilation, so the
    two masks differ by the measurement and by nothing else.

    Raises ValueError on an unrecognised source rather than guessing --
    a canopy dict this function cannot read is not something to derive a
    mandatory exclusion gate from.
    """
    source = canopy_source(canopy)
    if source == CANOPY_SOURCE_LIDAR_HAG:
        return tree_root_zone_mask(
            canopy["array"], canopy["resolution_meters"], buffer_meters=buffer_meters
        )
    if source == CANOPY_SOURCE_NLCD_TCC:
        from canopy_cover_data import canopy_cover_root_zone_mask

        return canopy_cover_root_zone_mask(
            canopy["array"], canopy["resolution_meters"], buffer_meters=buffer_meters
        )
    raise ValueError(
        f"canopy dict carries an unrecognised source {source!r} -- expected "
        f"{CANOPY_SOURCE_LIDAR_HAG!r} or {CANOPY_SOURCE_NLCD_TCC!r}"
    )


def summarize_canopy_height(canopy: Optional[dict]) -> str:
    """Plain-language summary, same purpose as dem_data.summarize_dem().
    SOURCE-AWARE: a TCC fallback dict is summarized by canopy_cover_data.
    summarize_tree_canopy_cover(), which reports percent cover, the
    254/255 counts and the blockiness -- summarizing it in metres of
    height-above-ground would be describing a measurement that was never
    taken."""
    if not canopy:
        return (
            "No canopy coverage was found for this property -- neither USGS 3DEP lidar "
            "height-above-ground nor the NLCD Tree Canopy Cover fallback has data here."
        )

    if canopy_source(canopy) == CANOPY_SOURCE_NLCD_TCC:
        from canopy_cover_data import summarize_tree_canopy_cover

        return summarize_tree_canopy_cover(canopy)

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
