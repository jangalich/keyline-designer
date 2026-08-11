"""
test_road_corridors.py

Offline (no-network) checks for road_corridors.py's coverage-greedy road
NETWORK pipeline -- hand-built synthetic DEMs and hand-built production
areas/selected water zone (same shapes build_road_network() actually
consumes: production areas carry 'render_fill_polygon_utm', the
OPTIMIZED/final production geometry production_area_ceiling.py produces,
and the water zone is the single SELECTED zone water_suitability.py's own
scoring picks, not a list of unscored candidates), not a real DEM/NHD/
SSURGO fetch. Mirrors test_water_candidate_zones.py's and
test_solar_suitability.py's "pure logic, independent of real data
fetches" approach.

Route generation now grows a road NETWORK outward from a real anchor
point via road_network_router.route_road_network() -- see that module's
own docstring for the coverage-greedy SELECT/STOP algorithm -- rather
than identifying and scoring genuine ridge lines from the DEM. There is
no more RIDGE_MIN_AREA_ACRES/RIDGE_MIN_FRAGMENT_CELLS/RIDGE_SLOPE_WEIGHT/
RIDGE_LENGTH_WEIGHT/RIDGE_PRODUCTION_WEIGHT, no _identify_ridge_cell_
mask()/_prune_ridge_networks()/_order_fragment_from_entry()/_score_ridge_
candidates(), and find_road_routes() no longer exists at all -- it's
build_road_network() now, returning a NETWORK dict (branches: [], plus
network-level totals) rather than a list of at-most-one scored route. See
road_corridors.py's own module docstring for the current constraint-stack
split: the parcel boundary and a buffered selected-water-zone are HARD
exclusions (as before); grade above MAX_ROAD_GRADE_PCT is now ALSO a HARD
exclusion (road_cost_path.build_cost_raster()'s own impassable_grade_pct)
-- a real change from the earlier unbounded-soft-penalty design; production
zones are a SOFT, proportionally-costlier traversal term that ALSO defines
what counts as "demand" for the router; floodplain stays a SOFT flat cost
penalty.
"""

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, box
from shapely.prepared import prep

from feature_schema import validate_feature_collection
from raster_grid import pixel_center_xy
from road_corridors import (
    MAX_ROAD_GRADE_PCT,
    MIN_CORRIDOR_LENGTH_METERS,
    STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT,
    _build_production_cell_mask,
    _snap_anchor_to_eligible_cell,
    build_road_network,
    corridors_to_geojson,
    identify_road_corridor_candidates,
)
from road_cost_path import build_cost_raster

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)


def _flat_dem(size=41, origin_x=500000.0, origin_y=4500205.0, elevation=100.0):
    """A perfectly flat DEM -- every cell the same elevation, so slope is
    0 everywhere the Horn kernel can compute it at all (the outer 1-cell
    ring stays NaN regardless -- compute_slope_and_aspect() never visits
    the border, see terrain_metrics.py)."""
    array = np.full((size, size), elevation, dtype=np.float32)
    return {"array": array, "resolution_meters": RESOLUTION, "origin_x": origin_x, "origin_y": origin_y, "crs": CRS}


def _lon_lat_for_cell(dem: dict, row: int, col: int) -> tuple[float, float]:
    x, y = pixel_center_xy(dem, row, col)
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", [x], [y])
    return (lons[0], lats[0])


def _flat_cost_raster(dem: dict, excluded_mask=None) -> np.ndarray:
    """A finite-everywhere-except-excluded cost raster over a flat DEM,
    for tests that only care about snapping geometry, not slope."""
    rows, cols = dem["array"].shape
    slope_pct = np.zeros((rows, cols), dtype=np.float32)
    if excluded_mask is None:
        excluded_mask = np.zeros((rows, cols), dtype=bool)
    return build_cost_raster(dem, slope_pct, excluded_mask)


def _cell_box_utm(dem: dict, row_lo: int, row_hi: int, col_lo: int, col_hi: int):
    """A UTM box covering cells [row_lo, row_hi) x [col_lo, col_hi) of dem,
    for building a synthetic production/water polygon aligned to real DEM
    cell boundaries."""
    px, py = dem["resolution_meters"]
    x0 = dem["origin_x"] + col_lo * px
    x1 = dem["origin_x"] + col_hi * px
    y1 = dem["origin_y"] - row_lo * py
    y0 = dem["origin_y"] - row_hi * py
    return box(x0, y0, x1, y1)


# =====================================================================
# _snap_anchor_to_eligible_cell: snaps to the nearest FINITE-cost cell,
# not the raw anchor cell (unchanged from before this branch)
# =====================================================================

flat_dem_snap = _flat_dem()
snap_anchor = _lon_lat_for_cell(flat_dem_snap, 38, 2)
excluded_mask_snap = np.zeros((41, 41), dtype=bool)
excluded_mask_snap[38, 2] = True  # exclude the exact cell the anchor above points at
cost_raster_with_hole = _flat_cost_raster(flat_dem_snap, excluded_mask_snap)
snapped = _snap_anchor_to_eligible_cell(flat_dem_snap, cost_raster_with_hole, snap_anchor)
assert snapped is not None and snapped != (38, 2), (
    "the anchor's own raw cell is hard-excluded -- snapping must land on a DIFFERENT, actually-eligible cell"
)
assert np.isfinite(cost_raster_with_hole[snapped]), "the snapped cell itself must be finite-cost"

fully_excluded_snap = np.ones((41, 41), dtype=bool)
cost_raster_none_eligible = _flat_cost_raster(flat_dem_snap, fully_excluded_snap)
assert _snap_anchor_to_eligible_cell(flat_dem_snap, cost_raster_none_eligible, snap_anchor) is None, (
    "with literally no eligible cell anywhere, snapping must return None, not raise or fabricate a cell"
)
print("_snap_anchor_to_eligible_cell correctly snaps past a hard-excluded anchor cell to real eligible ground, "
      "and returns None honestly when nothing is eligible at all.")


# =====================================================================
# _build_production_cell_mask: True only inside the given union AND on-parcel (unchanged logic)
# =====================================================================

flat_dem_prod = _flat_dem()
flat_boundary = box(500000, 4500000, 500205, 4500205)
production_polygon = box(500000, 4500000, 500100, 4500205)
production_prepared2 = prep(production_polygon)
boundary_prepared2 = prep(flat_boundary)
production_mask2 = _build_production_cell_mask(flat_dem_prod, production_prepared2, boundary_prepared2)
for r in range(1, 40):
    for c in range(1, 40):
        point = Point(pixel_center_xy(flat_dem_prod, r, c))
        expected = boundary_prepared2.contains(point) and production_prepared2.contains(point)
        assert production_mask2[r, c] == expected, f"production mask mismatch at ({r}, {c})"
assert _build_production_cell_mask(flat_dem_prod, None, boundary_prepared2).sum() == 0, (
    "a None prepared geometry (no production zones/floodplain/water at all) must produce an all-False mask"
)
print("_build_production_cell_mask correctly flags only on-parcel cells actually inside the given union.")


# =====================================================================
# build_road_network(): end-to-end on a real synthetic DEM with real
# nearby production demand -- the network must actually grow a branch to
# serve it
# =====================================================================

network_dem = _flat_dem()
network_boundary = box(500000, 4500000, 500205, 4500205)
network_anchor = _lon_lat_for_cell(network_dem, 38, 2)  # near a corner
network_production_areas = [
    {"id": 0, "render_fill_polygon_utm": _cell_box_utm(network_dem, 0, 41, 0, 20)}
]  # a large block spanning the full height of the DEM -- large enough (and far enough beyond the
# anchor's own PRODUCTION_SERVICE_RADIUS_METERS baseline coverage) that a real branch is worth building

network = build_road_network(
    network_dem, network_production_areas, None, network_boundary, network_anchor
)
assert network["branches"], (
    "expected at least one branch: there is real, nearby, reachable production demand for the "
    "router to serve"
)
assert network["total_length_meters"] > 0
assert network["total_served_acres"] > 0
assert network["stop_reason"] in {"all_demand_served", "no_reachable_demand", "cost_per_acre_exceeded"}

for branch in network["branches"]:
    assert len(branch["cells"]) >= 2
    assert len(branch["points_xyz"]) >= 2
    assert branch["line_utm"].length > 0
    assert branch["line_utm"].within(network_boundary.buffer(1e-6)), "every branch must stay entirely on-parcel"
    assert branch["branch_role"] in {"trunk", "spur", "water_spur"}
    footprint = branch["cell_footprint_polygon_utm"]
    assert footprint is not None and not footprint.is_empty and footprint.area > 0
    assert footprint.within(network_boundary.buffer(1e-6))
    for key in ("branch_index", "joins_branch_index", "length_meters", "total_cost", "newly_served_acres",
                "avg_grade_pct", "crosses_floodplain", "crosses_production_zone", "production_cells_crossed"):
        assert key in branch, f"missing branch field: {key}"

assert network["cells"], "network-level 'cells' must list every branch cell across the whole network"
network_footprint = network["cell_footprint_polygon_utm"]
assert network_footprint is not None and not network_footprint.is_empty and network_footprint.area > 0
assert network_footprint.within(network_boundary.buffer(1e-6))
print(
    f"build_road_network() on a real synthetic DEM with nearby production demand grows "
    f"{len(network['branches'])} branch(es) totaling {network['total_length_meters']:.0f}m, staying "
    f"entirely on-parcel, with real per-branch and network-level footprints."
)


# --- MAX_ROAD_GRADE_PCT is now a genuine HARD ceiling, not merely an unbounded soft penalty ---

steep_dem = _flat_dem()
steep_array = steep_dem["array"]
# A cliff: every cell south of row 20 drops far enough that the grade
# between rows 19 and 20 vastly exceeds MAX_ROAD_GRADE_PCT -- no branch
# should ever be routed across that row boundary.
steep_array[20:, :] -= 500.0
steep_boundary = box(500000, 4500000, 500205, 4500205)
steep_anchor = _lon_lat_for_cell(steep_dem, 38, 2)  # south side, below the cliff
steep_production_areas = [
    {"id": 0, "render_fill_polygon_utm": _cell_box_utm(steep_dem, 0, 19, 0, 41)}
]  # north side, above the cliff -- unreachable without crossing it

steep_network = build_road_network(steep_dem, steep_production_areas, None, steep_boundary, steep_anchor)
for branch in steep_network["branches"]:
    for (r1, c1), (r2, c2) in zip(branch["cells"], branch["cells"][1:]):
        assert not (r1 < 20 <= r2 or r2 < 20 <= r1), (
            "a branch crossed the hard-excluded cliff row -- grade above MAX_ROAD_GRADE_PCT must be "
            "impassable, never merely expensive"
        )
print(
    f"MAX_ROAD_GRADE_PCT ({MAX_ROAD_GRADE_PCT}%) is enforced as a genuine hard exclusion -- no branch "
    f"crosses a grade break that steep, regardless of how much production demand sits on the far side."
)


# =====================================================================
# corridors_to_geojson: schema-valid, one feature per branch, required
# (and NO stale ridge_*) properties
# =====================================================================

geojson = corridors_to_geojson(network, floodplain_data_is_fallback=True)
validate_feature_collection(geojson)
required_props = {
    "branch_index", "branch_role", "joins_branch_index", "length_ft", "avg_grade_pct",
    "newly_served_acres", "crosses_floodplain", "crosses_production_zone",
    "total_length_ft", "total_served_acres", "stop_reason", "constraints_satisfied",
}
removed_props = {
    "corridor_type", "anchor_status", "anchor_road_name", "anchor_road_distance_ft", "fan_direction",
    "rank", "suitability_score", "avg_slope_pct", "ridge_avg_slope_pct", "ridge_production_cells_crossed",
    "ridge_slope_score", "ridge_length_score", "ridge_production_score", "ridge_weighted_score",
    "ridge_proximity_score", "weighted_score", "slope_score", "length_score", "production_score",
}
assert len(geojson["features"]) == len(network["branches"]), "one feature per branch, no ranking/truncation"
for index, feature in enumerate(geojson["features"]):
    assert feature["properties"]["layer"] == "suggested_road_corridor"
    assert feature["id"] == f"road-corridor-{index + 1}"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    present_removed = removed_props & feature["properties"].keys()
    assert not present_removed, f"these properties should no longer exist at all: {present_removed}"
    assert feature["properties"]["constraints_satisfied"] == ["outside_pond_zone", "grade_within_max"], (
        "grade is now genuinely hard-enforced (impassable_grade_pct) -- it belongs in "
        "constraints_satisfied now, alongside the still-hard pond zone"
    )
    assert feature["geometry"]["type"] == "LineString"
    notes = feature["properties"]["confidence_notes"].lower()
    assert "topographic suggestion" in notes and "not a surveyed" in notes
    assert "fallback" in notes, "floodplain fallback should be flagged in confidence_notes"
print(
    "corridors_to_geojson output is schema-valid, layer='suggested_road_corridor', one feature per "
    "branch with required properties present, constraints_satisfied now lists both pond and grade as "
    "hard, and every ridge-era property (ridge_*/rank/suitability_score/weighted_score/etc.) is "
    "correctly absent."
)

empty_geojson = corridors_to_geojson({"branches": [], "total_length_meters": 0.0, "total_served_acres": 0.0,
                                       "unserved_acres": 0.0, "stop_reason": "no_demand"})
assert empty_geojson["features"] == [], "an empty network must produce an empty FeatureCollection, not raise"
print("corridors_to_geojson correctly produces an empty FeatureCollection for an empty (no-branch) network.")


# =====================================================================
# empty-network-shape coverage: build_road_network()/identify_road_
# corridor_candidates() must return the SAME real, reportable shape
# (branches=[], never None, never an exception) across every "no road"
# reason
# =====================================================================

EMPTY_NETWORK_KEYS = {
    "branches", "total_length_meters", "total_served_acres", "unserved_acres", "stop_reason",
    "cells", "cell_footprint_polygon_utm",
}


def _assert_empty_network_shape(result: dict, context: str) -> None:
    assert EMPTY_NETWORK_KEYS.issubset(result.keys()), f"{context}: missing keys {EMPTY_NETWORK_KEYS - result.keys()}"
    assert result["branches"] == [], f"{context}: expected branches == [], got {result['branches']}"
    assert result["cells"] == [], f"{context}: expected cells == [], got {result['cells']}"
    assert result["total_length_meters"] == 0.0, f"{context}: expected total_length_meters == 0.0"
    footprint = result["cell_footprint_polygon_utm"]
    assert footprint is not None and footprint.is_empty, f"{context}: expected an empty (not None) footprint polygon"
    assert isinstance(result["stop_reason"], str) and result["stop_reason"], f"{context}: expected a real stop_reason string"


# --- 1. no anchor at all (identify_road_corridor_candidates()'s own Optional anchor_lon_lat=None) ---

no_anchor_result = identify_road_corridor_candidates(
    [(-79.98, 40.64), (-79.97, 40.64), (-79.97, 40.65)], dem=_flat_dem()
)
_assert_empty_network_shape(no_anchor_result["road_network"], "no anchor given")
assert no_anchor_result["zones_geojson"]["features"] == []
assert no_anchor_result["selected_road_corridor"] is None
print("identify_road_corridor_candidates() with no anchor_lon_lat given returns the empty-network shape, "
      "not None and not an exception.")

# --- 2. anchor snaps to nowhere (every cell hard-excluded -- a boundary that doesn't overlap the DEM at all) ---

nowhere_dem = _flat_dem()
nowhere_boundary = box(0.0, 0.0, 1.0, 1.0)  # nowhere near this DEM's real UTM extent
nowhere_anchor = _lon_lat_for_cell(nowhere_dem, 20, 20)
nowhere_result = build_road_network(nowhere_dem, [], None, nowhere_boundary, nowhere_anchor)
_assert_empty_network_shape(nowhere_result, "anchor snaps to nowhere")
print("build_road_network() with a boundary that leaves no eligible cell anywhere returns the "
      "empty-network shape.")

# --- 3. no production areas at all (zero demand -- route_road_network()'s own 'no_demand' path) ---

no_production_result = build_road_network(network_dem, [], None, network_boundary, network_anchor)
_assert_empty_network_shape(no_production_result, "no production areas")
assert no_production_result["stop_reason"] == "no_demand"
print("build_road_network() with no production areas at all (zero demand) returns the empty-network "
      "shape with stop_reason='no_demand'.")

# --- 4. total network length below min_corridor_length_meters (real branches found, then discarded) ---

too_strict_result = build_road_network(
    network_dem, network_production_areas, None, network_boundary, network_anchor,
    min_corridor_length_meters=1_000_000.0,
)
_assert_empty_network_shape(too_strict_result, "below min_corridor_length_meters")
assert too_strict_result["stop_reason"] == "corridor_too_short"
print(f"build_road_network() with an unreasonably high min_corridor_length_meters (real demand exists, "
      f"but MIN_CORRIDOR_LENGTH_METERS default is {MIN_CORRIDOR_LENGTH_METERS}) still returns the "
      f"empty-network shape, discarding the router's own real result.")


# =====================================================================
# regression: routes stay on-parcel, not drawn from the DEM's buffered margin
# =====================================================================
#
# This is the exact live bug found against the real property: dem_data.py
# fetches a DEM buffered ~100m past the drawn boundary (correct and
# intentional, for terrain-analysis context), but route generation must
# still be structurally confined to the actual parcel.
buffered_dem = _flat_dem(size=61, origin_x=500000.0, origin_y=4500305.0)
parcel_boundary = box(500050, 4500050, 500250, 4500250)  # smaller than the DEM's own 305x305m extent
buffered_anchor = _lon_lat_for_cell(buffered_dem, 58, 2)  # near the buffered DEM's own edge, off-parcel
buffered_production_areas = [
    {"id": 0, "render_fill_polygon_utm": _cell_box_utm(buffered_dem, 12, 40, 12, 45)}
]

buffered_network = build_road_network(
    buffered_dem, buffered_production_areas, None, parcel_boundary, buffered_anchor
)
assert buffered_network["branches"], "expected at least one branch on this uniform, buffered DEM"
for branch in buffered_network["branches"]:
    assert branch["line_utm"].within(parcel_boundary.buffer(1e-6)), (
        "a branch extends outside the real parcel boundary -- route geometry must be drawn from "
        "on-parcel cells only, not the DEM's buffered margin"
    )
print(
    "Parcel clipping: the network on a DEM extending well past the parcel boundary stays entirely "
    "within the real (smaller) parcel -- even though the given anchor point itself was off-parcel, "
    "snapping correctly pulled it onto real eligible ground."
)


# =====================================================================
# floodplain is a SOFT cost penalty: the network may still cross it
# =====================================================================

floodplain_dem = _flat_dem()
floodplain_boundary = box(500000, 4500000, 500205, 4500205)
floodplain_anchor = _lon_lat_for_cell(floodplain_dem, 38, 2)
floodplain_production_areas = [
    {"id": 0, "render_fill_polygon_utm": _cell_box_utm(floodplain_dem, 0, 19, 0, 41)}
]  # north side, far from the (southern) anchor
# A floodplain band directly between the anchor and the production zone --
# the network has no way to reach it without crossing.
connector_floodplain = _cell_box_utm(floodplain_dem, 20, 25, 0, 41)

# max_meters_per_served_acre is raised well above its own default here
# purely so the router keeps extending far enough to actually reach the
# north-side production zone in this synthetic fixture instead of
# stopping just short of the floodplain band (a real "not worth it yet"
# stop, exercised elsewhere in this file) -- the point of THIS test is
# proving floodplain crossing is soft, not that a real property's own
# tuned defaults would always justify crossing this exact synthetic band.
floodplain_network = build_road_network(
    floodplain_dem, floodplain_production_areas, None, floodplain_boundary, floodplain_anchor,
    hydric_floodplain_union=connector_floodplain, max_meters_per_served_acre=100_000.0,
)
assert floodplain_network["branches"], (
    "a floodplain band blocking the only path to the production zone must NOT prevent routing "
    "entirely -- it's a SOFT cost penalty"
)
assert any(b["crosses_floodplain"] for b in floodplain_network["branches"]), (
    "expected at least one branch to actually cross the floodplain band, proving it's traversable"
)
print("Floodplain/hydric ground is a SOFT cost penalty: the network still crosses a blocking "
      "floodplain band (crosses_floodplain=True on at least one branch) rather than routing failing "
      "outright.")


# =====================================================================
# selected water zone is still a HARD exclusion, and water_target_cells
# sit just outside its own buffer (a water_spur branch can be built)
# =====================================================================

water_dem = _flat_dem()
water_boundary = box(500000, 4500000, 500205, 4500205)
water_anchor = _lon_lat_for_cell(water_dem, 38, 2)
water_production_areas = [
    {"id": 0, "render_fill_polygon_utm": _cell_box_utm(water_dem, 0, 41, 0, 20)}
]
selected_water_zone = {"id": 0, "render_fill_polygon_utm": _cell_box_utm(water_dem, 15, 19, 25, 29)}
pond_prepared = prep(selected_water_zone["render_fill_polygon_utm"].buffer(25))  # matches POND_ZONE_EXCLUSION_BUFFER_METERS

water_network = build_road_network(
    water_dem, water_production_areas, selected_water_zone, water_boundary, water_anchor
)
assert water_network["branches"], "expected a real network even with a selected water zone present"
for branch in water_network["branches"]:
    for x, y, _z in branch["points_xyz"]:
        assert not pond_prepared.contains(Point(x, y)), (
            f"branch point ({x}, {y}) falls inside the buffered selected water-system zone -- still a HARD exclusion"
        )
print("Selected water-system zone exclusion correctly keeps every branch point clear of the buffered zone.")


# =====================================================================
# steep-grade engineering-consideration note is additive, threshold-gated
# =====================================================================

gentle_geojson = corridors_to_geojson(network)  # the original gentle, flat-DEM network from earlier
for feature in gentle_geojson["features"]:
    if feature["properties"]["avg_grade_pct"] <= STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT:
        assert "real engineering consideration" not in feature["properties"]["confidence_notes"]
        assert "TOPOGRAPHIC SUGGESTION only, not a surveyed road alignment" in feature["properties"]["confidence_notes"]
print(
    f"Gentle-grade branches (<= {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}%) correctly omit the "
    f"additive steep-grade engineering-consideration note, while the blanket disclaimer stays present."
)

print("\nAll road_corridors checks passed.")
