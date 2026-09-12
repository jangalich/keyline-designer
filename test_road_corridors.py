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
    STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT,
    _build_production_cell_mask,
    _snap_anchor_to_eligible_cell,
    build_road_network,
    corridors_to_geojson,
    identify_road_corridor_candidates,
)
from road_cost_path import build_cost_raster, cost_distance_field

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

# NO FLOOR TO PIN DOWN ANY MORE. This test used to pass
# min_corridor_length_meters=1.0 so that a network-growth assertion could
# not be flipped by this fixture's ~104 m total sitting a few metres above
# the 100 m default floor. That floor is deleted -- whatever the router
# builds is kept -- so the coupling is gone with it and the call is the
# plain one.
network = build_road_network(
    network_dem, network_production_areas, None, network_boundary, network_anchor,
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

# --- 4. THE NETWORK-LENGTH FLOOR IS GONE: a short network is KEPT ---
#
# This section used to pass min_corridor_length_meters=1_000_000.0 and
# assert that build_road_network() threw the router's own real result away
# with stop_reason 'corridor_too_short'. Both the parameter and the floor
# are deleted, so what is asserted now is the opposite and it is the point
# of the deletion: this fixture's network totals about 104 m, well under
# the 100 m the old default would have measured it against once a spur or
# two came off, and it survives. There is no length at which a routed
# network is discarded.
import inspect as _inspect  # noqa: E402
import road_corridors as _road_corridors  # noqa: E402

assert not hasattr(_road_corridors, "MIN_CORRIDOR_LENGTH_METERS")
assert "min_corridor_length_meters" not in _inspect.signature(build_road_network).parameters
_short_result = build_road_network(
    network_dem, network_production_areas, None, network_boundary, network_anchor,
)
assert _short_result["branches"], "the router's own result must reach the caller"
assert _short_result["stop_reason"] != "corridor_too_short"
assert _short_result["total_length_meters"] > 0
print(f"build_road_network() KEEPS the network the router built "
      f"({_short_result['total_length_meters']:.1f} m over "
      f"{len(_short_result['branches'])} branch(es), stop_reason "
      f"{_short_result['stop_reason']!r}); the length floor and its "
      f"'corridor_too_short' outcome are deleted, constant and parameter alike.")


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
    buffered_dem, buffered_production_areas, None, parcel_boundary, buffered_anchor,
    # A HUGE PER-ACRE CEILING, because this section is about GEOMETRY and
    # must not be a router-tuning assertion in disguise. The anchor sits
    # deliberately far from the demand block (that is the bug being
    # regressed -- an off-parcel anchor near the buffered DEM's edge), so
    # with PRODUCTION_SERVICE_RADIUS_METERS at 25 m the first extension the
    # router finds already costs more per acre than it will pay and it
    # stops with no branch to check the clipping of. Freeing the ceiling
    # puts the branches back; every assertion below is about where they run.
    max_meters_per_served_acre=1e9,
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
# canopy_mask is a SOFT cost penalty threaded through build_cost_raster:
# a canopy band across the only path is still traversable, but raises the
# routed network's total cost vs. the same fixture with no canopy mask
# =====================================================================

canopy_dem = _flat_dem()
canopy_boundary = box(500000, 4500000, 500205, 4500205)
canopy_anchor = _lon_lat_for_cell(canopy_dem, 38, 2)
canopy_production_areas = [
    {"id": 0, "render_fill_polygon_utm": _cell_box_utm(canopy_dem, 0, 19, 0, 41)}
]  # north side, far from the (southern) anchor

# A canopy band directly between the anchor and the production zone,
# spanning the full parcel width -- every path to the demand must cross it.
canopy_band = np.zeros(canopy_dem["array"].shape, dtype=bool)
canopy_band[20:26, :] = True

# Same fixture routed with and without the canopy mask. max_meters_per_
# served_acre is raised (same reasoning as the floodplain test above) so
# the router reaches the north-side demand rather than stopping short.
canopy_network = build_road_network(
    canopy_dem, canopy_production_areas, None, canopy_boundary, canopy_anchor,
    canopy_mask=canopy_band, max_meters_per_served_acre=100_000.0,
)
canopy_free_network = build_road_network(
    canopy_dem, canopy_production_areas, None, canopy_boundary, canopy_anchor,
    canopy_mask=None, max_meters_per_served_acre=100_000.0,
)

assert canopy_network["branches"], (
    "a canopy band blocking the only path to the production zone must NOT prevent routing entirely "
    "-- it's a SOFT cost penalty, not a hard exclusion"
)
assert any(canopy_band[r, c] for r, c in canopy_network["cells"]), (
    "expected the routed network to actually pass through the canopy band, proving it's traversable"
)
canopy_total_cost = sum(b["total_cost"] for b in canopy_network["branches"])
canopy_free_total_cost = sum(b["total_cost"] for b in canopy_free_network["branches"])
assert canopy_total_cost > canopy_free_total_cost, (
    f"crossing the canopy band must cost strictly more than the same route with no canopy penalty "
    f"({canopy_total_cost} vs {canopy_free_total_cost}) -- proving canopy_mask actually feeds "
    "build_cost_raster()"
)
print("Canopy is a SOFT cost penalty wired through build_cost_raster(): the network still crosses a "
      "blocking canopy band, but at a strictly higher total cost than the same fixture with no canopy "
      "mask.")


# =====================================================================
# _fetch_canopy_soft_cost_mask(): DEGRADES GRACEFULLY on a canopy outage
# (returns None, does not raise), and otherwise passes a raw-canopy
# (buffer_meters=0.0) mask straight through
# =====================================================================

import road_corridors as _rc

_dummy_boundary = box(500000, 4500000, 500205, 4500205)
_dummy_dem = _flat_dem()
_orig_canopy_fn = _rc.get_required_tree_root_zone_mask_utm
try:
    def _raising_canopy_fetch(*args, **kwargs):
        # Same RuntimeError get_required_tree_root_zone_mask_utm() itself
        # raises when HAG coverage is missing -- production/solar treat this
        # as a hard failure; roads must NOT.
        raise RuntimeError("Canopy height data unavailable for this property")

    _rc.get_required_tree_root_zone_mask_utm = _raising_canopy_fetch
    degraded = _rc._fetch_canopy_soft_cost_mask(_dummy_boundary, _dummy_dem, canopy_height=None)
    assert degraded is None, f"a canopy outage must degrade to None (not raise, not a mask), got {degraded!r}"

    sentinel_mask = np.ones(_dummy_dem["array"].shape, dtype=bool)
    captured = {}

    def _ok_canopy_fetch(boundary, dem, buffer_meters=None, canopy_height=None):
        captured["buffer_meters"] = buffer_meters
        return sentinel_mask

    _rc.get_required_tree_root_zone_mask_utm = _ok_canopy_fetch
    passed_through = _rc._fetch_canopy_soft_cost_mask(_dummy_boundary, _dummy_dem, canopy_height=None)
    assert passed_through is sentinel_mask, "a successful canopy fetch must be passed straight through unchanged"
    assert captured["buffer_meters"] == 0.0, (
        f"roads must request RAW canopy (buffer_meters=0.0), not the root-zone buffer, got {captured['buffer_meters']}"
    )
finally:
    _rc.get_required_tree_root_zone_mask_utm = _orig_canopy_fn
print("_fetch_canopy_soft_cost_mask degrades gracefully to None on a canopy outage (does not raise), and "
      "passes a successful raw-canopy (buffer_meters=0.0) mask straight through.")


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

# =====================================================================
# SYNTHETIC STEEP-BAND reproduction of the real-parcel failure: a narrow
# ~22% band separating a gentle plateau (with the anchor) from the far
# plateau (with all the production demand). This is the exact shape that,
# with the old hard 15% grade ceiling, made every production cell
# unreachable and produced ZERO road branches; with MAX_ROAD_GRADE_PCT
# raised to a cliff-only 35% ceiling the band is costly but crossable.
# Runs entirely offline forever -- no DEM/NHD/SSURGO fetch of any kind.
# =====================================================================

_STEEP_RES = (10.0, 10.0)
_STEEP_ROWS, _STEEP_COLS = 5, 15
_STEEP_BAND_COL = 7            # a single-column band => a one-cell-thick wall
_STEEP_BAND_SLOPE = 22.0       # ~22% pitch: above the OLD 15% wall, below the new 35% cliff cutoff
_STEEP_ORIGIN_X, _STEEP_ORIGIN_Y = 500000.0, 4500000.0

# DEM: near plateau flat at 100m (cols < band), far plateau flat at 102.2m
# (cols >= band) -- the 2.2m rise falls entirely on the single 10m-wide
# band cell, so ONLY the segment entering that cell reads ~22% grade along
# the centerline; every other segment is dead flat.
_steep_array = np.full((_STEEP_ROWS, _STEEP_COLS), 100.0, dtype=np.float32)
_steep_array[:, _STEEP_BAND_COL:] = 102.2
_steep_dem = {
    "array": _steep_array, "resolution_meters": _STEEP_RES,
    "origin_x": _STEEP_ORIGIN_X, "origin_y": _STEEP_ORIGIN_Y, "crs": CRS,
}
# Per-cell slope raster supplied directly (so the band is EXACTLY the
# single column at 22% and nothing else) -- this is the same slope raster
# build_cost_raster()'s impassable_grade_pct is tested against below and
# the same one build_road_network()'s new cell-level metrics read from.
_steep_slope = np.zeros((_STEEP_ROWS, _STEEP_COLS), dtype=np.float32)
_steep_slope[:, _STEEP_BAND_COL] = _STEEP_BAND_SLOPE
# Uniform (zero) TPI so base travel cost is uniform and the least-cost
# route across the flat plateaus is a straight horizontal line -- makes the
# band cell the route crosses, and thus steep_meters, hand-computable.
_steep_tpi = np.zeros((_STEEP_ROWS, _STEEP_COLS), dtype=np.float64)
_steep_boundary = box(
    _STEEP_ORIGIN_X, _STEEP_ORIGIN_Y - _STEEP_ROWS * 10,
    _STEEP_ORIGIN_X + _STEEP_COLS * 10, _STEEP_ORIGIN_Y,
)

# --- reachability: the OLD 15% ceiling walls the far plateau off entirely;
#     the NEW 35% ceiling lets the route through (via cost_distance_field) ---
_steep_source = (2, 0)          # near plateau, centre row
_steep_far_cell = (2, 13)       # far plateau, same row
_steep_no_exclusion = np.zeros((_STEEP_ROWS, _STEEP_COLS), dtype=bool)

_cost_at_15 = build_cost_raster(_steep_dem, _steep_slope, _steep_no_exclusion, impassable_grade_pct=15.0)
_field_at_15 = cost_distance_field(_steep_dem, _cost_at_15, [_steep_source])
assert not np.isfinite(_field_at_15["accumulated_cost"][_steep_far_cell]), (
    "with impassable_grade_pct=15.0 the 22% band must HARD-exclude the only crossing, leaving the far "
    "plateau unreachable (accumulated_cost == inf) -- this is the exact real-parcel 0/993 failure"
)

_cost_at_35 = build_cost_raster(_steep_dem, _steep_slope, _steep_no_exclusion, impassable_grade_pct=MAX_ROAD_GRADE_PCT)
_field_at_35 = cost_distance_field(_steep_dem, _cost_at_35, [_steep_source])
assert np.isfinite(_field_at_35["accumulated_cost"][_steep_far_cell]), (
    f"with MAX_ROAD_GRADE_PCT={MAX_ROAD_GRADE_PCT} the 22% band is costly but PERMITTED, so the far "
    "plateau must be reachable (finite accumulated_cost)"
)
assert MAX_ROAD_GRADE_PCT > _STEEP_BAND_SLOPE > 15.0, (
    "the band must sit above the old 15% wall and below the new cliff cutoff for this test to mean anything"
)
print(
    f"Steep-band reachability: the 22% band is UNREACHABLE at a 15% ceiling (accumulated_cost == inf) "
    f"but REACHABLE at MAX_ROAD_GRADE_PCT={MAX_ROAD_GRADE_PCT} -- the real-parcel failure, reproduced offline."
)

# --- the resulting branch's cell-level metrics surface the pitch that its
#     gentle average hides ---
_steep_production = [
    {"id": 0, "render_fill_polygon_utm": box(
        _STEEP_ORIGIN_X + 10 * 10, _STEEP_ORIGIN_Y - _STEEP_ROWS * 10,
        _STEEP_ORIGIN_X + _STEEP_COLS * 10, _STEEP_ORIGIN_Y,
    )}
]  # far plateau (cols >= 10), reachable only by crossing the band

_steep_anchor = _lon_lat_for_cell(_steep_dem, 2, 0)  # near plateau, centre row
_steep_network = build_road_network(
    _steep_dem, _steep_production, None, _steep_boundary, _steep_anchor,
    slope_pct=_steep_slope, tpi=_steep_tpi,
    # small service radius so the anchor's own baseline coverage doesn't
    # already reach the far plateau (which would need no road at all), and a
    # huge per-acre ceiling so the router is free to build the crossing
    # rather than stopping short in this tiny fixture. There is no length
    # floor to disable any more.
    service_radius_meters=15.0, max_meters_per_served_acre=1e9,
)
assert _steep_network["branches"], (
    "with the 35% cliff ceiling a road network MUST be produced across the band -- the old 15% wall "
    "produced zero branches here, which is the whole bug"
)
_steep_trunk = next(b for b in _steep_network["branches"] if b["branch_role"] == "trunk")
# The trunk runs straight along row 2, crossing the single band cell (2, 7)
# horizontally exactly once.
assert (2, _STEEP_BAND_COL) in _steep_trunk["cells"], "the trunk must cross the band cell"
assert _steep_trunk["max_grade_pct"] > 15.0, (
    f"the trunk's steepest CELL ({_steep_trunk['max_grade_pct']}%) must exceed 15% -- it crosses the 22% band"
)
assert _steep_trunk["avg_grade_pct"] < 15.0, (
    f"the trunk's centerline AVERAGE grade ({_steep_trunk['avg_grade_pct']:.3f}%) must be below 15% -- a "
    "gentle overall route with one steep cell is exactly the case max_grade_pct/steep_meters exist to surface"
)
# Hand-computed steep_meters: the route crosses exactly ONE band cell, and
# it enters that cell on a straight horizontal step, so the steep length is
# one cell width == 10.0m (px). Nothing else along the route is steep.
_expected_steep_meters = 10.0
assert abs(_steep_trunk["steep_meters"] - _expected_steep_meters) < 1e-9, (
    f"steep_meters must equal the hand-computed length of the band cells crossed "
    f"({_expected_steep_meters}m: one 10m-wide band cell entered horizontally), got {_steep_trunk['steep_meters']}"
)
# Network-level rollup: steepest single cell across the whole network, and
# total steep length summed over every branch (only the trunk is steep).
assert _steep_network["max_grade_pct"] == _steep_trunk["max_grade_pct"]
assert abs(_steep_network["steep_meters"] - _expected_steep_meters) < 1e-9

# The same metrics reach the GeoJSON per feature (max_grade_pct, steep_ft).
_steep_geojson = corridors_to_geojson(_steep_network)
_trunk_feature = next(
    f for f in _steep_geojson["features"] if f["properties"]["branch_role"] == "trunk"
)
assert _trunk_feature["properties"]["max_grade_pct"] == round(_steep_trunk["max_grade_pct"], 1)
assert _trunk_feature["properties"]["steep_ft"] == round(_expected_steep_meters / 0.3048, 1)
print(
    f"Steep-band branch metrics: trunk avg_grade_pct={_steep_trunk['avg_grade_pct']:.2f}% (gentle) but "
    f"max_grade_pct={_steep_trunk['max_grade_pct']}% (steep), steep_meters={_steep_trunk['steep_meters']}m "
    f"(one band cell), exposed as GeoJSON max_grade_pct/steep_ft -- the low average does not hide the pitch."
)


# =====================================================================
# _terrain_quality_score() -- the normalized cost-per-meter ratio and its
# 0-100 score, HAND-DERIVED with no network routed at all. The function
# reads two plain floats off a network dict plus the DEM's own grid, so
# literals are the fully-controlled fixture; every expected value below is
# worked out from road_cost_path._BASE_TRAVEL_COST and the documented
# 100 - ratio * 10 scale by hand, never read back off the implementation.
# =====================================================================
from road_cost_path import _BASE_TRAVEL_COST  # noqa: E402

from road_corridors import _terrain_quality_ratio, _terrain_quality_score  # noqa: E402

assert _BASE_TRAVEL_COST == 1.0, (
    "every hand-derived expectation below is worked out against a base travel cost of "
    "1.0 cost-unit per meter; re-derive them if road_cost_path re-scales its own surface"
)

_TQ_DEM_5M = {"resolution_meters": (5.0, 5.0)}
_TQ_DEM_3M = {"resolution_meters": (3.0, 3.0)}

# 1. UNIFORM BASE-COST GROUND at 5 m. A network of 500.0 m over ground
#    costing exactly _BASE_TRAVEL_COST per meter accumulates 500.0 cost
#    (road_cost_path's Dijkstra weights each edge as
#    hypot(dc * px, dr * py) * cell cost -- real meters times a
#    cost-per-meter). ratio = (500.0 / 500.0) / 1.0 = 1.0 exactly, and
#    the score is the scale's own neutral-flat-ground anchor, 90.0.
assert _terrain_quality_ratio(500.0, 500.0) is not None
assert abs(_terrain_quality_ratio(500.0, 500.0) - 1.0) < 1e-9
assert _terrain_quality_score(500.0, 500.0, _TQ_DEM_5M) == 90.0

# 2. THE NORMALIZATION TEST -- the SAME ground at 3 m resolution. The
#    identical physical route over the identical uniform base-cost ground
#    accumulates the identical cost, because accumulated cost is scaled by
#    REAL METERS and not by cell count: 500 m of base-cost ground is 500.0
#    cost at 3 m exactly as at 5 m. The ratio and score must therefore be
#    IDENTICAL across the two resolutions. An implementation that divided
#    the base by min(px, py) -- normalizing as if cost were tallied per
#    cell edge -- would read ratio 5.0 here and 3.0 above, and fails this.
assert abs(_terrain_quality_ratio(500.0, 500.0) - 1.0) < 1e-9
assert _terrain_quality_score(500.0, 500.0, _TQ_DEM_3M) == 90.0
assert _terrain_quality_score(500.0, 500.0, _TQ_DEM_3M) == _terrain_quality_score(500.0, 500.0, _TQ_DEM_5M)

#    Same check on non-base ground, so the invariance is not an artifact
#    of the ratio being exactly 1: a floodplain crossing on flat ground
#    costs 1.0 + FLOODPLAIN_CROSSING_COST_PENALTY (5.0) = 6.0 per meter by
#    build_cost_raster()'s own arithmetic, which is the scale's own
#    ratio-6.0 anchor -> 100 - 60 = 40.0, at either resolution.
assert _terrain_quality_score(3000.0, 500.0, _TQ_DEM_5M) == 40.0
assert _terrain_quality_score(3000.0, 500.0, _TQ_DEM_3M) == 40.0

# 3. NO NETWORK -> None, NEVER 0.0. There is no road to score, and a 0.0
#    would read as "terrible ground" about ground nothing ran over.
_tq_no_network = _terrain_quality_score(0.0, 0.0, _TQ_DEM_5M)
assert _tq_no_network is None, f"no network must score None, got {_tq_no_network!r}"
assert not isinstance(_tq_no_network, float), "None, never a float 0.0 that reads as terrible ground"
assert _terrain_quality_score(0.0, 0, _TQ_DEM_5M) is None  # int 0 length, same answer
assert _terrain_quality_ratio(0.0, 0.0) is None

# 4. WORSE THAN THE SCALE'S BOTTOM -> clamped to 0.0, never negative.
#    ratio 12.0 (6000.0 / 500.0) would be 100 - 120 = -20 unclamped.
assert _terrain_quality_ratio(6000.0, 500.0) == 12.0
assert _terrain_quality_score(6000.0, 500.0, _TQ_DEM_5M) == 0.0
#    And far past it -- ratio 100.0 -- still 0.0, not a runaway negative.
assert _terrain_quality_score(50000.0, 500.0, _TQ_DEM_5M) == 0.0

# 5. BETTER THAN NEUTRAL FLAT GROUND -> above 90, and never above 100.
#    ratio 0.5 is the scale's own ridge/TPI-discount anchor: TPI discounts
#    a ridge cell to about 0.5x base (TPI_PREFERENCE_STRENGTH 0.5), which
#    is why ~95 is the realistic ceiling and 100 is not reachable by real
#    routed ground.
_tq_ridge = _terrain_quality_score(250.0, 500.0, _TQ_DEM_5M)
assert _terrain_quality_ratio(250.0, 500.0) == 0.5
assert _tq_ridge == 95.0 and 90.0 < _tq_ridge <= 100.0
#    The clamp's own top end: a ratio of 0 cannot arise from a real cost
#    raster (every finite cell is strictly positive), but the bound holds.
assert _terrain_quality_score(0.0, 500.0, _TQ_DEM_5M) == 100.0

# The remaining documented anchors, all worked from the cost surface's own
# arithmetic (GRADE_PENALTY_WEIGHT 0.0133 on grade percent squared):
#   10% sustained grade -> 1 + 0.0133 * 100  = 2.33 -> 100 - 23.3 = 76.7
#   15% sustained grade -> 1 + 0.0133 * 225  = 3.9925 -> 100 - 39.925 = 60.1
assert _terrain_quality_score(2.33 * 500.0, 500.0, _TQ_DEM_5M) == 76.7
assert _terrain_quality_score(3.9925 * 500.0, 500.0, _TQ_DEM_5M) == 60.1

# A DEM whose grid is not real ground is a programming error, not a score.
try:
    _terrain_quality_score(500.0, 500.0, {"resolution_meters": (0.0, 5.0)})
except ValueError as _exc:
    assert "resolution_meters" in str(_exc)
else:
    raise AssertionError("a non-positive cell size must raise, not silently score")

print(
    "_terrain_quality_score: uniform base-cost ground reads ratio 1.0 -> 90.0 at BOTH 5m and 3m "
    "(the normalization check -- a per-cell-edge normalization would read 5.0 and 3.0); the "
    "floodplain-on-flat anchor reads 6.0 -> 40.0 at both; no network returns None rather than "
    "0.0; ratio 12.0 clamps to 0.0 rather than -20; ratio 0.5 reads 95.0, above 90 and at or "
    "below 100; the 10%/15% sustained-grade anchors read 76.7/60.1."
)


# =====================================================================
# narrative_data -- build_narrative_data()'s own contract, against a
# hand-built network dict with clean, hand-checkable numbers (the
# builder reads only plain fields off build_road_network()'s shape, so a
# literal dict is the fully-controlled fixture). Entry-point wiring is
# checked in test_road_corridors_pipeline.py.
# =====================================================================
import json  # noqa: E402

from road_corridors import _empty_road_network, build_narrative_data  # noqa: E402

_nd_network = {
    "branches": [
        {
            "branch_index": 0, "branch_role": "trunk", "joins_branch_index": None,
            "length_meters": 152.4, "newly_served_acres": 9.0, "avg_grade_pct": 6.5,
            "max_grade_pct": 21.3, "steep_meters": 30.48,
            "crosses_floodplain": True, "crosses_production_zone": False,
        },
        {
            "branch_index": 1, "branch_role": "water_spur", "joins_branch_index": 0,
            "length_meters": 30.48, "newly_served_acres": 0.0, "avg_grade_pct": 4.0,
            "max_grade_pct": 9.0, "steep_meters": 0.0,
            "crosses_floodplain": False, "crosses_production_zone": True,
        },
    ],
    # THE NETWORK-LEVEL GRADE AVERAGE, LENGTH-WEIGHTED, as build_road_
    # network() publishes it: (152.4 * 6.5 + 30.48 * 4.0) / 182.88 =
    # 6.0833..., which the block rounds to 6.1. The naive mean of the two
    # branch averages is 5.25 -- asserted below to be a DIFFERENT number,
    # so this fixture cannot pass under a naive aggregation either.
    "avg_grade_pct": (152.4 * 6.5 + 30.48 * 4.0) / 182.88,
    # Crossing lengths in METRES, summed across the network, exactly as
    # build_road_network() publishes them; the block converts to feet.
    # 25.908 m = 85 ft, 36.576 m = 120 ft, 15.24 m = 50 ft.
    "crossing_meters": {"production": 25.908, "canopy": 36.576, "floodplain": 15.24},
    "total_length_meters": 182.88,  # 600 ft exactly
    # 2.5x base cost per meter over that length (182.88 * 2.5), chosen so
    # the quality block below is hand-checkable: ratio 2.5, score
    # 100 - 2.5 * 10 = 75.0.
    "total_cost": 457.2,
    "total_served_acres": 9.0,
    "unserved_acres": 3.0,
    "stop_reason": "diminishing_returns",
    "max_grade_pct": 21.3,
    "steep_meters": 30.48,  # 100 ft exactly
}
# The grid the network is nominally routed on. Read by build_narrative_data()
# only as _terrain_quality_score()'s own grid tripwire -- the resolution does
# not enter the ratio (see that function on why it cancels out of the cost
# raster's own edge weights).
_nd_dem = {"resolution_meters": (5.0, 5.0)}
_nd = build_narrative_data(
    _nd_network,
    service_radius_meters=60.96,  # 200 ft exactly
    water_zone_excluded=True,
    floodplain_data_available=True,
    floodplain_data_is_fallback=False,
    canopy_data_available=True,
    dem=_nd_dem,
)
assert json.loads(json.dumps(_nd)) == _nd, "narrative_data must be json.dumps()-clean with no custom encoder"
assert set(_nd) == {
    "network_found", "stop_reason", "determination", "access", "quality",
    "crossings", "scales", "branches",
}
assert _nd["network_found"] is True and _nd["stop_reason"] == "diminishing_returns"
assert _nd["determination"] == {
    "grade_ceiling_pct": 35.0,
    "steep_grade_threshold_pct": 10.0,
    "max_grade_pct": 21.3,
    "avg_grade_pct": 6.1,  # length-weighted; the naive mean of 6.5 and 4.0 is 5.25
    "steep_ft": 100.0,
    "water_zone_excluded": True,
    "floodplain_data_available": True,
    "floodplain_data_is_fallback": False,
    "canopy_data_available": True,
}
# THE WEIGHTING IS VISIBLE IN THE PUBLISHED NUMBER, not only in the
# helper: a naive mean of the two branch averages would have put 5.25 on
# the wire here.
assert _nd["determination"]["avg_grade_pct"] != round((6.5 + 4.0) / 2, 1)
assert _nd["access"] == {
    "branch_count": 2,
    "total_length_ft": 600.0,
    "served_acres": 9.0,
    "unserved_acres": 3.0,
    "served_pct_of_production": 75.0,  # 9 of 12 demand acres in range
    "service_radius_ft": 200.0,
    "reaches_water_zone": True,  # the water_spur branch
}
# QUESTION 3, its own top-level key -- never folded into 'determination'
# (HOW the route was determined) or 'access' (HOW MUCH ACCESS it provides).
assert _nd["quality"] == {
    "terrain_quality_score": 75.0,  # 100 - 2.5 * 10
    "cost_per_meter_ratio": 2.5,    # 457.2 / 182.88 / _BASE_TRAVEL_COST
    "total_path_cost": 457.2,
}
assert "terrain_quality_score" not in _nd["determination"] and "terrain_quality_score" not in _nd["access"], (
    "terrain quality is its own question and its own block -- folding it into either "
    "existing block would answer a different question than that block's own"
)

# QUESTION 4 -- crossing LENGTHS, summed across the network, in feet, and
# named for what the interface calls the ground: BLOCK, not production
# zone. 25.908 m -> 85.0 ft, 36.576 m -> 120.0 ft, 15.24 m -> 50.0 ft.
assert _nd["crossings"] == {
    "crosses_block_ft": 85.0,
    "crosses_canopy_ft": 120.0,
    "crosses_floodplain_ft": 50.0,
}
assert "crosses_production_zone_ft" not in _nd["crossings"], (
    "the panel-facing key says BLOCK -- the interface's own word for a production zone"
)
# NO PER-BRANCH DATA IN THE PANEL BLOCK, and the per-branch booleans are
# untouched by it: 'crossings' describes the whole network and nothing else.
assert set(_nd["crossings"]) == {
    "crosses_block_ft", "crosses_canopy_ft", "crosses_floodplain_ft"
}

# THE SCALE FOR EVERY SCORED VALUE, on the wire -- production_area_
# ceiling's rule, which the terrain quality score had been crossing the
# wire without. Range and direction first, then the load-bearing fact:
# the score is normalized against an ABSOLUTE base (road_cost_path's own
# base travel cost per metre), NOT against anything about this parcel, so
# the same score means the same thing on two different properties.
_scales = _nd["scales"]
assert set(_scales) == {"terrain_quality_score", "cost_per_meter_ratio", "crossings"}
_tq_scale = _scales["terrain_quality_score"]
assert _tq_scale["range"] == [0.0, 100.0]
assert _tq_scale["direction"] == "higher_is_better"
assert _tq_scale["parcel_relative"] is False, (
    "the terrain quality score normalizes against a CONSTANT of the cost surface, not "
    "against this parcel's own observed range -- a reader must be able to compare two "
    "properties' scores, which water's parcel_observed_max scale explicitly cannot"
)
assert _tq_scale["normalizer_cost_per_meter"] == _BASE_TRAVEL_COST
assert _tq_scale["clamps_at_ratio"] == 10.0 and _tq_scale["practical_max"] == 95.0
assert _tq_scale["claim"] == "relative_screening_value"
# EVERY ANCHOR IS THE FUNCTION'S OWN ARITHMETIC, not a table that can
# drift away from it: each row is recomputed through _terrain_quality_
# score() at a length of 1.0 m, where total_cost IS the ratio.
for _anchor in _tq_scale["anchors"]:
    assert _terrain_quality_score(_anchor["ratio"], 1.0, _TQ_DEM_5M) == _anchor["score"], _anchor
assert _scales["cost_per_meter_ratio"]["direction"] == "lower_is_better", (
    "the ratio and the score run in OPPOSITE directions -- exactly the confusion an "
    "undeclared scale causes, so each declares its own"
)
assert _scales["cost_per_meter_ratio"]["neutral"] == 1.0
assert _scales["crossings"]["zero_means"] == "measured_crossed_none"
assert _scales["crossings"]["null_means"] == "ground_data_unavailable_not_measured"
assert _nd["branches"][0] == {
    "branch_index": 0, "role": "trunk", "joins_branch_index": None, "length_ft": 500.0,
    "newly_served_acres": 9.0, "avg_grade_pct": 6.5, "max_grade_pct": 21.3, "steep_ft": 100.0,
    "crosses_floodplain": True, "crosses_production_zone": False,
}
assert _nd["branches"][1]["role"] == "water_spur" and _nd["branches"][1]["length_ft"] == 100.0
assert _nd["branches"][1]["joins_branch_index"] == 0

# No-network shapes: with NO production demand at all, served_pct is None
# (nothing to serve is not a measured 0% coverage); with real unserved
# demand it is a real 0.0.
_nd_empty = build_narrative_data(
    _empty_road_network("no_eligible_anchor"),
    service_radius_meters=60.96, water_zone_excluded=False,
    floodplain_data_available=False, floodplain_data_is_fallback=False,
    canopy_data_available=False, dem=_nd_dem,
)
assert _nd_empty["network_found"] is False and _nd_empty["branches"] == []
assert _nd_empty["access"]["served_pct_of_production"] is None
assert _nd_empty["access"]["reaches_water_zone"] is False
# NO NETWORK -> NO SCORE, never a 0.0. A 0.0 would read as "terrible
# ground" about ground nothing was ever routed over.
assert _nd_empty["quality"] == {
    "terrain_quality_score": None,
    "cost_per_meter_ratio": None,
    "total_path_cost": 0.0,
}
# ZERO METRES OF ROAD CROSS ZERO METRES OF ANYTHING -- a measured 0.0 on
# every ground, needing no mask to have existed, and the same shape
# steep_ft already reports for an empty network.
assert _nd_empty["crossings"] == {
    "crosses_block_ft": 0.0, "crosses_canopy_ft": 0.0, "crosses_floodplain_ft": 0.0
}
# The grade average follows max_grade_pct's own convention rather than
# introducing a lone None beside it; network_found is the guard for both.
assert _nd_empty["determination"]["avg_grade_pct"] == 0.0
assert _nd_empty["determination"]["max_grade_pct"] == 0.0
# The scale block describes the INSTRUMENT, not the reading -- it ships
# identically whether or not there is a network to score.
assert _nd_empty["scales"] == _nd["scales"]
_nd_unserved = build_narrative_data(
    _empty_road_network("no_eligible_anchor", unserved_acres=3.0),
    service_radius_meters=60.96, water_zone_excluded=False,
    floodplain_data_available=False, floodplain_data_is_fallback=False,
    canopy_data_available=False, dem=_nd_dem,
)
assert _nd_unserved["access"]["served_pct_of_production"] == 0.0
print(
    "narrative_data: json-clean; determination (35% ceiling, 21.3% steepest cell, 100 steep ft, all "
    "constraint flags passed through) and access (600 ft network serving 9.0 of 12.0 demand acres = "
    "75.0%, water spur reaches the pond) all match hand-checked values; quality is its own third "
    "key (ratio 2.5 -> score 75.0, raw cost 457.2 carried for diagnostics); no-demand empty network "
    "reports served_pct None and a None terrain score, real unserved demand reports 0.0."
)
print(
    "narrative_data, the panel's own additions: determination carries the network's LENGTH-WEIGHTED "
    "avg_grade_pct (6.1, not the naive 5.25 mean of its 6.5 and 4.0 branches); 'crossings' is a "
    "fourth top-level block of network-summed LENGTHS in feet (block 85.0, canopy 120.0, floodplain "
    "50.0) with no per-branch data in it and BLOCK as the panel's word for a production zone; "
    "'scales' ships the terrain score's range, direction, the absolute base it normalizes against "
    "(parcel_relative False) and every anchor, each recomputed through _terrain_quality_score() "
    "rather than transcribed."
)


# =====================================================================
# NETWORK-LEVEL AGGREGATION -- one figure for a whole TREE of branches,
# and the reduction stated for each. A network's branches have different
# lengths, grades and roles, so "the network's grade" is a choice, not a
# read: max is a MAX (the steepest point anywhere), average is
# LENGTH-WEIGHTED (the grade of the average metre), and the terrain score
# is already length-weighted by construction.
# =====================================================================
from road_corridors import _length_weighted_avg_grade_pct, _mask_crossing_meters  # noqa: E402

# THE CASE A NAIVE AGGREGATION GETS WRONG, built so the two answers are
# nowhere near each other: ONE LONG GENTLE BRANCH and ONE SHORT STEEP
# ONE. 900 ft of 3% trunk and a 40 ft 20% stub.
_agg_branches = [
    {"length_meters": 274.32, "avg_grade_pct": 3.0},   # 900 ft, gentle
    {"length_meters": 12.192, "avg_grade_pct": 20.0},  # 40 ft, steep
]
_naive_mean = (3.0 + 20.0) / 2                                   # 11.5
_weighted = (274.32 * 3.0 + 12.192 * 20.0) / (274.32 + 12.192)   # 3.7233...
assert round(_naive_mean, 1) == 11.5 and round(_weighted, 1) == 3.7
assert abs(_naive_mean - _weighted) > 7.0, (
    "this fixture only proves something if the naive and weighted means differ SUBSTANTIALLY -- "
    "a fixture where they happen to agree would pass under either aggregation"
)
assert abs(_length_weighted_avg_grade_pct(_agg_branches) - _weighted) < 1e-9
assert round(_length_weighted_avg_grade_pct(_agg_branches), 1) != round(_naive_mean, 1), (
    "a 40 ft stub must not get the same vote as a 900 ft trunk"
)
# The order of the branches cannot matter, and neither can the trunk
# being first: a weighted mean is a weighted mean.
assert _length_weighted_avg_grade_pct(_agg_branches) == _length_weighted_avg_grade_pct(
    list(reversed(_agg_branches))
)
# Degenerate: no branches, and branches whose lengths sum to zero, both
# report 0.0 rather than raising on the division.
assert _length_weighted_avg_grade_pct([]) == 0.0
assert _length_weighted_avg_grade_pct([{"length_meters": 0.0, "avg_grade_pct": 9.0}]) == 0.0

# THE SAME CASE THROUGH THE WHOLE PUBLICATION PATH, network dict -> wire,
# so the weighting is asserted where a consumer actually reads it and not
# only in the helper.
_agg_network = {
    "branches": [
        {
            "branch_index": 0, "branch_role": "trunk", "joins_branch_index": None,
            "length_meters": 274.32, "newly_served_acres": 6.0, "avg_grade_pct": 3.0,
            "max_grade_pct": 5.0, "steep_meters": 0.0,
            "crosses_floodplain": False, "crosses_production_zone": False,
        },
        {
            "branch_index": 1, "branch_role": "spur", "joins_branch_index": 0,
            "length_meters": 12.192, "newly_served_acres": 0.4, "avg_grade_pct": 20.0,
            # THE STEEPEST POINT ANYWHERE IN THE NETWORK sits on this
            # 40 ft stub -- 4.3% of the network's length. A max that
            # diluted itself by length would lose it entirely.
            "max_grade_pct": 26.0, "steep_meters": 12.192,
            "crosses_floodplain": False, "crosses_production_zone": False,
        },
    ],
    "total_length_meters": 286.512,
    "total_cost": 286.512 * 1.5,
    "total_served_acres": 6.4,
    "unserved_acres": 0.0,
    "stop_reason": "all_demand_served",
    "max_grade_pct": 26.0,
    "avg_grade_pct": _weighted,
    "steep_meters": 12.192,
    "crossing_meters": {"production": 0.0, "canopy": 0.0, "floodplain": 0.0},
}
_agg_nd = build_narrative_data(
    _agg_network, service_radius_meters=60.96, water_zone_excluded=False,
    floodplain_data_available=True, floodplain_data_is_fallback=False,
    canopy_data_available=True, dem=_nd_dem,
)
assert _agg_nd["determination"]["avg_grade_pct"] == 3.7, _agg_nd["determination"]["avg_grade_pct"]
assert _agg_nd["determination"]["avg_grade_pct"] != round(_naive_mean, 1)
# MAX GRADE IS THE STEEPEST POINT ANYWHERE, not a length-weighted
# anything: the 26% cell on a 40 ft stub reaches the wire intact.
assert _agg_nd["determination"]["max_grade_pct"] == 26.0
assert _agg_nd["determination"]["max_grade_pct"] == max(
    b["max_grade_pct"] for b in _agg_network["branches"]
)
assert _agg_nd["determination"]["max_grade_pct"] > _agg_nd["determination"]["avg_grade_pct"] * 5

print(
    "network-level grade aggregation: average is LENGTH-WEIGHTED -- a 900 ft 3% trunk with a 40 ft "
    "20% stub publishes 3.7%, where a naive mean of the branch averages would publish 11.5%; max "
    "grade is the steepest point ANYWHERE (26.0% on that same 4%-of-length stub) and is never "
    "diluted by the gentle road around it."
)


# =====================================================================
# THE SCORE'S OWN REDUCTION -- _terrain_quality_score() is called once on
# the network's summed cost and length, which IS the length-weighted mean
# of the branches' costs per metre. Asserted against the alternative it
# is not: a mean of per-branch scores.
# =====================================================================
_red_branches = [
    {"length_meters": 900.0, "total_cost": 900.0 * 1.2},   # long, good ground: ratio 1.2
    {"length_meters": 40.0, "total_cost": 40.0 * 8.0},     # short, terrible ground: ratio 8.0
]
_red_length = sum(b["length_meters"] for b in _red_branches)
_red_cost = sum(b["total_cost"] for b in _red_branches)
_red_score = _terrain_quality_score(_red_cost, _red_length, _TQ_DEM_5M)
_red_weighted_ratio = sum(
    (b["total_cost"] / b["length_meters"]) * b["length_meters"] for b in _red_branches
) / _red_length
assert abs(_red_cost / _red_length - _red_weighted_ratio) < 1e-9, (
    "summing both halves before dividing IS length-weighting -- these are the same number"
)
_red_naive_score = sum(
    _terrain_quality_score(b["total_cost"], b["length_meters"], _TQ_DEM_5M) for b in _red_branches
) / len(_red_branches)
assert _red_score == 85.1 and round(_red_naive_score, 1) == 54.0, (_red_score, _red_naive_score)
assert _red_score != round(_red_naive_score, 1), (
    "a mean of per-branch scores would let a 40 m patch of terrible ground halve the score of a "
    "940 m network -- the reduction is length-weighted, which is what summing before dividing does"
)
print(
    "terrain score reduction: the score is computed ONCE on the network's summed cost and length, "
    "which is already the LENGTH-WEIGHTED mean of the branches' costs per metre (85.1 for a 900 m "
    "ratio-1.2 trunk plus a 40 m ratio-8.0 stub); a mean of per-branch scores would have published "
    "54.0 for the same network."
)


# =====================================================================
# CROSSING LENGTHS -- summed across the whole network, one figure per
# ground, 0.0 and None kept apart
# =====================================================================

# A route that must cross TWO SEPARATE production blocks. The anchor sits
# at one corner and the demand is split into two disjoint column bands,
# so the network runs through both -- the case the "one figure, not
# three" rule is about.
_xdem = _flat_dem()
_xboundary = box(500000, 4500000, 500205, 4500205)
_xanchor = _lon_lat_for_cell(_xdem, 38, 2)
_xblock_a_poly = _cell_box_utm(_xdem, 0, 41, 4, 12)
_xblock_b_poly = _cell_box_utm(_xdem, 0, 41, 20, 30)
_xnetwork = build_road_network(
    _xdem,
    [
        {"id": 0, "render_fill_polygon_utm": _xblock_a_poly},
        {"id": 1, "render_fill_polygon_utm": _xblock_b_poly},
    ],
    None,
    _xboundary,
    _xanchor,
    # An all-False canopy mask: canopy data DID arrive and marks nothing
    # this route crosses. That is the 0.0-versus-None case, side by side
    # with floodplain, whose union was never supplied at all.
    canopy_mask=np.zeros(_xdem["array"].shape, dtype=bool),
)
assert _xnetwork["branches"], "the two-block fixture must route a network or it asserts nothing"

_xboundary_prepared = prep(_xboundary)
_xmask_a = _build_production_cell_mask(_xdem, prep(_xblock_a_poly), _xboundary_prepared)
_xmask_b = _build_production_cell_mask(_xdem, prep(_xblock_b_poly), _xboundary_prepared)
assert not (_xmask_a & _xmask_b).any(), "the two blocks must be disjoint or the sum below is not a sum"

# Measured per block, independently, with the same new-construction
# bookkeeping build_road_network() uses.
_x_per_block = {"a": 0.0, "b": 0.0}
_x_built: set = set()
for _xbranch in _xnetwork["branches"]:
    _xcells = _xbranch["cells"]
    _x_per_block["a"] += _mask_crossing_meters(_xdem, _xcells, _xmask_a, _x_built)
    _x_per_block["b"] += _mask_crossing_meters(_xdem, _xcells, _xmask_b, _x_built)
    _x_built.update(_xcells)
assert _x_per_block["a"] > 0.0 and _x_per_block["b"] > 0.0, (
    f"the network must actually cross BOTH blocks for this to be the two-block case: {_x_per_block}"
)

# ONE FIGURE, EQUAL TO THEIR SUM -- not one per block.
assert abs(_xnetwork["crossing_meters"]["production"] - (_x_per_block["a"] + _x_per_block["b"])) < 1e-9, (
    _xnetwork["crossing_meters"]["production"], _x_per_block
)
# And it is a sum over the BRANCHES too, not a re-measurement of one of them.
assert abs(
    _xnetwork["crossing_meters"]["production"]
    - sum(b["crossing_meters"]["production"] for b in _xnetwork["branches"])
) < 1e-9
# A crossing length can never exceed the road that does the crossing.
assert _xnetwork["crossing_meters"]["production"] <= _xnetwork["total_length_meters"] + 1e-9

# ZERO IS DISTINGUISHABLE FROM NULL, in the same network dict: canopy
# data arrived and marked nothing crossed (a measured 0.0), floodplain
# data never arrived at all (None).
assert _xnetwork["crossing_meters"]["canopy"] == 0.0
assert _xnetwork["crossing_meters"]["canopy"] is not None
assert _xnetwork["crossing_meters"]["floodplain"] is None
_xnd = build_narrative_data(
    _xnetwork, service_radius_meters=60.96, water_zone_excluded=False,
    floodplain_data_available=False, floodplain_data_is_fallback=False,
    canopy_data_available=True, dem=_xdem,
)
assert _xnd["crossings"]["crosses_canopy_ft"] == 0.0
assert _xnd["crossings"]["crosses_floodplain_ft"] is None
assert _xnd["crossings"]["crosses_block_ft"] > 0.0
assert _xnd["determination"]["canopy_data_available"] is True
assert _xnd["determination"]["floodplain_data_available"] is False, (
    "the *_data_available flags are what say which ground a None belongs to"
)

# A NETWORK THAT CROSSES NOTHING AT ALL reports 0.0 on every ground it
# had a mask for -- same fixture, production demand moved far from the
# route's own corridor is not reliably arrangeable, so this measures the
# masks directly: an all-False mask over the real routed cells is 0.0,
# never None, and a None mask is None, never 0.0.
_xnone_built: set = set()
_xall_false = np.zeros(_xdem["array"].shape, dtype=bool)
for _xbranch in _xnetwork["branches"]:
    assert _mask_crossing_meters(_xdem, _xbranch["cells"], _xall_false, _xnone_built) == 0.0
    assert _mask_crossing_meters(_xdem, _xbranch["cells"], None, _xnone_built) is None
    _xnone_built.update(_xbranch["cells"])

print(
    f"crossing lengths: a network crossing TWO separate blocks reports ONE figure "
    f"({_xnetwork['crossing_meters']['production']:.2f} m) equal to the sum of the two measured "
    f"separately ({_x_per_block['a']:.2f} + {_x_per_block['b']:.2f}), never one figure per block, "
    f"and never more than the network's own length; a ground whose mask existed and marked nothing "
    f"reports a measured 0.0 while a ground with no mask at all reports None, in the same dict."
)


print("\nAll road_corridors checks passed.")
