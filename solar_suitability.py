"""
solar_suitability.py

Solar/structure siting data layer for permanent building placement (Scale
of Permanence step 6): ranks candidate SITES for a small, fixed-footprint
solar-generating structure (e.g. a barn or shed with rooftop panels), not
a large ground-mounted array. This produces RANKED CANDIDATES, not a
single placement decision — Claude narrates the tradeoffs between them in
the report (see report_generator.py step 6). Finding "the one best spot"
is explicitly not this module's job.

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
            HARD exclusion (buffered), unchanged
        --> farm roads (farm_roads_data.py) -- still a hard proximity
            constraint + reported distance, unchanged in spirit
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
already-fetched DEM dict, production areas, water-candidate zones, and
road geometries (all in the DEM's own projected CRS) and does no network
I/O itself — same reason as water_candidate_zones.py's
find_candidate_zones(): so the scoring logic is unit-testable against a
synthetic DEM independent of whether any of the real data fetches (DEM,
roads, SSURGO) are working. flag_prime_farmland_conflicts() is a second,
separate pure function for exactly the same reason, applied to the SSURGO
farmland lookup specifically — unchanged by this pass.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import identify_production_areas
from raster_grid import SQUARE_METERS_PER_ACRE, pixel_center_xy
from road_corridors import POND_ZONE_EXCLUSION_BUFFER_METERS, identify_road_corridor_candidates
from soil_data import coordinates_to_wkt_polygon, get_farmland_classification_for_polygon, is_prime_farmland
from terrain_metrics import aspect_score, aspect_to_compass_label, compute_shading_score, compute_slope_and_aspect
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

# Weights for the combined 0-1 suitability score (must sum to 1.0).
# PRODUCTION_PROXIMITY_SCORE_WEIGHT is new in the point-candidate model
# (Step 4) — a real but secondary preference (a structure sited at a
# production zone's edge is more useful to that zone's own operation than
# one far away), deliberately smaller than any of the three physical-
# suitability factors it sits alongside: slope/aspect/shading determine
# whether a site is buildable and productive at all, while production
# proximity is a layout nicety on top of that, not a substitute for it.
# The other three keep their original relative proportions (slope still
# weighted highest, aspect and shading equal) scaled down to leave room
# for the new term. CONFIGURABLE — tune against your own property once
# real production data is available to check the ranking against.
SLOPE_SCORE_WEIGHT = 0.35
ASPECT_SCORE_WEIGHT = 0.25
SHADING_SCORE_WEIGHT = 0.25
PRODUCTION_PROXIMITY_SCORE_WEIGHT = 0.15

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

# Candidates must be within this distance of a mapped road to be
# considered reachable/wireable at all — a hard constraint, not just a
# scoring input. Unchanged by this pass (road-proximity reporting stays
# as-is; road CORRIDOR GENERATION's own exclusion logic is a separate,
# already-agreed follow-on task, not touched here). CONFIGURABLE.
ROAD_PROXIMITY_BUFFER_METERS = 150.0

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
    "(buffered) — a structure should not sit on or immediately against that ground. It also "
    "inherits the limitations of production_area.py (a slope-only production-zone heuristic), "
    "water_candidate_zones.py (a DEM-derived valley/gradient heuristic), and farm_roads_data.py "
    "(public road/right-of-way data only — may miss private farm tracks). "
    "{road_fallback_note}{farmland_note}Treat this as a starting shortlist to walk and "
    "ground-truth, not a final site plan."
)

ROAD_FALLBACK_NOTE = (
    "No existing road data was available for this property, so road-proximity "
    "scoring (distance_to_road_ft) is measured against the top-ranked SUGGESTED "
    "road corridor (road_corridors.py) instead of a real road — itself a "
    "topographic suggestion, not a surveyed alignment. "
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
    road_geometries_utm: Optional[list[LineString]],
    boundary_polygon_utm: Polygon,
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
    to.

    water_zones is water_candidate_zones.find_candidate_zones()'s own
    output shape (each entry carrying 'polygon_utm') — hard-excluded
    (buffered) exactly as before; production_areas is
    production_area.py's/production_suitability.py's own patch shape
    (each entry carrying 'polygon_utm') but is NO LONGER a hard exclusion
    here — see PRODUCTION_PROXIMITY_SCORE_WEIGHT above for how it's used
    instead (a scored edge-proximity preference).

    road_geometries_utm=None means "road data unavailable" (the fetch
    itself failed) and disables the road-proximity constraint entirely
    (with that noted by the caller); an empty list [] means "fetched
    successfully, no roads found nearby" and is treated as a real,
    binding constraint (nothing will qualify) — unchanged from before.

    boundary_polygon_utm is the real parcel (NOT the DEM's buffered
    extent — dem_data.py fetches ~100m past the drawn boundary on
    purpose). Each candidate's nominal (fixed-size) footprint is
    intersected with it — a footprint sampled near the boundary can come
    back smaller than the nominal cap, or be dropped entirely if too
    little survives (see MIN_STRUCTURE_FOOTPRINT_FRACTION).

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

    raw_production_union = unary_union([p["polygon_utm"] for p in production_areas]) if production_areas else None
    production_boundary_geom = raw_production_union.boundary if raw_production_union is not None else None

    raw_water_union = unary_union([z["polygon_utm"] for z in water_zones]) if water_zones else None
    water_exclusion = (
        raw_water_union.buffer(water_zone_exclusion_buffer_meters) if raw_water_union is not None else None
    )

    road_union = unary_union(road_geometries_utm) if road_geometries_utm else None
    apply_road_constraint = road_geometries_utm is not None  # None = data unavailable, don't apply

    footprint_side_m = _footprint_side_meters(max_structure_footprint_acres)
    min_footprint_area_m2 = (
        max_structure_footprint_acres * SQUARE_METERS_PER_ACRE * MIN_STRUCTURE_FOOTPRINT_FRACTION
    )

    candidates = []

    for x, y in _generate_candidate_points(boundary_polygon_utm, candidate_point_spacing_meters):
        nominal_footprint = box(
            x - footprint_side_m / 2, y - footprint_side_m / 2, x + footprint_side_m / 2, y + footprint_side_m / 2
        )

        footprint = nominal_footprint.intersection(boundary_polygon_utm)
        if footprint.is_empty or footprint.area < min_footprint_area_m2:
            continue  # off-parcel, or too little of the nominal footprint survives clipping to be a real pad

        if water_exclusion is not None and footprint.intersects(water_exclusion):
            continue  # hard exclusion, unchanged in spirit -- a structure can't sit on/against pond-siting ground

        cells = _cells_within_polygon(dem, footprint, rows, cols)
        if not cells:
            continue  # no DEM data at all under this footprint

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
    road_corridors.select_optimal_road_corridor() (e.g. re-scoring this
    site's road-proximity against the newly-selected corridor specifically)
    -- solar's own road-proximity fallback (real mapped road vs. suggested-
    corridor fallback, see _suggested_corridor_as_road_fallback()) is left
    exactly as-is. That interplay is deliberately deferred, same as the
    fencing/roads-and-structures interplay already deferred elsewhere in
    this pipeline, until real results from both selections independently
    are available to look at.

    Returns None if scored_candidates is empty -- a real, reportable "no
    candidates at all" outcome, not an error.
    """
    if not scored_candidates:
        return None
    return max(scored_candidates, key=lambda c: c["suitability_score"])


def candidates_to_geojson(
    candidates: list[dict],
    shading_is_rough_proxy: bool = True,
    road_data_is_fallback: bool = False,
    spacing_meters: float = CANDIDATE_POINT_SPACING_METERS,
    max_structure_footprint_acres: float = MAX_STRUCTURE_FOOTPRINT_ACRES,
) -> dict:
    """Wraps find_candidate_solar_zones() (+ optionally
    flag_prime_farmland_conflicts()) output as the schema-conformant
    GeoJSON FeatureCollection this feature delivers
    (layer="solar_infrastructure"). road_data_is_fallback flags that
    road-proximity scoring used a suggested corridor (road_corridors.py)
    in place of real road data — see identify_solar_candidate_zones()."""
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
        road_fallback_note=ROAD_FALLBACK_NOTE if road_data_is_fallback else "",
        farmland_note=farmland_note,
    )

    features = []
    for candidate in candidates:
        constraints_satisfied = [
            "outside_water_candidate_zone",
            f"max_slope<={MAX_SOLAR_SLOPE_PCT:.0f}pct",
            f"suitability_score>={MIN_SUITABILITY_SCORE * 100:.0f}",
        ]
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


def _suggested_corridor_as_road_fallback(
    boundary_coordinates: list[tuple[float, float]], dem: dict
) -> Optional[list[LineString]]:
    """
    Road-proximity fallback for identify_solar_candidate_zones(): when no
    existing-road data is available, uses the top-ranked
    "suggested_road_corridor" feature (road_corridors.py) — already
    rank-ordered, so index 0 is rank 1 — as the proximity anchor instead,
    reprojected into dem['crs']. Returns None (not an empty list) if
    corridor generation itself produced nothing or failed, so the caller
    can tell "no fallback available" apart from "fallback available but
    empty," matching find_candidate_solar_zones()'s own None-vs-[]
    convention for road_geometries_utm. UNCHANGED by the point-candidate
    redesign.
    """
    try:
        corridor_result = identify_road_corridor_candidates(boundary_coordinates, dem=dem)
        features = corridor_result["zones_geojson"]["features"]
        if not features:
            return None

        top_corridor = features[0]
        coords = top_corridor["geometry"]["coordinates"]
        xs, ys = warp_transform("EPSG:4326", dem["crs"], [p[0] for p in coords], [p[1] for p in coords])
        return [LineString(zip(xs, ys))]
    except Exception:
        return None


def identify_solar_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_prime_farmland: bool = True,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    production areas, water-candidate zones, and farm roads; runs the
    point-candidate scoring; checks the SSURGO prime-farmland conflict;
    and returns the "solar_infrastructure" GeoJSON FeatureCollection.
    UNCHANGED call shape from before the point-candidate redesign — only
    find_candidate_solar_zones()'s own internal model changed.

    Returns:
        {
            'zones_geojson': dict,                   # every scored candidate, ranked
            'all_scored_candidates': list[dict],     # find_candidate_solar_zones()'s own raw
                                                        # scored list (post-farmland-flagging)
            'selected_structure_site': Optional[dict],  # select_optimal_structure_site()'s
                                                           # single rank-1 answer, or None if no
                                                           # candidates exist
        }

    production_areas is computed directly from the DEM already in hand
    (identify_production_areas()) so it needs no network fetch or try/
    except here, and is passed through to find_candidate_solar_zones()
    for its (now scoring-only, not exclusion) role — see that function's
    docstring. water-zone exclusion, by contrast, is now scoped to only
    the SINGLE selected water zone (water_suitability.
    select_optimal_water_zone(), same selection function
    tree_zone_candidates.py's own rewiring in this same pipeline already
    reuses) rather than every water candidate — confirmed live that
    excluding all of a real property's several legitimate, separately-
    buffered water zones can together cover enough of a small parcel to
    zero out every solar candidate, even though each zone's own geometry
    is individually normal. Per product decision, this app targets small
    farms only: one well-suited water zone is sufficient, so only that
    one zone's own (buffered) footprint should ever be excluded here.
    water_suitability.identify_water_suitability() is called directly
    (its own real per-zone SSURGO/NHD fetches degrade independently and
    gracefully, same as every other network-backed layer in this
    pipeline) so this reaches the exact same selected zone every other
    consumer of select_optimal_water_zone() in this pipeline does, not an
    independently re-derived approximation of it. An empty candidate list
    (no water zone could be selected at all) is a real, valid outcome —
    solar's water-zone exclusion then simply has nothing to exclude, same
    as today's behavior when water_zones=[] is passed to
    find_candidate_solar_zones().

    Each real NETWORK fetch (farm roads, SSURGO, water suitability)
    still degrades independently and gracefully — an outage there
    shouldn't block solar candidates from being identified at all, same
    reasoning as every other optional layer in this pipeline (imagery,
    water candidate zones' own DEM fetch, etc.).
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    production_areas = identify_production_areas(dem, boundary_polygon_utm)

    water_result = identify_water_suitability(boundary_coordinates, dem=dem)
    selected_water_zone = water_result["selected_water_zone"]
    water_zones = [selected_water_zone] if selected_water_zone else []

    try:
        roads = get_farm_roads_for_boundary(boundary_coordinates)
        road_lines_wgs84 = [g["geometry"] for g in roads]
    except Exception:
        road_lines_wgs84 = None  # fetch failed -- disables the road constraint, see find_candidate_solar_zones

    road_geometries_utm = None
    if road_lines_wgs84 is not None:
        road_geometries_utm = []
        for geometry in road_lines_wgs84:
            coords = geometry["coordinates"]
            line_lists = coords if geometry["type"] == "MultiLineString" else [coords]
            for line in line_lists:
                xs, ys = warp_transform("EPSG:4326", dem["crs"], [p[0] for p in line], [p[1] for p in line])
                road_geometries_utm.append(LineString(zip(xs, ys)))

    # No existing-road data at all (fetch failed, or genuinely none nearby)
    # -- fall back to the top-ranked suggested road corridor as the
    # proximity anchor instead, rather than leaving the constraint
    # disabled/zeroed. This is a fallback only: real existing-road data
    # (a non-empty road_geometries_utm above) is left untouched.
    road_data_is_fallback = False
    if not road_geometries_utm:
        road_geometries_utm = _suggested_corridor_as_road_fallback(boundary_coordinates, dem)
        road_data_is_fallback = road_geometries_utm is not None

    candidates = find_candidate_solar_zones(
        dem, production_areas, water_zones, road_geometries_utm, boundary_polygon_utm, **zone_kwargs
    )

    if check_prime_farmland and candidates:
        try:
            wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
            farmland_classifications = get_farmland_classification_for_polygon(wkt_polygon)
            candidates = flag_prime_farmland_conflicts(candidates, farmland_classifications)
        except Exception:
            pass  # SSURGO outage -- candidates just won't carry a prime_farmland_conflict flag this run

    return {
        "zones_geojson": candidates_to_geojson(candidates, road_data_is_fallback=road_data_is_fallback),
        "all_scored_candidates": candidates,
        "selected_structure_site": select_optimal_structure_site(candidates),
    }


def fetch_and_select_optimal_structure_site(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
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
    result = identify_solar_candidate_zones(boundary_coordinates, dem=dem, **zone_kwargs)
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

    print("Identifying solar structure candidates for property boundary...\n")

    try:
        result = identify_solar_candidate_zones(property_boundary)
        print(summarize_solar_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
