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

CANDIDATE_POINT_SPACING_METERS=25 on this 300m x 300m boundary produces
an 11x11 interior grid of 121 candidate points (edge-exact points are
outside the boundary's own interior, same "no candidate drawn from the
exact boundary line" reasoning every other layer in this pipeline already
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
    SOLAR_RATING_BANDS,
    _production_proximity_score,
    _solar_rating,
    candidates_to_geojson,
    find_candidate_solar_zones,
    flag_prime_farmland_conflicts,
    measure_structure_site,
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
assert len(sample_points) == 121, f"expected an 11x11 interior grid on this 300m x 300m boundary, got {len(sample_points)}"
for x, y in sample_points:
    assert BOUNDARY.buffer(1e-6).contains(Point(x, y)), "every sampled point must be on-parcel"
print(f"Grid sampling produces the expected {len(sample_points)} on-parcel candidate point(s).")


# --- geometric soundness: candidates now form INSIDE the production zone (the actual fix) ---

candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, max_candidates=200)
assert len(candidates) == 62, f"expected 62 candidates (121 grid points minus 53 too-far-from-road minus 6 water-excluded), got {len(candidates)}"

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
# Every candidate in the x=150 column sits exactly on the production edge (distance 0); on this
# uniform terrain they should all score identically -- confirms the proximity term is driven by
# real geometry, not sample order.
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

candidates_no_road_data = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, None, BOUNDARY, max_candidates=200)
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

no_production_candidates = find_candidate_solar_zones(DEM, [], WATER_ZONES, ROAD, BOUNDARY, max_candidates=200)
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

no_gate_candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, max_candidates=200)
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
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, canopy_mask_utm=canopy_mask, max_candidates=200
)
assert len(canopy_gated_candidates) == len(candidates) - 1, (
    "a canopy mask covering exactly one surviving candidate's footprint should hard-exclude only that one "
    "(the mask is cell-based and unbuffered, so it cannot reach a neighboring candidate's own footprint)"
)
assert not any(
    c["polygon_utm"].intersects(target_footprint) for c in canopy_gated_candidates
), "the canopy-covered candidate must not survive"
print(
    f"canopy_mask_utm hard-excludes a candidate whose footprint touches real, existing canopy "
    f"({len(candidates)} -> {len(canopy_gated_candidates)} candidates), before any scoring."
)


# --- tree_zone_exclusion_polygon_utm: hard exclusion, buffered, same pattern as water ---

# The exclusion buffers the target footprint by 5.0m. At the 25m candidate spacing that buffer
# exceeds the ~4.88m gap between neighboring candidate footprints, so it reaches the target's
# immediate grid neighbors too -- a tree-zone exclusion correctly removes EVERY candidate it
# intersects, not only the one at its center. Assert that real contract (the covered candidate
# gone, no survivor still intersecting the polygon) rather than a spacing-fragile exact count.
tree_zone_exclusion = target_footprint.buffer(5.0)  # comfortably covers the target footprint (and, at 25m spacing, its neighbors)
tree_gated_candidates = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    tree_zone_exclusion_polygon_utm=tree_zone_exclusion, max_candidates=200,
)
assert 0 < len(tree_gated_candidates) < len(candidates), (
    "a tree-zone exclusion polygon must hard-exclude at least the candidate(s) it covers, without "
    "wiping out every candidate on the parcel"
)
assert not any(
    c["polygon_utm"].intersects(tree_zone_exclusion) for c in tree_gated_candidates
), "no surviving candidate may intersect the tree-zone exclusion polygon"
assert not any(
    c["polygon_utm"].intersects(target_footprint) for c in tree_gated_candidates
), "the tree-zone-excluded candidate must not survive"
print(
    f"tree_zone_exclusion_polygon_utm hard-excludes every intersecting candidate "
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


# =====================================================================
# narrative_data -- the stored factor scores and build_narrative_data()'s
# own contract, on this file's baseline fixture (uniform ~4% south-facing
# grade, production zone in the west half, road along the south edge).
# Entry-point wiring is checked in test_solar_suitability_pipeline.py.
# =====================================================================
import json  # noqa: E402

import solar_suitability  # noqa: E402
from production_area_ceiling import ELEVATION_POSITION_BANDS, _elevation_position  # noqa: E402
from solar_suitability import build_narrative_data  # noqa: E402

_nd_candidates = find_candidate_solar_zones(DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, max_candidates=5)
assert _nd_candidates, "the baseline fixture must produce candidates for the narrative checks"

# The four factor scores are stored on every candidate (additively) and
# recompose the composite exactly (linear combination, so any mismatch
# beyond rounding means the stored values aren't what was scored).
for _nd_c in _nd_candidates:
    _recomposed = 100.0 * (
        SLOPE_SCORE_WEIGHT * _nd_c["slope_score"]
        + ASPECT_SCORE_WEIGHT * _nd_c["aspect_score"]
        + SHADING_SCORE_WEIGHT * _nd_c["shading_score"]
        + PRODUCTION_PROXIMITY_SCORE_WEIGHT * _nd_c["production_proximity_score"]
    )
    assert abs(_recomposed - _nd_c["suitability_score"]) < 0.3, (
        f"stored factor scores must recompose suitability_score: {_recomposed} vs {_nd_c['suitability_score']}"
    )

_nd = build_narrative_data(
    _nd_candidates,
    BOUNDARY,
    road_proximity_source="real_mapped_road",
    tree_zone_exclusion_available=True,
    water_zone_excluded=True,
    existing_canopy_excluded=False,
)
assert json.loads(json.dumps(_nd)) == _nd, "narrative_data must be json.dumps()-clean with no custom encoder"
assert set(_nd) == {
    "site_found", "candidate_count", "road_proximity_source", "gates", "scales",
    "no_candidates", "selected_site",
}, sorted(_nd)
assert _nd["site_found"] is True and _nd["candidate_count"] == len(_nd_candidates)
# STEP-LEVEL: the same value gates carries, promoted beside candidate_count
# because ft-to-road is the panel's headline figure and its MEANING depends
# entirely on which access source answered.
assert _nd["road_proximity_source"] == "real_mapped_road" == _nd["gates"]["road_proximity_source"]
assert _nd["no_candidates"] is None, "a run that found a site has no zero-candidate explanation"
assert _nd["gates"] == {
    "existing_canopy_excluded": False,
    "water_zone_excluded": True,
    "tree_zone_exclusion_checked": True,
    "road_proximity_source": "real_mapped_road",
    "prime_farmland_checked": False,  # flag_prime_farmland_conflicts() never ran on these
    # NEITHER DRAINAGE GATE WAS APPLIED on this call (no unions supplied),
    # and absent is not clear: both read False and the name list is empty.
    "hydric_gate_checked": False,
    "floodplain_gate_checked": False,
    "drainage_gates_checked": [],
}
# THE BANDS ARE ON THE WIRE, so the frontend holds no threshold. Solar's
# own cuts, and production's ELEVATION_POSITION_BANDS by IMPORT -- asserted
# against the imported constant itself, not a copy of its values.
assert _nd["scales"]["solar_rating"]["bands"] == solar_suitability.SOLAR_RATING_BANDS
assert _nd["scales"]["elevation_position"]["bands"] is ELEVATION_POSITION_BANDS, (
    "elevation_position must ship PRODUCTION's own bands object, not a second copy of the cuts"
)
assert _nd["scales"]["solar_rating"]["not_a"] == "rank_among_candidates"
_nd_selected = max(_nd_candidates, key=lambda c: c["suitability_score"])
_nd_site = _nd["selected_site"]
assert _nd_site["score"] == round(_nd_selected["suitability_score"], 1)
assert _nd_site["footprint_acres"] == round(_nd_selected["footprint_area_acres"], 1)
assert _nd_site["location"]["production_zone_relationship"] == _nd_selected["production_zone_relationship"]
assert _nd_site["location"]["distance_to_road_ft"] == round(_nd_selected["distance_to_road_m"] / 0.3048, 1)
assert _nd_site["location"]["distance_to_production_edge_ft"] == round(
    _nd_selected["distance_to_production_zone_m"] / 0.3048, 1
)
assert _nd_site["location"]["signed_distance_to_production_ft"] == round(
    _nd_selected["signed_distance_to_production_m"] / 0.3048, 1
)
assert _nd_site["location"]["elevation_position"] == _nd_selected["elevation_position"]
assert _nd_site["location"]["elevation_percentile_of_parcel"] == _nd_selected[
    "elevation_percentile_of_parcel"
]
assert _nd_site["solar_rating"] == _nd_selected["solar_rating"]
assert _nd_site["solar_value"] == _nd_selected["solar_value"]
assert _nd_site["location"]["distance_to_water_zone_ft"] == round(
    _nd_selected["distance_to_water_zone_m"] / 0.3048, 1
)
assert _nd_site["location"]["position_in_parcel"] in {
    "center", "north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest",
}
assert _nd_site["benefits"]["facing"] == "south", (
    "the uniform south-facing fixture's selected site must narrate as south-facing, got "
    f"{_nd_site['benefits']['facing']!r}"
)
assert _nd_site["benefits"]["avg_slope_pct"] == round(_nd_selected["avg_slope_pct"], 1)
assert _nd_site["benefits"]["factors"] == {
    "slope": round(_nd_selected["slope_score"] * 100.0, 1),
    "aspect": round(_nd_selected["aspect_score"] * 100.0, 1),
    "shading": round(_nd_selected["shading_score"] * 100.0, 1),
    "production_proximity": round(_nd_selected["production_proximity_score"] * 100.0, 1),
}
assert _nd_site["benefits"]["prime_farmland_conflict"] is None, (
    "prime farmland was never checked on these candidates -- must read None, never False"
)

# No-candidate outcome: site_found False, selected_site None (never a
# zeroed-out site block), gates still reported so a narrative can explain
# the empty result.
_nd_none = build_narrative_data(
    [], BOUNDARY, road_proximity_source="unavailable",
    tree_zone_exclusion_available=False, water_zone_excluded=False, existing_canopy_excluded=True,
)
assert _nd_none["site_found"] is False and _nd_none["selected_site"] is None
assert _nd_none["candidate_count"] == 0
assert _nd_none["gates"]["road_proximity_source"] == "unavailable"
assert _nd_none["no_candidates"] is None, (
    "no tally was collected on this call, so there is NO reason to report -- None, never a "
    "fabricated explanation"
)
assert json.loads(json.dumps(_nd_none)) == _nd_none

# AND WITH A TALLY, THE ZERO SAYS WHY. A fully gated parcel is a real
# outcome and must not read as a broken generate.
_nd_gated = build_narrative_data(
    [], BOUNDARY, road_proximity_source="selected_road_corridor",
    tree_zone_exclusion_available=True, water_zone_excluded=True, existing_canopy_excluded=True,
    drainage_gates_checked=("outside_hydric_soil", "outside_floodplain"),
    rejection_tally={
        "sampled": 40, "not_measurable": 6, "cleared": 0,
        "gates": {"outside_hydric_soil": 30, "outside_floodplain": 12, "outside_existing_canopy": 4},
    },
)
assert _nd_gated["site_found"] is False and _nd_gated["selected_site"] is None
_reasons = _nd_gated["no_candidates"]
assert _reasons["reason"] == "every_pad_failed_a_gate"
assert _reasons["pads_sampled"] == 40 and _reasons["pads_measurable"] == 34
assert [entry["gate"] for entry in _reasons["blocking_gates"]] == [
    "outside_hydric_soil", "outside_floodplain", "outside_existing_canopy"
], "blocking gates must be ordered most-rejections-first -- the top one is the one to argue with"
assert _reasons["blocking_gates"][0]["pads_rejected"] == 30
assert _nd_gated["gates"]["hydric_gate_checked"] is True
assert _nd_gated["gates"]["floodplain_gate_checked"] is True
assert json.loads(json.dumps(_nd_gated)) == _nd_gated

# "nothing could hold a pad at all" is a DIFFERENT answer from "every pad
# failed a gate", and the reason says which.
_nd_unpaddable = build_narrative_data(
    [], BOUNDARY, road_proximity_source="unavailable",
    tree_zone_exclusion_available=True, water_zone_excluded=False, existing_canopy_excluded=True,
    rejection_tally={"sampled": 7, "not_measurable": 7, "cleared": 0, "gates": {}},
)
assert _nd_unpaddable["no_candidates"]["reason"] == "no_pad_was_measurable"
assert _nd_unpaddable["no_candidates"]["blocking_gates"] == []
print(
    "narrative_data: stored factor scores recompose the composite on every candidate; the selected "
    f"site narrates its location (position {_nd_site['location']['position_in_parcel']!r}, "
    f"{_nd_site['location']['production_zone_relationship']} production zone) and benefits "
    f"(south-facing, factors {_nd_site['benefits']['factors']}) consistently with its own stored "
    "values; unchecked prime farmland reads None; the no-candidate case reports site_found=False "
    "with selected_site=None."
)
print(
    f"  scales on the wire: solar_rating bands {solar_suitability.SOLAR_RATING_BANDS} (cuts owned "
    f"here, marked CONFIGURABLE) and elevation_position bands {ELEVATION_POSITION_BANDS} "
    "(production's own object, IMPORTED, asserted by identity). The selected site reads "
    f"{_nd_site['solar_rating']!r} at {_nd_site['solar_value']}/100 solar and sits on the "
    f"{_nd_site['location']['elevation_position']!r} at percentile "
    f"{_nd_site['location']['elevation_percentile_of_parcel']}."
)
print(
    "  ZERO CANDIDATES SAYS WHY: a fully gated parcel reports reason "
    f"{_reasons['reason']!r} over {_reasons['pads_measurable']} measurable pad(s) with blocking "
    f"gates {[ (e['gate'], e['pads_rejected']) for e in _reasons['blocking_gates'] ]} "
    "(most-rejections first); a parcel where no pad was measurable at all reports "
    f"{_nd_unpaddable['no_candidates']['reason']!r} instead. Neither reads as a broken generate."
)



# =====================================================================
# THE POINT, NOT THE PAD: the two distances, measured
# =====================================================================
# Branch test 1 and test 2. Both wrong answers came from ONE cause --
# shapely's .distance() returns 0.0 when geometries INTERSECT and when one
# CONTAINS the other -- so both are asserted against the OLD pad-based
# value as well as the new one. An assertion that only checked the fix
# would not show that there was anything to fix.

_road_union = unary_union(ROAD)
_production_union = unary_union([p["render_fill_polygon_utm"] for p in PRODUCTION_AREAS])


def _pad_for(x, y):
    """The clipped 0.1-acre pad a candidate at (x, y) is scored over --
    _measure_footprint()'s own construction, so the 'before' value is the
    number the shipped code actually produced."""
    half = FOOTPRINT_SIDE_M / 2
    return box(x - half, y - half, x + half, y + half).intersection(BOUNDARY)


# --- test 1: ROAD DISTANCE. A pad that intersects the road ------------
# The road runs along the parcel's south edge (y = 4500000) and the pad is
# 20.1 m per side, so a point 8 m north of the road has a pad that CROSSES
# it. That is not a contrived case: the road-proximity constraint tunes
# candidates to sit close to a road, which is exactly why the pad
# intersected one on essentially every candidate.
_ROAD_X, _ROAD_Y = 500150.0, 4500008.0
_road_pad = _pad_for(_ROAD_X, _ROAD_Y)
assert _road_pad.intersects(_road_union), "this fixture point must have a pad that crosses the road"
assert _road_pad.distance(_road_union) == 0.0, (
    "THE BEFORE: a pad intersecting the road measures 0.0 m from it, which is what shipped as "
    "'ft to road' -- if this ever stops being 0.0 the branch's premise has changed"
)

_road_site = measure_structure_site(
    _ROAD_X, _ROAD_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY
)
assert _road_site is not None
_expected_point_m = Point(_ROAD_X, _ROAD_Y).distance(_road_union)
assert _road_site["distance_to_road_m"] == round(_expected_point_m, 1), (
    f"distance_to_road_m must be the POINT's distance ({_expected_point_m:.1f} m), got "
    f"{_road_site['distance_to_road_m']}"
)
assert _road_site["distance_to_road_m"] > 0.0, (
    "THE AFTER: the same geometry now reports a real, non-zero distance from the site's own point"
)
print(
    f"[test 1] ROAD DISTANCE from the POINT: a pad that CROSSES the road measured "
    f"{_road_pad.distance(_road_union):.1f} m (0.0 -- the shipped value, on every such candidate); "
    f"the same site's point measures {_road_site['distance_to_road_m']} m "
    f"({_road_site['distance_to_road_m'] / 0.3048:.1f} ft), which is the honest answer to 'how far "
    "is this site from a road' and is now the panel's headline figure."
)

# The GATE moved with the distance, or a candidate could report 60 ft and
# still clear a 15 m buffer it never actually cleared.
_gated = measure_structure_site(
    _ROAD_X, _ROAD_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    road_proximity_buffer_meters=_expected_point_m / 2,
)
assert _gated["constraints"]["within_road_proximity_buffer"] is False, (
    "the road gate must be measured from the POINT too -- a pad-based gate would pass this site, "
    "whose pad touches the road, against a buffer half its point's real distance"
)
_ungated = measure_structure_site(
    _ROAD_X, _ROAD_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    road_proximity_buffer_meters=_expected_point_m * 2,
)
assert _ungated["constraints"]["within_road_proximity_buffer"] is True
print(
    f"  and the GATE moved with it: the same site fails a "
    f"{_expected_point_m / 2:.1f} m buffer and passes a {_expected_point_m * 2:.1f} m one -- one "
    "definition of 'how far is this site from a road', used by the gate and the figure alike."
)


# --- test 2: PRODUCTION. Inside a block vs at its edge ----------------
# The production block is box(500000, 4500000, 500150, 4500300). A point
# deep in its middle and a point right on its eastern edge are the two
# cases that used to be indistinguishable.
_DEEP_X, _DEEP_Y = 500070.0, 4500150.0     # ~70 m inside the block's east edge
_EDGE_X, _EDGE_Y = 500150.0, 4500150.0     # exactly ON the block's east edge
_OUT_X, _OUT_Y = 500220.0, 4500150.0       # ~70 m outside it

_deep = measure_structure_site(_DEEP_X, _DEEP_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY)
_edge = measure_structure_site(_EDGE_X, _EDGE_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY)
_out = measure_structure_site(_OUT_X, _OUT_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY)

# THE BEFORE, both halves of it: the pad-based reading called the edge
# site 0.0 (it intersects the boundary LINE) and gave the deep site a
# POSITIVE number indistinguishable from the outside site's.
assert _pad_for(_EDGE_X, _EDGE_Y).distance(_production_union.boundary) == 0.0
_deep_pad_m = _pad_for(_DEEP_X, _DEEP_Y).distance(_production_union.boundary)
_out_pad_m = _pad_for(_OUT_X, _OUT_Y).distance(_production_union.boundary)
assert _deep_pad_m > 0.0 and _out_pad_m > 0.0, (
    "THE BEFORE: a site buried inside a block and one outside it both measured a POSITIVE "
    "distance to the block's edge, with nothing in the number to tell them apart"
)

# THE AFTER: the SIGN tells them apart, and the relationship agrees with it.
assert _deep["production_zone_relationship"] == "inside"
assert _deep["signed_distance_to_production_m"] < 0.0, "inside a block is NEGATIVE"
assert _deep["distance_to_production_zone_m"] == abs(_deep["signed_distance_to_production_m"])
assert abs(_edge["signed_distance_to_production_m"]) < 1e-6, "on the edge is 0.0, neither sign"
assert _out["production_zone_relationship"] == "outside"
assert _out["signed_distance_to_production_m"] > 0.0, "outside every block is POSITIVE"
# The two cases the old reading confused are now different numbers.
assert _deep["signed_distance_to_production_m"] != _out["signed_distance_to_production_m"]
print(
    f"[test 2] PRODUCTION, inside vs edge vs outside, as the distance's SIGN: deep inside "
    f"{_deep['signed_distance_to_production_m']} m ({_deep['production_zone_relationship']}), on the "
    f"edge {_edge['signed_distance_to_production_m']} m ({_edge['production_zone_relationship']}), "
    f"outside {_out['signed_distance_to_production_m']} m ({_out['production_zone_relationship']}). "
    f"Measured off the PAD the deep site read +{_deep_pad_m:.1f} m and the outside site "
    f"+{_out_pad_m:.1f} m -- same sign, nothing to tell them apart -- and the edge site read 0.0, "
    "which is the same 0.0 an intersecting pad reports."
)
# A site with NO blocks at all reports None, never 0.0 and never a sign.
_no_blocks = measure_structure_site(_DEEP_X, _DEEP_Y, DEM, [], WATER_ZONES, ROAD, BOUNDARY)
assert _no_blocks["signed_distance_to_production_m"] is None
assert _no_blocks["distance_to_production_zone_m"] is None
assert _no_blocks["production_zone_relationship"] == "outside"
print("  with no blocks on the parcel both distances are None -- never a 0.0 that reads as 'on the edge'.")


# =====================================================================
# THE TWO DRAINAGE GATES (branch tests 4 and 6)
# =====================================================================
# Real gated geometry, not a stub: a hydric band across the parcel's
# middle and a floodplain band down its east side, both plain polygons of
# the kind road_corridors._fetch_floodplain_hydric_unions() returns.

HYDRIC_UNION = box(500000, 4500120, 500300, 4500180)      # an east-west wet band
FLOODPLAIN_UNION = box(500240, 4500000, 500300, 4500300)  # a north-south flood band
assert HYDRIC_UNION.intersects(FLOODPLAIN_UNION), (
    "the two fixture grounds must OVERLAP somewhere, so a site on BOTH is reachable -- that is the "
    "case a single combined union can never report"
)

_ungated_run = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY, max_candidates=10 ** 6
)
_gated_run = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    hydric_union_utm=HYDRIC_UNION,
    floodplain_union_utm=FLOODPLAIN_UNION,
    max_candidates=10 ** 6,
)
assert len(_gated_run) < len(_ungated_run), (
    "the two gates must actually exclude ground on this fixture, or the zero below would be "
    "vacuous"
)
# THE GATE, ASSERTED AGAINST THE GEOMETRY ITSELF: not one generated
# candidate's pad touches either ground.
for _c in _gated_run:
    assert not _c["polygon_utm"].intersects(HYDRIC_UNION), (
        f"a GENERATED candidate landed on hydric soil: {_c['polygon_utm'].centroid}"
    )
    assert not _c["polygon_utm"].intersects(FLOODPLAIN_UNION), (
        f"a GENERATED candidate landed in the floodplain: {_c['polygon_utm'].centroid}"
    )
# And the ground they used to sit on is real ground they were dropped from.
_dropped_hydric = [c for c in _ungated_run if c["polygon_utm"].intersects(HYDRIC_UNION)]
_dropped_flood = [c for c in _ungated_run if c["polygon_utm"].intersects(FLOODPLAIN_UNION)]
assert _dropped_hydric and _dropped_flood
print(
    f"[test 4] GENERATED CANDIDATES ARE HARD-GATED: {len(_ungated_run)} pads clear the stack "
    f"ungated, {len(_gated_run)} with the two drainage gates applied -- and NOT ONE of the "
    f"{len(_gated_run)} intersects either ground, asserted against the gate geometry itself. "
    f"Ungated, {len(_dropped_hydric)} sat on hydric soil and {len(_dropped_flood)} in the "
    "floodplain."
)

# EACH GATE IS INDEPENDENT: applied alone, it excludes its own ground and
# leaves the other's alone. A combined union cannot make this statement.
_hydric_only = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    hydric_union_utm=HYDRIC_UNION, max_candidates=10 ** 6,
)
_flood_only = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    floodplain_union_utm=FLOODPLAIN_UNION, max_candidates=10 ** 6,
)
assert not any(c["polygon_utm"].intersects(HYDRIC_UNION) for c in _hydric_only)
assert any(c["polygon_utm"].intersects(FLOODPLAIN_UNION) for c in _hydric_only), (
    "the hydric gate alone must NOT exclude floodplain ground -- they are two gates, not one"
)
assert not any(c["polygon_utm"].intersects(FLOODPLAIN_UNION) for c in _flood_only)
assert any(c["polygon_utm"].intersects(HYDRIC_UNION) for c in _flood_only)
print(
    f"  and the two are INDEPENDENT: hydric alone leaves {len(_hydric_only)} pads (none on hydric, "
    f"some still in floodplain), floodplain alone leaves {len(_flood_only)} (none in floodplain, "
    "some still on hydric). One combined union could not tell those two runs apart."
)

# AN UNAPPLIED GATE IS ABSENT, NOT TRIVIALLY TRUE -- the same convention
# the road source and the tree-zone polygon already use.
_no_gates = measure_structure_site(
    _DEEP_X, _DEEP_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY
)
assert "outside_hydric_soil" not in _no_gates["constraints"]
assert "outside_floodplain" not in _no_gates["constraints"]
_both_gates = measure_structure_site(
    _DEEP_X, _DEEP_Y, DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    hydric_union_utm=HYDRIC_UNION, floodplain_union_utm=FLOODPLAIN_UNION,
)
assert {"outside_hydric_soil", "outside_floodplain"} <= set(_both_gates["constraints"])
print(
    "  an unapplied gate has NO constraint entry at all (absent, never a trivially-satisfied "
    "True): a site is never reported clear of a check that did not run."
)

# --- test 6: a FULLY GATED parcel returns zero, and says why ----------
_wall_to_wall = box(*BOUNDARY.bounds)
_tally = {}
_none_left = find_candidate_solar_zones(
    DEM, PRODUCTION_AREAS, WATER_ZONES, ROAD, BOUNDARY,
    hydric_union_utm=_wall_to_wall,
    rejection_tally=_tally,
    max_candidates=10 ** 6,
)
assert _none_left == [], "a parcel entirely on hydric soil must generate NO candidate at all"
assert _tally["cleared"] == 0
assert _tally["sampled"] > 0 and _tally["gates"]["outside_hydric_soil"] > 0
_zero_narrative = build_narrative_data(
    _none_left, BOUNDARY, road_proximity_source="real_mapped_road",
    tree_zone_exclusion_available=True, water_zone_excluded=True, existing_canopy_excluded=True,
    drainage_gates_checked=("outside_hydric_soil",), rejection_tally=_tally,
)
assert _zero_narrative["site_found"] is False
assert _zero_narrative["candidate_count"] == 0
assert _zero_narrative["selected_site"] is None
assert _zero_narrative["no_candidates"]["reason"] == "every_pad_failed_a_gate"
assert _zero_narrative["no_candidates"]["blocking_gates"][0]["gate"] == "outside_hydric_soil"
assert _zero_narrative["gates"]["hydric_gate_checked"] is True
assert _zero_narrative["gates"]["floodplain_gate_checked"] is False, (
    "only the hydric gate ran here, and the floodplain answer must say 'not checked' rather than "
    "'clear'"
)
print(
    f"[test 6] A FULLY GATED PARCEL REPORTS THAT STATE: with hydric soil wall to wall, "
    f"{_tally['sampled']} pad(s) sampled, {_tally['cleared']} cleared, and the narrative says "
    f"reason={_zero_narrative['no_candidates']['reason']!r} with blocking gate "
    f"{_zero_narrative['no_candidates']['blocking_gates'][0]['gate']!r} rejecting "
    f"{_zero_narrative['no_candidates']['blocking_gates'][0]['pads_rejected']} of "
    f"{_zero_narrative['no_candidates']['pads_measurable']} measurable pad(s). Empty, and it says "
    "why -- not a blank result that reads as a broken generate."
)


# =====================================================================
# THE SOLAR RATING AND THE ELEVATION POSITION (branch tests 7 and 8)
# =====================================================================

# NOT A RANK: the same value gets the same word whatever it is ranked
# against, and every band boundary is lower-inclusive / upper-exclusive
# with the top band closing at 100.
for _word, (_low, _high) in SOLAR_RATING_BANDS.items():
    assert _solar_rating(_low) == _word, f"{_low} must read {_word!r} (lower-inclusive)"
    if _high < 100.0:
        assert _solar_rating(_high) != _word, f"{_high} must NOT read {_word!r} (upper-exclusive)"
assert _solar_rating(100.0) == "excellent", "the top band closes AT 100"
assert _solar_rating(None) is None, "no value is no word, never a default one"
_covered = sorted(SOLAR_RATING_BANDS.values(), key=lambda b: b[0])
assert _covered[0][0] == 0.0 and _covered[-1][1] == 100.0
for _a, _b in zip(_covered, _covered[1:]):
    assert _a[1] == _b[0], "the bands must be gapless and non-overlapping across [0, 100]"

# The value is the two SOLAR factors together, weighted by their own
# weights -- so retuning a weight retunes the rating with it.
for _c in candidates[:5]:
    _expected = round(
        100.0
        * (ASPECT_SCORE_WEIGHT * _c["aspect_score"] + SHADING_SCORE_WEIGHT * _c["shading_score"])
        / (ASPECT_SCORE_WEIGHT + SHADING_SCORE_WEIGHT),
        1,
    )
    assert abs(_c["solar_value"] - _expected) <= 0.1, (_c["solar_value"], _expected)
    assert _c["solar_rating"] == _solar_rating(_c["solar_value"])

# THE DISTRIBUTION, REPORTED -- the cuts were chosen against it, so it is
# printed rather than only asserted.
_solar_values = sorted(c["solar_value"] for c in candidates)
_by_word = {}
for _c in candidates:
    _by_word[_c["solar_rating"]] = _by_word.get(_c["solar_rating"], 0) + 1
print(
    f"[test 7] SOLAR RATING: {len(_solar_values)} pads on this uniform south-facing fixture run "
    f"{_solar_values[0]} to {_solar_values[-1]} (median {_solar_values[len(_solar_values) // 2]}) "
    f"-- a TIGHT CLUSTER, which is the distribution the uneven cuts were chosen against. Words: "
    f"{dict(sorted(_by_word.items()))}. Bands {SOLAR_RATING_BANDS}, CONFIGURABLE, on the wire in "
    "narrative_data['scales'] so the frontend holds no threshold."
)

# --- test 8: elevation_position is PRODUCTION'S bands, by import ------
for _c in candidates:
    assert _c["elevation_position"] == _elevation_position(_c["elevation_percentile_of_parcel"]), (
        "a candidate's position word must be production's own classifier applied to its own "
        "percentile -- not a second implementation"
    )
    if _c["elevation_percentile_of_parcel"] is not None:
        assert 0.0 <= _c["elevation_percentile_of_parcel"] <= 100.0
        _low, _high = ELEVATION_POSITION_BANDS[_c["elevation_position"]]
        assert _low <= _c["elevation_percentile_of_parcel"] <= _high
# ASSERTED AGAINST THE IMPORTED CONSTANT, NOT A COPY: this is the same
# object production declares, so a retune there retunes structures with it.
assert solar_suitability.ELEVATION_POSITION_BANDS is ELEVATION_POSITION_BANDS
assert solar_suitability._SCALES["elevation_position"]["bands"] is ELEVATION_POSITION_BANDS
_positions = {}
for _c in candidates:
    _positions[_c["elevation_position"]] = _positions.get(_c["elevation_position"], 0) + 1
print(
    f"[test 8] ELEVATION POSITION from PRODUCTION'S OWN BANDS, imported (identity-checked, not a "
    f"copy of the cuts): {dict(sorted(_positions.items(), key=lambda kv: ELEVATION_POSITION_BANDS[kv[0]][0]))} "
    f"across {len(candidates)} pads on a parcel falling {array.max() - array.min():.1f} m "
    "north to south."
)


print("\nAll solar_suitability checks passed.")
