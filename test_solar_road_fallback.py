"""
test_solar_road_fallback.py

Offline (no-network) checks for solar_suitability.py's TWO-TIER road-
proximity model -- the integration glue between road_corridors.py's
selected road corridor and solar_suitability.py's candidate scoring.

The tiering was INVERTED from what an earlier version of this file tested
(see solar_suitability.py's own module/identify_solar_candidate_zones()
docstrings for the full rationale):

  Tier 1 (PRIMARY): the property's own single SELECTED road corridor
    (road_corridors.identify_road_corridor_candidates()'s
    'selected_road_corridor', its 'cell_footprint_polygon_utm'), within
    ROAD_CORRIDOR_PROXIMITY_METERS (15m). This corridor is the real
    primary road source now, NOT a stand-in used only when real road data
    is missing.
  Tier 2 (FALLBACK): only if Tier 1 produces ZERO candidates (no selected
    corridor exists, or one exists but nothing survives the rest of the
    constraint stack near it) -- real mapped roads
    (farm_roads_data.get_farm_roads_for_boundary()) at
    ROAD_PROXIMITY_BUFFER_METERS (150m). This is the pre-inversion
    road-proximity logic, demoted from primary to fallback.
  Terminal: if Tier 1 produces nothing AND Tier 2's own fetch fails
    outright, the road-proximity constraint is disabled entirely
    (distance_to_road_ft null), and candidates still form.

properties.road_proximity_source records which tier actually produced the
result: "selected_road_corridor" | "real_mapped_road" | "unavailable".

This file drives identify_solar_candidate_zones() directly with controlled
road inputs (a synthetic selected_road_corridor= override for Tier 1; a
patched identify_road_corridor_candidates()/get_farm_roads_for_boundary()
for Tiers 2 and terminal) so each tier's outcome is exercised
deterministically without any network, the same override/patch conventions
test_solar_suitability_pipeline.py already uses. The mandatory,
non-degrading canopy gate (production_area.get_required_tree_root_zone_
mask_utm(), reached by identify_solar_candidate_zones() on every run) is
mocked clean here for the same reason that file mocks it -- an unmocked run
hard-fails before producing any candidate at all in a network-restricted
sandbox. identify_tree_zone_candidates() (an orthogonal hard exclusion that
would otherwise self-fetch and, on this uniform fixture, wipe out every
candidate) is mocked to an empty result too, same reasoning.
"""

from unittest.mock import Mock, patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, box

import production_area
import production_area_ceiling
import solar_suitability
from dem_data import _utm_epsg_for_lonlat
from solar_suitability import (
    ROAD_CORRIDOR_PROXIMITY_METERS,
    ROAD_PROXIMITY_BUFFER_METERS,
    identify_solar_candidate_zones,
)


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
SIZE = 60  # 300m x 300m -- big enough for CANDIDATE_POINT_SPACING_METERS (25m) to sample several points
HALF_EXTENT = SIZE * RESOLUTION / 2
origin_x = center_x - HALF_EXTENT
origin_y = center_y + HALF_EXTENT

utm_corners_x = [origin_x, origin_x + SIZE * RESOLUTION, origin_x + SIZE * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - SIZE * RESOLUTION, origin_y - SIZE * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A uniform ~4% south-facing grade across the whole grid -- gentle enough
# that production_area.py claims essentially the whole parcel as one
# production zone and every sampled point clears solar's slope/aspect/
# shading floor, so candidate formation is governed by the road-proximity
# tier under test (not by terrain), same fixture style
# test_solar_suitability_pipeline.py established.
array = np.zeros((SIZE, SIZE), dtype=np.float32)
for row in range(SIZE):
    array[row, :] = 100.0 - row * 0.2

dem = {
    "array": array, "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x, "origin_y": origin_y, "crs": DST_CRS,
}

boundary_xs, boundary_ys = warp_transform(
    "EPSG:4326", DST_CRS, [pt[0] for pt in boundary_coordinates], [pt[1] for pt in boundary_coordinates],
)
from shapely.geometry import Polygon  # noqa: E402

BOUNDARY_POLYGON_UTM = Polygon(zip(boundary_xs, boundary_ys))

# Real optimized production patches for this fixture, reused as an override so
# no run below has to self-compute (and network-fetch) them. Canopy/soil mocked
# clean purely for offline-ness/determinism, same as everywhere else here.
with patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy):
    PRODUCTION_AREAS = production_area_ceiling.identify_optimized_production_areas(
        boundary_coordinates, dem=dem
    )["scored_patches"]
assert PRODUCTION_AREAS, "test setup: this gentle uniform slope should yield at least one production patch"

# A single selected water zone is a HARD exclusion. Fabricated well OUTSIDE
# this fixture's DEM extent so it excludes nothing -- candidate formation
# stays governed purely by the road tier under test.
OFFEXTENT_WATER_ZONE = {
    "id": "synthetic-test-water-zone",
    "render_fill_polygon_utm": Point(origin_x - 1000.0, origin_y - 1000.0).buffer(5.0),
}

# A synthetic selected road corridor: a thin strip crossing the full width
# of the DEM at mid-height, so candidates along that band clear Tier 1's
# tight ROAD_CORRIDOR_PROXIMITY_METERS (15m) gate. Same shape/purpose as
# test_solar_suitability_pipeline.py's own OVERRIDE_SELECTED_ROAD_CORRIDOR.
SELECTED_ROAD_CORRIDOR = {
    "id": "synthetic-test-road-corridor",
    "cell_footprint_polygon_utm": box(origin_x, center_y - 2.0, origin_x + SIZE * RESOLUTION, center_y + 2.0),
}

# A real mapped road for the Tier 2 fallback: get_farm_roads_for_boundary()'s
# own shape (a list of {"name", "geometry"} with a WGS84 LineString geometry).
# A mid-height line spanning the parcel, so candidates across it clear Tier 2's
# wider ROAD_PROXIMITY_BUFFER_METERS (150m) gate.
mid_lons, mid_lats = warp_transform(
    DST_CRS, "EPSG:4326", [origin_x, origin_x + SIZE * RESOLUTION], [center_y, center_y],
)
REAL_MAPPED_ROADS = [{
    "name": "County Rd 1",
    "geometry": {"type": "LineString", "coordinates": [[mid_lons[0], mid_lats[0]], [mid_lons[1], mid_lats[1]]]},
}]

_COMMON_KWARGS = dict(
    dem=dem,
    boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
    production_areas=PRODUCTION_AREAS,
    selected_water_zone=OFFEXTENT_WATER_ZONE,
    check_prime_farmland=False,
)


# --- Tier 1 (PRIMARY): a selected road corridor drives road proximity, and --
# --- the real-mapped-road fallback is NOT consulted even though real road ----
# --- data would be available -- the corridor takes priority ------------------

tier2_probe = Mock(return_value=REAL_MAPPED_ROADS)
with patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}), \
     patch.object(solar_suitability, "get_farm_roads_for_boundary", tier2_probe):
    tier1_result = identify_solar_candidate_zones(
        boundary_coordinates, selected_road_corridor=SELECTED_ROAD_CORRIDOR, **_COMMON_KWARGS
    )

tier1_features = tier1_result["zones_geojson"]["features"]
assert tier1_features, "expected at least one solar candidate near the selected road corridor"
for feature in tier1_features:
    props = feature["properties"]
    assert props["road_proximity_source"] == "selected_road_corridor", (
        "with a selected road corridor present, Tier 1 is the primary road source -- "
        f"road_proximity_source should be 'selected_road_corridor', got {props['road_proximity_source']!r}"
    )
    assert props["distance_to_road_ft"] is not None, (
        "distance_to_road_ft should be a real value measured against the selected corridor, not None"
    )
    notes = props["confidence_notes"]
    assert "SELECTED road corridor" in notes and "road_corridors.py" in notes, (
        "Tier 1 must flag in confidence_notes that proximity is measured against the SELECTED road corridor"
    )
    assert f"{ROAD_CORRIDOR_PROXIMITY_METERS:.0f}m" in notes, (
        "Tier 1's confidence_notes must state the 15m corridor-proximity buffer"
    )
assert tier2_probe.call_count == 0, (
    "the real-mapped-road fallback (Tier 2) must NOT be consulted when the selected road corridor "
    "(Tier 1) already produced candidates -- the corridor is the primary source, not the fallback"
)
print(f"Tier 1 (primary): with a selected road corridor available, {len(tier1_features)} solar candidate(s) "
      f"use it for proximity scoring (road_proximity_source='selected_road_corridor'), and the real-mapped-road "
      f"fallback is never consulted even though real road data would be available.")


# --- Tier 2 (FALLBACK): no selected corridor -> the real-mapped-road ---------
# --- fallback fires -----------------------------------------------------------

with patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}), \
     patch.object(solar_suitability, "identify_road_corridor_candidates",
                  return_value={"selected_road_corridor": None}), \
     patch.object(solar_suitability, "get_farm_roads_for_boundary", return_value=REAL_MAPPED_ROADS):
    tier2_result = identify_solar_candidate_zones(boundary_coordinates, **_COMMON_KWARGS)

tier2_features = tier2_result["zones_geojson"]["features"]
assert tier2_features, "expected at least one solar candidate near the real mapped road"
for feature in tier2_features:
    props = feature["properties"]
    assert props["road_proximity_source"] == "real_mapped_road", (
        "with NO selected corridor but real mapped road data available, Tier 2 should produce the result -- "
        f"road_proximity_source should be 'real_mapped_road', got {props['road_proximity_source']!r}"
    )
    assert props["distance_to_road_ft"] is not None, (
        "distance_to_road_ft should be a real value measured against the mapped road, not None"
    )
    notes = props["confidence_notes"]
    assert "real mapped road data" in notes and "farm_roads_data.py" in notes, (
        "Tier 2 must flag in confidence_notes that proximity fell back to real mapped road data"
    )
    assert f"{ROAD_PROXIMITY_BUFFER_METERS:.0f}m" in notes, (
        "Tier 2's confidence_notes must state the 150m mapped-road-proximity buffer"
    )
print(f"Tier 2 (fallback): with no selected corridor but real mapped road data available, "
      f"{len(tier2_features)} solar candidate(s) fall back to it (road_proximity_source='real_mapped_road'), "
      f"flagged in confidence_notes.")


# --- Terminal: no selected corridor AND no reachable road data -> the road ---
# --- constraint is disabled entirely, but candidates still form --------------

with patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}), \
     patch.object(solar_suitability, "identify_road_corridor_candidates",
                  return_value={"selected_road_corridor": None}), \
     patch.object(solar_suitability, "get_farm_roads_for_boundary", side_effect=RuntimeError("no network")):
    terminal_result = identify_solar_candidate_zones(boundary_coordinates, **_COMMON_KWARGS)

terminal_features = terminal_result["zones_geojson"]["features"]
assert terminal_features, (
    "candidates should still form with the road constraint disabled -- an unreachable road fetch must not "
    "take down the whole layer"
)
for feature in terminal_features:
    props = feature["properties"]
    assert props["road_proximity_source"] == "unavailable", (
        "with neither a selected corridor nor reachable road data, road_proximity_source should be "
        f"'unavailable', got {props['road_proximity_source']!r}"
    )
    assert props["distance_to_road_ft"] is None, (
        "distance_to_road_ft should be null when the road-proximity constraint is disabled entirely"
    )
    assert "disabled entirely" in props["confidence_notes"], (
        "the disabled-road-constraint state must be flagged in confidence_notes"
    )
print(f"Terminal: with neither a selected corridor nor reachable road data, {len(terminal_features)} solar "
      f"candidate(s) still form with the road-proximity constraint disabled entirely "
      f"(road_proximity_source='unavailable', distance_to_road_ft null).")


print("\nAll solar road-proximity two-tier checks passed.")
