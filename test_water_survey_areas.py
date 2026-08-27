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

import inspect
import json
import math

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

# --- excavated slope: 1.0 dead flat, linear to 0 at 8% ---
assert float(excavated_slope_score(np.array(0.0))) == 1.0
assert math.isclose(float(excavated_slope_score(np.array(4.0))), 0.5)
assert float(excavated_slope_score(np.array(8.0))) == 0.0
assert float(excavated_slope_score(np.array(9.0))) == 0.0
assert float(excavated_slope_score(np.array(np.nan))) == 0.0
print("Excavated slope: 1.0 flat, gone at 8 percent.")

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

# --- TWI percentile on a hand-built 3x3: 9 distinct values -> ranks
#     0/8 .. 8/8; a dead-flat 3x3 -> 0.5 everywhere (mean-rank ties) ---
values = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]])
mask = np.ones((3, 3), dtype=bool)
pct = parcel_relative_percentile(values, mask)
expected = np.array([[0.0, 0.125, 0.25], [0.375, 0.5, 0.625], [0.75, 0.875, 1.0]])
assert np.allclose(pct, expected), f"distinct 3x3 must rank 0..1 in eighths, got {pct}"
flat_pct = parcel_relative_percentile(np.full((3, 3), 2.0), mask)
assert np.allclose(flat_pct, 0.5), "all-equal ground shares the neutral mean-rank 0.5, never 'driest'"
masked = parcel_relative_percentile(values, np.array([[True, True, False]] * 3))
assert np.isnan(masked[0, 2]), "off-parcel cells carry NaN, excluded from the population"
# 6 on-parcel values 1,2,4,5,7,8 -> value 7 has 4 below of n-1=5 -> 0.8
assert math.isclose(masked[2, 0], 0.8)
print("TWI percentile: hand-built 3x3 ranks in eighths; flat ties read 0.5; mask bounds the population.")

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
#   slope 5%               -> embankment 1.0 (in 3-8), excavated 1-5/8 = 0.375
#   TWI percentile 0.6 (given directly), depression 0.25 m -> 0.5
#     -> wetness = 0.5*0.6 + 0.5*0.5 = 0.55
#   soil grid 0.8
# embankment = .30*1 + .25*1 + .25*.8 + .20*.6              = 0.87
# excavated  = .35*.55 + .30*.8 + .25*.375 + .10*1          = 0.62625
blend_dem = _dem(np.full((3, 3), 100.0))
surfaces = compute_suitability_surfaces(
    blend_dem,
    gate_mask=np.ones((3, 3), dtype=bool),
    flow_accumulation=np.full((3, 3), 324.0),
    slope_pct=np.full((3, 3), 5.0),
    twi_percentile=np.full((3, 3), 0.6),
    depression_depth=np.full((3, 3), 0.25),
    soil_score_grid=np.full((3, 3), 0.8),
)
assert np.allclose(surfaces[SURVEY_TYPE_EMBANKMENT], 0.87), (
    f"hand-computed embankment blend 0.87, got {surfaces[SURVEY_TYPE_EMBANKMENT][1, 1]}"
)
assert np.allclose(surfaces[SURVEY_TYPE_EXCAVATED], 0.62625), (
    f"hand-computed excavated blend 0.62625, got {surfaces[SURVEY_TYPE_EXCAVATED][1, 1]}"
)
# The gate mask zeroes both surfaces before anything reads them:
gated = compute_suitability_surfaces(
    blend_dem,
    gate_mask=np.zeros((3, 3), dtype=bool),
    flow_accumulation=np.full((3, 3), 324.0),
    slope_pct=np.full((3, 3), 5.0),
    twi_percentile=np.full((3, 3), 0.6),
    depression_depth=np.full((3, 3), 0.25),
    soil_score_grid=np.full((3, 3), 0.8),
)
assert np.all(gated[SURVEY_TYPE_EMBANKMENT] == 0.0) and np.all(gated[SURVEY_TYPE_EXCAVATED] == 0.0)
print("Surface blend: one cell hand-computed through both full blends (0.87 / 0.62625); mask zeroes both.")


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
# into the excavated classifier. Column step s over 5 m cells ->
# horizontal-neighbor grade s/5*100 (the diagonal is s/7.07, smaller, so
# the horizontal IS the max). Expected classifier scores:
# 0% -> 1.0, 4% -> 0.5, 8% -> 0.0, 12% -> 0.0.
for col_step, expected_grade, expected_score in ((0.0, 0.0, 1.0), (0.2, 4.0, 0.5), (0.4, 8.0, 0.0), (0.6, 12.0, 0.0)):
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
print("Slope units: percent grade verified on hand-built planes at 0/4/8/12%, classifier scores 1.0/0.5/0.0/0.0.")

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
    twi_percentile=diag_zeros,
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

# And the aggregation over it: one zone whose envelope closes the
# corner notches -- the neighborhood claim, applied AFTER extraction.
diag_surfaces = {
    SURVEY_TYPE_EMBANKMENT: diag_raw,
    SURVEY_TYPE_EXCAVATED: diag_zeros,
    "criteria": {SURVEY_TYPE_EMBANKMENT: diag_criteria, SURVEY_TYPE_EXCAVATED: {}},
}
diag_gate_context = {
    "twi_percentile": diag_zeros,
    "depression_depth": diag_zeros,
    "flow_accumulation": np.ones((20, 20)),
    "slope_pct": diag_zeros,
    "soil_covered_mask": diag_mask & False,
    "soil_checked": False,
}
diag_zones = build_survey_zones(diag_dem, diag_regions, diag_surfaces, diag_gate_context, diag_boundary)
assert len(diag_zones) == 1 and diag_zones[0]["member_count"] == 1
assert diag_zones[0]["zone_acres"] > diag_zones[0]["member_acres"], (
    "the closing envelope fills the diagonal's corner notches -- the ground to walk exceeds the anchor"
)
assert diag_zones[0]["mean_suitability"] == 1.0, (
    "zone score statistics come from MEMBER cells only -- the envelope never launders the 0.5 background in"
)
print("Diagonal ridge: raw peaks extract at 0.9 as one 8-connected member; its zone walks a larger envelope while scoring only the anchor.")

# --- Closing math, hand-derived (the survey-zone geometry core). Two
# 20x20 m squares. At SURVEY_ZONE_GROUPING_DISTANCE_METERS = 30, a 20 m
# gap bridges (gaps up to the FULL distance bridge: each side buffers
# out 15); a 40 m gap does not. Envelope area for the bridged pair:
# the filled 60x20 rectangle = 1200 m^2 MINUS two menisci where the
# round buffer joins sag across the gap -- sagitta 15 - sqrt(15^2-10^2)
# = 3.82 m over the 20 m gap, ~50-60 m^2 per side -- so the assertion
# uses a STATED tolerance (within 120 m^2 of 1200), not equality. A
# singleton closes back to itself (dilation then erosion of a convex
# square is exact up to buffer discretization). ---
assert SURVEY_ZONE_GROUPING_DISTANCE_METERS == 30.0
sq_a = box(0.0, 0.0, 20.0, 20.0)
sq_b_near = box(40.0, 0.0, 60.0, 20.0)   # 20 m gap < 30 -> bridges
sq_b_far = box(60.0, 0.0, 80.0, 20.0)    # 40 m gap > 30 -> stays apart

near_envelopes = _close_member_footprints([sq_a, sq_b_near], SURVEY_ZONE_GROUPING_DISTANCE_METERS)
assert len(near_envelopes) == 1, "a 20 m gap at 30 m grouping must fuse into ONE zone envelope"
near_area = near_envelopes[0].area
assert abs(near_area - 1200.0) <= 120.0, (
    f"the fused envelope is the 60x20 rectangle (1200 m^2) minus the two round-join menisci "
    f"(sagitta 3.82 m over the 20 m gap): got {near_area:.1f} m^2, outside the stated tolerance"
)
assert near_area > sq_a.area + sq_b_near.area, "the bridge genuinely adds ground beyond the members"

far_envelopes = _close_member_footprints([sq_a, sq_b_far], SURVEY_ZONE_GROUPING_DISTANCE_METERS)
assert len(far_envelopes) == 2, "a 40 m gap at 30 m grouping must stay TWO zones"
for envelope in far_envelopes:
    assert abs(envelope.area - 400.0) / 400.0 < 0.01, (
        f"an unbridged square closes back to its own 400 m^2, got {envelope.area:.1f}"
    )

lone_envelopes = _close_member_footprints([sq_a], SURVEY_ZONE_GROUPING_DISTANCE_METERS)
assert len(lone_envelopes) == 1 and abs(lone_envelopes[0].area - 400.0) / 400.0 < 0.01, (
    "a singleton closes back to approximately itself -- the large-single-candidate case needs no special rule"
)
print("Closing math: 20 m gap fuses (1200 m^2 minus stated menisci), 40 m gap stays two, singleton returns itself.")


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
# >= the 0.6 default -> ONE 100-cell member; embankment = 0.35 < 0.6 ->
# none. The single member closes to one zone whose clipped envelope IS
# the boundary box -- which makes the DUAL ACREAGE distinction visible
# on this very fixture: member_acres counts CELLS (100 x 0.0061776 =
# 0.6178) while zone_acres measures the clipped envelope POLYGON
# (49.8 x 49.8 m = 2480.04 m^2 = 0.6128) -- two different questions,
# deliberately not interchangeable.
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
expected_flat_score = 0.35 * 0.25 + 0.30 * 1.0 + 0.25 * 1.0 + 0.10 * (CA / 2.0)

flat_members = flat_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(flat_members) == 1 and flat_members[0]["cell_count"] == 100
flat_member = flat_members[0]
assert math.isclose(flat_member["mean_suitability"], round(expected_flat_score, 4)), (
    f"hand-derived member mean {round(expected_flat_score, 4)}, got {flat_member['mean_suitability']}"
)
assert flat_result["regions_by_type"][SURVEY_TYPE_EMBANKMENT] == [], "0.35 < 0.6 -> no embankment member"

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
assert flat_zone["twi_percentile_mean"] == 0.5 and flat_zone["depression_depth_max_m"] == 0.0
assert flat_zone["soil_coverage_fraction"] == 1.0 and flat_zone["criteria_complete"] is True
assert flat_zone["confidence"] == "high", "soil coverage + complete criteria = 2 signals = HIGH"
assert FLAG_NO_SERVICE_RELATIONSHIP in flat_zone["flags"], (
    "no production areas supplied -> the no-service case is a ZONE flag, never a drop"
)
assert flat_zone["below_min_area"] is False
# Member <-> zone linkage, both ways:
assert flat_zone["member_ids"] == [flat_member["id"]]
assert flat_member["zone_id"] == flat_zone["id"]
# Envelope hugs the boundary on every side -> adjacency ~1.0 (test 3).
# Not exactly 1.0: the vector closing's buffer arcs are polygonal
# approximations, so eroding the dilated square shaves each corner a few
# centimeters inside the boundary line -- ~0.6% of the perimeter on this
# 49.8 m box, a discretization artifact of the closing, not a
# measurement change.
assert flat_zone["boundary_adjacency_fraction"] > 0.99, (
    f"the clipped envelope hugs the boundary (minus closing-discretization corner shavings) -- "
    f"expected > 0.99, got {flat_zone['boundary_adjacency_fraction']}"
)
print(
    f"Fixture 1 (uniform wet flat): one member (mean {flat_member['mean_suitability']} hand-derived), one "
    f"zone with DISTINCT dual acreage ({flat_zone['zone_acres']} ac envelope vs {flat_zone['member_acres']} "
    "ac anchor), linkage both ways, adjacency 1.0."
)

# --- FIXTURE 2: V-valley + hand-built accumulation ribbon -> one
# embankment member chain, one zone that closes back to (approximately)
# the ribbon itself. 40x21: elevation = 100 + |c-10|*0.30 - r*0.25;
# every cell's max-neighbor grade is the downhill diagonal (0.30+0.25)/
# hypot(5,5) = 7.78% (uniform), inside the 3-8% sweet spot -> embankment
# slope 1.0. Boundary covers centers rows 2..37 x cols 2..18 (612
# cells). flow_accumulation is a hand-built OVERRIDE: 1 cell everywhere
# except the channel column (c=10), which carries 15*(r+1) cells. TWI
# (uniform tan) orders exactly by accumulation:
#   576 side cells all equal -> mean-rank (0.5*575)/611 = 0.4705...
#   36 on-parcel channel cells distinct, ranks (576+i)/611, i = r-2.
# Per-cell RAW embankment (soil never checked -> neutral 0.5):
#   side:    .25 + .125 + .20*0.4705 = 0.4691       < 0.6 -> out
#   channel: .30*d(r) + .375 + .20*(576+i)/611      -> monotone in r,
#            crossing the 0.6 default partway down the valley.
# Expected members = the channel rows where that formula clears 0.6 --
# a contiguous bottom segment, computed below from the STATED formula.
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
v_accumulation = np.ones((V_ROWS, V_COLS))
for r in range(V_ROWS):
    v_accumulation[r, V_CHANNEL] = 15 * (r + 1)

v_result = compute_water_survey_areas(V_DEM, V_BOUNDARY, flow_accumulation=v_accumulation)

# The stated per-cell formulas, cross-checked against the module's own
# raw surface on every gated cell:
v_gate = np.zeros((V_ROWS, V_COLS), dtype=bool)
v_gate[2:38, 2:19] = True
side_twi = (0.5 * 575) / 611
v_raw_expected = np.zeros((V_ROWS, V_COLS))
for r in range(V_ROWS):
    for c in range(V_COLS):
        if not v_gate[r, c]:
            continue
        if c == V_CHANNEL:
            acres = 15 * (r + 1) * CA
            d = min(max((acres - 0.5) / 1.5, 0.0), 1.0)
            v_raw_expected[r, c] = 0.30 * d + 0.375 + 0.20 * (576 + (r - 2)) / 611
        else:
            v_raw_expected[r, c] = 0.375 + 0.20 * side_twi
assert np.allclose(
    np.where(v_gate, v_result["surfaces"][SURVEY_TYPE_EMBANKMENT], 0.0), v_raw_expected
), "the RAW embankment blend must match the stated per-cell formulas on every gated cell"

expected_members = {
    (r, V_CHANNEL) for r in range(2, 38) if v_raw_expected[r, V_CHANNEL] >= 0.6
}
assert expected_members, "the fixture must clear the 0.6 default partway down the channel"
v_members = v_result["regions_by_type"][SURVEY_TYPE_EMBANKMENT]
assert len(v_members) == 1, f"the channel segment is one 8-connected member, got {len(v_members)}"
v_member = v_members[0]
assert set(v_member["cells"]) == expected_members, "member cells equal the formula-derived expectation"
expected_mean = round(float(np.mean([v_raw_expected[r, c] for r, c in expected_members])), 4)
assert v_member["mean_suitability"] == expected_mean, (
    f"hand-summed member mean {expected_mean}, got {v_member['mean_suitability']}"
)
assert v_result["regions_by_type"][SURVEY_TYPE_EXCAVATED] == []

v_zones = v_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
assert len(v_zones) == 1 and v_zones[0]["member_count"] == 1
v_zone = v_zones[0]
assert v_zone["mean_suitability"] == expected_mean, "zone statistics are the member chain's own"
# A straight one-cell-wide ribbon closes back to approximately itself
# (dilate-then-erode of a straight band is exact up to discretization):
assert abs(v_zone["zone_acres"] - v_zone["member_acres"]) / v_zone["member_acres"] < 0.02, (
    f"a lone straight ribbon's envelope is ~its own footprint: {v_zone['zone_acres']} vs {v_zone['member_acres']}"
)
bottom_row = max(r for r, c in expected_members)
assert v_zone["wettest_cell_rowcol"] == (bottom_row, V_CHANNEL)
assert math.isclose(v_zone["contributing_area_acres_at_wettest_cell"], round(15 * (bottom_row + 1) * CA, 2))
assert v_zone["boundary_adjacency_fraction"] < 0.1, "an interior ribbon barely touches the boundary"
print(
    f"Fixture 2 (V-valley): {len(expected_members)}-cell channel member at the 0.6 default (formula-"
    f"derived), one zone closing back to itself (zone {v_zone['zone_acres']} ac vs anchor "
    f"{v_zone['member_acres']} ac), wettest at the valley bottom."
)

# --- FIXTURE 2b: member-vs-zone split where the envelope ADDS ground.
# Same flat construction as fixture 1, but soil covers TWO patches
# (cols 5..8 and cols 12..14) with best-wet soil; the 3-column gap
# (15 m) scores the neutral 0.5 soil -> 0.4878 < 0.6 -> NOT a member.
# 15 m < the 30 m grouping -> the two members fuse into ONE zone whose
# envelope bridges the gap. Score statistics from members ONLY: every
# member cell scores 0.63780888, so the zone mean must be exactly that
# -- if envelope ground were laundered in, the 30 gap cells at 0.4878
# would drag the mean to ~0.593.
split_soil_inputs = {
    "ksat_rows": [{"mukey": "A", "ksat_r": 0.05}, {"mukey": "B", "ksat_r": 0.05}],
    "components": [
        {"mukey": "A", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"},
        {"mukey": "B", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"},
    ],
    "geometries_by_mukey": {
        "A": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 25.0, ORIGIN_Y - 75.0, ORIGIN_X + 45.0, ORIGIN_Y - 25.0))
        ),
        "B": transform_geom(
            CRS, "EPSG:4326", mapping(box(ORIGIN_X + 60.0, ORIGIN_Y - 75.0, ORIGIN_X + 75.0, ORIGIN_Y - 25.0))
        ),
    },
}
split_result = compute_water_survey_areas(FLAT_DEM, FLAT_BOUNDARY, soil_inputs=split_soil_inputs)
split_members = split_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(split_members) == 2, f"two soil patches -> two members (gap cells at 0.4878 < 0.6), got {len(split_members)}"
assert {m["cell_count"] for m in split_members} == {40, 30}, "4x10 and 3x10 cell patches"
split_zones = split_result["zones_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(split_zones) == 1, "a 15 m gap at 30 m grouping fuses the two members into ONE zone"
split_zone = split_zones[0]
assert split_zone["member_count"] == 2 and split_zone["cell_count"] == 70
assert math.isclose(split_zone["member_acres"], round(70 * CA, 4))
assert split_zone["zone_acres"] > split_zone["member_acres"], (
    "the bridged envelope adds the gap -- more ground to walk than anchored it"
)
assert abs(split_zone["zone_acres"] - expected_zone_acres) / expected_zone_acres < 0.02, (
    f"the bridged envelope spans ~the full 50 m block ({expected_zone_acres:.4f} ac), got {split_zone['zone_acres']}"
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

# --- FIXTURE 3: a sub-floor ZONE is FLAGGED AND PRESENT (and can even
# be selected -- first-run posture). Boundary covers only a 3x3 block:
# 9 member cells = 0.0556 ac < the 0.1 ac floor, flagged on MEMBER
# acreage (the anchoring signal -- an envelope can be arbitrarily
# larger than the ground that earned it). ---
TINY_BOUNDARY = box(
    ORIGIN_X + 8 * RESOLUTION + 0.1,
    ORIGIN_Y - 11 * RESOLUTION + 0.1,
    ORIGIN_X + 11 * RESOLUTION - 0.1,
    ORIGIN_Y - 8 * RESOLUTION - 0.1,
)
tiny_result = compute_water_survey_areas(FLAT_DEM, TINY_BOUNDARY, soil_inputs=GOOD_WET_SOIL_INPUTS)
tiny_zones = tiny_result["zones_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(tiny_zones) == 1, "a sub-floor zone must be PRESENT, never dropped"
tiny_zone = tiny_zones[0]
assert tiny_zone["cell_count"] == 9
assert tiny_zone["member_acres"] < MIN_SURVEY_REGION_AREA_ACRES
assert tiny_zone["below_min_area"] is True and FLAG_BELOW_MIN_AREA in tiny_zone["flags"], (
    "below the floor is a FLAG on member acreage carrying the exact number, not a filter"
)
assert tiny_result["selected_water_zone"] is tiny_zone, (
    "first-run posture: flags never affect selection -- the flagged zone still wins an empty field"
)
print("Fixture 3 (sub-floor): 9-cell zone present with below_min_area flag on member acreage; still selectable.")


# =========================================================================
# 4. CONTRACT -- the selected ZONE is a selected_water_zone
# =========================================================================

selected = flat_result["selected_water_zone"]
assert selected is not None and isinstance(selected, dict) and selected, (
    "the contract is 'non-empty dict or None' -- truthiness gates in solar/tree/fencing depend on it"
)
assert selected is flat_zone, "the pooled rank-1 ZONE is the selection"

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
# the dual-acreage numbers, and carries the parcel-relative TWI caveat:
narrative = build_narrative_data(hit_result)
json.dumps(narrative)
assert narrative["zone_found"] is True
assert narrative["twi_is_parcel_relative"] is True and "THIS parcel" in narrative["twi_note"]
assert len(narrative["zones"]) == len(hit_result["zones"]), "narrative lists ALL zones, no cap"
zone_block = narrative["zones"][0]
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

EXPORT_PATH = "/tmp/claude-0/-home-user/3daf47c5-7be7-5f2b-ada4-aac56a622357/scratchpad/water_survey_areas_test.geojson"
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
print(f"Export: {export['feature_count']} features; zone + member layers with linkage both ways; all geometries parse.")

# GREP-ASSERT: no serialization-time reprojection in any emitter.
for emitter in (
    survey_areas_to_geojson,
    wsa._zone_feature_properties,
    wsa._member_feature_properties,
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
# And the threshold comparison prints, on RAW surfaces:
comparison = diag.summarize_threshold_comparison(identify_like, FLAT_DEM)
assert "THRESHOLD COMPARISON (raw surfaces" in comparison and "t=0.6" in comparison and "<- default" in comparison
print("Instrumentation rider: soil sub-signals + map unit in the deepest-fill table; threshold comparison prints on raw surfaces.")

print("\nAll water_survey_areas checks passed.")
