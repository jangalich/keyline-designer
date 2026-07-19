"""
test_solar_suitability.py

Offline (no-network) checks for solar_suitability.py's constraint-stack
and ranking logic — a hand-built synthetic DEM plus hand-built production/
water zones and roads (same shapes find_candidate_solar_zones() actually
consumes), not a real DEM/road/SSURGO fetch. Mirrors
test_water_candidate_zones.py's "pure logic, independent of real data
fetches" approach.

Also a regression test for two real bugs found live against the real
property:
  1. Water-candidate zones (pond/dam siting ground) were never checked at
     all -- a solar candidate could sit directly on top of one. Fixed by
     hard-excluding them the same way production zones are (buffered by
     road_corridors.POND_ZONE_EXCLUSION_BUFFER_METERS, reused rather than
     reinvented).
  2. distance_to_production_zone_ft (and distance_to_road_ft) were
     measured from the candidate polygon's CENTROID to the BUFFERED
     exclusion geometry, not the candidate's own nearest EDGE to the RAW
     zone -- both effects independently inflated the reported distance
     above the real nearest-edge distance a plain shapely .distance()
     check gives. Fixed to measure edge-to-edge against raw geometry;
     this file cross-checks that directly against independent
     .distance() calls, not just against the (previously also wrong)
     code path.

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

import numpy as np
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from feature_schema import validate_feature_collection
from road_corridors import POND_ZONE_EXCLUSION_BUFFER_METERS
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
WATER_ZONES: list[dict] = []
ROAD = [LineString([(500000, 4500000), (500200, 4500000)])]

# The DEM's own full extent — used everywhere except the parcel-clipping
# regression test below, so existing behavior/assertions are unaffected
# (100% on-parcel, nothing to clip).
BOUNDARY = box(ORIGIN_X, ORIGIN_Y - ROWS * RESOLUTION[1], ORIGIN_X + COLS * RESOLUTION[0], ORIGIN_Y)


# --- geometric soundness: both candidates stay outside production zones, ranked correctly ---

candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY)
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


# --- regression: reported distances match independent shapely edge-to-edge checks exactly ---

raw_production_union = unary_union([p["polygon_utm"] for p in PRODUCTION_AREAS])
road_union = unary_union(ROAD)

for candidate, label in ((west, "west"), (east, "east")):
    independent_production_distance = candidate["polygon_utm"].distance(raw_production_union)
    independent_road_distance = candidate["polygon_utm"].distance(road_union)

    assert abs(candidate["distance_to_production_zone_m"] - independent_production_distance) < 1e-6, (
        f"{label}: reported distance_to_production_zone_m ({candidate['distance_to_production_zone_m']}) "
        f"must equal an independent polygon.distance() check ({independent_production_distance}), not a "
        f"centroid- or buffered-geometry-based approximation"
    )
    assert abs(candidate["distance_to_road_m"] - independent_road_distance) < 1e-6, (
        f"{label}: reported distance_to_road_m ({candidate['distance_to_road_m']}) must equal an "
        f"independent polygon.distance() check ({independent_road_distance})"
    )
    # The centroid-based bug specifically overstated distance -- confirm the
    # fixed (edge-based) value is strictly less than what centroid.distance()
    # would have given, on these non-trivially-sized candidate polygons.
    centroid_production_distance = candidate["polygon_utm"].centroid.distance(raw_production_union)
    assert independent_production_distance < centroid_production_distance, (
        f"{label}: edge-to-edge distance should be less than centroid-to-edge distance for a real polygon "
        f"(centroid was {centroid_production_distance}, edge was {independent_production_distance}) -- "
        f"if these are equal, the fix silently regressed back to centroid-based measurement"
    )
print(
    f"Reported distances match independent shapely .distance() checks exactly "
    f"(west: {west['distance_to_production_zone_m']}m to production, {west['distance_to_road_m']}m to road; "
    f"east: {east['distance_to_production_zone_m']}m to production, {east['distance_to_road_m']}m to road) "
    f"-- and are strictly less than the old centroid-based measurement would have given."
)


# --- water-candidate zone exclusion: a zone placed inside the west region shrinks/splits it ---

water_zone_cutting_west = [
    {"valley_id": 0, "polygon_utm": box(500110, 4500000, 500125, 4500200)}
]
candidates_with_water_zone = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, water_zone_cutting_west, ROAD, BOUNDARY)

water_zone_exclusion = box(500110, 4500000, 500125, 4500200).buffer(POND_ZONE_EXCLUSION_BUFFER_METERS)
for candidate in candidates_with_water_zone:
    assert not candidate["polygon_utm"].intersects(water_zone_exclusion), (
        "no candidate should overlap a buffered water-candidate zone -- this is the exact "
        "bug found live: a candidate sitting directly on/against a water-candidate zone"
    )
    assert candidate["distance_to_water_zone_m"] is not None and candidate["distance_to_water_zone_m"] >= 0
    independent_water_distance = candidate["polygon_utm"].distance(box(500110, 4500000, 500125, 4500200))
    assert abs(candidate["distance_to_water_zone_m"] - independent_water_distance) < 1e-6

# The water zone cuts directly through the middle of the west region (x 110-125,
# well inside the west region's x 100-145 span) -- west must shrink relative to
# the no-water-zone baseline, whether that leaves a smaller remaining patch or
# eliminates it entirely (either is a legitimate real-constraint outcome, not a bug).
west_area_before = west["polygon_utm"].area
west_like_after = [c for c in candidates_with_water_zone if c["polygon_utm"].bounds[0] < 500110]
west_area_after = sum(c["polygon_utm"].area for c in west_like_after)
assert west_area_after < west_area_before, (
    f"a water-candidate zone cutting through the middle of the west region should shrink it "
    f"(before: {west_area_before}m2, after: {west_area_after}m2)"
)
print(
    f"A water-candidate zone placed inside the west region correctly excludes candidates from its "
    f"buffered footprint (west area {west_area_before:.0f}m2 -> {west_area_after:.0f}m2), and "
    f"distance_to_water_zone_m matches an independent shapely check."
)


# --- road data unavailable (None) disables the proximity constraint, extending coverage north ---

candidates_no_road_data = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, None, BOUNDARY)
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

candidates_empty_roads = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, [], BOUNDARY)
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
    "distance_to_production_zone_ft", "distance_to_water_zone_ft", "constraints_satisfied",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "solar_infrastructure"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert "outside_water_candidate_zone" in feature["properties"]["constraints_satisfied"]
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert "canopy height model" in feature["properties"]["confidence_notes"] or "rough" in feature["properties"]["confidence_notes"].lower()
    assert "water-candidate" in feature["properties"]["confidence_notes"].lower()
print("candidates_to_geojson output is schema-valid, layer='solar_infrastructure', with all required "
      "properties including distance_to_water_zone_ft and the water-zone exclusion constraint/caveat.")

# distance_to_road_ft should roughly be the meters value / 0.3048
for candidate, feature in zip(flagged, geojson["features"]):
    expected_ft = candidate["distance_to_road_m"] / 0.3048
    assert abs(feature["properties"]["distance_to_road_ft"] - expected_ft) < 0.5
print("distance_to_road_ft is correctly converted from meters.")


# --- min suitability score threshold is actually applied ---

strict_candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, min_suitability_score=0.99)
assert strict_candidates == [], "a near-impossible suitability threshold should leave no qualifying candidates"
print(f"Raising min_suitability_score above what any cell can reach (default is {MIN_SUITABILITY_SCORE}) correctly yields no candidates.")


# --- regression: candidates stay on-parcel, not drawn from the DEM's buffered margin ---
#
# This is the exact live bug found against the real property: dem_data.py
# fetches a DEM buffered ~100m past the drawn boundary (correct and
# intentional, for terrain-analysis context), but find_candidate_solar_zones()
# never restricted eligible cells to the actual parcel -- a candidate could
# be built entirely from off-parcel ground. Here the DEM is a uniform,
# gentle south-facing slope spanning the FULL 300m x 300m grid (so,
# unclipped, eligible ground spans the whole width, well past the parcel
# on every side); the boundary below is a smaller 200m x 200m parcel with
# a 50m buffer margin on every side, mirroring dem_data.py's real buffer
# relationship at test scale.
buffered_size = 60
buffered_array = np.full((buffered_size, buffered_size), 100.0, dtype=np.float32)
for row in range(buffered_size):
    buffered_array[row, :] = 100.0 - row * 0.2  # uniform ~4% south-facing grade, same as the WEST region above
buffered_dem = {
    "array": buffered_array,
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500300.0,
    "crs": CRS,
}
# The real parcel: 200m x 200m, with a 50m buffer margin to the DEM's edge
# on every side -- the DEM covers ground the parcel itself doesn't.
parcel_boundary = box(500050, 4500050, 500250, 4500250)

clipped_candidates = find_candidate_solar_zones(
    buffered_dem, [], [], None, parcel_boundary, max_candidates=50
)
assert clipped_candidates, "expected at least one solar candidate on this uniform, buffered south-facing slope"
for candidate in clipped_candidates:
    assert candidate["polygon_utm"].within(parcel_boundary.buffer(1e-6)), (
        f"candidate (rank {candidate['rank']}) extends outside the real parcel boundary -- "
        f"candidate geometry must be drawn from on-parcel cells only, not the DEM's buffered margin"
    )

# Confirm the regression test is actually meaningful (not vacuously
# passing): on the SAME terrain, using boundary == the DEM's full extent
# (i.e., no real clipping, matching the old unclipped behavior) produces
# candidates that DO extend past the smaller real parcel.
full_extent_boundary = box(500000, 4500000, 500300, 4500300)
unclipped_candidates = find_candidate_solar_zones(
    buffered_dem, [], [], None, full_extent_boundary, max_candidates=50
)
outside_parcel = [c for c in unclipped_candidates if not c["polygon_utm"].within(parcel_boundary.buffer(1e-6))]
assert outside_parcel, (
    "expected some candidates to extend past the smaller real parcel when boundary == full DEM extent -- "
    "if none do, this regression test isn't actually exercising the parcel-clipping fix"
)
print(
    f"Parcel clipping: {len(clipped_candidates)} solar candidate(s) on a slope spanning well past the parcel "
    f"boundary all stay entirely within the real (smaller) parcel, not the DEM's buffered extent "
    f"(confirmed meaningful: {len(outside_parcel)}/{len(unclipped_candidates)} candidates extend past the "
    f"parcel when clipping is effectively disabled)."
)

print("\nAll solar_suitability checks passed.")
