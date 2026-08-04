"""
test_road_corridors.py

Offline (no-network) checks for road_corridors.py's ridge-line
identification/pruning/scoring/selection pipeline — hand-built synthetic
DEMs and hand-built production areas/selected water zone (same shapes
find_road_routes() actually consumes: production areas carry
'render_fill_polygon_utm', the OPTIMIZED/final production geometry
production_area_ceiling.py produces, and the water zone is the single
SELECTED zone water_suitability.py's own scoring picks, not a list of
unscored candidates), not a real DEM/NHD/SSURGO fetch. Mirrors
test_water_candidate_zones.py's and test_solar_suitability.py's "pure
logic, independent of real data fetches" approach.

Route generation now identifies genuine ridge lines from the DEM (D8
flow accumulation against an inverted DEM — see road_corridors.py's own
module docstring and _identify_ridge_cell_mask()), prunes them against
the HARD water/boundary exclusions (and drops any surviving fragment
shorter than RIDGE_MIN_FRAGMENT_CELLS outright), scores the survivors on
a slope/length/production-crossing weighted formula (no separate
proximity-to-anchor term — see road_corridors.py's own
_score_ridge_candidates() docstring for why that was redundant with
road_cost_path.least_cost_path()'s own total_cost), and routes a single
least-cost connector from the anchor to the single highest-scoring
candidate. This replaced an earlier "three fan-direction destinations
from the anchor" design — there is no more 'fan_direction' field, no
_select_fan_destination_cells()/_farthest_intersection_point()/
_snap_utm_point_to_eligible_cell(), and find_road_routes() now returns
AT MOST ONE route, not a ranked list of up to three. k_shortest_paths()
itself is untouched and still exported by road_cost_path.py, just not
called from this module. See road_corridors.py's own module docstring
for the current constraint-stack split (water + boundary HARD-excluded
from ridge candidates outright; production is a HARD exclusion for the
anchor-to-ridge connector only, but a SOFT, cell-based scoring term
against the ridge fragment itself; floodplain + grade stay SOFT cost
penalties on the anchor-to-ridge connector only).
"""

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, box
from shapely.prepared import prep

from feature_schema import validate_feature_collection
from raster_grid import D8_OFFSETS, connected_components, pixel_center_xy
from road_corridors import (
    RIDGE_LENGTH_WEIGHT,
    RIDGE_MIN_AREA_ACRES,
    RIDGE_MIN_FRAGMENT_CELLS,
    RIDGE_PRODUCTION_WEIGHT,
    RIDGE_SLOPE_WEIGHT,
    STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT,
    _build_production_cell_mask,
    _identify_ridge_cell_mask,
    _order_fragment_from_entry,
    _prune_ridge_networks,
    _score_ridge_candidates,
    _snap_anchor_to_eligible_cell,
    corridors_to_geojson,
    find_road_routes,
    identify_road_corridor_candidates,
)
from road_cost_path import build_cost_raster
from terrain_metrics import compute_slope_and_aspect

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)
RIDGE_RESOLUTION = (10.0, 10.0)  # coarser cells so a modest grid clears RIDGE_MIN_AREA_ACRES quickly


def _flat_dem(size=41, origin_x=500000.0, origin_y=4500205.0, elevation=100.0):
    """A perfectly flat DEM -- every cell the same elevation, so slope is
    0 everywhere the Horn kernel can compute it at all (the outer 1-cell
    ring stays NaN regardless -- compute_slope_and_aspect() never visits
    the border, see terrain_metrics.py). Useful whenever a test wants
    grade entirely out of the way. NOTE: a flat DEM has no relief at all,
    so it has no ridge line either -- see the dedicated "no relief, no
    ridge" regression test below."""
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


def _ridge_dem(size=41, peak_col=20, drop_per_col=1.0, origin_x=500000.0, origin_y=4500410.0):
    """A single hip/gable-style ridge running the full north-south extent
    of the DEM at column `peak_col` -- elevation is highest at that column
    and drops off linearly (at drop_per_col per column) on either side,
    constant along every row. In the INVERTED DEM (see road_corridors.
    _invert_dem()), this is a single monotonic bowl with its low point
    along the same column -- flow converges there from both sides within
    every row, with no row-to-row mixing (elevation doesn't vary by row at
    all), so accumulation at (row, peak_col) is exactly `size` (every
    column's own cell in that row) for EVERY row, and accumulation one
    column off-center is exactly `size // 2` (only that side's own chain)
    -- comfortably straddling RIDGE_MIN_AREA_ACRES at RIDGE_RESOLUTION
    (10m cells): size=41 columns * 0.0247105 ac/cell = 1.013 ac at the
    peak column, vs 20 * 0.0247105 = 0.494 ac one column off -- so
    _identify_ridge_cell_mask() should recover EXACTLY the peak column,
    no more and no less. See the dedicated test below, which asserts this
    exact mask rather than just "some ridge cells exist somewhere"."""
    array = np.zeros((size, size), dtype=np.float32)
    cols = np.arange(size)
    row_profile = 100.0 - np.abs(cols - peak_col) * drop_per_col
    array[:, :] = row_profile[np.newaxis, :]
    return {
        "array": array.astype(np.float32),
        "resolution_meters": RIDGE_RESOLUTION,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "crs": CRS,
    }


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


# =====================================================================
# _identify_ridge_cell_mask: recovers a real ridge line from D8 flow
# accumulation against an inverted DEM
# =====================================================================

ridge_dem = _ridge_dem()
ridge_mask = _identify_ridge_cell_mask(ridge_dem)

expected_ridge_mask = np.zeros((41, 41), dtype=bool)
expected_ridge_mask[:, 20] = True
assert np.array_equal(ridge_mask, expected_ridge_mask), (
    "expected the ridge mask to be True EXACTLY at the peak column (all 41 rows) and False everywhere "
    "else, given the hand-derived flow-accumulation math in _ridge_dem()'s own docstring"
)

ridge_labels, ridge_num_networks = connected_components(ridge_mask)
assert ridge_num_networks == 1, f"expected the single straight ridge column to form ONE connected network, got {ridge_num_networks}"
assert int((ridge_labels == 0).sum()) == 41
print(
    "_identify_ridge_cell_mask correctly recovers the exact ridge column from D8 flow accumulation "
    "against the inverted DEM, forming a single connected network of the expected size."
)


# --- a perfectly flat DEM has no relief, and therefore no ridge at all ---

flat_ridge_mask = _identify_ridge_cell_mask(_flat_dem())
assert not flat_ridge_mask.any(), "a flat DEM has no relief at all -- it must not produce any ridge cells"
no_relief_routes = find_road_routes(_flat_dem(), [], None, box(500000, 4500000, 500205, 4500205), _lon_lat_for_cell(_flat_dem(), 38, 2))
assert no_relief_routes == [], (
    "with no ridge candidates identified at all on a flat DEM, find_road_routes() must report the real "
    "'no route' outcome ([]), not fabricate a route from nothing"
)
print("A flat DEM (no relief) correctly produces no ridge cells and no road route at all.")


# =====================================================================
# _prune_ridge_networks: strip hard-excluded cells out of each original
# ridge network, keep only the single largest surviving fragment per
# original network
# =====================================================================

# One straight-line network, split by an excluded band into an asymmetric
# top (5 cells) / bottom (10 cells) pair -- the larger (bottom) survives.
single_network_mask = np.zeros((41, 41), dtype=bool)
single_network_mask[:, 20] = True
split_excluded = np.zeros((41, 41), dtype=bool)
split_excluded[5:31, 20] = True  # carve out rows 5-30, leaving rows 0-4 (5) and 31-40 (10)

pruned = _prune_ridge_networks(single_network_mask, split_excluded)
assert len(pruned) == 1, f"expected exactly one surviving candidate from the one original network, got {len(pruned)}"
expected_surviving_fragment = [(r, 20) for r in range(31, 41)]
assert sorted(pruned[0]) == sorted(expected_surviving_fragment), (
    f"expected the LARGER (10-cell, rows 31-40) fragment to survive over the smaller (5-cell, rows 0-4) "
    f"one, got {sorted(pruned[0])}"
)
print("_prune_ridge_networks keeps only the largest surviving fragment when an exclusion severs a ridge network.")

# A network entirely inside the exclusion contributes no candidate at all.
fully_excluded = np.ones((41, 41), dtype=bool)
assert _prune_ridge_networks(single_network_mask, fully_excluded) == [], (
    "a ridge network with every one of its own cells hard-excluded must contribute NO candidate at all"
)
print("_prune_ridge_networks correctly drops a network that's entirely hard-excluded.")

# Two independent original networks each produce their OWN candidate.
two_networks_mask = np.zeros((41, 41), dtype=bool)
two_networks_mask[:, 5] = True   # network A, full column
two_networks_mask[:, 35] = True  # network B, full column (far enough away to stay a separate component)
no_exclusion = np.zeros((41, 41), dtype=bool)
two_pruned = _prune_ridge_networks(two_networks_mask, no_exclusion)
assert len(two_pruned) == 2, f"expected one candidate per original network (2 independent columns), got {len(two_pruned)}"
pruned_columns = sorted({cells[0][1] for cells in two_pruned})
assert pruned_columns == [5, 35], f"expected candidates at columns 5 and 35, got {pruned_columns}"
for cells in two_pruned:
    assert len(cells) == 41, "an unexcluded network should survive completely intact, all 41 cells"
print("_prune_ridge_networks returns one candidate per original, independent ridge network, each fully intact when unexcluded.")

# --- RIDGE_MIN_FRAGMENT_CELLS floor: a fragment shorter than this is dropped outright, whether it started that short or got cut down to it ---

# A genuinely tiny original network (6 cells, no exclusion involved at
# all -- the live bug this floor fixes: even absent any pruning, a
# 6-7 cell ridge could still surface as the winning "road corridor").
tiny_network_mask = np.zeros((41, 41), dtype=bool)
tiny_network_mask[10:16, 20] = True  # 6 cells, below RIDGE_MIN_FRAGMENT_CELLS (10)
assert _prune_ridge_networks(tiny_network_mask, no_exclusion) == [], (
    "a genuinely tiny original network (6 cells, RIDGE_MIN_FRAGMENT_CELLS is 10) must be dropped outright, "
    "even with zero exclusion involved -- it's just not a meaningful road-worthy landform"
)

# A network that starts comfortably above the floor but gets severed by
# an exclusion down to a largest-surviving-fragment BELOW the floor.
severed_below_floor_excluded = np.zeros((41, 41), dtype=bool)
severed_below_floor_excluded[6:41, 20] = True  # leaves only rows 0-5 (6 cells) of the 41-cell single_network_mask
assert _prune_ridge_networks(single_network_mask, severed_below_floor_excluded) == [], (
    "a network severed down to a 6-cell largest surviving fragment (below RIDGE_MIN_FRAGMENT_CELLS) must "
    "be dropped, exactly like a genuinely tiny original network -- the floor applies AFTER pruning, to "
    "whatever fragment actually survives"
)

# Exactly at the floor (10 cells, the split_excluded case above) still
# survives -- RIDGE_MIN_FRAGMENT_CELLS is a "shorter than" cutoff, not
# "shorter than or equal to."
assert len(_prune_ridge_networks(single_network_mask, split_excluded)[0]) == 10 == RIDGE_MIN_FRAGMENT_CELLS
print(
    f"_prune_ridge_networks drops any surviving fragment shorter than RIDGE_MIN_FRAGMENT_CELLS "
    f"({RIDGE_MIN_FRAGMENT_CELLS} cells) -- whether it started that short or got cut down to it by "
    f"exclusion -- while a fragment exactly at the floor still survives."
)


# =====================================================================
# _order_fragment_from_entry: orders an unordered fragment cell set into
# a single entry-to-far-end path
# =====================================================================

straight_fragment = [(10, 10), (10, 11), (10, 12), (10, 13), (10, 14)]
straight_path = _order_fragment_from_entry(straight_fragment, (10, 10))
assert straight_path == straight_fragment, f"expected the straight-line fragment ordered start-to-end, got {straight_path}"
print("_order_fragment_from_entry correctly orders a simple straight-line fragment from its entry cell.")

# A branching fragment: a vertical spine with a horizontal spur -- verify
# structural correctness (valid D8 path, no repeats, every cell real)
# and that the endpoint reached is a genuine farthest cell, computed via
# an INDEPENDENT brute-force BFS in this test (own implementation, not a
# call into the function under test) since 8-connected adjacency between
# the spine and the spur makes hand-computing exact hop distances
# error-prone to get right by inspection.
branch_fragment = (
    [(5, 20), (6, 20), (7, 20), (8, 20), (9, 20)]  # vertical spine
    + [(7, 16), (7, 17), (7, 18), (7, 19), (7, 21), (7, 22), (7, 23), (7, 24)]  # horizontal spur off row 7
)
branch_entry = (5, 20)
branch_set = set(branch_fragment)


def _brute_force_bfs_distances(cells: set, root: tuple[int, int]) -> dict:
    from collections import deque as _deque

    dist = {root: 0}
    queue = _deque([root])
    while queue:
        cur = queue.popleft()
        cr, cc = cur
        for dr, dc in D8_OFFSETS:
            nbr = (cr + dr, cc + dc)
            if nbr in cells and nbr not in dist:
                dist[nbr] = dist[cur] + 1
                queue.append(nbr)
    return dist


branch_distances = _brute_force_bfs_distances(branch_set, branch_entry)
max_distance = max(branch_distances.values())
farthest_cells = {cell for cell, d in branch_distances.items() if d == max_distance}

branch_path = _order_fragment_from_entry(branch_fragment, branch_entry)
assert branch_path[0] == branch_entry, "the ordered path must start at entry_cell"
assert branch_path[-1] in farthest_cells, (
    f"the ordered path must end at a genuine farthest cell from entry_cell (independently BFS-verified); "
    f"got {branch_path[-1]}, expected one of {farthest_cells}"
)
assert len(branch_path) == len(set(branch_path)), "the ordered path must not repeat any cell"
assert all(cell in branch_set for cell in branch_path), "every cell in the ordered path must belong to the fragment"
for (r1, c1), (r2, c2) in zip(branch_path, branch_path[1:]):
    assert (abs(r1 - r2), abs(c1 - c2)) in {(0, 1), (1, 0), (1, 1)}, (
        f"consecutive path cells ({r1},{c1}) -> ({r2},{c2}) must be real D8 neighbors"
    )
print(
    "_order_fragment_from_entry produces a valid, non-repeating, D8-adjacent path from entry_cell to a "
    "genuine (independently-verified) farthest cell on a branching fragment."
)


# =====================================================================
# _score_ridge_candidates: three-term normalized weighted scoring
# =====================================================================

split_row = 20
two_zone_array = np.zeros((41, 41), dtype=np.float32)
for row in range(41):
    if row < split_row:
        two_zone_array[row, :] = 100.0 - row * 0.1  # gentle zone, ~2% grade
    else:
        two_zone_array[row, :] = (100.0 - split_row * 0.1) - (row - split_row) * 3.0  # steep zone, ~60% grade
two_zone_dem = {
    "array": two_zone_array, "resolution_meters": RESOLUTION,
    "origin_x": 500000.0, "origin_y": 4500205.0, "crs": CRS,
}

anchor_cell = (5, 2)
flat_short = [(5, c) for c in range(30, 40)]        # 10 cells, gentle zone
flat_long = [(15, c) for c in range(5, 35)]          # 30 cells, gentle zone
steep_short = [(25, c) for c in range(10, 20)]       # 10 cells, steep zone
unreachable = [(35, c) for c in range(10, 15)]       # 5 cells, made hard-excluded below

score_excluded_mask = np.zeros((41, 41), dtype=bool)
score_excluded_mask[35, 10:15] = True
score_slope_pct, _ = compute_slope_and_aspect(two_zone_dem["array"], two_zone_dem["resolution_meters"])
score_cost_raster = build_cost_raster(two_zone_dem, score_slope_pct, score_excluded_mask)

# Half of flat_long's own cells sit inside a production zone -- production
# is a SOFT, cell-based scoring term on the ridge fragment now (not a hard
# prune), so flat_long must still be scored (not dropped), just penalized
# on production_score relative to the other, production-free candidates.
score_production_mask = np.zeros((41, 41), dtype=bool)
score_production_mask[15, 5:20] = True  # first 15 of flat_long's 30 cells

scored = _score_ridge_candidates(
    two_zone_dem, score_cost_raster, [flat_short, flat_long, steep_short, unreachable], anchor_cell,
    score_production_mask,
)
assert len(scored) == 3, f"the unreachable (hard-excluded) candidate must be dropped, expected 3 survivors, got {len(scored)}"
scored_cell_sets = [set(c["cells"]) for c in scored]
assert set(map(tuple, unreachable)) not in scored_cell_sets, "the unreachable candidate must not appear in the output at all"

for candidate in scored:
    assert "proximity_score" not in candidate, (
        "proximity_score no longer exists -- least_cost_path()'s own total_cost already governs which "
        "reachable entry point each candidate connects through, scoring it again was redundant"
    )
    for key in ("slope_score", "length_score", "production_score", "weighted_score"):
        assert 0.0 <= candidate[key] <= 1.0, f"{key}={candidate[key]} out of [0, 1] range"
    expected_weighted = (
        RIDGE_SLOPE_WEIGHT * candidate["slope_score"]
        + RIDGE_LENGTH_WEIGHT * candidate["length_score"]
        + RIDGE_PRODUCTION_WEIGHT * candidate["production_score"]
    )
    assert abs(candidate["weighted_score"] - expected_weighted) < 1e-9, (
        f"weighted_score must equal the RIDGE_*_WEIGHT-combined formula exactly, got "
        f"{candidate['weighted_score']} vs expected {expected_weighted}"
    )

by_cells = {tuple(c["cells"]): c for c in scored}
steep_candidate = by_cells[tuple(steep_short)]
long_candidate = by_cells[tuple(flat_long)]
short_candidate = by_cells[tuple(flat_short)]
assert steep_candidate["slope_score"] == 0.0, (
    "the steep candidate has the WORST (highest) avg_slope_pct among survivors, so its slope_score must "
    "be exactly 0.0 (1 - max/max)"
)
assert long_candidate["length_score"] == 1.0, (
    "the longest candidate (30 cells, the max in this batch) must score exactly 1.0 on length_score"
)
assert long_candidate["production_cells_crossed"] == 15, (
    f"flat_long has exactly 15 of its own cells inside score_production_mask, got "
    f"{long_candidate['production_cells_crossed']}"
)
assert long_candidate["production_score"] == 0.0, (
    "flat_long has the WORST (highest) production_cells_crossed among survivors, so its production_score "
    "must be exactly 0.0 (1 - max/max)"
)
assert steep_candidate["production_cells_crossed"] == 0 and steep_candidate["production_score"] == 1.0, (
    "a candidate with zero production-zone cells must score exactly 1.0 on production_score"
)
assert short_candidate["production_cells_crossed"] == 0 and short_candidate["production_score"] == 1.0
print(
    "_score_ridge_candidates drops unreachable candidates, keeps every score within [0, 1], computes "
    "weighted_score as the exact RIDGE_*_WEIGHT-combined formula, and correctly gives the worst-slope "
    "candidate a 0.0 slope_score, the longest candidate a 1.0 length_score, and the most production-zone-"
    "crossed candidate a 0.0 production_score (a candidate is now SCORED for crossing production, never "
    "dropped for it)."
)


# =====================================================================
# find_road_routes: end-to-end on a real single-ridge DEM
# =====================================================================

ridge_boundary = box(500000, 4500000, 500410, 4500410)
ridge_anchor = _lon_lat_for_cell(ridge_dem, 38, 2)  # off-ridge, near a corner

routes = find_road_routes(ridge_dem, [], None, ridge_boundary, ridge_anchor)
assert len(routes) == 1, f"the current design selects a single winning ridge candidate -- expected exactly 1 route, got {len(routes)}"
route = routes[0]
assert route["rank"] == 1
assert len(route["points_xyz"]) >= 2
assert route["line_utm"].length > 0
assert route["line_utm"].within(ridge_boundary.buffer(1e-6)), "the route must stay entirely within the parcel boundary"
for key in ("avg_slope_pct", "slope_score", "length_score", "production_score", "weighted_score"):
    assert key in route, f"missing ridge-scoring diagnostic field: {key}"
assert "proximity_score" not in route, "proximity_score no longer exists as a separate scoring term"
assert 0.0 <= route["weighted_score"] <= 1.0
expected_suitability = max(0.0, min(100.0, route["weighted_score"] * 100.0))
assert abs(route["suitability_score"] - expected_suitability) < 1e-9, (
    "suitability_score must be the winning candidate's own weighted_score scaled to 0-100"
)
print(
    f"find_road_routes() on a real single-ridge DEM returns exactly ONE route (rank 1), staying on-parcel, "
    f"with suitability_score ({route['suitability_score']:.1f}) correctly derived from weighted_score."
)

# --- cell_footprint_polygon_utm: the REAL, non-zero-width footprint of the route's own path cells,
#     traced back from line_utm's own zero-width centerline to the cell-based form routing computed it in ---

footprint = route["cell_footprint_polygon_utm"]
assert footprint is not None and not footprint.is_empty, "the winning route must have a real, non-empty cell footprint"
assert footprint.area > 0, "cell_footprint_polygon_utm must have REAL, non-zero area -- unlike line_utm, a zero-width LineString"
px, py = ridge_dem["resolution_meters"]
min_expected_area = len(route["points_xyz"]) * 0.25 * px * py  # a loose floor: at least a quarter-cell per point, well below any real per-cell union
assert footprint.area > min_expected_area, (
    f"cell_footprint_polygon_utm ({footprint.area} sq m) looks too small to be a real per-cell union along "
    f"the whole route -- got suspiciously close to zero-width"
)
assert footprint.buffer(1e-6).contains(route["line_utm"]), (
    "the route's own zero-width line_utm must fall entirely within (a tiny buffer of) its own cell footprint -- "
    "same path cells, just the line vs. the real footprint built from them"
)
assert footprint.within(ridge_boundary.buffer(1e-6)), "the cell footprint must also stay entirely within the parcel boundary"
print(
    f"find_road_routes()'s cell_footprint_polygon_utm is a real, non-zero-area polygon ({footprint.area:.1f} sq m) "
    f"built from the same path cells as line_utm, fully containing line_utm and staying on-parcel."
)


# --- min_corridor_length_meters can still drop the winning candidate's own final route ---

too_strict_routes = find_road_routes(ridge_dem, [], None, ridge_boundary, ridge_anchor, min_corridor_length_meters=1_000_000.0)
assert too_strict_routes == [], "an unreasonably high min_corridor_length_meters must still drop the route as a real 'no route' outcome"
print("min_corridor_length_meters still correctly drops a too-short final route (connector + ridge fragment).")


# --- production zone is now a SOFT, cell-based scoring term on the RIDGE itself (no longer a prune) -- but stays a HARD exclusion on the anchor-to-ridge CONNECTOR ---

production_areas = [{"id": 0, "render_fill_polygon_utm": box(500000, 4500205, 500410, 4500410)}]  # northern half, far from the (southern) anchor
production_prepared = prep(production_areas[0]["render_fill_polygon_utm"])
boundary_prepared = prep(ridge_boundary)
production_mask = _build_production_cell_mask(ridge_dem, production_prepared, boundary_prepared)

# Independently recover the FULL, un-pruned ridge candidate (production is
# no longer part of the mask _prune_ridge_networks() ever sees at all) to
# know exactly how many of its own cells genuinely fall inside the
# production zone -- the expected value the route below must match if
# (and only if) the candidate survives pruning fully intact.
no_water_or_production_mask = np.zeros_like(production_mask)
full_ridge_candidates = _prune_ridge_networks(_identify_ridge_cell_mask(ridge_dem), no_water_or_production_mask)
assert len(full_ridge_candidates) == 1
expected_production_cells_crossed = sum(1 for r, c in full_ridge_candidates[0] if production_mask[r, c])
assert expected_production_cells_crossed > 0, "test setup: the production zone must actually overlap part of the ridge"

production_routes = find_road_routes(ridge_dem, production_areas, None, ridge_boundary, ridge_anchor)
assert production_routes, (
    "a production zone overlapping PART of the ridge must no longer eliminate the route entirely -- "
    "production is a soft scoring term on the ridge fragment now, not a hard prune"
)
route = production_routes[0]
assert route["crosses_production_zone"] is True, "the winning ridge fragment must be free to cross the production zone now"
assert route["production_cells_crossed"] == expected_production_cells_crossed, (
    f"the ridge candidate must survive pruning FULLY INTACT (not shrunk by production) -- expected "
    f"{expected_production_cells_crossed} production-zone cells actually crossed (the ridge's real "
    f"overlap), got {route['production_cells_crossed']}"
)
assert route["production_score"] < 1.0, "a candidate that crosses any production cells must be penalized on production_score"
print(
    "A production zone overlapping part of the ridge no longer prunes/shrinks the candidate -- it survives "
    "fully intact, crosses the production zone, and is instead penalized via production_score/"
    "crosses_production_zone."
)

# A production zone swallowing the ENTIRE ridge still leaves no route at
# all -- NOT because pruning ate the candidate anymore (it isn't pruned),
# but because the anchor-to-ridge CONNECTOR stays a HARD exclusion against
# production: least_cost_path() can never treat a production-zone cell as
# a reachable destination, so if literally every one of the ridge's own
# cells sits inside production, none of them is a valid connector
# destination at all -- the candidate gets dropped by _score_ridge_
# candidates() as unreachable, same real "no route" outcome as before,
# for the connector-hard-exclusion reason now, not a ridge-prune reason.
whole_ridge_production = [{"id": 0, "render_fill_polygon_utm": ridge_boundary.buffer(1.0)}]
swallowed_routes = find_road_routes(ridge_dem, whole_ridge_production, None, ridge_boundary, ridge_anchor)
assert swallowed_routes == [], (
    "a production zone covering the entire ridge must still leave NO route at all -- the connector can "
    "never reach into production, so a fully-swallowed ridge has no reachable entry point left"
)
print(
    "A production zone covering the entire ridge still leaves zero routes -- not because pruning removes "
    "the candidate anymore, but because the anchor-to-ridge connector can never reach into production, "
    "confirming production is still genuinely hard for the connector."
)


# --- selected water zone is still a HARD exclusion on the ridge candidate ---

selected_water_zone = {"id": 0, "render_fill_polygon_utm": box(500190, 4500000, 500210, 4500205)}  # southern half of the ridge column
pond_prepared = prep(selected_water_zone["render_fill_polygon_utm"].buffer(25))  # matches POND_ZONE_EXCLUSION_BUFFER_METERS
pond_routes = find_road_routes(ridge_dem, [], selected_water_zone, ridge_boundary, ridge_anchor)
assert pond_routes, "expected a route using the surviving (northern) portion of the ridge"
# Per-CELL check (not a raw line.intersects() check against the buffered
# polygon) -- the underlying model excludes whole DEM cells, not
# sub-cell geometry, so a straight segment BETWEEN two individually-clear
# cell centers can still graze the buffer's own edge without any cell
# itself ever being inside it; same pattern as the production-zone check
# above.
for x, y, _z in pond_routes[0]["points_xyz"]:
    assert not pond_prepared.contains(Point(x, y)), (
        f"route point ({x}, {y}) falls inside the buffered selected water-system zone -- still a HARD exclusion"
    )
print("Selected water-system zone exclusion correctly prunes the ridge candidate and keeps every route cell clear of the buffered zone.")


# --- floodplain is a SOFT cost penalty on the connector: the route may still cross it ---

# A floodplain band between the anchor's own corner and the ridge column
# -- the connector has no way to reach the ridge without crossing it.
connector_floodplain = box(500000, 4500000, 500190, 4500410)
floodplain_routes = find_road_routes(ridge_dem, [], None, ridge_boundary, ridge_anchor, hydric_floodplain_union=connector_floodplain)
assert floodplain_routes, "a floodplain band blocking the only path to the ridge must NOT prevent routing entirely -- it's a SOFT cost penalty"
assert floodplain_routes[0]["crosses_floodplain"], "expected the connector to actually cross the floodplain band, proving it's traversable"
print("Floodplain/hydric ground is a SOFT cost penalty: the connector still crosses a blocking floodplain band (crosses_floodplain=True) rather than routing failing outright.")


# --- steep-grade engineering-consideration note is additive, threshold-gated ---

steep_ridge_dem = _ridge_dem(drop_per_col=6.0)  # much steeper flanks -- forces a high avg_grade_pct on the connector
steep_ridge_anchor = _lon_lat_for_cell(steep_ridge_dem, 38, 2)
steep_ridge_routes = find_road_routes(steep_ridge_dem, [], None, ridge_boundary, steep_ridge_anchor)
assert steep_ridge_routes, "expected a route even on the steep-flanked ridge DEM -- grade has no hard ceiling"
steep_geojson = corridors_to_geojson(steep_ridge_routes)
steep_features = [f for f in steep_geojson["features"] if f["properties"]["avg_grade_pct"] > STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT]
assert steep_features, f"expected the route on the steep-flanked ridge to average above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade"
for feature in steep_features:
    notes = feature["properties"]["confidence_notes"]
    assert "real engineering consideration" in notes and "drainage/culverts" in notes and "water bars" in notes
    assert "TOPOGRAPHIC SUGGESTION only, not a surveyed road alignment" in notes, (
        "the steep-grade note must be ADDITIVE -- the existing blanket disclaimer must still be present"
    )

gentle_geojson = corridors_to_geojson(routes)  # the original, gentle-flank ridge_dem routes from earlier
for feature in gentle_geojson["features"]:
    if feature["properties"]["avg_grade_pct"] <= STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT:
        assert "real engineering consideration" not in feature["properties"]["confidence_notes"]
print(
    f"Routes above {STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT}% grade carry the additive steep-grade "
    f"engineering-consideration note; the blanket disclaimer stays present either way."
)


# =====================================================================
# output: schema-valid FeatureCollection, required (and NO stale) properties
# =====================================================================

geojson = corridors_to_geojson(routes, floodplain_data_is_fallback=True)
validate_feature_collection(geojson)
required_props = {
    "rank", "suitability_score", "avg_grade_pct", "length_ft", "crosses_floodplain",
    "crosses_production_zone", "ridge_avg_slope_pct", "ridge_production_cells_crossed",
    "ridge_slope_score", "ridge_length_score", "ridge_production_score",
    "ridge_weighted_score", "constraints_satisfied",
}
removed_props = {
    "corridor_type", "anchor_status", "anchor_road_name", "anchor_road_distance_ft", "fan_direction",
    "ridge_proximity_score",
}
assert len(geojson["features"]) <= 1, "the current design produces at most ONE feature, never a ranked alternates list"
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "suggested_road_corridor"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    present_removed = removed_props & feature["properties"].keys()
    assert not present_removed, f"these properties should no longer exist at all: {present_removed}"
    assert 0.0 <= feature["properties"]["suitability_score"] <= 100.0
    for score_key in ("ridge_slope_score", "ridge_length_score", "ridge_production_score", "ridge_weighted_score"):
        assert 0.0 <= feature["properties"][score_key] <= 1.0, f"{score_key} out of [0, 1] range"
    assert feature["properties"]["constraints_satisfied"] == ["outside_pond_zone"], (
        "production is a hard exclusion on the connector only now, not the whole route -- it must NOT "
        "appear in constraints_satisfied anymore, only the still-fully-hard pond zone should"
    )
    assert not any("grade" in c for c in feature["properties"]["constraints_satisfied"]), (
        "grade is a soft cost penalty now, not a hard-satisfied constraint -- it must not appear here"
    )
    assert feature["geometry"]["type"] == "LineString"
    notes = feature["properties"]["confidence_notes"].lower()
    assert "topographic suggestion" in notes and "not a surveyed" in notes
    assert "fallback" in notes, "floodplain fallback should be flagged in confidence_notes"
    assert "ridge" in notes, "confidence_notes should describe the ridge-line routing model"
print(
    "corridors_to_geojson output is schema-valid, layer='suggested_road_corridor', with required properties "
    "(including the ridge_* three-term score breakdown and crosses_production_zone) present, at most one "
    "feature ever returned, and every removed property (fan_direction/corridor_type/anchor_status/"
    "anchor_road_name) correctly absent; constraints_satisfied now lists only pond as hard, omits "
    "production and grade entirely."
)


# =====================================================================
# regression: routes stay on-parcel, not drawn from the DEM's buffered margin
# =====================================================================
#
# This is the exact live bug found against the real property: dem_data.py
# fetches a DEM buffered ~100m past the drawn boundary (correct and
# intentional, for terrain-analysis context), but route generation must
# still be structurally confined to the actual parcel.
buffered_ridge_dem = _ridge_dem(size=61, peak_col=30, origin_x=500000.0, origin_y=4500610.0)
parcel_boundary = box(500050, 4500050, 500350, 4500350)  # smaller than the DEM's own 610x610m extent
buffered_anchor = _lon_lat_for_cell(buffered_ridge_dem, 55, 5)  # near the buffered DEM's own edge, off-parcel

buffered_routes = find_road_routes(buffered_ridge_dem, [], None, parcel_boundary, buffered_anchor)
assert buffered_routes, "expected a route on this uniform, buffered ridge DEM"
assert buffered_routes[0]["line_utm"].within(parcel_boundary.buffer(1e-6)), (
    "the route extends outside the real parcel boundary -- route geometry must be drawn from on-parcel "
    "cells only, not the DEM's buffered margin"
)
print(
    "Parcel clipping: the route on a DEM extending well past the parcel boundary stays entirely within "
    "the real (smaller) parcel -- even though the given anchor point itself was off-parcel, snapping "
    "correctly pulled it onto real eligible ground."
)


# =====================================================================
# _snap_anchor_to_eligible_cell: snaps to the nearest FINITE-cost cell, not the raw anchor cell (unchanged)
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
    "a None prepared geometry (no production zones/floodplain at all) must produce an all-False mask"
)
print("_build_production_cell_mask correctly flags only on-parcel cells actually inside the given union.")


# =====================================================================
# identify_road_corridor_candidates() degrades gracefully with no anchor point given
# =====================================================================

no_anchor_result = identify_road_corridor_candidates([(-79.98, 40.64), (-79.97, 40.64), (-79.97, 40.65)], dem=_flat_dem())
assert no_anchor_result["zones_geojson"]["features"] == []
assert no_anchor_result["all_scored_candidates"] == []
assert no_anchor_result["selected_road_corridor"] is None
print("identify_road_corridor_candidates() with no anchor_lon_lat given degrades to the same empty-result "
      "shape as any other not-yet-computable layer, rather than raising.")

print("\nAll road_corridors checks passed.")
