"""
test_road_corridors.py

Offline (no-network) checks for road_corridors.py's constraint-stack,
anchor-to-farthest-point routing, and ranking logic — hand-built synthetic
DEMs and hand-built production areas/selected water zone (same shapes
find_road_routes() actually consumes: production areas carry
'render_fill_polygon_utm', the OPTIMIZED/final production geometry
production_area_ceiling.py produces, and the water zone is the single
SELECTED zone water_suitability.py's own scoring picks, not a list of
unscored candidates), not a real DEM/NHD/SSURGO fetch. Mirrors
test_water_candidate_zones.py's and test_solar_suitability.py's "pure
logic, independent of real data fetches" approach.

Route generation is now anchor-driven (road_cost_path.py's cost raster +
k_shortest_paths(), routed from a single given anchor point to the
farthest eligible point on the property) — see road_corridors.py's own
module docstring for the current constraint-stack split (water + production
HARD-excluded, floodplain + grade now SOFT cost penalties). There is no
more 'corridor_type', 'anchor_status', 'anchor_road_name', or
'crosses_production_zone' field anywhere in the output; this file no
longer asserts anything about any of them (except asserting their
absence).
"""

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, box
from shapely.prepared import prep

from feature_schema import validate_feature_collection
from raster_grid import pixel_center_xy
from road_corridors import (
    STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT,
    _build_production_cell_mask,
    _find_farthest_eligible_cell,
    _snap_anchor_to_eligible_cell,
    corridors_to_geojson,
    find_road_routes,
    identify_road_corridor_candidates,
)
from road_cost_path import build_cost_raster

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
    route stays level."""
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        array[row, :] = 100.0 - row * grade_per_row
    return {"array": array, "resolution_meters": RESOLUTION, "origin_x": origin_x, "origin_y": origin_y, "crs": CRS}


def _lon_lat_for_cell(dem: dict, row: int, col: int) -> tuple[float, float]:
    x, y = pixel_center_xy(dem, row, col)
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", [x], [y])
    return (lons[0], lats[0])


dem = _flat_dem()
boundary = box(500000, 4500000, 500205, 4500205)
sw_corner_anchor = _lon_lat_for_cell(dem, 38, 2)  # near the SW corner, well inside the boundary


# --- basic route generation: non-empty, ranked by ascending total_cost, degenerate single-route scoring ---

routes = find_road_routes(dem, [], None, boundary, sw_corner_anchor)
assert routes, "expected at least one route on an open, flat, unconstrained property"
for route in routes:
    assert len(route["points_xyz"]) >= 2, "a returned route must have at least 2 points to form a line"
    assert route["line_utm"].length > 0
costs = [route["total_cost"] for route in routes]
assert costs == sorted(costs), "routes must be ranked by ascending total_cost, cheapest first"
ranks = [route["rank"] for route in routes]
assert ranks == sorted(ranks) == list(range(1, len(routes) + 1))
if len(routes) == 1:
    assert routes[0]["suitability_score"] == 100.0, (
        "the degenerate single-route case must score 100.0 (best/only option), not 0.0 from dividing "
        "total_cost by itself -- see find_road_routes()'s own comment on this fix"
    )
else:
    scores = [route["suitability_score"] for route in routes]
    assert scores == sorted(scores, reverse=True), "suitability_score must be descending as total_cost ascends"
print(
    f"Anchor-driven generation produces {len(routes)} route(s) on an open property, ranked by ascending "
    f"total_cost, with the degenerate single-route scoring case handled correctly."
)


# --- _snap_anchor_to_eligible_cell: snaps to the nearest FINITE-cost cell, not the raw anchor cell ---

slope_pct = np.zeros((41, 41), dtype=np.float32)
excluded_mask = np.zeros((41, 41), dtype=bool)
excluded_mask[38, 2] = True  # exclude the exact cell the anchor above points at
cost_raster_with_hole = build_cost_raster(dem, slope_pct, excluded_mask)
snapped = _snap_anchor_to_eligible_cell(dem, cost_raster_with_hole, sw_corner_anchor)
assert snapped is not None and snapped != (38, 2), (
    "the anchor's own raw cell is hard-excluded -- snapping must land on a DIFFERENT, actually-eligible cell"
)
assert np.isfinite(cost_raster_with_hole[snapped]), "the snapped cell itself must be finite-cost"

fully_excluded = np.ones((41, 41), dtype=bool)
cost_raster_none_eligible = build_cost_raster(dem, slope_pct, fully_excluded)
assert _snap_anchor_to_eligible_cell(dem, cost_raster_none_eligible, sw_corner_anchor) is None, (
    "with literally no eligible cell anywhere, snapping must return None, not raise or fabricate a cell"
)
print("_snap_anchor_to_eligible_cell correctly snaps past a hard-excluded anchor cell to real eligible ground, "
      "and returns None honestly when nothing is eligible at all.")


# --- _find_farthest_eligible_cell: maximum cell-grid distance from the source ---

eligible = np.zeros((41, 41), dtype=bool)
eligible[5, 5] = True
eligible[5, 6] = True  # a closer eligible cell
eligible[35, 38] = True  # the genuinely farthest eligible cell from (5, 5)
farthest = _find_farthest_eligible_cell(dem, np.where(eligible, 1.0, np.inf), (5, 5), boundary)
assert farthest == (35, 38), f"expected the genuinely farthest eligible cell (35, 38), got {farthest}"
print("_find_farthest_eligible_cell correctly picks the maximum cell-grid-distance eligible cell, not just "
      "any eligible cell.")


# --- _build_production_cell_mask: True only inside the given union AND on-parcel (unchanged logic) ---

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
    "a None prepared geometry (no production zones/floodplain at all) must produce an all-False mask"
)
print("_build_production_cell_mask correctly flags only on-parcel cells actually inside the given union.")


# --- production zones are now a HARD exclusion: no route may use a cell inside one ---

production_areas = [{"id": 0, "render_fill_polygon_utm": box(500080, 4500080, 500125, 4500125)}]
production_prepared2 = prep(production_areas[0]["render_fill_polygon_utm"])
excluded_production_mask = _build_production_cell_mask(dem, production_prepared2, boundary_prepared)

production_routes = find_road_routes(dem, production_areas, None, boundary, sw_corner_anchor)
assert production_routes, "expected at least one route around the production zone"
for route in production_routes:
    for x, y, _z in route["points_xyz"]:
        col = round((x - dem["origin_x"]) / RESOLUTION[0] - 0.5)
        row = round((dem["origin_y"] - y) / RESOLUTION[1] - 0.5)
        assert not excluded_production_mask[row, col], (
            f"route cell ({row}, {col}) falls inside the production zone -- production must be a HARD "
            f"exclusion now, no route cell may ever be inside it"
        )
print("Production zones are a HARD exclusion: no generated route ever actually occupies a production cell.")


# --- a production zone that fully blocks every route leaves NO route at all (confirms it's genuinely hard) ---

blocking_wall = [{"id": 0, "render_fill_polygon_utm": box(500090, 4500000, 500115, 4500205)}]  # full N-S span
blocked_routes = find_road_routes(dem, blocking_wall, None, boundary, sw_corner_anchor)
assert blocked_routes == [], (
    "a production zone spanning the full property height with no gap should leave NO legal route at all -- "
    "if any route were still found crossing it, production wouldn't really be hard-excluded"
)
print("A production zone with no gap at all correctly blocks every route -- confirms the exclusion is genuinely hard.")


# --- selected water zone is still a HARD exclusion: a route must not cross the (buffered) selected zone ---
#
# selected_water_zone is water_suitability.fetch_and_select_optimal_water_zone()'s
# own single rank-1 answer shape -- carries 'render_fill_polygon_utm', same
# optimized/final-geometry field production_areas above uses, not 'polygon_utm'.
selected_water_zone = {"id": 0, "render_fill_polygon_utm": box(500080, 4500080, 500085, 4500125)}
pond_routes = find_road_routes(dem, [], selected_water_zone, boundary, sw_corner_anchor)
pond_exclusion = box(500080, 4500080, 500085, 4500125).buffer(25)  # matches the module's own pond buffer
assert pond_routes, "expected routes for the pond-exclusion check"
for route in pond_routes:
    assert not route["line_utm"].intersects(pond_exclusion), (
        "no route should cross the (buffered) selected water-system zone -- still a HARD exclusion"
    )
print("Selected water-system zone exclusion correctly keeps routes clear of the buffered zone.")


# --- floodplain is now a SOFT cost penalty: a route MAY still cross it, and isn't blocked outright ---

# A floodplain band spanning the FULL property height -- if this were still
# a hard exclusion (as in an earlier version of this module), NO route
# could exist at all, the same way the full-height production wall above
# left zero routes. Since floodplain is soft now, a route must still be
# found, crossing straight through it.
full_span_floodplain = box(500090, 4500000, 500115, 4500205)
floodplain_routes = find_road_routes(
    dem, [], None, boundary, sw_corner_anchor, hydric_floodplain_union=full_span_floodplain
)
assert floodplain_routes, (
    "a full-height floodplain band must NOT block routing entirely -- floodplain is a SOFT cost penalty now, "
    "not a hard exclusion (contrast with the production-wall test above, which correctly found zero routes)"
)
assert any(r["crosses_floodplain"] for r in floodplain_routes), (
    "expected at least one route to actually cross the floodplain band, proving it's traversable"
)
open_routes = find_road_routes(dem, [], None, boundary, sw_corner_anchor)
assert min(r["total_cost"] for r in floodplain_routes) > min(r["total_cost"] for r in open_routes), (
    "crossing the floodplain penalty region should cost more than the same route on otherwise-open ground"
)
print(
    "Floodplain/hydric ground is a SOFT cost penalty, not a hard exclusion: a route can still cross a "
    "full-height floodplain band (correctly flagged crosses_floodplain=True), at a real cost premium over "
    "equivalent open ground."
)


# --- grade has NO hard ceiling anymore: even a very steep hillside still produces a route ---

steep_hillside = _hillside_dem(size=41, grade_per_row=5.0)  # ~100% grade north-south
steep_boundary = box(500000, 4500000, 500205, 4500205)
steep_anchor = _lon_lat_for_cell(steep_hillside, 38, 20)
steep_routes = find_road_routes(steep_hillside, [], None, steep_boundary, steep_anchor)
assert steep_routes, (
    "grade has no hard ceiling under the current design -- even a very steep hillside must still produce a "
    "route (just an expensive one), unlike an earlier version of this module that hard-capped at "
    "MAX_ROAD_GRADE_PCT and would have returned []"
)
print("Grade is no longer hard-capped: a very steep hillside still produces a route rather than zero candidates.")


# --- steep-grade engineering-consideration note is additive, threshold-gated ---

hillside = _hillside_dem(grade_per_row=0.6)
hillside_boundary = box(500000, 4500000, 500205, 4500205)
hillside_anchor_steep_end = _lon_lat_for_cell(hillside, 2, 20)  # near the top -- routes south, climbing/descending
hillside_routes = find_road_routes(hillside, [], None, hillside_boundary, hillside_anchor_steep_end)
hillside_geojson = corridors_to_geojson(hillside_routes)
steep_features = [f for f in hillside_geojson["features"] if f["properties"]["avg_grade_pct"] > STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT]
assert steep_features, f"expected at least one route above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade on this hillside"
for feature in steep_features:
    notes = feature["properties"]["confidence_notes"]
    assert "real engineering consideration" in notes and "drainage/culverts" in notes and "water bars" in notes, (
        f"a route above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade "
        f"(avg_grade_pct={feature['properties']['avg_grade_pct']}) must carry the steep-grade engineering-"
        f"consideration note, got: {notes}"
    )
    assert "TOPOGRAPHIC SUGGESTION only, not a surveyed road alignment" in notes, (
        "the steep-grade note must be ADDITIVE -- the existing blanket disclaimer must still be present"
    )

gentle_flat_routes = find_road_routes(dem, [], None, boundary, sw_corner_anchor)
gentle_geojson = corridors_to_geojson(gentle_flat_routes)
for feature in gentle_geojson["features"]:
    assert feature["properties"]["avg_grade_pct"] <= STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT
    assert "real engineering consideration" not in feature["properties"]["confidence_notes"], (
        "a route at or below the steep-grade threshold must NOT carry the engineering-consideration note"
    )
print(
    f"Routes above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade carry the additive steep-grade "
    f"engineering-consideration note; routes at or below it correctly omit it; the blanket disclaimer "
    f"stays present either way."
)


# --- distinct routes actually differ meaningfully when real alternatives exist (k_shortest_paths + dedup) ---
#
# A narrower (shorter, less laterally-open) property than the other tests
# above -- a wide-open 2D gap leaves enough lateral wiggle room that
# several slightly-offset crossings of the SAME gap can all legitimately
# clear the dedup overlap threshold without representing a meaningfully
# different route choice; a single-cell-wide gap on a narrower property
# keeps this test's two scenarios clean and unambiguous.
gap_rows, gap_cols = 21, 41
gap_array = np.full((gap_rows, gap_cols), 100.0, dtype=np.float32)
gap_dem = {
    "array": gap_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500105.0, "crs": CRS,
}
gap_boundary = box(500000, 4500000, 500205, 4500105)
gap_anchor = _lon_lat_for_cell(gap_dem, 10, 2)

wall = box(500100, 4500000, 500105, 4500105)
gap_bottom = box(500095, 4500002.5, 500110, 4500007.5)  # single-cell-tall gap near the south edge
gap_top = box(500095, 4500097.5, 500110, 4500102.5)  # single-cell-tall gap near the north edge
two_gap_wall = wall.difference(gap_bottom).difference(gap_top)
two_gap_production = [{"id": 0, "render_fill_polygon_utm": two_gap_wall}]

two_gap_routes = find_road_routes(gap_dem, two_gap_production, None, gap_boundary, gap_anchor)
assert len(two_gap_routes) >= 2, (
    f"expected at least 2 distinct routes with two legal gaps around the production wall, got {len(two_gap_routes)}"
)
top_cells = set((p[0], p[1]) for p in two_gap_routes[0]["points_xyz"])
second_cells = set((p[0], p[1]) for p in two_gap_routes[1]["points_xyz"])
overlap = len(top_cells & second_cells) / min(len(top_cells), len(second_cells))
assert overlap < 0.7, (
    f"the top two routes overlap {overlap:.2f} -- expected genuinely distinct routes (through different "
    f"gaps), not near-duplicates that should have been caught by k_shortest_paths()'s own dedup"
)
print(
    f"With two legal gaps around a hard obstacle, {len(two_gap_routes)} genuinely distinct routes are found "
    f"(top-2 cell overlap only {overlap:.2f}), not near-duplicates slipping past dedup."
)

one_gap_wall = wall.difference(gap_bottom)
one_gap_production = [{"id": 0, "render_fill_polygon_utm": one_gap_wall}]
one_gap_routes = find_road_routes(gap_dem, one_gap_production, None, gap_boundary, gap_anchor)
assert len(one_gap_routes) == 1, (
    f"expected exactly one route with only one legal gap, got {len(one_gap_routes)}"
)
assert one_gap_routes[0]["suitability_score"] == 100.0
print("With only one legal gap, exactly one route is found, correctly scored 100.0 (the degenerate case).")


# --- output: schema-valid FeatureCollection on the required layer, with required (and NO stale) properties ---

geojson = corridors_to_geojson(routes, floodplain_data_is_fallback=True)
validate_feature_collection(geojson)
required_props = {"rank", "suitability_score", "avg_grade_pct", "length_ft", "crosses_floodplain", "constraints_satisfied"}
removed_props = {"corridor_type", "anchor_status", "anchor_road_name", "anchor_road_distance_ft", "crosses_production_zone"}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "suggested_road_corridor"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    present_removed = removed_props & feature["properties"].keys()
    assert not present_removed, f"these properties should no longer exist at all: {present_removed}"
    assert 0.0 <= feature["properties"]["suitability_score"] <= 100.0
    assert "outside_production_zone" in feature["properties"]["constraints_satisfied"], (
        "production is a HARD exclusion again under the current design -- this constraint IS genuinely "
        "satisfied by every route and should be reported"
    )
    assert "outside_pond_zone" in feature["properties"]["constraints_satisfied"]
    assert not any("grade" in c for c in feature["properties"]["constraints_satisfied"]), (
        "grade is a soft cost penalty now, not a hard-satisfied constraint -- it must not appear here"
    )
    assert feature["geometry"]["type"] == "LineString"
    notes = feature["properties"]["confidence_notes"].lower()
    assert "topographic suggestion" in notes and "not a surveyed" in notes
    assert "fallback" in notes, "floodplain fallback should be flagged in confidence_notes"
    assert "least-cost-path" in notes, "confidence_notes should describe the cost-driven routing model"
    assert "anchor" in notes, "confidence_notes should describe the anchor-point routing model"
print("corridors_to_geojson output is schema-valid, layer='suggested_road_corridor', with required properties "
      "present and every removed property (corridor_type/anchor_status/anchor_road_name/"
      "crosses_production_zone) correctly absent; constraints_satisfied correctly lists production+pond as "
      "hard, omits grade entirely.")


# --- regression: routes stay on-parcel, not drawn from the DEM's buffered margin ---
#
# This is the exact live bug found against the real property: dem_data.py
# fetches a DEM buffered ~100m past the drawn boundary (correct and
# intentional, for terrain-analysis context), but route generation must
# still be structurally confined to the actual parcel.
buffered_size = 80
buffered_array = np.full((buffered_size, buffered_size), 100.0, dtype=np.float32)
buffered_dem = {
    "array": buffered_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500400.0, "crs": CRS,
}
parcel_boundary = box(500050, 4500050, 500350, 4500350)
buffered_anchor = _lon_lat_for_cell(buffered_dem, 70, 10)  # near the buffered DEM's own edge, off-parcel

buffered_routes = find_road_routes(buffered_dem, [], None, parcel_boundary, buffered_anchor)
assert buffered_routes, "expected at least one route on this uniform, buffered DEM"

for route in buffered_routes:
    # parcel_boundary is convex (a box), so if every contributing cell is
    # on-parcel, the whole route line must stay within the parcel.
    assert route["line_utm"].within(parcel_boundary.buffer(1e-6)), (
        f"route (rank {route['rank']}) extends outside the real parcel boundary -- route geometry must be "
        f"drawn from on-parcel cells only, not the DEM's buffered margin"
    )
print(
    f"Parcel clipping: {len(buffered_routes)} route(s) on a DEM extending well past the parcel boundary all "
    f"stay entirely within the real (smaller) parcel -- even though the given anchor point itself was "
    f"off-parcel, snapping correctly pulled it onto real eligible ground."
)


# --- identify_road_corridor_candidates() degrades gracefully with no anchor point given ---

no_anchor_result = identify_road_corridor_candidates([(-79.98, 40.64), (-79.97, 40.64), (-79.97, 40.65)], dem=dem)
assert no_anchor_result["zones_geojson"]["features"] == []
assert no_anchor_result["all_scored_candidates"] == []
assert no_anchor_result["selected_road_corridor"] is None
print("identify_road_corridor_candidates() with no anchor_lon_lat given degrades to the same empty-result "
      "shape as any other not-yet-computable layer, rather than raising.")

print("\nAll road_corridors checks passed.")
