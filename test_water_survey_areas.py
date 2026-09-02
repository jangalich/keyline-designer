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

# --- THE ABSOLUTE TWI CURVE at its breakpoints: hand values BELOW / AT
#     the floor / BETWEEN / AT full credit / ABOVE. No population is
#     consulted anywhere, which is the whole point of the change. ---
_lo, _hi = wsa.TWI_SCORE_MIN_BREAKPOINT, wsa.TWI_SCORE_FULL_CREDIT_BREAKPOINT
_mid = (_lo + _hi) / 2.0
hand = np.array([[_lo - 3.0, _lo, _mid], [_hi, _hi + 3.0, _lo + 0.25 * (_hi - _lo)], [np.nan, 0.0, 1e6]])
scored = twi_score(hand)
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
assert math.isclose(twi_score(np.array([5.0]), min_breakpoint=0.0, full_credit_breakpoint=10.0)[0], 0.5)
print(f"Absolute TWI curve: 0.0 at/below {_lo}, linear ramp, 1.0 at/above {_hi}; NaN propagates; configurable.")

# THE PROPERTY THE WHOLE BRANCH EXISTS FOR, stated at the unit level: the
# score of a value does not depend on what other values are present.
assert twi_score(np.array([7.0]))[0] == twi_score(np.array([7.0, 20.0, 20.0, 20.0]))[0], (
    "an absolute curve scores a value identically whatever population surrounds it -- "
    "adding the wettest cells on the landscape must not move anyone else's score"
)
print("Absolute TWI curve: one cell's score is independent of every other cell. That IS the fix.")

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
assert EMBANKMENT_WEIGHTS == {"drainage_area": 0.30, "slope": 0.25, "soil": 0.25, "twi": 0.20}
assert EXCAVATED_WEIGHTS == {"wetness": 0.35, "soil": 0.30, "slope": 0.25, "drainage_runon": 0.10}
print("Weights: both types sum to 1.0 at the documented v1 priors.")

# One hand-computed cell through each type's FULL blend. Inputs chosen so
# every criterion lands at a hand-checkable value at 5m cells
# (cell area = 25/4046.8564224 = 0.0061776 ac):
#   accumulation 324 cells -> 2.0016 ac -> drainage band 1.0, run-on 1.0
#   slope 5%               -> embankment 1.0 (in 3-8), excavated 1.0
#                             (full credit through the seep-widened 5%)
#   TWI percentile 0.6 (given directly), depression 0.25 m -> 0.5
#     -> wetness = 0.5*0.6 + 0.5*0.5 = 0.55
#   soil grid 0.8
# embankment = .30*1 + .25*1 + .25*.8 + .20*.6              = 0.87
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
assert np.allclose(surfaces[SURVEY_TYPE_EMBANKMENT], 0.87), (
    f"hand-computed embankment blend 0.87, got {surfaces[SURVEY_TYPE_EMBANKMENT][1, 1]}"
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
print("Surface blend: one cell hand-computed through both full blends (0.87 / 0.7825); mask zeroes both.")


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

flat_result = compute_water_survey_areas(FLAT_DEM, FLAT_BOUNDARY, soil_inputs=GOOD_WET_SOIL_INPUTS)

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
flat_twi = (math.log(5.0 / wsa.TWI_MIN_SLOPE_TAN) - wsa.TWI_SCORE_MIN_BREAKPOINT) / (
    wsa.TWI_SCORE_FULL_CREDIT_BREAKPOINT - wsa.TWI_SCORE_MIN_BREAKPOINT
)
assert math.isclose(flat_twi, 0.6292982978540596), "ln(5000) on the 6.0 -> 10.0 ramp"
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


def _v_twi(accumulation_cells):
    """The absolute TWI score for this fixture's uniform grade, from the
    RAW value -- a = accumulation * cell_area / cell_width = 5 * cells,
    tan(beta) uniform, then the module's own ramp. NO POPULATION: the
    same accumulation scores the same here whatever else is on the
    grid, which is exactly what the old `(576 + i) / 611` rank could not
    say."""
    raw = math.log(5.0 * accumulation_cells / V_TAN_BETA)
    return min(
        max(
            (raw - wsa.TWI_SCORE_MIN_BREAKPOINT)
            / (wsa.TWI_SCORE_FULL_CREDIT_BREAKPOINT - wsa.TWI_SCORE_MIN_BREAKPOINT),
            0.0,
        ),
        1.0,
    )


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
        v_emb_expected[r, c] = 0.30 * d + 0.25 * 1.0 + 0.25 * 0.5 + 0.20 * twi
        v_exc_expected[r, c] = 0.35 * (0.5 * twi) + 0.30 * 0.5 + 0.25 * v_slope_score_exc + 0.10 * runon
assert np.allclose(
    np.where(v_gate, v_result["surfaces"][SURVEY_TYPE_EMBANKMENT], 0.0), v_emb_expected
), "the RAW embankment blend must match the stated per-cell formulas on every gated cell"
assert np.allclose(
    np.where(v_gate, v_result["surfaces"][SURVEY_TYPE_EXCAVATED], 0.0), v_exc_expected
), "the RAW excavated blend must match the stated per-cell formulas on every gated cell"

expected_channel = {(r, V_CHANNEL) for r in range(2, 38)}
assert all(v_emb_expected[r, c] >= 0.5 for r, c in expected_channel)
assert all(v_exc_expected[r, c] >= 0.5 for r, c in expected_channel)
assert all(
    v_emb_expected[r, c] < 0.5 and v_exc_expected[r, c] < 0.5
    for r in range(2, 38)
    for c in range(2, 19)
    if c != V_CHANNEL
), "every side cell stays out of both ribbons at 0.5"

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
# The seeding, hand-derived. The qualifying cells are exactly the 36
# on-parcel channel cells (blend 0.6652..0.875, ascending with r; every
# side cell is 0.375 < 0.5).
#
# THE ABSOLUTE CURVE PUTS A PLATEAU AT THE TOP OF THIS SURFACE, and the
# seeding order is worth stating because of it. Both of the
# accumulation-driven criteria SATURATE: drainage_band_score reaches 1.0
# at 2 acres (row 5) and twi_score reaches 1.0 at raw TWI 10.0 (also row
# 5), so rows 5..37 all blend to EXACTLY 0.875 and the argmax is a
# 33-way tie. select_embankment_seeds() resolves ties row-major (its own
# documented determinism), so the first seed is the TOP of the plateau
# at row 5 rather than the most-accumulated cell at row 37.
#
# UNDER THE RETIRED PERCENTILE THERE WAS NO PLATEAU: ranking gave every
# channel cell a distinct score by construction, so seeds walked down
# from row 37. The plateau is the honest consequence of an absolute
# curve with full credit -- two cells that both carry an established
# drainageway ARE equally good on these criteria, and a scoring system
# that manufactures a difference between them is inventing precision.
# Which member of a tie anchors a compartment is a SEEDING question, and
# seeding is deliberately not what this branch changed.
#
# Iterative claiming at 30 m (6 cells of row distance, symmetric and
# inclusive): the row-5 seed claims rows 0..11 (taking rows 2-4 with
# it), then row 12 claims 6..18, and so on -> seeds at rows 5, 12, 19,
# 26, 33.
v_seeds = v_result["embankment_seeds"]
assert [record["rowcol"] for record in v_seeds] == [
    (5, V_CHANNEL), (12, V_CHANNEL), (19, V_CHANNEL), (26, V_CHANNEL), (33, V_CHANNEL)
], f"hand-derived 30 m claiming order, got {[record['rowcol'] for record in v_seeds]}"
assert all(record["blend_score"] >= 0.5 for record in v_seeds)
assert all(math.isclose(record["blend_score"], 0.875) for record in v_seeds), (
    "every seed sits on the saturated plateau, all five at the identical 0.875"
)
# Every seed FAILS, honestly, because this prism valley has a CONSTANT
# cross-section -- crest-to-crest width is identical at every station,
# so the along-channel minimum lands on the seed's own station (argmin
# tie -> index 0: the valley never narrows below the seed) -- the ONE
# failure the accepted-terminal doctrine kept: no_constriction. (The
# r=37 seed's walk is cut by the boundary with only its own station
# measured -- a single station IS the seed station, so it reads
# no_constriction too, not a terminal acceptance: a dam at the storage
# cell is degenerate.)
assert all(record["status"] == wsa.SEED_STATUS_FAILED for record in v_seeds), (
    "a constant-width prism never narrows below any seed -- every seed reports nothing"
)
assert all(
    record["reason_code"] == wsa.REASON_NO_CONSTRICTION for record in v_seeds
), f"widths never drop below the seed station -> no_constriction, got {[r['reason_code'] for r in v_seeds]}"
assert v_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT] == [], (
    "no constriction, no compartment, no fallback -- the hull does not exist on this path"
)

# The lone excavated zone survives, ranks 1, selects; with no
# embankment zone there is no cross-type agreement to report.
v_exc_zone = exc_zones[0]
assert "presented" not in v_exc_zone
assert v_exc_zone["rank"] == 1
assert v_result["selected_water_zone"] is v_exc_zone
assert v_exc_zone["cross_type_overlaps"] == []
assert v_exc_zone["sparse_anchor"] is False

# The narrative carries the seed accounting: 5 seeds, 5 failed, each
# with its reason code -- the reach with no on-parcel pinch reports
# honestly as nothing.
v_narrative = build_narrative_data(v_result)
assert v_narrative["zone_count"] == 1 and len(v_narrative["zones"]) == 1
assert v_narrative["embankment_zone_count"] == 0 and v_narrative["excavated_zone_count"] == 1
assert v_narrative["embankment_generation"] == wsa.PROVENANCE_SEED_COMPARTMENT
assert v_narrative["embankment_seed_count"] == 5 and v_narrative["embankment_failed_seed_count"] == 5
assert {entry["reason_code"] for entry in v_narrative["embankment_failed_seeds"]} == {
    wsa.REASON_NO_CONSTRICTION
}
print(
    f"Fixture 2 (V-valley, compartment change): excavated ribbons the 36 channel cells (mean "
    f"{v_exc_zone['mean_suitability']}); embankment seeds 5 plateau cells and every walk honestly fails "
    "no_constriction (constant prism cross-section -- the valley never narrows below any seed)."
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
split_result = compute_water_survey_areas(FLAT_DEM, FLAT_BOUNDARY, soil_inputs=split_soil_inputs)
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
tiny_result = compute_water_survey_areas(FLAT_DEM, TINY_BOUNDARY, soil_inputs=GOOD_WET_SOIL_INPUTS)
assert tiny_result["zones"] == [] and tiny_result["zones_by_type"][SURVEY_TYPE_EXCAVATED] == [], (
    "a sub-floor zone is OUT of the pipeline output -- the floor is a filter now"
)
assert tiny_result["selected_water_zone"] is None, "no survivor -> the selection is honestly None"
assert "presented_zones" not in tiny_result, "the presentation machinery is deleted, key and all"
assert len(tiny_result["dropped_zones"]) == 1, "the drop is carried, never silent"
tiny_dropped = tiny_result["dropped_zones"][0]
assert tiny_dropped["status"] == wsa.ZONE_STATUS_DROPPED
assert tiny_dropped["drop_reason"] == FLAG_BELOW_MIN_AREA, "the reason code attributes the drop"
assert tiny_dropped["rank"] is None and "presented" not in tiny_dropped
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
strip_result = compute_water_survey_areas(FLAT_DEM, STRIP_BOUNDARY, soil_inputs=strip_soil_inputs)
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

# --- THE PRESENTATION CAP IS DELETED (pre-merge change 3): absence
# asserted at the module surface, not just unexercised. The constant,
# the function, and the per-zone property are all gone -- surviving IS
# shipping -- and no code path in the module even NAMES the deleted
# machinery (AST-level, so docstring history notes stay legal). ---
from water_survey_areas import attach_cross_type_overlaps, rank_survey_zones_per_type, select_survey_zone  # noqa: E402

assert not hasattr(wsa, "WATER_ZONE_PRESENTATION_TOP_N"), "the cap constant is deleted, not zeroed or bypassed"
assert not hasattr(wsa, "apply_presentation"), "the presentation function is deleted with its guarantee/swap logic"
for zone_holder in (flat_result["zones"], v_result["zones"], strip_result["zones"], tiny_result["dropped_zones"]):
    for z in zone_holder:
        assert "presented" not in z, "no zone -- surviving or dropped -- carries the deleted property"
_wsa_module_ast = ast.parse(inspect.getsource(wsa))
_called_names = {
    node.func.id for node in ast.walk(_wsa_module_ast)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
}
assert "apply_presentation" not in _called_names, "nothing in the module still calls the deleted machinery"

# Rank + selection on hand-built zone dicts: every survivor is ranked
# within its type ON ITS TYPE'S OWN INSTRUMENT -- embankment by SEED
# blend score (the compartment change), excavated by member-mean
# suitability -- and the pooled rank-1 invariant holds with NO cap in
# between. The embankment minis carry a deliberately LOW compartment
# mean_suitability (0.1) beside their seed blends: if ranking or
# selection ever read the compartment mean, every assertion below
# flips -- the walked ground's mean must never rank a compartment.
def _mini_zone(zid, stype, mean, acres, poly, seed_blend=None):
    zone = {
        "id": zid, "survey_type": stype, "mean_suitability": mean,
        "polygon_utm": poly,
    }
    if stype == SURVEY_TYPE_EMBANKMENT:
        zone["seed_blend_score"] = seed_blend
        zone["zone_acres"] = acres
    else:
        zone["member_acres"] = acres
    return zone


rank_pool = [
    _mini_zone(0, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(0, 0, 20, 20), seed_blend=0.9),
    _mini_zone(1, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(100, 0, 120, 20), seed_blend=0.8),
    _mini_zone(2, SURVEY_TYPE_EMBANKMENT, 0.1, 1.0, box(200, 0, 220, 20), seed_blend=0.7),
    _mini_zone(3, SURVEY_TYPE_EXCAVATED, 0.65, 1.0, box(10, 0, 30, 20)),
    _mini_zone(4, SURVEY_TYPE_EXCAVATED, 0.6, 1.0, box(300, 0, 320, 20)),
]
rank_survey_zones_per_type(rank_pool)
assert [z["rank"] for z in rank_pool] == [1, 2, 3, 1, 2], (
    "EVERY survivor is ranked within its type (embankment by seed blend, despite the 0.1 compartment "
    "means) -- rank 3 exists because nothing caps the list at 3 anymore"
)
assert select_survey_zone(rank_pool) is rank_pool[0], (
    "the pooled rank-1 invariant on the per-type scores: seed blend 0.9 beats member-mean 0.65 -- and "
    "the 0.1 compartment mean never enters the pool"
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
print("Cap deletion: constant/function/property absent, all survivors ranked (a rank 3 exists), rank-1 selects, cross-type fractions hand-verified at 0.5.")


# =========================================================================
# 4. CONTRACT -- the selected ZONE is a selected_water_zone
# =========================================================================

selected = flat_result["selected_water_zone"]
assert selected is not None and isinstance(selected, dict) and selected, (
    "the contract is 'non-empty dict or None' -- truthiness gates in solar/tree/fencing depend on it"
)
assert selected is flat_zone, "the pooled rank-1 ZONE is the selection"
assert selected["status"] == wsa.ZONE_STATUS_NOMINATED and "presented" not in selected, (
    "the selected zone is a surviving zone, full stop -- the presented distinction no longer exists"
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
    "representative_elevation_m": 150.0,  # 50 m ABOVE the zone -> pump required
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
# the dual-acreage numbers, and carries the TWI RESOLUTION-CALIBRATION
# caveat (the one that replaced the retired parcel-relative caveat):
narrative = build_narrative_data(hit_result)
json.dumps(narrative)
assert narrative["zone_found"] is True
assert narrative["twi_is_absolute"] is True, "TWI is absolute now and narrative_data says so"
assert "THIS parcel" not in narrative["twi_note"], (
    "the retired parcel-relative claim must not survive in the note -- it is no longer true"
)
assert "resolution-dependent" in narrative["twi_note"] and "calibrated" in narrative["twi_note"], (
    "what survives is the RESOLUTION-CALIBRATION caveat, which still is true"
)
assert "twi_is_parcel_relative" not in narrative, "the retired caveat flag is gone, not renamed in place"
assert narrative["zones"] and len(narrative["zones"]) == narrative["zone_count"] == len(hit_result["zones"]), (
    "narrative lists ALL surviving zones with the total count -- the cap and its counters are gone"
)
assert narrative["dropped_count"] == 0
for gone_key in ("presented_count", "presentation_top_n", "presentation_guarantee_applied"):
    assert gone_key not in narrative, f"the deleted cap's narrative counter {gone_key} must not resurface"
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
# The deleted `presented` property never reaches the wire; the honesty
# reports (sparse anchor + cross-type agreement) do:
assert "presented" not in zone_feature["properties"]
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
assert "presented" not in dropped_props and dropped_props["rank"] is None
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
    assert _by_key["suitability"]["value"] == 0.6123 and _by_key["suitability"]["unit"] is None
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
assert set(_scales) == {"suitability", "rank", "overlap_pct", "boundary_adjacency_pct"}
assert _scales["suitability"]["min"] == 0.0 and _scales["suitability"]["max"] == 1.0
assert _scales["suitability"]["higher_is_better"] is True
for _type in wsa.SURVEY_TYPES:
    assert _scales["suitability"]["parcel_observed_max"][_type] == round(
        float(np.max(flat_result["surfaces"][_type])), 4
    ), "the observed ceiling IS the type's own surface maximum -- measured, not assumed"
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

print("\nAll water_survey_areas checks passed.")
