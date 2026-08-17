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

    STEP 3 -- cluster_and_gate(): 4-connected-component labeling
        (raster_grid.connected_components(connectivity=4)) of WHATEVER cell
        mask a caller passes in (STEP 1's raw eligible mask here, or
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
render_layout_map.py draws production zones as clipped contour-line
texture (via contour_lines.py), clipped NOT to geometry_wgs84 but to a
separate display geometry, render_fill_polygon_utm (cluster_and_gate()'s
bounded morphological opening of the cell mask; see PART 1 there). That
field is genuinely consumed downstream, not display-inert: render_layout_
map.py clips against it, fencing.py derives the developed-footprint fence
loops from it, and production_areas_to_geojson() reports its acreage as
render_fill_area_acres. See render_layout_map.py's own docstring for why
filled-shape rendering (and the cosmetic hull-smoothing it used to need)
was replaced by contour texture.

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

import logging
import math
from collections import deque
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from canopy_height_data import TREE_ROOT_ZONE_BUFFER_METERS, get_canopy_height_for_boundary, tree_root_zone_mask
from farm_roads_data import ROAD_EXCLUSION_BUFFER_METERS, get_road_exclusion_union_utm
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_suitability import ASPECT_FACTOR_WEIGHT, SLOPE_FACTOR_WEIGHT
from raster_grid import (
    D8_OFFSETS,
    SQUARE_METERS_PER_ACRE,
    attempt_waist_split,
    binary_dilate,
    binary_erode,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    eroded_cell_mask,
    pixel_center_xy,
    waist_erosion_radius_cells,
)
from soil_data import (
    coordinates_to_wkt_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    hydric_disqualifying_mukeys,
)
from terrain_metrics import aspect_score, compute_slope_and_aspect

_LOGGER = logging.getLogger(__name__)

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

# A single 4-connected cluster can legitimately contain a narrow "waist" --
# real, physically connected eligible ground that pinches down to
# something too narrow to sensibly treat as one zone (e.g. two decent-
# sized fields joined by a thin strip). Narrower than this, and it reads
# as two separate zones rather than one -- see cluster_and_gate()'s own
# docstring for the erosion-based detection this feeds. CONFIGURABLE.
#
# RADII TUNING CAVEAT (applies to this constant, RENDER_OPENING_RADIUS_METERS,
# and RENDER_LEAD_ERODE_CELLS below): these radii were tuned against two
# boundaries of a single ~16-acre reference property. The working window is
# narrow -- 24 m was the only swept value at which BOTH boundaries produced two
# zones, with 18 m under-detecting and 30 m over-splitting on both. A fixed
# metre value may not hold across parcel sizes: a 3-acre parcel and a 30-acre
# parcel plausibly want different thresholds, since both the neck width that
# reads as "unfarmable through" and the fringe width worth trimming scale with
# the size of the fields involved. If these turn out not to generalise, the fix
# is to DERIVE them from parcel extent rather than to keep re-tuning a fixed
# constant.
MIN_ZONE_WAIST_METERS = 24.0  # split threshold; see RADII TUNING CAVEAT above

# Radius of the morphological OPENING (erode by r + RENDER_LEAD_ERODE_CELLS,
# dilate back by r) used to build render_fill_polygon_utm -- see cluster_and_
# gate()'s docstring. Deliberately a SEPARATE constant from MIN_ZONE_WAIST_
# METERS: that one defines when a neck is narrow enough to count as two
# disconnected zones (the SPLIT decision); this one defines how thin a
# protrusion or neck the DRAWN shape trims/severs (a render-geometry decision).
# Retuning either must not silently move the other. See the RADII TUNING CAVEAT
# on MIN_ZONE_WAIST_METERS above -- it applies here too.
RENDER_OPENING_RADIUS_METERS = 12.0  # ~40 ft

# Extra erosion applied ahead of the render opening, in CELLS (not metres) --
# this is a fringe-trim, and one cell is one cell regardless of grid
# resolution. Folded into eroded_cell_mask()'s radius so it composes into a
# single erosion: erode(r + LEAD) -> dilate(r), an asymmetric opening that
# severs features narrower than 2*(r + LEAD) while restoring only r. Separate,
# independently retunable constant (see the RADII TUNING CAVEAT above).
RENDER_LEAD_ERODE_CELLS = 1

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


def _fetch_road_exclusion_union_utm(
    boundary_coordinates: list, dem: dict, buffer_meters: float = ROAD_EXCLUSION_BUFFER_METERS
):
    """
    Thin wrapper around farm_roads_data.get_road_exclusion_union_utm() --
    exists so every entry point that needs a road-exclusion union
    (identify_production_areas() and production_area_ceiling.
    identify_optimized_production_areas() in this pipeline;
    water_candidate_zones.py's own water-zone road exclusion, at its own
    WATER_ZONE_ROAD_BUFFER_METERS) calls the SAME function (imported from
    here, not straight from farm_roads_data.py independently in each
    caller), the same "shared helper, one definition" reasoning _fetch_
    disqualifying_soil_union() above already established for the
    hydric-soil fetch -- among other things, this means a single test
    mock of get_road_exclusion_union_utm here covers every entry point,
    rather than needing to patch several independent import bindings.

    buffer_meters defaults to ROAD_EXCLUSION_BUFFER_METERS (production's
    own buffer) but is a real, independent parameter -- a caller with its
    own separately-tunable buffer constant (e.g.
    water_candidate_zones.WATER_ZONE_ROAD_BUFFER_METERS) passes it
    explicitly rather than this module's value silently applying instead.

    Returns None if no roads were found nearby -- the common, clean case,
    same "checked and genuinely nothing there" convention _fetch_
    disqualifying_soil_union() uses. Any fetch failure is left to
    propagate up uncaught; callers degrade gracefully on their own (see
    identify_production_areas()'s own check_roads handling).
    """
    return get_road_exclusion_union_utm(boundary_coordinates, dem, buffer_meters=buffer_meters)


def _fetch_tree_root_zone_mask_utm(
    boundary_coordinates: list,
    dem: dict,
    buffer_meters: float = TREE_ROOT_ZONE_BUFFER_METERS,
    canopy_height: Optional[dict] = None,
):
    """
    Fetches USGS 3DEP lidar HAG coverage for this boundary and returns the
    dilated tree-root-zone cell mask (canopy_height_data.tree_root_zone_
    mask()'s own output -- already on dem's own grid, so it can be used
    directly against compute_step1_eligible_cells()'s cell indices with no
    further alignment work).

    buffer_meters defaults to TREE_ROOT_ZONE_BUFFER_METERS (production's
    own buffer) but is threaded through to tree_root_zone_mask() as a
    real, independent parameter -- a caller with its own separately-
    tunable buffer constant (e.g. water_candidate_zones.
    WATER_ZONE_CANOPY_BUFFER_METERS) passes it explicitly rather than
    this module's value silently applying instead.

    canopy_height is an optional pre-fetched override: the SAME dict
    canopy_height_data.get_canopy_height_for_boundary() returns (already
    reprojected onto dem's own grid -- e.g. parcel_data.ParcelData.
    canopy_height). When supplied, it is used verbatim and NO network
    fetch happens; when None (the default), this fetches canopy for
    boundary_coordinates itself, so every existing caller keeps its
    current fetch-it-myself behavior unchanged. Either way the downstream
    None-vs-mask logic below is identical -- this parameter only changes
    where the canopy dict comes from, never what is done with it.

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
    canopy = canopy_height
    if canopy is None:
        canopy = get_canopy_height_for_boundary(boundary_coordinates, dem)
    if canopy is None:
        return None
    return tree_root_zone_mask(canopy["array"], canopy["resolution_meters"], buffer_meters=buffer_meters)


def get_required_tree_root_zone_mask_utm(
    boundary_polygon_utm: Polygon,
    dem: dict,
    buffer_meters: float = TREE_ROOT_ZONE_BUFFER_METERS,
    canopy_height: Optional[dict] = None,
):
    """
    Fetches a REQUIRED (non-optional) tree-root-zone mask for
    boundary_polygon_utm -- the shared "fetch canopy, or fail hard"
    building block behind the woody-vegetation gate, so every entry point
    that ultimately needs one applies it identically instead of each
    reimplementing (or, worse, quietly omitting) its own copy.
    identify_production_areas() (this module), production_area_ceiling.
    identify_optimized_production_areas(), and water_candidate_zones.py's
    own mandatory canopy exclusion (at its own WATER_ZONE_CANOPY_BUFFER_
    METERS) all call this directly, rather than any of them duplicating
    the boundary-reprojection + fetch + raise sequence itself.

    buffer_meters defaults to TREE_ROOT_ZONE_BUFFER_METERS (production's
    own buffer) and is threaded straight through to _fetch_tree_root_
    zone_mask_utm() -- see that function's own docstring for why this
    stays a real, independent parameter rather than a shared constant.

    canopy_height is an optional pre-fetched override, forwarded verbatim
    to _fetch_tree_root_zone_mask_utm(): the SAME dict canopy_height_data.
    get_canopy_height_for_boundary() returns (e.g. parcel_data.ParcelData.
    canopy_height). When supplied it is used as-is and no canopy network
    fetch happens; when None (the default) canopy is fetched for this
    boundary as before, so every existing caller is unchanged. Note the
    boundary reprojection below still runs regardless -- it is only used
    as get_canopy_height_for_boundary()'s input when a fetch actually
    happens, and is harmlessly ignored when the override short-circuits it.

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
    tree_root_zone_mask_utm = _fetch_tree_root_zone_mask_utm(
        boundary_coordinates, dem, buffer_meters=buffer_meters, canopy_height=canopy_height
    )
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

    Thin, list-of-cells wrapper around raster_grid.cell_union_footprint()
    (the same corner-computation logic, generalized to take a boolean
    mask so water_candidate_zones.py's own cell clusters can reuse it too)
    -- every call site here already has a `cells` list rather than a mask,
    so this just builds a mask sized to bound `cells` and delegates.
    Sized off `cells` itself (not dem['array'].shape) since this function
    only ever needs a mask large enough to hold the given cells --
    cell_union_footprint() derives each square's real coordinates from
    dem's origin/resolution directly, never from the mask's own shape.
    """
    if not cells:
        return Polygon()
    rows = max(r for r, _ in cells) + 1
    cols = max(c for _, c in cells) + 1
    mask = np.zeros((rows, cols), dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return cell_union_footprint(dem, mask)


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

    # DELIBERATELY 8-connected (connected_components()'s default), NOT the
    # 4-connected labeling cluster_and_gate() now uses for production-cluster
    # identity. This labels STEP 1 SOURCE REGIONS for source_patch_id
    # bookkeeping (STEP 4's soil carved-acres lookup) -- a different question
    # from which cells form one production zone. Switching it to 4-connected
    # would change which source region a cluster's cells trace back to, and
    # thus its soil_carved_acres attribution. The two connectivities differing
    # within this file is intentional, not an oversight.
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
    """Thin wrapper around raster_grid.waist_erosion_radius_cells() -- the
    meters-to-cell-radius conversion this module's own waist detection
    used to own directly, extracted to raster_grid.py as a shared utility
    so water_candidate_zones.py's water-zone clustering can reuse the
    exact same conversion (same pattern already used for
    cell_union_footprint() -- see _cell_union_footprint() below). Kept
    here, thin, because test_production_area.py calls this exact name
    directly; zero behavior change from before the extraction."""
    return waist_erosion_radius_cells(dem, min_waist_meters)


def _attempt_waist_split(
    cells: list[tuple[int, int]],
    grid_shape: tuple[int, int],
    dem: dict,
    min_area_acres: float,
    min_waist_meters: float = MIN_ZONE_WAIST_METERS,
) -> list[dict]:
    """Thin wrapper around raster_grid.attempt_waist_split() -- Part 1's
    waist detection/splitting for ONE cluster's own cell mask, extracted
    to raster_grid.py as a shared utility (see cluster_and_gate()'s own
    docstring for how this fits into STEP 3) so water_candidate_zones.py's
    water-zone clustering can reuse the exact same detection/splitting
    logic against its own post-dilation eligibility mask, rather than a
    second, independently-maintained copy. Zero behavior change from
    before the extraction; see raster_grid.attempt_waist_split()'s own
    docstring for the full algorithm, including the "cells"/"render_cells"
    distinction each returned dict carries."""
    return attempt_waist_split(cells, grid_shape, dem, min_area_acres, min_waist_meters)


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
    outside -- a real, enclosed hole. Uses 8-connected flood-fill for the
    background against cluster_and_gate()'s now-4-connected foreground --
    the standard COMPLEMENTARY foreground/background connectivity pairing
    that keeps "is this enclosed" well-defined instead of ambiguous at
    diagonal touches. The background connectivity always tracks the
    OPPOSITE of the foreground's: because cluster_and_gate() now labels
    clusters 4-connected, the background must flood 8-connected. Leaving it
    4-connected against a 4-connected foreground would let a diagonal gap
    read as sealed in BOTH directions -- a topological paradox that
    manufactures false enclosed holes.

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

    # 8-connected background flood (D8_OFFSETS), complementary to
    # cluster_and_gate()'s 4-connected foreground labeling -- see this
    # function's docstring for why the pairing must stay opposite.
    while queue:
        r, c = queue.popleft()
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < height and 0 <= nc < width and background[nr, nc] and not reached[nr, nc]:
                reached[nr, nc] = True
                queue.append((nr, nc))

    enclosed_mask = background & ~reached
    if not enclosed_mask.any():
        return []

    # Enclosed holes are BACKGROUND regions, so they are grouped with the
    # same 8-connectivity the background flood above uses (the default) --
    # NOT the 4-connectivity cluster_and_gate() labels its FOREGROUND
    # clusters with. Keeping this on 8 is what makes two corner-touching
    # enclosed background cells read as one hole, consistent with the flood
    # that found them; this is deliberate, not an oversight.
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
    STEP 3 of the consolidated production-zone pipeline: 4-connected-
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
        single 4-connected cluster can contain a narrow "waist" -- real,
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
        real gap between their polygon_utm footprints. The visual gap at a
        waist is drawn instead from 'render_fill_polygon_utm' (see below),
        whose opening keeps the two pieces on their own sides of the pinch;
        polygon_utm/geometry_wgs84/area_acres and every other downstream
        consumer (zones_geojson, suitability scoring) continue to reflect
        the full, POST-reclaim footprint.

        'render_fill_polygon_utm' is a bounded,
        ASYMMETRIC, DISC morphological OPENING of this cluster's own cell mask,
        clipped to polygon_utm --

            opened = binary_dilate(
                eroded_cell_mask(cluster_cells, r, element="disc",
                                 extra_erode_cells=RENDER_LEAD_ERODE_CELLS),
                r, element="disc",
            )
            render_fill_polygon_utm =
                cell_union_footprint(dem, opened).intersection(polygon_utm)

        where r = RENDER_OPENING_RADIUS_METERS in cells. The opening erodes the
        cluster by r + RENDER_LEAD_ERODE_CELLS (the "lead erode", folded into a
        single erosion) and then dilates the survivors back by only r -- an
        ASYMMETRIC opening that severs features narrower than 2*(r + lead) while
        restoring only r, so narrow features are annihilated rather than shrunk
        to slivers and every edge is inset by the lead. The DISC structuring
        element (vs binary_erode()'s square default) gives Euclidean reach, so
        corners round instead of developing flat 45-degree facets at larger
        radii. An opening is anti-extensive (opened <= original): outer edges
        return close to their original position, thin protrusions are trimmed,
        and a neck severed by the erosion CANNOT be re-joined (dilation only
        regrows the survivors that remained). It does NOT fill notches, pockets,
        or other concavities -- that is a CLOSING (dilate-then-erode), the
        opposite operation, tried and rejected (see below). Clipping to
        polygon_utm makes the result BOUNDED BY CONSTRUCTION:
        render_fill_polygon_utm.area <= polygon_utm.area always (asserted;
        raises on violation). Despite the "render_" name, this is NOT purely
        a cosmetic display-only field -- water_candidate_zones.py's own
        eligibility gate, road_corridors.py's own production hard-exclusion
        mask, tree_zone_candidates.py's claimed-geometry exclusion, and
        solar_suitability.py's production exclusion all reuse this exact
        field (not polygon_utm) so a downstream candidate can't sit inside
        what a reader sees as one coherent production zone on the rendered
        map. Because those modules consume it as EXCLUSION geometry, the
        bounded-above guarantee matters: the fill can never over-claim
        ground the cell gate excluded, which the old convex hull could do
        without limit.

        WHY AN OPENING, NOT THE OLD DILATE/ERODE ROUND-TRIP: an earlier
        attempt at a raster dilate-then-erode (and the equivalent vector
        buffer(+r).buffer(-r)) was tried and rejected. That is a CLOSING --
        the inverse operation with the inverse purpose: it fills wide gaps,
        and was rejected because no radius could close real-world gaps
        without also closing genuine waist-split gaps. This is an OPENING
        (erode THEN dilate), whose purpose is the opposite -- it trims
        narrow protrusions and keeps necks open rather than filling them --
        so that objection does not transfer. The convex hull that replaced
        those closings is itself now removed: a hull has no upper bound on
        how far it can bulge past the shape it wraps, which is unacceptable
        for a field three-plus modules treat as authoritative exclusion
        geometry.

        OPENING/WAIST-SPLIT INTERACTION: the opening operates on the
        survivors of its OWN erosion, so a neck narrower than 2r is severed
        before the dilation and the two lobes' fills stay on their own sides
        of the pinch -- an opening cannot bridge a gap it just cut. A
        waisted cluster therefore yields a MultiPolygon fill routinely (more
        often than the old hull did); every consumer above already tolerates
        MultiPolygon (audited).

        If the cluster is thinner than r throughout, the opening is empty;
        render_fill_polygon_utm then falls back to polygon_utm (a zone with
        no interior is honestly drawn as its own footprint) and the fallback
        is logged once.

        render_layout_map.py clips contour lines against render_fill_
        polygon_utm -- see that module's own docstring. Computed for EVERY
        cluster, split or not -- geometrically
        equal to polygon_utm when the cluster has no notch or protrusion
        narrower than r (e.g. a clean rectangular field, whose edges the
        opening returns almost exactly), and strictly smaller wherever the
        opening trims a narrow feature.

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
    PLUS 'render_fill_polygon_utm' (see PART 1 above), 'cells' (this
    cluster's own constituent DEM cells),
    'hole_footprints' (list[Polygon], [] if none), and 'source_patch_id' --
    consumed directly by production_suitability.py's
    score_production_areas(), so STEP 4 never has to recompute/recover
    cluster membership from a mask a second time. 'id' is assigned
    sequentially across the FINAL patch list (after any Part-1 splitting),
    not the pre-split connected-component label, so a waist split's two
    resulting sub-clusters each get their own distinct id.

    polygon_utm/geometry_wgs84 is normally a single Polygon: labeling here
    is 4-connected (connected_components(cell_mask, connectivity=4)), so
    every cluster is edge-connected and two cells that touch only at a
    shared corner fall into SEPARATE clusters rather than one. Their real
    ground squares meet at a single point and never merge under
    unary_union, so a cluster's footprint is no longer asked to bridge a
    corner touch and comes back as one solid Polygon. A MultiPolygon
    remains schema-valid (feature_schema.py's GeoJSON schema accepts it,
    and boundary clipping against a concave parcel edge can still split a
    footprint into pieces), but it should no longer arise from corner-touch
    labeling the way it did under the old 8-connected labeling.
    """
    labels, num_components = connected_components(cell_mask, connectivity=4)
    slope_source_labels = step1["slope_source_labels"]

    patches = []
    next_id = 0
    for component_id in range(num_components):
        component_cells = [(int(r), int(c)) for r, c in np.argwhere(labels == component_id)]
        if not component_cells:
            continue

        for split_result in _attempt_waist_split(component_cells, cell_mask.shape, dem, min_area_acres):
            cluster_cells = split_result["cells"]
            elevations = [float(dem["array"][r, c]) for r, c in cluster_cells]

            footprint = _cell_union_footprint(cluster_cells, dem)
            polygon_utm = footprint.intersection(boundary_polygon_utm)
            if polygon_utm.is_empty:
                continue

            area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
            if area_acres < min_area_acres:
                continue

            # render_fill_polygon_utm: a bounded morphological OPENING of this
            # cluster's own cell mask, then CLIP to polygon_utm. This is an
            # ASYMMETRIC, DISC opening: erode by RENDER_OPENING_RADIUS_METERS +
            # RENDER_LEAD_ERODE_CELLS, then dilate back by only
            # RENDER_OPENING_RADIUS_METERS. The lead erode composes into the
            # single erosion (see eroded_cell_mask()), so it severs features
            # narrower than 2*(r + lead) while restoring only r -- annihilating
            # narrow features rather than shrinking them to slivers, and leaving
            # every edge inset by the lead. The DISC element (vs the square
            # default) gives Euclidean reach, so corners round instead of
            # developing the flat 45-degree facets a square element leaves at
            # larger radii. An opening is anti-extensive (opened <= original), so
            # it TRIMS thin protrusions and SEVERS narrow necks (a neck severed
            # by the erosion cannot be re-joined -- dilation only regrows the
            # survivors that remained) while returning wide outer edges close to
            # their original position. It does NOT fill notches, pockets, or
            # concavities -- that would require a closing (dilate-then-erode), the
            # opposite operation, tried and rejected. Clipping to polygon_utm
            # makes the result BOUNDED BY CONSTRUCTION -- it can never claim
            # ground the cell gate excluded, a strictly stronger guarantee than
            # the old convex hull (which had no upper bound on how far it could
            # bulge past the real footprint, and which several downstream modules
            # consume as production EXCLUSION geometry). See cluster_and_gate()'s
            # docstring.
            opening_radius_cells = waist_erosion_radius_cells(dem, RENDER_OPENING_RADIUS_METERS)
            opened_mask = binary_dilate(
                eroded_cell_mask(
                    cluster_cells,
                    cell_mask.shape,
                    dem,
                    RENDER_OPENING_RADIUS_METERS,
                    element="disc",
                    extra_erode_cells=RENDER_LEAD_ERODE_CELLS,
                ),
                opening_radius_cells,
                element="disc",
            )
            if opened_mask.any():
                render_fill_polygon_utm = cell_union_footprint(dem, opened_mask).intersection(polygon_utm)
            else:
                # The cluster is thinner than the opening radius throughout, so
                # the opening is empty. The honest fill for a zone with no
                # interior is its own footprint -- fall back to polygon_utm
                # rather than emit empty render geometry.
                render_fill_polygon_utm = polygon_utm
                _LOGGER.warning(
                    "cluster_and_gate: the RENDER_OPENING_RADIUS_METERS=%.1fm opening eroded a "
                    "%d-cell (%.4f ac) cluster to nothing; render_fill_polygon_utm falling back to "
                    "polygon_utm for this cluster.",
                    RENDER_OPENING_RADIUS_METERS,
                    len(cluster_cells),
                    area_acres,
                )

            # Bounded above by the footprint, always -- the branch's core
            # guarantee and the whole reason the unbounded hull was removed.
            # Clipping to polygon_utm makes this hold by construction; assert it
            # and raise on violation (matching fencing.py's hard-containment
            # discipline) so any future regression is caught loudly rather than
            # silently over-claiming exclusion ground.
            if render_fill_polygon_utm.area > polygon_utm.area * (1 + 1e-9) + 1e-6:
                raise ValueError(
                    "cluster_and_gate: render_fill_polygon_utm.area "
                    f"({render_fill_polygon_utm.area:.6f} m^2) exceeds polygon_utm.area "
                    f"({polygon_utm.area:.6f} m^2) -- the opening's clip to polygon_utm must keep "
                    "the drawn fill within the real cell-gated footprint, never claiming excluded ground."
                )

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
    canopy_height: Optional[dict] = None,
) -> list[dict]:
    """
    Returns one entry per candidate production-area patch, clipped to the
    real parcel (see module docstring's PARCEL CLIPPING section):

        {
            'id': int,
            'area_acres': float,
            'representative_elevation_m': float,
            'polygon_utm': shapely Polygon/MultiPolygon,
            'render_fill_polygon_utm': shapely Polygon/MultiPolygon,  # bounded opening of the cluster
                                                                   # mask, clipped to polygon_utm -- see cluster_and_gate()
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

    canopy_height is an optional pre-fetched override forwarded straight to
    get_required_tree_root_zone_mask_utm(): the SAME dict canopy_height_
    data.get_canopy_height_for_boundary() returns (e.g. parcel_data.
    ParcelData.canopy_height). When supplied, the mandatory gate above uses
    it instead of fetching canopy itself -- letting a context-aware caller
    that already holds the parcel's canopy avoid a redundant Planetary
    Computer round-trip; when None (the default) canopy is fetched here as
    before, so this gate's hard-fail semantics are entirely unchanged.

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

    tree_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, canopy_height=canopy_height
    )

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
                # Acreage of the geometry the MAP actually draws (the bounded
                # morphological opening render_layout_map.py clips production
                # contour texture to), NOT the full cell-union footprint
                # area_acres reports. render_fill_polygon_utm is always a
                # subset of polygon_utm, so this is <= area_acres for every
                # patch (see cluster_and_gate()'s containment assertion).
                "render_fill_area_acres": round(
                    p["render_fill_polygon_utm"].area / SQUARE_METERS_PER_ACRE, 2
                ),
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
