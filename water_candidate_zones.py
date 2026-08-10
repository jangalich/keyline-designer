"""
water_candidate_zones.py

Step 3 of water-system candidate-zone identification: a purely cell-based
eligibility mask + real cell-union footprint, mirroring the pattern
production_area.py's own pipeline uses (eligibility mask -> connected
components -> per-cell-square union geometry, not a smoothed buffer or
convex hull) -- see compute_water_eligible_cells()'s docstring.

    DEM (dem_data.py)
        --> raw flow-accumulation grid (valley_delineation.
            get_flow_accumulation_for_dem() -- the same grid
            delineate_valleys() thresholds/traces internally)
        --> production areas (production_area.py)
        --> [this module] per-DEM-cell eligibility mask (contributing-area
            PERCENTILE BAND + service distance + boundary setback +
            canopy root-zone exclusion + existing-road exclusion)
        --> connected components -> waist-split -> cell-union footprint
            per cluster
        --> whole-zone scoring (one representative point per zone) ->
            candidate-zone polygons, one per qualifying cluster

This REPLACES the earlier per-traced-valley-branch line-walk entirely:
there is no valley/branch identity carried into a zone anymore. A zone is
now just "a connected cluster of individually-eligible DEM cells" --
exactly the same "cluster's own connectivity defines it" logic
production_area.py's clusters already use, just applied to a different
per-cell eligibility test. Finding one "best" pond/dam site within that
zone is explicitly out of scope here (see the confidence_notes on the
output feature) -- that's future, separate, more detailed work (storage
volume, dam wall geometry).

Elevation relative to the production area(s) a zone could serve is NOT a
generation-time exclusion here -- it used to be (a hard "must clear
MIN_GRAVITY_GRADIENT" gate), but that discarded genuinely well-suited
water-system ground before scoring ever got to weigh it: a site that's
otherwise excellent but sits below its nearest production area (requiring
a pump) is a real, valid candidate -- a pump is a cost/maintenance
tradeoff, not a disqualification. This module instead computes and
attaches the raw elevation-differential/gradient data for every candidate
zone's relationship to each production area it could plausibly serve
(see production_area_relationships below), and leaves turning that into a
"gravity is preferred" SCORE to water_suitability.py -- the same
gate-to-preference move production_suitability.py already made for soil
(see that module's own docstring) and solar_suitability.py already made
for production-zone proximity.

find_candidate_zones() below is deliberately a pure function over an
already-fetched dem plus already-computed production_areas/boundary -- no
DEM fetch, no network. That split is what makes Stage 2 ("is the
zone-filtering logic correct") testable independently of Stage 1 ("is the
DEM/valley delineation accurate") -- see test_water_candidate_zones.py,
and the module docstrings on dem_data.py/valley_delineation.py/
production_area.py for the same reasoning applied to the layers underneath
this one.

Each zone also carries render_fill_polygon_utm/render_fill_geometry_wgs84
-- a DISPLAY-ONLY convex hull of the real cell-union footprint,
re-intersected with the parcel boundary, same construction and same
reasoning as production_area.py's own render_fill_polygon_utm (see that
module's docstring): a water zone traced along a narrow, winding drainage
band is a genuinely concave, notched shape, and the hull reads as one
coherent blob at render time instead. This NEVER replaces polygon_utm/
geometry_wgs84 for scoring, eligibility, or the narrative report -- those
stay the real, unsmoothed cell-union footprint, untouched. It is
deliberately allowed to overlap a production area's own
render_fill_polygon_utm at render time -- that overlap is a display-only
coincidence, not a real siting conflict (the eligibility-gate production
exclusion above already keeps a water zone's REAL geometry off real
production ground; see WATER_ZONE_PRODUCTION_SETBACK_METERS).
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import (
    MIN_ZONE_WAIST_METERS,
    _fetch_road_exclusion_union_utm,
    compute_slope_percent,
    get_required_tree_root_zone_mask_utm,
    identify_production_areas,
    production_areas_to_geojson,
)
from raster_grid import (
    D8_OFFSETS,
    SQUARE_METERS_PER_ACRE,
    attempt_waist_split,
    binary_dilate,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    pixel_center_xy,
)
from valley_delineation import (
    delineate_valleys,
    get_flow_accumulation_for_dem,
    valleys_to_geojson,
)

# Zones within this distance of the property boundary are excluded even
# if geometrically valid — too close to the property line to realistically
# develop (access, neighbor impact, and likely setback/easement rules this
# pipeline has no data on). CONFIGURABLE.
MIN_BOUNDARY_SETBACK_METERS = 15.0

# How far downhill a candidate cell's elevation advantage is considered
# relevant to a given production-area patch at all. Beyond this, even a
# technically-qualifying gradient isn't a plausible single contour-channel
# run. CONFIGURABLE.
MAX_SERVICE_DISTANCE_METERS = 800.0

# Guards against a candidate cell sitting immediately adjacent to (but
# genuinely OUTSIDE) a production-area patch, where "above by X% grade
# over Y meters" no longer means anything (Y too small to be meaningful).
# Deliberately NOT applied to a cell already INSIDE/touching a patch
# (distance == 0 — see compute_water_eligible_cells()): that guard is
# about rejecting a near-but-separate siting as too close for the
# distance math to mean anything, not about rejecting siting inside the
# production area at all. That distinction is real, not academic — a
# single production-area patch can legitimately cover most of a parcel
# (production_area.py's own slope threshold, confirmed live: ~95% of one
# real reference property), and a strict "distance < 10m is always too
# close" reading would then reject nearly every candidate cell on that
# property outright, since almost everywhere on it genuinely IS inside
# that one patch. Same "gate becomes a genuinely-inapplicable rule at this
# property's real scale, fix it, don't just re-tune the number" pattern as
# road_corridors.py's/production_suitability.py's own earlier softened-
# exclusion fixes documented in README.md. CONFIGURABLE.
MIN_SERVICE_DISTANCE_METERS = 10.0

# Minimum upstream contributing area for a DEM cell to count as sitting on
# a genuine drainage feature at all, replacing the old "is this cell near
# a traced valley branch LINE" test. Mirrors valley_delineation.py's own
# MIN_STREAM_CONTRIBUTING_AREA_ACRES stream threshold (same reasoning:
# concentrated flow, not diffuse sheet flow off a slope) but kept as a
# separate, independently-tunable constant since this module's use case
# (water-system siting) doesn't have to move in lockstep with
# valley_delineation.py's own general-purpose valley threshold.
#
# ROLE CHANGED: this used to be the gate itself (a cell qualified iff its
# own contributing area cleared this bar, full stop). It is now only the
# FLOOR that defines the "drainage-qualifying population" a percentile
# band (VALLEY_ACCUMULATION_PERCENTILE_LOW/HIGH below) is computed over --
# see compute_water_eligible_cells()'s own docstring for why a single hard
# threshold was replaced: it admitted every cell from the parcel's single
# dominant master channel/outlet down to the bar equally, with no way to
# distinguish "the real drainage band" from "technically-qualifying but
# marginal ground barely above the floor." A single, low floor here casts
# a wide net for the population; the percentile band is what actually
# decides eligibility now.
#
# Since the role changed from gate to floor, the real-property-tuned
# 3.0-acre value from the old single-threshold gate no longer applies --
# a floor should be LOW (cast a wide net for the population), not itself
# a selective threshold. 0.5 acres is a low, deliberately unvalidated
# STARTING value for this new role. CONFIGURABLE — re-tune with
# diagnose_water_zone_mask.py against your own property.
MIN_VALLEY_CONTRIBUTING_AREA_ACRES = 0.5

# The percentile band (numpy percentile, 0-100 scale) applied to the
# drainage-qualifying population's own flow_accumulation_cells values --
# see compute_water_eligible_cells()'s own docstring for the two-step
# selection (population, then band) this replaces the old single
# MIN_VALLEY_CONTRIBUTING_AREA_ACRES hard threshold with. A cell qualifies
# for this gate iff its own value falls within
# [LOW percentile, HIGH percentile] of the population's distribution --
# excluding both the diffuse, barely-above-the-floor tail (bottom of the
# distribution) and the single dominant master channel/outlet (top of the
# distribution, concentrated enough to read as one drainage LINE rather
# than a genuine candidate BAND). NOT YET VALIDATED against a real
# property -- re-tune with diagnose_water_zone_mask.py. CONFIGURABLE.
VALLEY_ACCUMULATION_PERCENTILE_LOW = 25.0
VALLEY_ACCUMULATION_PERCENTILE_HIGH = 75.0

# Drop tiny, noise-sized eligible-cell clusters below this real cell-union
# footprint area. A small first-pass default, deliberately NOT yet
# validated against a real property the way production_area.py's own
# MIN_PRODUCTION_AREA_ACRES has been — tune once ground-truthed.
# CONFIGURABLE.
MIN_WATER_ZONE_AREA_ACRES = 0.1

# The raw percentile-band-qualifying mask is only ever one cell wide along
# the exact drainage path (see compute_water_eligible_cells()'s own
# docstring) -- confirmed live: without widening it, real zones came back
# as thin, one-cell-wide traces rather than a surveyable area, and most
# separate drainage segments never cleared MIN_WATER_ZONE_AREA_ACRES at
# all. This dilates the band mask by this many meters (converted to a
# cell radius, see _survey_buffer_radius_cells()) BEFORE the service-
# distance/on-parcel/boundary-setback tests run, so a genuinely
# qualifying drainage cell reads as a walkable-width band, not a hairline.
#
# TUNED live against the real reference property alongside the OLD
# single-threshold gate this replaced (see MIN_VALLEY_CONTRIBUTING_AREA_
# ACRES's own history above) -- NOT YET RE-VALIDATED against the
# percentile-band approach specifically. CONFIGURABLE -- re-tune with
# diagnose_water_zone_mask.py against your own property.
WATER_ZONE_SURVEY_BUFFER_METERS = 10.0

# A single 8-connected cluster of eligible water-zone cells can pinch down
# to a narrow "waist" the same way a production-zone cluster can (see
# production_area.py's own MIN_ZONE_WAIST_METERS/attempt_waist_split()) --
# here the pinch is typically an artifact of WATER_ZONE_SURVEY_BUFFER_METERS's
# own dilation bridging two genuinely separate drainage patches that never
# actually touched before widening. raster_grid.attempt_waist_split()
# (shared with production_area.py -- extracted there from what used to be
# this pipeline's own private helper, see that module's docstring for the
# extraction note) is applied to the post-dilation eligibility mask,
# before clustering, so a dilation-induced merge is split back into two
# independent zones.
#
# Starting value reuses production_area.MIN_ZONE_WAIST_METERS's own answer
# to "how narrow before this reads as two things, not one" as the anchor --
# NOT independently derived, and NOT YET VALIDATED against a real
# property. CONFIGURABLE.
WATER_ZONE_MIN_WAIST_METERS = MIN_ZONE_WAIST_METERS

# How far past a tree-cell's own footprint the woody-vegetation hard
# exclusion extends for water zones specifically -- reuses canopy_height_
# data.tree_root_zone_mask() (the SAME threshold-then-dilate raster
# operation production_area.py's own woody-vegetation gate uses), but at
# its OWN buffer distance, not canopy_height_data.TREE_ROOT_ZONE_BUFFER_
# METERS directly: a separate, independently-named constant even though
# it happens to currently equal the same 10ft value -- same "constants
# stay separate even when numerically identical" convention this
# pipeline already applies elsewhere (e.g. this module's own
# MIN_BOUNDARY_SETBACK_METERS/MIN_DOWNSTREAM_CLEARANCE_METERS history),
# so a future retune of one doesn't silently couple to the other.
# CONFIGURABLE.
WATER_ZONE_CANOPY_BUFFER_METERS = 3.048  # 10ft

# How far past a fetched road/right-of-way line's own mapped geometry the
# hard water-zone exclusion extends -- reuses farm_roads_data.
# get_road_exclusion_union_utm() (the SAME real vector road/ROW fetch
# production_area.py's own existing-road gate uses), but at its OWN
# buffer distance, not farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS
# directly (production's own default there is 0.0 -- a no-op buffer) --
# same separate-constant reasoning as WATER_ZONE_CANOPY_BUFFER_METERS
# above. This is a genuinely NEW exclusion for this module: water zones
# haven't excluded roads before. CONFIGURABLE.
WATER_ZONE_ROAD_BUFFER_METERS = 3.048  # 10ft

# A cell inside ANY production area's own render_fill_polygon_utm
# (production_area.py's cluster_and_gate()/identify_production_areas() --
# the waist-split-aware convex hull, reclipped to the real parcel
# boundary, NOT polygon_utm's raw cell-union footprint -- chosen
# specifically because it reins in slivers/branches rather than
# ballooning past them) is hard-excluded from water-zone eligibility
# outright: production ground and water-system infrastructure ground are
# mutually exclusive uses of the same ground, the same cell-level AND'd
# hard-exclusion pattern already applied to canopy/road above, just
# against a different real footprint, and checked against the UNION of
# every production area's render_fill_polygon_utm, not just whichever one
# a zone might eventually be scored against (find_candidate_zones()'s own
# whole-zone scoring picks that afterward — this gate runs before any
# zone even exists). 5.0 meters is the required setback margin beyond
# the production polygon itself. NOT YET VALIDATED against a real
# property, same caveat every other threshold in this pipeline carries.
# CONFIGURABLE.
WATER_ZONE_PRODUCTION_SETBACK_METERS = 5.0

# Sentinel distinguishing "the canopy/road check genuinely ran" from
# "never checked at all" -- same convention production_area.py's own
# _CANOPY_CHECK_UNCHECKED/_ROAD_CHECK_UNCHECKED use, for the same reason:
# compute_water_eligible_cells()/find_candidate_zones() stay pure,
# network-free, and directly unit-testable with these gates simply
# skipped by default (see test_water_candidate_zones.py, which never
# fetches either), while identify_water_system_candidate_zones() (the
# real network entry point) is what actually makes the canopy gate
# MANDATORY -- by always calling get_required_tree_root_zone_mask_utm()
# (which itself either returns a real mask or raises/propagates, see that
# function's own docstring), never leaving this sentinel active on the
# real path. The road gate stays genuinely optional even at the network
# layer (graceful degrade on fetch failure), same as production's own
# check_roads handling.
_CANOPY_CHECK_UNCHECKED = object()
_ROAD_CHECK_UNCHECKED = object()

# Zones at or under this size already read as a reasonable survey pointer
# on their own -- select_optimal_survey_subarea() (see that function's own
# docstring) skips sub-area selection entirely for them, returning None,
# rather than carving an even-smaller sub-region out of ground that's
# already a modest, walkable size. CONFIGURABLE, unvalidated against a
# real property yet, same caveat every other threshold in this pipeline
# carries.
WATER_ZONE_SUBAREA_TRIGGER_ACRES = 1.0

# The optimal sub-area's own size cap -- greedy region-growing (see
# select_optimal_survey_subarea()) stops once this acreage is reached (or
# no adjacent candidate cells remain, if the zone itself is smaller than
# this after excluding cells inside the production area it serves). A
# starting value, not yet validated against a real property. CONFIGURABLE.
WATER_ZONE_SUBAREA_TARGET_ACRES = 0.5

WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES = (
    "This identifies a general candidate zone for water-system "
    "infrastructure (keyline plowing patterns, pond/dam potential, ram "
    "pump routing) — a connected cluster of DEM cells, each individually "
    "on a genuine drainage feature and within plausible service distance "
    "of a candidate production area, outside the boundary setback. "
    "Elevation relative to that production area is NOT a generation-time "
    "filter here: a candidate sitting BELOW its nearest production area "
    "(which would need a pump to deliver water uphill) is still reported, "
    "same as one sitting comfortably above it (which could gravity-feed) "
    "— see properties.production_area_relationships for the real "
    "elevation differential/gradient this candidate was measured against, "
    "and water_suitability.py for how that's turned into a real, weighted "
    "preference score rather than a pass/fail gate. This is NOT a "
    "specific pond or dam site: actual siting requires separate, more "
    "detailed analysis (storage volume, dam wall geometry, spillway "
    "design) not covered here. It also inherits the limitations of the "
    "layers it's built on — DEM-derived flow accumulation and a "
    "slope-only production-area heuristic — so treat this as a starting "
    "area to walk and ground-truth, not a final answer."
)


def _survey_buffer_radius_cells(dem: dict, buffer_meters: float) -> int:
    """
    Converts WATER_ZONE_SURVEY_BUFFER_METERS (a real-world distance) into a
    cell-count dilation radius using the DEM's own resolution_meters --
    same meters-to-cell-units conversion pattern raster_grid.
    waist_erosion_radius_cells() uses (average of the two axis
    resolutions, in case they ever differ). Unlike that function, this is
    a direct radius (not a width being halved into one), so it's rounded
    UP (via ceil) with no further halving -- the buffer is never narrower
    than requested. buffer_meters <= 0 correctly yields 0 (no dilation at
    all), since ceil(0 / cell_size) == 0.
    """
    px, py = dem["resolution_meters"]
    cell_size = (px + py) / 2.0
    return math.ceil(buffer_meters / cell_size)


def compute_water_eligible_cells(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    min_valley_contributing_area_acres: float = MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    accumulation_percentile_low: float = VALLEY_ACCUMULATION_PERCENTILE_LOW,
    accumulation_percentile_high: float = VALLEY_ACCUMULATION_PERCENTILE_HIGH,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    survey_buffer_meters: float = WATER_ZONE_SURVEY_BUFFER_METERS,
    canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    production_setback_meters: float = WATER_ZONE_PRODUCTION_SETBACK_METERS,
) -> np.ndarray:
    """
    Cell-based STEP 1/2: computes the raw flow-accumulation grid directly
    from `dem` (valley_delineation.get_flow_accumulation_for_dem() — the
    same contributing-cell-count grid delineate_valleys() thresholds/
    traces internally, recomputed here rather than reusing a traced
    branch) and gates each cell on SIX independent checks, ALL of which
    must pass for a cell to be eligible:

      1. Contributing-area PERCENTILE BAND, not a single hard threshold:
         first, the "drainage-qualifying population" is every ON-PARCEL
         cell whose own flow_accumulation_cells value clears
         min_valley_contributing_area_acres (a low floor -- see that
         constant's own docstring for its changed role). Then the
         accumulation_percentile_low/high percentiles (numpy.percentile,
         0-100 scale) of THAT population's own values are computed --
         NOT over the whole on-parcel grid, just the population that
         already cleared the floor. A cell (on-parcel or not -- on-parcel-
         ness is checked separately by gate 2 below, same architecture the
         old single-threshold gate used) qualifies for THIS gate iff its
         own flow_accumulation_cells value falls within
         [p_low, p_high] of that population's distribution. This replaces
         the old single hard threshold, which admitted every cell from a
         parcel's single dominant master channel/outlet down to the floor
         equally -- unable to distinguish "the real drainage band" from
         "technically-qualifying but marginal ground barely above the
         floor," or to exclude a single dominant channel so concentrated
         it reads as one drainage LINE rather than a genuine candidate
         BAND.

         If the population is empty (no on-parcel cell clears the floor
         at all), this gate produces an all-False mask -- there's nothing
         to compute a percentile band over.

         This raw per-cell test only ever qualifies a thin, one-cell-wide
         trace along the exact drainage path -- before checks 2/3 below
         run at all, this band mask is WIDENED by dilating it
         (raster_grid.binary_dilate()) by survey_buffer_meters (converted
         to a cell radius via _survey_buffer_radius_cells()), so a real
         zone reads as a walkable-width band, not a hairline. Dilation
         happens on this band mask specifically, NOT on the final combined
         eligible_mask below -- dilating the final mask would let a cell
         that fails the service-distance/setback tests qualify just by
         sitting next to one that passes, which isn't the intent; every
         dilated band cell must still independently clear checks 2/3 on
         its own.

      2. On-parcel (boundary_polygon_utm.contains(cell center)) AND at
         least min_boundary_setback_meters from boundary_polygon_utm's own
         boundary.

      3. Within max_service_distance_meters of at least one production
         area's polygon_utm, and NOT within min_service_distance_meters of
         it UNLESS the cell is already inside/touching that patch
         (distance == 0). Real bug, found live and fixed for the old
         per-branch-point version of this same check: with a single
         production-area patch covering ~95% of a real reference
         property, "distance < min_service_distance is too close" rejected
         every point on that property outright, since a point genuinely
         inside a patch that large has nowhere else to be relative to it.
         min_service_distance_meters exists to reject a near-but-SEPARATE
         siting (where "above by X% grade over Y meters" stops meaning
         anything for Y too small) — it was never meant to reject siting
         INSIDE the production area entirely, and shouldn't, per this
         whole feature's "elevation/proximity is a preference, not a
         gate" direction (see module docstring). This gate only tests
         whether ANY production area is within range -- it does NOT pick
         a "best" one; that's find_candidate_zones()'s own whole-zone
         scoring now, computed once per surviving cluster from a single
         representative point, not per cell (see that function's own
         docstring for why per-cell tagging + per-cluster aggregation was
         removed in favor of this).

      4. NOT within the woody-vegetation root zone: canopy_root_zone_mask_
         utm (canopy_height_data.tree_root_zone_mask()'s own output, at
         this module's own WATER_ZONE_CANOPY_BUFFER_METERS -- see that
         constant's docstring) must be False at this cell. Reuses
         production_area.py's already-validated canopy gate directly
         (same fetch, same threshold-then-dilate raster mask, just a
         separately-tunable buffer distance) rather than reimplementing
         it -- pure raster AND, never vectorized into a polygon buffer/
         difference (see canopy_height_data.py's own module docstring for
         why that path caused the original hydric-soil sliver-
         fragmentation bug). If canopy_root_zone_mask_utm is left at its
         default sentinel (_CANOPY_CHECK_UNCHECKED -- "never checked at
         all," the same convention production_area.py's own
         _CANOPY_CHECK_UNCHECKED uses), this gate is skipped entirely --
         see identify_water_system_candidate_zones() for how the real
         network entry point makes this gate MANDATORY instead, by always
         fetching-or-raising before this function is ever called.

      5. NOT inside road_exclusion_union_utm: real road/right-of-way
         vector geometry (farm_roads_data.get_road_exclusion_union_utm()'s
         own output, at this module's own WATER_ZONE_ROAD_BUFFER_METERS),
         tested via cell-center .contains(), same pattern production_
         area.py's own existing-road gate uses -- a genuinely NEW
         exclusion for this module (water zones haven't excluded roads
         before). A real None (checked, no roads found nearby -- the
         common, clean case) or the default sentinel (_ROAD_CHECK_
         UNCHECKED, "never checked at all") both mean this gate is
         skipped; only a real fetched union polygon excludes anything.
         Unlike canopy, this stays OPTIONAL even on the real network path
         (graceful degrade on fetch failure, mirroring production_area.py's
         own check_roads handling) -- see module docstring.

      6. NOT inside any production area's own render_fill_polygon_utm
         (production_area.py's cluster_and_gate()/identify_production_
         areas() -- the waist-split-aware convex hull, reclipped to the
         real parcel boundary, NOT polygon_utm's raw cell-union footprint;
         chosen over polygon_utm specifically because it reins in
         slivers/branches rather than ballooning past them), buffered by
         production_setback_meters (see WATER_ZONE_PRODUCTION_SETBACK_
         METERS's own docstring -- 0.0 by default, a hard edge-to-edge
         boundary). Tested against the UNION of every production area's
         render_fill_polygon_utm, not just whichever one a zone might
         eventually be scored against -- gate 3 above (service distance)
         explicitly ALLOWS a cell to sit inside a production area's
         polygon_utm (distance == 0 is not "too close"), but that's a
         distance-math carve-out, not a statement that production ground
         and water-system ground are the same ground; this gate is what
         actually keeps them mutually exclusive. Unlike canopy/road, this
         has no "unchecked" sentinel -- production_areas is always a
         required, already-computed argument here (never optionally
         fetched by this function), so the exclusion union is always built
         from whatever was passed in; an empty production_areas list
         yields no exclusion at all (nothing to exclude), which is moot in
         practice since gate 3 above already returns nothing eligible with
         no production areas to serve.

    Elevation/gradient is deliberately NOT a gate here (see module
    docstring's "gravity is a preference, not a gate" framing) — do not
    add a min-gradient or elevation-band exclusion; a cell otherwise
    eligible is never excluded for sitting below its best-matching
    production area.

    Returns eligible_mask: np.ndarray[bool], same shape as dem['array'].
    """
    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    area_per_cell = cell_area_acres(dem)
    min_contributing_cells = min_valley_contributing_area_acres / area_per_cell
    floor_mask = flow_accumulation_cells >= min_contributing_cells

    boundary_prepared = prep(boundary_polygon_utm)
    boundary_line = boundary_polygon_utm.boundary

    population_values = [
        float(flow_accumulation_cells[r, c])
        for r, c in np.argwhere(floor_mask)
        if boundary_prepared.contains(Point(*pixel_center_xy(dem, int(r), int(c))))
    ]

    rows, cols = dem["array"].shape
    if not population_values:
        return np.zeros((rows, cols), dtype=bool)

    p_low = np.percentile(population_values, accumulation_percentile_low)
    p_high = np.percentile(population_values, accumulation_percentile_high)
    band_mask = floor_mask & (flow_accumulation_cells >= p_low) & (flow_accumulation_cells <= p_high)

    survey_buffer_radius_cells = _survey_buffer_radius_cells(dem, survey_buffer_meters)
    band_mask = binary_dilate(band_mask, survey_buffer_radius_cells)

    eligible_mask = np.zeros((rows, cols), dtype=bool)
    array = dem["array"]

    canopy_checked = canopy_root_zone_mask_utm is not _CANOPY_CHECK_UNCHECKED

    road_union = (
        road_exclusion_union_utm
        if road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED and road_exclusion_union_utm is not None
        else None
    )
    road_prepared = prep(road_union) if road_union is not None else None

    production_footprints = [patch["render_fill_polygon_utm"] for patch in production_areas]
    production_exclusion_union_utm = (
        unary_union(production_footprints).buffer(production_setback_meters)
        if production_footprints
        else None
    )
    production_prepared = (
        prep(production_exclusion_union_utm) if production_exclusion_union_utm is not None else None
    )

    for r, c in np.argwhere(band_mask):
        r, c = int(r), int(c)
        elevation = float(array[r, c])
        if np.isnan(elevation):
            continue

        x, y = pixel_center_xy(dem, r, c)
        point = Point(x, y)

        if not boundary_prepared.contains(point):
            continue
        if point.distance(boundary_line) < min_boundary_setback_meters:
            continue
        if canopy_checked and canopy_root_zone_mask_utm[r, c]:
            continue
        if road_prepared is not None and road_prepared.contains(point):
            continue
        if production_prepared is not None and production_prepared.contains(point):
            continue

        within_service_distance = False
        for patch in production_areas:
            distance = point.distance(patch["polygon_utm"])
            if distance > max_service_distance_meters:
                continue
            if 0 < distance < min_service_distance_meters:
                continue
            within_service_distance = True
            break

        if not within_service_distance:
            continue

        eligible_mask[r, c] = True

    return eligible_mask


def _zone_production_area_relationships(
    representative_point: Point,
    representative_elevation_m: float,
    production_areas: list[dict],
    max_service_distance_meters: float,
    min_service_distance_meters: float,
) -> list[dict]:
    """
    Whole-zone version of the old per-cell "best production-area
    relationship" tagging + per-cluster median aggregation: computed ONCE
    per surviving cluster from a single representative point/elevation
    (see find_candidate_zones()'s own docstring) rather than rolled up
    (median) across every member cell's own per-cell tag. Same output
    shape the old aggregation produced, so every downstream consumer
    (zones_to_geojson(), water_suitability.py) is unaffected by this
    change:

        {
            'production_area_id': int,
            'elevation_differential_m': float,  # + = zone sits above the
                                                  # production area
                                                  # (gravity-favorable);
                                                  # - = below (would need a
                                                  # pump)
            'distance_m': float,
            'gradient_pct': float,               # elevation_differential_m
                                                  # / distance_m * 100 —
                                                  # can be negative
            'above_production_area': bool,
        }
    sorted by elevation_differential_m descending (most gravity-favorable
    first), same convention as before.

    Only production areas within max_service_distance_meters (and outside
    min_service_distance_meters, unless distance == 0 -- same carve-out as
    compute_water_eligible_cells()'s own gate) are included -- a zone
    whose representative point falls outside every production area's
    service-distance window returns [] (see find_candidate_zones()'s own
    handling of this case: such a zone is dropped, since there's no single
    headline "served" relationship left to report for it).
    """
    relationships = []
    for patch in production_areas:
        distance = representative_point.distance(patch["polygon_utm"])
        if distance > max_service_distance_meters:
            continue
        if 0 < distance < min_service_distance_meters:
            continue

        elevation_differential_m = representative_elevation_m - patch["representative_elevation_m"]
        gradient_pct = (elevation_differential_m / distance * 100) if distance > 0 else 0.0
        relationships.append(
            {
                "production_area_id": patch["id"],
                "elevation_differential_m": round(elevation_differential_m, 2),
                "distance_m": round(distance, 1),
                "gradient_pct": round(gradient_pct, 2),
                "above_production_area": elevation_differential_m > 0,
            }
        )

    relationships.sort(key=lambda r: -r["elevation_differential_m"])
    return relationships


def select_optimal_survey_subarea(
    zone: dict,
    production_areas: list[dict],
    dem: dict,
) -> Optional[dict]:
    """
    For a zone whose full footprint is large enough that pointing someone
    at the WHOLE thing isn't a very actionable survey instruction, picks a
    smaller, higher-confidence sub-region within it -- favoring elevation
    advantage and proximity to the production area the zone actually
    serves. This is a SUGGESTION layered alongside the zone's own real,
    full geometry (see module docstring's "REPLACES the earlier
    per-traced-valley-branch line-walk" framing for why the full zone
    footprint itself stays the authoritative candidate area) -- it never
    replaces or shrinks polygon_utm/area_acres, which remain the source
    of truth for narrative use.

    zone must be one of find_candidate_zones()'s own zone dicts (needs
    'cells' -- the zone's own member (row, col) cells, same list
    find_candidate_zones() already built via attempt_waist_split(), not
    refetched or reclassified from the raw DEM here -- and
    'primary_production_area_relationship', to identify which production
    area to measure against without re-deriving it).

    Returns None if the zone's own real area (zone['polygon_utm'].area)
    is at or under WATER_ZONE_SUBAREA_TRIGGER_ACRES -- the full zone
    already reads as a reasonable, walkable survey pointer at that size,
    so there's nothing smaller worth carving out. Also returns None if,
    after excluding every zone cell that falls INSIDE the primary
    production area's own polygon_utm (a survey sub-area must sit outside
    land already claimed for production), no candidate cell remains.

    SCORING (per remaining candidate cell):
      - Elevation advantage: this cell's own elevation minus the primary
        production area's representative_elevation_m -- higher is more
        gravity-favorable. Normalized 0-1 across the candidate
        population's own min/max (NOT the whole zone's, since excluded
        cells shouldn't skew the scale) -- a flat range (every candidate
        tied) normalizes to a neutral 0.5 for every cell, not an arbitrary
        1.0, since there's no real differentiation to reward.
      - Proximity: planar distance from this cell's center to the
        production area's own polygon_utm boundary -- closer is better,
        so this is 1 - the same min/max normalization applied to
        elevation advantage.
      Composite score is a simple, UNWEIGHTED average of the two --
      deliberately a starting point (like every other equal-weighting
      choice in this pipeline), not a tuned composite.

    GROWING: seeds the sub-area at the single highest-scoring candidate
    cell, then greedily adds whichever remaining candidate cell is
    8-connected-adjacent (raster_grid.D8_OFFSETS) to the CURRENT sub-area
    and has the highest score, repeating until WATER_ZONE_SUBAREA_TARGET_
    ACRES is reached or no adjacent candidate remains (e.g. the zone
    itself, after exclusions, is smaller than the target). This keeps the
    result one real, contiguous patch -- not just the top-N cells by
    score scattered across the zone, which wouldn't be a walkable
    sub-area at all.

    Builds the sub-area's real geometry via raster_grid.
    cell_union_footprint() -- the same shared utility every other cell-
    cluster footprint in this pipeline uses, never a hull or a buffer.

    Returns:
        {
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'area_acres': float,
        }
    """
    area_acres = zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
    if area_acres <= WATER_ZONE_SUBAREA_TRIGGER_ACRES:
        return None

    primary_production_area_id = zone["primary_production_area_relationship"]["production_area_id"]
    primary_patch = next((p for p in production_areas if p["id"] == primary_production_area_id), None)
    if primary_patch is None:
        return None

    production_polygon = primary_patch["polygon_utm"]
    production_elevation = primary_patch["representative_elevation_m"]
    array = dem["array"]

    candidates = []
    for r, c in zone["cells"]:
        x, y = pixel_center_xy(dem, r, c)
        point = Point(x, y)
        if production_polygon.contains(point):
            continue
        elevation = float(array[r, c])
        if np.isnan(elevation):
            continue
        candidates.append((r, c, elevation - production_elevation, point.distance(production_polygon)))

    if not candidates:
        return None

    def _normalize(value: float, lo: float, hi: float) -> float:
        if hi - lo <= 0:
            return 0.5
        return (value - lo) / (hi - lo)

    advantages = [adv for _r, _c, adv, _dist in candidates]
    distances = [dist for _r, _c, _adv, dist in candidates]
    adv_lo, adv_hi = min(advantages), max(advantages)
    dist_lo, dist_hi = min(distances), max(distances)

    scores: dict[tuple[int, int], float] = {}
    for r, c, adv, dist in candidates:
        elevation_score = _normalize(adv, adv_lo, adv_hi)
        proximity_score = 1.0 - _normalize(dist, dist_lo, dist_hi)
        scores[(r, c)] = (elevation_score + proximity_score) / 2.0

    remaining = set(scores.keys())
    seed = max(remaining, key=lambda cell: (scores[cell], -cell[0], -cell[1]))
    subarea_cells = {seed}
    remaining.discard(seed)

    area_per_cell = cell_area_acres(dem)
    target_cell_count = max(1, round(WATER_ZONE_SUBAREA_TARGET_ACRES / area_per_cell))

    while len(subarea_cells) < target_cell_count and remaining:
        frontier = [
            cell for cell in remaining
            if any((cell[0] + dr, cell[1] + dc) in subarea_cells for dr, dc in D8_OFFSETS)
        ]
        if not frontier:
            break
        best = max(frontier, key=lambda cell: (scores[cell], -cell[0], -cell[1]))
        subarea_cells.add(best)
        remaining.discard(best)

    subarea_mask = np.zeros(array.shape, dtype=bool)
    for r, c in subarea_cells:
        subarea_mask[r, c] = True

    polygon_utm = cell_union_footprint(dem, subarea_mask)
    subarea_area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
    geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

    return {
        "polygon_utm": polygon_utm,
        "geometry_wgs84": geometry_wgs84,
        "area_acres": round(subarea_area_acres, 3),
    }


def find_candidate_zones(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    min_valley_contributing_area_acres: float = MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    accumulation_percentile_low: float = VALLEY_ACCUMULATION_PERCENTILE_LOW,
    accumulation_percentile_high: float = VALLEY_ACCUMULATION_PERCENTILE_HIGH,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
    min_water_zone_area_acres: float = MIN_WATER_ZONE_AREA_ACRES,
    survey_buffer_meters: float = WATER_ZONE_SURVEY_BUFFER_METERS,
    min_zone_waist_meters: float = WATER_ZONE_MIN_WAIST_METERS,
    canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    production_setback_meters: float = WATER_ZONE_PRODUCTION_SETBACK_METERS,
) -> list[dict]:
    """
    Cell-based zone-filtering logic (Step 3) — see module docstring for
    why this takes the already-fetched `dem` (to derive its own flow-
    accumulation grid directly) plus already-computed production_areas
    rather than a list of pre-traced valley branches, and for why
    elevation/gradient is not one of the filters applied here
    (min_gravity_gradient is not part of this signature at all — it's
    water_suitability.py's scoring concern, not a generation-time
    parameter).

    canopy_root_zone_mask_utm/road_exclusion_union_utm/
    production_setback_meters are forwarded straight through to
    compute_water_eligible_cells() -- see that function's own docstring
    (gates 4/5/6) for what each does and its default-sentinel/skipped-
    when-unset behavior. This function itself does no fetching of
    either -- identify_water_system_candidate_zones() is what actually
    fetches canopy/road for the real network path (production_setback_
    meters is not fetched at all, just a plain configurable distance).

    Builds the per-cell eligibility mask (compute_water_eligible_cells() —
    including its own survey_buffer_meters dilation of the raw
    percentile-band mask, see that function's docstring for why a zone
    needs to be wider than a one-cell-wide drainage trace), clusters it
    via raster_grid.connected_components() — exactly the same "cluster's
    own connectivity defines a zone" pattern production_area.py's own
    patches use, with no valley identity carried into this pass at all —
    then attempts a WAIST split on each cluster (raster_grid.
    attempt_waist_split(), shared with production_area.py's own zone
    clustering -- see that module's docstring for why: the survey-buffer
    dilation above can bridge two genuinely separate drainage patches that
    never actually touched before widening, and a narrower-than-
    min_zone_waist_meters pinch reads as two zones, not one). Each
    resulting (possibly split) sub-cluster's REAL cell-union footprint
    (raster_grid.cell_union_footprint()) is built -- not a hull or a line
    buffer -- and clipped to boundary_polygon_utm; clusters below
    min_water_zone_area_acres after clipping are dropped as noise (the
    same min_water_zone_area_acres also gates whether a waist split is
    committed at all -- see attempt_waist_split()'s own docstring).

    Scoring is WHOLE-ZONE, computed once per surviving cluster, not per
    cell and not aggregated from per-cell tags: a representative elevation
    (median of the cluster's own member cells' elevations -- same pattern
    production_area.py's own representative_elevation_m uses) and a
    representative point (the cluster's own real footprint centroid) are
    computed once, and _zone_production_area_relationships() measures
    that single point/elevation against every production area within
    service distance. A cluster whose representative point falls outside
    every production area's service-distance window (a real, if rare,
    possibility for an oddly-shaped or elongated cluster whose individual
    member cells were each near SOME patch, but whose centroid isn't near
    any) is dropped -- there is no single headline "served" relationship
    to report for it, the same trade-off "aggregate up from individual
    cells" always carried, just now paid at the whole-zone level instead
    of the per-cell level.

    Two zone-level aggregates carried alongside the above, for
    water_suitability.py's topographic_factor: contributing_area_cells
    (median flow_accumulation_cells cell-count value, NOT acres, across
    the cluster's own member cells) and slope_pct (median local slope
    percent, production_area.compute_slope_percent()'s own steepest-
    neighbor definition, reused rather than reinvented). These are new,
    additive zone-level fields -- water_suitability.py's own
    topographic_factor still derives its gradient_pct/contributing_area_
    acres inputs from a SEPARATE spatial match against traced valley
    branches (delineate_valleys() output, see water_suitability.
    _valley_topographic_inputs_for_zone()'s own docstring), unaffected by
    and not yet wired to these two fields -- confirmed compatible with
    waist-split (possibly-smaller, possibly-more-numerous) clusters via
    the existing test suite, since that matching is purely geometric
    (zone_polygon_utm containment), not keyed off any per-zone field this
    change touches.

    Returns one entry per qualifying cell cluster:
        {
            'id': int,
            'served_production_area_ids': [int, ...],
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'render_fill_polygon_utm': shapely Polygon/MultiPolygon,
                # DISPLAY-ONLY -- the real cell-union footprint's plain
                # convex hull, re-intersected with boundary_polygon_utm --
                # NEVER used for scoring/eligibility/the narrative report,
                # which all stay on polygon_utm/geometry_wgs84 untouched.
                # Same construction production_area.py's own
                # render_fill_polygon_utm uses -- see render_layout_map.py
                # for where this is actually drawn.
            'render_fill_geometry_wgs84': GeoJSON geometry dict,
                # render_fill_polygon_utm's WGS84 reprojection, same
                # polygon_utm/geometry_wgs84 pairing convention.
            'production_area_relationships': [...],   # see
                _zone_production_area_relationships()'s docstring — one
                entry per served production area, sorted most-gravity-
                favorable first
            'primary_production_area_relationship': dict,  # same shape as
                one production_area_relationships entry — the single most
                gravity-favorable one, for callers that just want one
                headline number
            'contributing_area_cells': float,  # median, see above
            'slope_pct': float,                # median, see above
            'cells': [(row, col), ...],  # the zone's own member DEM cells --
                same "expose raw cluster membership on the dict" precedent
                production_area.py's own patches already establish, so a
                consumer (select_optimal_survey_subarea() below, or a
                future one) never has to recover cluster membership from
                a mask a second time
            'optimal_subarea_polygon_utm': shapely Polygon/MultiPolygon or None,
            'optimal_subarea_geometry_wgs84': GeoJSON geometry dict or None,
            'optimal_subarea_acres': float or None,
                # select_optimal_survey_subarea()'s own output, attached to
                # every zone (always present, never a missing key) --
                # None for a zone at or under WATER_ZONE_SUBAREA_TRIGGER_
                # ACRES, a real smaller sub-region otherwise. A suggestion
                # layered ALONGSIDE polygon_utm/area_acres, which stay the
                # full, real, unchanged zone geometry -- see that
                # function's own docstring.
        }
    'id' is assigned sequentially across the surviving cluster list, same
    convention production_area.py's own patches use — there is no more
    stable "valley identity" to key a zone off of, since a zone's own
    cell-cluster connectivity is what defines it now.
    """
    if not production_areas:
        return []

    eligible_mask = compute_water_eligible_cells(
        dem,
        production_areas,
        boundary_polygon_utm,
        min_valley_contributing_area_acres,
        accumulation_percentile_low,
        accumulation_percentile_high,
        max_service_distance_meters,
        min_service_distance_meters,
        min_boundary_setback_meters,
        survey_buffer_meters,
        canopy_root_zone_mask_utm,
        road_exclusion_union_utm,
        production_setback_meters,
    )

    labels, num_components = connected_components(eligible_mask)

    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    slope_pct_grid = compute_slope_percent(dem["array"], dem["resolution_meters"])
    array = dem["array"]

    zones = []
    next_id = 0
    for component_id in range(num_components):
        cluster_mask = labels == component_id
        cluster_cells = [(int(r), int(c)) for r, c in np.argwhere(cluster_mask)]
        if not cluster_cells:
            continue

        for split_result in attempt_waist_split(
            cluster_cells, eligible_mask.shape, dem, min_water_zone_area_acres, min_zone_waist_meters
        ):
            sub_cells = split_result["cells"]
            sub_mask = np.zeros(eligible_mask.shape, dtype=bool)
            for r, c in sub_cells:
                sub_mask[r, c] = True

            footprint = cell_union_footprint(dem, sub_mask)
            polygon_utm = footprint.intersection(boundary_polygon_utm)
            if polygon_utm.is_empty:
                continue

            area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
            if area_acres < min_water_zone_area_acres:
                continue

            representative_elevation_m = float(np.median([array[r, c] for r, c in sub_cells]))
            representative_point = polygon_utm.centroid

            production_area_relationships = _zone_production_area_relationships(
                representative_point,
                representative_elevation_m,
                production_areas,
                max_service_distance_meters,
                min_service_distance_meters,
            )
            if not production_area_relationships:
                continue

            contributing_area_cells = float(np.median([flow_accumulation_cells[r, c] for r, c in sub_cells]))
            cluster_slopes = [
                float(slope_pct_grid[r, c]) for r, c in sub_cells if not np.isnan(slope_pct_grid[r, c])
            ]
            slope_pct = float(np.median(cluster_slopes)) if cluster_slopes else 0.0

            geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

            # render_fill_polygon_utm: a DISPLAY-ONLY geometry, never used
            # for scoring/eligibility/the narrative report -- same
            # construction production_area.py's own render_fill_polygon_utm
            # uses (the real cell-union footprint's plain convex hull,
            # re-intersected with boundary_polygon_utm so it never extends
            # past the real parcel edge). A water zone traced along a
            # narrow drainage band can be a long, winding, concave shape;
            # the hull reads as a single coherent "this is the water zone"
            # blob at render time instead of a blocky, notched outline --
            # see render_layout_map.py for where this is actually drawn.
            # render_fill_geometry_wgs84 is its WGS84-reprojected pairing,
            # same polygon_utm/geometry_wgs84 pairing convention this zone
            # dict already follows.
            render_fill_polygon_utm = footprint.convex_hull.intersection(boundary_polygon_utm)
            render_fill_geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(render_fill_polygon_utm))

            zone = {
                "id": next_id,
                "served_production_area_ids": sorted(
                    r["production_area_id"] for r in production_area_relationships
                ),
                "polygon_utm": polygon_utm,
                "geometry_wgs84": geometry_wgs84,
                "render_fill_polygon_utm": render_fill_polygon_utm,
                "render_fill_geometry_wgs84": render_fill_geometry_wgs84,
                "cells": sub_cells,
                "production_area_relationships": production_area_relationships,
                "primary_production_area_relationship": production_area_relationships[0],
                "contributing_area_cells": round(contributing_area_cells, 2),
                "slope_pct": round(slope_pct, 2),
            }

            optimal_subarea = select_optimal_survey_subarea(zone, production_areas, dem)
            if optimal_subarea is not None:
                zone["optimal_subarea_polygon_utm"] = optimal_subarea["polygon_utm"]
                zone["optimal_subarea_geometry_wgs84"] = optimal_subarea["geometry_wgs84"]
                zone["optimal_subarea_acres"] = optimal_subarea["area_acres"]
            else:
                zone["optimal_subarea_polygon_utm"] = None
                zone["optimal_subarea_geometry_wgs84"] = None
                zone["optimal_subarea_acres"] = None

            zones.append(zone)
            next_id += 1

    return zones


def zones_to_geojson(zones: list[dict]) -> dict:
    """Wraps find_candidate_zones() output as the schema-conformant
    GeoJSON FeatureCollection this feature actually delivers
    (layer="water_system_candidate"). This is the UNSCORED diagnostic
    layer — confidence stays CONFIDENCE_LOW/flat here, same as
    production_area.py's own production_areas_to_geojson() before
    production_suitability.py enriches it; water_suitability.py is where
    real, differentiated confidence/suitability_score get added, on this
    same layer, following that exact precedent."""
    features = [
        make_feature(
            feature_id=f"water-system-candidate-{z['id']}",
            geometry=z["geometry_wgs84"],
            layer="water_system_candidate",
            label=f"Water system candidate zone {z['id']}",
            confidence=CONFIDENCE_LOW,
            confidence_notes=WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES,
            extra_properties={
                "served_production_area_ids": z["served_production_area_ids"],
                "production_area_relationships": z["production_area_relationships"],
                "primary_production_area_relationship": z["primary_production_area_relationship"],
                "contributing_area_cells": z["contributing_area_cells"],
                "slope_pct": z["slope_pct"],
                "render_fill_geometry_wgs84": z["render_fill_geometry_wgs84"],
                "optimal_subarea_geometry_wgs84": z["optimal_subarea_geometry_wgs84"],
                "optimal_subarea_acres": z["optimal_subarea_acres"],
            },
        )
        for z in zones
    ]
    return make_feature_collection(features)


def identify_water_system_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    valleys: Optional[list[dict]] = None,
    production_areas: Optional[list[dict]] = None,
    canopy_height: Optional[dict] = None,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in —
    e.g. reused from generate_full_report.py already fetching it, or
    supplied directly in a test), identifies production-area candidates,
    and returns:

        {
            'zones_geojson': FeatureCollection,             # layer="water_system_candidate" — the deliverable
            'valleys_geojson': FeatureCollection,            # layer="valley" — diagnostic (Stage 1)
            'production_areas_geojson': FeatureCollection,   # layer="production_area_candidate" — diagnostic
        }

    dem, boundary_polygon_utm, valleys, and production_areas are all
    optional overrides, independently of one another (supplying one does
    not require supplying the rest) — each falls back to being self-
    computed exactly as before if not supplied. This lets a caller that
    has already computed some or all of these upstream (e.g. a shared
    pipeline-context orchestrator reusing one DEM/boundary/valleys/
    production-areas pass across several KSOP steps) pass them straight
    through instead of this function re-deriving or re-fetching its own
    copies:
      - boundary_polygon_utm: the same warp_transform-then-Polygon(...)
        reprojection this function has always done inline, computed only
        if not supplied.
      - valleys: valley_delineation.delineate_valleys(dem)'s own output
        by default. Only ever consumed by this function's own diagnostic
        valleys_geojson output below — find_candidate_zones() itself
        never reads valleys at all (it derives its own flow-accumulation
        grid directly from `dem`, see that function's own docstring), so
        supplying/omitting this override changes valleys_geojson only,
        never zones_geojson.
      - production_areas: production_area.identify_production_areas(dem,
        boundary_polygon_utm)'s own raw (un-ceiling-trimmed) patches by
        default, but an override doesn't have to match that exact shape —
        find_candidate_zones() only ever reads a patch's 'polygon_utm',
        'render_fill_polygon_utm', 'id', and 'representative_elevation_m'
        fields, so production_area_ceiling.
        identify_optimized_production_areas()'s scored_patches (a strict
        superset of those same fields) is a valid drop-in override too.

    valleys_geojson is still produced via valley_delineation.
    delineate_valleys() purely as diagnostic output (unchanged, own
    traced-branch geometry, own thresholds) — useful for inspecting "did
    we find the right valleys" independently of "is the zone logic
    right," per this feature's stated debugging goal — but
    find_candidate_zones() itself no longer consumes delineate_valleys()'s
    traced branches at all; it derives its own flow-accumulation grid
    directly from `dem` (see find_candidate_zones()'s own docstring).

    This is the network entry point that makes find_candidate_zones()'s
    canopy exclusion genuinely MANDATORY: it always calls production_area.
    get_required_tree_root_zone_mask_utm() (at this module's own
    WATER_ZONE_CANOPY_BUFFER_METERS) before find_candidate_zones() ever
    runs, so a missing/unreliable canopy fetch raises (RuntimeError for no
    coverage at all, canopy_height_data.CanopyCoverageIncompleteError for
    coverage too sparse to trust) rather than silently proceeding without
    the check -- same hard-fail behavior as production_area.identify_
    production_areas()'s own woody-vegetation gate, reusing that exact
    function rather than a parallel copy. The road exclusion, by
    contrast, is fetched too but degrades GRACEFULLY on failure (falls
    back to "not checked," same as production's own check_roads default)
    -- see compute_water_eligible_cells()'s own docstring (gates 4/5).

    canopy_height is an optional pre-fetched override in the same family as
    the dem/boundary/valleys/production_areas overrides above: the SAME
    dict canopy_height_data.get_canopy_height_for_boundary() returns (e.g.
    parcel_data.ParcelData.canopy_height). When supplied it is forwarded to
    BOTH canopy consumers on this path -- the default production_areas
    fetch (identify_production_areas() above) and this module's own
    mandatory get_required_tree_root_zone_mask_utm() call -- so neither
    re-fetches canopy from the network; when None (the default) both fetch
    as before, leaving the mandatory-gate semantics unchanged.
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    if boundary_polygon_utm is None:
        boundary_xs, boundary_ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    if valleys is None:
        valleys = delineate_valleys(dem)

    if production_areas is None:
        production_areas = identify_production_areas(dem, boundary_polygon_utm, canopy_height=canopy_height)

    canopy_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=WATER_ZONE_CANOPY_BUFFER_METERS, canopy_height=canopy_height
    )

    try:
        road_exclusion_union_utm = _fetch_road_exclusion_union_utm(
            boundary_coordinates, dem, buffer_meters=WATER_ZONE_ROAD_BUFFER_METERS
        )
    except Exception:
        road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    zones = find_candidate_zones(
        dem,
        production_areas,
        boundary_polygon_utm,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
        **zone_kwargs,
    )

    return {
        "zones_geojson": zones_to_geojson(zones),
        "valleys_geojson": valleys_to_geojson(valleys),
        "production_areas_geojson": production_areas_to_geojson(production_areas),
    }


def summarize_water_system_candidate_zones(result: dict) -> str:
    zone_count = len(result["zones_geojson"]["features"])
    valley_count = len(result["valleys_geojson"]["features"])
    production_area_count = len(result["production_areas_geojson"]["features"])

    if zone_count == 0:
        return (
            f"{valley_count} primary valley(s) and {production_area_count} "
            "production-area candidate(s) found, but no drainage cell falls "
            "within the service-distance/boundary-setback thresholds — no "
            "water system candidate zones identified."
        )

    return (
        f"Water system candidate zones: {zone_count} "
        f"(from {valley_count} primary valley(s) and "
        f"{production_area_count} production-area candidate(s))"
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

    print("Identifying water system candidate zones for property boundary...\n")

    try:
        result = identify_water_system_candidate_zones(property_boundary)
        print(summarize_water_system_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer — not a fully sandboxed environment."
        )
