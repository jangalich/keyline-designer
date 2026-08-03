"""
road_cost_path.py

Standalone cost-surface + least-cost-path (LCP) core — the substrate this
pipeline intends to eventually route road_corridors.py's corridor
generation through, replacing the contour-band/ridge-top generators'
current approach of finding a clean line through an eligible cell blob
and hoping it's a reasonable route. That wiring is a later prompt; this
module has zero callers yet. It is deliberately standalone and importable
against nothing but raster_grid.py (and plain numpy), same reasoning as
valley_delineation.py's own docstring: independently unit-testable against
a synthetic DEM, without any of the several real network fetches or the
existing road_corridors.py pipeline it will eventually feed.

Pipeline:

    DEM + slope + exclusions --> per-cell traversal cost raster -->
    multi-source/multi-destination Dijkstra over that raster (or Yen's
    algorithm, layered on top, for several distinct alternatives) -->
    ordered path cells --> (x, y, elevation) points

Hard exclusion vs. soft cost penalty, current design (this module's own
responsibility only covers the SLOPE half of that split -- see each
constant/param's own docstring below for exactly what moved where):
  - HARD (np.inf in cost_raster, a route genuinely cannot cross it at
    all): excluded_mask -- which is now the CALLER's responsibility to
    build correctly, expected to already fold in off-boundary ground, the
    selected water-system zone (buffered), AND production zone(s) (see
    road_corridors.py once a later prompt wires this in) -- plus any cell
    where slope_pct itself is NaN (undefined terrain, see build_cost_
    raster()'s own docstring).
  - SOFT (a finite cost penalty, a route CAN still choose to pay it):
    grade (an unbounded quadratic penalty -- there is no grade ceiling in
    this design anymore, unlike the hard MAX_ROAD_GRADE_PCT cutoff earlier
    prompts used) and floodplain/riparian ground (a flat additive penalty,
    moved here from being a hard exclusion in earlier prompts).

build_cost_raster() turns that split into one per-cell cost grid.
least_cost_path() is Dijkstra, structurally modeled on
valley_delineation.fill_depressions()'s own heap loop: same "a min-heap of
(priority, tie-break counter, row, col) tuples, popped in ascending
priority order, each cell finalized (closed) exactly once" shape. The two
differences are what fill_depressions() seeds the heap from and what it
compares (multi-source at cost 0 here vs. the grid's own valid border at
its own elevation there; a real edge weight — real ground distance times
the target cell's traversal cost — instead of an elevation comparison).
k_shortest_paths() layers Yen's algorithm on top of least_cost_path()
itself (calls it repeatedly against small, per-spur raster copies) to
surface several genuinely distinct alternative routes, not just the one
cheapest path.
"""

import heapq
import math
from typing import Optional

import numpy as np

from raster_grid import D8_OFFSETS, pixel_center_xy

# How sharply a cell's cost rises with slope: cost adds
# grade_penalty_weight * slope_pct ** 2 cost-units per cell. UNBOUNDED --
# there is no grade ceiling in this design (unlike earlier prompts' hard
# MAX_ROAD_GRADE_PCT cutoff), so this has no ceiling to normalize against
# either; slope_pct itself (a raw percent grade, e.g. 15.0 for 15%) is
# squared directly. Rescaled from this constant's own earlier
# ceiling-normalized value (weight * (slope_pct / max_grade_pct) ** 2,
# where a cell AT the old 15%-grade ceiling cost 3.0x base) to keep
# roughly the same real-world behavior at that same 15% grade under the
# new unnormalized formula: 3.0 / 15.0**2 = 0.0133 -- same "rescale a
# weight when the formula it's expressed against changes" technique
# road_corridors.py's own scoring weights already use elsewhere in this
# pipeline. CONFIGURABLE — still a deliberately unvalidated starting
# value, not tuned against any real diagnostic sweep yet; same caveat
# every other threshold in this pipeline carries and same expectation
# that it gets re-tuned once there's a real property to check routed
# candidates against.
GRADE_PENALTY_WEIGHT = 0.0133

# Flat additive cost-units added to any cell inside a floodplain/riparian
# buffer, on top of its grade-based cost -- floodplain/riparian ground
# moved from a HARD exclusion (earlier prompts) to this SOFT penalty under
# the current design: a route can still cross it when the alternative is
# a costly enough detour, it just costs more per cell. CONFIGURABLE, same
# unvalidated-starting-value caveat as GRADE_PENALTY_WEIGHT above -- a
# placeholder magnitude (comparable to a few cells' worth of flat-ground
# travel cost, the same order of magnitude the old, now-removed
# PRODUCTION_CROSSING_COST_PENALTY used), not a tuned trade-off between
# "meters of floodplain crossed" and "extra route distance."
FLOODPLAIN_CROSSING_COST_PENALTY = 5.0

# Baseline cost-per-cell for perfectly flat, unpenalized ground — the
# reference GRADE_PENALTY_WEIGHT and FLOODPLAIN_CROSSING_COST_PENALTY are
# additions on top of. Not itself CONFIGURABLE: it only sets the unit scale
# the other two are expressed in (cost-units per cell of pure travel
# distance), so changing it would just rescale every cost uniformly.
_BASE_TRAVEL_COST = 1.0

# Safety ceiling on how many alternative routes k_shortest_paths() will
# ever generate -- NOT a design target every call is expected to reach.
# Real terrain rarely offers this many genuinely distinct (post-dedup)
# alternatives between one source and one destination; this just bounds
# the worst-case work (each additional route costs up to one full
# least_cost_path() call per cell already on the previous route).
# CONFIGURABLE.
MAX_ROUTES_TO_GENERATE = 8

# A candidate route is rejected as a near-duplicate of an already-accepted
# one if the fraction of its own cells also present in that accepted
# route's cells is at or above this threshold (see _cell_overlap_fraction()
# -- the fraction is computed against the SMALLER of the two routes' own
# cell counts, so a short near-duplicate spur off a much longer accepted
# route is still caught correctly, not diluted by the longer route's
# extra unique cells the way a plain Jaccard/union-based fraction would
# be). 0.7 is a deliberately unvalidated starting value, same CONFIGURABLE
# caveat as every other threshold in this module.
ROUTE_DEDUP_OVERLAP_THRESHOLD = 0.7


def build_cost_raster(
    dem: dict,
    slope_pct: np.ndarray,
    excluded_mask: np.ndarray,
    floodplain_mask: Optional[np.ndarray] = None,
    grade_penalty_weight: float = GRADE_PENALTY_WEIGHT,
    floodplain_penalty: float = FLOODPLAIN_CROSSING_COST_PENALTY,
) -> np.ndarray:
    """
    Per-cell traversal-cost grid, same shape as dem['array'].

    np.inf wherever excluded_mask is True or slope_pct is NaN (an edge/
    nodata-adjacent cell terrain_metrics.compute_slope_and_aspect() can't
    compute a real slope for at all -- folded into the same "not real,
    traversable ground" bucket excluded_mask represents, rather than left
    to produce a NaN cost that would silently corrupt the Dijkstra heap
    comparisons in least_cost_path()).

    excluded_mask is entirely the CALLER's own responsibility to build
    correctly -- under the current design it's expected to already
    include off-boundary cells, the selected water-system zone (buffered),
    AND production zone(s), all HARD exclusions (see road_corridors.py
    once a later prompt wires this in). This module has no
    max_grade_pct or production_mask parameter anymore: grade has no hard
    ceiling here at all (an arbitrarily steep, non-excluded cell is always
    traversable, just increasingly expensive -- see grade_penalty_weight
    below), and production is no longer this module's own soft term --
    it's now the caller's job to fold into excluded_mask before calling
    this at all, since the current design treats it as hard, not soft.

    Every other cell gets a finite cost: _BASE_TRAVEL_COST (raw travel
    distance) plus grade_penalty_weight * slope_pct ** 2 -- an UNBOUNDED
    quadratic grade penalty (see that constant's own comment for why it's
    no longer normalized against any ceiling) -- plus floodplain_penalty
    for any cell where floodplain_mask is True (floodplain_mask omitted or
    None means no floodplain penalty at all, e.g. a caller that hasn't
    computed one yet).
    """
    array = dem["array"]
    rows, cols = array.shape

    hard_excluded = excluded_mask | np.isnan(slope_pct)
    traversable = ~hard_excluded

    grade_pct_squared = np.zeros((rows, cols), dtype=np.float64)
    grade_pct_squared[traversable] = slope_pct[traversable].astype(np.float64) ** 2

    cost = np.full((rows, cols), _BASE_TRAVEL_COST, dtype=np.float64)
    cost += grade_penalty_weight * grade_pct_squared

    if floodplain_mask is not None:
        cost[floodplain_mask] += floodplain_penalty

    cost[hard_excluded] = np.inf
    return cost


def least_cost_path(
    dem: dict,
    cost_raster: np.ndarray,
    source_cells: list[tuple[int, int]],
    destination_cells: list[tuple[int, int]],
) -> Optional[dict]:
    """
    Multi-source, multi-destination Dijkstra over D8_OFFSETS adjacency,
    weighted by cost_raster. Returns the single cheapest path from ANY
    source cell to ANY destination cell:

        {
            "cells": [(r, c), ...],       # source -> destination, in order
            "total_cost": float,
            "source_cell": (r, c),
            "destination_cell": (r, c),   # whichever destination candidate actually won
        }

    Returns None if no destination cell is reachable at all (every path is
    blocked by hard exclusions) -- a real, reportable routing outcome, not
    an error condition.

    Same heap-loop shape as valley_delineation.fill_depressions(): a
    min-heap of (cost, tie-break counter, row, col), each cell closed
    (finalized) exactly once. Seeded from every source cell at cost 0
    instead of fill_depressions()'s grid border; edge weight is real
    ground distance to a neighbor (math.hypot(dc * px, dr * py)) times
    that neighbor's own cost_raster value, instead of an elevation
    comparison. Terminates the instant any destination cell is popped off
    the heap -- Dijkstra's own ascending-priority pop order guarantees
    that's the cheapest reachable destination across every source/
    destination pairing at once, so there's no need to run this once per
    candidate pair separately.
    """
    rows, cols = cost_raster.shape
    px, py = dem["resolution_meters"]
    destinations = set(destination_cells)

    best_cost = np.full((rows, cols), np.inf, dtype=np.float64)
    came_from: dict[tuple[int, int], Optional[tuple[int, int]]] = {}
    origin_source: dict[tuple[int, int], tuple[int, int]] = {}
    closed = np.zeros((rows, cols), dtype=bool)

    heap: list[tuple[float, int, int, int]] = []
    counter = 0

    for r, c in source_cells:
        if not np.isfinite(cost_raster[r, c]) or best_cost[r, c] <= 0.0:
            continue
        best_cost[r, c] = 0.0
        came_from[(r, c)] = None
        origin_source[(r, c)] = (r, c)
        heapq.heappush(heap, (0.0, counter, r, c))
        counter += 1

    while heap:
        cost, _, r, c = heapq.heappop(heap)
        if closed[r, c]:
            continue
        closed[r, c] = True

        if (r, c) in destinations:
            cells = [(r, c)]
            cur = (r, c)
            while came_from[cur] is not None:
                cur = came_from[cur]
                cells.append(cur)
            cells.reverse()
            return {
                "cells": cells,
                "total_cost": float(cost),
                "source_cell": origin_source[(r, c)],
                "destination_cell": (r, c),
            }

        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < rows and 0 <= nc < cols) or closed[nr, nc]:
                continue
            neighbor_cost = cost_raster[nr, nc]
            if not np.isfinite(neighbor_cost):
                continue
            new_cost = cost + math.hypot(dc * px, dr * py) * neighbor_cost
            if new_cost < best_cost[nr, nc]:
                best_cost[nr, nc] = new_cost
                came_from[(nr, nc)] = (r, c)
                origin_source[(nr, nc)] = origin_source[(r, c)]
                heapq.heappush(heap, (new_cost, counter, nr, nc))
                counter += 1

    return None


def _path_cumulative_costs(dem: dict, cost_raster: np.ndarray, cells: list[tuple[int, int]]) -> list[float]:
    """Running cost total at each index of an already-found path's own
    `cells` list (index 0 is always 0.0, the source itself), using the
    exact same edge-weight formula least_cost_path() itself accumulates
    with (real ground distance to the next cell times that next cell's own
    cost_raster value) -- lets k_shortest_paths() know the true cost of
    the ROOT portion of a previous route up to any given spur node,
    without re-running Dijkstra just to ask that question."""
    px, py = dem["resolution_meters"]
    cumulative = [0.0]
    for (r1, c1), (r2, c2) in zip(cells, cells[1:]):
        step_cost = math.hypot((c2 - c1) * px, (r2 - r1) * py) * cost_raster[r2, c2]
        cumulative.append(cumulative[-1] + step_cost)
    return cumulative


def _cell_overlap_fraction(cells_a: list[tuple[int, int]], cells_b: list[tuple[int, int]]) -> float:
    """
    Fraction of the SMALLER route's own cells that also appear in the
    other route -- deliberately divided by min(len(a), len(b)), not
    len(union) (Jaccard): a short near-duplicate deviation off a much
    longer accepted route should still register as heavily overlapping,
    and a plain union-based fraction would understate that (diluted by
    the longer route's many extra unique cells). 0.0 if either route is
    empty.
    """
    set_a, set_b = set(cells_a), set(cells_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / min(len(set_a), len(set_b))


def k_shortest_paths(
    dem: dict,
    cost_raster: np.ndarray,
    source_cell: tuple[int, int],
    destination_cell: tuple[int, int],
    max_routes: int = MAX_ROUTES_TO_GENERATE,
    dedup_overlap_threshold: float = ROUTE_DEDUP_OVERLAP_THRESHOLD,
) -> list[dict]:
    """
    Yen's algorithm, layered on top of least_cost_path() (calls it
    repeatedly, never reimplements Dijkstra itself): generates the single
    shortest source->destination route first, then repeatedly finds the
    next-cheapest genuine DEVIATION from an already-accepted route --
    a "spur" path from each node along that route, searched against a
    per-spur COPY of cost_raster with already-used ground temporarily
    blocked (np.inf) so the spur is forced to actually diverge rather than
    immediately rediscovering the same route:

      - every node on the route's own root path UP TO (not including) the
        spur node is blocked outright, so the spur can't loop back through
        ground this route already used to get there;
      - whichever specific next cell any already-accepted OR already-
        queued candidate route sharing this exact same root took from the
        spur node is ALSO blocked -- a node-level stand-in for excluding
        that one directed edge (this raster-based Dijkstra has no separate
        per-edge cost to exclude), forcing the spur into a genuinely
        different next step. Checking already-queued candidates too (not
        just accepted routes, which is all classic Yen's algorithm
        requires) isn't necessary for correctness -- an exact-duplicate
        candidate is already caught by the cells-based dedup below -- but
        it steers the search toward discovering a genuinely NEW deviation
        at that spur point instead of just re-deriving one already sitting
        in the candidate pool, which serves this function's actual goal
        (real diversity, not just distinctness) better.

    A candidate deviation is only ACCEPTED if its cell-overlap fraction
    (_cell_overlap_fraction()) against EVERY already-accepted route is
    below dedup_overlap_threshold -- once a candidate fails that check
    against some accepted route, it can never pass later (the accepted
    set only grows), so a rejected candidate is dropped for good, not
    reconsidered. Stops as soon as max_routes routes are accepted OR the
    candidate pool is exhausted (no undiscovered deviation exists, or
    every remaining one is too similar to something already accepted) --
    returns whatever was actually found either way, never padded to
    max_routes with a manufactured near-duplicate.

    Returns a list of dicts, each the exact same shape least_cost_path()
    itself returns ({"cells", "total_cost", "source_cell",
    "destination_cell"}), so path_cells_to_points_xyz() works unchanged on
    any of them. Returns [] if even the first (cheapest) route isn't
    reachable at all -- same "no path found" outcome least_cost_path()
    itself reports as None, just list-shaped here since every OTHER
    return path from this function is a list.
    """
    first_path = least_cost_path(dem, cost_raster, [source_cell], [destination_cell])
    if first_path is None:
        return []

    accepted = [first_path]

    candidates: list[dict] = []
    seen_candidate_cells: set[tuple[tuple[int, int], ...]] = set()

    while len(accepted) < max_routes:
        previous_cells = accepted[-1]["cells"]
        cumulative_costs = _path_cumulative_costs(dem, cost_raster, previous_cells)

        for i in range(len(previous_cells) - 1):
            spur_node = previous_cells[i]
            root_path = previous_cells[: i + 1]

            spur_cost_raster = cost_raster.copy()
            for node in root_path[:-1]:
                spur_cost_raster[node] = np.inf
            for other in accepted + candidates:
                other_cells = other["cells"]
                if len(other_cells) > i and other_cells[: i + 1] == root_path:
                    spur_cost_raster[other_cells[i + 1]] = np.inf

            spur_result = least_cost_path(dem, spur_cost_raster, [spur_node], [destination_cell])
            if spur_result is None:
                continue

            full_cells = root_path[:-1] + spur_result["cells"]
            full_cells_key = tuple(full_cells)
            if full_cells_key in seen_candidate_cells:
                continue
            seen_candidate_cells.add(full_cells_key)

            candidates.append(
                {
                    "cells": full_cells,
                    "total_cost": cumulative_costs[i] + spur_result["total_cost"],
                    "source_cell": source_cell,
                    "destination_cell": destination_cell,
                }
            )

        if not candidates:
            break  # candidate pool exhausted -- no undiscovered deviation exists at all

        candidates.sort(key=lambda c: c["total_cost"])
        next_route = None
        remaining = []
        for candidate in candidates:
            if next_route is None and all(
                _cell_overlap_fraction(candidate["cells"], a["cells"]) < dedup_overlap_threshold
                for a in accepted
            ):
                next_route = candidate
            else:
                remaining.append(candidate)
        candidates = remaining

        if next_route is None:
            break  # every remaining candidate is too similar to something already accepted

        accepted.append(next_route)

    return accepted


def path_cells_to_points_xyz(dem: dict, cells: list[tuple[int, int]]) -> list[tuple[float, float, float]]:
    """
    Thin wrapper over raster_grid.pixel_center_xy() + dem['array']: turns
    least_cost_path()'s ordered (row, col) cells into the same points_xyz
    shape (a list of (x, y, elevation_m) tuples, in the DEM's own
    projected CRS) road_corridors.py's _pca_centerline() and
    _generate_ridge_candidate_runs() already produce -- so a future prompt
    can swap a cost-path route in as a third points_xyz source without
    touching anchoring, scoring, or GeoJSON output downstream.
    """
    array = dem["array"]
    return [(*pixel_center_xy(dem, r, c), float(array[r, c])) for r, c in cells]


if __name__ == "__main__":
    # Offline smoke test, two DEMs sharing the same source/destination row
    # and the same central column obstacle band, differing only in which
    # gaps that band leaves open:
    #
    #   - TWO-ROUTE DEM: the obstacle leaves a gap near the top AND a gap
    #     near the bottom -- two genuinely distinct, roughly comparable-
    #     cost ways around it. k_shortest_paths() should return both,
    #     as two DIFFERENT routes (one through each gap), not two
    #     near-identical variants of the same detour.
    #   - ONE-ROUTE DEM: the same obstacle, but the top gap is closed too
    #     -- only the bottom gap offers a legal way across at all.
    #     k_shortest_paths() should return exactly one route (dedup
    #     correctly refuses to manufacture a near-duplicate second one
    #     just to pad the list).
    size = 21
    center_row = size // 2
    obstacle_col = size // 2
    array = np.zeros((size, size), dtype=np.float32)
    slope_pct = np.zeros((size, size), dtype=np.float32)

    def _obstacle_mask(top_gap: bool, bottom_gap: bool) -> np.ndarray:
        mask = np.zeros((size, size), dtype=bool)
        mask[:, obstacle_col] = True
        if top_gap:
            mask[1, obstacle_col] = False
        if bottom_gap:
            mask[size - 2, obstacle_col] = False
        return mask

    dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }
    source_cell = (center_row, 0)
    destination_cell = (center_row, size - 1)

    print("--- Two legal routes (gaps at both ends of the obstacle) ---")
    two_route_excluded = _obstacle_mask(top_gap=True, bottom_gap=True)
    two_route_cost_raster = build_cost_raster(dem, slope_pct, two_route_excluded)
    two_routes = k_shortest_paths(dem, two_route_cost_raster, source_cell, destination_cell, max_routes=4)

    print(f"Routes found: {len(two_routes)}")
    for i, route in enumerate(two_routes):
        uses_top_gap = (1, obstacle_col) in route["cells"]
        uses_bottom_gap = (size - 2, obstacle_col) in route["cells"]
        print(
            f"  Route {i}: {len(route['cells'])} cells, total_cost={route['total_cost']:.2f}, "
            f"via top gap={uses_top_gap}, via bottom gap={uses_bottom_gap}"
        )

    assert len(two_routes) >= 2, f"expected at least 2 distinct routes around a two-sided obstacle, got {len(two_routes)}"
    gap_sides = set()
    for route in two_routes[:2]:
        if (1, obstacle_col) in route["cells"]:
            gap_sides.add("top")
        if (size - 2, obstacle_col) in route["cells"]:
            gap_sides.add("bottom")
    assert gap_sides == {"top", "bottom"}, (
        f"expected the top-2 routes to use DIFFERENT gaps (one each), got sides used: {gap_sides}"
    )
    overlap = _cell_overlap_fraction(two_routes[0]["cells"], two_routes[1]["cells"])
    assert overlap < ROUTE_DEDUP_OVERLAP_THRESHOLD, (
        f"the two accepted routes overlap {overlap:.2f}, at or above the dedup threshold -- they should be "
        f"genuinely distinct, not near-identical variants of the same detour"
    )
    print(f"First two routes use different gaps and overlap only {overlap:.2f} (below the dedup threshold).\n")

    print("--- One legal route (the top gap is also blocked) ---")
    one_route_excluded = _obstacle_mask(top_gap=False, bottom_gap=True)
    one_route_cost_raster = build_cost_raster(dem, slope_pct, one_route_excluded)
    one_routes = k_shortest_paths(dem, one_route_cost_raster, source_cell, destination_cell, max_routes=4)

    print(f"Routes found: {len(one_routes)}")
    for i, route in enumerate(one_routes):
        print(f"  Route {i}: {len(route['cells'])} cells, total_cost={route['total_cost']:.2f}")

    assert len(one_routes) == 1, (
        f"expected exactly one route when only one side of the obstacle is legal, got {len(one_routes)} -- "
        f"dedup should have refused to pad the list with a near-duplicate"
    )
    assert all((size - 2, obstacle_col) in one_routes[0]["cells"] for _ in [None]), (
        "the one route found must actually use the only open gap"
    )
    print("Exactly one route found, correctly not padded with a manufactured near-duplicate.\n")

    print("Smoke test passed.")
