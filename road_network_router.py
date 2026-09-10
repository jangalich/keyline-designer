"""
road_network_router.py

Coverage-greedy road network router over a precomputed cost surface
(road_cost_path.build_cost_raster()'s own output) -- the core of the road
corridor rewrite this module's own docstring in the originating prompt
describes as replacing ridge-fragment detection entirely. Instead of
finding a landform and routing a corridor to it, this grows a road network
outward from a REAL anchor point (the existing named-road access point),
one branch at a time, choosing each extension by how much production
access it buys per unit of routing cost, and stopping once further road
stops being worth building. The terminus is an OUTPUT of this algorithm,
never an input -- unlike every earlier ridge-fragment/contour-band
generator this replaces, nothing here is told in advance where the road
should end.

This module wires into nothing: it imports numpy, math, raster_grid, and
road_cost_path only, and neither road_corridors.py nor any pipeline caller
references it yet -- see route_road_network()'s own docstring for the
entry point this eventually feeds.

ALGORITHM, restated in implementation terms (see route_road_network()'s
own docstring for the full contract):

  Each iteration runs one multi-source-to-all Dijkstra
  (road_cost_path.cost_distance_field()) from the anchor over a
  "working" cost raster (identical to the caller's own cost_raster,
  except every cell already part of a previously accepted branch is
  reset to EXISTING_ROAD_TRAVERSAL_COST -- a small positive epsilon, not
  0.0, since a zero-weight edge would silently break Dijkstra's own
  optimality guarantee, the same reasoning road_cost_path.py's own
  mandatory positivity assertion exists for). That produces a full
  shortest-path TREE rooted at the anchor, covering every reachable cell
  in the grid.

  This module then walks that tree once, by DFS, to find -- for every
  reachable cell t -- newly_served_acres(t): the real, non-overlapping
  NEW production acreage a route from the anchor to t would bring within
  service_radius_meters, over and above whatever earlier accepted
  branches (plus the anchor itself, which is always assumed to already
  provide baseline coverage as real, existing infrastructure) already
  serve. Coverage is monotone down the tree -- route(t) is always
  route(parent(t)) plus exactly one new cell -- so this is computed
  EXACTLY, in one linear tree walk, via a single persistent int
  "cover_count" grid and a reversible increment/decrement around each
  node's visit (see _enter_coverage()/_leave_coverage() below): entering
  a node counts its own service disc IN, leaving it counts that same
  disc back OUT, so sibling subtrees never see each other's temporary
  coverage and a candidate's served() value only ever reflects cells
  that are its own ancestors' and its own. Recomputing every candidate's
  coverage from scratch (or summing per-cell coverage without the
  counter) would either be far too slow over a real grid or would
  double-count overlapping service discs -- both are exactly what this
  reversible-counter technique avoids.

  Among every node with newly_served_acres > 0, the node minimizing
  accumulated_cost(t) / newly_served_acres(t) is selected -- terrain
  quality (accumulated_cost, which already reflects grade/floodplain/TPI/
  production penalties) decides between competing candidates. Before
  that candidate is accepted, its length_meters(t) / newly_served_acres(t)
  -- a plain, terrain-blind meters-per-acre figure a person can actually
  evaluate -- is checked against max_meters_per_served_acre. These two
  ratios are DELIBERATELY different quantities measuring different
  things (accumulated cost for selection, real distance for the stopping
  rule) -- see route_road_network()'s own docstring for why they must
  stay that way.

  Once that loop ends and the water spur has had its one attempt, a
  single LEAF-PRUNING pass drops every terminal branch shorter than
  MIN_LEAF_BRANCH_METERS -- the 5-7 m stubs a coverage-greedy loop
  legitimately accepts when a cell or two of demand sits just past the
  end of a real road, and which add no value and read as noise on the
  rendered map. It is a cleanup over the finished topology, not part of
  the growth rule: it changes no selection, no stopping decision and no
  coverage count, it never touches the trunk or a water_spur, and it
  never cascades. See _prune_leaf_branches() for all of that in detail.
"""

import math
from typing import Optional

import numpy as np

from raster_grid import build_disc_kernel_offsets, cell_area_acres
from road_cost_path import backtrace_route, cost_distance_field

# Radius (meters) within which a production cell counts as "served" by a
# road cell -- the service-area disc kernel this module builds once, via
# raster_grid.build_disc_kernel_offsets(), and reuses for every node
# visited across every iteration. CONFIGURABLE, same deliberately-
# unvalidated-starting-value caveat every other threshold in this
# pipeline carries -- not tuned against any real diagnostic sweep yet.
PRODUCTION_SERVICE_RADIUS_METERS = 25.0

# Stopping threshold, in real meters of NEW road construction per newly
# served acre -- once the cheapest remaining extension (by
# accumulated_cost per acre, see SELECT above) would cost more real
# distance than this per acre it newly serves, the router stops rather
# than accepting it. Deliberately a real-distance figure, not a cost
# figure -- see this module's own docstring for why SELECT and STOP use
# different ratios.
#
# 500.0 was CHOSEN BY SWEEPING this ceiling against a real reference
# parcel and comparing the rendered networks side by side -- it is not
# derived from anything, and no closed form produces it. On that terrain
# 500 produced materially better networks than the 200 it replaces: 200
# stopped the router while real, close production ground was still
# unserved. CONFIGURABLE, and still carries the same unvalidated-
# starting-value caveat every other threshold here does -- one reference
# parcel read by eye is a better starting point than a guess, not a
# validated figure.
MAX_ROAD_METERS_PER_SERVED_ACRE = 500.0

# Real-meters ceiling on the water spur's own NEW construction length
# (existing-road cells the spur happens to reuse don't count against
# this, same "cells already part of an accepted branch contribute 0"
# rule every other branch's own length_meters obeys) -- a spur that
# would have to build more than this much fresh road to reach the
# cheapest reachable water_target_cells entry is skipped entirely rather
# than accepted at any cost. CONFIGURABLE, same unvalidated-starting-
# value caveat.
MAX_WATER_SPUR_METERS = 150.0

# Minimum real length, in meters, a TERMINAL branch must reach to be
# worth building at all -- a leaf shorter than this is a stub, one or two
# cells hanging off the end of a real road, and is pruned after routing
# finishes (see _prune_leaf_branches() and route_road_network()'s own
# docstring). Roughly one service radius, deliberately: a spur is only
# worth its own construction if it is long enough to reach ground its
# parent branch does not already serve, and a leaf shorter than the
# service radius by definition cannot. NEVER applied to a non-leaf
# branch, to the trunk, or to a water_spur -- see _prune_leaf_branches()
# for why each of those exemptions exists. CONFIGURABLE, same
# unvalidated-starting-value caveat as every other threshold here.
MIN_LEAF_BRANCH_METERS = 25.0

# Traversal cost assigned to every cell already part of an accepted
# branch, once that branch is accepted -- a small POSITIVE epsilon, never
# 0.0 (see this module's own docstring for why a zero-weight edge would
# silently break Dijkstra's optimality guarantee). CONFIGURABLE, same
# unvalidated-starting-value caveat as every other threshold here.
EXISTING_ROAD_TRAVERSAL_COST = 0.01

# Tolerance below which remaining unserved acreage is treated as exactly
# zero -- distinguishes "all_demand_served" (every acre of real demand
# ended up served, up to ordinary float64 summation noise) from
# "no_reachable_demand" (some demand genuinely never got served because
# it was never reachable from the anchor at all). Not itself a tunable
# real-world threshold like the constants above -- purely a float
# comparison guard.
_UNSERVED_ACRES_EPSILON = 1e-9


def _kernel_padding(kernel_dr: np.ndarray, kernel_dc: np.ndarray) -> tuple[int, int]:
    """
    How far the service-radius kernel reaches from its own center, per
    axis -- (0, 0) is always in the kernel (build_disc_kernel_offsets()'s
    own guarantee) and the kernel is point-symmetric about it (the
    disc-radius test is symmetric under negating both dr and dc), so
    padding cover_count/demand_acres by exactly this much on every side
    guarantees every kernel_dr/kernel_dc offset from any real grid cell
    lands in bounds -- no per-call clipping needed inside the DFS walk.
    """
    pad_r = int(np.max(np.abs(kernel_dr))) if kernel_dr.size else 0
    pad_c = int(np.max(np.abs(kernel_dc))) if kernel_dc.size else 0
    return pad_r, pad_c


def _enter_coverage(
    r: int,
    c: int,
    cover_count: np.ndarray,
    demand_acres: np.ndarray,
    kernel_dr: np.ndarray,
    kernel_dc: np.ndarray,
    pad_r: int,
    pad_c: int,
) -> float:
    """
    Counts cell (r, c)'s own service disc IN to cover_count (both already
    padded by pad_r/pad_c -- see _kernel_padding()). Returns the
    newly_served_acres delta -- the real acreage of every demand cell
    whose cover_count transitioned 0 -> 1 as a direct result, i.e. cells
    (r, c) is the FIRST currently-active path cell to reach. Mutates
    cover_count in place; paired 1:1 with _leave_coverage() below (same
    kernel, opposite direction) so a caller can undo this exactly.

    Plain fancy-index `+= 1` is correct (not np.add.at): a disc kernel
    never contains a duplicate (dr, dc) offset, so every target cell in a
    single call is written to exactly once -- there is no aliasing for
    np.add.at's scatter-add to guard against, and it would only add
    overhead here.
    """
    rr = kernel_dr + (r + pad_r)
    cc = kernel_dc + (c + pad_c)
    newly = cover_count[rr, cc] == 0
    cover_count[rr, cc] += 1
    return float(demand_acres[rr, cc][newly].sum())


def _leave_coverage(
    r: int,
    c: int,
    cover_count: np.ndarray,
    demand_acres: np.ndarray,
    kernel_dr: np.ndarray,
    kernel_dc: np.ndarray,
    pad_r: int,
    pad_c: int,
) -> float:
    """
    Exact inverse of _enter_coverage(): counts (r, c)'s own service disc
    back OUT. Returns the newly_served_acres delta that must be
    subtracted -- the acreage of every demand cell whose cover_count
    transitioned 1 -> 0, i.e. (r, c) was the ONLY currently-active path
    cell still covering it. Relies on strict LIFO enter/leave nesting (a
    node's own subtree is always fully entered-and-left before this
    runs) to guarantee this exactly mirrors that node's own
    _enter_coverage() call. Decrements first, then checks which cells
    just landed on 0 -- the exact mirror of _enter_coverage()'s
    check-then-increment order.
    """
    rr = kernel_dr + (r + pad_r)
    cc = kernel_dc + (c + pad_c)
    cover_count[rr, cc] -= 1
    newly_zero = cover_count[rr, cc] == 0
    return float(demand_acres[rr, cc][newly_zero].sum())


def _build_children_map(
    field: dict, anchor_cell: tuple[int, int]
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """
    cost_distance_field()'s own came_from_row/came_from_col, inverted
    into parent -> [children] -- the shape this module's DFS walks.
    Built fresh every iteration (the tree itself changes iteration to
    iteration, as previously accepted branches turn to near-free
    EXISTING_ROAD_TRAVERSAL_COST and pull cheaper paths through them).
    Only reachable cells (finite accumulated_cost -- see
    road_cost_path._dijkstra()'s own -1-sentinel disambiguation rule)
    are included; the anchor itself is the walk's root, never anyone's
    child.
    """
    accumulated_cost = field["accumulated_cost"]
    came_from_row = field["came_from_row"]
    came_from_col = field["came_from_col"]

    reachable_rows, reachable_cols = np.where(np.isfinite(accumulated_cost))
    children: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for r, c in zip(reachable_rows.tolist(), reachable_cols.tolist()):
        if (r, c) == anchor_cell:
            continue
        parent = (int(came_from_row[r, c]), int(came_from_col[r, c]))
        children.setdefault(parent, []).append((r, c))
    return children


def _compute_served_tree(
    anchor_cell: tuple[int, int],
    children: dict[tuple[int, int], list[tuple[int, int]]],
    cover_count: np.ndarray,
    demand_acres: np.ndarray,
    kernel_dr: np.ndarray,
    kernel_dc: np.ndarray,
    pad_r: int,
    pad_c: int,
    rows: int,
    cols: int,
) -> np.ndarray:
    """
    DFS over the tree via an explicit stack (never recursion -- path
    depth can exceed Python's recursion limit on a large grid), recording
    served[r, c] = the cumulative newly_served_acres a route from the
    anchor to (r, c) would bring, for every reachable cell. Every
    push/pop pair is an (enter, leave) event, in the exact LIFO order
    _enter_coverage()/_leave_coverage() need: cover_count (and the
    persistent baseline it started this call at, from every previously
    accepted branch plus the anchor's own initial coverage) is back to
    exactly where it started once this returns -- this walk only ever
    EVALUATES candidates, it never itself commits anything. cover_count
    and demand_acres are both padded (see _kernel_padding()); served
    itself stays unpadded -- it's indexed by real (r, c) tree nodes only.
    """
    served = np.zeros((rows, cols), dtype=np.float64)
    running = 0.0
    stack: list[tuple[tuple[int, int], bool]] = [(anchor_cell, False)]

    while stack:
        node, leaving = stack.pop()
        r, c = node
        if leaving:
            running -= _leave_coverage(r, c, cover_count, demand_acres, kernel_dr, kernel_dc, pad_r, pad_c)
            continue

        running += _enter_coverage(r, c, cover_count, demand_acres, kernel_dr, kernel_dc, pad_r, pad_c)
        served[r, c] = running
        stack.append((node, True))
        for child in children.get(node, []):
            stack.append((child, False))

    return served


def _select_best_candidate(
    served: np.ndarray, accumulated_cost: np.ndarray
) -> Optional[tuple[int, int]]:
    """
    Among every cell with newly_served_acres > 0 and finite
    accumulated_cost, the one minimizing accumulated_cost / served --
    ties broken toward lower accumulated_cost, then lower (row, col), so
    the result is deterministic run to run. Returns None if no cell
    qualifies at all.
    """
    candidate_rows, candidate_cols = np.where(served > 0.0)
    if candidate_rows.size == 0:
        return None

    costs = accumulated_cost[candidate_rows, candidate_cols]
    finite = np.isfinite(costs)
    if not np.any(finite):
        return None

    candidate_rows = candidate_rows[finite]
    candidate_cols = candidate_cols[finite]
    costs = costs[finite]
    served_values = served[candidate_rows, candidate_cols]
    ratios = costs / served_values

    # np.lexsort's LAST key is primary: ratio first, then cost, then row,
    # then col -- exactly the stated tie-break order.
    order = np.lexsort((candidate_cols, candidate_rows, costs, ratios))
    best = int(order[0])
    return (int(candidate_rows[best]), int(candidate_cols[best]))


def _new_length_meters(
    cells: list[tuple[int, int]], accepted_cells: set[tuple[int, int]], px: float, py: float
) -> float:
    """
    Real-world length of NEW construction along an ordered cell path --
    any cell already part of an accepted branch contributes 0 (existing
    road, not new construction), matching exactly the cell set
    working_cost_raster's own epsilon overwrite uses.
    """
    length = 0.0
    for i in range(1, len(cells)):
        if cells[i] in accepted_cells:
            continue
        (r0, c0), (r1, c1) = cells[i - 1], cells[i]
        length += math.hypot((c1 - c0) * px, (r1 - r0) * py)
    return length


def _trim_to_join(
    raw_cells: list[tuple[int, int]], accepted_cells: set[tuple[int, int]]
) -> tuple[list[tuple[int, int]], Optional[tuple[int, int]]]:
    """
    Drops every leading cell already belonging to an accepted branch
    except the last one (the join cell) -- the trunk (accepted_cells
    still empty) is left untouched with no join at all.
    """
    prefix_len = 0
    while prefix_len < len(raw_cells) and raw_cells[prefix_len] in accepted_cells:
        prefix_len += 1

    if prefix_len == 0:
        return raw_cells, None

    join_cell = raw_cells[prefix_len - 1]
    return raw_cells[prefix_len - 1 :], join_cell


def _prune_leaf_branches(network: dict, min_leaf_branch_meters: float) -> dict:
    """
    Removes every too-short TERMINAL branch from a finished network, and
    recomputes the network-level totals to match.

    A branch is a LEAF when no other branch's joins_branch_index names
    it. A leaf shorter than min_leaf_branch_meters is a stub -- one or
    two cells hanging off the end of a real road, serving a fraction of
    an acre -- and is worth neither building nor drawing. Three branches
    are NEVER pruned, however short:

      * A "water_spur". It is short BY DESIGN -- it only has to reach the
        ring of traversable ground just outside the pond buffer, and
        max_water_spur_meters is already its own length rule. Its
        newly_served_acres is 0.0 by construction, so a length test here
        is not measuring coverage at all: pruning it would silently
        delete the parcel's pond access, which is not what this rule is
        for.

      * The trunk. A network whose only branch is a short trunk is a
        legitimately SHORT ROAD -- the road is short because the field is
        close -- not a stub. Deleting it is exactly the mistake the
        deleted network-level corridor-length floor made: it threw away
        correct short networks and reported zero served acres against
        real unserved ground, which reads as failure when the truth is
        the opposite.

      * Any branch that is not a leaf, under any circumstance.

    SINGLE PASS, DELIBERATELY. The leaf set is computed ONCE, against the
    ORIGINAL topology, and exactly those branches are removed. This does
    NOT iterate and does NOT cascade, and an iterate-until-stable loop --
    which looks like the obvious implementation -- would be WRONG here.
    Consider a continuation chain trunk -> A (5 m, joined by B) -> B (7 m,
    leaf): only B is a stub. A is not a leaf, it is the middle of one
    continuous road, and its 5 m is real construction that B's own
    geometry hangs off. Cascading would notice A became a leaf once B
    left and take it too, trimming 12 m off the end of a single
    continuous road rather than removing a stub. One pass removes B and
    stops, which is the intended behavior.

    TOTALS are adjusted by exactly the pruned branches' own figures --
    no coverage recomputation is attempted, and none is needed. Coverage
    is monotone down the branch tree (each branch's newly_served_acres is
    already the MARGINAL acreage it and only it brought in), so a leaf's
    newly_served_acres is EXACTLY the coverage lost when it goes: nothing
    downstream of a leaf exists to have shared it.

    BRANCH ORDER IS PRESERVED EXACTLY -- nothing is re-sorted, and the
    trunk stays first (it is index 0 and is never pruned). But
    joins_branch_index IS re-pointed, and has to be. A branch has no id
    of its own in this module's output: joins_branch_index is a POSITION
    in the "branches" list, and road_corridors.build_road_network()
    re-derives its own "branch_index" field by enumerating that same
    list, which is in turn what the wire feature id
    ("road-corridor-<network>-<branch_index + 1>") and
    wire_translation.check_road_network_complete()'s tree-closure check
    are both built on. Remove a branch from the middle of the list and
    every later position shifts by one, so a join label left at its old
    number silently names the WRONG BRANCH -- or one that is no longer
    there, which the commit gate rejects as incoherent_feature_group on
    every feature in the network.

    The references stay valid as REFERENCES, in other words, only because
    they are rewritten to the positions their own targets now hold. Each
    is remapped through the same old-position -> new-position map the
    surviving list itself defines; a branch whose label does not move
    is passed through as the very same object, untouched. A join target
    can never itself have been pruned -- something joins it, so it is not
    a leaf -- so every label has somewhere to point.

    If every branch is pruned, the empty-network shape is returned with
    stop_reason "all_branches_below_minimum" -- the loop's own original
    stop_reason would be misleading there, since growth is not why this
    network came back empty.
    """
    branches = network["branches"]

    # The leaf set, computed ONCE against the ORIGINAL topology -- see the
    # single-pass paragraph above for why this is never recomputed.
    joined_indices = {
        branch["joins_branch_index"]
        for branch in branches
        if branch["joins_branch_index"] is not None
    }

    pruned_indices = {
        index
        for index, branch in enumerate(branches)
        if index not in joined_indices
        and branch["branch_role"] not in ("trunk", "water_spur")
        and branch["length_meters"] < min_leaf_branch_meters
    }
    if not pruned_indices:
        return network

    pruned = [branches[index] for index in sorted(pruned_indices)]
    pruned_length = sum(branch["length_meters"] for branch in pruned)
    pruned_acres = sum(branch["newly_served_acres"] for branch in pruned)

    # Old position -> new position, over the survivors in their original
    # order. joins_branch_index is a position in this list (see the
    # docstring), so every surviving label is rewritten through this map;
    # a branch whose label is unchanged is passed through as-is rather
    # than copied.
    surviving_indices = [index for index in range(len(branches)) if index not in pruned_indices]
    remapped_index = {old_index: new_index for new_index, old_index in enumerate(surviving_indices)}

    survivors = []
    for old_index in surviving_indices:
        branch = branches[old_index]
        joins = branch["joins_branch_index"]
        new_joins = remapped_index[joins] if joins is not None else None
        survivors.append(branch if new_joins == joins else {**branch, "joins_branch_index": new_joins})

    return {
        "branches": survivors,
        "total_length_meters": float(network["total_length_meters"] - pruned_length),
        "total_served_acres": float(network["total_served_acres"] - pruned_acres),
        "unserved_acres": float(network["unserved_acres"] + pruned_acres),
        "stop_reason": network["stop_reason"] if survivors else "all_branches_below_minimum",
    }


def route_road_network(
    dem: dict,
    cost_raster: np.ndarray,
    anchor_cell: tuple[int, int],
    demand_mask: np.ndarray,
    water_target_cells: Optional[list[tuple[int, int]]] = None,
    service_radius_meters: float = PRODUCTION_SERVICE_RADIUS_METERS,
    max_meters_per_served_acre: float = MAX_ROAD_METERS_PER_SERVED_ACRE,
    max_water_spur_meters: float = MAX_WATER_SPUR_METERS,
    min_leaf_branch_meters: float = MIN_LEAF_BRANCH_METERS,
) -> dict:
    """
    Grows a road network outward from anchor_cell -- the real, existing
    named-road access point this parcel is already reachable from -- one
    branch at a time, greedily by production access bought per unit of
    routing cost, until further road stops being worth building. See
    this module's own docstring for the full algorithm; the essential
    contract:

      SELECT (which candidate wins, every iteration): the node
      minimizing accumulated_cost / newly_served_acres -- accumulated
      cost already reflects grade/floodplain/TPI/production penalties
      from cost_raster, so this lets terrain quality decide between
      competing candidates that serve similar acreage.

      STOP (whether the winning candidate gets built at all): that same
      node's length_meters / newly_served_acres against
      max_meters_per_served_acre -- a plain, terrain-blind real-distance
      figure a person can actually evaluate. These two ratios are
      DELIBERATELY different: unifying them would let a route deep
      inside a production-penalized parcel (very high accumulated_cost,
      but no worse in real meters) get rejected for the wrong reason, or
      a cheap-looking-by-cost but absurdly long detour get accepted for
      the wrong one.

    anchor_cell is treated as already providing PRODUCTION_SERVICE_RADIUS
    coverage on its own, at zero cost, before any branch is ever built --
    it is real, existing infrastructure, not something this router needs
    to construct. Every accepted branch cell afterward is reset to
    EXISTING_ROAD_TRAVERSAL_COST (a small positive epsilon, never 0.0) in
    a fresh working copy of cost_raster, so later iterations route
    through it near-free without breaking Dijkstra's optimality
    guarantee.

    Returns:

        {
          "branches": [
            {
              "cells": [(r, c), ...],         # ordered, joint-cell first
              "branch_role": "trunk" | "spur" | "water_spur",
              "length_meters": float,
              "total_cost": float,
              "newly_served_acres": float,    # 0.0 for water_spur
              "joins_branch_index": int | None,
            }, ...
          ],
          "total_length_meters": float,
          "total_served_acres": float,
          "unserved_acres": float,
          "stop_reason": "no_demand" | "all_demand_served" | "no_reachable_demand"
                         | "cost_per_acre_exceeded" | "all_branches_below_minimum",
        }

    branches is a real, reportable [] (never None, never an error) when
    demand_mask has no True cells at all ("no_demand"), when anchor_cell's
    own baseline coverage already serves every acre of real demand
    ("all_demand_served"), when no remaining demand is reachable from it
    at all ("no_reachable_demand"), when even the very first
    candidate's meters-per-acre is already too expensive
    ("cost_per_acre_exceeded"), or when leaf pruning below removed every
    branch there was ("all_branches_below_minimum" -- reported instead of
    the loop's own reason, which would misdescribe why the network came
    back empty).

    LEAF PRUNING runs last, after the coverage loop AND after the water
    spur step, over the finished topology: every TERMINAL branch shorter
    than min_leaf_branch_meters is dropped, in ONE pass against the
    original topology, and the network totals are adjusted by exactly
    those branches' own figures. The trunk and any "water_spur" are never
    pruned, and neither is any branch that is not a leaf. See
    _prune_leaf_branches() for the full rule and for why cascading is
    wrong. Pruning removes leaves only -- nothing downstream depends on a
    leaf, which is why nothing here is re-routed and no coverage is
    recomputed. Branch ORDER is untouched; the surviving
    joins_branch_index labels are re-pointed at the positions their own
    targets now hold, which is what keeps them referring to the same
    branches they always did.

    water_target_cells, if non-empty, is tried exactly once after the
    main loop ends, regardless of why it ended: the cheapest reachable
    cell in water_target_cells (over the FINAL working_cost_raster, i.e.
    benefiting from every accepted branch's own near-free
    EXISTING_ROAD_TRAVERSAL_COST) is accepted as a "water_spur" branch
    only if its own new (non-existing-road) length is within
    max_water_spur_meters -- otherwise it is skipped and stop_reason is
    left exactly as the main loop set it. water_target_cells must already
    be traversable cells ADJACENT to the water zone itself (which
    cost_raster hard-excludes) -- a cell inside the zone is unreachable
    by construction and would silently never be selected.
    """
    rows, cols = cost_raster.shape
    px, py = dem["resolution_meters"]
    anchor_cell = (int(anchor_cell[0]), int(anchor_cell[1]))
    cell_area = cell_area_acres(dem)

    kernel_offsets = build_disc_kernel_offsets(dem["resolution_meters"], service_radius_meters)
    kernel_dr = np.array([offset[0] for offset in kernel_offsets], dtype=np.int64)
    kernel_dc = np.array([offset[1] for offset in kernel_offsets], dtype=np.int64)
    pad_r, pad_c = _kernel_padding(kernel_dr, kernel_dc)

    total_demand_acres = float(np.sum(demand_mask)) * cell_area

    # cover_count and demand_acres are both padded by (pad_r, pad_c) on
    # every side so every kernel_dr/kernel_dc offset from any real (r, c)
    # lands in bounds -- no per-call clipping inside the DFS walk. The pad
    # itself must read as zero demand acreage everywhere (np.zeros already
    # gives that), so a padded cell can never contribute served area.
    cover_count = np.zeros((rows + 2 * pad_r, cols + 2 * pad_c), dtype=np.int64)
    demand_acres = np.zeros((rows + 2 * pad_r, cols + 2 * pad_c), dtype=np.float64)
    demand_acres[pad_r : pad_r + rows, pad_c : pad_c + cols] = demand_mask.astype(np.float64) * cell_area

    total_served_acres = _enter_coverage(
        anchor_cell[0], anchor_cell[1], cover_count, demand_acres, kernel_dr, kernel_dc, pad_r, pad_c
    )

    working_cost_raster = cost_raster
    accepted_cells: set[tuple[int, int]] = set()
    cell_to_branch_index: dict[tuple[int, int], int] = {}
    branches: list[dict] = []
    stop_reason = ""

    while True:
        field = cost_distance_field(dem, working_cost_raster, [anchor_cell])
        children = _build_children_map(field, anchor_cell)
        served = _compute_served_tree(
            anchor_cell, children, cover_count, demand_acres, kernel_dr, kernel_dc, pad_r, pad_c, rows, cols
        )

        best_cell = _select_best_candidate(served, field["accumulated_cost"])
        if best_cell is None:
            if total_demand_acres <= _UNSERVED_ACRES_EPSILON:
                stop_reason = "no_demand"
            else:
                unserved_acres = max(0.0, total_demand_acres - total_served_acres)
                stop_reason = "all_demand_served" if unserved_acres <= _UNSERVED_ACRES_EPSILON else "no_reachable_demand"
            break

        raw_cells = backtrace_route(field["came_from_row"], field["came_from_col"], best_cell)
        served_acres = float(served[best_cell])
        new_length = _new_length_meters(raw_cells, accepted_cells, px, py)

        if new_length / served_acres > max_meters_per_served_acre:
            stop_reason = "cost_per_acre_exceeded"
            break

        trimmed_cells, join_cell = _trim_to_join(raw_cells, accepted_cells)
        joins_branch_index = cell_to_branch_index[join_cell] if join_cell is not None else None
        branch_index = len(branches)

        for cell in raw_cells:
            _enter_coverage(cell[0], cell[1], cover_count, demand_acres, kernel_dr, kernel_dc, pad_r, pad_c)
        total_served_acres += served_acres

        for cell in trimmed_cells:
            if cell not in cell_to_branch_index:
                cell_to_branch_index[cell] = branch_index
        accepted_cells.update(trimmed_cells)

        accepted_rows = np.array([cell[0] for cell in trimmed_cells], dtype=np.int64)
        accepted_cols = np.array([cell[1] for cell in trimmed_cells], dtype=np.int64)
        working_cost_raster = working_cost_raster.copy()
        working_cost_raster[accepted_rows, accepted_cols] = EXISTING_ROAD_TRAVERSAL_COST

        branches.append(
            {
                "cells": trimmed_cells,
                "branch_role": "trunk" if branch_index == 0 else "spur",
                "length_meters": new_length,
                "total_cost": float(field["accumulated_cost"][best_cell]),
                "newly_served_acres": served_acres,
                "joins_branch_index": joins_branch_index,
            }
        )

    if water_target_cells:
        field = cost_distance_field(dem, working_cost_raster, [anchor_cell])
        accumulated_cost = field["accumulated_cost"]

        reachable_targets = [
            (float(accumulated_cost[target]), target)
            for target in water_target_cells
            if np.isfinite(accumulated_cost[target])
        ]
        if reachable_targets:
            _, best_target = min(reachable_targets, key=lambda entry: (entry[0], entry[1][0], entry[1][1]))
            raw_cells = backtrace_route(field["came_from_row"], field["came_from_col"], best_target)
            new_length = _new_length_meters(raw_cells, accepted_cells, px, py)

            if new_length <= max_water_spur_meters:
                trimmed_cells, join_cell = _trim_to_join(raw_cells, accepted_cells)
                joins_branch_index = cell_to_branch_index[join_cell] if join_cell is not None else None
                branches.append(
                    {
                        "cells": trimmed_cells,
                        "branch_role": "water_spur",
                        "length_meters": new_length,
                        "total_cost": float(accumulated_cost[best_target]),
                        "newly_served_acres": 0.0,
                        "joins_branch_index": joins_branch_index,
                    }
                )

    unserved_acres = max(0.0, total_demand_acres - total_served_acres)
    network = {
        "branches": branches,
        "total_length_meters": float(sum(branch["length_meters"] for branch in branches)),
        "total_served_acres": total_served_acres,
        "unserved_acres": unserved_acres,
        "stop_reason": stop_reason,
    }

    # Leaf pruning, last: after the coverage loop AND after the water spur
    # step, so it sees the network's FINAL topology (a water_spur can join
    # a branch, which makes that branch a non-leaf, and it is exempt
    # itself). Nothing is re-routed or recomputed after this -- only
    # leaves are removed, and nothing downstream depends on a leaf.
    return _prune_leaf_branches(network, min_leaf_branch_meters)
