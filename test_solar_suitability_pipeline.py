"""
test_solar_suitability_pipeline.py

End-to-end offline check that identify_solar_candidate_zones()'s wiring
(DEM -> production areas -> roads -> SSURGO -> constraint stack -> ranked
GeoJSON) fits together, using a hand-built synthetic DEM passed via
`dem=` (same approach as test_water_system_candidate_pipeline.py).

The farm-roads and SSURGO fetches inside identify_solar_candidate_zones()
are real network calls this sandbox can't reach — that's fine and by
design: both are wrapped in their own try/except and degrade gracefully
(no road data -> proximity constraint disabled; no SSURGO data -> no
prime_farmland_conflict property), the same "a network layer fetch
failing doesn't take down the whole feature" pattern the rest of this
pipeline already uses. So this test doubles as a live check of that
graceful-degradation path, not just the DEM/production-area wiring.
"""

import numpy as np
from rasterio.warp import transform as warp_transform

from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection
from solar_suitability import identify_solar_candidate_zones

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"

center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

RESOLUTION = 5.0
SIZE = 40
HALF_EXTENT = SIZE * RESOLUTION / 2
origin_x = center_x - HALF_EXTENT
origin_y = center_y + HALF_EXTENT

utm_corners_x = [origin_x, origin_x + SIZE * RESOLUTION, origin_x + SIZE * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - SIZE * RESOLUTION, origin_y - SIZE * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A flat western half (production_area.py will treat this as production
# land -- the point here is checking the pipeline wires together and
# degrades gracefully with no reachable road/SSURGO data, not re-testing
# the constraint-stack math test_solar_suitability.py already covers) and
# a ~17% south-facing slope on the eastern half: too steep for
# production_area.py's own 15% threshold (so it's NOT claimed as
# production land) but well within solar_suitability.py's looser 20%
# tolerance (ground-mount racking handles more grade than row crops).
array = np.zeros((SIZE, SIZE), dtype=np.float32)
for row in range(SIZE):
    for col in range(SIZE):
        if col < SIZE // 2:
            array[row, col] = 100.0  # flat west half
        else:
            array[row, col] = 100.0 - row * 0.85  # ~17% south-facing grade, east half

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

result = identify_solar_candidate_zones(boundary_coordinates, dem=synthetic_dem)

assert "zones_geojson" in result
validate_feature_collection(result["zones_geojson"])

features = result["zones_geojson"]["features"]
print(f"Pipeline ran end-to-end with unreachable road/SSURGO data: {len(features)} solar candidate zone(s).")
assert len(features) >= 1, (
    "expected at least one solar candidate zone: the east half is too steep for "
    "production_area.py's threshold but well within solar's, and south-facing"
)

for feature in features:
    props = feature["properties"]
    assert props["layer"] == "solar_infrastructure"
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    # Road data was unreachable in this sandbox -> the fetch fails ->
    # road_lines_wgs84 is None -> the proximity constraint is disabled ->
    # distance_to_road_ft should be None, not silently zero or fabricated.
    assert props["distance_to_road_ft"] is None, (
        "with no reachable road data, distance_to_road_ft should be None, not a fabricated value"
    )
    assert "prime_farmland_conflict" not in props, (
        "with no reachable SSURGO data, candidates shouldn't carry a fabricated farmland conflict flag"
    )

print("Candidates correctly reflect unavailable road/SSURGO data (None / absent) rather than fabricating values.")
print("\nAll solar suitability pipeline checks passed.")
