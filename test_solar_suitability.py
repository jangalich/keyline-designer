"""
test_solar_suitability.py

Offline (no-network) checks for solar_suitability.py's constraint-stack
and ranking logic — a hand-built synthetic DEM plus hand-built production
areas/roads (same shapes find_candidate_solar_zones() actually consumes),
not a real DEM/road/SSURGO fetch. Mirrors test_water_candidate_zones.py's
"pure logic, independent of real data fetches" approach.

Layout (40x40 cells, 5m resolution -> 200m x 200m):
  - x in [0, 80]:      production zone 0 (excluded, +15m buffer)
  - x in [100, 145]:   WEST candidate region -- mild south-facing slope
  - x in [145, 160]:   production zone 1 (excluded, +15m buffer) -- keeps
                        the two candidate regions from merging into one
                        connected component, without resorting to a tall
                        DEM spike (which would itself cast an unwanted
                        shadow onto its neighbors and confound the test)
  - x in [175, 200]:   EAST candidate region -- mild NORTH-facing slope
                        (worse aspect, same slope magnitude -- this is
                        what differentiates the two candidates' rank)
  - a road runs along the whole south edge (y=0); the default 150m
    proximity buffer excludes roughly the northern quarter of the grid
"""

from shapely.geometry import LineString, box

from feature_schema import validate_feature_collection
from solar_suitability import (
    MIN_SUITABILITY_SCORE,
    candidates_to_geojson,
    find_candidate_solar_zones,
    flag_prime_farmland_conflicts,
)

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)
ROWS = COLS = 40
ORIGIN_X, ORIGIN_Y = 500000.0, 4500200.0

import numpy as np

array = np.full((ROWS, COLS), 100.0, dtype=np.float32)
for row in range(ROWS):
    for col in range(20, 29):
        array[row, col] = 100.0 - row * 0.2  # west region: south-facing, ~4% grade
for row in range(ROWS):
    for col in range(32, 40):
        array[row, col] = 100.0 + row * 0.2  # east region: north-facing, ~4% grade

DEM = {"array": array, "resolution_meters": RESOLUTION, "origin_x": ORIGIN_X, "origin_y": ORIGIN_Y, "crs": CRS}

PRODUCTION_AREAS = [
    {"id": 0, "representative_elevation_m": 100.0, "polygon_utm": box(500000, 4500000, 500080, 4500200)},
    {"id": 1, "representative_elevation_m": 100.0, "polygon_utm": box(500145, 4500000, 500160, 4500200)},
]
ROAD = [LineString([(500000, 4500000), (500200, 4500000)])]


# --- geometric soundness: both candidates stay outside production zones, ranked correctly ---

candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, ROAD)
assert len(candidates) == 2, f"expected 2 candidate regions (west + east), got {len(candidates)}"

west, east = candidates[0], candidates[1]

assert west["rank"] == 1 and east["rank"] == 2
assert west["suitability_score"] > east["suitability_score"], (
    "the south-facing (west) region should outrank the equally-sloped but "
    "north-facing (east) region"
)
assert west["aspect_label"] == "S"
assert east["aspect_label"] == "N"
print(f"Ranking: west (south-facing, score {west['suitability_score']}) ranks above "
      f"east (north-facing, score {east['suitability_score']}).")

for candidate, label in ((west, "west"), (east, "east")):
    min_x, min_y, max_x, max_y = candidate["polygon_utm"].bounds
    assert min_x > 80, f"{label} candidate should stay outside production zone 0's footprint+buffer"
    assert min_x > 95 or max_x < 130, f"{label} candidate should respect production zone 1's exclusion too"
    assert max_y <= 4500152, f"{label} candidate should stay within the road proximity buffer (~150m), got max_y={max_y}"
    assert candidate["distance_to_production_zone_m"] is not None and candidate["distance_to_production_zone_m"] > 0
    assert candidate["distance_to_road_m"] is not None and candidate["distance_to_road_m"] >= 0
print("Both candidates are geometrically outside production zones and within the road proximity buffer.")


# --- road data unavailable (None) disables the proximity constraint, extending coverage north ---

candidates_no_road_data = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, None)
assert len(candidates_no_road_data) == 2
no_road_max_y = max(c["polygon_utm"].bounds[3] for c in candidates_no_road_data)
with_road_max_y = max(c["polygon_utm"].bounds[3] for c in candidates)
assert no_road_max_y > with_road_max_y, (
    "with road data unavailable (None), the proximity constraint should be disabled, "
    "so candidates should extend further north than when a real road buffer is applied"
)
assert all(c["distance_to_road_m"] is None for c in candidates_no_road_data)
print("Road data unavailable (None) disables the proximity constraint instead of zeroing out every candidate.")


# --- road data present but empty ([]) is a real, binding constraint: zero candidates ---

candidates_empty_roads = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, [])
assert candidates_empty_roads == [], (
    "an empty road list (successfully fetched, genuinely no roads nearby) should be treated "
    "as a real constraint -- nothing is within any proximity buffer of a nonexistent road"
)
print("Road data present but empty is treated as a real, binding constraint (zero candidates).")


# --- flag_prime_farmland_conflicts: flags, does not exclude or re-rank ---

prime_classifications = [
    {"mukey": "1", "muname": "Some prime soil", "farmland_classification": "All areas are prime farmland"}
]
flagged = flag_prime_farmland_conflicts([dict(c) for c in candidates], prime_classifications)
assert len(flagged) == 2, "flagging must not remove any candidates"
assert all(c["prime_farmland_conflict"] is True for c in flagged)
assert flagged[0]["rank"] == 1 and flagged[1]["rank"] == 2, "flagging must not re-rank candidates"
print("flag_prime_farmland_conflicts flags every candidate without excluding or re-ranking any.")

non_prime_classifications = [
    {"mukey": "2", "muname": "Some other soil", "farmland_classification": "Not prime farmland"}
]
not_flagged = flag_prime_farmland_conflicts([dict(c) for c in candidates], non_prime_classifications)
assert all(c["prime_farmland_conflict"] is False for c in not_flagged)
print("flag_prime_farmland_conflicts correctly finds no conflict when no prime soil is present.")


# --- output: schema-valid FeatureCollection on the required layer, with required properties ---

geojson = candidates_to_geojson(flagged)
validate_feature_collection(geojson)
required_props = {
    "suitability_score", "avg_slope_pct", "aspect", "distance_to_road_ft",
    "distance_to_production_zone_ft", "constraints_satisfied",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "solar_infrastructure"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert "canopy height model" in feature["properties"]["confidence_notes"] or "rough" in feature["properties"]["confidence_notes"].lower()
print("candidates_to_geojson output is schema-valid, layer='solar_infrastructure', with all required properties.")

# distance_to_road_ft should roughly be the meters value / 0.3048
for candidate, feature in zip(flagged, geojson["features"]):
    expected_ft = candidate["distance_to_road_m"] / 0.3048
    assert abs(feature["properties"]["distance_to_road_ft"] - expected_ft) < 0.5
print("distance_to_road_ft is correctly converted from meters.")


# --- min suitability score threshold is actually applied ---

strict_candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, ROAD, min_suitability_score=0.99)
assert strict_candidates == [], "a near-impossible suitability threshold should leave no qualifying candidates"
print(f"Raising min_suitability_score above what any cell can reach (default is {MIN_SUITABILITY_SCORE}) correctly yields no candidates.")

print("\nAll solar_suitability checks passed.")
