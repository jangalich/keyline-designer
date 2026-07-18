"""
test_terrain_metrics.py

Offline (no-network) checks for terrain_metrics.py's slope/aspect/shading
derivation — all pure numpy against small synthetic DEM arrays.
"""

import math

import numpy as np

from terrain_metrics import (
    aspect_score,
    aspect_to_compass_label,
    compute_shading_score,
    compute_slope_and_aspect,
)

RESOLUTION = (5.0, 5.0)


# --- compute_slope_and_aspect: a south-facing slope reads as aspect=180 ---

size = 7
south_facing = np.zeros((size, size), dtype=np.float32)
for row in range(size):
    for col in range(size):
        south_facing[row, col] = (size - 1 - row) * 2.0  # higher to the north

slope, aspect = compute_slope_and_aspect(south_facing, RESOLUTION)
assert slope[3, 3] > 0, "a real, uniform grade should have nonzero slope"
assert abs(aspect[3, 3] - 180.0) < 1.0, f"expected south-facing aspect (~180), got {aspect[3, 3]}"
print("compute_slope_and_aspect reads a south-facing slope as aspect ~180 deg.")

north_facing = np.zeros((size, size), dtype=np.float32)
for row in range(size):
    for col in range(size):
        north_facing[row, col] = row * 2.0  # higher to the south -> slope faces north

_, aspect_n = compute_slope_and_aspect(north_facing, RESOLUTION)
assert abs(aspect_n[3, 3] - 0.0) < 1.0 or abs(aspect_n[3, 3] - 360.0) < 1.0, (
    f"expected north-facing aspect (~0/360), got {aspect_n[3, 3]}"
)
print("compute_slope_and_aspect reads a north-facing slope as aspect ~0/360 deg.")

flat = np.full((size, size), 50.0, dtype=np.float32)
flat_slope, flat_aspect = compute_slope_and_aspect(flat, RESOLUTION)
assert flat_slope[3, 3] == 0.0
assert math.isnan(flat_aspect[3, 3]), "aspect should be undefined (NaN) on perfectly flat ground"
print("compute_slope_and_aspect reads 0% slope and undefined aspect on flat ground.")


# --- aspect_score: south is best, north is worst, flat (NaN) is neutral-best ---

assert aspect_score(180.0) == 1.0
assert aspect_score(0.0) == 0.0
assert 0.4 < aspect_score(90.0) < 0.6  # east/west should land near the midpoint
assert aspect_score(float("nan")) == 1.0, "flat ground (no aspect) shouldn't be penalized"
print("aspect_score peaks south, bottoms out north, and treats flat ground as neutral-best.")

assert aspect_to_compass_label(180.0) == "S"
assert aspect_to_compass_label(0.0) == "N"
assert aspect_to_compass_label(225.0) == "SW"
print("aspect_to_compass_label maps bearings to the expected compass points.")


# --- compute_shading_score: a real obstruction to the south lowers the score ---

size2 = 15
terrain = np.zeros((size2, size2), dtype=np.float32)
terrain[9, 5:10] = 5.0  # a modest ridge south of a candidate point

shaded_scores = compute_shading_score(terrain, RESOLUTION)
candidate_under_ridge = shaded_scores[5, 7]  # directly north of the ridge
candidate_open_south = shaded_scores[12, 7]  # south of the ridge -- ridge is behind it (north), irrelevant

assert candidate_under_ridge < 1.0, "a real ridge to the south should reduce the shading score"
assert candidate_open_south == 1.0, (
    "terrain to the NORTH is irrelevant to northern-hemisphere solar shading "
    "and shouldn't affect the score at all"
)
assert candidate_under_ridge < candidate_open_south
print("compute_shading_score penalizes a real southern obstruction and ignores terrain to the north.")

flat_terrain = np.zeros((size2, size2), dtype=np.float32)
flat_scores = compute_shading_score(flat_terrain, RESOLUTION)
assert flat_scores[7, 7] == 1.0, "perfectly flat terrain should have a fully open horizon"
print("compute_shading_score reads a fully open horizon (1.0) on flat terrain.")

print("\nAll terrain_metrics checks passed.")
