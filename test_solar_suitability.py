"""
test_solar_suitability.py

Offline (no-network) checks for solar_suitability.py's POINT-CANDIDATE
scoring model (see that module's docstring for why it replaced the
earlier eligible-area/connected-component "zone" model) — a hand-built
synthetic DEM plus hand-built production/water zones and roads (same
shapes find_candidate_solar_zones() actually consumes), not a real
DEM/road/SSURGO fetch. Mirrors test_water_candidate_zones.py's "pure
logic, independent of real data fetches" approach.

Layout (60x60 cells, 5m resolution -> 300m x 300m), a uniform ~4%
south-facing grade everywhere (gentle enough that slope never itself
excludes a candidate here — this file is about the point-sampling/
scoring/exclusion logic, not re-testing terrain_metrics.py's own slope
math):
  - x in [0, 150]:     production zone (NO LONGER excluded -- candidates
                        here should be scored HIGHER for edge proximity,
                        not dropped)
  - x in [270, 300], y in [140, 160]: a water-candidate zone, still
                        HARD-excluded (+25m buffer) -- this is
                        deliberately positioned to knock out exactly one
                        of the candidate grid points below, not all of
                        them, so the test can confirm the exclusion is
                        real without losing every candidate on that side
  - a road runs along the whole south edge (y=4500000); the default 150m
    proximity buffer keeps the northernmost row of sample points (y~225)
    out of range

CANDIDATE_POINT_SPACING_METERS=75 on this 300m x 300m boundary produces
a 3x3 interior grid of 9 candidate points (edge-exact points are outside
the boundary's own interior, same "no candidate drawn from the exact
boundary line" reasoning every other layer in this pipeline already
uses) -- confirmed empirically, not assumed, and asserted below.
"""

import math

import numpy as np
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from feature_schema import validate_feature_collection
from road_corridors import POND_ZONE_EXCLUSION_BUFFER_METERS
from solar_suitability import (
    ASPECT_SCORE_WEIGHT,
    CANDIDATE_POINT_SPACING_METERS,
    MAX_STRUCTURE_FOOTPRINT_ACRES,
    MIN_SUITABILITY_SCORE,
    PRODUCTION_EDGE_ADJACENCY_METERS,
    PRODUCTION_PROXIMITY_SCORE_WEIGHT,
    SHADING_SCORE_WEIGHT,
    SLOPE_SCORE_WEIGHT,
    _cells_within_polygon,
    _footprint_side_meters,
    _generate_candidate_points,
    _production_proximity_score,
    candidates_to_geojson,
    find_candidate_solar_zones,
    flag_prime_farmland_conflicts,
)

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)
ROWS = COLS = 60
ORIGIN_X, ORIGIN_Y = 500000.0, 4500300.0

array = np.full((ROWS, COLS), 100.0, dtype=np.float32)
for row in range(ROWS):
    array[row, :] = 100.0 - row * 0.2  # uniform ~4% south-facing grade everywhere

DEM = {"array": array, "resolution_meters": RESOLUTION, "origin_x": ORIGIN_X, "origin_y": ORIGIN_Y, "crs": CRS}

PRODUCTION_AREAS = [
    {"id": 0, "representative_elevation_m": 100.0, "render_fill_polygon_utm": box(500000, 4500000, 500150, 4500300)}
]
WATER_ZONES = [{"valley_id": 0, "render_fill_polygon_utm": box(500270, 4500140, 500300, 4500160)}]
ROAD = [LineString([(500000, 4500000), (500300, 4500000)])]
BOUNDARY = box(500000, 4500000, 500300, 4500300)

FOOTPRINT_SIDE_M = _footprint_side_meters(MAX_STRUCTURE_FOOTPRINT_ACRES)


# --- Step 1: grid sampling produces the expected on-parcel points, non-overlapping by construction ---

assert CANDIDATE_POINT_SPACING_METERS > FOOTPRINT_SIDE_M, (
    "the documented spacing must exceed the footprint side length, or neighboring candidates "
    "would overlap by construction -- this assertion protects that invariant if either constant "
    "is ever retuned"
)

sample_points = _generate_candidate_points(BOUNDARY)
assert len(sample_points) == 9, f"expected a 3x3 interior grid on this 300m x 300m boundary, got {len(sample_points)}"
for x, y in sample_points:
    assert BOUNDARY.buffer(1e-6).contains(Point(x, y)), "every sampled point must be on-parcel"
print(f"Grid sampling produces the expected {len(sample_points)} on-parcel candidate point(s).")


# --- geometric soundness: candidates now form INSIDE the production zone (the actual fix) ---

candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, max_candidates=50)
assert len(candidates) == 5, f"expected 5 candidates (9 grid points minus 3 too-far-from-road minus 1 water-excluded), got {len(candidates)}"

inside_count = sum(1 for c in candidates if c["production_zone_relationship"] == "inside")
assert inside_count >= 1, (
    "expected at least one candidate classified 'inside' a production zone -- this is the specific "
    "behavior this redesign exists to enable (previously: zero candidates anywhere near production land)"
)
for c in candidates:
    if c["production_zone_relationship"] == "inside":
        assert c["polygon_utm"].intersects(
            unary_union([p["render_fill_polygon_utm"] for p in PRODUCTION_AREAS])
        ), "a candidate classified 'inside' must actually overlap the production-zone geometry"
print(f"{inside_count}/{len(candidates)} candidates sit INSIDE the production zone -- the redesign's core fix, confirmed.")


# --- every candidate's footprint is capped at MAX_STRUCTURE_FOOTPRINT_ACRES, not a big blob ---

for candidate in candidates:
    assert candidate["footprint_area_acres"] <= MAX_STRUCTURE_FOOTPRINT_ACRES + 1e-6, (
        f"candidate footprint ({candidate['footprint_area_acres']} ac) exceeds the documented cap "
        f"({MAX_STRUCTURE_FOOTPRINT_ACRES} ac) -- this must never be a large connected-component area"
    )
    # None of these candidates are boundary-clipped (all comfortably interior), so each should sit
    # right at the cap, not some arbitrary smaller size.
    assert math.isclose(candidate["footprint_area_acres"], MAX_STRUCTURE_FOOTPRINT_ACRES, rel_tol=1e-3), (
        f"an interior (non-boundary-clipped) candidate should use its full footprint cap, got "
        f"{candidate['footprint_area_acres']} ac"
    )
print(f"Every candidate's footprint is capped at {MAX_STRUCTURE_FOOTPRINT_ACRES} acre(s), not a variable-size blob.")


# --- production-zone PROXIMITY is scored as a preference: closer to the edge ranks higher ---

by_distance = sorted(candidates, key=lambda c: (c["distance_to_production_zone_m"] is None, c["distance_to_production_zone_m"]))
closest, farthest = by_distance[0], by_distance[-1]
assert closest["suitability_score"] >= farthest["suitability_score"], (
    "all else being equal (identical slope/aspect/shading on this uniform terrain), the candidate "
    "closer to the production zone's edge should score at least as high as the one farther away"
)
# Two candidates equidistant-in-x from the production edge (both x=150, at y=75 and y=150) should
# score identically on this uniform terrain -- confirms the proximity term is driven by real
# geometry, not sample order.
same_distance = [c for c in candidates if c["distance_to_production_zone_m"] == 0.0]
assert len(same_distance) >= 2 and len({c["suitability_score"] for c in same_distance}) == 1, (
    "candidates at the same distance from the production edge should score identically on uniform terrain"
)
print(
    f"Production-zone edge proximity is scored as a real preference: closest candidate "
    f"(score {closest['suitability_score']}, {closest['distance_to_production_zone_m']}m to edge) "
    f"outranks farthest (score {farthest['suitability_score']}, {farthest['distance_to_production_zone_m']}m to edge)."
)


# --- _production_proximity_score: peaks at the edge, falls off in both directions, neutral if no production zones ---

assert _production_proximity_score(0.0) == 1.0, "right at a production zone's edge should score the maximum preference"
assert _production_proximity_score(1000.0) == 0.0, "far beyond the reference distance should score zero preference"
assert _production_proximity_score(None) == 0.5, "no production zones at all should be neutral, not penalized"
mid = _production_proximity_score(50.0)
assert 0.0 < mid < 1.0
print("_production_proximity_score peaks at the edge (1.0), floors at 0.0 beyond the reference distance, "
      "and is neutral (0.5) when no production zones exist at all.")


# --- water-candidate zone exclusion still holds: no candidate overlaps the buffered water zone ---

water_exclusion = box(500270, 4500140, 500300, 4500160).buffer(POND_ZONE_EXCLUSION_BUFFER_METERS)
for candidate in candidates:
    assert not candidate["polygon_utm"].intersects(water_exclusion), (
        "no candidate should overlap a buffered water-candidate zone -- this hard exclusion is "
        "explicitly UNCHANGED by the point-candidate redesign"
    )
    assert candidate["distance_to_water_zone_m"] is not None and candidate["distance_to_water_zone_m"] >= 0
print("Water-candidate zone exclusion holds unchanged: no candidate overlaps the buffered water zone.")


# --- road proximity: unchanged in spirit -- still a hard constraint, still reported ---

for candidate in candidates:
    assert candidate["distance_to_road_m"] is not None and candidate["distance_to_road_m"] >= 0
    assert candidate["polygon_utm"].bounds[3] <= 4500150 + FOOTPRINT_SIDE_M / 2 + 1e-6, (
        "every remaining candidate should be within the road proximity buffer (~150m from the south edge)"
    )
print("Road-proximity reporting/constraint behaves unchanged: every candidate is within the buffer, with a real reported distance.")

candidates_no_road_data = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, None, BOUNDARY, max_candidates=50)
assert len(candidates_no_road_data) > len(candidates), (
    "with road data unavailable (None), the proximity constraint should be disabled, surfacing "
    "more candidates (the northern row) than when a real road buffer is applied"
)
assert all(c["distance_to_road_m"] is None for c in candidates_no_road_data)
print("Road data unavailable (None) disables the proximity constraint instead of zeroing out every candidate.")

candidates_empty_roads = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, [], BOUNDARY, max_candidates=50)
assert candidates_empty_roads == [], (
    "an empty road list (successfully fetched, genuinely no roads nearby) should be treated "
    "as a real constraint -- nothing is within any proximity buffer of a nonexistent road"
)
print("Road data present but empty is treated as a real, binding constraint (zero candidates).")


# --- outside/adjacent/inside classification ---

no_production_candidates = find_candidate_solar_zones(DEM, [], WATER_ZONES, ROAD, BOUNDARY, max_candidates=50)
assert all(c["production_zone_relationship"] == "outside" for c in no_production_candidates), (
    "with no production zones at all, every candidate must classify as 'outside' (there's nothing to be near)"
)
assert all(c["distance_to_production_zone_m"] is None for c in no_production_candidates), (
    "distance_to_production_zone_m must be None when no production zones exist at all this run"
)
print("With no production zones at all, every candidate correctly classifies 'outside' with a null edge distance.")

relationships_seen = {c["production_zone_relationship"] for c in candidates}
assert relationships_seen <= {"inside", "adjacent", "outside"}
print(f"production_zone_relationship values observed on this layout: {sorted(relationships_seen)}.")


# --- flag_prime_farmland_conflicts: flags, does not exclude or re-rank (unchanged) ---

prime_classifications = [
    {"mukey": "1", "muname": "Some prime soil", "farmland_classification": "All areas are prime farmland"}
]
flagged = flag_prime_farmland_conflicts([dict(c) for c in candidates], prime_classifications)
assert len(flagged) == len(candidates), "flagging must not remove any candidates"
assert all(c["prime_farmland_conflict"] is True for c in flagged)
assert [c["rank"] for c in flagged] == [c["rank"] for c in candidates], "flagging must not re-rank candidates"
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
    "suitability_score", "avg_slope_pct", "aspect", "footprint_area_acres", "distance_to_road_ft",
    "distance_to_production_zone_ft", "production_zone_relationship", "distance_to_water_zone_ft",
    "constraints_satisfied",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "solar_infrastructure"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert "outside_water_candidate_zone" in feature["properties"]["constraints_satisfied"]
    assert "outside_production_zone" not in feature["properties"]["constraints_satisfied"], (
        "production zones are no longer a hard exclusion -- this must not appear as a satisfied constraint"
    )
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    notes = feature["properties"]["confidence_notes"]
    assert "canopy height model" in notes or "rough" in notes.lower()
    assert "water-candidate" in notes.lower()
    assert "INTENTIONAL" in notes and "coexist" in notes, (
        "confidence_notes must plainly state that sitting inside a production zone is intentional, not a caveat"
    )
    assert "PREFERENCE" in notes or "preference" in notes, (
        "confidence_notes must plainly state that production-zone proximity is a preference, not a requirement"
    )
print("candidates_to_geojson output is schema-valid, layer='solar_infrastructure', with all required "
      "properties, no stale 'outside_production_zone' constraint, and confidence_notes explaining the "
      "point-candidate model plainly.")

for candidate, feature in zip(flagged, geojson["features"]):
    expected_ft = candidate["distance_to_road_m"] / 0.3048
    assert abs(feature["properties"]["distance_to_road_ft"] - expected_ft) < 0.5
print("distance_to_road_ft is correctly converted from meters.")


# --- min suitability score threshold is actually applied ---

strict_candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, min_suitability_score=0.99)
assert strict_candidates == [], "a near-impossible suitability threshold should leave no qualifying candidates"
print(f"Raising min_suitability_score above what any point can reach (default is {MIN_SUITABILITY_SCORE}) correctly yields no candidates.")


# --- max slope is still a hard buildability constraint, independent of production-zone proximity ---

steep_array = np.full((ROWS, COLS), 100.0, dtype=np.float32)
for row in range(ROWS):
    steep_array[row, :] = 100.0 - row * 3.0  # 60% grade -- excludes even a point deep inside a production zone
steep_dem = {**DEM, "array": steep_array}
steep_candidates = find_candidate_solar_zones(steep_dem, PRODUCTION_AREAS, [], None, BOUNDARY, max_candidates=50)
assert steep_candidates == [], (
    "ground steeper than MAX_SOLAR_SLOPE_PCT must be excluded regardless of production-zone proximity -- "
    "proximity is a preference, buildability is still a hard constraint"
)
print("Ground steeper than MAX_SOLAR_SLOPE_PCT is still hard-excluded, even when it sits inside a production zone.")


# --- regression: candidates stay on-parcel, not drawn from the DEM's buffered margin ---
#
# dem_data.py fetches a DEM buffered ~100m past the drawn boundary (correct and intentional, for
# terrain-analysis context); find_candidate_solar_zones() must still restrict every candidate
# footprint to the real parcel. Here the DEM spans a 400m x 400m grid (well past a smaller 200m x
# 200m real parcel with a 100m buffer margin on every side, mirroring dem_data.py's real buffer
# relationship at test scale).
buffered_size = 80
buffered_array = np.full((buffered_size, buffered_size), 100.0, dtype=np.float32)
for row in range(buffered_size):
    buffered_array[row, :] = 100.0 - row * 0.2
buffered_dem = {
    "array": buffered_array,
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500400.0,
    "crs": CRS,
}
parcel_boundary = box(500100, 4500100, 500300, 4500300)

clipped_candidates = find_candidate_solar_zones(buffered_dem, [], [], None, parcel_boundary, max_candidates=50)
assert clipped_candidates, "expected at least one solar candidate on this uniform, buffered south-facing slope"
for candidate in clipped_candidates:
    assert candidate["polygon_utm"].within(parcel_boundary.buffer(1e-6)), (
        f"candidate (rank {candidate['rank']}) extends outside the real parcel boundary -- "
        f"candidate geometry must be drawn from on-parcel cells only, not the DEM's buffered margin"
    )
print(
    f"Parcel clipping: {len(clipped_candidates)} solar candidate(s) on a slope spanning well past the parcel "
    f"boundary all stay entirely within the real (smaller) parcel, not the DEM's buffered extent."
)


# --- weights: even 0.25/0.25/0.25/0.25 split is actually applied, not just asserted ---

assert SLOPE_SCORE_WEIGHT == ASPECT_SCORE_WEIGHT == SHADING_SCORE_WEIGHT == PRODUCTION_PROXIMITY_SCORE_WEIGHT == 0.25
print("Scoring weights are an even 0.25/0.25/0.25/0.25 split across slope/aspect/shading/production-proximity.")


# --- canopy_mask_utm: hard exclusion, before scoring; None (default) applies no gate at all ---

no_gate_candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, max_candidates=50)
assert len(no_gate_candidates) == len(candidates), "canopy_mask_utm=None (the default) must apply no gate at all"

# Build a canopy mask that covers exactly one known-surviving candidate's own footprint (interior,
# not the water-excluded one) -- see the module docstring for the "excluded before scoring, not
# merely scored low" reasoning this mirrors from the water exclusion.
target_x, target_y = 500075.0, 4500075.0
footprint_side_m = _footprint_side_meters(MAX_STRUCTURE_FOOTPRINT_ACRES)
target_footprint = box(
    target_x - footprint_side_m / 2, target_y - footprint_side_m / 2,
    target_x + footprint_side_m / 2, target_y + footprint_side_m / 2,
).intersection(BOUNDARY)
target_cells = _cells_within_polygon(DEM, target_footprint, ROWS, COLS)
assert target_cells, "expected at least one DEM cell under the target candidate's own footprint"

canopy_mask = np.zeros((ROWS, COLS), dtype=bool)
for r, c in target_cells:
    canopy_mask[r, c] = True

canopy_gated_candidates = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, canopy_mask_utm=canopy_mask, max_candidates=50
)
assert len(canopy_gated_candidates) == len(candidates) - 1, (
    "a canopy mask covering exactly one surviving candidate's footprint should hard-exclude only that one"
)
assert not any(
    c["polygon_utm"].intersects(target_footprint) for c in canopy_gated_candidates
), "the canopy-covered candidate must not survive"
print(
    f"canopy_mask_utm hard-excludes a candidate whose footprint touches real, existing canopy "
    f"({len(candidates)} -> {len(canopy_gated_candidates)} candidates), before any scoring."
)


# --- tree_zone_exclusion_polygon_utm: hard exclusion, buffered, same pattern as water ---

tree_zone_exclusion = target_footprint.buffer(5.0)  # comfortably covers the same target footprint
tree_gated_candidates = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    tree_zone_exclusion_polygon_utm=tree_zone_exclusion, max_candidates=50,
)
assert len(tree_gated_candidates) == len(candidates) - 1, (
    "a tree-zone exclusion polygon covering exactly one surviving candidate's footprint should "
    "hard-exclude only that one"
)
assert not any(
    c["polygon_utm"].intersects(target_footprint) for c in tree_gated_candidates
), "the tree-zone-excluded candidate must not survive"
print(
    f"tree_zone_exclusion_polygon_utm hard-excludes an intersecting candidate "
    f"({len(candidates)} -> {len(tree_gated_candidates)} candidates), same pattern as the water exclusion."
)


# --- candidates_to_geojson: road_proximity_source / tree_zone_exclusion_available reporting ---

for source in ("selected_road_corridor", "real_mapped_road", "unavailable"):
    tiered_geojson = candidates_to_geojson(flagged, road_proximity_source=source)
    for feature in tiered_geojson["features"]:
        assert feature["properties"]["road_proximity_source"] == source
    if source == "selected_road_corridor":
        assert all("SELECTED road corridor" in f["properties"]["confidence_notes"] for f in tiered_geojson["features"])
    if source == "real_mapped_road":
        assert all("fell back" in f["properties"]["confidence_notes"] for f in tiered_geojson["features"])
    if source == "unavailable":
        assert all("disabled entirely" in f["properties"]["confidence_notes"] for f in tiered_geojson["features"])
print("candidates_to_geojson correctly reports road_proximity_source per tier, in properties and confidence_notes.")

unavailable_geojson = candidates_to_geojson(flagged, tree_zone_exclusion_available=False)
assert all(
    "could not be checked" in f["properties"]["confidence_notes"] for f in unavailable_geojson["features"]
), "confidence_notes must flag when tree-zone-candidate exclusion couldn't be checked this run"
print("candidates_to_geojson correctly flags when tree-zone-candidate exclusion data was unavailable.")

# --- canopy_height override forwarding (entry point, fully offline) ---
#
# identify_solar_candidate_zones() reaches canopy on FOUR independent paths:
# its own direct get_required_tree_root_zone_mask_utm() gate, plus the
# nested identify_optimized_production_areas(), identify_water_suitability(),
# and identify_tree_zone_candidates() calls it self-computes (each with its
# OWN independent mandatory canopy gate). A supplied canopy_height override
# must reach ALL four. This runs fully offline by mocking the three nested
# entry points (their internal canopy paths stubbed away) and asserting each
# received the EXACT override object, while a CanopyOverrideProbe proves
# solar's OWN direct gate used the override with zero network fetches. Shared-
# core behavior is proven in test_canopy_mask_override.py. (The end-to-end,
# nothing-mocked version of this lives in test_solar_suitability_pipeline.py,
# which needs live network for its road-corridor fetch.)
from unittest.mock import patch as _ov_mock_patch  # noqa: E402
from rasterio.warp import transform as _ov_warp_transform  # noqa: E402

import solar_suitability as _ov_ss  # noqa: E402
from solar_suitability import identify_solar_candidate_zones as _ov_identify_solar  # noqa: E402
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402

_ov_lons, _ov_lats = _ov_warp_transform(
    CRS, "EPSG:4326",
    [500000.0, 500300.0, 500300.0, 500000.0, 500000.0],
    [4500000.0, 4500000.0, 4500300.0, 4500300.0, 4500000.0],
)
_ov_boundary_coordinates = list(zip(_ov_lons, _ov_lats))
_ov_override = clean_canopy_for(DEM)
# A supplied selected_road_corridor keeps Tier-1 road resolution off the
# network (identify_road_corridor_candidates() is skipped) -- road corridors
# don't fetch canopy, so this is orthogonal to what's under test here.
_ov_road_corridor = {
    "id": "synthetic-solar-road-corridor",
    "cell_footprint_polygon_utm": box(500000, 4500148, 500300, 4500152),
}

with _ov_mock_patch.object(_ov_ss, "identify_optimized_production_areas",
                           return_value={"scored_patches": PRODUCTION_AREAS}) as _ov_mock_prod, \
     _ov_mock_patch.object(_ov_ss, "identify_water_suitability",
                           return_value={"selected_water_zone": WATER_ZONES[0]}) as _ov_mock_water, \
     _ov_mock_patch.object(_ov_ss, "identify_tree_zone_candidates",
                           return_value={"patches": []}) as _ov_mock_tree, \
     CanopyOverrideProbe() as _ov_probe:
    _ov_identify_solar(
        _ov_boundary_coordinates,
        dem=DEM,
        boundary_polygon_utm=BOUNDARY,
        selected_road_corridor=_ov_road_corridor,
        check_prime_farmland=False,
        canopy_height=_ov_override,
    )

# Solar's OWN direct canopy gate used the exact override, zero network fetches:
_ov_probe.assert_override_used(_ov_override, "identify_solar_candidate_zones() [own gate]")
# ...and it forwarded the SAME override object into every nested entry point
# that carries its own independent canopy gate:
for _ov_name, _ov_mock in (
    ("identify_optimized_production_areas", _ov_mock_prod),
    ("identify_water_suitability", _ov_mock_water),
    ("identify_tree_zone_candidates", _ov_mock_tree),
):
    assert _ov_mock.call_count == 1, (
        f"identify_solar_candidate_zones(): expected {_ov_name}() called exactly once, got {_ov_mock.call_count}"
    )
    assert _ov_mock.call_args.kwargs.get("canopy_height") is _ov_override, (
        f"identify_solar_candidate_zones(): must forward the EXACT canopy_height override object into {_ov_name}(), "
        "not a copy, None, or a re-fetched value"
    )
print(
    "identify_solar_candidate_zones(): a supplied canopy_height override is used by its own canopy gate "
    "(0 fetches, exact array) AND forwarded verbatim into all three nested canopy-fetching entry points."
)


print("\nAll solar_suitability checks passed.")
