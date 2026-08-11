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
PRODUCTION_SERVICE_RADIUS_METERS = 100.0

# Stopping threshold, in real meters of NEW road construction per newly
# served acre -- once the cheapest remaining extension (by
# accumulated_cost per acre, see SELECT above) would cost more real
# distance than this per acre it newly serves, the router stops rather
# than accepting it. Deliberately a real-distance figure, not a cost
# figure -- see this module's own docstring for why SELECT and STOP use
# different ratios. CONFIGURABLE, same unvalidated-starting-value caveat.
MAX_ROAD_METERS_PER_SERVED_ACRE = 100.0

# Real-meters ceiling on the water spur's own NEW construction length
# (existing-road cells the spur happens to reuse don't count against
# this, same "cells already part of an accepted branch contribute 0"
# rule every other branch's own length_meters obeys) -- a spur that
# would have to build more than this much fresh road to reach the
# cheapest reachable water_target_cells entry is skipped entirely rather
# than accepted at any cost. CONFIGURABLE, same unvalidated-starting-
# value caveat.
MAX_WATER_SPUR_METERS = 150.0

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


def _kernel_targets(
    r: int, c: int, kernel_dr: np.ndarray, kernel_dc: np.ndarray, rows: int, cols: int
) -> tuple[np.ndarray, np.ndarray]:
    """Every (row, col) the service-radius kernel reaches from (r, c), clipped to the grid."""
    nr = kernel_dr + r
    nc = kernel_dc + c
    valid = (nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
    return nr[valid], nc[valid]


def _enter_coverage(
    r: int,
    c: int,
    cover_count: np.ndarray,
    demand_mask: np.ndarray,
    kernel_dr: np.ndarray,
    kernel_dc: np.ndarray,
    rows: int,
    cols: int,
    cell_area: float,
) -> float:
    """
    Counts cell (r, c)'s own service disc IN to cover_count. Returns the
    newly_served_acres delta -- the real acreage of every demand cell
    whose cover_count transitioned 0 -> 1 as a direct result, i.e. cells
    (r, c) is the FIRST currently-active path cell to reach. Mutates
    cover_count in place; paired 1:1 with _leave_coverage() below (same
    kernel, opposite direction) so a caller can undo this exactly.
    """
    nr, nc = _kernel_targets(r, c, kernel_dr, kernel_dc, rows, cols)
    cover_count[nr, nc] += 1
    newly_covered = demand_mask[nr, nc] & (cover_count[nr, nc] == 1)
    return float(np.count_nonzero(newly_covered)) * cell_area


def _leave_coverage(
    r: int,
    c: int,
    cover_count: np.ndarray,
    demand_mask: np.ndarray,
    kernel_dr: np.ndarray,
    kernel_dc: np.ndarray,
    rows: int,
    cols: int,
    cell_area: float,
) -> float:
    """
    Exact inverse of _enter_coverage(): counts (r, c)'s own service disc
    back OUT. Returns the newly_served_acres delta that must be
    subtracted -- the acreage of every demand cell whose cover_count
    transitioned 1 -> 0, i.e. (r, c) was the ONLY currently-active path
    cell still covering it. Relies on strict LIFO enter/leave nesting (a
    node's own subtree is always fully entered-and-left before this
    runs) to guarantee this exactly mirrors that node's own
    _enter_coverage() call.
    """
    nr, nc = _kernel_targets(r, c, kernel_dr, kernel_dc, rows, cols)
    cover_count[nr, nc] -= 1
    newly_uncovered = demand_mask[nr, nc] & (cover_count[nr, nc] == 0)
    return float(np.count_nonzero(newly_uncovered)) * cell_area


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
    demand_mask: np.ndarray,
    kernel_dr: np.ndarray,
    kernel_dc: np.ndarray,
    rows: int,
    cols: int,
    cell_area: float,
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
    EVALUATES candidates, it never itself commits anything.
    """
    served = np.zeros((rows, cols), dtype=np.float64)
    running = 0.0
    stack: list[tuple[tuple[int, int], bool]] = [(anchor_cell, False)]

    while stack:
        node, leaving = stack.pop()
        r, c = node
        if leaving:
            running -= _leave_coverage(r, c, cover_count, demand_mask, kernel_dr, kernel_dc, rows, cols, cell_area)
            continue

        running += _enter_coverage(r, c, cover_count, demand_mask, kernel_dr, kernel_dc, rows, cols, cell_area)
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


def route_road_network(
    dem: dict,
    cost_raster: np.ndarray,
    anchor_cell: tuple[int, int],
    demand_mask: np.ndarray,
    water_target_cells: Optional[list[tuple[int, int]]] = None,
    service_radius_meters: float = PRODUCTION_SERVICE_RADIUS_METERS,
    max_meters_per_served_acre: float = MAX_ROAD_METERS_PER_SERVED_ACRE,
    max_water_spur_meters: float = MAX_WATER_SPUR_METERS,
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
          "stop_reason": "all_demand_served" | "no_reachable_demand" | "cost_per_acre_exceeded",
        }

    branches is a real, reportable [] (never None, never an error) when
    anchor_cell's own baseline coverage already serves every acre of real
    demand, or when no demand is reachable from it at all, or when even
    the very first candidate's meters-per-acre is already too expensive.

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

    total_demand_acres = float(np.sum(demand_mask)) * cell_area

    cover_count = np.zeros((rows, cols), dtype=np.int64)
    total_served_acres = _enter_coverage(
        anchor_cell[0], anchor_cell[1], cover_count, demand_mask, kernel_dr, kernel_dc, rows, cols, cell_area
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
            anchor_cell, children, cover_count, demand_mask, kernel_dr, kernel_dc, rows, cols, cell_area
        )

        best_cell = _select_best_candidate(served, field["accumulated_cost"])
        if best_cell is None:
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
            _enter_coverage(cell[0], cell[1], cover_count, demand_mask, kernel_dr, kernel_dc, rows, cols, cell_area)
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
    return {
        "branches": branches,
        "total_length_meters": float(sum(branch["length_meters"] for branch in branches)),
        "total_served_acres": total_served_acres,
        "unserved_acres": unserved_acres,
        "stop_reason": stop_reason,
    }
