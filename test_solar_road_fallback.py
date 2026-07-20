"""
test_solar_road_fallback.py

Offline (no-network) checks for solar_suitability.py's road-proximity
fallback: when no existing-road data is available, road-proximity scoring
should fall back to the top-ranked suggested_road_corridor
(road_corridors.py) instead of leaving the constraint disabled or hard-
excluding every candidate — but ONLY as a fallback; real existing-road
data must still win when it's actually available.

Uses a hand-built DEM with two distinct regions (west: a narrow ridge
with steep flanks, road-corridor-friendly; east: a broad, gentle uniform
slope, solar-eligible) so both road-corridor generation and solar-
candidate generation have real, independent terrain to work with in the
same DEM — see test_road_corridors.py and test_solar_suitability.py for
those mechanisms tested in isolation; this file is specifically about the
integration glue between the two.

NOTE: earlier versions of this fixture carved the east region as a small
isolated plateau, specifically sized to stay too small for
production_area.py's own area floor -- that mattered under the OLD
zone-exclusion solar model, where a solar candidate overlapping a
production zone was hard-excluded. Under the point-candidate model
(solar_suitability.py), production-zone overlap is no longer excluded at
all (a candidate can legitimately sit INSIDE a production zone), so the
east region can simply be a broad gentle slope like any other layer's
fixture -- no special small-area carving needed anymore.
"""

from unittest.mock import patch

import numpy as np
from rasterio.warp import transform as warp_transform

import solar_suitability
from dem_data import _utm_epsg_for_lonlat
from solar_suitability import _suggested_corridor_as_road_fallback, identify_solar_candidate_zones

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"

center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

RESOLUTION = 5.0
ROWS, COLS = 40, 60  # 200m x 300m -- big enough for CANDIDATE_POINT_SPACING_METERS (75m) to sample several points
RIDGE_COLS = 15  # west ridge is only 75m wide, so the gentle east region starts close enough to stay within the road-proximity buffer
origin_x = center_x - COLS * RESOLUTION / 2
origin_y = center_y + ROWS * RESOLUTION / 2

utm_corners_x = [origin_x, origin_x + COLS * RESOLUTION, origin_x + COLS * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - ROWS * RESOLUTION, origin_y - ROWS * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

array = np.zeros((ROWS, COLS), dtype=np.float32)
for row in range(ROWS):
    for col in range(COLS):
        if col < RIDGE_COLS:
            distance_from_ridge = abs((ROWS - 1 - row) - col)
            array[row, col] = 100.0 - distance_from_ridge * 1.2 - row * 0.15  # west: road-corridor-friendly ridge
        else:
            array[row, col] = 100.0 - row * 0.2  # east: gentle ~4% uniform slope, solar-eligible

dem = {
    "array": array, "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x, "origin_y": origin_y, "crs": DST_CRS,
}


# --- _suggested_corridor_as_road_fallback returns a real corridor when one exists ---

fallback_lines = _suggested_corridor_as_road_fallback(boundary_coordinates, dem)
assert fallback_lines is not None and len(fallback_lines) == 1
assert fallback_lines[0].length > 0
print("_suggested_corridor_as_road_fallback returns a real corridor LineString when road corridors exist.")


# --- with no existing-road data reachable, solar candidates use the corridor fallback ---

# get_farm_roads_for_boundary raises in this sandbox anyway (no network route),
# but patch it explicitly so this test doesn't depend on sandbox network policy.
# Patched on solar_suitability (where the name is bound via "from farm_roads_data
# import get_farm_roads_for_boundary"), not on farm_roads_data itself -- patching
# the origin module wouldn't affect the reference already imported into
# solar_suitability's own namespace.
with patch.object(solar_suitability, "get_farm_roads_for_boundary", side_effect=RuntimeError("no network")):
    result = identify_solar_candidate_zones(boundary_coordinates, dem=dem)

features = result["zones_geojson"]["features"]
assert len(features) >= 1, "expected at least one solar candidate on the gentle east slope"

for feature in features:
    props = feature["properties"]
    assert props["distance_to_road_ft"] is not None, (
        "with no real road data but a real suggested corridor available, distance_to_road_ft "
        "should be a real fallback-based value, not None"
    )
    assert props["distance_to_road_ft"] > 0
    notes = props["confidence_notes"]
    assert "SUGGESTED road corridor" in notes and "road_corridors.py" in notes, (
        "the road-proximity fallback must be flagged explicitly in confidence_notes"
    )
print(f"With no existing-road data, {len(features)} solar candidate(s) correctly fall back to the "
      f"suggested road corridor for proximity scoring, flagged in confidence_notes.")


# --- real existing-road data takes priority; the fallback is never invoked ---

real_road_geojson = {
    "type": "LineString",
    "coordinates": [[lons[0], lats[0]], [lons[1], lats[1]]],
}
with patch.object(
    solar_suitability, "get_farm_roads_for_boundary",
    return_value=[{"name": "Real Rd", "geometry": real_road_geojson}],
):
    with patch.object(solar_suitability, "identify_road_corridor_candidates") as mock_corridor_fn:
        result_with_real_road = identify_solar_candidate_zones(boundary_coordinates, dem=dem)
        assert not mock_corridor_fn.called, (
            "the road-corridor fallback must not be invoked at all when real road data is available"
        )

for feature in result_with_real_road["zones_geojson"]["features"]:
    notes = feature["properties"]["confidence_notes"]
    assert "SUGGESTED road corridor" not in notes, (
        "confidence_notes should not claim a corridor fallback was used when real road data was available"
    )
print("With real existing-road data available, the corridor fallback is never invoked (real data takes priority).")

print("\nAll solar road-fallback checks passed.")
