"""
water_suitability.py

Adds a suitability RANKING to water-system candidate zones that
water_candidate_zones.py has already identified — it does not change
which ground counts as a candidate or how its boundary is drawn (that
stays entirely water_candidate_zones.py's job, untouched here). Same
"score, don't regenerate" split as production_suitability.py and
solar_suitability.py already use over their own upstream candidate
layers.

WHAT SCORING MEASURES, AND WHAT IT DELIBERATELY DOES NOT. A score here
answers one question: what IS this site, as landform? Three factors, all
positively weighted, all describing properties of the ground itself --

    delivery  -> gravity_feed_factor      (weight 0.35)
    holding   -> soil_water_holding_factor (weight 0.30)
    geometry  -> basin_shape_factor        (weight 0.35)

Everything else this pipeline knows about a candidate -- refill context,
clearing cost, land-use tradeoffs -- is REPORTED alongside the score and
never folded into it. The dividing line is whether the farmer can change
it. Landform cannot be changed: a valley that does not close, a soil that
will not hold, a site below the ground it must serve are facts about the
place, and they belong in the number. Overlaps and context CAN be
changed, at a price the farmer is the one to weigh: clearing canopy,
relocating a track, conceding production ground, or trucking in refill
are decisions, not defects. Folding a decision into the score silently
makes it on the farmer's behalf and hides its cost; reporting it as a
measured percentage puts the tradeoff where it can be argued with.

    gravity_feed_factor (weight GRAVITY_FEED_SCORE_WEIGHT = 0.35) --
        real elevation differential/gradient vs. the production area a
        zone could serve (water_candidate_zones.py's own
        production_area_relationships, no longer a generation-time gate --
        see that module's docstring). Gravity delivery is the main
        economic reason a keyline pond/dam site is worth siting above
        production land at all rather than anywhere else convenient, so a
        real, comfortable gravity-feed relationship is the strongest
        positive signal this layer can report. A below-elevation
        (pump-required) candidate scores LOWER on this one factor, not
        excluded, not zeroed across the whole composite, and not
        apologized for in confidence_notes -- needing a pump is a real
        cost/maintenance tradeoff against site quality. A candidate with
        NO production area within service range scores 0.0 here and
        survives: see the no-relationship note below.

    soil_water_holding_factor (weight SOIL_WATER_HOLDING_SCORE_WEIGHT =
        0.30) -- real SSURGO saturated hydraulic conductivity
        (chorizon.ksat_r), area-weighted across each candidate's own
        footprint. A pond built on a soil that will not hold water is a
        real, physical viability problem regardless of how good every
        other factor looks, so this needs real weight, not a minor
        tiebreaker.

    basin_shape_factor (weight BASIN_SHAPE_SCORE_WEIGHT = 0.35) -- the
        geometry of the impoundment itself, computed ENTIRELY from the
        level-pool measurements water_candidate_zones.py already carries
        on the zone (abutments, cross-section stations, dam-band width).
        No new geometry is computed here and no DEM is re-read. Three
        equally-subweighted components -- enclosure, upstream persistence
        and wall economy -- see _basin_shape_factor() for each. This is
        the factor that distinguishes a real valley basin from a shallow
        swale, which is the distinction the whole multi-candidate
        generation sequence exists to surface.

WHAT WAS DELETED, AND WHY -- both deletions are design corrections, not
value retunes, and both are asserted absent by test_water_suitability.py.

    stream_permanence_factor (was weight 0.20) is GONE. It was
    bonus-only by construction (its neutral baseline was 0.5 and a
    missing stream could never cost a candidate anything), it was coarse
    (NHD stream centerlines carry a documented 100-300 m planimetric
    offset from the DEM on the reference property, so "the nearest
    mapped stream is 180 m away" was not a statement about this site),
    and what it was reaching for -- refill reliability -- is
    engineering-verification context rather than site geometry. A farm
    pond's refill is settled by a yield calculation and a season of
    observation, not by a weighted proxy. The NHD fetch, the FCode
    classification and the proximity reference are all deleted with it.

    topographic_factor (was weight 0.15) is GONE, both subcomponents.
    Its gradient sweet spot was a PROXY for exactly what the level-pool
    delineation now MEASURES DIRECTLY, and on the reference property the
    two disagree: the sweet spot awards a full 1.0 at a 10% valley grade,
    while the measured 2.5 m pool on that same channel dies roughly 25 m
    upstream. When a proxy and a direct measurement of the same property
    disagree, the measurement wins and the proxy goes -- keeping both
    would have double-counted the agreement and hidden the disagreement.
    Its contributing-area subcomponent was refill context, and leaves for
    the same reason the stream factor did. NOTE: the zone dict's OWN
    contributing-area and slope aggregates (contributing_area_cells,
    slope_pct) are generation-side, remain untouched, and are still
    reported as informational properties -- what left is this module's
    scoring use of them, not the measurements.

These three weights (0.35 / 0.30 / 0.35, summing to 1.0) follow the same
"document the reasoning, not just the number" standard as
road_corridors.py's PRODUCTION_AVOIDANCE_SCORE_WEIGHT/
EROSION_AVOIDANCE_SCORE_WEIGHT -- CONFIGURABLE, tune against a real
property once ground-truthed.

REPORTED, NOT WEIGHTED. Every scored zone carries three overlap
measurements with identical sentinel semantics (None = never checked,
0.0 = checked and genuinely none): canopy_overlap_pct and
road_overlap_pct (both generation-side, unchanged) and
production_overlap_pct (this module's own, new -- the share of a
candidate's footprint sitting on ground the production layer selected).
Conceding production ground to a pond is a land-use tradeoff of exactly
the same standing as clearing canopy: a real cost, borne by the farmer,
which the survey's job is to measure and state rather than to decide.

NO CANDIDATE IS DROPPED FOR HAVING NO PRODUCTION AREA TO SERVE. A zone
whose representative point clears no production area within service
distance used to be discarded at generation time; that drop was a
temporary guard, and it is gone. Such a zone now scores
gravity_feed_factor = 0.0, carries an informational flag and a plain
confidence note, and appears in the ranking like any other. "The best
available site on this parcel" has to be answerable on a parcel with no
production land near water, and a survey that returns nothing there has
failed rather than concluded.

Unlike production_suitability.py/solar_suitability.py (which both report
a flat CONFIDENCE_LOW on every candidate — confidence there reflects the
UPSTREAM heuristics' own limitations, which are the same for every
candidate on that layer), this module computes REAL, differentiated
confidence per zone from how much of that SPECIFIC zone's scoring was
actually backed by live, checked data: whether its own soil footprint had
a real, checked SSURGO ksat_r reading covering a meaningful share of its
area, and whether its level-pool MEASUREMENTS came back complete -- every
cross-section station actually measured, and at least one abutment search
that ran on a usable stem direction. See _confidence_for(). This genuinely
varies zone-to-zone even within one live run (zones sit over different
soil map units with different coverage, and a short or flat-tied stem
leaves some zones with fewer real measurements than others), unlike a
single flat value that can only ever change between runs.

An incomplete measurement dents CONFIDENCE and never the score. A station
the stem walk could not reach is an ABSENT measurement, not a dry one --
valley_level_pool.py's own station-status contract -- so persistence is
computed over the measured stations only and its subweight is
redistributed when too few remain. Fabricating a 0.0 to fill the gap
would report ground nobody looked at as ground that holds no water.

No MIN_SUITABILITY_SCORE cutoff is applied here — every zone
water_candidate_zones.py generated is scored and returned, low score or
high. Filtering some out would silently re-introduce a threshold-based
exclusion of exactly the kind Part 1 of this feature removed (a real
below-elevation or otherwise imperfect candidate that scores low should
still be visible, not disappear before anyone sees the number).

This module ranks (`rank` — 1 = highest suitability_score, over ALL
returned zones) every zone water_candidate_zones.py generated — nothing is
filtered out of the ranked list itself, same "zone, not a point" framing
water_candidate_zones.py already states in its own confidence_notes.
select_optimal_water_zone() adds one further, explicit, deliberately
simple step on top of that ranking: picking the single rank-1 zone as
"the plan" for downstream consumers (e.g. tree_zone_candidates.py's search-
space subtraction) that need one unambiguous answer rather than the full
candidate set. Per product decision, this app targets small farms only —
one well-suited water zone is sufficient, so no multi-candidate
coexistence logic sits on top of this selection.

    water_candidate_zones.find_candidate_zones() zones
        --> [this module] per-zone soil fetch (SSURGO ksat_r)
        --> per-zone scoring (gravity / soil / basin shape), all three
            factors read off the zone's own carried measurements and its
            own soil footprint -- no second DEM pass, no hydrology fetch
        --> per-zone production-overlap measurement (reported, not scored)
        --> enriched "water_system_candidate" features (same layer,
            same zones -- with suitability_score/*_factor/confidence
            properties added)

score_water_zones() is the pure scoring core: it takes already-computed
zones/dem plus optionally pre-fetched per-zone soil data, and does no
network I/O itself — same pure-core-vs-network-fetch split as every other
candidate-scoring module in this pipeline, so the scoring math is
unit-testable against synthetic input independent of whether SSURGO is
reachable. It no longer takes `valleys`: nothing in scoring reads a valley
any more (that was the deleted topographic factor's only use).
identify_water_suitability() still takes and still needs `valleys` -- it
forwards them into find_candidate_zones(), whose keypoint self-compute
path would otherwise re-delineate the same valleys a second time.
identify_water_suitability() is the fetch-and-score entry point. Like
water_candidate_zones.identify_water_system_candidate_zones(), it accepts
dem/boundary_polygon_utm/valleys/production_areas as independent, optional
overrides -- each falls back to being self-computed exactly as before if
not supplied, so an upstream orchestrator that has already computed some
or all of these (e.g. a shared pipeline-context pass reused across several
KSOP steps) can pass them straight through instead of this module
re-deriving or re-fetching its own copies.

production_areas' self-compute fallback specifically sources production_
area_ceiling.identify_optimized_production_areas()'s own scored_patches
(the optimized, ceiling-trimmed production geometry), NOT production_
area.identify_production_areas()'s raw, un-ceiling-trimmed patches --
this was a real, deliberate fix, not a plumbing-only change.
road_corridors.py already scores/excludes against production_area_
ceiling.py's OPTIMIZED production geometry (identify_optimized_
production_areas(...)["scored_patches"], with a RuntimeError guard on the
expected shape). Before this fix, this module's OWN production-area
input -- which feeds gravity-feed scoring (production_area_relationships /
primary_production_area_relationship, see _gravity_feed_factor() below)
and therefore which water zone gets selected as rank 1 -- came from the
RAW, un-trimmed identify_production_areas() instead: two different
notions of "where's the production land" feeding two decisions
(water-zone selection, then road routing) that are supposed to be
looking at the same property. Switching this module's own default source
to the optimized/ceiling-trimmed patches means selected_water_zone can
genuinely shift on a real property versus the pre-fix behavior, since
candidate zones are now scored against smaller, ceiling-trimmed
production-area geometry -- an intentional correction, not a regression.

PRESENTATION IS CAPPED; GENERATION AND RANKING ARE NOT. Every candidate
is scored, ranked and returned -- see the no-cutoff note above -- but the
narrative block and the report prose describe only the top
WATER_ZONE_PRESENTATION_TOP_N by rank, plus one line stating how many
survivors there are in total. Generation is deliberately uncapped and a
real parcel can produce a dozen candidates; listing all of them in prose
buries the ones that matter without adding a decision the reader can act
on. The full ranked list stays in all_scored_zones and in the GeoJSON,
which is where a reader who wants candidate nine goes.

build_narrative_data() produces that block, and report_generator.py's
water candidate-zones summary renders the top-N view when it is supplied,
falling back to water_candidate_zones.py's own generation-side block when
it is not (an unscored run has no ranks to trim by, and inventing an
order there would be worse than listing everything).
"""

import math
from typing import Optional

from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM, make_feature, make_feature_collection
from production_area import (
    METERS_PER_FOOT,
    _fetch_road_exclusion_union_utm,
    get_required_tree_root_zone_mask_utm,
    identify_production_areas,
)
from raster_grid import SQUARE_METERS_PER_ACRE
from production_area_ceiling import identify_optimized_production_areas
from soil_data import get_saturated_hydraulic_conductivity_for_polygon, get_soil_geometries_for_polygon
from valley_delineation import delineate_valleys
from valley_level_pool import ABUTMENT_SEARCH_HALF_WIDTH_METERS, STATION_MEASURED
from water_candidate_zones import (
    WATER_ZONE_CANOPY_BUFFER_METERS,
    _ROAD_CHECK_UNCHECKED,
    find_candidate_zones,
)

# The EXPLICIT "the water pipeline already ran and selected NOTHING" answer,
# for forwarding selected_water_zone between pipeline steps. select_optimal_
# water_zone()/fetch_and_select_optimal_water_zone() themselves still return
# plain None for that outcome (their public contract, unchanged) -- but every
# selected_water_zone override parameter downstream (road_corridors.identify_
# road_corridor_candidates(), solar_suitability.identify_solar_candidate_
# zones(), tree_zone_candidates.identify_tree_zone_candidates()) treats None
# as "not supplied" (this pipeline's standard None-as-sentinel override
# convention) and reacts by re-running this ENTIRE module's pipeline as its
# self-compute fallback -- the single most expensive fallback in the
# pipeline, measured firing FIVE times across one build_pipeline_context()
# run on a no-qualifying-water-zone parcel. Same trap, and same fix, as
# pipeline_context.py's selected_road_corridor field (which forwards build_
# road_network()'s full dict, branches=[] and all, NEVER None): an empty
# answer has to be forwarded as a real, explicit value to be reused at all.
# Roads already had a non-None shape for "nothing" (the empty network dict);
# a water-zone selection of "nothing" has no dict shape of its own, so this
# constant IS that explicit value. An orchestrator that already ran the
# selection passes `selected_water_zone if selected_water_zone is not None
# else NO_WATER_ZONE`; each accepting entry point normalizes it back to None
# internally (its downstream code keeps None's existing "no zone" meaning)
# and skips the self-compute. A caller-supplied bare None still self-computes
# exactly as before -- the existing override convention is untouched.
NO_WATER_ZONE = object()

# --- composite weights (must sum to 1.0). See module docstring for the
# full reasoning behind each: delivery, holding, geometry -- what the site
# IS, with everything the farmer could change reported beside the score
# rather than folded into it. CONFIGURABLE.
GRAVITY_FEED_SCORE_WEIGHT = 0.35
SOIL_WATER_HOLDING_SCORE_WEIGHT = 0.30
BASIN_SHAPE_SCORE_WEIGHT = 0.35

_WEIGHT_SUM = (
    GRAVITY_FEED_SCORE_WEIGHT + SOIL_WATER_HOLDING_SCORE_WEIGHT + BASIN_SHAPE_SCORE_WEIGHT
)
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"water suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

SUITABILITY_SCORE_SCALE = 100

# How many candidates the NARRATIVE and the report prose describe, by
# rank. NOT a filter: generation is uncapped, every candidate is scored
# and ranked, and all_scored_zones / the GeoJSON carry the full list. This
# bounds PROSE only, because a dozen candidates described in full buries
# the three that matter. The narrative states the total survivor count
# beside the top N so the trim is visible rather than silent.
# CONFIGURABLE.
WATER_ZONE_PRESENTATION_TOP_N = 3

# --- gravity-feed factor -------------------------------------------------

# At/above this gradient, gravity_feed_factor reaches its 1.0 ceiling --
# reusing the exact value water_candidate_zones.py's old hard gate used
# (MIN_GRAVITY_GRADIENT = 0.01, i.e. 1%) as the threshold for "comfortably,
# reliably gravity-feeds," now as a scoring ceiling rather than a pass/fail
# cutoff. CONFIGURABLE.
GRAVITY_FULL_CREDIT_GRADIENT_PCT = 1.0

# gravity_feed_factor at exactly 0% differential (level with the
# production area): meaningfully worse than a real gravity win, but not
# yet a pump-lift deficit either -- a real, distinct middle case.
# CONFIGURABLE.
GRAVITY_LEVEL_GROUND_FACTOR = 0.6

# At/beyond this deficit gradient, gravity_feed_factor floors at
# GRAVITY_MIN_FACTOR -- further steepening past a substantial pump lift
# isn't meaningfully distinguishable at this heuristic's resolution (a big
# pump vs. a huge pump), so the score stops decreasing rather than
# implying false precision. CONFIGURABLE.
GRAVITY_MAX_DEFICIT_GRADIENT_PCT = -5.0

# Floor for a below-elevation (pump-required) candidate — deliberately
# NOT 0.0: this is still a real, valid, scoreable site (see module
# docstring); a pump is a cost/maintenance tradeoff against the other
# three factors, not a disqualification, so this factor alone never zeroes
# out a candidate's composite score. CONFIGURABLE.
GRAVITY_MIN_FACTOR = 0.2

# water_candidate_zones.py applies no minimum-service-distance gate, so a
# real zone can legitimately sit AT distance_m == 0 from the production
# area it serves (inside/touching a patch that covers most of the parcel —
# see that module's own comment). At distance 0, gradient (rise/run) is
# mathematically undefined -- there's no run to divide the real elevation
# differential by. Silently defaulting the resulting gradient_pct to 0.0
# (water_candidate_zones._zone_production_area_relationships()'s
# div-by-zero guard) was a real bug found live: every distance-0 zone
# scored an IDENTICAL, uninformative GRAVITY_LEVEL_GROUND_FACTOR (0.6)
# regardless of whether its real elevation_differential_m was +7m or -9m
# -- discarding exactly the signal this factor exists to report. Rather
# than inventing a fake "run" to force a percent-gradient number out of a
# 0m distance, a distance-0 zone is scored DIRECTLY off its real
# elevation_differential_m (meters of head, not percent grade) against its
# own, separate reference scale below -- deliberately smaller than
# GRAVITY_FULL_CREDIT_GRADIENT_PCT's implied meters-at-typical-distance,
# since there's no run diluting it here: a zone already sitting inside/
# against the production area only needs a modest, real few meters of
# elevation edge to be a comfortably usable gravity-feed relationship at
# that scale. CONFIGURABLE.
GRAVITY_ZERO_DISTANCE_FULL_CREDIT_METERS = 3.0
GRAVITY_ZERO_DISTANCE_MAX_DEFICIT_METERS = -3.0


def _scaled_gravity_score(value: float, full_credit_ref: float, max_deficit_ref: float) -> float:
    """
    Shared 0-1 interpolation shape for BOTH the percent-gradient scale
    (typical case, real distance > 0) and the meters-of-head scale
    (distance == 0 case, see GRAVITY_ZERO_DISTANCE_FULL_CREDIT_METERS
    above): 1.0 at/above full_credit_ref, GRAVITY_LEVEL_GROUND_FACTOR at
    exactly 0 (level ground), floors at GRAVITY_MIN_FACTOR at/beyond
    max_deficit_ref — never 0.0, see that constant's own comment for why.
    """
    if value >= full_credit_ref:
        return 1.0
    if value >= 0:
        fraction = value / full_credit_ref
        return GRAVITY_LEVEL_GROUND_FACTOR + fraction * (1.0 - GRAVITY_LEVEL_GROUND_FACTOR)
    if value <= max_deficit_ref:
        return GRAVITY_MIN_FACTOR
    fraction = value / max_deficit_ref  # in (0, 1)
    return GRAVITY_LEVEL_GROUND_FACTOR - fraction * (GRAVITY_LEVEL_GROUND_FACTOR - GRAVITY_MIN_FACTOR)


def _gravity_feed_factor(elevation_differential_m: float, distance_m: float, gradient_pct: float) -> float:
    """
    0-1 score for a zone's real elevation relationship to the production
    area it could best serve (water_candidate_zones.py's
    primary_production_area_relationship). Continuous and monotonic.

    distance_m == 0 (the zone sits inside/touching the production area --
    a real, expected case, not an edge case to special-case away) scores
    directly off elevation_differential_m against the
    GRAVITY_ZERO_DISTANCE_*_METERS references, NOT off gradient_pct (which
    water_candidate_zones.py reports as 0.0 there, a real "undefined
    gradient" placeholder, not a real 0% grade — see this module's
    GRAVITY_ZERO_DISTANCE_FULL_CREDIT_METERS comment for why scoring that
    placeholder directly was a real bug). Every distance_m > 0 case scores
    off the normal percent-gradient scale, unchanged.
    """
    if distance_m == 0:
        return _scaled_gravity_score(
            elevation_differential_m, GRAVITY_ZERO_DISTANCE_FULL_CREDIT_METERS, GRAVITY_ZERO_DISTANCE_MAX_DEFICIT_METERS
        )
    return _scaled_gravity_score(gradient_pct, GRAVITY_FULL_CREDIT_GRADIENT_PCT, GRAVITY_MAX_DEFICIT_GRADIENT_PCT)


# --- soil water-holding factor (SSURGO chorizon.ksat_r) ------------------

# NRCS's own standard Ksat class breakpoints (Soil Survey Manual;
# micrometers/second) -- see soil_data.get_saturated_hydraulic_
# conductivity_for_polygon()'s own comment for the full verification and
# reasoning (ksat_r vs. awc_r, chorizon vs. component). WATER_HOLDING_GOOD
# (0.1) is the top of the "low" class -- comfortably slow/water-holding
# for a pond. WATER_HOLDING_POOR (100.0) is the bottom of the "very
# high"/rapid class -- comfortably too permeable without a liner.
# CONFIGURABLE.
WATER_HOLDING_GOOD_KSAT_UM_PER_S = 0.1
WATER_HOLDING_POOR_KSAT_UM_PER_S = 100.0

# Neutral default when no real ksat_r reading is available for a zone
# (fetch failed, or no SSURGO geometry actually overlapped its footprint)
# -- same "unknown defaults to neutral, not penalized" convention
# solar_suitability.py's _production_proximity_score() and
# tree_zone_candidates.py's unavailable-factor handling both already use.
WATER_HOLDING_UNAVAILABLE_FACTOR = 0.5

# A zone's own soil coverage (the fraction of its footprint actually
# matched to SSURGO geometry with a usable ksat_r) below this fraction is
# treated as too thin a sample to trust for scoring OR confidence -- same
# "a component covering just 5% of an area doesn't tell you what's really
# there" reasoning as soil_data._component_confidence(). CONFIGURABLE.
MIN_SOIL_COVERAGE_FRACTION = 0.3


def _water_holding_factor(ksat_r_um_per_s: Optional[float]) -> float:
    """
    0-1 score from real SSURGO saturated hydraulic conductivity: 1.0 at/
    below WATER_HOLDING_GOOD_KSAT_UM_PER_S (comfortably slow -- holds
    water well), 0.0 at/above WATER_HOLDING_POOR_KSAT_UM_PER_S
    (comfortably rapid -- leaks badly), on a LOG scale between them (Ksat
    spans several orders of magnitude in practice, so a linear scale would
    barely differentiate the moderately-low/moderately-high range where
    most real soils actually fall). WATER_HOLDING_UNAVAILABLE_FACTOR
    (neutral) if no real reading is available.
    """
    if ksat_r_um_per_s is None:
        return WATER_HOLDING_UNAVAILABLE_FACTOR
    ksat = max(float(ksat_r_um_per_s), 1e-6)
    log_ksat = math.log10(ksat)
    log_good = math.log10(WATER_HOLDING_GOOD_KSAT_UM_PER_S)
    log_poor = math.log10(WATER_HOLDING_POOR_KSAT_UM_PER_S)
    fraction = (log_ksat - log_good) / (log_poor - log_good)
    return max(0.0, min(1.0, 1.0 - fraction))


def _area_weighted_ksat(
    zone_polygon_utm: Polygon, ksat_rows: list[dict], geometries_by_mukey: dict, dem_crs: str
) -> Optional[dict]:
    """
    Area-weighted mean ksat_r (micrometers/second) across every SSURGO map
    unit intersecting zone_polygon_utm's own footprint -- a zone can
    legitimately span more than one map unit, so this weights each map
    unit's ksat_r by how much of the zone's own area it actually covers,
    not just an unweighted average across whatever mukeys the query
    happened to return.

    Returns {'ksat_r_um_per_s': float, 'coverage_fraction': float} (a real
    checked-and-clean result), or None if nothing with a usable ksat_r
    reading actually overlaps the zone's footprint (checked, nothing
    usable found -- distinct from "never checked at all", which the
    caller represents by leaving this zone's id absent from its own dict
    entirely).
    """
    ksat_by_mukey: dict[str, float] = {}
    for row in ksat_rows:
        try:
            ksat_by_mukey[row["mukey"]] = float(row["ksat_r"])
        except (TypeError, ValueError, KeyError):
            continue

    zone_area = zone_polygon_utm.area
    if not ksat_by_mukey or zone_area <= 0:
        return None

    weighted_sum = 0.0
    total_overlap_area = 0.0
    for mukey, ksat_r in ksat_by_mukey.items():
        geometry = geometries_by_mukey.get(mukey)
        if geometry is None:
            continue
        geometry_utm = shape(transform_geom("EPSG:4326", dem_crs, geometry))
        overlap_area = zone_polygon_utm.intersection(geometry_utm).area
        if overlap_area <= 0:
            continue
        weighted_sum += ksat_r * overlap_area
        total_overlap_area += overlap_area

    if total_overlap_area <= 0:
        return None

    return {
        "ksat_r_um_per_s": weighted_sum / total_overlap_area,
        "coverage_fraction": min(1.0, total_overlap_area / zone_area),
    }


def _fetch_water_holding_data_for_zone(zone: dict, dem: dict) -> Optional[dict]:
    """
    Real network fetch for one zone's own footprint: SSURGO's saturated
    hydraulic conductivity (soil_data.get_saturated_hydraulic_conductivity_
    for_polygon()) plus real map-unit polygon geometry
    (soil_data.get_soil_geometries_for_polygon(), reused as-is), combined
    via _area_weighted_ksat(). Uses shape(...).wkt rather than
    soil_data.coordinates_to_wkt_polygon() because a water-system zone's
    geometry_wgs84 can legitimately be a MultiPolygon (water_candidate_
    zones.py's buffered runs don't always merge into one contiguous
    piece) -- coordinates_to_wkt_polygon() assumes a single-ring Polygon,
    which every OTHER caller in this pipeline's candidate geometry
    actually is, but a water-system zone isn't guaranteed to be.
    """
    wkt_polygon = shape(zone["geometry_wgs84"]).wkt
    ksat_rows = get_saturated_hydraulic_conductivity_for_polygon(wkt_polygon)
    if not ksat_rows:
        return None
    geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)
    return _area_weighted_ksat(zone["polygon_utm"], ksat_rows, geometries_by_mukey, dem["crs"])


# --- basin shape factor (level-pool geometry, already measured) ----------

# Sub-weights within basin_shape_factor. They sum to 1.0 and are EQUAL by
# decision, not by default: enclosure, upstream persistence and wall
# economy each describe a different way a valley either is or is not a
# basin, and there is no ground-truthed basis yet for ranking one above
# the others. Equal thirds say that honestly; an unequal split would imply
# a calibration nobody has done. CONFIGURABLE.
BASIN_ENCLOSURE_SUBWEIGHT = 1.0 / 3.0
BASIN_PERSISTENCE_SUBWEIGHT = 1.0 / 3.0
BASIN_WALL_ECONOMY_SUBWEIGHT = 1.0 / 3.0

_BASIN_SUBWEIGHT_SUM = (
    BASIN_ENCLOSURE_SUBWEIGHT + BASIN_PERSISTENCE_SUBWEIGHT + BASIN_WALL_ECONOMY_SUBWEIGHT
)
assert math.isclose(_BASIN_SUBWEIGHT_SUM, 1.0, abs_tol=1e-6), (
    f"basin-shape subweights must sum to 1.0, got {_BASIN_SUBWEIGHT_SUM}"
)

# The upstream-persistence ratio at and above which that subcomponent
# scores a full 1.0.
#
# WHAT THE RATIO IS. valley_level_pool.py samples three cross-sections
# walking upstream from the dam line: station 0 at the wall, then two
# more at CROSS_SECTION_STATION_SPACING_METERS intervals along the traced
# stem. r is the share of the total flooded cross-sectional area that
# sits at the two UPSTREAM stations:
#
#     r = (area_1 + area_2) / (area_0 + area_1 + area_2)
#
# A real valley basin keeps meaningful width and depth as the water backs
# up, so a real share of its section area sits away from the wall. A
# swale floods a puddle against the dam line and is dry 25 m upstream:
# area_1 and area_2 are 0.0, r is 0.0, and the component scores 0.0. This
# is the DIRECT measurement that replaced the deleted topographic
# factor's gradient sweet-spot proxy.
#
# WHY 0.25. Three stations at equal spacing, so a perfectly prismatic
# channel of constant section would give r = 2/3. Real valleys taper, and
# the reference property's only genuine basin -- the confluence candidate
# -- measured r ~= 0.18, with every swale on the same parcel at 0.0. A
# reference of 0.25 puts that candidate at ~0.73 (a strong but not
# saturated score, leaving headroom for a better basin to outrank it) and
# leaves the swales at 0.0. The component therefore reproduces the by-eye
# terrain judgment on the one property where the answer is known.
#
# CALIBRATION PENDING, and this is the constant most in need of it: it is
# anchored to a SINGLE candidate on a SINGLE property. It should be
# re-derived once several real, built or surveyed ponds can be measured.
# CONFIGURABLE.
PERSISTENCE_REFERENCE_RATIO = 0.25

# Fewer than this many stations with status == measured and the
# persistence ratio is not computable at all (a ratio needs a denominator
# and at least one upstream term to be a ratio of anything). Below it the
# subcomponent is DROPPED and the remaining subweights are renormalized --
# never filled with a fabricated 0.0, which would report an unmeasured
# reach as a dry one. See the module docstring: an absent measurement
# dents confidence, not the score.
MIN_MEASURED_STATIONS_FOR_PERSISTENCE = 2


def _basin_enclosure_score(zone: dict) -> float:
    """
    Do the valley walls close on the dam line? 1.0 both sides, 0.5 one,
    0.0 neither -- read straight off valley_level_pool.py's abutment
    search, which walked out from the anchor along the dam axis looking
    for ground back up at the waterline.

    A SIDE THAT CROSSES A MAJOR DRAINAGE COUNTS AS NOT FOUND, even when
    the abutment search itself reported found. That flag means the dam
    band ran into a second creek before it found a shoulder, and a second
    creek is not a shoulder: damming across it is a different, larger
    structure impounding different water, with its own permitting and its
    own spillway. Treating it as enclosure would score the one finding
    that most disqualifies a site as if it were the finding that most
    qualifies it.
    """
    sides = 0
    for side in ("left", "right"):
        found = bool(zone.get(f"abutment_found_{side}"))
        crosses = bool(zone.get(f"dam_band_crosses_major_drainage_{side}"))
        if found and not crosses:
            sides += 1
    return sides / 2.0


def _persistence_ratio(stations: list[dict]) -> Optional[float]:
    """
    (area_1 + area_2) / (area_0 + area_1 + area_2) over the stations whose
    status is `measured`, in station-index order -- or None when fewer
    than MIN_MEASURED_STATIONS_FOR_PERSISTENCE were measured, or when the
    measured sections carry no area at all (a zero denominator is not a
    ratio of 0.0, it is an absence of anything to take a ratio of).

    UNREACHABLE STATIONS ARE EXCLUDED, NOT ZEROED. valley_level_pool.py
    marks a station unreachable_stem_end when the traced stem ended before
    reaching it and carries None for its width and area -- the status
    contract exists precisely so a consumer cannot read a missing
    measurement as a dry cross-section. Counting one as 0.0 area would
    manufacture the very reading the contract forbids.
    """
    measured = [
        st for st in sorted(stations, key=lambda s: s["station_index"])
        if st.get("status") == STATION_MEASURED and st.get("flooded_cross_section_area_m2") is not None
    ]
    if len(measured) < MIN_MEASURED_STATIONS_FOR_PERSISTENCE:
        return None
    areas = [float(st["flooded_cross_section_area_m2"]) for st in measured]
    total = sum(areas)
    if total <= 0.0:
        return None
    return sum(areas[1:]) / total


def _basin_persistence_score(ratio: Optional[float]) -> Optional[float]:
    """min(1.0, r / PERSISTENCE_REFERENCE_RATIO), or None when r itself is
    None -- the caller renormalizes rather than substituting a value."""
    if ratio is None:
        return None
    return min(1.0, max(0.0, ratio / PERSISTENCE_REFERENCE_RATIO))


def _basin_wall_economy_score(zone: dict, abutment_search_half_width_meters: float) -> float:
    """
    1 - (station-0 flooded width / the full sampling window), clamped to
    [0, 1]: how much dam does this pond cost? Station 0 sits ON the dam
    line, so its flooded width IS the width of water the wall has to hold
    back. Narrow water behind a wall means a short wall and a cheap
    structure; water spanning the whole valley means a long one.

    The denominator is the full sampling window -- 2x
    valley_level_pool.ABUTMENT_SEARCH_HALF_WIDTH_METERS -- because that is
    the widest thing the cross-section could have seen. A station-0 width
    that FILLS the window scores 0.0.

    THAT ZERO IS HONEST, INCLUDING WHERE IT IS INCONVENIENT. On the
    reference property the confluence candidate -- the one genuine basin
    on the parcel -- floods the entire window at station 0 and scores 0.0
    here. That is the correct reading: its wall really would be long,
    because two valleys meet there and the ground opens. The factor is not
    apologizing for it and is not being softened to avoid it. Enclosure
    and persistence still separate that candidate from the swales, which
    is the whole reason basin shape has three components rather than one.
    """
    window = 2.0 * abutment_search_half_width_meters
    if window <= 0:
        return 0.0
    station_zero = next(
        (st for st in zone["level_pool"]["stations"] if st["station_index"] == 0), None
    )
    if station_zero is None or station_zero.get("flooded_width_m") is None:
        # Station 0 sits at the anchor itself, so an unreachable one means
        # the stem walk failed at the wall. Report no wall economy rather
        # than inventing one; confidence already registers the gap.
        return 0.0
    return max(0.0, min(1.0, 1.0 - float(station_zero["flooded_width_m"]) / window))


def _basin_shape_factor(
    zone: dict, abutment_search_half_width_meters: float = ABUTMENT_SEARCH_HALF_WIDTH_METERS
) -> dict:
    """
    Blends the three subcomponents into basin_shape_factor, returning the
    sub-scores alongside it -- same pattern the deleted _topographic_
    factor() used, so a test or a confidence note can see WHY a zone
    scored the way it did rather than only the blend.

    Computed entirely from measurements the zone already carries. No DEM
    is read, no geometry is built, and no network is touched: this is a
    reading of valley_level_pool.py's output, which is what makes it a
    direct measurement rather than a proxy.

    When persistence is not computable (see _persistence_ratio()), its
    subweight is REDISTRIBUTED across the surviving components in
    proportion to their own weights, so the factor still spans 0-1 and a
    zone with a short stem is neither rewarded nor punished for the
    missing reading. It is reported as persistence_score=None, and the
    incompleteness lands on confidence instead.

    Returns
        {
            'basin_shape_factor': float,
            'enclosure_score': float,
            'persistence_score': float or None,   # None = not computable
            'persistence_ratio': float or None,
            'wall_economy_score': float,
            'persistence_available': bool,
        }
    """
    enclosure = _basin_enclosure_score(zone)
    ratio = _persistence_ratio(zone["level_pool"]["stations"])
    persistence = _basin_persistence_score(ratio)
    wall_economy = _basin_wall_economy_score(zone, abutment_search_half_width_meters)

    components = [
        (BASIN_ENCLOSURE_SUBWEIGHT, enclosure),
        (BASIN_WALL_ECONOMY_SUBWEIGHT, wall_economy),
    ]
    if persistence is not None:
        components.append((BASIN_PERSISTENCE_SUBWEIGHT, persistence))

    weight_total = sum(w for w, _ in components)
    factor = sum(w * v for w, v in components) / weight_total if weight_total > 0 else 0.0

    return {
        "basin_shape_factor": factor,
        "enclosure_score": enclosure,
        "persistence_score": persistence,
        "persistence_ratio": ratio,
        "wall_economy_score": wall_economy,
        "persistence_available": persistence is not None,
    }


# --- production overlap (REPORTED, never scored) -------------------------

def _production_overlap_pct(
    zone_polygon_utm: Polygon, production_areas: Optional[list[dict]]
) -> Optional[float]:
    """
    Percent of this candidate's own footprint sitting on ground the
    production layer selected -- the third overlap measurement, beside
    canopy_overlap_pct and road_overlap_pct, with the SAME sentinel
    semantics those two established:

        None -> never checked (no production geometry supplied at all)
        0.0  -> checked, and the candidate genuinely overlaps none

    Measured against render_fill_polygon_utm, the geometry the map
    actually draws, so a farmer comparing the number to the picture is
    comparing the same two things.

    REPORTED, NOT SCORED, deliberately: conceding production ground to a
    pond is a land-use tradeoff of exactly the same standing as clearing
    canopy for one. Which is worth more -- the acre of water or the acre
    of crop -- is the farmer's call and depends on the farm, so the survey
    measures it and states it rather than pricing it into a rank.
    """
    if production_areas is None:
        return None
    if not production_areas:
        return 0.0
    geometries = [
        pa["render_fill_polygon_utm"]
        for pa in production_areas
        if pa.get("render_fill_polygon_utm") is not None and not pa["render_fill_polygon_utm"].is_empty
    ]
    if not geometries:
        return 0.0
    zone_area = zone_polygon_utm.area
    if zone_area <= 0:
        return 0.0
    union = unary_union(geometries)
    return round(zone_polygon_utm.intersection(union).area / zone_area * 100, 1)


# --- confidence -----------------------------------------------------------

# Sentinel distinguishing "this zone's soil fetch genuinely ran and found
# nothing usable" (a real dict value of None) from "never checked at all"
# (fetch failed, or the check was skipped) -- same reasoning as
# production_suitability.py's _SOIL_CHECK_UNAVAILABLE.
_DATA_CHECK_UNAVAILABLE = object()


def _measurement_completeness_signal(zone: dict) -> bool:
    """
    Did this zone's level-pool measurement come back COMPLETE? Two
    conditions, both required:

      1. every cross-section station has status == measured. An
         unreachable_stem_end station means the traced stem ran out before
         the sampler got there, so part of the pool was never looked at.
      2. at least one abutment search completed on a usable stem
         direction. stem_direction_degenerate means the stem was too short
         to give the dam axis a direction, in which case the abutment walk
         went out along a fallback bearing and neither side's answer
         describes this valley's actual shoulders.

    This is the signal that REPLACED the deleted stream check. It is a
    better one for the same reason the stream factor was worth deleting:
    it is about THIS candidate's own measurements rather than about how
    close a coarsely-registered map line happens to fall.
    """
    stations = zone["level_pool"]["stations"]
    if not stations:
        return False
    if any(st.get("status") != STATION_MEASURED for st in stations):
        return False
    if zone["level_pool"].get("stem_direction_degenerate"):
        return False
    return bool(zone.get("abutment_found_left")) or bool(zone.get("abutment_found_right"))


def _confidence_for(soil_data: Optional[dict], measurement_complete: bool) -> str:
    """
    Real, per-zone confidence (unlike production_suitability.py/
    solar_suitability.py's flat CONFIDENCE_LOW -- see module docstring for
    why that's the right call here but not there): counts how many of two
    real, checkable quality signals this SPECIFIC zone actually has --

      1. a real SSURGO ksat_r reading covering at least
         MIN_SOIL_COVERAGE_FRACTION of this zone's own footprint (not just
         "the fetch didn't error" -- a technically-successful fetch that
         only covers 2% of the zone's area isn't a trustworthy read)
      2. a complete level-pool measurement set -- see
         _measurement_completeness_signal()

    2 signals -> CONFIDENCE_HIGH, 1 -> CONFIDENCE_MEDIUM, 0 -> CONFIDENCE_LOW.
    This genuinely differs zone-to-zone within a single live run: zones sit
    over different soil map units with different real coverage, and a zone
    on a short or flat-tied stem comes back with fewer real measurements
    than one on a long, well-traced channel.

    AN INCOMPLETE MEASUREMENT LANDS HERE, NOT ON THE SCORE. Where
    persistence could not be computed the basin factor renormalizes around
    the gap rather than filling it (see _basin_shape_factor()); the fact
    that something was missing is reported through this signal instead.
    """
    soil_signal = soil_data is not None and soil_data.get("coverage_fraction", 0.0) >= MIN_SOIL_COVERAGE_FRACTION

    signal_count = int(soil_signal) + int(bool(measurement_complete))
    if signal_count >= 2:
        return CONFIDENCE_HIGH
    if signal_count == 1:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


# --- confidence_notes -------------------------------------------------

def _gravity_note(primary_relationship: dict) -> str:
    differential = primary_relationship["elevation_differential_m"]
    distance = primary_relationship["distance_m"]
    gradient = primary_relationship["gradient_pct"]
    production_area_id = primary_relationship["production_area_id"]

    if primary_relationship["above_production_area"]:
        return (
            f"Sits {differential}m above production area {production_area_id} over {distance}m "
            f"({gradient}% grade) -- a real gravity-feed relationship."
        )
    return (
        f"Sits {abs(differential)}m BELOW production area {production_area_id} over {distance}m "
        f"({gradient}% grade) -- delivering water to that production area from here would need a "
        "pump. This is a real cost/maintenance tradeoff against this candidate's other real "
        "qualities (soil water-holding, basin shape) reflected in gravity_feed_factor "
        "below -- it is not a defect in the site itself, and this candidate remains a real, valid, "
        "scoreable option."
    )


def _soil_note(soil_data: Optional[dict]) -> str:
    if soil_data is None:
        return (
            "No real SSURGO saturated hydraulic conductivity (ksat_r) reading could be matched to "
            "this candidate's own footprint (fetch failed, no soil geometry available, or the check "
            "was skipped) -- soil_water_holding_factor defaulted to a neutral value, NOT measured."
        )
    return (
        f"Real SSURGO saturated hydraulic conductivity: {round(soil_data['ksat_r_um_per_s'], 3)} "
        f"micrometers/second (area-weighted across {round(soil_data['coverage_fraction'] * 100, 0)}% of "
        "this candidate's own footprint) -- lower values hold water better for a pond; see "
        "soil_data.get_saturated_hydraulic_conductivity_for_polygon()'s own docstring for the NRCS "
        "class breakpoints this is scored against and why ksat_r (not awc_r) is the right SSURGO "
        "field for pond-site water-holding, as opposed to plant-available water capacity."
    )


def _basin_note(basin: dict) -> str:
    enclosure_text = {1.0: "both", 0.5: "one", 0.0: "neither"}.get(
        basin["enclosure_score"], f"{basin['enclosure_score']}"
    )
    if basin["persistence_score"] is None:
        persistence_text = (
            "Upstream persistence: NOT COMPUTABLE -- fewer than "
            f"{MIN_MEASURED_STATIONS_FOR_PERSISTENCE} cross-sections were actually measured (the "
            "traced stem ended first), so this component was dropped and the remaining basin "
            "subweights were renormalized rather than filled with a fabricated zero. That is an "
            "absent measurement, not a dry one, and it is reported through confidence instead."
        )
    else:
        persistence_text = (
            f"Upstream persistence: {round(basin['persistence_ratio'], 3)} of the measured flooded "
            f"cross-section area sits at the two upstream stations rather than against the dam line "
            f"(scored {round(basin['persistence_score'], 3)} against a "
            f"{PERSISTENCE_REFERENCE_RATIO} reference) -- a real valley basin keeps section area as "
            "the water backs up; a swale floods a puddle at the wall and is dry 25m upstream."
        )
    return (
        f"Basin shape {round(basin['basin_shape_factor'], 3)}, from three measured components. "
        f"Enclosure: {enclosure_text} valley shoulder(s) were found on the dam line (a side where the "
        "dam band ran into a SECOND drainage counts as no shoulder -- that is a different, larger "
        f"structure, not an abutment). {persistence_text} Wall economy: "
        f"{round(basin['wall_economy_score'], 3)}, from how much of the cross-section sampling window "
        "the water at the dam line fills -- water spanning the whole window means a long wall, and "
        "scores zero honestly even on a genuinely good basin where two valleys meet."
    )


def _no_service_note() -> str:
    return (
        "No production area lies within this survey's service range of this candidate -- delivery "
        "would require distribution infrastructure this survey does not evaluate, so "
        "gravity_feed_factor is 0.0 here. The candidate is NOT dropped for it: the site's own "
        "landform is scored on its merits, and where the water goes is a separate question with its "
        "own answers (a longer line, a different production layout, or a use other than irrigation)."
    )


def _overlap_note(zone: dict) -> str:
    def _fmt(value, label):
        if value is None:
            return f"{label} NOT CHECKED"
        return f"{label} {value}%"

    return (
        "Reported, NOT scored -- siting costs a farmer weighs rather than a defect in the ground: "
        + ", ".join(
            (
                _fmt(zone.get("canopy_overlap_pct"), "existing tree canopy"),
                _fmt(zone.get("road_overlap_pct"), "mapped road corridor"),
                _fmt(zone.get("production_overlap_pct"), "selected production ground"),
            )
        )
        + " of this candidate's footprint. Clearing canopy, moving a track and conceding cropland are "
        "all real prices with real value on the other side; which is worth paying depends on the farm, "
        "so this survey measures them and leaves the trade to the farmer."
    )


WATER_SUITABILITY_INTRO_NOTE = (
    "This ADDS a suitability ranking to water-system candidate zones already identified by "
    "water_candidate_zones.py -- it does not change which ground counts as a candidate or its "
    "boundary (see that layer's own confidence_notes for the underlying zone-generation caveats). "
    "suitability_score (0-100) is a weighted composite of THREE independently-stored 0-1 factors "
    "describing what the site IS as landform: gravity_feed_factor (delivery, weight "
    f"{GRAVITY_FEED_SCORE_WEIGHT}), soil_water_holding_factor (holding, weight "
    f"{SOIL_WATER_HOLDING_SCORE_WEIGHT}), and basin_shape_factor (geometry, weight "
    f"{BASIN_SHAPE_SCORE_WEIGHT}). Refill context, clearing cost and land-use overlap are REPORTED "
    "beside the score and never folded into it -- landform cannot be changed, while those are "
    "tradeoffs the farmer is the one to weigh. See this module's own docstring for the full "
    "reasoning behind each weight and for what was deleted from this composite and why. This is a "
    "landform + soil ranking, not a certainty -- ground-truth before committing to a specific site "
    "within this zone."
)


def _confidence_notes_for(scored_zone: dict) -> str:
    parts = [WATER_SUITABILITY_INTRO_NOTE]
    if scored_zone["primary_production_area_relationship"] is None:
        parts.append(_no_service_note())
    else:
        parts.append(_gravity_note(scored_zone["primary_production_area_relationship"]))
    parts.append(_soil_note(scored_zone["_soil_data"]))
    parts.append(_basin_note(scored_zone["_basin"]))
    parts.append(_overlap_note(scored_zone))
    return " ".join(parts)


# --- scoring core -----------------------------------------------------

def score_water_zones(
    zones: list[dict],
    dem: dict,
    soil_data_by_zone_id: Optional[dict] = None,
    production_areas: Optional[list[dict]] = None,
    abutment_search_half_width_meters: float = ABUTMENT_SEARCH_HALF_WIDTH_METERS,
) -> list[dict]:
    """
    Pure scoring logic -- see module docstring for why this takes
    already-computed zones/dem plus optionally pre-fetched per-zone soil
    data rather than fetching anything itself.

    zones is water_candidate_zones.find_candidate_zones()'s own output,
    UNCHANGED -- this function does not alter membership or geometry, only
    scores it.

    `valleys` IS GONE FROM THIS SIGNATURE. It served exactly one consumer,
    the deleted topographic factor, and nothing in scoring reads a valley
    any more. identify_water_suitability() still takes valleys and still
    needs them -- it forwards them into find_candidate_zones() so the
    keypoint self-compute path does not re-delineate the same valleys a
    second time -- but carrying a parameter here that nothing reads would
    imply this layer still consults them.

    soil_data_by_zone_id maps zone['id'] to that zone's own pre-fetched
    data (_area_weighted_ksat()'s dict), or a real None if the fetch ran
    and genuinely found nothing usable. A zone id simply ABSENT from the
    dict (or the whole argument omitted) means "never checked" -- same
    None-vs-absent convention as production_suitability.py's
    disqualifying_soil_by_patch_id.

    production_areas is the same list find_candidate_zones() was given,
    used ONLY to measure production_overlap_pct (reported, never scored --
    see _production_overlap_pct()). Omitting it leaves that measurement at
    None, "never checked", exactly as an unfetched canopy mask leaves
    canopy_overlap_pct.

    Returns a flat list of scored zone dicts (zones's own dicts, extended
    with every property water_suitability_to_geojson() reports -- see that
    function for the full property list), sorted by suitability_score
    descending with 'rank' assigned (1 = highest). Every zone
    find_candidate_zones() returned is scored and returned here -- no
    MIN_SUITABILITY_SCORE-style cutoff (see module docstring for why).
    """
    soil_data_by_zone_id = soil_data_by_zone_id or {}

    scored: list[dict] = []

    for zone in zones:
        # A zone with no production area in service range is a real,
        # scoreable candidate now, not a dropped one (see the module
        # docstring). Its relationship list is empty, so there is no
        # headline relationship to read and gravity scores 0.0 -- the
        # honest answer to "how well does this deliver to production
        # ground" when there is no production ground to deliver to.
        primary_relationship = zone["primary_production_area_relationship"]
        if primary_relationship is None:
            gravity_factor = 0.0
        else:
            gravity_factor = _gravity_feed_factor(
                primary_relationship["elevation_differential_m"],
                primary_relationship["distance_m"],
                primary_relationship["gradient_pct"],
            )

        soil_entry = soil_data_by_zone_id.get(zone["id"], _DATA_CHECK_UNAVAILABLE)
        soil_data_available = soil_entry is not _DATA_CHECK_UNAVAILABLE
        soil_data = soil_entry if soil_data_available else None
        ksat_r_um_per_s = soil_data["ksat_r_um_per_s"] if soil_data is not None else None
        soil_factor = _water_holding_factor(ksat_r_um_per_s)

        basin = _basin_shape_factor(zone, abutment_search_half_width_meters)
        basin_factor = basin["basin_shape_factor"]

        composite = (
            GRAVITY_FEED_SCORE_WEIGHT * gravity_factor
            + SOIL_WATER_HOLDING_SCORE_WEIGHT * soil_factor
            + BASIN_SHAPE_SCORE_WEIGHT * basin_factor
        )

        measurement_complete = _measurement_completeness_signal(zone)

        scored_zone = {
            **zone,
            "suitability_score": round(composite * SUITABILITY_SCORE_SCALE, 1),
            "gravity_feed_factor": round(gravity_factor, 3),
            "soil_water_holding_factor": round(soil_factor, 3),
            "basin_shape_factor": round(basin_factor, 3),
            "basin_enclosure_score": round(basin["enclosure_score"], 3),
            "basin_persistence_score": (
                round(basin["persistence_score"], 3) if basin["persistence_score"] is not None else None
            ),
            "basin_persistence_ratio": (
                round(basin["persistence_ratio"], 4) if basin["persistence_ratio"] is not None else None
            ),
            "basin_wall_economy_score": round(basin["wall_economy_score"], 3),
            "basin_persistence_available": basin["persistence_available"],
            "ksat_r_um_per_s": round(ksat_r_um_per_s, 4) if ksat_r_um_per_s is not None else None,
            "soil_coverage_pct": round(soil_data["coverage_fraction"] * 100, 1) if soil_data is not None else None,
            "soil_data_available": soil_data_available,
            # REPORTED, never scored -- joins the two generation-side
            # overlaps the zone already carries, with the same sentinels.
            "production_overlap_pct": _production_overlap_pct(zone["polygon_utm"], production_areas),
            "has_service_relationship": primary_relationship is not None,
            "measurement_complete": measurement_complete,
            "confidence": _confidence_for(soil_data, measurement_complete),
            # underscore-prefixed: intermediate data confidence_notes needs, not part of the
            # reported property set (water_suitability_to_geojson() doesn't emit these directly)
            "_soil_data": soil_data,
            "_basin": basin,
        }
        scored_zone["confidence_notes"] = _confidence_notes_for(scored_zone)
        scored.append(scored_zone)

    scored.sort(key=lambda z: -z["suitability_score"])
    for rank, scored_zone in enumerate(scored, start=1):
        scored_zone["rank"] = rank

    return scored


def select_optimal_water_zone(scored_zones: list[dict]) -> Optional[dict]:
    """
    Explicit selection step on top of score_water_zones()'s own ranking:
    returns the single zone with rank == 1 (highest suitability_score) --
    no logic beyond that. Per product decision, this app targets small
    farms only, where one well-suited water zone is sufficient; no
    multi-candidate coexistence logic is needed here (unlike, say, a
    working landscape large enough to justify several ponds).

    Returns None if scored_zones is empty -- a real, reportable "no
    candidates at all" outcome, not an error.
    """
    if not scored_zones:
        return None
    return max(scored_zones, key=lambda z: z["suitability_score"])


def water_suitability_to_geojson(scored_zones: list[dict]) -> dict:
    """Wraps score_water_zones() output as a schema-conformant GeoJSON
    FeatureCollection on the SAME layer water_candidate_zones.py's own
    zones_to_geojson() uses ("water_system_candidate") -- these are the
    same zones, just enriched with suitability_score, its component
    factors, and real, differentiated confidence -- not a new/different
    layer, same precedent as production_suitability_to_geojson()."""
    features = []
    for zone in scored_zones:
        label = f"Water system candidate zone {zone['id']} (suitability rank {zone['rank']})"
        features.append(
            make_feature(
                feature_id=f"water-system-candidate-{zone['id']}",
                geometry=zone["geometry_wgs84"],
                layer="water_system_candidate",
                label=label,
                confidence=zone["confidence"],
                confidence_notes=zone["confidence_notes"],
                extra_properties={
                    "served_production_area_ids": zone["served_production_area_ids"],
                    "rank": zone["rank"],
                    "suitability_score": zone["suitability_score"],
                    "gravity_feed_factor": zone["gravity_feed_factor"],
                    "soil_water_holding_factor": zone["soil_water_holding_factor"],
                    "basin_shape_factor": zone["basin_shape_factor"],
                    "basin_enclosure_score": zone["basin_enclosure_score"],
                    "basin_persistence_score": zone["basin_persistence_score"],
                    "basin_persistence_ratio": zone["basin_persistence_ratio"],
                    "basin_wall_economy_score": zone["basin_wall_economy_score"],
                    "basin_persistence_available": zone["basin_persistence_available"],
                    "primary_production_area_relationship": zone["primary_production_area_relationship"],
                    "production_area_relationships": zone["production_area_relationships"],
                    "has_service_relationship": zone["has_service_relationship"],
                    "ksat_r_um_per_s": zone["ksat_r_um_per_s"],
                    "soil_coverage_pct": zone["soil_coverage_pct"],
                    "soil_data_available": zone["soil_data_available"],
                    "measurement_complete": zone["measurement_complete"],
                    # The three overlaps ride together, all three REPORTED
                    # and none of them scored -- see _overlap_note().
                    "canopy_overlap_pct": zone["canopy_overlap_pct"],
                    "road_overlap_pct": zone["road_overlap_pct"],
                    "production_overlap_pct": zone["production_overlap_pct"],
                    "wall_offset_downstream_m": zone["wall_offset_downstream_m"],
                    "flags": zone["flags"],
                    "representative_elevation_m": zone["representative_elevation_m"],
                },
            )
        )
    return make_feature_collection(features)


def summarize_water_suitability(scored_zones: list[dict]) -> str:
    """Full ranked table for terminal/diagnostic use -- NOT trimmed to
    WATER_ZONE_PRESENTATION_TOP_N. That cap is a PROSE decision (see
    build_narrative_data()); a diagnostic reader asking for the ranking
    wants the ranking."""
    if not scored_zones:
        return "No water system candidate zones to score."

    lines = [f"Water system candidate suitability ranking ({len(scored_zones)} candidate(s)):"]
    for zone in sorted(scored_zones, key=lambda z: z["rank"]):
        relationship = zone["primary_production_area_relationship"]
        if relationship is None:
            gravity_note = "NO PRODUCTION AREA IN RANGE"
        else:
            gravity_note = "gravity-feeds" if relationship["above_production_area"] else "PUMP-REQUIRED"
        persistence = zone["basin_persistence_score"]
        persistence_text = "n/a" if persistence is None else str(persistence)
        lines.append(
            f"  - Rank {zone['rank']}: zone {zone['id']}, score {zone['suitability_score']}/100 "
            f"(confidence={zone['confidence']}), gravity={zone['gravity_feed_factor']} ({gravity_note}), "
            f"soil={zone['soil_water_holding_factor']}, basin={zone['basin_shape_factor']} "
            f"[enclosure={zone['basin_enclosure_score']}, persistence={persistence_text}, "
            f"wall={zone['basin_wall_economy_score']}], overlaps canopy={zone['canopy_overlap_pct']}%/"
            f"road={zone['road_overlap_pct']}%/production={zone['production_overlap_pct']}%"
        )
    return "\n".join(lines)


def _round1(value):
    return round(float(value), 1) if value is not None else None


def build_narrative_data(
    scored_zones: list[dict], top_n: int = WATER_ZONE_PRESENTATION_TOP_N
) -> dict:
    """
    Pre-digested, JSON-clean scoring facts for the report layer -- same
    contract every other module's build_narrative_data() follows: plain
    Python scalars only, no Shapely, no numpy, imperial where a farmer
    reads it, and no prose (report_generator.py writes the sentences).

    TOP-N BY RANK, WITH THE TOTAL STATED. `zones` carries only the top
    `top_n` candidates and `candidate_count` carries how many survived in
    total, so the report can say "3 of 9 shown, ranked" rather than
    silently implying there were three. The trim is presentation only:
    every candidate is still scored, still ranked, and still present in
    all_scored_zones and in the GeoJSON. See
    WATER_ZONE_PRESENTATION_TOP_N.

    Each described candidate carries its provenance (which family
    nominated it, and from which keypoint), its three factor scores with
    the basin sub-scores that explain the third, and the three overlap
    measurements that are reported rather than scored.
    """
    ranked = sorted(scored_zones, key=lambda z: z["rank"])
    presented = ranked[: max(0, int(top_n))]

    return {
        "zone_found": bool(ranked),
        "candidate_count": len(ranked),
        "presented_count": len(presented),
        "presentation_top_n": int(top_n),
        "factor_weights": {
            "gravity_feed": GRAVITY_FEED_SCORE_WEIGHT,
            "soil_water_holding": SOIL_WATER_HOLDING_SCORE_WEIGHT,
            "basin_shape": BASIN_SHAPE_SCORE_WEIGHT,
        },
        "zones": [
            {
                "id": zone["id"],
                "rank": zone["rank"],
                "suitability_score": zone["suitability_score"],
                "confidence": zone["confidence"],
                "area_acres": _round1(zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE),
                "provenance": {
                    "nominated_by": zone["nominated_by"],
                    "keypoint_id": zone["keypoint_id"],
                    "valley_id": zone["valley_id"],
                    "wall_offset_downstream_ft": (
                        _round1(zone["wall_offset_downstream_m"] / METERS_PER_FOOT)
                        if zone["wall_offset_downstream_m"] is not None
                        else None
                    ),
                    "anchor_off_parcel": bool(zone["anchor_off_parcel"]),
                },
                "factors": {
                    "gravity_feed": zone["gravity_feed_factor"],
                    "soil_water_holding": zone["soil_water_holding_factor"],
                    "basin_shape": zone["basin_shape_factor"],
                },
                "basin": {
                    "enclosure": zone["basin_enclosure_score"],
                    "persistence": zone["basin_persistence_score"],
                    "persistence_ratio": zone["basin_persistence_ratio"],
                    "persistence_available": zone["basin_persistence_available"],
                    "wall_economy": zone["basin_wall_economy_score"],
                },
                # All three REPORTED, none scored. None means never
                # checked; 0.0 means checked and genuinely none.
                "overlaps": {
                    "canopy_pct": zone["canopy_overlap_pct"],
                    "road_pct": zone["road_overlap_pct"],
                    "production_pct": zone["production_overlap_pct"],
                },
                "has_service_relationship": zone["has_service_relationship"],
                "measurement_complete": zone["measurement_complete"],
                "flags": list(zone["flags"]),
            }
            for zone in presented
        ],
    }


# --- fetch-and-score entry point ---------------------------------------

def identify_water_suitability(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    valleys: Optional[list[dict]] = None,
    production_areas: Optional[list[dict]] = None,
    keypoints: Optional[list[dict]] = None,
    check_soil: bool = True,
    zone_kwargs: Optional[dict] = None,
    canopy_height: Optional[dict] = None,
    **score_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    delineates valleys and production areas, generates water-system
    candidate zones (water_candidate_zones.py, unchanged), fetches real
    SSURGO ksat_r geometry per zone, scores the result, and returns the
    enriched "water_system_candidate" GeoJSON FeatureCollection.

    THERE IS NO NHD FETCH HERE ANY MORE. The stream-permanence factor was
    deleted (see the module docstring for why), and with it the only
    reason this entry point ever touched hydrology_data. One fewer
    network dependency on the water path is a side benefit, not the
    reason.

    dem, boundary_polygon_utm, valleys, and production_areas are all
    optional overrides, independently of one another -- each falls back to
    being self-computed exactly as before if not supplied, same "reuse
    what an upstream orchestrator already computed" pattern water_
    candidate_zones.identify_water_system_candidate_zones() already
    established for these same four values. See that function's own
    docstring for the general reasoning; the one thing that's different
    here is what production_areas defaults to when not supplied (see
    below).

    valleys IS STILL LOAD-BEARING even though scoring no longer reads a
    valley (the topographic factor that did is deleted). It is now
    FORWARDED INTO find_candidate_zones(), which hands it to
    keypoint_detection.detect_keypoints() on the keypoint self-compute
    path. Without that forward, a standalone call to this function
    delineates valleys once here and detect_keypoints() delineates the
    same valleys a second time inside generation -- the deferred fix the
    gates-narrow branch flagged, closed here. identify_water_system_
    candidate_zones() already forwarded its own copy; this path did not.
    test_water_suitability.py asserts delineate_valleys() runs exactly
    once on the standalone path by call count, not by inspection.

    keypoints is an optional pre-detected override in the same family --
    keypoint_detection.detect_keypoints()'s own list -- passed straight
    through to water_candidate_zones.find_candidate_zones(), which uses it
    as its FAMILY 1 nomination source and otherwise detects keypoints
    itself. Supplying it is what keeps keypoint detection to exactly ONE
    run per pipeline pass: build_pipeline_context() detects once and hands
    the same list to both water paths. This module reads nothing off a
    keypoint itself; it only forwards.

    canopy_height is an optional pre-fetched override in the same family:
    the SAME dict canopy_height_data.get_canopy_height_for_boundary()
    returns (e.g. parcel_data.ParcelData.canopy_height). When supplied it
    is forwarded to BOTH canopy consumers this function reaches -- the
    default production_areas fetch (identify_optimized_production_areas()
    above) and this module's own mandatory get_required_tree_root_zone_
    mask_utm() call -- so neither re-fetches canopy from the network; when
    None (the default) both fetch as before.

    production_areas defaults to production_area_ceiling.identify_
    optimized_production_areas()'s own scored_patches -- the SAME
    optimized/ceiling-trimmed production geometry road_corridors.py
    already scores/excludes against -- NOT production_area.identify_
    production_areas()'s raw, un-ceiling-trimmed patches. This matters
    here specifically because this module's own gravity-feed scoring
    (production_area_relationships / primary_production_area_relationship,
    see _gravity_feed_factor()) measures each candidate zone against
    whichever production area it could best serve, and that measurement is
    what decides selected_water_zone -- so this module's own notion of
    "where's the production land" needs to match road_corridors.py's, not
    a different, larger, untrimmed one. Supplying production_areas
    explicitly (e.g. an upstream orchestrator's already-computed optimized
    patches) still works exactly as before; this only changes this
    function's own SELF-COMPUTE fallback when production_areas is left
    unsupplied.

    Each zone's own SSURGO fetch degrades independently and gracefully (an
    SDA outage for one zone's footprint shouldn't block scoring for the
    others) -- same reasoning production_suitability.py's own per-patch
    soil fetch already uses. It is now the ONLY network fetch scoring
    itself performs.

    The production_areas this function resolved are forwarded into
    score_water_zones() so every candidate carries a real
    production_overlap_pct rather than the "never checked" sentinel.

    Returns:
        {
            'zones_geojson': dict,             # every scored zone, ranked
            'scored_zones': list[dict],        # same list, kept for backward compatibility
            'all_scored_zones': list[dict],    # same list -- the full, unfiltered ranking
            'selected_water_zone': Optional[dict],  # select_optimal_water_zone()'s single
                                                       # rank-1 answer, or None if no zones exist
            'narrative_data': dict,            # build_narrative_data() -- TOP-N by rank, with
                                               #   the total survivor count beside it
        }
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
        production_areas = identify_optimized_production_areas(
            boundary_coordinates, dem=dem, canopy_height=canopy_height
        )["scored_patches"]

    # Same mandatory-canopy/optional-road wiring identify_water_system_
    # candidate_zones() uses -- this entry point also reaches
    # find_candidate_zones() directly (not through that function), so it
    # needs its own copy of the same fetch-or-raise/fetch-or-degrade calls.
    #
    # BOTH ARE MEASUREMENT INPUTS NOW, not gates: canopy and roads no
    # longer gate water-zone nomination (see compute_water_eligible_
    # cells()); they feed each candidate's canopy_overlap_pct /
    # road_overlap_pct. The fetch-or-raise posture on canopy is kept
    # deliberately -- a silently missing measurement is worse than a loud
    # failure while this pipeline is being validated, and the pipeline path
    # supplies canopy_height from ParcelData anyway so the fetch is free
    # there. Road keeps its graceful degrade: an outage yields
    # road_overlap_pct=None ("not checked"), never a fabricated 0.0.
    canopy_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=WATER_ZONE_CANOPY_BUFFER_METERS, canopy_height=canopy_height
    )

    try:
        # No buffer_meters override: the default IS the intended value --
        # farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS, the single shared
        # definition of "how far off an existing road" (see that
        # constant's docstring), same as every other consumer.
        road_exclusion_union_utm = _fetch_road_exclusion_union_utm(boundary_coordinates, dem)
    except Exception:
        road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    zones = find_candidate_zones(
        dem,
        production_areas,
        boundary_polygon_utm,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
        keypoints=keypoints,
        # THE DEFERRED FIX, CLOSED. Without this forward the keypoint
        # self-compute path inside find_candidate_zones() re-delineates
        # the valleys this function already delineated above -- the same
        # DEM, the same answer, twice. Scoring itself reads nothing off a
        # valley any more (the topographic factor that did is deleted);
        # this is the parameter's only remaining job, and it is a real one.
        valleys=valleys,
        **(zone_kwargs or {}),
    )

    soil_data_by_zone_id: dict = {}
    if check_soil:
        for zone in zones:
            try:
                soil_data_by_zone_id[zone["id"]] = _fetch_water_holding_data_for_zone(zone, dem)
            except Exception:
                pass  # left absent from the dict -- score_water_zones() treats that as "never checked"

    scored = score_water_zones(
        zones, dem, soil_data_by_zone_id, production_areas=production_areas, **score_kwargs
    )

    return {
        "zones_geojson": water_suitability_to_geojson(scored),
        "scored_zones": scored,
        "all_scored_zones": scored,
        "selected_water_zone": select_optimal_water_zone(scored),
        "narrative_data": build_narrative_data(scored),
    }


def fetch_and_select_optimal_water_zone(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    **suitability_kwargs,
) -> Optional[dict]:
    """
    Convenience wrapper for callers (e.g. render_layout_map.py) that want
    a single best water system candidate zone directly from a boundary --
    fetches the DEM (unless one is passed in) and runs the full
    identify_water_suitability() pipeline, then returns its own
    selected_water_zone (select_optimal_water_zone()'s rank-1 answer, or
    None if no candidate zones were found). A thin fetch-and-select
    convenience call, not a second/different selection -- reuses
    identify_water_suitability()'s own selected_water_zone rather than
    re-deriving "the best one" independently, so there is exactly one
    definition of "selected" for callers working from a boundary and
    callers working from an already-scored list to agree on.

    boundary_polygon_utm=/valleys=/production_areas= overrides are
    available here too, despite not appearing in this signature -- they
    pass straight through **suitability_kwargs into identify_water_
    suitability(), same as check_soil=/zone_kwargs=/any
    score_kwargs already do.
    """
    result = identify_water_suitability(boundary_coordinates, dem=dem, **suitability_kwargs)
    return result["selected_water_zone"]


if __name__ == "__main__":
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Scoring water system candidate zones for property boundary...\n")

    try:
        result = identify_water_suitability(property_boundary)
        print(summarize_water_suitability(result["scored_zones"]))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services, NHD, and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
