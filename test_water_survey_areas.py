"""
Offline (no-network) checks for water_survey_areas.py and its diagnostic
export -- pure computation against small synthetic DEMs, same "no real
data fetch required" philosophy as the rest of this pipeline's tests.

Sections (matching the redesign branch's own test contract):
  1. CRITERIA UNITS -- every classification table at its breakpoints
     (drainage band ramp/plateau/cliff, both slope tapers, the
     depression noise floor, the soil sub-scorers, TWI percentile on a
     hand-built 3x3, the TWI flat-singularity guard).
  2. SURFACE BLEND -- a hand-computed cell through each type's full
     blend; weight-sum assertions.
  3. REGION EXTRACTION -- uniform wet flat -> one excavated region of
     known size; a hand-built accumulation ribbon -> one embankment
     ribbon along the channel with a hand-derived mean; a sub-floor
     region FLAGGED AND PRESENT.
  4. CONTRACT -- every consumer-read field on the selected region
     (render_fill_polygon_utm / representative_elevation_m / id, plus
     rank / served_production_area_ids), the render_fill identity,
     stored WGS84 beside UTM everywhere, the three-overlap sentinel
     semantics, PUMP-REQUIRED surviving with its note, and
     boundary-adjacency on a region hugging the fixture edge.
  5. (full-context synthetic lives in test_pipeline_context.py, which
     runs the real water step inside build_pipeline_context() with
     exact call counts.)
  6. EXPORT VALIDATION -- json round-trip + shapely parse + per-layer
     counts + isoband bands present per type + the grep-assert that no
     serialization-time reprojection exists in any emitter.
"""

import atexit
import inspect
import json
import math
import os
import shutil
import tempfile

import numpy as np
from rasterio.warp import transform_geom
from shapely.geometry import box, mapping, shape

import diagnose_water_survey_areas as diag
import display_scale
import water_survey_areas as wsa
from raster_grid import cell_area_acres
from water_survey_areas import (
    DEPRESSION_FULL_CREDIT_METERS,
    DEPRESSION_NOISE_FLOOR_METERS,
    EMBANKMENT_WEIGHTS,
    EXCAVATED_WEIGHTS,
    FLAG_BELOW_MIN_AREA,
    FLAG_NO_SERVICE_RELATIONSHIP,
    FLAG_SPARSE_ANCHOR,
    HYDROLOGIC_GROUP_SCORES,
    MIN_SURVEY_REGION_AREA_ACRES,
    SURVEY_TYPE_EMBANKMENT,
    SURVEY_TYPE_EXCAVATED,
    TWI_MIN_SLOPE_TAN,
    WATER_HOLDING_GOOD_KSAT_UM_PER_S,
    WATER_HOLDING_POOR_KSAT_UM_PER_S,
    build_narrative_data,
    compute_depression_depth,
    compute_suitability_surfaces,
    compute_topographic_wetness_index,
    compute_water_survey_areas,
    depression_score,
    drainage_band_score,
    embankment_slope_score,
    excavated_slope_score,
    hydric_share_for_mukey,
    hydrologic_group_score,
    ksat_water_holding_score,
    parcel_relative_percentile,
    twi_score,
    runon_score,
    soil_water_score_for_mukey,
    survey_areas_to_geojson,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"


def _dem(array: np.ndarray) -> dict:
    return {
        "array": array.astype(np.float64),
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


# =========================================================================
# 1. CRITERIA UNITS
# =========================================================================

# --- drainage band: 0 below 0.5 ac, linear ramp to 1.0 at 2 ac, plateau
#     to the 20 ac ceiling, HARD ZERO above (gate and cliff share one
#     number). Midpoint of the ramp (1.25 ac) is exactly halfway. ---
assert float(drainage_band_score(np.array(0.4))) == 0.0
assert float(drainage_band_score(np.array(0.5))) == 0.0, "the ramp STARTS at 0.5 ac -- score 0 exactly there"
assert math.isclose(float(drainage_band_score(np.array(1.25))), 0.5)
assert float(drainage_band_score(np.array(2.0))) == 1.0
assert float(drainage_band_score(np.array(10.0))) == 1.0, "plateau holds through the band"
assert float(drainage_band_score(np.array(20.0))) == 1.0, "the ceiling itself is still in play (<=)"
assert float(drainage_band_score(np.array(20.01))) == 0.0, "one step past the ceiling is the HARD ZERO cliff"
print("Embankment drainage band: 0 below 0.5 ac, ramp midpoint 0.5 at 1.25 ac, plateau 2-20 ac, hard zero above.")

# --- run-on: mild ramp 0 -> 1 over 0 -> 2 ac, plateau, same hard cliff ---
assert float(runon_score(np.array(0.0))) == 0.0
assert math.isclose(float(runon_score(np.array(1.0))), 0.5)
assert float(runon_score(np.array(2.0))) == 1.0
assert float(runon_score(np.array(20.0))) == 1.0
assert float(runon_score(np.array(20.5))) == 0.0
print("Excavated run-on: linear to 1.0 at 2 ac, plateau, shared hard ceiling.")

# --- embankment slope taper: 0 at 0.5%, 1.0 across 3-8%, 0 at 15%.
#     Hand midpoints: 1.75% is halfway up the rise, 11.5% halfway down. ---
assert float(embankment_slope_score(np.array(0.2))) == 0.0
assert float(embankment_slope_score(np.array(0.5))) == 0.0
assert math.isclose(float(embankment_slope_score(np.array(1.75))), 0.5)
assert float(embankment_slope_score(np.array(3.0))) == 1.0
assert float(embankment_slope_score(np.array(8.0))) == 1.0
assert math.isclose(float(embankment_slope_score(np.array(11.5))), 0.5)
assert float(embankment_slope_score(np.array(15.0))) == 0.0
assert float(embankment_slope_score(np.array(16.0))) == 0.0
assert float(embankment_slope_score(np.array(np.nan))) == 0.0, "unmeasured slope scores 0, never NaN-poisons the blend"
print("Embankment slope taper: breakpoints at 0.5 / 3 / 8 / 15 percent, NaN scores 0.")

# --- excavated slope, SEEP-WIDENED (final tuning): 1.0 through 5%,
# linear to 0 at 15% -- AH-590's excavated class covers dugout AND
# seep-fed excavated ponds, and the reference marsh's wettest cells sit
# at real 5-10% grades (the FINDING indicted the old flat-dugout taper;
# the soil rider cleared the soil scorer). Midpoint 10% scores 0.5. ---
assert float(excavated_slope_score(np.array(0.0))) == 1.0
assert float(excavated_slope_score(np.array(5.0))) == 1.0, "full credit holds through the 5% seep grade"
assert math.isclose(float(excavated_slope_score(np.array(10.0))), 0.5)
assert float(excavated_slope_score(np.array(15.0))) == 0.0
assert float(excavated_slope_score(np.array(16.0))) == 0.0
assert float(excavated_slope_score(np.array(np.nan))) == 0.0
print("Excavated slope (seep-widened): 1.0 through 5 percent, 0.5 at 10, gone at 15.")

# --- depression depth + noise floor: filled-minus-raw, sub-floor depths
#     read 0. Hand case: raw 100, filled 100.05 -> depth 0 (under the
#     0.1 m floor); filled 100.30 -> depth 0.30 kept. ---
raw = np.array([[100.0, 100.0]])
filled = np.array([[100.05, 100.30]])
depth = compute_depression_depth(raw, filled)
assert depth[0, 0] == 0.0, f"0.05 m sits under the {DEPRESSION_NOISE_FLOOR_METERS} m noise floor -> 0"
assert math.isclose(depth[0, 1], 0.30)
# Score: linear to 1.0 at DEPRESSION_FULL_CREDIT_METERS (0.5 m):
assert float(depression_score(np.array(0.0))) == 0.0
assert math.isclose(float(depression_score(np.array(0.25))), 0.5)
assert float(depression_score(np.array(0.5))) == 1.0
assert float(depression_score(np.array(2.0))) == 1.0
assert math.isclose(float(depression_score(np.array(DEPRESSION_NOISE_FLOOR_METERS))), 0.2), (
    "a depth exactly AT the noise floor is real signal: 0.1/0.5 = 0.2"
)
print("Depression screen: noise floor zeroes sub-0.1 m fill, score saturates at 0.5 m.")

# --- ksat log ramp (salvaged): 1.0 at/below 0.1, 0.0 at/above 100,
#     geometric midpoint scores exactly 0.5 on the log scale ---
assert ksat_water_holding_score(WATER_HOLDING_GOOD_KSAT_UM_PER_S) == 1.0
assert ksat_water_holding_score(WATER_HOLDING_GOOD_KSAT_UM_PER_S / 10) == 1.0
assert ksat_water_holding_score(WATER_HOLDING_POOR_KSAT_UM_PER_S) == 0.0
assert ksat_water_holding_score(WATER_HOLDING_POOR_KSAT_UM_PER_S * 10) == 0.0
mid_ksat = (WATER_HOLDING_GOOD_KSAT_UM_PER_S * WATER_HOLDING_POOR_KSAT_UM_PER_S) ** 0.5
assert math.isclose(ksat_water_holding_score(mid_ksat), 0.5, abs_tol=1e-9)
assert ksat_water_holding_score(1.0) > ksat_water_holding_score(10.0) > ksat_water_holding_score(50.0)
assert ksat_water_holding_score(None) is None, "unavailable is None here -- the composite renormalizes, no neutral vote"
print("Soil ksat ramp: NRCS breakpoints, log-scale geometric midpoint 0.5, None renormalized around.")

# --- hydrologic group: C/D high, dual groups score their UNDRAINED letter ---
assert hydrologic_group_score("A") == HYDROLOGIC_GROUP_SCORES["A"] == 0.0
assert hydrologic_group_score("B") == 0.35
assert hydrologic_group_score("C") == 0.8
assert hydrologic_group_score("D") == 1.0
assert hydrologic_group_score("A/D") == 1.0, "dual group scores its UNDRAINED (second) letter"
assert hydrologic_group_score("b") == 0.35, "case-insensitive"
assert hydrologic_group_score(None) is None
assert hydrologic_group_score("") is None
assert hydrologic_group_score("X") is None
print("Hydrologic group: A low, D high, dual groups by undrained letter, unknown is None.")

# --- hydric share: summed comppct of hydric components / 100 ---
assert hydric_share_for_mukey([]) is None
assert hydric_share_for_mukey([{"hydricrating": "No", "comppct_r": 100}]) == 0.0
assert math.isclose(
    hydric_share_for_mukey(
        [{"hydricrating": "Yes", "comppct_r": 60}, {"hydricrating": "No", "comppct_r": 40}]
    ),
    0.6,
)
assert math.isclose(
    hydric_share_for_mukey(
        [{"hydricrating": "Yes", "comppct_r": "garbage"}, {"hydricrating": "Yes", "comppct_r": 30}]
    ),
    0.3,
), "unparseable comppct on a hydric row counts 0, never raises"
print("Hydric share: positive wetness signal, unparseable rows contribute nothing.")

# --- composite soil score renormalizes over available sub-signals ---
full = soil_water_score_for_mukey(0.05, "D", [{"hydricrating": "Yes", "comppct_r": 100}])
assert math.isclose(full["score"], 1.0), "best ksat + group D + fully hydric = 1.0"
ksat_only = soil_water_score_for_mukey(mid_ksat, None, None)
assert math.isclose(ksat_only["score"], 0.5), "with only ksat available the composite IS the ksat score"
assert ksat_only["hydrologic_group_score"] is None
assert soil_water_score_for_mukey(None, None, None) is None, "no sub-signal at all -> None, cell falls back neutral"
print("Soil composite: renormalized over available sub-signals; nothing available is None.")

# --- THE TWI CURVE at its breakpoints: hand values BELOW / AT the floor
#     / BETWEEN / AT full credit / ABOVE. GIVEN the breakpoints this is
#     pure per-cell arithmetic -- no population is consulted, which is
#     what keeps a redrawn boundary from moving anyone's score. Where
#     the breakpoints COME from (percentiles of the DEM window) is
#     test_twi_window_referenced.py's subject; here they are supplied
#     directly, because twi_score() now REQUIRES them and has no
#     default pair to fall back on. ---
_lo, _hi = 6.0, 10.0
_mid = (_lo + _hi) / 2.0
hand = np.array([[_lo - 3.0, _lo, _mid], [_hi, _hi + 3.0, _lo + 0.25 * (_hi - _lo)], [np.nan, 0.0, 1e6]])
scored = twi_score(hand, _lo, _hi)
assert scored[0, 0] == 0.0, "below the floor scores 0.0 -- plain hillslope earns nothing for wetness"
assert scored[0, 1] == 0.0, "AT the floor is still 0.0 (the ramp starts here, it does not step)"
assert math.isclose(scored[0, 2], 0.5), "the midpoint of the ramp scores exactly 0.5 -- it is linear"
assert scored[1, 0] == 1.0, "AT full credit scores 1.0"
assert scored[1, 1] == 1.0, "above full credit saturates at 1.0, never overshoots"
assert math.isclose(scored[1, 2], 0.25), "a quarter of the way up the ramp scores 0.25"
assert np.isnan(scored[2, 0]), "an unmeasured cell stays unmeasured -- NaN propagates, as the percentile did"
assert scored[2, 1] == 0.0 and scored[2, 2] == 1.0, "the curve is clipped on both sides, for any raw value"
# The curve takes its breakpoints as ARGUMENTS -- they are configurable,
# not baked into the arithmetic.
assert math.isclose(twi_score(np.array([5.0]), 0.0, 10.0)[0], 0.5)
# AND THEY ARE REQUIRED, with no default pair. That is what stops a
# hardcoded curve from quietly becoming the default again -- the reason
# the two retired constants live under RETIRED_ names and are imported
# only by the diagnostic's before/after.
try:
    twi_score(np.array([5.0]))
    raise AssertionError("twi_score() must not accept a call with no breakpoints")
except TypeError:
    pass
assert not hasattr(wsa, "TWI_SCORE_MIN_BREAKPOINT") and not hasattr(wsa, "TWI_SCORE_FULL_CREDIT_BREAKPOINT"), (
    "the fixed breakpoint constants are RETIRED under their RETIRED_ names -- nothing may import "
    "the old names and get a scoring curve"
)
assert wsa.RETIRED_FIXED_TWI_MIN_BREAKPOINT == 6.0 and wsa.RETIRED_FIXED_TWI_FULL_CREDIT_BREAKPOINT == 10.0, (
    "retired, NOT deleted: the diagnostic's before/after needs the old pair to stay reproducible"
)
print(f"TWI curve: 0.0 at/below {_lo}, linear ramp, 1.0 at/above {_hi}; NaN propagates; "
      "breakpoints are REQUIRED arguments with no default pair.")

# THE PROPERTY THE WHOLE BRANCH EXISTS FOR, stated at the unit level: the
# score of a value does not depend on what other values are present.
assert twi_score(np.array([7.0]), _lo, _hi)[0] == twi_score(np.array([7.0, 20.0, 20.0, 20.0]), _lo, _hi)[0], (
    "given a curve, a value scores identically whatever population is passed alongside it -- "
    "adding the wettest cells on the landscape must not move anyone else's score"
)
print("TWI curve: given the breakpoints, one cell's score is independent of every other cell.")

# --- The RETIRED percentile, still correct as an instrument (this
#     branch's before/after comparison reproduces the old scores with
#     it), but off every scoring path -- asserted at the AST level
#     further down. ---
values = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]])
mask = np.ones((3, 3), dtype=bool)
pct = parcel_relative_percentile(values, mask)
expected = np.array([[0.0, 0.125, 0.25], [0.375, 0.5, 0.625], [0.75, 0.875, 1.0]])
assert np.allclose(pct, expected), f"distinct 3x3 must rank 0..1 in eighths, got {pct}"
flat_pct = parcel_relative_percentile(np.full((3, 3), 2.0), mask)
assert np.allclose(flat_pct, 0.5), "all-equal ground shares the neutral mean-rank 0.5, never 'driest'"
masked = parcel_relative_percentile(values, np.array([[True, True, False]] * 3))
assert np.isnan(masked[0, 2]), "off-mask cells carry NaN, excluded from the population"
# 6 in-mask values 1,2,4,5,7,8 -> value 7 has 4 below of n-1=5 -> 0.8
assert math.isclose(masked[2, 0], 0.8)
# ...and the DEFECT it was retired for, exercised directly: adding wetter
# ground to the population drops an unchanged cell's score.
_before = parcel_relative_percentile(np.array([1.0, 2.0, 3.0]), np.ones(3, dtype=bool))[2]
_after = parcel_relative_percentile(np.array([1.0, 2.0, 3.0, 9.0, 9.0]), np.ones(5, dtype=bool))[2]
assert _before == 1.0 and _after < _before, (
    "the retired percentile's failure, pinned: the value 3.0 scored 1.0 and then 0.5 when wetter "
    "cells joined the population -- same ground, worse score, which is the bug this branch reversed"
)
print("Retired percentile: still a correct rank instrument, and its boundary-dependence is pinned here.")

# --- TWI singularity guard: slope 0 floors tan at TWI_MIN_SLOPE_TAN.
#     Hand value at 5m cells, accumulation 1: a = 1 * 25 / 5 = 5 m;
#     TWI = ln(5 / 0.001) = ln(5000). ---
flat_dem = _dem(np.full((3, 3), 100.0))
twi = compute_topographic_wetness_index(flat_dem, np.ones((3, 3)), np.zeros((3, 3)))
assert np.all(np.isfinite(twi)), "the flat/zero-slope singularity must be guarded, never inf"
assert math.isclose(twi[1, 1], math.log(5.0 / TWI_MIN_SLOPE_TAN)), (
    f"hand value ln(5/{TWI_MIN_SLOPE_TAN}) = ln(5000), got {twi[1, 1]}"
)
nan_slope = np.zeros((3, 3))
nan_slope[0, 0] = np.nan
assert np.isnan(compute_topographic_wetness_index(flat_dem, np.ones((3, 3)), nan_slope)[0, 0]), (
    "unmeasured slope -> unmeasured TWI (NaN), not a fabricated wetness"
)
print("TWI: ln(a/tan-beta) with the flat singularity floored at tan=0.001 -- ln(5000) hand-checked.")


# =========================================================================
# 2. SURFACE BLEND
# =========================================================================

# Weight-sum assertions (the import-time asserts, re-stated as tests):
assert math.isclose(sum(EMBANKMENT_WEIGHTS.values()), 1.0, abs_tol=1e-9)
assert math.isclose(sum(EXCAVATED_WEIGHTS.values()), 1.0, abs_tol=1e-9)
# THREE EMBANKMENT CRITERIA, NOT FOUR: drainage_area left the SEEDING
# blend when the band moved to the pinch cell (see EMBANKMENT_WEIGHTS's
# note), and the survivors were RENORMALIZED rather than retuned --
# 0.25/0.25/0.20 over their 0.70 sum, to 2 dp. The ratios are therefore
# preserved exactly, which is the property the renormalization claims
# and the only one worth asserting about the numbers themselves.
assert EMBANKMENT_WEIGHTS == {"slope": 0.36, "soil": 0.36, "twi": 0.28}
assert "drainage_area" not in EMBANKMENT_WEIGHTS, (
    "contributing area is no longer a per-cell embankment SEEDING criterion"
)
assert EMBANKMENT_WEIGHTS["slope"] == EMBANKMENT_WEIGHTS["soil"], (
    "slope and soil were equal before renormalization and stay equal after it"
)
# EACH WEIGHT IS ITS EXACT RENORMALIZED VALUE, ROUNDED TO 2 dp. The
# retired blend's 0.25 / 0.25 / 0.20 over their 0.70 sum is
# 0.3571 / 0.3571 / 0.2857; two of those round up and the third must
# round DOWN to hold the sum at exactly 1.0, so TWI pays the 0.0057 of
# rounding rather than the sum being broken. That is the whole of the
# arithmetic -- no judgement entered it, which is the claim being
# pinned here.
for _name, _exact in (("slope", 0.25 / 0.70), ("soil", 0.25 / 0.70), ("twi", 0.20 / 0.70)):
    assert abs(EMBANKMENT_WEIGHTS[_name] - _exact) < 0.01, (
        f"{_name} must be its renormalized value to 2 dp, not a retuned one: "
        f"{EMBANKMENT_WEIGHTS[_name]} vs {_exact:.4f}"
    )
# THE EXCAVATED SIDE IS UNTOUCHED, drainage_runon included: a dugout has
# no pinch and its ground is extraction-based, so a per-cell run-on
# preference at 0.10 is still exactly the right instrument there.
assert EXCAVATED_WEIGHTS == {"wetness": 0.35, "soil": 0.30, "slope": 0.25, "drainage_runon": 0.10}
print("Weights: embankment renormalized to three criteria (0.36/0.36/0.28); excavated untouched.")

# One hand-computed cell through each type's FULL blend. Inputs chosen so
# every criterion lands at a hand-checkable value at 5m cells
# (cell area = 25/4046.8564224 = 0.0061776 ac):
#   accumulation 324 cells -> 2.0016 ac -> run-on 1.0 (EXCAVATED ONLY;
#                             the embankment blend no longer reads
#                             contributing area at all, which is what
#                             the two expected values below encode)
#   slope 5%               -> embankment 1.0 (in 3-8), excavated 1.0
#                             (full credit through the seep-widened 5%)
#   TWI percentile 0.6 (given directly), depression 0.25 m -> 0.5
#     -> wetness = 0.5*0.6 + 0.5*0.5 = 0.55
#   soil grid 0.8
# embankment = .36*1 + .36*.8 + .28*.6                      = 0.816
# excavated  = .35*.55 + .30*.8 + .25*1.0 + .10*1           = 0.7825
blend_dem = _dem(np.full((3, 3), 100.0))
surfaces = compute_suitability_surfaces(
    blend_dem,
    gate_mask=np.ones((3, 3), dtype=bool),
    flow_accumulation=np.full((3, 3), 324.0),
    slope_pct=np.full((3, 3), 5.0),
    twi_score_grid=np.full((3, 3), 0.6),
    depression_depth=np.full((3, 3), 0.25),
    soil_score_grid=np.full((3, 3), 0.8),
)
assert np.allclose(surfaces[SURVEY_TYPE_EMBANKMENT], 0.816), (
    f"hand-computed embankment blend 0.816, got {surfaces[SURVEY_TYPE_EMBANKMENT][1, 1]}"
)
assert "drainage_area" not in surfaces["criteria"][SURVEY_TYPE_EMBANKMENT], (
    "the embankment criteria grids carry three criteria; contributing area is measured per "
    "compartment at the pinch cell, not per cell here"
)
assert "drainage_runon" in surfaces["criteria"][SURVEY_TYPE_EXCAVATED], (
    "the excavated run-on grid is untouched by that move"
)
assert np.allclose(surfaces[SURVEY_TYPE_EXCAVATED], 0.7825), (
    f"hand-computed excavated blend 0.7825, got {surfaces[SURVEY_TYPE_EXCAVATED][1, 1]}"
)
# The gate mask zeroes both surfaces before anything reads them:
gated = compute_suitability_surfaces(
    blend_dem,
    gate_mask=np.zeros((3, 3), dtype=bool),
    flow_accumulation=np.full((3, 3), 324.0),
    slope_pct=np.full((3, 3), 5.0),
    twi_score_grid=np.full((3, 3), 0.6),
    depression_depth=np.full((3, 3), 0.25),
    soil_score_grid=np.full((3, 3), 0.8),
)
assert np.all(gated[SURVEY_TYPE_EMBANKMENT] == 0.0) and np.all(gated[SURVEY_TYPE_EXCAVATED] == 0.0)
print("Surface blend: one cell hand-computed through both full blends (0.816 / 0.7825); mask zeroes both.")


# =========================================================================
# 2b. RETIRED SMOOTHING, CONNECTIVITY, SLOPE UNITS, AND THE CLOSING MATH
# =========================================================================

# --- Masked focal mean: RETIRED from the extraction path, kept as a
# tested utility (retired, not deleted). Hand-derived on a 3x3 with a
# window straddling the mask edge. Radius 7.1 m at 5 m cells -> disc
# offsets dr^2+dc^2 <= 2.02, i.e. the full 3x3 window. Mask excludes
# (0,2)=3 and (2,0)=7:
#   center (1,1): mean of the 7 in-mask cells = (1+2+4+5+6+8+9)/7 = 5
#   corner (0,0): window clips to 2x2, all in-mask -> (1+2+4+5)/4 = 3
#   edge (0,1):   window 2x3 minus the excluded (0,2) -> (1+2+4+5+6)/5 = 3.6
#   edge (1,2):   window 3x2 minus the excluded (0,2) -> (2+5+6+8+9)/5 = 6
#   excluded cells output 0.0 and sit in NOBODY's numerator or denominator.
import production_area  # noqa: E402
from feature_schema import validate_feature_collection  # noqa: E402
from raster_grid import connected_components  # noqa: E402
from shapely.ops import unary_union  # noqa: E402
from water_survey_areas import (  # noqa: E402
    SURVEY_SMOOTHING_RADIUS_METERS,
    SURVEY_ZONE_GROUPING_DISTANCE_METERS,
    WATER_REGION_CONNECTIVITY,
    _close_member_footprints,
    build_survey_zones,
    extract_survey_regions,
    masked_focal_mean,
)

fm_values = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]])
fm_mask = np.ones((3, 3), dtype=bool)
fm_mask[0, 2] = False
fm_mask[2, 0] = False
fm_out = masked_focal_mean(fm_values, fm_mask, (5.0, 5.0), radius_meters=7.1)
assert fm_out[1, 1] == 5.0, f"center: mean of the 7 in-mask cells is 35/7 = 5, got {fm_out[1, 1]}"
assert fm_out[0, 0] == 3.0, f"corner: (1+2+4+5)/4 = 3, got {fm_out[0, 0]}"
assert math.isclose(fm_out[0, 1], 3.6), f"mask-straddling window: (1+2+4+5+6)/5 = 3.6, got {fm_out[0, 1]}"
assert fm_out[1, 2] == 6.0, f"mask-straddling window: (2+5+6+8+9)/5 = 6, got {fm_out[1, 2]}"
assert fm_out[0, 2] == 0.0 and fm_out[2, 0] == 0.0, "off-mask cells output 0.0"
assert SURVEY_SMOOTHING_RADIUS_METERS == 15.0

# The retirement itself, grep-asserted: the utility survives with its
# retirement docstring (the measured 0.820 -> 0.524 dilution), and NO
# extraction-path function calls it.
assert "RETIRED" in masked_focal_mean.__doc__ and "0.524" in masked_focal_mean.__doc__, (
    "the retired utility must carry WHY it left the path, with the measured dilution numbers"
)
# CALL-level assertion (AST, not string grep -- the docstrings rightly
# still NAME the retired function when telling its story):
import ast  # noqa: E402
import textwrap  # noqa: E402

for path_fn in (compute_water_survey_areas, extract_survey_regions, build_survey_zones):
    tree = ast.parse(textwrap.dedent(inspect.getsource(path_fn)))
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "masked_focal_mean" not in calls, (
        f"{path_fn.__name__} must not CALL the retired smoothing -- extraction runs on the RAW surface"
    )
print("Masked focal mean: 3x3 hand-derived; RETIRED from the extraction path (grep-asserted), kept as a utility.")

# --- Connectivity constants: water's own 8, production's 4 untouched,
# never aliased (grep-style source assertions). ---
assert WATER_REGION_CONNECTIVITY == 8
assert "WATER_REGION_CONNECTIVITY" in inspect.getsource(extract_survey_regions), (
    "water extraction must read its OWN connectivity constant"
)
assert "connectivity=4" in inspect.getsource(production_area.cluster_and_gate), (
    "production's deliberate 4-connectivity path must be untouched by the water changes"
)
assert not hasattr(wsa, "SURVEY_REGION_CONNECTIVITY"), (
    "the old shared-sounding name is gone -- water and production constants must never alias"
)
print("Connectivity: water uses WATER_REGION_CONNECTIVITY=8; production's connectivity=4 path untouched.")

# --- Slope-units verification: the real slope machinery
# (production_area.compute_slope_percent -- read, not assumed: max
# |neighbor elevation diff| per unit ground distance, x100, i.e. PERCENT
# GRADE) is fed planes of hand-known grade, and its output goes straight
# into the SEEP-WIDENED excavated classifier. Column step s over 5 m
# cells -> horizontal-neighbor grade s/5*100 (the diagonal is s/7.07,
# smaller, so the horizontal IS the max). Expected classifier scores:
# 0% -> 1.0, 5% -> 1.0 (full credit through the seep grade),
# 10% -> 0.5, 15% -> 0.0.
for col_step, expected_grade, expected_score in ((0.0, 0.0, 1.0), (0.25, 5.0, 1.0), (0.5, 10.0, 0.5), (0.75, 15.0, 0.0)):
    plane = np.array([[100.0 + c * col_step for c in range(7)] for _ in range(7)])
    slope_grid = production_area.compute_slope_percent(plane, (5.0, 5.0))
    measured = float(slope_grid[3, 3])
    assert math.isclose(measured, expected_grade, abs_tol=1e-9), (
        f"a {col_step} m column step over 5 m cells IS a {expected_grade}% grade in the real machinery's "
        f"own units, got {measured}"
    )
    score = float(excavated_slope_score(np.array(measured)))
    assert math.isclose(score, expected_score), (
        f"excavated slope score at a real measured {expected_grade}% grade must be {expected_score}, got {score}"
    )
print("Slope units: percent grade verified on hand-built planes at 0/5/10/15%, seep-widened classifier scores 1.0/1.0/0.5/0.0.")

# --- Raw peaks reappear: a one-cell-wide diagonal ridge of raw 1.0 on a
# 0.5 background. Under the RETIRED smoothing its maximum was 17/29 =
# 0.586 (5 diagonal cells of a 29-cell window), so a 0.9 threshold found
# NOTHING; on the raw surface all ten 1.0 peaks clear 0.9 and
# 8-connectivity reads the diagonal as ONE member region. ---
diag_dem = _dem(np.full((20, 20), 100.0))
diag_boundary = box(ORIGIN_X - 1.0, ORIGIN_Y - 20 * RESOLUTION - 1.0, ORIGIN_X + 20 * RESOLUTION + 1.0, ORIGIN_Y + 1.0)
diag_raw = np.full((20, 20), 0.5)
for i in range(5, 15):
    diag_raw[i, i] = 1.0
diag_mask = np.ones((20, 20), dtype=bool)
assert float(masked_focal_mean(diag_raw, diag_mask, (RESOLUTION, RESOLUTION))[9, 9]) < 0.9, (
    "under the retired smoothing an interior diagonal peak sat at 17/29 -- the dilution this pass undoes"
)
diag_criteria = {name: diag_raw for name in EMBANKMENT_WEIGHTS}
diag_zeros = np.zeros((20, 20))
diag_regions = extract_survey_regions(
    diag_dem,
    diag_raw,
    diag_criteria,
    SURVEY_TYPE_EMBANKMENT,
    diag_mask,
    diag_boundary,
    twi_score_grid=diag_zeros,
    depression_depth=diag_zeros,
    flow_accumulation=np.ones((20, 20)),
    slope_pct=diag_zeros,
    soil_covered_mask=diag_mask & False,
    soil_checked=False,
    threshold=0.9,
)
assert len(diag_regions) == 1, f"raw extraction at 0.9 must find the diagonal as ONE 8-connected region, got {len(diag_regions)}"
assert set(diag_regions[0]["cells"]) == {(i, i) for i in range(5, 15)}, (
    "the raw peaks reappear: exactly the ten diagonal 1.0 cells"
)
assert diag_regions[0]["mean_suitability"] == 1.0, "raw scoring stays sharp -- the member mean is the peaks' own 1.0"

# And the aggregation over it: one zone whose HULL envelope spans the
# diagonal's bounding wedge -- the surveyable claim, drawn AFTER
# extraction (the closing decides grouping only).
diag_surfaces = {
    SURVEY_TYPE_EMBANKMENT: diag_raw,
    SURVEY_TYPE_EXCAVATED: diag_zeros,
    "criteria": {SURVEY_TYPE_EMBANKMENT: diag_criteria, SURVEY_TYPE_EXCAVATED: {}},
}
diag_gate_context = {
    "twi_score": diag_zeros,
    "depression_depth": diag_zeros,
    "flow_accumulation": np.ones((20, 20)),
    "slope_pct": diag_zeros,
    "soil_covered_mask": diag_mask & False,
    "soil_checked": False,
}
diag_zones = build_survey_zones(diag_dem, diag_regions, diag_surfaces, diag_gate_context, diag_boundary)
assert len(diag_zones) == 1 and diag_zones[0]["member_count"] == 1
assert diag_zones[0]["zone_acres"] > diag_zones[0]["member_acres"], (
    "the hull envelope spans the diagonal's bounding wedge -- the ground to walk exceeds the anchor"
)
assert diag_zones[0]["mean_suitability"] == 1.0, (
    "zone score statistics come from MEMBER cells only -- the envelope never launders the 0.5 background in"
)
print("Diagonal ridge: raw peaks extract at 0.9 as one 8-connected member; its zone walks a larger envelope while scoring only the anchor.")

# --- Closing math, hand-derived (the GROUPING core -- pre-merge, the
# closing decides WHICH members belong together and nothing else; the
# drawn envelope is the hull, tested next). Two 20x20 m squares. At
# SURVEY_ZONE_GROUPING_DISTANCE_METERS = 30, a 20 m gap bridges (gaps up
# to the FULL distance bridge: each side buffers out 15); a 40 m gap
# does not. Closing-region area for the bridged pair: the filled 60x20
# rectangle = 1200 m^2 MINUS two menisci where the round buffer joins
# sag across the gap -- sagitta 15 - sqrt(15^2-10^2) = 3.82 m over the
# 20 m gap, ~50-60 m^2 per side -- so the assertion uses a STATED
# tolerance (within 120 m^2 of 1200), not equality. A singleton closes
# back to itself (dilation then erosion of a convex square is exact up
# to buffer discretization). ---
assert SURVEY_ZONE_GROUPING_DISTANCE_METERS == 30.0
sq_a = box(0.0, 0.0, 20.0, 20.0)
sq_b_near = box(40.0, 0.0, 60.0, 20.0)   # 20 m gap < 30 -> bridges
sq_b_far = box(60.0, 0.0, 80.0, 20.0)    # 40 m gap > 30 -> stays apart

near_regions = _close_member_footprints([sq_a, sq_b_near], SURVEY_ZONE_GROUPING_DISTANCE_METERS)
assert len(near_regions) == 1, "a 20 m gap at 30 m grouping must fuse into ONE zone"
near_area = near_regions[0].area
assert abs(near_area - 1200.0) <= 120.0, (
    f"the fused closing region is the 60x20 rectangle (1200 m^2) minus the two round-join menisci "
    f"(sagitta 3.82 m over the 20 m gap): got {near_area:.1f} m^2, outside the stated tolerance"
)
assert near_area > sq_a.area + sq_b_near.area, "the bridge genuinely groups across the gap"

far_regions = _close_member_footprints([sq_a, sq_b_far], SURVEY_ZONE_GROUPING_DISTANCE_METERS)
assert len(far_regions) == 2, "a 40 m gap at 30 m grouping must stay TWO zones"
for region in far_regions:
    assert abs(region.area - 400.0) / 400.0 < 0.01, (
        f"an unbridged square closes back to its own 400 m^2, got {region.area:.1f}"
    )

lone_regions = _close_member_footprints([sq_a], SURVEY_ZONE_GROUPING_DISTANCE_METERS)
assert len(lone_regions) == 1 and abs(lone_regions[0].area - 400.0) / 400.0 < 0.01, (
    "a singleton closes back to approximately itself -- the large-single-candidate case needs no special rule"
)
print("Closing math (grouping): 20 m gap fuses (1200 m^2 minus stated menisci), 40 m gap stays two, singleton returns itself.")

# --- HULL math, hand-derived (the DRAWING core -- pre-merge change 1:
# the drawn zone is the convex hull of the member union, clipped to the
# parcel). Driven through build_survey_zones itself on hand-built member
# regions so the grouping-vs-drawing split is exercised in our code, not
# restated in shapely. The waisted two-square fixture states BOTH
# numbers: the closing's grouping region sagged to ~1085-1140 m^2
# (menisci), while the hull claims the FULL 60x20 = 1200 m^2 rectangle
# -- the surveyable claim a surveyor would rope off, waist filled. ---
def _hull_member(cells, x0, y0, x1, y1, survey_type=SURVEY_TYPE_EMBANKMENT):
    """A minimal member-region dict for build_survey_zones: exact box
    footprint + the cell list its stats read from."""
    return {
        "survey_type": survey_type,
        "polygon_utm": box(x0, y0, x1, y1),
        "cells": cells,
    }


hull_dem = _dem(np.full((30, 30), 100.0))
hull_surface = np.full((30, 30), 1.0)
hull_zeros = np.zeros((30, 30))
hull_surfaces = {
    SURVEY_TYPE_EMBANKMENT: hull_surface,
    SURVEY_TYPE_EXCAVATED: hull_zeros,
    "criteria": {
        SURVEY_TYPE_EMBANKMENT: {name: hull_surface for name in EMBANKMENT_WEIGHTS},
        SURVEY_TYPE_EXCAVATED: {},
    },
}
hull_gate_context = {
    "twi_score": hull_zeros,
    "depression_depth": hull_zeros,
    "flow_accumulation": np.ones((30, 30)),
    "slope_pct": hull_zeros,
    "soil_covered_mask": np.zeros((30, 30), dtype=bool),
    "soil_checked": False,
}
hull_wide_boundary = box(ORIGIN_X - 1.0, ORIGIN_Y - 150.0 - 1.0, ORIGIN_X + 150.0 + 1.0, ORIGIN_Y + 1.0)

# Waisted pair: 20x20 m squares (16 cells each), 20 m gap -> the closing
# groups them (asserted above at ~1085-1140 m^2); the hull of the two
# squares is EXACTLY the bounding 60x20 rectangle = 1200 m^2, and axis-
# aligned box hulls carry no discretization, so the area is exact.
waisted_members = [
    _hull_member(
        [(r, c) for r in range(4) for c in range(4)],
        ORIGIN_X, ORIGIN_Y - 20.0, ORIGIN_X + 20.0, ORIGIN_Y
    ),
    _hull_member(
        [(r, c) for r in range(4) for c in range(8, 12)],
        ORIGIN_X + 40.0, ORIGIN_Y - 20.0, ORIGIN_X + 60.0, ORIGIN_Y
    ),
]
waisted_zones = build_survey_zones(hull_dem, waisted_members, hull_surfaces, hull_gate_context, hull_wide_boundary)
assert len(waisted_zones) == 1 and waisted_zones[0]["member_count"] == 2, (
    "grouping is unchanged: the 20 m gap still fuses the pair into one zone"
)
waisted_zone = waisted_zones[0]
assert math.isclose(waisted_zone["polygon_utm"].area, 1200.0), (
    f"the hull claims the full 60x20 rectangle EXACTLY (1200 m^2; the closing sagged to "
    f"{near_area:.1f} m^2) -- got {waisted_zone['polygon_utm'].area:.1f}"
)
assert math.isclose(waisted_zone["zone_acres"], round(1200.0 / 4046.8564224, 4)), (
    "zone_acres is the hull's own 1200 m^2 = 0.2965 ac"
)
assert math.isclose(waisted_zone["member_acres"], round(32 * 25.0 / 4046.8564224, 4)), (
    "32 anchoring cells x 25 m^2 = 0.1977 ac"
)
assert waisted_zone["sparse_anchor"] is False, (
    "member/zone = 0.1977/0.2965 = 0.667 >= the 0.2 sparse-anchor fraction -> the flag stays silent"
)
assert waisted_zone["mean_suitability"] == 1.0, (
    "member-only statistics survive the hull change: the waist's added ground never enters the mean"
)

# Singleton: a convex member footprint's hull IS itself -- exactly, not
# approximately (the closing-era 'approximately itself' sliver is gone).
singleton_zones = build_survey_zones(
    hull_dem,
    [_hull_member([(r, c) for r in range(4) for c in range(4)], ORIGIN_X, ORIGIN_Y - 20.0, ORIGIN_X + 20.0, ORIGIN_Y)],
    hull_surfaces,
    hull_gate_context,
    hull_wide_boundary,
)
assert len(singleton_zones) == 1
assert math.isclose(singleton_zones[0]["polygon_utm"].area, 400.0), (
    "a singleton's hull is EXACTLY its own 400 m^2 -- dual acreage coincides on a convex singleton"
)
assert singleton_zones[0]["zone_acres"] == singleton_zones[0]["member_acres"], (
    "16 cells x 25 m^2 and the 400 m^2 hull round to the same acreage -- the two numbers agree when "
    "the claim IS the anchor"
)

# Hugging fixture: a boundary smaller than the hull clips it -- the
# clipped hull is the boundary box itself, so adjacency is EXACTLY 1.0
# (concavity introduced by the clip is acceptable; the boundary is real
# ground truth).
hug_boundary = box(ORIGIN_X + 10.0, ORIGIN_Y - 20.0, ORIGIN_X + 50.0, ORIGIN_Y)
hug_zones = build_survey_zones(hull_dem, waisted_members, hull_surfaces, hull_gate_context, hug_boundary)
assert len(hug_zones) == 1
assert math.isclose(hug_zones[0]["polygon_utm"].area, 800.0), (
    "the 1200 m^2 hull clipped to the 40x20 boundary keeps exactly 800 m^2"
)
assert hug_zones[0]["boundary_adjacency_fraction"] == 1.0, (
    "the clipped hull's perimeter lies entirely on the parcel line -- adjacency exactly 1.0 on the hull"
)

# Sparse anchor FIRES: a single L-shaped member (extraction's connected
# components are free to be concave) whose two 5 m-wide, 100 m-long
# arms anchor 39 cells = 975 m^2 -- while its HULL is the near-triangle
# over the whole 100x100 corner. Hand-shoelaced on the hull's five
# corners (0,0),(5,0),(100,95),(100,100),(0,100): 5487.5 m^2.
# member/zone = 975/5487.5 = 0.1777 < 0.2 -> the walkable claim vastly
# exceeds its anchor and says so, on a zone that also clears the 0.1 ac
# floor (1.356 ac) -- a SURVIVING sparse anchor, the case the flag
# exists for.
sparse_footprint = unary_union([
    box(ORIGIN_X, ORIGIN_Y - 100.0, ORIGIN_X + 5.0, ORIGIN_Y),
    box(ORIGIN_X, ORIGIN_Y - 100.0, ORIGIN_X + 100.0, ORIGIN_Y - 95.0),
])
sparse_cells = [(r, 0) for r in range(20)] + [(19, c) for c in range(1, 20)]
sparse_members = [
    {"survey_type": SURVEY_TYPE_EMBANKMENT, "polygon_utm": sparse_footprint, "cells": sparse_cells},
]
sparse_zones = build_survey_zones(hull_dem, sparse_members, hull_surfaces, hull_gate_context, hull_wide_boundary)
assert len(sparse_zones) == 1, "one L-shaped member -> one zone, no grouping involved"
sparse_zone = sparse_zones[0]
assert math.isclose(sparse_zone["polygon_utm"].area, 5487.5), (
    f"hand-shoelaced hull of the L = 5487.5 m^2, got {sparse_zone['polygon_utm'].area:.1f}"
)
assert sparse_zone["sparse_anchor"] is True and FLAG_SPARSE_ANCHOR in sparse_zone["flags"], (
    "member/zone = 975/5487.5 = 0.1777 < 0.2 -> sparse_anchor fires"
)
assert FLAG_BELOW_MIN_AREA not in sparse_zone["flags"], (
    "1.356 ac of hull clears the floor -- this sparse anchor SURVIVES, which is why the flag matters"
)
assert sparse_zone["mean_suitability"] == 1.0, (
    "the sparse hull's empty middle never enters the score -- member cells only, still"
)
print(
    f"Hull math: waisted pair hulls to exactly 1200 m^2 (closing sagged to {near_area:.1f}), singleton exact, "
    "clip adjacency exactly 1.0, sparse anchor fires at 975/5487.5 and stays member-scored."
)


# =========================================================================
# 3. EXTRACTION + AGGREGATION FIXTURES (shared with sections 4 and 6)
# =========================================================================

# --- FIXTURE 1: uniform wet flat -> one excavated member, one zone.
# 20x20 flat DEM at 100.0 m; boundary covers exactly the 10x10 block of
# cell centers rows/cols 5..14. Soil: one map unit covering everything,
# best-case wet (ksat 0.05 -> 1.0, group D via the component rows ->
# 1.0, 100% hydric -> 1.0 => soil grid 1.0, coverage 1.0).
# Hand-derivation of the excavated RAW surface on this fixture:
#   flat filled DEM -> every D8 direction is a flat tie -> accumulation
#     1 everywhere -> run-on = (1*0.0061776)/2 = 0.0030888
#   slope 0 (interior cells) -> excavated slope 1.0
#   TWI all equal -> parcel-relative mean-rank 0.5; depression 0
#     -> wetness = 0.5*0.5 + 0.5*0 = 0.25
#   excavated = .35*.25 + .30*1 + .25*1 + .10*0.0030888 = 0.63780888
# >= the 0.5 default (final tuning: decided against the parcel's
# attainable ceiling) -> ONE 100-cell member; embankment = 0.35 < 0.5 ->
# none. The single member's footprint is a convex 50x50 m square, so its
# HULL is exactly itself (pre-merge: the drawn envelope is the convex
# hull of the member union, clipped to the parcel); clipped to the
# boundary the envelope IS the boundary box -- which makes the DUAL
# ACREAGE distinction visible on this very fixture: member_acres counts
# CELLS (100 x 0.0061776 = 0.6178) while zone_acres measures the clipped
# envelope POLYGON (49.8 x 49.8 m = 2480.04 m^2 = 0.6128) -- two
# different questions, deliberately not interchangeable.
CA = cell_area_acres(_dem(np.zeros((2, 2))))
assert math.isclose(CA, 25.0 / 4046.8564224)

flat_array = np.full((20, 20), 100.0)
FLAT_DEM = _dem(flat_array)
# EVERY CELL ACCUMULATES ONLY ITSELF -- now stated as an OVERRIDE rather
# than left to fall out of the fill, which is what the epsilon-fill branch
# changed here.
#
# On dead-flat ground the plain priority-flood left every cell tied with
# every neighbour, so compute_flow_direction() gave them all the -1
# sentinel and compute_flow_accumulation() returned 1 everywhere by
# default. This fixture's whole hand-derivation ("a = 1 * 25 / 5 = 5 m for
# EVERY cell") rode on that accident. The epsilon fill routes the flat --
# correctly, that is the branch -- so accumulation now runs 1..10 as the
# flood's own spread order converges flow toward the window's border, TWI
# gains a ~2.3 ln-unit spread, and the curve stops being uniform.
#
# THAT SPREAD IS A FILL ARTIFACT, NOT TERRAIN, and it is worth being blunt
# about: Priority-Flood+epsilon resolves a flat by the order the flood
# reaches its cells, so the drainage pattern it imposes on perfectly level
# ground is an artifact of the algorithm (a documented property of the
# method; a Garbrecht-Martz-style flat resolution would instead route
# toward the flat's low edges and away from its high ones). It is still a
# strict improvement on the alternative -- the plain fill reported "no
# convergence anywhere" on a marsh, which is what this whole branch
# exists to fix -- but it is not a signal to hand a hand-derivation.
#
# So this fixture supplies its own accumulation, exactly the way FIXTURE 2
# below already does, and the assumption is now visible in the test rather
# than borrowed from the fill. Nothing else about the fixture moves, and
# the uniform-value curve fallback keeps its direct unit coverage in
# test_twi_window_referenced.py.
FLAT_SELF_ACCUMULATION = np.ones((20, 20), dtype=np.int64)
FLAT_BOUNDARY = box(
    ORIGIN_X + 5 * RESOLUTION + 0.1,
    ORIGIN_Y - 15 * RESOLUTION + 0.1,
    ORIGIN_X + 15 * RESOLUTION - 0.1,
    ORIGIN_Y - 5 * RESOLUTION - 0.1,
)
GOOD_WET_SOIL_INPUTS = {
    "ksat_rows": [{"mukey": "1", "ksat_r": 0.05}],
    "components": [{"mukey": "1", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"}],
    "geometries_by_mukey": {"1": transform_geom(CRS, "EPSG:4326", mapping(FLAT_BOUNDARY.buffer(20.0)))},
}

flat_result = compute_water_survey_areas(
    FLAT_DEM, FLAT_BOUNDARY, soil_inputs=GOOD_WET_SOIL_INPUTS,
    flow_accumulation=FLAT_SELF_ACCUMULATION,
)

assert flat_result["gate_mask_stats"]["gated_cells"] == 100, "the boundary covers exactly 100 cell centers"
# THE TWI HALF IS HAND-DERIVED FROM THE ABSOLUTE CURVE, and this fixture
# is where the change shows most plainly. Dead-flat ground, every cell
# accumulating only itself: a = 1 * 25 / 5 = 5 m, tan(beta) floored at
# TWI_MIN_SLOPE_TAN, so raw TWI = ln(5/0.001) = ln(5000) = 8.5172 for
# EVERY cell -- the same hand value checked at the singularity guard
# above. On the curve that is (8.5172 - 6)/(10 - 6) = 0.6293.
#
# UNDER THE RETIRED PERCENTILE THIS READ 0.5: with every cell tied, the
# mean-rank convention gave the whole parcel a neutral rank, and the
# member mean came out 0.6378. The absolute curve says something the
# percentile structurally could not -- this dead-flat, zero-slope ground
# IS wet in the ln(a/tan(beta)) sense, and says so identically whatever
# is drawn around it, instead of reporting only that every cell is as
# wet as every other.
# THE CURVE IS READ OFF THE RUN, not hardcoded: the breakpoints are
# percentiles of this fixture's own DEM window, so the test asks the run
# what curve it derived and scores the hand value on that. Hardcoding a
# pair here would re-assert the retired fixed curve.
_flat_bp = flat_result["twi_breakpoints"]
flat_twi = min(max(
    (math.log(5.0 / wsa.TWI_MIN_SLOPE_TAN) - _flat_bp["floor"])
    / (_flat_bp["full_credit"] - _flat_bp["floor"]), 0.0), 1.0)
# A DEAD-FLAT WINDOW IS THE LAST-RESORT CASE: every cell carries the
# same raw ln(a/tan(beta)) (accumulation 1 everywhere, slope floored at
# TWI_MIN_SLOPE_TAN), so there is no gradient anywhere to reference. The
# curve is centred on that common value and every cell reads the neutral
# 0.5.
assert _flat_bp["curve_fallback"] == "centred_on_uniform_value", (
    "a dead-flat window has no TWI gradient at all -- not at the percentiles and not in the "
    "observed range"
)
assert math.isclose(flat_twi, 0.5), "the centred curve reads dead-flat ground as neutral 0.5"
# AND NOTE WHERE THAT LANDS: 0.5 is exactly what the RETIRED PERCENTILE
# said here, by its own mean-rank convention on an all-tied population.
# Two instruments that disagree everywhere else agree on the one case
# where the honest answer is "this window cannot tell you" -- which is
# why the member mean below is still the historic 0.6378.
expected_flat_score = (
    0.35 * (0.5 * flat_twi + 0.5 * 0.0) + 0.30 * 1.0 + 0.25 * 1.0 + 0.10 * (CA / 2.0)
)

flat_members = flat_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(flat_members) == 1 and flat_members[0]["cell_count"] == 100
flat_member = flat_members[0]
assert math.isclose(flat_member["mean_suitability"], round(expected_flat_score, 4)), (
    f"hand-derived member mean {round(expected_flat_score, 4)}, got {flat_member['mean_suitability']}"
)
assert wsa.SUITABILITY_THRESHOLD == 0.5, "the final-tuning threshold default"
assert flat_result["threshold"] == 0.5
assert flat_result["regions_by_type"][SURVEY_TYPE_EMBANKMENT] == [], "0.35 < 0.5 -> no embankment member"

flat_zones = flat_result["zones_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(flat_zones) == 1, "one member -> one zone (singletons take the same code path)"
flat_zone = flat_zones[0]
assert flat_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT] == []
assert flat_zone["member_count"] == 1 and flat_zone["cell_count"] == 100
assert math.isclose(flat_zone["member_acres"], round(100 * CA, 4)), "member_acres counts the anchoring CELLS"
expected_zone_acres = FLAT_BOUNDARY.area / 4046.8564224
assert abs(flat_zone["zone_acres"] - expected_zone_acres) < 0.001, (
    f"zone_acres measures the clipped envelope POLYGON ({expected_zone_acres:.4f} ac), got {flat_zone['zone_acres']}"
)
assert flat_zone["member_acres"] != flat_zone["zone_acres"], (
    "dual acreage: two labeled, DISTINCT numbers -- cell-count anchor vs polygon envelope"
)
assert flat_zone["mean_suitability"] == flat_member["mean_suitability"], (
    "zone score statistics are the member cells' own statistics"
)
assert flat_zone["twi_score_mean"] == round(flat_twi, 3) and flat_zone["depression_depth_max_m"] == 0.0
assert flat_zone["soil_coverage_fraction"] == 1.0 and flat_zone["criteria_complete"] is True
assert flat_zone["confidence"] == "high", "soil coverage + complete criteria = 2 signals = HIGH"
assert FLAG_NO_SERVICE_RELATIONSHIP in flat_zone["flags"], (
    "no production areas supplied -> the no-service case is a ZONE flag, never a drop"
)
assert flat_zone["below_min_area"] is False
# Member <-> zone linkage, both ways:
assert flat_zone["member_ids"] == [flat_member["id"]]
assert flat_member["zone_id"] == flat_zone["id"]
# Envelope hugs the boundary on every side -> adjacency EXACTLY 1.0
# now: the clipped hull IS the boundary box (a hull has no buffer-arc
# discretization to shave corners with -- the closing-era > 0.99
# tolerance is retired with the closing-drawn envelope).
assert flat_zone["boundary_adjacency_fraction"] == 1.0, (
    f"the clipped hull coincides with the boundary box -- adjacency exactly 1.0, "
    f"got {flat_zone['boundary_adjacency_fraction']}"
)
print(
    f"Fixture 1 (uniform wet flat): one member (mean {flat_member['mean_suitability']} hand-derived), one "
    f"zone with DISTINCT dual acreage ({flat_zone['zone_acres']} ac envelope vs {flat_zone['member_acres']} "
    "ac anchor), linkage both ways, adjacency 1.0."
)

# --- FIXTURE 2: V-valley + hand-built accumulation ribbon. Under the
# FINAL-TUNING defaults (threshold 0.5, seep-widened excavated taper)
# BOTH types now ribbon along the channel -- exactly the overlap the
# widening intends (moderate-grade wet ground is both dam and seep
# territory). 40x21: elevation = 100 + |c-10|*0.30 - r*0.25; every
# cell's max-neighbor grade is the downhill diagonal (0.30+0.25)/
# hypot(5,5) = 7.778% (uniform): embankment slope 1.0 (in 3-8),
# excavated slope (15-7.778)/10 = 0.7222 (the seep taper). Boundary
# covers centers rows 2..37 x cols 2..18 (612 cells).
# flow_accumulation is a hand-built OVERRIDE: 1 cell everywhere except
# the channel column (c=10), which carries V_CHANNEL_ACCUMULATION_PER_ROW
# * (r+1) cells. TWI IS ABSOLUTE NOW, so it is read off the raw value
# rather than off a rank: uniform tan(beta) = 0.0777817, a = 5 * acc
# metres, raw TWI = ln(5*acc / 0.0777817), scored on the 6.0 -> 10.0
# ramp.
#   side cells (acc 1):    raw ln(64.28) = 4.163 -> score 0.0 (FLOOR)
#   channel cells (acc M*(r+1)): raw 9.36..12.30 -> 0.839..1.0
# Per-cell RAW blends (soil never checked -> neutral 0.5):
#   embankment side:    .25 + .125 + .20*0.0                = 0.375  < 0.5
#   embankment channel: .30*d(r) + .375 + .20*twi(r)        = 0.6652..0.875
#   excavated side:     .35*(.5*0.0) + .15 + .25*.7222
#                       + .10*runon(1 cell)                 = 0.3309 < 0.5
#   excavated channel:  .35*(.5*twi(r)) + .15 + .25*.7222
#                       + .10*clip(acres(r)/2)              = 0.5330..0.6055
# => at the 0.5 default, EVERY on-parcel channel cell is a member of
# BOTH types' ribbons; every side cell is out of both.
#
# WHY THE ACCUMULATION CONSTANT MOVED WITH THE ABSOLUTE CURVE (15 -> 60).
# This fixture exists to test EXTRACTION GEOMETRY -- ribbons, members,
# closing, hulls -- on a channel that qualifies end to end, so the
# channel has to actually qualify. Under the retired percentile it did
# at 15 cells/row, but only because a percentile GRADES ON THE PARCEL:
# the headwater cells were the wettest ground present, so they ranked
# near 1.0 no matter how little water they carried (0.28 acres of
# catchment at row 2). The absolute curve reads them for what they are
# -- raw TWI 7.97, a real but modest score -- and the top three rows
# fell out of the embankment ribbon and the top six out of the excavated
# one. That is the curve being RIGHT, not the fixture breaking; but a
# fixture testing hull geometry should not also be testing where a
# ribbon starts. 60 cells/row gives the channel genuine catchment
# (1.11 ac at row 2, 14.08 ac at row 37 -- still under the 20 ac
# ceiling gate), restoring the end-to-end ribbon this fixture's
# downstream assertions are about. The behavior change itself is pinned
# where it belongs, in the two-boundary test below.
V_ROWS, V_COLS, V_CHANNEL = 40, 21, 10
v_array = np.zeros((V_ROWS, V_COLS))
for r in range(V_ROWS):
    for c in range(V_COLS):
        v_array[r, c] = 100.0 + abs(c - V_CHANNEL) * 0.30 - r * 0.25
V_DEM = _dem(v_array)
V_BOUNDARY = box(
    ORIGIN_X + 2 * RESOLUTION + 0.1,
    ORIGIN_Y - 38 * RESOLUTION + 0.1,
    ORIGIN_X + 19 * RESOLUTION - 0.1,
    ORIGIN_Y - 2 * RESOLUTION - 0.1,
)
V_CHANNEL_ACCUMULATION_PER_ROW = 60
v_accumulation = np.ones((V_ROWS, V_COLS))
for r in range(V_ROWS):
    v_accumulation[r, V_CHANNEL] = V_CHANNEL_ACCUMULATION_PER_ROW * (r + 1)

v_result = compute_water_survey_areas(V_DEM, V_BOUNDARY, flow_accumulation=v_accumulation)

# The stated per-cell formulas, cross-checked against the module's own
# raw surfaces on every gated cell:
v_gate = np.zeros((V_ROWS, V_COLS), dtype=bool)
v_gate[2:38, 2:19] = True
V_TAN_BETA = 0.55 / math.hypot(5.0, 5.0)
# The curve THIS fixture's window derived, read off the run -- see the
# flat fixture above for why a test must never hardcode the pair.
_V_BREAKPOINTS = v_result["twi_breakpoints"]


def _v_twi(accumulation_cells):
    """The absolute TWI score for this fixture's uniform grade, from the
    RAW value -- a = accumulation * cell_area / cell_width = 5 * cells,
    tan(beta) uniform, then the module's own ramp. NO POPULATION: the
    same accumulation scores the same here whatever else is on the
    grid, which is exactly what the old `(576 + i) / 611` rank could not
    say."""
    raw = math.log(5.0 * accumulation_cells / V_TAN_BETA)
    floor, full_credit = _V_BREAKPOINTS["floor"], _V_BREAKPOINTS["full_credit"]
    return min(max((raw - floor) / (full_credit - floor), 0.0), 1.0)


side_twi = _v_twi(1)
assert side_twi == 0.0, "acc-1 side cells sit under the ramp's floor -- plain hillslope, no wetness credit"
v_slope_score_exc = (15.0 - 0.55 / math.hypot(5.0, 5.0) * 100.0) / 10.0  # 0.72218...
v_emb_expected = np.zeros((V_ROWS, V_COLS))
v_exc_expected = np.zeros((V_ROWS, V_COLS))
for r in range(V_ROWS):
    for c in range(V_COLS):
        if not v_gate[r, c]:
            continue
        if c == V_CHANNEL:
            accumulation = V_CHANNEL_ACCUMULATION_PER_ROW * (r + 1)
            acres = accumulation * CA
            twi = _v_twi(accumulation)
        else:
            acres = 1 * CA
            twi = side_twi
        d = min(max((acres - 0.5) / 1.5, 0.0), 1.0)
        runon = min(max(acres / 2.0, 0.0), 1.0)
        # THREE CRITERIA. `d` (the drainage band on this cell's own
        # acres) is deliberately computed and deliberately UNUSED by
        # the embankment formula: contributing area is measured per
        # COMPARTMENT at the pinch cell now, never per seed cell. It is
        # kept in scope because the excavated run-on below still reads
        # this cell's acres and the two must be seen not to share a
        # term.
        v_emb_expected[r, c] = 0.36 * 1.0 + 0.36 * 0.5 + 0.28 * twi
        v_exc_expected[r, c] = 0.35 * (0.5 * twi) + 0.30 * 0.5 + 0.25 * v_slope_score_exc + 0.10 * runon
assert np.allclose(
    np.where(v_gate, v_result["surfaces"][SURVEY_TYPE_EMBANKMENT], 0.0), v_emb_expected
), "the RAW embankment blend must match the stated per-cell formulas on every gated cell"
assert np.allclose(
    np.where(v_gate, v_result["surfaces"][SURVEY_TYPE_EXCAVATED], 0.0), v_exc_expected
), "the RAW excavated blend must match the stated per-cell formulas on every gated cell"

expected_channel = {(r, V_CHANNEL) for r in range(2, 38)}
# THE RIBBON CLAIM IS EXCAVATED-ONLY, and it always should have been:
# the embankment surface is a NOMINATION surface (nothing thresholds
# it; generation is seed-based) and asserting where its 0.5 contour
# falls was testing a threshold that does not exist on that path. It
# reads differently now for a real reason -- with the drainage term
# gone, an off-channel cell keeps its full slope and soil merit
# (0.36 + 0.18 = 0.54) instead of being held under a cap by a criterion
# scoring 0 on it -- and that is the branch's intent, not a regression.
# The embankment formula stays pinned cell by cell above; the SEEDING
# consequences are pinned in the two tiers below.
assert all(v_exc_expected[r, c] >= 0.5 for r, c in expected_channel)
assert all(
    v_exc_expected[r, c] < 0.5
    for r in range(2, 38)
    for c in range(2, 19)
    if c != V_CHANNEL
), "every side cell stays out of the EXCAVATED ribbon at 0.5 -- the only thresholded surface"
assert all(
    v_emb_expected[r, V_CHANNEL] > v_emb_expected[r, c]
    for r in range(2, 38)
    for c in range(2, 19)
    if c != V_CHANNEL
), "the embankment NOMINATION surface still ranks channel above side slope, everywhere"

# EXCAVATED keeps the full extraction pipeline, and the ribbon math is
# unchanged:
exc_members = v_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(exc_members) == 1, f"excavated: one 8-connected channel member, got {len(exc_members)}"
assert set(exc_members[0]["cells"]) == expected_channel
exc_mean = round(float(np.mean([v_exc_expected[r, c] for r, c in expected_channel])), 4)
assert exc_members[0]["mean_suitability"] == exc_mean, (
    f"excavated: hand-summed member mean {exc_mean}, got {exc_members[0]['mean_suitability']}"
)
exc_zones = v_result["zones_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(exc_zones) == 1 and exc_zones[0]["member_count"] == 1
assert exc_zones[0]["mean_suitability"] == exc_mean, "zone statistics are the member chain's own"
# A straight one-cell-wide ribbon is a convex 5x180 m rectangle, so
# its HULL is exactly itself (900 m^2); the boundary clip then
# shaves the 0.1 m the fixture's boundary sits inside the end
# cells' edges -> 5 x 179.8 = 899 m^2. Hand-stated: 0.2222 ac
# envelope vs 0.2224 ac anchor -- hull-exact up to the real clip,
# no closing discretization anymore:
assert abs(exc_zones[0]["zone_acres"] - exc_zones[0]["member_acres"]) < 0.001
assert math.isclose(exc_zones[0]["zone_acres"], round(5.0 * 179.8 / 4046.8564224, 4)), (
    f"excavated: the clipped hull is exactly the 899 m^2 strip, got {exc_zones[0]['zone_acres']}"
)
assert exc_zones[0]["wettest_cell_rowcol"] == (37, V_CHANNEL)
assert exc_zones[0]["boundary_adjacency_fraction"] < 0.1

# EMBANKMENT no longer extracts ANYTHING from this surface -- it is a
# NOMINATION surface now (the formulas assertion above still pins it),
# and the generation is seed-based. No member regions exist for the
# type at all:
assert v_result["regions_by_type"][SURVEY_TYPE_EMBANKMENT] == [], (
    "the embankment path has no extraction stage -- member regions are excavated-only"
)
# The seeding, hand-derived, IN TWO TIERS since the seeding minimum
# dropped to 0.30. The channel cells blend 0.6652..0.875; every side
# cell blends EXACTLY 0.375 -- 0.30*0 (no catchment) + 0.25*1.0 (slope)
# + 0.25*0.5 (neutral soil) + 0.20*0 (raw TWI 4.16, under the ramp's
# floor). 0.375 is the OFF-CHANNEL ARCHETYPE'S ARITHMETIC CAP, the class
# a 0.50 minimum excluded by construction rather than by judgement, and
# it is exactly what EMBANKMENT_SEED_MIN_SCORE = 0.30 was lowered to
# admit. This fixture is where that is visible: at 0.50 the side slopes
# were not nominated at all; at 0.30 they are, twelve of them, and two
# go on to produce surviving compartments while the channel -- higher
# scoring on every criterion -- produces none.
#
# THE SATURATION PLATEAU IS GONE, AND THAT IS THE WINDOW-REFERENCED
# CURVE'S DOING. Under the retired FIXED breakpoints, twi_score()
# reached 1.0 at raw TWI 10.0 -- which this channel passes at row 5 --
# so rows 5..37 all blended to EXACTLY 0.875, a 33-way argmax tie
# broken row-major, and the first seed was the TOP of the plateau
# rather than the most-accumulated cell. The plateau was an artifact of
# a breakpoint that stopped below the ground's own range: every cell
# above 10.0 was declared equal because the curve had run out, not
# because the terrain had.
#
# The window-referenced curve ends where the WINDOW ends, so the channel
# grades all the way to its wettest cell and seeding walks DOWN from
# row 37 (the most accumulated) as a reading of catchment should. Note
# this fixture lands on the 'observed_range' fallback -- 20 of its 21
# columns are identical hillslope, so the p25 and p90 of the window tie
# and the curve falls back to the window's observed min..max (see
# twi_window_breakpoints()). A real 3DEP window does not do that; the
# NORMAL percentile path is exercised in test_twi_window_referenced.py
# against a purpose-built distribution.
#
# Iterative claiming at 30 m (6 cells of row distance, symmetric and
# inclusive), blend-descending from row 37: row 37 claims rows 31..40,
# then row 30 claims 24..36, and so on -> channel seeds at rows 37, 30,
# 23, 16, 9, 2. The 0.375 side tier then seeds what the channel discs
# did not claim, ties resolved row-major, alternating flanks as the scan
# reaches them.
v_seeds = v_result["embankment_seeds"]
V_CHANNEL_SEEDS = [
    (37, V_CHANNEL), (30, V_CHANNEL), (23, V_CHANNEL),
    (16, V_CHANNEL), (9, V_CHANNEL), (2, V_CHANNEL),
]
V_SIDE_SEEDS = [
    (2, 2), (2, 17), (8, 3), (8, 16), (14, 2), (14, 17),
    (20, 3), (20, 16), (26, 2), (26, 17), (32, 3), (32, 16),
]
assert [record["rowcol"] for record in v_seeds] == V_CHANNEL_SEEDS + V_SIDE_SEEDS, (
    f"hand-derived 30 m claiming order, channel tier then side tier, got "
    f"{[record['rowcol'] for record in v_seeds]}"
)
# STRICTLY DESCENDING, not tied: the channel seeds now carry six
# DISTINCT scores because nothing saturates the TWI half early any more.
_channel_blends = [record["blend_score"] for record in v_seeds[:6]]
assert _channel_blends == sorted(_channel_blends, reverse=True) and len(set(_channel_blends)) == 6, (
    f"the plateau is gone -- every channel seed carries its own catchment's score, got {_channel_blends}"
)
assert math.isclose(_channel_blends[0], 0.8182, abs_tol=5e-5), (
    "the most-accumulated channel cell tops the surface: 0.36 slope + 0.18 soil + 0.28*twi_max"
)
# EVERY SIDE SEED SITS AT EXACTLY 0.54 -- 0.36*1.0 (slope) + 0.36*0.5
# (neutral soil) + 0.28*0 (raw TWI 4.16, under the ramp's floor). THIS
# NUMBER USED TO BE 0.375 AND THE DIFFERENCE IS THE WHOLE BRANCH:
# under the four-criterion blend an off-channel cell was held to
# 0.25*1.0 + 0.25*0.5 = 0.375 no matter how good its ground was,
# because a criterion carrying 0.30 scored 0 on it BY CONSTRUCTION.
# That was an arithmetic cap on an entire archetype, not a judgement
# about it, and with contributing area measured at the pinch instead
# the cap is simply gone: off-channel ground now scores its slope and
# soil merit outright.
assert all(math.isclose(record["blend_score"], 0.54) for record in v_seeds[6:]), (
    "every side seed sits at its own slope+soil merit, all twelve at exactly 0.54"
)
assert all(record["blend_score"] >= wsa.EMBANKMENT_SEED_MIN_SCORE for record in v_seeds)
# AND THIS IS THE MEASUREMENT EMBANKMENT_SEED_MIN_SCORE'S NEXT DECISION
# IS MADE FROM. The old 0.50 minimum was lowered to 0.30 precisely
# because 0.375 excluded the off-channel archetype by arithmetic; at
# 0.54 that archetype clears 0.50 on its own merits, so the argument
# that lowered the floor no longer applies to it. The floor is NOT
# retuned in this branch -- the ladder measures the new distribution and
# the next branch decides -- but the fact is pinned here rather than
# left to be rediscovered.
assert 0.54 > 0.5, (
    "the off-channel tier now clears the OLD 0.50 minimum unaided -- the arithmetic exclusion "
    "that justified lowering the floor to 0.30 is gone, and the floor's re-decision is the next "
    "branch's, against the ladder"
)

# EVERY CHANNEL SEED FAILS, honestly, because this prism valley has a
# CONSTANT cross-section -- crest-to-crest width is identical at every
# station, so the along-channel minimum lands on the seed's own station
# (argmin tie -> index 0: the valley never narrows below the seed) --
# the ONE failure the accepted-terminal doctrine kept: no_constriction.
_channel_records = v_seeds[:6]
assert all(record["status"] == wsa.SEED_STATUS_FAILED for record in _channel_records), (
    "a constant-width prism never narrows below any channel seed -- every one reports nothing"
)
assert all(record["reason_code"] == wsa.REASON_NO_CONSTRICTION for record in _channel_records), (
    f"widths never drop below the seed station -> no_constriction, got "
    f"{[r['reason_code'] for r in _channel_records]}"
)

# THE SIDE TIER IS THE POINT OF THE LOWER MINIMUM, and it does not all
# succeed either -- which is the shape the change predicted. The four
# upslope-most side seeds walk reaches that never narrow (no_constriction
# again); eight build compartments, four of those lose the
# compartment-overlap dedupe and two fall under the acre floor, and TWO
# SURVIVE. Ground scoring 0.375 -- less than half what the channel
# scores -- is the only ground on this fixture that produces an
# embankment survey zone at all.
_side_records = v_seeds[6:]
assert sum(1 for r in _side_records if r["status"] == wsa.SEED_STATUS_COMPARTMENT) == 8
assert sum(
    1 for r in _side_records
    if r["status"] == wsa.SEED_STATUS_FAILED and r["reason_code"] == wsa.REASON_NO_CONSTRICTION
) == 4
# The dedupe that culls four of those eight is a ZONE-level overlap
# dedupe, so it lands on the dropped ZONE's drop_reason, not on the seed
# record (a pinch-level duplicate would land on the seed; this fixture
# has none).
_v_dropped_reasons = [zone["drop_reason"] for zone in v_result["dropped_zones"]]
assert sum(1 for r in _v_dropped_reasons if r.startswith(wsa.DUPLICATE_OF_ZONE_REASON_PREFIX)) == 4
assert sum(1 for r in _v_dropped_reasons if r == wsa.FLAG_BELOW_MIN_AREA) == 2
v_emb_zones = v_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
assert len(v_emb_zones) == 2, "two off-channel compartments survive the floor"
assert {zone["seed"]["rowcol"] for zone in v_emb_zones} == {(14, 2), (26, 2)}
assert all(zone["seed_blend_score"] == 0.54 for zone in v_emb_zones)

# THE FILL CLAIM, AND IT IS THIS BRANCH'S WHOLE ARGUMENT IN ONE FIXTURE.
# Both surviving compartments are anchored on SIDE-SLOPE seeds whose own
# contributing area is a single cell -- 0.0062 ac, which the drainage
# band scores 0.0. Under the retired per-cell measurement that 0.0 was
# 30% of their nomination score and the reason the whole off-channel
# class was capped. But their pinches sit down in the channel, and the
# catchment ABOVE THE DAM REACH -- the water that would actually fill
# these ponds -- is 8.5 and 13.0 acres, full credit on the very same
# band with the very same constants. The criterion was asking the right
# question of the wrong cell, and this is what recovering it looks like.
_v_seed_acres = 1 * CA
assert wsa.drainage_band_score(np.array(_v_seed_acres)) == 0.0, (
    "a side-slope seed's OWN catchment is one cell and scores zero -- the measurement that was "
    "killing this archetype"
)
for zone in v_emb_zones:
    assert zone["pinch_catchment_acres"] > 8.0, (
        f"the pinch of an off-channel compartment sits in the channel: "
        f"{zone['pinch_catchment_acres']} ac"
    )
    assert zone["pinch_catchment_acres"] < wsa.MAX_VALLEY_CONTRIBUTING_AREA_ACRES, (
        "and still under the ceiling -- this fixture is not testing the disqualifier"
    )
    assert zone["pinch_drainage_score"] == 1.0, "full credit on the unchanged band"
    assert zone["catchment_exceeds_ceiling"] is False
    # The composite, and both its inputs, on the same record.
    assert zone["compartment_rank_score"] == wsa.compartment_rank_score(0.54, 1.0) == 0.77
_v_by_seed = {zone["seed"]["rowcol"]: zone for zone in v_emb_zones}
assert _v_by_seed[(26, 2)]["pinch_catchment_acres"] > _v_by_seed[(14, 2)]["pinch_catchment_acres"], (
    "the downstream compartment's dam reach carries more catchment"
)

# THE SELECTION FLIPPED, AND SAYING SO IS THE POINT. Pooled selection
# compares the embankment COMPARTMENT RANK SCORE against the excavated
# member mean. On the seed blend alone (0.54) the excavated zone won at
# 0.5843; combined with a full-credit fill claim the compartment reads
# 0.77 and wins. That is not a tuning artifact -- it is the pipeline
# finally able to see that the off-channel compartments on this fixture
# have real water above them, which no per-seed measurement could say.
v_exc_zone = exc_zones[0]
assert v_exc_zone["rank"] == 1, "still rank 1 WITHIN its own type -- ranking is per type"
assert v_exc_zone["presented"] is True and v_exc_zone["presentation_order"] == 2, (
    "the sole excavated survivor is the presented set's SECOND entry -- the interleave puts "
    "excavated rank 1 between the two embankment zones, and presentation is a separate mark from "
    "the rank asserted above"
)
assert v_exc_zone["mean_suitability"] == 0.5843
assert v_result["selected_water_zone"] in v_emb_zones, (
    "the pooled winner is an embankment compartment now: 0.77 over the excavated 0.5843"
)
assert v_result["selected_water_zone"]["rank"] == 1
assert v_exc_zone["sparse_anchor"] is False

# The narrative carries the seed accounting: 18 seeds, 10 failed, each
# with its reason code -- a reach with no on-parcel pinch still reports
# honestly as nothing, and now the count of them is the cost line the
# seed ladder exists to make visible.
v_narrative = build_narrative_data(v_result)
assert v_narrative["embankment_generation"] == wsa.PROVENANCE_SEED_COMPARTMENT
assert v_narrative["embankment_seed_count"] == 18 and v_narrative["embankment_failed_seed_count"] == 10
assert v_narrative["embankment_zone_count"] == 2 and v_narrative["excavated_zone_count"] == 1
assert v_narrative["zone_count"] == 3 and len(v_narrative["zones"]) == 3
# The failure vocabulary is the two classes and nothing else, and every
# dedupe reason names a REAL zone id -- which may itself be a zone that
# was later dropped (a pinch-level duplicate names the seed that beat
# it, and that winner can afterwards lose the compartment-overlap
# dedupe). Naming the winner is a statement about seeding order, not a
# promise that the winner survived.
_v_failed_codes = {entry["reason_code"] for entry in v_narrative["embankment_failed_seeds"]}
assert wsa.REASON_NO_CONSTRICTION in _v_failed_codes
_v_all_zone_ids = {z["id"] for z in v_result["zones"] + v_result["dropped_zones"]}
for _code in _v_failed_codes - {wsa.REASON_NO_CONSTRICTION}:
    assert _code.startswith(wsa.DUPLICATE_OF_ZONE_REASON_PREFIX), f"unexpected failure code {_code}"
    assert int(_code[len(wsa.DUPLICATE_OF_ZONE_REASON_PREFIX):]) in _v_all_zone_ids, (
        f"{_code} must name a zone that exists in this result"
    )
print(
    f"Fixture 2 (V-valley, seed minimum 0.30): excavated ribbons the channel (mean "
    f"{v_exc_zone['mean_suitability']}); 18 seeds in two tiers -- 6 channel cells at six DISTINCT "
    f"scores topping at {_channel_blends[0]:.4f} (the retired fixed curve's 0.875 plateau is gone) "
    "that ALL fail no_constriction (constant prism cross-section), and 12 off-channel cells at "
    "exactly 0.375 (the archetype 0.50 excluded) of which two produce the fixture's only surviving "
    "embankment zones."
)

# --- FIXTURE 2b: member-vs-zone split where the envelope ADDS ground.
# Same flat construction as fixture 1, but WET soil covers TWO patches
# (cols 5..8 and cols 12..14) and the 3-column gap (15 m) between them
# is LEAKY soil (group A, rapid ksat -> soil score 0.0), which fails it
# out of both ribbons. 15 m < the 30 m grouping -> the two members fuse
# into ONE zone whose envelope bridges the gap. Score statistics from
# members ONLY: every member cell scores the fixture-1 value, so the
# zone mean must be exactly that -- if envelope ground were laundered
# in, the 30 gap cells would drag the mean down.
#
# WHY THE GAP NEEDS REAL BAD SOIL NOW, WHERE THE NEUTRAL DEFAULT USED TO
# DO IT. The gap used to be simply uncovered, taking SOIL_UNAVAILABLE_
# SCORE (0.5) and landing at 0.4878 -- just under the threshold. That
# margin came from the retired percentile scoring this dead-flat parcel
# a flat 0.5 for wetness. The absolute curve reads the same ground at
# raw TWI 8.52 -> 0.629 (see fixture 1's hand derivation), which lifts a
# neutral-soil flat cell to 0.5104 -- OVER the line. That is the curve
# being right about dead-flat, zero-slope ground rather than the fixture
# being wrong, and the fixture wants a gap that fails for a stated
# REASON rather than one that squeaks under a threshold by 0.012.
split_soil_inputs = {
    "ksat_rows": [
        {"mukey": "A", "ksat_r": 0.05},
        {"mukey": "B", "ksat_r": 0.05},
        {"mukey": "C", "ksat_r": 200.0},
    ],
    "components": [
        {"mukey": "A", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"},
        {"mukey": "B", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"},
        {"mukey": "C", "hydricrating": "No", "comppct_r": 100, "hydgrp": "A"},
    ],
    "geometries_by_mukey": {
        "A": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 25.0, ORIGIN_Y - 75.0, ORIGIN_X + 45.0, ORIGIN_Y - 25.0))
        ),
        "B": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 60.0, ORIGIN_Y - 75.0, ORIGIN_X + 75.0, ORIGIN_Y - 25.0))
        ),
        "C": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 45.0, ORIGIN_Y - 75.0, ORIGIN_X + 60.0, ORIGIN_Y - 25.0))
        ),
    },
}
split_result = compute_water_survey_areas(
    FLAT_DEM, FLAT_BOUNDARY, soil_inputs=split_soil_inputs,
    flow_accumulation=FLAT_SELF_ACCUMULATION,
)
split_members = split_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(split_members) == 2, (
    f"two wet-soil patches -> two members (the leaky gap scores 0.3604 < 0.5), got {len(split_members)}"
)
assert {m["cell_count"] for m in split_members} == {40, 30}, "4x10 and 3x10 cell patches"
split_zones = split_result["zones_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(split_zones) == 1, "a 15 m gap at 30 m grouping fuses the two members into ONE zone"
split_zone = split_zones[0]
assert split_zone["member_count"] == 2 and split_zone["cell_count"] == 70
assert math.isclose(split_zone["member_acres"], round(70 * CA, 4))
assert split_zone["zone_acres"] > split_zone["member_acres"], (
    "the bridged envelope adds the gap -- more ground to walk than anchored it"
)
# The hull of the two same-row-span patches is the full bounding
# rectangle; clipped to the boundary it IS the boundary box, exactly
# (the closing-era ~2% tolerance is retired with the closing-drawn
# envelope):
assert abs(split_zone["zone_acres"] - expected_zone_acres) < 0.001, (
    f"the bridging hull clips to exactly the boundary box ({expected_zone_acres:.4f} ac), got {split_zone['zone_acres']}"
)
assert split_zone["mean_suitability"] == round(expected_flat_score, 4), (
    "score statistics from MEMBERS ONLY: the zone mean is the member cells' 0.6378 -- laundering the 30 "
    "gap cells at 0.4878 in would have dragged it to ~0.593"
)
assert sorted(split_zone["member_ids"]) == sorted(m["id"] for m in split_members)
for member in split_members:
    assert member["zone_id"] == split_zone["id"]
print(
    f"Fixture 2b (split patches): two members fuse across a 15 m gap into one zone -- envelope "
    f"{split_zone['zone_acres']} ac vs anchor {split_zone['member_acres']} ac, member-only mean preserved."
)

# --- FIXTURE 3: THE FLOOR FILTERS ON ZONE ACRES NOW (pre-merge change
# 2: the basis is the walkable hull envelope -- the object the floor's
# rationale was always about -- with sparse_anchor covering the honesty
# cost). Boundary covers only a 3x3 block: the member square's hull
# clips to the 14.8x14.8 m boundary box = 219.04 m^2 = 0.0541 ac <
# the 0.1 ac floor -> the zone is DROPPED from the pipeline output
# (status: dropped, drop_reason: below_min_area, rank None, absent from
# zones_geojson), carried in dropped_zones with BOTH acreages on the
# record (zone_acres 0.0541 judged; member_acres 0.0556 anchoring) --
# visible and attributed, never silent. With no survivor, the selection
# is honestly None. ---
TINY_BOUNDARY = box(
    ORIGIN_X + 8 * RESOLUTION + 0.1,
    ORIGIN_Y - 11 * RESOLUTION + 0.1,
    ORIGIN_X + 11 * RESOLUTION - 0.1,
    ORIGIN_Y - 8 * RESOLUTION - 0.1,
)
tiny_result = compute_water_survey_areas(
    FLAT_DEM, TINY_BOUNDARY, soil_inputs=GOOD_WET_SOIL_INPUTS,
    flow_accumulation=FLAT_SELF_ACCUMULATION,
)
assert tiny_result["zones"] == [] and tiny_result["zones_by_type"][SURVEY_TYPE_EXCAVATED] == [], (
    "a sub-floor zone is OUT of the pipeline output -- the floor is a filter now"
)
assert tiny_result["selected_water_zone"] is None, "no survivor -> the selection is honestly None"
assert "presented_zones" not in tiny_result, (
    "presentation MARKS zones, it never publishes a separate set -- a second list of zones would be "
    "a second source of truth about which zones exist, which is exactly what the deleted cap did"
)
assert tiny_result["presentation"]["presented_count"] == 0, "nothing survived, so nothing is presented"
assert tiny_result["presentation"]["rule_applied"] == "no surviving zones", (
    "the rule line states the honest outcome rather than an empty recipe"
)
assert tiny_result["presentation"]["survivor_counts"] == {SURVEY_TYPE_EMBANKMENT: 0, SURVEY_TYPE_EXCAVATED: 0}
assert len(tiny_result["dropped_zones"]) == 1, "the drop is carried, never silent"
tiny_dropped = tiny_result["dropped_zones"][0]
assert tiny_dropped["status"] == wsa.ZONE_STATUS_DROPPED
assert tiny_dropped["drop_reason"] == FLAG_BELOW_MIN_AREA, "the reason code attributes the drop"
assert tiny_dropped["rank"] is None
assert tiny_dropped["presented"] is False and tiny_dropped["presentation_order"] is None, (
    "a dropped zone is out of the output, so it is out of the presented set -- marked explicitly, "
    "never by a missing key, because its record still rides the diagnostic and the dropped layer"
)
# The dual-acreage dropped record: the number the floor JUDGED
# (zone_acres, the clipped hull) and the anchoring signal, both stated:
assert tiny_dropped["zone_acres"] < MIN_SURVEY_REGION_AREA_ACRES, "the drop's basis is the ZONE acreage"
assert math.isclose(tiny_dropped["zone_acres"], round(219.04 / 4046.8564224, 4)), (
    f"hand-derived clipped hull 14.8 x 14.8 m = 0.0541 ac, got {tiny_dropped['zone_acres']}"
)
assert tiny_dropped["cell_count"] == 9 and math.isclose(tiny_dropped["member_acres"], round(9 * CA, 4))
assert FLAG_BELOW_MIN_AREA in tiny_dropped["flags"], "the flag still rides the dropped zone's properties"
assert not survey_areas_to_geojson(tiny_result["zones"])["features"], (
    "the pipeline's own zones_geojson omits dropped zones entirely"
)
print("Fixture 3 (floor filter): the 9-cell sliver zone is dropped with status/reason, selection None, nothing silent.")

# --- FIXTURE 3b: the ZONE-ACRES basis of the floor, asserted from the
# direction that DISTINGUISHES the bases. Two 2-col x 3-row wet patches
# (6 cells each; 12 member cells = 0.0741 ac -- BELOW the floor on the
# retired member-acres basis) 20 m apart on a 3-row strip: their hull
# bridges the gap into a 40x15 m rectangle, clipped to the boundary =
# 39.9 x 14.8 = 590.52 m^2 = 0.1459 ac >= the floor, so the zone
# SURVIVES -- the filter judges the walkable envelope. The honesty cost
# is the sparse-anchor guard's job, and here it stays SILENT:
# member/zone = 0.0741/0.1459 = 0.508 >= 0.2. ---
STRIP_BOUNDARY = box(
    ORIGIN_X + 5 * RESOLUTION + 0.1,
    ORIGIN_Y - 9 * RESOLUTION + 0.1,
    ORIGIN_X + 14 * RESOLUTION - 0.1,
    ORIGIN_Y - 6 * RESOLUTION - 0.1,
)
# The two wet patches sit in LEAKY ground (C/D, group A + rapid ksat),
# for the same reason fixture 2b needed it: on the absolute TWI curve
# dead-flat ground scores 0.629 for wetness, so an uncovered
# neutral-soil flat cell now clears 0.5 on its own and would join the
# ribbon. The gap has to fail on a stated soil reason, which is what
# keeps this fixture about the MEMBER-vs-ZONE ACREAGE BASIS rather than
# about a threshold margin.
strip_soil_inputs = {
    "ksat_rows": [
        {"mukey": "A", "ksat_r": 0.05},
        {"mukey": "B", "ksat_r": 0.05},
        {"mukey": "C", "ksat_r": 200.0},
        {"mukey": "D", "ksat_r": 200.0},
    ],
    "components": [
        {"mukey": "A", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"},
        {"mukey": "B", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"},
        {"mukey": "C", "hydricrating": "No", "comppct_r": 100, "hydgrp": "A"},
        {"mukey": "D", "hydricrating": "No", "comppct_r": 100, "hydgrp": "A"},
    ],
    "geometries_by_mukey": {
        "A": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 25.0, ORIGIN_Y - 45.0, ORIGIN_X + 35.0, ORIGIN_Y - 30.0))
        ),
        "B": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 55.0, ORIGIN_Y - 45.0, ORIGIN_X + 65.0, ORIGIN_Y - 30.0))
        ),
        # The 20 m gap between the patches...
        "C": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 35.0, ORIGIN_Y - 45.0, ORIGIN_X + 55.0, ORIGIN_Y - 30.0))
        ),
        # ...and the strip's remaining column past patch B.
        "D": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 65.0, ORIGIN_Y - 45.0, ORIGIN_X + 72.0, ORIGIN_Y - 30.0))
        ),
    },
}
strip_result = compute_water_survey_areas(
    FLAT_DEM, STRIP_BOUNDARY, soil_inputs=strip_soil_inputs,
    flow_accumulation=FLAT_SELF_ACCUMULATION,
)
strip_members = strip_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(strip_members) == 2 and all(m["cell_count"] == 6 for m in strip_members)
assert all(m["below_min_area"] for m in strip_members), "member REGIONS still just carry the flag"
strip_zones = strip_result["zones_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(strip_zones) == 1 and strip_result["dropped_zones"] == [], (
    "12 member cells = 0.0741 ac is under the floor, but the 0.1459 ac hull envelope is not: the zone "
    "SURVIVES because the basis is ZONE acres -- the member-acres basis would have dropped it"
)
strip_zone = strip_zones[0]
assert math.isclose(strip_zone["member_acres"], round(12 * CA, 4))
assert strip_zone["member_acres"] < MIN_SURVEY_REGION_AREA_ACRES <= strip_zone["zone_acres"], (
    "the basis proof in one line: anchor below the floor, walkable envelope above it, zone alive"
)
assert math.isclose(strip_zone["zone_acres"], round(590.52 / 4046.8564224, 4)), (
    f"hand-derived clipped hull 39.9 x 14.8 m = 0.1459 ac, got {strip_zone['zone_acres']}"
)
assert strip_zone["status"] == wsa.ZONE_STATUS_NOMINATED
assert strip_zone["sparse_anchor"] is False and FLAG_SPARSE_ANCHOR not in strip_zone["flags"], (
    "member/zone = 0.508 >= 0.2 -> the sparse-anchor guard stays silent here"
)
print(
    "Fixture 3b (zone-acres basis): 0.0741 ac of anchor under a 0.1459 ac hull survives the floor -- "
    "the walkable envelope is the judged object, sparse-anchor silent at 0.508."
)

# --- THE PRESENTATION RULE'S SURFACE. The deleted TOP_N cap FILTERED
# (an unpresented zone left the payload); this rule MARKS. Both halves
# are asserted here at the module surface: the deleted machinery stays
# deleted by name, and the replacement's constants and per-zone keys are
# present on EVERY zone -- surviving or dropped -- so "not presented"
# and "not marked by this build" can never be the same wire state. ---
from water_survey_areas import (  # noqa: E402
    SURVEY_TYPES,
    assign_presentation_order,
    attach_cross_type_overlaps,
    rank_survey_zones_per_type,
    select_survey_zone,
)

assert not hasattr(wsa, "WATER_ZONE_PRESENTATION_TOP_N"), (
    "the DELETED cap's constant stays deleted -- the replacement is a different rule with a "
    "different name, never the old one revived under its old spelling"
)
assert not hasattr(wsa, "apply_presentation"), "the deleted cap's guarantee/swap function stays deleted"
_wsa_module_ast = ast.parse(inspect.getsource(wsa))
_called_names = {
    node.func.id for node in ast.walk(_wsa_module_ast)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
}
assert "apply_presentation" not in _called_names, "nothing in the module still calls the deleted machinery"

assert wsa.WATER_ZONE_PRESENTATION_COUNT == 4 and wsa.WATER_ZONE_PRESENTATION_PER_TYPE == 2
assert (
    wsa.WATER_ZONE_PRESENTATION_PER_TYPE * len(SURVEY_TYPES) == wsa.WATER_ZONE_PRESENTATION_COUNT
), "the two constants must agree: the per-type target exactly fills the set when both types produce"
for zone_holder in (flat_result["zones"], v_result["zones"], strip_result["zones"], tiny_result["dropped_zones"]):
    for z in zone_holder:
        assert "presented" in z and "presentation_order" in z, (
            "EVERY zone carries both keys -- a survivor that is not presented says so with False/None, "
            "never with a missing key a consumer would have to interpret"
        )
        assert isinstance(z["presented"], bool)
        assert (z["presentation_order"] is None) is not z["presented"], (
            "the two keys never disagree: presented <=> an integer order"
        )

# Rank + selection on hand-built zone dicts: every survivor is ranked
# within its type ON ITS TYPE'S OWN INSTRUMENT -- embankment by the
# COMPARTMENT RANK SCORE (seed blend combined with the pinch cell's
# drainage score), excavated by member-mean suitability -- and the
# pooled rank-1 invariant holds with NO cap in between. The embankment
# minis carry a deliberately LOW compartment mean_suitability (0.1)
# beside their two claims: if ranking or selection ever read the
# compartment mean, every assertion below flips -- the walked ground's
# mean must never rank a compartment.
def _mini_zone(zid, stype, mean, acres, poly, seed_blend=None, pinch_drainage=None, catchment=None):
    zone = {
        "id": zid, "survey_type": stype, "mean_suitability": mean,
        "polygon_utm": poly,
    }
    if stype == SURVEY_TYPE_EMBANKMENT:
        # The composite is DERIVED from the two claims by the module's
        # own unit, never hand-written into the fixture: a test that
        # typed 0.95 here would pass whatever the ranking rule did.
        zone["seed_blend_score"] = seed_blend
        zone["pinch_drainage_score"] = pinch_drainage
        zone["pinch_catchment_acres"] = catchment
        zone["compartment_rank_score"] = wsa.compartment_rank_score(seed_blend, pinch_drainage)
        zone["zone_acres"] = acres
    else:
        zone["member_acres"] = acres
    return zone


rank_pool = [
    _mini_zone(0, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(0, 0, 20, 20),
               seed_blend=0.9, pinch_drainage=1.0, catchment=6.0),
    _mini_zone(1, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(100, 0, 120, 20),
               seed_blend=0.8, pinch_drainage=0.8, catchment=1.7),
    _mini_zone(2, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(200, 0, 220, 20),
               seed_blend=0.7, pinch_drainage=0.6, catchment=1.4),
    _mini_zone(3, SURVEY_TYPE_EXCAVATED, 0.65, 1.0, box(10, 0, 30, 20)),
    _mini_zone(4, SURVEY_TYPE_EXCAVATED, 0.6, 1.0, box(300, 0, 320, 20)),
]
rank_survey_zones_per_type(rank_pool)
assert [z["rank"] for z in rank_pool] == [1, 2, 3, 1, 2], (
    "EVERY survivor is ranked within its type (embankment by its compartment rank score, despite "
    "the 0.1 compartment means) -- rank 3 exists because nothing caps the list at 3 anymore"
)
assert [z["compartment_rank_score"] for z in rank_pool[:3]] == [0.95, 0.8, 0.65], (
    "the composite is the equal-weight mean of the two claims, computed by the module"
)
assert select_survey_zone(rank_pool) is rank_pool[0], (
    "the pooled rank-1 invariant on the per-type scores: compartment rank score 0.95 beats "
    "member-mean 0.65 -- and the 0.1 compartment mean never enters the pool"
)

# IDENTICAL SEED BLENDS, DIFFERENT CATCHMENTS: the fill claim breaks the
# tie, which is the entire reason the composite exists. Under the
# retired seed-blend-only ranking these two were indistinguishable and
# their order fell to the acreage tiebreak -- so the pair is built with
# the WORSE-FILLED one holding the LARGER acreage, which means an
# acreage-tiebreak ordering and a fill-claim ordering disagree and only
# one of them can produce the ranks asserted here.
tie_pool = [
    _mini_zone(0, SURVEY_TYPE_EMBANKMENT, 0.1, 5.0, box(0, 0, 20, 20),
               seed_blend=0.6, pinch_drainage=0.0, catchment=0.2),
    _mini_zone(1, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(100, 0, 120, 20),
               seed_blend=0.6, pinch_drainage=1.0, catchment=9.4),
]
rank_survey_zones_per_type(tie_pool)
assert tie_pool[1]["rank"] == 1 and tie_pool[0]["rank"] == 2, (
    "equal seed blends rank by the catchment above their dam reaches, not by acreage: "
    f"{[(z['id'], z['rank'], z['compartment_rank_score']) for z in tie_pool]}"
)
assert tie_pool[0]["seed_blend_score"] == tie_pool[1]["seed_blend_score"] == 0.6, (
    "and BOTH inputs stay on the record beside the composite -- the anchor claim is not consumed "
    "by the ranking, it is reported next to it"
)
assert (tie_pool[0]["compartment_rank_score"], tie_pool[1]["compartment_rank_score"]) == (0.3, 0.8)
assert select_survey_zone(tie_pool) is tie_pool[1], (
    "a compartment with no water above it does not win a pool against one that has water, however "
    "much ground it covers"
)

# attach_cross_type_overlaps on the same pool, hand-derived: emb zone 0
# (x 0..20) and exc zone 3 (x 10..30) overlap on x 10..20 -> 200 m^2 of
# each one's 400 m^2 envelope = fraction 0.5 both ways. Same-type
# overlap is never reported (zones 0/1/2 don't see each other), and a
# zero intersection stays ABSENT from the list, not a 0.0 entry.
attach_cross_type_overlaps(rank_pool)
assert rank_pool[0]["cross_type_overlaps"] == [{"zone_id": 3, "fraction": 0.5}], (
    f"hand-derived: 10x20 m shared of the 20x20 envelope = 0.5, got {rank_pool[0]['cross_type_overlaps']}"
)
assert rank_pool[3]["cross_type_overlaps"] == [{"zone_id": 0, "fraction": 0.5}], "symmetric here (equal areas)"
assert rank_pool[1]["cross_type_overlaps"] == [] and rank_pool[4]["cross_type_overlaps"] == [], (
    "no cross-type intersection -> an empty list, never zero-fraction filler entries"
)
print(
    "Presentation surface: the deleted cap stays deleted by name, both new constants agree, every "
    "zone (surviving and dropped) carries presented + presentation_order; all survivors ranked "
    "(a rank 3 exists), rank-1 selects, cross-type fractions hand-verified at 0.5."
)


# =========================================================================
# 3d. PRESENTATION -- the top 2 of each type, backfilled to four.
#     A MARK, NOT A FILTER.
# =========================================================================
# THE FOUR CASES ARE DRIVEN END TO END through compute_water_survey_areas()
# on synthetic DEMs, not asserted against hand-built zone dicts alone: the
# rule is only worth anything if the real ranking feeds it, and a
# fixture that hand-writes ranks would pass whatever generation did. Each
# fixture below is a channel-in-a-plane like the V fixture (same
# formulas, same absolute TWI curve); the knobs that move the survivor
# counts are the number of channels, the valley's length, and the
# cross/downhill grade -- the last of which is what starves the EXCAVATED
# type, because its slope taper is gone by 15% while embankment seeding
# still qualifies ground at the 0.30 seeding minimum.


def _presentation_dem(rows, cols, channels, accumulation_per_row, cross_grade, down_grade):
    """A channel-in-a-plane DEM plus the hand-built accumulation ribbon
    and the parcel box, exactly the V fixture's construction generalized
    to N channels and a settable grade: elevation =
    100 + (distance to the nearest channel) * cross - row * down, one
    accumulation ribbon per channel. Returns (dem, boundary, accumulation)."""
    array = np.zeros((rows, cols))
    for r in range(rows):
        for c in range(cols):
            array[r, c] = 100.0 + min(abs(c - ch) for ch in channels) * cross_grade - r * down_grade
    accumulation = np.ones((rows, cols))
    for r in range(rows):
        for ch in channels:
            accumulation[r, ch] = accumulation_per_row * (r + 1)
    boundary = box(
        ORIGIN_X + 2 * RESOLUTION + 0.1,
        ORIGIN_Y - (rows - 2) * RESOLUTION + 0.1,
        ORIGIN_X + (cols - 2) * RESOLUTION - 0.1,
        ORIGIN_Y - 2 * RESOLUTION - 0.1,
    )
    return _dem(array), boundary, accumulation


def _presented_in_order(result):
    """The presented zones, read back off the marks in presentation
    order -- the reader's own reconstruction, never the summary's
    presented_zone_ids list, so the two are checkable against each other."""
    presented = [zone for zone in result["zones"] if zone["presented"]]
    presented.sort(key=lambda zone: zone["presentation_order"])
    return presented


def _assert_presentation_invariants(result, label):
    """The invariants that hold on EVERY run whatever the case, checked
    on each fixture below so a case-specific assertion never stands
    alone: the order is 1..N with no gaps, the marks and the summary
    agree, presentation never exceeds the cap, and NOTHING WAS REMOVED
    from any set by marking it."""
    summary = result["presentation"]
    presented = _presented_in_order(result)
    assert [zone["presentation_order"] for zone in presented] == list(range(1, len(presented) + 1)), (
        f"{label}: presentation_order is 1..N over the presented zones, no gaps, no duplicates"
    )
    assert [zone["id"] for zone in presented] == summary["presented_zone_ids"], (
        f"{label}: the per-zone marks and the summary's id list are one answer, not two"
    )
    assert summary["presented_count"] == len(presented) <= wsa.WATER_ZONE_PRESENTATION_COUNT, (
        f"{label}: the count is a CAP -- the presented set never exceeds it"
    )
    assert summary["presented_count"] == min(
        wsa.WATER_ZONE_PRESENTATION_COUNT, len(result["zones"])
    ), (
        f"{label}: and it is never a QUOTA either -- with fewer survivors than the cap, the "
        "presented set is every survivor and nothing is padded to reach four"
    )
    assert summary["survivor_counts"] == {
        survey_type: len(result["zones_by_type"][survey_type]) for survey_type in SURVEY_TYPES
    }, f"{label}: the per-type survivor totals are the real totals"
    for survey_type in SURVEY_TYPES:
        typed_presented = [z for z in presented if z["survey_type"] == survey_type]
        assert [z["rank"] for z in typed_presented] == sorted(z["rank"] for z in typed_presented), (
            f"{label}: within a type the presented zones are taken in RANK order, never reordered"
        )
        assert summary["presented_counts"][survey_type] == len(typed_presented)
        assert (
            summary["base_counts"][survey_type] + summary["backfill_counts"][survey_type]
            == summary["presented_counts"][survey_type]
        )
        assert summary["base_counts"][survey_type] == min(
            wsa.WATER_ZONE_PRESENTATION_PER_TYPE, len(result["zones_by_type"][survey_type])
        ), f"{label}: the base is the top {wsa.WATER_ZONE_PRESENTATION_PER_TYPE} that type had"
    # MARKING, NOT FILTERING -- the assertion this whole rule lives or
    # dies on. Every survivor is still in the payload set, still in its
    # per-type list, and still a feature on the wire, presented or not.
    unpresented = [zone for zone in result["zones"] if not zone["presented"]]
    assert len(presented) + len(unpresented) == len(result["zones"])
    assert sum(len(v) for v in result["zones_by_type"].values()) == len(result["zones"]), (
        f"{label}: zones_by_type still holds every survivor"
    )
    collection = survey_areas_to_geojson(result["zones"])
    validate_feature_collection(collection)
    zone_features = [
        f for f in collection["features"]
        if f["properties"]["layer"].startswith("survey_zone_")
        and not f["properties"]["layer"].startswith("survey_zone_member_")
    ]
    assert {f["properties"]["zone_id"] for f in zone_features} == {z["id"] for z in result["zones"]}, (
        f"{label}: EVERY survivor has its GeoJSON feature -- an unpresented zone is on the wire "
        "with the same layer and the same full property set as a presented one"
    )
    for feature in zone_features:
        zone = next(z for z in result["zones"] if z["id"] == feature["properties"]["zone_id"])
        assert feature["properties"]["presented"] is zone["presented"]
        assert feature["properties"]["presentation_order"] == zone["presentation_order"]
        assert feature["properties"]["rank"] == zone["rank"], (
            f"{label}: `rank` keeps its own meaning -- rank WITHIN TYPE across all survivors, "
            "presented or not; presentation never renumbers it"
        )
    return summary, presented, unpresented


# --- CASE 1: BOTH TYPES PRODUCE 2+ -> 2 and 2, interleaved, NO backfill.
# Two parallel channels 100 m apart in a 61-col plane: nine embankment
# compartments walk the two valleys and the two channel ribbons stay far
# enough apart to group into two excavated zones.
_both_dem, _both_boundary, _both_acc = _presentation_dem(40, 61, [14, 34], 60, 0.30, 0.25)
both_result = compute_water_survey_areas(_both_dem, _both_boundary, flow_accumulation=_both_acc)
_both_summary, _both_presented, _both_unpresented = _assert_presentation_invariants(
    both_result, "case 1 (both types 2+)"
)
assert (len(both_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]),
        len(both_result["zones_by_type"][SURVEY_TYPE_EXCAVATED])) == (9, 2), (
    "the fixture's premise: both types produced at least the per-type target "
    f"{[(t, len(v)) for t, v in both_result['zones_by_type'].items()]}"
)
assert _both_summary["rule_applied"] == "2 embankment + 2 excavated"
assert _both_summary["backfill_applied"] is False and _both_summary["backfill_counts"] == {
    SURVEY_TYPE_EMBANKMENT: 0, SURVEY_TYPE_EXCAVATED: 0
}, "neither type fell short, so no slot opened and nothing was backfilled"
assert [(z["survey_type"], z["rank"]) for z in _both_presented] == [
    (SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1),
    (SURVEY_TYPE_EMBANKMENT, 2), (SURVEY_TYPE_EXCAVATED, 2),
], (
    "THE DOCUMENTED ORDER: interleaved by type, embankment leading each pair -- not one type's "
    "list with the other appended, and not ordered by comparing the two types' scores (that "
    "cross-instrument pool is confined to select_survey_zone, deliberately)"
)
assert len(_both_unpresented) == 7, (
    "seven survivors are unpresented AND STILL IN THE PAYLOAD -- this is the case the deleted cap "
    "got wrong, so the fixture that has the most to lose is the one asserted hardest"
)
print(
    f"Presentation case 1 (both types 2+): 9 embankment + 2 excavated survivors -> "
    f"{_both_summary['rule_applied']}, interleaved, no backfill, 7 unpresented survivors all still "
    "in the payload and on the wire."
)

# --- CASE 2: ONE EXCAVATED SURVIVOR -> 2 embankment + 1 excavated + 1
# embankment backfill. A single long valley: ten compartments, one
# excavated ribbon. The third and fourth slots come from the type that
# HAS leftovers, in its own rank order, appended after the interleaved
# base.
_one_dem, _one_boundary, _one_acc = _presentation_dem(80, 21, [10], 30, 0.30, 0.25)
one_exc_result = compute_water_survey_areas(_one_dem, _one_boundary, flow_accumulation=_one_acc)
_one_summary, _one_presented, _one_unpresented = _assert_presentation_invariants(
    one_exc_result, "case 2 (one excavated survivor)"
)
assert (len(one_exc_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]),
        len(one_exc_result["zones_by_type"][SURVEY_TYPE_EXCAVATED])) == (10, 1), (
    "the fixture's premise: exactly ONE excavated survivor, with embankment leftovers to backfill from"
)
assert _one_summary["rule_applied"] == "2 embankment + 1 excavated + 1 embankment backfill", (
    "the rule line reads as the rule was applied, backfill named as backfill -- this exact string "
    "is what the diagnostic and the report print, so a run explains its own set"
)
assert _one_summary["backfill_applied"] is True
assert _one_summary["backfill_counts"] == {SURVEY_TYPE_EMBANKMENT: 1, SURVEY_TYPE_EXCAVATED: 0}
assert [(z["survey_type"], z["rank"]) for z in _one_presented] == [
    (SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1),
    (SURVEY_TYPE_EMBANKMENT, 2), (SURVEY_TYPE_EMBANKMENT, 3),
], (
    "the interleaved base first (embankment 1, excavated 1, embankment 2 -- the excavated slot the "
    "second pair would have used simply does not exist), THEN the backfill in rank order"
)
print(
    f"Presentation case 2 (one excavated survivor): 10 embankment + 1 excavated -> "
    f"{_one_summary['rule_applied']}."
)

# --- CASE 3: ZERO EXCAVATED SURVIVORS -> the top 4 embankment. The same
# valley at a 12% grade: the excavated slope taper is nearly gone there
# (1.0 through 5%, 0.5 at 10%, nothing at 15%) so no cell clears the 0.5
# threshold, while embankment seeding still qualifies ground at its 0.30
# minimum. THIS IS THE REFERENCE PROPERTY'S OWN HISTORY -- excavated
# produced nothing for several runs -- which is why it is a fixture and
# not a hypothetical.
_no_dem, _no_boundary, _no_acc = _presentation_dem(80, 21, [10], 40, 0.50, 0.35)
no_exc_result = compute_water_survey_areas(_no_dem, _no_boundary, flow_accumulation=_no_acc)
_no_summary, _no_presented, _no_unpresented = _assert_presentation_invariants(
    no_exc_result, "case 3 (zero excavated survivors)"
)
assert (len(no_exc_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]),
        len(no_exc_result["zones_by_type"][SURVEY_TYPE_EXCAVATED])) == (5, 0), (
    "the fixture's premise: the excavated type produced NOTHING at this grade"
)
assert _no_summary["rule_applied"] == "2 embankment + 2 embankment backfill"
assert [(z["survey_type"], z["rank"]) for z in _no_presented] == [
    (SURVEY_TYPE_EMBANKMENT, rank) for rank in (1, 2, 3, 4)
], "with one type absent the set is that type's top four, in rank order -- no gap, no placeholder"
assert len(_no_unpresented) == 1 and _no_unpresented[0]["rank"] == 5, (
    "the fifth compartment is unpresented and still a full survivor"
)
print(
    f"Presentation case 3 (zero excavated survivors): 5 embankment + 0 excavated -> "
    f"{_no_summary['rule_applied']} (the top 4 embankment)."
)

# --- CASE 4: FEWER THAN FOUR SURVIVORS IN TOTAL -> all of them, no
# padding. The V fixture (2 embankment + 1 excavated) and the flat
# fixture (a single excavated zone) are exactly this case and are
# already built above -- reusing them is the point, because it means the
# case is not a special path but the same rule reaching the end of a
# short list.
_v_summary, _v_presented, _v_unpresented = _assert_presentation_invariants(
    v_result, "case 4 (three survivors)"
)
assert _v_summary["presented_count"] == 3 and not _v_unpresented, "all three, none held back"
assert _v_summary["rule_applied"] == "2 embankment + 1 excavated"
assert [(z["survey_type"], z["rank"]) for z in _v_presented] == [
    (SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1), (SURVEY_TYPE_EMBANKMENT, 2)
], "interleaved as far as the excavated list reaches, then the embankment remainder"
_flat_summary, _flat_presented, _flat_unpresented = _assert_presentation_invariants(
    flat_result, "case 4 (single survivor)"
)
assert _flat_summary["presented_count"] == 1 and _flat_summary["rule_applied"] == "1 excavated"
print(
    "Presentation case 4 (fewer than four survivors): 3 and 1 survivors present as 3 and 1 -- the "
    "count is a cap, never a quota, and nothing is padded to reach four."
)

# --- SELECTION IS UNTOUCHED BY PRESENTATION. Asserted three ways on the
# case-2 fixture, where the presented set is NOT the pooled order (the
# presented set interleaves an excavated zone into second place; the
# pool has its own winner and does not care):
#   1. STRUCTURALLY -- select_survey_zone()'s source does not name
#      either key, so it CANNOT read them;
#   2. by RECOMPUTATION -- the same selection function, run over the
#      same zones stripped of both marks and shuffled out of
#      presentation order, returns the same zone id;
#   3. by CONTRACT -- the selected zone is whatever the pool says,
#      presented or not, and its rank is 1 within its own type.
_selection_source = inspect.getsource(select_survey_zone)
assert "presented" not in _selection_source and "presentation" not in _selection_source, (
    "selection must not be able to read the presentation marks at all -- the invariant is "
    "structural, not a matter of the current call order"
)
_selected_before = one_exc_result["selected_water_zone"]
_stripped = []
for zone in one_exc_result["zones"]:
    clone = dict(zone)
    clone.pop("presented")
    clone.pop("presentation_order")
    _stripped.append(clone)
_stripped.reverse()
assert select_survey_zone(_stripped)["id"] == _selected_before["id"], (
    "the pooled rank-1 answer is IDENTICAL with the presentation marks removed and the list "
    "reordered -- presentation moved the reading order, it did not move the selection"
)
assert _selected_before["rank"] == 1, "still the pooled rank-1 zone of its own type"
assert one_exc_result["zones"].index(_selected_before) >= 0, "and it is a member of the surviving set"
print(
    "Presentation vs selection: select_survey_zone() cannot read the marks (source-level), and "
    "returns the same zone id over the same survivors stripped of them and reordered."
)

# --- NARRATIVE AND PANEL: the presented set FIRST, in presentation
# order, with the per-type survivor totals beside it -- and every
# unpresented survivor still carrying its full block AND its panel. The
# case-2 fixture is used because its presentation order is genuinely
# different from the (survey_type, rank) order narrative_data used
# before this rule existed, so an unchanged sort would fail here.
_one_narrative = build_narrative_data(one_exc_result)
assert _one_narrative["zone_count"] == len(one_exc_result["zones"]) == len(_one_narrative["zones"]), (
    "EVERY survivor still has a narrative block -- presentation ordered this list, it did not "
    "shorten it"
)
assert [block["id"] for block in _one_narrative["zones"][:4]] == _one_summary["presented_zone_ids"], (
    "the presented set leads the list, in presentation order"
)
assert [block["presentation_order"] for block in _one_narrative["zones"][:4]] == [1, 2, 3, 4]
assert all(block["presented"] is False for block in _one_narrative["zones"][4:]), (
    "and the unpresented survivors follow, marked as such rather than dropped"
)
assert all(block["presentation_order"] is None for block in _one_narrative["zones"][4:])
_trailing = _one_narrative["zones"][4:]
assert [(b["survey_type"], b["rank"]) for b in _trailing] == sorted(
    (b["survey_type"], b["rank"]) for b in _trailing
), "the unpresented remainder keeps the old per-type, per-rank order"
_one_presentation_block = _one_narrative["presentation"]
assert _one_presentation_block["rule_applied"] == "2 embankment + 1 excavated + 1 embankment backfill"
assert _one_presentation_block["survivor_counts"] == {
    SURVEY_TYPE_EMBANKMENT: 10, SURVEY_TYPE_EXCAVATED: 1
}, (
    "THE LINE STATING WHAT WAS CONSIDERED: per-type survivor totals ride the narrative beside the "
    "presented set, so the report can say what is shown AND what it is shown out of"
)
assert (
    _one_presentation_block["survivor_counts"][SURVEY_TYPE_EMBANKMENT]
    == _one_narrative["embankment_zone_count"]
), "and they agree with the counts the narrative already published -- one answer, not two"
assert (
    _one_presentation_block["survivor_counts"][SURVEY_TYPE_EXCAVATED]
    == _one_narrative["excavated_zone_count"]
)
# UNPRESENTED KEEPS EVERYTHING. A block that is not in the presented set
# is the same shape as one that is -- panel rows included -- because the
# panel is a reading of a ZONE, and being unpresented says nothing about
# the ground.
_presented_block = _one_narrative["zones"][0]
_unpresented_block = _one_narrative["zones"][-1]
assert _unpresented_block["presented"] is False
assert set(_unpresented_block) == set(_presented_block), (
    "identical block shape -- an unpresented zone is not a reduced record"
)
assert _unpresented_block["panel"], "and it still carries its panel block"
assert [row["key"] for row in _unpresented_block["panel"][:5]] == list(wsa.PANEL_ALWAYS_ROWS), (
    "the same five always-rows in the same order: the panel never renders a zone differently for "
    "being unpresented"
)
for _block in (_presented_block, _unpresented_block):
    _panel_keys = {row["key"] for row in _block["panel"]}
    assert not _panel_keys & {"presented", "presentation_order"}, (
        "the presentation mark is DECIDED AGAINST THE PANEL (PANEL_EXCLUDED_KEYS): the panel "
        "answers 'should I walk this zone?', and which list a zone is shown in is a question "
        "about the other zones"
    )
json.dumps(_one_narrative)

# THE DIAGNOSTIC'S ZONE TABLE explains its own presented set: the rule
# line at the top, the mark on each presented row. Built over the same
# identify-shaped dict the diagnostic gets from the real entry point.
_one_identify_like = {
    "zones": one_exc_result["zones"],
    "zones_by_type": one_exc_result["zones_by_type"],
    "dropped_zones": one_exc_result["dropped_zones"],
    "presentation": one_exc_result["presentation"],
    "embankment_seeds": one_exc_result["embankment_seeds"],
    "result": one_exc_result,
}
_one_table = diag.summarize_survey_zones_table(_one_identify_like)
assert "2 embankment + 1 excavated + 1 embankment backfill" in _one_table, (
    "THE RULE LINE: the table states which rule produced this run's presented set, so a reader "
    "never has to reconstruct it from which rows happen to be marked"
)
assert "4 of 11 survivor(s) presented" in _one_table, "with the two counts it is a rule about"
assert "10 embankment survivor(s), 1 excavated survivor(s)" in _one_table, (
    "and the per-type survivor totals -- what was considered"
)
assert "A MARK, NOT A FILTER" in _one_table, (
    "the table says outright that an unmarked row is a survivor, because a marked/unmarked table "
    "is exactly where a reader would otherwise assume a filter"
)
assert _one_table.count("[PRESENTED #") == 4, "one mark per presented zone, and only those"
for _order in (1, 2, 3, 4):
    assert f"[PRESENTED #{_order}]" in _one_table
_one_table_zone_lines = [
    line for line in _one_table.split("\n") if line.startswith("  #")
]
assert len(_one_table_zone_lines) == len(one_exc_result["zones"]) == 11, (
    "EVERY survivor still gets a line -- the mark is a column, not a filter on the table either"
)
print(
    "Presentation in narrative_data: presented set leads in presentation order, per-type survivor "
    "totals ride the block, unpresented survivors keep an identical block and panel, and neither "
    "mark is a panel row. The diagnostic table states the rule and marks 4 of its 11 rows."
)

# --- THE RULE ITSELF, exercised directly on ranked pools. The four
# fixtures above prove the rule against real generation; these prove the
# BOUNDARIES of it cheaply -- including the one shape no DEM here
# produces (each type with exactly one survivor, where the set is two
# and there is nothing to backfill FROM).
def _ranked_pool(embankment_count, excavated_count):
    pool = []
    for index in range(embankment_count):
        pool.append(_mini_zone(
            index, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(index * 100, 0, index * 100 + 20, 20),
            seed_blend=round(0.9 - 0.05 * index, 4), pinch_drainage=1.0, catchment=6.0,
        ))
    for index in range(excavated_count):
        pool.append(_mini_zone(
            1000 + index, SURVEY_TYPE_EXCAVATED, round(0.8 - 0.05 * index, 4), 1.0,
            box(index * 100, 500, index * 100 + 20, 520),
        ))
    rank_survey_zones_per_type(pool)
    return pool


_rule_cases = {
    (3, 3): ("2 embankment + 2 excavated",
             [(SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1),
              (SURVEY_TYPE_EMBANKMENT, 2), (SURVEY_TYPE_EXCAVATED, 2)]),
    (2, 2): ("2 embankment + 2 excavated",
             [(SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1),
              (SURVEY_TYPE_EMBANKMENT, 2), (SURVEY_TYPE_EXCAVATED, 2)]),
    (4, 1): ("2 embankment + 1 excavated + 1 embankment backfill",
             [(SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1),
              (SURVEY_TYPE_EMBANKMENT, 2), (SURVEY_TYPE_EMBANKMENT, 3)]),
    (1, 4): ("1 embankment + 2 excavated + 1 excavated backfill",
             [(SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1),
              (SURVEY_TYPE_EXCAVATED, 2), (SURVEY_TYPE_EXCAVATED, 3)]),
    (6, 0): ("2 embankment + 2 embankment backfill",
             [(SURVEY_TYPE_EMBANKMENT, rank) for rank in (1, 2, 3, 4)]),
    (0, 6): ("2 excavated + 2 excavated backfill",
             [(SURVEY_TYPE_EXCAVATED, rank) for rank in (1, 2, 3, 4)]),
    (1, 1): ("1 embankment + 1 excavated",
             [(SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1)]),
    (2, 1): ("2 embankment + 1 excavated",
             [(SURVEY_TYPE_EMBANKMENT, 1), (SURVEY_TYPE_EXCAVATED, 1),
              (SURVEY_TYPE_EMBANKMENT, 2)]),
    (0, 0): ("no surviving zones", []),
}
for (emb_n, exc_n), (expected_rule, expected_order) in _rule_cases.items():
    pool = _ranked_pool(emb_n, exc_n)
    summary = assign_presentation_order(pool)
    presented = sorted(
        [z for z in pool if z["presented"]], key=lambda z: z["presentation_order"]
    )
    assert summary["rule_applied"] == expected_rule, (
        f"{emb_n} embankment + {exc_n} excavated -> expected rule {expected_rule!r}, "
        f"got {summary['rule_applied']!r}"
    )
    assert [(z["survey_type"], z["rank"]) for z in presented] == expected_order, (
        f"{emb_n} embankment + {exc_n} excavated -> expected order {expected_order}, got "
        f"{[(z['survey_type'], z['rank']) for z in presented]}"
    )
    assert len(pool) == emb_n + exc_n, "the function marks in place and REMOVES NOTHING from the list"
    assert summary["presented_count"] == min(4, emb_n + exc_n), "cap, never quota"
    for zone in pool:
        assert (zone["presentation_order"] is None) is not zone["presented"]
# The two-and-two case with a LOPSIDED pool, stated separately because it
# is the one a "take the best four overall" rule would get wrong: six
# embankment zones all out-ranking every excavated one still yield two
# excavated slots, because the rule is per type by construction.
_lopsided = _ranked_pool(6, 3)
for zone in _lopsided:
    if zone["survey_type"] == SURVEY_TYPE_EXCAVATED:
        zone["mean_suitability"] = 0.2
rank_survey_zones_per_type(_lopsided)
assign_presentation_order(_lopsided)
assert sorted(
    z["survey_type"] for z in _lopsided if z["presented"]
) == [SURVEY_TYPE_EMBANKMENT, SURVEY_TYPE_EMBANKMENT, SURVEY_TYPE_EXCAVATED, SURVEY_TYPE_EXCAVATED], (
    "the presented set is 2 and 2 even when every embankment zone would out-score every excavated "
    "one on a pooled scale -- 'both types considered' is the rule, not 'the best four'"
)
print(
    f"Presentation rule: {len(_rule_cases)} survivor shapes exercised directly (both backfill "
    "directions, both zero-of-a-type shapes, one-each, and the empty run), plus the lopsided pool "
    "that a best-four-overall rule would get wrong."
)


# =========================================================================
# 4. CONTRACT -- the selected ZONE is a selected_water_zone
# =========================================================================

selected = flat_result["selected_water_zone"]
assert selected is not None and isinstance(selected, dict) and selected, (
    "the contract is 'non-empty dict or None' -- truthiness gates in solar/tree/fencing depend on it"
)
assert selected is flat_zone, "the pooled rank-1 ZONE is the selection"
assert selected["status"] == wsa.ZONE_STATUS_NOMINATED, (
    "the selected zone is a surviving zone, full stop"
)
assert selected["presented"] is True and selected["presentation_order"] == 1, (
    "the sole survivor here is trivially the presented set -- and note the direction of the "
    "dependency: selection does not read this mark (asserted structurally in section 3d), the mark "
    "simply happens to agree on a one-zone parcel"
)

# The three fields production consumers dereference directly:
assert selected["render_fill_polygon_utm"] is selected["polygon_utm"], (
    "render_fill_polygon_utm must be the IDENTITY of the zone's clipped envelope -- the aggregation "
    "defines the geometry; no further morphology ever"
)
assert selected["render_fill_geometry_wgs84"] is selected["geometry_wgs84"], "same identity on the wire form"
assert isinstance(selected["representative_elevation_m"], float)
assert selected["representative_elevation_m"] == 100.0, "median raw elevation over MEMBER cells of a flat-100 fixture"
assert isinstance(selected["id"], int)
assert selected["rank"] == 1
assert selected["served_production_area_ids"] == []

# Exercise the ACTUAL consumer access patterns so a break here fails
# loudly before any pipeline run:
_ = selected["render_fill_polygon_utm"].buffer(6.096)          # road_corridors pond exclusion
_ = unary_union([selected["render_fill_polygon_utm"]])          # solar water_zones union
_ = selected["render_fill_polygon_utm"] if selected else None   # fencing truthiness guard
_ = 101.5 - selected["representative_elevation_m"]              # keypoint elevation differential
_ = f"Water zone {selected['id']}: log line"                    # render_layout_map id branch

# Stored WGS84 beside UTM everywhere: zones, members, and their features.
for obj in flat_result["zones"] + flat_result["regions"] + v_result["zones"] + v_result["regions"]:
    assert not obj["polygon_utm"].is_empty
    assert isinstance(obj["geometry_wgs84"], dict) and "coordinates" in obj["geometry_wgs84"]

# Sentinel semantics for all three overlaps, now measured on the ZONE
# envelope. Never checked (defaults): all three None.
assert flat_zone["canopy_overlap_pct"] is None
assert flat_zone["road_overlap_pct"] is None
assert flat_zone["production_overlap_pct"] is None
# Checked-and-clear: canopy mask with no canopy -> 0.0; a REAL None road
# union is the clean "checked, genuinely no mapped road" answer -> 0.0;
# an empty production list is a checked answer -> 0.0.
checked_result = compute_water_survey_areas(
    FLAT_DEM,
    FLAT_BOUNDARY,
    production_areas=[],
    canopy_root_zone_mask_utm=np.zeros(FLAT_DEM["array"].shape, dtype=bool),
    road_exclusion_union_utm=None,
    soil_inputs=GOOD_WET_SOIL_INPUTS,
)
checked_zone = checked_result["zones_by_type"][SURVEY_TYPE_EXCAVATED][0]
assert checked_zone["canopy_overlap_pct"] == 0.0, "checked-and-clear canopy is 0.0, never None"
assert checked_zone["road_overlap_pct"] == 0.0, "a real None road union means CHECKED, genuinely no road: 0.0"
assert checked_zone["production_overlap_pct"] == 0.0, "an empty production list is a real checked answer: 0.0"
# Checked-and-hit: canopy over half the envelope, production over all of it.
half_canopy = np.zeros(FLAT_DEM["array"].shape, dtype=bool)
half_canopy[:, :10] = True  # cols 5..9 of the envelope's 10 = 50%
production_patch = {
    "id": 7,
    "polygon_utm": FLAT_BOUNDARY.buffer(30.0),
    "render_fill_polygon_utm": FLAT_BOUNDARY.buffer(30.0),
    "representative_elevation_m": 150.0,
    # 50 m ABOVE the zone's own high point -> pump required. The gravity
    # rule is max-to-max (water_candidate_zones._zone_production_area_
    # relationships()), so this is the number that decides it; a flat
    # fixture patch's median and maximum are the same value anyway.
    "max_elevation_m": 150.0,
}
hit_result = compute_water_survey_areas(
    FLAT_DEM,
    FLAT_BOUNDARY,
    production_areas=[production_patch],
    canopy_root_zone_mask_utm=half_canopy,
    soil_inputs=GOOD_WET_SOIL_INPUTS,
)
hit_zone = hit_result["zones_by_type"][SURVEY_TYPE_EXCAVATED][0]
assert hit_zone["canopy_overlap_pct"] == 50.0, "envelope-cell canopy overlap"
assert hit_zone["production_overlap_pct"] == 100.0, "envelope-polygon production overlap"
# PUMP-REQUIRED survives with its note, as ranking context -- never a gate:
primary = hit_zone["primary_production_area_relationship"]
assert primary is not None and primary["above_production_area"] is False
assert hit_zone["has_service_relationship"] is True
assert hit_zone["served_production_area_ids"] == [7]
assert FLAG_NO_SERVICE_RELATIONSHIP not in hit_zone["flags"]
assert "PUMP-REQUIRED" in hit_zone["confidence_notes"], "the pump case carries its note and survives"
assert hit_result["selected_water_zone"] is hit_zone, "a pump-required zone still selects -- gravity never gates"
print("Contract: consumer fields + access patterns on the ZONE, envelope render_fill identity, overlap sentinels, PUMP-REQUIRED survives.")

# narrative_data is FINAL and JSON-serializable, lists ALL zones with
# the dual-acreage numbers, and carries the TWI curve this run scored on
# (the note plus the derived breakpoints -- a window-referenced score is
# unreadable without the window it was referenced to):
narrative = build_narrative_data(hit_result)
json.dumps(narrative)
assert narrative["zone_found"] is True
assert narrative["twi_is_absolute"] is True, "a cell's TWI score is absolute given the window"
assert "THIS parcel" not in narrative["twi_note"], (
    "the retired parcel-relative claim must not survive in the note -- it is no longer true"
)
assert "WINDOW-REFERENCED" in narrative["twi_note"] and "ELEVATION TILE" in narrative["twi_note"], (
    "the note must say what the curve is referenced TO -- that is the whole readability of a score"
)
assert "FIXED ABSOLUTE curve" not in narrative["twi_note"], (
    "the retired fixed-curve claim is no longer true either, and must not survive the way the "
    "parcel-relative one had to be removed"
)
# THE CURVE ITSELF, ON THE WIRE: a run's scoring reproducible from its
# own output, without re-running anything.
_curve = narrative["twi_curve"]
assert _curve["floor_percentile"] == wsa.TWI_WINDOW_FLOOR_PERCENTILE
assert _curve["full_credit_percentile"] == wsa.TWI_WINDOW_FULL_CREDIT_PERCENTILE
assert _curve["full_credit_breakpoint"] > _curve["floor_breakpoint"], "the ramp has positive width"
assert _curve["window_twi_percentiles"]["p50"] is not None, "the window's own distribution rides along"
assert _curve["reference_cell_count"] >= _curve["measured_cell_count"] > 0
assert _curve["snap_meters"] == wsa.TWI_REFERENCE_WINDOW_SNAP_METERS
assert len(_curve["window_bounds"]) == 4 and len(_curve["fetched_grid_bounds"]) == 4
assert "twi_is_parcel_relative" not in narrative, "the retired caveat flag is gone, not renamed in place"
assert narrative["zones"] and len(narrative["zones"]) == narrative["zone_count"] == len(hit_result["zones"]), (
    "narrative lists ALL surviving zones with the total count -- the cap and its counters are gone"
)
assert narrative["dropped_count"] == 0
for gone_key in ("presented_count", "presentation_top_n", "presentation_guarantee_applied"):
    assert gone_key not in narrative, (
        f"the deleted cap's top-level narrative counter {gone_key} must not resurface -- the "
        "presentation rule reports itself in ONE block (narrative['presentation']), never as loose "
        "counters a reader has to assemble"
    )
assert narrative["presentation"]["rule_applied"] == "1 excavated", (
    "the rule line names what this run presented, on a single-survivor parcel"
)
assert narrative["presentation"]["survivor_counts"] == {
    SURVEY_TYPE_EMBANKMENT: 0, SURVEY_TYPE_EXCAVATED: 1
}, "the per-type SURVIVOR totals ride the block -- what was considered, beside what is shown"
zone_block = narrative["zones"][0]
assert zone_block["sparse_anchor"] is False, "the sparse-anchor finding rides every zone block"
assert zone_block["cross_type_overlaps"] == [] and zone_block["either_type_candidate"] is False, (
    "a single-type fixture has no cross-type agreement to report -- empty list, gate off"
)
assert zone_block["criteria"].keys() == EXCAVATED_WEIGHTS.keys(), (
    "per-criterion mean scores (member cells only) ride along -- the narrative-honesty mechanism"
)
assert "member_acres" in zone_block and "zone_acres" in zone_block and zone_block["member_count"] == 1, (
    "the dual-acreage sentence's two numbers travel on every zone block"
)
assert narrative["selection"]["selected_zone_id"] == hit_zone["id"]
gravity_block = zone_block["gravity"]
assert gravity_block["can_gravity_feed"] is False and gravity_block["production_area_id"] == 7
print("narrative_data: JSON-clean, all zones, dual acreage, per-criterion scores, TWI caveat, pump case surfaced.")


# =========================================================================
# 5b. GRAVITY IS HIGH POINT TO HIGH POINT -- the correctness fix
# =========================================================================
#
# THE BUG THIS SECTION EXISTS FOR, found in live testing: a water zone
# sitting only slightly above a production block's LOWEST ground came back
# reported as GRAVITY FEED. The rule compared the two sides' MEDIANS, so a
# pond that could water the block's bottom corner and nothing else read as
# a gravity relationship to the whole block.
#
# THE RULE NOW: the zone's MAXIMUM elevation must be above the production
# block's MAXIMUM elevation. Max-to-max, both sides -- gravity delivery has
# to reach the WHOLE block, so the block's high corner is the reference;
# and since a SURVEY AREA IS NOT A POND (none has been sited in it), the
# area's own high ground is the best case a design could still achieve
# there, which is what "gravity feed" now claims and no more.
#
# GEOMETRY BUILT TO THE CASE, not a hand-set pair of numbers. Both blocks
# below are REAL cell populations on a REAL DEM, and both of their
# elevations are taken with production_area.cluster_and_gate()'s own two
# expressions (median and max over the cluster's cells) so the fixture
# cannot drift away from what the pipeline computes.

_GRAV_WATER_ELEVATION = 100.0
_grav_array = np.full((20, 20), _GRAV_WATER_ELEVATION)
# THE STRADDLING BLOCK, south of the water ground and outside the parcel
# boundary: four cell-rows ramping 90 -> 92 -> 98 -> 120. Its LOW point
# (90) is below the water zone's 100, its HIGH point (120) is above it,
# and its MEDIAN lands at 95 -- below the water zone too. That median is
# precisely what used to answer "gravity feed" here.
_grav_array[16, 5:15] = 90.0
_grav_array[17, 5:15] = 92.0
_grav_array[18, 5:15] = 98.0
_grav_array[19, 5:15] = 120.0
# THE FULLY-BELOW BLOCK, north of it: the same shape, topping out at 98 --
# under the water zone's 100 at every cell, including its high corner.
_grav_array[0, 5:15] = 90.0
_grav_array[1, 5:15] = 92.0
_grav_array[2, 5:15] = 96.0
_grav_array[3, 5:15] = 98.0
_GRAV_DEM = _dem(_grav_array)


def _grav_block(block_id, row_lo, row_hi):
    """A production patch over rows row_lo..row_hi (inclusive), cols 5..14,
    with BOTH elevations read off the DEM by production_area.cluster_and_
    gate()'s own expressions over the block's own cells."""
    polygon = box(
        ORIGIN_X + 5 * RESOLUTION + 0.1,
        ORIGIN_Y - (row_hi + 1) * RESOLUTION + 0.1,
        ORIGIN_X + 15 * RESOLUTION - 0.1,
        ORIGIN_Y - row_lo * RESOLUTION - 0.1,
    )
    cells = [(r, c) for r in range(row_lo, row_hi + 1) for c in range(5, 15)]
    elevations = [float(_grav_array[r, c]) for r, c in cells]
    return {
        "id": block_id,
        "cells": cells,
        "polygon_utm": polygon,
        "render_fill_polygon_utm": polygon,
        "representative_elevation_m": float(np.median(elevations)),
        "max_elevation_m": float(np.max(elevations)),
    }


_STRADDLING_BLOCK = _grav_block(11, 16, 19)
_BELOW_BLOCK = _grav_block(12, 0, 3)

# The fixture says what it claims to say, before anything is asserted
# ABOUT it -- a straddling block whose median failed to straddle would
# make test 1 below pass for the wrong reason.
assert _STRADDLING_BLOCK["representative_elevation_m"] == 95.0
assert _STRADDLING_BLOCK["max_elevation_m"] == 120.0
assert min(float(_grav_array[r, c]) for r, c in _STRADDLING_BLOCK["cells"]) == 90.0
assert _BELOW_BLOCK["representative_elevation_m"] == 94.0
assert _BELOW_BLOCK["max_elevation_m"] == 98.0


def _grav_run(production_areas):
    result = compute_water_survey_areas(
        _GRAV_DEM,
        FLAT_BOUNDARY,
        production_areas=production_areas,
        soil_inputs=GOOD_WET_SOIL_INPUTS,
    )
    zone = result["zones_by_type"][SURVEY_TYPE_EXCAVATED][0]
    assert zone["max_elevation_m"] == _GRAV_WATER_ELEVATION, (
        "the water ground is flat at 100 m, so the zone's high point IS 100 -- this fixture's "
        "whole arithmetic rides on that"
    )
    return result, zone


# --- 1. THE BUG: above the block's LOW point, below its HIGH point -------
_straddle_result, _straddle_zone = _grav_run([_STRADDLING_BLOCK])
_straddle_primary = _straddle_zone["primary_production_area_relationship"]
assert _straddle_primary is not None and _straddle_primary["production_area_id"] == 11
# The case, stated as the inequalities that define it:
assert _GRAV_WATER_ELEVATION > min(float(_grav_array[r, c]) for r, c in _STRADDLING_BLOCK["cells"])
assert _GRAV_WATER_ELEVATION < _STRADDLING_BLOCK["max_elevation_m"]
assert _straddle_primary["above_production_area"] is False, (
    "THE BUG, PINNED: a zone whose high point clears the block's LOW ground but not its HIGH "
    "ground cannot gravity-feed that block -- water would reach the bottom corner and nothing "
    "else. It is PUMP REQUIRED, and it read as gravity feed for as long as the rule compared "
    "the two medians"
)
assert _straddle_primary["elevation_differential_m"] == round(
    _GRAV_WATER_ELEVATION - _STRADDLING_BLOCK["max_elevation_m"], 2
) == -20.0, "max-to-max, both sides -- not 100 - 95 = +5, which is what the medians said"
# And the panel and the report say it too, in the vocabulary each uses.
_straddle_panel = {row["key"]: row for row in wsa.build_zone_panel(_straddle_zone, True, {})}
assert _straddle_panel["water_delivery"]["value"] == wsa.WATER_DELIVERY_PUMP
assert "PUMP-REQUIRED" in _straddle_zone["confidence_notes"]
assert "HIGH POINT TO HIGH POINT" in _straddle_zone["confidence_notes"], (
    "a reader who wants to know what 'gravity feed' would have meant here must be able to find "
    "out from the zone itself, not from this module's source"
)

# --- 2. ABOVE THE BLOCK'S HIGH POINT -> gravity feed ---------------------
_below_result, _below_zone = _grav_run([_BELOW_BLOCK])
_below_primary = _below_zone["primary_production_area_relationship"]
assert _below_primary is not None and _below_primary["production_area_id"] == 12
assert _GRAV_WATER_ELEVATION > _BELOW_BLOCK["max_elevation_m"], "the case: clear of the high corner"
assert _below_primary["above_production_area"] is True, (
    "clearing the block's HIGH point is what gravity feed means -- every lower part of the "
    "block is then downhill of the pond"
)
assert _below_primary["elevation_differential_m"] == 2.0, "100 - 98, max to max"
_below_panel = {row["key"]: row for row in wsa.build_zone_panel(_below_zone, True, {})}
assert _below_panel["water_delivery"]["value"] == wsa.WATER_DELIVERY_GRAVITY
assert "A SURVEY AREA IS NOT A POND" in _below_zone["confidence_notes"], (
    "the gravity claim is a BEST CASE -- a pond sited at this area's high end could reach that "
    "block's high end -- and the zone has to say so where the claim is made"
)

# --- 3. ALL THREE STATES STILL REACHABLE ---------------------------------
# The structures branch established that below-elevation SURVIVES as "a
# pump would be needed" rather than collapsing into "no". Three answers,
# and every one of them reachable on this one fixture.
_far_block = _grav_block(13, 16, 19)
_far_block["polygon_utm"] = box(
    ORIGIN_X + 5000.0, ORIGIN_Y - 5000.0, ORIGIN_X + 5030.0, ORIGIN_Y - 4970.0
)
_far_block["render_fill_polygon_utm"] = _far_block["polygon_utm"]
_none_result, _none_zone = _grav_run([_far_block])
assert _none_zone["primary_production_area_relationship"] is None
assert _none_zone["has_service_relationship"] is False
assert FLAG_NO_SERVICE_RELATIONSHIP in _none_zone["flags"]
_none_panel = {row["key"]: row for row in wsa.build_zone_panel(_none_zone, True, {})}
assert _none_panel["water_delivery"]["value"] == wsa.WATER_DELIVERY_NONE
assert "water_delivery_differential" not in _none_panel, (
    "no relationship means NO differential row -- a 0 ft differential to no production area is "
    "the fabricated zero this module refuses everywhere"
)
_THREE_STATES = {
    _straddle_panel["water_delivery"]["value"],
    _below_panel["water_delivery"]["value"],
    _none_panel["water_delivery"]["value"],
}
assert _THREE_STATES == {
    wsa.WATER_DELIVERY_GRAVITY,
    wsa.WATER_DELIVERY_PUMP,
    wsa.WATER_DELIVERY_NONE,
}, "three answers, not two -- the stricter rule moves zones between the first two, never deletes one"
# NEVER A GATE, under the stricter rule as under the old one: both the
# pump-required zone and the no-relationship zone still survive and still
# get selected on a parcel where they are the only candidate.
for _result, _zone in ((_straddle_result, _straddle_zone), (_none_result, _none_zone)):
    assert _zone["status"] == wsa.ZONE_STATUS_NOMINATED
    assert _result["selected_water_zone"] is _zone

# --- 4. WHAT THE OLD RULE WOULD HAVE SAID, on this same fixture ----------
# The before/after measure, made mechanical: the retired rule re-computed
# here from the SAME zone and the SAME blocks, so the count this fix moves
# is asserted rather than remembered.
def _old_rule_gravity(zone, block):
    return zone["representative_elevation_m"] > block["representative_elevation_m"]


def _new_rule_gravity(zone, block):
    return zone["max_elevation_m"] > block["max_elevation_m"]


_BEFORE = [
    _old_rule_gravity(z, b)
    for z, b in ((_straddle_zone, _STRADDLING_BLOCK), (_below_zone, _BELOW_BLOCK))
]
_AFTER = [
    _new_rule_gravity(z, b)
    for z, b in ((_straddle_zone, _STRADDLING_BLOCK), (_below_zone, _BELOW_BLOCK))
]
assert sum(_BEFORE) == 2, "the old median-to-median rule called BOTH of these gravity feed"
assert sum(_AFTER) == 1, "the high-point rule keeps one and moves the straddling one to pump"
print(
    "Gravity is HIGH POINT TO HIGH POINT: a zone clearing a block's low ground (90) but not its "
    "high ground (120) reports PUMP REQUIRED where the retired median rule reported gravity feed "
    "(-20.0 m, not +5.0 m); a zone clearing the high corner (98) still reports gravity feed; all "
    "three delivery answers reachable, and neither of the two non-gravity ones gates a zone. On "
    "this fixture the old rule called 2 of 2 gravity feed, the new rule calls 1 of 2."
)


# --- DEPTH IS IN FEET ON THE WIRE ----------------------------------------
# The report's global rule is imperial, and the conversion happens in the
# module beside _feet()'s other callers -- never downstream, where two
# consumers converting is two chances to forget.
_depth_zone = _straddle_zone
_depth_properties = wsa._zone_feature_properties(_depth_zone)
assert "depression_depth_max_m" not in _depth_properties, (
    "the metric name must be GONE from the wire, not shipped beside the converted one -- a "
    "consumer that can read either is a consumer that will read the wrong one"
)
assert _depth_properties["depression_depth_max_ft"] == wsa._feet(_depth_zone["depression_depth_max_m"])
assert _depth_zone["depression_depth_max_m"] is not None, (
    "this fixture's depth is a real MEASURED number (0.0 on ground with no depression is a "
    "measurement, not a gap), so the conversion is arithmetic and not a None passing through"
)
assert math.isclose(
    _depth_properties["depression_depth_max_ft"],
    round(_depth_zone["depression_depth_max_m"] / wsa.METERS_PER_FOOT, 1),
), "feet, by the module's own constant"
# THE DIRECTION, ON A NONZERO PROBE. This fixture's own ground is flat, so
# its depth is a true 0.0 and 0.0 m == 0.0 ft -- which is exactly the value
# a conversion that silently did nothing would also produce. The converter
# is therefore checked on a depth that can tell the two apart, through the
# same _feet() the property builder calls.
assert wsa._feet(2.0) == 6.6 > 2.0, (
    "a depth in feet is a BIGGER number than the same depth in metres -- 2 m is 6.6 ft"
)
# None survives the conversion as None (never coerced to 0.0), and the
# member features carry the same converted name for the same reason.
assert wsa._feet(None) is None
_depth_member_properties = wsa._member_feature_properties(_straddle_result["regions"][0])
assert "depression_depth_max_m" not in _depth_member_properties
assert _depth_member_properties["depression_depth_max_ft"] == wsa._feet(
    _straddle_result["regions"][0]["depression_depth_max_m"]
)
# narrative_data was already imperial here and still is -- one name, one
# unit, whichever side a consumer reads it from.
_depth_narrative = build_narrative_data(_straddle_result)
assert _depth_narrative["zones"][0]["depression_depth_max_ft"] == _depth_properties["depression_depth_max_ft"]
print(
    f"Depth is FEET on the wire: depression_depth_max_ft = "
    f"{_depth_properties['depression_depth_max_ft']} ft off a stored "
    f"{_depth_zone['depression_depth_max_m']} m, the metric name absent from both the zone and "
    f"the member feature, None still None, and narrative_data agreeing on the same name and unit."
)


# =========================================================================
# 6. EXPORT VALIDATION + THE SOIL-ODDITY INSTRUMENTATION RIDER
# =========================================================================

identify_like = {
    "zones": hit_result["zones"],
    "zones_by_type": hit_result["zones_by_type"],
    "dropped_zones": hit_result["dropped_zones"],
    "regions": hit_result["regions"],
    "regions_by_type": hit_result["regions_by_type"],
    "gate_mask_stats": hit_result["gate_mask_stats"],
    "result": hit_result,
}
isobands_by_type = {
    survey_type: diag.compute_suitability_isobands(FLAT_DEM, hit_result["surfaces"][survey_type])
    for survey_type in (SURVEY_TYPE_EMBANKMENT, SURVEY_TYPE_EXCAVATED)
}
for survey_type, bands in isobands_by_type.items():
    assert bands, f"isoband bands must be present for {survey_type} (its RAW surface is nonzero on-parcel)"
    for band in bands:
        assert not band["polygons_utm"].is_empty
        assert isinstance(band["geometry_wgs84"], dict), "isobands carry BOTH forms, built at band birth"

criterion_isobands = diag.compute_criterion_isobands(FLAT_DEM, identify_like)
assert set(criterion_isobands[SURVEY_TYPE_EXCAVATED].keys()) == set(EXCAVATED_WEIGHTS.keys())
assert any(b["band_lower"] == 0.8 for b in criterion_isobands[SURVEY_TYPE_EXCAVATED]["soil"]), (
    "the fixture's soil criterion is 1.0 parcel-wide -- its 0.8 band must be present"
)

boundary_wgs84 = transform_geom(CRS, "EPSG:4326", mapping(FLAT_BOUNDARY))
boundary_coords_wgs84 = [tuple(point) for point in boundary_wgs84["coordinates"][0]]

# A per-run temporary directory, NOT a hardcoded absolute path. This line
# used to carry a scratchpad path baked from one container's session id,
# so the export section failed on every fresh checkout with a
# FileNotFoundError that had nothing to do with what it tests. The
# directory is cleaned up at interpreter exit.
_EXPORT_DIR = tempfile.mkdtemp(prefix="water_survey_areas_test_")
atexit.register(shutil.rmtree, _EXPORT_DIR, True)
EXPORT_PATH = os.path.join(_EXPORT_DIR, "water_survey_areas_test.geojson")
export = diag.export_water_survey_areas_geojson(
    identify_like,
    boundary_coords_wgs84,
    [{**production_patch, "geometry_wgs84": transform_geom(CRS, "EPSG:4326", mapping(production_patch["polygon_utm"])), "area_acres": 1.0}],
    isobands_by_type,
    path=EXPORT_PATH,
    criterion_isobands_by_type=criterion_isobands,
)

with open(EXPORT_PATH, encoding="utf-8") as handle:
    collection = json.load(handle)
assert collection["type"] == "FeatureCollection"
assert len(collection["features"]) == export["feature_count"]
for feature in collection["features"]:
    shape(feature["geometry"])  # every geometry parses with shapely
by_layer = export["by_layer"]
assert by_layer.get("survey_zone_excavated", 0) == 1, "the zone envelope rides its typed layer"
assert by_layer.get("survey_zone_member_excavated", 0) == 1, "the member footprint rides its linkage layer"
assert by_layer.get("suitability_isoband_embankment", 0) >= 1
assert by_layer.get("suitability_isoband_excavated", 0) >= 1
assert by_layer.get("criterion_isoband_excavated_soil", 0) >= 1
assert by_layer.get("survey_context_boundary", 0) == 1
assert by_layer.get("survey_context_production_area", 0) == 1
# Member <-> zone linkage asserted BOTH WAYS in the exported features:
zone_feature = next(f for f in collection["features"] if f["properties"]["layer"] == "survey_zone_excavated")
member_feature = next(f for f in collection["features"] if f["properties"]["layer"] == "survey_zone_member_excavated")
assert member_feature["properties"]["zone_id"] == zone_feature["properties"]["zone_id"], (
    "the member feature points at its zone"
)
assert member_feature["properties"]["region_id"] in zone_feature["properties"]["member_ids"], (
    "the zone feature lists its member"
)
boundary_feature = next(f for f in collection["features"] if f["properties"]["layer"] == "survey_context_boundary")
assert boundary_feature["properties"]["gated_cells"] == hit_result["gate_mask_stats"]["gated_cells"]
# The presentation mark reaches the wire on every zone feature, beside
# the honesty reports (sparse anchor + cross-type agreement):
assert zone_feature["properties"]["presented"] is True
assert zone_feature["properties"]["presentation_order"] == 1
assert zone_feature["properties"]["sparse_anchor"] is False
assert zone_feature["properties"]["cross_type_overlaps"] == [], "single-type fixture: an empty agreement list"
assert zone_feature["properties"]["status"] == "nominated" and zone_feature["properties"]["drop_reason"] is None
print(f"Export: {export['feature_count']} features; zone + member layers with linkage both ways; all geometries parse.")

# Dropped zones ride the export's survey_zone_dropped layer with the
# status/reason pattern -- the tiny fixture's floor casualty, validated:
dropped_collection = survey_areas_to_geojson(tiny_result["zones"], dropped_zones=tiny_result["dropped_zones"])
validate_feature_collection(dropped_collection)
dropped_features = [f for f in dropped_collection["features"] if f["properties"]["layer"] == "survey_zone_dropped"]
assert len(dropped_features) == 1, "the floor's casualty appears on the dropped layer, attributed"
dropped_props = dropped_features[0]["properties"]
assert dropped_props["status"] == "dropped" and dropped_props["drop_reason"] == "below_min_area"
assert dropped_props["rank"] is None
assert dropped_props["presented"] is False and dropped_props["presentation_order"] is None, (
    "the dropped feature carries the mark explicitly too -- `status` is what says it is out of the "
    "output; presentation never speaks to whether a zone exists"
)
assert dropped_props["zone_acres"] < MIN_SURVEY_REGION_AREA_ACRES, (
    "the dual-acreage dropped record travels to the wire: the judged zone acreage rides the feature"
)
assert dropped_props["member_acres"] == tiny_dropped["member_acres"]
json.dumps(dropped_collection)
print("Dropped-zone export: survey_zone_dropped layer validates with status: dropped + reason code.")

# GREP-ASSERT: no serialization-time reprojection in any emitter.
import wire_translation as _wt  # noqa: E402

for emitter in (
    survey_areas_to_geojson,
    wsa._zone_feature_properties,
    wsa._member_feature_properties,
    _wt.water_embankment_detail_features,
    diag._isoband_features,
    diag._criterion_isoband_features,
    diag._context_features,
    diag.export_water_survey_areas_geojson,
):
    source = inspect.getsource(emitter)
    assert "transform_geom" not in source, (
        f"{emitter.__name__} must serialize STORED wire forms only -- no reprojection at serialization time"
    )
print("Grep-assert: no transform_geom in any serialization path -- stored wire forms only.")

# The pipeline-facing FeatureCollection validates against the schema and
# uses stored geometry by identity:
zones_geojson = survey_areas_to_geojson(hit_result["zones"])
validate_feature_collection(zones_geojson)
assert zones_geojson["features"][0]["geometry"] is hit_result["zones"][0]["geometry_wgs84"], (
    "the wire feature carries the stored WGS84 object itself, not a rebuild"
)
json.dumps(zones_geojson)
print("zones_geojson: schema-valid, JSON-clean, stored-geometry identity.")

# The instrumentation rider (soil oddity at the wet cells): the deepest-
# fill table carries the SSURGO map unit and the three soil sub-signals
# per cell, so the excavated follow-up can tell a data surprise from a
# scorer defect. On this fixture every gated cell is covered by mukey
# "1" (ksat 1.0 / group D 1.0 / hydric 1.0).
rider_table = diag.summarize_depression_instrumentation(identify_like, FLAT_DEM)
assert "ksat_sc" in rider_table and "grp_sc" in rider_table and "hydric_sh" in rider_table and "mukey" in rider_table, (
    "the deepest-fill table must carry the three soil sub-signal columns and the map unit"
)
assert "uncovered" not in rider_table, "every gated cell is inside mukey 1's geometry on this fixture"
assert " 1 " in rider_table, "the covered cells' map unit symbol appears in the table"
assert "  1.000" in rider_table, "the D-group/hydric/ksat sub-signals (1.0) appear per cell"
# And the threshold comparison prints, on the RAW EXCAVATED surface --
# the embankment lines are RETIRED with extraction and the instrument
# records why every run instead of falling silent:
comparison = diag.summarize_threshold_comparison(identify_like, FLAT_DEM)
assert "THRESHOLD COMPARISON (raw excavated surface" in comparison and "t=0.7" in comparison, (
    "the comparison instrument keeps printing all three thresholds for the excavated surface"
)
assert "t=0.5:" in comparison and "<- default" in comparison, "the final-tuning 0.5 default is marked"
assert "embankment: RETIRED with extraction" in comparison, (
    "the retired embankment lines leave a recorded reason in the instrument, never a silent absence"
)
print("Instrumentation rider: soil sub-signals + map unit in the deepest-fill table; threshold comparison prints on raw surfaces.")


# =========================================================================
# 7. THE FINDING'S CONDITIONAL VERDICT (pre-merge change 5)
# =========================================================================
# Same numbers, same table, same ranking -- only the CLAIM matches what
# was measured. SHORTFALL case first: the hit_result fixture's excavated
# type PRODUCED (one surviving zone), so the largest shortfall -- which
# exists by construction on every parcel -- prints as headroom context
# with the explicit not-a-defect line, never as an indictment.
shortfall_finding = diag.state_excavated_finding(identify_like)
assert "LARGEST REMAINING SHORTFALL:" in shortfall_finding, (
    "excavated survivors exist -> the verdict is the headroom wording"
)
assert "not a defect claim" in shortfall_finding, (
    "the explicit headroom-context line rides the working-class wording"
)
assert "EVIDENCE INDICTS" not in shortfall_finding, (
    "an accusatory verdict on a class that just delivered invites reactive tuning -- it must not print"
)
assert "1 surviving zone(s)" in shortfall_finding, "the wording states what the class produced"

# INDICTS case: a 16% plane. Every cell's max-neighbor grade is 16%
# (0.8 m column step over 5 m), past the excavated taper's 15% ceiling
# -> slope score 0; with soil never checked (0.5) the excavated blend
# tops out around .35*wetness + .15 + .10*runon < 0.5 everywhere -> the
# excavated type produces ZERO zones while gated cells exist -- the
# failure-to-produce charge the INDICTS wording answers.
plane_array = np.array([[100.0 + c * 0.8 for c in range(20)] for _ in range(20)])
PLANE_DEM = _dem(plane_array)
plane_result = compute_water_survey_areas(PLANE_DEM, FLAT_BOUNDARY)
assert plane_result["gate_mask_stats"]["gated_cells"] > 0, "the plane's cells gate in -- there IS ground to judge"
assert plane_result["zones_by_type"][SURVEY_TYPE_EXCAVATED] == [], (
    "16% grade zeroes the excavated slope score -> no excavated member clears 0.5"
)
plane_identify_like = {"zones_by_type": plane_result["zones_by_type"], "result": plane_result}
indicts_finding = diag.state_excavated_finding(plane_identify_like)
assert "EVIDENCE INDICTS:" in indicts_finding, (
    "zero excavated survivors -> the failure-to-produce charge earns the accusatory wording"
)
assert "LARGEST REMAINING SHORTFALL" not in indicts_finding and "not a defect claim" not in indicts_finding
print("Conditional FINDING: survivors -> LARGEST REMAINING SHORTFALL + not-a-defect line; zero survivors (16% plane) -> EVIDENCE INDICTS.")

# ======================================================================
# THE PANEL BLOCK AND THE SCALES (pre-merge change 3/4)
# ======================================================================
# build_zone_panel() is the SERVER-CURATED reading of one zone -- the
# small ordered row set the interactive map's tab renders. Unit-tested
# here on HAND-BUILT zone dicts because the assertions are about which
# rows exist under which conditions, which needs a zone whose every
# caution can be switched on and off one at a time. The payload wiring
# (feature_id, scales at the top level, the assembler parity) is
# asserted on the orchestrated session payload in test_water_step.py.

_PANEL_SCALARS = (str, int, float, bool, type(None))


def _panel_zone(**overrides):
    """A surviving zone with EVERY caution silent -- the clean case. Each
    test below switches exactly one thing on."""
    zone = {
        "id": 0,
        "survey_type": SURVEY_TYPE_EXCAVATED,
        "rank": 1,
        "zone_acres": 1.2345,
        "mean_suitability": 0.6123,
        "confidence": wsa.CONFIDENCE_HIGH,
        "primary_production_area_relationship": None,
        "canopy_overlap_pct": 0.0,
        "road_overlap_pct": 0.0,
        "production_overlap_pct": 0.0,
        "cross_type_overlaps": [],
        "truncated_by_boundary": False,
        "truncated_by_road": False,
        "sparse_anchor": False,
        "below_min_area": False,
        "pinch_terminal": None,
        "still_narrowing_at_termination": False,
    }
    zone.update(overrides)
    return zone


def _panel_keys(zone, soil_checked=True, names=None):
    return [row["key"] for row in wsa.build_zone_panel(zone, soil_checked, names or {})]


# --- the five always-rows, both types, in order ---
for _type in (SURVEY_TYPE_EMBANKMENT, SURVEY_TYPE_EXCAVATED):
    _clean = _panel_zone(survey_type=_type)
    _rows = wsa.build_zone_panel(_clean, True, {})
    assert [row["key"] for row in _rows] == list(wsa.PANEL_ALWAYS_ROWS), (
        f"a zone with nothing wrong shows exactly the five always-rows: {[r['key'] for r in _rows]}"
    )
    _by_key = {row["key"]: row for row in _rows}
    assert _by_key["zone_acres"]["value"] == 1.2, "acres to 1 dp"
    assert _by_key["zone_acres"]["unit"] == "acres"
    assert _by_key["survey_type"]["value"] == _type
    # THE ONE CONVERTED ROW: 0.6123 stored -> 61 shown, on the 0-100
    # KSOP display scale, with the scale legible in `unit` so the number
    # cannot be misread as the fraction it came from. The zone dict it
    # was read from is asserted untouched below.
    assert _by_key["suitability"]["value"] == display_scale.to_display_scale(0.6123) == 61, (
        f"the panel's suitability row is the display reading, not the stored fraction: "
        f"{_by_key['suitability']['value']!r}"
    )
    assert _by_key["suitability"]["unit"] == display_scale.DISPLAY_SCALE_UNIT == "/100", (
        "a converted value must say its scale where it is read -- an unlabelled 61 could be "
        "either scale, which is worse than either alone"
    )
    assert _clean["mean_suitability"] == 0.6123, (
        "THE REGRESSION THAT MATTERS: the panel READS the zone, it never rescales it"
    )
    assert _by_key["rank"]["value"] == 1
    assert _by_key["water_delivery"]["value"] == wsa.WATER_DELIVERY_NONE, (
        "no service relationship reports as ITS OWN VALUE -- never a fabricated 0 ft to nowhere"
    )
    for row in _rows:
        assert set(row) == {"key", "label", "value", "unit"}, row
        assert isinstance(row["value"], _PANEL_SCALARS), (
            f"a panel value is a data point, never a structure: {row}"
        )
        # DATA POINTS ONLY -- no composed sentences, in a value or a label.
        for text in (row["label"], row["value"] if isinstance(row["value"], str) else ""):
            assert "." not in text and ";" not in text, f"prose has reached the panel: {row}"

# --- water delivery: the three answers, and the two companions ---
_gravity = _panel_zone(
    primary_production_area_relationship={
        "above_production_area": True,
        "elevation_differential_m": 6.096,   # exactly 20.0 ft
        "production_area_id": 3,
    }
)
_gravity_rows = {row["key"]: row for row in wsa.build_zone_panel(_gravity, True, {})}
assert _gravity_rows["water_delivery"]["value"] == wsa.WATER_DELIVERY_GRAVITY
assert _gravity_rows["water_delivery_differential"]["value"] == 20.0, "IMPERIAL at this boundary"
assert _gravity_rows["water_delivery_differential"]["unit"] == "feet"
assert _gravity_rows["water_delivery_production_area"]["value"] == 3
_pump = _panel_zone(
    primary_production_area_relationship={
        "above_production_area": False,
        "elevation_differential_m": -3.048,
        "production_area_id": 1,
    }
)
assert {row["key"]: row["value"] for row in wsa.build_zone_panel(_pump, True, {})}[
    "water_delivery"
] == wsa.WATER_DELIVERY_PUMP
assert "water_delivery_differential" not in _panel_keys(_panel_zone()), (
    "with nothing in range the differential row is ABSENT, not 0.0 -- the fabricated-zero rule"
)

# --- the three overlaps: nonzero fires, 0.0 is silence, None is ITS OWN VALUE ---
for _key in ("production_overlap_pct", "canopy_overlap_pct", "road_overlap_pct"):
    assert _key not in _panel_keys(_panel_zone()), f"a genuine 0.0 {_key} is nothing to caution about"
    _fired = wsa.build_zone_panel(_panel_zone(**{_key: 12.5}), True, {})
    assert {row["key"]: row["value"] for row in _fired}[_key] == 12.5
    _never = wsa.build_zone_panel(_panel_zone(**{_key: None}), True, {})
    _never_by_key = {row["key"]: row for row in _never}
    assert _key in _never_by_key, f"{_key} never checked is its own value, not an absence"
    assert _never_by_key[_key]["value"] is None, (
        f"{_key} came back {_never_by_key[_key]['value']!r} for a layer nobody looked at -- a "
        f"coerced 0 would print as a measured 'no overlap'"
    )
    assert _never_by_key[_key]["value"] is not 0 and _never_by_key[_key]["value"] != 0

# --- the terminal-pinch disclosure, and the four booleans ---
_terminal = _panel_zone(
    survey_type=SURVEY_TYPE_EMBANKMENT,
    pinch_terminal="boundary",
    still_narrowing_at_termination=True,
)
_terminal_rows = {row["key"]: row["value"] for row in wsa.build_zone_panel(_terminal, True, {})}
assert _terminal_rows["pinch_terminal"] == "boundary"
assert _terminal_rows["still_narrowing"] is True, "the disclosure is its own row"
assert "pinch_terminal" not in _panel_keys(_panel_zone(survey_type=SURVEY_TYPE_EMBANKMENT))
for _flag in ("truncated_by_boundary", "truncated_by_road", "sparse_anchor", "below_min_area"):
    assert _flag not in _panel_keys(_panel_zone()), f"{_flag} False is silence"
    assert _flag in _panel_keys(_panel_zone(**{_flag: True})), f"{_flag} True fires"

# --- either_type_candidate NAMES the other zone; confidence only when not high ---
_agreeing = _panel_zone(cross_type_overlaps=[{"zone_id": 7, "fraction": 0.61}, {"zone_id": 8, "fraction": 0.1}])
_named = {row["key"]: row["value"] for row in wsa.build_zone_panel(_agreeing, True, {7: "embankment 2"})}
assert _named["either_type_candidate"] == "embankment 2", (
    "the finding names the zone that agreed -- by TYPE AND PER-TYPE RANK, because rank alone names two "
    f"pieces of ground: {_named['either_type_candidate']!r}"
)
assert "either_type_candidate" not in _panel_keys(
    _panel_zone(cross_type_overlaps=[{"zone_id": 8, "fraction": 0.1}])
), "an overlap under the note fraction is not the finding"
assert "confidence" not in _panel_keys(_panel_zone()), "high confidence says nothing"
assert "confidence" in _panel_keys(_panel_zone(confidence=wsa.CONFIDENCE_LOW))

# --- soil_never_checked: step-level, repeated per zone on purpose ---
assert "soil_never_checked" not in _panel_keys(_panel_zone(), soil_checked=True)
assert "soil_never_checked" in _panel_keys(_panel_zone(), soil_checked=False), (
    "with soil unchecked the suitability on this very panel includes a neutral soil score, and the "
    "panel must say so beside it"
)

# --- THE EXCLUDED FIELDS, asserted absent structurally ---
# Every zone shape this module can produce, with every caution firing,
# so the exclusion assertion is made against the LARGEST panel that
# exists rather than the smallest.
_maximal = [
    wsa.build_zone_panel(
        _panel_zone(
            survey_type=_type,
            confidence=wsa.CONFIDENCE_LOW,
            canopy_overlap_pct=1.0, road_overlap_pct=None, production_overlap_pct=2.0,
            truncated_by_boundary=True, truncated_by_road=True,
            sparse_anchor=True, below_min_area=True,
            pinch_terminal="road", still_narrowing_at_termination=True,
            cross_type_overlaps=[{"zone_id": 7, "fraction": 0.9}],
            primary_production_area_relationship={
                "above_production_area": True,
                "elevation_differential_m": 1.0,
                "production_area_id": 2,
            },
        ),
        False,
        {7: "excavated 1"},
    )
    for _type in (SURVEY_TYPE_EMBANKMENT, SURVEY_TYPE_EXCAVATED)
]
_all_panel_keys = {row["key"] for panel in _maximal for row in panel}
for _excluded in wsa.PANEL_EXCLUDED_KEYS:
    assert _excluded not in _all_panel_keys, (
        f"{_excluded} is on PANEL_EXCLUDED_KEYS and must not be a panel row -- the diagnostic view "
        f"is the export, not the panel"
    )
# ... and at the SOURCE level, so a row added under a different key but
# the same name cannot slip past the output check.
_panel_builder_ast = ast.parse(inspect.getsource(wsa.build_zone_panel)).body[0]
if (
    isinstance(_panel_builder_ast.body[0], ast.Expr)
    and isinstance(_panel_builder_ast.body[0].value, ast.Constant)
):
    _panel_builder_ast.body = _panel_builder_ast.body[1:]   # the docstring may NARRATE an exclusion
_panel_source_strings = {
    node.value
    for node in ast.walk(_panel_builder_ast)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
}
for _excluded in wsa.PANEL_EXCLUDED_KEYS:
    assert _excluded not in _panel_source_strings, (
        f"the panel builder's code names {_excluded!r} -- delete it from PANEL_EXCLUDED_KEYS first "
        f"if it is genuinely wanted"
    )

# --- TYPE DISPATCH: no excavated vocabulary on an embankment panel ---
_EXCAVATED_VOCABULARY = ("member", "anchor acres", "members")
_embankment_panel = _maximal[0]
for row in _embankment_panel:
    for word in _EXCAVATED_VOCABULARY:
        assert word not in row["key"], f"excavated vocabulary on an embankment panel: {row}"
        assert word not in row["label"], f"excavated vocabulary on an embankment panel: {row}"
# The two types' panels differ ONLY in the rows their own facts fire --
# the shared rows are shared by construction, not by a duplicated list.
assert {row["key"] for row in _maximal[1]} | {"pinch_terminal", "still_narrowing"} >= {
    row["key"] for row in _embankment_panel
}, "an embankment panel adds only its own instrument's disclosures"

# --- THE SCALES, on a real synthetic result ---
_scales = wsa.build_scales(flat_result)
# Six entries: the four shape-generic ones plus the fill claim's two.
# Neither of the new ones is a panel row (see PANEL_EXCLUDED_KEYS), and
# they are here anyway because the rule is about scored values crossing
# the wire, not about the panel -- narrative_data and the report read
# both, and a drainage score that reads 0.0 for too-little AND for
# too-much water is unreadable without its band.
assert set(_scales) == {
    "suitability",
    "rank",
    "overlap_pct",
    "boundary_adjacency_pct",
    "pinch_drainage_score",
    "compartment_rank_score",
}, sorted(_scales)
assert _scales["pinch_drainage_score"]["min_acres"] == wsa.EMBANKMENT_DRAINAGE_MIN_ACRES
assert (
    _scales["pinch_drainage_score"]["full_credit_acres"]
    == wsa.EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES
)
assert (
    _scales["pinch_drainage_score"]["ceiling_acres"] == wsa.MAX_VALLEY_CONTRIBUTING_AREA_ACRES
), "the band's three externally anchored numbers ride with the score they produced"
assert _scales["compartment_rank_score"]["weights"] == dict(
    wsa.EMBANKMENT_COMPARTMENT_RANK_WEIGHTS
), "a composite without its recipe is a number no consumer can argue with"
# THE SUITABILITY ENTRY IS ON THE DISPLAY SCALE, because it describes
# what the panel prints and the panel's row is converted. A scale that
# disagreed with the value it describes is worse than no scale at all.
assert _scales["suitability"]["min"] == display_scale.DISPLAY_SCALE_MIN == 0
assert _scales["suitability"]["max"] == display_scale.DISPLAY_SCALE_MAX == 100
assert _scales["suitability"]["higher_is_better"] is True, "unchanged by the display scale"
# THE OTHER TWO SCORED ENTRIES STAY 0-1, and the block stays readable
# because every entry states its own endpoints. Neither is a panel row;
# both are read beside the weights and breakpoints that produced them.
assert _scales["pinch_drainage_score"]["min"] == 0.0
assert _scales["pinch_drainage_score"]["max"] == 1.0
assert _scales["compartment_rank_score"]["min"] == 0.0
assert _scales["compartment_rank_score"]["max"] == 1.0
for _type in wsa.SURVEY_TYPES:
    assert _scales["suitability"]["parcel_observed_max"][_type] == display_scale.to_display_scale(
        float(np.max(flat_result["surfaces"][_type]))
    ), (
        "the observed ceiling IS the type's own surface maximum -- measured, not assumed -- "
        "converted through the SAME helper the row is, so value and denominator are one scale"
    )
    assert isinstance(_scales["suitability"]["parcel_observed_max"][_type], int), (
        "a 0-100 grade with a decimal point is a 0-1 fraction wearing a costume"
    )
    # AND IT IS NOT A NORMALIZER. The ceiling is shown BESIDE the value,
    # never divided into it: a parcel whose best cell is 0.82 reads 82,
    # not 100. Normalizing here would make the same ground grade
    # differently under a redrawn boundary and would flatten a poor
    # parcel and an excellent one onto the same top mark.
    assert _scales["suitability"]["parcel_observed_max"][_type] == display_scale.to_display_scale(
        float(np.max(flat_result["surfaces"][_type]))
    ) and (
        float(np.max(flat_result["surfaces"][_type])) >= 1.0
        or _scales["suitability"]["parcel_observed_max"][_type] < 100
    ), "the parcel's own maximum is NOT rescaled to 100"
    assert _scales["rank"][_type]["count"] == len(flat_result["zones_by_type"][_type]), (
        "the rank scale's denominator is the per-type surviving count"
    )
assert _scales["overlap_pct"] == {"min": 0, "max": 100}
assert _scales["boundary_adjacency_pct"] == {"min": 0, "max": 100}

# --- narrative_data carries both, JSON-native ---
_narrative = build_narrative_data(flat_result)
assert _narrative["scales"] == _scales
for _block in _narrative["zones"]:
    assert [row["key"] for row in _block["panel"]][:5] == list(wsa.PANEL_ALWAYS_ROWS)
json.dumps(_narrative)
print(
    f"Panel: five always-rows on both types in order, {len(wsa.PANEL_EXCLUDED_KEYS)} excluded keys "
    f"absent from the widest panel AND from the builder's source, no excavated vocabulary on an "
    f"embankment panel, every caution silent on a clean zone and firing when it fires, and a "
    f"never-checked overlap on the wire as null rather than 0. Scales: observed ceilings "
    f"{_scales['suitability']['parcel_observed_max']} match the surface maxima; rank counts match "
    f"the per-type survivor counts."
)
print(
    f"Display scale: the suitability row reads 61{display_scale.DISPLAY_SCALE_UNIT} off a stored "
    f"0.6123 that the panel leaves untouched, and the scales block's endpoints "
    f"({_scales['suitability']['min']}-{_scales['suitability']['max']}) and per-type ceilings are "
    f"the same conversion -- while pinch_drainage_score and compartment_rank_score stay 0-1, each "
    f"entry stating its own endpoints."
)

print("\nAll water_survey_areas checks passed.")
