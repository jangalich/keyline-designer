"""
test_road_corridors_pipeline.py

End-to-end offline check that identify_road_corridor_candidates()'s
wiring (DEM -> optimized production areas -> the single selected water
zone -> floodplain fetch -> anchor-to-farthest-point routing -> ranked
GeoJSON) fits together, using a hand-built synthetic DEM passed via `dem=`
(same approach as test_water_system_candidate_pipeline.py /
test_solar_suitability_pipeline.py).

production_areas comes from production_area_ceiling.identify_optimized_
production_areas() (the OPTIMIZED/final, ceiling-trimmed patch shape,
scored against render_fill_polygon_utm) rather than production_area.
identify_production_areas()'s raw pre-optimization patches, and the
water-zone hard exclusion comes from water_suitability.fetch_and_
select_optimal_water_zone() -- the single rank-1 SELECTED zone -- rather
than every unscored candidate zone water_candidate_zones.py generates.
Both production and water are now HARD exclusions (an earlier version of
this module treated production as a soft cost preference); floodplain
moved the other way, from a hard exclusion to a soft cost penalty. The
erosion-prone-soil preference this module used to carry has been removed
outright (KSOP: Soil is step 8, below Farm Roads at step 4), so this test
no longer exercises or mocks anything erosion-related.

Both the production-area and water-zone pipelines share production_area.
py's mandatory woody-vegetation (canopy) gate -- a real network fetch
this sandbox can't reach, and one that deliberately does NOT degrade
gracefully (see production_area.identify_production_areas()'s own
docstring), so it's mocked here with a synthetic "no trees anywhere"
canopy result, same convention test_production_area_ceiling.py /
test_water_suitability.py already use. The disqualifying-soil fetch (used
internally by both production_area.py and production_area_ceiling.py) is
ALSO mocked here purely to keep this "offline" test fast and deterministic
rather than depending on how quickly a given environment's proxy rejects
the call -- it already degrades gracefully on its own if left unmocked.

The NHD and SSURGO fetches inside identify_road_corridor_candidates()
(and inside water_suitability.py's own per-zone soil/stream scoring) are
real network calls this sandbox can't reach — by design, each is wrapped
in its own try/except and degrades gracefully (no NHD/SSURGO data ->
floodplain cost-penalty union falls back to buffered valley lines,
flagged in confidence_notes). This test doubles as a live check of that
degradation path. anchor_lon_lat is a real, required input now (see
road_corridors.py's own module docstring) -- there's no farm-roads fetch
or named-road anchoring/connector search left in this module at all, so
unlike an earlier version of this test, no road-data mocking is needed
here.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform

import production_area
import production_area_ceiling
from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection
from road_corridors import identify_road_corridor_candidates


def _fake_clean_canopy(boundary_coordinates, dem):
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),  # below threshold everywhere -- no trees
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub",
    }

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"

center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

RESOLUTION = 5.0
ROWS = COLS = 30
origin_x = center_x - COLS * RESOLUTION / 2
origin_y = center_y + ROWS * RESOLUTION / 2

utm_corners_x = [origin_x, origin_x + COLS * RESOLUTION, origin_x + COLS * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - ROWS * RESOLUTION, origin_y - ROWS * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A narrow ridge crest with steep flanks: the flanks are too steep (>15%)
# for production_area.py's own threshold, so nothing here gets claimed as
# production land, but the crest itself stays under the 10% road-grade
# threshold -- deliberately chosen so this test actually exercises corridor
# generation end-to-end rather than having every candidate area swallowed
# by production_area.py's (unmodified, reused-as-is) exclusion, which is
# what happens on a uniformly gentle hillside since MAX_ROAD_GRADE_PCT
# (10%) is stricter than production_area.py's own MAX_PRODUCTION_SLOPE_PCT
# (15%) -- any land flat enough for a road is also flat enough to have
# already been claimed as production land, unless (as here) it's a narrow
# enough strip to fall under production_area.py's own minimum-area filter.
array = np.zeros((ROWS, COLS), dtype=np.float32)
for row in range(ROWS):
    for col in range(COLS):
        distance_from_ridge = abs((ROWS - 1 - row) - col)
        array[row, col] = 100.0 - distance_from_ridge * 1.2 - row * 0.15

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

# A real anchor point ON the ridge crest itself (row=15, col=14 satisfies
# distance_from_ridge == 0 in the array-building loop above) -- routing
# starts from here and heads to the farthest eligible point on the ridge.
anchor_row, anchor_col = 15, 14
anchor_x = origin_x + (anchor_col + 0.5) * RESOLUTION
anchor_y = origin_y - (anchor_row + 0.5) * RESOLUTION
anchor_lons, anchor_lats = warp_transform(DST_CRS, "EPSG:4326", [anchor_x], [anchor_y])
anchor_lon_lat = (anchor_lons[0], anchor_lats[0])

# identify_road_corridor_candidates() now calls production_area_ceiling.
# identify_optimized_production_areas() and water_suitability.fetch_and_
# select_optimal_water_zone() -- both of which, via production_area.py's
# shared helpers, do their own disqualifying-soil fetch (gracefully
# degrading on failure) and MANDATORY canopy fetch (hard-fails on failure,
# see this file's own docstring). The soil fetch is mocked here purely to
# keep this "offline" pipeline test fast and deterministic rather than
# depending on how quickly a given environment's proxy rejects the call;
# the canopy fetch MUST be mocked, since an unreachable network would
# otherwise raise instead of degrading.
with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy):
    result = identify_road_corridor_candidates(boundary_coordinates, dem=synthetic_dem, anchor_lon_lat=anchor_lon_lat)

assert "zones_geojson" in result
validate_feature_collection(result["zones_geojson"])

features = result["zones_geojson"]["features"]
print(f"Pipeline ran end-to-end with unreachable NHD/SSURGO data: {len(features)} road route(s).")
assert len(features) >= 1, (
    "expected at least one road route: the ridge crest is too narrow to be claimed as "
    "production land, giving the anchor point real eligible ground to route across"
)

for feature in features:
    props = feature["properties"]
    assert props["layer"] == "suggested_road_corridor"
    assert feature["geometry"]["type"] == "LineString"
    assert "anchor_status" not in props, "anchor_status no longer exists -- anchoring is bypassed entirely"
    assert "anchor_road_name" not in props, "anchor_road_name no longer exists -- anchoring is bypassed entirely"
    assert "crosses_production_zone" not in props, (
        "crosses_production_zone no longer exists -- production is a hard exclusion, it can never fire"
    )
    assert "crosses_floodplain" in props
    notes = props["confidence_notes"].lower()
    # No NHD/SSURGO data was reachable in this sandbox -> the floodplain
    # cost-penalty union fell back to buffered valley lines, flagged here.
    assert "dem-only fallback" in notes, "the floodplain-fallback caveat should appear in confidence_notes"

print("Routes correctly reflect unreachable NHD/SSURGO data (floodplain fallback flagged in confidence_notes), "
      "with no stale anchor/production-crossing properties.")
print("\nAll road corridor pipeline checks passed.")
