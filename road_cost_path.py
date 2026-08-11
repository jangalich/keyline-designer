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
    multi-source/multi-destination Dijkstra over that raster -->
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
    # Offline smoke test of least_cost_path(), against DEMs sharing the same
    # source/destination row and the same central column obstacle band,
    # differing only in which gaps (if any) that band leaves open:
    #
    #   - TWO-GAP DEM: a gap near the top AND a gap near the bottom -- a
    #     path is found, and it must actually pass through one of those two
    #     gap cells (Dijkstra picks whichever is cheaper; both are legal).
    #   - ONE-GAP DEM: the top gap is also closed -- only the bottom gap
    #     offers a legal way across, so the returned path must use that
    #     specific cell.
    #   - NO-GAP DEM: the obstacle band is fully closed -- no destination
    #     cell is reachable at all, so least_cost_path() must return None.
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

    print("--- Two-gap obstacle (gaps at both ends of the band) ---")
    no_obstacle = np.zeros((size, size), dtype=bool)
    open_ground_cost_raster = build_cost_raster(dem, slope_pct, no_obstacle)
    open_ground_path = least_cost_path(dem, open_ground_cost_raster, [source_cell], [destination_cell])
    print(f"Obstacle-free total_cost: {open_ground_path['total_cost']:.2f}")

    two_gap_excluded = _obstacle_mask(top_gap=True, bottom_gap=True)
    two_gap_cost_raster = build_cost_raster(dem, slope_pct, two_gap_excluded)
    two_gap_path = least_cost_path(dem, two_gap_cost_raster, [source_cell], [destination_cell])

    assert two_gap_path is not None, "expected a path to be found through a two-gap obstacle"
    uses_top_gap = (1, obstacle_col) in two_gap_path["cells"]
    uses_bottom_gap = (size - 2, obstacle_col) in two_gap_path["cells"]
    print(
        f"Path found: {len(two_gap_path['cells'])} cells, total_cost={two_gap_path['total_cost']:.2f}, "
        f"via top gap={uses_top_gap}, via bottom gap={uses_bottom_gap}"
    )
    assert uses_top_gap or uses_bottom_gap, (
        "expected the path to pass through one of the two open gap cells"
    )
    assert math.isfinite(two_gap_path["total_cost"]), "expected a finite total_cost"
    assert two_gap_path["total_cost"] > open_ground_path["total_cost"], (
        f"expected routing around the obstacle ({two_gap_path['total_cost']:.2f}) to cost strictly more than "
        f"the same source/destination pair on obstacle-free ground ({open_ground_path['total_cost']:.2f})"
    )
    print("Path passes through an open gap and costs strictly more than the obstacle-free route.\n")

    print("--- One-gap obstacle (top gap closed) ---")
    one_gap_excluded = _obstacle_mask(top_gap=False, bottom_gap=True)
    one_gap_cost_raster = build_cost_raster(dem, slope_pct, one_gap_excluded)
    one_gap_path = least_cost_path(dem, one_gap_cost_raster, [source_cell], [destination_cell])

    assert one_gap_path is not None, "expected a path to be found through the one remaining open gap"
    print(f"Path found: {len(one_gap_path['cells'])} cells, total_cost={one_gap_path['total_cost']:.2f}")
    assert (size - 2, obstacle_col) in one_gap_path["cells"], (
        "the path found must use the one open gap cell specifically"
    )
    print("Path correctly uses the one open gap cell.\n")

    print("--- Fully closed obstacle (no gaps at all) ---")
    closed_excluded = _obstacle_mask(top_gap=False, bottom_gap=False)
    closed_cost_raster = build_cost_raster(dem, slope_pct, closed_excluded)
    closed_path = least_cost_path(dem, closed_cost_raster, [source_cell], [destination_cell])

    print(f"Path found: {closed_path}")
    assert closed_path is None, (
        f"expected least_cost_path() to return None when no destination cell is reachable, got {closed_path}"
    )
    print("Correctly returned None -- no destination reachable.\n")

    print("Smoke test passed.")
