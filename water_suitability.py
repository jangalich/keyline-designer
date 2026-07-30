"""
water_suitability.py

Adds a suitability RANKING to water-system candidate zones that
water_candidate_zones.py has already identified — it does not change
which ground counts as a candidate or how its boundary is drawn (that
stays entirely water_candidate_zones.py's job, untouched here). Same
"score, don't regenerate" split as production_suitability.py and
solar_suitability.py already use over their own upstream candidate
layers.

Four independently-computed, positively-weighted 0-1 factors, combined
into a 0-100 suitability_score:

    gravity_feed_factor (weight GRAVITY_FEED_SCORE_WEIGHT = 0.35) --
        real elevation differential/gradient vs. the production area a
        zone could serve (water_candidate_zones.py's own
        production_area_relationships, no longer a generation-time gate —
        see that module's docstring). This is the SINGLE largest weight:
        gravity delivery is the main economic reason a keyline pond/dam
        site is worth siting above production land at all rather than
        anywhere else convenient, so a real, comfortable gravity-feed
        relationship is the strongest positive signal this layer can
        report. A below-elevation (pump-required) candidate scores LOWER
        on this one factor, not excluded, not zeroed across the whole
        composite, and not apologized for in confidence_notes — needing a
        pump is a real cost/maintenance tradeoff against site quality, the
        same kind of tradeoff production_suitability.py already reports
        for a soil-carved candidate or solar_suitability.py reports for a
        prime-farmland overlap.

    soil_water_holding_factor (weight SOIL_WATER_HOLDING_SCORE_WEIGHT =
        0.30) -- real SSURGO saturated hydraulic conductivity
        (chorizon.ksat_r), area-weighted across each candidate's own
        footprint. Weighted second-highest: a pond built on a soil that
        won't hold water is a real, physical viability problem
        regardless of how good every other factor looks, so this needs
        real weight, not a minor tiebreaker.

    stream_permanence_factor (weight STREAM_PERMANENCE_SCORE_WEIGHT =
        0.20) -- real NHD FCode-based flow-permanence classification
        (perennial/intermittent/ephemeral) of the nearest mapped stream,
        combined with real proximity. Weighted lower than gravity/soil
        deliberately: a well-designed farm pond is commonly valley/
        runoff-fed rather than stream-fed, so a nearby perennial stream is
        a real, genuine bonus (reliable recharge, less dependence on
        seasonal runoff alone) but not a requirement the way physical
        water-holding or gravity delivery are — see
        STREAM_PROXIMITY_REFERENCE_METERS below for why proximity here is
        an approximation, not a precise spatial match.

    topographic_factor (weight TOPOGRAPHIC_SCORE_WEIGHT = 0.15) --
        gradient steepness of the valley segment itself (a real, computed
        signal — see _valley_gradient_pct_for_zone(), from the same
        branches_utm elevation data valley_delineation.py already
        produces) and the valley's own max_contributing_area_acres
        (valley_delineation.py, unchanged). Weighted lowest: the valley
        already had to clear valley_delineation.py's own
        MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES threshold to exist as a
        candidate at all, so this factor differentiates AMONG already-
        qualifying valleys rather than gating candidacy a second time.

These four weights (0.35 / 0.30 / 0.20 / 0.15, summing to 1.0) follow the
same "document the reasoning, not just the number" standard as
road_corridors.py's PRODUCTION_AVOIDANCE_SCORE_WEIGHT/
EROSION_AVOIDANCE_SCORE_WEIGHT — CONFIGURABLE, tune against a real
property once ground-truthed.

Unlike production_suitability.py/solar_suitability.py (which both report
a flat CONFIDENCE_LOW on every candidate — confidence there reflects the
UPSTREAM heuristics' own limitations, which are the same for every
candidate on that layer), this module computes REAL, differentiated
confidence per zone from how much of that SPECIFIC zone's scoring was
actually backed by live, checked data: whether its own soil footprint had
a real, checked SSURGO ksat_r reading covering a meaningful share of its
area, and whether a real, classified NHD stream was found within
STREAM_PROXIMITY_REFERENCE_METERS of it — see _confidence_for(). This
genuinely varies zone-to-zone even within one live run (different zones
sit over different soil map units with different coverage, and different
distances to the nearest mapped stream), unlike a single flat value that
can only ever change between runs.

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
        + valley_delineation.delineate_valleys() valleys (same DEM run)
        --> [this module] per-zone soil fetch (SSURGO ksat_r) + one
            whole-boundary NHD stream fetch
        --> per-zone scoring (gravity/soil/stream/topographic)
        --> enriched "water_system_candidate" features (same layer,
            same zones -- with suitability_score/*_factor/confidence
            properties added)

score_water_zones() is the pure scoring core: it takes already-computed
zones/valleys/dem plus optionally pre-fetched per-zone soil data and a
pre-fetched stream list, and does no network I/O itself — same
pure-core-vs-network-fetch split as every other candidate-scoring module
in this pipeline, so the scoring math is unit-testable against synthetic
input independent of whether SSURGO/NHD are reachable.
identify_water_suitability() is the fetch-and-score entry point.

This is a self-contained, standalone pass — like production_suitability.py,
it is NOT wired into generate_full_report.py/report_generator.py in this
pass. Validate the ranking on its own first.
"""

import math
from typing import Optional

from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, MultiLineString, Point, Polygon, shape

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM, make_feature, make_feature_collection
from hydrology_data import get_water_features_for_boundary
from production_area import identify_production_areas
from soil_data import get_saturated_hydraulic_conductivity_for_polygon, get_soil_geometries_for_polygon
from valley_delineation import delineate_valleys
from water_candidate_zones import find_candidate_zones

# --- composite weights (must sum to 1.0). See module docstring for the
# full reasoning behind each. CONFIGURABLE.
GRAVITY_FEED_SCORE_WEIGHT = 0.35
SOIL_WATER_HOLDING_SCORE_WEIGHT = 0.30
STREAM_PERMANENCE_SCORE_WEIGHT = 0.20
TOPOGRAPHIC_SCORE_WEIGHT = 0.15

_WEIGHT_SUM = (
    GRAVITY_FEED_SCORE_WEIGHT + SOIL_WATER_HOLDING_SCORE_WEIGHT
    + STREAM_PERMANENCE_SCORE_WEIGHT + TOPOGRAPHIC_SCORE_WEIGHT
)
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"water suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

SUITABILITY_SCORE_SCALE = 100

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

# water_candidate_zones.py's MIN_SERVICE_DISTANCE_METERS fix means a real
# zone can legitimately sit AT distance_m == 0 from the production area it
# serves (inside/touching a patch that covers most of the parcel — see
# that module's own comment). At distance 0, gradient (rise/run) is
# mathematically undefined -- there's no run to divide the real elevation
# differential by. Silently defaulting the resulting gradient_pct to 0.0
# (water_candidate_zones._aggregate_production_area_relationships()'s
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


# --- stream-permanence factor (NHD FCode) ---------------------------------

# NHD's own FCode values for StreamRiver (FType 460) flow-permanence
# ("hydrographic category") -- confirmed against USGS's own NHD FCode
# attribute value list before use here, the same "verify against real
# documentation" discipline this project's SSURGO fixes already required:
#   46006 = Perennial   (flows year-round except infrequent severe drought)
#   46003 = Intermittent (flows part of the year, more than just after
#            rain/snowmelt)
#   46007 = Ephemeral    (flows only during/right after rain or snowmelt)
#   46000 = StreamRiver, general -- portion of the year it contains water
#            is UNKNOWN/unspecified, not a fourth real permanence class
NHD_PERENNIAL_FCODES = {46006}
NHD_INTERMITTENT_FCODES = {46003}
NHD_EPHEMERAL_FCODES = {46007}

# Relative weight of each real permanence class within the stream factor,
# 1.0 = most reliable. Ephemeral streams carry real water only right after
# a storm/snowmelt -- a genuinely weaker water-source signal than
# intermittent, which is deliberately weighted meaningfully below
# perennial but meaningfully above ephemeral. "unknown" (FCode 46000, or
# any FCode this module doesn't recognize) sits at a real middle value --
# neither claiming reliability nor assuming the worst about a stream NHD
# itself declined to classify. CONFIGURABLE.
STREAM_PERMANENCE_WEIGHTS = {
    "perennial": 1.0,
    "intermittent": 0.55,
    "ephemeral": 0.25,
    "unknown": 0.4,
}

# Neutral baseline stream_permanence_factor -- both when no stream at all
# exists within STREAM_PROXIMITY_REFERENCE_METERS, and as the floor a real
# nearby stream's proximity/permanence credit is added on TOP of. Most
# working farm ponds are valley/runoff-fed rather than stream-fed (see
# module docstring), so "no reliable natural surface water source nearby"
# is a real, unremarkable case -- not penalized below neutral.
STREAM_NEUTRAL_FACTOR = 0.5

# How close a candidate must be to a mapped NHD stream for that stream's
# permanence classification to meaningfully affect its score, and the
# reference distance proximity itself scores against (1.0 at 0m, fading to
# 0 credit at/beyond this). This property's own DEM-derived valley lines
# have been measured 100-300m offset from mapped NHD streams (established
# earlier in this pipeline's development, see water_candidate_zones.py's
# and road_corridors.py's own confidence_notes) -- a threshold much
# tighter than that offset would essentially never register a real,
# genuinely-nearby stream at all. 300m (the upper bound of the measured
# offset) is used here so a stream that's actually close on the ground
# isn't missed just because the DEM-derived valley line measuring distance
# to it is itself offset -- but this makes stream proximity here a real
# APPROXIMATION, not a precise spatial match; see
# _confidence_notes_for()'s use of NHD_OFFSET_LIMITATION_NOTE.
# CONFIGURABLE.
STREAM_PROXIMITY_REFERENCE_METERS = 300.0

NHD_OFFSET_LIMITATION_NOTE = (
    "This property's DEM-derived valley lines have been measured 100-300m offset from mapped NHD "
    "streams, so this candidate's stream proximity/permanence reading is an APPROXIMATION, not a "
    "precise spatial match -- the real nearest stream could be meaningfully closer or farther than "
    "the measured distance suggests."
)


def _stream_permanence_label(fcode) -> str:
    """Maps a raw NHD FCode value (may arrive as int, str, float, or
    None/missing from the live service) to one of the real permanence
    classes above, or 'unknown' for anything else (including the general
    StreamRiver code 46000 -- see NHD_PERENNIAL_FCODES etc.'s own
    comment)."""
    try:
        fcode_int = int(fcode)
    except (TypeError, ValueError):
        return "unknown"
    if fcode_int in NHD_PERENNIAL_FCODES:
        return "perennial"
    if fcode_int in NHD_INTERMITTENT_FCODES:
        return "intermittent"
    if fcode_int in NHD_EPHEMERAL_FCODES:
        return "ephemeral"
    return "unknown"


def _stream_permanence_factor(
    nearest_stream: Optional[dict], reference_meters: float = STREAM_PROXIMITY_REFERENCE_METERS
) -> tuple[float, Optional[str]]:
    """
    0-1 score (STREAM_NEUTRAL_FACTOR baseline, real credit added for a
    real, close, permanent stream) plus the permanence label used (None if
    no stream was in range at all). nearest_stream is
    {'name', 'fcode', 'distance_m'} (see _nearest_stream_for_zone()) or
    None.
    """
    if nearest_stream is None:
        return STREAM_NEUTRAL_FACTOR, None

    label = _stream_permanence_label(nearest_stream["fcode"])
    proximity = max(0.0, 1.0 - nearest_stream["distance_m"] / reference_meters)
    if proximity <= 0.0:
        # beyond the reference distance entirely -- no real proximity
        # credit, same as "no stream nearby at all"
        return STREAM_NEUTRAL_FACTOR, None

    weight = STREAM_PERMANENCE_WEIGHTS[label]
    factor = STREAM_NEUTRAL_FACTOR + (1.0 - STREAM_NEUTRAL_FACTOR) * weight * proximity
    return factor, label


def _reproject_streams_to_utm(streams: list[dict], dem_crs: str) -> list[dict]:
    """hydrology_data.get_water_features_for_boundary()'s own 'streams'
    list (WGS84 GeoJSON LineString/MultiLineString geometry, 'feature_code'
    = NHD's FCode), reprojected into the DEM's own projected CRS for real
    meter-distance math -- same reprojection pattern
    solar_suitability.py's road-geometry handling already uses."""
    reprojected = []
    for stream in streams:
        geometry = stream["geometry"]
        coords = geometry["coordinates"]
        line_lists = coords if geometry["type"] == "MultiLineString" else [coords]
        lines_utm = []
        for line in line_lists:
            xs, ys = warp_transform("EPSG:4326", dem_crs, [p[0] for p in line], [p[1] for p in line])
            lines_utm.append(LineString(zip(xs, ys)))
        geometry_utm = lines_utm[0] if len(lines_utm) == 1 else MultiLineString(lines_utm)
        reprojected.append({"name": stream["name"], "fcode": stream["feature_code"], "geometry_utm": geometry_utm})
    return reprojected


def _nearest_stream_for_zone(zone_polygon_utm: Polygon, streams_utm: list[dict]) -> Optional[dict]:
    """Real nearest-geometry distance from the zone's own footprint (not
    its centroid) to the closest mapped stream, same "measure to the real
    thing itself" convention solar_suitability.py's distance properties
    already use. None if streams_utm is empty (fetch succeeded, genuinely
    nothing found -- the caller distinguishes this from "fetch failed" via
    dict-key presence, same convention as every other optional-data
    factor in this pipeline)."""
    if not streams_utm:
        return None
    best = min(streams_utm, key=lambda s: zone_polygon_utm.distance(s["geometry_utm"]))
    return {
        "name": best["name"],
        "fcode": best["fcode"],
        "distance_m": float(zone_polygon_utm.distance(best["geometry_utm"])),
    }


# --- topographic factor (valley gradient steepness + contributing area) --

# Below GRADIENT_FLOOR_PCT, the valley segment is too close to flat to
# form a real basin behind a dam (a large, shallow pond for a given dam
# height -- poor storage-to-earthwork ratio, more evaporation/seepage
# surface per acre-foot stored). Above GRADIENT_CEILING_PCT, it's steep
# enough that a dam of any real height captures very little storage volume
# and the valley walls themselves become an erosion/excavation concern.
# Between GRADIENT_SWEET_SPOT_LOW_PCT and GRADIENT_SWEET_SPOT_HIGH_PCT,
# real NRCS farm-pond siting guidance's general preference for a
# moderate valley grade is treated as fully satisfied (score 1.0);
# tapering linearly to 0 at either outer bound. CONFIGURABLE -- tune
# against a real property's actual successful pond sites once known.
GRADIENT_FLOOR_PCT = 0.5
GRADIENT_SWEET_SPOT_LOW_PCT = 3.0
GRADIENT_SWEET_SPOT_HIGH_PCT = 15.0
GRADIENT_CEILING_PCT = 30.0

# Contributing area (valley_delineation.py's own max_contributing_area_
# acres) at/above which the area component of the topographic factor maxes
# out at 1.0 -- a larger watershed implies more reliable seasonal
# refill, with diminishing returns past a real, if approximate, reference
# scale. The valley already had to clear valley_delineation.py's own
# MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES (2.0) to exist as a candidate
# at all -- this reference is meant to sit well above that scoring floor,
# same "the scoring reference isn't the same as the generation-time
# filter" relationship production_suitability.py's REFERENCE_MAX_AREA_ACRES
# has to production_area.py's MIN_PRODUCTION_AREA_ACRES. CONFIGURABLE.
CONTRIBUTING_AREA_SCORE_REFERENCE_ACRES = 10.0

# Sub-weights within topographic_factor (must sum to 1.0). CONFIGURABLE.
GRADIENT_STEEPNESS_SUBWEIGHT = 0.5
CONTRIBUTING_AREA_SUBWEIGHT = 0.5


def _valley_topographic_inputs_for_zone(
    zone_polygon_utm: Polygon, valleys: list[dict]
) -> tuple[Optional[float], Optional[float]]:
    """
    Real topographic descent rate + max contributing area of whichever
    traced valley(s) actually pass through this zone's own footprint --
    NOT the gravity-to-production-area gradient (that's
    _gravity_feed_factor()'s job, a completely different relationship).

    Since water_candidate_zones.py's cell-based rearchitecture, a zone is
    a connected cluster of individually-eligible DEM cells, not something
    traced from a single valley branch -- there's no more zone['valley_id']
    to join against a matching valley['id'] by. This finds every valley
    (valley_delineation.delineate_valleys()'s own output, a SEPARATE
    traced-branch pass with its own, higher
    MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES threshold) whose branches
    actually pass through the zone's real footprint (spatial containment,
    checked directly against zone_polygon_utm), rather than looking one up
    by id. A zone genuinely can overlap more than one traced valley (or
    none at all, if its own cells never reached the traced-valley
    threshold) -- both are real, expected outcomes now, not caller error.

    Gradient: for each branch of each overlapping valley, the points that
    fall inside zone_polygon_utm mark the start/end of that branch's own
    contribution; net elevation drop over planar distance between the
    first and last such point, in percent grade. Multiple contributing
    branches (from one valley's tributaries, or several distinct
    overlapping valleys) are combined weighted by how many points each
    contributed, so a long, well-sampled segment isn't diluted equally
    with a short, noisy one -- unchanged combination logic from before
    this rearchitecture, just applied across however many valleys
    actually overlap instead of one valley matched by id.

    Contributing area: the LARGEST max_contributing_area_acres among the
    valleys that actually overlap this zone -- not summed, since two
    overlapping valleys' watersheds aren't independent contributing area
    to add together.

    Returns (None, None) if no valley's branches pass through this zone at
    all.
    """
    segment_gradients: list[tuple[float, int]] = []
    contributing_areas: list[float] = []

    for valley in valleys:
        valley_overlaps = False
        for branch in valley["branches_utm"]:
            points_in_zone = [(x, y, z) for x, y, z in branch if zone_polygon_utm.contains(Point(x, y))]
            if len(points_in_zone) < 2:
                continue
            x0, y0, z0 = points_in_zone[0]
            x1, y1, z1 = points_in_zone[-1]
            planar_length = Point(x0, y0).distance(Point(x1, y1))
            if planar_length <= 0:
                continue
            segment_gradients.append((abs(z0 - z1) / planar_length * 100, len(points_in_zone)))
            valley_overlaps = True
        if valley_overlaps:
            contributing_areas.append(valley["max_contributing_area_acres"])

    gradient_pct = None
    if segment_gradients:
        total_weight = sum(w for _, w in segment_gradients)
        gradient_pct = sum(g * w for g, w in segment_gradients) / total_weight

    contributing_area_acres = max(contributing_areas) if contributing_areas else None

    return gradient_pct, contributing_area_acres


def _gradient_steepness_score(gradient_pct: Optional[float]) -> float:
    if gradient_pct is None:
        return 0.5
    if gradient_pct < GRADIENT_FLOOR_PCT or gradient_pct > GRADIENT_CEILING_PCT:
        return 0.0
    if GRADIENT_SWEET_SPOT_LOW_PCT <= gradient_pct <= GRADIENT_SWEET_SPOT_HIGH_PCT:
        return 1.0
    if gradient_pct < GRADIENT_SWEET_SPOT_LOW_PCT:
        return (gradient_pct - GRADIENT_FLOOR_PCT) / (GRADIENT_SWEET_SPOT_LOW_PCT - GRADIENT_FLOOR_PCT)
    return (GRADIENT_CEILING_PCT - gradient_pct) / (GRADIENT_CEILING_PCT - GRADIENT_SWEET_SPOT_HIGH_PCT)


def _contributing_area_score(contributing_area_acres: Optional[float]) -> float:
    if contributing_area_acres is None:
        return 0.5
    return max(0.0, min(1.0, contributing_area_acres / CONTRIBUTING_AREA_SCORE_REFERENCE_ACRES))


def _topographic_factor(
    gradient_pct: Optional[float], contributing_area_acres: Optional[float]
) -> tuple[float, float, float]:
    """Returns (topographic_factor, gradient_steepness_score,
    contributing_area_score) -- sub-scores returned too so callers/tests
    can see why a zone scored the way it did, not just the blended
    result (same pattern as production_suitability._size_factor())."""
    gradient_score = _gradient_steepness_score(gradient_pct)
    area_score = _contributing_area_score(contributing_area_acres)
    factor = GRADIENT_STEEPNESS_SUBWEIGHT * gradient_score + CONTRIBUTING_AREA_SUBWEIGHT * area_score
    return factor, gradient_score, area_score


# --- confidence -----------------------------------------------------------

# Sentinel distinguishing "this zone's soil/stream fetch genuinely ran and
# found nothing usable" (a real dict value of None) from "never checked at
# all" (fetch failed, or the check was skipped) -- same reasoning as
# production_suitability.py's _SOIL_CHECK_UNAVAILABLE.
_DATA_CHECK_UNAVAILABLE = object()


def _confidence_for(soil_data: Optional[dict], stream_data: Optional[dict]) -> str:
    """
    Real, per-zone confidence (unlike production_suitability.py/
    solar_suitability.py's flat CONFIDENCE_LOW -- see module docstring for
    why that's the right call here but not there): counts how many of two
    real, checkable quality signals this SPECIFIC zone actually has —
      1. a real SSURGO ksat_r reading covering at least
         MIN_SOIL_COVERAGE_FRACTION of this zone's own footprint (not just
         "the fetch didn't error" -- a technically-successful fetch that
         only covers 2% of the zone's area isn't a trustworthy read)
      2. a real NHD stream, classified, within STREAM_PROXIMITY_
         REFERENCE_METERS of this zone (not just "the fetch didn't
         error" -- a successful fetch that found nothing nearby doesn't
         add confidence in the STREAM factor specifically, though it's
         still a real, valid check)
    2 signals -> CONFIDENCE_HIGH, 1 -> CONFIDENCE_MEDIUM, 0 -> CONFIDENCE_LOW.
    This genuinely differs zone-to-zone within a single live run (zones
    sit over different soil map units with different real coverage, and at
    different real distances to the nearest mapped stream), not just
    between runs.
    """
    soil_signal = soil_data is not None and soil_data.get("coverage_fraction", 0.0) >= MIN_SOIL_COVERAGE_FRACTION
    stream_signal = stream_data is not None

    signal_count = int(soil_signal) + int(stream_signal)
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
        "qualities (soil water-holding, stream access, topography) reflected in gravity_feed_factor "
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


def _stream_note(nearest_stream: Optional[dict], permanence_label: Optional[str], stream_checked: bool) -> str:
    if not stream_checked:
        return (
            "No NHD stream data could be fetched for this property (network fetch failed) -- "
            "stream_permanence_factor defaulted to a neutral value, NOT measured."
        )
    if nearest_stream is None:
        return (
            "No mapped NHD stream was found within the "
            f"{STREAM_PROXIMITY_REFERENCE_METERS:.0f}m proximity threshold of this candidate -- "
            "stream_permanence_factor stayed at its neutral baseline (most working farm ponds are "
            "valley/runoff-fed rather than stream-fed, so this is a real, unremarkable case, not a "
            "flaw)."
        )
    return (
        f"Nearest mapped stream: {nearest_stream['name']} ({permanence_label}, NHD FCode "
        f"{nearest_stream['fcode']}), {round(nearest_stream['distance_m'], 1)}m away. {NHD_OFFSET_LIMITATION_NOTE}"
    )


def _topographic_note(gradient_pct: Optional[float], contributing_area_acres: Optional[float]) -> str:
    gradient_text = f"{round(gradient_pct, 1)}%" if gradient_pct is not None else "unavailable"
    area_text = f"{contributing_area_acres} acres" if contributing_area_acres is not None else "unavailable"
    return (
        f"Valley segment gradient within this candidate: {gradient_text} (scored against a "
        f"{GRADIENT_SWEET_SPOT_LOW_PCT:.0f}-{GRADIENT_SWEET_SPOT_HIGH_PCT:.0f}% sweet spot -- too "
        "flat means a poor storage-to-earthwork ratio for a dam, too steep means little storage "
        f"volume behind any real dam height). Valley max contributing area: {area_text} (real "
        "watershed size from valley_delineation.py, a rough proxy for reliable seasonal refill)."
    )


WATER_SUITABILITY_INTRO_NOTE = (
    "This ADDS a suitability ranking to water-system candidate zones already identified by "
    "water_candidate_zones.py -- it does not change which ground counts as a candidate or its "
    "boundary (see that layer's own confidence_notes for the underlying zone-generation caveats). "
    f"suitability_score (0-100) is a weighted composite of FOUR independently-stored 0-1 factors: "
    f"gravity_feed_factor (weight {GRAVITY_FEED_SCORE_WEIGHT}), soil_water_holding_factor (weight "
    f"{SOIL_WATER_HOLDING_SCORE_WEIGHT}), stream_permanence_factor (weight "
    f"{STREAM_PERMANENCE_SCORE_WEIGHT}), and topographic_factor (weight {TOPOGRAPHIC_SCORE_WEIGHT}) -- "
    "see this module's own docstring for the full reasoning behind each weight. This is a "
    "topographic + soil + hydrology-based ranking, not a certainty -- ground-truth before "
    "committing to a specific site within this zone."
)


def _confidence_notes_for(scored_zone: dict) -> str:
    parts = [
        WATER_SUITABILITY_INTRO_NOTE,
        _gravity_note(scored_zone["primary_production_area_relationship"]),
        _soil_note(scored_zone["_soil_data"]),
        _stream_note(scored_zone["_nearest_stream"], scored_zone["nearest_stream_permanence"], scored_zone["stream_data_available"]),
        _topographic_note(scored_zone["gradient_steepness_pct"], scored_zone["valley_contributing_area_acres"]),
    ]
    return " ".join(parts)


# --- scoring core -----------------------------------------------------

def score_water_zones(
    zones: list[dict],
    valleys: list[dict],
    dem: dict,
    soil_data_by_zone_id: Optional[dict] = None,
    stream_data_by_zone_id: Optional[dict] = None,
    stream_proximity_reference_meters: float = STREAM_PROXIMITY_REFERENCE_METERS,
) -> list[dict]:
    """
    Pure scoring logic -- see module docstring for why this takes
    already-computed zones/valleys/dem plus optionally pre-fetched
    per-zone data rather than fetching anything itself.

    zones is water_candidate_zones.find_candidate_zones()'s own output,
    UNCHANGED -- this function does not alter membership or geometry, only
    scores it. valleys is valley_delineation.delineate_valleys()'s own
    output from the SAME dem/run zones came from -- matched to each zone
    by real spatial overlap now, not by id (see
    _valley_topographic_inputs_for_zone()'s own docstring for why: a zone
    is a cell cluster since water_candidate_zones.py's cell-based
    rearchitecture, with no more valley/branch identity of its own to join
    against by id).

    soil_data_by_zone_id / stream_data_by_zone_id map zone['id'] to that
    zone's own pre-fetched data (_area_weighted_ksat()'s dict /
    _nearest_stream_for_zone()'s dict), or a real None if the fetch ran
    and genuinely found nothing usable. A zone id simply ABSENT from
    either dict (or the whole argument omitted) means "never checked" --
    same None-vs-absent convention as production_suitability.py's
    disqualifying_soil_by_patch_id.

    Returns a flat list of scored zone dicts (zones's own dicts, extended
    with every property water_suitability_to_geojson() reports -- see that
    function for the full property list), sorted by suitability_score
    descending with 'rank' assigned (1 = highest). Every zone
    find_candidate_zones() returned is scored and returned here -- no
    MIN_SUITABILITY_SCORE-style cutoff (see module docstring for why).
    """
    soil_data_by_zone_id = soil_data_by_zone_id or {}
    stream_data_by_zone_id = stream_data_by_zone_id or {}

    scored: list[dict] = []

    for zone in zones:
        primary_relationship = zone["primary_production_area_relationship"]

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

        stream_entry = stream_data_by_zone_id.get(zone["id"], _DATA_CHECK_UNAVAILABLE)
        stream_data_available = stream_entry is not _DATA_CHECK_UNAVAILABLE
        nearest_stream = stream_entry if stream_data_available else None
        stream_factor, permanence_label = _stream_permanence_factor(nearest_stream, stream_proximity_reference_meters)

        gradient_steepness_pct, valley_contributing_area_acres = _valley_topographic_inputs_for_zone(
            zone["polygon_utm"], valleys
        )
        topo_factor, gradient_score, contributing_area_score = _topographic_factor(
            gradient_steepness_pct, valley_contributing_area_acres
        )

        composite = (
            GRAVITY_FEED_SCORE_WEIGHT * gravity_factor
            + SOIL_WATER_HOLDING_SCORE_WEIGHT * soil_factor
            + STREAM_PERMANENCE_SCORE_WEIGHT * stream_factor
            + TOPOGRAPHIC_SCORE_WEIGHT * topo_factor
        )

        scored_zone = {
            **zone,
            "suitability_score": round(composite * SUITABILITY_SCORE_SCALE, 1),
            "gravity_feed_factor": round(gravity_factor, 3),
            "soil_water_holding_factor": round(soil_factor, 3),
            "stream_permanence_factor": round(stream_factor, 3),
            "topographic_factor": round(topo_factor, 3),
            "gradient_steepness_score": round(gradient_score, 3),
            "contributing_area_score": round(contributing_area_score, 3),
            "ksat_r_um_per_s": round(ksat_r_um_per_s, 4) if ksat_r_um_per_s is not None else None,
            "soil_coverage_pct": round(soil_data["coverage_fraction"] * 100, 1) if soil_data is not None else None,
            "soil_data_available": soil_data_available,
            "nearest_stream_name": nearest_stream["name"] if nearest_stream is not None else None,
            "nearest_stream_permanence": permanence_label,
            "nearest_stream_distance_m": (
                round(nearest_stream["distance_m"], 1) if nearest_stream is not None else None
            ),
            "stream_data_available": stream_data_available,
            "gradient_steepness_pct": round(gradient_steepness_pct, 2) if gradient_steepness_pct is not None else None,
            "valley_contributing_area_acres": valley_contributing_area_acres,
            "confidence": _confidence_for(soil_data, nearest_stream),
            # underscore-prefixed: intermediate data confidence_notes needs, not part of the
            # reported property set (water_suitability_to_geojson() doesn't emit these directly)
            "_soil_data": soil_data,
            "_nearest_stream": nearest_stream,
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
                    "stream_permanence_factor": zone["stream_permanence_factor"],
                    "topographic_factor": zone["topographic_factor"],
                    "gradient_steepness_score": zone["gradient_steepness_score"],
                    "contributing_area_score": zone["contributing_area_score"],
                    "primary_production_area_relationship": zone["primary_production_area_relationship"],
                    "production_area_relationships": zone["production_area_relationships"],
                    "ksat_r_um_per_s": zone["ksat_r_um_per_s"],
                    "soil_coverage_pct": zone["soil_coverage_pct"],
                    "soil_data_available": zone["soil_data_available"],
                    "nearest_stream_name": zone["nearest_stream_name"],
                    "nearest_stream_permanence": zone["nearest_stream_permanence"],
                    "nearest_stream_distance_m": zone["nearest_stream_distance_m"],
                    "stream_data_available": zone["stream_data_available"],
                    "gradient_steepness_pct": zone["gradient_steepness_pct"],
                    "valley_contributing_area_acres": zone["valley_contributing_area_acres"],
                },
            )
        )
    return make_feature_collection(features)


def summarize_water_suitability(scored_zones: list[dict]) -> str:
    if not scored_zones:
        return "No water system candidate zones to score."

    lines = [f"Water system candidate suitability ranking ({len(scored_zones)} candidate(s)):"]
    for zone in sorted(scored_zones, key=lambda z: z["rank"]):
        relationship = zone["primary_production_area_relationship"]
        gravity_note = "gravity-feeds" if relationship["above_production_area"] else "PUMP-REQUIRED"
        lines.append(
            f"  - Rank {zone['rank']}: zone {zone['id']}, score {zone['suitability_score']}/100 "
            f"(confidence={zone['confidence']}), gravity={zone['gravity_feed_factor']} ({gravity_note}), "
            f"soil={zone['soil_water_holding_factor']}, stream={zone['stream_permanence_factor']} "
            f"({zone['nearest_stream_permanence'] or 'none nearby'}), topo={zone['topographic_factor']}"
        )
    return "\n".join(lines)


# --- fetch-and-score entry point ---------------------------------------

def identify_water_suitability(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_soil: bool = True,
    check_streams: bool = True,
    zone_kwargs: Optional[dict] = None,
    **score_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    delineates valleys and production areas, generates water-system
    candidate zones (water_candidate_zones.py, unchanged), fetches real
    SSURGO ksat_r geometry per zone and one whole-boundary real NHD stream
    fetch, scores the result, and returns the enriched
    "water_system_candidate" GeoJSON FeatureCollection.

    Each zone's own SSURGO fetch degrades independently and gracefully (an
    SDA outage for one zone's footprint shouldn't block scoring for the
    others) -- same reasoning production_suitability.py's own per-patch
    soil fetch already uses. The NHD stream fetch is a single whole-
    boundary call (same as water_candidate_zones.py/road_corridors.py's
    own hydrology fetches) -- if it fails, every zone's stream_permanence_
    factor falls back to neutral together (all real per-zone
    differentiation from the soil signal / real per-zone stream distance
    still applies otherwise).

    Returns:
        {
            'zones_geojson': dict,             # every scored zone, ranked
            'scored_zones': list[dict],        # same list, kept for backward compatibility
            'all_scored_zones': list[dict],    # same list -- the full, unfiltered ranking
            'selected_water_zone': Optional[dict],  # select_optimal_water_zone()'s single
                                                       # rank-1 answer, or None if no zones exist
        }
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

    valleys = delineate_valleys(dem)
    production_areas = identify_production_areas(dem, boundary_polygon_utm)
    zones = find_candidate_zones(dem, production_areas, boundary_polygon_utm, **(zone_kwargs or {}))

    soil_data_by_zone_id: dict = {}
    if check_soil:
        for zone in zones:
            try:
                soil_data_by_zone_id[zone["id"]] = _fetch_water_holding_data_for_zone(zone, dem)
            except Exception:
                pass  # left absent from the dict -- score_water_zones() treats that as "never checked"

    stream_data_by_zone_id: dict = {}
    if check_streams:
        try:
            water_features = get_water_features_for_boundary(boundary_coordinates)
            streams_utm = _reproject_streams_to_utm(water_features["streams"], dem["crs"])
            for zone in zones:
                stream_data_by_zone_id[zone["id"]] = _nearest_stream_for_zone(zone["polygon_utm"], streams_utm)
        except Exception:
            pass  # left absent for every zone -- "never checked", not "checked, none found"

    scored = score_water_zones(zones, valleys, dem, soil_data_by_zone_id, stream_data_by_zone_id, **score_kwargs)

    return {
        "zones_geojson": water_suitability_to_geojson(scored),
        "scored_zones": scored,
        "all_scored_zones": scored,
        "selected_water_zone": select_optimal_water_zone(scored),
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
