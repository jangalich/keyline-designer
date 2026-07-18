"""
test_valley_delineation.py

Offline (no-network) checks for valley_delineation.py — Stage 1 of the
water-system candidate-zone feature: "is the DEM/valley delineation
accurate." Everything here runs against small, synthetic DEM dicts built
by hand, not a real USGS fetch (dem_data.py needs real network access and
isn't exercised here), so these checks are about the terrain-analysis
algorithm itself, independent of whether a specific live DEM fetch worked.
"""

import numpy as np

from valley_delineation import (
    MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES,
    compute_flow_accumulation,
    compute_flow_direction,
    delineate_valleys,
    fill_depressions,
    valleys_to_geojson,
)
from feature_schema import validate_feature_collection

RESOLUTION = (5.0, 5.0)
BASE_DEM = {
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}


def _dem(array: np.ndarray) -> dict:
    return {**BASE_DEM, "array": array}


# --- fill_depressions: a single interior pit gets raised to its spill elevation ---

pit = np.array(
    [
        [10.0, 10.0, 10.0],
        [10.0, 1.0, 10.0],  # a pit well below its surroundings
        [10.0, 10.0, 10.0],
    ],
    dtype=np.float32,
)
filled = fill_depressions(pit)
assert filled[1, 1] == 10.0, f"pit should be filled to its surrounding elevation, got {filled[1, 1]}"
assert filled[0, 0] == 10.0, "cells that weren't pits shouldn't be altered"
print("fill_depressions raises an interior pit to its spill elevation.")


# --- compute_flow_direction: flow points toward the single lowest neighbor ---

ramp = np.array(
    [
        [30.0, 30.0, 30.0],
        [30.0, 20.0, 30.0],
        [30.0, 10.0, 30.0],
    ],
    dtype=np.float32,
)
flow_to_row, flow_to_col = compute_flow_direction(ramp, RESOLUTION)
assert (flow_to_row[1, 1], flow_to_col[1, 1]) == (2, 1), (
    "center cell (elev 20) should flow to the lower cell below it (elev 10), "
    f"got target ({flow_to_row[1, 1]}, {flow_to_col[1, 1]})"
)
print("compute_flow_direction routes each cell to its steepest downhill neighbor.")


# --- compute_flow_accumulation: contributing count grows monotonically downhill ---

column = np.array([[40.0], [30.0], [20.0], [10.0]], dtype=np.float32)
ftr, ftc = compute_flow_direction(column, RESOLUTION)
accumulation = compute_flow_accumulation(column, ftr, ftc)
assert list(accumulation[:, 0]) == [1, 2, 3, 4], (
    f"a straight downhill column should accumulate 1,2,3,4 top-to-bottom, got {list(accumulation[:, 0])}"
)
print("compute_flow_accumulation sums contributing cells correctly down a simple channel.")


# --- delineate_valleys: a synthetic V-shaped valley is found as one primary valley ---

size = 30
array = np.zeros((size, size), dtype=np.float32)
for row in range(size):
    for col in range(size):
        distance_from_valley = abs((size - 1 - row) - col)
        array[row, col] = distance_from_valley * 2.0 + row * 0.5

valleys = delineate_valleys(_dem(array))
assert len(valleys) == 1, f"expected exactly 1 primary valley in the synthetic V-shape, got {len(valleys)}"
valley = valleys[0]
assert valley["max_contributing_area_acres"] >= MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES
assert valley["geometry_wgs84"]["type"] in ("LineString", "MultiLineString")
assert len(valley["branches_utm"][0]) >= 2, "a valley branch needs at least 2 points to be a line"
print(
    f"delineate_valleys finds the synthetic V-shaped valley as one primary valley "
    f"({valley['max_contributing_area_acres']} acres contributing area)."
)

# Raising the primary-valley threshold above what the synthetic valley
# actually generates should drop it entirely — confirms the threshold is
# actually being applied, not just decorative.
huge_threshold = valley["max_contributing_area_acres"] * 100
assert delineate_valleys(_dem(array), min_primary_valley_area_acres=huge_threshold) == []
print("Raising min_primary_valley_area_acres above the found valley's size correctly drops it.")


# --- valleys_to_geojson: schema-valid output with the "valley" layer ---

geojson = valleys_to_geojson(valleys)
validate_feature_collection(geojson)
assert geojson["features"][0]["properties"]["layer"] == "valley"
print("valleys_to_geojson output is schema-valid with layer='valley'.")


# --- nodata handling: a DEM with a hole of missing data doesn't crash or route through it ---

array_with_gap = array.copy()
array_with_gap[10:15, 10:15] = np.nan
gapped_valleys = delineate_valleys(_dem(array_with_gap))
assert isinstance(gapped_valleys, list)  # just shouldn't raise
print("delineate_valleys tolerates a nodata gap in the DEM without raising.")

print("\nAll valley_delineation checks passed.")
