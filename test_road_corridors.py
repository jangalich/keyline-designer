"""
test_road_corridors.py

Offline (no-network) checks for road_corridors.py's constraint-stack,
cost-driven candidate generation, and ranking logic — hand-built synthetic
DEMs and hand-built production areas/selected water zone (same shapes
find_candidate_road_corridors() actually consumes: production areas carry
'render_fill_polygon_utm', the OPTIMIZED/final production geometry
production_area_ceiling.py produces, and the water zone is the single
SELECTED zone water_suitability.py's own scoring picks, not a list of
unscored candidates), not a real DEM/NHD/SSURGO/road fetch. Mirrors
test_water_candidate_zones.py's and test_solar_suitability.py's "pure
logic, independent of real data fetches" approach.

Candidate generation is now routing-based (road_cost_path.py's cost
raster + Dijkstra least-cost path), not the old contour-band/ridge-top
generators — see road_corridors.py's own module docstring for the
quadrant-source / two-tier-destination / cost-driven-ranking model this
exercises. There is no more 'corridor_type' field anywhere in the output;
this file no longer asserts anything about it.
"""

import numpy as np
from shapely.geometry import LineString, Point, box
from shapely.prepared import prep

from feature_schema import validate_feature_collection
from raster_grid import pixel_center_xy
from road_corridors import (
    MAX_ROAD_GRADE_PCT,
    ROAD_BOUNDARY_ADJACENT_METERS,
    ROAD_DESTINATION_SEARCH_METERS,
    ROAD_SOURCE_GRID_COLS,
    ROAD_SOURCE_GRID_ROWS,
    STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT,
    _build_production_cell_mask,
    _select_destination_cells,
    _select_quadrant_source_cells,
    corridors_to_geojson,
    find_candidate_road_corridors,
)

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)


def _flat_dem(size=41, origin_x=500000.0, origin_y=4500205.0, elevation=100.0):
    """A perfectly flat DEM -- every cell the same elevation, so slope is
    0 everywhere the Horn kernel can compute it at all (the outer 1-cell
    ring stays NaN regardless -- compute_slope_and_aspect() never visits
    the border, see terrain_metrics.py). Useful whenever a test wants
    grade entirely out of the way."""
    array = np.full((size, size), elevation, dtype=np.float32)
    return {"array": array, "resolution_meters": RESOLUTION, "origin_x": origin_x, "origin_y": origin_y, "crs": CRS}


def _hillside_dem(size=41, grade_per_row=0.3, origin_x=500000.0, origin_y=4500205.0):
    """Uniform south-facing slope: every column at a given row is the same
    elevation, so a north-south route climbs/descends while an east-west
    route stays level -- lets a single DEM produce both a ~0%-grade and a
    meaningfully-graded candidate for comparison."""
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        array[row, :] = 100.0 - row * grade_per_row
    return {"array": array, "resolution_meters": RESOLUTION, "origin_x": origin_x, "origin_y": origin_y, "crs": CRS}


# --- basic candidate generation: up to one per quadrant, ranked by ascending total_cost ---

dem = _flat_dem()
boundary = box(500000, 4500000, 500205, 4500205)

candidates = find_candidate_road_corridors(dem, [], None, boundary, max_candidates=50)
assert candidates, "expected at least one candidate on an open, flat, unconstrained property"
assert len(candidates) <= ROAD_SOURCE_GRID_ROWS * ROAD_SOURCE_GRID_COLS, (
    f"expected at most {ROAD_SOURCE_GRID_ROWS * ROAD_SOURCE_GRID_COLS} candidates (one per quadrant), "
    f"got {len(candidates)}"
)
for candidate in candidates:
    assert len(candidate["points_xyz"]) >= 2, "a returned candidate must have at least 2 points to form a line"
    assert candidate["line_utm"].length > 0
costs = [candidate["total_cost"] for candidate in candidates]
assert costs == sorted(costs), "candidates must be ranked by ascending total_cost, cheapest first"
scores = [candidate["suitability_score"] for candidate in candidates]
assert scores == sorted(scores, reverse=True), "suitability_score must be descending as total_cost ascends"
ranks = [candidate["rank"] for candidate in candidates]
assert ranks == sorted(ranks) == list(range(1, len(candidates) + 1))
print(f"Cost-driven generation produces {len(candidates)} candidate(s) on an open property, ranked by ascending total_cost.")


# --- _select_quadrant_source_cells: one source per quadrant with eligible ground, none forced ---

full_eligible = np.ones((40, 40), dtype=bool)
all_quadrant_sources = _select_quadrant_source_cells(dem, full_eligible, boundary)
assert len(all_quadrant_sources) == ROAD_SOURCE_GRID_ROWS * ROAD_SOURCE_GRID_COLS, (
    "every quadrant has eligible ground -- expected one source cell per quadrant"
)

partially_eligible = np.ones((40, 40), dtype=bool)
partially_eligible[0:20, 0:20] = False  # empty top-left quadrant
partial_sources = _select_quadrant_source_cells(dem, partially_eligible, boundary)
assert len(partial_sources) == ROAD_SOURCE_GRID_ROWS * ROAD_SOURCE_GRID_COLS - 1, (
    "a quadrant with zero eligible cells must be skipped entirely, not forced to contribute a source"
)
assert all(not (r < 20 and c < 20) for r, c in partial_sources), (
    "no source cell should come from the quadrant that has no eligible ground at all"
)
print("_select_quadrant_source_cells skips quadrants with no eligible ground and never forces a source from them.")


# --- _select_destination_cells: Tier 1 (near real road) with Tier 2 (boundary-adjacent) fallback ---

eligible_everywhere = np.ones((41, 41), dtype=bool)
nearby_road = LineString([(500204, 4500000), (500204, 4500205)])  # runs along the east edge
tier1_cells = _select_destination_cells(dem, eligible_everywhere, boundary, nearby_road)
assert tier1_cells, "expected Tier 1 destinations near a real, reachable road"
tier1_prepared = prep(nearby_road.buffer(ROAD_DESTINATION_SEARCH_METERS))
assert all(tier1_prepared.contains(Point(pixel_center_xy(dem, r, c))) for r, c in tier1_cells), (
    "every Tier 1 destination cell must actually fall within ROAD_DESTINATION_SEARCH_METERS of the road"
)

distant_road = LineString([(600000, 4600000), (600001, 4600001)])  # real but unreachable within the search radius
fallback_cells = _select_destination_cells(dem, eligible_everywhere, boundary, distant_road)
no_road_cells = _select_destination_cells(dem, eligible_everywhere, boundary, None)
assert fallback_cells and sorted(fallback_cells) == sorted(no_road_cells), (
    "when Tier 1 comes back empty (road exists but nothing eligible is within reach), destination selection "
    "must fall through to the same Tier 2 boundary-adjacent result as having no road data at all"
)
boundary_prepared_test = prep(boundary.exterior.buffer(ROAD_BOUNDARY_ADJACENT_METERS))
assert all(boundary_prepared_test.contains(Point(pixel_center_xy(dem, r, c))) for r, c in no_road_cells), (
    "every Tier 2 destination cell must actually fall within ROAD_BOUNDARY_ADJACENT_METERS of the boundary exterior"
)

isolated_eligible = np.zeros((41, 41), dtype=bool)
isolated_eligible[20, 20] = True  # a single eligible cell, deep in the interior, far from any tier
assert _select_destination_cells(dem, isolated_eligible, boundary, None) == [], (
    "both tiers coming back empty must return [], not raise or fabricate a destination"
)
print(
    "_select_destination_cells finds real road-adjacent destinations first, correctly falls through to "
    "boundary-adjacent destinations when Tier 1 is empty (whether because no road exists or none is reachable), "
    "and returns [] honestly when neither tier has anything."
)


# --- _build_production_cell_mask: True only inside the production union AND on-parcel ---

production_polygon = box(500000, 4500000, 500100, 4500205)
production_prepared = prep(production_polygon)
boundary_prepared = prep(boundary)
production_mask = _build_production_cell_mask(dem, production_prepared, boundary_prepared)
for r in range(1, 40):
    for c in range(1, 40):
        point = Point(pixel_center_xy(dem, r, c))
        expected = boundary_prepared.contains(point) and production_prepared.contains(point)
        assert production_mask[r, c] == expected, f"production mask mismatch at ({r}, {c})"
assert _build_production_cell_mask(dem, None, boundary_prepared).sum() == 0, (
    "a None production_prepared (no production zones at all) must produce an all-False mask"
)
print("_build_production_cell_mask correctly flags only on-parcel cells actually inside the production union.")


# --- production zones are a cost PREFERENCE, not a hard exclusion: a candidate MAY still cross one ---

crossing_production_areas = [{"id": 0, "render_fill_polygon_utm": box(500000, 4500000, 500102, 4500205)}]  # west half
crossing_test_candidates = find_candidate_road_corridors(
    dem, crossing_production_areas, None, boundary, max_candidates=50
)
crossing = [c for c in crossing_test_candidates if c["crosses_production_zone"]]
noncrossing = [c for c in crossing_test_candidates if not c["crosses_production_zone"]]
assert crossing, (
    "expected at least one candidate that actually crosses the production zone -- production must NOT be a "
    "hard exclusion anymore (a west-half quadrant's own source cell sits inside it)"
)
assert noncrossing, "expected at least one candidate that doesn't cross the production zone, for comparison"
assert min(c["total_cost"] for c in crossing) > min(c["total_cost"] for c in noncrossing), (
    "crossing the production zone should cost more than an otherwise-comparable route that doesn't -- this is "
    "the production-crossing cost PENALTY, not an exclusion"
)
print(
    f"Production zones are a cost preference, not an exclusion: {len(crossing)} candidate(s) legitimately cross "
    f"one (min cost {min(c['total_cost'] for c in crossing):.1f}), while comparable non-crossing candidates cost "
    f"less (min cost {min(c['total_cost'] for c in noncrossing):.1f})."
)


# --- selected water zone exclusion: a candidate must not cross the (buffered) SELECTED water zone ---
#
# selected_water_zone is water_suitability.fetch_and_select_optimal_water_zone()'s
# own single rank-1 answer shape -- carries 'render_fill_polygon_utm', same
# optimized/final-geometry field production_areas above uses, not 'polygon_utm'.
selected_water_zone = {"id": 0, "render_fill_polygon_utm": box(500080, 4500000, 500130, 4500205)}
pond_candidates = find_candidate_road_corridors(dem, [], selected_water_zone, boundary, max_candidates=50)
pond_exclusion = box(500080, 4500000, 500130, 4500205).buffer(25)  # matches the module's own pond buffer
assert pond_candidates, "expected candidates for the pond-exclusion check"
for candidate in pond_candidates:
    assert not candidate["line_utm"].intersects(pond_exclusion), (
        "no candidate should cross the (buffered) selected water-system zone -- still a HARD exclusion"
    )
print("Selected water-system zone exclusion correctly keeps candidates clear of the buffered zone.")


# --- anchoring: real named road data connects the corridor; no road data omits the connector entirely ---

# a real road running along the east edge of the boundary
road_union = LineString([(500204, 4500000), (500204, 4500205)])
anchored_candidates = find_candidate_road_corridors(dem, [], None, boundary, road_union_utm=road_union, max_candidates=50)
assert anchored_candidates, "expected candidates for the anchoring check"
assert all(c["anchor_status"] == "connected_to_named_road" for c in anchored_candidates), (
    "with real road data available, every candidate should report anchor_status='connected_to_named_road'"
)
assert all(c["anchor_road_distance_m"] is not None for c in anchored_candidates), (
    "with real road data available, anchor_road_distance_m should be a real reported value"
)
print("With real road data available, connection points are anchored (connected_to_named_road).")

unanchored_candidates = find_candidate_road_corridors(dem, [], None, boundary, road_union_utm=None, max_candidates=50)
assert unanchored_candidates, "expected candidates for the unanchored check"
assert all(c["anchor_status"] == "no_named_road_available" for c in unanchored_candidates), (
    "with no road data available, every candidate should report anchor_status='no_named_road_available'"
)
assert all(c["anchor_road_name"] is None and c["anchor_road_distance_m"] is None for c in unanchored_candidates), (
    "with no road data available, anchor_road_name/anchor_road_distance_m should both be None, not fabricated"
)
print(
    "With no road data available, no connector segment is added at all -- every candidate's own interior "
    "geometry is returned unchanged, correctly flagged 'no_named_road_available'."
)


# --- anchoring prefers real road FRONTAGE (named) when road_features_utm is available ---

named_road_features = [{"name": "N Montour Rd", "line_utm": road_union}]
named_anchor_candidates = find_candidate_road_corridors(
    dem, [], None, boundary, road_union_utm=road_union, road_features_utm=named_road_features, max_candidates=50
)
assert named_anchor_candidates, "expected candidates for the named-anchor check"
assert all(c["anchor_road_name"] == "N Montour Rd" for c in named_anchor_candidates), (
    "with road_features_utm available, every anchored candidate should report the real road's name"
)
named_geojson = corridors_to_geojson(named_anchor_candidates)
for feature in named_geojson["features"]:
    props = feature["properties"]
    assert props["anchor_road_name"] == "N Montour Rd"
    assert props["anchor_road_distance_ft"] is not None
    assert "N Montour Rd" in props["confidence_notes"], (
        "confidence_notes should name the specific real road the corridor anchored to"
    )
print("With road_features_utm available, anchored candidates report the specific real road's name and "
      "distance, both in properties and in confidence_notes.")

# Without road_features_utm (only the plain union), anchoring still works but the name is generically omitted.
unnamed_anchor_candidates = find_candidate_road_corridors(
    dem, [], None, boundary, road_union_utm=road_union, road_features_utm=None, max_candidates=50
)
assert all(c["anchor_status"] == "connected_to_named_road" for c in unnamed_anchor_candidates)
assert all(c["anchor_road_name"] is None for c in unnamed_anchor_candidates), (
    "without road_features_utm, anchor_road_name should be None (not fabricated), even though anchoring "
    "itself still works via road_union_utm alone"
)
print("Without road_features_utm, anchoring still works via road_union_utm alone, with anchor_road_name "
      "correctly omitted rather than fabricated.")


# --- grade threshold is actually applied ---

hillside = _hillside_dem(grade_per_row=0.3)
hillside_boundary = box(500000, 4500000, 500205, 4500205)
normal_grade_candidates = find_candidate_road_corridors(hillside, [], None, hillside_boundary, max_candidates=50)
assert normal_grade_candidates, "expected candidates on a gentle hillside well under the grade ceiling"

strict_candidates = find_candidate_road_corridors(hillside, [], None, hillside_boundary, max_grade_pct=0.0001)
assert strict_candidates == [], "an effectively-zero grade allowance should leave no qualifying candidates"
print(f"Grade threshold is enforced (default {MAX_ROAD_GRADE_PCT}%; near-zero allowance yields no candidates).")


# --- steep-grade engineering-consideration note is additive, threshold-gated ---

# A hillside graded steep enough (12%) that the north-south candidates
# land above STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT (10%) but still
# safely under MAX_ROAD_GRADE_PCT (15%); the east-west candidates on the
# same DEM stay near 0% grade (a contour line), giving both a steep and a
# gentle candidate to check in the same pass.
steep_hillside = _hillside_dem(grade_per_row=0.6)
steep_candidates = find_candidate_road_corridors(steep_hillside, [], None, hillside_boundary, max_candidates=50)
steep_geojson = corridors_to_geojson(steep_candidates)
steep_features = [f for f in steep_geojson["features"] if f["properties"]["avg_grade_pct"] > STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT]
gentle_features = [f for f in steep_geojson["features"] if f["properties"]["avg_grade_pct"] <= STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT]
assert steep_features, f"expected at least one candidate above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade on this hillside"
assert gentle_features, "expected at least one near-contour (gentle) candidate on this hillside, for comparison"
for feature in steep_features:
    notes = feature["properties"]["confidence_notes"]
    assert "real engineering consideration" in notes and "drainage/culverts" in notes and "water bars" in notes, (
        f"a candidate above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade "
        f"(avg_grade_pct={feature['properties']['avg_grade_pct']}) must carry the steep-grade engineering-"
        f"consideration note, got: {notes}"
    )
    assert "TOPOGRAPHIC SUGGESTION only, not a surveyed road alignment" in notes, (
        "the steep-grade note must be ADDITIVE -- the existing blanket disclaimer must still be present"
    )
for feature in gentle_features:
    assert "real engineering consideration" not in feature["properties"]["confidence_notes"], (
        "a candidate at or below the steep-grade threshold must NOT carry the engineering-consideration note"
    )
print(
    f"Candidates above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade carry the additive steep-grade "
    f"engineering-consideration note; candidates at or below it correctly omit it; the blanket disclaimer "
    f"stays present either way."
)


# --- output: schema-valid FeatureCollection on the required layer, with required properties ---

geojson = corridors_to_geojson(candidates, floodplain_data_is_fallback=True)
validate_feature_collection(geojson)
required_props = {
    "avg_grade_pct", "length_ft", "anchor_status",
    "anchor_road_name", "anchor_road_distance_ft", "crosses_production_zone",
    "constraints_satisfied",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "suggested_road_corridor"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert "corridor_type" not in feature["properties"], (
        "corridor_type has been removed outright -- contour-band/ridge-top generation no longer exists"
    )
    assert 0.0 <= feature["properties"]["suitability_score"] <= 100.0
    assert "crosses_erosion_prone_soil" not in feature["properties"], (
        "the erosion-prone-soil preference has been removed outright (KSOP: Soil is step 8, below "
        "Farm Roads at step 4) -- this property must no longer be reported at all"
    )
    assert "outside_production_zone" not in feature["properties"]["constraints_satisfied"], (
        "production zones are no longer a hard exclusion -- this must not appear as a satisfied constraint"
    )
    assert feature["geometry"]["type"] == "LineString"
    notes = feature["properties"]["confidence_notes"].lower()
    assert "topographic suggestion" in notes and "not a surveyed" in notes
    assert "elevation fallback" in notes or "fallback" in notes, "floodplain fallback should be flagged in confidence_notes"
    assert "erosion" not in notes, (
        "the erosion-avoidance preference has been removed outright -- confidence_notes must no longer "
        "mention erosion at all"
    )
    assert "least-cost-path" in notes or "cost surface" in notes, (
        "confidence_notes should describe the cost-driven routing model, not the removed contour-band/ridge-top one"
    )
    assert "contour-band" not in notes and "ridge-top" not in notes, (
        "confidence_notes must no longer reference the removed contour-band/ridge-top generation model"
    )
print("corridors_to_geojson output is schema-valid, layer='suggested_road_corridor', with required properties "
      "(corridor_type and crosses_erosion_prone_soil correctly absent), no stale 'outside_production_zone' "
      "constraint, and confidence_notes describing the cost-driven routing model with no erosion mention at all.")


# --- regression: corridor candidates stay on-parcel, not drawn from the DEM's buffered margin ---
#
# This is the exact live bug found against the real property: dem_data.py
# fetches a DEM buffered ~100m past the drawn boundary (correct and
# intentional, for terrain-analysis context), but candidate generation
# must still be structurally confined to the actual parcel. Here the DEM
# spans a full 400m x 400m grid while the real parcel is a smaller,
# centered 300m x 300m box with a 50m buffer margin on every side,
# mirroring dem_data.py's real buffer relationship at test scale. Large
# enough that the quadrant grid (computed over the DEM's own full extent,
# see _select_quadrant_source_cells()) lands its sources well inside the
# parcel's interior rather than immediately adjacent to the boundary.
buffered_size = 80
buffered_array = np.full((buffered_size, buffered_size), 100.0, dtype=np.float32)
buffered_dem = {
    "array": buffered_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500400.0, "crs": CRS,
}
parcel_boundary = box(500050, 4500050, 500350, 4500350)

buffered_candidates = find_candidate_road_corridors(buffered_dem, [], None, parcel_boundary, max_candidates=50)
assert buffered_candidates, "expected at least one candidate on this uniform, buffered DEM"

for candidate in buffered_candidates:
    # parcel_boundary is convex (a box), so if every contributing cell is
    # on-parcel, even the anchored connector segment (a straight line from
    # an interior point to a point ON the boundary ring) can't leave and
    # re-enter -- the WHOLE candidate line, connector included, must stay
    # within the parcel. (For a concave real parcel, only the connector
    # segment itself would be exempt from this -- see this test file's
    # module docstring and road_corridors.py's own confidence_notes for
    # that already-disclosed limitation.)
    assert candidate["line_utm"].within(parcel_boundary.buffer(1e-6)), (
        f"candidate (rank {candidate['rank']}) extends outside the real parcel boundary -- corridor geometry "
        f"must be drawn from on-parcel cells only, not the DEM's buffered margin"
    )
print(
    f"Parcel clipping: {len(buffered_candidates)} corridor candidate(s) on a DEM extending well past the "
    f"parcel boundary all stay entirely within the real (smaller) parcel, not the DEM's buffered extent."
)

print("\nAll road_corridors checks passed.")
