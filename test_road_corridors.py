"""
test_road_corridors.py

Offline (no-network) checks for road_corridors.py's constraint-stack,
fan-based anchor-driven routing, and ranking logic — hand-built synthetic
DEMs and hand-built production areas/selected water zone (same shapes
find_road_routes() actually consumes: production areas carry
'render_fill_polygon_utm', the OPTIMIZED/final production geometry
production_area_ceiling.py produces, and the water zone is the single
SELECTED zone water_suitability.py's own scoring picks, not a list of
unscored candidates), not a real DEM/NHD/SSURGO fetch. Mirrors
test_water_candidate_zones.py's and test_solar_suitability.py's "pure
logic, independent of real data fetches" approach.

Route generation now casts up to three "fan" rays from the anchor point --
the primary axis toward boundary_polygon_utm's own centroid, plus that
axis rotated ±22.5° -- and routes independently (one least_cost_path()
call each) to wherever each ray exits the boundary, snapped to the
nearest eligible cell (_select_fan_destination_cells()). This replaced an
earlier "single farthest-point destination, several k_shortest_paths()
alternates to it" design -- k_shortest_paths() itself is untouched and
still exported by road_cost_path.py, just not called from this module
right now. See road_corridors.py's own module docstring for the current
constraint-stack split (water + production HARD-excluded, floodplain +
grade SOFT cost penalties). There is no more 'corridor_type',
'anchor_status', 'anchor_road_name', or 'crosses_production_zone' field
anywhere in the output; this file no longer asserts anything about any of
them (except asserting their absence).
"""

import io
from contextlib import redirect_stdout

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, box
from shapely.prepared import prep

from feature_schema import validate_feature_collection
from raster_grid import pixel_center_xy
from road_corridors import (
    STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT,
    _build_production_cell_mask,
    _select_fan_destination_cells,
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


def _flat_cost_raster(dem: dict, excluded_mask=None) -> np.ndarray:
    """A finite-everywhere-except-excluded cost raster over a flat DEM,
    for tests that only care about fan-ray/snapping geometry, not slope."""
    rows, cols = dem["array"].shape
    slope_pct = np.zeros((rows, cols), dtype=np.float32)
    if excluded_mask is None:
        excluded_mask = np.zeros((rows, cols), dtype=bool)
    return build_cost_raster(dem, slope_pct, excluded_mask)


dem = _flat_dem()
boundary = box(500000, 4500000, 500205, 4500205)
sw_corner_anchor = _lon_lat_for_cell(dem, 38, 2)  # near the SW corner, well inside the boundary


# --- basic route generation: non-empty, ranked by ascending total_cost, degenerate single-route scoring ---

routes = find_road_routes(dem, [], None, boundary, sw_corner_anchor)
assert routes, "expected at least one route on an open, flat, unconstrained property"
for route in routes:
    assert len(route["points_xyz"]) >= 2, "a returned route must have at least 2 points to form a line"
    assert route["line_utm"].length > 0
    assert route["fan_direction"] in ("primary", "left", "right")
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
    f"Fan-based generation produces {len(routes)} route(s) on an open property, ranked by ascending "
    f"total_cost, each carrying a real fan_direction, with the degenerate single-route scoring case "
    f"handled correctly."
)


# --- _snap_anchor_to_eligible_cell: snaps to the nearest FINITE-cost cell, not the raw anchor cell (unchanged) ---

excluded_mask = np.zeros((41, 41), dtype=bool)
excluded_mask[38, 2] = True  # exclude the exact cell the anchor above points at
cost_raster_with_hole = _flat_cost_raster(dem, excluded_mask)
snapped = _snap_anchor_to_eligible_cell(dem, cost_raster_with_hole, sw_corner_anchor)
assert snapped is not None and snapped != (38, 2), (
    "the anchor's own raw cell is hard-excluded -- snapping must land on a DIFFERENT, actually-eligible cell"
)
assert np.isfinite(cost_raster_with_hole[snapped]), "the snapped cell itself must be finite-cost"

fully_excluded = np.ones((41, 41), dtype=bool)
cost_raster_none_eligible = _flat_cost_raster(dem, fully_excluded)
assert _snap_anchor_to_eligible_cell(dem, cost_raster_none_eligible, sw_corner_anchor) is None, (
    "with literally no eligible cell anywhere, snapping must return None, not raise or fabricate a cell"
)
print("_snap_anchor_to_eligible_cell correctly snaps past a hard-excluded anchor cell to real eligible ground, "
      "and returns None honestly when nothing is eligible at all.")


# --- _select_fan_destination_cells: up to 3 destinations, one per fan direction, spread across the property ---

open_cost_raster = _flat_cost_raster(dem)
center_anchor_cell = (20, 20)
fan_cells = _select_fan_destination_cells(dem, open_cost_raster, center_anchor_cell, boundary)
assert len(fan_cells) == 3, f"expected all 3 fan rays to succeed on an open square property, got {len(fan_cells)}"
assert len(set(fan_cells)) == 3, "expected 3 genuinely DISTINCT destination cells (not deduplicated away)"
# Every fan destination must itself be a real eligible cell (finite cost).
for cell in fan_cells:
    assert np.isfinite(open_cost_raster[cell]), f"fan destination {cell} is not itself finite-cost"
# And every fan destination must land ON (or extremely near) the boundary's
# own exterior ring -- that's the whole point of casting a ray OUT to it.
boundary_ring_prepared = prep(boundary.exterior.buffer(RESOLUTION[0]))  # 1-cell tolerance for raster snapping
for cell in fan_cells:
    x, y = pixel_center_xy(dem, *cell)
    assert boundary_ring_prepared.contains(Point(x, y)), (
        f"fan destination {cell} at ({x}, {y}) doesn't actually sit near the boundary's own exterior ring"
    )
print(f"_select_fan_destination_cells finds {len(fan_cells)} genuinely distinct destinations on an open "
      f"square property, each landing on the boundary's own exterior ring.")


# --- _select_fan_destination_cells: dedup -- multiple rays snapping to the SAME real destination collapse to one ---

tiny_cluster_mask = np.ones((41, 41), dtype=bool)
tiny_cluster_mask[1:4, 1:4] = False  # the ONLY eligible ground anywhere is this tiny NW cluster
tiny_cluster_cost_raster = _flat_cost_raster(dem, tiny_cluster_mask)
deduped_cells = _select_fan_destination_cells(dem, tiny_cluster_cost_raster, (20, 20), boundary)
assert len(deduped_cells) == 1, (
    f"with only one real eligible cluster anywhere on the property, every fan ray's own exit point should "
    f"snap to it and collapse to a single deduplicated destination, got {len(deduped_cells)}"
)
print("_select_fan_destination_cells correctly deduplicates when multiple rays snap to the same real cell, "
      "rather than returning duplicate destinations.")


# --- _select_fan_destination_cells: defensive fallback -- an anchor cell outside boundary_polygon_utm ---

small_boundary = box(500150, 4500150, 500200, 4500200)  # far from the SW-corner anchor cell used below
outside_anchor_cell = (38, 2)
buffer = io.StringIO()
with redirect_stdout(buffer):
    outside_result = _select_fan_destination_cells(dem, open_cost_raster, outside_anchor_cell, small_boundary)
log_output = buffer.getvalue()
assert outside_result, (
    "the primary ray (pointed straight at the boundary's own centroid) should still succeed even from an "
    "anchor cell well outside the boundary -- only the off-axis rays are expected to miss such a small, "
    "distant target"
)
assert "falls outside boundary_polygon_utm" in log_output, (
    "a ray failing because the anchor itself is outside the boundary should be logged as exactly that "
    "condition, not confused with the (shouldn't-happen) ray-too-short case"
)
assert "shouldn't be possible" not in log_output, (
    "this scenario is the anchor-outside-boundary condition, not the defensive ray-too-short one -- the "
    "wrong condition must not be logged"
)
print("_select_fan_destination_cells logs the correct condition (anchor cell outside boundary_polygon_utm) "
      "when a ray misses, and drops that fan point rather than forcing one.")


# --- narrow/sliver-shaped parcel: exercises the ±22.5° fan geometry somewhere other than a square/hexagon parcel ---
#
# A 20m-wide x 400m-tall sliver -- narrow enough that the off-axis
# (left/right) rays exit through the parcel's own SIDE wall almost
# immediately, close to the anchor itself, rather than traveling the
# sliver's own length the way the primary ray does. Real, worth knowing
# behavior this surfaces: a fan destination that lands only a few cells
# from the anchor can have a post-snap bearing that reads as close to
# the PRIMARY axis (see find_road_routes()'s own bearing-based
# fan_direction classification) even though it was genuinely cast as an
# off-axis ray -- direction labels reflect the resulting route's own
# geometry, not a guaranteed one-to-one tag per original ray. A square/
# hexagon-shaped parcel (every other test in this file) doesn't surface
# this at all, since its own off-axis rays travel far enough from the
# anchor before exiting that bearing drift after snapping is negligible.
sliver_rows, sliver_cols = 80, 4
sliver_array = np.full((sliver_rows, sliver_cols), 100.0, dtype=np.float32)
sliver_dem = {
    "array": sliver_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500400.0, "crs": CRS,
}
sliver_boundary = box(500000, 4500000, 500020, 4500400)
sliver_anchor = _lon_lat_for_cell(sliver_dem, 78, 1)  # near the sliver's own south end

sliver_routes = find_road_routes(sliver_dem, [], None, sliver_boundary, sliver_anchor)
assert sliver_routes, "expected at least one route on the sliver parcel (the primary, along-the-sliver direction)"
for route in sliver_routes:
    assert route["line_utm"].within(sliver_boundary.buffer(1e-6)), (
        "every route on the sliver parcel must still stay entirely within its own (narrow) boundary"
    )
    assert route["length_m"] >= 30.0  # MIN_CORRIDOR_LENGTH_METERS -- degenerate near-anchor destinations are dropped
print(
    f"Sliver parcel: {len(sliver_routes)} route(s) survive min-length filtering (fan directions: "
    f"{[r['fan_direction'] for r in sliver_routes]}) -- confirms the ±22.5° fan geometry doesn't crash or "
    f"produce nonsensical/degenerate output on an extreme narrow parcel, even where off-axis rays exit "
    f"close enough to the anchor that their resulting bearing can read as 'primary'."
)


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


# --- production zones are a HARD exclusion: no route may use a cell inside one ---

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


# --- a production zone surrounding the anchor on every side leaves NO route at all (confirms it's genuinely hard) ---
#
# With up to 3 independent fan-direction routes now (not one destination),
# an obstacle has to block EVERY direction to leave zero routes total --
# a single N-S wall (an earlier version of this test's own obstacle)
# no longer reliably does that, since a fan ray on the SAME side of the
# wall as the anchor never needs to cross it at all. Encircling the
# anchor's own small pocket entirely in production land is the genuinely
# unambiguous "every direction is blocked" construction instead.
anchor_x, anchor_y = pixel_center_xy(dem, 38, 2)
anchor_pocket = box(anchor_x - 7, anchor_y - 7, anchor_x + 7, anchor_y + 7)
surrounding_production = [{"id": 0, "render_fill_polygon_utm": boundary.difference(anchor_pocket)}]
surrounded_routes = find_road_routes(dem, surrounding_production, None, boundary, sw_corner_anchor)
assert surrounded_routes == [], (
    "a production zone surrounding the anchor's own small pocket on every side should leave NO legal route "
    "at all in any fan direction -- if any route were still found, production wouldn't really be hard-excluded"
)
print("A production zone surrounding the anchor on every side correctly blocks every fan direction -- "
      "confirms the exclusion is genuinely hard.")


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


# --- floodplain is a SOFT cost penalty: a route MAY still cross it, and isn't blocked outright ---

# A floodplain band spanning the FULL property height -- if this were still
# a hard exclusion (as in an earlier version of this module), no route in
# ANY fan direction crossing it could exist. Since floodplain is soft,
# every fan direction that would otherwise need to cross it still finds a
# route straight through.
full_span_floodplain = box(500090, 4500000, 500115, 4500205)
floodplain_routes = find_road_routes(
    dem, [], None, boundary, sw_corner_anchor, hydric_floodplain_union=full_span_floodplain
)
assert floodplain_routes, (
    "a full-height floodplain band must NOT block routing entirely -- floodplain is a SOFT cost penalty now, "
    "not a hard exclusion"
)
assert any(r["crosses_floodplain"] for r in floodplain_routes), (
    "expected at least one route to actually cross the floodplain band, proving it's traversable"
)
open_routes = find_road_routes(dem, [], None, boundary, sw_corner_anchor)
assert min(r["total_cost"] for r in floodplain_routes) > min(r["total_cost"] for r in open_routes), (
    "crossing the floodplain penalty region should cost more than the same routes on otherwise-open ground"
)
print(
    "Floodplain/hydric ground is a SOFT cost penalty, not a hard exclusion: routes can still cross a "
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


# --- distinct fan-direction routes actually differ meaningfully (low mutual cell overlap) ---

gap_rows, gap_cols = 21, 41
gap_array = np.full((gap_rows, gap_cols), 100.0, dtype=np.float32)
gap_dem = {
    "array": gap_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500105.0, "crs": CRS,
}
gap_boundary = box(500000, 4500000, 500205, 4500105)
gap_anchor = _lon_lat_for_cell(gap_dem, 10, 2)

gap_routes = find_road_routes(gap_dem, [], None, gap_boundary, gap_anchor)
assert len(gap_routes) >= 2, f"expected at least 2 fan-direction routes on this open property, got {len(gap_routes)}"
directions_seen = {r["fan_direction"] for r in gap_routes}
assert len(directions_seen) == len(gap_routes), (
    f"expected every surviving route to carry a DISTINCT fan_direction label, got {[r['fan_direction'] for r in gap_routes]}"
)
cellsets = [set((p[0], p[1]) for p in r["points_xyz"]) for r in gap_routes]
max_overlap = max(
    len(cellsets[i] & cellsets[j]) / min(len(cellsets[i]), len(cellsets[j]))
    for i in range(len(cellsets)) for j in range(i + 1, len(cellsets))
)
assert max_overlap < 0.7, (
    f"the fan-direction routes overlap up to {max_overlap:.2f} -- expected genuinely distinct routes headed "
    f"to different regions of the property, not near-duplicates"
)
print(
    f"Fan-direction routes are genuinely distinct: {len(gap_routes)} routes with unique fan_direction labels "
    f"({sorted(directions_seen)}), maximum pairwise cell overlap only {max_overlap:.2f}."
)


# --- output: schema-valid FeatureCollection on the required layer, with required (and NO stale) properties ---

geojson = corridors_to_geojson(routes, floodplain_data_is_fallback=True)
validate_feature_collection(geojson)
required_props = {
    "rank", "suitability_score", "avg_grade_pct", "length_ft",
    "crosses_floodplain", "fan_direction", "constraints_satisfied",
}
removed_props = {"corridor_type", "anchor_status", "anchor_road_name", "anchor_road_distance_ft", "crosses_production_zone"}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "suggested_road_corridor"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert feature["properties"]["fan_direction"] in ("primary", "left", "right")
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
      "(including fan_direction) present and every removed property (corridor_type/anchor_status/"
      "anchor_road_name/crosses_production_zone) correctly absent; constraints_satisfied correctly lists "
      "production+pond as hard, omits grade entirely.")


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
