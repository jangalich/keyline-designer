"""
test_solar_suitability_pipeline.py

End-to-end offline check that identify_solar_candidate_zones()'s wiring
(DEM -> production areas -> roads -> SSURGO -> constraint stack -> ranked
GeoJSON) fits together, using a hand-built synthetic DEM passed via
`dem=` (same approach as test_water_system_candidate_pipeline.py).

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
# a small flat plateau on the eastern half: too small for
# production_area.py's own MIN_PRODUCTION_AREA_ACRES floor (0.5 acres,
# so it's NOT claimed as production land) but comfortably above
# solar_suitability.py's own, more permissive MIN_CANDIDATE_AREA_ACRES
# floor (0.25 acres). Falls away radially at PLATEAU_OUTER_GRADE_PCT
# beyond its flat core -- continuous at the flat/falloff boundary (no
# artificial cliff, which would otherwise wreck the DEM-only shading
# proxy right at the edge).
#
# NOTE: this used to carve the east half as a uniform ~17% south slope --
# too steep for production_area.py's OLD 15% ceiling but within
# solar_suitability.py's 20% one. Production's ceiling was raised to 20%
# (matching solar's own), so that slope-based gap no longer exists; the
# area-floor gap above is the real, still-valid discriminator between
# the two layers now (same reasoning test_road_corridors_pipeline.py's
# own ridge-crest design already relies on, not slope, for keeping its
# corridor-worthy crest out of production_area.py's own candidate set).
PLATEAU_CENTER = (20, 30)
PLATEAU_FLAT_RADIUS_CELLS = 4
PLATEAU_OUTER_GRADE_PCT = 25.0
PLATEAU_PEAK_ELEVATION = 300.0

array = np.zeros((SIZE, SIZE), dtype=np.float32)
plateau_row, plateau_col = PLATEAU_CENTER
for row in range(SIZE):
    for col in range(SIZE):
        if col < SIZE // 2:
            array[row, col] = 100.0  # flat west half (production)
        else:
            dist_cells = ((row - plateau_row) ** 2 + (col - plateau_col) ** 2) ** 0.5
            dist_m = dist_cells * RESOLUTION
            flat_radius_m = PLATEAU_FLAT_RADIUS_CELLS * RESOLUTION
            drop = max(0.0, dist_m - flat_radius_m) * (PLATEAU_OUTER_GRADE_PCT / 100.0)
            array[row, col] = PLATEAU_PEAK_ELEVATION - drop

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
    "expected at least one solar candidate zone: the east plateau is too small for "
    "production_area.py's own area floor but well within solar's, and its own flat "
    "core reads as genuinely low-slope, favorably-shaded ground"
)

for feature in features:
    props = feature["properties"]
    assert props["layer"] == "solar_infrastructure"
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    # Existing-road data was unreachable in this sandbox -> the farm-roads
    # fetch fails -> road_lines_wgs84 is None -> falls back to the
    # top-ranked SUGGESTED road corridor instead (road_corridors.py,
    # DEM-only -- its own NHD/SSURGO refinements degrade gracefully too,
    # but corridor GENERATION itself needs no network at all, so this
    # sandbox's policy doesn't block it). distance_to_road_ft should
    # therefore be a real fallback-based value, not None and not silently
    # fabricated.
    assert props["distance_to_road_ft"] is not None, (
        "with no reachable existing-road data, distance_to_road_ft should fall back to the "
        "suggested-corridor-based value, not stay None"
    )
    notes = props["confidence_notes"]
    assert "SUGGESTED road corridor" in notes and "road_corridors.py" in notes, (
        "the road-proximity fallback must be flagged explicitly in confidence_notes"
    )
    assert "prime_farmland_conflict" not in props, (
        "with no reachable SSURGO data, candidates shouldn't carry a fabricated farmland conflict flag"
    )

print("Candidates correctly fall back to the suggested-corridor-based road distance (flagged in "
      "confidence_notes) and correctly omit a fabricated farmland conflict flag when SSURGO is unreachable.")
print("\nAll solar suitability pipeline checks passed.")
