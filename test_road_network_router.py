"""
test_road_network_router.py

Offline (no-network) checks for road_network_router._route() --
the coverage-greedy road network router. Script-style convention, same as
test_road_cost_path.py: synthetic DEMs with hand-derivable geometry, plain
asserts, and a print per check. Every fixture below is a uniform flat cost
raster (grade/floodplain/TPI/production terms are road_cost_path.py's own
concern, already covered by test_road_cost_path.py -- this file is purely
about the coverage-greedy SELECT/STOP loop, the reversible coverage
counter, branch trimming/joining, and the water spur).

A note on distances throughout: a 100m service radius and a 100m/acre
ceiling together leave very little slack on a uniform cost raster -- a
demand block has to be both close enough and big enough that its own real
acreage supports the real distance needed to reach it. Every fixture's
numbers below were chosen (and verified against this module's own actual
output, not hand-guessed) to clear that bar with real margin, not to sit
exactly on the edge of it. Those two figures are PINNED here (see _route()
below) rather than read off PRODUCTION_SERVICE_RADIUS_METERS and
MAX_ROAD_METERS_PER_SERVED_ACRE, which are configurable and have since
moved: a fixture measured against one radius asserts nothing once another
is substituted under it.
"""

import math
import os
import time

import numpy as np

from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres
from road_cost_path import least_cost_path
from road_network_router import (
    MAX_ROAD_METERS_PER_SERVED_ACRE,
    PRODUCTION_SERVICE_RADIUS_METERS,
    route_road_network,
)

# THE GEOMETRY THESE FIXTURES WERE BUILT FOR, PINNED RATHER THAN INHERITED.
# Every block size, offset and distance below was measured against a 100 m
# service radius and a 100 m/acre ceiling, and each sits clear of the edge
# ON THOSE NUMBERS. They are CONFIGURABLE constants, and this branch moved
# both -- the radius to 25 m, the ceiling to 200 -- which silently re-aimed
# every fixture here at geometry it was not built for: at a 25 m radius a
# demand block that used to fall entirely within one trunk's service area
# no longer does, and section 1's "exactly one branch" became zero.
#
# So the fixtures now state the geometry they mean. This file is about the
# SELECT/STOP loop, the reversible coverage counter, branch trimming and
# joining, and the water spur -- none of which is a claim about what the
# constants should BE, and all of which is untestable if a tuning change
# can move the fixture out from under the assertion. The defaults' actual
# values are asserted once, just below, and by test_roads_step.py section 1
# against the reference parcel.
FIXTURE_SERVICE_RADIUS_METERS = 100.0
FIXTURE_MAX_METERS_PER_SERVED_ACRE = 100.0


def _route(*args, **kwargs):
    """route_road_network() with this file's own fixture geometry, unless a
    section overrides it explicitly."""
    kwargs.setdefault("service_radius_meters", FIXTURE_SERVICE_RADIUS_METERS)
    kwargs.setdefault("max_meters_per_served_acre", FIXTURE_MAX_METERS_PER_SERVED_ACRE)
    return route_road_network(*args, **kwargs)


# The shipped defaults, which the fixtures deliberately do NOT use.
assert PRODUCTION_SERVICE_RADIUS_METERS == 25.0
assert MAX_ROAD_METERS_PER_SERVED_ACRE == 200.0

RESOLUTION = (5.0, 5.0)


def _dem(shape: tuple[int, int]) -> dict:
    return {
        "array": np.zeros(shape, dtype=np.float32),
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _block_mask(shape, row_range, col_range) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    mask[row_range[0] : row_range[1], col_range[0] : col_range[1]] = True
    return mask


def _cell_distance_to_mask(dem, cells, row, col) -> float:
    px, py = dem["resolution_meters"]
    return min(math.hypot((col - c) * px, (row - r) * py) for r, c in cells)


# --- 1. Uniform flat cost, one square demand block offset from the anchor:
# --- exactly one branch (trunk), it reaches far enough for the WHOLE
# --- block to fall within the service radius, and newly_served_acres
# --- matches the block's own hand-computed area exactly. ---

shape1 = (80, 80)
dem1 = _dem(shape1)
cost_raster1 = np.ones(shape1, dtype=np.float64)
anchor1 = (40, 0)
block1_rows, block1_cols = (35, 45), (22, 32)
demand1 = _block_mask(shape1, block1_rows, block1_cols)
block1_area = (block1_rows[1] - block1_rows[0]) * (block1_cols[1] - block1_cols[0]) * cell_area_acres(dem1)

result1 = _route(dem1, cost_raster1, anchor1, demand1)

assert len(result1["branches"]) == 1, f"expected exactly one branch, got {len(result1['branches'])}"
branch1 = result1["branches"][0]
assert branch1["branch_role"] == "trunk", f"expected role 'trunk', got {branch1['branch_role']}"
assert abs(branch1["newly_served_acres"] - block1_area) < 1e-9, (
    f"expected newly_served_acres == block's true area {block1_area}, got {branch1['newly_served_acres']}"
)

max_dist1 = max(
    _cell_distance_to_mask(dem1, branch1["cells"], r, c)
    for r in range(*block1_rows)
    for c in range(*block1_cols)
)
assert max_dist1 <= 100.0, f"every block cell must be within the 100m service radius of the branch, worst case was {max_dist1}"
print(
    f"1. Uniform flat cost, offset block: 1 branch, role='trunk', reaches every block cell within "
    f"{max_dist1:.2f}m (<=100m), newly_served_acres={branch1['newly_served_acres']:.6f} == block area {block1_area:.6f} to 1e-9."
)


# --- 2. Anchor already within service radius of the ENTIRE demand block:
# --- branches == [], stop_reason "all_demand_served", total_length_meters 0.0. ---

shape2 = (40, 40)
dem2 = _dem(shape2)
cost_raster2 = np.ones(shape2, dtype=np.float64)
anchor2 = (20, 0)
demand2 = _block_mask(shape2, (19, 22), (3, 6))  # well within 100m of the anchor

result2 = _route(dem2, cost_raster2, anchor2, demand2)

assert result2["branches"] == [], f"expected no branches, got {result2['branches']}"
assert result2["stop_reason"] == "all_demand_served", f"expected 'all_demand_served', got {result2['stop_reason']}"
assert result2["total_length_meters"] == 0.0, f"expected total_length_meters 0.0, got {result2['total_length_meters']}"
print(
    "2. Anchor already covers the entire block: branches=[], stop_reason='all_demand_served', "
    f"total_length_meters=0.0, total_served_acres={result2['total_served_acres']:.6f}."
)


# --- 3. No demand at all: branches == [], no exception, and stop_reason
# --- is the distinct "no_demand" -- NOT "all_demand_served", which would
# --- be a false claim that there was real demand and it got served. ---

shape3 = (30, 30)
dem3 = _dem(shape3)
cost_raster3 = np.ones(shape3, dtype=np.float64)
anchor3 = (15, 15)
demand3 = np.zeros(shape3, dtype=bool)

result3 = _route(dem3, cost_raster3, anchor3, demand3)

assert result3["branches"] == [], f"expected no branches with zero demand, got {result3['branches']}"
assert result3["stop_reason"] == "no_demand", f"expected stop_reason 'no_demand', got {result3['stop_reason']}"
print(f"3. No demand at all: branches=[], no exception raised, stop_reason='{result3['stop_reason']}'.")


# --- 4. Two demand blocks in different directions: two branches, roles
# --- "trunk" then "spur", spur.joins_branch_index == 0, spur's first cell
# --- is a trunk cell, and the spur's own new length is strictly less than
# --- a from-anchor route to the same endpoint (it must not re-traverse
# --- the trunk's full length as new construction). ---

shape4 = (120, 120)
dem4 = _dem(shape4)
cost_raster4 = np.ones(shape4, dtype=np.float64)
anchor4 = (40, 0)
demand4 = _block_mask(shape4, (35, 45), (22, 32))       # block A: east of the anchor
demand4 |= _block_mask(shape4, (10, 20), (5, 15))       # block B: north of the anchor

result4 = _route(dem4, cost_raster4, anchor4, demand4)

assert len(result4["branches"]) == 2, f"expected exactly two branches, got {len(result4['branches'])}"
trunk4, spur4 = result4["branches"]
assert trunk4["branch_role"] == "trunk", f"expected first branch role 'trunk', got {trunk4['branch_role']}"
assert spur4["branch_role"] == "spur", f"expected second branch role 'spur', got {spur4['branch_role']}"
assert spur4["joins_branch_index"] == 0, f"expected spur to join branch 0 (the trunk), got {spur4['joins_branch_index']}"
assert spur4["cells"][0] in trunk4["cells"], "spur's first (join) cell must be a cell of the trunk"

spur4_endpoint = spur4["cells"][-1]
direct4 = least_cost_path(dem4, cost_raster4, [anchor4], [spur4_endpoint])
direct4_length = sum(
    math.hypot((c1 - c0) * 5.0, (r1 - r0) * 5.0)
    for (r0, c0), (r1, c1) in zip(direct4["cells"], direct4["cells"][1:])
)
assert spur4["length_meters"] < direct4_length, (
    f"spur's new length ({spur4['length_meters']}) must be strictly less than a from-anchor route to the "
    f"same endpoint ({direct4_length}) -- it must not re-traverse the trunk's full length as new construction"
)
print(
    f"4. Two blocks in different directions: 2 branches (trunk, spur), spur joins branch 0 at trunk cell "
    f"{spur4['cells'][0]}, spur new length {spur4['length_meters']:.2f}m < direct-route length {direct4_length:.2f}m."
)


# --- 5. Demand block reachable only across a cost band: cheap band ->
# --- branch built; expensive band -> the cheapest route detours around
# --- it, its real length pushes meters-per-acre over the threshold, and
# --- branches == [] with stop_reason "cost_per_acre_exceeded". ---

shape5 = (60, 100)
dem5 = _dem(shape5)
anchor5 = (0, 45)
demand5 = _block_mask(shape5, (22, 32), (40, 50))
band_row5 = 6
band_cols5 = (0, 70)  # leaves cols 70:99 open as a detour around the band's east end


def _cost_raster5(band_cost: float) -> np.ndarray:
    cost_raster = np.ones(shape5, dtype=np.float64)
    cost_raster[band_row5, band_cols5[0] : band_cols5[1]] = band_cost
    return cost_raster


result5_cheap = _route(dem5, _cost_raster5(2.0), anchor5, demand5)
assert len(result5_cheap["branches"]) == 1, (
    f"expected a branch to be built with a cheap band, got {result5_cheap['branches']}"
)
assert result5_cheap["stop_reason"] != "cost_per_acre_exceeded"

result5_expensive = _route(dem5, _cost_raster5(1000.0), anchor5, demand5)
assert result5_expensive["branches"] == [], (
    f"expected no branches once the band forces a too-long detour, got {result5_expensive['branches']}"
)
assert result5_expensive["stop_reason"] == "cost_per_acre_exceeded", (
    f"expected stop_reason 'cost_per_acre_exceeded', got {result5_expensive['stop_reason']}"
)
print(
    "5. Expensive band: band_cost=2.0 builds a branch "
    f"(length={result5_cheap['branches'][0]['length_meters']:.1f}m); band_cost=1000.0 forces a detour whose "
    "real length exceeds the meters-per-acre threshold -> branches=[], stop_reason='cost_per_acre_exceeded'."
)


# --- 6. Coverage counter correctness: two OVERLAPPING demand blocks (cols
# --- 42:52 shared) whose service discs overlap heavily once roads reach
# --- them. total_served_acres must equal the true UNION area (via
# --- inclusion-exclusion, hand-computed independently of any branch's own
# --- reported acreage), not double-counted. ---

shape6 = (80, 160)
dem6 = _dem(shape6)
cost_raster6 = np.ones(shape6, dtype=np.float64)
anchor6 = (40, 0)
block6_a = _block_mask(shape6, (35, 45), (22, 52))   # 10 x 30
block6_b = _block_mask(shape6, (35, 45), (42, 72))   # 10 x 30, overlaps block A in cols 42:52
demand6 = block6_a | block6_b
true_union_cells6 = int(np.sum(demand6))
true_union_acres6 = true_union_cells6 * cell_area_acres(dem6)

# Hand-derived via inclusion-exclusion, independently of demand6 itself:
area_a6 = 10 * 30
area_b6 = 10 * 30
overlap6 = 10 * 10
assert true_union_cells6 == area_a6 + area_b6 - overlap6, "inclusion-exclusion sanity check on the fixture itself"

result6 = _route(dem6, cost_raster6, anchor6, demand6)
assert len(result6["branches"]) >= 2, f"expected 2+ branches (discs must overlap), got {len(result6['branches'])}"
assert abs(result6["total_served_acres"] - true_union_acres6) < 1e-9, (
    f"total_served_acres ({result6['total_served_acres']}) must equal the true union area ({true_union_acres6}), "
    "not the sum of each branch's own disc coverage computed independently"
)
naive_sum6 = sum(b["newly_served_acres"] for b in result6["branches"])
assert abs(naive_sum6 - true_union_acres6) < 1e-9, "sum of branches' own newly_served_acres must equal the true union too (each is already marginal)"
print(
    f"6. Overlapping discs: {len(result6['branches'])} branches, total_served_acres={result6['total_served_acres']:.6f} "
    f"== true union area {true_union_acres6:.6f} (inclusion-exclusion of {area_a6}+{area_b6}-{overlap6} cells), not double-counted."
)


# --- 7. Water spur within range: a "water_spur" branch exists and its
# --- final cell is one of water_target_cells. No production demand at
# --- all, so the spur routes straight from the anchor with no join. ---

shape7 = (40, 40)
dem7 = _dem(shape7)
cost_raster7 = np.ones(shape7, dtype=np.float64)
anchor7 = (0, 0)
demand7 = np.zeros(shape7, dtype=bool)
water_targets7 = [(24, 0)]  # 120m straight south -- within the default 150m max_water_spur_meters

result7 = _route(dem7, cost_raster7, anchor7, demand7, water_target_cells=water_targets7)

water_branches7 = [b for b in result7["branches"] if b["branch_role"] == "water_spur"]
assert len(water_branches7) == 1, f"expected exactly one water_spur branch, got {len(water_branches7)}"
water_branch7 = water_branches7[0]
assert water_branch7["cells"][-1] in water_targets7, "water_spur's final cell must be one of water_target_cells"
assert water_branch7["newly_served_acres"] == 0.0, "water_spur must report newly_served_acres 0.0"
assert water_branch7["joins_branch_index"] is None, "with zero production branches, the water_spur has no trunk to join"
print(
    f"7. Water spur within range (120m <= 150m default): water_spur branch built, final cell "
    f"{water_branch7['cells'][-1]} in water_target_cells, length={water_branch7['length_meters']:.1f}m."
)


# --- 8. Water spur out of range: lowering max_water_spur_meters below the
# --- achievable distance skips the spur entirely -- no branch, no
# --- exception. ---

result8 = _route(
    dem7, cost_raster7, anchor7, demand7, water_target_cells=water_targets7, max_water_spur_meters=100.0
)
water_branches8 = [b for b in result8["branches"] if b["branch_role"] == "water_spur"]
assert water_branches8 == [], f"expected no water_spur branch once max_water_spur_meters (100m) is below the achievable 120m, got {water_branches8}"
print("8. Water spur out of range (max_water_spur_meters=100.0 < achievable 120m): no water_spur branch, no exception.")


# --- 9. Determinism: the same fixture run 5 times produces byte-identical
# --- branch cell lists every time. ---

runs9 = [_route(dem4, cost_raster4, anchor4, demand4) for _ in range(5)]
first_cells9 = [b["cells"] for b in runs9[0]["branches"]]
for i, run in enumerate(runs9[1:], start=2):
    cells_i = [b["cells"] for b in run["branches"]]
    assert cells_i == first_cells9, f"run {i} produced different branch cell lists than run 1 -- routing is not deterministic"
print(f"9. Determinism: 5 runs of the same fixture produced byte-identical branch cell lists ({len(first_cells9)} branches each).")


# --- 10. TIMING, report-only, must not assert. Two measurements, BOTH
# --- gated behind ROUTER_TIMING=1 (see the bottom of this section) --
# --- skipped by default because 10b alone takes on the order of 20
# --- minutes of real wall-clock, on every run of this suite, on every
# --- branch that so much as touches code near the router -- a cost with
# --- no corresponding signal for most of those runs, since this section
# --- asserts nothing. Set ROUTER_TIMING=1 to actually collect the
# --- numbers when they're the thing you're checking.
# ---
# --- (a) PARCEL-SCALE, matching what this tool actually targets ("a few
# --- acres up to roughly 20-30" -- see road_network_router.py's own
# --- module docstring). Grid sized to a 30-acre parcel plus
# --- dem_data.DEFAULT_BUFFER_METERS (100.0m) of fetch buffer on every
# --- side, at dem_data's own real output resolution
# --- (DEFAULT_RESOLUTION_METERS = 5.0m, confirmed by inspection of
# --- dem_data.py -- hardcoded below rather than imported, so this
# --- otherwise network-free test file doesn't pull rasterio/requests in
# --- for two float constants). Demand is a single CONTIGUOUS ~15-acre
# --- blob, not scattered cells -- a real production zone is a blob, and
# --- scattering demand inflates iteration count in a way that will never
# --- happen in practice.
# ---
# --- (b) TRUE PRODUCTION WORST CASE: dem_data.get_dem_for_boundary()
# --- caps every fetched grid at dem_data.MAX_GRID_DIMENSION (300 cells
# --- per side, confirmed by inspection of dem_data.py -- hardcoded below
# --- for the same "stay network-free" reason as the two constants above,
# --- not imported) -- beyond that span the fetch coarsens resolution
# --- instead of growing the grid, so 300x300 at this same real 5m
# --- resolution is the largest grid this router can ever actually be
# --- handed in production, not an arbitrary oversized figure (a stale
# --- 400x400 fixture here used to measure an input that cannot occur).
# --- At 5m resolution that's a 1500m square, ~556-acre parcel, still far
# --- beyond this tool's own stated upper bound. Still a single
# --- contiguous demand blob (~40% of the grid), not scattered. This size
# --- is explicitly not one this tool is meant to handle; if it's slow,
# --- that is the finding this measurement exists to report, not a bug to
# --- fix here.

_TEST_RESOLUTION_METERS = 5.0  # dem_data.DEFAULT_RESOLUTION_METERS, confirmed by inspection
_TEST_BUFFER_METERS = 100.0    # dem_data.DEFAULT_BUFFER_METERS, confirmed by inspection
_TEST_MAX_GRID_DIMENSION = 300  # dem_data.MAX_GRID_DIMENSION, confirmed by inspection


def _run_timing(label, grid_side_meters, demand_acres_target, resolution_meters):
    n = max(2, round(grid_side_meters / resolution_meters))
    shape = (n, n)
    dem = {
        "array": np.zeros(shape, dtype=np.float32),
        "resolution_meters": (resolution_meters, resolution_meters),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }
    cost_raster = np.ones(shape, dtype=np.float64)
    area_per_cell = cell_area_acres(dem)
    blob_side_cells = max(1, round(math.sqrt(demand_acres_target / area_per_cell)))
    inset = max(0, (n - blob_side_cells) // 2)
    demand_mask = _block_mask(shape, (inset, inset + blob_side_cells), (inset, inset + blob_side_cells))
    demand_acres_actual = float(np.sum(demand_mask)) * area_per_cell
    anchor = (0, n // 2)

    start = time.perf_counter()
    result = _route(dem, cost_raster, anchor, demand_mask)
    elapsed = time.perf_counter() - start

    print(
        f"{label}: grid={n}x{n} cells, resolution={resolution_meters}m, demand_acres={demand_acres_actual:.2f}, "
        f"elapsed={elapsed:.2f}s, iterations(branches)={len(result['branches'])}, stop_reason='{result['stop_reason']}', "
        f"total_served_acres={result['total_served_acres']:.2f}, unserved_acres={result['unserved_acres']:.2f}."
    )
    return result


if os.environ.get("ROUTER_TIMING") == "1":
    parcel_side_m = math.sqrt(30.0 * SQUARE_METERS_PER_ACRE)
    grid_side_m_a = parcel_side_m + 2 * _TEST_BUFFER_METERS
    print(
        f"10a. fixture: 30-acre parcel ({parcel_side_m:.1f}m square) + {_TEST_BUFFER_METERS:.0f}m buffer "
        f"-> {grid_side_m_a:.1f}m grid square at {_TEST_RESOLUTION_METERS}m resolution."
    )
    _run_timing("10a. TIMING (report-only, parcel-scale)", grid_side_m_a, 15.0, _TEST_RESOLUTION_METERS)

    grid_side_m_b = _TEST_MAX_GRID_DIMENSION * _TEST_RESOLUTION_METERS
    demand_acres_target_b = 0.4 * (grid_side_m_b**2) / SQUARE_METERS_PER_ACRE
    print(
        f"10b. fixture: {_TEST_MAX_GRID_DIMENSION}x{_TEST_MAX_GRID_DIMENSION} grid (dem_data.MAX_GRID_DIMENSION, "
        f"the largest grid get_dem_for_boundary() can ever actually return) at {_TEST_RESOLUTION_METERS}m -> "
        f"{grid_side_m_b:.1f}m square (~{(grid_side_m_b**2)/SQUARE_METERS_PER_ACRE:.0f} acres) -- the true "
        "production worst case, still far beyond this tool's stated 'a few to ~20-30 acre' design range."
    )
    _run_timing(
        f"10b. TIMING (report-only, worst-case {_TEST_MAX_GRID_DIMENSION}x{_TEST_MAX_GRID_DIMENSION})",
        grid_side_m_b, demand_acres_target_b, _TEST_RESOLUTION_METERS,
    )
else:
    print(
        "10. TIMING (10a parcel-scale, 10b worst-case) SKIPPED -- report-only, asserts nothing, and 10b alone "
        "takes on the order of 20 minutes. Set ROUTER_TIMING=1 to run both and see the numbers."
    )


print("\nAll test_road_network_router.py checks passed.")
