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
            contributing-area ceiling + on-parcel + service distance +
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
For water zones this is now the PLAIN bounded cell-union footprint
(cell_union_footprint(...).intersection(boundary_polygon_utm)) -- the same
geometry as polygon_utm, NOT a convex hull and NOT a morphological
opening. Production zones use an opening because they are large fields
with a ragged fringe worth trimming; a water zone is at most
WATER_ZONE_TARGET_ACRES, so an opening at any useful radius could erase it
entirely, and the honest, untrimmed footprint is what a reviewer needs to
see before deciding whether it needs generalising at all. The invariant
render_fill_polygon_utm is a subset of polygon_utm is asserted anyway
(trivially true by construction here), so it stays enforced if the
geometry ever changes.
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
    _fetch_road_exclusion_union_utm,
    compute_slope_percent,
    get_required_tree_root_zone_mask_utm,
    identify_production_areas,
    production_areas_to_geojson,
)
from raster_grid import (
    D8_OFFSETS,
    SQUARE_METERS_PER_ACRE,
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
# Every surviving cluster is greedily trimmed (lowest flow accumulation
# first) down to at or below this size before one candidate is selected.
# CONFIGURABLE.
WATER_ZONE_TARGET_ACRES = 0.5

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


def compute_water_eligible_cells(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    max_valley_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
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

      3. It FAILS the service-distance gate: it is NOT within
         max_service_distance_meters of any production area's polygon_utm,
         OR it is within min_service_distance_meters of the nearest one
         while NOT already inside/touching that patch (distance == 0).
         Real bug, found live and fixed for the old
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
    'cells' -- the zone's own post-trim member (row, col) cells, not
    refetched or reclassified from the raw DEM here -- and
    'primary_production_area_relationship', to identify which production
    area to measure against without re-deriving it).

    NOTE: as of the water-zone-selection rebuild this function is dead on
    the real pipeline path -- find_candidate_zones() now trims every zone
    to at most WATER_ZONE_TARGET_ACRES (0.5), which is below
    WATER_ZONE_SUBAREA_TRIGGER_ACRES (1.0), so the trigger check below
    always returns None. It is retained (investigate-only in this branch,
    not removed) and still callable directly for tests.

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
    max_valley_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
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
    now use: CLUSTER -> greedy TRIM every cluster to target -> select ONE
    candidate -> plain bounded footprint. Concretely:

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

      3. Greedily TRIM every surviving cluster down to
         water_zone_target_acres by keeping only its highest-flow-
         accumulation cells (equivalently: remove cells lowest-accumulation
         first until at or below target). Implemented as "sort by
         accumulation and take the top N cells" where N is the number of
         whole cells that fit in the target area -- equivalent to and
         cheaper than iterative removal. A cluster already at or below the
         target passes through untrimmed; a cluster between the floor and
         the target is legitimate and is NOT padded. Connectivity is NOT
         enforced during the trim: if the retained cells form two disjoint
         pieces within the target area, that is an accepted, honest
         outcome.

      4. Select ONE candidate -- the cluster with the highest TOTAL (sum)
         flow accumulation across its own POST-TRIM cells. The ordering is
         deliberate: ranking before the trim would let a sprawling,
         low-accumulation cluster win on size alone (sum scales with cell
         count), so every cluster is trimmed to its own best target-sized
         area first, making the sums comparable -- the sum then answers
         "whose best target-acre area carries the most drainage?" A known,
         accepted consequence: a cluster between the floor and the target
         has fewer cells, so its sum is lower and it generally loses to a
         cluster that can fill the full target -- intended, since a
         full-target survey area is a better deliverable than an undersized
         one even when the small one's individual cells score well. This
         branch returns that single zone (or [] if nothing qualifies); a
         second-pass candidate is deliberately deferred.

    Scoring is WHOLE-ZONE, computed once for the selected cluster, not per
    cell and not aggregated from per-cell tags: a representative elevation
    (median of the cluster's own post-trim member cells' elevations -- same
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
                # The PLAIN bounded cell-union footprint
                # (cell_union_footprint(...).intersection(boundary)) -- the
                # SAME geometry as polygon_utm, NOT a convex hull and NOT a
                # morphological opening (see module docstring for why water
                # zones keep the honest, untrimmed footprint). Asserted to
                # be a subset of polygon_utm. Still carried as a separate
                # field so render_layout_map.py's polygon_utm/render_fill
                # pairing is unchanged.
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
            'optimal_subarea_polygon_utm': None,
            'optimal_subarea_geometry_wgs84': None,
            'optimal_subarea_acres': None,
                # select_optimal_survey_subarea()'s own output, attached to
                # every zone (always present, never a missing key). Since
                # every zone is now trimmed to at most WATER_ZONE_TARGET_
                # ACRES (0.5), which is below WATER_ZONE_SUBAREA_TRIGGER_
                # ACRES (1.0), select_optimal_survey_subarea() always
                # returns None on this path -- these fields are effectively
                # always None now. The function and constants are retained
                # (investigate-only in this branch), not removed.
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
        min_service_distance_meters,
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
    # Whole-cell target: keep the N highest-accumulation cells that fit at
    # or below the target area. floor() guarantees N * area_per_cell <=
    # target; max(1, ...) keeps at least one cell for a tiny target.
    target_cell_count = max(1, int(math.floor(water_zone_target_acres / area_per_cell + 1e-9)))

    # Build every surviving-and-trimmed cluster's zone dict, then select
    # the single one with the highest POST-TRIM summed flow accumulation.
    candidates = []  # (post_trim_sum, tiebreak, zone_dict)
    for component_id in range(num_components):
        cluster_mask = labels == component_id
        cluster_cells = [(int(r), int(c)) for r, c in np.argwhere(cluster_mask)]
        if not cluster_cells:
            continue

        # Cluster-size noise filter on the FULL clipped cluster, BEFORE any
        # trim (the direct analogue of production's MIN_PRODUCTION_AREA_
        # ACRES). A cluster between this floor and the target survives and
        # is not padded.
        full_mask = np.zeros(eligible_mask.shape, dtype=bool)
        for r, c in cluster_cells:
            full_mask[r, c] = True
        full_polygon = cell_union_footprint(dem, full_mask).intersection(boundary_polygon_utm)
        if full_polygon.is_empty:
            continue
        if full_polygon.area / SQUARE_METERS_PER_ACRE < min_water_zone_area_acres:
            continue

        # Greedy trim to target: keep the top-N cells by flow accumulation.
        # Equivalent to iterative "remove lowest-accumulation first until at
        # or below target." Connectivity is intentionally NOT enforced. Ties
        # broken by (row, col) for determinism.
        if len(cluster_cells) > target_cell_count:
            ordered = sorted(
                cluster_cells,
                key=lambda rc: (float(flow_accumulation_cells[rc[0], rc[1]]), rc[0], rc[1]),
            )
            trimmed_cells = ordered[-target_cell_count:]
        else:
            trimmed_cells = cluster_cells

        sub_mask = np.zeros(eligible_mask.shape, dtype=bool)
        for r, c in trimmed_cells:
            sub_mask[r, c] = True

        footprint = cell_union_footprint(dem, sub_mask)
        polygon_utm = footprint.intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        representative_elevation_m = float(np.median([array[r, c] for r, c in trimmed_cells]))
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

        post_trim_sum = float(sum(flow_accumulation_cells[r, c] for r, c in trimmed_cells))

        contributing_area_cells = float(np.median([flow_accumulation_cells[r, c] for r, c in trimmed_cells]))
        cluster_slopes = [
            float(slope_pct_grid[r, c]) for r, c in trimmed_cells if not np.isnan(slope_pct_grid[r, c])
        ]
        slope_pct = float(np.median(cluster_slopes)) if cluster_slopes else 0.0

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

        # render_fill_polygon_utm: the PLAIN bounded cell-union footprint,
        # clipped to boundary_polygon_utm -- the same geometry as
        # polygon_utm, NOT a convex hull and NOT a morphological opening.
        # Water zones are at most WATER_ZONE_TARGET_ACRES, so an opening at
        # any useful radius could erase them; the honest, untrimmed
        # footprint is what a reviewer needs to see first. See module
        # docstring.
        render_fill_polygon_utm = cell_union_footprint(dem, sub_mask).intersection(boundary_polygon_utm)
        # Invariant: render_fill_polygon_utm is a subset of polygon_utm.
        # Trivially true by construction here (identical geometry), but
        # asserted anyway so it is enforced if the geometry ever changes --
        # matching production_area.cluster_and_gate()'s hard-containment
        # discipline.
        if render_fill_polygon_utm.area > polygon_utm.area * (1 + 1e-9) + 1e-6:
            raise ValueError(
                "find_candidate_zones: render_fill_polygon_utm.area "
                f"({render_fill_polygon_utm.area:.6f} m^2) exceeds polygon_utm.area "
                f"({polygon_utm.area:.6f} m^2) -- the bounded footprint must never claim "
                "ground outside the real cell-gated, boundary-clipped footprint."
            )
        render_fill_geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(render_fill_polygon_utm))

        zone = {
            "id": 0,
            "served_production_area_ids": sorted(
                r["production_area_id"] for r in production_area_relationships
            ),
            "polygon_utm": polygon_utm,
            "geometry_wgs84": geometry_wgs84,
            "render_fill_polygon_utm": render_fill_polygon_utm,
            "render_fill_geometry_wgs84": render_fill_geometry_wgs84,
            "cells": trimmed_cells,
            "production_area_relationships": production_area_relationships,
            "primary_production_area_relationship": production_area_relationships[0],
            "contributing_area_cells": round(contributing_area_cells, 2),
            "slope_pct": round(slope_pct, 2),
            # select_optimal_survey_subarea() always returns None now (every
            # zone is at most WATER_ZONE_TARGET_ACRES, below the subarea
            # trigger), so these are always None. The function/constants are
            # retained (investigate-only) rather than removed.
            "optimal_subarea_polygon_utm": None,
            "optimal_subarea_geometry_wgs84": None,
            "optimal_subarea_acres": None,
        }

        # Tiebreak on the representative point so selection is deterministic
        # if two clusters ever tie on post-trim sum.
        candidates.append((post_trim_sum, (representative_point.x, representative_point.y), zone))

    if not candidates:
        return []

    # Select ONE candidate: the cluster with the highest post-trim summed
    # flow accumulation (see this function's docstring for why this happens
    # AFTER the trim, not before).
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [candidates[0][2]]


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
