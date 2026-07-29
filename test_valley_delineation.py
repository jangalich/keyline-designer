"""
test_valley_delineation.py

Offline (no-network) checks for valley_delineation.py — Stage 1 of the
water-system candidate-zone feature: "is the DEM/valley delineation
accurate." Everything here runs against small, synthetic DEM dicts built
by hand, not a real USGS fetch (dem_data.py needs real network access and
isn't exercised here), so these checks are about the terrain-analysis
algorithm itself, independent of whether a specific live DEM fetch worked.
"""

import inspect

import numpy as np
from shapely.geometry import Point, box

from valley_delineation import (
    MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES,
    VALLEY_CONFIDENCE_NOTES,
    compute_flow_accumulation,
    compute_flow_direction,
    delineate_valleys,
    fill_depressions,
    get_flow_accumulation_for_dem,
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


# --- get_flow_accumulation_for_dem: exposes the same grid delineate_valleys() ---
# --- thresholds/traces internally, aligned to the DEM and usable standalone ---

flow_accumulation_cells = get_flow_accumulation_for_dem(_dem(array))
assert flow_accumulation_cells.shape == array.shape, (
    f"flow accumulation grid should match the DEM's shape {array.shape}, got {flow_accumulation_cells.shape}"
)
assert np.all(flow_accumulation_cells >= 0), "flow accumulation is a contributing-cell count and can't be negative"

# The synthetic DEM's valley floor is the diagonal where
# abs((size - 1 - row) - col) == 0; cells there should have accumulated
# much more upstream area than cells on the ridge line far from it
# (col == 0, row == size - 1, which is about as far from the diagonal as
# this grid gets), since real flow converges onto the diagonal.
valley_floor_cells = [(row, size - 1 - row) for row in range(size)]
valley_floor_accumulation = [flow_accumulation_cells[r, c] for r, c in valley_floor_cells]
ridge_accumulation = flow_accumulation_cells[size - 1, 0]
assert max(valley_floor_accumulation) > ridge_accumulation * 5, (
    "cells along the synthetic diagonal drainage path should accumulate meaningfully more upstream area "
    f"than an off-path ridge cell, got max valley-floor={max(valley_floor_accumulation)} "
    f"vs ridge={ridge_accumulation}"
)
print(
    "get_flow_accumulation_for_dem returns a DEM-aligned, non-negative grid with the synthetic "
    f"drainage path (max {max(valley_floor_accumulation)}) accumulating far more than an off-path "
    f"ridge cell ({ridge_accumulation})."
)


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


# --- parcel clipping: unlike production_area.py/road_corridors.py/solar_suitability.py, ---
# --- valleys are INTENTIONALLY NOT clipped to the parcel boundary ---
#
# Those other three layers had a real, confirmed live bug: candidates
# derived from the DEM's ~100m buffered margin (dem_data.py) were returned
# un-clipped, describing land off the actual parcel. Valleys are
# different by design, not by oversight: a drainage line's head or an
# inflection point legitimately sits off-parcel sometimes (flow doesn't
# stop at a property line), and that's meaningful upstream/downstream
# context for the water-system candidate-zone logic built on top of this,
# not noise to filter out. So delineate_valleys() deliberately takes no
# boundary parameter at all and does no clipping.
#
# This is a documentation/API-shape guard, not a bug regression: it
# confirms (a) that's still true of the current signature, so a future
# change doesn't silently start requiring/enforcing a boundary here to
# "match" the other three layers, and (b) a synthetic valley spanning
# past a plausible smaller "parcel" is returned in full, unclipped.
assert "boundary" not in inspect.signature(delineate_valleys).parameters, (
    "delineate_valleys() unexpectedly gained a boundary parameter -- if valleys are now meant to be "
    "clipped, update this test (and its rationale above) deliberately; don't let it happen by accident"
)

# The synthetic V-shaped valley above spans the full 30x30 grid (150m x
# 150m at this DEM's 5m resolution); a plausible smaller "parcel" cut out
# of the middle of it leaves real valley geometry on both sides.
plausible_parcel = box(500000 + 50, 4500000 - 100, 500000 + 100, 4500000 - 50)
branch_points = [Point(x, y) for x, y, _elevation in valley["branches_utm"][0]]
off_parcel_points = [p for p in branch_points if not plausible_parcel.contains(p)]
assert off_parcel_points, (
    "expected at least some of this valley's branch points to fall outside a smaller reference "
    "parcel cut from the middle of the DEM -- if none do, this test isn't actually exercising anything"
)
assert len(off_parcel_points) < len(branch_points), (
    "expected at least some branch points to remain on-parcel too, so this is a genuine partial-overlap case"
)
assert "may legitimately extend past the drawn property boundary" in VALLEY_CONFIDENCE_NOTES, (
    "VALLEY_CONFIDENCE_NOTES should plainly disclose that valley geometry isn't clipped to the parcel"
)
print(
    f"delineate_valleys() correctly returns valley branches un-clipped: {len(off_parcel_points)}/"
    f"{len(branch_points)} of this valley's branch points fall outside a smaller reference parcel, "
    f"and that's disclosed in VALLEY_CONFIDENCE_NOTES rather than silently clipped away."
)

print("\nAll valley_delineation checks passed.")
