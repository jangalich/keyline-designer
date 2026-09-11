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

Sections 10-16 are the exception to "synthetic DEM": leaf-spur pruning is
a rule about branch TOPOLOGY and arithmetic, so those fixtures state the
topology directly as network dicts and hand them to _prune_leaf_branches()
-- see the header on section 10 for why routing one off a grid would test
a Dijkstra tie-break instead of the rule. Section 17 then checks the rule
is actually wired into route_road_network() itself, on a real routed
fixture.

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
    MIN_LEAF_BRANCH_METERS,
    PRODUCTION_SERVICE_RADIUS_METERS,
    _prune_leaf_branches,
    route_road_network,
)

# THE GEOMETRY THESE FIXTURES WERE BUILT FOR, PINNED RATHER THAN INHERITED.
# Every block size, offset and distance below was measured against a 100 m
# service radius and a 100 m/acre ceiling, and each sits clear of the edge
# ON THOSE NUMBERS. They are CONFIGURABLE constants, and both have since
# moved -- the radius to 25 m, the ceiling to 200 and now 500 -- which
# silently re-aimed every fixture here at geometry it was not built for:
# at a 25 m radius a demand block that used to fall entirely within one
# trunk's service area no longer does, and section 1's "exactly one
# branch" became zero.
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

# LEAF PRUNING IS PINNED OFF for sections 1-9, for the same reason the two
# figures above are pinned: those fixtures were built and measured against
# a router that did no pruning, and every one of them is about something
# else -- the SELECT/STOP loop, the reversible coverage counter, branch
# trimming and joining, the water spur. The shipped 25 m default silently
# re-aims them: section 4's real, deliberate spur measures under 25 m of
# NEW construction at this fixture's own 100 m radius (the trunk it joins
# already carries most of the distance), so the default prunes it and
# section 4's "exactly two branches" becomes one -- an assertion about
# joining, failing on a rule about stub length.
#
# 0.0 disables pruning outright: the length test is a strict '<', and no
# real branch has negative length. Pruning gets its own fixtures instead,
# stating their own topology -- sections 10-16 against
# _prune_leaf_branches() directly, and section 17 through
# route_road_network() itself with the threshold set off a branch that run
# actually produced. The shipped default's real value is asserted once,
# just below.
FIXTURE_MIN_LEAF_BRANCH_METERS = 0.0


def _route(*args, **kwargs):
    """route_road_network() with this file's own fixture geometry, unless a
    section overrides it explicitly."""
    kwargs.setdefault("service_radius_meters", FIXTURE_SERVICE_RADIUS_METERS)
    kwargs.setdefault("max_meters_per_served_acre", FIXTURE_MAX_METERS_PER_SERVED_ACRE)
    kwargs.setdefault("min_leaf_branch_meters", FIXTURE_MIN_LEAF_BRANCH_METERS)
    return route_road_network(*args, **kwargs)


# The shipped defaults, which the fixtures deliberately do NOT use.
assert PRODUCTION_SERVICE_RADIUS_METERS == 25.0
assert MAX_ROAD_METERS_PER_SERVED_ACRE == 250.0
assert MIN_LEAF_BRANCH_METERS == 50.0

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


# =====================================================================
# LEAF-SPUR PRUNING (sections 10-16). Every fixture below is a SYNTHETIC
# NETWORK DICT handed straight to _prune_leaf_branches(), not a routed
# grid -- deliberately. What is under test here is a rule about
# TOPOLOGY and ARITHMETIC ("which branches are leaves, which of those
# are exempt, and what the totals become"), and stating the topology
# outright is the only way to test it with numbers a reader can check by
# hand. Building a real cost raster that happens to produce a 5 m stub
# hanging off a 7 m continuation would pin the assertion to a Dijkstra
# tie-break rather than to the pruning rule, and section 2 below -- the
# continuation chain, the one an iterating implementation fails -- is
# not reliably constructible from terrain at all.
#
# Each fixture's own totals are consistent by construction: total_length
# _meters is the sum of the branches' lengths, total_served_acres is the
# anchor's own baseline coverage plus the sum of their newly_served_
# acres, and unserved_acres is whatever is left of a stated demand
# figure. That is exactly the shape route_road_network() itself returns.

_PRUNE_ANCHOR_BASELINE_ACRES = 0.40   # the anchor's own coverage, which no branch owns
_PRUNE_TOTAL_DEMAND_ACRES = 6.00      # a stated demand figure, so unserved is checkable


def _branch(role, length, acres, joins, cost=1.0):
    """One branch dict in exactly route_road_network()'s own shape. 'cells'
    is a placeholder pair -- pruning never reads it, and giving it real
    geometry would imply this fixture came off a grid, which it did not.
    cost is this branch's own accumulated path cost; it defaults to 1.0
    (pruning itself never reads it either) and is stated explicitly only
    by the fixtures in section 17b, which are about the network-level
    total_cost summed over the survivors."""
    return {
        "cells": [(0, 0), (0, 1)],
        "branch_role": role,
        "length_meters": length,
        "total_cost": cost,
        "newly_served_acres": acres,
        "joins_branch_index": joins,
    }


def _network(branches, stop_reason="cost_per_acre_exceeded"):
    """The network dict those branches add up to, with totals derived from
    the branches themselves rather than restated -- so a fixture cannot
    quietly disagree with its own arithmetic."""
    served = _PRUNE_ANCHOR_BASELINE_ACRES + sum(b["newly_served_acres"] for b in branches)
    return {
        "branches": branches,
        "total_length_meters": float(sum(b["length_meters"] for b in branches)),
        "total_served_acres": served,
        "unserved_acres": _PRUNE_TOTAL_DEMAND_ACRES - served,
        "stop_reason": stop_reason,
    }


# --- 10. A real trunk plus one 5 m terminal stub: the stub is pruned,
# --- the trunk survives, and each of the three network totals moves by
# --- EXACTLY the stub's own figures -- no recomputation, no rounding. ---

trunk10 = _branch("trunk", 180.0, 3.20, None)
stub10 = _branch("spur", 5.0, 0.06, 0)
before10 = _network([trunk10, stub10])
after10 = _prune_leaf_branches(before10, MIN_LEAF_BRANCH_METERS)

assert len(after10["branches"]) == 1, f"expected the stub pruned and the trunk kept, got {after10['branches']}"
assert after10["branches"][0] is trunk10, "the surviving branch must be the trunk itself, unmodified"
assert abs(after10["total_length_meters"] - (before10["total_length_meters"] - 5.0)) < 1e-12, (
    f"total_length_meters must drop by exactly the stub's 5.0 m, got {after10['total_length_meters']} "
    f"from {before10['total_length_meters']}"
)
assert abs(after10["total_served_acres"] - (before10["total_served_acres"] - 0.06)) < 1e-12, (
    f"total_served_acres must drop by exactly the stub's own 0.06 acres, got {after10['total_served_acres']}"
)
assert abs(after10["unserved_acres"] - (before10["unserved_acres"] + 0.06)) < 1e-12, (
    f"unserved_acres must gain back exactly that same 0.06 acres, got {after10['unserved_acres']}"
)
assert after10["stop_reason"] == before10["stop_reason"], (
    "branches survive, so the loop's own stop_reason must be left exactly as it was"
)
print(
    f"10. Trunk (180.0 m) + 5.0 m terminal stub: stub pruned, trunk kept; total_length "
    f"{before10['total_length_meters']:.2f} -> {after10['total_length_meters']:.2f} m, served "
    f"{before10['total_served_acres']:.2f} -> {after10['total_served_acres']:.2f} ac, unserved "
    f"{before10['unserved_acres']:.2f} -> {after10['unserved_acres']:.2f} ac -- each by exactly the stub's own value."
)


# --- 11. THE CONTINUATION CHAIN, the section this rule exists for:
# --- trunk -> A (5 m, joined by B) -> B (7 m, leaf). ONLY B is pruned.
# --- A is under the threshold too, and survives anyway, because it is
# --- NOT A LEAF -- it is the middle of one continuous road. An
# --- iterate-until-stable implementation notices A became a leaf once B
# --- left, takes it as well, and trims 12 m off the end of a single
# --- road instead of removing a stub. It fails here, which is the point
# --- of the section. ---

trunk11 = _branch("trunk", 150.0, 2.50, None)
branch_a11 = _branch("spur", 5.0, 0.05, 0)     # joins the trunk; B joins THIS
branch_b11 = _branch("spur", 7.0, 0.07, 1)     # joins A -- the only leaf in the chain
before11 = _network([trunk11, branch_a11, branch_b11])
after11 = _prune_leaf_branches(before11, MIN_LEAF_BRANCH_METERS)

assert after11["branches"] == [trunk11, branch_a11], (
    f"expected ONLY branch B pruned (trunk and A both survive), got {after11['branches']}"
)
assert branch_a11 in after11["branches"], (
    "branch A (5 m, under the threshold) MUST survive -- it is not a leaf, and pruning it would trim "
    "12 m off the end of one continuous road. An iterating implementation fails exactly here."
)
assert after11["branches"][1]["joins_branch_index"] == 0, "A's own join must be untouched"
assert abs(after11["total_length_meters"] - 155.0) < 1e-12, (
    f"only B's 7 m may come off 162.0, got {after11['total_length_meters']}"
)
assert abs(after11["total_served_acres"] - (before11["total_served_acres"] - 0.07)) < 1e-12, (
    "only B's own newly_served_acres may come off"
)
print(
    "11. Continuation chain trunk -> A(5 m, joined by B) -> B(7 m, leaf): ONLY B pruned. A survives "
    f"despite being under the {MIN_LEAF_BRANCH_METERS:.0f} m threshold, because it is not a leaf; total_length "
    f"162.00 -> {after11['total_length_meters']:.2f} m (7 m, not 12). Single pass, no cascade."
)


# --- 12. A genuine mid-branch spur ABOVE the threshold survives -- the
# --- rule is about stubs, not about spurs. Both a leaf spur well over
# --- the threshold and a leaf spur sitting exactly ON it are kept (the
# --- test is strictly '<', so the boundary value is worth building). ---

trunk12 = _branch("trunk", 150.0, 2.50, None)
real_spur12 = _branch("spur", 62.0, 0.80, 0)
boundary_spur12 = _branch("spur", MIN_LEAF_BRANCH_METERS, 0.30, 0)
before12 = _network([trunk12, real_spur12, boundary_spur12])
after12 = _prune_leaf_branches(before12, MIN_LEAF_BRANCH_METERS)

assert after12 is before12, "with nothing to prune, the original network dict must come back untouched"
assert after12["branches"] == [trunk12, real_spur12, boundary_spur12], (
    f"a leaf spur at or above the threshold must survive, got {after12['branches']}"
)
print(
    f"12. Mid-branch leaf spurs at {real_spur12['length_meters']:.1f} m and exactly "
    f"{MIN_LEAF_BRANCH_METERS:.1f} m (the boundary): both survive, network returned untouched."
)


# --- 13. A water_spur BELOW the threshold is NOT pruned, however short.
# --- It is short by design -- it only has to reach the ring of ground
# --- just outside the pond buffer -- and its newly_served_acres is 0.0
# --- by construction, so a length test here measures nothing about
# --- coverage. Pruning it would silently delete the parcel's pond
# --- access. An ordinary spur of the SAME length, in the same network,
# --- IS pruned -- so what is doing the work here is the role, not the
# --- length. ---

trunk13 = _branch("trunk", 150.0, 2.50, None)
water_spur13 = _branch("water_spur", 8.0, 0.0, 0)
plain_spur13 = _branch("spur", 8.0, 0.04, 0)   # same length, no exemption
before13 = _network([trunk13, water_spur13, plain_spur13])
after13 = _prune_leaf_branches(before13, MIN_LEAF_BRANCH_METERS)

assert water_spur13 in after13["branches"], (
    f"the water_spur must NOT be pruned at {water_spur13['length_meters']} m -- it is short by design, "
    f"got {after13['branches']}"
)
assert plain_spur13 not in after13["branches"], (
    "an ordinary spur of the SAME 8.0 m length must still be pruned -- the exemption is the role, not the length"
)
assert after13["branches"] == [trunk13, water_spur13]
assert abs(after13["total_length_meters"] - 158.0) < 1e-12, (
    f"only the plain spur's 8 m may come off 166.0, got {after13['total_length_meters']}"
)
print(
    f"13. water_spur at 8.0 m (well under {MIN_LEAF_BRANCH_METERS:.0f} m) survives, while an ordinary spur of the "
    "SAME 8.0 m in the same network is pruned: the exemption is branch_role, not length."
)


# --- 14. A network whose ONLY branch is a 10 m trunk is returned INTACT.
# --- It is a leaf, and it is far under the threshold, and it is still a
# --- legitimate short road -- the road is short because the field is
# --- close. Deleting it is precisely the mistake the deleted network-
# --- level corridor-length floor made. ---

trunk14 = _branch("trunk", 10.0, 1.10, None)
before14 = _network([trunk14], stop_reason="all_demand_served")
after14 = _prune_leaf_branches(before14, MIN_LEAF_BRANCH_METERS)

assert after14 is before14, "a lone short trunk must come back as the very same untouched network"
assert after14["branches"] == [trunk14], f"the trunk must survive, got {after14['branches']}"
assert after14["total_length_meters"] == 10.0
assert after14["stop_reason"] == "all_demand_served", (
    "a surviving network keeps its own stop_reason -- it was never emptied"
)
print(
    "14. Lone 10.0 m trunk (a leaf, far under the threshold): NOT pruned, network returned intact with "
    f"stop_reason='{after14['stop_reason']}' -- a short road is a correct answer, not a stub."
)


# --- 15. Every branch a sub-threshold leaf (no trunk among them, so no
# --- exemption applies): all are pruned and the result is the empty-
# --- network shape with the NEW stop_reason. The loop's own original
# --- reason is deliberately NOT reported -- growth is not why this came
# --- back empty, and saying so would misdescribe the outcome. ---

leaf_a15 = _branch("spur", 6.0, 0.05, None)
leaf_b15 = _branch("spur", 9.0, 0.08, None)
before15 = _network([leaf_a15, leaf_b15], stop_reason="cost_per_acre_exceeded")
after15 = _prune_leaf_branches(before15, MIN_LEAF_BRANCH_METERS)

assert after15["branches"] == [], f"expected every branch pruned, got {after15['branches']}"
assert after15["stop_reason"] == "all_branches_below_minimum", (
    f"expected stop_reason 'all_branches_below_minimum', got {after15['stop_reason']!r} -- reporting the "
    "loop's own 'cost_per_acre_exceeded' here would be misleading"
)
assert after15["total_length_meters"] == 0.0, f"expected 0.0 total length, got {after15['total_length_meters']}"
assert abs(after15["total_served_acres"] - _PRUNE_ANCHOR_BASELINE_ACRES) < 1e-12, (
    f"total_served_acres must fall back to the anchor's own baseline coverage "
    f"({_PRUNE_ANCHOR_BASELINE_ACRES}), got {after15['total_served_acres']}"
)
assert abs(after15["unserved_acres"] - (_PRUNE_TOTAL_DEMAND_ACRES - _PRUNE_ANCHOR_BASELINE_ACRES)) < 1e-12, (
    f"unserved_acres must gain back both leaves' acreage, got {after15['unserved_acres']}"
)
assert set(after15) == set(before15), "the empty result must keep exactly the same keys as any other network"
print(
    "15. Every branch a sub-threshold leaf: branches=[], total_length_meters=0.0, total_served_acres back to "
    f"the anchor's own {_PRUNE_ANCHOR_BASELINE_ACRES:.2f} ac baseline, unserved {after15['unserved_acres']:.2f} ac, "
    f"stop_reason='{after15['stop_reason']}' (NOT the loop's own '{before15['stop_reason']}')."
)


# --- 16. Determinism, and the JOIN LABELS the survivors carry. Branch
# --- ORDER is preserved, but joins_branch_index is a POSITION in the
# --- branches list -- road_corridors.build_road_network() re-derives its
# --- own branch_index by enumerating that list, and the wire feature id
# --- and the commit gate's tree-closure check are both built on that. So
# --- removing a branch from the MIDDLE shifts every later position, and
# --- a label left at its old number names the wrong branch. Both
# --- fixtures below check the labels still name the SAME BRANCH OBJECTS
# --- they named before pruning -- once where nothing has to move, and
# --- once where a label genuinely does. ---

fixture16 = [
    _branch("trunk", 140.0, 2.40, None),      # 0
    _branch("spur", 5.0, 0.05, 0),            # 1  under threshold, but branch 2 joins it
    _branch("spur", 48.0, 0.70, 1),           # 2  under threshold, but branch 3 joins it
    _branch("spur", 4.0, 0.03, 2),            # 3  stub, and the LAST branch -> pruned
    _branch("water_spur", 11.0, 0.0, 0),      # 4  short, exempt
]
# Branches 1 and 2 are BOTH under the threshold and both survive: 1 because
# branch 2 joins it, 2 because branch 3 does. And once 3 goes, 2 IS a leaf
# under the threshold -- a second pass would take it, and then 1, unwinding
# the whole chain. One pass, so it stands.
runs16 = [_prune_leaf_branches(_network(fixture16), MIN_LEAF_BRANCH_METERS) for _ in range(5)]
for i, run in enumerate(runs16[1:], start=2):
    assert run == runs16[0], f"prune run {i} differed from run 1 -- pruning is not deterministic"

survivors16 = runs16[0]["branches"]
assert [b["length_meters"] for b in survivors16] == [140.0, 5.0, 48.0, 11.0], (
    f"expected only the 4 m stub pruned, in the original order, got {[b['length_meters'] for b in survivors16]}"
)
# Branch 3 was the last non-exempt branch, so only the water_spur moves
# (position 4 -> 3) and every label already names the right position.
assert [b["joins_branch_index"] for b in survivors16] == [None, 0, 1, 0], (
    f"no label needs to move in this fixture, got {[b['joins_branch_index'] for b in survivors16]}"
)
for branch in survivors16:
    if branch["joins_branch_index"] is not None:
        assert 0 <= branch["joins_branch_index"] < len(survivors16), (
            f"joins_branch_index {branch['joins_branch_index']} is not a position in the "
            f"{len(survivors16)}-branch result"
        )

# THE SHIFTING CASE: the stub is branch 1, in the MIDDLE, so branch 2 and
# branch 3 both slide down one and branch 3's own label must follow its
# target. Hand-derived: survivors are original [0, 2, 3] -> positions
# [0, 1, 2], so branch 3's "joins 2" must become "joins 1" -- still the
# 90 m spur, which is the whole point. An implementation that leaves the
# label at 2 has it naming ITSELF; one that leaves it pointing past the
# end has the commit gate reject the whole network. Both surviving spurs
# are deliberately well ABOVE the threshold, so the only branch this
# fixture prunes is the one it means to.
fixture16b = [
    _branch("trunk", 140.0, 2.40, None),      # 0
    _branch("spur", 4.0, 0.03, 0),            # 1  stub in the MIDDLE -> pruned
    _branch("spur", 90.0, 1.05, 0),           # 2  joins the trunk
    _branch("spur", 70.0, 0.85, 2),           # 3  joins branch 2 -- the label that must move
]
after16b = _prune_leaf_branches(_network(fixture16b), MIN_LEAF_BRANCH_METERS)
survivors16b = after16b["branches"]

assert [b["length_meters"] for b in survivors16b] == [140.0, 90.0, 70.0], (
    f"expected the middle stub pruned and the order otherwise kept, got "
    f"{[b['length_meters'] for b in survivors16b]}"
)
assert [b["joins_branch_index"] for b in survivors16b] == [None, 0, 1], (
    f"branch 3's label must follow its target from position 2 to position 1, got "
    f"{[b['joins_branch_index'] for b in survivors16b]}"
)
# The label must still name the SAME BRANCH -- checked by identity of the
# thing at that position, not by the number.
assert survivors16b[2]["joins_branch_index"] == 1 and survivors16b[1]["length_meters"] == 90.0, (
    "the 70 m spur must still join the 90 m spur it always joined, not the trunk and not itself"
)
assert survivors16b[0] is fixture16b[0], "a branch whose label does not move is passed through untouched"
assert fixture16b[3]["joins_branch_index"] == 2, (
    "pruning must not mutate the caller's own branch dicts -- the moved label is a new dict"
)
print(
    "16. Determinism: 5 prunes of the same 5-branch fixture produced identical output, only the 4.0 m stub "
    "removed. Join labels are positions: pruning a MIDDLE stub re-points the 70 m spur's label 2 -> 1 so it "
    "still names the same 90 m spur, branch order is preserved, and unmoved branches pass through untouched."
)


# --- 17. END TO END through route_road_network() itself: pruning is
# --- actually WIRED IN, not merely available. Section 4's two-block
# --- fixture routes a trunk and a real spur; re-running it with
# --- min_leaf_branch_meters set above the spur's own measured length
# --- prunes exactly that spur, and the totals move by exactly its own
# --- figures. The threshold is read off the branch this run actually
# --- produced, so this asserts the wiring rather than a distance. ---

result17_kept = _route(dem4, cost_raster4, anchor4, demand4, min_leaf_branch_meters=0.0)
assert len(result17_kept["branches"]) == 2, f"expected section 4's two branches, got {len(result17_kept['branches'])}"
trunk17, spur17 = result17_kept["branches"]
assert spur17["branch_role"] == "spur" and spur17["joins_branch_index"] == 0

result17_pruned = _route(
    dem4, cost_raster4, anchor4, demand4, min_leaf_branch_meters=spur17["length_meters"] + 1.0
)
assert len(result17_pruned["branches"]) == 1, (
    f"expected the leaf spur pruned by route_road_network() itself, got {len(result17_pruned['branches'])} branches"
)
assert result17_pruned["branches"][0]["branch_role"] == "trunk", "the trunk must be what survives"
assert abs(
    result17_pruned["total_length_meters"] - (result17_kept["total_length_meters"] - spur17["length_meters"])
) < 1e-9, "total_length_meters must drop by exactly the pruned spur's own length"
assert abs(
    result17_pruned["total_served_acres"] - (result17_kept["total_served_acres"] - spur17["newly_served_acres"])
) < 1e-9, "total_served_acres must drop by exactly the pruned spur's own newly_served_acres"
assert abs(
    result17_pruned["unserved_acres"] - (result17_kept["unserved_acres"] + spur17["newly_served_acres"])
) < 1e-9, "unserved_acres must gain back exactly that same acreage"
print(
    f"17. End to end through route_road_network(): section 4's {spur17['length_meters']:.2f} m leaf spur is kept at "
    f"min_leaf_branch_meters=0.0 and pruned at {spur17['length_meters'] + 1.0:.2f}; total_length "
    f"{result17_kept['total_length_meters']:.2f} -> {result17_pruned['total_length_meters']:.2f} m, served "
    f"{result17_kept['total_served_acres']:.4f} -> {result17_pruned['total_served_acres']:.4f} ac. Pruning is wired in."
)


# --- 17b. THE NETWORK-LEVEL total_cost, and the fact that it is summed
# --- AFTER LEAF PRUNING. Two things are under test and they are
# --- different: that the figure is the plain sum of the branches' own
# --- total_cost (nothing re-derived from geometry), and that the branch
# --- list it sums over is the SURVIVING one. Section 4's fixture routes
# --- a trunk and a real leaf spur, so re-running it with
# --- min_leaf_branch_meters above the spur's own measured length gives a
# --- network whose pruned leaf carried real, non-zero cost -- an
# --- implementation that summed before pruning reports that cost for
# --- road it did not return, and fails the second assertion below while
# --- passing nothing else differently. ---

result17b_kept = _route(dem4, cost_raster4, anchor4, demand4, min_leaf_branch_meters=0.0)
trunk17b, spur17b = result17b_kept["branches"]

# (a) The plain sum, over the branches actually returned.
assert "total_cost" in result17b_kept, "route_road_network() must publish a network-level total_cost"
assert abs(
    result17b_kept["total_cost"] - sum(b["total_cost"] for b in result17b_kept["branches"])
) < 1e-9, "network total_cost must be exactly the sum of the returned branches' own total_cost"
assert spur17b["total_cost"] > 0.0, (
    "the fixture is only discriminating if the branch about to be pruned carries real cost"
)

# (b) Summed AFTER pruning: the pruned leaf's cost is NOT in the total.
result17b_pruned = _route(
    dem4, cost_raster4, anchor4, demand4, min_leaf_branch_meters=spur17b["length_meters"] + 1.0
)
assert [b["branch_role"] for b in result17b_pruned["branches"]] == ["trunk"], "the leaf spur must be gone"
assert abs(
    result17b_pruned["total_cost"] - sum(b["total_cost"] for b in result17b_pruned["branches"])
) < 1e-9, "the total must still be the sum over the SURVIVING branches"
assert abs(result17b_pruned["total_cost"] - trunk17b["total_cost"]) < 1e-9, (
    f"with only the trunk surviving, total_cost must be the trunk's own "
    f"{trunk17b['total_cost']:.6f}, got {result17b_pruned['total_cost']:.6f}"
)
assert result17b_pruned["total_cost"] < result17b_kept["total_cost"] - 1e-9, (
    f"summing before pruning would leave the pruned spur's {spur17b['total_cost']:.6f} in the "
    f"total: kept {result17b_kept['total_cost']:.6f} vs pruned {result17b_pruned['total_cost']:.6f}"
)

# (c) Every branch pruned -> the empty-network shape still carries a real
#     0.0, not a missing key: nothing survives, so nothing is summed.
demand17b_none = np.zeros((40, 40), dtype=bool)
result17b_empty = _route(_dem((40, 40)), np.ones((40, 40), dtype=np.float64), (0, 0), demand17b_none)
assert result17b_empty["branches"] == [] and result17b_empty["total_cost"] == 0.0, (
    f"an empty network must carry total_cost 0.0, got {result17b_empty['total_cost']!r}"
)

# (d) The same rule stated directly on topology, independent of any
#     Dijkstra tie-break: hand-picked branch costs, one sub-threshold leaf
#     among them, and the surviving sum worked out by hand.
fixture17b = [
    _branch("trunk", 140.0, 2.40, None, cost=140.0),   # 0  survives
    _branch("spur", 90.0, 1.05, 0, cost=220.0),        # 1  survives
    _branch("spur", 4.0, 0.03, 0, cost=33.0),          # 2  stub -> pruned
]
survivors17b = _prune_leaf_branches(_network(fixture17b), MIN_LEAF_BRANCH_METERS)["branches"]
assert [b["length_meters"] for b in survivors17b] == [140.0, 90.0]
assert sum(b["total_cost"] for b in survivors17b) == 360.0, "140.0 + 220.0, with the stub's 33.0 left out"
assert sum(b["total_cost"] for b in fixture17b) == 393.0, "the pre-pruning sum, which is NOT what is published"

print(
    f"17b. Network total_cost: {result17b_kept['total_cost']:.2f} over 2 branches equals the sum of their own "
    f"total_cost, and summing runs AFTER leaf pruning -- pruning section 4's {spur17b['length_meters']:.2f} m leaf "
    f"drops the total to {result17b_pruned['total_cost']:.2f} (the trunk's own cost), leaving the pruned branch's "
    f"{spur17b['total_cost']:.2f} out. An all-pruned/empty network carries a real 0.0; on the hand-stated "
    "topology the surviving sum is 360.0, not the pre-pruning 393.0."
)


# --- 18. TIMING, report-only, must not assert. Two measurements, BOTH
# --- gated behind ROUTER_TIMING=1 (see the bottom of this section) --
# --- skipped by default because 18b alone takes on the order of 20
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
        f"18a. fixture: 30-acre parcel ({parcel_side_m:.1f}m square) + {_TEST_BUFFER_METERS:.0f}m buffer "
        f"-> {grid_side_m_a:.1f}m grid square at {_TEST_RESOLUTION_METERS}m resolution."
    )
    _run_timing("18a. TIMING (report-only, parcel-scale)", grid_side_m_a, 15.0, _TEST_RESOLUTION_METERS)

    grid_side_m_b = _TEST_MAX_GRID_DIMENSION * _TEST_RESOLUTION_METERS
    demand_acres_target_b = 0.4 * (grid_side_m_b**2) / SQUARE_METERS_PER_ACRE
    print(
        f"18b. fixture: {_TEST_MAX_GRID_DIMENSION}x{_TEST_MAX_GRID_DIMENSION} grid (dem_data.MAX_GRID_DIMENSION, "
        f"the largest grid get_dem_for_boundary() can ever actually return) at {_TEST_RESOLUTION_METERS}m -> "
        f"{grid_side_m_b:.1f}m square (~{(grid_side_m_b**2)/SQUARE_METERS_PER_ACRE:.0f} acres) -- the true "
        "production worst case, still far beyond this tool's stated 'a few to ~20-30 acre' design range."
    )
    _run_timing(
        f"18b. TIMING (report-only, worst-case {_TEST_MAX_GRID_DIMENSION}x{_TEST_MAX_GRID_DIMENSION})",
        grid_side_m_b, demand_acres_target_b, _TEST_RESOLUTION_METERS,
    )
else:
    print(
        "18. TIMING (18a parcel-scale, 18b worst-case) SKIPPED -- report-only, asserts nothing, and 18b alone "
        "takes on the order of 20 minutes. Set ROUTER_TIMING=1 to run both and see the numbers."
    )


print("\nAll test_road_network_router.py checks passed.")
