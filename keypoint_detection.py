"""
keypoint_detection.py

Standalone keypoint detection. Independent of KSOP: it has exactly one job
-- find the keypoints within (or just outside) the drawn boundary, mark
them on the layout map, and carry their data into the report for
narration. It makes NO siting judgement. A keypoint inside a production
zone, under canopy, on a road -- all are reported. The user decides what to
do with the information. The only spatial constraint is the boundary, and
even that is a soft margin (see KEYPOINT_BOUNDARY_MARGIN_METERS).

A keypoint is the inflection in a primary valley's long profile: the lowest
point of the steep upper reach, equivalently the highest point of the
gentler lower reach. Every primary valley has exactly one, so this module
returns exactly one keypoint per valley by construction (or none, honestly,
when every candidate split is rejected -- never a relaxed gate).

Pipeline (each step reuses valley_delineation.py rather than reimplementing
D8 hydrology):

    fill_depressions() -> compute_flow_direction() -> compute_flow_
    accumulation()                                   [valley_delineation]
        --> delineate_valleys() for primary valleys (its own MIN_PRIMARY_
            VALLEY_CONTRIBUTING_AREA_ACRES gate is what qualifies a valley;
            this module adds NO catchment threshold of its own)
        --> invert the flow-direction arrays into an UPSTREAM map (each cell
            -> the cells that drain directly into it)
        --> per valley: outlet = highest-accumulation branch endpoint; trace
            the main stem UPSTREAM FROM THE OUTLET, always taking the
            highest-accumulation feeder, until the valley runs out
        --> drop any valley with no stem cell within KEYPOINT_BOUNDARY_
            MARGIN_METERS of the drawn boundary (the buffered window holds
            valleys that are not this property's)
        --> sample RAW elevation + cumulative distance along the stem,
            smooth, take cell-to-cell slope percent, smooth again
        --> CANDIDATES: evaluate the TWO-SEGMENT LEAST-SQUARES FIT at every
            valid split along the profile (keypoint_split_candidates())
        --> FILTERS, per candidate, each recording its reason code: slope
            must drop, the split must lie within the boundary margin, and it
            must not sit on ground the fill raised
        --> SELECTION: among the survivors ON the parcel if there are any,
            otherwise among the off-parcel ones, the candidate standing
            HIGHEST by raw elevation, ties to the best fit residual
            (select_keypoint_candidate()) -> one keypoint per valley, flagged
            on/off parcel; no survivor -> no keypoint, reasons recorded

EIGHT RULES, each encoded so the approach it replaced is not reintroduced
(each was diagnosed from a real dead end or a real candidate table -- see the
tests for the executable form):

  1. Profile the RAW elevation array, never the FILLED one.
     delineate_valleys() builds branches_utm from filled[r, c], and
     the depression fill raises every pit to its spill elevation, so a marsh
     reads as a near-flat plateau (flat resolution then tilts it by a
     millimetre or two, which routes it but does not make it terrain) and
     any inflection detector fires on the steep-to-flat
     transition ENTERING the fill -- an artifact of the filling, not
     landform. Measured on a synthetic bowl: profiling raw puts
     the break at the bowl bottom where the fill-depth gate rejects it;
     profiling filled puts it at the plateau entrance with a large drop and
     zero fill depth, which would sail through every gate. Flow direction
     and accumulation still use the filled array (that is what makes them
     well-defined); ONLY the elevation profile uses raw dem['array'].

  2. Trace the stem UPSTREAM FROM THE OUTLET; do not select a branch.
     Three branch-selection heuristics were tried and all failed: highest
     terminal accumulation (every branch in a valley ends at the same outlet
     cell, so this value is identical across all of them -- max() just picks
     whichever comes first); longest branch (picks the longest headwater
     tail, so the keypoint lands high on a tributary with a fraction of an
     acre of catchment); shared trunk / branch-set intersection (returns 0-1
     cells, because branches_rowcol is a decomposition into disjoint
     segments between confluences, not head-to-outlet paths). The working
     method takes the valley's outlet cell (highest accumulation among
     branch endpoints) and walks upstream, always taking the highest-
     accumulation feeder, until the valley runs out -- one path per valley,
     no ties, no dependence on how branches are decomposed.

  3. Locate the keypoint by a TWO-SEGMENT FIT, not by the largest slope
     drop. Largest absolute drop is scale-dependent (on a concave-up profile
     the upper reach is steeper, so a 24%->16% break beats a 13%->7% one and
     the headwater always wins); largest relative drop has the opposite bias
     (near the outlet slopes are small, so any difference is a large ratio
     and it drifts to the toe). The scale-free method treats the keypoint as
     a global property of the whole profile: for each candidate split, fit a
     straight line to everything above it and another to everything below,
     and take the split minimising total squared residual. That is the
     definition -- where the profile best divides into a steep segment and a
     gentle one -- and it returns exactly one answer per valley with no
     window size and no tie-breaking.

  4. Constrain the fit to positions where slope actually drops.
     Unconstrained, the fit returns splits where slope INCREASES downstream
     (observed 19.0% -> 21.2%), the opposite of a keypoint. Requiring a
     minimum drop between a window above and below (KEYPOINT_MIN_SLOPE_DROP_
     PCT over KEYPOINT_MIN_RUN_CELLS either side) moves every affected valley
     onto a legitimate inflection.

  5. Make the boundary margin part of the CHOICE, not a filter on it.
     The margin used to be applied after the fit had already committed the
     valley to its globally best split, so a valley running straight through
     the parcel reported NOTHING whenever that one split happened to land
     further out in the buffer than the margin allows -- the admissible
     splits it also held were never ranked. Measured on the reference
     property: the 8.17-acre valley carries 150 m of channel across the
     parcel and 13 eligible on-parcel splits, and returned no keypoint at
     all, because its best fit sat 47 m out. Since where a keypoint may sit
     is known before any line is fitted, it belongs in the candidate set,
     and the valley then answers with its best ADMISSIBLE split. This is not
     a relaxed gate: the margin is enforced exactly as before, and nothing
     beyond it is ever returned -- what changed is that a valley is no
     longer silenced by a split it was never allowed to use.

  6. Apply EVERY filter to every candidate, then select; never select, then
     filter. The same lesson as #5, generalised once the epsilon fill made
     the marsh gate hit it. With stems now routed through depressions, a
     depression's own raw plunge can be the profile's best two-segment
     split; while the marsh gate was a filter on the ONE chosen split, that
     cost the valley its keypoint outright (the synthetic bowl in
     test_keypoint_detection.py, test 1). The fit now scores every valid
     split, each filter rejects candidates independently with a reason
     code, and a survivor is selected (fix #7) -- so a
     contaminated best split costs its valley nothing but that split, and a
     valley with no survivor reports why candidate by candidate. No filter
     changed value or logic; on a profile where no filter rejects the
     global best split, the answer is byte-identical to the single-split
     search. KNOWN LIMIT, reported not fixed: filtering candidates does not
     un-contaminate the RESIDUAL. On the synthetic bowl the residual keeps
     falling toward the filled run, so the best surviving split is the
     unfilled cell at its rim -- three cells below the true inflection.
     Scoping the fit itself to unfilled ground is the remedy for that, and
     a separate change; diagnose_keypoint_candidates.py counts how often a
     real selection sits next to a fill-rejected split. (Under fix #7's
     highest-survivor rule the bowl's selection moves off the rim, to the
     top of the real-ground block; the residual contamination is unchanged
     and still recorded.)

  7. SELECT THE HIGHEST SURVIVOR, not the best residual. Once #6 exposed
     the full survivor sets, they showed the filters are already the
     geometric gate: a valley routinely holds a dozen consecutive
     filter-passing splits, and the whole-stem residual systematically
     picked the lowest of them. Choosing among qualified inflections is a
     USE question, and a keypoint's use is the ground it commands by
     gravity, which is monotone in elevation. The reasoning, the earlier
     position it reverses, and the edge-cell consequence (the selection is
     the top, weakest-drop cell of its block, so KEYPOINT_MIN_SLOPE_DROP_PCT
     now moves every selection) are recorded at select_keypoint_candidate().

  8. PREFER AN ON-PARCEL SURVIVOR; FALL BACK, NEVER DROP. Elevation ranks
     the survivors because a keypoint's value is the land it commands by
     gravity, and that value is only realisable on ground the owner
     controls -- so on-parcel precedence sits ABOVE elevation: the highest
     on-parcel survivor is selected, and only a valley with none selects its
     highest off-parcel survivor. The partition reads the candidate's own
     on_parcel flag; the margin stays the only boundary rule, and a valley
     whose survivors are all off parcel keeps its keypoint (dropping it
     would be a qualification decision, which is the filters' job).
     Reasoning at select_keypoint_candidate().

A SELECTION RULE FLAGGED AND DELIBERATELY NOT BUILT: select the highest
survivor whose slope drop is within some fraction of the best drop available
among the valley's survivors -- trading a little elevation for a stronger
break, and loosening the selection's dependence on the slope-drop constant.
Deferred until the straight-elevation rule's per-valley drop gap
(diagnose_keypoint_candidates.py prints it every run) shows whether that gap
matters on real ground.

A GATE THAT WAS TRIED AND IS DELIBERATELY NOT INCLUDED: a normalised two-
segment residual gate (residual as a fraction of the profile's elevation
variance) accepted 14 of 14 valleys with values clustering in 0.0010-0.0058
-- no separation at all. Any monotone concave profile fits two lines
reasonably well, so it cannot distinguish valleys with a keypoint from
valleys without. Since every primary valley has one, no such gate is
needed, and none is added here.

detect_keypoints() is a PURE function over an already-fetched dem plus the
drawn boundary polygon -- no soil, canopy, road, or climate input, no
ParcelData. Taking dem directly keeps it runnable when unrelated services
are down (the climate API timing out currently blocks fetch_parcel_data()
entirely) and matches the independent-of-KSOP scope. flow direction, flow
accumulation, the filled array, and the valleys are OPTIONAL overrides that
each self-compute from dem when not supplied, and every supplied override is
forwarded into the internal computation rather than recomputed -- the same
self-computing override pattern the rest of this pipeline uses.
identify_keypoints() is the network entry point that fetches/derives the dem
and boundary when a caller does not already hold them.
"""

import logging
import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point

from feature_schema import CONFIDENCE_LOW
from raster_grid import cell_area_acres, pixel_center_xy
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    delineate_valleys,
    fill_and_resolve,
)

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# CONSTANTS
#
# CALIBRATION CAVEAT (repeated in every constant's docstring below): every
# KEYPOINT_* threshold here was tuned against TWO independently-drawn
# boundaries of a SINGLE 13.23-acre reference property (the drawn boundary in
# reference_fixture.py; these caveats said "~16-acre" until it was measured).
# None has been validated on any other property. Treat them as a first
# calibration, not a settled default.
# --------------------------------------------------------------------------

# Cells over which the raw elevation profile (and, separately, the derived
# slope profile) is smoothed with a centered moving average before the
# inflection search -- enough to damp single-cell DEM noise without erasing a
# real slope break. Tuned against two boundaries of one 13.23-acre reference
# property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_PROFILE_SMOOTH_CELLS = 5

# Minimum run of profile cells on EACH side of a candidate split -- the
# steep upper reach above and the gentler lower reach below must each occupy
# at least this many cells for the split to be considered, and the slope-drop
# window (mean slope above vs below) is measured over exactly this many
# slopes on each side. Guards against a one-cell blip being read as an
# inflection. Tuned against two boundaries of one 13.23-acre reference
# property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_RUN_CELLS = 6

# Minimum drop (mean slope-percent over the KEYPOINT_MIN_RUN_CELLS window
# above minus the same over the window below) for a split to be an eligible
# keypoint candidate at all. This is fix #4 in the module docstring: without
# it the least-squares fit can settle on a split where slope INCREASES
# downstream, the opposite of a keypoint. Tuned against two boundaries of one
# 13.23-acre reference property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_SLOPE_DROP_PCT = 3.0

# The marsh gate, applied to every candidate split (fix #6). A split whose
# fill depth (filled minus raw elevation at the split cell) exceeds this is
# sitting on ground the priority-flood raised -- a pit/marsh whose steep-to-
# flat entrance is a filling artifact, not landform. Small and nonzero so genuine near-zero fill (a cell barely
# touched by the flood) still passes. It is a SECOND, independent defense
# alongside fix #1 (raw profiling): the inflection on a filled profile lands
# at the pit RIM, where fill depth is ~0, so the fill gate alone would not
# catch it -- raw profiling is what does; this catches the residual case.
# Tuned against two boundaries of one 13.23-acre reference property; NOT
# validated elsewhere. CONFIGURABLE.
KEYPOINT_FILL_ARTIFACT_THRESHOLD_M = 0.15

# Boundary margin. delineate_valleys() runs on the buffered DEM, so valleys
# extending past the property line are found and their stems run off it. This
# distance is what "near the drawn boundary" means, and it does THREE things
# in detect_keypoints(): a valley with no stem cell this close is dropped
# before it is profiled; a candidate split further out is rejected
# (REJECT_OFF_MARGIN), so the keypoint is only ever chosen among stem cells
# this close; and the chosen keypoint is flagged and measured (on_parcel /
# distance_outside_boundary_m). Nothing further outside is ever returned.
#
# FLAGGED, NOT RETUNED: since fix #7 this value also moves SELECTION, not
# only qualification. The selection prefers an on-parcel survivor (fix #8)
# but falls back to the off-parcel ones the margin admitted, so for a valley
# whose survivors all sit outside the line (valley 2 on the reference
# property: 24.2 and 19.55 m out) this constant alone decides whether it has
# a keypoint and which one.
#
# The justification is DRAWING PRECISION, not terrain: a boundary traced by
# hand over aerial imagery is easily 10-25 m off, so a keypoint 14 m outside
# is within the error of the line rather than genuinely off the property.
#
# THE OBSERVED DATA BEHIND 25 m, re-measured 2026-09-22 on a live 3DEP fetch
# for the reference property. Taking each primary valley's UNCONSTRAINED best
# split (the positions that set the threshold, before the margin narrows the
# choice): 1 of 4 landed on-parcel and the off-parcel distances were 19.6,
# 47.1 and 228.4 m. 25 m sits in the clean gap between 19.6 and 47.1.
#
# The earlier note here cited a different dataset -- 8 of 14 keypoints
# on-parcel, off-parcel distances 14, 20, 52, 125, 231, 236 m, and two
# retained keypoints of 6.36/6.69 ac at 346.5/347.0 m. Those figures were
# measured before d4dc2ee ("Epsilon fill: filled flats get a defined flow
# direction") corrected a flow field that dead-ended on 73 interior cells and
# split this property's drainage into 8 valleys where there are 4. They are
# recorded here only so nobody looks for the keypoints they name: the code no
# longer produces them, and the gap those distances showed (20 to 52 m) is not
# the gap 25 m now sits in. The VALUE has not moved; its evidence has been
# replaced. Tuned against two boundaries of one 13.23-acre reference property;
# NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_BOUNDARY_MARGIN_METERS = 25.0


KEYPOINT_CONFIDENCE_NOTES = (
    "A keypoint is the inflection in a primary valley's long profile -- the "
    "break from the steep upper reach to the gentler lower reach (Yeomans), "
    "the lowest point of the steep slope and the highest point of the gentle "
    "one below it -- located here from DEM-derived D8 flow direction/"
    "accumulation and a RAW-elevation long profile sampled along the valley's "
    "main stem, traced upstream from the valley outlet. It is NOT surveyed or "
    "field-verified, inherits every limitation of the DEM and the D8 valley "
    "delineation beneath it (see valley_delineation.py), and every detection "
    "threshold was tuned against two independently-drawn boundaries of a "
    "single 13.23-acre reference property and has NOT been validated elsewhere. "
    "A keypoint may legitimately sit just outside the drawn boundary (within "
    "a small margin for hand-drawing precision -- see distance_outside_"
    "boundary_m / on_parcel); flow paths do not stop at a property line. "
    "Treat a keypoint as a starting point to walk and ground-truth, not a "
    "final determination."
)


def build_upstream_map(
    flow_to_row: np.ndarray, flow_to_col: np.ndarray
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """
    Inverts compute_flow_direction()'s (flow_to_row, flow_to_col) arrays into
    an upstream adjacency map: each key cell (r, c) maps to the list of cells
    that flow DIRECTLY into it (its immediate feeders). A cell with no
    upstream feeders (a ridge/source cell) simply never appears as a key.

    This is the exact inverse of the downstream flow field -- flow direction
    says where a cell drains TO; this says what drains INTO a cell -- and is
    what the upstream stem walk (highest-accumulation feeder each step) is
    traced over. Cells with flow_to_row < 0 (grid-edge outlets, or cells
    nodata walls off from the border -- compute_flow_direction()'s -1
    sentinel, which since the epsilon fill no longer appears on interior
    flats) have no downstream target and contribute no upstream edge.
    """
    rows, cols = flow_to_row.shape
    upstream: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for r in range(rows):
        for c in range(cols):
            tr = int(flow_to_row[r, c])
            tc = int(flow_to_col[r, c])
            if tr < 0:
                continue
            upstream.setdefault((tr, tc), []).append((r, c))
    return upstream


def trace_stem_from_outlet(
    valley: dict,
    upstream_map: dict[tuple[int, int], list[tuple[int, int]]],
    flow_accumulation: np.ndarray,
) -> list[tuple[int, int]]:
    """
    Traces a valley's single main stem, returned ordered upstream ->
    downstream (the head/topmost cell first, the outlet last).

    This is fix #2 from the module docstring -- the ONE method that survived
    after three branch-selection heuristics failed. The valley's outlet is
    the highest-flow-accumulation cell among its branches' terminal cells
    (every branch in a valley ends at the same outlet, so this is robust to
    how the branches happen to be decomposed). From the outlet the walk goes
    UPSTREAM, at each step taking the highest-accumulation feeder among the
    cells that drain into the current cell, until the valley runs out (no
    feeder left). Because flow direction only ever points a cell at a
    strictly lower neighbor, the upstream map is a DAG and this walk
    terminates; accumulation strictly decreases upstream, so there is no risk
    of doubling back (a `visited` set guards ties defensively regardless).

    The walk is NOT clipped to the valley's own delineated cells: the whole
    point is to follow the stem ABOVE the stream-delineation threshold, up to
    the ridge, so the profiled reach actually contains the inflection (which
    on a small parcel can sit above where concentrated flow -- and therefore
    the delineated valley -- begins).
    """
    endpoints = [branch[-1] for branch in valley["branches_rowcol"] if branch]
    if not endpoints:
        # No traceable branch (delineate_valleys() never emits this -- it drops
        # branches under 2 cells -- but a hand-supplied valley override might);
        # an empty stem is caught by the caller's short-stem gate.
        return []
    # Highest accumulation wins; deterministic (row, col) tie-break so the
    # same DEM always yields the same outlet.
    outlet = max(
        endpoints,
        key=lambda cell: (float(flow_accumulation[cell[0], cell[1]]), -cell[0], -cell[1]),
    )

    stem_downstream_first = [outlet]
    visited = {outlet}
    current = outlet
    while True:
        feeders = [f for f in upstream_map.get(current, ()) if f not in visited]
        if not feeders:
            break
        best = max(
            feeders,
            key=lambda f: (float(flow_accumulation[f[0], f[1]]), -f[0], -f[1]),
        )
        stem_downstream_first.append(best)
        visited.add(best)
        current = best

    # Collected outlet-first; reverse so the profile reads upstream (top) ->
    # downstream (outlet), the orientation the fit and slope windows assume.
    return list(reversed(stem_downstream_first))


def _centered_moving_average(values: np.ndarray, window_cells: int) -> np.ndarray:
    """
    Centered moving average with an edge-clamped (shrinking) window, so no
    wrap-around or zero-padding artifact is introduced at the profile ends.
    window_cells <= 1 returns the values unchanged. The half-width is
    window_cells // 2 on each side.
    """
    n = len(values)
    if window_cells <= 1 or n == 0:
        return np.asarray(values, dtype=float)
    half = window_cells // 2
    out = np.empty(n, dtype=float)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = float(np.mean(values[lo:hi]))
    return out


def _profile_along_stem(
    stem: list[tuple[int, int]],
    elevation_array: np.ndarray,
    dem: dict,
    smooth_cells: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Builds the long profile along the stem cells (ordered upstream ->
    downstream). Elevation is sampled from `elevation_array` at each cell (RAW
    dem['array'] on the real path -- see module docstring fix #1); cumulative
    distance is real ground distance between successive cell centers
    (pixel_center_xy(), so a diagonal step costs sqrt(2)x a cardinal one).
    Elevation is smoothed, cell-to-cell descent slope percent (rise/run*100,
    positive downhill) is taken, then the slope is smoothed again.

    Returns (cumulative_distance[len n], smoothed_elevation[len n],
    smoothed_slope_pct[len n-1]); the slope at index i is the descent from
    stem cell i to cell i+1.
    """
    elevations = np.array([float(elevation_array[r, c]) for r, c in stem], dtype=float)
    xys = [pixel_center_xy(dem, r, c) for r, c in stem]

    smoothed_elev = _centered_moving_average(elevations, smooth_cells)

    cumulative = np.zeros(len(stem), dtype=float)
    slopes = np.empty(len(stem) - 1, dtype=float)
    for i in range(len(stem) - 1):
        (x0, y0), (x1, y1) = xys[i], xys[i + 1]
        run = math.hypot(x1 - x0, y1 - y0)
        cumulative[i + 1] = cumulative[i] + run
        drop = smoothed_elev[i] - smoothed_elev[i + 1]
        slopes[i] = (drop / run * 100.0) if run > 0 else 0.0

    smoothed_slope = _centered_moving_average(slopes, smooth_cells)
    return cumulative, smoothed_elev, smoothed_slope


def _line_residual_sum_of_squares(x: np.ndarray, y: np.ndarray) -> float:
    """
    Ordinary-least-squares fit of y against x, returning the residual sum of
    squares (the total squared vertical distance from the points to the
    best-fit line). Closed-form, so no polyfit conditioning warnings on short
    or near-collinear segments. Fewer than two points has no residual (0.0);
    a segment with zero spread in x collapses to the horizontal mean, whose
    residual is the y variance -- the correct degenerate answer, not an
    error.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 2:
        return 0.0
    x_mean = x.mean()
    y_mean = y.mean()
    sxx = float(np.sum((x - x_mean) ** 2))
    if sxx == 0.0:
        return float(np.sum((y - y_mean) ** 2))
    sxy = float(np.sum((x - x_mean) * (y - y_mean)))
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean
    residual = y - (slope * x + intercept)
    return float(np.sum(residual ** 2))


# Rejection reason codes, one per EXISTING filter, carried by every candidate
# split a filter turns away. The tuple order is the order the filters are
# listed in detect_keypoints()'s docstring and the order a candidate's own
# reasons are recorded in; it is not a precedence -- every filter is applied
# to every candidate, and a candidate records EVERY filter it fails, so the
# run-level tally counts fill-artifact contamination directly rather than
# only where no earlier filter happened to fire first.
REJECT_SLOPE_DROP = "slope_drop_below_min"
REJECT_OFF_MARGIN = "outside_boundary_margin"
REJECT_FILL_ARTIFACT = "fill_artifact"
KEYPOINT_REJECTION_REASONS = (REJECT_SLOPE_DROP, REJECT_OFF_MARGIN, REJECT_FILL_ARTIFACT)


def keypoint_split_candidates(
    distance: np.ndarray,
    elevation: np.ndarray,
    slope_pct: np.ndarray,
    min_run_cells: int,
) -> list[dict]:
    """
    CANDIDATE GENERATION: the two-segment fit (fix #3) evaluated at EVERY
    valid split position along the profile, not only the winning one.

    For a split at stem cell index k, "above" is the upstream reach cells
    [0..k] and "below" is the downstream reach cells [k..n-1] (the split cell
    k -- the would-be keypoint -- is the shared endpoint of both fitted
    lines). A position is VALID if and only if it satisfies the minimum-
    segment-length constraint on both sides: k ranges over [min_run_cells,
    n-1-min_run_cells], guaranteeing at least min_run_cells profile cells
    (and slope samples) above and below. That is the same range the fit has
    always searched; no other constraint is applied here, and nothing
    outside the range ever enters the set.

    Each candidate carries the figures the filters and the selection read:
    'index' (k), 'slope_above_pct' / 'slope_below_pct' (mean smoothed slope
    over the min_run_cells slopes just above / just below k), 'slope_drop_pct'
    (above minus below), and 'residual' (the total residual sum of squares of
    the two independent OLS lines, elevation vs distance). Returned in index
    order, upstream -> downstream. A profile too short for any valid split
    returns [].

    The global argmin of 'residual' over this set is the profile's best two-
    segment split with no filter applied; the keypoint the detector returns is
    the best-residual candidate that survives every filter
    (select_keypoint_candidate()).
    """
    n = len(elevation)
    candidates = []
    for k in range(min_run_cells, n - min_run_cells):
        slope_above = float(np.mean(slope_pct[k - min_run_cells:k]))
        slope_below = float(np.mean(slope_pct[k:k + min_run_cells]))
        residual = (
            _line_residual_sum_of_squares(distance[:k + 1], elevation[:k + 1])
            + _line_residual_sum_of_squares(distance[k:], elevation[k:])
        )
        candidates.append(
            {
                "index": k,
                "slope_above_pct": slope_above,
                "slope_below_pct": slope_below,
                "slope_drop_pct": slope_above - slope_below,
                "residual": residual,
            }
        )
    return candidates


def global_argmin_candidate(candidates: list[dict]) -> Optional[dict]:
    """
    The candidate with the lowest fit residual, with NO filter applied -- the
    profile's own best two-segment split. Ties go to the upstream-most
    (lowest index) candidate, the same tie-break the fit has always had.
    None for an empty set. Reported, never returned as a keypoint on its own
    account: it is the reference the fall-through count is measured against.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda cand: cand["residual"])


# Which side of the partition a valley's selection came from
# (select_keypoint_candidate()): an on-parcel survivor existed and the
# highest of those was taken, or none did and the highest off-parcel one was.
SELECTION_ON_PARCEL = "on_parcel"
SELECTION_OFF_PARCEL_FALLBACK = "off_parcel_fallback"


def _selection_rank(cand: dict) -> tuple:
    """Highest raw elevation first, then the lowest fit residual, then the
    upstream-most (lowest) index -- the ordering within either partition."""
    return (-cand["elevation_m"], cand["residual"], cand["index"])


def select_keypoint_candidate(candidates: list[dict]) -> Optional[dict]:
    """
    SELECTION: among the candidates that survived every filter (an empty
    'rejected_by' list), partitioned by the candidate's own 'on_parcel' flag:

      1. if any survivor is on the parcel, the on-parcel survivor standing
         HIGHEST by raw elevation ('elevation_m');
      2. otherwise the off-parcel survivor standing highest.

    In both branches ties on elevation go to the lowest fit residual -- the
    retired selection rule kept as the tiebreak -- and then to the upstream-
    most (lowest index) candidate, so the answer is deterministic. If none
    survives, None: the valley produces no keypoint, and the rejected
    candidates' reason codes are the record of why. Which branch fired is
    read back off the result: an off-parcel selection is the fallback, by
    construction (SELECTION_ON_PARCEL / SELECTION_OFF_PARCEL_FALLBACK).

    Three positions are recorded below, because each was a response to
    evidence rather than a reversal of the argument before it.

    FIRST POSITION: select the best (lowest) two-segment residual, never
    elevation. Yeomans' keypoint is the inflection where the steep reach
    meets the gentle one, and its value comes from BEING that inflection,
    not from being high; ranking by elevation would prefer weak inflections
    far up a long stem over the real one, trading a geometric definition for
    a proxy, and "can it serve production" is a use criterion that already
    lives downstream as the gravity relationship.

    SECOND POSITION: select the highest survivor. The first held while the
    fit returned ONE split per valley and the filters had not yet been shown
    to qualify more than one. The candidate tables
    (diagnose_keypoint_candidates.py) showed they qualify many: on the
    reference property valley 8 has thirteen consecutive survivors (indices
    13-25, 342.26 down to 333.50 m) and valley 4 thirteen in two blocks --
    every one a slope-dropping, in-margin, unfilled split, i.e. a keypoint by
    this module's own rules. The residual was not choosing between an
    inflection and a non-inflection; it was minimising total two-segment
    misfit over the WHOLE stem, which on these profiles systematically
    prefers splits low down, where a practitioner reads the sharpest local
    break. Once every survivor is a filter-passing inflection, choosing among
    them is an APPLICATION question, and a keypoint's practical value is the
    land it commands by gravity, which is monotone in elevation. So
    elevation is the defensible basis: the filters are the geometric gate,
    and selection is use.

    THIRD POSITION: prefer a survivor ON the parcel, and only then the
    highest. Elevation decides among survivors because of the land a
    keypoint commands by gravity -- and that value is only realisable on
    ground the owner controls: a keypoint 20 m past the line is somewhere the
    owner cannot build. On-parcel precedence therefore sits ABOVE elevation,
    not beside it. It is a PREFERENCE among what the filters already
    admitted, and nothing more:
      * the partition reads the 'on_parcel' flag the candidate record
        already carries (_stem_boundary_margin()); it introduces no distance
        threshold of its own, and KEYPOINT_BOUNDARY_MARGIN_METERS remains
        the only boundary rule;
      * a valley whose survivors are ALL off parcel is NOT dropped -- it
        falls back to its highest off-parcel survivor. The margin was
        calibrated deliberately to keep nearby off-parcel inflections as
        real features of the property's drainage, and dropping a valley here
        would be a QUALIFICATION decision made inside selection.
        Qualification is the filters' job. A fallback keypoint carries
        on_parcel False and its distance_outside_boundary_m, and the
        diagnostics name the branch, so it is never read as an on-parcel
        one.
    On the reference property this moves nothing: valley 2 is the only
    valley with no on-parcel survivor (24.2 and 19.55 m out), so its
    fallback returns the same index 40; valleys 8 and 4 already select an
    on-parcel survivor that is also their highest.

    The residual stays on the record -- on every candidate, as the tiebreak,
    and in the diagnostics -- because it is how a strange survivor set gets
    noticed.

    THE EDGE-CELL CONSEQUENCE, ACCEPTED DELIBERATELY. Survivors come in
    contiguous blocks along the stem, bounded by cells where the slope drop
    crosses KEYPOINT_MIN_SLOPE_DROP_PCT (the other two filters bound blocks
    too, but on real ground it is mostly this one). Elevation falls down the
    stem, so "highest survivor" lands at the TOP EDGE of the highest block:
    the cell immediately below a rejection. On the reference property that
    is valley 8's index 13 at a 4.12% drop, directly below index 12 at
    2.85%, while the block's strongest break is index 16 at 5.82%. So the
    selected keypoint is systematically the WEAKEST-drop member of its own
    block, and a small change to the slope-drop constant moves every
    selection -- the constant is now load-bearing for WHERE a keypoint sits,
    not only WHETHER one exists. This is known and accepted, not
    overlooked; diagnose_keypoint_candidates.py prints, per valley, the
    selected drop against the best drop among its survivors so the gap is
    visible every run. The flagged alternative that would narrow it is in
    the module docstring (deferred, not built). (With the on-parcel
    preference the block in question can also be the highest ON-PARCEL
    block rather than the highest overall; the same edge rule applies.)
    """
    survivors = [cand for cand in candidates if not cand["rejected_by"]]
    if not survivors:
        return None
    # With no on-parcel survivor, every survivor is off parcel: the fallback.
    pool = [cand for cand in survivors if cand["on_parcel"]] or survivors
    return min(pool, key=_selection_rank)


def two_segment_keypoint_split(
    distance: np.ndarray,
    elevation: np.ndarray,
    slope_pct: np.ndarray,
    min_run_cells: int,
    min_slope_drop_pct: float,
    position_is_eligible=None,
) -> Optional[tuple[int, float, float, float, float]]:
    """
    The two-segment keypoint fit (fix #3) over a bare profile, restricted to
    splits where slope actually drops (fix #4) and, optionally, to positions
    satisfying position_is_eligible (a k -> bool predicate standing in for
    the boundary margin, fix #5; a failure is recorded as REJECT_OFF_MARGIN).
    Built from keypoint_split_candidates() and the same per-candidate
    filters detect_keypoints() applies, over a bare profile with no stem
    cells, so the fill-depth filter has nothing to read and is not applied.

    This is the FIT'S OWN answer -- the best-residual surviving split -- and
    NOT the detector's selection: detect_keypoints() selects the HIGHEST
    survivor by raw elevation (select_keypoint_candidate(), where the reason
    is recorded), which a bare profile has no raw stem elevations to do. The
    residual survives as that selection's tiebreak and on every candidate's
    record; the tests drive the fit's definition (fixes #3-#5) through this
    on hand-built profiles.

    Returns (k, slope_above_pct, slope_below_pct, slope_drop_pct,
    total_residual) for the best-residual surviving split, or None if none
    survives (too short a profile, slope never drops by the required amount,
    or nothing satisfying position_is_eligible).
    """
    candidates = keypoint_split_candidates(distance, elevation, slope_pct, min_run_cells)
    for cand in candidates:
        cand["rejected_by"] = []
        if cand["slope_drop_pct"] < min_slope_drop_pct:
            cand["rejected_by"].append(REJECT_SLOPE_DROP)
        if position_is_eligible is not None and not position_is_eligible(cand["index"]):
            cand["rejected_by"].append(REJECT_OFF_MARGIN)
    survivors = [cand for cand in candidates if not cand["rejected_by"]]
    if not survivors:
        return None
    chosen = min(survivors, key=lambda cand: cand["residual"])
    return (
        chosen["index"],
        chosen["slope_above_pct"],
        chosen["slope_below_pct"],
        chosen["slope_drop_pct"],
        chosen["residual"],
    )


def _stem_boundary_margin(stem: list, dem: dict, boundary_polygon_utm) -> list:
    """
    Measures every stem cell against the drawn boundary ONCE per valley,
    returning [(point, on_parcel, distance_outside_boundary_m), ...] in stem
    order (0.0 outside-distance when the cell is on the parcel).

    One pass, read three times: by the valley-level margin gate, by the fit's
    per-split eligibility test, and by the surviving keypoint's own
    on_parcel / distance_outside_boundary_m fields. Measuring inside the fit
    loop instead would repeat a shapely distance per candidate split.
    """
    measured = []
    for row, col in stem:
        point = Point(*pixel_center_xy(dem, row, col))
        if boundary_polygon_utm.contains(point) or boundary_polygon_utm.touches(point):
            measured.append((point, True, 0.0))
        else:
            measured.append((point, False, float(point.distance(boundary_polygon_utm))))
    return measured


def detect_keypoints(
    dem: dict,
    boundary_polygon_utm,
    *,
    flow_to_row: Optional[np.ndarray] = None,
    flow_to_col: Optional[np.ndarray] = None,
    flow_accumulation: Optional[np.ndarray] = None,
    filled: Optional[np.ndarray] = None,
    valleys: Optional[list[dict]] = None,
    profile_smooth_cells: int = KEYPOINT_PROFILE_SMOOTH_CELLS,
    min_run_cells: int = KEYPOINT_MIN_RUN_CELLS,
    min_slope_drop_pct: float = KEYPOINT_MIN_SLOPE_DROP_PCT,
    fill_artifact_threshold_m: float = KEYPOINT_FILL_ARTIFACT_THRESHOLD_M,
    boundary_margin_meters: float = KEYPOINT_BOUNDARY_MARGIN_METERS,
    diagnostics: Optional[dict] = None,
    _profile_from_filled: bool = False,
) -> list[dict]:
    """
    Detects keypoints on `dem`, returning one dict per surviving keypoint --
    at most one per primary valley, by construction -- or [] when nothing
    survives (the honest empty answer, never a relaxed gate).

    Pure and network-free: dem and boundary_polygon_utm (the drawn boundary
    reprojected into dem['crs'], for the margin test) are the only real
    inputs -- NO soil, canopy, road, or climate. flow_to_row/flow_to_col/
    flow_accumulation/filled/valleys are OPTIONAL overrides that each self-
    compute from dem (via valley_delineation.py) when not supplied, and every
    supplied override is forwarded into the internal computation rather than
    recomputed. A caller that already holds these (e.g. a shared pipeline
    pass) passes them straight through.

    Per valley: the outlet is the highest-accumulation branch endpoint; the
    main stem is traced upstream from it (trace_stem_from_outlet()); the RAW-
    elevation long profile is sampled and smoothed along the stem; then
    CANDIDATES -> FILTERS -> SELECTION (fix #6):
      * candidates: every valid split (keypoint_split_candidates() -- at
        least min_run_cells profile cells on each side);
      * filters, each applied to every candidate independently, each
        recording its reason code on the candidate: slope drop below
        min_slope_drop_pct (REJECT_SLOPE_DROP); more than
        boundary_margin_meters outside the drawn boundary
        (REJECT_OFF_MARGIN); on ground the fill raised by more than
        fill_artifact_threshold_m (REJECT_FILL_ARTIFACT, the marsh gate);
      * selection: the highest on-parcel survivor by raw elevation, or,
        when no survivor is on the parcel, the highest off-parcel one (the
        fallback -- the valley is never dropped for it); ties to the lowest
        fit residual (select_keypoint_candidate(), where the reasoning --
        and the two earlier rules it refines -- is recorded).

    Valley gates -- a valley yields NO keypoint if:
      * its stem is shorter than 2 * min_run_cells + 2 cells (too short to
        hold a steep run, a gentle run, and a split between them);
      * no stem cell at all is within boundary_margin_meters of the drawn
        boundary (the valley is somewhere else in the buffered DEM window and
        has nothing to say about this property);
      * no candidate survives the filters. Counted under the retired names,
        told apart in the retired order: rejected_no_slope_drop (no split
        drops slope -- the profile is effectively straight),
        rejected_off_margin (some do, none inside the margin),
        rejected_fill_artifact (some pass both, and every one of those sits
        on filled ground).
    There is deliberately NO catchment gate, NO deduplication, NO production
    exclusion, and NO residual/normalised-fit gate (see the module
    docstring for why each was rejected).

    diagnostics, if a dict is passed, is populated in place -- a reporting
    hook only; it does not affect the return value. It carries the valley-
    level counters above, plus:
      'fall_through_valleys'       valleys where a filter rejected the
                                   global best-residual split and the valley
                                   kept a keypoint anyway;
      'fill_fall_through_valleys'  the subset where the fill gate rejected
                                   the split the pre-fall-through search
                                   would have chosen, and the valley kept a
                                   keypoint anyway;
      'selection_moved_valleys'    valleys whose selected survivor is not
                                   the best-residual survivor;
      'off_parcel_fallback_valleys' valleys with a keypoint and no on-parcel
                                   survivor, so the off-parcel fallback
                                   selected it;
      'parcel_preference_moved_valleys' valleys whose selection is not the
                                   highest survivor overall (the on-parcel
                                   preference moved it);
      'candidate_rejections'       {reason code: count}, every reason of
                                   every rejected candidate, across the run;
      'valley_candidates'          per profiled valley: valley_id,
                                   stem_length_cells, global_argmin_index,
                                   pre_fill_best_index,
                                   fit_best_survivor_index, selected_index,
                                   selected_slope_drop_pct,
                                   best_survivor_slope_drop_pct/_index,
                                   survivor_blocks ((first, last) index
                                   runs), survivors_on_parcel /
                                   survivors_off_parcel (counts),
                                   selection_branch (SELECTION_ON_PARCEL /
                                   SELECTION_OFF_PARCEL_FALLBACK / None),
                                   selected_on_parcel,
                                   selected_distance_outside_boundary_m,
                                   highest_survivor_index (the highest
                                   survivor with no partition),
                                   parcel_preference_moved,
                                   fall_through, fill_fall_through,
                                   selection_moved, outcome,
                                   and 'candidates' -- every candidate with
                                   index, rowcol, elevation_m,
                                   contributing_acres, slope_above/below/
                                   drop_pct, residual, on_parcel,
                                   distance_outside_boundary_m, fill_depth_m,
                                   rejected_by (reason codes) and outcome
                                   ('selected' / 'survived' / 'rejected').
    diagnose_keypoint_candidates.py prints it as tables. _profile_from_filled is a test-only hook that profiles
    the FILLED array instead of raw, to demonstrate fix #1's failure mode;
    never set it on a real path.

    Each returned dict (geometry-first, no narrative):
        {
            'id': int,                          # 0-based, ranked by slope_drop_pct desc
            'valley_id': int,
            'rowcol': (row, col),
            'point_utm': shapely Point,
            'geometry_wgs84': GeoJSON Point,
            'elevation_m': float,               # RAW elevation at the keypoint
            'contributing_acres': float,
            'slope_above_pct': float,
            'slope_below_pct': float,
            'slope_drop_pct': float,
            'stem_length_cells': int,
            'position_along_stem': int,         # split index k, upstream->downstream
            'on_parcel': bool,
            'distance_outside_boundary_m': float,   # 0.0 when inside
            'confidence': str,
            'confidence_notes': str,
        }
    """
    if filled is None:
        filled = fill_and_resolve(dem["array"])
    if flow_to_row is None or flow_to_col is None:
        flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    if flow_accumulation is None:
        flow_accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)
    if valleys is None:
        valleys = delineate_valleys(dem)

    raw_array = dem["array"]
    profile_array = filled if _profile_from_filled else raw_array
    area_per_cell = cell_area_acres(dem)

    upstream_map = build_upstream_map(flow_to_row, flow_to_col)

    # Minimum stem length to hold a min_run steep reach, a min_run gentle
    # reach, and a split cell between them (fix: reject before profiling, so a
    # short stem returns no keypoint and no exception -- never a partial fit).
    min_stem_cells = 2 * min_run_cells + 2

    stats = {
        "valleys": len(valleys),
        "rejected_short_stem": 0,
        "rejected_valley_off_margin": 0,
        "rejected_no_slope_drop": 0,
        "rejected_fill_artifact": 0,
        "rejected_off_margin": 0,
        "surviving": 0,
        # Valleys where some filter rejected the global best-residual split
        # and the valley kept a keypoint anyway. fill_fall_through_valleys is
        # the subset fix #6 added: the fill-artifact gate rejected the split
        # the single-split search would have returned, and the valley kept a
        # keypoint anyway (under that search, it lost it).
        "fall_through_valleys": 0,
        "fill_fall_through_valleys": 0,
        # Valleys whose selected (highest) survivor is not the one the
        # retired residual rule would have picked among the same survivors.
        "selection_moved_valleys": 0,
        # Valleys whose keypoint came from the off-parcel fallback (no
        # survivor on the parcel), and valleys where the on-parcel
        # preference selected something other than the highest survivor.
        "off_parcel_fallback_valleys": 0,
        "parcel_preference_moved_valleys": 0,
        # Every rejected candidate's every reason, tallied across the run.
        "candidate_rejections": {reason: 0 for reason in KEYPOINT_REJECTION_REASONS},
    }
    valley_candidates: list[dict] = []

    survivors: list[dict] = []
    for valley in valleys:
        if not valley.get("branches_rowcol"):
            continue

        stem = trace_stem_from_outlet(valley, upstream_map, flow_accumulation)
        if len(stem) < min_stem_cells:
            stats["rejected_short_stem"] += 1
            continue

        # The margin, measured over the whole stem before anything is fitted.
        # delineate_valleys() runs on the BUFFERED dem, so some primary
        # valleys never come within reach of the drawn boundary at all; one
        # with no stem cell inside the margin has nothing to say about this
        # property and is dropped here rather than profiled and then thrown
        # away. (Behaviourally the same as letting the fit find nothing
        # eligible below -- this is what makes the diagnostics say which of
        # the two actually happened.)
        stem_margin = _stem_boundary_margin(stem, dem, boundary_polygon_utm)

        def within_margin(index, _margin=stem_margin):
            _point, on_parcel, distance_outside = _margin[index]
            return on_parcel or distance_outside <= boundary_margin_meters

        if not any(within_margin(i) for i in range(len(stem))):
            stats["rejected_valley_off_margin"] += 1
            continue

        # Distance, smoothed elevation, and smoothed slope all come from one
        # pass over the SAME profile array (raw on the real path -- fix #1);
        # the fit's residual reads elevation-vs-distance, the slope-drop
        # filter reads slope.
        distance, elevation, slope_pct = _profile_along_stem(
            stem, profile_array, dem, profile_smooth_cells
        )

        # CANDIDATES -> FILTERS -> SELECTION. Every valid split is a
        # candidate; each existing filter is applied to each candidate
        # independently, recording every reason it fails; the highest
        # on-parcel survivor -- or, with none on the parcel, the highest
        # off-parcel one -- is the keypoint (select_keypoint_candidate()).
        # A contaminated best split therefore no longer costs the valley
        # its keypoint -- the next-best survivor answers -- and a valley with
        # no survivor says why, candidate by candidate.
        candidates = keypoint_split_candidates(distance, elevation, slope_pct, min_run_cells)
        for cand in candidates:
            k = cand["index"]
            r, c = stem[k]
            _point, on_parcel, distance_outside = stem_margin[k]
            fill_depth = float(filled[r, c]) - float(raw_array[r, c])
            cand["rowcol"] = (r, c)
            cand["elevation_m"] = float(raw_array[r, c])
            cand["contributing_acres"] = float(flow_accumulation[r, c]) * area_per_cell
            cand["on_parcel"] = on_parcel
            cand["distance_outside_boundary_m"] = distance_outside
            cand["fill_depth_m"] = fill_depth
            rejected_by = []
            # Fix #4: slope must actually drop across the split.
            if cand["slope_drop_pct"] < min_slope_drop_pct:
                rejected_by.append(REJECT_SLOPE_DROP)
            # Fix #5: the boundary margin. Applying it per candidate is what
            # "the margin constrains the choice" always meant -- a valley that
            # runs through the parcel answers with its best ADMISSIBLE split
            # (measured on the reference property: the 8.17-acre valley's best
            # fit sat 47 m out while it held 13 eligible on-parcel splits).
            if not within_margin(k):
                rejected_by.append(REJECT_OFF_MARGIN)
            # The marsh gate: a split on ground the fill raised by more than
            # fill_artifact_threshold_m is a filling artifact, not landform.
            # It used to be a filter on the ONE chosen split, so a valley
            # whose best split sat on filled ground lost its keypoint outright
            # -- which, once the epsilon fill routed stems through
            # depressions, meant a depression's own plunge could silence a
            # valley holding a real inflection above it. Per candidate, the
            # artifact split is still never returned; the valley's next-best
            # split on real ground answers instead.
            if fill_depth > fill_artifact_threshold_m:
                rejected_by.append(REJECT_FILL_ARTIFACT)
            cand["rejected_by"] = rejected_by
            for reason in rejected_by:
                stats["candidate_rejections"][reason] += 1

        chosen = select_keypoint_candidate(candidates)
        best_overall = global_argmin_candidate(candidates)
        # The split the retired single-split search returned before its fill
        # gate: the best-residual candidate passing slope drop and margin.
        # Kept only for the report -- it is what "the previous best" means
        # for a valley the fill gate used to silence.
        pre_fill_best = min(
            (
                cand for cand in candidates
                if REJECT_SLOPE_DROP not in cand["rejected_by"]
                and REJECT_OFF_MARGIN not in cand["rejected_by"]
            ),
            key=lambda cand: cand["residual"],
            default=None,
        )
        for cand in candidates:
            cand["outcome"] = "selected" if cand is chosen else ("rejected" if cand["rejected_by"] else "survived")
        passing = [cand for cand in candidates if not cand["rejected_by"]]
        # What the retired residual rule would have selected among these
        # same survivors -- the record of how far the elevation rule moved.
        fit_best = min(passing, key=lambda cand: cand["residual"], default=None)
        # The highest survivor with no on/off-parcel partition -- the rule
        # this selection refines -- so a valley the preference moved shows it.
        highest = min(passing, key=_selection_rank, default=None)
        on_parcel_count = sum(1 for cand in passing if cand["on_parcel"])
        # The strongest break available among the survivors, against which
        # the selected (top-of-block, weakest-edge) drop is measured.
        best_drop = max(passing, key=lambda cand: cand["slope_drop_pct"], default=None)
        # Survivors as contiguous runs of stem index, so the edge-cell
        # behaviour (selection at the top of the highest block) is visible.
        blocks: list[list[int]] = []
        for cand in passing:
            if blocks and cand["index"] == blocks[-1][1] + 1:
                blocks[-1][1] = cand["index"]
            else:
                blocks.append([cand["index"], cand["index"]])

        valley_record = {
            "valley_id": int(valley["id"]),
            "stem_length_cells": len(stem),
            "global_argmin_index": None if best_overall is None else best_overall["index"],
            "pre_fill_best_index": None if pre_fill_best is None else pre_fill_best["index"],
            "fit_best_survivor_index": None if fit_best is None else fit_best["index"],
            "selected_index": None if chosen is None else chosen["index"],
            "selected_slope_drop_pct": None if chosen is None else chosen["slope_drop_pct"],
            "best_survivor_slope_drop_pct": None if best_drop is None else best_drop["slope_drop_pct"],
            "best_survivor_slope_drop_index": None if best_drop is None else best_drop["index"],
            "survivor_blocks": [tuple(block) for block in blocks],
            # The partition the selection read: survivors on / off the
            # parcel, which branch answered, and -- so a fallback keypoint is
            # never mistaken for an on-parcel one -- where the selection sits.
            "survivors_on_parcel": on_parcel_count,
            "survivors_off_parcel": len(passing) - on_parcel_count,
            "selection_branch": None if chosen is None else (
                SELECTION_ON_PARCEL if chosen["on_parcel"] else SELECTION_OFF_PARCEL_FALLBACK
            ),
            "selected_on_parcel": None if chosen is None else chosen["on_parcel"],
            "selected_distance_outside_boundary_m": None if chosen is None else chosen["distance_outside_boundary_m"],
            "highest_survivor_index": None if highest is None else highest["index"],
            "parcel_preference_moved": chosen is not None and chosen is not highest,
            # A filter turned the global best-residual split away and the
            # valley still has a keypoint.
            "fall_through": chosen is not None and bool(best_overall["rejected_by"]),
            # The fill gate rejected the split the single-split search would
            # have returned (and then lost the valley over), and the valley
            # still has a keypoint.
            "fill_fall_through": chosen is not None and pre_fill_best is not None
            and REJECT_FILL_ARTIFACT in pre_fill_best["rejected_by"],
            # The elevation rule chose a different survivor than the residual
            # rule would have.
            "selection_moved": chosen is not None and chosen is not fit_best,
            "outcome": None,
            "candidates": candidates,
        }
        valley_candidates.append(valley_record)

        if chosen is None:
            # No survivor. The valley-level counter keeps the retired
            # meaning, told apart in the retired order: NO split drops slope
            # anywhere; some do, but none inside the margin; some pass both,
            # but every one of those sits on filled ground.
            if pre_fill_best is not None:
                outcome = "rejected_fill_artifact"
            elif any(REJECT_SLOPE_DROP not in cand["rejected_by"] for cand in candidates):
                outcome = "rejected_off_margin"
            else:
                outcome = "rejected_no_slope_drop"
            stats[outcome] += 1
            valley_record["outcome"] = outcome
            continue

        valley_record["outcome"] = "selected"
        if valley_record["fall_through"]:
            stats["fall_through_valleys"] += 1
        if valley_record["fill_fall_through"]:
            stats["fill_fall_through_valleys"] += 1
        if valley_record["selection_moved"]:
            stats["selection_moved_valleys"] += 1
        if valley_record["selection_branch"] == SELECTION_OFF_PARCEL_FALLBACK:
            stats["off_parcel_fallback_valleys"] += 1
        if valley_record["parcel_preference_moved"]:
            stats["parcel_preference_moved_valleys"] += 1

        k = chosen["index"]
        r, c = chosen["rowcol"]
        point, on_parcel, distance_outside = stem_margin[k]
        x, y = point.x, point.y
        lon, lat = warp_transform(dem["crs"], "EPSG:4326", [x], [y])

        survivors.append(
            {
                "valley_id": int(valley["id"]),
                "rowcol": (r, c),
                "point_utm": point,
                "geometry_wgs84": {"type": "Point", "coordinates": (lon[0], lat[0])},
                "elevation_m": round(chosen["elevation_m"], 2),
                "contributing_acres": round(chosen["contributing_acres"], 2),
                "slope_above_pct": round(chosen["slope_above_pct"], 2),
                "slope_below_pct": round(chosen["slope_below_pct"], 2),
                "slope_drop_pct": round(chosen["slope_drop_pct"], 2),
                "stem_length_cells": len(stem),
                "position_along_stem": int(k),
                "on_parcel": on_parcel,
                "distance_outside_boundary_m": round(distance_outside, 2),
                "confidence": CONFIDENCE_LOW,
                "confidence_notes": KEYPOINT_CONFIDENCE_NOTES,
            }
        )

    # Stable, meaningful ordering: strongest inflection first. IDs are
    # assigned after sorting so keypoint 0 is always the sharpest break.
    survivors.sort(key=lambda cand: -cand["slope_drop_pct"])
    for new_id, cand in enumerate(survivors):
        cand["id"] = new_id

    stats["surviving"] = len(survivors)
    if diagnostics is not None:
        diagnostics.update(stats)
        diagnostics["valley_candidates"] = valley_candidates

    _LOGGER.info(
        "keypoint detection: valleys=%d rejected(short_stem=%d valley_off_margin=%d "
        "no_slope_drop=%d fill_artifact=%d off_margin=%d) surviving=%d "
        "fall_through=%d (fill=%d) off_parcel_fallback=%d candidate_rejections=%s",
        stats["valleys"],
        stats["rejected_short_stem"],
        stats["rejected_valley_off_margin"],
        stats["rejected_no_slope_drop"],
        stats["rejected_fill_artifact"],
        stats["rejected_off_margin"],
        stats["surviving"],
        stats["fall_through_valleys"],
        stats["fill_fall_through_valleys"],
        stats["off_parcel_fallback_valleys"],
        stats["candidate_rejections"],
    )

    return survivors


def keypoints_to_geojson(keypoints: list[dict]) -> dict:
    """
    Wraps detect_keypoints() output as a schema-conformant GeoJSON
    FeatureCollection (layer="keypoint"), following valleys_to_geojson()'s
    shape. Each feature carries the keypoint's real measured values
    (elevation, contributing catchment, the three slope figures, stem
    position, and the on/off-parcel margin flags) as properties --
    geometry-first, no narrative. An empty keypoint list yields an empty
    FeatureCollection, honestly, not a placeholder feature.

    CONSOLIDATED into wire_translation.py -- see valleys_to_geojson().
    """
    from wire_translation import keypoints_to_feature_collection

    return keypoints_to_feature_collection(keypoints)


def identify_keypoints(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    boundary_polygon_utm=None,
    valleys: Optional[list[dict]] = None,
    **detect_kwargs,
) -> dict:
    """
    Network entry point: fetches/derives whatever the caller does not already
    hold, then runs detect_keypoints(). dem, boundary_polygon_utm, and
    valleys are each OPTIONAL overrides that self-compute when not supplied,
    independently of one another, and each is forwarded into every internal
    call rather than re-fetched. Deliberately takes NO parcel_data and NO
    canopy/soil/road input -- keypoint detection is pure terrain analysis and
    is independent of KSOP, so it stays runnable when those services are
    down. Returns:

        {
            'keypoints': list[dict],                 # detect_keypoints()'s own dicts
            'keypoints_geojson': FeatureCollection,  # layer="keypoint"
            'valleys_geojson': FeatureCollection,    # layer="valley" (diagnostic)
        }

    'keypoints' is the raw geometry-first list (the shape the report generator
    consumes directly, as the retired layout map did); 'keypoints_geojson' is its
    schema-conformant GeoJSON wrapping for map/vector output.

    This path needs the network for the DEM fetch alone; the offline unit
    tests drive detect_keypoints() directly with synthetic inputs instead.
    """
    # Imported lazily so the offline detect_keypoints() path (and its tests)
    # never import the network layer just to reach the pure detector.
    from dem_data import get_dem_for_boundary
    from valley_delineation import valleys_to_geojson

    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    if boundary_polygon_utm is None:
        from shapely.geometry import Polygon

        boundary_xs, boundary_ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    if valleys is None:
        valleys = delineate_valleys(dem)

    keypoints = detect_keypoints(
        dem,
        boundary_polygon_utm,
        valleys=valleys,
        **detect_kwargs,
    )

    return {
        "keypoints": keypoints,
        "keypoints_geojson": keypoints_to_geojson(keypoints),
        "valleys_geojson": valleys_to_geojson(valleys),
    }


def summarize_keypoints(keypoints: list[dict]) -> str:
    if not keypoints:
        return (
            "No keypoints detected on this property (no primary valley's long "
            "profile held a qualifying steep-to-gentle inflection within the "
            "boundary margin) -- the honest empty answer, not a relaxed gate."
        )
    lines = [f"Keypoints detected: {len(keypoints)}"]
    for k in keypoints:
        location = (
            "on parcel"
            if k["on_parcel"]
            else f"{k['distance_outside_boundary_m']:.0f} m off parcel"
        )
        lines.append(
            f"  - Keypoint {k['id']} (valley {k['valley_id']}): "
            f"{k['elevation_m']:.1f} m, {k['contributing_acres']:.1f} ac catchment, "
            f"slope drop {k['slope_drop_pct']:.1f}% "
            f"({k['slope_above_pct']:.1f}% -> {k['slope_below_pct']:.1f}%), {location}"
        )
    return "\n".join(lines)
