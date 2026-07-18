"""
test_production_area.py

Offline (no-network) checks for production_area.py's slope-based
production/cultivation-area heuristic. Runs against a small synthetic DEM
built by hand — a flat low bench next to a steep rise — so these are
checks of the classification logic itself, not a real fetched DEM.
"""

import numpy as np

from feature_schema import validate_feature_collection
from production_area import (
    compute_slope_percent,
    identify_production_areas,
    production_areas_to_geojson,
)

RESOLUTION = (5.0, 5.0)
BASE_DEM = {
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}


def _dem(array: np.ndarray) -> dict:
    return {**BASE_DEM, "array": array}


# --- compute_slope_percent: flat ground is ~0%, a real rise is high ---

flat = np.full((5, 5), 100.0, dtype=np.float32)
flat_slope = compute_slope_percent(flat, RESOLUTION)
assert np.nanmax(flat_slope) == 0.0, f"flat DEM should have 0% slope everywhere, got max {np.nanmax(flat_slope)}"
print("compute_slope_percent reads 0% slope on perfectly flat ground.")

steep = np.array(
    [
        [100.0, 100.0, 100.0],
        [110.0, 110.0, 110.0],  # 10m rise over one 5m cell = 200% grade
        [120.0, 120.0, 120.0],
    ],
    dtype=np.float32,
)
steep_slope = compute_slope_percent(steep, RESOLUTION)
assert steep_slope[1, 1] == 200.0, f"expected 200% grade at the middle row, got {steep_slope[1, 1]}"
print("compute_slope_percent correctly reads a steep grade.")


# --- identify_production_areas: finds the flat bench, not the steep rise ---

size = 30
array = np.zeros((size, size), dtype=np.float32)
for row in range(size):
    for col in range(size):
        array[row, col] = 100.0 if row < 15 else 100.0 + (row - 14) * 5.0

patches = identify_production_areas(_dem(array))
assert len(patches) == 1, f"expected exactly 1 production-area patch, got {len(patches)}"
patch = patches[0]
assert patch["representative_elevation_m"] == 100.0, (
    f"the flat bench is uniformly 100m, expected that as the representative elevation, "
    f"got {patch['representative_elevation_m']}"
)
assert patch["polygon_utm"].geom_type == "Polygon"
assert patch["geometry_wgs84"]["type"] == "Polygon"
print(
    f"identify_production_areas isolates the flat bench "
    f"({patch['area_acres']} acres, {patch['representative_elevation_m']}m) and excludes the steep rise."
)

# A stricter (lower) slope threshold that the bench itself still clears
# should still find it; a threshold below what the bench actually is (0%,
# perfectly flat) shouldn't be possible to fail, so instead check the
# inverse: raising the minimum-area filter above the bench's real size
# drops it, confirming that filter is actually applied.
huge_area_threshold = patch["area_acres"] * 100
assert identify_production_areas(_dem(array), min_area_acres=huge_area_threshold) == []
print("Raising min_area_acres above the found patch's size correctly drops it.")


# --- production_areas_to_geojson: schema-valid with the diagnostic layer name ---

geojson = production_areas_to_geojson(patches)
validate_feature_collection(geojson)
assert geojson["features"][0]["properties"]["layer"] == "production_area_candidate"
print("production_areas_to_geojson output is schema-valid with layer='production_area_candidate'.")

print("\nAll production_area checks passed.")
