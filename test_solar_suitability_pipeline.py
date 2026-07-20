"""
test_solar_suitability_pipeline.py

End-to-end offline check that identify_solar_candidate_zones()'s wiring
(DEM -> production areas -> roads -> SSURGO -> point-candidate scoring ->
ranked GeoJSON) fits together, using a hand-built synthetic DEM passed
via `dem=` (same approach as test_water_system_candidate_pipeline.py).

The farm-roads and SSURGO fetches inside identify_solar_candidate_zones()
are real network calls this sandbox can't reach — that's fine and by
design: both are wrapped in their own try/except and degrade gracefully
(no road data -> falls back to the top-ranked suggested road corridor,
itself DEM-only and not blocked by this sandbox's network policy; no
SSURGO data -> no prime_farmland_conflict property), the same "a network
layer fetch failing doesn't take down the whole feature" pattern the
rest of this pipeline already uses. So this test doubles as a live check
of that graceful-degradation path, not just the DEM/production-area
wiring.

Terrain is deliberately simple here: a uniform gentle south-facing slope
across the whole grid. Under the OLD eligible-area/exclusion model this
would have been a poor test fixture (the whole grid would register as
ONE production zone and hard-exclude the whole thing, leaving nothing
for solar) — that's exactly the live bug the point-candidate redesign
fixes (see solar_suitability.py's module docstring), so this simple
layout now deliberately doubles as an end-to-end confirmation that
candidates form on/near production land instead of being swallowed by
it, through the real identify_production_areas()/identify_solar_candidate_zones()
wiring, not just the pure function test_solar_suitability.py already
covers directly.
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
SIZE = 60  # 300m x 300m, big enough for CANDIDATE_POINT_SPACING_METERS (75m) to sample several points
HALF_EXTENT = SIZE * RESOLUTION / 2
origin_x = center_x - HALF_EXTENT
origin_y = center_y + HALF_EXTENT

utm_corners_x = [origin_x, origin_x + SIZE * RESOLUTION, origin_x + SIZE * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - SIZE * RESOLUTION, origin_y - SIZE * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A uniform ~4% south-facing grade across the entire grid -- flat/gentle
# enough that production_area.py claims essentially the whole parcel as
# one production zone.
array = np.zeros((SIZE, SIZE), dtype=np.float32)
for row in range(SIZE):
    array[row, :] = 100.0 - row * 0.2

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
print(f"Pipeline ran end-to-end with unreachable road/SSURGO data: {len(features)} solar candidate(s).")
assert len(features) >= 1, (
    "expected at least one solar candidate: this gentle, uniform terrain should clear the slope/"
    "aspect/shading suitability floor everywhere sampled"
)

relationships_seen = {f["properties"]["production_zone_relationship"] for f in features}
assert "inside" in relationships_seen, (
    "expected at least one candidate classified 'inside' the (whole-parcel) production zone here -- "
    "this is the exact scenario the point-candidate redesign exists to enable; under the old "
    "exclusion model this synthetic property would have produced ZERO solar candidates"
)
print(f"production_zone_relationship values observed: {sorted(relationships_seen)} -- "
      f"candidates correctly form inside/near production land instead of being excluded by it.")

for feature in features:
    props = feature["properties"]
    assert props["layer"] == "solar_infrastructure"
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert props["footprint_area_acres"] > 0
    assert "prime_farmland_conflict" not in props, (
        "with no reachable SSURGO data, candidates shouldn't carry a fabricated farmland conflict flag"
    )

# Existing-road data was unreachable in this sandbox -> the farm-roads
# fetch fails -> road_lines_wgs84 is None -> falls back to the top-ranked
# SUGGESTED road corridor instead (road_corridors.py, DEM-only). On THIS
# terrain, road_corridors.py's own (unchanged, out-of-scope-for-this-pass)
# production-zone exclusion also excludes essentially the whole parcel
# from corridor generation -- there simply is no corridor to fall back
# to, so distance_to_road_ft staying None here is correct, real
# degradation (not a fabricated value), same as the "no road data and no
# fallback available" path test_solar_road_fallback.py exercises
# directly with terrain built specifically to produce a real fallback
# corridor instead.
road_distances = {f["properties"]["distance_to_road_ft"] for f in features}
assert road_distances == {None}, (
    f"expected every candidate's distance_to_road_ft to be None on this terrain (no real road data, "
    f"and road_corridors.py's own production-zone exclusion leaves no fallback corridor to use "
    f"either), got {road_distances}"
)
for feature in features:
    notes = feature["properties"]["confidence_notes"]
    assert "SUGGESTED road corridor" not in notes, (
        "confidence_notes should not claim a corridor fallback was used when none was actually available"
    )
print("With no reachable road data AND no fallback corridor available (this terrain's whole parcel is "
      "production land, which road_corridors.py's own unchanged exclusion logic keeps corridors off of), "
      "distance_to_road_ft correctly stays None rather than fabricating a value.")
print("\nAll solar suitability pipeline checks passed.")
