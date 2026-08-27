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
# 3. REGION EXTRACTION (+ shared fixtures for sections 4 and 6)
# =========================================================================

# --- FIXTURE 1: uniform wet flat -> ONE excavated region of known size.
# 20x20 flat DEM at 100.0 m; boundary covers exactly the 10x10 block of
# cell centers rows/cols 5..14 (edges at half-cell offsets so center
# containment is unambiguous). Soil: one map unit covering everything,
# best-case wet (ksat 0.05 -> 1.0, group D -> 1.0, 100% hydric -> 1.0
# => soil grid 1.0, coverage 1.0).
# Hand-derivation of the excavated surface on this fixture:
#   flat filled DEM -> every D8 direction is a flat tie -> accumulation
#     1 everywhere -> run-on = (1*0.0061776)/2 = 0.0030888
#   slope 0 (interior cells) -> excavated slope 1.0
#   TWI all equal -> parcel-relative mean-rank 0.5; depression 0
#     -> wetness = 0.5*0.5 + 0.5*0 = 0.25
#   excavated = .35*.25 + .30*1 + .25*1 + .10*0.0030888 = 0.63780888
# and the embankment surface: drainage 0 (under 0.5 ac), slope 0 (under
# the 0.5% floor), soil .25, twi .10 -> 0.35 < 0.5 -> NO embankment
# region. So: exactly one region, excavated, all 100 gated cells.
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
    "hydrologic_group_rows": [{"mukey": "1", "hydgrp": "D"}],
    "components": [{"mukey": "1", "hydricrating": "Yes", "comppct_r": 100}],
    "geometries_by_mukey": {"1": transform_geom(CRS, "EPSG:4326", mapping(FLAT_BOUNDARY.buffer(20.0)))},
}

flat_result = compute_water_survey_areas(FLAT_DEM, FLAT_BOUNDARY, soil_inputs=GOOD_WET_SOIL_INPUTS)

assert flat_result["gate_mask_stats"]["gated_cells"] == 100, "the boundary covers exactly 100 cell centers"
excavated_regions = flat_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(excavated_regions) == 1, f"uniform wet flat must yield exactly ONE excavated region, got {len(excavated_regions)}"
assert flat_result["regions_by_type"][SURVEY_TYPE_EMBANKMENT] == [], (
    "flat ground with no catchment must yield NO embankment region (surface 0.35 < 0.5)"
)
flat_region = excavated_regions[0]
assert flat_region["cell_count"] == 100, f"the region is ALL 100 gated cells, got {flat_region['cell_count']}"
expected_flat_score = 0.35 * 0.25 + 0.30 * 1.0 + 0.25 * 1.0 + 0.10 * (CA / 2.0)
assert math.isclose(flat_region["mean_suitability"], round(expected_flat_score, 4)), (
    f"hand-derived mean {round(expected_flat_score, 4)}, got {flat_region['mean_suitability']}"
)
assert flat_region["max_suitability"] == flat_region["mean_suitability"], "uniform fixture -> uniform score"
assert math.isclose(flat_region["area_acres"], round(100 * CA, 4)), "acreage is cell-count x cell area"
assert flat_region["below_min_area"] is False and FLAG_BELOW_MIN_AREA not in flat_region["flags"]
assert flat_region["twi_percentile_mean"] == 0.5, "flat ties read the neutral mean-rank 0.5"
assert flat_region["depression_depth_max_m"] == 0.0
assert flat_region["soil_coverage_fraction"] == 1.0
assert flat_region["criteria_complete"] is True
assert flat_region["confidence"] == "high", "soil coverage + complete criteria = 2 signals = HIGH"
assert flat_region["slope_median_pct"] == 0.0
assert FLAG_NO_SERVICE_RELATIONSHIP in flat_region["flags"], (
    "no production areas supplied -> the no-service case is a FLAG, never a drop"
)
print(
    f"Fixture 1 (uniform wet flat): one excavated region, 100 cells, mean {flat_region['mean_suitability']} "
    "hand-derived, confidence high, no-service FLAGGED not dropped."
)

# --- FIXTURE 2: a hand-built accumulation ribbon -> one embankment
# ribbon along the channel. 40x21 V-valley: elevation =
# 100 + |c-10|*0.30 - r*0.25. Every cell's max-neighbor grade is the
# downhill diagonal into the channel: (0.30+0.25)/hypot(5,5) = 7.78%
# (uniform!), inside the 3-8% sweet spot -> embankment slope 1.0,
# excavated slope 1-7.78/8 = 0.0275. Boundary covers centers rows 2..37
# x cols 2..18 (612 cells, all with full Horn neighborhoods).
# flow_accumulation is supplied as an OVERRIDE, hand-built: 1 cell
# everywhere except the channel column (c=10), which carries
# 15*(r+1) cells -- so drainage acres cross the 0.5 ac band start at
# 15*(r+1)*0.0061776 >= 0.5 and reach full credit at 2 ac, all
# hand-computable. TWI (uniform tan) orders exactly by accumulation:
#   576 side cells all equal -> mean-rank (0 + 0.5*575)/611 = 0.470...
#   36 on-parcel channel cells distinct, ranks (576+i)/611, i by row.
# Per-cell embankment (soil never checked -> neutral 0.5):
#   side:    .30*0 + .25*1 + .25*.5 + .20*.4705 = 0.4691 < 0.5 -> out
#   channel: .30*d(r) + .375 + .20*(576+i)/611 >= 0.5635      -> in
# => the region is EXACTLY the 36 on-parcel channel cells: a 1-cell-wide
# ribbon, its mean hand-summable from the same formulas.
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

v_regions = v_result["regions_by_type"][SURVEY_TYPE_EMBANKMENT]
assert len(v_regions) == 1, f"the drainage band must carve exactly one embankment ribbon, got {len(v_regions)}"
ribbon = v_regions[0]
assert ribbon["cell_count"] == 36, f"hand-counted ribbon: the 36 on-parcel channel cells, got {ribbon['cell_count']}"
assert all(c == V_CHANNEL for _, c in ribbon["cells"]), "every ribbon cell hugs the channel column"
assert sorted(r for r, _ in ribbon["cells"]) == list(range(2, 38)), "the ribbon spans on-parcel rows 2..37"

# Hand-sum the expected mean from the stated per-cell formula:
expected_cells = []
for i, r in enumerate(range(2, 38)):
    acres = 15 * (r + 1) * CA
    d = min(max((acres - 0.5) / 1.5, 0.0), 1.0)
    twi_pct = (576 + i) / 611
    expected_cells.append(0.30 * d + 0.25 * 1.0 + 0.25 * 0.5 + 0.20 * twi_pct)
assert math.isclose(ribbon["mean_suitability"], round(float(np.mean(expected_cells)), 4)), (
    f"hand-summed ribbon mean {round(float(np.mean(expected_cells)), 4)}, got {ribbon['mean_suitability']}"
)
assert v_result["regions_by_type"][SURVEY_TYPE_EXCAVATED] == [], (
    "7.78% ground is embankment territory -- no excavated region on this fixture"
)
# The ribbon's wettest cell is the bottom channel cell (highest TWI), and
# its contributing area is the hand-built 15*38 cells:
assert ribbon["wettest_cell_rowcol"] == (37, V_CHANNEL)
assert math.isclose(ribbon["contributing_area_acres_at_wettest_cell"], round(15 * 38 * CA, 2))
# An interior ribbon touches the boundary only at its two 5m ends:
assert ribbon["boundary_adjacency_fraction"] < 0.1, (
    f"an interior ribbon barely touches the boundary, got {ribbon['boundary_adjacency_fraction']}"
)
print(
    f"Fixture 2 (V-valley + hand-built accumulation): one 36-cell embankment ribbon along the channel, "
    f"mean {ribbon['mean_suitability']} hand-summed, wettest cell at the valley bottom."
)

# --- FIXTURE 3: a sub-floor region is FLAGGED AND PRESENT (and can even
# be selected -- first-run posture). Same wet-flat construction, but the
# boundary covers only a 3x3 block: 9 cells = 0.0556 ac < the 0.1 ac
# floor. ---
TINY_BOUNDARY = box(
    ORIGIN_X + 8 * RESOLUTION + 0.1,
    ORIGIN_Y - 11 * RESOLUTION + 0.1,
    ORIGIN_X + 11 * RESOLUTION - 0.1,
    ORIGIN_Y - 8 * RESOLUTION - 0.1,
)
tiny_soil = dict(GOOD_WET_SOIL_INPUTS)
tiny_result = compute_water_survey_areas(FLAT_DEM, TINY_BOUNDARY, soil_inputs=tiny_soil)
tiny_regions = tiny_result["regions_by_type"][SURVEY_TYPE_EXCAVATED]
assert len(tiny_regions) == 1, "a sub-floor region must be PRESENT, never dropped"
tiny_region = tiny_regions[0]
assert tiny_region["cell_count"] == 9
assert tiny_region["area_acres"] < MIN_SURVEY_REGION_AREA_ACRES
assert tiny_region["below_min_area"] is True and FLAG_BELOW_MIN_AREA in tiny_region["flags"], (
    "below the floor is a FLAG carrying the exact acreage, not a filter"
)
assert tiny_result["selected_water_zone"] is tiny_region, (
    "first-run posture: flags never affect selection -- the flagged region still wins an empty field"
)
print("Fixture 3 (sub-floor): 9-cell region present with below_min_area flag and its exact acreage; still selectable.")


# =========================================================================
# 4. CONTRACT -- the selected region IS a selected_water_zone
# =========================================================================

selected = flat_result["selected_water_zone"]
assert selected is not None and isinstance(selected, dict) and selected, (
    "the contract is 'non-empty dict or None' -- truthiness gates in solar/tree/fencing depend on it"
)

# The three fields production consumers dereference directly
# (pipeline_context keypoint pass, road/solar/tree/fencing geometry,
# render_layout_map's ripple clip and its id log line):
assert selected["render_fill_polygon_utm"] is selected["polygon_utm"], (
    "render_fill_polygon_utm must be the IDENTITY of polygon_utm -- no morphology exists in this module"
)
assert selected["render_fill_geometry_wgs84"] is selected["geometry_wgs84"], "same identity on the wire form"
assert isinstance(selected["representative_elevation_m"], float)
assert selected["representative_elevation_m"] == 100.0, "median raw elevation of a flat-100 fixture"
assert isinstance(selected["id"], int)
# Fields the pipeline tests read tolerantly on the selected zone:
assert selected["rank"] == 1
assert selected["served_production_area_ids"] == []

# Exercise the ACTUAL consumer access patterns so a break here fails
# loudly before any pipeline run:
from shapely.ops import unary_union  # noqa: E402

_ = selected["render_fill_polygon_utm"].buffer(6.096)          # road_corridors pond exclusion
_ = unary_union([selected["render_fill_polygon_utm"]])          # solar water_zones union
_ = selected["render_fill_polygon_utm"] if selected else None   # fencing truthiness guard
_ = 101.5 - selected["representative_elevation_m"]              # keypoint elevation differential
_ = f"Water zone {selected['id']}: log line"                    # render_layout_map id branch

# Stored WGS84 beside UTM everywhere: regions and their geojson features.
for region in flat_result["regions"] + v_result["regions"]:
    assert not region["polygon_utm"].is_empty
    assert isinstance(region["geometry_wgs84"], dict) and "coordinates" in region["geometry_wgs84"]

# Sentinel semantics for all three overlaps.
# Never checked (defaults): all three None.
assert flat_region["canopy_overlap_pct"] is None
assert flat_region["road_overlap_pct"] is None
assert flat_region["production_overlap_pct"] is None
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
checked_region = checked_result["regions_by_type"][SURVEY_TYPE_EXCAVATED][0]
assert checked_region["canopy_overlap_pct"] == 0.0, "checked-and-clear canopy is 0.0, never None"
assert checked_region["road_overlap_pct"] == 0.0, "a real None road union means CHECKED, genuinely no road: 0.0"
assert checked_region["production_overlap_pct"] == 0.0, "an empty production list is a real checked answer: 0.0"
# Checked-and-hit: canopy over half the parcel, production over all of it.
half_canopy = np.zeros(FLAT_DEM["array"].shape, dtype=bool)
half_canopy[:, :10] = True  # cols 5..9 of the region's 10 = 50%
production_patch = {
    "id": 7,
    "polygon_utm": FLAT_BOUNDARY.buffer(30.0),
    "render_fill_polygon_utm": FLAT_BOUNDARY.buffer(30.0),
    "representative_elevation_m": 150.0,  # 50 m ABOVE the region -> pump required
}
hit_result = compute_water_survey_areas(
    FLAT_DEM,
    FLAT_BOUNDARY,
    production_areas=[production_patch],
    canopy_root_zone_mask_utm=half_canopy,
    soil_inputs=GOOD_WET_SOIL_INPUTS,
)
hit_region = hit_result["regions_by_type"][SURVEY_TYPE_EXCAVATED][0]
assert hit_region["canopy_overlap_pct"] == 50.0
assert hit_region["production_overlap_pct"] == 100.0
# PUMP-REQUIRED survives with its note, as ranking context -- never a gate:
primary = hit_region["primary_production_area_relationship"]
assert primary is not None and primary["above_production_area"] is False
assert hit_region["has_service_relationship"] is True
assert hit_region["served_production_area_ids"] == [7]
assert FLAG_NO_SERVICE_RELATIONSHIP not in hit_region["flags"]
assert "PUMP-REQUIRED" in hit_region["confidence_notes"], "the pump case carries its note and survives"
assert hit_result["selected_water_zone"] is hit_region, "a pump-required region still selects -- gravity never gates"

# Boundary adjacency on a region hugging the fixture edge: fixture 1's
# region is clipped to the boundary on all four sides, so its perimeter
# IS the boundary -- adjacency 1.0.
assert flat_region["boundary_adjacency_fraction"] == 1.0, (
    f"a region filling the parcel abuts the line on every side, got {flat_region['boundary_adjacency_fraction']}"
)
print("Contract: consumer fields + access patterns, render_fill identity, overlap sentinels, PUMP-REQUIRED survives, boundary adjacency 1.0 on the hugging fixture.")

# narrative_data is FINAL and JSON-serializable, lists ALL regions, and
# carries the parcel-relative TWI caveat:
narrative = build_narrative_data(hit_result)
json.dumps(narrative)
assert narrative["region_found"] is True
assert narrative["twi_is_parcel_relative"] is True and "THIS parcel" in narrative["twi_note"]
assert len(narrative["regions"]) == len(hit_result["regions"]), "narrative lists ALL regions, no cap"
assert narrative["regions"][0]["criteria"].keys() == EXCAVATED_WEIGHTS.keys(), (
    "per-criterion mean scores ride along -- the narrative-honesty mechanism"
)
assert narrative["selection"]["selected_region_id"] == hit_region["id"]
gravity_block = narrative["regions"][0]["gravity"]
assert gravity_block["can_gravity_feed"] is False and gravity_block["production_area_id"] == 7
print("narrative_data: JSON-clean, all regions, per-criterion scores, TWI caveat, pump case surfaced.")


# =========================================================================
# 6. EXPORT VALIDATION
# =========================================================================

identify_like = {
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
    assert bands, f"isoband bands must be present for {survey_type} (its surface is nonzero on-parcel)"
    for band in bands:
        assert not band["polygons_utm"].is_empty
        assert isinstance(band["geometry_wgs84"], dict), "isobands carry BOTH forms, built at band birth"

# WGS84 boundary ring for the context layer (the fixture's own wire form):
boundary_wgs84 = transform_geom(CRS, "EPSG:4326", mapping(FLAT_BOUNDARY))
boundary_coords_wgs84 = [tuple(point) for point in boundary_wgs84["coordinates"][0]]

EXPORT_PATH = "/tmp/claude-0/-home-user/3daf47c5-7be7-5f2b-ada4-aac56a622357/scratchpad/water_survey_areas_test.geojson"
export = diag.export_water_survey_areas_geojson(
    identify_like,
    boundary_coords_wgs84,
    [{**production_patch, "geometry_wgs84": transform_geom(CRS, "EPSG:4326", mapping(production_patch["polygon_utm"])), "area_acres": 1.0}],
    isobands_by_type,
    path=EXPORT_PATH,
)

with open(EXPORT_PATH, encoding="utf-8") as handle:
    collection = json.load(handle)
assert collection["type"] == "FeatureCollection"
assert len(collection["features"]) == export["feature_count"]
for feature in collection["features"]:
    shape(feature["geometry"])  # every geometry parses with shapely
by_layer = export["by_layer"]
assert by_layer.get("survey_region_excavated", 0) == 1, "the fixture's one region rides the excavated layer"
assert by_layer.get("suitability_isoband_embankment", 0) >= 1
assert by_layer.get("suitability_isoband_excavated", 0) >= 1
assert by_layer.get("survey_context_boundary", 0) == 1
assert by_layer.get("survey_context_production_area", 0) == 1
boundary_feature = next(f for f in collection["features"] if f["properties"]["layer"] == "survey_context_boundary")
assert boundary_feature["properties"]["gated_cells"] == hit_result["gate_mask_stats"]["gated_cells"], (
    "the boundary context feature carries the gate-mask summary"
)
print(f"Export: {export['feature_count']} features, all layers present, geometries shapely-parse, gate stats on the boundary feature.")

# GREP-ASSERT: no serialization-time reprojection. Every emitter consumes
# stored wire forms; transform_geom may appear only where objects are
# BORN (region birth in extract_survey_regions, band birth in
# compute_suitability_isobands).
for emitter in (
    survey_areas_to_geojson,
    wsa._region_feature_properties,
    diag._isoband_features,
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
zones_geojson = survey_areas_to_geojson(hit_result["regions"])
from feature_schema import validate_feature_collection  # noqa: E402

validate_feature_collection(zones_geojson)
assert zones_geojson["features"][0]["geometry"] is hit_result["regions"][0]["geometry_wgs84"], (
    "the wire feature carries the stored WGS84 object itself, not a rebuild"
)
json.dumps(zones_geojson)
print("zones_geojson: schema-valid, JSON-clean, stored-geometry identity.")

print("\nAll water_survey_areas checks passed.")
