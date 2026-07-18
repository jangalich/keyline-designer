"""
test_water_system_candidate_pipeline.py

End-to-end offline check that the full Step 1-3 wiring in
identify_water_system_candidate_zones() (dem -> valleys -> production
areas -> zones -> GeoJSON) actually fits together, using a hand-built
synthetic DEM passed in directly via the `dem=` parameter instead of a
real USGS fetch (dem_data.get_dem_for_boundary() needs live network access
and is exercised separately, manually, against real internet access — see
its __main__ block).

This complements, rather than replaces, test_valley_delineation.py (Stage
1 logic in isolation), test_production_area.py (the production-area
heuristic in isolation), and test_water_candidate_zones.py (Stage 2 zone-
filtering logic in isolation): those catch a broken algorithm; this catches
a broken wire between them (wrong parameter order, mismatched coordinate
systems, etc.) that per-module tests wouldn't.
"""

import numpy as np
from rasterio.warp import transform as warp_transform

from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection
from water_candidate_zones import identify_water_system_candidate_zones

# Real-world centroid (western PA, same region as this repo's other test
# fixtures) so the UTM zone/CRS math is genuine, not made up.
CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"

center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

RESOLUTION = 5.0
SIZE = 40  # 40 x 40 cells at 5m = 200m x 200m
HALF_EXTENT = SIZE * RESOLUTION / 2

origin_x = center_x - HALF_EXTENT
origin_y = center_y + HALF_EXTENT

# A synthetic property boundary: the square this DEM actually covers,
# reprojected back to WGS84 lon/lat — guarantees the boundary and the DEM
# genuinely line up, the same way a real fetch would.
utm_corners_x = [origin_x, origin_x + SIZE * RESOLUTION, origin_x + SIZE * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - SIZE * RESOLUTION, origin_y - SIZE * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# Terrain: a valley descending from the north edge (high) down to a flat
# bench along the south edge (low) — same shape as the individual-module
# smoke tests, combined into one DEM so a valley and a servable production
# area both genuinely exist on the same synthetic property.
array = np.zeros((SIZE, SIZE), dtype=np.float32)
for row in range(SIZE):
    for col in range(SIZE):
        if row >= SIZE - 8:
            array[row, col] = 100.0  # flat southern bench (production area)
        else:
            distance_from_center_col = abs(col - SIZE // 2)
            array[row, col] = 100.0 + (SIZE - 8 - row) * 3.0 + distance_from_center_col * 1.5

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

result = identify_water_system_candidate_zones(
    boundary_coordinates, dem=synthetic_dem, min_boundary_setback_meters=5.0
)

for key in ("zones_geojson", "valleys_geojson", "production_areas_geojson"):
    assert key in result, f"missing '{key}' in identify_water_system_candidate_zones() result"
    validate_feature_collection(result[key])

print(
    f"Pipeline ran end-to-end: {len(result['valleys_geojson']['features'])} valley(s), "
    f"{len(result['production_areas_geojson']['features'])} production area(s), "
    f"{len(result['zones_geojson']['features'])} water system candidate zone(s)."
)

assert len(result["valleys_geojson"]["features"]) >= 1, "expected at least one valley on this synthetic terrain"
assert len(result["production_areas_geojson"]["features"]) >= 1, "expected the flat bench to register as a production area"
assert len(result["zones_geojson"]["features"]) >= 1, (
    "expected at least one water system candidate zone: the synthetic valley "
    "descends steadily toward the flat bench, well above the default 1% gradient"
)

for feature in result["zones_geojson"]["features"]:
    assert feature["properties"]["layer"] == "water_system_candidate"
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")

print("All water system candidate zone features use layer='water_system_candidate' with polygon geometry.")
print("\nAll end-to-end pipeline checks passed.")
