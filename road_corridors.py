"""
road_corridors.py

Computes a suggested road NETWORK from the DEM, for properties that lack
existing farm road/access data — this REPLACES having Claude infer a
plausible-sounding corridor in prose during report generation
(report_generator.py's Farm Roads step used to do exactly that, with an
explicit "no dedicated access data" disclaimer). Same pattern as
water_candidate_zones.py and solar_suitability.py: the backend computes
real geometry, Claude narrates from the result.

COVERAGE-GREEDY NETWORK ROUTING (this module's current design, replacing
an earlier ridge-line-following approach — see git history,
`git log --all -- road_corridors.py`, for that one): a road network is
grown outward from the given REAL anchor point (the real, existing
named-road access point this parcel is already reachable from) one branch
at a time, by road_network_router.route_road_network(), choosing each
extension by how much NEW production acreage it brings within service
range per unit of routing cost, until further road stops being worth
building. The road's terminus (or termini — a network can have several
branches) is an OUTPUT of the algorithm, not something this module
searches a landform for in advance. See road_network_router.py's own
module docstring for the full algorithm; build_road_network() below is
the thin wrapper that turns this module's own inputs (production areas,
the union of the selected water zones, the floodplain cost-penalty union)
into the cost surface and demand mask that router needs, then turns its
per-branch cell output back into real geometry.

ONE NETWORK PER CALL, AND THE BRANCHES ARE NOT ALTERNATIVES. identify_
road_corridor_candidates() is named like its siblings but returns ONE
road_network; the branches inside it form a tree (each carries
joins_branch_index, and newly_served_acres is computed given the branches
already placed). On the interactive path the CANDIDATES are therefore
networks, one per access point the user tries -- step_registry.py's roads
entry declares that (Accumulation) and step_orchestrator.py keeps the
networks side by side. Each call routes independently: the router works on
a fresh copy of the cost raster, so a network generated earlier in the
session is never seen as existing infrastructure by a later one.

Pipeline (build_road_network()):

  1. slope_pct: compute_slope_and_aspect(dem) if not already supplied by
     the caller.
  2. tpi: topographic_position.compute_tpi(dem) if not already supplied —
     a ridge-preference cost discount/premium on road_cost_path.
     build_cost_raster()'s own base travel term (cheaper on crests/spurs,
     costlier in hollows/draws), NOT the same thing as the old ridge-
     detection design's own "does this cell sit on a ridge at all"
     boolean test.
  3. HARD exclusion mask: the parcel boundary (off-parcel cells) + the
     UNION OF THE SELECTED water-system zones, buffered
     (POND_ZONE_EXCLUSION_BUFFER_METERS) — via the existing
     _build_exclusion_cell_mask(). Production is deliberately NOT part of
     this mask anymore (see step 5) — that's the whole point of this
     rewrite: a road exists to SERVE production land, not to avoid it.
  4. production_mask via the existing _build_production_cell_mask(). This
     one array now serves TWO roles: a SOFT, proportionally-costlier
     traversal term road_cost_path.build_cost_raster() applies, AND the
     router's own demand_mask — the real acreage this network is trying
     to bring within service_radius_meters. Building it once and reusing
     it for both avoids computing the same on-parcel/inside-production
     cell test twice.
  5. floodplain_mask via the same _build_production_cell_mask(), reused
     against hydric_floodplain_union instead of a production union — a
     SOFT flat cost penalty, unchanged in spirit from before this
     rewrite.
  6. cost_raster = road_cost_path.build_cost_raster(), now passing tpi,
     production_mask, impassable_grade_pct=MAX_ROAD_GRADE_PCT, and the
     caller-supplied canopy_mask (a SOFT woody-vegetation crossing penalty
     — built and gracefully degraded upstream in identify_road_corridor_
     candidates(), passed straight through here like slope_pct/tpi) —
     grade is a genuine HARD ceiling again under this design (see that
     constant's own comment), not merely an unbounded soft penalty.
  7. anchor_cell via the existing _snap_anchor_to_eligible_cell() — the
     empty-network shape (see below) is returned if the anchor snaps to
     no eligible cell at all.
  8. water_target_cells: cells immediately outside the buffered pond
     exclusion (one cell ring, via raster_grid.binary_dilate()) that are
     still finite-cost — see build_road_network()'s own body for why
     targets have to sit just outside the buffer, not inside it, and why
     an empty list (no water zone at all) is the correct input when
     nothing was selected.
  9. road_network_router.route_road_network() grows the network; each
     returned branch's own raw cell path is turned back into real
     geometry (points_xyz, line_utm, geometry_wgs84,
     cell_footprint_polygon_utm) plus per-branch floodplain/production-
     crossing diagnostics.
 10. WHATEVER THE ROUTER BUILT IS KEPT. There is no length floor: a
     network the router judged worth building is not thrown away here for
     being short. The router's own per-acre stopping rule already decides
     what is worth building, and a second, terrain-blind rule on the
     summed total could only discard a network that rule had accepted --
     silently, and for a reason ("it is short") that is not itself a
     reason a road is not worth having.

Constraint stack, current design:
  - HARD (np.inf in cost_raster, a branch genuinely cannot cross it at
    all, road_cost_path.build_cost_raster()'s own excluded_mask/
    impassable_grade_pct arguments):
      - outside the SELECTED water-system zones, buffered
        (POND_ZONE_EXCLUSION_BUFFER_METERS) — routing on the uphill side
        of a pond/dam, not across its face or catchment inlet.

        SUPERSEDED PRODUCT DECISION, recorded because these lines used to
        state the opposite: this was "the one rank-1 zone this property's
        own water-suitability scoring actually picked". The water step is
        now SELECT-ONLY and MULTI-SELECT — the user may choose any number
        of survey zones, across both survey types, or none — and all of
        them are claimed ground. `selected_water_zone` therefore carries
        ONE zone-shaped value whose render_fill_polygon_utm is the UNION
        of the selection (wire_translation.water_zone_union()), and
        nothing below changes: pond_union buffers that one geometry
        exactly as it buffered one zone's.

        ONE CONSEQUENCE IS REPORTED AND NOT FIXED HERE. The same prepared
        object also derives water_target_cells (the one-cell ring just
        outside the buffered exclusion), and road_network_router.py tries
        the water spur ONCE, taking the cheapest reachable target under
        MAX_WATER_SPUR_METERS. Under a union that ring surrounds ALL the
        selected zones, so selecting three zones still gets ONE spur, to
        whichever is cheapest. That is accepted for now — but
        narrative_data's `reaches_water_zone` is a BOOLEAN and cannot say
        WHICH zone was reached, so the report can no longer name the
        served site. It surfaces at the roads step.
      - the parcel boundary itself (off-parcel cells).
      - grade above MAX_ROAD_GRADE_PCT — a genuine ceiling now (see that
        constant's own comment for why this changed).
  - SOFT (a finite cost penalty in cost_raster, a branch CAN still choose
    to pay it):
      - production zone(s) (production_area_ceiling.py's own optimized/
        final geometry) — proportionally costlier
        (road_cost_path.PRODUCTION_TRAVERSAL_COST_MULTIPLIER), not
        excluded; the SAME cells also define what counts as "demand" the
        router is trying to serve in the first place (see step 4 above).
      - floodplain/hydric ground, sourced from real NHD stream/water-body
        buffers + SSURGO hydric soil polygons (NOT inferred from DEM
        elevation alone) — falls back to a buffer around delineated
        valley lines only if neither NHD nor SSURGO data is reachable,
        and flags that fallback explicitly in confidence_notes. A flat
        additive penalty (road_cost_path.FLOODPLAIN_CROSSING_COST_PENALTY).
      - canopy/woody vegetation, sourced from USGS 3DEP lidar HAG
        coverage (production_area.get_required_tree_root_zone_mask_utm() at
        a 0.0m buffer — RAW canopy cells, not the +TREE_ROOT_ZONE_BUFFER_
        METERS root-protection dilation production/solar apply). A flat
        additive penalty (road_cost_path.CANOPY_CROSSING_COST_PENALTY).
        Unlike production/solar, where a canopy outage is a HARD failure,
        this term DEGRADES GRACEFULLY — a canopy outage simply drops the
        term (see identify_road_corridor_candidates()), same as every other
        real-data fetch in this module.
      - TPI ridge preference — a discount/premium on the base travel cost
        only (road_cost_path.TPI_PREFERENCE_STRENGTH), never the grade,
        floodplain, or canopy terms.

Erosion-prone soil (SSURGO K-factor) is deliberately NOT part of this
module's constraint stack at all, hard or soft — this pipeline's own
KSOP (Keyline Scale of Permanence) ordering puts Soil at step 8, well
below Farm Roads at step 4; scoring a step-4 feature against step-8 data
inverted that ordering, so the erosion-avoidance preference this module
used to carry has been removed outright, not just softened further.

build_road_network() is the pure geometric core — see
water_candidate_zones.py's and solar_suitability.py's docstrings for why
this split matters (independently testable against a synthetic DEM,
without any of the several real network fetches this feature depends on).
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union
from shapely.prepared import prep
import requests

from dem_data import get_dem_for_boundary
from hydrology_data import get_water_features_for_boundary
from production_area import get_required_tree_root_zone_mask_utm
from production_area_ceiling import identify_optimized_production_areas
from raster_grid import binary_dilate, cell_area_acres, cell_union_footprint, pixel_center_xy
# _BASE_TRAVEL_COST is PRIVATE to road_cost_path.py, and is imported here
# deliberately rather than restated as a local literal: it is the unit the
# cost raster's every other term is expressed in (see build_cost_raster()),
# and _terrain_quality_score() below normalizes against exactly it. A copy
# of the number here would go stale the moment road_cost_path re-scales its
# own surface, and the score would keep reporting against a base the cost
# raster no longer uses -- which is to say it would be meaningless while
# still looking like a measurement. Reaching for the private name keeps the
# two locked together.
from road_cost_path import _BASE_TRAVEL_COST, build_cost_raster, path_cells_to_points_xyz
from road_network_router import (
    MAX_ROAD_METERS_PER_SERVED_ACRE,
    MAX_WATER_SPUR_METERS,
    PRODUCTION_SERVICE_RADIUS_METERS,
    route_road_network,
)
from soil_data import (
    coordinates_to_wkt_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    hydric_disqualifying_mukeys,
)
from terrain_metrics import compute_slope_and_aspect
from topographic_position import compute_tpi
from valley_delineation import delineate_valleys
from water_suitability import NO_WATER_ZONE, fetch_and_select_optimal_water_zone

METERS_PER_FOOT = 0.3048

# The EXPLICIT "the roads step already ran and selected NO road" answer, for
# forwarding selected_road_corridor between pipeline steps. The exact
# counterpart of water_suitability.NO_WATER_ZONE, for the exact reason that
# constant exists (see its own docstring): every selected_road_corridor
# override parameter downstream (tree_zone_candidates.identify_tree_zone_
# candidates(), solar_suitability.identify_solar_candidate_zones(),
# fencing.identify_fencing()) treats None as "not supplied" (this pipeline's
# standard None-as-sentinel override convention) and reacts by re-running
# this ENTIRE module's routing pass as its self-compute fallback -- a full
# cost-surface build and a greedy network route, from an anchor the caller
# may not even hold -- and the network it grows is then hard-excluded from
# tree siting and used as the structure's road source, silently overriding
# the decision it was standing in for.
#
# The BATCH path never needed this: pipeline_context.py forwards
# build_road_network()'s full dict, branches=[] and all, NEVER None, so an
# empty routing result already travels as a real value there. The
# INTERACTIVE path does need it. A roads commit of ZERO features is a
# deliberate "no road on this property" (step_registry.CommitContract.
# min_features), and the committed value behind it is an empty LIST of
# networks, which no `selected_road_corridor=` consumer takes -- each takes
# ONE network dict. Forwarding None in its place is the silent override
# above; forwarding an empty-network dict would be a value indistinguishable
# from a routing pass that ran and found nothing, and one that every
# consumer would try to read fields off. So, as for water, an explicit
# constant IS the value: the step registry declares it as the roads consumes
# edge's `empty_commit` and forwards it, by identity, whenever the roads
# commit is empty. Each accepting entry point normalizes it back to None
# internally (its downstream code keeps None's existing "no road" meaning)
# and skips the self-compute. A caller-supplied bare None still self-computes
# exactly as before -- the existing override convention is untouched.
#
# IT IS TRUTHY, AND THAT IS THE RIGHT SHAPE. object() is what NO_WATER_ZONE
# is, so the two are compared, normalized and declared identically. A falsy
# sentinel was considered and rejected: it would pass silently through every
# `if selected_road_corridor:` guard downstream without any consumer having
# to know it exists -- which is exactly the property that lets a consumer
# that was never taught the sentinel keep working by accident until the day
# one of them reads a field off it. A truthy opaque object reaching an
# un-normalizing consumer fails at once, on the first subscript, naming
# the object; that is the loud failure a sentinel owes the code around it.
NO_ROAD_CORRIDOR = object()

# CLIFF cutoff, NOT a buildable-grade limit. Any cell whose slope_pct
# exceeds this is HARD-excluded (road_cost_path.build_cost_raster()'s own
# impassable_grade_pct argument, see build_road_network()) -- but the bar
# is deliberately set at genuine cliff terrain, not at the highest grade a
# road should be built on. Grades between ~10% and 35% are COSTLY but
# PERMITTED: they are handled by build_cost_raster()'s quadratic grade
# penalty (road_cost_path.GRADE_PENALTY_WEIGHT), which already makes a 22%
# pitch cost roughly 7x base, so the router avoids steep ground unless it
# is the only way through -- exactly the behavior a hard wall at a lower
# value overrode. STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT (10.0), NOT
# this constant, remains the figure the narrative flags a route against as
# needing real engineering consideration.
#
# WHY THIS IS 35 AND NOT 15 -- read before re-tightening it. This was
# 15.0 for exactly one branch, wired straight into impassable_grade_pct as
# a hard exclusion. On a real 17.4-acre western Pennsylvania parcel that
# made ALL 993 production cells unreachable from the access point, so the
# router produced ZERO road branches. The cause was NOT broadly steep
# terrain: a NARROW steep band separates the anchor from the production
# ground. Sweeping the ceiling proved it -- reachability was 0/993 at 15%
# AND at 20%, then jumped to 917/993 at 25%, and 25% reaches EXACTLY as
# much as no ceiling at all. The barrier is a thin stripe, not a broadly
# steep parcel; a farm road crossing a short 22% pitch to reach otherwise-
# unreachable fields is a real road (farms cut benches and switchbacks
# across exactly that). A future reader who re-tightens this toward a
# "buildable grade" number will silently reintroduce that 0/993 failure --
# leave the cliff cutoff high and let the quadratic penalty do the
# grade-avoidance work. CONFIGURABLE -- and a deliberately UNVALIDATED
# starting point, not a validated threshold (same caveat every other
# threshold in this pipeline carries): 35.0 is chosen to sit clearly above
# the ~25% the real-parcel sweep showed was needed and clearly below true
# cliff/talus terrain, not tuned against a broad diagnostic sweep.
MAX_ROAD_GRADE_PCT = 35.0

# Above this average grade, a branch is steep enough that confidence_notes
# flags it as needing real engineering consideration (surface material,
# drainage/culverts, water bars) before construction -- not just the
# blanket "topographic suggestion, not a surveyed alignment" caveat every
# branch already carries. Set below MAX_ROAD_GRADE_PCT (the new hard
# ceiling): a branch can still legitimately average anywhere between this
# threshold and that ceiling, and that whole range is where routine grading
# alone isn't enough. CONFIGURABLE.
STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT = 10.0

# Buffer beyond a pond/water-system candidate zone's own footprint -- this
# needs to keep a route off a dam face or catchment inlet, not just the
# pond footprint itself. CONFIGURABLE.
POND_ZONE_EXCLUSION_BUFFER_METERS = 25.0

# Buffer applied to NHD streams/water bodies (and, in the elevation-only
# fallback case, to delineated valley lines) as the floodplain/riparian
# cost-penalty region. 30m (~100ft) is a commonly-used minimal riparian
# buffer rule of thumb, not a site-specific regulatory setback.
# CONFIGURABLE.
FLOODPLAIN_STREAM_BUFFER_METERS = 30.0

# How far past the parcel boundary to still care about a fetched NHD
# stream/water-body's own geometry before it gets buffered into the
# floodplain cost-penalty union. REAL BUG, FOUND LIVE (fixed here):
# hydrology_data.py's NHD query (an ArcGIS `query` operation) returns
# each matching feature's FULL, un-clipped geometry for anything that
# merely intersects the query bounding box -- a long stream or a large
# waterbody that just touches that box can come back with geometry
# extending far past the property, which then got buffered (widening it
# further) and unioned into this mask WHOLESALE. Confirmed live: a
# 33.9-ACRE floodplain/hydric union on a 13.23-acre parcel. Same
# root-cause CATEGORY (a fetch returning geometry far beyond the input
# boundary's actual relevant extent, not clipped to it) as the earlier
# soil_data.get_soil_geometries_for_polygon() bug -- that one's already
# fixed at the SSURGO query itself (real STIntersects + STIntersection
# clipping); NHD's ArcGIS `query` endpoint has no equivalent server-side
# clip parameter here, so this clips CLIENT-SIDE instead, against a
# generous context region around the parcel -- comfortably larger than
# both dem_data.py's own ~100m DEM fetch buffer and
# FLOODPLAIN_STREAM_BUFFER_METERS itself, so clipping here can never cut
# off a stream segment that would otherwise matter to on-parcel routing.
# CONFIGURABLE.
FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS = 200.0

# SECOND real bug, found live AFTER the fetch-context clip above landed:
# that clip is applied to each stream/water-body feature's RAW geometry
# BEFORE it gets buffered by FLOODPLAIN_STREAM_BUFFER_METERS -- nothing
# bounded the geometry AFTER buffering, so the buffer stroke itself (and,
# more significantly, however much of the stream's length survived the
# 200m fetch-context clip) could still produce a final piece extending
# well past any distance actually relevant to this parcel. Confirmed live
# and visually (plotted GeoJSON against the real property): an 11.2-acre
# union where only 0.077 acres (0.6% of the parcel) actually overlapped
# it -- the rest was a long buffered band running along Montour Run,
# entirely on the far side of N Montour Rd from the field, nowhere near
# the parcel. This constant bounds the FINAL unioned/buffered result
# (after FLOODPLAIN_STREAM_BUFFER_METERS is applied) to a region tied to
# the parcel itself, independent of how far the original fetched geometry
# or its buffer stroke reached. Deliberately meaningfully SMALLER than
# FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS (200m) -- that larger buffer's
# job is only to avoid losing a stream segment's geometry at fetch time;
# this one's job is to bound what actually counts as "close enough to this
# parcel to matter." Still meaningfully LARGER than
# FLOODPLAIN_STREAM_BUFFER_METERS (30m) alone, so a stream running close
# to (but not quite touching) the parcel -- genuine near-parcel floodplain
# risk on a differently-shaped property -- doesn't get clipped away just
# because its own 30m buffer stroke doesn't quite reach the boundary.
# CONFIGURABLE.
FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS = 75.0

ROAD_CORRIDOR_CONFIDENCE_NOTES_TEMPLATE = (
    "This is a TOPOGRAPHIC SUGGESTION only, not a surveyed road alignment — "
    "generated by growing a road network outward from the given real "
    "anchor point (road_network_router.route_road_network(), see "
    "road_corridors.py and road_network_router.py), one branch at a time, "
    "over a least-cost terrain surface (road_cost_path.build_cost_raster()) "
    "-- not a civil engineering design. The parcel boundary and a buffered "
    "pond/water-system zone are HARD exclusions no branch can ever cross; "
    f"grade above {MAX_ROAD_GRADE_PCT}% is now HARD-excluded too, not just "
    "penalized. Production land is a SOFT, proportionally-costlier "
    "traversal term a branch may still cross when the alternative costs "
    "more (see properties.crosses_production_zone) -- it's also what this "
    "network exists to SERVE, not merely avoid. Floodplain/hydric ground "
    "is a SOFT flat cost penalty for the same reason (see "
    "properties.crosses_floodplain). {floodplain_crossing_note}"
    "{production_crossing_note}{steep_grade_note}{floodplain_fallback_note}"
    "Treat this as a starting point for a site visit and real survey, not a "
    "construction-ready alignment."
)

FLOODPLAIN_CROSSING_NOTE = (
    "This specific branch DOES cross floodplain/hydric ground — that's intentional/expected under "
    "this model, not a caveat to apologize for; routing found crossing it was still cheaper overall "
    "than detouring around it. "
)

PRODUCTION_CROSSING_NOTE = (
    "This specific branch DOES cross a production zone — that's intentional/expected under this "
    "model, not a caveat to apologize for; production crossing is a soft, proportionally-costlier "
    "traversal term, not a hard exclusion, so a branch can still cross production land when the "
    "alternative route costs more. "
)

# Additive caveat (see STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT above) —
# appended only for branches steep enough that routine grading isn't
# enough; the blanket "topographic suggestion" disclaimer above still
# applies to every branch regardless of grade.
STEEP_GRADE_ENGINEERING_NOTE = (
    "This branch's average grade ({avg_grade_pct}%) is steep enough for an unpaved farm road that "
    "real engineering consideration — surface material, drainage/culverts, water bars — is needed "
    "before construction, not just routine grading: erosion, traction, and washout risk all "
    "increase meaningfully above {threshold_pct:.0f}%, especially under heavy rainfall or "
    "freeze-thaw conditions. "
)

# A real access point is where the property meets a road along its own
# perimeter, not an arbitrary interior or exterior point — see
# validate_access_point_on_boundary() below. A couple of meters covers
# GPS/digitizing slop for a point genuinely picked on the boundary line
# without also accepting a point that's actually well inside or outside
# the parcel.
ACCESS_POINT_BOUNDARY_TOLERANCE_METERS = 3.0


def _utm_epsg_for_lonlat(longitude: float, latitude: float) -> int:
    """
    Same formula as dem_data.py's/fencing.py's own private helpers of the
    same name, duplicated here rather than imported: this validation
    needs a projected meters-based CRS the same way DEM analysis does,
    but doesn't need (and shouldn't require) fetching an entire DEM
    raster just to get one EPSG code.
    """
    zone = int((longitude + 180) // 6) + 1
    return (32600 if latitude >= 0 else 32700) + zone


def _utm_crs_for_boundary(boundary_coordinates: list[tuple[float, float]]) -> str:
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    center_lon = (min(lons) + max(lons)) / 2
    center_lat = (min(lats) + max(lats)) / 2
    return f"EPSG:{_utm_epsg_for_lonlat(center_lon, center_lat)}"


def validate_access_point_on_boundary(
    boundary_coordinates: list[tuple[float, float]],
    anchor_lon_lat: tuple[float, float],
    tolerance_meters: float = ACCESS_POINT_BOUNDARY_TOLERANCE_METERS,
) -> None:
    """
    Raises ValueError unless anchor_lon_lat sits on (or within
    tolerance_meters of) boundary_coordinates' own edge, measured in UTM
    meters — a genuine access point is the spot where the property meets
    a road along its perimeter, not an interior or far-exterior point.
    Fails loud rather than silently accepting (or worse, quietly
    snapping) a routing start point that doesn't correspond to anything
    real on the ground, same "fail loud, don't fake a good result"
    pattern this pipeline's other mandatory gates already use (e.g. the
    canopy-coverage checks).

    Deliberately does NOT require a DEM: this is meant to run as an
    early, fast rejection of malformed input before any of this
    pipeline's real network fetches start, so it derives its own
    lightweight UTM CRS from the boundary's centroid longitude
    (_utm_crs_for_boundary()) rather than requiring a caller to have
    already fetched one.
    """
    utm_crs = _utm_crs_for_boundary(boundary_coordinates)

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        utm_crs,
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    anchor_xs, anchor_ys = warp_transform("EPSG:4326", utm_crs, [anchor_lon_lat[0]], [anchor_lon_lat[1]])
    anchor_point_utm = Point(anchor_xs[0], anchor_ys[0])

    distance_meters = boundary_polygon_utm.exterior.distance(anchor_point_utm)
    if distance_meters > tolerance_meters:
        raise ValueError(
            f"access_point {tuple(anchor_lon_lat)} is {distance_meters:.1f}m from the property "
            f"boundary edge (tolerance is {tolerance_meters}m) -- it must be the point where the "
            "property meets a road along its perimeter, not an interior or far-exterior point."
        )


def _build_exclusion_cell_mask(dem: dict, excluded_prepared, boundary_prepared) -> np.ndarray:
    """Per-cell boolean mask (True = excluded), built once by testing
    every valid cell center against the (prepared, unioned) exclusion
    geometry AND against the real parcel boundary — reused once for the
    whole property rather than re-testing point-in-polygon per candidate
    route.

    A cell outside boundary_prepared is excluded here the same way an
    excluded-zone cell is — this is what keeps generated routes from
    being built out of the DEM's buffered area past the drawn boundary
    (dem_data.py fetches ~100m past the parcel on purpose; without this,
    a route could be built entirely from off-parcel cells)."""
    array = dem["array"]
    rows, cols = array.shape
    excluded = np.zeros((rows, cols), dtype=bool)

    valid = ~np.isnan(array)
    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            point = Point(pixel_center_xy(dem, r, c))
            if not boundary_prepared.contains(point):
                excluded[r, c] = True
            elif excluded_prepared is not None and excluded_prepared.contains(point):
                excluded[r, c] = True
    return excluded


def _build_production_cell_mask(dem: dict, production_prepared, boundary_prepared) -> np.ndarray:
    """Per-cell boolean mask (True = cell center falls inside the given
    prepared geometry AND on-parcel) -- same iteration structure as
    _build_exclusion_cell_mask(), different (and simpler) boundary
    handling: True only when BOTH conditions hold, rather than "excluded
    if either."

    Despite the name (this function's own logic is unchanged from when it
    was first added purely for production-zone masking), build_road_
    network() now reuses it for THREE different geometries: production_
    areas' own render_fill_polygon_utm union (this mask's own cells now
    serve BOTH as a SOFT, proportionally-costlier traversal term in
    cost_raster and as the router's own demand_mask -- see that
    function's own docstring), the floodplain/hydric union (a SOFT cost
    penalty), and the buffered selected-water-zone union (used to derive
    water_target_cells) -- the underlying "is this on-parcel cell inside
    that prepared geometry" test is identical every time, so there's no
    reason to duplicate it under a second name.

    production_prepared may be None (no production zones at all, or --
    when reused for floodplain/water -- no floodplain/water data at all),
    in which case every cell is simply False."""
    array = dem["array"]
    rows, cols = array.shape
    production_mask = np.zeros((rows, cols), dtype=bool)

    if production_prepared is None:
        return production_mask

    valid = ~np.isnan(array)
    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            point = Point(pixel_center_xy(dem, r, c))
            if boundary_prepared.contains(point) and production_prepared.contains(point):
                production_mask[r, c] = True
    return production_mask


def _snap_anchor_to_eligible_cell(
    dem: dict,
    cost_raster: np.ndarray,
    anchor_lon_lat: tuple[float, float],
) -> Optional[tuple[int, int]]:
    """
    Warps the given (lon, lat) anchor point into the DEM's own UTM CRS
    (same warp_transform pattern used throughout this module) and returns
    the nearest cell where np.isfinite(cost_raster[r, c]) -- routing
    starts from wherever eligible ground is actually closest to the given
    anchor, not from the anchor's own raw grid cell (which might itself be
    off-parcel, in a hard-excluded zone, or simply not a valid DEM cell at
    all).

    The anchor's approximate (row, col) is computed as the direct algebraic
    inverse of raster_grid.pixel_center_xy() (solved for row/col given x/y,
    rather than searched for), then the actual nearest ELIGIBLE cell is
    found by comparing every finite-cost cell's own grid distance to that
    point -- the direct inverse alone can legitimately land on a
    hard-excluded cell, so it's only a starting estimate, not the answer.

    Returns None if cost_raster has no finite-cost cell anywhere at all
    (the whole property is hard-excluded) -- a real, reportable "nothing
    to route from" outcome, matching this module's own least_cost_path()-
    style None-on-failure convention, rather than returning a nonsensical
    cell or raising.
    """
    eligible_rc = np.argwhere(np.isfinite(cost_raster))
    if eligible_rc.size == 0:
        return None

    anchor_xs, anchor_ys = warp_transform("EPSG:4326", dem["crs"], [anchor_lon_lat[0]], [anchor_lon_lat[1]])
    anchor_x, anchor_y = anchor_xs[0], anchor_ys[0]

    px, py = dem["resolution_meters"]
    approx_col = (anchor_x - dem["origin_x"]) / px - 0.5
    approx_row = (dem["origin_y"] - anchor_y) / py - 0.5

    distances_sq = (eligible_rc[:, 0] - approx_row) ** 2 + (eligible_rc[:, 1] - approx_col) ** 2
    nearest = eligible_rc[np.argmin(distances_sq)]
    return (int(nearest[0]), int(nearest[1]))


def _grade_stats(points_xyz: list[tuple[float, float, float]]) -> tuple[float, float]:
    grades = []
    for (x1, y1, z1), (x2, y2, z2) in zip(points_xyz, points_xyz[1:]):
        distance = math.hypot(x2 - x1, y2 - y1)
        if distance > 0:
            grades.append(abs(z2 - z1) / distance * 100.0)
    if not grades:
        return 0.0, 0.0
    return float(np.mean(grades)), float(np.std(grades))


def _cell_steep_stats(
    dem: dict, cells: list[tuple[int, int]], slope_pct: np.ndarray
) -> tuple[float, float]:
    """Cell-level steep-section metrics for one branch, computed from the
    per-cell slope raster this same network was routed over -- DELIBERATELY
    different from _grade_stats() above, which averages the centerline's own
    elevation profile. A route can average a gentle 6% overall and still
    contain a single 24% cell where it crosses a narrow pitch; avg_grade_pct
    would hide that, and these two metrics exist precisely to surface it.

    Returns (max_grade_pct, steep_meters):
      - max_grade_pct: the steepest single cell along the branch (the raw
        per-cell slope_pct value, not a segment average).
      - steep_meters: total path length attributable to cells steeper than
        STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT. A segment (the step from
        one ordered cell to the next) contributes its own real ground length
        when the cell it ENTERS exceeds that threshold -- the same
        "cost/length accrues on entering a cell" convention road_cost_path's
        own Dijkstra edge weight uses, so N contiguous steep cells crossed
        straight-through contribute exactly N cells' worth of length.

    Routed cells are always finite-slope (the cost raster hard-excludes
    NaN-slope cells before routing ever reaches them), but NaN is guarded
    anyway rather than allowed to poison the max."""
    px, py = dem["resolution_meters"]
    max_grade_pct = 0.0
    steep_meters = 0.0
    for index, (r, c) in enumerate(cells):
        slope = float(slope_pct[r, c])
        if not math.isnan(slope):
            if slope > max_grade_pct:
                max_grade_pct = slope
            if index > 0 and slope > STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT:
                r0, c0 = cells[index - 1]
                steep_meters += math.hypot((c - c0) * px, (r - r0) * py)
    return max_grade_pct, steep_meters


# The three grounds a road network's crossing length is measured against,
# in the order the panel lists them. Each name is the key used on every
# crossing dict in this module (per branch and network-level alike); the
# panel's own row labels are minted in build_narrative_data(), where
# "production" becomes BLOCK -- the word the interface uses for a
# production zone now. The internal name stays "production" on purpose:
# it names the same mask, the same ids and the same per-branch
# crosses_production_zone boolean the rest of this pipeline already
# carries, and renaming an id to follow a display word is how a wire
# contract and a label drift apart.
_CROSSING_GROUNDS = ("production", "canopy", "floodplain")


def _mask_crossing_meters(
    dem: dict,
    cells: list[tuple[int, int]],
    mask: Optional[np.ndarray],
    already_built: set,
) -> Optional[float]:
    """
    How many real metres of NEW road this branch builds ACROSS the ground
    `mask` marks -- or None when the mask is None, because a ground whose
    data never arrived was not measured and must not report a 0.0 that
    reads as "crossed nothing".

    WHY A LENGTH AND NOT A BOOLEAN. crosses_floodplain/crosses_production_
    zone answer "did it touch that ground at all", and a corridor clipping
    a block's corner for 20 ft and one running 300 ft up its middle answer
    that question identically. They are not the same fact about a road.
    The precedent is commit_validation.crossings() and the trees contract's
    four grounds, which both record the ACREAGE of each crossed ground for
    exactly this reason; a road is a line rather than an area, so the same
    shape is recorded in the unit a line has.

    TWO CONVENTIONS, BOTH BORROWED RATHER THAN INVENTED:

      * COST/LENGTH ACCRUES ON ENTERING A CELL -- the step from one
        ordered cell to the next contributes its own real ground length
        when the cell it ENTERS is in the mask. That is road_cost_path's
        own Dijkstra edge weight convention, and _cell_steep_stats() above
        measures steep_meters the same way, so N contiguous marked cells
        crossed straight through contribute exactly N cells' worth.

      * NEW CONSTRUCTION ONLY -- a cell already belonging to an
        earlier-accepted branch contributes 0, exactly as road_network_
        router._new_length_meters() computes the branch's own
        length_meters. Without it a spur that runs back down the trunk to
        reach its own start would re-charge the trunk's crossings to the
        network total, and the summed crossing length could exceed the
        network's own total_length_meters -- a figure no reader could
        make sense of beside it.

    Cells are ordered and the first is never counted (there is no step
    into it), which is the same index-0 treatment steep_meters gives.
    """
    if mask is None:
        return None
    px, py = dem["resolution_meters"]
    meters = 0.0
    for index in range(1, len(cells)):
        cell = cells[index]
        if cell in already_built or not mask[cell[0], cell[1]]:
            continue
        r0, c0 = cells[index - 1]
        meters += math.hypot((cell[1] - c0) * px, (cell[0] - r0) * py)
    return meters


def _length_weighted_avg_grade_pct(branches: list[dict]) -> float:
    """
    ONE average grade for a whole network: each branch's own centreline
    average weighted by the branch's own length_meters.

    LENGTH-WEIGHTED, NOT A MEAN OF BRANCH AVERAGES, and the difference is
    not cosmetic. A plain mean gives a 40 ft spur the same vote as a 900 ft
    trunk: a network that is 900 ft of 3% trunk plus 40 ft of 20% stub
    averages 11.5% unweighted and 3.7% weighted, and only the second
    describes what driving the network is like. The weighted figure is the
    grade of the average METRE of road, which is the question "what is the
    average grade of this network" actually asks.

    THERE WAS NO NETWORK-LEVEL AVERAGE BEFORE THIS. avg_grade_pct existed
    per branch only; max_grade_pct and steep_meters were the only rollups,
    so nothing had to be un-learned here -- but a consumer reaching for
    "the network's grade" had only the branch list, and the obvious thing
    to do with a branch list is average it.

    Weighted over the branches ACTUALLY EMITTED, whose length_meters are
    the new-construction lengths the router charged, so the denominator is
    a real road length and never double-counts reused cells. A sub-2-cell
    branch dropped before geometry has no centreline and so no grade to
    contribute -- the same exclusion max_grade_pct already makes.

    0.0 for a network with no branches, matching max_grade_pct's own
    default rather than introducing a lone None beside it: network_found
    is the guard that says there is no road, for both.
    """
    total_length = sum(float(b["length_meters"]) for b in branches)
    if total_length <= 0.0:
        return 0.0
    return float(
        sum(float(b["avg_grade_pct"]) * float(b["length_meters"]) for b in branches) / total_length
    )


def _empty_road_network(stop_reason: str, unserved_acres: float = 0.0) -> dict:
    """
    The canonical "no road network at all" shape -- returned (never None,
    never an exception) whenever build_road_network() or identify_road_
    corridor_candidates() can't produce any branches at all, for a reason
    that doesn't come from road_network_router.route_road_network() itself
    (which already returns this same branches=[] shape on its own for its
    own stop_reason values -- see that function's own docstring). Used
    here for the one failure mode that happens BEFORE the router ever
    runs: the anchor snapping to no eligible cell at all. Nothing discards
    a network the router DID build -- see the module docstring's step 10.
    """
    return {
        "branches": [],
        "total_length_meters": 0.0,
        # Uniform with route_road_network()'s own returned shape, which
        # publishes a network-level "total_cost" summed over its surviving
        # branches -- no branches, no accumulated cost. Carried here so
        # every consumer can read the key off either producer without a
        # membership test.
        "total_cost": 0.0,
        "total_served_acres": 0.0,
        "unserved_acres": unserved_acres,
        "stop_reason": stop_reason,
        "max_grade_pct": 0.0,
        # The length-weighted network average (see _length_weighted_avg_
        # grade_pct()) -- 0.0 here for the same reason max_grade_pct above
        # is: no branches, no grade, and network_found is what tells a
        # consumer there is no road rather than a suspiciously level one.
        "avg_grade_pct": 0.0,
        "steep_meters": 0.0,
        # Zero metres of road cross zero metres of anything, on every
        # ground, WITHOUT any of the three masks having to exist -- which
        # is why these are a measured 0.0 rather than the None an
        # unavailable ground earns on a network that was actually routed
        # (see build_road_network()). Same shape as steep_meters beside
        # them: a length summed over branches, and there are none.
        "crossing_meters": {ground: 0.0 for ground in _CROSSING_GROUNDS},
        "cells": [],
        "cell_footprint_polygon_utm": Polygon(),
    }


def build_road_network(
    dem: dict,
    production_areas: list[dict],
    selected_water_zone: Optional[dict],
    boundary_polygon_utm: Polygon,
    anchor_lon_lat: tuple[float, float],
    hydric_floodplain_union=None,
    canopy_mask: Optional[np.ndarray] = None,
    slope_pct: Optional[np.ndarray] = None,
    tpi: Optional[np.ndarray] = None,
    service_radius_meters: float = PRODUCTION_SERVICE_RADIUS_METERS,
    max_meters_per_served_acre: float = MAX_ROAD_METERS_PER_SERVED_ACRE,
    max_water_spur_meters: float = MAX_WATER_SPUR_METERS,
) -> dict:
    """
    Pure geometric core — see module docstring for why this takes
    already-computed inputs (production areas, the selected water ground,
    floodplain cost-penalty union) rather than fetching or delineating
    anything itself.

    selected_water_zone is ONE zone-shaped dict standing for all the water
    ground the user selected -- the union of the committed survey zones on
    the interactive path (wire_translation.water_zone_union()), or a single
    self-computed answer on the batch path -- or None if there is none. It
    is read for exactly one field, 'render_fill_polygon_utm' (the SAME
    optimized/final geometry production_areas below uses, not the raw
    'polygon_utm'), HARD-excluded (buffered by
    POND_ZONE_EXCLUSION_BUFFER_METERS). One field is the whole contract:
    the union carries no id, acreage or elevation, because a union of
    several zones is not itself a zone and a value for any of those would
    be invented.
    production_areas is production_area_ceiling.identify_optimized_
    production_areas()'s own 'scored_patches' -- the OPTIMIZED/final,
    ceiling-trimmed patch shape (each entry carrying
    'render_fill_polygon_utm') -- this SAME geometry now defines both a
    SOFT traversal-cost penalty and road_network_router.route_road_
    network()'s own demand_mask: the acreage this network exists to bring
    within service_radius_meters. hydric_floodplain_union is a shapely
    geometry (or None to skip that penalty entirely) already in
    dem['crs'] -- a SOFT cost penalty.

    canopy_mask is an already-computed boolean woody-vegetation grid (same
    shape as dem['array'], True where a cell should pay the SOFT canopy
    crossing penalty road_cost_path.build_cost_raster() applies), or None
    to skip that penalty entirely. Like slope_pct/tpi it is NOT fetched
    here -- this function stays network-free (see module docstring); the
    canopy fetch (and its graceful degradation to None on a canopy outage)
    lives in identify_road_corridor_candidates(), which builds this mask
    with production_area.get_required_tree_root_zone_mask_utm() at a 0.0m
    buffer (raw canopy cells, not the root-protection dilation production/
    solar use) and passes the result straight through.

    anchor_lon_lat is the single (lon, lat) point the network is grown
    outward FROM (snapped to the nearest eligible cell -- see
    _snap_anchor_to_eligible_cell()).

    boundary_polygon_utm is the hard limit on which DEM cells routing can
    draw from at all, via _build_exclusion_cell_mask() -- the DEM covers
    ~100m past the drawn boundary on purpose (dem_data.py), but network
    geometry must come from on-parcel cells only.

    slope_pct/tpi are optional pre-computed overrides -- when omitted,
    computed once here (compute_slope_and_aspect()/topographic_position.
    compute_tpi(), respectively) rather than fetched or recomputed a
    second time; a caller that already has either (e.g. an upstream
    orchestrator) passes it straight through. tpi is computed against the
    SAME dem passed in -- this never fetches its own DEM.

    service_radius_meters/max_meters_per_served_acre/max_water_spur_meters
    forward straight through to road_network_router.route_road_network()
    -- see that function's own docstring for what each one controls;
    defaults come from road_network_router.py itself.

    Returns:
        {
          "branches": [
            {
              "cells": [(r, c), ...],           # ordered, joint-cell first
              "branch_role": "trunk" | "spur" | "water_spur",
              "branch_index": int,               # this branch's own index into "branches"
              "joins_branch_index": int | None,
              "length_meters": float,             # NEW construction only, see route_road_network()
              "total_cost": float,
              "newly_served_acres": float,        # 0.0 for water_spur
              "points_xyz": [(x, y, elevation_m), ...],
              "line_utm": LineString,             # zero-width, dem['crs']
              "geometry_wgs84": GeoJSON LineString,
              "cell_footprint_polygon_utm": Polygon/MultiPolygon,  # THIS branch's own cells only
              "avg_grade_pct": float,            # centerline-elevation average (see _grade_stats())
              "max_grade_pct": float,            # steepest single CELL (see _cell_steep_stats())
              "steep_meters": float,             # length of this branch's cells above the steep threshold
              "crosses_floodplain": bool,
              "crosses_production_zone": bool,
              "production_cells_crossed": int,
              "crossing_meters": {               # NEW-construction metres this branch
                "production": float | None,      #   builds across each ground, None when
                "canopy": float | None,          #   that ground's mask was unavailable
                "floodplain": float | None,      #   (see _mask_crossing_meters())
              },
            }, ...
          ],                                      # [] when no network at all
          "total_length_meters": float,
          "total_served_acres": float,
          "unserved_acres": float,
          "stop_reason": str,
          "max_grade_pct": float,                 # steepest single cell across the WHOLE network (max of branches)
          "avg_grade_pct": float,                 # LENGTH-WEIGHTED mean of the branch averages across the
                                                  #   WHOLE network (see _length_weighted_avg_grade_pct())
          "steep_meters": float,                  # steep-cell length summed across the WHOLE network
          "crossing_meters": {                    # crossing length per ground, summed across the WHOLE
            "production": float | None,           #   network -- ONE figure per ground however many
            "canopy": float | None,               #   separate blocks/patches of it the network crosses.
            "floodplain": float | None,           #   0.0 = measured, crossed none; None = no mask
          },
          "reaches_water_zone": bool,             # the ROUTER ran a water spur -- true even when that spur
                                                  #   was zero-length and so never became a branch below
          "cells": [(r, c), ...],                 # every branch cell, deduped, across the WHOLE network
          "cell_footprint_polygon_utm": Polygon/MultiPolygon,  # union of every branch's own footprint
        }

    avg_grade_pct and max_grade_pct/steep_meters are deliberately different
    quantities: avg_grade_pct averages the centerline's own elevation
    profile (a route can average a gentle 6% overall), while max_grade_pct
    and steep_meters come from the per-CELL slope raster and surface the
    single 24% cell that gentle average would otherwise hide -- see
    _cell_steep_stats().

    THE TWO NETWORK-LEVEL GRADES REDUCE DIFFERENTLY, and each reduction is
    the only honest one for its own quantity. max_grade_pct is a MAX over
    the branches, so it is the steepest point ANYWHERE in the network --
    the worst pitch does not get averaged away by how much gentle road
    surrounds it. avg_grade_pct is a LENGTH-WEIGHTED mean, so it is the
    grade of the average metre of road rather than the average of the
    branches, which would let a 40 ft stub outvote a 900 ft trunk.

    "cells"/"cell_footprint_polygon_utm" at the top level exist so
    exclusion consumers (e.g. tree_zone_candidates.py) get the WHOLE
    network, not just the trunk -- under-excluding a spur would be silent
    and wrong. A branch shorter than 2 cells (no real geometry to draw a
    line from) is dropped from "branches" before its own geometry is
    built, but its cell(s) still count toward the network-level "cells"/
    "cell_footprint_polygon_utm" above.

    Returns the empty-network shape (_empty_road_network(), branches=[],
    never None, never an exception) if the anchor snaps to no eligible
    cell at all -- a real, reportable "no network" outcome, not an error.
    A network the router DID build is returned as it stands, however
    short. route_road_network() itself already returns this
    same branches=[] shape (with its own stop_reason) when demand_mask has
    no True cells at all ("no_demand"), the anchor's own baseline coverage
    already serves every acre of demand ("all_demand_served"), no
    remaining demand is reachable at all ("no_reachable_demand"), even
    the first candidate's own cost-per-acre is already too expensive
    ("cost_per_acre_exceeded"), or its own leaf pruning removed every
    branch it had grown ("all_branches_below_minimum") -- this function
    just adds "cells"/"cell_footprint_polygon_utm" on top of that same
    result.
    """
    if slope_pct is None:
        slope_pct, _aspect_deg = compute_slope_and_aspect(dem["array"], dem["resolution_meters"])

    if tpi is None:
        tpi = compute_tpi(dem)

    pond_union = (
        selected_water_zone["render_fill_polygon_utm"].buffer(POND_ZONE_EXCLUSION_BUFFER_METERS)
        if selected_water_zone is not None
        else None
    )
    pond_prepared = prep(pond_union) if pond_union is not None and not pond_union.is_empty else None
    boundary_prepared = prep(boundary_polygon_utm)

    # Boundary + buffered pond ONLY -- production is deliberately NOT
    # folded in here anymore (see module docstring): it's a soft
    # traversal-cost term below instead, applied via cost_raster's own
    # production_mask argument.
    hard_exclusion_mask = _build_exclusion_cell_mask(dem, pond_prepared, boundary_prepared)

    raw_production_union = (
        unary_union([p["render_fill_polygon_utm"] for p in production_areas]) if production_areas else None
    )
    production_prepared = (
        prep(raw_production_union) if raw_production_union is not None and not raw_production_union.is_empty else None
    )
    # This ONE mask serves two roles: a soft, proportionally-costlier
    # traversal term in cost_raster, and route_road_network()'s own
    # demand_mask (the real acreage this network exists to serve).
    production_mask = _build_production_cell_mask(dem, production_prepared, boundary_prepared)

    floodplain_mask = None
    if hydric_floodplain_union is not None and not hydric_floodplain_union.is_empty:
        floodplain_prepared = prep(hydric_floodplain_union)
        floodplain_mask = _build_production_cell_mask(dem, floodplain_prepared, boundary_prepared)

    cost_raster = build_cost_raster(
        dem,
        slope_pct,
        hard_exclusion_mask,
        floodplain_mask=floodplain_mask,
        tpi=tpi,
        production_mask=production_mask,
        impassable_grade_pct=MAX_ROAD_GRADE_PCT,
        canopy_mask=canopy_mask,
    )

    anchor_cell = _snap_anchor_to_eligible_cell(dem, cost_raster, anchor_lon_lat)
    if anchor_cell is None:
        total_demand_acres = float(production_mask.sum()) * cell_area_acres(dem)
        return _empty_road_network("no_eligible_anchor", unserved_acres=total_demand_acres)

    # Water target cells: one cell ring immediately OUTSIDE the buffered
    # pond exclusion (raster_grid.binary_dilate() by one cell, minus the
    # exclusion itself), further restricted to finite-cost cells in
    # cost_raster -- the pond is hard-excluded at a
    # POND_ZONE_EXCLUSION_BUFFER_METERS buffer, so targets sit just
    # outside that buffer (the road stops there -- correct, you do not run
    # a farm road across a dam wall). A cell INSIDE the exclusion is
    # unreachable by construction and would make route_road_network()'s
    # own water spur silently find nothing every time.
    water_target_cells: list[tuple[int, int]] = []
    if pond_prepared is not None:
        pond_cell_mask = _build_production_cell_mask(dem, pond_prepared, boundary_prepared)
        target_ring = binary_dilate(pond_cell_mask, 1) & ~pond_cell_mask
        target_mask = target_ring & np.isfinite(cost_raster)
        water_target_cells = [(int(r), int(c)) for r, c in np.argwhere(target_mask)]

    network_result = route_road_network(
        dem,
        cost_raster,
        anchor_cell,
        production_mask,
        water_target_cells=water_target_cells,
        service_radius_meters=service_radius_meters,
        max_meters_per_served_acre=max_meters_per_served_acre,
        max_water_spur_meters=max_water_spur_meters,
    )

    branches_out = []
    network_cell_mask = np.zeros(dem["array"].shape, dtype=bool)
    all_cells: list[tuple[int, int]] = []
    seen_cells: set[tuple[int, int]] = set()

    for branch_index, branch in enumerate(network_result["branches"]):
        cells = branch["cells"]
        # Snapshotted BEFORE this branch's own cells join seen_cells: the
        # crossing measurement below charges NEW construction only, and
        # "new" means "not already part of a branch accepted before this
        # one" -- road_network_router._new_length_meters()'s own rule, so
        # the crossing lengths stay commensurable with length_meters.
        # Taken before the sub-2-cell `continue` too, because such a
        # branch is still accepted road the next branch can run back
        # along, even though it never becomes a drawn feature.
        already_built = set(seen_cells)
        for cell in cells:
            network_cell_mask[cell[0], cell[1]] = True
            if cell not in seen_cells:
                seen_cells.add(cell)
                all_cells.append(cell)

        if len(cells) < 2:
            continue  # no real geometry to draw a line from

        points = path_cells_to_points_xyz(dem, cells)
        line = LineString([(p[0], p[1]) for p in points])

        branch_cell_mask = np.zeros(dem["array"].shape, dtype=bool)
        for cell in cells:
            branch_cell_mask[cell[0], cell[1]] = True
        branch_footprint = cell_union_footprint(dem, branch_cell_mask)

        avg_grade_pct, _grade_stddev_pct = _grade_stats(points)
        max_grade_pct, steep_meters = _cell_steep_stats(dem, cells, slope_pct)
        crosses_floodplain = hydric_floodplain_union is not None and line.intersects(hydric_floodplain_union)
        production_cells_crossed = sum(1 for r, c in cells if production_mask[r, c])
        crosses_production_zone = production_cells_crossed > 0
        # HOW FAR this branch runs across each ground, beside the booleans
        # above rather than instead of them -- measured on the SAME cell
        # masks the cost raster charged against, which is what makes the
        # length and the penalty the router paid describe one event.
        #
        # THE FLOODPLAIN BOOLEAN AND THE FLOODPLAIN LENGTH CAN DISAGREE AT
        # THE MARGIN, deliberately and in one direction only. The boolean
        # is a geometry test (the centreline intersecting the union at
        # all); the length is a cell test (the cell CENTRE inside it, the
        # same test that decided whether the router paid the crossing
        # penalty there). A centreline clipping the union's edge without
        # any routed cell centre falling inside reads True at 0.0 m. That
        # is the honest pair: the line does touch that ground, and the
        # road built none of its length on it.
        crossing_meters = {
            "production": _mask_crossing_meters(dem, cells, production_mask, already_built),
            "canopy": _mask_crossing_meters(dem, cells, canopy_mask, already_built),
            "floodplain": _mask_crossing_meters(dem, cells, floodplain_mask, already_built),
        }

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        lons, lats = warp_transform(dem["crs"], "EPSG:4326", xs, ys)

        branches_out.append(
            {
                "cells": cells,
                "branch_role": branch["branch_role"],
                "branch_index": branch_index,
                "joins_branch_index": branch["joins_branch_index"],
                "length_meters": branch["length_meters"],
                "total_cost": branch["total_cost"],
                "newly_served_acres": branch["newly_served_acres"],
                "points_xyz": points,
                "line_utm": line,
                "geometry_wgs84": {"type": "LineString", "coordinates": list(zip(lons, lats))},
                "cell_footprint_polygon_utm": branch_footprint,
                "avg_grade_pct": avg_grade_pct,
                "max_grade_pct": max_grade_pct,
                "steep_meters": steep_meters,
                "crosses_floodplain": crosses_floodplain,
                "crosses_production_zone": crosses_production_zone,
                "production_cells_crossed": production_cells_crossed,
                "crossing_meters": crossing_meters,
            }
        )

    return {
        "branches": branches_out,
        "total_length_meters": network_result["total_length_meters"],
        # Straight through from the router, which already summed it over
        # the branches that survived its own leaf pruning. NOT re-derived
        # from branches_out above: a sub-2-cell branch dropped there (no
        # segment, no line to draw) is still real routed road the router
        # paid accumulated cost for, and its length is still in
        # total_length_meters -- dropping its cost while keeping its
        # length would understate cost-per-meter and flatter the terrain.
        "total_cost": network_result["total_cost"],
        "total_served_acres": network_result["total_served_acres"],
        "unserved_acres": network_result["unserved_acres"],
        "stop_reason": network_result["stop_reason"],
        # Network-level steep-section rollup across every branch: the
        # steepest single cell anywhere in the network, and the total
        # steep-cell length summed over all branches (see _cell_steep_stats()
        # for both). Computed over the branches actually emitted above, so a
        # sub-2-cell branch dropped before geometry (no segment, no reportable
        # grade) never contributes.
        "max_grade_pct": max((b["max_grade_pct"] for b in branches_out), default=0.0),
        # ONE average grade for the whole network, LENGTH-WEIGHTED across
        # the branches -- see _length_weighted_avg_grade_pct() for why a
        # mean of branch averages is the wrong answer and for the fact
        # that no network-level average existed at all before this.
        "avg_grade_pct": _length_weighted_avg_grade_pct(branches_out),
        "steep_meters": float(sum(b["steep_meters"] for b in branches_out)),
        # CROSSING LENGTH PER GROUND, SUMMED ACROSS THE WHOLE NETWORK --
        # one figure per ground, not one per crossed block: a network
        # running through three separate production blocks reports the
        # total length it spends inside production ground, because the
        # question the panel asks is how much of this road is built on
        # that ground, not how many pieces of it there are.
        #
        # 0.0 AND None ARE DIFFERENT ANSWERS AND BOTH ARE REACHABLE HERE.
        # 0.0 is measured: the mask existed and no routed cell was in it,
        # so this network crosses none of that ground (the panel drops the
        # row, which it can only do safely because the value is not a
        # stand-in for "we did not look"). None is the canopy or
        # floodplain fetch having failed, leaving no mask to measure
        # against -- the block's own "UNAVAILABLE IS None, NEVER 0.0"
        # rule, with determination's *_data_available flags saying which
        # case a None is. production_mask is always built, so its figure
        # is always measured.
        "crossing_meters": {
            ground: (
                0.0
                if not branches_out
                else (
                    None
                    if any(b["crossing_meters"][ground] is None for b in branches_out)
                    else float(sum(b["crossing_meters"][ground] for b in branches_out))
                )
            )
            for ground in _CROSSING_GROUNDS
        },
        # Whether the network reaches the selected water ground at all --
        # read off the ROUTER'S OWN branch list, deliberately, not off
        # branches_out above. A water spur can come back with a single cell
        # and zero new length: that means the network ALREADY runs through
        # the traversable cell next to the pond, so the spur had nothing
        # left to build. That is the strongest possible "yes" to this
        # question, and it is exactly the case the sub-2-cell drop removes
        # from branches_out (no segment, no line to draw) -- deriving this
        # flag there would answer "the road does not reach the pond" about
        # a road that runs right past it. The cell itself is in "cells"
        # and the footprint either way; only the separate feature is gone.
        "reaches_water_zone": any(
            b["branch_role"] == "water_spur" for b in network_result["branches"]
        ),
        "cells": all_cells,
        "cell_footprint_polygon_utm": cell_union_footprint(dem, network_cell_mask),
    }


def _confidence_notes_for_route(
    floodplain_data_is_fallback: bool,
    avg_grade_pct: float,
    crosses_floodplain: bool,
    crosses_production_zone: bool,
) -> str:
    floodplain_crossing_note = FLOODPLAIN_CROSSING_NOTE if crosses_floodplain else ""
    production_crossing_note = PRODUCTION_CROSSING_NOTE if crosses_production_zone else ""

    steep_grade_note = (
        STEEP_GRADE_ENGINEERING_NOTE.format(
            avg_grade_pct=round(avg_grade_pct, 1), threshold_pct=STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT
        )
        if avg_grade_pct > STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT
        else ""
    )
    floodplain_fallback_note = (
        "Floodplain/wet-ground cost scoring used a DEM-only fallback (buffered delineated valley "
        "lines), not real NHD/SSURGO data, because that data wasn't available for this run. "
        if floodplain_data_is_fallback
        else ""
    )
    return ROAD_CORRIDOR_CONFIDENCE_NOTES_TEMPLATE.format(
        floodplain_crossing_note=floodplain_crossing_note,
        production_crossing_note=production_crossing_note,
        steep_grade_note=steep_grade_note,
        floodplain_fallback_note=floodplain_fallback_note,
    )


def corridors_to_geojson(
    road_network: dict,
    floodplain_data_is_fallback: bool = False,
) -> dict:
    """Wraps build_road_network() output as the schema-conformant GeoJSON
    FeatureCollection this feature delivers (layer="suggested_road_corridor")
    -- one Feature per branch, not one Feature for the whole network.

    CONSOLIDATED into wire_translation.py (as road_network_to_feature_
    collection) -- this name stays as the module's own entry point,
    forwarding to the single implementation kept there.
    _confidence_notes_for_route() above stays HERE: the routing caveats
    are this module's own domain knowledge, not wire shape."""
    from wire_translation import road_network_to_feature_collection

    return road_network_to_feature_collection(road_network, floodplain_data_is_fallback)


# =====================================================================
# NARRATIVE DATA -- report-facing, FINAL values only
# =====================================================================
# Everything below exists to answer FOUR report questions about this
# module's deliverable, and nothing else:
#
#   1. HOW was the suggested route determined?
#   2. HOW MUCH ACCESS does it provide to the farm?
#   3. HOW GOOD IS THE GROUND the route runs on?
#   4. WHAT GROUND does it cross, and HOW MUCH of it?
#
# Question 3 is its own question and its own block. It is not a facet of
# either of the other two: a network can run over excellent ground and
# serve almost nothing, or cross a floodplain to reach every acre there
# is, and a reader needs those told apart. It is also answered WITHOUT
# any new analysis -- the cost surface already integrated grade, TPI,
# floodplain, canopy and production traversal, so the router's own
# accumulated cost is already the answer and only needed normalizing.
# See _terrain_quality_score() for that normalization and for the claim
# discipline the resulting number is reported under.
#
# Question 4 is likewise its own block and not a facet of 3. The quality
# score folds a floodplain crossing into ONE number along with grade and
# ridge position; "how much floodplain" is a separate fact, and a
# 20-foot clip of a block's corner is a different fact again from 300
# feet through its middle -- which the booleans that preceded this could
# not say at all. See _mask_crossing_meters().
#
# ---------------------------------------------------------------------
# ROADS' PANEL DIVERGES FROM EVERY OTHER STEP'S, DELIBERATELY. DO NOT
# "FIX" IT INTO CONSISTENCY.
# ---------------------------------------------------------------------
# The cross-step panel is three sections: a header (headline figures), a
# middle section, and the crossings. Everywhere else -- production,
# water, trees -- the MIDDLE SECTION IS THE SCORE'S OWN FACTORS: the
# weighted criteria that were combined to produce the number in the
# header, so the reader can see what made it.
#
# Roads' middle section is length, average grade and max grade, and NONE
# OF THOSE IS A FACTOR OF THE SCORE. They cannot be. This step's score
# is cost per metre (see _terrain_quality_score()), and length and grade
# are OUTCOMES of the routing that produced that cost, not inputs the
# score weighs: the router chose where to go, and the network's length
# and its grades are what fell out of that choice. Dividing accumulated
# cost by length is not "weighing length as a factor"; length is the
# denominator that makes the cost readable, and a longer network is
# neither better nor worse for being longer.
#
# So roads' middle section answers "WHAT IS THIS NETWORK LIKE" rather
# than "WHAT MADE THIS NUMBER". That is the right answer for this step
# and a reader is not being short-changed: the score's own composition is
# published in full in `scales` (the ratio it restates, the base it
# normalizes against, the anchors), which is where a reader who wants
# "what made this number" should be sent -- not to a middle section
# rebuilt out of quantities that did not make it.
#
# A future change that replaces this middle section with the score's
# factors, for consistency with the other steps, will be inventing
# factors this score does not have.
#
# The same two hard rules production_area_ceiling.py's narrative block
# established govern every value here:
#
#   1. FINAL. The consumer must never convert, calculate, or relate two
#      values to get a third. Imperial at this boundary (feet, acres --
#      never metres); grade in percent; everything rounded to 1 decimal
#      place, because the precision emitted is the precision narrated.
#   2. DERIVED, NEVER RECOMPUTED. Every figure is read off the network
#      dict build_road_network() already returns -- no routing pass, cost
#      raster, or grade statistic is re-run to report on itself. The one
#      derived figure (served share of production demand) is one division
#      over two numbers the router already produced.
#
# The output is plain JSON: numbers, booleans, strings, dicts, lists.
# json.dumps() must work on it with no custom encoder.
#
# UNAVAILABLE IS None, NEVER 0.0 -- and a constraint that never ran is
# reported as not-applied (its *_available flag False), never as
# silently satisfied. Without those flags a narrative could claim
# "routed around floodplain ground" off a run where the NHD/SSURGO
# fetches were down.
#
# NO REASON STRINGS. This block emits values; the report writes prose --
# the determination story (grown outward from the real access point, one
# branch at a time, by new acreage served per unit routing cost) is
# carried by stop_reason, the constraint flags, and each branch's own
# newly_served_acres, which are the data that story is written FROM.


def _round1(value):
    """1 decimal place, or None passed straight through -- the single
    rounding boundary for this whole block. None means 'not known', and
    must never be silently rounded into a 0.0 that reads as a
    measurement."""
    return None if value is None else round(float(value), 1)


def _feet(meters):
    """Metres to feet at this block's own rounding boundary, None passed
    straight through -- the metric-to-imperial conversion happens HERE,
    in the module, never downstream in the report."""
    return None if meters is None else round(float(meters) / METERS_PER_FOOT, 1)


# HOW TO READ EVERY SCORED NUMBER THIS BLOCK SHIPS.
# production_area_ceiling.py's `scales` is the precedent and water_survey_
# areas.build_scales()'s docstring states the rule outright: NO SCORED
# VALUE CROSSES THE WIRE WITHOUT ITS SCALE, because a bare number with no
# range is not a measurement a reader can act on. The terrain quality
# score has been crossing the wire without one. This is that scale.
#
# WHAT 61 OUT OF 100 IS OUT OF, stated plainly because it is the question
# the whole block exists to answer. The score is a linear restatement of
# cost_per_meter_ratio, and that ratio is the network's own average cost
# per metre divided by road_cost_path._BASE_TRAVEL_COST -- the cost of one
# metre of level, open, non-floodplain, non-canopy, non-production ground
# on the very cost surface the router ran on. Ratio 1.0 is therefore
# "ordinary flat ground", and score = 100 - ratio * 10 puts it at 90.
#
# IT IS NOT PARCEL-RELATIVE, AND THAT IS THE LOAD-BEARING FACT.
# _BASE_TRAVEL_COST is a CONSTANT of road_cost_path.py, not a statistic of
# this parcel: no boundary, no elevation range, no observed best or worst
# cell enters it. So 61 means the same thing on two different parcels --
# the same ratio of routing cost to level open ground -- and two
# properties' scores are directly comparable. That is the opposite of
# water's suitability scale, which ships `parcel_observed_max` precisely
# BECAUSE its ceiling is the parcel's own (see build_scales() there), and
# the contrast is worth stating rather than leaving a reader to assume
# this one works the same way. `parcel_relative: False` says it on the
# wire so no consumer has to read either module to find out.
#
# NO CEILING IS DECLARED BECAUSE COST PER METRE HAS NONE. There is no
# terrain-independent worst road: a network crossing a ridge can cost an
# arbitrary multiple of base. What the scale has instead is a chosen
# BOTTOM -- ratio 10.0 reads 0.0 and everything worse clamps there -- and
# that bottom is a judgement about where a report should stop
# distinguishing bad ground from worse, not a measured limit. It ships as
# `clamps_at_ratio` so a reader can see that a 0.0 means "at or past the
# bottom of the scale", never "ten times base cost exactly".
_TERRAIN_QUALITY_SCALE = {
    "range": [0.0, 100.0],
    "direction": "higher_is_better",
    # ABSOLUTE, not parcel-relative -- see the block comment above. The
    # normalizer is a constant of the cost surface, so the same score on
    # two parcels means the same thing.
    "parcel_relative": False,
    "normalized_against": "base_travel_cost_per_meter",
    "normalizer_cost_per_meter": float(_BASE_TRAVEL_COST),
    "normalizer_means": (
        "the cost of one metre of level, open, non-floodplain, non-canopy, "
        "non-block ground on the cost surface this network was routed over"
    ),
    "score_from_ratio": "100 - cost_per_meter_ratio * 10, clamped to [0, 100]",
    "clamps_at_ratio": 10.0,
    # 100 is not practically reachable: TPI discounts an ideal ridge cell
    # to about half base cost, and no real road runs on ideal ground end
    # to end. A scale whose top is unreachable is a scale that does not
    # promise a perfect road exists -- shipped so a panel does not render
    # 88 as though 100 were on offer.
    "practical_max": 95.0,
    # The cost surface's OWN arithmetic, not anchors tuned against any
    # property -- see _terrain_quality_score() for the derivation of each.
    "anchors": [
        {"ratio": 0.5, "score": 95.0, "ground": "ridge line, ideal ground"},
        {"ratio": 1.0, "score": 90.0, "ground": "level open ground at base cost"},
        {"ratio": 2.33, "score": 76.7, "ground": "sustained 10% grade"},
        {"ratio": 3.99, "score": 60.1, "ground": "sustained 15% grade"},
        {"ratio": 6.0, "score": 40.0, "ground": "floodplain crossing on the flat"},
        {"ratio": 9.31, "score": 6.9, "ground": "sustained 25% grade"},
        {"ratio": 10.0, "score": 0.0, "ground": "the bottom of the scale"},
    ],
    # THE CLAIM DISCIPLINE, on the wire rather than only in a docstring,
    # because it is the panel and the report that would otherwise make the
    # claim. 77 means "reasonable ground for this terrain"; it does NOT
    # mean "77% as good as a perfect road", and nothing in the arithmetic
    # supports that reading.
    "claim": "relative_screening_value",
    "not_a": "percentage_of_an_ideal_road",
    # DELIBERATELY UNVALIDATED, same caveat every threshold in this
    # pipeline carries: the 10.0 multiplier and the clamp are a judgement,
    # not figures a sweep has confirmed.
    "calibration": "unvalidated_starting_values",
    "applies_to": ["quality.terrain_quality_score"],
}

# The quantity the score is a linear restatement of, shipped with its own
# entry for water's stated reason: an entry per value, each declaring its
# own scale, so a consumer never has to guess which scale it is looking
# at. LOWER IS BETTER here while the score above is higher-is-better --
# two directions in one block is exactly the confusion an undeclared
# scale causes.
_COST_PER_METER_RATIO_SCALE = {
    "min": 0.0,
    "max": None,  # unbounded above: cost per metre has no terrain-independent ceiling
    "direction": "lower_is_better",
    "unit": "multiples_of_base_travel_cost_per_meter",
    "parcel_relative": False,
    "neutral": 1.0,  # level open ground at base cost
    "applies_to": ["quality.cost_per_meter_ratio"],
}

# The crossing lengths are MEASUREMENTS, not scores, and they get an entry
# anyway -- not for a range they do not have, but for the one thing a
# reader cannot infer from the number: that 0.0 and null are different
# answers here. 0.0 is measured ground crossed none of; null is a fetch
# that did not land, and determination's own *_data_available flags say
# which ground that was.
_CROSSING_SCALE = {
    "unit": "feet",
    "min": 0.0,
    "zero_means": "measured_crossed_none",
    "null_means": "ground_data_unavailable_not_measured",
    "summed_over": "the whole network, one figure per ground however many separate areas of it are crossed",
    "counts": "new construction only -- road already built by an earlier branch is not re-counted",
    "applies_to": [
        "crossings.crosses_block_ft",
        "crossings.crosses_canopy_ft",
        "crossings.crosses_floodplain_ft",
    ],
}

_SCALES = {
    "terrain_quality_score": _TERRAIN_QUALITY_SCALE,
    "cost_per_meter_ratio": _COST_PER_METER_RATIO_SCALE,
    "crossings": _CROSSING_SCALE,
}


def _terrain_quality_score(
    total_cost: float, total_length_meters: float, dem: dict
) -> Optional[float]:
    """
    A 0-100 terrain quality score for a routed road network, derived
    ENTIRELY from the router's own accumulated path cost -- no new
    analysis, no second pass over the DEM, no geometry re-measured.

    road_cost_path.build_cost_raster() already integrates everything that
    makes ground good or bad for a road: TPI ridge preference, the
    quadratic grade penalty, floodplain crossing, canopy crossing,
    production traversal. The Dijkstra accumulated cost along a routed
    branch is therefore ALREADY a quality measure -- it was simply never
    normalized or exposed. This function normalizes it and nothing else.

    Returns None when total_length_meters is 0 -- there is no road to
    score. That is deliberately not 0.0: a 0.0 here would read as "a
    terrible road" when the truth is "no road", and a report quoting it
    would say something false about ground nothing was ever routed over.

    THE ARITHMETIC

      1. cost_per_meter    = total_cost / total_length_meters
      2. base_cost_per_meter = road_cost_path._BASE_TRAVEL_COST
      3. ratio             = cost_per_meter / base_cost_per_meter
      4. score             = 100.0 - (ratio * 10.0), clamped to
                             [0.0, 100.0], rounded to one decimal.

    WHAT THE SCORE IS OUT OF, and whether that is parcel-relative: see
    _SCALES above, which now ships the answer on the wire. In short --
    the base is road_cost_path._BASE_TRAVEL_COST, a CONSTANT of the cost
    surface rather than any statistic of this parcel, so the score is
    ABSOLUTE and two properties' scores mean the same thing. Cost per
    metre has no natural ceiling, so the scale declares a chosen bottom
    (ratio 10 -> 0.0, clamped) instead of a maximum.

    NORMALIZATION, and why the DEM resolution does NOT appear in it.
    Normalizing is mandatory -- an un-normalized cost-per-meter is just a
    raw cost figure with no scale to read it against. The base it is
    normalized against is road_cost_path._BASE_TRAVEL_COST itself,
    because road_cost_path's own Dijkstra (cost_distance_field(), and
    least_cost_path() with it) weights every edge as

        math.hypot(dc * px, dr * py) * cost_raster[neighbor]

    -- REAL METERS multiplied by a cost-per-meter. Accumulated cost is
    therefore already in meter-scaled units, and dividing it by real
    length already yields an average cost-per-meter directly comparable
    to _BASE_TRAVEL_COST at ANY grid resolution: identical terrain reads
    the same ratio at 3 m and at 5 m, which is exactly the property this
    normalization has to have.

    Dividing additionally by min(px, py) -- which would be the right
    normalization if accumulated cost were tallied PER CELL EDGE rather
    than per meter -- would INTRODUCE the resolution dependence rather
    than remove it (the same ground would read ratio 5.0 at 5 m and 3.0
    at 3 m) and would put every anchor below out by a factor of the cell
    size. The edge weight formula above is what settles it; the anchors
    are the cross-check, and they only come out right against this base:
    a floodplain crossing on flat ground costs 1.0 + 5.0 = 6.0 per cell
    by build_cost_raster()'s own arithmetic, and reads ratio 6.0 here.

    THE SCALE. Anchors below come from the cost surface's OWN arithmetic
    -- _BASE_TRAVEL_COST 1.0, GRADE_PENALTY_WEIGHT 0.0133 on grade
    percent squared, FLOODPLAIN_CROSSING_COST_PENALTY 5.0,
    TPI_PREFERENCE_STRENGTH 0.5 -- and are NOT tuned against any
    property:

        ratio 0.5  (ridge, ideal ground, TPI discount)  -> 95
        ratio 1.0  (neutral flat ground, base cost)     -> 90
        ratio 2.3  (sustained 10% grade)                -> 77
        ratio 4.0  (sustained 15% grade)                -> 60
        ratio 6.0  (floodplain crossing on flat)        -> 40
        ratio 10.0 (the bottom of the scale)            ->  0

    Read the rows as rounded: 10% grade is 1 + 0.0133 * 100 = 2.33 -> 76.7
    and 15% is 3.9925 -> 60.1. A SUSTAINED 25% GRADE, the worst of these,
    is 9.3125 -> 6.9 -- just short of the bottom rather than exactly at
    it, so the scale does keep a little resolution past the worst grade a
    farm road plausibly holds.

    100 IS NOT PRACTICALLY REACHABLE, and that is honest rather than a
    defect. TPI discounts a ridge cell to about 0.5x base, and no real
    road runs on ideal ground end to end, so roughly 95 is the realistic
    ceiling. A scale whose top is unreachable is a scale that does not
    promise a perfect road exists.

    CLAIM DISCIPLINE. This is a RELATIVE SCREENING VALUE, not a
    percentage of an ideal road. 77 means "reasonable ground for this
    terrain" -- it does NOT mean "77% as good as a perfect road", and
    nothing here supports that reading. It is the same discipline the
    water standards alignment document applies to suitability scores: a
    number that ranks and screens, never one that claims a measured
    fraction of an ideal.

    ONE QUESTION ONLY: how good is the ground this road runs on. No
    economy, coverage, branch-count or served-acreage term enters it --
    those are property-suitability questions, already reported under
    narrative_data's own 'access' block, and folding any of them in here
    would make a network score badly for serving few acres well.

    UNVALIDATED STARTING VALUES, same caveat every threshold in this
    pipeline carries: the 10.0 multiplier and the [0.0, 100.0] clamp
    bounds are a judgement about how fast a report should stop
    distinguishing bad ground from worse, not figures any sweep has
    validated. The multiplier sets where the scale bottoms out (ratio 10
    -- roughly a sustained 25% grade -- reads 0), and the clamp is what
    keeps a worse-than-that network at 0 rather than negative. Both are
    expected to move once there is real ground to check them against.

    IT IS NOT A PER-BRANCH SCORE, AND THERE IS NO REDUCTION STEP. This
    function is called ONCE, on the network's own summed total_cost and
    total_length_meters -- there is no per-branch score to combine, and
    no branch ever carries one. That is worth stating because the
    reduction it AMOUNTS TO is the one the network's average grade had to
    be given explicitly:

        sum(cost_i) / sum(length_i)  ==  sum(length_i * cpm_i) / sum(length_i)

    -- the network's cost per metre is already the LENGTH-WEIGHTED mean
    of the branches' own costs per metre, because summing both halves
    before dividing is what length-weighting is. A 40 ft spur over
    terrible ground cannot drag the score the way it would drag a mean of
    per-branch scores. The score is a linear function of that ratio, so
    it inherits the weighting exactly.

    THE ONE CAVEAT, and it is the paragraph below: total_cost includes
    each branch's traversal back along already-accepted branches while
    total_length_meters counts new construction only, so the ratio is a
    length-weighted mean over slightly more cost than length. That
    inflation is bounded at about 1% of the reused length (see below) and
    is not a per-branch weighting error.

    total_cost and total_length_meters come from the SAME network dict
    (route_road_network()'s own, summed after leaf pruning, or
    build_road_network()'s passthrough of it). They are not measured over
    quite the same road: total_length_meters counts NEW construction
    only, while each branch's accumulated cost also includes running back
    along already-accepted branches to reach its own start. That
    traversal is charged at road_network_router.EXISTING_ROAD_TRAVERSAL_
    COST (0.01, one percent of base), so the inflation it adds to the
    ratio is bounded at about 1% of the reused length and is deliberately
    left in rather than corrected for -- backing it out would mean
    re-deriving cost from geometry, which this function does not do.
    """
    if total_length_meters == 0:
        return None

    # dem is read for its grid resolution only as a tripwire: a
    # non-positive or non-finite cell size means total_length_meters was
    # measured on a grid that does not describe real ground, and every
    # figure below would be arithmetic over a meaningless denominator.
    # The resolution itself does NOT enter the ratio -- see the
    # normalization note above for why it cancels out of the edge weights
    # entirely.
    px, py = dem["resolution_meters"]
    if not (math.isfinite(px) and math.isfinite(py)) or min(px, py) <= 0.0:
        raise ValueError(
            f"_terrain_quality_score() got a DEM with resolution_meters {(px, py)!r} -- "
            "every cell size must be finite and strictly positive or the network's own "
            "length_meters, and so the cost-per-meter ratio below, describe nothing real."
        )

    cost_per_meter = float(total_cost) / float(total_length_meters)
    base_cost_per_meter = _BASE_TRAVEL_COST
    ratio = cost_per_meter / base_cost_per_meter

    return round(min(100.0, max(0.0, 100.0 - ratio * 10.0)), 1)


def _terrain_quality_ratio(total_cost: float, total_length_meters: float) -> Optional[float]:
    """
    The normalized cost-per-meter ratio _terrain_quality_score() scores
    from, rounded to 2 decimals -- published alongside the score so a
    reader can see the quantity the score is a linear restatement of,
    rather than having to trust the score alone. None on the same
    condition, and for the same reason: no road, nothing to report.
    """
    if total_length_meters == 0:
        return None
    return round((float(total_cost) / float(total_length_meters)) / _BASE_TRAVEL_COST, 2)


def build_narrative_data(
    road_network: dict,
    service_radius_meters: float,
    water_zone_excluded: bool,
    floodplain_data_available: bool,
    floodplain_data_is_fallback: bool,
    canopy_data_available: bool,
    dem: dict,
) -> dict:
    """
    The 'narrative_data' block identify_road_corridor_candidates()
    attaches to its result -- pre-computed, FINAL, JSON-serialisable
    values answering the report questions in this section's header
    comment. Data only: no prose, no interpretation. road_network is
    build_road_network()'s own return dict (or _empty_road_network()'s),
    read but never modified.

    dem is the SAME grid the network was routed on -- passed in, never
    re-fetched -- and is read only by _terrain_quality_score() below, as
    its own grid tripwire (see there).

    The flag parameters say what the run that produced road_network
    ACTUALLY applied -- water_zone_excluded (a selected water zone
    existed and was hard-excluded, buffered), floodplain_data_available
    (a floodplain/hydric cost-penalty union existed at all) with
    floodplain_data_is_fallback (that union came from the DEM-only
    valley-line fallback, not real NHD/SSURGO data), and
    canopy_data_available (the soft canopy crossing penalty was active).
    identify_road_corridor_candidates() passes what it genuinely
    fetched/derived; the caller passes them so this block never guesses
    at configuration. service_radius_meters is likewise the value the
    router ACTUALLY used (an override, or road_network_router.py's
    default).

    Shape:

        {
          'network_found': bool,
          'stop_reason': str,         # the router's own reason growth ended --
                                      #   'diminishing_returns', 'no_anchor_given',
                                      #   'all_demand_served', ... -- the single
                                      #   most load-bearing determination value
          'determination': {          # question 1 -- HOW the route was determined
            'grade_ceiling_pct',      #   hard exclusion: no branch cell exceeds this
            'steep_grade_threshold_pct',
                                      #   above this, a grade needs real engineering
            'max_grade_pct',          #   steepest single cell ANYWHERE in the network
            'avg_grade_pct',          #   the network's LENGTH-WEIGHTED average grade --
                                      #   the grade of the average metre of road, never a
                                      #   mean of the branch averages
            'steep_ft',               #   total length of cells above the threshold
            'water_zone_excluded',    #   pond/dam ground was hard-excluded (buffered)
            'floodplain_data_available',
            'floodplain_data_is_fallback',
            'canopy_data_available',
          },
          'access': {                 # question 2 -- HOW MUCH ACCESS it provides
            'branch_count',
            'total_length_ft',
            'served_acres',           #   production acreage within the service
                                      #   radius of the network
            'unserved_acres',         #   production acreage left out of range
            'served_pct_of_production',
                                      #   served / (served + unserved); None when
                                      #   there is no production demand at all
            'service_radius_ft',      #   how far off the road 'served' reaches
            'reaches_water_zone',     #   the network reaches the pond site's edge --
                                      #   by its own water spur, or because a branch
                                      #   already ran through that cell (a zero-length
                                      #   spur, which draws nothing but still counts)
          },
          'quality': {                # question 3 -- HOW GOOD IS THE GROUND the
                                      #   route runs on. Its own key, deliberately:
                                      #   'determination' answers HOW the route was
                                      #   determined and 'access' answers HOW MUCH
                                      #   ACCESS it provides, and terrain quality is
                                      #   neither of those questions
            'terrain_quality_score',  #   0-100, higher is better; None when there is
                                      #   no network to score. A RELATIVE SCREENING
                                      #   value, never a percentage of an ideal road
                                      #   -- see _terrain_quality_score() for the
                                      #   scale, its anchors, and that claim discipline
            'cost_per_meter_ratio',   #   the normalized ratio the score restates,
                                      #   2 decimals; None on the same condition
            'total_path_cost',        #   the router's own raw accumulated cost over
                                      #   the surviving branches, for diagnostics
          },
          'crossings': {              # question 4 -- WHAT GROUND does the network cross,
                                      #   and HOW MUCH of it. Lengths, summed across the
                                      #   WHOLE network, one figure per ground: a network
                                      #   crossing three separate blocks reports the total
                                      #   length it spends on block ground, not three
                                      #   figures. 0.0 is measured ("crossed none of it");
                                      #   None is that ground's data never arriving
            'crosses_block_ft',       #   production ground -- BLOCK is the interface's
                                      #   word for it; the internal id stays 'production'
            'crosses_canopy_ft',      #   woody vegetation (None when canopy was unavailable)
            'crosses_floodplain_ft',  #   floodplain/hydric (None when NHD+SSURGO were both out)
          },
          'scales': {                 # how to read every scored value above -- see _SCALES.
                                      #   The terrain quality score's range, direction, what
                                      #   it normalizes against and whether that is
                                      #   parcel-relative (it is NOT); the ratio's own
                                      #   direction and unit; and the crossing lengths'
                                      #   zero-versus-null rule
          },
          'branches': [               # in branch order (trunk first), one entry per
                                      #   drawn branch
            {
              'branch_index', 'role',   # 'trunk' | 'spur' | 'water_spur'
              'joins_branch_index',     # which branch this one grows off
                                        #   (None for the trunk) -- lets a
                                        #   narrative say "a spur off the
                                        #   trunk" without the geometry
              'length_ft',
              'newly_served_acres',     # the acreage THIS branch alone brought into
                                        #   range -- why the router chose to build it
              'avg_grade_pct',          # centerline average
              'max_grade_pct',          # steepest single cell on this branch
              'steep_ft',
              'crosses_floodplain',
              'crosses_production_zone',
            }, ...
          ],
        }
    """
    branches = road_network["branches"]
    served_acres = float(road_network["total_served_acres"])
    unserved_acres = float(road_network["unserved_acres"])
    total_demand_acres = served_acres + unserved_acres
    total_length_meters = float(road_network["total_length_meters"])
    total_cost = float(road_network["total_cost"])
    crossing_meters = road_network["crossing_meters"]

    return {
        "network_found": bool(branches),
        "stop_reason": str(road_network["stop_reason"]),
        "determination": {
            "grade_ceiling_pct": _round1(MAX_ROAD_GRADE_PCT),
            "steep_grade_threshold_pct": _round1(STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT),
            "max_grade_pct": _round1(road_network["max_grade_pct"]),
            # THE NETWORK'S AVERAGE, LENGTH-WEIGHTED -- see
            # _length_weighted_avg_grade_pct(). It sits beside
            # max_grade_pct because the pair is one fact ("how steep is
            # this network") told from both ends, and because a consumer
            # given only max_grade_pct has no honest way to reach the
            # average: the branch list is per-branch data, and averaging
            # it is the naive reduction this field exists to prevent.
            "avg_grade_pct": _round1(road_network["avg_grade_pct"]),
            "steep_ft": _feet(road_network["steep_meters"]),
            "water_zone_excluded": bool(water_zone_excluded),
            "floodplain_data_available": bool(floodplain_data_available),
            "floodplain_data_is_fallback": bool(floodplain_data_is_fallback),
            "canopy_data_available": bool(canopy_data_available),
        },
        "access": {
            "branch_count": len(branches),
            "total_length_ft": _feet(road_network["total_length_meters"]),
            "served_acres": _round1(served_acres),
            "unserved_acres": _round1(unserved_acres),
            "served_pct_of_production": (
                _round1(served_acres / total_demand_acres * 100.0) if total_demand_acres > 0 else None
            ),
            "service_radius_ft": _feet(service_radius_meters),
            # Off the network-level flag build_road_network() publishes (see
            # there for why it is not re-derived from the branch list). The
            # fallback scan is for a network dict that predates that field --
            # a rehydrated one off the wire, which has only the branches that
            # became features and so cannot know about a dropped zero-length
            # spur either way.
            "reaches_water_zone": bool(
                road_network["reaches_water_zone"]
                if "reaches_water_zone" in road_network
                else any(b["branch_role"] == "water_spur" for b in branches)
            ),
        },
        # QUESTION 3, its own top-level key and not a field inside either
        # block above: 'determination' answers HOW the route was determined,
        # 'access' answers HOW MUCH ACCESS it provides, and how good the
        # ground is is a third question that belongs in neither. Purely
        # additive -- no existing key, value or type above changes.
        "quality": {
            "terrain_quality_score": _terrain_quality_score(total_cost, total_length_meters, dem),
            "cost_per_meter_ratio": _terrain_quality_ratio(total_cost, total_length_meters),
            # The raw accumulated cost, unrounded and unnormalized, so a
            # diagnostic can recover the ratio's own numerator without
            # re-running the router.
            "total_path_cost": total_cost,
        },
        # QUESTION 4, its own top-level key for the same reason 'quality'
        # is: what ground the route crosses and how much of it is neither
        # how the route was determined, how much access it gives, nor how
        # good the ground it runs on is on average. A road can score well
        # on ground quality overall and still spend 300 ft inside a
        # production block, and a reader is entitled to that as its own
        # number rather than as an inference from a score.
        #
        # LENGTHS, NOT BOOLEANS. A corridor clipping a block's corner for
        # 20 ft and one running 300 ft through its middle are different
        # facts, and a yes/no cannot tell them apart. The precedents are
        # commit_validation.crossings() and the trees contract's four
        # grounds, both of which record the ACREAGE of each crossed
        # ground; a road is a line rather than an area, so this records
        # the same shape in the unit a line has. The per-branch booleans
        # are unchanged and stay in 'branches' below -- this block is the
        # network-level answer and carries no per-branch data at all.
        #
        # BLOCK IS THE WORD, and only in the key. Production zones are
        # called blocks in the interface now, so the panel-facing name
        # says block; the mask, the ids, and the per-branch
        # crosses_production_zone boolean it is measured from all keep
        # their existing names (see _CROSSING_GROUNDS).
        "crossings": {
            "crosses_block_ft": _feet(crossing_meters["production"]),
            "crosses_canopy_ft": _feet(crossing_meters["canopy"]),
            "crosses_floodplain_ft": _feet(crossing_meters["floodplain"]),
        },
        # HOW TO READ EVERY SCORED VALUE ABOVE -- a module-level constant,
        # forwarded by identity rather than rebuilt per call, exactly as
        # production_area_ceiling.py forwards its own _SCALES. It depends
        # on nothing about this network: it describes the instrument, not
        # the reading.
        "scales": _SCALES,
        "branches": [
            {
                "branch_index": int(b["branch_index"]),
                "role": str(b["branch_role"]),
                "joins_branch_index": (
                    int(b["joins_branch_index"]) if b["joins_branch_index"] is not None else None
                ),
                "length_ft": _feet(b["length_meters"]),
                "newly_served_acres": _round1(b["newly_served_acres"]),
                "avg_grade_pct": _round1(b["avg_grade_pct"]),
                "max_grade_pct": _round1(b["max_grade_pct"]),
                "steep_ft": _feet(b["steep_meters"]),
                "crosses_floodplain": bool(b["crosses_floodplain"]),
                "crosses_production_zone": bool(b["crosses_production_zone"]),
            }
            for b in branches
        ],
    }


def _log_fetch_failure(label: str, exc: Exception) -> None:
    """
    Every real-data fetch in this module degrades gracefully on failure
    (see the module docstring), but "gracefully" shouldn't mean "silently
    identical whether the cause was a transient network timeout or a real
    bug in the query itself." A 4xx HTTP status means the server rejected
    the REQUEST as malformed (bad SQL, a column/table that doesn't exist)
    — that will fail exactly the same way on every future run, unlike a
    timeout or connection error, which might not. Anything that isn't
    even a requests error (a KeyError from an unexpected response shape,
    etc.) is just as clearly a real bug, not network flakiness. Print a
    distinct message for each case so this doesn't look like ordinary
    network unavailability in the logs.
    """
    if isinstance(exc, requests.exceptions.RequestException):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None and 400 <= status < 500:
            print(
                f"  {label}: request rejected by the server (HTTP {status}) — likely a real "
                f"query/schema bug, not transient network unavailability: {exc}"
            )
        else:
            print(f"  {label}: network request failed ({exc}), continuing without it.")
    else:
        print(f"  {label}: unexpected failure, not a network error ({type(exc).__name__}: {exc}).")


def _fetch_canopy_soft_cost_mask(
    boundary_polygon_utm: Polygon,
    dem: dict,
    canopy_height: Optional[dict] = None,
) -> Optional[np.ndarray]:
    """
    Raw-canopy (woody-vegetation) cell mask for the SOFT road-cost term
    build_cost_raster() applies (road_cost_path.CANOPY_CROSSING_COST_
    PENALTY), on dem's own grid -- or None when canopy data is unavailable,
    because this module DEGRADES GRACEFULLY on a canopy outage rather than
    aborting.

    buffer_meters=0.0 deliberately: roads see the RAW canopy cells, not the
    +TREE_ROOT_ZONE_BUFFER_METERS root-protection dilation production/solar
    apply -- a road only pays for the trees it actually crosses, not a
    protective standoff around them. The canopy_height override is
    forwarded verbatim, so a caller that already fetched canopy for this
    boundary (e.g. pipeline_context.build_pipeline_context()) never
    triggers a second, redundant fetch here.

    production_area.get_required_tree_root_zone_mask_utm() is production/
    solar's own "fetch canopy or fail hard" building block -- it RAISES
    (RuntimeError) when HAG coverage is missing, and lets other fetch
    failures (retries exhausted, CanopyCoverageIncompleteError) propagate.
    Roads deliberately catch ALL of those and DEGRADE GRACEFULLY: canopy is
    only a soft preference here, so any canopy outage drops the term
    (returns None) rather than aborting the whole network. That's
    consistent with every other real-data fetch in this module (floodplain,
    water, soil) and with the floodplain soft term right beside it, which
    likewise contributes nothing when its data is unavailable -- a road
    network is still a useful, honest answer without the canopy-avoidance
    preference folded in, unlike a production/water zone that can't be
    certified free of tree cover at all.
    """
    try:
        return get_required_tree_root_zone_mask_utm(
            boundary_polygon_utm, dem, buffer_meters=0.0, canopy_height=canopy_height
        )
    except Exception as e:
        _log_fetch_failure("canopy height fetch", e)
        print("  Continuing without the soft canopy road-cost term (canopy is a soft preference here, not a hard gate).")
        return None


def _fetch_floodplain_hydric_union(
    boundary_coordinates,
    dem,
    valleys,
    boundary_polygon_utm,
    soil_components: Optional[list[dict]] = None,
    water_features: Optional[dict] = None,
    soil_geometries: Optional[dict] = None,
) -> tuple[Optional[object], bool]:
    """NHD stream/water-body buffers + SSURGO hydric soil polygons,
    unioned; falls back to buffering the already-computed delineated
    valley lines (an elevation-only proxy) only if BOTH real sources are
    unreachable. Returns (union_or_None, is_fallback). This union feeds a
    SOFT cost penalty (see module docstring), not a hard exclusion -- the
    fetch/clip/buffer logic that builds it is otherwise unchanged.

    soil_components, water_features, and soil_geometries are all optional --
    same None-falls-back-to-self-fetch convention every override in this
    pipeline uses. A caller that already fetched soil composition data,
    NHD water features, and/or SSURGO map-unit geometry for this exact
    boundary (e.g. parcel_data.fetch_parcel_data()) passes any/all of them
    through here instead of paying for a second, redundant get_soil_data_
    for_polygon()/get_water_features_for_boundary()/get_soil_geometries_
    for_polygon() fetch. water_features must be the same {'streams': [...],
    'water_bodies': [...]} shape get_water_features_for_boundary() itself
    returns; soil_geometries must be the same {mukey: geojson_geometry}
    shape get_soil_geometries_for_polygon() itself returns -- both are
    exactly what ParcelData's own water_features/soil_geometries fields
    already hold, so a ParcelData-backed caller can pass those fields
    straight through unchanged. soil_geometries is only ever consulted when
    hydric_mukeys comes back non-empty (see below); if it's supplied but
    hydric_mukeys is empty, it's simply never looked at -- not an error.

    Each fetched NHD feature is clipped to a generous context region
    around boundary_polygon_utm (FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS)
    BEFORE being buffered into the union — see that constant's own
    comment for the real bug this fixes: NHD's query returns each
    matching feature's full, un-clipped geometry, so a stream or
    waterbody merely touching the (already-buffered) fetch bounding box
    could come back with geometry extending far past the property,
    ballooning the resulting union to many times the parcel's own size.
    The SSURGO hydric piece needs no equivalent clip here — it already
    comes back clipped to the parcel's own wkt_polygon from
    get_soil_geometries_for_polygon() itself (STIntersection), so it can
    never exceed the parcel's own area in the first place.

    A mukey only contributes its hydric geometry here if its SUMMED hydric
    component percentage meets soil_data.MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE
    (soil_data.hydric_disqualifying_mukeys()) — a real, second bug found
    live alongside the NHD-clipping one above: a map unit that's 99%+
    well/moderately-well-drained but has a single 1%-of-composition hydric
    component was previously flagged the SAME as a map unit that's 85%+
    hydric, and its entire (much larger) mapped polygon got included right
    alongside genuinely wet ground.

    Each fetched NHD feature's BUFFERED piece is additionally intersected
    against boundary_polygon_utm.buffer(FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS)
    before being added to the union — see that constant's own comment for
    the THIRD bug this fixes: the fetch-context clip above bounds each raw
    stream feature before it's buffered, but nothing previously bounded
    the result AFTER buffering, so the buffer stroke (and however much
    stream length survived the looser fetch-context clip) could still
    produce a final piece extending well past any distance actually
    relevant to this parcel. Confirmed live: an 11.2-acre union of which
    only 0.077 acres (0.6%) actually overlapped the real parcel — a long
    buffered band along Montour Run, entirely on the far side of N Montour
    Rd from the field.
    """
    context_region = boundary_polygon_utm.buffer(FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS)
    final_relevance_region = boundary_polygon_utm.buffer(FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS)
    pieces = []

    try:
        if water_features is None:
            water_features = get_water_features_for_boundary(boundary_coordinates)
        for feature in water_features["streams"] + water_features["water_bodies"]:
            geometry = feature.get("geometry")
            if geometry is None:
                continue
            utm_geometry = shape(transform_geom("EPSG:4326", dem["crs"], geometry))
            clipped_geometry = utm_geometry.intersection(context_region)
            if clipped_geometry.is_empty:
                continue
            buffered_geometry = clipped_geometry.buffer(FLOODPLAIN_STREAM_BUFFER_METERS)
            relevant_geometry = buffered_geometry.intersection(final_relevance_region)
            if relevant_geometry.is_empty:
                continue
            pieces.append(relevant_geometry)
    except Exception as e:
        _log_fetch_failure("NHD stream/water-body fetch", e)

    try:
        wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
        if soil_components is None:
            soil_components = get_soil_data_for_polygon(wkt_polygon)
        hydric_mukeys = hydric_disqualifying_mukeys(soil_components)
        if hydric_mukeys:
            if soil_geometries is None:
                soil_geometries = get_soil_geometries_for_polygon(wkt_polygon)
            for mukey in hydric_mukeys:
                geometry = soil_geometries.get(mukey)
                if geometry is not None:
                    pieces.append(shape(transform_geom("EPSG:4326", dem["crs"], geometry)))
    except Exception as e:
        _log_fetch_failure("SSURGO hydric soil fetch", e)

    if pieces:
        return unary_union(pieces), False

    # Fallback: neither NHD nor SSURGO reachable -- use the valley network
    # already computed for pond-zone identification as a coarse,
    # elevation-only "probably wet ground follows drainage lines" proxy.
    fallback_pieces = []
    for valley in valleys:
        for branch in valley["branches_utm"]:
            line = LineString([(p[0], p[1]) for p in branch])
            fallback_pieces.append(line.buffer(FLOODPLAIN_STREAM_BUFFER_METERS))

    return (unary_union(fallback_pieces) if fallback_pieces else None), True


def identify_road_corridor_candidates(
    boundary_coordinates: list[tuple[float, float]],
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    dem: Optional[dict] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    production_areas: Optional[list[dict]] = None,
    valleys: Optional[list[dict]] = None,
    selected_water_zone: Optional[dict] = None,
    hydric_floodplain_union=None,
    floodplain_data_is_fallback: Optional[bool] = None,
    canopy_height: Optional[dict] = None,
    **corridor_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches/derives everything build_road_
    network() needs (optimized production areas, the selected water ground
    -- ONE zone-shaped value whose geometry is the UNION of the user's
    selected survey zones on the interactive path, or the batch path's own
    single pick -- and the floodplain cost-penalty union) and returns the
    "suggested_road_corridor" GeoJSON FeatureCollection for ONE network
    grown from anchor_lon_lat. Every real-data fetch degrades independently
    and gracefully, same pattern as water_candidate_zones.py and
    solar_suitability.py.

    canopy_height is an optional pre-fetched override (the same dict
    canopy_height_data.get_canopy_height_for_boundary() returns, e.g.
    parcel_data.ParcelData.canopy_height) forwarded into this function's
    own production_areas/selected_water_zone self-compute calls
    (identify_optimized_production_areas()/fetch_and_select_optimal_
    water_zone(), both of which already accept it) when those aren't
    themselves already overridden -- same nested-forwarding pattern
    solar_suitability.py's identify_solar_candidate_zones() and tree_
    zone_candidates.py's identify_tree_zone_candidates() already use, so
    a caller supplying canopy_height here never causes either nested call
    to issue its own redundant canopy fetch. It is ALSO used directly here,
    for the SOFT canopy road-cost term: this function builds a raw-canopy
    cell mask (production_area.get_required_tree_root_zone_mask_utm() at a
    0.0m buffer) and passes it into build_road_network() as canopy_mask,
    forwarding the same canopy_height override so this direct use shares the
    one fetch too. Unlike production/solar, this canopy use DEGRADES
    GRACEFULLY rather than gating: a canopy outage drops the soft term (the
    network is still generated), it does not abort the whole feature.

    anchor_lon_lat (lon, lat) is the real, chosen access point routing
    starts from (see build_road_network()) -- kept Optional here (default
    None) purely so every EXISTING caller of this entry point that hasn't
    been updated to supply a real anchor point yet keeps working exactly
    as it did for "no road network available" (the same empty-result shape
    any other not-yet-computable layer in this pipeline already returns)
    rather than a hard TypeError. This is NOT a fabricated fallback anchor
    point -- there is no reasonable one to invent (a real anchor is a
    product decision -- where does this property actually connect to the
    outside world -- not something derivable from the boundary alone), so
    None here simply means no network can be generated at all yet.

    dem, boundary_polygon_utm, production_areas, valleys, selected_water_
    zone, and hydric_floodplain_union are all optional overrides,
    independently of one another -- each falls back to being self-computed
    exactly as before if not supplied, same "reuse what an upstream
    orchestrator already computed" pattern water_candidate_zones.
    identify_water_system_candidate_zones() and water_suitability.
    identify_water_suitability() already established for these same
    values. When selected_water_zone isn't itself overridden, its self-
    compute fallback still passes this function's own already-sourced
    boundary_polygon_utm/valleys/production_areas through to fetch_and_
    select_optimal_water_zone() (which forwards them into identify_water_
    suitability() via **suitability_kwargs), so those three are never
    re-derived a third, independent time just to pick the water zone.
    selected_water_zone ALSO accepts water_suitability.NO_WATER_ZONE --
    the explicit "the water pipeline already ran and selected nothing"
    answer (see that constant's own docstring): it is normalized back to
    None here and the self-compute is SKIPPED, unlike a bare None (which
    is indistinguishable from "not supplied" under this pipeline's
    standard override convention and still self-computes as before).
    floodplain_data_is_fallback pairs with hydric_floodplain_union (see
    _fetch_floodplain_hydric_union()'s own return value) -- if a caller
    supplies hydric_floodplain_union directly without saying whether it's
    a real fetch or the valley-line fallback, it defaults to False (assume
    real) rather than silently mislabeling a genuine fallback union.

    Returns:
        {
            'zones_geojson': dict,                # one Feature per branch, ranked by nothing --
                                                    # see corridors_to_geojson()
            'road_network': dict,                 # build_road_network()'s own full return
            'selected_road_corridor': Optional[dict],  # the SAME road_network dict, or None
                                                         # when road_network['branches'] is empty
            'narrative_data': dict,               # report-facing, FINAL, JSON-serialisable
                                                    # values -- see build_narrative_data()
        }

    'narrative_data' is PURELY ADDITIVE: every other key above, and every
    field on the network/branches, is byte-identical to what this
    function returned before it existed. It answers two report questions
    (how was the suggested route determined / how much access does it
    provide to the farm) with pre-computed, imperial, rounded values a
    narrative can quote directly -- derived entirely from the network
    dict build_road_network() already returned, so adding it re-runs no
    routing pass, cost raster, or grade statistic. See
    build_narrative_data()'s own docstring for the field contract. It is
    attached on the no-anchor early return too, so a narrative can
    explain THAT outcome (network_found False, stop_reason
    'no_anchor_given') rather than finding the key missing.
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    if anchor_lon_lat is None:
        empty_network = _empty_road_network("no_anchor_given")
        return {
            "zones_geojson": corridors_to_geojson(empty_network),
            "road_network": empty_network,
            "selected_road_corridor": None,
            # No fetch ran on this early path -- every constraint flag is
            # honestly False ("never applied"), not a claimed exclusion.
            "narrative_data": build_narrative_data(
                empty_network,
                service_radius_meters=corridor_kwargs.get("service_radius_meters", PRODUCTION_SERVICE_RADIUS_METERS),
                water_zone_excluded=False,
                floodplain_data_available=False,
                floodplain_data_is_fallback=False,
                canopy_data_available=False,
                dem=dem,
            ),
        }

    if boundary_polygon_utm is None:
        boundary_xs, boundary_ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    # Optimized/final production geometry (production_area_ceiling.py's
    # own ceiling-trimmed, clustered/gated result) -- NOT production_area.
    # identify_production_areas()'s raw pre-optimization patches. Each
    # patch is expected to carry 'render_fill_polygon_utm' (production_
    # area.py's own render-only convex-hull field), the same field
    # build_road_network() hard-excludes/soft-penalizes against below;
    # fail loudly here rather than let a KeyError surface deep inside that
    # masking code if this pipeline's own patch shape ever changes.
    if production_areas is None:
        production_areas = identify_optimized_production_areas(
            boundary_coordinates, dem=dem, canopy_height=canopy_height
        )["scored_patches"]
    if production_areas and "render_fill_polygon_utm" not in production_areas[0]:
        raise RuntimeError(
            "identify_optimized_production_areas()'s scored_patches no longer carry "
            "'render_fill_polygon_utm' -- road_corridors.py's production-zone soft penalty/demand mask "
            "depends on this field; update build_road_network() to match the new shape."
        )

    if valleys is None:
        valleys = delineate_valleys(dem)  # reused for the floodplain fallback below

    # The selected water ground: on the interactive path the UNION of the
    # user's committed survey zones (wire_translation.water_zone_union()),
    # on the batch path the one zone its own scoring picked -- never every
    # unscored candidate zone water_candidate_zones.py generates (see
    # module docstring). water_suitability.NO_WATER_ZONE is the EXPLICIT
    # "already ran the selection, nothing qualified" answer (see that
    # constant's own docstring) AND the answer an EMPTY water commit
    # arrives as: reuse it (normalized back to None -- everything below
    # keeps None's existing "no zone" meaning) rather than treating it as
    # "not supplied" and re-running the whole water pipeline, which would
    # override a user's deliberate "no water zone". A bare None still
    # self-computes exactly as before.
    if selected_water_zone is NO_WATER_ZONE:
        selected_water_zone = None
    elif selected_water_zone is None:
        selected_water_zone = fetch_and_select_optimal_water_zone(
            boundary_coordinates,
            dem=dem,
            boundary_polygon_utm=boundary_polygon_utm,
            valleys=valleys,
            production_areas=production_areas,
            canopy_height=canopy_height,
        )

    if hydric_floodplain_union is None:
        hydric_floodplain_union, floodplain_data_is_fallback = _fetch_floodplain_hydric_union(
            boundary_coordinates, dem, valleys, boundary_polygon_utm
        )
    elif floodplain_data_is_fallback is None:
        floodplain_data_is_fallback = False

    canopy_mask = _fetch_canopy_soft_cost_mask(boundary_polygon_utm, dem, canopy_height=canopy_height)

    road_network = build_road_network(
        dem,
        production_areas,
        selected_water_zone,
        boundary_polygon_utm,
        anchor_lon_lat,
        hydric_floodplain_union=hydric_floodplain_union,
        canopy_mask=canopy_mask,
        **corridor_kwargs,
    )

    return {
        "zones_geojson": corridors_to_geojson(
            road_network,
            floodplain_data_is_fallback=floodplain_data_is_fallback,
        ),
        "road_network": road_network,
        "selected_road_corridor": road_network if road_network["branches"] else None,
        "narrative_data": build_narrative_data(
            road_network,
            # The value the router ACTUALLY used -- a corridor_kwargs
            # override, or road_network_router.py's own default.
            service_radius_meters=corridor_kwargs.get("service_radius_meters", PRODUCTION_SERVICE_RADIUS_METERS),
            water_zone_excluded=selected_water_zone is not None,
            floodplain_data_available=hydric_floodplain_union is not None,
            floodplain_data_is_fallback=bool(floodplain_data_is_fallback),
            canopy_data_available=canopy_mask is not None,
            dem=dem,
        ),
    }


def road_network_is_empty(result: dict) -> bool:
    """
    True when identify_road_corridor_candidates()'s result holds NO ROUTED
    NETWORK -- the router grew nothing from this access point.

    THE STEP REGISTRY'S OWN PREDICATE, declared by the roads entry as
    Accumulation.empty_result and called by step_orchestrator.py, which
    cannot read this result shape for itself. What "no network" means is
    this module's fact, so the test for it lives with the function that
    produces it rather than as an `if step_id == "roads"` in the
    orchestrator.

    READS narrative_data.network_found, which is `bool(branches)` -- the
    same emptiness build_narrative_data() already publishes, not a second
    opinion assembled here. Every no-network outcome reaches this as
    False: the router's own stop_reason values (no_demand,
    all_demand_served, no_reachable_demand, cost_per_acre_exceeded on the
    very first candidate, all_branches_below_minimum once leaf pruning
    took the last branch) and _empty_road_network()'s two (no_anchor_given,
    no_eligible_anchor) alike. They are one answer to the user -- "no road
    comes from here" -- and the access point is the thing they have in
    common, which is why the orchestrator treats them the same.

    NOT AN ERROR, and this returning True is not a failure of this module:
    a parcel with no production ground honestly has no road to build. It is
    the ORCHESTRATOR that decides an access point which routes nothing is
    not a candidate worth a slot.
    """
    return not bool(result["narrative_data"]["network_found"])


def fetch_and_select_optimal_road_corridor(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    **corridor_kwargs,
) -> Optional[dict]:
    """
    Convenience wrapper for callers that want a single GeoJSON Feature
    (the network's own trunk branch, feature 0) rather than the full
    FeatureCollection -- identify_road_corridor_candidates() already
    returns one feature per branch, trunk first (branch_index 0), so this
    just returns that top GeoJSON Feature (or None if nothing cleared the
    constraint stack, or no anchor_lon_lat was given).
    """
    result = identify_road_corridor_candidates(boundary_coordinates, dem=dem, **corridor_kwargs)
    features = result["zones_geojson"]["features"]
    return features[0] if features else None


def summarize_road_corridor_candidates(result: dict) -> str:
    network = result["road_network"]
    if not network["branches"]:
        return f"No road network generated (stop_reason={network['stop_reason']})."

    lines = [
        f"Road network: {len(network['branches'])} branch(es), "
        f"{network['total_length_meters']:.0f}m total length, "
        f"{network['total_served_acres']:.2f} acres served, "
        f"{network['unserved_acres']:.2f} acres unserved (stop_reason={network['stop_reason']})."
    ]
    for branch in network["branches"]:
        joins = f", joins branch {branch['joins_branch_index']}" if branch["joins_branch_index"] is not None else ""
        crossing = " [crosses floodplain]" if branch["crosses_floodplain"] else ""
        crossing += " [crosses production zone]" if branch["crosses_production_zone"] else ""
        lines.append(
            f"  - Branch {branch['branch_index']} ({branch['branch_role']}{joins}): "
            f"{branch['length_meters']:.0f}m, {branch['avg_grade_pct']:.1f}% avg grade, "
            f"{branch['newly_served_acres']:.2f} new acres served{crossing}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]
    anchor_lon_lat = (-79.98356157031265, 40.64303511679458)

    print("Identifying suggested road network for property boundary...\n")

    try:
        result = identify_road_corridor_candidates(property_boundary, anchor_lon_lat=anchor_lon_lat)
        print(summarize_road_corridor_candidates(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
