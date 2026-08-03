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
    multi-source/multi-destination Dijkstra over that raster --> ordered
    path cells --> (x, y, elevation) points

build_cost_raster() keeps today's two HARD exclusions (excluded_mask,
max-grade ceiling) exactly as hard as they are in road_corridors.py's own
constraint stack — a corridor still cannot be generated through a
water-system buffer, floodplain, or a grade over the pinned ceiling, full
stop, not just discouraged. Everything else becomes a finite, differentiable
cost: gentler cells cost close to their raw travel distance, cells near the
legal grade ceiling cost meaningfully more (a quadratic grade penalty, not
linear, so the cost curve steepens sharply as a candidate route approaches
the ceiling rather than staying comfortably below it), and cells inside a
production zone carry a flat additive penalty (a real but soft preference,
same "crossing production land is a valid option, just a worse one" stance
road_corridors.py's own docstring already takes towards production zones).

least_cost_path() is Dijkstra, structurally modeled on
valley_delineation.fill_depressions()'s own heap loop: same "a min-heap of
(priority, tie-break counter, row, col) tuples, popped in ascending
priority order, each cell finalized (closed) exactly once" shape. The two
differences are what fill_depressions() seeds the heap from and what it
compares (multi-source at cost 0 here vs. the grid's own valid border at
its own elevation there; a real edge weight — real ground distance times
the target cell's traversal cost — instead of an elevation comparison).
"""

import heapq
import math
from typing import Optional

import numpy as np

from raster_grid import D8_OFFSETS, pixel_center_xy

# How sharply a cell's cost rises as its slope approaches max_grade_pct:
# cost adds GRADE_PENALTY_WEIGHT * (slope_pct / max_grade_pct) ** 2 cost-
# units per cell (so a cell right at the legal ceiling costs
# GRADE_PENALTY_WEIGHT above a flat cell's cost of 1.0, and a cell at half
# the ceiling costs only a quarter of that). CONFIGURABLE — a deliberately
# unvalidated starting value, not tuned against any real diagnostic sweep
# yet; same caveat every other threshold in this pipeline carries (see e.g.
# water_candidate_zones.py's own CONFIGURABLE constants) and same
# expectation that it gets re-tuned once there's a real property to check
# routed candidates against.
GRADE_PENALTY_WEIGHT = 3.0

# Flat additive cost-units added to any cell inside a production zone, on
# top of its grade-based cost. CONFIGURABLE, same unvalidated-starting-
# value caveat as GRADE_PENALTY_WEIGHT above — this is a placeholder
# magnitude (comparable to a few cells' worth of flat-ground travel cost),
# not a tuned trade-off between "acres of production land crossed" and
# "extra route distance."
PRODUCTION_CROSSING_COST_PENALTY = 5.0

# Baseline cost-per-cell for perfectly flat, unpenalized ground — the
# reference GRADE_PENALTY_WEIGHT and PRODUCTION_CROSSING_COST_PENALTY are
# additions on top of. Not itself CONFIGURABLE: it only sets the unit scale
# the other two are expressed in (cost-units per cell of pure travel
# distance), so changing it would just rescale every cost uniformly.
_BASE_TRAVEL_COST = 1.0


def build_cost_raster(
    dem: dict,
    slope_pct: np.ndarray,
    excluded_mask: np.ndarray,
    max_grade_pct: float,
    production_mask: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    Per-cell traversal-cost grid, same shape as dem['array'].

    np.inf wherever excluded_mask is True, slope_pct > max_grade_pct, or
    slope_pct is NaN (an edge/nodata-adjacent cell terrain_metrics.
    compute_slope_and_aspect() can't compute a real slope for at all --
    folded into the same "not real, traversable ground" bucket
    excluded_mask represents, rather than left to produce a NaN cost that
    would silently corrupt the Dijkstra heap comparisons in
    least_cost_path()). These stay HARD exclusions, exactly as they are in
    today's road_corridors.py constraint stack -- not softened into a large
    finite cost.

    Every other cell gets a finite cost: _BASE_TRAVEL_COST (raw travel
    distance) plus GRADE_PENALTY_WEIGHT * (slope_pct / max_grade_pct) ** 2
    (a quadratic grade penalty -- cells near the legal ceiling cost
    meaningfully more than comfortably-gentle ones), plus
    PRODUCTION_CROSSING_COST_PENALTY for any cell where production_mask is
    True (production_mask omitted or None means no production-zone
    penalty at all, e.g. a caller that hasn't computed one yet).
    """
    array = dem["array"]
    rows, cols = array.shape

    hard_excluded = excluded_mask | np.isnan(slope_pct) | (slope_pct > max_grade_pct)
    traversable = ~hard_excluded

    grade_ratio = np.zeros((rows, cols), dtype=np.float64)
    grade_ratio[traversable] = slope_pct[traversable] / max_grade_pct

    cost = np.full((rows, cols), _BASE_TRAVEL_COST, dtype=np.float64)
    cost += GRADE_PENALTY_WEIGHT * grade_ratio**2

    if production_mask is not None:
        cost[production_mask] += PRODUCTION_CROSSING_COST_PENALTY

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
    # Offline smoke test: a flat synthetic DEM with a deliberate obstacle
    # -- a production-zone band sitting squarely on the straight line
    # between a source and a destination, with a clear, cheap detour
    # around one end of it. Confirms least_cost_path() actually detours
    # around the obstacle instead of paying to cross it, and that the
    # detour's total_cost beats what a straight line through the obstacle
    # would have cost.
    size = 21
    array = np.zeros((size, size), dtype=np.float32)
    slope_pct = np.zeros((size, size), dtype=np.float32)
    excluded_mask = np.zeros((size, size), dtype=bool)

    center_row = size // 2
    obstacle_col = size // 2
    # A short vertical wall of production land, centered on the direct
    # source->destination row, with open lanes a few cells above and below
    # it -- a real detour, not a dead end.
    production_mask = np.zeros((size, size), dtype=bool)
    production_mask[center_row - 2 : center_row + 3, obstacle_col] = True

    dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    max_grade_pct = 15.0
    cost_raster = build_cost_raster(dem, slope_pct, excluded_mask, max_grade_pct, production_mask)

    source_cells = [(center_row, 0)]
    destination_cells = [(center_row, size - 1)]

    result = least_cost_path(dem, cost_raster, source_cells, destination_cells)

    if result is None:
        print("No path found -- smoke test DEM is misconfigured (obstacle fully blocks the grid).")
    else:
        crosses_obstacle = any(production_mask[r, c] for r, c in result["cells"])
        straight_line_cells = [(center_row, col) for col in range(size)]
        straight_line_cost = sum(
            5.0 * cost_raster[r, c] for r, c in straight_line_cells[1:]  # first cell is the source, cost-free
        )

        print(f"Path found: {len(result['cells'])} cells, total_cost={result['total_cost']:.2f}")
        print(f"Source: {result['source_cell']}, destination: {result['destination_cell']}")
        print(f"Path crosses the production obstacle: {crosses_obstacle}")
        print(f"Straight-line-through-obstacle cost would have been: {straight_line_cost:.2f}")

        assert not crosses_obstacle, "expected the path to detour around the production obstacle, not cross it"
        assert result["total_cost"] < straight_line_cost, (
            f"expected the detour ({result['total_cost']:.2f}) to cost less than "
            f"a straight line through the obstacle ({straight_line_cost:.2f})"
        )
        print("Smoke test passed: least-cost path detours around the obstacle and beats the straight-line cost.")
