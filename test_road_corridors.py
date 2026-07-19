"""
test_road_corridors.py

Offline (no-network) checks for road_corridors.py's constraint-stack,
corridor-generation, and ranking logic — hand-built synthetic DEMs and
hand-built production/pond zones (same shapes
find_candidate_road_corridors() actually consumes), not a real DEM/NHD/
SSURGO/road fetch. Mirrors test_water_candidate_zones.py's and
test_solar_suitability.py's "pure logic, independent of real data
fetches" approach.
"""

import numpy as np
from shapely.geometry import LineString, box

from feature_schema import validate_feature_collection
from road_corridors import (
    MAX_ROAD_GRADE_PCT,
    corridors_to_geojson,
    find_candidate_road_corridors,
)

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)


def _flat_hillside_dem(rows=40, cols=40, grade_per_row=0.3, origin_x=500000.0, origin_y=4500200.0):
    """Uniform south-facing slope: every column at a given row is the same
    elevation, so a whole row is a natural contour band -- ideal for
    exercising contour-band generation deterministically."""
    array = np.zeros((rows, cols), dtype=np.float32)
    for row in range(rows):
        array[row, :] = 100.0 - row * grade_per_row
    return {"array": array, "resolution_meters": RESOLUTION, "origin_x": origin_x, "origin_y": origin_y, "crs": CRS}


def _diagonal_ridge_dem(rows=30, cols=30, cross_slope=0.3, downhill_per_row=0.2, origin_x=500000.0, origin_y=4500150.0):
    """A gentle ridge crest running along the grid's diagonal, tapering
    off (and descending overall) away from it -- exercises ridge-top
    generation deterministically (see road_corridors.py's module
    docstring for why this is 'delineate_valleys on an inverted DEM')."""
    array = np.zeros((rows, cols), dtype=np.float32)
    for row in range(rows):
        for col in range(cols):
            distance_from_ridge = abs((rows - 1 - row) - col)
            array[row, col] = 100.0 - distance_from_ridge * cross_slope - row * downhill_per_row
    return {"array": array, "resolution_meters": RESOLUTION, "origin_x": origin_x, "origin_y": origin_y, "crs": CRS}


# --- contour-band generation + production-zone exclusion ---

dem = _flat_hillside_dem()
boundary = box(500000, 4500000, 500200, 4500200)
# a production zone splitting the hillside straight down the middle
production_areas = [
    {"id": 0, "representative_elevation_m": 100.0, "polygon_utm": box(500095, 4500000, 500115, 4500200)}
]

candidates = find_candidate_road_corridors(dem, production_areas, [], boundary, max_candidates=50)
assert candidates, "expected at least one contour-band candidate on a uniform hillside"
assert all(c["corridor_type"] == "contour" for c in candidates), (
    "a uniform hillside with no ridge feature should only produce contour-band candidates"
)

production_exclusion = box(500095, 4500000, 500115, 4500200).buffer(15)  # matches the module's own exclusion buffer
for candidate in candidates:
    assert not candidate["line_utm"].intersects(production_exclusion), (
        "no contour-band candidate should cross the (buffered) production zone"
    )
print(f"Contour-band generation produces {len(candidates)} candidate(s), all routing around the production zone.")


# --- ranking is score-driven, not hardcoded to "always rank left before right" ---

ranks = [c["rank"] for c in candidates]
scores = [c["suitability_score"] for c in candidates]
assert ranks == sorted(ranks)
assert scores == sorted(scores, reverse=True), "candidates must be ranked by suitability_score, descending"
print("Candidates are ranked strictly by suitability_score.")


# --- both corridor types are generated where terrain supports them, ranked together ---

rows = 30
combined_cols = 60
combined_array = np.zeros((rows, combined_cols), dtype=np.float32)
for row in range(rows):
    for col in range(combined_cols):
        if col < 30:
            combined_array[row, col] = 100.0 - row * 0.3  # west half: contour-friendly
        else:
            rc = col - 30
            distance_from_ridge = abs((rows - 1 - row) - rc)
            combined_array[row, col] = 130.0 - distance_from_ridge * 0.3 - row * 0.2  # east half: ridge

combined_dem = {
    "array": combined_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500150.0, "crs": CRS,
}
combined_boundary = box(500000, 4500000, 500300, 4500150)

combined_candidates = find_candidate_road_corridors(combined_dem, [], [], combined_boundary, max_candidates=50)
combined_types = {c["corridor_type"] for c in combined_candidates}
assert combined_types == {"contour", "ridge"}, (
    f"expected both corridor types on terrain supporting both, got only {combined_types}"
)
combined_scores = [c["suitability_score"] for c in combined_candidates]
assert combined_scores == sorted(combined_scores, reverse=True), (
    "contour and ridge candidates must be ranked together in one list, not two separate rankings"
)
print(f"Both corridor types generated on mixed terrain ({len(combined_candidates)} total), ranked together by score.")


# --- pond zone exclusion: a candidate must not cross a (buffered) pond/water zone ---

pond_zones = [{"valley_id": 0, "polygon_utm": box(500080, 4500000, 500130, 4500200)}]
pond_candidates = find_candidate_road_corridors(dem, [], pond_zones, boundary, max_candidates=50)
pond_exclusion = box(500080, 4500000, 500130, 4500200).buffer(25)  # matches the module's own pond buffer
for candidate in pond_candidates:
    assert not candidate["line_utm"].intersects(pond_exclusion), (
        "no candidate should cross the (buffered) pond/water-system zone"
    )
print("Pond/water-system zone exclusion correctly keeps candidates clear of the buffered zone.")


# --- anchoring: real road data gives a non-arbitrary connection point; no road data flags it ---

# a real road running along the west edge of the boundary
road_union = LineString([(500000, 4500000), (500000, 4500200)])
anchored_candidates = find_candidate_road_corridors(dem, [], [], boundary, road_union_utm=road_union, max_candidates=50)
assert anchored_candidates, "expected candidates for the anchoring check"
assert all(not c["connection_point_is_arbitrary"] for c in anchored_candidates), (
    "with real road data available, no candidate's connection point should be flagged as arbitrary"
)
print("With real road data available, connection points are anchored (not flagged arbitrary).")

no_road_candidates = find_candidate_road_corridors(dem, [], [], boundary, road_union_utm=None, max_candidates=50)
assert all(c["connection_point_is_arbitrary"] for c in no_road_candidates), (
    "with no road data available, every candidate's connection point should be flagged arbitrary"
)
print("With no road data available, every candidate's connection point is correctly flagged as arbitrary.")


# --- grade threshold is actually applied ---

strict_candidates = find_candidate_road_corridors(dem, [], [], boundary, max_grade_pct=0.0001)
assert strict_candidates == [], "an effectively-zero grade allowance should leave no qualifying candidates"
print(f"Grade threshold is enforced (default {MAX_ROAD_GRADE_PCT}%; near-zero allowance yields no candidates).")


# --- output: schema-valid FeatureCollection on the required layer, with required properties ---

geojson = corridors_to_geojson(candidates, floodplain_data_is_fallback=True, erosion_data_unavailable=True)
validate_feature_collection(geojson)
required_props = {"corridor_type", "avg_grade_pct", "length_ft", "constraints_satisfied"}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "suggested_road_corridor"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert feature["geometry"]["type"] == "LineString"
    notes = feature["properties"]["confidence_notes"].lower()
    assert "topographic suggestion" in notes and "not a surveyed" in notes
    assert "elevation fallback" in notes or "fallback" in notes, "floodplain fallback should be flagged in confidence_notes"
    assert "erosion-prone soil data" in notes, "erosion data unavailability should be flagged in confidence_notes"
print("corridors_to_geojson output is schema-valid, layer='suggested_road_corridor', with required properties and fallback caveats.")


# --- regression: corridor candidates stay on-parcel, not drawn from the DEM's buffered margin ---
#
# This is the exact live bug found against the real property: dem_data.py
# fetches a DEM buffered ~100m past the drawn boundary (correct and
# intentional, for terrain-analysis context), but contour-band/ridge-top
# generation never restricted its candidate cells to the actual parcel --
# a corridor could be built entirely from off-parcel ground. Here the DEM
# is a uniform south-facing slope spanning the FULL 250m x 250m grid (so,
# unclipped, a natural contour band would span the whole width, well past
# the parcel on both sides); the boundary below is a smaller 150m x 150m
# parcel with a 50m buffer margin on every side, mirroring dem_data.py's
# real buffer relationship at test scale.
buffered_rows = buffered_cols = 50
buffered_array = np.zeros((buffered_rows, buffered_cols), dtype=np.float32)
for row in range(buffered_rows):
    buffered_array[row, :] = 100.0 - row * 0.3  # uniform south-facing slope, same across every column
buffered_dem = {
    "array": buffered_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500250.0, "crs": CRS,
}
# The real parcel: 150m x 150m, with a 50m buffer margin to the DEM's edge
# on every side -- the DEM covers ground the parcel itself doesn't.
parcel_boundary = box(500050, 4500050, 500200, 4500200)

buffered_candidates = find_candidate_road_corridors(buffered_dem, [], [], parcel_boundary, max_candidates=50)
assert buffered_candidates, "expected at least one contour-band candidate on this uniform, buffered hillside"

for candidate in buffered_candidates:
    # parcel_boundary is convex (a box), so if every contributing cell is
    # on-parcel, even the anchored connector segment (a straight line from
    # an interior point to a point ON the boundary ring) can't leave and
    # re-enter -- the WHOLE anchored line, connector included, must stay
    # within the parcel. (For a concave real parcel, only the connector
    # segment itself would be exempt from this -- see this test file's
    # module docstring and road_corridors.py's own confidence_notes for
    # that already-disclosed limitation.)
    assert candidate["line_utm"].within(parcel_boundary.buffer(1e-6)), (
        f"{candidate['corridor_type']} candidate (rank {candidate['rank']}) extends outside the real parcel "
        f"boundary -- corridor geometry must be drawn from on-parcel cells only, not the DEM's buffered margin"
    )
print(
    f"Parcel clipping: {len(buffered_candidates)} corridor candidate(s) on a hillside spanning well past the "
    f"parcel boundary all stay entirely within the real (smaller) parcel, not the DEM's buffered extent."
)

print("\nAll road_corridors checks passed.")
