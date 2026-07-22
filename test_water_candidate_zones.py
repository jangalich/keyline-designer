"""
test_water_candidate_zones.py

Offline (no-network) checks for water_candidate_zones.py's Step 3 zone-
filtering logic — Stage 2 of this feature: "is the zone-filtering logic
correct," deliberately independent of Stage 1 (DEM/valley delineation
accuracy — see test_valley_delineation.py / test_production_area.py).

Valleys and production areas here are hand-built dicts in the same shape
delineate_valleys()/identify_production_areas() actually produce (real
UTM-meter coordinates + elevations), not derived from a DEM at all — this
tests find_candidate_zones()'s service-distance/boundary-setback filtering
AND its elevation-relationship math directly. Gravity/elevation is no
longer a generation-time gate here (see water_candidate_zones.py's module
docstring) — a below-elevation (pump-required) valley must still produce a
real, qualifying zone, just tagged with a negative elevation differential
for water_suitability.py to score as a preference, not exclude outright.
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
# above the patch's own elevation for most of its length, EXCEPT the
# northernmost few points (within the 15m default boundary setback of the
# y=300 edge) and the southernmost points (within/adjacent to the patch
# itself, inside the default 10m minimum service distance).
VALLEY_TOWARD_PATCH = {
    "id": 0,
    "branches_utm": [
        [(100.0, y, 100.0 + 0.02 * (y - 30)) for y in range(295, 0, -5)]
    ],
}

# Valley 1: a short run entirely within a few meters of the north boundary
# (y in [287, 299], all within the 15m setback of y=300), given a huge
# elevation margin (500m) that would trivially clear any gradient — it
# should still be excluded entirely, because every point fails the
# boundary-setback check (a real, still-enforced generation-time filter).
VALLEY_HUGGING_BOUNDARY = {
    "id": 1,
    "branches_utm": [[(20.0, y, 500.0) for y in range(299, 285, -2)]],
}

# Valley 2: runs north (y=295) to south (y=35, well outside the patch's own
# footprint and outside the 10m minimum service distance) sitting BELOW the
# patch's 100m elevation the entire way (80m flat) — a real site that would
# need a pump to deliver water uphill to the production area. Per this
# module's post-gate-removal behavior, this must still produce a real,
# qualifying candidate zone (not be silently excluded), tagged with a
# negative elevation differential.
VALLEY_BELOW_PATCH = {
    "id": 2,
    "branches_utm": [[(160.0, y, 80.0) for y in range(295, 30, -5)]],
}


def _zone_for(valley_id, zones):
    matches = [z for z in zones if z["valley_id"] == valley_id]
    return matches[0] if matches else None


# --- default thresholds: valleys 0 and 2 both qualify (gravity is no ---
# --- longer a gate); valley 1 (boundary-hugging) does not             ---

zones = find_candidate_zones(
    [VALLEY_TOWARD_PATCH, VALLEY_HUGGING_BOUNDARY, VALLEY_BELOW_PATCH], PRODUCTION_AREAS, BOUNDARY, CRS
)
assert len(zones) == 2, f"expected exactly 2 qualifying valleys (0 and 2), got {len(zones)}"
print("Removing the hard gravity gate: both the above- and below-elevation valleys qualify.")

zone0 = _zone_for(0, zones)
assert zone0 is not None, "valley 0 (above the patch) should qualify"
assert zone0["served_production_area_ids"] == [0]
assert not zone0["polygon_utm"].is_empty
assert zone0["polygon_utm"].within(BOUNDARY.buffer(1e-6)), "zone must stay within the property boundary"
primary0 = zone0["primary_production_area_relationship"]
assert primary0["above_production_area"] is True
assert primary0["elevation_differential_m"] > 0
assert primary0["gradient_pct"] > 0
print(
    f"Valley 0 (real gradient toward the production area) produces a qualifying zone, "
    f"above_production_area=True, elevation_differential_m={primary0['elevation_differential_m']}, "
    f"gradient_pct={primary0['gradient_pct']}."
)

assert _zone_for(1, zones) is None, (
    "valley 1 sits entirely within the boundary setback and should be excluded "
    "despite its huge elevation margin -- boundary setback is still a real, "
    "unchanged generation-time filter"
)
print("Valley within the boundary setback is still excluded even with a large elevation margin.")

zone2 = _zone_for(2, zones)
assert zone2 is not None, (
    "valley 2 (below the production area's elevation -- would need a pump) must still "
    "be generated as a real candidate, not silently excluded by a hard gravity gate"
)
assert zone2["served_production_area_ids"] == [0]
primary2 = zone2["primary_production_area_relationship"]
assert primary2["above_production_area"] is False, "valley 2 sits below the production area"
assert primary2["elevation_differential_m"] < 0, "elevation differential must be reported as real and negative"
assert primary2["gradient_pct"] < 0
print(
    f"Valley 2 (below the production area, pump-required) produces a real, qualifying "
    f"zone -- NOT silently excluded -- with elevation_differential_m={primary2['elevation_differential_m']} "
    f"(negative), gradient_pct={primary2['gradient_pct']}."
)


# --- no production areas at all means no zones, regardless of valleys ---

assert find_candidate_zones([VALLEY_TOWARD_PATCH], [], BOUNDARY, CRS) == []
print("No production-area candidates means no water system candidate zones.")


# --- min/max service distance bounds are still real generation-time filters ---

no_service_zones = find_candidate_zones(
    [VALLEY_TOWARD_PATCH],
    PRODUCTION_AREAS,
    BOUNDARY,
    CRS,
    max_service_distance_meters=5.0,  # far tighter than valley 0's actual distance to the patch
)
assert no_service_zones == [], (
    "a max_service_distance_meters tighter than the valley's actual distance to any "
    "production area should still exclude it -- service-distance bounds are unchanged "
    "real filters, not preferences"
)
print("Max service distance is still a real, enforced generation-time filter.")


# --- output is a schema-valid FeatureCollection on the required layer ---

geojson = zones_to_geojson(zones)
validate_feature_collection(geojson)
feature = next(f for f in geojson["features"] if f["properties"]["source_valley_id"] == 0)
assert feature["properties"]["layer"] == "water_system_candidate"
assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon"), (
    f"zone geometry must be a polygon/band, not a point — got {feature['geometry']['type']}"
)
assert "pond or dam site" in feature["properties"]["confidence_notes"].lower() or (
    "not a specific pond" in feature["properties"]["confidence_notes"].lower()
)
assert "production_area_relationships" in feature["properties"]
assert "primary_production_area_relationship" in feature["properties"]
print("zones_to_geojson output is schema-valid, layer='water_system_candidate', polygon geometry, carries elevation-relationship data.")

below_feature = next(f for f in geojson["features"] if f["properties"]["source_valley_id"] == 2)
assert below_feature["properties"]["primary_production_area_relationship"]["above_production_area"] is False
print("Below-elevation zone's GeoJSON feature reports above_production_area=False, not omitted or excluded.")

print("\nAll water_candidate_zones checks passed.")
