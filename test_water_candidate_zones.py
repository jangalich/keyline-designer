"""
test_water_candidate_zones.py

Offline (no-network) checks for water_candidate_zones.py's Step 3 zone-
filtering logic — Stage 2 of this feature: "is the zone-filtering logic
correct," deliberately independent of Stage 1 (DEM/valley delineation
accuracy — see test_valley_delineation.py / test_production_area.py).

Valleys and production areas here are hand-built dicts in the same shape
delineate_valleys()/identify_production_areas() actually produce (real
UTM-meter coordinates + elevations), not derived from a DEM at all — this
tests find_candidate_zones()'s gradient/distance/setback math directly.
"""

from shapely.geometry import box

from feature_schema import validate_feature_collection
from water_candidate_zones import find_candidate_zones, zones_to_geojson

CRS = "EPSG:32617"

# A 200m x 300m property, x in [0, 200], y in [0, 300].
BOUNDARY = box(0, 0, 200, 300)

# One production-area patch near the south edge, representative elevation 100m.
PRODUCTION_AREAS = [
    {"id": 0, "representative_elevation_m": 100.0, "polygon_utm": box(50, 0, 150, 30)}
]

# Valley 0: runs north (y=295, head) to south (y=5, near the patch), at a
# steady 2% grade toward the patch (100m + 0.02 * (y - 30)) — comfortably
# above the module's default 1% minimum gradient, so most of it should
# qualify, EXCEPT the northernmost few points (within the 15m default
# boundary setback of the y=300 edge) and the southernmost points
# (within/adjacent to the patch itself, inside the default 10m minimum
# service distance).
VALLEY_TOWARD_PATCH = {
    "id": 0,
    "branches_utm": [
        [(100.0, y, 100.0 + 0.02 * (y - 30)) for y in range(295, 0, -5)]
    ],
}

# Valley 1: a short run entirely within a few meters of the north boundary
# (y in [287, 299], all within the 15m setback of y=300), given a huge
# elevation margin (500m) that would trivially clear any gradient check —
# it should still be excluded entirely, because every point fails the
# boundary-setback check.
VALLEY_HUGGING_BOUNDARY = {
    "id": 1,
    "branches_utm": [[(20.0, y, 500.0) for y in range(299, 285, -2)]],
}


def _zone_for(valley_id, zones):
    matches = [z for z in zones if z["valley_id"] == valley_id]
    return matches[0] if matches else None


# --- default thresholds: valley 0 qualifies, valley 1 (boundary-hugging) does not ---

zones = find_candidate_zones(
    [VALLEY_TOWARD_PATCH, VALLEY_HUGGING_BOUNDARY], PRODUCTION_AREAS, BOUNDARY, CRS
)
assert len(zones) == 1, f"expected exactly 1 qualifying valley, got {len(zones)}"

zone = _zone_for(0, zones)
assert zone is not None, "valley 0 (real gradient toward the patch) should qualify"
assert zone["served_production_area_ids"] == [0]
assert not zone["polygon_utm"].is_empty
assert zone["polygon_utm"].within(BOUNDARY.buffer(1e-6)), "zone must stay within the property boundary"
print("Valley with a real, sustained gradient toward the production area produces a qualifying zone.")

assert _zone_for(1, zones) is None, (
    "valley 1 sits entirely within the boundary setback and should be excluded "
    "despite its huge elevation margin"
)
print("Valley within the boundary setback is excluded even with a large elevation margin.")


# --- stricter gradient than the valley's actual ~2% grade rejects it entirely ---

strict_zones = find_candidate_zones(
    [VALLEY_TOWARD_PATCH, VALLEY_HUGGING_BOUNDARY],
    PRODUCTION_AREAS,
    BOUNDARY,
    CRS,
    min_gravity_gradient=0.03,
)
assert strict_zones == [], (
    f"a 3% minimum gradient should reject a valley with only a ~2% actual grade, got {strict_zones}"
)
print("Raising the minimum gradient above the valley's actual grade correctly disqualifies it.")


# --- no production areas at all means no zones, regardless of valleys ---

assert find_candidate_zones([VALLEY_TOWARD_PATCH], [], BOUNDARY, CRS) == []
print("No production-area candidates means no water system candidate zones.")


# --- output is a schema-valid FeatureCollection on the required layer ---

geojson = zones_to_geojson(zones)
validate_feature_collection(geojson)
feature = geojson["features"][0]
assert feature["properties"]["layer"] == "water_system_candidate"
assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon"), (
    f"zone geometry must be a polygon/band, not a point — got {feature['geometry']['type']}"
)
assert "pond or dam site" in feature["properties"]["confidence_notes"].lower() or (
    "not a specific pond" in feature["properties"]["confidence_notes"].lower()
)
print("zones_to_geojson output is schema-valid, layer='water_system_candidate', polygon geometry.")

print("\nAll water_candidate_zones checks passed.")
