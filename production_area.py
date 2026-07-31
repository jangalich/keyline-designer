"""
production_area.py

Heuristic identification of candidate production/cultivation area(s) on a
property from DEM-derived slope — a structured stand-in for the same
judgment report_generator.py's Scale of Permanence prompt already makes in
prose (the "Land Shape" section: which parts of the property read as
strong, workable production land versus steep/awkward/marginal ground).

This is the FOUNDATION module of the consolidated production-zone
pipeline (this file / production_area_ceiling.py / production_suitability.py
together). It owns STEP 1 and STEP 3 of that pipeline, which every entry
point in the other two files reuses rather than recomputing:

    STEP 1 -- compute_step1_eligible_cells(): for every on-parcel DEM
        cell, hard, cell-level gates decide eligibility outright: slope
        (MAX_PRODUCTION_SLOPE_PCT), hydric/disqualifying soil (soil_data.
        hydric_disqualifying_mukeys(), tested against each cell's own
        CENTER POINT, not a continuous-geometry difference), and woody-
        vegetation root zone (canopy_height_data.tree_root_zone_mask() --
        USGS 3DEP lidar height-above-ground thresholded and DILATED as a
        raster mask, same reasoning as below, never vectorized). "On-
        parcel" itself is tested against the real parcel boundary shrunk
        inward by PRODUCTION_BOUNDARY_SETBACK_METERS, not the raw
        boundary -- a plain shapely negative buffer, since that's a single
        clean polygon operation on already-known parcel geometry, not
        raster-derived. Every eligible cell is then scored on slope+aspect
        only (size/compactness isn't a per-cell property; see production_
        suitability.py's STEP 4 for that). Computed ONCE per pipeline run
        and reused by every step after it -- this is what replaces the old
        architecture's three independent mask rebuilds
        (identify_production_areas(), production_suitability.py's
        cell-recovery, production_area_ceiling.py's pool-building) and the
        old continuous-geometry soil-carving pass entirely. A cell is
        either in the eligible mask or it isn't -- no polygon differencing,
        so no spurious slivers can be created by construction. The same
        logic applies to the tree-root-zone gate: threshold-then-dilate
        stays a raster operation on the cell mask throughout, precisely to
        avoid reintroducing that same sliver-fragmentation failure mode via
        a vectorize-then-buffer/difference shortcut.

    STEP 3 -- cluster_and_gate(): 8-connected-component labeling
        (raster_grid.connected_components()) of WHATEVER cell mask a caller
        passes in (STEP 1's raw eligible mask here, or
        production_area_ceiling.py's post-STEP-2-trim survivor mask), each
        cluster's REAL cell-union footprint (_cell_union_footprint(), not a
        convex hull), and a pure area survival gate
        (MIN_PRODUCTION_AREA_ACRES) -- nothing else gates survival here; no
        soil check, since hydric cells were already excluded at STEP 1.
        Shared by this module's own identify_production_areas() (no ceiling
        trim) and production_area_ceiling.py's identify_optimized_production_
        areas() (with the trim) -- one implementation, not two. Also runs two
        further sub-steps per cluster, each cluster mask's own raster
        morphology, NOT continuous-geometry polygon work: WAIST detection/
        splitting (a narrower-than-MIN_ZONE_WAIST_METERS pinch reads as two
        zones, not one -- see _attempt_waist_split()) and true-HOLE
        detection (excluded ground fully enclosed by a cluster's own
        eligible cells, with no path to its outer boundary -- see
        _detect_hole_footprints()). See cluster_and_gate()'s own docstring
        for the full detail; a waist triggers a split, a hole doesn't (there
        is nothing to split -- it's one solid lobe with a gap) but is
        surfaced as narrative metadata via 'hole_footprints' (e.g. "a wet
        spot mid-field" worth calling out in report text) -- production
        zones render as clipped contour-line texture (contour_lines.py),
        not a filled/smoothed shape, so there is no separate display
        geometry to punch a hole out of anymore.

identify_production_areas() is STEP 1 (no trim) + STEP 3 chained together,
plus its own disqualifying-soil fetch (graceful-degrading, same
fetch-then-continue pattern every other optional network layer in this
pipeline uses) -- this is the "raw" production-area candidate list every
existing caller (water_candidate_zones.py, road_corridors.py,
solar_suitability.py) already consumes via this exact function/signature;
they get STEP 1's cell-level hydric exclusion automatically, with no
changes needed on their end. It does NOT apply production_area_ceiling.py's
80%-of-parcel ceiling trim (STEP 2) -- these three modules deliberately
still read the un-trimmed candidate list; see production_area_ceiling.py's
and tree_zone_candidates.py's own docstrings for why that's a separate,
already-tracked wiring item, not something this consolidation changes.

PARCEL CLIPPING: identify_production_areas() requires the real parcel
boundary (boundary_polygon_utm — dem_data.py deliberately fetches a DEM
buffered 100m past the drawn boundary) and clips every candidate down to
its on-parcel portion (STEP 1's own on-parcel cell test) before returning
it. A patch with zero on-parcel area, or whose clipped remainder falls
below MIN_PRODUCTION_AREA_ACRES, is dropped entirely.

FOOTPRINT GEOMETRY: polygon_utm/area_acres/geometry_wgs84 are built from
the REAL per-cell-square union footprint (_cell_union_footprint()), NOT a
convex hull of cell center points -- a convex hull fills in concave gaps
between actual qualifying cells with ground that was never actually
eligible, over-reporting the real footprint. _cell_union_footprint()
snaps each cell square's corners to a shared coordinate expression
(derived directly from the DEM's own origin/resolution, not a cell
CENTER offset by +/- half a width) so adjacent cells' shared edges are
bit-for-bit identical -- confirmed live: without this, floating-point
rounding on realistic (large-magnitude UTM) origin values left visible
razor-thin sliver gaps in the unary_union'd footprint instead of a fully
dissolved polygon. polygon_utm/geometry_wgs84 CAN legitimately come back
as a MultiPolygon: two cells whose real ground squares only touch at a
shared corner don't merge into one solid Polygon under unary_union.
render_layout_map.py draws production zones directly from geometry_wgs84
(clipped contour-line texture, via contour_lines.py) -- there is no
separate display/smoothed geometry field; see that module's own docstring
for why filled-shape rendering (and the cosmetic hull-smoothing it used
to need) was replaced.

TRUE HOLES vs WAISTS: cluster_and_gate() also carries each cluster's own
'hole_footprints' (list[Polygon], [] if none) -- real, excluded ground
fully enclosed by that cluster's own eligible cells, with no path to its
outer boundary. This is DIFFERENT from a "waist" (a pinch narrower than
MIN_ZONE_WAIST_METERS that DOES have an opening to the outside on both
sides): a waist triggers a split into independent clusters; a hole does
not (there's nothing to split -- it's one solid lobe with a gap).
hole_footprints is narrative metadata only (e.g. "a wet spot mid-field"
worth calling out in report text) -- it does not feed rendering or any
other geometry, and polygon_utm/area_acres are unaffected either way --
the real cell-union footprint already excludes hole cells by
construction.
"""

import math
from collections import deque

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from canopy_height_data import get_canopy_height_for_boundary, tree_root_zone_mask
from farm_roads_data import get_road_exclusion_union_utm
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_suitability import ASPECT_FACTOR_WEIGHT, SLOPE_FACTOR_WEIGHT
from raster_grid import (
    D8_OFFSETS,
    SQUARE_METERS_PER_ACRE,
    binary_dilate,
    binary_erode,
    cell_area_acres,
    connected_components,
    pixel_center_xy,
)
from soil_data import (
    coordinates_to_wkt_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    hydric_disqualifying_mukeys,
)
from terrain_metrics import aspect_score, compute_slope_and_aspect

# Cells at or below this slope are considered plausibly workable
# production/cultivation ground for this heuristic. 15% is a common
# rule-of-thumb ceiling for mechanized row-crop work specifically, but
# ground up to ~20-25% is workable for pasture, hay, or contour-managed
# production — 20% still leaves meaningful separation from genuinely
# steep, erosion-prone ground. This is a HARD gate (cells above it are
# excluded outright, never merely scored low) — see STEP 1's own docstring
# for why a soft score here would let a worst-first trim end up keeping
# genuinely-too-steep cells just to hit a volume target. CONFIGURABLE.
MAX_PRODUCTION_SLOPE_PCT = 20.0

# Drop tiny, likely-noisy contiguous patches below this size. CONFIGURABLE.
MIN_PRODUCTION_AREA_ACRES = 0.5

# A single 8-connected cluster can legitimately contain a narrow "waist" --
# real, physically connected eligible ground that pinches down to
# something too narrow to sensibly treat as one zone (e.g. two decent-
# sized fields joined by a thin strip). Narrower than this, and it reads
# as two separate zones rather than one -- see cluster_and_gate()'s own
# docstring for the erosion-based detection this feeds. 40 ft (~12m) is a
# documented STARTING value only -- like every other threshold in this
# pipeline (MAX_PRODUCTION_SLOPE_PCT, MIN_PRODUCTION_AREA_ACRES), it is
# UNVALIDATED against real ground-truth and needs tuning against real
# properties, not a derived or measured figure. CONFIGURABLE.
MIN_ZONE_WAIST_METERS = 12.0  # ~40 ft

# Genuinely excluded ground (steep slope or hydric soil -- STEP 1's own
# gates, NOT road/tree) can sit as a small, scattered pocket entirely
# inside an otherwise-solid zone, rendering as an unexplained blank gap
# in that zone's own contour-line texture -- confirmed live. render_
# fill_polygon_utm (see cluster_and_gate()'s own docstring) closes over
# pockets up to roughly TWICE this radius wide via a morphological
# CLOSING (dilate-then-erode, see _close_cell_mask()) of the zone's own
# render cell mask, so contour lines drawn against it simply continue
# across a small real pocket instead of leaving a hole.
#
# HARD CONSTRAINT: closing operates on render_polygon_utm's own
# PRE-reclaim cells (see _attempt_waist_split()) -- a radius large
# enough to also close over a real inter-zone waist-split gap would
# silently defeat the whole reason render_polygon_utm exists, re-fusing
# two zones that are genuinely separate. Since closing can bridge a gap
# up to roughly 2x this radius wide, this value must stay under HALF the
# smallest real waist-split gap this pipeline can produce. Empirically
# probed against this pipeline's own erosion math at dem_data.py's fixed
# 5m DEM resolution (dem_data.DEFAULT_RESOLUTION_METERS): the most
# adversarial synthetic case (a single-pixel-wide, single-row-long throat
# right at the MIN_ZONE_WAIST_METERS threshold -- about as thin a real
# waist as this pipeline can split on) produces a ~25m render_polygon_utm
# gap, so this value is kept comfortably under half of that (12.5m) with
# real margin. Documented STARTING value, like every other threshold in
# this pipeline -- UNVALIDATED against real ground-truth beyond that
# synthetic probe, tuned against live screenshots showing small excluded
# pockets not yet fully closed. CONFIGURABLE, but re-run test_production_
# area.py's/test_render_layout_map.py's waist-split non-overlap
# regressions before raising it further.
FILL_SMOOTHING_RADIUS_METERS = 10.0

METERS_PER_FOOT = 0.3048

# Cells within this distance of the real parcel boundary are excluded from
# eligibility outright -- production ground shouldn't be planned flush
# against a property line. 10ft, CONFIGURABLE, unvalidated against a real
# property yet (same caveat every other threshold in this pipeline
# carries). Applied as a single clean shapely negative buffer on the
# boundary polygon itself, not a raster operation -- the parcel boundary
# is already known, clean polygon geometry, so unlike the tree-root-zone
# mask below there's no raster-fragmentation risk here to avoid. This
# SHRINKS the polygon compute_step1_eligible_cells() tests each cell
# CENTER against for its existing on-parcel filter (see that function's
# docstring) -- it does not change boundary_polygon_utm itself, which
# callers (cluster_and_gate()'s footprint clip,
# production_area_ceiling.py's parcel-acreage ceiling math) still use as
# the real, full parcel boundary.
PRODUCTION_BOUNDARY_SETBACK_METERS = 10 * METERS_PER_FOOT  # ~3.048m

# --- per-cell weighting (STEP 1's own scoring, used to order STEP 2's
# worst-first ceiling trim) ---
#
# production_suitability.py's zone-level composite score weights three
# factors: SLOPE_FACTOR_WEIGHT (0.55), SIZE_FACTOR_WEIGHT (0.30, acreage +
# Polsby-Popper shape compactness), ASPECT_FACTOR_WEIGHT (0.15). size_factor
# is a CLUSTER-level shape property -- "is this patch's footprint compact
# or a thin sliver" has no meaning for a single grid cell, so it's excluded
# from per-cell scoring entirely rather than forced onto cells it can't
# describe. The remaining two factors' EXISTING relative weight (0.55:0.15,
# i.e. 11:3) is preserved here, just renormalized to sum to 1.0 on its own
# -- not a new ratio invented for this pass.
_PER_CELL_WEIGHT_SUM = SLOPE_FACTOR_WEIGHT + ASPECT_FACTOR_WEIGHT
PER_CELL_SLOPE_WEIGHT = SLOPE_FACTOR_WEIGHT / _PER_CELL_WEIGHT_SUM
PER_CELL_ASPECT_WEIGHT = ASPECT_FACTOR_WEIGHT / _PER_CELL_WEIGHT_SUM

assert math.isclose(PER_CELL_SLOPE_WEIGHT + PER_CELL_ASPECT_WEIGHT, 1.0, abs_tol=1e-9), (
    "per-cell slope/aspect weights must sum to 1.0"
)

# Sentinel distinguishing "the disqualifying-soil fetch genuinely ran and
# found nothing" (a real value of None) from "never checked at all" (fetch
# failed, or check_soil=False) -- same reasoning as the rest of this
# pipeline's None-vs-"unavailable" conventions.
_SOIL_CHECK_UNCHECKED = object()

# Same convention as _SOIL_CHECK_UNCHECKED, for existing-road exclusion --
# and, unlike the tree-root-zone mask below, a real None here DOES mean
# the same thing None means for soil: "checked, and genuinely no roads
# found nearby" (farm_roads_data.get_road_exclusion_union_utm()'s own
# clean-result convention), a real value distinct from "never checked at
# all" (fetch failed, or check_roads=False). Road exclusion degrades
# GRACEFULLY on fetch failure, same as soil -- unlike the woody-vegetation
# gate, this is not a newly-closed detection gap, just the same known-
# incomplete (public-ROW-only) signal farm_roads_data.py already was; see
# that module's own docstring.
_ROAD_CHECK_UNCHECKED = object()

# Same convention as _SOIL_CHECK_UNCHECKED, for the tree-root-zone mask --
# still used by compute_step1_eligible_cells()'s own default, and by
# production_area_ceiling.optimize_production_areas()'s own default (the
# "pure logic, no network I/O" core, which can still be called directly,
# e.g. in tests, without any canopy data at all -- see that function's own
# docstring). Both this module's identify_production_areas() and
# production_area_ceiling.identify_optimized_production_areas() -- the
# two real NETWORK entry points -- no longer have an "unchecked" path of
# their own: the woody-vegetation gate is mandatory there, so each always
# passes a real, checked mask into compute_step1_eligible_cells() (or
# raises before ever reaching it). See get_required_tree_root_zone_mask_
# utm(), the shared fetch-or-raise helper both entry points call.
_CANOPY_CHECK_UNCHECKED = object()

PRODUCTION_AREA_CONFIDENCE_NOTES = (
    "This is a slope + hydric-soil + woody-vegetation + existing-road "
    f"heuristic (contiguous ground at or below {MAX_PRODUCTION_SLOPE_PCT}% "
    "grade, with hydric/wetland soil excluded cell-by-cell before "
    "clustering -- see soil_data.hydric_disqualifying_mukeys() -- tree-"
    "root-zone cells excluded cell-by-cell via USGS 3DEP lidar height-"
    "above-ground, see canopy_height_data.py -- and existing mapped road "
    "right-of-way excluded cell-by-cell, see farm_roads_data.get_road_"
    "exclusion_union_utm()), not a validated production-area "
    f"determination. Cells within {PRODUCTION_BOUNDARY_SETBACK_METERS:.1f}m "
    "of the real parcel boundary are also excluded (a fixed inward "
    "setback, not derived from any of the above). Unlike the soil and "
    "road checks, the woody-vegetation check is mandatory: identify_"
    "production_areas() refuses to return any candidate at all if USGS "
    "3DEP lidar HAG coverage couldn't be fetched or was too sparse to "
    "trust, rather than returning geometry that was never actually "
    "checked for tree cover -- see canopy_height_data.py. Road exclusion "
    "degrades gracefully like the soil check: if USGS National Map road "
    "data couldn't be fetched, this candidate's geometry may still "
    "include ground within existing road right-of-way that wasn't caught "
    "-- see farm_roads_data.py, which is itself a known-incomplete "
    "(public-ROW-only) signal even when the fetch succeeds. It doesn't "
    "account for soil quality beyond the hard hydric exclusion, drainage, "
    "existing land use, or the property owner's actual crop/grazing plans "
    "— see soil_data.py and the Land Shape section of the full Scale of "
    "Permanence report for those factors."
)


def compute_slope_percent(
    array: np.ndarray, resolution_meters: tuple[float, float]
) -> np.ndarray:
    """
    Local terrain steepness at every valid cell: the steepest elevation
    change (up or down, whichever neighbor differs most per unit ground
    distance) among its up-to-8 neighbors, as a percent grade
    (rise/run * 100). NaN at nodata cells.
    """
    rows, cols = array.shape
    valid = ~np.isnan(array)
    px, py = resolution_meters
    slope = np.full((rows, cols), np.nan, dtype=np.float32)

    offsets = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            steepest = 0.0
            for dr, dc in offsets:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    distance = math.hypot(dc * px, dr * py)
                    grade = abs(float(array[r, c]) - float(array[nr, nc])) / distance * 100.0
                    steepest = max(steepest, grade)
            slope[r, c] = steepest

    return slope


def _slope_factor(slope_values_pct: list[float], max_slope_pct: float) -> float:
    """1.0 at 0% grade, falling linearly to 0.0 at max_slope_pct -- the same
    slope ceiling that decides eligibility at all, so a cell/cluster sitting
    right at that ceiling (barely qualifying) scores near 0 rather than
    being treated the same as dead-flat ground."""
    if not slope_values_pct:
        return 0.0
    avg_slope = float(np.mean(slope_values_pct))
    return max(0.0, min(1.0, 1.0 - avg_slope / max_slope_pct))


def per_cell_score(slope_pct: float, aspect_deg: float, max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT) -> float:
    """
    0-1 per-cell quality score for one DEM cell's own real slope/aspect --
    reuses _slope_factor() (called on a length-1 list, the same formula
    production_suitability.py's STEP 4 composite uses at the cluster level)
    and terrain_metrics.aspect_score() (already a per-value function, so it
    needs no adaptation). See PER_CELL_SLOPE_WEIGHT/PER_CELL_ASPECT_WEIGHT
    above for the weighting and why size has no per-cell counterpart.
    """
    slope_factor = _slope_factor([slope_pct], max_slope_pct)
    aspect_factor = aspect_score(aspect_deg)
    return PER_CELL_SLOPE_WEIGHT * slope_factor + PER_CELL_ASPECT_WEIGHT * aspect_factor


def _fetch_disqualifying_soil_union(wkt_polygon: str, dem: dict):
    """
    Real SSURGO polygon geometry (not just component ratings) for every map
    unit whose SUMMED hydric component percentage meets
    soil_data.MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE, within wkt_polygon,
    unioned and reprojected into dem['crs']. Returns None if nothing
    disqualifying was found (the common, clean case -- not an error).

    soil_data.hydric_disqualifying_mukeys() decides which mukeys qualify --
    shared, not reimplemented here, with road_corridors.py's own hydric
    union fetch. Component-table rows (get_soil_data_for_polygon()) decide
    WHICH mukeys qualify; a second, geometry-specific query
    (get_soil_geometries_for_polygon()) fetches only those mukeys' actual
    polygons.

    Fetched ONCE per pipeline call, over the real parcel boundary (or
    whatever region a caller passes) -- STEP 1 tests each ELIGIBLE cell's
    own center point against this single union, so there is no need for a
    separate per-candidate-patch soil fetch the way the old, pre-
    consolidation architecture required (patches didn't exist yet when this
    runs).
    """
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    disqualifying_mukeys = hydric_disqualifying_mukeys(soil_components)
    if not disqualifying_mukeys:
        return None

    geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)
    pieces = [
        shape(transform_geom("EPSG:4326", dem["crs"], geometries_by_mukey[mukey]))
        for mukey in disqualifying_mukeys
        if mukey in geometries_by_mukey
    ]
    return unary_union(pieces) if pieces else None


def _fetch_road_exclusion_union_utm(boundary_coordinates: list, dem: dict):
    """
    Thin wrapper around farm_roads_data.get_road_exclusion_union_utm() --
    exists so BOTH identify_production_areas() (this module) and
    production_area_ceiling.identify_optimized_production_areas() call
    the SAME function (imported from here, not straight from farm_roads_
    data.py independently in each caller), the same "shared helper, one
    definition" reasoning _fetch_disqualifying_soil_union() above already
    established for the hydric-soil fetch -- among other things, this
    means a single test mock of get_road_exclusion_union_utm here covers
    both entry points, rather than needing to patch two independent
    import bindings.

    Returns None if no roads were found nearby -- the common, clean case,
    same "checked and genuinely nothing there" convention _fetch_
    disqualifying_soil_union() uses. Any fetch failure is left to
    propagate up uncaught; callers degrade gracefully on their own (see
    identify_production_areas()'s own check_roads handling).
    """
    return get_road_exclusion_union_utm(boundary_coordinates, dem)


def _fetch_tree_root_zone_mask_utm(boundary_coordinates: list, dem: dict):
    """
    Fetches USGS 3DEP lidar HAG coverage for this boundary and returns the
    dilated tree-root-zone cell mask (canopy_height_data.tree_root_zone_
    mask()'s own output -- already on dem's own grid, so it can be used
    directly against compute_step1_eligible_cells()'s cell indices with no
    further alignment work).

    Returns None if no HAG coverage exists for this boundary at all -- a
    genuine no-data outcome (see canopy_height_data.py's own docstring),
    passed straight through unchanged; the caller (identify_production_
    areas()) treats a None result here as a hard failure (raises
    RuntimeError), NOT something to gracefully degrade on. Coverage that
    exists but is too sparse to trust (canopy_height_data.
    CanopyCoverageIncompleteError) and any other fetch failure are left to
    propagate up uncaught -- this function does no exception handling of
    its own beyond the None pass-through.
    """
    canopy = get_canopy_height_for_boundary(boundary_coordinates, dem)
    if canopy is None:
        return None
    return tree_root_zone_mask(canopy["array"], canopy["resolution_meters"])


def get_required_tree_root_zone_mask_utm(boundary_polygon_utm: Polygon, dem: dict):
    """
    Fetches a REQUIRED (non-optional) tree-root-zone mask for
    boundary_polygon_utm -- the shared "fetch canopy, or fail hard"
    building block behind the woody-vegetation gate, so every entry point
    that ultimately calls compute_step1_eligible_cells() applies it
    identically instead of each reimplementing (or, worse, quietly
    omitting) its own copy. identify_production_areas() (this module) and
    production_area_ceiling.identify_optimized_production_areas() both
    call this directly, rather than either duplicating the boundary-
    reprojection + fetch + raise sequence itself.

    Reprojects boundary_polygon_utm to WGS84 (the lon/lat convention
    canopy_height_data.get_canopy_height_for_boundary() takes) and calls
    _fetch_tree_root_zone_mask_utm().

    Raises RuntimeError if no HAG coverage exists for this boundary at
    all -- "can't verify this is free of tree cover" is treated as a hard
    failure here, not a lower-confidence result to hand back with a
    caveat (see this module's own identify_production_areas() docstring
    for the reasoning). Any OTHER fetch failure -- retries exhausted, or
    canopy_height_data.CanopyCoverageIncompleteError for coverage that
    exists but is too sparse to trust -- is left to propagate up
    UNCAUGHT, unchanged: this function does no exception handling beyond
    the None-vs-RuntimeError translation.
    """
    xs, ys = boundary_polygon_utm.exterior.coords.xy
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
    boundary_coordinates = list(zip(lons, lats))
    tree_root_zone_mask_utm = _fetch_tree_root_zone_mask_utm(boundary_coordinates, dem)
    if tree_root_zone_mask_utm is None:
        raise RuntimeError(
            "Canopy height data unavailable for this property -- cannot verify "
            "production zones are free of tree cover. Refusing to generate a "
            "production zone without this check."
        )
    return tree_root_zone_mask_utm


def _cell_union_footprint(cells: list[tuple[int, int]], dem: dict):
    """
    The REAL footprint of a set of DEM cells: the union of each cell's own
    ground square at the DEM's resolution -- NOT a convex hull of cell
    CENTER points. A convex hull of centers fills in any concave gaps
    between the actual qualifying cells with ground that was never actually
    a surviving cell at all -- for a large, roughly-solid patch this means
    the hull reports MORE area than the true footprint; for a sparse,
    scattered fragment it can under-report instead (it misses each
    individual cell's own half-cell-width margin the real union always
    includes). Either way, the hull is only an approximation and this
    function is the accurate one every spatial consumer should use.

    GRID-SEAM FIX: each square's corners are computed directly from its
    own row/col boundary via `origin +/- N * resolution` -- NOT via
    pixel_center_xy() (a cell's CENTER) offset by +/- half a cell width.
    The old center-then-half-width approach computed each shared edge via
    two DIFFERENT floating-point expressions depending on which
    neighboring cell was doing the computing (e.g. cell c's right edge as
    `(origin + (c+0.5)*px) + px/2` vs cell c+1's left edge as
    `(origin + (c+1.5)*px) - px/2`) -- mathematically identical, but not
    bit-for-bit identical once origin_x/origin_y are realistic
    large-magnitude UTM values (confirmed live: this left visible
    razor-thin sliver gaps in rendered output, unary_union() failing to
    fully dissolve adjacent squares' shared edges). Computing both cell
    c's right edge and cell c+1's left edge from the exact same expression
    (`origin + (c+1) * resolution`) makes every shared edge bit-for-bit
    identical by construction, regardless of origin's magnitude -- a real
    correctness fix to the geometry itself, not a cosmetic one, since
    every consumer of this function (polygon_utm/area_acres/
    geometry_wgs84, not just rendering) was getting the same
    sliver-fragmented footprint. buffer(0) afterward is cheap, defensive
    cleanup against any remaining near-zero-area topology noise from
    unary_union'ing many touching squares -- the corner-snapping above
    should already make it a no-op in practice.
    """
    px, py = dem["resolution_meters"]
    origin_x = dem["origin_x"]
    origin_y = dem["origin_y"]
    squares = []
    for r, c in cells:
        x0 = origin_x + c * px
        x1 = origin_x + (c + 1) * px
        y1 = origin_y - r * py
        y0 = origin_y - (r + 1) * py
        squares.append(box(x0, y0, x1, y1))
    return unary_union(squares).buffer(0)


def compute_step1_eligible_cells(
    dem: dict,
    boundary_polygon_utm: Polygon,
    disqualifying_soil_union_utm=_SOIL_CHECK_UNCHECKED,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    tree_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    boundary_setback_meters: float = PRODUCTION_BOUNDARY_SETBACK_METERS,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
) -> dict:
    """
    STEP 1 of the consolidated production-zone pipeline -- computed ONCE
    per pipeline run and reused by every step after it (see module
    docstring).

    disqualifying_soil_union_utm: a pre-fetched shapely Polygon/MultiPolygon
    (already reprojected into dem['crs']) of disqualifying (hydric) soil, a
    real None (soil WAS checked and genuinely came back clean), or the
    default sentinel meaning "not checked at all" (soil_data_available will
    be False and every cell that clears the slope gate is treated as
    eligible, same as if hydric soil didn't exist -- graceful degradation,
    same convention as every other optional network layer in this
    pipeline).

    tree_root_zone_mask_utm: a pre-fetched boolean np.ndarray the same
    shape as dem['array'] (canopy_height_data.tree_root_zone_mask()'s own
    output -- already dilated, already cell-aligned to this exact DEM
    grid) marking woody-vegetation root-zone cells, or the default
    sentinel meaning "not checked at all" (canopy_data_available will be
    False and every cell that clears the other gates is treated as
    eligible, same graceful degradation as the soil case above). Unlike
    disqualifying_soil_union_utm, there is no real-None state here -- a
    boundary with NO USGS 3DEP lidar HAG coverage can't be distinguished
    from "genuinely no trees" by an all-False mask, so callers collapse
    that outcome to the sentinel too (see production_area.
    _fetch_tree_root_zone_mask_utm()).

    boundary_setback_meters: shrinks (via a plain negative buffer) the
    polygon used ONLY for this function's own on-parcel cell-center test
    below -- boundary_polygon_utm itself is untouched and still what
    cluster_and_gate()'s footprint clip and any caller's own parcel-
    acreage math use as the real, full parcel boundary.

    road_exclusion_union_utm: a pre-fetched shapely Polygon/MultiPolygon
    (already reprojected into dem['crs']) of existing road right-of-way
    (farm_roads_data.get_road_exclusion_union_utm()'s own output), a real
    None (roads WERE checked and genuinely none found nearby -- same
    real-None convention disqualifying_soil_union_utm above uses, unlike
    the canopy case), or the default sentinel meaning "not checked at
    all" (road_data_available will be False and every cell that clears
    the other gates is treated as eligible, same graceful degradation as
    the soil case above).

    Returns:
        {
            'eligible_mask': np.ndarray[bool],       # slope-, soil-, canopy-, road-eligible,
                                                       # on-parcel (post-setback)
            'slope_only_mask': np.ndarray[bool],     # slope-eligible + on-parcel (post-setback),
                                                       # BEFORE the hydric/canopy/road gates -- this
                                                       # is the connectivity source used for
                                                       # source_patch_id/soil_carved
                                                       # bookkeeping, not a second scoring pass
            'slope_source_labels': np.ndarray[int],  # 8-connected-component labels of
                                                       # slope_only_mask (-1 outside it)
            'slope_pct': np.ndarray[float],
            'aspect_deg': np.ndarray[float],
            'per_cell_slope_factor': np.ndarray[float],   # NaN outside eligible_mask
            'per_cell_aspect_factor': np.ndarray[float],  # NaN outside eligible_mask
            'per_cell_score': np.ndarray[float],          # combined; NaN outside eligible_mask;
                                                            # STEP 2's worst-first trim ordering
            'soil_carved_acres_by_cell': np.ndarray[float],  # NaN outside slope_only_mask;
                                                               # same value replicated across
                                                               # every cell of one contiguous
                                                               # slope-only source region --
                                                               # bookkeeping only, not per-cell
            'soil_carved_pct_by_cell': np.ndarray[float],
            'soil_data_available': bool,     # whether the hydric check actually ran
            'canopy_data_available': bool,   # whether the woody-vegetation check actually ran
            'tree_root_zone_hit': np.ndarray[bool],  # cells excluded by the canopy gate specifically
            'road_data_available': bool,     # whether the existing-road check actually ran
            'road_hit': np.ndarray[bool],    # cells excluded by the existing-road gate specifically
        }
    """
    array = dem["array"]
    resolution = dem["resolution_meters"]
    rows, cols = array.shape

    slope_pct = compute_slope_percent(array, resolution)
    _, aspect_deg = compute_slope_and_aspect(array, resolution)

    slope_ok = (~np.isnan(slope_pct)) & (slope_pct <= max_slope_pct)

    on_parcel_boundary_utm = (
        boundary_polygon_utm.buffer(-boundary_setback_meters)
        if boundary_setback_meters > 0
        else boundary_polygon_utm
    )
    boundary_prepared = prep(on_parcel_boundary_utm)
    slope_only_mask = np.zeros((rows, cols), dtype=bool)
    for r, c in np.argwhere(slope_ok):
        r, c = int(r), int(c)
        if boundary_prepared.contains(Point(pixel_center_xy(dem, r, c))):
            slope_only_mask[r, c] = True

    soil_data_available = disqualifying_soil_union_utm is not _SOIL_CHECK_UNCHECKED
    soil_union = disqualifying_soil_union_utm if soil_data_available else None

    hydric_hit = np.zeros((rows, cols), dtype=bool)
    if soil_union is not None:
        soil_prepared = prep(soil_union)
        for r, c in np.argwhere(slope_only_mask):
            r, c = int(r), int(c)
            if soil_prepared.contains(Point(pixel_center_xy(dem, r, c))):
                hydric_hit[r, c] = True

    canopy_data_available = tree_root_zone_mask_utm is not _CANOPY_CHECK_UNCHECKED
    tree_root_zone_hit = np.zeros((rows, cols), dtype=bool)
    if canopy_data_available:
        tree_root_zone_hit = slope_only_mask & tree_root_zone_mask_utm

    road_data_available = road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED
    road_union = road_exclusion_union_utm if road_data_available else None

    road_hit = np.zeros((rows, cols), dtype=bool)
    if road_union is not None:
        road_prepared = prep(road_union)
        for r, c in np.argwhere(slope_only_mask):
            r, c = int(r), int(c)
            if road_prepared.contains(Point(pixel_center_xy(dem, r, c))):
                road_hit[r, c] = True

    eligible_mask = slope_only_mask & (~hydric_hit) & (~tree_root_zone_hit) & (~road_hit)

    per_cell_slope_factor = np.full((rows, cols), np.nan, dtype=np.float32)
    per_cell_aspect_factor = np.full((rows, cols), np.nan, dtype=np.float32)
    per_cell_composite = np.full((rows, cols), np.nan, dtype=np.float32)
    for r, c in np.argwhere(eligible_mask):
        r, c = int(r), int(c)
        sf = _slope_factor([float(slope_pct[r, c])], max_slope_pct)
        af = aspect_score(float(aspect_deg[r, c]))
        per_cell_slope_factor[r, c] = sf
        per_cell_aspect_factor[r, c] = af
        per_cell_composite[r, c] = PER_CELL_SLOPE_WEIGHT * sf + PER_CELL_ASPECT_WEIGHT * af

    slope_source_labels, num_slope_sources = connected_components(slope_only_mask)

    area_per_cell = cell_area_acres(dem)
    soil_carved_acres_by_cell = np.full((rows, cols), np.nan, dtype=np.float32)
    soil_carved_pct_by_cell = np.full((rows, cols), np.nan, dtype=np.float32)
    for label in range(num_slope_sources):
        label_mask = slope_source_labels == label
        total_cells = int(label_mask.sum())
        if total_cells == 0:
            continue
        eligible_cells_in_label = int((label_mask & eligible_mask).sum())
        carved_cells = total_cells - eligible_cells_in_label
        carved_acres = round(carved_cells * area_per_cell, 2)
        total_acres = total_cells * area_per_cell
        carved_pct = round(carved_acres / total_acres * 100, 1) if total_acres > 0 else 0.0
        soil_carved_acres_by_cell[label_mask] = carved_acres
        soil_carved_pct_by_cell[label_mask] = carved_pct

    return {
        "eligible_mask": eligible_mask,
        "slope_only_mask": slope_only_mask,
        "slope_source_labels": slope_source_labels,
        "slope_pct": slope_pct,
        "aspect_deg": aspect_deg,
        "per_cell_slope_factor": per_cell_slope_factor,
        "per_cell_aspect_factor": per_cell_aspect_factor,
        "per_cell_score": per_cell_composite,
        "soil_carved_acres_by_cell": soil_carved_acres_by_cell,
        "soil_carved_pct_by_cell": soil_carved_pct_by_cell,
        "soil_data_available": soil_data_available,
        "canopy_data_available": canopy_data_available,
        "tree_root_zone_hit": tree_root_zone_hit,
        "road_data_available": road_data_available,
        "road_hit": road_hit,
    }


def _waist_erosion_radius_cells(dem: dict, min_waist_meters: float) -> int:
    """
    Converts MIN_ZONE_WAIST_METERS (a real-world distance) into a cell-
    count erosion radius using the DEM's own resolution_meters -- same
    meters-to-cell-units conversion pattern this pipeline's other
    ground-distance thresholds already use, just applied to a radius
    instead of an area. Eroding a mask by radius r cells strips away
    anything narrower than roughly (2r) cells wide, so the radius is half
    the minimum waist width, rounded UP (via ceil) so any real waist
    genuinely narrower than min_waist_meters is reliably eroded away
    rather than surviving due to a too-small radius. Always at least 1
    cell, so a nonzero min_waist_meters always does *something*.
    """
    px, py = dem["resolution_meters"]
    cell_size = (px + py) / 2.0
    return max(1, math.ceil(min_waist_meters / cell_size / 2.0))


def _fill_smoothing_radius_cells(dem: dict, radius_meters: float) -> int:
    """
    Converts FILL_SMOOTHING_RADIUS_METERS (a real-world distance) into a
    cell-count structuring-element radius, same meters-to-cell-units
    conversion _waist_erosion_radius_cells() above uses -- but WITHOUT
    that function's halving: this radius IS the closing operation's own
    structuring-element radius directly (not half a minimum width), so a
    pocket up to roughly TWICE radius_meters wide gets fully closed over
    by _close_cell_mask() below. Always at least 1 cell.
    """
    px, py = dem["resolution_meters"]
    cell_size = (px + py) / 2.0
    return max(1, math.ceil(radius_meters / cell_size))


def _close_cell_mask(
    cells: list[tuple[int, int]],
    grid_shape: tuple[int, int],
    dem: dict,
    radius_meters: float,
) -> np.ndarray:
    """
    Morphological CLOSING (dilate then erode, via raster_grid.py's own
    D8/Chebyshev binary_dilate()/binary_erode() -- the exact same
    structuring element _attempt_waist_split()'s own erosion uses, just
    run dilate-first instead of erode-first) of the boolean mask built
    from `cells`. Closing is the standard technique for "fill small
    interior pockets/gaps without meaningfully altering the mask's own
    outer boundary": dilating first fuses together anything narrower than
    roughly 2x radius_meters (a real pocket of excluded ground, or a
    narrow neck of it), and the matching erode afterward shrinks the
    result back toward the original boundary everywhere EXCEPT where two
    dilated fronts fused across a gap -- there, erosion can't re-open
    what dilation already sealed. A gap wider than roughly 2x
    radius_meters never fuses in the first place, so it survives fully
    intact -- this is exactly what keeps a real waist-split gap (see
    render_polygon_utm/FILL_SMOOTHING_RADIUS_METERS's own docstring)
    open as long as the radius stays under half that gap's real width.

    PADDED by radius_cells on every side before dilating, then cropped
    back to grid_shape afterward -- both binary_dilate()/binary_erode()
    treat anything outside their own array bounds as background, so
    without this padding, a mask sitting close to grid_shape's own edge
    (e.g. a cluster whose real cells extend near the DEM's own grid
    boundary, not just near ITS OWN mask boundary) would have its
    dilation step artificially clipped there -- the following erode step
    would then shrink that edge back further than the original boundary,
    a real border artifact, not a genuine gap being closed. Padding gives
    dilation the same room to grow on every side that an unbounded
    closing would have, so the result nets back to the exact original
    boundary along any genuine outer edge, same as closing's own
    standard mathematical guarantee (confirmed against a mask deliberately
    placed within radius_cells of grid_shape's own edge, not assumed).

    Returns the closed boolean mask (same grid_shape as `cells` came
    from) -- callers build the real footprint via _cell_union_footprint()
    on its True cells, same "not a hull" convention as every other
    footprint in this pipeline.
    """
    rows, cols = grid_shape
    radius_cells = _fill_smoothing_radius_cells(dem, radius_meters)

    padded_rows, padded_cols = rows + 2 * radius_cells, cols + 2 * radius_cells
    padded_mask = np.zeros((padded_rows, padded_cols), dtype=bool)
    for r, c in cells:
        padded_mask[r + radius_cells, c + radius_cells] = True

    dilated = binary_dilate(padded_mask, radius_cells)
    closed_padded = binary_erode(dilated, radius_cells)
    return closed_padded[radius_cells : radius_cells + rows, radius_cells : radius_cells + cols]


def _reclaim_stripped_cells(
    cluster_cells: set[tuple[int, int]],
    seed_labels: dict[tuple[int, int], int],
) -> dict[tuple[int, int], int]:
    """
    Part 1, step 2a: recovers the cells erosion stripped away -- erosion
    only exists to DECIDE whether a cluster splits, never to permanently
    remove real, eligible ground. Multi-source 8-connected BFS, confined
    to `cluster_cells` (the ORIGINAL, pre-erosion cluster footprint):
    every stripped cell (a cell in cluster_cells not already in
    seed_labels) is assigned to whichever eroded sub-component's frontier
    reaches it first, expanding one ring at a time from every surviving
    sub-component simultaneously. BFS ring distance under 8-connected
    (D8_OFFSETS) adjacency is exactly Chebyshev pixel distance -- the same
    adjacency connected_components() already uses -- so this is a simple
    per-cell nearest-surviving-component assignment by pixel distance,
    staying entirely in cell-space: not a hull, not a buffer.
    """
    assignment = dict(seed_labels)
    queue = deque(seed_labels.items())
    while queue:
        (r, c), label = queue.popleft()
        for dr, dc in D8_OFFSETS:
            neighbor = (r + dr, c + dc)
            if neighbor in cluster_cells and neighbor not in assignment:
                assignment[neighbor] = label
                queue.append((neighbor, label))
    return assignment


def _attempt_waist_split(
    cells: list[tuple[int, int]],
    grid_shape: tuple[int, int],
    dem: dict,
    min_area_acres: float,
    min_waist_meters: float = MIN_ZONE_WAIST_METERS,
) -> list[dict]:
    """
    Part 1: waist detection and splitting for ONE cluster's own cell mask
    -- a raster morphological operation on the cell mask itself (via
    raster_grid.binary_erode()), NOT a continuous-geometry operation on
    any polygon. See cluster_and_gate()'s own docstring for how this
    fits into STEP 3.

    Erodes the cluster by MIN_ZONE_WAIST_METERS (converted to a cell
    radius via _waist_erosion_radius_cells()) and re-labels the result. If
    erosion doesn't produce 2+ components, there's no real waist here --
    returns [{"cells": cells, "render_cells": cells}] completely unchanged
    (this function is idempotent and side-effect-free for clusters with no
    real waist, e.g. a normal, roughly-convex field).

    If erosion DOES produce 2+ components, reclaims every stripped cell
    back onto its nearest surviving sub-component
    (_reclaim_stripped_cells()) and checks each reclaimed sub-cluster's
    own REAL cell-union footprint (_cell_union_footprint(), not a cell
    count) against min_area_acres. The split is committed -- returning one
    dict per sub-cluster, each with "cells" (the full, POST-reclaim cell
    set -- every stripped cell reassigned back to its nearest surviving
    piece, used for everything reported: area_acres, polygon_utm,
    geometry_wgs84, suitability scoring) and "render_cells" (the narrower
    PRE-reclaim cell set -- exactly the cells that survived erosion and
    landed on this sub-component, before any stripped cell was reassigned
    anywhere; used ONLY to build render_polygon_utm for display, see
    cluster_and_gate()) -- only if EVERY sub-cluster clears min_area_acres
    on its own POST-reclaim footprint; otherwise this returns
    [{"cells": cells, "render_cells": cells}] unchanged (step 2c: a
    technically-2+-component erosion result that can't actually support
    2+ real zones isn't a split).
    """
    if len(cells) <= 1:
        return [{"cells": cells, "render_cells": cells}]

    rows, cols = grid_shape
    cell_mask = np.zeros((rows, cols), dtype=bool)
    for r, c in cells:
        cell_mask[r, c] = True

    radius_cells = _waist_erosion_radius_cells(dem, min_waist_meters)
    eroded_mask = binary_erode(cell_mask, radius_cells)

    eroded_labels, num_eroded = connected_components(eroded_mask)
    if num_eroded < 2:
        return [{"cells": cells, "render_cells": cells}]

    cluster_cells = set(cells)
    seed_labels = {
        (int(r), int(c)): int(eroded_labels[r, c]) for r, c in np.argwhere(eroded_mask)
    }
    assignment = _reclaim_stripped_cells(cluster_cells, seed_labels)

    sub_groups: dict[int, list[tuple[int, int]]] = {}
    for cell, label in assignment.items():
        sub_groups.setdefault(label, []).append(cell)

    if len(sub_groups) < 2:
        return [{"cells": cells, "render_cells": cells}]

    for group_cells in sub_groups.values():
        footprint = _cell_union_footprint(group_cells, dem)
        area_acres = footprint.area / SQUARE_METERS_PER_ACRE
        if area_acres < min_area_acres:
            return [{"cells": cells, "render_cells": cells}]

    pre_reclaim_groups: dict[int, list[tuple[int, int]]] = {}
    for cell, label in seed_labels.items():
        pre_reclaim_groups.setdefault(label, []).append(cell)

    return [
        {"cells": group_cells, "render_cells": pre_reclaim_groups[label]}
        for label, group_cells in sub_groups.items()
    ]


def _detect_hole_footprints(cells: list[tuple[int, int]], dem: dict) -> list[Polygon]:
    """
    Part 2: true-hole detection for one FINAL cluster (post-waist-
    splitting) -- run independently of Part 1's erosion (a cluster can
    have neither, either, or both a waist AND a separate hole). A true
    hole is excluded (ineligible) ground fully enclosed by this cluster's
    own eligible cells, with NO path to the cluster's outer boundary --
    different from a waist, which has an opening to the outside on both
    sides of the pinch.

    Standard flood-fill-from-the-border approach: builds a local sub-grid
    covering just this cluster's own bounding box, then floods the
    BACKGROUND (non-cluster cells) starting from the sub-grid's own outer
    edges. Any background cell the flood never reaches has no path to the
    outside -- a real, enclosed hole. Uses 4-connected flood-fill for the
    background against this cluster's 8-connected foreground (the
    standard foreground/background connectivity pairing that keeps "is
    this enclosed" well-defined instead of ambiguous at diagonal
    touches).

    Returns one Polygon per enclosed hole component (its own real
    cell-union footprint via _cell_union_footprint()) -- [] if the
    cluster has no true hole.
    """
    if not cells:
        return []

    rows = [r for r, _ in cells]
    cols = [c for _, c in cells]
    r0, r1 = min(rows), max(rows)
    c0, c1 = min(cols), max(cols)
    height, width = r1 - r0 + 1, c1 - c0 + 1

    cluster_mask = np.zeros((height, width), dtype=bool)
    for r, c in cells:
        cluster_mask[r - r0, c - c0] = True
    background = ~cluster_mask

    reached = np.zeros((height, width), dtype=bool)
    queue: deque = deque()

    def _seed(r: int, c: int) -> None:
        if background[r, c] and not reached[r, c]:
            reached[r, c] = True
            queue.append((r, c))

    for r in range(height):
        _seed(r, 0)
        _seed(r, width - 1)
    for c in range(width):
        _seed(0, c)
        _seed(height - 1, c)

    four_offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
    while queue:
        r, c = queue.popleft()
        for dr, dc in four_offsets:
            nr, nc = r + dr, c + dc
            if 0 <= nr < height and 0 <= nc < width and background[nr, nc] and not reached[nr, nc]:
                reached[nr, nc] = True
                queue.append((nr, nc))

    enclosed_mask = background & ~reached
    if not enclosed_mask.any():
        return []

    hole_labels, num_holes = connected_components(enclosed_mask)
    footprints = []
    for label in range(num_holes):
        hole_cells = [(int(r) + r0, int(c) + c0) for r, c in np.argwhere(hole_labels == label)]
        footprints.append(_cell_union_footprint(hole_cells, dem))
    return footprints


def cluster_and_gate(
    cell_mask: np.ndarray,
    dem: dict,
    boundary_polygon_utm: Polygon,
    step1: dict,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
) -> list[dict]:
    """
    STEP 3 of the consolidated production-zone pipeline: 8-connected-
    component labeling of `cell_mask` (STEP 1's raw eligible_mask here with
    no trim, or production_area_ceiling.py's post-STEP-2-trim survivor
    mask), each cluster's REAL cell-union footprint
    (_cell_union_footprint()), and a pure area survival gate -- a cluster
    survives if and only if its real footprint clears min_area_acres.
    Nothing else gates survival here: soil was already excluded at STEP 1,
    so there is nothing left to carve or reject at this stage.

    Two sub-steps run on top of the base connected-component labeling,
    each cluster at a time:

      PART 1 -- waist detection/splitting (_attempt_waist_split()): a
        single 8-connected cluster can contain a narrow "waist" -- real,
        physically connected eligible ground that pinches down to
        something narrower than MIN_ZONE_WAIST_METERS (e.g. two decent-
        sized fields joined by a thin strip) and reads as two zones, not
        one. Detected via raster erosion of the cluster's own cell mask
        (NOT continuous-geometry polygon differencing), and committed only
        if every resulting sub-cluster still clears min_area_acres on its
        own real, reclaimed footprint. This is DIFFERENT from a true hole
        (Part 2 below): a waist has an opening to the outside on both
        sides of the pinch, so it can be split into two standalone zones;
        a hole doesn't connect to the outside at all, so there's nothing
        to split -- it's one solid lobe with a gap.

        Erosion's reclaim step (_reclaim_stripped_cells()) reassigns every
        stripped cell back onto whichever resulting piece is nearest, so
        no real acreage is lost -- but it also means two split zones can
        end up directly adjacent, sharing cells at the pinch with ZERO
        real gap between their polygon_utm footprints. 'render_polygon_utm'
        exists to make the split visually legible without touching the
        reported geometry at all: for each committed split, it's built
        from that sub-cluster's PRE-reclaim cells only (exactly what
        survived erosion, before any stripped cell was reassigned), via
        the same real cell-union approach (_cell_union_footprint()) used
        everywhere else in this pipeline -- not a hull, not a buffer. Every
        reclaimed cell (from BOTH resulting pieces) is excluded from BOTH
        pieces' render_polygon_utm, so render_layout_map.py's contour
        clipping (see that module) shows a real blank strip at the waist.
        For a cluster with no waist split at all, render_polygon_utm is
        simply polygon_utm -- no change in rendering for the ordinary
        case. polygon_utm/geometry_wgs84/area_acres and every other
        downstream consumer (zones_geojson, suitability scoring) continue
        to reflect the full, POST-reclaim footprint, completely unaffected
        by render_polygon_utm.

        'render_fill_polygon_utm' is a SECOND, separate render-only field,
        built from render_polygon_utm's own cells (render_cells) closed
        over via _close_cell_mask() at FILL_SMOOTHING_RADIUS_METERS (see
        that constant's own docstring) -- genuinely excluded (steep or
        hydric, NOT road/tree -- those were already excluded at STEP 1)
        ground can sit as a small, scattered pocket entirely inside an
        otherwise-solid zone, rendering as an unexplained blank gap in
        that zone's own contour-line texture. Closing fills a pocket up
        to roughly 2x FILL_SMOOTHING_RADIUS_METERS wide while leaving a
        real waist-split gap (always wider than that, by construction --
        see FILL_SMOOTHING_RADIUS_METERS's own docstring for the hard
        constraint) genuinely open. render_layout_map.py clips contour
        lines against render_fill_polygon_utm, NOT render_polygon_utm --
        see that module's own docstring. Computed for EVERY cluster, split
        or not (a small excluded pocket can sit inside an ordinary,
        non-split zone just as easily) -- never equal to render_polygon_utm
        by construction (closing always grows-then-shrinks, even when it
        nets back to the identical boundary), but geometrically identical
        whenever there's nothing to close over.

      PART 2 -- true-hole detection (_detect_hole_footprints()): runs
        independently of Part 1, once per FINAL cluster (i.e. after any
        Part-1 split). A hole is excluded ground fully enclosed by that
        cluster's own eligible cells. Stored as 'hole_footprints' -- does
        NOT alter polygon_utm/area_acres (the real cell-union footprint
        already correctly excludes hole cells) -- purely narrative
        metadata (e.g. "a wet spot mid-field" worth calling out in report
        text); it feeds no geometry downstream (render_fill_polygon_utm
        above already independently smooths over small enclosed pockets
        for DISPLAY, but that's a separate concern from this narrative
        field, which still reports every real enclosed hole regardless of
        whether it got closed over for rendering).

    `step1` is compute_step1_eligible_cells()'s own return dict -- used
    here only to attach each surviving cluster's 'source_patch_id' (the
    connected-component label of STEP 1's pre-hydric slope_only_mask that
    the cluster's cells trace back to), which production_suitability.py's
    STEP 4 uses to look up that source region's soil_carved_acres/pct
    bookkeeping. A caller with no meaningful step1 in scope can still pass
    one built with disqualifying_soil_union_utm=None.

    Returns the same shape identify_production_areas() itself returns,
    PLUS 'render_polygon_utm'/'render_fill_polygon_utm' (see PART 1
    above), 'cells' (this cluster's own constituent DEM cells),
    'hole_footprints' (list[Polygon], [] if none), and 'source_patch_id' --
    consumed directly by production_suitability.py's
    score_production_areas(), so STEP 4 never has to recompute/recover
    cluster membership from a mask a second time. 'id' is assigned
    sequentially across the FINAL patch list (after any Part-1 splitting),
    not the pre-split connected-component label, so a waist split's two
    resulting sub-clusters each get their own distinct id.

    polygon_utm/geometry_wgs84 CAN legitimately come back as a
    MultiPolygon: connected_components() is 8-connected (diagonal
    neighbors count as one component), but two cells whose real ground
    squares only touch at a shared corner do not merge into one solid
    Polygon under unary_union -- their true combined footprint really is
    disconnected. feature_schema.py's GeoJSON schema already accepts
    MultiPolygon.
    """
    labels, num_components = connected_components(cell_mask)
    slope_source_labels = step1["slope_source_labels"]

    patches = []
    next_id = 0
    for component_id in range(num_components):
        component_cells = [(int(r), int(c)) for r, c in np.argwhere(labels == component_id)]
        if not component_cells:
            continue

        for split_result in _attempt_waist_split(component_cells, cell_mask.shape, dem, min_area_acres):
            cluster_cells = split_result["cells"]
            render_cells = split_result["render_cells"]
            elevations = [float(dem["array"][r, c]) for r, c in cluster_cells]

            footprint = _cell_union_footprint(cluster_cells, dem)
            polygon_utm = footprint.intersection(boundary_polygon_utm)
            if polygon_utm.is_empty:
                continue

            area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
            if area_acres < min_area_acres:
                continue

            if render_cells is cluster_cells:
                # No waist split for this cluster -- render_polygon_utm
                # must simply equal polygon_utm, same object, no change
                # in rendering behavior for the ordinary, non-split case.
                render_polygon_utm = polygon_utm
            else:
                render_footprint = _cell_union_footprint(render_cells, dem)
                render_polygon_utm = render_footprint.intersection(boundary_polygon_utm)

            # render_fill_polygon_utm: render_polygon_utm's own cells
            # (render_cells -- the waist-aware, PRE-reclaim set), closed
            # over (see FILL_SMOOTHING_RADIUS_METERS/_close_cell_mask()'s
            # own docstrings) to swallow small excluded (steep/hydric)
            # pockets whole, while leaving a real waist-split gap open
            # (it's wider than the closing radius can bridge). Always
            # computed, split or not -- a small excluded pocket can sit
            # inside an ordinary, non-split zone just as easily.
            closed_mask = _close_cell_mask(render_cells, cell_mask.shape, dem, FILL_SMOOTHING_RADIUS_METERS)
            closed_cells = [(int(r), int(c)) for r, c in np.argwhere(closed_mask)]
            render_fill_footprint = _cell_union_footprint(closed_cells, dem)
            render_fill_polygon_utm = render_fill_footprint.intersection(boundary_polygon_utm)

            hole_footprints = _detect_hole_footprints(cluster_cells, dem)

            geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

            first_r, first_c = cluster_cells[0]
            source_patch_id = int(slope_source_labels[first_r, first_c])

            patches.append(
                {
                    "id": next_id,
                    "area_acres": round(float(area_acres), 2),
                    "representative_elevation_m": float(np.median(elevations)),
                    "polygon_utm": polygon_utm,
                    "render_polygon_utm": render_polygon_utm,
                    "render_fill_polygon_utm": render_fill_polygon_utm,
                    "geometry_wgs84": geometry_wgs84,
                    "cells": cluster_cells,
                    "hole_footprints": hole_footprints,
                    "source_patch_id": source_patch_id,
                }
            )
            next_id += 1

    return patches


def identify_production_areas(
    dem: dict,
    boundary_polygon_utm: Polygon,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
    check_soil: bool = True,
    check_roads: bool = True,
) -> list[dict]:
    """
    Returns one entry per candidate production-area patch, clipped to the
    real parcel (see module docstring's PARCEL CLIPPING section):

        {
            'id': int,
            'area_acres': float,
            'representative_elevation_m': float,
            'polygon_utm': shapely Polygon/MultiPolygon,
            'render_polygon_utm': shapely Polygon/MultiPolygon,  # == polygon_utm unless this cluster went
                                                                   # through a waist split -- see cluster_and_gate()
            'render_fill_polygon_utm': shapely Polygon/MultiPolygon,  # render_polygon_utm's own cells,
                                                                   # closed over -- see cluster_and_gate()
            'geometry_wgs84': GeoJSON geometry dict,
            'cells': list[(row, col)],
            'hole_footprints': list[shapely Polygon],  # [] if none -- see module docstring's TRUE HOLES vs WAISTS
            'source_patch_id': int,
        }

    STEP 1 (compute_step1_eligible_cells(), no ceiling trim) + STEP 3
    (cluster_and_gate()) chained together -- see module docstring. When
    check_soil is True (the default), this fetches real disqualifying-soil
    geometry for the real parcel boundary ONCE and feeds it into STEP 1's
    cell-level hydric gate; a fetch failure degrades gracefully to
    slope-only eligibility (same fetch-then-continue pattern every other
    optional network layer in this pipeline already uses), not a crash --
    existing callers (water_candidate_zones.py, road_corridors.py,
    solar_suitability.py) that already call this exact function/signature
    get STEP 1's hydric exclusion automatically, with no changes needed on
    their end.

    The woody-vegetation gate is NOT optional, unlike check_soil above --
    there is no check_canopy flag and no degrade-on-failure path. This
    calls get_required_tree_root_zone_mask_utm() (the SAME shared fetch
    production_area_ceiling.identify_optimized_production_areas() also
    calls, so both entry points into compute_step1_eligible_cells() apply
    this gate identically rather than one silently omitting it), which
    fetches USGS 3DEP lidar HAG coverage for the real parcel boundary ONCE
    and derives the dilated tree-root-zone mask STEP 1's cell-level canopy
    gate needs; a fetch failure (network exhausted after retries, or the
    HAG coverage that DID come back being too sparse to trust --
    canopy_height_data.CanopyCoverageIncompleteError) is deliberately left
    to propagate up UNCAUGHT here, and "no HAG coverage at all for this
    boundary" raises RuntimeError rather than silently proceeding without
    the check. Reasoning: unlike hydric soil (a soft, "reduce confidence
    and continue" concern), a production zone this pipeline can't verify
    is free of tree cover is not a safe thing to hand back with a caveat
    attached -- it's a wrong answer, not a lower-confidence one, so the
    whole pipeline call fails rather than returning candidates that were
    never actually checked for woody vegetation. The fixed boundary-
    setback exclusion (PRODUCTION_BOUNDARY_SETBACK_METERS) always applies
    too -- it's plain polygon geometry on the known parcel boundary, not a
    network-backed layer, so there's nothing to fail on.

    check_roads works the same way check_soil does (graceful degrade, NOT
    the canopy gate's hard-fail behavior): when True (the default), this
    fetches real existing-road geometry for the real parcel boundary ONCE
    (farm_roads_data.get_road_exclusion_union_utm()) and feeds it into
    STEP 1's cell-level road-exclusion gate; a fetch failure degrades
    gracefully to road_data_available=False, not a crash. This is
    deliberately NOT hardened like canopy was: farm_roads_data.py is
    already a known-incomplete signal (public right-of-way only, see that
    module's own docstring), not a newly-closed detection gap, so a fetch
    failure here doesn't change what kind of answer this pipeline is
    already giving -- it's noted via road_data_available/confidence_notes,
    same as the soil check.
    """
    disqualifying_soil_union_utm = _SOIL_CHECK_UNCHECKED
    if check_soil:
        try:
            xs, ys = boundary_polygon_utm.exterior.coords.xy
            lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
            wkt_polygon = coordinates_to_wkt_polygon(list(zip(lons, lats)))
            disqualifying_soil_union_utm = _fetch_disqualifying_soil_union(wkt_polygon, dem)
        except Exception:
            disqualifying_soil_union_utm = _SOIL_CHECK_UNCHECKED

    tree_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(boundary_polygon_utm, dem)

    road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED
    if check_roads:
        try:
            xs, ys = boundary_polygon_utm.exterior.coords.xy
            lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
            boundary_coordinates = list(zip(lons, lats))
            road_exclusion_union_utm = _fetch_road_exclusion_union_utm(boundary_coordinates, dem)
        except Exception:
            road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    step1 = compute_step1_eligible_cells(
        dem,
        boundary_polygon_utm,
        disqualifying_soil_union_utm=disqualifying_soil_union_utm,
        max_slope_pct=max_slope_pct,
        tree_root_zone_mask_utm=tree_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )
    return cluster_and_gate(step1["eligible_mask"], dem, boundary_polygon_utm, step1, min_area_acres)


def production_areas_to_geojson(patches: list[dict]) -> dict:
    """Wraps identify_production_areas() output as a schema-conformant
    GeoJSON FeatureCollection (layer="production_area_candidate") — a
    diagnostic layer, useful for checking this heuristic's output directly
    against a known property, independent of the valley/zone logic built
    on top of it."""
    features = [
        make_feature(
            feature_id=f"production-area-{p['id']}",
            geometry=p["geometry_wgs84"],
            layer="production_area_candidate",
            label=f"Production area candidate {p['id']}",
            confidence=CONFIDENCE_LOW,
            confidence_notes=PRODUCTION_AREA_CONFIDENCE_NOTES,
            extra_properties={
                "area_acres": p["area_acres"],
                "representative_elevation_m": round(p["representative_elevation_m"], 1),
            },
        )
        for p in patches
    ]
    return make_feature_collection(features)


def summarize_production_areas(patches: list[dict]) -> str:
    if not patches:
        return "No production-area candidates identified on this property."

    lines = [f"Production-area candidates found: {len(patches)}"]
    for p in sorted(patches, key=lambda x: -x["area_acres"]):
        lines.append(
            f"  - Patch {p['id']}: {p['area_acres']} acres, "
            f"representative elevation {p['representative_elevation_m']:.1f}m"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline smoke test with a synthetic DEM: a flat low bench (workable
    # production land) bordered by a steep rise. See
    # test_production_area.py for the assertion-based version.
    size = 30
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        for col in range(size):
            if row < 15:
                array[row, col] = 100.0  # flat bench
            else:
                array[row, col] = 100.0 + (row - 14) * 5.0  # steep rise

    synthetic_dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    # A boundary matching the DEM's full extent — this smoke test isn't
    # about parcel clipping, just the slope heuristic itself.
    full_extent_boundary = box(500000.0, 4500000.0 - size * 5.0, 500000.0 + size * 5.0, 4500000.0)

    patches = identify_production_areas(synthetic_dem, full_extent_boundary, check_soil=False)
    print(summarize_production_areas(patches))
