"""
solar_suitability.py

Solar/structure siting data layer for permanent building placement (Scale
of Permanence step 6): ranks candidate SITES for a small, fixed-footprint
solar-generating structure (e.g. a barn or shed with rooftop panels), not
a large ground-mounted array. This produces RANKED CANDIDATES, not a
single placement decision — Claude narrates the tradeoffs between them in
the report (see report_generator.py step 6). Finding "the one best spot"
is explicitly not this module's job.

CONSTRAINT STACK, this pass (brings this layer in line with the rest of
the pipeline, which has since moved to render_fill_polygon_utm-based
exclusions, optimized/ceiling-trimmed production geometry, and a real
selected road corridor):
  - Production geometry is now production_area_ceiling.
    identify_optimized_production_areas()'s OPTIMIZED, ceiling-trimmed
    'scored_patches' (NOT production_area.identify_production_areas()'s
    raw, un-trimmed candidates), and every union built from it uses each
    patch's own 'render_fill_polygon_utm' (NOT 'polygon_utm') — same
    "reads as one coherent shape" field/reasoning tree_zone_candidates.py
    already uses. Still NOT a hard exclusion for solar — production stays
    a scored edge-proximity PREFERENCE (see PRODUCTION_PROXIMITY_SCORE_
    WEIGHT below); only the source geometry/field changed.
  - Water-candidate zone exclusion now also uses the single selected
    water zone's own 'render_fill_polygon_utm' (not 'polygon_utm') —
    still a HARD, buffered exclusion (POND_ZONE_EXCLUSION_BUFFER_METERS,
    reused from road_corridors.py), unchanged in spirit.
  - Real, existing tree canopy is now a NEW hard exclusion: a candidate
    footprint touching real USGS 3DEP lidar canopy coverage (production_
    area.get_required_tree_root_zone_mask_utm(), buffered by
    TREE_ROOT_ZONE_BUFFER_METERS) is excluded outright, before any
    scoring happens — same "excluded before scoring" treatment as water.
    THIS FETCH IS MANDATORY AND DOES NOT DEGRADE: unlike every other
    network-backed layer in this module (which all fail gracefully — a
    real outage shouldn't block candidates from being identified at
    all), a canopy-data outage here is left to propagate UNCAUGHT and
    fails the whole run. This is a deliberate, intentional asymmetry, not
    an oversight: siting a structure on or under real, existing tree
    cover without knowing it is a genuine physical siting error, not a
    lower-confidence result worth handing back with a caveat — the exact
    same "can't verify this is free of tree cover, refuse rather than
    guess" reasoning production_area.py's/production_area_ceiling.py's/
    tree_zone_candidates.py's own mandatory canopy gates already use.
    Switching production to optimized geometry (above) already pulls in
    a SECOND, fully independent mandatory canopy gate inside
    identify_optimized_production_areas() itself (its own
    CanopyCoverageIncompleteError/RuntimeError) — both are expected to
    hard-fail independently on a canopy outage; neither is caught here.
  - A NEW hard exclusion against EVERY ranked tree-zone candidate
    (tree_zone_candidates.identify_tree_zone_candidates()'s own 'patches'
    — the full ranked list, not just the top one: unlike water/road,
    trees has no single "selected" zone by design, since each ranked
    patch is independently, separately plantable). A candidate footprint
    intersecting the union of every tree-zone candidate's own
    'render_fill_polygon_utm', buffered by
    TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS (10ft — a real,
    independently-tunable clearance, NOT reused from
    POND_ZONE_EXCLUSION_BUFFER_METERS or TREE_ROOT_ZONE_BUFFER_METERS,
    which mean different things), is excluded. UNLIKE the canopy gate
    above, this call degrades GRACEFULLY on an ordinary fetch failure
    (network outage, etc. — noted in confidence_notes) — identify_tree_
    zone_candidates() itself is a real, network-fetch-heavy dependency
    (it calls into production/water/road and does its own soil/stream
    fetches), not a single mandatory building block the way the canopy
    mask itself is. The ONE exception: if that failure is specifically
    canopy_height_data.CanopyCoverageIncompleteError bubbling up from
    INSIDE identify_tree_zone_candidates()'s own mandatory canopy gate,
    it is left to propagate uncaught here too, same reasoning as above —
    not caught and downgraded to "couldn't check this run."
  - Road proximity is now TWO-TIER instead of "real road, else a
    DEM-only suggested corridor treated as a road stand-in":
      Tier 1 (primary): the property's own single SELECTED road corridor
        (road_corridors.identify_road_corridor_candidates(), given this
        module's own anchor_lon_lat parameter — the real, user-picked
        access point, threaded down from generate_full_report.py) — its
        'cell_footprint_polygon_utm', within ROAD_CORRIDOR_PROXIMITY_METERS
        (15m). This corridor is the real primary source now, not a
        stand-in for missing data.
      Tier 2 (fallback): only if Tier 1 produces ZERO candidates (no
        selected corridor exists at all, OR one exists but nothing
        survives every other constraint near it) — real mapped roads
        (farm_roads_data.get_farm_roads_for_boundary()), at the original
        ROAD_PROXIMITY_BUFFER_METERS (150m). This is exactly the
        pre-this-pass road-proximity logic, demoted from primary to
        fallback.
    If Tier 2's own fetch fails outright (a network error, not "zero
    roads found") AND Tier 1 also produced nothing, the road constraint
    is disabled entirely (flagged in confidence_notes) — same terminal
    fallback behavior as before. properties.road_proximity_source
    reports which tier actually produced the result: "selected_road_
    corridor" | "real_mapped_road" | "unavailable".
  - Before grid-sampling candidate points at all, the SEARCH REGION is
    restricted to boundary_polygon_utm intersected with whichever tier's
    road source geometry, buffered by its own proximity buffer plus one
    footprint side length (so a footprint reaching the buffer from just
    outside isn't missed) — a pure GENERATION-TIME optimization, not a
    correctness change: the real per-footprint distance gate
    (footprint.distance(road_union) <= proximity_buffer) still runs
    unchanged and is what actually decides eligibility.
  - Scoring weights are now an even 0.25/0.25/0.25/0.25 split across
    slope/aspect/shading/production-proximity (see SLOPE_SCORE_WEIGHT
    etc. below) — a deliberate simplification from the previous
    0.35/0.25/0.25/0.15 split, unrelated to this pass's constraint
    changes.

POINT-CANDIDATE MODEL (this module's second design; see below for why the
first one — a broad eligible-AREA polygon — was replaced):

    DEM (dem_data.py, already in main)
        --> slope/aspect/shading (terrain_metrics.py)
        --> candidate points, sampled on a grid across the property
        --> [this module] per-point scoring:
                slope + aspect + shading (real DEM signals, averaged over
                    a small local window matching the candidate's own
                    capped footprint)
                + production-zone-edge PROXIMITY (a preference, not an
                    exclusion — see below)
        --> water-candidate zones (water_candidate_zones.py) -- still a
            HARD exclusion (buffered), unchanged in spirit (source field
            updated, see CONSTRAINT STACK above)
        --> real, existing tree canopy -- NEW hard exclusion, mandatory/
            non-degrading (see CONSTRAINT STACK above)
        --> every ranked tree-zone candidate (tree_zone_candidates.py) --
            NEW hard exclusion, buffered, gracefully degrading (see
            CONSTRAINT STACK above)
        --> the selected road corridor, else farm roads (farm_roads_data.py)
            -- two-tier hard proximity constraint + reported distance (see
            CONSTRAINT STACK above)
        --> ranked candidate structure-footprint polygons
            (layer="solar_infrastructure")

WHY THIS REPLACED THE EARLIER ZONE MODEL: the original version of this
layer computed one broad eligible-AREA polygon per connected component of
low-slope, well-scored ground (the same "zone" shape production_area.py
uses), then hard-EXCLUDED it from production zones (buffered) — the same
mental model as production, water, or a future trees/windbreak layer,
which really are competing land uses over a large area. A small solar-
generating STRUCTURE is not that: it's a point-footprint building that can
genuinely coexist with production land around or even under its own small
footprint, the same way a shed can sit at the edge of a field without
taking that field out of production. Modeling it as a broad excluded zone
was the wrong shape for what it represents — and that mismatch became a
real, live bug once production_area.py's own slope ceiling was raised to
match this module's MAX_SOLAR_SLOPE_PCT (both 20%): with both layers
drawing eligibility from nearly the same gentle-ground footprint,
production's zone exclusion consumed essentially all of solar's own
eligible area, and real-property runs started returning ZERO candidates.
Modeling this as small, independently-scored POINT candidates instead of
one shared area-based eligibility pool fixes that at the root, not just
by re-tuning thresholds again.

Water-candidate zones (pond/dam siting ground) are still HARD-excluded
(buffered) — a solar-generating structure sitting on or immediately
against a candidate pond/dam site is a real physical conflict a small
building can't route around the way it can sit near/inside production
land. The exclusion buffer reuses road_corridors.py's own
POND_ZONE_EXCLUSION_BUFFER_METERS rather than a new constant, same as
before. find_candidate_solar_zones() itself still hard-excludes every
water zone it's GIVEN (unchanged) — but identify_solar_candidate_zones(),
its full-pipeline caller, now passes only the SINGLE selected water zone
(water_suitability.select_optimal_water_zone(), same selection already
reused by tree_zone_candidates.py in this pipeline), not every water
candidate. Confirmed live: excluding all of a real property's several
separately-legitimate, separately-buffered water zones can together cover
enough of a small parcel to zero out every solar candidate, even though
each zone's own geometry is individually normal — per product decision,
this app targets small farms only, so one well-suited water zone is
sufficient to exclude against.

find_candidate_solar_zones() is the geometric/scoring core: it takes an
already-fetched DEM dict, production areas, water-candidate zones, a
road source geometry (called once per tier — see CONSTRAINT STACK above),
an already-fetched canopy mask, and an already-computed tree-zone
exclusion polygon (all in the DEM's own projected CRS) and does no
network I/O itself — same reason as water_candidate_zones.py's
find_candidate_zones(): so the scoring logic is unit-testable against a
synthetic DEM independent of whether any of the real data fetches (DEM,
roads, SSURGO, canopy) are working. canopy_mask_utm/tree_zone_exclusion_
polygon_utm both default to None ("gate not applied at all" — useful for
callers/tests that don't care about either exclusion, same "pure-logic
core's own default" reasoning tree_zone_candidates.score_tree_search_
space() already uses for its own canopy mask parameter); identify_solar_
candidate_zones() (the full pipeline entry point) always supplies a real
canopy mask, since its own fetch is mandatory and non-degrading.
flag_prime_farmland_conflicts() is a second, separate pure function for
exactly the same reason, applied to the SSURGO farmland lookup
specifically — unchanged by this pass.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

from canopy_height_data import CanopyCoverageIncompleteError, TREE_ROOT_ZONE_BUFFER_METERS
from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import get_required_tree_root_zone_mask_utm
from production_area_ceiling import identify_optimized_production_areas
from raster_grid import SQUARE_METERS_PER_ACRE, pixel_center_xy
from road_corridors import POND_ZONE_EXCLUSION_BUFFER_METERS, identify_road_corridor_candidates
from soil_data import coordinates_to_wkt_polygon, get_farmland_classification_for_polygon, is_prime_farmland
from terrain_metrics import aspect_score, aspect_to_compass_label, compute_shading_score, compute_slope_and_aspect
from tree_zone_candidates import identify_tree_zone_candidates
from water_suitability import identify_water_suitability

METERS_PER_FOOT = 0.3048

# Ground a solar-generating structure sits on can tolerate more grade than
# row-crop production land, but not arbitrarily much — beyond this, site
# prep (grading, foundation work) starts dominating cost. A candidate
# point whose local average slope exceeds this is hard-excluded, not just
# scored down: this is a real buildability ceiling, unrelated to
# production-zone proximity (see PRODUCTION_PROXIMITY_SCORE_WEIGHT below
# for how proximity is handled — as a preference, not a constraint).
# CONFIGURABLE.
MAX_SOLAR_SLOPE_PCT = 20.0

# How many candidate points to sample per acre... no — see
# CANDIDATE_POINT_SPACING_METERS: sampling is grid-spacing-based, not
# count-based, so density scales naturally with property size.

# Grid spacing (meters) between sampled candidate points (Step 1 of the
# point-candidate model). Deliberately LARGER than MAX_STRUCTURE_FOOTPRINT_ACRES's
# own footprint side length (~63.6m at the 1-acre default — see
# _footprint_side_meters()) so neighboring candidates' footprints never
# overlap by construction: each sampled point becomes its own genuinely
# distinct siting option for Claude/the user to compare, not a cloud of
# near-duplicate, heavily-overlapping candidates the way a tighter grid
# would produce. 75m leaves a real (~11.4m) gap between adjacent
# candidate footprints — plausible real spacing between structures on a
# working farm, not just the mathematical minimum. CONFIGURABLE — must
# stay above the footprint side length or neighboring candidates will
# start overlapping.
CANDIDATE_POINT_SPACING_METERS = 75.0

# Maximum footprint for one candidate structure (Step 3): this models a
# small building (a barn/shed with rooftop panels), not a ground-mounted
# array, so its footprint is capped small and fixed, not sized to however
# much contiguous eligible ground happens to exist at that point. Named,
# documented constant rather than a literal so the "how big is a
# candidate structure" assumption is visible and tunable in one place.
# CONFIGURABLE.
MAX_STRUCTURE_FOOTPRINT_ACRES = 1.0

# A candidate point near the property boundary can have its nominal
# footprint clipped down by boundary_polygon_utm (see
# find_candidate_solar_zones()'s parcel-clipping reasoning, same pattern
# as every other layer in this pipeline). Below this fraction of the
# nominal footprint's own area, what's left is more sliver than usable
# building pad, and the candidate is dropped rather than reported as a
# technically-nonempty but meaningless remainder. CONFIGURABLE.
MIN_STRUCTURE_FOOTPRINT_FRACTION = 0.5

# Weights for the combined 0-1 suitability score (must sum to 1.0). Even
# 0.25 split across all four factors — slope/aspect/shading determine
# whether a site is buildable and productive at all, while production
# proximity is a layout nicety on top of that; an equal split is a
# deliberate simplification from an earlier 0.35/0.25/0.25/0.15 split
# that weighted slope highest and production proximity lowest.
# CONFIGURABLE — tune against your own property once real production
# data is available to check the ranking against.
SLOPE_SCORE_WEIGHT = 0.25
ASPECT_SCORE_WEIGHT = 0.25
SHADING_SCORE_WEIGHT = 0.25
PRODUCTION_PROXIMITY_SCORE_WEIGHT = 0.25

_WEIGHT_SUM = SLOPE_SCORE_WEIGHT + ASPECT_SCORE_WEIGHT + SHADING_SCORE_WEIGHT + PRODUCTION_PROXIMITY_SCORE_WEIGHT
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"solar suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

# Below this combined score (0-1 scale), a point isn't worth surfacing as
# a candidate at all, even if it technically clears every hard
# constraint. CONFIGURABLE.
MIN_SUITABILITY_SCORE = 0.4

# Production-zone-edge PROXIMITY scoring (Step 4 — NOT an exclusion; see
# module docstring for why production zones are no longer hard-excluded
# here). distance_to_production_zone_edge is measured to the nearest
# production zone's own BOUNDARY LINE, not its filled area — this is
# deliberate: a shapely .distance() to a filled polygon reads 0 for BOTH
# "just touching the edge" and "buried deep in the middle," which would
# make a candidate near an edge indistinguishable from one that isn't.
# Measuring to the boundary line instead means proximity peaks right at
# an edge (approached from either inside or outside) and falls off in
# both directions from there — exactly the "prefer the edge, don't
# require production land at all, don't over-reward the deep interior"
# shape this feature asks for.
#
# Score is 1.0 right at a production zone's edge, falling linearly to 0.0
# at or beyond this reference distance (in either direction — outside
# past this range, or buried this deep inside a large zone, away from any
# edge). CONFIGURABLE.
PRODUCTION_PROXIMITY_REFERENCE_METERS = 100.0

# Within this distance of a production zone's own edge (but not
# overlapping it), a candidate is classified "adjacent" rather than
# "outside" in properties.production_zone_relationship (see
# _classify_production_zone_relationship()). Reuses the same distance
# value this module's PREVIOUS zone-exclusion-buffer constant used
# (production zones used to be hard-excluded by this same margin) —
# repurposed here as a classification threshold, not an exclusion.
# CONFIGURABLE.
PRODUCTION_EDGE_ADJACENCY_METERS = 15.0

# TIER 1 (primary): candidates must be within this distance of the
# property's own single SELECTED road corridor (road_corridors.
# identify_road_corridor_candidates()'s own 'cell_footprint_polygon_utm')
# to be considered reachable/wireable at all — a hard constraint, not
# just a scoring input. Deliberately much tighter than the TIER 2
# fallback buffer below: a corridor is a real, specific routed alignment
# on THIS property, not a generic "somewhere near a mapped road" signal,
# so a candidate can reasonably be expected to sit close to it, not just
# within the same broad neighborhood. CONFIGURABLE.
ROAD_CORRIDOR_PROXIMITY_METERS = 15.0

# TIER 2 (fallback, only used when Tier 1 produces zero candidates — see
# module docstring): candidates must be within this distance of a real
# mapped road (farm_roads_data.py) instead. This is the original,
# pre-this-pass road-proximity buffer, demoted from primary to fallback.
# CONFIGURABLE.
ROAD_PROXIMITY_BUFFER_METERS = 150.0

# Buffer (meters) around the union of EVERY ranked tree-zone candidate
# (tree_zone_candidates.identify_tree_zone_candidates()'s own 'patches' —
# the full list, not just the top-ranked one, since trees has no single
# "selected" zone by design) within which a solar candidate footprint is
# HARD-excluded — see module docstring. Deliberately NOT the same
# constant as POND_ZONE_EXCLUSION_BUFFER_METERS (a dam-face/catchment-
# inlet clearance) or TREE_ROOT_ZONE_BUFFER_METERS (an existing-canopy
# root-zone clearance) even though the values may currently be close —
# this one means "structure-to-planned-tree-zone clearance" specifically
# and should be free to drift independently later. 10ft. CONFIGURABLE.
TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS = 10 * METERS_PER_FOOT

# How many top-ranked candidates to return. Deliberately more than 1 —
# per this feature's framing, ties/close calls should surface as multiple
# candidates for Claude to compare, not get silently collapsed into one.
# CONFIGURABLE.
MAX_CANDIDATES = 5

SOLAR_CONFIDENCE_NOTES_TEMPLATE = (
    "This identifies a ranked CANDIDATE SITE for a small, fixed-footprint solar-generating "
    "structure (e.g. a barn or shed with rooftop panels) — NOT a large ground-mounted array, and "
    "NOT a final placement decision; see the report's Permanent Buildings section for tradeoffs "
    "against other ranked candidates. Each candidate is a real point sampled on a {spacing_m:.0f}m "
    "grid across the property, scored from real DEM-derived slope and aspect and a shading proxy "
    "averaged over a small local window matching its own capped footprint (at most "
    "{max_footprint_acres} acre(s), {footprint_side_m:.0f}m per side) — a small structure footprint, "
    "not a large connected-component eligible-area polygon. Shading is {shading_caveat} "
    "Candidates MAY sit fully inside a production zone — that is INTENTIONAL, not a caveat to "
    "apologize for: a small structure genuinely can coexist with production land around and under "
    "it, unlike a pond/dam site. Proximity to a production zone's own edge is a scored PREFERENCE, "
    "not a requirement (see properties.production_zone_relationship and "
    "properties.distance_to_production_zone_ft) — a candidate far from any production zone, or "
    "sitting deep inside a large one away from its edge, is still a valid, real candidate, just a "
    "lower-preference one. Water-candidate (pond/dam siting) zones ARE still hard-excluded "
    "(buffered) — a structure should not sit on or immediately against that ground. Real, EXISTING "
    "tree canopy (USGS 3DEP lidar) is ALSO hard-excluded (buffered by "
    "{canopy_buffer_ft:.0f}ft) — this check is MANDATORY and does not degrade; a canopy-data outage "
    "fails this run outright rather than silently skip it. Every ranked TREE-ZONE CANDIDATE "
    "(tree_zone_candidates.py, the full ranked list, not just the top one) is ALSO hard-excluded, "
    "buffered by {tree_zone_buffer_ft:.0f}ft{tree_zone_availability_note}. It also "
    "inherits the limitations of production_area_ceiling.py (a slope-only production-zone "
    "heuristic, ceiling-trimmed), water_candidate_zones.py (a DEM-derived valley/gradient "
    "heuristic), road_corridors.py (a DEM-only topographic suggestion, not a surveyed alignment), "
    "and farm_roads_data.py (public road/right-of-way data only — may miss private farm tracks). "
    "{road_proximity_note}{farmland_note}Treat this as a starting shortlist to walk and "
    "ground-truth, not a final site plan."
)

ROAD_PROXIMITY_NOTE_BY_SOURCE = {
    "selected_road_corridor": (
        "Road-proximity scoring (distance_to_road_ft) is measured against the property's own single "
        "SELECTED road corridor (road_corridors.py) — a real, ridge-routed topographic suggestion "
        "specific to this property, not a surveyed alignment — within "
        f"{ROAD_CORRIDOR_PROXIMITY_METERS:.0f}m. "
    ),
    "real_mapped_road": (
        "No selected road corridor was available (or nothing survived the constraint stack near it), "
        "so road-proximity scoring fell back to real mapped road data (farm_roads_data.py, public "
        f"road/right-of-way data only) within {ROAD_PROXIMITY_BUFFER_METERS:.0f}m instead. "
    ),
    "unavailable": (
        "Neither a selected road corridor nor real mapped road data was available for this run, so "
        "the road-proximity constraint is disabled entirely for these candidates — "
        "distance_to_road_ft is null. "
    ),
}

TREE_ZONE_EXCLUSION_UNAVAILABLE_NOTE = (
    " (tree-zone candidate data was not available for this run, so this exclusion could not be "
    "checked — candidates here are NOT confirmed clear of planned tree-zone ground)"
)

SHADING_CAVEAT_HORIZON_ONLY = (
    "estimated from a DEM-only horizon/terrain-shading proxy (terrain_metrics.py) — "
    "this has no way to see vegetation or tree canopy, since no canopy height model "
    "(DSM) or NDVI data was available/used for this run. A real canopy height model "
    "would be a meaningfully more accurate shading signal than this."
)


def _footprint_side_meters(max_structure_footprint_acres: float) -> float:
    """Side length of the square footprint a candidate is capped at —
    derived from the acreage cap rather than a separately-configured
    literal, so the two can never drift apart."""
    return math.sqrt(max_structure_footprint_acres * SQUARE_METERS_PER_ACRE)


def _slope_score(slope_pct: float, max_slope_pct: float) -> float:
    return max(0.0, 1.0 - slope_pct / max_slope_pct)


def _production_proximity_score(
    distance_to_production_edge_m: Optional[float],
    reference_meters: float = PRODUCTION_PROXIMITY_REFERENCE_METERS,
) -> float:
    """
    0-1 preference score for how close a candidate sits to a production
    zone's own EDGE (see PRODUCTION_PROXIMITY_REFERENCE_METERS above for
    why distance is measured to the boundary line, not the filled area):
    1.0 right at an edge, falling linearly to 0.0 at reference_meters or
    beyond — whether that's out past every production zone, or buried
    that deep inside a large one, away from its own boundary.

    None (no production zones exist on this property at all) scores a
    neutral 0.5 — there's no production geometry to be near or far from,
    so this axis shouldn't reward or penalize the candidate either way.
    """
    if distance_to_production_edge_m is None:
        return 0.5
    return max(0.0, 1.0 - distance_to_production_edge_m / reference_meters)


def _classify_production_zone_relationship(
    footprint_polygon,
    raw_production_union,
    distance_to_production_edge_m: Optional[float],
    adjacency_meters: float,
) -> str:
    """properties.production_zone_relationship: 'inside' if the candidate's
    own footprint overlaps any production zone at all, 'adjacent' if it
    doesn't but sits within adjacency_meters of one's edge, else
    'outside' (including the case where no production zones exist on
    this property at all)."""
    if raw_production_union is not None and footprint_polygon.intersects(raw_production_union):
        return "inside"
    if distance_to_production_edge_m is not None and distance_to_production_edge_m <= adjacency_meters:
        return "adjacent"
    return "outside"


def _circular_mean_aspect_deg(aspect_values_deg: list[float]) -> Optional[float]:
    """Mean compass bearing via vector averaging (a plain arithmetic mean
    of e.g. 350 deg and 10 deg would wrongly give 180 instead of 0).
    Returns None if every input is undefined (an all-flat candidate)."""
    valid = [a for a in aspect_values_deg if not math.isnan(a)]
    if not valid:
        return None
    sin_sum = sum(math.sin(math.radians(a)) for a in valid)
    cos_sum = sum(math.cos(math.radians(a)) for a in valid)
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def _generate_candidate_points(
    boundary_polygon_utm: Polygon, spacing_meters: float = CANDIDATE_POINT_SPACING_METERS
) -> list[tuple[float, float]]:
    """
    Step 1 of the point-candidate model: samples candidate locations on a
    regular grid across the property's bounding box at spacing_meters
    spacing, keeping only points that fall on-parcel (boundary_polygon_utm
    is the real drawn parcel, not the DEM's buffered fetch extent — same
    on-parcel reasoning every other layer in this pipeline already uses).
    """
    minx, miny, maxx, maxy = boundary_polygon_utm.bounds
    boundary_prepared = prep(boundary_polygon_utm)

    points = []
    y = miny
    while y <= maxy:
        x = minx
        while x <= maxx:
            point = Point(x, y)
            if boundary_prepared.contains(point):
                points.append((x, y))
            x += spacing_meters
        y += spacing_meters
    return points


def _cells_within_polygon(dem: dict, polygon, rows: int, cols: int) -> list[tuple[int, int]]:
    """DEM cell (row, col) indices whose pixel-center point falls within
    polygon — the local scoring window for one candidate's footprint
    (Step 2). Scans only polygon's own bounding box (padded by one cell),
    not the whole grid: a ~1-acre footprint only ever touches a handful
    of cells, so this stays cheap even for a dense sample grid."""
    minx, miny, maxx, maxy = polygon.bounds
    px, py = dem["resolution_meters"]
    origin_x, origin_y = dem["origin_x"], dem["origin_y"]

    col_lo = max(0, int((minx - origin_x) / px) - 1)
    col_hi = min(cols - 1, int((maxx - origin_x) / px) + 1)
    row_lo = max(0, int((origin_y - maxy) / py) - 1)
    row_hi = min(rows - 1, int((origin_y - miny) / py) + 1)

    prepared = prep(polygon)
    cells = []
    for r in range(row_lo, row_hi + 1):
        for c in range(col_lo, col_hi + 1):
            if prepared.contains(Point(pixel_center_xy(dem, r, c))):
                cells.append((r, c))
    return cells


def find_candidate_solar_zones(
    dem: dict,
    production_areas: list[dict],
    water_zones: list[dict],
    road_geometries_utm: Optional[list],
    boundary_polygon_utm: Polygon,
    canopy_mask_utm: Optional[np.ndarray] = None,
    tree_zone_exclusion_polygon_utm: Optional[object] = None,
    max_solar_slope_pct: float = MAX_SOLAR_SLOPE_PCT,
    min_suitability_score: float = MIN_SUITABILITY_SCORE,
    water_zone_exclusion_buffer_meters: float = POND_ZONE_EXCLUSION_BUFFER_METERS,
    road_proximity_buffer_meters: float = ROAD_PROXIMITY_BUFFER_METERS,
    max_structure_footprint_acres: float = MAX_STRUCTURE_FOOTPRINT_ACRES,
    candidate_point_spacing_meters: float = CANDIDATE_POINT_SPACING_METERS,
    production_edge_adjacency_meters: float = PRODUCTION_EDGE_ADJACENCY_METERS,
    production_proximity_reference_meters: float = PRODUCTION_PROXIMITY_REFERENCE_METERS,
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict]:
    """
    Pure point-candidate scoring core — see module docstring for why this
    takes already-computed inputs rather than fetching anything, and for
    why this samples independently-scored POINT candidates rather than
    computing one shared eligible-AREA polygon the way this module used
    to. Called ONCE PER ROAD TIER by identify_solar_candidate_zones() (see
    module docstring) — road_geometries_utm/road_proximity_buffer_meters
    are the generalized "road source geometry + its own proximity buffer"
    parameters that make that possible without duplicating the rest of
    this scoring core per tier.

    water_zones is water_suitability.py's own selected-zone shape (each
    entry carrying 'render_fill_polygon_utm', NOT 'polygon_utm' — see
    module docstring) — hard-excluded (buffered) exactly as before;
    production_areas is production_area_ceiling.identify_optimized_
    production_areas()'s own OPTIMIZED 'scored_patches' shape (each entry
    carrying 'render_fill_polygon_utm', NOT 'polygon_utm') but is NOT a
    hard exclusion here — see PRODUCTION_PROXIMITY_SCORE_WEIGHT above for
    how it's used instead (a scored edge-proximity preference).

    canopy_mask_utm is a per-cell boolean np.ndarray on the DEM's own
    grid (production_area.get_required_tree_root_zone_mask_utm()'s own
    output) or None. A candidate whose footprint touches ANY True cell is
    HARD-excluded before any scoring happens, same "excluded before
    scoring, not merely scored low" treatment as water. None means "gate
    not applied at all" — this pure-logic core's own default, useful for
    callers/tests that don't care about canopy (same convention tree_
    zone_candidates.score_tree_search_space() already uses for its own
    canopy mask parameter); identify_solar_candidate_zones() (the full
    pipeline entry point) always supplies a real mask, since its own
    canopy fetch is MANDATORY and does not degrade (see module
    docstring).

    tree_zone_exclusion_polygon_utm is an already-buffered shapely
    geometry (the union of every ranked tree-zone candidate's own
    'render_fill_polygon_utm', buffered by
    TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS — see module docstring)
    or None. A candidate footprint intersecting it is HARD-excluded, same
    pattern as the water exclusion below. None means either "gate not
    applied" (a caller/test that doesn't care) or "checked, but genuinely
    no tree-zone candidates exist on this property" — both cases result
    in no exclusion, which is correct either way; a fetch that failed
    outright is the caller's (identify_solar_candidate_zones()'s) own
    concern to flag in confidence_notes, not this pure core's.

    road_geometries_utm=None means "road data unavailable for this tier"
    (the fetch itself failed, or there's no selected road corridor at
    all) and disables the road-proximity constraint entirely for this
    call (with that noted by the caller); an empty list [] means
    "fetched successfully, no roads found nearby" and is treated as a
    real, binding constraint (nothing will qualify) — unchanged from
    before.

    boundary_polygon_utm is the real parcel (NOT the DEM's buffered
    extent — dem_data.py fetches ~100m past the drawn boundary on
    purpose). Each candidate's nominal (fixed-size) footprint is
    intersected with it — a footprint sampled near the boundary can come
    back smaller than the nominal cap, or be dropped entirely if too
    little survives (see MIN_STRUCTURE_FOOTPRINT_FRACTION). Before
    sampling even starts, the SEARCH REGION passed to
    _generate_candidate_points() is further restricted to
    boundary_polygon_utm.intersection(road_union.buffer(road_proximity_
    buffer_meters + footprint_side_m)) whenever the road constraint is
    active — a pure GENERATION-TIME optimization (avoids sampling points
    that can never survive the real per-footprint distance gate below),
    not a correctness change: that real gate still runs unchanged and is
    what actually decides eligibility, since a footprint can straddle the
    restricted region's own edge.

    Every reported distance (production-zone EDGE, water zone, road) is
    the real nearest-geometry distance from the candidate's own clipped
    footprint polygon — not its centroid, and not a buffered/derived
    intermediate geometry — same "answer 'how far is this candidate from
    the thing itself,' not from some derived approximation" reasoning the
    previous zone model already used for water/road distance.

    Returns up to max_candidates entries, ranked best-first:
        {
            'rank': int,
            'suitability_score': float,        # 0-100
            'avg_slope_pct': float,
            'aspect_deg': Optional[float],      # None if the candidate is essentially flat
            'aspect_label': str,
            'distance_to_road_m': Optional[float],
            'distance_to_production_zone_m': Optional[float],  # to nearest production zone EDGE; None only if no production zones exist at all
            'production_zone_relationship': str,  # 'inside' | 'adjacent' | 'outside'
            'distance_to_water_zone_m': Optional[float],
            'footprint_area_acres': float,      # <= max_structure_footprint_acres; smaller only if boundary-clipped
            'polygon_utm': shapely Polygon,
            'geometry_wgs84': GeoJSON geometry dict,
        }
    """
    array = dem["array"]
    resolution = dem["resolution_meters"]
    rows, cols = array.shape

    slope_pct, aspect_deg = compute_slope_and_aspect(array, resolution)
    shading = compute_shading_score(array, resolution)

    raw_production_union = (
        unary_union([p["render_fill_polygon_utm"] for p in production_areas]) if production_areas else None
    )
    production_boundary_geom = raw_production_union.boundary if raw_production_union is not None else None

    raw_water_union = (
        unary_union([z["render_fill_polygon_utm"] for z in water_zones]) if water_zones else None
    )
    water_exclusion = (
        raw_water_union.buffer(water_zone_exclusion_buffer_meters) if raw_water_union is not None else None
    )

    road_union = unary_union(road_geometries_utm) if road_geometries_utm else None
    apply_road_constraint = road_geometries_utm is not None  # None = data unavailable, don't apply

    footprint_side_m = _footprint_side_meters(max_structure_footprint_acres)
    min_footprint_area_m2 = (
        max_structure_footprint_acres * SQUARE_METERS_PER_ACRE * MIN_STRUCTURE_FOOTPRINT_FRACTION
    )

    # Generation-time-only optimization (see this function's own
    # docstring): restrict the sampled search region to stay near the
    # active road source, rather than scanning the full parcel. The real
    # per-footprint distance gate below is unchanged and is what actually
    # decides eligibility -- this purely avoids wasting a sample point on
    # ground that gate could never let through.
    search_region = boundary_polygon_utm
    if apply_road_constraint and road_union is not None and not road_union.is_empty:
        restricted = boundary_polygon_utm.intersection(
            road_union.buffer(road_proximity_buffer_meters + footprint_side_m)
        )
        if not restricted.is_empty:
            search_region = restricted

    candidates = []

    for x, y in _generate_candidate_points(search_region, candidate_point_spacing_meters):
        nominal_footprint = box(
            x - footprint_side_m / 2, y - footprint_side_m / 2, x + footprint_side_m / 2, y + footprint_side_m / 2
        )

        footprint = nominal_footprint.intersection(boundary_polygon_utm)
        if footprint.is_empty or footprint.area < min_footprint_area_m2:
            continue  # off-parcel, or too little of the nominal footprint survives clipping to be a real pad

        if water_exclusion is not None and footprint.intersects(water_exclusion):
            continue  # hard exclusion, unchanged in spirit -- a structure can't sit on/against pond-siting ground

        if tree_zone_exclusion_polygon_utm is not None and footprint.intersects(tree_zone_exclusion_polygon_utm):
            continue  # hard exclusion -- a structure shouldn't sit on/against planned tree-zone ground

        cells = _cells_within_polygon(dem, footprint, rows, cols)
        if not cells:
            continue  # no DEM data at all under this footprint

        if canopy_mask_utm is not None and any(canopy_mask_utm[r, c] for r, c in cells):
            continue  # hard exclusion, before any scoring -- footprint touches real, existing tree canopy

        cell_slopes = [float(slope_pct[r, c]) for r, c in cells if not math.isnan(slope_pct[r, c])]
        if not cell_slopes:
            continue  # Horn's method needs a full 3x3 neighborhood -- an edge/nodata-adjacent footprint can end up empty here
        avg_slope_pct = float(np.mean(cell_slopes))
        if avg_slope_pct > max_solar_slope_pct:
            continue  # too steep to build on -- a real buildability ceiling, independent of production-zone proximity

        if apply_road_constraint:
            if road_union is None or footprint.distance(road_union) > road_proximity_buffer_meters:
                continue

        cell_aspects = [float(aspect_deg[r, c]) for r, c in cells if not math.isnan(aspect_deg[r, c])]
        mean_aspect = _circular_mean_aspect_deg(cell_aspects)
        a_score = aspect_score(mean_aspect if mean_aspect is not None else float("nan"))

        cell_shading = [float(shading[r, c]) for r, c in cells if not math.isnan(shading[r, c])]
        sh_score = float(np.mean(cell_shading)) if cell_shading else 0.5

        s_score = _slope_score(avg_slope_pct, max_solar_slope_pct)

        distance_to_production_edge_m = (
            float(footprint.distance(production_boundary_geom)) if production_boundary_geom is not None else None
        )
        p_score = _production_proximity_score(distance_to_production_edge_m, production_proximity_reference_meters)

        combined = (
            SLOPE_SCORE_WEIGHT * s_score
            + ASPECT_SCORE_WEIGHT * a_score
            + SHADING_SCORE_WEIGHT * sh_score
            + PRODUCTION_PROXIMITY_SCORE_WEIGHT * p_score
        )
        if combined < min_suitability_score:
            continue

        relationship = _classify_production_zone_relationship(
            footprint, raw_production_union, distance_to_production_edge_m, production_edge_adjacency_meters
        )

        distance_to_water_zone_m = (
            float(footprint.distance(raw_water_union)) if raw_water_union is not None else None
        )
        distance_to_road_m = float(footprint.distance(road_union)) if road_union is not None else None

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(footprint))

        candidates.append(
            {
                "suitability_score": round(combined * 100, 1),
                "avg_slope_pct": round(avg_slope_pct, 1),
                "aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
                "aspect_label": aspect_to_compass_label(mean_aspect) if mean_aspect is not None else "flat",
                "distance_to_road_m": round(distance_to_road_m, 1) if distance_to_road_m is not None else None,
                "distance_to_production_zone_m": (
                    round(distance_to_production_edge_m, 1) if distance_to_production_edge_m is not None else None
                ),
                "production_zone_relationship": relationship,
                "distance_to_water_zone_m": (
                    round(distance_to_water_zone_m, 1) if distance_to_water_zone_m is not None else None
                ),
                "footprint_area_acres": round(footprint.area / SQUARE_METERS_PER_ACRE, 3),
                "polygon_utm": footprint,
                "geometry_wgs84": geometry_wgs84,
            }
        )

    candidates.sort(key=lambda cand: -cand["suitability_score"])
    candidates = candidates[:max_candidates]
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank

    return candidates


def flag_prime_farmland_conflicts(
    candidates: list[dict], farmland_classifications: list[dict]
) -> list[dict]:
    """
    Pure post-processing step: checks each candidate's polygon against
    SSURGO farmland classification and adds 'prime_farmland_conflict'
    (bool) and 'prime_farmland_note' (str) to each candidate dict, in
    place, and returns it. Does NOT exclude or re-rank anything — a
    technically great solar site on prime farmland is still flagged, not
    dropped, per the Scale of Permanence tension between competing land
    uses this feature is explicitly meant to surface, not resolve.
    UNCHANGED by the point-candidate redesign.

    farmland_classifications is soil_data.get_farmland_classification_for_
    polygon()'s output — this function takes it pre-fetched (no network
    here) so it's unit-testable with a synthetic list.

    This function only ever sets prime_farmland_conflict based on whether
    ANY prime-farmland map unit was found intersecting the boundary this
    classification data was fetched for — it doesn't have per-candidate
    soil geometry to check individually (SSURGO map unit polygons, not
    just their farmland-class attribute, would be needed for that), so a
    "yes" here means "somewhere in the area this data covers," not
    necessarily "exactly under this polygon." That's stated in the note
    added to each flagged candidate, not left implicit.
    """
    any_prime = any(is_prime_farmland(c.get("farmland_classification")) for c in farmland_classifications)

    for candidate in candidates:
        candidate["prime_farmland_conflict"] = any_prime
        if any_prime:
            candidate["prime_farmland_note"] = (
                "Prime (or conditionally prime) farmland soil was found in this area per "
                "SSURGO — this candidate may sit on or near land better suited to production "
                "than solar infrastructure. This is a tradeoff to weigh, not an exclusion."
            )
        else:
            candidate["prime_farmland_note"] = "No prime farmland classification found in this area per SSURGO."

    return candidates


def select_optimal_structure_site(scored_candidates: list[dict]) -> Optional[dict]:
    """
    Explicit selection step on top of find_candidate_solar_zones()'s own
    ranking: returns the single candidate with rank == 1 (highest
    suitability_score) -- no logic beyond that. Same pattern as
    water_suitability.select_optimal_water_zone() and
    road_corridors.select_optimal_road_corridor(); per product decision,
    this app targets small farms only, where one well-suited structure
    site is sufficient -- no multi-candidate coexistence logic is needed
    here.

    Deliberately does NOT attempt to reconcile this selection with
    road_corridors.select_optimal_road_corridor() beyond the two-tier
    road-proximity constraint identify_solar_candidate_zones() already
    applies (selected corridor primary, real mapped roads as fallback --
    see module docstring). That interplay is deliberately deferred, same
    as the fencing/roads-and-structures interplay already deferred
    elsewhere in this pipeline, until real results from both selections
    independently are available to look at.

    Returns None if scored_candidates is empty -- a real, reportable "no
    candidates at all" outcome, not an error.
    """
    if not scored_candidates:
        return None
    return max(scored_candidates, key=lambda c: c["suitability_score"])


def candidates_to_geojson(
    candidates: list[dict],
    shading_is_rough_proxy: bool = True,
    road_proximity_source: str = "unavailable",
    tree_zone_exclusion_available: bool = True,
    spacing_meters: float = CANDIDATE_POINT_SPACING_METERS,
    max_structure_footprint_acres: float = MAX_STRUCTURE_FOOTPRINT_ACRES,
) -> dict:
    """Wraps find_candidate_solar_zones() (+ optionally
    flag_prime_farmland_conflicts()) output as the schema-conformant
    GeoJSON FeatureCollection this feature delivers
    (layer="solar_infrastructure"). road_proximity_source reports which
    tier actually produced these candidates ("selected_road_corridor" |
    "real_mapped_road" | "unavailable" — see module docstring);
    tree_zone_exclusion_available flags whether the tree-zone-candidate
    exclusion could be checked this run at all (a graceful-degradation
    outcome, see identify_solar_candidate_zones())."""
    farmland_note = ""
    if candidates and "prime_farmland_conflict" in candidates[0]:
        farmland_note = (
            "SSURGO prime-farmland overlap was checked and is reported per-candidate "
            "in properties.prime_farmland_conflict — see properties.prime_farmland_note. "
        )

    confidence_notes = SOLAR_CONFIDENCE_NOTES_TEMPLATE.format(
        spacing_m=spacing_meters,
        max_footprint_acres=max_structure_footprint_acres,
        footprint_side_m=_footprint_side_meters(max_structure_footprint_acres),
        shading_caveat=SHADING_CAVEAT_HORIZON_ONLY if shading_is_rough_proxy else "computed from a real canopy height model (DSM-derived), not a rough proxy.",
        canopy_buffer_ft=TREE_ROOT_ZONE_BUFFER_METERS / METERS_PER_FOOT,
        tree_zone_buffer_ft=TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS / METERS_PER_FOOT,
        tree_zone_availability_note="" if tree_zone_exclusion_available else TREE_ZONE_EXCLUSION_UNAVAILABLE_NOTE,
        road_proximity_note=ROAD_PROXIMITY_NOTE_BY_SOURCE[road_proximity_source],
        farmland_note=farmland_note,
    )

    features = []
    for candidate in candidates:
        constraints_satisfied = [
            "outside_water_candidate_zone",
            "outside_existing_canopy",
            f"max_slope<={MAX_SOLAR_SLOPE_PCT:.0f}pct",
            f"suitability_score>={MIN_SUITABILITY_SCORE * 100:.0f}",
        ]
        if tree_zone_exclusion_available:
            constraints_satisfied.append("outside_tree_zone_candidate_buffer")
        if candidate.get("distance_to_road_m") is not None:
            constraints_satisfied.append("within_road_proximity_buffer")

        distance_to_road_ft = (
            round(candidate["distance_to_road_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_road_m") is not None
            else None
        )
        distance_to_production_zone_ft = (
            round(candidate["distance_to_production_zone_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_production_zone_m") is not None
            else None
        )
        distance_to_water_zone_ft = (
            round(candidate["distance_to_water_zone_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_water_zone_m") is not None
            else None
        )

        extra_properties = {
            "rank": candidate["rank"],
            "suitability_score": candidate["suitability_score"],
            "avg_slope_pct": candidate["avg_slope_pct"],
            "aspect": candidate["aspect_label"],
            "aspect_degrees": candidate["aspect_deg"],
            "footprint_area_acres": candidate["footprint_area_acres"],
            "distance_to_road_ft": distance_to_road_ft,
            "road_proximity_source": road_proximity_source,
            "distance_to_production_zone_ft": distance_to_production_zone_ft,
            "production_zone_relationship": candidate["production_zone_relationship"],
            "distance_to_water_zone_ft": distance_to_water_zone_ft,
            "constraints_satisfied": constraints_satisfied,
        }
        if "prime_farmland_conflict" in candidate:
            extra_properties["prime_farmland_conflict"] = candidate["prime_farmland_conflict"]
            extra_properties["prime_farmland_note"] = candidate["prime_farmland_note"]

        # Confidence reflects geometric/data-quality reliability (this
        # layer stacks a slope-only production heuristic, a DEM-only
        # shading proxy, and public-only road data), NOT site
        # desirability — a prime-farmland conflict, or sitting inside a
        # production zone, doesn't make the geometry itself any less
        # trustworthy, so neither is folded into confidence.
        features.append(
            make_feature(
                feature_id=f"solar-candidate-{candidate['rank']}",
                geometry=candidate["geometry_wgs84"],
                layer="solar_infrastructure",
                label=f"Solar structure candidate (rank {candidate['rank']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties=extra_properties,
            )
        )

    return make_feature_collection(features)


def identify_solar_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    production_areas: Optional[list[dict]] = None,
    valleys: Optional[list[dict]] = None,
    selected_water_zone: Optional[dict] = None,
    selected_road_corridor: Optional[dict] = None,
    hydric_floodplain_union=None,
    floodplain_data_is_fallback: Optional[bool] = None,
    check_prime_farmland: bool = True,
    canopy_height: Optional[dict] = None,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    the mandatory canopy exclusion mask, optimized production areas, the
    single selected water zone, every ranked tree-zone candidate, and the
    two-tier road-proximity source; runs the point-candidate scoring
    (once, or twice if Tier 1 produces nothing — see below); checks the
    SSURGO prime-farmland conflict; and returns the "solar_infrastructure"
    GeoJSON FeatureCollection. See module docstring for the full
    constraint-stack rationale; this docstring covers wiring/ordering.

    dem, boundary_polygon_utm, production_areas, selected_water_zone, and
    selected_road_corridor are all optional overrides, independently of
    one another -- each falls back to being self-computed exactly as
    before if not supplied, same "reuse what an upstream orchestrator
    already computed" pattern water_suitability.identify_water_
    suitability() and road_corridors.identify_road_corridor_candidates()
    already established for these same values. valleys is a pure
    pass-through convenience: it is forwarded as-is (including None) to
    the identify_water_suitability()/identify_road_corridor_candidates()
    calls below, which already have their own correct None-falls-back-
    to-self-compute handling for it -- there is no third copy of that
    fallback logic here. hydric_floodplain_union/floodplain_data_is_
    fallback are forwarded the same way to identify_road_corridor_
    candidates() alone (see that function's own docstring for what they
    mean); they only take effect when selected_road_corridor is not
    itself supplied, same as production_areas/valleys/boundary_polygon_
    utm below.

    canopy_mask_utm and tree_zone_exclusion_polygon_utm are NOT among
    these overrides -- both are always self-computed here (see module
    docstring); that's a deliberate, separate scope decision, not an
    oversight.

    Returns:
        {
            'zones_geojson': dict,                   # every scored candidate, ranked
            'all_scored_candidates': list[dict],     # find_candidate_solar_zones()'s own raw
                                                        # scored list (post-farmland-flagging)
            'selected_structure_site': Optional[dict],  # select_optimal_structure_site()'s
                                                           # single rank-1 answer, or None if no
                                                           # candidates exist
        }

    CANOPY (mandatory, non-degrading — see module docstring): fetched
    directly via production_area.get_required_tree_root_zone_mask_utm(),
    NOT wrapped in try/except — a fetch failure is left to propagate
    UNCAUGHT and fails this whole call, same as production_area.py's/
    production_area_ceiling.py's/tree_zone_candidates.py's own mandatory
    canopy gates. identify_optimized_production_areas() below pulls in a
    SECOND, fully independent copy of this same mandatory gate internally
    — both are expected to hard-fail independently on a canopy outage;
    neither is caught here.
      canopy_height is an optional pre-fetched override in the same family
      as the dem/boundary/production/water/valleys overrides: the SAME dict
      canopy_height_data.get_canopy_height_for_boundary() returns (e.g.
      parcel_data.ParcelData.canopy_height). When supplied it is forwarded
      to EVERY canopy consumer this function reaches -- its own direct gate
      above plus the nested identify_optimized_production_areas(),
      identify_water_suitability(), and identify_tree_zone_candidates()
      calls below -- so none of those redundant, independent canopy fetches
      hit the network; when None (the default) each fetches as before,
      leaving every gate's hard-fail semantics unchanged.

    PRODUCTION is production_area_ceiling.identify_optimized_production_
    areas()'s own OPTIMIZED, ceiling-trimmed 'scored_patches' (not
    production_area.identify_production_areas()'s raw candidates), passed
    through to find_candidate_solar_zones() for its scoring-only (not
    exclusion) role — see that function's docstring. Skipped entirely when
    this function's own production_areas override is supplied.

    WATER exclusion is scoped to only the SINGLE selected water zone
    (water_suitability.select_optimal_water_zone(), same selection
    function tree_zone_candidates.py's own rewiring in this same pipeline
    already reuses), using its 'render_fill_polygon_utm' — confirmed live
    that excluding all of a real property's several legitimate,
    separately-buffered water zones can together cover enough of a small
    parcel to zero out every solar candidate, even though each zone's own
    geometry is individually normal. water_suitability.
    identify_water_suitability()'s own real per-zone SSURGO/NHD fetches
    degrade independently and gracefully. Skipped entirely when this
    function's own selected_water_zone override is supplied; when it is
    NOT but boundary_polygon_utm/production_areas/valleys ARE, those three
    are passed through as kwargs so identify_water_suitability() doesn't
    re-derive its own independent copies.

    ROAD PROXIMITY is two-tier (see module docstring for the full
    rationale). Tier 1's own selected_road_corridor is resolved BEFORE
    TREE-ZONE-CANDIDATE exclusion below (not after, despite Tier 1's own
    SCORING step running after it) specifically so that step can forward
    this function's own real selected_road_corridor into its own nested
    identify_tree_zone_candidates() call -- see that section's own
    docstring paragraph below for why.
      Tier 1 (primary) resolution: road_corridors.identify_road_corridor_
        candidates() is called directly here, given this function's own
        anchor_lon_lat parameter (the real, user-picked access point,
        threaded down from generate_full_report.py — None degrades to no
        corridor, same as identify_road_corridor_candidates() itself
        already handles), and its own 'selected_road_corridor' (None if no
        corridor exists) is used as the road source at ROAD_CORRIDOR_
        PROXIMITY_METERS. Not wrapped in try/except: this call's own
        internal production-zone fetch is ALSO a mandatory canopy gate (a
        THIRD independent one), same "expected to hard-fail independently"
        reasoning as above. Skipped entirely when this function's own
        selected_road_corridor override is supplied as a real (non-None)
        value; a caller-supplied None is indistinguishable from "not
        supplied" (same None-as-sentinel convention every other override
        in this function uses) and still self-computes -- the Tier 1/
        Tier 2 scoring branching further below is unaffected either way,
        since it already treats a self-computed None (no corridor exists)
        and any other None identically. When selected_road_corridor is NOT
        overridden but boundary_polygon_utm/production_areas/valleys ARE,
        those three (plus hydric_floodplain_union/floodplain_data_is_
        fallback, forwarded as-is) are passed through as kwargs so
        identify_road_corridor_candidates() doesn't re-derive its own
        independent copies.
      Tier 1 (primary) scoring / Tier 2 (fallback, only if Tier 1 produced
        zero candidates — whether because no corridor exists at all, or
        one exists but nothing survives near it): real mapped roads
        (farm_roads_data.get_farm_roads_for_boundary(), unchanged fetch
        logic) at ROAD_PROXIMITY_BUFFER_METERS — this IS today's
        pre-this-pass road-proximity logic, demoted from primary to
        fallback. If this fetch fails outright (a real network error, not
        "zero roads found") and Tier 1 also produced nothing, the road
        constraint is disabled entirely for a final find_candidate_solar_
        zones() call (road_geometries_utm=None) — same terminal fallback
        behavior this module has always had.
    road_proximity_source ("selected_road_corridor" | "real_mapped_road" |
    "unavailable") records which tier actually produced the result and is
    threaded into candidates_to_geojson()'s confidence_notes/per-feature
    properties.

    TREE-ZONE-CANDIDATE exclusion (identify_tree_zone_candidates(), the
    full ranked 'patches' list) degrades GRACEFULLY on an ordinary fetch
    failure — noted in confidence_notes via candidates_to_geojson()'s own
    tree_zone_exclusion_available flag — UNLESS the failure is
    specifically canopy_height_data.CanopyCoverageIncompleteError
    bubbling up from that call's own internal mandatory canopy gate, in
    which case it propagates uncaught here too, same reasoning as the
    module's own primary canopy gate above. This call forwards this
    function's own already-resolved boundary_polygon_utm/production_
    areas/valleys/selected_water_zone/selected_road_corridor/hydric_
    floodplain_union/floodplain_data_is_fallback, so it reuses them rather
    than self-computing independent copies of production_areas/selected_
    water_zone/selected_road_corridor all over again — previously this
    call forwarded none of those (only boundary_coordinates/dem/
    anchor_lon_lat), a real, measured redundancy found once pipeline_
    context.py's own build_pipeline_context() started supplying overrides
    to THIS function and fixed here.
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

    # MANDATORY, non-degrading -- see module docstring and this
    # function's own docstring. Deliberately NOT wrapped in try/except.
    canopy_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=TREE_ROOT_ZONE_BUFFER_METERS, canopy_height=canopy_height
    )

    if production_areas is None:
        # Optimized/ceiling-trimmed production geometry -- pulls in its
        # own SECOND, independent mandatory canopy gate internally; also
        # not caught here (see this function's own docstring).
        production_result = identify_optimized_production_areas(
            boundary_coordinates, dem=dem, canopy_height=canopy_height
        )
        production_areas = production_result["scored_patches"]

    if selected_water_zone is None:
        water_result = identify_water_suitability(
            boundary_coordinates,
            dem=dem,
            boundary_polygon_utm=boundary_polygon_utm,
            valleys=valleys,
            production_areas=production_areas,
            canopy_height=canopy_height,
        )
        selected_water_zone = water_result["selected_water_zone"]
    water_zones = [selected_water_zone] if selected_water_zone else []

    # --- Tier 1 (primary) road source: the property's own single selected
    # road corridor, within ROAD_CORRIDOR_PROXIMITY_METERS. Resolved HERE
    # (moved up from directly above the Tier 1 scoring call below) SPECIFICALLY
    # so the tree-zone-candidate exclusion block right after this can forward
    # this function's own real selected_road_corridor into its own nested
    # identify_tree_zone_candidates() call, instead of leaving that call to
    # self-compute an independent, redundant copy -- the same reasoning
    # already applies to production_areas/selected_water_zone above, both
    # resolved before this point for the same purpose. Not wrapped in
    # try/except: this call's own internal production-zone fetch is ALSO a
    # mandatory canopy gate (a THIRD independent one), same "expected to
    # hard-fail independently" reasoning as the primary canopy gate above.
    # Skipped entirely when this function's own selected_road_corridor
    # override is supplied as a real (non-None) value; a caller-supplied
    # None is indistinguishable from "not supplied" (same None-as-sentinel
    # convention every other override in this function uses) and still
    # self-computes -- the Tier 1/Tier 2 scoring branching below is
    # unaffected either way, since it already treats a self-computed None
    # (no corridor exists) and any other None identically. ---
    if selected_road_corridor is None:
        corridor_result = identify_road_corridor_candidates(
            boundary_coordinates,
            anchor_lon_lat=anchor_lon_lat,
            dem=dem,
            boundary_polygon_utm=boundary_polygon_utm,
            production_areas=production_areas,
            valleys=valleys,
            selected_water_zone=selected_water_zone,
            hydric_floodplain_union=hydric_floodplain_union,
            floodplain_data_is_fallback=floodplain_data_is_fallback,
            canopy_height=canopy_height,
        )
        selected_road_corridor = corridor_result["selected_road_corridor"]

    # Tree-zone-candidate exclusion: graceful degradation on an ordinary
    # fetch failure, EXCEPT for CanopyCoverageIncompleteError bubbling up
    # from this call's own internal mandatory canopy gate -- see this
    # function's own docstring. Forwards this function's own already-
    # resolved boundary_polygon_utm/production_areas/valleys/selected_
    # water_zone/selected_road_corridor/hydric_floodplain_union/
    # floodplain_data_is_fallback so this nested call reuses them instead
    # of self-computing independent copies of production_areas/selected_
    # water_zone/selected_road_corridor all over again -- previously this
    # call forwarded none of them (only boundary_coordinates/dem/
    # anchor_lon_lat), a real, measured redundancy found and fixed after
    # pipeline_context.py's own build_pipeline_context() started supplying
    # these overrides (see that module's own KNOWN LIMITATIONS #4, prior
    # to this fix).
    tree_zone_exclusion_polygon_utm = None
    tree_zone_exclusion_available = True
    try:
        tree_zone_result = identify_tree_zone_candidates(
            boundary_coordinates,
            dem=dem,
            anchor_lon_lat=anchor_lon_lat,
            boundary_polygon_utm=boundary_polygon_utm,
            production_areas=production_areas,
            valleys=valleys,
            selected_water_zone=selected_water_zone,
            selected_road_corridor=selected_road_corridor,
            hydric_floodplain_union=hydric_floodplain_union,
            floodplain_data_is_fallback=floodplain_data_is_fallback,
            canopy_height=canopy_height,
        )
        tree_zone_patches = tree_zone_result["patches"]
        if tree_zone_patches:
            tree_zone_union = unary_union([p["render_fill_polygon_utm"] for p in tree_zone_patches])
            tree_zone_exclusion_polygon_utm = tree_zone_union.buffer(TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS)
    except CanopyCoverageIncompleteError:
        raise
    except Exception:
        tree_zone_exclusion_available = False

    common_zone_kwargs = dict(
        canopy_mask_utm=canopy_mask_utm,
        tree_zone_exclusion_polygon_utm=tree_zone_exclusion_polygon_utm,
    )
    common_zone_kwargs.update(zone_kwargs)

    # --- Tier 1 (primary) scoring: within ROAD_CORRIDOR_PROXIMITY_METERS
    # of the selected_road_corridor resolved above (self-compute moved
    # earlier -- see that block's own comment). ---
    candidates = []
    road_proximity_source = "unavailable"

    if selected_road_corridor is not None:
        candidates = find_candidate_solar_zones(
            dem,
            production_areas,
            water_zones,
            [selected_road_corridor["cell_footprint_polygon_utm"]],
            boundary_polygon_utm,
            road_proximity_buffer_meters=ROAD_CORRIDOR_PROXIMITY_METERS,
            **common_zone_kwargs,
        )
        if candidates:
            road_proximity_source = "selected_road_corridor"

    # --- Tier 2 (fallback): only reached if Tier 1 produced zero
    # candidates -- either no corridor existed at all, or one existed but
    # nothing survived the rest of the constraint stack near it. ---
    if not candidates:
        try:
            roads = get_farm_roads_for_boundary(boundary_coordinates)
            road_lines_wgs84 = [g["geometry"] for g in roads]
        except Exception:
            road_lines_wgs84 = None  # real fetch failure, not "zero roads found"

        if road_lines_wgs84 is not None:
            road_geometries_utm = []
            for geometry in road_lines_wgs84:
                coords = geometry["coordinates"]
                line_lists = coords if geometry["type"] == "MultiLineString" else [coords]
                for line in line_lists:
                    xs, ys = warp_transform("EPSG:4326", dem["crs"], [p[0] for p in line], [p[1] for p in line])
                    road_geometries_utm.append(LineString(zip(xs, ys)))

            candidates = find_candidate_solar_zones(
                dem,
                production_areas,
                water_zones,
                road_geometries_utm,
                boundary_polygon_utm,
                road_proximity_buffer_meters=ROAD_PROXIMITY_BUFFER_METERS,
                **common_zone_kwargs,
            )
            road_proximity_source = "real_mapped_road"
        else:
            # Tier 2's own fetch failed outright, AND Tier 1 produced
            # nothing -- fall through to the terminal behavior: disable
            # the road constraint entirely for a final scoring pass.
            candidates = find_candidate_solar_zones(
                dem,
                production_areas,
                water_zones,
                None,
                boundary_polygon_utm,
                **common_zone_kwargs,
            )
            road_proximity_source = "unavailable"

    if check_prime_farmland and candidates:
        try:
            wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
            farmland_classifications = get_farmland_classification_for_polygon(wkt_polygon)
            candidates = flag_prime_farmland_conflicts(candidates, farmland_classifications)
        except Exception:
            pass  # SSURGO outage -- candidates just won't carry a prime_farmland_conflict flag this run

    return {
        "zones_geojson": candidates_to_geojson(
            candidates,
            road_proximity_source=road_proximity_source,
            tree_zone_exclusion_available=tree_zone_exclusion_available,
        ),
        "all_scored_candidates": candidates,
        "selected_structure_site": select_optimal_structure_site(candidates),
    }


def fetch_and_select_optimal_structure_site(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    **zone_kwargs,
) -> Optional[dict]:
    """
    Convenience wrapper for callers (e.g. render_layout_map.py) that want
    a single best solar structure site candidate rather than the full
    ranked FeatureCollection -- identify_solar_candidate_zones() already
    returns candidates rank-ordered best-first (feature 0 = rank 1), so
    this just returns that top GeoJSON Feature (or None if nothing
    cleared the constraint stack). Selects nothing new -- it picks the #1
    entry an existing, unchanged ranking already produced.
    """
    result = identify_solar_candidate_zones(boundary_coordinates, dem=dem, anchor_lon_lat=anchor_lon_lat, **zone_kwargs)
    features = result["zones_geojson"]["features"]
    return features[0] if features else None


def summarize_solar_candidate_zones(result: dict) -> str:
    features = result["zones_geojson"]["features"]
    if not features:
        return "No solar structure candidates identified (nothing cleared the constraint stack)."

    lines = [f"Solar structure candidates: {len(features)}"]
    for feature in features:
        props = feature["properties"]
        conflict = " [prime farmland conflict]" if props.get("prime_farmland_conflict") else ""
        lines.append(
            f"  - Rank {props['rank']}: score {props['suitability_score']}/100, "
            f"{props['footprint_area_acres']}ac, {props['avg_slope_pct']}% slope, {props['aspect']}-facing, "
            f"{props['distance_to_road_ft']}ft to road, {props['production_zone_relationship']} production zone "
            f"({props['distance_to_production_zone_ft']}ft to edge){conflict}"
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

    # Manual-testing-only reference anchor -- imported here, not at module
    # level, so this stays a __main__-only test fixture rather than a
    # production dependency (see render_layout_map.py's own module
    # docstring for this constant).
    from render_layout_map import _PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT

    print("Identifying solar structure candidates for property boundary...\n")

    try:
        result = identify_solar_candidate_zones(
            property_boundary, anchor_lon_lat=_PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT
        )
        print(summarize_solar_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
