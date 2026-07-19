"""
test_production_area.py

Offline (no-network) checks for production_area.py's slope-based
production/cultivation-area heuristic. Runs against a small synthetic DEM
built by hand — a flat low bench next to a steep rise — so these are
checks of the classification logic itself, not a real fetched DEM.
"""

import numpy as np
from shapely.geometry import box

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

# The DEM's own full extent (origin_y is the upper-left/max-y corner) —
# used wherever a test isn't specifically about parcel clipping, so that
# behavior matches the pre-clipping expectations exactly (100% on-parcel,
# nothing to clip).
FULL_EXTENT_BOUNDARY = box(500000.0, 4500000.0 - 30 * 5.0, 500000.0 + 30 * 5.0, 4500000.0)


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

patches = identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY)
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
assert identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY, min_area_acres=huge_area_threshold) == []
print("Raising min_area_acres above the found patch's size correctly drops it.")


# --- regression: candidates are clipped to the real parcel boundary, not the buffered DEM extent ---
#
# This is the exact live bug found against the real property: dem_data.py
# fetches a DEM buffered ~100m past the drawn boundary (correct and
# intentional, for flow-accumulation context), but identify_production_areas()
# used to return each patch's full footprint over that buffered extent
# without ever clipping back down to the actual parcel. Checked live
# against the real six-point property boundary via
# shapely.Polygon.contains()/.intersection(): of 6 production-area
# candidates, only 1 was fully on-parcel; the rest ranged from 0% to 34%
# on-parcel. Point-sampling a single vertex per polygon (the earlier,
# weaker check) missed this entirely -- only checking the full polygon's
# overlap against the real boundary catches it.

# Case 1: a boundary covering only the WEST HALF of the flat bench's x-range
# (the bench spans the full x in [500000, 500150]) -- the bench should be
# clipped down to roughly half its unclipped size, not returned at its
# full (partly off-parcel) footprint.
west_half_boundary = box(500000.0, 4500000.0 - 30 * 5.0, 500075.0, 4500000.0)
west_half_patches = identify_production_areas(_dem(array), west_half_boundary)
assert len(west_half_patches) == 1, (
    f"expected the bench to still qualify as a (smaller) candidate on the west-half boundary, "
    f"got {len(west_half_patches)} patch(es)"
)
west_patch = west_half_patches[0]
assert west_patch["area_acres"] < patch["area_acres"], (
    f"a boundary covering only half the bench should clip its area down "
    f"(full: {patch['area_acres']} acres, west-half boundary: {west_patch['area_acres']} acres)"
)
on_parcel_fraction = (
    west_patch["polygon_utm"].intersection(west_half_boundary).area / west_patch["polygon_utm"].area
)
assert on_parcel_fraction > 0.999, (
    f"the returned candidate geometry must itself be (effectively) 100% on-parcel after clipping, "
    f"got {on_parcel_fraction * 100:.1f}%"
)
assert west_patch["polygon_utm"].within(west_half_boundary.buffer(1e-6)), (
    "clipped candidate polygon must stay within the real parcel boundary"
)
print(
    f"Parcel clipping: a boundary covering only half the bench correctly shrinks the candidate "
    f"({patch['area_acres']} acres unclipped -> {west_patch['area_acres']} acres clipped), "
    f"and the returned geometry checks out as {on_parcel_fraction * 100:.1f}% on-parcel."
)

# Case 2: a boundary entirely disjoint from the DEM's extent altogether --
# every candidate patch (built from cells the boundary never covers at
# all) must be dropped completely (the 0%-on-parcel case actually found
# live: ids 2, 47, 57, 65 in the real check).
disjoint_boundary = box(600000.0, 4600000.0, 600100.0, 4600100.0)
disjoint_patches = identify_production_areas(_dem(array), disjoint_boundary)
assert disjoint_patches == [], (
    f"a boundary that doesn't overlap the DEM at all should drop every candidate entirely, "
    f"got {len(disjoint_patches)} patch(es)"
)
print("Parcel clipping: a boundary entirely off the DEM's extent correctly drops every candidate (0% on-parcel).")

# Case 3: a boundary clipping the bench down to a sliver below
# MIN_PRODUCTION_AREA_ACRES should drop it entirely, same as the
# unclipped min_area_acres filter above -- confirms clipping and the
# area-floor filter compose correctly rather than the floor only ever
# applying to the pre-clip estimate.
sliver_boundary = box(500000.0, 4500000.0 - 30 * 5.0, 500002.0, 4500000.0)  # 2m wide sliver of the bench
sliver_patches = identify_production_areas(_dem(array), sliver_boundary)
assert sliver_patches == [], (
    f"a boundary clipping the bench down to a sliver below MIN_PRODUCTION_AREA_ACRES should drop it, "
    f"got {len(sliver_patches)} patch(es)"
)
print("Parcel clipping: a boundary clipping the bench below the minimum area correctly drops it.")


# --- production_areas_to_geojson: schema-valid with the diagnostic layer name ---

geojson = production_areas_to_geojson(patches)
validate_feature_collection(geojson)
assert geojson["features"][0]["properties"]["layer"] == "production_area_candidate"
print("production_areas_to_geojson output is schema-valid with layer='production_area_candidate'.")

print("\nAll production_area checks passed.")
