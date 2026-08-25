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
        --> [this module] per-DEM-cell eligibility mask (ABSOLUTE
            contributing-area ceiling + on-parcel + max service distance +
            boundary setback (now 0.0, inert) + canopy root-zone
            exclusion + existing-road exclusion + production-area
            exclusion)
        --> connected components (4-connected) -> per-cluster greedy trim
            to a fixed survey-area target -> select ONE cluster (highest
            post-trim summed flow accumulation) -> plain bounded cell-
            union footprint
        --> whole-zone scoring (one representative point) -> exactly one
            candidate-zone polygon (or none)

This mirrors the same pattern the production-zone pipeline now uses (hard
exclusion gates -> cluster -> greedy trim -> bounded footprint), applied
to a water-system per-cell eligibility test. It REPLACES an earlier
design built on a boundary-dependent contributing-area PERCENTILE BAND, a
waist split, and a convex hull:

  - The percentile band was boundary-dependent -- a percentile is defined
    relative to its population, and that population was "the cells inside
    the drawn boundary," so moving the boundary moved the selected band
    even though the terrain was unchanged. Contributing area in acres is a
    physical property of the terrain, so the gate is now an ABSOLUTE
    ceiling (MAX_VALLEY_CONTRIBUTING_AREA_ACRES), not a relative band.
  - There is no minimum contributing area: the deliverable is a survey
    area ("this area has the best potential based on flow accumulation"),
    not a pass/fail on pond viability. A hard minimum returns nothing on
    parcels near the top of a watershed; reporting the best available
    site is more useful than reporting nothing.
  - There is no waist split and no convex hull here -- water zones are at
    most WATER_ZONE_TARGET_ACRES, small enough that splitting adds no
    value and an opening at any useful radius could erase them entirely.

A zone is "a connected cluster of individually-eligible DEM cells,"
trimmed to its own best WATER_ZONE_TARGET_ACRES -- the same "cluster's own
connectivity defines it" logic production_area.py's clusters already use.
Finding one "best" pond/dam site within that zone is explicitly out of
scope here (see the confidence_notes on the output feature) -- that's
future, separate, more detailed work (storage volume, dam wall geometry).
This branch produces exactly ONE water zone; a second-pass candidate
(re-running with the first zone added to the exclusion gate) is
deliberately deferred.

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

Each zone also carries render_fill_polygon_utm/render_fill_geometry_wgs84.
For water zones this is a bounded morphological OPENING of the zone's own
cell mask, clipped to polygon_utm -- the same disc opening production zones
use (raster_grid.eroded_cell_mask()/binary_dilate() with element="disc"),
but at a DELIBERATELY tiny radius (WATER_ZONE_RENDER_OPENING_RADIUS_METERS)
and with NO lead erode, because a ~0.5-acre zone (~81 cells, ~9x9 on a 5m
grid) cannot afford to lose a cell off every edge. The opening softens the
blocky cell-union edge and trims single-cell protrusions; it can also sever
a genuinely too-narrow pinch, so render_fill_polygon_utm may be a
MultiPolygon (acceptable -- the pinch is too narrow to be one coherent
survey area). A zone thinner than the opening radius throughout erodes to
nothing; render_fill_polygon_utm then falls back to polygon_utm (non-empty,
logged once). The invariant render_fill_polygon_utm is a subset of
polygon_utm is asserted, raising on violation. polygon_utm stays the real,
unsmoothed cell-union footprint at the WATER_ZONE_TARGET_ACRES target;
render_fill_polygon_utm is smaller, the same way production's drawn fill
runs a fraction of the eligible footprint.
"""

import logging
import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely import contains_xy
from shapely.geometry import Point, Polygon, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import (
    METERS_PER_FOOT,
    _fetch_road_exclusion_union_utm,
    compute_slope_percent,
    get_required_tree_root_zone_mask_utm,
    identify_production_areas,
    production_areas_to_geojson,
)
from raster_grid import (
    D4_OFFSETS,
    SQUARE_METERS_PER_ACRE,
    binary_dilate,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    eroded_cell_mask,
    pixel_center_xy,
    waist_erosion_radius_cells,
)
from valley_delineation import (
    delineate_valleys,
    get_flow_accumulation_for_dem,
    valleys_to_geojson,
)

_LOGGER = logging.getLogger(__name__)

# Setback from the property boundary applied to candidate water-zone
# cells. ZEROED (was 15.0). Flow accumulation is maximal where water
# leaves the parcel, which is always near an edge -- on the reference
# property every high-accumulation cell sat 0.87-10.8 m from the boundary
# and was eliminated by the old 15 m setback, discarding the best sites.
# The constant, its docstring, and every code path that reads it are kept
# so a setback can be reintroduced without a schema change; only its VALUE
# is 0.0, which makes the setback test (distance < 0.0) inert. What would
# justify reintroducing a nonzero value: real, parcel-specific setback/
# easement/access rules this pipeline currently has no data on. Note the
# on-parcel containment test is a SEPARATE, independent guard (see
# compute_water_eligible_cells()) -- zeroing this setback does not weaken
# off-parcel exclusion. CONFIGURABLE.
MIN_BOUNDARY_SETBACK_METERS = 0.0

# How far downhill a candidate cell's elevation advantage is considered
# relevant to a given production-area patch at all. Beyond this, even a
# technically-qualifying gradient isn't a plausible single contour-channel
# run. CONFIGURABLE.
MAX_SERVICE_DISTANCE_METERS = 800.0

# Absolute ceiling on a cell's own contributing area. Above roughly this,
# a pond site silts in, runs turbid, and needs engineered spillway capacity
# regardless of pond size (NRCS CPS 378 changes freeboard requirements above
# 20 drainage acres). This is a peak-flow and sediment limit, NOT a fill-rate
# ratio -- it does not scale with target pond size, which is why it is a flat
# value rather than a multiple of anything.
#
# This ABSOLUTE ceiling replaces the old boundary-dependent percentile band
# (VALLEY_ACCUMULATION_PERCENTILE_LOW/HIGH) and the old lower gate
# (MIN_VALLEY_CONTRIBUTING_AREA_ACRES). A percentile is defined relative to
# its population -- the cells inside the drawn boundary -- so moving the
# boundary moved the selected band even though the terrain was unchanged
# (the core bug this rewrite fixes). Contributing area in acres is a
# physical property of the terrain and does not depend on where a line was
# drawn, so the gate is now absolute. There is deliberately NO lower
# bound: the deliverable is a survey area, not a pass/fail on pond
# viability, so water zones report the best available site rather than
# returning nothing near the top of a watershed. CONFIGURABLE.
MAX_VALLEY_CONTRIBUTING_AREA_ACRES = 20.0

# Drop tiny, noise-sized eligible-cell clusters below this real cell-union
# footprint area. A small first-pass default, deliberately NOT yet
# validated against a real property the way production_area.py's own
# MIN_PRODUCTION_AREA_ACRES has been — tune once ground-truthed. This is
# the cluster-size floor -- the direct analogue of production's
# MIN_PRODUCTION_AREA_ACRES -- NOT a contributing-area floor; there is no
# contributing-area minimum in this design. CONFIGURABLE.
MIN_WATER_ZONE_AREA_ACRES = 0.1

# Target survey-area size. The deliverable is a survey pointer -- "this area
# has the best potential based on flow accumulation" -- not a pond footprint,
# so this generalises the area the way production zones' contour fill does.
# Every surviving cluster is grown from its highest-accumulation seed (4-
# connected) up to at or below this size before one candidate is selected.
# CONFIGURABLE. (Deriving this from a site's supportable pond size is a
# separate, later decision -- kept at 0.5 here.)
WATER_ZONE_TARGET_ACRES = 0.5

# Opening radius for the water zone render fill. DELIBERATELY tiny compared
# with production's 24m: a 0.5-acre zone is roughly 81 cells (~9x9 on a 5m
# grid), and an opening removes features narrower than 2r. At r = 1 cell this
# trims single-cell protrusions and softens the blocky cell-union edge; at
# r = 2 cells it would remove anything under 20m, which on a 9-cell-wide shape
# is most of the zone. No lead erode -- a 0.5-acre zone cannot afford an extra
# cell off every edge. CONFIGURABLE.
WATER_ZONE_RENDER_OPENING_RADIUS_METERS = 5.0

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

# The road exclusion deliberately has NO per-module buffer constant of its
# own (unlike WATER_ZONE_CANOPY_BUFFER_METERS above): this module's road
# gate reads farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS -- the single
# definition every consumer of the existing-road exclusion shares -- via
# _fetch_road_exclusion_union_utm()'s own default, same as production/
# ceiling/exclusion_zones. A separate per-module water road-buffer
# constant (3.048m/10ft) used to live here on the general separate-
# constants principle; it was DELETED because it answered the same single question
# ("how far should a proposed feature stay off an existing road?") as the
# shared constant, not a different one -- see ROAD_EXCLUSION_BUFFER_
# METERS's own docstring for the full reasoning, and for when a split
# would become legitimate again.
#
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
#
# KEPT while the former 10 m minimum-service-distance siting gate was
# removed -- the two are different kinds of rule and must not be confused.
# This 5 m is a physical BUILD MARGIN: the minimum ground between a pond
# wall and worked production ground, a construction constraint that holds
# regardless of siting. The removed 10 m gate was a SITING heuristic that
# tried to hold a pond off a field edge on the premise that "adjacent is
# too close" -- a premise that was wrong (water zones may butt right up to
# production) and that never worked cleanly at real scale: a single
# production patch can cover most of a parcel, so a strict "distance < 10 m
# is too close" reading rejected nearly every candidate cell and needed a
# distance == 0 carve-out just to function. A build margin has neither
# problem, so it stays. CONFIGURABLE.
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

# A THIRD, separate sentinel -- for identify_water_system_candidate_zones()'s
# road_exclusion_union_utm PARAMETER, distinguishing "the caller supplied
# nothing, self-fetch" from "the caller supplied a real None". It is NOT
# _ROAD_CHECK_UNCHECKED above, and the difference matters: a real None is
# farm_roads_data.get_road_exclusion_union_utm()'s own clean result --
# "checked, and genuinely no mapped road nearby", the common case on a rural
# parcel -- so it must be REUSED (road_data_available True, no second fetch),
# while _ROAD_CHECK_UNCHECKED means the check never ran at all and the gate
# is skipped. Collapsing the two would reintroduce the redundant fetch on
# exactly the parcels the pass-through exists to spare, which is the trap
# exclusion_zones._ROAD_UNION_NOT_SUPPLIED was introduced to avoid on the
# other side of the same union.
_ROAD_UNION_NOT_SUPPLIED = object()

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


def compute_water_eligible_cells(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    max_valley_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    production_setback_meters: float = WATER_ZONE_PRODUCTION_SETBACK_METERS,
) -> np.ndarray:
    """
    Cell-based STEP 1: computes the raw flow-accumulation grid directly
    from `dem` (valley_delineation.get_flow_accumulation_for_dem() — the
    same contributing-cell-count grid delineate_valleys() thresholds/
    traces internally, recomputed here rather than reusing a traced
    branch) and applies a set of HARD EXCLUSION GATES. A cell is eligible
    unless ANY of these holds:

      1. Its own contributing area exceeds max_valley_contributing_area_
         acres -- an ABSOLUTE ceiling, NOT a boundary-dependent percentile
         band and NOT a lower threshold. Contributing area in acres is a
         physical property of the terrain (flow_accumulation_cells value *
         cell area) and does not depend on where a boundary was drawn, so
         the gate is absolute: it cannot move when the boundary moves,
         which was the core bug in the old percentile band (a percentile
         is defined relative to its population -- the on-parcel cells --
         so moving the boundary moved the selected band even though the
         terrain was unchanged). There is deliberately NO lower bound: the
         deliverable is a survey area, not a pass/fail on pond viability,
         so a cell is never excluded merely for LOW contributing area --
         the best available site is reported rather than nothing. See
         MAX_VALLEY_CONTRIBUTING_AREA_ACRES's own docstring for the NRCS
         CPS 378 / siltation reasoning behind the 20-acre ceiling.

      2. It is OFF-PARCEL (not boundary_polygon_utm.contains(cell center))
         -- a hard exclusion in its own right. This is a SEPARATE,
         independent test from the boundary setback below: even with
         min_boundary_setback_meters == 0.0 (the current value), an
         off-parcel cell is still excluded here. The setback is an
         additional, and now inert, test on top of this one, NOT a
         replacement for it.

      3. It FAILS the max-service-distance gate: it is NOT within
         max_service_distance_meters of any production area's polygon_utm.
         A pond too far from the ground it serves is useless regardless of
         how good the drainage is, so this remains a real generation-time
         filter. There is deliberately NO minimum-service-distance gate:
         an earlier 10 m "too close to a production edge" rule was removed
         (water zones may butt right up to production -- there is no siting
         reason to hold a pond off a field edge), so a cell at ANY distance
         at or below max_service_distance_meters (distance == 0 included,
         i.e. inside/touching a patch) passes here. Production overlap is
         still excluded -- by the separate production-exclusion gate (6),
         not by this one. This gate only tests whether ANY production area
         is within range -- it does NOT pick a "best" one; that's
         find_candidate_zones()'s own whole-zone
         scoring now, computed once per surviving cluster from a single
         representative point, not per cell (see that function's own
         docstring for why per-cell tagging + per-cluster aggregation was
         removed in favor of this).

      4. It is INSIDE the woody-vegetation root zone: canopy_root_zone_mask_
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

      5. It is INSIDE road_exclusion_union_utm: real road/right-of-way
         vector geometry (farm_roads_data.get_road_exclusion_union_utm()'s
         own output, at the shared farm_roads_data.ROAD_EXCLUSION_BUFFER_
         METERS -- the single definition of "how far off an existing road"
         every consumer reads; this module's former separate copy was
         deleted, see the constants section above),
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

      6. It is INSIDE any production area's own render_fill_polygon_utm
         (production_area.py's cluster_and_gate()/identify_production_
         areas() -- the bounded morphological opening, clipped to the real
         parcel boundary, NOT polygon_utm's raw cell-union footprint;
         chosen over polygon_utm specifically because it reins in
         slivers/branches rather than ballooning past them), buffered by
         production_setback_meters (see WATER_ZONE_PRODUCTION_SETBACK_
         METERS's own docstring -- 0.0 by default, a hard edge-to-edge
         boundary). Tested against the UNION of every production area's
         render_fill_polygon_utm, not just whichever one a zone might
         eventually be scored against -- gate 3 above (max service
         distance) allows a cell to sit inside a production area's
         polygon_utm (distance == 0 passes), but sitting close to (or
         inside) a production patch is not a statement that production
         ground and water-system ground are the same ground; this gate is
         what actually keeps them mutually exclusive. Unlike canopy/road, this
         has no "unchecked" sentinel -- production_areas is always a
         required, already-computed argument here (never optionally
         fetched by this function), so the exclusion union is always built
         from whatever was passed in; an empty production_areas list
         yields no exclusion at all (nothing to exclude), which is moot in
         practice since gate 3 above already returns nothing eligible with
         no production areas to serve.

    The boundary setback (min_boundary_setback_meters) is still applied as
    an additional test on top of gate 2, but its value is 0.0 (see
    MIN_BOUNDARY_SETBACK_METERS), so "distance < 0.0" is never true and the
    setback is inert. Zeroing it does NOT weaken the off-parcel exclusion
    (gate 2), which is a separate containment test.

    There is NO percentile band, NO minimum contributing area, and NO
    survey-buffer dilation here anymore -- the eligible mask is a broad
    per-cell gate result, not a one-cell-wide hairline, so nothing needs
    widening before clustering. Every eligible cell independently cleared
    the absolute contributing-area ceiling; there is no dilation step that
    could admit a cell above the ceiling by adjacency.

    Elevation/gradient is deliberately NOT a gate here (see module
    docstring's "gravity is a preference, not a gate" framing) — do not
    add a min-gradient or elevation-band exclusion; a cell otherwise
    eligible is never excluded for sitting below its best-matching
    production area.

    Returns eligible_mask: np.ndarray[bool], same shape as dem['array'].
    """
    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    area_per_cell = cell_area_acres(dem)
    max_contributing_cells = max_valley_contributing_area_acres / area_per_cell
    # Absolute contributing-area ceiling: eligible cells are those AT OR
    # BELOW the ceiling (no lower bound). NaN accumulation compares False,
    # so NaN cells are already excluded here as well as by the elevation
    # NaN guard below.
    ceiling_mask = flow_accumulation_cells <= max_contributing_cells

    rows, cols = dem["array"].shape
    eligible_mask = np.zeros((rows, cols), dtype=bool)
    array = dem["array"]

    boundary_prepared = prep(boundary_polygon_utm)
    boundary_line = boundary_polygon_utm.boundary

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

    for r, c in np.argwhere(ceiling_mask):
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

    Only production areas within max_service_distance_meters are included
    (the same max-service-distance gate compute_water_eligible_cells()
    applies; there is no minimum-service-distance gate) -- a zone
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


def _grow_zone_cells(
    cluster_cells: list[tuple[int, int]],
    flow_accumulation_cells: np.ndarray,
    target_cell_count: int,
) -> list[tuple[int, int]]:
    """
    Connected greedy growth of ONE cluster to target_cell_count cells:
    seed with the cluster's single highest-accumulation cell, then
    repeatedly add the highest-accumulation cell that is 4-CONNECTED-
    adjacent to the current set, until the set reaches target_cell_count or
    no adjacent cell remains. The result is connected by construction.

    4-connectivity (D4_OFFSETS), NOT 8: diagonal-only adjacency means two
    cells sharing a single corner point, which cell_union_footprint()
    renders as a disjoint MultiPolygon -- 4-connected growth keeps the
    footprint a single Polygon.

    Ties (equal accumulation) are broken deterministically by (row, col).
    No lookahead, no jump rule, no fragment-reconnect: a lower-accumulation
    adjacent cell is deliberately taken over a higher-accumulation
    non-adjacent one.
    """
    cluster_set = set(cluster_cells)

    def _key(cell):
        # Highest accumulation first; deterministic (smallest row, then col)
        # on ties via negated coordinates under max().
        return (float(flow_accumulation_cells[cell[0], cell[1]]), -cell[0], -cell[1])

    seed = max(cluster_cells, key=_key)
    grown = {seed}
    frontier: set[tuple[int, int]] = set()

    def _push_neighbors(cell):
        r, c = cell
        for dr, dc in D4_OFFSETS:
            neighbor = (r + dr, c + dc)
            if neighbor in cluster_set and neighbor not in grown:
                frontier.add(neighbor)

    _push_neighbors(seed)
    while len(grown) < target_cell_count and frontier:
        best = max(frontier, key=_key)
        frontier.discard(best)
        grown.add(best)
        _push_neighbors(best)

    return list(grown)


def find_candidate_zones(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    max_valley_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_water_zone_area_acres: float = MIN_WATER_ZONE_AREA_ACRES,
    water_zone_target_acres: float = WATER_ZONE_TARGET_ACRES,
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

    Builds the per-cell eligibility mask (compute_water_eligible_cells() --
    the absolute-ceiling hard-exclusion gate, no percentile band and no
    survey-buffer dilation), then follows the same pattern production zones
    now use: CLUSTER -> connected greedy GROWTH of every cluster to target ->
    select ONE candidate -> bounded morphological opening. Concretely:

      1. Cluster the eligible mask 4-connected
         (raster_grid.connected_components(connectivity=4)), matching
         production_area.cluster_and_gate()'s own labeling so a cluster's
         cells are edge-connected and its footprint is a single Polygon
         rather than a corner-touch MultiPolygon.

      2. Clip each cluster's real cell-union footprint
         (raster_grid.cell_union_footprint()) to boundary_polygon_utm and
         drop any cluster whose clipped footprint is below
         min_water_zone_area_acres. This is the cluster-size noise filter
         (the direct analogue of production's MIN_PRODUCTION_AREA_ACRES),
         applied to the FULL cluster before trimming -- NOT a quality
         judgement.

      3. Grow each surviving cluster to water_zone_target_acres by CONNECTED
         GREEDY GROWTH from a seed (see _grow_zone_cells()): seed with the
         cluster's single highest-accumulation cell, then repeatedly add the
         highest-accumulation cell that is 4-CONNECTED-adjacent to the
         current set, until the set reaches the target cell count or no
         adjacent cell remains. The result is connected by construction --
         no post-hoc connectivity check, no largest-component retention --
         and 4-connectivity (not 8) guarantees the cell-union footprint is a
         single Polygon rather than a corner-touch MultiPolygon. This
         replaces an earlier top-N-by-accumulation trim, which had no
         adjacency constraint and could return several disconnected
         fragments (a survey area a farmer walks should be one place). The
         trade-off is deliberate: growth will sometimes take a lower-
         accumulation adjacent cell over a higher-accumulation one elsewhere
         in the cluster -- that is the point; there is no lookahead, jump
         rule, or fragment-reconnect heuristic. A cluster exhausted before
         reaching target (no adjacent cells left) is simply smaller than
         target -- legitimate, NOT padded, and NOT dropped on that ground
         alone. (Since the cluster is itself 4-connected from step 1, growth
         reaches every cell, so a cluster at or below target grows to its
         whole self.)

      4. Select ONE candidate -- the cluster with the highest TOTAL (sum)
         flow accumulation across its own POST-GROWTH cells. The ordering is
         deliberate: ranking before growth would let a sprawling, low-
         accumulation cluster win on size alone (sum scales with cell
         count), so every cluster is grown to its own best target-sized area
         first, making the sums comparable -- the sum then answers "whose
         best target-acre area carries the most drainage?" A known, accepted
         consequence: a cluster between the floor and the target has fewer
         cells, so its sum is lower and it generally loses to a cluster that
         can fill the full target -- intended, since a full-target survey
         area is a better deliverable than an undersized one even when the
         small one's individual cells score well. This branch returns that
         single zone (or [] if nothing qualifies); a second-pass candidate
         is deliberately deferred.

    Scoring is WHOLE-ZONE, computed once for the selected cluster, not per
    cell and not aggregated from per-cell tags: a representative elevation
    (median of the cluster's own post-growth member cells' elevations -- same
    pattern production_area.py's own representative_elevation_m uses) and a
    representative point (the cluster's own real footprint centroid) are
    computed once, and _zone_production_area_relationships() measures
    that single point/elevation against every production area within
    service distance. A cluster whose representative point falls outside
    every production area's service-distance window (a real, if rare,
    possibility for an oddly-shaped or elongated cluster whose individual
    member cells were each near SOME patch, but whose centroid isn't near
    any) is not eligible to be selected -- there is no single headline
    "served" relationship to report for it.

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
    and not yet wired to these two fields, since that matching is purely
    geometric (zone_polygon_utm containment), not keyed off any per-zone
    field this change touches.

    Returns a list with EXACTLY ONE selected zone (or [] if none qualify):
        {
            'id': int,   # always 0 -- exactly one zone is produced
            'served_production_area_ids': [int, ...],
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'render_fill_polygon_utm': shapely Polygon/MultiPolygon,
                # A bounded morphological OPENING of the zone's own cell
                # mask (disc erode-then-dilate at WATER_ZONE_RENDER_OPENING_
                # RADIUS_METERS, no lead erode), clipped to polygon_utm.
                # Smaller than polygon_utm, may be a MultiPolygon if the
                # opening severs a too-narrow pinch, and falls back to
                # polygon_utm (logged) if the zone erodes to nothing. Always
                # asserted a subset of polygon_utm. See module docstring.
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
            'cells': [(row, col), ...],  # the zone's own post-growth member
                DEM cells -- same "expose raw cluster membership on the dict"
                precedent production_area.py's own patches already establish,
                so a consumer never has to recover membership from a mask a
                second time
        }
    'id' is always 0 -- exactly one zone is produced.
    """
    if not production_areas:
        return []

    eligible_mask = compute_water_eligible_cells(
        dem,
        production_areas,
        boundary_polygon_utm,
        max_valley_contributing_area_acres,
        max_service_distance_meters,
        min_boundary_setback_meters,
        canopy_root_zone_mask_utm,
        road_exclusion_union_utm,
        production_setback_meters,
    )

    # 4-connected clustering, matching production_area.cluster_and_gate()'s
    # own labeling so each cluster's footprint is a single Polygon rather
    # than a corner-touch MultiPolygon.
    labels, num_components = connected_components(eligible_mask, connectivity=4)

    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    slope_pct_grid = compute_slope_percent(dem["array"], dem["resolution_meters"])
    array = dem["array"]

    area_per_cell = cell_area_acres(dem)
    grid_shape = eligible_mask.shape
    # Whole-cell target: grow to at most the N cells that fit at or below
    # the target area. floor() guarantees N * area_per_cell <= target;
    # max(1, ...) keeps at least one cell for a tiny target.
    target_cell_count = max(1, int(math.floor(water_zone_target_acres / area_per_cell + 1e-9)))

    # Grow every surviving cluster, then select the single one with the
    # highest POST-GROWTH summed flow accumulation. render_fill (the
    # bounded opening) is computed only for the selected winner, so a
    # wipeout fallback is logged at most once.
    candidates = []  # (post_growth_sum, tiebreak, grown_cells, polygon_utm, rels, metadata...)
    for component_id in range(num_components):
        cluster_mask = labels == component_id
        cluster_cells = [(int(r), int(c)) for r, c in np.argwhere(cluster_mask)]
        if not cluster_cells:
            continue

        # Cluster-size noise filter on the FULL clipped cluster, BEFORE
        # growth (the direct analogue of production's MIN_PRODUCTION_AREA_
        # ACRES). A cluster between this floor and the target survives and
        # is not padded.
        full_mask = np.zeros(grid_shape, dtype=bool)
        for r, c in cluster_cells:
            full_mask[r, c] = True
        full_polygon = cell_union_footprint(dem, full_mask).intersection(boundary_polygon_utm)
        if full_polygon.is_empty:
            continue
        if full_polygon.area / SQUARE_METERS_PER_ACRE < min_water_zone_area_acres:
            continue

        # Connected greedy growth to target (see _grow_zone_cells()). The
        # result is a single 4-connected component; a cluster at or below
        # target grows to its whole self (it is itself 4-connected).
        grown_cells = _grow_zone_cells(cluster_cells, flow_accumulation_cells, target_cell_count)

        sub_mask = np.zeros(grid_shape, dtype=bool)
        for r, c in grown_cells:
            sub_mask[r, c] = True

        polygon_utm = cell_union_footprint(dem, sub_mask).intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        representative_elevation_m = float(np.median([array[r, c] for r, c in grown_cells]))
        representative_point = polygon_utm.centroid

        production_area_relationships = _zone_production_area_relationships(
            representative_point,
            representative_elevation_m,
            production_areas,
            max_service_distance_meters,
        )
        if not production_area_relationships:
            continue

        post_growth_sum = float(sum(flow_accumulation_cells[r, c] for r, c in grown_cells))

        contributing_area_cells = float(np.median([flow_accumulation_cells[r, c] for r, c in grown_cells]))
        cluster_slopes = [
            float(slope_pct_grid[r, c]) for r, c in grown_cells if not np.isnan(slope_pct_grid[r, c])
        ]
        slope_pct = float(np.median(cluster_slopes)) if cluster_slopes else 0.0

        candidates.append(
            {
                "post_growth_sum": post_growth_sum,
                "tiebreak": (representative_point.x, representative_point.y),
                "cells": grown_cells,
                "sub_mask": sub_mask,
                "polygon_utm": polygon_utm,
                "representative_elevation_m": representative_elevation_m,
                "production_area_relationships": production_area_relationships,
                "contributing_area_cells": contributing_area_cells,
                "slope_pct": slope_pct,
            }
        )

    if not candidates:
        return []

    # Select ONE candidate: the highest post-growth summed flow accumulation
    # (see this function's docstring for why this happens AFTER growth).
    winner = max(candidates, key=lambda cand: (cand["post_growth_sum"], cand["tiebreak"]))

    polygon_utm = winner["polygon_utm"]
    render_fill_polygon_utm = _render_opening(
        winner["sub_mask"], winner["cells"], grid_shape, dem, polygon_utm
    )
    # Invariant: render_fill_polygon_utm is a subset of polygon_utm (the
    # opening is clipped to it, so this holds by construction) -- assert and
    # raise on violation, matching production_area.cluster_and_gate()'s
    # hard-containment discipline.
    if render_fill_polygon_utm.area > polygon_utm.area * (1 + 1e-9) + 1e-6:
        raise ValueError(
            "find_candidate_zones: render_fill_polygon_utm.area "
            f"({render_fill_polygon_utm.area:.6f} m^2) exceeds polygon_utm.area "
            f"({polygon_utm.area:.6f} m^2) -- the opening's clip to polygon_utm must keep the "
            "drawn fill within the real cell-gated, boundary-clipped footprint."
        )

    geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))
    render_fill_geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(render_fill_polygon_utm))
    relationships = winner["production_area_relationships"]

    zone = {
        "id": 0,
        "served_production_area_ids": sorted(r["production_area_id"] for r in relationships),
        "polygon_utm": polygon_utm,
        "geometry_wgs84": geometry_wgs84,
        "render_fill_polygon_utm": render_fill_polygon_utm,
        "render_fill_geometry_wgs84": render_fill_geometry_wgs84,
        "cells": winner["cells"],
        "production_area_relationships": relationships,
        "primary_production_area_relationship": relationships[0],
        "contributing_area_cells": round(winner["contributing_area_cells"], 2),
        "slope_pct": round(winner["slope_pct"], 2),
        "representative_elevation_m": winner["representative_elevation_m"],
    }
    return [zone]


def _render_opening(sub_mask, cells, grid_shape, dem, polygon_utm):
    """
    Bounded morphological OPENING of the zone's own cell mask, clipped to
    polygon_utm -- the display fill. Disc erode-then-dilate at
    WATER_ZONE_RENDER_OPENING_RADIUS_METERS with NO lead erode (a ~0.5-acre
    zone cannot afford an extra cell off every edge). Same construction
    production_area.cluster_and_gate() uses for its own render fill, minus
    the lead erode and at a much smaller radius.

    The opening softens the blocky cell-union edge and trims single-cell
    protrusions; it can also sever a genuinely too-narrow pinch, so the
    result may be a MultiPolygon (acceptable). A zone thinner than the
    opening radius throughout erodes to nothing -- in that case fall back to
    polygon_utm (non-empty) and log once.
    """
    radius_cells = waist_erosion_radius_cells(dem, WATER_ZONE_RENDER_OPENING_RADIUS_METERS)
    opened = binary_dilate(
        eroded_cell_mask(cells, grid_shape, dem, WATER_ZONE_RENDER_OPENING_RADIUS_METERS, element="disc"),
        radius_cells,
        element="disc",
    )
    if opened.any():
        return cell_union_footprint(dem, opened).intersection(polygon_utm)

    _LOGGER.warning(
        "find_candidate_zones: the WATER_ZONE_RENDER_OPENING_RADIUS_METERS=%.1fm opening eroded a "
        "%d-cell zone to nothing; render_fill_polygon_utm falling back to polygon_utm.",
        WATER_ZONE_RENDER_OPENING_RADIUS_METERS,
        len(cells),
    )
    return polygon_utm


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
            },
        )
        for z in zones
    ]
    return make_feature_collection(features)


# =====================================================================
# NARRATIVE DATA -- report-facing, FINAL values only
# =====================================================================
# Everything below exists to answer THREE report questions about this
# module's deliverable, and nothing else:
#
#   1. WHERE is the water-system survey area on the map?
#   2. WHY is this area conducive to a water system?
#   3. HOW does it serve the farm?
#
# The same two hard rules production_area_ceiling.py's narrative block
# established govern every value here:
#
#   1. FINAL. The consumer must never convert, calculate, or relate two
#      values to get a third. Imperial at this boundary (acres, feet --
#      never metres or cell counts); slope/gradient in percent; position
#      as a compass word, not coordinates; everything rounded to 1
#      decimal place, because the precision emitted is the precision
#      narrated.
#   2. DERIVED, NEVER RECOMPUTED. Every figure is read off the zone dict
#      find_candidate_zones() already returns -- no gate, growth pass, or
#      selection is re-run to report on itself. Two narrow, documented
#      exceptions, same class as production_area_ceiling.py's own
#      _on_parcel_cell_mask() carve-out: the parcel-wide elevation range
#      (one vectorised containment test no pipeline step computes, needed
#      to place the zone as upper/lower ground) and the centroid bearing
#      (trivial arithmetic on footprints already in hand).
#
# The output is plain JSON: numbers, booleans, strings, dicts, lists.
# json.dumps() must work on it with no custom encoder.
#
# UNAVAILABLE IS None, NEVER 0.0. A gradient of 0.0 at distance 0 is a
# div-by-zero placeholder, not a measured level grade (the same live bug
# water_suitability._gravity_feed_factor() exists to sidestep) -- it is
# emitted as None here so a narrative cannot read it as a measurement.
#
# NO REASON STRINGS. This block emits values; the report writes prose --
# same division of labor as production_area_ceiling.build_narrative_data().

# 8-point compass words for position_in_parcel -- whole words, because this
# feeds narrative prose directly. Deliberately a separate tuple from
# production_area_ceiling.py's own private _COMPASS_WORDS (same "constants
# stay separate even when identical" convention this module already applies
# to its buffer distances).
_COMPASS_WORDS = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
)

# A zone whose centroid sits within this fraction of the parcel's
# equivalent-circle radius of the parcel's own centroid reads as "center"
# rather than a compass direction -- naming a bearing for a near-central
# offset would narrate precision the position doesn't have. The
# equivalent-circle radius (sqrt(area/pi)) makes the test scale with the
# parcel rather than with any fixed distance. CONFIGURABLE.
_CENTER_POSITION_MAX_OFFSET_FRACTION = 0.2


def _round1(value):
    """1 decimal place, or None passed straight through -- the single
    rounding boundary for this whole block. None means 'not known', and
    must never be silently rounded into a 0.0 that reads as a
    measurement."""
    return None if value is None else round(float(value), 1)


def _feet(meters):
    """Metres to feet at this block's own rounding boundary, None passed
    straight through -- the metric-to-imperial conversion happens HERE, in
    the module, never downstream in the report."""
    return None if meters is None else round(float(meters) / METERS_PER_FOOT, 1)


def _parcel_elevation_range(dem: dict, boundary_polygon_utm: Polygon):
    """
    (low, high) elevation across DEM cells whose CENTER falls inside the
    real parcel boundary, or None on a parcel with no relief at all (where
    "upper" and "lower" ground mean nothing). This is one of the two
    values in this block no pipeline step already computes -- one
    vectorised shapely.contains_xy() call over the whole grid, the same
    cell-center convention and same documented exception
    production_area_ceiling.py's narrative block already makes.
    """
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    col_centers = dem["origin_x"] + (np.arange(cols) + 0.5) * px
    row_centers = dem["origin_y"] - (np.arange(rows) + 0.5) * py
    xs, ys = np.meshgrid(col_centers, row_centers)
    on_parcel = np.asarray(contains_xy(boundary_polygon_utm, xs, ys), dtype=bool)
    elevations = dem["array"][on_parcel]
    elevations = elevations[~np.isnan(elevations)]
    if elevations.size and float(elevations.max()) > float(elevations.min()):
        return float(elevations.min()), float(elevations.max())
    return None


def _position_in_parcel(zone_polygon_utm, boundary_polygon_utm: Polygon) -> str:
    """
    Where the zone sits within the parcel, as an 8-point compass word (or
    "center") -- the bearing from the parcel's own centroid to the zone's
    footprint centroid, the same centroid find_candidate_zones() already
    used as the zone's representative point. UTM axes are +x east / +y
    north, so atan2(dx, dy) IS a compass bearing (0 = north, clockwise).
    """
    parcel_centroid = boundary_polygon_utm.centroid
    zone_centroid = zone_polygon_utm.centroid
    dx = zone_centroid.x - parcel_centroid.x
    dy = zone_centroid.y - parcel_centroid.y
    equivalent_radius = math.sqrt(boundary_polygon_utm.area / math.pi)
    if equivalent_radius <= 0 or math.hypot(dx, dy) <= equivalent_radius * _CENTER_POSITION_MAX_OFFSET_FRACTION:
        return "center"
    bearing_deg = math.degrees(math.atan2(dx, dy)) % 360.0
    return _COMPASS_WORDS[int(round(bearing_deg / 45.0)) % 8]


def _relationship_narrative(relationship: dict) -> dict:
    """
    One production-area relationship, restated in this block's own FINAL
    units -- read off the already-computed (and already-rounded)
    production_area_relationships entry, never re-measured.

    can_gravity_feed is the narrative reading of above_production_area: a
    zone sitting above the ground it serves can deliver water downhill by
    gravity; one sitting below would need a pump -- a real cost/maintenance
    tradeoff, not a defect (see module docstring's "gravity is a
    preference, not a gate" framing).

    gradient_pct is None -- not 0.0 -- when distance is 0: rise-over-run is
    mathematically undefined with no run, and the raw relationship's 0.0
    there is a div-by-zero placeholder a narrative would misread as
    measured level ground (the exact bug water_suitability.
    _gravity_feed_factor() documents). The real elevation differential
    still carries the signal in that case.
    """
    distance_m = float(relationship["distance_m"])
    return {
        "production_area_id": int(relationship["production_area_id"]),
        "can_gravity_feed": bool(relationship["above_production_area"]),
        "elevation_differential_ft": _feet(relationship["elevation_differential_m"]),
        "distance_ft": _feet(distance_m),
        "gradient_pct": None if distance_m == 0 else _round1(relationship["gradient_pct"]),
    }


def build_narrative_data(
    zones: list[dict],
    dem: dict,
    boundary_polygon_utm: Polygon,
    production_area_count: int,
    canopy_data_available: bool,
    road_data_available: bool,
    contributing_area_ceiling_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    target_acres: float = WATER_ZONE_TARGET_ACRES,
) -> dict:
    """
    The 'narrative_data' block identify_water_system_candidate_zones()
    attaches to its result -- pre-computed, FINAL, JSON-serialisable
    values answering the three report questions in this section's header
    comment. Data only: no prose, no interpretation. zones is
    find_candidate_zones()'s own return value, unread beyond its fields --
    at most one zone by design, so this block describes the single winner
    (comparison-level content against unreturned runner-up clusters is
    deliberately absent: only the winner's own package leaves this
    module, per the narrative_data convention's winner-only nuance).

    canopy_data_available / road_data_available say whether each optional
    exclusion gate genuinely ran on the path that produced `zones` --
    identify_water_system_candidate_zones() passes True for canopy always
    (its canopy gate is fetch-or-raise, so any result it returns at all
    was canopy-checked) and True for road only when the road fetch
    actually succeeded. Without these a narrative could claim "verified
    clear of mapped roads" off a run where the road service was down.

    contributing_area_ceiling_acres / target_acres are the values the run
    ACTUALLY used (a zone_kwargs override, or this module's defaults) --
    the caller passes them so this block never guesses at configuration.

    Shape:

        {
          'zone_found': bool,
          'production_area_count': int,   # candidate production areas that
                                          #   existed to serve -- 0 explains
                                          #   a no-zone outcome by itself
          'gates': {
            'canopy_data_available', 'road_data_available',
          },
          'zone': None when zone_found is False, else {
            'area_acres',               # the real, boundary-clipped footprint
            'target_acres',             # the survey-area target it was grown
                                        #   toward; smaller area_acres means
                                        #   the cluster exhausted first --
                                        #   legitimate, not padded
            'location': {               # question 1 -- WHERE on the map
              'position_in_parcel',     #   "center" or an 8-point compass word
              'elevation_percentile_of_parcel',
                                        #   0 = the parcel's lowest ground,
                                        #   100 = its highest; None on a
                                        #   parcel with no relief. Uses the
                                        #   zone's own representative
                                        #   elevation -- the SAME value its
                                        #   gravity relationships were
                                        #   measured from, not a parallel
                                        #   estimate
            },
            'drainage': {               # question 2 -- WHY conducive
              'contributing_area_acres',
                                        #   median contributing area across
                                        #   the zone's own cells, converted
                                        #   from the cell-count figure the
                                        #   zone already carries -- how much
                                        #   watershed drains through this
                                        #   ground
              'contributing_area_ceiling_acres',
                                        #   the absolute eligibility ceiling
                                        #   every member cell cleared (NRCS
                                        #   CPS 378 siltation/peak-flow
                                        #   reasoning -- see MAX_VALLEY_
                                        #   CONTRIBUTING_AREA_ACRES)
              'slope_median_pct',       #   median local slope across the
                                        #   zone's own cells
            },
            'service': {                # question 3 -- HOW it serves the farm
              'served_production_area_count',
              'served_production_area_ids',
              'relationships',          #   one entry per served production
                                        #   area, most gravity-favorable
                                        #   first (same order the zone's own
                                        #   relationships already carry) --
                                        #   see _relationship_narrative()
            },
          },
        }
    """
    data = {
        "zone_found": bool(zones),
        "production_area_count": int(production_area_count),
        "gates": {
            "canopy_data_available": bool(canopy_data_available),
            "road_data_available": bool(road_data_available),
        },
        "zone": None,
    }
    if not zones:
        return data

    zone = zones[0]
    area_per_cell = cell_area_acres(dem)
    relationships = zone["production_area_relationships"]

    elevation_range = _parcel_elevation_range(dem, boundary_polygon_utm)
    if elevation_range is None:
        elevation_percentile = None
    else:
        low, high = elevation_range
        elevation_percentile = _round1(
            max(0.0, min(100.0, (float(zone["representative_elevation_m"]) - low) / (high - low) * 100.0))
        )

    data["zone"] = {
        "area_acres": _round1(zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE),
        "target_acres": _round1(target_acres),
        "location": {
            "position_in_parcel": _position_in_parcel(zone["polygon_utm"], boundary_polygon_utm),
            "elevation_percentile_of_parcel": elevation_percentile,
        },
        "drainage": {
            "contributing_area_acres": _round1(float(zone["contributing_area_cells"]) * area_per_cell),
            "contributing_area_ceiling_acres": _round1(contributing_area_ceiling_acres),
            "slope_median_pct": _round1(zone["slope_pct"]),
        },
        "service": {
            "served_production_area_count": len(relationships),
            "served_production_area_ids": [int(i) for i in zone["served_production_area_ids"]],
            "relationships": [_relationship_narrative(r) for r in relationships],
        },
    }
    return data


def identify_water_system_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    valleys: Optional[list[dict]] = None,
    production_areas: Optional[list[dict]] = None,
    canopy_height: Optional[dict] = None,
    road_exclusion_union_utm=_ROAD_UNION_NOT_SUPPLIED,
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
            'narrative_data': dict,                          # report-facing, FINAL, JSON-serialisable
                                                             #   values — see build_narrative_data()
        }

    'narrative_data' is PURELY ADDITIVE: every other key above, and every
    field on every zone/feature, is byte-identical to what this function
    returned before it existed. It answers three report questions (where
    is the survey area on the map / why is this area conducive to a water
    system / how does it serve the farm) with pre-computed, imperial,
    rounded values a narrative can quote directly -- derived entirely from
    the zone dict find_candidate_zones() already returned, so adding it
    re-runs no gate, no growth pass, and no selection. See
    build_narrative_data()'s own docstring for the field contract.

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

    road_exclusion_union_utm is the road gate's own pre-fetched union
    (farm_roads_data.get_road_exclusion_union_utm()'s output, reprojected
    into dem['crs'] and buffered at ROAD_EXCLUSION_BUFFER_METERS), so a
    caller that already built one for this exact boundary -- build_
    pipeline_context() does, for the exclusion-zones and production gates
    -- does not pay for a third, identical fetch here. Interchangeable
    because that buffer is a SINGLE SHARED CONSTANT read by every consumer,
    not three constants that happen to be equal; test_water_candidate_
    zones.py asserts the producer's and this module's self-fetch defaults
    are the same value so a future divergence fails loudly instead of
    silently substituting a wrong-buffer union.

    UNLIKE dem/boundary_polygon_utm/valleys/production_areas above, its
    "not supplied" is an explicit sentinel rather than None -- a real None
    is get_road_exclusion_union_utm()'s own clean answer ("checked, and
    genuinely no mapped road nearby", the common case) and is REUSED, not
    treated as missing. Same distinction exclusion_zones.identify_
    exclusion_zones() draws for the same union, and for the same reason:
    collapsing the two would reintroduce the redundant fetch on exactly
    the parcels the pass-through exists to spare.
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

    if road_exclusion_union_utm is _ROAD_UNION_NOT_SUPPLIED:
        try:
            # No buffer_meters override: the default IS the intended value --
            # farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS, the single shared
            # definition of "how far off an existing road" (see that
            # constant's docstring), same as every other consumer. That single
            # shared definition is also what makes the override above safe: a
            # caller-supplied union is interchangeable with this one because
            # both are built at the same buffer BY DEFINITION, which
            # test_water_candidate_zones.py asserts against the two signature
            # defaults rather than taking on trust.
            road_exclusion_union_utm = _fetch_road_exclusion_union_utm(boundary_coordinates, dem)
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
        "narrative_data": build_narrative_data(
            zones,
            dem,
            boundary_polygon_utm,
            production_area_count=len(production_areas),
            # Canopy is fetch-or-raise on this path (get_required_tree_root_
            # zone_mask_utm() above), so any result returned at all was
            # canopy-checked; the road fetch degrades gracefully, so its
            # flag reports whether the check genuinely ran.
            canopy_data_available=True,
            road_data_available=road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED,
            # The values this RUN actually used -- a zone_kwargs override,
            # or this module's defaults -- so the narrative never reports a
            # configured constant a caller overrode away.
            contributing_area_ceiling_acres=zone_kwargs.get(
                "max_valley_contributing_area_acres", MAX_VALLEY_CONTRIBUTING_AREA_ACRES
            ),
            target_acres=zone_kwargs.get("water_zone_target_acres", WATER_ZONE_TARGET_ACRES),
        ),
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
