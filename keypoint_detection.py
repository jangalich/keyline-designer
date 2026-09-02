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
when a gate rejects it -- never a relaxed gate).

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
        --> sample RAW elevation + cumulative distance along the stem,
            smooth, take cell-to-cell slope percent, smooth again
        --> locate the keypoint by a TWO-SEGMENT LEAST-SQUARES FIT over the
            long profile, restricted to splits where slope actually drops
        --> fill-artifact + stem-length gates -> one keypoint per valley
        --> boundary margin: keep any keypoint within KEYPOINT_BOUNDARY_
            MARGIN_METERS of the drawn boundary, flag on/off parcel

FOUR CRITERIA THAT FAILED, encoded here so they are not reintroduced (each
was diagnosed from a real dead end -- see the tests for the executable
form):

  1. Profile the RAW elevation array, never the FILLED one.
     delineate_valleys() builds branches_utm from filled[r, c], and
     fill_depressions() raises every pit to its spill elevation, so a marsh
     reads as a near-flat plateau (the epsilon fill's per-cell increment
     tilts it by a millimetre, which routes it but does not make it
     terrain) and any inflection detector fires on the steep-to-flat
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
    fill_depressions,
)

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# CONSTANTS
#
# CALIBRATION CAVEAT (repeated in every constant's docstring below): every
# KEYPOINT_* threshold here was tuned against TWO independently-drawn
# boundaries of a SINGLE ~16-acre reference property. None has been validated
# on any other property. Treat them as a first calibration, not a settled
# default.
# --------------------------------------------------------------------------

# Cells over which the raw elevation profile (and, separately, the derived
# slope profile) is smoothed with a centered moving average before the
# inflection search -- enough to damp single-cell DEM noise without erasing a
# real slope break. Tuned against two boundaries of one ~16-acre reference
# property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_PROFILE_SMOOTH_CELLS = 5

# Minimum run of profile cells on EACH side of a candidate split -- the
# steep upper reach above and the gentler lower reach below must each occupy
# at least this many cells for the split to be considered, and the slope-drop
# window (mean slope above vs below) is measured over exactly this many
# slopes on each side. Guards against a one-cell blip being read as an
# inflection. Tuned against two boundaries of one ~16-acre reference
# property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_RUN_CELLS = 6

# Minimum drop (mean slope-percent over the KEYPOINT_MIN_RUN_CELLS window
# above minus the same over the window below) for a split to be an eligible
# keypoint candidate at all. This is fix #4 in the module docstring: without
# it the least-squares fit can settle on a split where slope INCREASES
# downstream, the opposite of a keypoint. Tuned against two boundaries of one
# ~16-acre reference property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_SLOPE_DROP_PCT = 3.0

# The marsh gate. A keypoint whose fill depth (filled minus raw elevation at
# the keypoint cell) exceeds this is sitting on ground the priority-flood
# raised -- a pit/marsh whose steep-to-flat entrance is a filling artifact,
# not landform. Small and nonzero so genuine near-zero fill (a cell barely
# touched by the flood) still passes. It is a SECOND, independent defense
# alongside fix #1 (raw profiling): the inflection on a filled profile lands
# at the pit RIM, where fill depth is ~0, so the fill gate alone would not
# catch it -- raw profiling is what does; this catches the residual case.
# Tuned against two boundaries of one ~16-acre reference property; NOT
# validated elsewhere. CONFIGURABLE.
KEYPOINT_FILL_ARTIFACT_THRESHOLD_M = 0.15

# Boundary margin. delineate_valleys() runs on the buffered DEM, so valleys
# extending past the property line are found and their keypoints may sit off
# it. A keypoint is kept if it is inside the drawn boundary OR within this
# distance of it, and flagged (on_parcel / distance_outside_boundary_m); one
# any further outside is dropped.
#
# The justification is DRAWING PRECISION, not terrain: a boundary traced by
# hand over aerial imagery is easily 10-25 m off, so a keypoint 14 m outside
# is within the error of the line rather than genuinely off the property. On
# the ~16-acre reference property, 8 of 14 keypoints landed on-parcel; the
# off-parcel distances were 14, 20, 52, 125, 231, and 236 m. 25 m sits in the
# clean gap between 20 and 52 in that observed data. The two keypoints this
# margin retains are that property's most significant -- 6.36 and 6.69 ac of
# catchment, found independently from two different drawn boundaries at 346.5
# m and 347.0 m elevation, i.e. the same ground. Tuned against two boundaries
# of one ~16-acre reference property; NOT validated elsewhere. CONFIGURABLE.
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
    "single ~16-acre reference property and has NOT been validated elsewhere. "
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


def two_segment_keypoint_split(
    distance: np.ndarray,
    elevation: np.ndarray,
    slope_pct: np.ndarray,
    min_run_cells: int,
    min_slope_drop_pct: float,
) -> Optional[tuple[int, float, float, float, float]]:
    """
    Locates the keypoint by the two-segment least-squares fit (fix #3), among
    splits where slope actually drops (fix #4). This is the scale-free
    definition of the keypoint: the split at which the long profile best
    divides into a steep upper line and a gentle lower one.

    For a candidate split at stem cell index k, "above" is the upstream reach
    cells [0..k] and "below" is the downstream reach cells [k..n-1] (the split
    cell k -- the keypoint -- is the shared endpoint of both fitted lines). k
    ranges over [min_run_cells, n-1-min_run_cells], guaranteeing at least
    min_run_cells profile cells (and slope samples) on each side. A split is
    ELIGIBLE only if the mean slope over the min_run_cells slopes just above k
    exceeds the mean over the min_run_cells just below by at least
    min_slope_drop_pct -- without this the fit can settle where slope
    increases downstream, the opposite of a keypoint.

    Among eligible splits, the chosen k minimises the total residual sum of
    squares of the two independent OLS lines (elevation vs distance). Returns
    (k, slope_above_pct, slope_below_pct, slope_drop_pct, total_residual), or
    None if no eligible split exists (too short a profile, or slope never
    drops by the required amount -- the honest "no keypoint here" answer).
    """
    n = len(elevation)
    best = None
    for k in range(min_run_cells, n - min_run_cells):
        slope_above = float(np.mean(slope_pct[k - min_run_cells:k]))
        slope_below = float(np.mean(slope_pct[k:k + min_run_cells]))
        slope_drop = slope_above - slope_below
        if slope_drop < min_slope_drop_pct:
            continue
        residual = (
            _line_residual_sum_of_squares(distance[:k + 1], elevation[:k + 1])
            + _line_residual_sum_of_squares(distance[k:], elevation[k:])
        )
        if best is None or residual < best[4]:
            best = (k, slope_above, slope_below, slope_drop, residual)
    return best


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
    elevation long profile is sampled and smoothed along the stem; the
    keypoint is the two-segment-fit split among slope-dropping positions
    (two_segment_keypoint_split()).

    Gates -- a valley yields NO keypoint if:
      * its stem is shorter than 2 * min_run_cells + 2 cells (too short to
        hold a steep run, a gentle run, and a split between them);
      * no split has a slope drop of at least min_slope_drop_pct (the profile
        is effectively straight -- no inflection);
      * the chosen split sits on ground the fill raised by more than
        fill_artifact_threshold_m (the marsh gate);
      * the keypoint is more than boundary_margin_meters outside the drawn
        boundary.
    There is deliberately NO catchment gate, NO deduplication, NO production
    exclusion, and NO residual/normalised-fit gate (see the module
    docstring for why each was rejected).

    diagnostics, if a dict is passed, is populated in place with the per-
    valley stem/gate breakdown -- a reporting hook only; it does not affect
    the return value. _profile_from_filled is a test-only hook that profiles
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
        filled = fill_depressions(dem["array"])
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
        "rejected_no_slope_drop": 0,
        "rejected_fill_artifact": 0,
        "rejected_off_margin": 0,
        "surviving": 0,
    }

    survivors: list[dict] = []
    for valley in valleys:
        if not valley.get("branches_rowcol"):
            continue

        stem = trace_stem_from_outlet(valley, upstream_map, flow_accumulation)
        if len(stem) < min_stem_cells:
            stats["rejected_short_stem"] += 1
            continue

        # Distance, smoothed elevation, and smoothed slope all come from one
        # pass over the SAME profile array (raw on the real path -- fix #1);
        # the fit's residual reads elevation-vs-distance, the eligibility
        # window reads slope.
        distance, elevation, slope_pct = _profile_along_stem(
            stem, profile_array, dem, profile_smooth_cells
        )
        split = two_segment_keypoint_split(
            distance, elevation, slope_pct, min_run_cells, min_slope_drop_pct
        )
        if split is None:
            stats["rejected_no_slope_drop"] += 1
            continue

        k, slope_above, slope_below, slope_drop, _residual = split
        r, c = stem[k]

        fill_depth = float(filled[r, c]) - float(raw_array[r, c])
        if fill_depth > fill_artifact_threshold_m:
            stats["rejected_fill_artifact"] += 1
            continue

        x, y = pixel_center_xy(dem, r, c)
        point = Point(x, y)
        if boundary_polygon_utm.contains(point) or boundary_polygon_utm.touches(point):
            on_parcel = True
            distance_outside = 0.0
        else:
            on_parcel = False
            distance_outside = float(point.distance(boundary_polygon_utm))
            if distance_outside > boundary_margin_meters:
                stats["rejected_off_margin"] += 1
                continue

        contributing_acres = float(flow_accumulation[r, c]) * area_per_cell
        lon, lat = warp_transform(dem["crs"], "EPSG:4326", [x], [y])

        survivors.append(
            {
                "valley_id": int(valley["id"]),
                "rowcol": (r, c),
                "point_utm": point,
                "geometry_wgs84": {"type": "Point", "coordinates": (lon[0], lat[0])},
                "elevation_m": round(float(raw_array[r, c]), 2),
                "contributing_acres": round(contributing_acres, 2),
                "slope_above_pct": round(slope_above, 2),
                "slope_below_pct": round(slope_below, 2),
                "slope_drop_pct": round(slope_drop, 2),
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

    _LOGGER.info(
        "keypoint detection: valleys=%d rejected(short_stem=%d no_slope_drop=%d "
        "fill_artifact=%d off_margin=%d) surviving=%d",
        stats["valleys"],
        stats["rejected_short_stem"],
        stats["rejected_no_slope_drop"],
        stats["rejected_fill_artifact"],
        stats["rejected_off_margin"],
        stats["surviving"],
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

    'keypoints' is the raw geometry-first list (the shape render_layout_map.py
    and the report generator consume directly); 'keypoints_geojson' is its
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
