"""
test_topographic_position.py

Offline (no-network) checks for topographic_position.py, hand-derived
against small synthetic DEM dicts -- no live DEM fetch (dem_data.py)
anywhere in this chain, same split as test_valley_delineation.py.
"""

import numpy as np

from topographic_position import (
    MIN_TPI_NEIGHBORHOOD_CELLS,
    build_disc_kernel_offsets,
    compute_tpi,
)

RESOLUTION = (5.0, 5.0)
BASE_DEM = {
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}


def _dem(array: np.ndarray, resolution_meters=RESOLUTION) -> dict:
    return {**BASE_DEM, "resolution_meters": resolution_meters, "array": array}


# --- 1. Constant-elevation grid: TPI is 0.0 everywhere in the interior ---

size = 9
constant_array = np.full((size, size), 123.4, dtype=np.float64)
tpi_constant = compute_tpi(_dem(constant_array), radius_meters=12.0)
interior = tpi_constant[2:7, 2:7]
assert np.allclose(interior, 0.0, atol=1e-9), f"constant-elevation grid should have ~0.0 TPI everywhere interior, got {interior}"
print("Constant-elevation grid: TPI is ~0.0 (floating-point exact) across the interior.")


# --- 2. Uniform planar slope: TPI ~0.0 in the interior, cardinal AND diagonal ---

rows_idx, cols_idx = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")

cardinal_slope = (rows_idx * 3.0).astype(np.float64)  # elevation varies only with row
tpi_cardinal = compute_tpi(_dem(cardinal_slope), radius_meters=12.0)
assert np.allclose(tpi_cardinal[2:7, 2:7], 0.0, atol=1e-9), (
    f"cardinal-aligned planar slope should have ~0.0 TPI in the interior, got {tpi_cardinal[2:7, 2:7]}"
)
print("Cardinal-aligned uniform planar slope: TPI ~0.0 across the interior.")

diagonal_slope = (rows_idx * 1.5 + cols_idx * 2.0).astype(np.float64)  # varies with both row and col
tpi_diagonal = compute_tpi(_dem(diagonal_slope), radius_meters=12.0)
assert np.allclose(tpi_diagonal[2:7, 2:7], 0.0, atol=1e-9), (
    f"diagonal planar slope should have ~0.0 TPI in the interior, got {tpi_diagonal[2:7, 2:7]}"
)
print("Diagonal uniform planar slope: TPI ~0.0 across the interior.")


# --- 3. Symmetric roof/ridge DEM: crest positive, mid-flank ~0.0, toe break negative ---
#
# A "gable roof" elevation profile, constant across columns, that varies
# with row only: a linear backslope declining from a crest row, breaking
# to flat ground at a toe distance. Hand-derived below using the exact
# kernel weights build_disc_kernel_offsets() produces for radius=12.0 at
# this DEM's (5.0, 5.0) resolution: 3 cells at |dr|=2, 5 cells at
# |dr| in {0, 1} (21 offsets total; see module docstring/kernel derivation
# in the PR description for the by-hand weight table).

roof_rows = 21
roof_cols = 7
crest_row = 10
slope_per_row = 1.0  # meters of drop per row-step (= 0.2 m/m at this 5m resolution)
toe_break_distance = 6  # rows from the crest where the backslope flattens out
peak_elevation = 100.0
floor_elevation = peak_elevation - slope_per_row * toe_break_distance  # 94.0

roof_array = np.zeros((roof_rows, roof_cols), dtype=np.float64)
for row in range(roof_rows):
    distance = abs(row - crest_row)
    if distance <= toe_break_distance:
        elevation = peak_elevation - slope_per_row * distance
    else:
        elevation = floor_elevation
    roof_array[row, :] = elevation

tpi_roof = compute_tpi(_dem(roof_array), radius_meters=12.0)

# Hand-derived exact values (weights [3, 5, 5, 5, 3] for dr = -2..2, all
# columns constant so the mean only depends on the row weights):
#   crest (row 10, dist 0):       TPI = 22/21 * slope_per_row
#   mid-flank (row 7 / row 13, dist 3, deep in the linear backslope, far
#              from both the crest fold and the toe break):  TPI = 0 exactly
#   toe break (row 4 / row 16, dist 6, exactly at the concave kink where
#              the backslope flattens into the toe): TPI = -11/21 * slope_per_row
expected_crest_tpi = 22.0 / 21.0 * slope_per_row
expected_toe_tpi = -11.0 / 21.0 * slope_per_row

crest_tpi = tpi_roof[crest_row, 3]
mid_flank_tpi = tpi_roof[7, 3]
mid_flank_tpi_mirror = tpi_roof[13, 3]
toe_tpi = tpi_roof[4, 3]
toe_tpi_mirror = tpi_roof[16, 3]

assert np.isclose(crest_tpi, expected_crest_tpi, atol=1e-9), (
    f"crest TPI should be {expected_crest_tpi} (hand-derived), got {crest_tpi}"
)
assert crest_tpi > 0, f"crest cell should be strictly positive TPI (convex), got {crest_tpi}"

assert np.isclose(mid_flank_tpi, 0.0, atol=1e-9) and np.isclose(mid_flank_tpi_mirror, 0.0, atol=1e-9), (
    f"mid-flank cells (deep in the linear backslope) should be ~0.0 TPI (planar), "
    f"got {mid_flank_tpi} and {mid_flank_tpi_mirror}"
)

assert np.isclose(toe_tpi, expected_toe_tpi, atol=1e-9) and np.isclose(toe_tpi_mirror, expected_toe_tpi, atol=1e-9), (
    f"toe-break TPI should be {expected_toe_tpi} (hand-derived), got {toe_tpi} and {toe_tpi_mirror}"
)
assert toe_tpi < 0 and toe_tpi_mirror < 0, (
    f"toe-break cells (concave flank-to-flat kink) should be strictly negative TPI, got {toe_tpi}, {toe_tpi_mirror}"
)

# Crest strictly exceeds every other (full-neighborhood) flank row's TPI.
full_neighborhood_rows = range(2, roof_rows - 2)  # rows with a complete |dr|<=2 kernel
other_row_tpis = [tpi_roof[row, 3] for row in full_neighborhood_rows if row != crest_row]
assert all(crest_tpi > other for other in other_row_tpis), (
    f"crest TPI ({crest_tpi}) should strictly exceed every other flank row's TPI, "
    f"but did not exceed: {[v for v in other_row_tpis if v >= crest_tpi]}"
)
print(
    f"Roof/ridge DEM: crest TPI={crest_tpi:.6f} (> 0, hand-derived {expected_crest_tpi:.6f}), "
    f"mid-flank TPI={mid_flank_tpi:.6f} (~0.0), toe-break TPI={toe_tpi:.6f} "
    f"(< 0, hand-derived {expected_toe_tpi:.6f}); crest strictly exceeds every other flank row."
)


# --- 4. Non-square resolution: kernel is elliptical in cell terms ---

non_square_resolution = (5.0, 10.0)  # finer in x than y
offsets = build_disc_kernel_offsets(non_square_resolution, radius_meters=20.0)

# Hand-derived: condition is hypot(dc*5, dr*10) <= 20.
#   dr=0:  |dc*5| <= 20            -> dc in -4..4  -> 9 offsets
#   dr=+-1: (dc*5)^2 <= 400-100=300 -> |dc| <= 3.46 -> dc in -3..3 -> 7 offsets each
#   dr=+-2: (dc*5)^2 <= 400-400=0   -> dc == 0 only  -> 1 offset each
#   total = 9 + 2*7 + 2*1 = 25
count_dr0 = sum(1 for dr, dc in offsets if dr == 0)
count_dr_extreme = sum(1 for dr, dc in offsets if dr == 2)
total_count = len(offsets)

assert count_dr0 == 9, f"dr=0 row should include 9 cells (dc in -4..4), got {count_dr0}"
assert count_dr_extreme == 1, f"dr=2 row should include exactly 1 cell (dc == 0 only), got {count_dr_extreme}"
assert total_count == 25, f"expected 25 total kernel cells at radius=20.0, resolution=(5.0, 10.0), got {total_count}"
assert count_dr0 > count_dr_extreme, (
    "kernel should be wider along the finer-resolution (x) axis than tall along the coarser (y) axis -- "
    f"an ellipse in cell terms, not a circle (dr=0 count {count_dr0} vs dr=2 count {count_dr_extreme})"
)
print(
    f"Non-square resolution (5.0, 10.0) kernel at radius=20.0: {total_count} total cells, "
    f"{count_dr0} at dr=0 vs {count_dr_extreme} at dr=+-2 -- confirmed elliptical, not circular."
)


# --- 5. NaN handling: a block of nodata doesn't skew neighboring valid means ---

nan_size = 15
nan_array = np.full((nan_size, nan_size), 50.0, dtype=np.float64)
nan_array[5:8, 5:8] = np.nan  # a 3x3 nodata block

tpi_nan = compute_tpi(_dem(nan_array), radius_meters=12.0)

assert np.all(np.isnan(tpi_nan[5:8, 5:8])), "every nodata cell should read NaN in the output"

# A valid cell immediately adjacent to the nodata block, but with enough
# remaining valid neighbors, must still read exactly 0.0 -- if nodata were
# poisoning the mean (e.g. via nan_to_num without a validity mask), the
# mean would be pulled down and this would read positive instead.
adjacent_cell = tpi_nan[6, 4]
assert not np.isnan(adjacent_cell), "a valid cell adjacent to the nodata block should not itself become NaN"
assert adjacent_cell == 0.0, (
    f"a valid cell adjacent to the nodata block, on a constant-elevation grid, should still read exactly "
    f"0.0 TPI (nodata excluded from its mean, not skewing it), got {adjacent_cell}"
)

# Control: a valid cell far from the block reads the same exact 0.0.
control_cell = tpi_nan[12, 12]
assert control_cell == 0.0, f"a valid cell far from any nodata should read exactly 0.0, got {control_cell}"
assert adjacent_cell == control_cell, "nodata-adjacent and nodata-far valid cells should read identically on a constant grid"

print(
    f"NaN handling: nodata block reads NaN throughout; adjacent valid cell reads exactly "
    f"{adjacent_cell} (unskewed, matches the far-field control {control_cell})."
)


# --- 6. 7x7 grid, radius chosen so the kernel is exactly 3x3: fully hand-computed cell ---

# At (5.0, 5.0) resolution, radius=8.0 includes every |dr|<=1, |dc|<=1
# offset (max diagonal-neighbor distance hypot(5, 5) = 7.071 <= 8.0) and
# excludes every 2-cell-away offset (min distance hypot(0, 10) = 10.0 > 8.0)
# -- exactly a 3x3 kernel, 9 cells, no more and no less.
kernel_3x3 = build_disc_kernel_offsets((5.0, 5.0), radius_meters=8.0)
assert sorted(kernel_3x3) == sorted((dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)), (
    f"radius=8.0 at (5.0, 5.0) resolution should produce exactly the 3x3 kernel, got {sorted(kernel_3x3)}"
)

hand_array = np.array(
    [
        [1, 2, 3, 4, 5, 6, 7],
        [2, 3, 4, 5, 6, 7, 8],
        [3, 4, 5, 9, 7, 8, 9],  # deliberate bump at (2, 3) = 9 instead of 6
        [4, 5, 6, 7, 8, 9, 10],
        [5, 6, 7, 8, 9, 10, 11],
        [6, 7, 8, 9, 10, 11, 12],
        [7, 8, 9, 10, 11, 12, 13],
    ],
    dtype=np.float64,
)

tpi_hand = compute_tpi(_dem(hand_array), radius_meters=8.0)

# Named interior cell: (row=3, col=3), elevation 7. Its full 3x3
# neighborhood (rows 2-4, cols 2-4):
#   row 2: [5, 9, 7]   (cols 2, 3, 4)
#   row 3: [6, 7, 8]
#   row 4: [7, 8, 9]
# sum = 21 + 21 + 24 = 66, mean = 66 / 9 = 7.333...,
# TPI = 7 - 66/9 = (63 - 66)/9 = -1/3
expected_hand_tpi = 7.0 - 66.0 / 9.0
assert np.isclose(tpi_hand[3, 3], expected_hand_tpi, atol=1e-9), (
    f"hand-computed cell (3, 3) should be {expected_hand_tpi} (= -1/3), got {tpi_hand[3, 3]}"
)
print(f"7x7 hand-computed cell (row=3, col=3): TPI={tpi_hand[3, 3]:.9f}, expected {expected_hand_tpi:.9f} (-1/3).")


# --- MIN_TPI_NEIGHBORHOOD_CELLS: a valid cell with too few valid neighbors is NaN ---

tiny_dem = _dem(np.array([[10.0]], dtype=np.float64))
tpi_tiny = compute_tpi(tiny_dem, radius_meters=12.0)
assert tpi_tiny.shape == (1, 1)
assert np.isnan(tpi_tiny[0, 0]), (
    f"a single-cell grid has only 1 valid neighbor (itself), well below "
    f"MIN_TPI_NEIGHBORHOOD_CELLS={MIN_TPI_NEIGHBORHOOD_CELLS}, so it must read NaN, got {tpi_tiny[0, 0]}"
)
print(f"A cell with fewer than MIN_TPI_NEIGHBORHOOD_CELLS ({MIN_TPI_NEIGHBORHOOD_CELLS}) valid neighbors reads NaN.")


print("\nAll topographic_position checks passed.")
