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

from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection
import production_area as pa
from production_area import identify_production_areas
from valley_delineation import delineate_valleys
import water_candidate_zones as wcz
from water_candidate_zones import identify_water_system_candidate_zones

# identify_water_system_candidate_zones() calls production_area.identify_production_areas()
# internally with no check_soil/check_canopy passthrough of its own (water_candidate_zones.py
# is unchanged by the canopy-gate work) -- and that gate is now mandatory, with a fetch
# failure raising rather than degrading. Patched here (production_area's own module-level
# name, which is what identify_production_areas() actually looks up) to a fixed offline
# stub so this end-to-end wiring check stays fully offline; the gate's own hard-failure
# behavior has its own dedicated tests in test_canopy_height_data.py.
def _fake_clean_canopy(boundary_coordinates, dem):
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),  # below threshold everywhere -- no trees
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub",
    }


pa.get_canopy_height_for_boundary = _fake_clean_canopy

# The existing-road exclusion gate is optional (degrades gracefully, unlike canopy) but
# still defaults to check_roads=True and attempts a real fetch otherwise -- stubbed the
# same way, to "no roads found nearby" (None), so this end-to-end wiring check stays
# fully offline.
pa.get_road_exclusion_union_utm = lambda boundary_coordinates, dem, buffer_meters=None: None

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


# --- narrative_data: attached by the entry point, purely additive ---
#
# Detailed value-level checks (units, rounding, the distance-0 gradient
# None, position/percentile math) live in test_water_candidate_zones.py
# against hand-built fixtures; this end-to-end check covers the WIRING:
# the entry point attaches the block, adds no other key, and reports
# figures consistent with the same result's own GeoJSON.
import json  # noqa: E402

_PRE_NARRATIVE_RESULT_KEYS = {"zones_geojson", "valleys_geojson", "production_areas_geojson"}
assert _PRE_NARRATIVE_RESULT_KEYS <= set(result), (
    "narrative_data must be PURELY ADDITIVE -- missing pre-existing key(s): "
    f"{_PRE_NARRATIVE_RESULT_KEYS - set(result)}"
)
assert set(result) - _PRE_NARRATIVE_RESULT_KEYS == {"narrative_data"}, (
    f"narrative_data must be the ONLY new top-level key -- got {set(result) - _PRE_NARRATIVE_RESULT_KEYS}"
)
_nd = result["narrative_data"]
assert json.loads(json.dumps(_nd)) == _nd, "narrative_data must be json.dumps()-clean with no custom encoder"
assert _nd["zone_found"] == (len(result["zones_geojson"]["features"]) > 0)
assert _nd["candidate_count"] == len(result["zones_geojson"]["features"])
assert len(_nd["zones"]) == len(result["zones_geojson"]["features"])
assert _nd["production_area_count"] == len(result["production_areas_geojson"]["features"])
# The nomination record travels with the block, so an empty or short
# candidate list is explainable rather than merely reported.
assert set(_nd["nomination"]) == {"keypoints_considered", "keypoint_outcomes", "accumulation_seeds"}
assert all(o["outcome"] for o in _nd["nomination"]["keypoint_outcomes"]), (
    "every keypoint must carry a reason code for what happened to it"
)
# Canopy is fetch-or-raise on this path (stubbed to a clean fetch above),
# and the road stub returned a real None ("checked, no roads found") --
# both gates genuinely ran on this run.
assert _nd["gates"] == {"canopy_data_available": True, "road_data_available": True}
_zone_props = result["zones_geojson"]["features"][0]["properties"]
assert _nd["zone"]["service"]["served_production_area_ids"] == _zone_props["served_production_area_ids"], (
    "narrative_data must describe the SAME zone the GeoJSON reports"
)
assert _nd["zone"]["service"]["served_production_area_count"] == len(_zone_props["production_area_relationships"])
print(
    "narrative_data attached end-to-end: purely additive, json-clean, consistent with the same result's "
    f"GeoJSON ({_nd['candidate_count']} candidate(s); headline zone in the parcel's "
    f"{_nd['zone']['location']['position_in_parcel']}, {_nd['zone']['area_acres']} ac, nominated by "
    f"{_nd['zone']['provenance']['nominated_by']}, serving "
    f"{_nd['zone']['service']['served_production_area_count']} production area(s); "
    f"{_nd['nomination']['keypoints_considered']} keypoint(s) considered)."
)


# --- boundary_polygon_utm / valleys / production_areas overrides ---
#
# identify_water_system_candidate_zones() now accepts boundary_polygon_utm,
# valleys, and production_areas as optional overrides, mirroring the dem
# override pattern above -- each falls back to being self-computed exactly
# as before if not supplied, independently of the others. The three
# scenarios below reuse the SAME synthetic_dem/boundary_coordinates fixture
# as the baseline "no overrides" run above, so a genuine like-for-like
# comparison is possible.

fixture_boundary_xs, fixture_boundary_ys = warp_transform(
    "EPSG:4326",
    DST_CRS,
    [pt[0] for pt in boundary_coordinates],
    [pt[1] for pt in boundary_coordinates],
)
fixture_boundary_polygon_utm = Polygon(zip(fixture_boundary_xs, fixture_boundary_ys))

# Real (unmocked) valleys/production_areas for this fixture -- pa.
# get_canopy_height_for_boundary/get_road_exclusion_union_utm are already
# monkeypatched to offline stubs above, so this stays fully offline.
fixture_valleys = delineate_valleys(synthetic_dem)
fixture_production_areas = identify_production_areas(synthetic_dem, fixture_boundary_polygon_utm)
assert fixture_valleys, "test setup requires at least one valley on this synthetic terrain"
assert fixture_production_areas, "test setup requires at least one production area on this synthetic terrain"


# --- 1. all three overrides supplied: delineate_valleys()/identify_production_areas() must NOT be called ---

with mock_patch.object(wcz, "delineate_valleys") as mock_delineate_all, \
     mock_patch.object(wcz, "identify_production_areas") as mock_identify_pa_all:
    result_all_overrides = identify_water_system_candidate_zones(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=fixture_boundary_polygon_utm,
        valleys=fixture_valleys,
        production_areas=fixture_production_areas,
        min_boundary_setback_meters=5.0,
    )

assert mock_delineate_all.call_count == 0, "delineate_valleys must not be called when valleys is supplied"
assert mock_identify_pa_all.call_count == 0, "identify_production_areas must not be called when production_areas is supplied"

for key in ("zones_geojson", "valleys_geojson", "production_areas_geojson"):
    assert key in result_all_overrides, f"missing '{key}' with all three overrides supplied"
    validate_feature_collection(result_all_overrides[key])

assert len(result_all_overrides["valleys_geojson"]["features"]) == len(fixture_valleys), (
    "valleys_geojson must reflect the SUPPLIED valleys override, not a re-derived count"
)
assert len(result_all_overrides["production_areas_geojson"]["features"]) == len(fixture_production_areas), (
    "production_areas_geojson must reflect the SUPPLIED production_areas override, not a re-derived count"
)
assert len(result_all_overrides["zones_geojson"]["features"]) == len(result["zones_geojson"]["features"]), (
    "zones_geojson should be unchanged when the supplied overrides are the same real values self-computation "
    "would have produced anyway"
)
print(
    "\nWith all three overrides (boundary_polygon_utm/valleys/production_areas) supplied: "
    "delineate_valleys() and identify_production_areas() were called ZERO times, and the result matches "
    "the supplied override data."
)


# --- 2. none of the three overrides supplied: identical output to before this change (pure regression) ---
#
# This is exactly the baseline call already made above (dem=synthetic_dem only, no boundary_polygon_utm/
# valleys/production_areas) -- the self-compute code path for all three is unchanged by this branch (only
# now reached via "if X is None:" instead of unconditionally), so `result` above already IS this scenario's
# regression check. Cross-checked here against the override fixtures built from the identical synthetic_dem/
# boundary_coordinates, which must match exactly since both paths compute the same real values the same way.
assert len(result["valleys_geojson"]["features"]) == len(fixture_valleys), (
    "self-computed valleys (no override supplied) must match a direct delineate_valleys() call on the same dem"
)
assert len(result["production_areas_geojson"]["features"]) == len(fixture_production_areas), (
    "self-computed production_areas (no override supplied) must match a direct identify_production_areas() "
    "call on the same dem/boundary"
)
print(
    "With none of the three overrides supplied: self-computed valleys/production_areas counts match a direct "
    "call to delineate_valleys()/identify_production_areas() on the same dem -- unchanged, pre-existing behavior."
)


# --- 3. only ONE override supplied (production_areas): the other two must still self-compute correctly ---

with mock_patch.object(wcz, "delineate_valleys", wraps=wcz.delineate_valleys) as mock_delineate_partial, \
     mock_patch.object(wcz, "identify_production_areas") as mock_identify_pa_partial:
    result_partial_override = identify_water_system_candidate_zones(
        boundary_coordinates,
        dem=synthetic_dem,
        production_areas=fixture_production_areas,
        min_boundary_setback_meters=5.0,
    )

assert mock_delineate_partial.call_count == 1, (
    "valleys was not overridden -- delineate_valleys() must still be called exactly once to self-compute it"
)
assert mock_identify_pa_partial.call_count == 0, (
    "identify_production_areas must not be called when production_areas is supplied, even though the other "
    "two overrides were not"
)

for key in ("zones_geojson", "valleys_geojson", "production_areas_geojson"):
    assert key in result_partial_override, f"missing '{key}' with only production_areas overridden"
    validate_feature_collection(result_partial_override[key])

assert len(result_partial_override["production_areas_geojson"]["features"]) == len(fixture_production_areas), (
    "production_areas_geojson must reflect the SUPPLIED override"
)
assert len(result_partial_override["valleys_geojson"]["features"]) == len(fixture_valleys), (
    "valleys_geojson must reflect a correctly SELF-COMPUTED result (boundary_polygon_utm was also not "
    "overridden, so this also confirms self-computed boundary_polygon_utm was correct)"
)
print(
    "With only production_areas overridden: identify_production_areas() was called ZERO times, while "
    "delineate_valleys() was still called exactly once (valleys/boundary_polygon_utm correctly self-computed) "
    "-- the three overrides are independent, not all-or-nothing."
)

# --- canopy_height override forwarding ---
#
# identify_water_system_candidate_zones() reaches canopy on TWO independent
# paths when production_areas is left to self-compute: its default
# identify_production_areas() fetch AND its own mandatory get_required_tree_
# root_zone_mask_utm() gate. A supplied canopy_height override must reach
# BOTH, so neither re-fetches from the network. Shared-core behavior is
# proven in test_canopy_mask_override.py; this proves this entry point
# forwards the override down every canopy path. Reuses the synthetic_dem/
# boundary fixture the end-to-end run above already uses.
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402

_ov_override = clean_canopy_for(synthetic_dem)
with CanopyOverrideProbe() as _ov_probe:
    identify_water_system_candidate_zones(
        boundary_coordinates, dem=synthetic_dem, min_boundary_setback_meters=5.0, canopy_height=_ov_override
    )
_ov_probe.assert_override_used(_ov_override, "identify_water_system_candidate_zones()")
assert len(_ov_probe.mask_arrays) >= 2, (
    "identify_water_system_candidate_zones(): with production_areas self-computed, the override must "
    f"reach BOTH canopy paths (internal identify_production_areas + own gate), saw {len(_ov_probe.mask_arrays)}"
)
print(
    "identify_water_system_candidate_zones(): a supplied canopy_height override reaches BOTH internal "
    f"canopy paths ({len(_ov_probe.mask_arrays)} gates) -- 0 canopy fetches, exact override array used throughout."
)

print("\nAll end-to-end pipeline checks passed.")
