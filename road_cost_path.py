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
responsibility now covers the SLOPE, RIDGE-PREFERENCE, and PRODUCTION
terms too -- see each constant/param's own docstring below for exactly
what moved where):
  - HARD (np.inf in cost_raster, a route genuinely cannot cross it at
    all): excluded_mask -- which remains the CALLER's responsibility to
    build correctly, expected to fold in off-boundary ground and the
    selected water-system zone (buffered) -- plus any cell where
    slope_pct itself is NaN (undefined terrain, see build_cost_raster()'s
    own docstring), plus, when the caller opts in via
    impassable_grade_pct, any cell whose slope_pct exceeds that ceiling
    (a grade no farm road would ever be built on, distinct from
    excluded_mask itself -- see build_cost_raster()'s own docstring).
    Production ground is deliberately NOT part of this hard bucket
    anymore (see below) -- a caller building excluded_mask should not
    fold production zones into it under the current design.
  - SOFT (a finite cost penalty, a route CAN still choose to pay it):
    grade (an unbounded quadratic penalty by default -- see
    impassable_grade_pct above for the opt-in hard ceiling instead),
    floodplain/riparian ground (a flat additive penalty), canopy/woody
    vegetation (a flat additive penalty, structurally identical to the
    floodplain one -- see the canopy_mask parameter's own docstring), a TPI
    ridge-preference discount/premium (scales only the base travel term,
    cheaper on crests/spurs and costlier in hollows/draws -- see the tpi
    parameter's own docstring), and production-land traversal (a
    proportional multiplier on the total cost so far -- see the
    production_mask parameter's own docstring for why this moved back to
    SOFT after an earlier design made it HARD: a parcel that's mostly
    production ground can have no non-production route that serves
    anything at all, and a hard exclusion there makes the router come
    back with nothing rather than an honest, if costly, answer).

build_cost_raster() turns that split into one per-cell cost grid.

_dijkstra() is the shared heap loop underneath everything else in this
module, structurally modeled on valley_delineation.fill_depressions()'s
own heap loop: same "a min-heap of (priority, tie-break counter, row,
col) tuples, popped in ascending priority order, each cell finalized
(closed) exactly once" shape. The two differences are what
fill_depressions() seeds the heap from and what it compares (multi-source
at cost 0 here vs. the grid's own valid border at its own elevation
there; a real edge weight — real ground distance times the target cell's
traversal cost — instead of an elevation comparison). It can run either
point-to-point (stopping the instant any destination cell is popped —
Dijkstra's own ascending-priority pop order guarantees that's the
cheapest reachable destination across every source/destination pairing at
once) or one-to-all (run to exhaustion, no destination_cells given at
all) over the same accumulated-cost field. least_cost_path() is the
point-to-point caller; cost_distance_field() is the one-to-all caller —
see each function's own docstring.
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

# Flat additive cost-units added to any cell inside the canopy (woody
# vegetation) mask, on top of its grade-based cost -- structurally the
# SAME "flat additive SOFT penalty a route can still choose to pay"
# pattern as FLOODPLAIN_CROSSING_COST_PENALTY above, applied at the same
# stage (before the production multiplier, see build_cost_raster()), just
# against a different mask. Crossing standing timber is a real cost to a
# farm road (clearing, stumping, root systems), but it is deliberately
# SOFT here, not a hard exclusion -- a route can still cut through a stand
# of trees when detouring around it costs more. CONFIGURABLE, same
# deliberately-unvalidated-starting-value caveat as every other threshold
# in this module: this happens to START at the same order of magnitude as
# FLOODPLAIN_CROSSING_COST_PENALTY (a few cells' worth of flat-ground
# travel), but it is its own independent knob, not coupled to it, and is
# expected to be re-tuned once there's a real property to check routed
# candidates against.
CANOPY_CROSSING_COST_PENALTY = 5.0

# Baseline cost-per-cell for perfectly flat, unpenalized ground — the
# reference GRADE_PENALTY_WEIGHT and FLOODPLAIN_CROSSING_COST_PENALTY are
# additions on top of. Not itself CONFIGURABLE: it only sets the unit scale
# the other two are expressed in (cost-units per cell of pure travel
# distance), so changing it would just rescale every cost uniformly.
_BASE_TRAVEL_COST = 1.0

# Fraction of _BASE_TRAVEL_COST that TPI ridge preference can swing the
# base travel term by, in either direction: a strongly positive-TPI cell
# (crest/spur) costs as little as (1 - TPI_PREFERENCE_STRENGTH) times base
# travel cost, a strongly negative-TPI cell (hollow/draw) as much as
# (1 + TPI_PREFERENCE_STRENGTH) times base travel cost, with tanh()
# smoothly bounding every value in between (see build_cost_raster()'s own
# docstring for the exact formula). Values at or above 1.0 would drive the
# scaled base cost to zero or negative for a strongly-positive-TPI cell --
# rejected by this function's own mandatory positivity assertion, not
# silently clamped. CONFIGURABLE, same deliberately-unvalidated-starting-
# value caveat every other threshold in this module already carries.
TPI_PREFERENCE_STRENGTH = 0.5

# TPI magnitude (meters) at which the tanh() ridge-preference response is
# roughly two-thirds saturated (tanh(1) ≈ 0.762) -- i.e. the relief scale
# this module treats as "clearly a ridge or clearly a hollow" on a small
# farm parcel, not a borderline/near-flat reading. CONFIGURABLE, same
# unvalidated-starting-value caveat as TPI_PREFERENCE_STRENGTH above.
TPI_REFERENCE_METERS = 3.0

# Multiplier applied to a production-land cell's total cost (grade +
# floodplain + TPI-scaled travel, all of it) -- crossing cultivated ground
# is proportionally more costly whatever the terrain underneath happens to
# be, not a flat add-on the way FLOODPLAIN_CROSSING_COST_PENALTY is.
# CONFIGURABLE, same unvalidated-starting-value caveat as every other
# threshold in this module.
PRODUCTION_TRAVERSAL_COST_MULTIPLIER = 3.0


def build_cost_raster(
    dem: dict,
    slope_pct: np.ndarray,
    excluded_mask: np.ndarray,
    floodplain_mask: Optional[np.ndarray] = None,
    grade_penalty_weight: float = GRADE_PENALTY_WEIGHT,
    floodplain_penalty: float = FLOODPLAIN_CROSSING_COST_PENALTY,
    tpi: Optional[np.ndarray] = None,
    production_mask: Optional[np.ndarray] = None,
    impassable_grade_pct: Optional[float] = None,
    tpi_preference_strength: float = TPI_PREFERENCE_STRENGTH,
    tpi_reference_meters: float = TPI_REFERENCE_METERS,
    production_multiplier: float = PRODUCTION_TRAVERSAL_COST_MULTIPLIER,
    canopy_mask: Optional[np.ndarray] = None,
    canopy_penalty: float = CANOPY_CROSSING_COST_PENALTY,
) -> np.ndarray:
    """
    Per-cell traversal-cost grid, same shape as dem['array'].

    Every new parameter below (tpi, production_mask, impassable_grade_pct,
    canopy_mask, and the tuning knobs that go with them) is OPTIONAL and
    defaults to behavior IDENTICAL to this function's own earlier signature
    -- an existing caller that never passes any of them gets exactly the
    same cost raster as before this extension.

    excluded_mask remains entirely the CALLER's own responsibility to
    build correctly -- expected to already include off-boundary cells and
    the selected water-system zone (buffered), a HARD exclusion in both
    directions (np.inf, never traversable at any cost). It is NOT expected
    to fold production ground in anymore -- see production_mask below for
    why that moved back to a soft term here.

    Exact order of operations:

      1. hard_excluded = excluded_mask | np.isnan(slope_pct) -- np.inf
         wherever excluded_mask is True or slope_pct is NaN (an edge/
         nodata-adjacent cell terrain_metrics.compute_slope_and_aspect()
         can't compute a real slope for at all -- folded into the same
         "not real, traversable ground" bucket excluded_mask represents,
         rather than left to produce a NaN cost that would silently
         corrupt the Dijkstra heap comparisons in least_cost_path()).
      2. If impassable_grade_pct is given, any cell whose slope_pct
         exceeds it (strictly greater-than) is ADDITIONALLY marked
         hard_excluded -- an explicit hard grade ceiling, opt-in only:
         None (the default) leaves grade fully unbounded, same as before
         this parameter existed (an arbitrarily steep, non-excluded cell
         is always traversable, just increasingly expensive -- see
         grade_penalty_weight below). A cell exactly AT the ceiling is
         still finite (the comparison is strictly greater-than, not
         greater-or-equal).
      3. tpi_factor is 1.0 everywhere by default. Wherever tpi is
         supplied AND finite at that cell:
             tpi_factor = 1.0 - tpi_preference_strength * tanh(tpi / tpi_reference_meters)
         Positive TPI (crest/spur, locally high ground) yields a factor
         below 1.0 -- cheaper to cross. Negative TPI (hollow/draw) yields
         a factor above 1.0 -- costlier. tanh() bounds the factor to the
         open interval (1 - tpi_preference_strength, 1 + tpi_preference_
         strength) no matter how extreme a single cell's TPI reads, so one
         outlier cell can never dominate the surface.
         NaN in tpi is NEUTRAL, not excluded: it gets tpi_factor 1.0,
         exactly as if tpi had not been supplied for that cell at all.
         This is a DIFFERENT meaning of NaN from slope_pct's own (step 1
         above) -- topographic_position.compute_tpi() legitimately returns
         NaN at grid edges and in small-neighborhood cells by design (see
         that module's own docstring), and that ground is ordinary,
         traversable ground, not undefined terrain. A NaN tpi value must
         never propagate into the cost raster or make a cell impassable.
      4. cost = _BASE_TRAVEL_COST * tpi_factor
               + grade_penalty_weight * slope_pct ** 2
               + floodplain_penalty wherever floodplain_mask is True
               + canopy_penalty wherever canopy_mask is True
         tpi_factor scales ONLY the base travel term -- it must NOT scale
         the grade, floodplain, or canopy penalties, so a ridge cell that
         also happens to be steep still pays the full grade cost, not a
         ridge-discounted one. grade_penalty_weight * slope_pct ** 2 is
         itself still an UNBOUNDED quadratic penalty by default (see that
         constant's own comment) unless impassable_grade_pct (step 2) caps
         it off entirely. floodplain_penalty is added for any cell where
         floodplain_mask is True (floodplain_mask omitted or None means no
         floodplain penalty at all, e.g. a caller that hasn't computed one
         yet). canopy_penalty is added for any cell where canopy_mask is
         True, the exact same flat-additive way -- a SOFT woody-vegetation
         crossing term (canopy_mask omitted or None means no canopy
         penalty at all, e.g. a caller whose canopy fetch failed and chose
         to degrade gracefully rather than abort; see the canopy_mask
         parameter's own docstring). Both flat penalties are added here,
         BEFORE the production multiplier in step 5, so on production land
         they are scaled by it exactly as the grade/travel terms are.
      5. cost[production_mask] *= production_multiplier, applied to the
         FULL total from step 4 (grade + floodplain + canopy + TPI-scaled
         travel together), not just the base travel term. production_mask is a
         SOFT term again under the current design -- reversed from an
         earlier design that made production land the caller's job to
         fold into excluded_mask as a HARD exclusion. That reversal is
         deliberate: on a parcel that's mostly production ground, there
         may be no non-production route that serves anything at all, and
         a hard exclusion there makes the router come back with nothing
         rather than an honest, if costly, answer. Crossing cultivated
         ground is instead made proportionally expensive, whatever the
         terrain underneath -- a route can still choose to pay it when
         every alternative costs more. production_mask omitted or None
         means no production penalty at all, same as any other optional
         mask in this function.
      6. cost[hard_excluded] = np.inf -- applied LAST, so nothing in steps
         3-5 can ever pull a hard-excluded cell back to a finite cost.

    MANDATORY POSITIVITY ASSERTION: after step 6, every finite cell in the
    returned raster must be strictly greater than zero. This is a
    permanent tripwire, not a debug check (same reasoning as fencing.py's
    own containment assertion) -- a zero or negative edge weight would
    silently break Dijkstra's optimality guarantee: least_cost_path()
    would still return *a* path, just not necessarily the cheapest one,
    with nothing to indicate the failure. Raises ValueError naming the
    offending cell count and the minimum offending value if this ever
    fires -- in practice this can only happen with a misconfigured
    tpi_preference_strength >= 1.0 (see that constant's own comment).

    canopy_mask is an already-computed boolean woody-vegetation grid, same
    shape as dem['array'], True wherever a cell should pay the flat
    canopy_penalty, or None (no canopy penalty at all -- identical to this
    function's behavior before this parameter existed). It is the SAME kind
    of caller-supplied SOFT mask as floodplain_mask and carries no special
    boundary handling here: an off-parcel True cell is harmless because
    every off-parcel cell is already in excluded_mask and gets np.inf in
    step 6 regardless (step 6 runs LAST, after this penalty is added), so
    the caller is free to pass a raw whole-grid canopy mask without first
    intersecting it against the parcel. NaN has no meaning here -- this is
    a plain boolean mask, not a float field like tpi.

    tpi is an already-computed TPI grid (topographic_position.
    compute_tpi()'s own output), same shape as dem['array'], or None (no
    ridge preference at all, tpi_factor stays 1.0 everywhere -- identical
    to this function's behavior before this parameter existed). IMPORTANT
    caller-side constraint, spanning this module and dem_data.py, that has
    no other documentation: the radius_meters compute_tpi() was called
    with must not exceed dem_data.DEFAULT_BUFFER_METERS (currently
    100.0m), the buffer dem_data.get_dem_for_boundary() fetches past the
    drawn property boundary. TPI carries a kernel-truncation bias reaching
    exactly one radius inward from the DEM grid's own edge (a cell whose
    disc neighborhood gets clipped by the grid boundary reads a biased
    mean). At a 50m TPI radius against the DEM's own 100m fetch buffer,
    that bias dies out entirely within the buffer, 50m outside the
    property boundary itself. At a TPI radius above 100m, the bias would
    reach onto real property ground -- worst right at the boundary, which
    is exactly where a road corridor's own anchor point tends to sit.
    """
    array = dem["array"]
    rows, cols = array.shape

    hard_excluded = excluded_mask | np.isnan(slope_pct)
    if impassable_grade_pct is not None:
        hard_excluded = hard_excluded | (slope_pct > impassable_grade_pct)
    traversable = ~hard_excluded

    grade_pct_squared = np.zeros((rows, cols), dtype=np.float64)
    grade_pct_squared[traversable] = slope_pct[traversable].astype(np.float64) ** 2

    tpi_factor = np.ones((rows, cols), dtype=np.float64)
    if tpi is not None:
        tpi_valid = np.isfinite(tpi)
        tpi_factor[tpi_valid] = 1.0 - tpi_preference_strength * np.tanh(
            tpi[tpi_valid].astype(np.float64) / tpi_reference_meters
        )

    cost = _BASE_TRAVEL_COST * tpi_factor
    cost += grade_penalty_weight * grade_pct_squared

    if floodplain_mask is not None:
        cost[floodplain_mask] += floodplain_penalty

    if canopy_mask is not None:
        cost[canopy_mask] += canopy_penalty

    if production_mask is not None:
        cost[production_mask] *= production_multiplier

    cost[hard_excluded] = np.inf

    finite_mask = np.isfinite(cost)
    non_positive = finite_mask & (cost <= 0.0)
    if np.any(non_positive):
        bad_count = int(np.sum(non_positive))
        min_value = float(cost[finite_mask].min())
        raise ValueError(
            f"build_cost_raster() produced {bad_count} finite cell(s) with cost <= 0 "
            f"(minimum finite value {min_value}) -- every finite cell must be strictly "
            "positive or Dijkstra's optimality guarantee in least_cost_path()/"
            "cost_distance_field() silently breaks. This is most likely caused by "
            "tpi_preference_strength >= 1.0 driving a strongly ridge-preferred cell's "
            "base travel cost to zero or negative."
        )

    return cost


def _dijkstra(
    dem: dict,
    cost_raster: np.ndarray,
    source_cells: list[tuple[int, int]],
    destination_cells: Optional[list[tuple[int, int]]] = None,
) -> dict:
    """
    Multi-source Dijkstra over D8_OFFSETS adjacency, weighted by
    cost_raster -- the shared heap loop underneath both least_cost_path()
    (point-to-point) and cost_distance_field() (one-to-all). See this
    module's own docstring for the overall shape.

    destination_cells=None runs to exhaustion: every cell reachable from
    any source_cells entry gets its own finalized accumulated_cost/
    came_from before the heap empties. destination_cells given (as in
    least_cost_path()'s own use) terminates the instant any ONE of those
    cells is popped off the heap -- Dijkstra's own ascending-priority pop
    order guarantees that's the cheapest reachable destination across
    every source/destination pairing at once, so there's no need to run
    this once per candidate pair separately, and no need to keep going
    past that point.

    Returns:

        {
            "accumulated_cost": np.ndarray[float64] (rows, cols),
            "came_from_row": np.ndarray[int32] (rows, cols),
            "came_from_col": np.ndarray[int32] (rows, cols),
            "origin_row": np.ndarray[int32] (rows, cols),
            "origin_col": np.ndarray[int32] (rows, cols),
            "reached_cell": (row, col) or None,
        }

    accumulated_cost is np.inf at any cell never reached at all (either
    because destination_cells cut the search short before that cell was
    finalized, or because it's genuinely unreachable -- every path to it
    is blocked by a hard exclusion) and 0.0 at every seeded source cell.

    came_from_row/came_from_col hold the ABSOLUTE (row, col) of the
    upstream neighbor each cell was reached from, same convention as
    valley_delineation.compute_flow_direction()'s own flow_to_row/
    flow_to_col: separate same-shape int32 arrays (not stacked on a third
    axis), filled with -1. origin_row/origin_col hold the ABSOLUTE
    (row, col) of whichever source cell that cell's own cheapest path
    traces back to, same -1 fill.

    AMBIGUOUS SENTINEL, read this before touching either array directly:
    -1 in came_from_row/came_from_col does NOT by itself mean "this is a
    source cell" -- it equally means "this cell was never reached at
    all," since a source cell's own came_from is never set (there is no
    upstream neighbor to record) and an unreached cell's came_from is
    likewise never set (relaxation never touched it). The two cases are
    only distinguishable via accumulated_cost at that same cell:
    np.isfinite(accumulated_cost[r, c]) means (r, c) is a source cell (or
    was otherwise reached -- see below); accumulated_cost[r, c] == np.inf
    means (r, c) was never reached. This is exactly the kind of sentinel
    ambiguity this codebase has been bitten by before -- never read
    came_from_row/came_from_col == -1 alone as "source."

    reached_cell is the (row, col) that triggered early termination when
    destination_cells was given and one of its cells was popped off the
    heap, or None if destination_cells was None (ran to exhaustion) or if
    none of destination_cells was ever reachable at all.

    CRITICAL: the heap tuple shape (cost, counter, r, c) with a
    monotonically increasing tie-break counter, the neighbor visit order
    (D8_OFFSETS, unchanged), the strict `new_cost < best_cost[nr, nc]`
    relaxation comparison, the `closed` check placement (both on pop and
    in the neighbor loop), the edge weight formula (math.hypot(dc * px,
    dr * py) * neighbor cost), and the source-seeding guard below all
    determine WHICH of several equal-cost routes wins when there's a tie
    -- do not restructure any of them.
    """
    rows, cols = cost_raster.shape
    px, py = dem["resolution_meters"]
    destinations = set(destination_cells) if destination_cells is not None else None

    best_cost = np.full((rows, cols), np.inf, dtype=np.float64)
    came_from_row = np.full((rows, cols), -1, dtype=np.int32)
    came_from_col = np.full((rows, cols), -1, dtype=np.int32)
    origin_row = np.full((rows, cols), -1, dtype=np.int32)
    origin_col = np.full((rows, cols), -1, dtype=np.int32)
    closed = np.zeros((rows, cols), dtype=bool)

    heap: list[tuple[float, int, int, int]] = []
    counter = 0

    for r, c in source_cells:
        if not np.isfinite(cost_raster[r, c]) or best_cost[r, c] <= 0.0:
            continue
        best_cost[r, c] = 0.0
        origin_row[r, c] = r
        origin_col[r, c] = c
        heapq.heappush(heap, (0.0, counter, r, c))
        counter += 1

    reached_cell: Optional[tuple[int, int]] = None

    while heap:
        cost, _, r, c = heapq.heappop(heap)
        if closed[r, c]:
            continue
        closed[r, c] = True

        if destinations is not None and (r, c) in destinations:
            reached_cell = (r, c)
            break

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
                came_from_row[nr, nc] = r
                came_from_col[nr, nc] = c
                origin_row[nr, nc] = origin_row[r, c]
                origin_col[nr, nc] = origin_col[r, c]
                heapq.heappush(heap, (new_cost, counter, nr, nc))
                counter += 1

    return {
        "accumulated_cost": best_cost,
        "came_from_row": came_from_row,
        "came_from_col": came_from_col,
        "origin_row": origin_row,
        "origin_col": origin_col,
        "reached_cell": reached_cell,
    }


def backtrace_route(
    came_from_row: np.ndarray,
    came_from_col: np.ndarray,
    cell: tuple[int, int],
) -> list[tuple[int, int]]:
    """
    Walks came_from_row/came_from_col (in the exact shape _dijkstra()
    returns them -- ABSOLUTE row/col, -1 at a source/unreached cell, see
    _dijkstra()'s own docstring for the disambiguation rule) backward from
    `cell` to the source cell its own path traces back to, then reverses
    the walk into source-to-cell order -- the same ordering
    least_cost_path() has always returned in its own "cells" field.

    `cell` is expected to have actually been reached (i.e. its own
    accumulated_cost is finite) -- this walks until it hits a cell whose
    came_from is (-1, -1), which for a reached cell only ever means "this
    is the source it was traced from," never "unreached" (an unreached
    cell has no path to walk in the first place).
    """
    cells = [cell]
    r, c = cell
    while True:
        pr, pc = int(came_from_row[r, c]), int(came_from_col[r, c])
        if pr == -1 and pc == -1:
            break
        cells.append((pr, pc))
        r, c = pr, pc
    cells.reverse()
    return cells


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

    Thin wrapper over _dijkstra() (point-to-point: terminates the instant
    any destination cell is popped) + backtrace_route() -- see both of
    those functions' own docstrings for the heap-loop shape and the
    backtrace walk. This function's own observable behavior (signature,
    return shape, which path wins among equal-cost alternatives) is
    unchanged from before that split.
    """
    result = _dijkstra(dem, cost_raster, source_cells, destination_cells)
    reached_cell = result["reached_cell"]
    if reached_cell is None:
        return None

    r, c = reached_cell
    cells = backtrace_route(result["came_from_row"], result["came_from_col"], reached_cell)
    source_cell = (int(result["origin_row"][r, c]), int(result["origin_col"][r, c]))

    return {
        "cells": cells,
        "total_cost": float(result["accumulated_cost"][r, c]),
        "source_cell": source_cell,
        "destination_cell": reached_cell,
    }


def cost_distance_field(
    dem: dict,
    cost_raster: np.ndarray,
    source_cells: list[tuple[int, int]],
) -> dict:
    """
    One-to-all (well, multi-source-to-all) accumulated-cost field: runs
    _dijkstra() to exhaustion (no destination_cells at all) from
    source_cells, over the whole cost_raster grid, and returns the full
    field:

        {
            "accumulated_cost": np.ndarray[float64] (rows, cols),
            "came_from_row": np.ndarray[int32] (rows, cols),
            "came_from_col": np.ndarray[int32] (rows, cols),
            "origin_row": np.ndarray[int32] (rows, cols),
            "origin_col": np.ndarray[int32] (rows, cols),
        }

    accumulated_cost is np.inf at any cell unreachable from every one of
    source_cells (blocked by hard exclusions in every direction) and 0.0
    at each seeded source cell itself. came_from_row/came_from_col/
    origin_row/origin_col follow _dijkstra()'s own -1-filled, ABSOLUTE-
    row/col convention -- see _dijkstra()'s own docstring for the full
    "-1 is ambiguous between source and unreached, disambiguate via
    np.isfinite(accumulated_cost)" rule, which applies here identically.

    Intended for a caller that wants to evaluate many candidate cells
    against a single accumulated-cost field (e.g. a coverage-greedy
    router scoring every cell in the DEM as a potential route endpoint)
    without running a separate least_cost_path() search per candidate.
    Use backtrace_route() to recover the actual path to any specific
    reached cell from the came_from_row/came_from_col this returns.
    """
    result = _dijkstra(dem, cost_raster, source_cells, destination_cells=None)
    return {
        "accumulated_cost": result["accumulated_cost"],
        "came_from_row": result["came_from_row"],
        "came_from_col": result["came_from_col"],
        "origin_row": result["origin_row"],
        "origin_col": result["origin_col"],
    }


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
    # Offline smoke test. First, least_cost_path() against DEMs sharing the
    # same source/destination row and the same central column obstacle
    # band, differing only in which gaps (if any) that band leaves open:
    #
    #   - TWO-GAP DEM: a gap near the top AND a gap near the bottom -- a
    #     path is found, and it must actually pass through one of those two
    #     gap cells (Dijkstra picks whichever is cheaper; both are legal).
    #   - ONE-GAP DEM: the top gap is also closed -- only the bottom gap
    #     offers a legal way across, so the returned path must use that
    #     specific cell.
    #   - NO-GAP DEM: the obstacle band is fully closed -- no destination
    #     cell is reachable at all, so least_cost_path() must return None.
    #
    # Then, cost_distance_field()/backtrace_route() -- the one-to-all
    # exposure this module's _dijkstra() now supports underneath
    # least_cost_path() -- against: (a) a hand-derived octile-distance
    # check on open ground, (b) the same fully-closed obstacle band,
    # proving the -1 came_from sentinel's source-vs-unreached
    # disambiguation rule holds, and (c) a cross-validation against
    # least_cost_path()'s own point-to-point answer on the two-gap DEM.
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

    print("--- cost_distance_field(): uniform open ground, hand-derived octile distance ---")
    octile_source = (10, 0)
    field = cost_distance_field(dem, open_ground_cost_raster, [octile_source])
    accumulated_cost = field["accumulated_cost"]

    _diagonal_unit_cost = math.hypot(5.0, 5.0)  # 7.0710678..., full precision so 1e-9 checks hold at any distance

    def _expected_octile_cost(source, target):
        dr = abs(target[0] - source[0])
        dc = abs(target[1] - source[1])
        diag = min(dr, dc)
        straight = max(dr, dc) - diag
        return _diagonal_unit_cost * diag + 5.0 * straight

    octile_check_cells = [(10, 0), (10, 10), (10, 20), (0, 0), (20, 20), (0, 20), (15, 5)]
    for cell in octile_check_cells:
        actual = float(accumulated_cost[cell])
        expected = _expected_octile_cost(octile_source, cell)
        print(f"cell={cell}: actual={actual:.7f}, expected={expected:.7f}")
        assert abs(actual - expected) < 1e-9, (
            f"cell {cell}: expected octile-distance accumulated_cost {expected}, got {actual}"
        )
    print("Every checked cell's accumulated_cost matches the hand-derived octile distance to within 1e-9.\n")

    print("--- cost_distance_field(): fully-closed obstacle band, -1 came_from disambiguation ---")
    closed_field = cost_distance_field(dem, closed_cost_raster, [source_cell])
    closed_accumulated_cost = closed_field["accumulated_cost"]
    closed_came_from_row = closed_field["came_from_row"]

    beyond_band = [(r, c) for r in range(size) for c in range(obstacle_col + 1, size)]
    for r, c in beyond_band:
        assert closed_accumulated_cost[r, c] == np.inf, (
            f"cell ({r}, {c}) is beyond the fully-closed obstacle band -- expected accumulated_cost == inf, "
            f"got {closed_accumulated_cost[r, c]}"
        )
        assert closed_came_from_row[r, c] == -1, (
            f"cell ({r}, {c}) is beyond the fully-closed obstacle band -- expected came_from_row == -1, "
            f"got {closed_came_from_row[r, c]}"
        )

    source_side = [(r, c) for r in range(size) for c in range(0, obstacle_col) if np.isfinite(closed_cost_raster[r, c])]
    for r, c in source_side:
        assert np.isfinite(closed_accumulated_cost[r, c]), (
            f"cell ({r}, {c}) is on the source side of the fully-closed obstacle band -- expected a finite "
            f"accumulated_cost, got {closed_accumulated_cost[r, c]}"
        )
    print(
        f"All {len(beyond_band)} cells beyond the fully-closed band are unreachable (accumulated_cost == inf, "
        f"came_from_row == -1); all {len(source_side)} finite-cost source-side cells are reachable (finite "
        f"accumulated_cost) -- the -1 disambiguation rule holds.\n"
    )

    print("--- Cross-validation: cost_distance_field() + backtrace_route() vs. least_cost_path() ---")
    cross_field = cost_distance_field(dem, two_gap_cost_raster, [source_cell])
    cross_cells = backtrace_route(cross_field["came_from_row"], cross_field["came_from_col"], destination_cell)
    cross_cost = float(cross_field["accumulated_cost"][destination_cell])

    assert cross_cells == two_gap_path["cells"], (
        f"cost_distance_field()+backtrace_route() cell list must exactly match least_cost_path()'s own "
        f"'cells' for the same source/destination pair -- early termination must not change the answer. "
        f"got {cross_cells} vs {two_gap_path['cells']}"
    )
    assert abs(cross_cost - two_gap_path["total_cost"]) < 1e-9, (
        f"cost_distance_field()'s accumulated_cost at the destination ({cross_cost}) must equal "
        f"least_cost_path()'s own total_cost ({two_gap_path['total_cost']}) to within 1e-9"
    )
    print(
        f"cost_distance_field()+backtrace_route() reproduces least_cost_path()'s own {len(cross_cells)}-cell "
        f"path and total_cost ({cross_cost:.2f}) exactly on the two-gap DEM.\n"
    )

    print(f"accumulated_cost.dtype={accumulated_cost.dtype}, came_from_row.dtype={field['came_from_row'].dtype}, "
          f"came_from_row.shape={field['came_from_row'].shape}")

    print("Smoke test passed.")
