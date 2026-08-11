"""
test_road_cost_path.py

Offline (no-network) checks for road_cost_path.build_cost_raster()'s three
new optional terms -- TPI ridge-preference, production-land traversal
penalty, and the opt-in hard grade ceiling -- plus the mandatory
positivity assertion that guards Dijkstra's optimality guarantee. Hand-
derived values throughout, same script-style convention as
test_topographic_position.py. See road_cost_path.py's own __main__ block
for the pre-existing least_cost_path()/cost_distance_field() smoke test,
unchanged and re-run separately to confirm this extension didn't touch
any of that.
"""

import math

import numpy as np

from road_cost_path import (
    FLOODPLAIN_CROSSING_COST_PENALTY,
    GRADE_PENALTY_WEIGHT,
    PRODUCTION_TRAVERSAL_COST_MULTIPLIER,
    TPI_REFERENCE_METERS,
    build_cost_raster,
)

RESOLUTION = (5.0, 5.0)

# tanh(1) to full precision, hand-verified -- TPI_REFERENCE_METERS is 3.0,
# so a tpi=+-3.0 cell exercises exactly tanh(3.0 / 3.0) == tanh(1).
TANH_1 = 0.7615941559557649


def _dem(shape) -> dict:
    return {
        "array": np.zeros(shape, dtype=np.float32),
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


# --- 1. Backward compatibility (the critical one): tpi=None,
# --- production_mask=None, impassable_grade_pct=None must reproduce the
# --- pre-extension formula EXACTLY, inf placement included. ---

size = 7
slope = np.array([[0.0, 3.0, 8.0, 12.5, 20.0, 6.0, 1.0]] * size, dtype=np.float64)
slope[2, 2] = np.nan  # exercise the slope-NaN hard-exclusion path too

excluded_mask = np.zeros((size, size), dtype=bool)
excluded_mask[0, 0] = True
excluded_mask[3, 3] = True

floodplain_mask = np.zeros((size, size), dtype=bool)
floodplain_mask[1, :] = True
floodplain_mask[5, 5] = True

dem = _dem((size, size))
actual = build_cost_raster(dem, slope, excluded_mask, floodplain_mask=floodplain_mask)

hard_excluded_expected = excluded_mask | np.isnan(slope)
expected = np.full((size, size), 1.0, dtype=np.float64)
expected += GRADE_PENALTY_WEIGHT * np.where(~hard_excluded_expected, slope, 0.0) ** 2
expected[floodplain_mask] += FLOODPLAIN_CROSSING_COST_PENALTY
expected[hard_excluded_expected] = np.inf

assert np.array_equal(actual, expected), (
    f"backward-compat mismatch: build_cost_raster() with every new parameter left at its default "
    f"must reproduce the pre-extension formula exactly.\nactual=\n{actual}\nexpected=\n{expected}"
)
assert np.array_equal(np.isinf(actual), np.isinf(expected)), "inf placement diverged from the pre-extension formula"
print("1. Backward compatibility: raster is exactly array-equal (inf placement included) to the pre-extension formula.")


# --- 2. Grade term, currently untested on its own ---

slopes_only = np.array([[0.0, 5.0, 10.0, 15.0, 25.0]], dtype=np.float64)
dem2 = _dem((1, 5))
no_exclusion = np.zeros((1, 5), dtype=bool)
cost2 = build_cost_raster(dem2, slopes_only, no_exclusion)
expected2 = 1.0 + 0.0133 * slopes_only ** 2

assert np.array_equal(cost2, expected2), f"grade term mismatch: {cost2} != {expected2}"
assert math.isclose(cost2[0, 3], 3.9925, abs_tol=1e-9), f"15%% grade cell should be 3.9925, got {cost2[0, 3]}"
print(f"2. Grade term (no other terms): {cost2.tolist()} == 1.0 + 0.0133*slope**2 exactly (15%% -> ~3.9925).")


# --- 3. Floodplain term, currently untested on its own ---

dem3 = _dem((1, 2))
flat_slope = np.zeros((1, 2), dtype=np.float64)
no_exclusion3 = np.zeros((1, 2), dtype=bool)
floodplain3 = np.array([[True, False]])
cost3 = build_cost_raster(dem3, flat_slope, no_exclusion3, floodplain_mask=floodplain3)

assert cost3[0, 0] == 6.0, f"masked flat cell should be exactly 6.0, got {cost3[0, 0]}"
assert cost3[0, 1] == 1.0, f"unmasked flat cell should be exactly 1.0, got {cost3[0, 1]}"
print(f"3. Floodplain term (flat ground): masked cell={cost3[0, 0]}, unmasked cell={cost3[0, 1]}.")


# --- 4. TPI factor: flat ground, no other terms ---

dem4 = _dem((1, 5))
flat_slope4 = np.zeros((1, 5), dtype=np.float64)
no_exclusion4 = np.zeros((1, 5), dtype=bool)
tpi4 = np.array([[0.0, 3.0, -3.0, 100.0, -100.0]])
cost4 = build_cost_raster(dem4, flat_slope4, no_exclusion4, tpi=tpi4)

expected_zero = 1.0
expected_pos3 = 1.0 - 0.5 * TANH_1
expected_neg3 = 1.0 + 0.5 * TANH_1

assert cost4[0, 0] == expected_zero, f"tpi=0.0 should give factor 1.0, got {cost4[0, 0]}"
assert math.isclose(cost4[0, 1], expected_pos3, abs_tol=1e-12), f"tpi=+3.0 should give {expected_pos3}, got {cost4[0, 1]}"
assert math.isclose(cost4[0, 2], expected_neg3, abs_tol=1e-12), f"tpi=-3.0 should give {expected_neg3}, got {cost4[0, 2]}"

# Extremes converge toward the (1 - strength, 1 + strength) bounds -- mathematically
# tanh(x) < 1 for every finite x, so the factor mathematically never reaches 0.5/1.5
# exactly; at tpi=+-100.0 (x=+-33.3) tanh's float64 rounding lands on bit-exact 1.0
# (1-tanh(33.3) is ~1e-29, far below float64's ~2.2e-16 epsilon), so the computed
# factor rounds to bit-exact 0.5/1.5 too -- an IEEE-754 rounding artifact, not a
# violation of tanh's own open-interval range. The bound is therefore checked as
# <= / >=, while the monotonic "closer to the bound than the milder +-3.0 case" part
# of "converges toward" is still checked strictly.
assert cost4[0, 3] <= 0.5, f"tpi=+100.0 should approach (and, at float64 precision, reach) 0.5, got {cost4[0, 3]}"
assert cost4[0, 3] < expected_pos3, f"tpi=+100.0 should be closer to 0.5 than tpi=+3.0, got {cost4[0, 3]} vs {expected_pos3}"
assert cost4[0, 4] >= 1.5, f"tpi=-100.0 should approach (and, at float64 precision, reach) 1.5, got {cost4[0, 4]}"
assert cost4[0, 4] > expected_neg3, f"tpi=-100.0 should be closer to 1.5 than tpi=-3.0, got {cost4[0, 4]} vs {expected_neg3}"
print(
    f"4. TPI factor (flat ground): tpi=0.0 -> {cost4[0, 0]}, tpi=+3.0 -> {cost4[0, 1]:.10f}, "
    f"tpi=-3.0 -> {cost4[0, 2]:.10f}, tpi=+100.0 -> {cost4[0, 3]:.10f} (<=0.5), tpi=-100.0 -> {cost4[0, 4]:.10f} (>=1.5)."
)


# --- 5. TPI does not scale the grade (or floodplain) penalty ---

dem5 = _dem((1, 1))
slope5 = np.array([[15.0]])
no_exclusion5 = np.array([[False]])
tpi5 = np.array([[3.0]])
cost5 = build_cost_raster(dem5, slope5, no_exclusion5, tpi=tpi5)

expected5 = 1.0 * (1.0 - 0.5 * TANH_1) + 0.0133 * 225.0
wrong_fully_scaled = (1.0 + 0.0133 * 225.0) * (1.0 - 0.5 * TANH_1)

assert math.isclose(cost5[0, 0], expected5, abs_tol=1e-12), f"expected {expected5}, got {cost5[0, 0]}"
assert not math.isclose(cost5[0, 0], wrong_fully_scaled, abs_tol=1e-6), (
    "TPI factor must scale ONLY the base travel term, not the whole sum -- "
    f"got a value ({cost5[0, 0]}) matching the fully-scaled (wrong) formula ({wrong_fully_scaled})"
)
print(f"5. TPI+grade (tpi=+3.0, slope=15%%): {cost5[0, 0]:.10f} == base*(1-0.5*tanh(1)) + 0.0133*225, not scaled as a whole.")


# --- 6. NaN TPI is neutral, not a hard exclusion ---

dem6 = _dem((1, 1))
slope6 = np.array([[10.0]])
no_exclusion6 = np.array([[False]])
tpi_nan = np.array([[np.nan]])

cost6_with_nan_tpi = build_cost_raster(dem6, slope6, no_exclusion6, tpi=tpi_nan)
cost6_without_tpi = build_cost_raster(dem6, slope6, no_exclusion6, tpi=None)

assert math.isfinite(cost6_with_nan_tpi[0, 0]), f"NaN tpi must not make a cell impassable, got {cost6_with_nan_tpi[0, 0]}"
assert cost6_with_nan_tpi[0, 0] == cost6_without_tpi[0, 0], (
    f"a NaN tpi cell must cost exactly the same as if tpi had not been supplied at all: "
    f"{cost6_with_nan_tpi[0, 0]} != {cost6_without_tpi[0, 0]}"
)
print(f"6. NaN TPI is neutral: cost with tpi=NaN ({cost6_with_nan_tpi[0, 0]}) == cost with tpi=None ({cost6_without_tpi[0, 0]}), both finite.")


# --- 7. Production multiplier applies to the TOTAL, not just base travel ---

dem7 = _dem((1, 1))
slope7 = np.array([[10.0]])
no_exclusion7 = np.array([[False]])
floodplain7 = np.array([[True]])
production7 = np.array([[True]])
cost7 = build_cost_raster(dem7, slope7, no_exclusion7, floodplain_mask=floodplain7, production_mask=production7)

expected7 = 1.0 + GRADE_PENALTY_WEIGHT * 100.0
expected7 += FLOODPLAIN_CROSSING_COST_PENALTY
expected7 *= PRODUCTION_TRAVERSAL_COST_MULTIPLIER

assert cost7[0, 0] == expected7, f"expected {expected7}, got {cost7[0, 0]}"
print(f"7. Production multiplier (slope=10%%, floodplain=True): {cost7[0, 0]} == (1.0 + 0.0133*100 + 5.0) * 3.0.")


# --- 8. Grade ceiling: strictly greater-than, opt-in only ---

dem8 = _dem((1, 3))
slopes8 = np.array([[14.9, 15.1, 15.0]])
no_exclusion8 = np.zeros((1, 3), dtype=bool)

cost8_ceiling = build_cost_raster(dem8, slopes8, no_exclusion8, impassable_grade_pct=15.0)
assert math.isfinite(cost8_ceiling[0, 0]), "14.9%% grade must be finite under a 15.0%% ceiling"
assert math.isinf(cost8_ceiling[0, 1]), "15.1%% grade must be inf under a 15.0%% ceiling"
assert math.isfinite(cost8_ceiling[0, 2]), "15.0%% grade must be finite under a 15.0%% ceiling (strictly greater-than)"

cost8_no_ceiling = build_cost_raster(dem8, slopes8, no_exclusion8, impassable_grade_pct=None)
assert math.isfinite(cost8_no_ceiling[0, 0])
assert math.isfinite(cost8_no_ceiling[0, 1])
assert math.isfinite(cost8_no_ceiling[0, 2])
print(
    "8. Grade ceiling (impassable_grade_pct=15.0): 14.9%% finite, 15.1%% inf, 15.0%% finite (strictly greater-than); "
    "impassable_grade_pct=None leaves all three finite."
)


# --- 9. Positivity assertion fires on a misconfigured strength ---

dem9 = _dem((1, 1))
slope9 = np.array([[0.0]])
no_exclusion9 = np.array([[False]])
tpi9 = np.array([[1000.0]])

raised_value_error = False
try:
    build_cost_raster(dem9, slope9, no_exclusion9, tpi=tpi9, tpi_preference_strength=1.5)
except ValueError:
    raised_value_error = True

assert raised_value_error, "tpi_preference_strength=1.5 with a strongly positive tpi must raise ValueError"
print("9. Positivity assertion fires: tpi_preference_strength=1.5 with strongly positive tpi raises ValueError.")


# --- 10. Hard exclusion wins over every soft term ---

dem10 = _dem((1, 1))
slope10 = np.array([[0.0]])
excluded10 = np.array([[True]])
production10 = np.array([[True]])
tpi10 = np.array([[1000.0]])
cost10 = build_cost_raster(dem10, slope10, excluded10, tpi=tpi10, production_mask=production10)

assert math.isinf(cost10[0, 0]), (
    f"a cell in excluded_mask AND production_mask with strongly positive tpi must be inf, got {cost10[0, 0]}"
)
print("10. Hard exclusion wins: excluded_mask + production_mask + strongly positive tpi cell is inf, not a scaled finite value.")

print("\nAll test_road_cost_path.py checks passed.")
