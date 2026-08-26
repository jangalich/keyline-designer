"""
valley_level_pool.py

Level-pool delineation at a single anchor cell: given an already-fetched
DEM plus precomputed D8 flow arrays, this module answers "if a dam wall
stood on this cell, what ground would sit under the waterline, and what
would the wall have to span?" -- as GEOMETRY AND RELATIVE MEASUREMENTS,
never as a design.

    dem + filled + (flow_to_row, flow_to_col) + upstream map + anchor cell
        --> valley axis at the anchor (fit_valley_axis)
        --> backwater region   (walk the upstream map, raw-elevation test)
        --> dam-axis band      (walk the perpendicular, raw-elevation test)
        --> cross-section measurements at 3 upstream stations
        --> one dict: pool cells, band cells, abutment results, station
            measurements, flags

PURE AND NETWORK-FREE. Every input is an array or a plain dict; nothing
here fetches, and nothing here imports a network layer. That is what makes
it testable against synthetic DEMs with hand-computable answers (see
test_valley_level_pool.py), the same Stage-1/Stage-2 split
valley_delineation.py and water_candidate_zones.py already draw.

THE DIVISION OF LABOR BETWEEN THE FILLED AND THE RAW DEM, which is the
single most important thing to keep straight in here, mirrors
keypoint_detection.py's fix #1 exactly:

  * CONNECTIVITY runs on the FILLED-DEM flow field. "Which cells drain
    into this one" is only well-defined once depressions are filled --
    priority-flood filling is what guarantees every valid cell has a
    monotone path to the grid edge, so the upstream map is a DAG and the
    walk terminates. A pit in the raw DEM (real marsh or interpolation
    artifact) would otherwise sever the backwater at the pit.
  * The ELEVATION TEST reads the RAW DEM. fill_depressions() raises every
    pit to its spill elevation, so a filled-DEM elevation test would
    report a marsh bottom as sitting AT the spill level and exclude ground
    that a real pool would genuinely flood. The pool is only honest if the
    "is this ground under the waterline" question is asked of the terrain
    as measured.

So: the walk goes where the FILLED flow field says water comes from, and
the waterline is compared against what the RAW DEM says the ground is.

ONE HONEST LIMIT ON THE FIRST HALF, MEASURED RATHER THAN ASSUMED.
valley_delineation.fill_depressions() is the PLAIN priority-flood -- it
raises a pit to EXACTLY its spill elevation, with no epsilon -- and
compute_flow_direction() requires a STRICTLY positive slope. A filled pit
therefore TIES with the neighbour it should drain to and gets that
function's -1 "no downhill neighbour" sentinel, so the backwater walk
STOPS at a pit on the channel instead of crossing it. That is
valley_delineation.py's own documented flat-tie limitation (see its module
docstring), it is pre-existing, and nothing here can or should paper over
it: the fix belongs in the hydrology layer, as an epsilon fill or a
flat-resolution pass, not in a consumer.

What the fill still buys the walk is that the flow field is WELL-DEFINED
and acyclic at all -- which is what makes "walk upstream until the
elevation test fails" a terminating question rather than an open one. And
the raw elevation test is load-bearing today on the parts of this module
that do NOT go through the flow field -- the abutment search and the
station cross-sections read the raw DEM directly, where a filled reach can
read metres high and would more than double the reported flooded width.
test_valley_level_pool.py asserts both halves separately, including the
pit truncation, so an epsilon fill in the layer below fails these tests
loudly rather than silently changing what a pool means.

NO VOLUME, ANYWHERE. This module computes flooded WIDTH (meters) and
flooded CROSS-SECTIONAL AREA (square meters) at discrete stations. It
never integrates those along the valley, never multiplies anything by a
length, and never emits a cubic quantity. The reason is not modesty about
arithmetic: a storage volume is a DESIGN NUMBER. Quoting one off a 5 m
public DEM, at a reference height nobody surveyed, against a dam whose
wall geometry and spillway do not exist yet, would be a fabricated
engineering figure -- exactly the kind of thing this pipeline's
confidence_notes exist to refuse. The station measurements here are
RELATIVE RANKING INPUTS ("this reach holds a wider, deeper cross-section
than that one"), which is all a survey-pointer deliverable needs, and the
key names say so (no key in the returned dict names a capacity, a
storage, or a volume). A follow-up scoring branch reads these; it must
not turn them into one either.

POOL_REFERENCE_HEIGHT_METERS IS NOT A DESIGN HEIGHT. See its own
docstring. Everything in here is measured at ONE fixed reference height so
that candidate sites are compared on equal terms; the number is a
measuring stick, not a proposal.
"""

import logging
import math
from typing import Optional

import numpy as np

from raster_grid import pixel_center_xy

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# CONSTANTS
#
# CALIBRATION CAVEAT (repeated in every constant's docstring below, the same
# convention keypoint_detection.py's own constants block uses): every value
# here is a first calibration at small-farm dam scale. NONE has been
# validated beyond the reference property. Treat them as a starting point to
# tune against real ground, not a settled default.
# --------------------------------------------------------------------------

# The single reference waterline height, in meters above the anchor cell's
# RAW elevation, at which every candidate is delineated and measured.
#
# THIS IS NOT A DESIGN HEIGHT AND MUST NEVER BE REPORTED AS ONE. Nothing
# here proposes building a 2.5 m dam. It is a RELATIVE DELINEATION AND
# RANKING REFERENCE: measuring every candidate at one fixed height is what
# makes "this site's backwater is wider and its abutments closer than that
# site's" a statement about terrain rather than about two different
# assumed dam sizes. A real design height comes out of a survey, a
# spillway calculation, and a geotechnical look at the abutments -- none
# of which this pipeline has or claims.
#
# 2.5 m is chosen as a plausible small-farm pond/dam wall scale (roughly
# 8 ft), high enough that the backwater it traces is a real landform
# signal rather than DEM noise on a 5 m grid, and low enough that it does
# not flood half a gentle parcel. NOT validated beyond the reference
# property. CONFIGURABLE.
POOL_REFERENCE_HEIGHT_METERS = 2.5

# How far to either side of the anchor the dam-axis search walks looking
# for an abutment -- the point where raw terrain rises to the waterline
# and the wall could key into the hillside.
#
# 75 m is a deliberate outer bound, not an expectation: a dam wall much
# longer than that at this height stops being a farm pond and starts being
# an engineered structure, so a candidate whose terrain has not risen to
# the waterline within 75 m has told us something real (it sits on ground
# too open to impound cheaply) and the honest answer is
# abutment_found_left/right = False, NOT a wider search. Failing to find
# an abutment is a FINDING; it is never treated as an error or retried at
# a larger radius. NOT validated beyond the reference property.
# CONFIGURABLE.
ABUTMENT_SEARCH_HALF_WIDTH_METERS = 75.0

# Ceiling on how far upstream the backwater walk follows any single path,
# measured as CUMULATIVE GROUND DISTANCE ALONG THE PATH from the anchor
# (not straight-line distance, and not a cell count -- a diagonal D8 step
# costs sqrt(2)x a cardinal one, and that is what is accumulated).
#
# Its job is to bound the walk on ground the reference height alone will
# not bound: up a long, near-level draw the elevation test can stay true
# for hundreds of meters, which would report a pool stretching most of a
# parcel. 150 m keeps the delineated backwater at a scale a farmer can
# walk as one place, and pairs with water_candidate_zones.
# MAX_WATER_ZONE_AREA_ACRES, which bounds the same failure mode by area
# rather than by reach. NOT validated beyond the reference property.
# CONFIGURABLE.
MAX_BACKWATER_UPSTREAM_METERS = 150.0

# Spacing between the cross-section measurement stations, along the valley
# axis, upstream of the anchor. 25 m is roughly five cells on the 5 m DEM
# this pipeline fetches -- far enough apart that three stations sample
# genuinely different ground rather than three smoothed copies of the dam
# line, close enough that all three stay inside the backwater the
# reference height traces on a typical small valley. NOT validated beyond
# the reference property. CONFIGURABLE.
CROSS_SECTION_STATION_SPACING_METERS = 25.0

# How many cross-sections are measured, starting AT the anchor: stations
# sit at 0, +25 m, and +50 m upstream along the fitted valley axis. Three
# is the smallest number that shows a TREND (does the valley open out or
# pinch in going upstream?) rather than a single reading; more stations
# would sample past the reach the reference height reliably floods on a
# small parcel. NOT validated beyond the reference property. CONFIGURABLE.
CROSS_SECTION_STATIONS = 3

# Half-length of the walk, in cells, on EACH side of the anchor used to fit
# the local valley axis (so up to 2 * 4 + 1 = 9 cell centers are fitted).
#
# The fit exists because RAW D8 DIRECTION IS TOO QUANTIZED TO DEFINE A
# PERPENDICULAR: D8 only ever names one of 8 directions, so a dam axis
# built off it would snap to 45 degree increments and could be up to 22.5
# degrees off the real valley line -- at 75 m of search that is over 28 m
# of lateral error in where an abutment is looked for. A least-squares
# line through a short walk through the anchor recovers a continuous
# direction from the same quantized steps.
#
# 4 cells (20 m at 5 m resolution) is short enough that the fitted line
# stays LOCAL -- it describes the valley at the dam site, not the valley's
# average course -- and long enough to average out single-step D8
# quantization. NOT validated beyond the reference property. CONFIGURABLE.
VALLEY_AXIS_WALK_CELLS = 4


def rowcol_for_xy(dem: dict, x: float, y: float) -> Optional[tuple[int, int]]:
    """
    The grid cell whose square contains real-world point (x, y) in
    dem['crs'] meters -- the exact inverse of raster_grid.pixel_center_xy()
    (that function returns a cell's center; this returns the cell a point
    falls in, so pixel_center_xy() then rowcol_for_xy() round-trips).

    Returns None when the point falls outside the grid, which is a normal
    outcome here -- the perpendicular sampling walks off the DEM edge on
    any anchor near it -- not an error.
    """
    px, py = dem["resolution_meters"]
    col = int(math.floor((x - dem["origin_x"]) / px))
    row = int(math.floor((dem["origin_y"] - y) / py))
    rows, cols = dem["array"].shape
    if 0 <= row < rows and 0 <= col < cols:
        return (row, col)
    return None


def _cell_center_distance(dem: dict, a: tuple[int, int], b: tuple[int, int]) -> float:
    """Real ground distance between two cells' centers, so a diagonal D8
    step correctly costs sqrt(2)x a cardinal one on a square grid (and the
    right thing on a non-square one)."""
    ax, ay = pixel_center_xy(dem, a[0], a[1])
    bx, by = pixel_center_xy(dem, b[0], b[1])
    return math.hypot(bx - ax, by - ay)


def _downstream_walk(
    anchor: tuple[int, int],
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
    steps: int,
) -> list[tuple[int, int]]:
    """
    Up to `steps` cells downstream of `anchor`, following the D8 flow field
    (valley_delineation.compute_flow_direction()'s own (flow_to_row,
    flow_to_col) pair). Stops early at a -1 sentinel (grid-edge outlet or
    flat-plateau tie) or on revisiting a cell (defensive; the filled flow
    field is acyclic). `anchor` itself is NOT included.
    """
    walk: list[tuple[int, int]] = []
    seen = {anchor}
    current = anchor
    for _ in range(steps):
        tr = int(flow_to_row[current[0], current[1]])
        tc = int(flow_to_col[current[0], current[1]])
        if tr < 0:
            break
        nxt = (tr, tc)
        if nxt in seen:
            break
        walk.append(nxt)
        seen.add(nxt)
        current = nxt
    return walk


def _upstream_stem_walk(
    anchor: tuple[int, int],
    upstream_map: dict,
    flow_accumulation: np.ndarray,
    steps: int,
) -> list[tuple[int, int]]:
    """
    Up to `steps` cells upstream of `anchor` along the MAIN STEM: at each
    step take the highest-flow-accumulation feeder among the cells draining
    into the current one, with the same deterministic (row, col) tie-break
    keypoint_detection.trace_stem_from_outlet() uses -- reused convention,
    not a second, independently-drifting definition of "the stem." `anchor`
    itself is NOT included; the list reads anchor-outward (nearest first).
    """
    walk: list[tuple[int, int]] = []
    seen = {anchor}
    current = anchor
    for _ in range(steps):
        feeders = [f for f in upstream_map.get(current, ()) if f not in seen]
        if not feeders:
            break
        best = max(
            feeders,
            key=lambda f: (float(flow_accumulation[f[0], f[1]]), -f[0], -f[1]),
        )
        walk.append(best)
        seen.add(best)
        current = best
    return walk


def fit_valley_axis(
    dem: dict,
    anchor: tuple[int, int],
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
    upstream_map: dict,
    flow_accumulation: np.ndarray,
    walk_cells: int = VALLEY_AXIS_WALK_CELLS,
) -> tuple[float, float]:
    """
    The local valley direction at `anchor`, as a UNIT VECTOR POINTING
    DOWNSTREAM in dem['crs'] meters.

    Method: walk up to walk_cells cells downstream of the anchor through
    the D8 flow field, and up to walk_cells cells upstream along the
    highest-accumulation feeder over `upstream_map` (both walks reuse
    keypoint_detection.py's own conventions -- see _upstream_stem_walk()),
    take those cell centers plus the anchor's, and fit a line through them
    by TOTAL least squares (the principal axis of the centered point
    cloud's scatter matrix).

    Total least squares rather than y-on-x: a valley running due north
    would make an ordinary y = mx + b fit vertical and undefined, and the
    answer here is a DIRECTION, so the residual that matters is
    perpendicular distance to the line, not vertical distance. The
    principal eigenvector of the scatter matrix minimises exactly that.

    WHY FIT AT ALL: raw D8 direction names one of only 8 directions, so a
    perpendicular built from it snaps to 45 degree increments -- up to 22.5
    degrees of error, which at ABUTMENT_SEARCH_HALF_WIDTH_METERS is tens of
    meters of lateral error in where the dam axis is looked for. See
    VALLEY_AXIS_WALK_CELLS's own docstring.

    Degenerate cases, all honest rather than raised: with fewer than two
    distinct points (an anchor with no downstream target and no feeder, or
    a single-cell grid) the fit is undefined, and the D8 step direction is
    used instead; with no D8 step either, (0.0, -1.0) (due south, the
    default "downhill" on a north-up grid) is returned so callers always
    get a usable axis. Both fallbacks are logged at debug level.
    """
    downstream = _downstream_walk(anchor, flow_to_row, flow_to_col, walk_cells)
    upstream = _upstream_stem_walk(anchor, upstream_map, flow_accumulation, walk_cells)

    # Ordered upstream -> anchor -> downstream, so the endpoint difference
    # below is a real downstream-pointing vector.
    path = list(reversed(upstream)) + [anchor] + downstream
    points = np.array([pixel_center_xy(dem, r, c) for r, c in path], dtype=float)

    direction = None
    if len(points) >= 2:
        centered = points - points.mean(axis=0)
        scatter = centered.T @ centered
        eigenvalues, eigenvectors = np.linalg.eigh(scatter)
        if float(eigenvalues[-1]) > 0.0:
            direction = np.asarray(eigenvectors[:, -1], dtype=float)

    if direction is None:
        # No spread to fit (identical points, or a lone anchor cell): fall
        # back to the raw D8 step, quantized but real.
        tr = int(flow_to_row[anchor[0], anchor[1]])
        tc = int(flow_to_col[anchor[0], anchor[1]])
        if tr >= 0:
            ax, ay = pixel_center_xy(dem, anchor[0], anchor[1])
            bx, by = pixel_center_xy(dem, tr, tc)
            direction = np.array([bx - ax, by - ay], dtype=float)
            _LOGGER.debug(
                "fit_valley_axis: no fittable spread at %s; falling back to the raw D8 step", anchor
            )
        else:
            _LOGGER.debug(
                "fit_valley_axis: no fittable spread and no D8 step at %s; defaulting to due south", anchor
            )
            return (0.0, -1.0)

    norm = float(np.hypot(direction[0], direction[1]))
    if norm == 0.0:
        return (0.0, -1.0)
    unit = direction / norm

    # Orient DOWNSTREAM: the fitted axis is a line, so its eigenvector sign
    # is arbitrary. Point it along the path's own upstream-end ->
    # downstream-end vector.
    head = points[0]
    tail = points[-1]
    flow_vector = tail - head
    if float(np.dot(unit, flow_vector)) < 0.0:
        unit = -unit
    return (float(unit[0]), float(unit[1]))


def _sample_perpendicular(
    dem: dict,
    origin_xy: tuple[float, float],
    perpendicular: tuple[float, float],
    half_width_meters: float,
) -> tuple[list[float], list[float]]:
    """
    Raw-DEM elevations sampled along a straight line through origin_xy in
    the +/- `perpendicular` direction, at CELL RESOLUTION spacing
    (min(px, py) -- the finest step that cannot skip a cell), out to
    half_width_meters each way.

    Returns (offsets, elevations) ordered from the most negative offset to
    the most positive, where offset 0.0 is origin_xy itself. An offset
    whose point falls off the grid, or lands on a nodata (NaN) cell, is
    reported with np.nan for its elevation and kept in place, so callers
    can tell "the terrain here is unknown" from "the terrain here is below
    the waterline" -- the two must never collapse.
    """
    px, py = dem["resolution_meters"]
    step = min(float(px), float(py))
    array = dem["array"]
    x0, y0 = origin_xy
    ux, uy = perpendicular

    steps_out = int(math.floor(half_width_meters / step + 1e-9))
    offsets = [i * step for i in range(-steps_out, steps_out + 1)]
    elevations: list[float] = []
    for offset in offsets:
        cell = rowcol_for_xy(dem, x0 + ux * offset, y0 + uy * offset)
        if cell is None:
            elevations.append(float("nan"))
            continue
        elevations.append(float(array[cell[0], cell[1]]))
    return offsets, elevations


def _flooded_span(
    offsets: list[float],
    elevations: list[float],
    waterline_m: float,
    step: float,
) -> tuple[float, float, int]:
    """
    The CONTIGUOUS run of below-waterline samples AROUND THE CHANNEL --
    i.e. the run containing offset 0.0 -- as (flooded_width_meters,
    flooded_cross_section_area_m2, sample_count).

    Contiguous-around-the-channel, not "every below-waterline sample on the
    line": a perpendicular 75 m long on real terrain can clip an unrelated
    low pocket on the far side of a ridge, and counting it would report a
    cross-section the water cannot reach.

    Width is sample_count * step (each sample owns a step-wide slice of the
    line) and area is the same rectangle rule over the depth profile,
    sum(max(0, waterline - z) * step) -- a discrete integral, deliberately
    not a trapezoid: the DEM is a piecewise-constant grid, so a rectangle
    per cell is what the data actually says.

    (0.0, 0.0, 0) when the center sample is itself at or above the
    waterline, or is nodata -- an honest "no flooded cross-section here",
    never an interpolated one. NaN samples terminate the run on that side
    for the same reason.
    """
    if not offsets:
        return 0.0, 0.0, 0
    center = len(offsets) // 2
    if not math.isfinite(elevations[center]) or elevations[center] >= waterline_m:
        return 0.0, 0.0, 0

    lo = center
    while lo - 1 >= 0 and math.isfinite(elevations[lo - 1]) and elevations[lo - 1] < waterline_m:
        lo -= 1
    hi = center
    while hi + 1 < len(offsets) and math.isfinite(elevations[hi + 1]) and elevations[hi + 1] < waterline_m:
        hi += 1

    count = hi - lo + 1
    width = count * step
    area = sum((waterline_m - elevations[i]) * step for i in range(lo, hi + 1))
    return float(width), float(area), int(count)


def _find_abutment(
    dem: dict,
    anchor_xy: tuple[float, float],
    direction: tuple[float, float],
    waterline_m: float,
    half_width_meters: float,
) -> dict:
    """
    Walks one side of the dam axis from the anchor outward, at cell
    resolution, until RAW terrain rises to at least waterline_m (the
    abutment: where a wall at this waterline would key into the hillside)
    or half_width_meters runs out.

    Returns
        {
            'found': bool,
            'lateral_distance_m': float or None,   # to the abutment cell
            'rowcol': (row, col) or None,
            'elevation_m': float or None,
            'band_cells': [(row, col), ...],       # this side's sampled cells,
                                                   #   nearest-first, INCLUDING
                                                   #   the abutment cell
            'searched_distance_m': float,          # how far the walk got
            'left_grid': bool,                     # the walk ran off the DEM
        }

    found=False IS A REAL FINDING, not a failure: it says the ground stays
    below the waterline for half_width_meters, so no cheap wall keys in on
    this side. Nothing is retried at a wider radius (see
    ABUTMENT_SEARCH_HALF_WIDTH_METERS).

    The abutment cell IS included in band_cells: a dam wall lands ON its
    abutment, so the cell where terrain reaches the waterline is part of
    the structure's footprint, not past its end.

    Walking off the grid, or onto nodata, ends the walk with found=False
    and left_grid=True -- "we could not see far enough here", which must
    never be reported as "there is no abutment here".
    """
    px, py = dem["resolution_meters"]
    step = min(float(px), float(py))
    array = dem["array"]
    x0, y0 = anchor_xy
    ux, uy = direction

    band_cells: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    steps_out = int(math.floor(half_width_meters / step + 1e-9))
    searched = 0.0

    for i in range(1, steps_out + 1):
        distance = i * step
        cell = rowcol_for_xy(dem, x0 + ux * distance, y0 + uy * distance)
        if cell is None:
            return {
                "found": False,
                "lateral_distance_m": None,
                "rowcol": None,
                "elevation_m": None,
                "band_cells": band_cells,
                "searched_distance_m": round(searched, 3),
                "left_grid": True,
            }
        elevation = float(array[cell[0], cell[1]])
        if not math.isfinite(elevation):
            return {
                "found": False,
                "lateral_distance_m": None,
                "rowcol": None,
                "elevation_m": None,
                "band_cells": band_cells,
                "searched_distance_m": round(searched, 3),
                "left_grid": True,
            }
        searched = distance
        if cell not in seen:
            seen.add(cell)
            band_cells.append(cell)
        if elevation >= waterline_m:
            return {
                "found": True,
                "lateral_distance_m": round(distance, 3),
                "rowcol": cell,
                "elevation_m": round(elevation, 3),
                "band_cells": band_cells,
                "searched_distance_m": round(searched, 3),
                "left_grid": False,
            }

    return {
        "found": False,
        "lateral_distance_m": None,
        "rowcol": None,
        "elevation_m": None,
        "band_cells": band_cells,
        "searched_distance_m": round(searched, 3),
        "left_grid": False,
    }


def delineate_level_pool(
    dem: dict,
    filled: np.ndarray,
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
    flow_accumulation: np.ndarray,
    upstream_map: dict,
    anchor: tuple[int, int],
    reference_height_meters: float = POOL_REFERENCE_HEIGHT_METERS,
    abutment_search_half_width_meters: float = ABUTMENT_SEARCH_HALF_WIDTH_METERS,
    max_backwater_upstream_meters: float = MAX_BACKWATER_UPSTREAM_METERS,
    station_spacing_meters: float = CROSS_SECTION_STATION_SPACING_METERS,
    station_count: int = CROSS_SECTION_STATIONS,
    axis_walk_cells: int = VALLEY_AXIS_WALK_CELLS,
) -> dict:
    """
    Delineates the level pool at `anchor` and measures it. The core of this
    module; everything else here is a helper it calls.

    z0 is the RAW elevation at the anchor and the waterline is
    z_w = z0 + reference_height_meters. Three things are computed against
    that one waterline:

    1. THE BACKWATER REGION. Walk the upstream map outward from the anchor
       -- FULL FAN-OUT, every feeder of every visited cell, not just the
       main stem. Real backwater does not follow one channel; it spreads
       into every side draw whose ground sits below the waterline, and a
       stem-only walk would report a ribbon where the ground holds a fan.
       A visited cell joins the pool iff its RAW elevation is strictly
       below z_w. A path STOPS EXPANDING at the first cell that fails that
       test, is nodata, or whose cumulative along-path ground distance from
       the anchor exceeds max_backwater_upstream_meters -- and that cell is
       NOT included. Connectivity comes from the filled-DEM flow field; the
       elevation test reads the raw DEM (see the module docstring).

       STRUCTURAL GUARANTEE: no cell downstream of the anchor can enter the
       backwater, at any parameter setting. The upstream map is the exact
       inverse of the flow field, so walking it only ever reaches cells
       that drain INTO the anchor; a downstream cell is unreachable by
       construction, not by a filter that could be misconfigured. The dam
       band is therefore the zone's downstream edge, with the anchor on it.
       Asserted in test_valley_level_pool.py.

    2. THE DAM-AXIS BAND. fit_valley_axis() gives the local downstream
       direction; the dam axis is its perpendicular through the anchor.
       Both sides are walked at cell resolution until raw terrain reaches
       z_w (abutment found, distance recorded) or
       abutment_search_half_width_meters runs out (abutment_found_<side> =
       False -- a real finding, see _find_abutment()). Sampled points are
       mapped to cells and deduped.

       LEFT and RIGHT are named LOOKING DOWNSTREAM, the standard
       hydrological convention: left is the +90 degree rotation of the
       downstream axis (counter-clockwise in the +x-east/+y-north UTM
       frame), right is -90.

    3. CROSS-SECTION MEASUREMENTS at `station_count` stations spaced
       station_spacing_meters apart along the valley axis, UPSTREAM of the
       anchor (station 0 is the anchor itself). Each records the flooded
       width and flooded cross-sectional area of the contiguous
       below-waterline span around the channel. These are computed HERE,
       in the same pass that produced the pool geometry, specifically so
       the geometry and the numbers describing it cannot drift apart --
       a later scoring branch reading these is reading measurements of the
       exact polygon this run drew.

       NO VOLUME IS COMPUTED, STORED, OR REPORTED, here or downstream. See
       the module docstring for why. No key below names a capacity.

    Returns one dict:

        {
          'anchor_rowcol': (row, col),
          'anchor_elevation_m': float,       # RAW
          'waterline_elevation_m': float,    # anchor + reference height
          'reference_height_meters': float,
          'valley_axis_unit': (ux, uy),      # downstream-pointing unit vector
          'dam_axis_unit': (ux, uy),         # the perpendicular (left-pointing)
          'pool_cells': [(row, col), ...],       # backwater, anchor included
          'pool_cell_distance_m': {(row, col): float},  # along-path distance
                                                 #   from the anchor -- what the
                                                 #   caller's area cap truncates by
          'band_cells': [(row, col), ...],       # dam axis, anchor included
          'zone_cells': [(row, col), ...],       # pool UNION band
          'abutments': {'left': {...}, 'right': {...}},   # _find_abutment() dicts
          'abutment_found_left': bool,
          'abutment_found_right': bool,
          'dam_band_width_m': float,             # left + right searched extent
                                                 #   plus the anchor's own cell
          'stations': [ {                        # measurements, NOT a design
              'station_index': int,
              'offset_upstream_m': float,
              'on_grid': bool,
              'flooded_width_m': float or None,
              'flooded_cross_section_area_m2': float or None,
              'sample_count': int or None,
            }, ... ],
          'backwater_distance_limited': bool,    # some path hit the reach cap
          'backwater_cell_count': int,
        }
    """
    rows, cols = dem["array"].shape
    r0, c0 = int(anchor[0]), int(anchor[1])
    if not (0 <= r0 < rows and 0 <= c0 < cols):
        raise ValueError(f"delineate_level_pool: anchor {anchor} is outside the {rows}x{cols} DEM grid")

    raw = dem["array"]
    anchor_elevation = float(raw[r0, c0])
    if not math.isfinite(anchor_elevation):
        raise ValueError(f"delineate_level_pool: anchor {anchor} sits on a nodata (NaN) DEM cell")
    waterline = anchor_elevation + float(reference_height_meters)

    # --- 1. backwater: full fan-out over the FILLED flow field's inverse,
    # --- with the inclusion test read off the RAW DEM.
    anchor_cell = (r0, c0)
    pool_distance: dict[tuple[int, int], float] = {anchor_cell: 0.0}
    pool_cells: list[tuple[int, int]] = [anchor_cell]
    distance_limited = False
    frontier = [anchor_cell]
    while frontier:
        current = frontier.pop()
        for feeder in upstream_map.get(current, ()):
            if feeder in pool_distance:
                continue
            distance = pool_distance[current] + _cell_center_distance(dem, current, feeder)
            if distance > max_backwater_upstream_meters:
                distance_limited = True
                continue
            elevation = float(raw[feeder[0], feeder[1]])
            if not math.isfinite(elevation) or elevation >= waterline:
                continue
            pool_distance[feeder] = distance
            pool_cells.append(feeder)
            frontier.append(feeder)

    # --- 2. dam-axis band, perpendicular to the fitted valley axis.
    axis = fit_valley_axis(
        dem, anchor_cell, flow_to_row, flow_to_col, upstream_map, flow_accumulation, walk_cells=axis_walk_cells
    )
    # +90 degrees from downstream = river-left, looking downstream.
    dam_axis_left = (-axis[1], axis[0])
    dam_axis_right = (axis[1], -axis[0])
    anchor_xy = pixel_center_xy(dem, r0, c0)

    left = _find_abutment(dem, anchor_xy, dam_axis_left, waterline, abutment_search_half_width_meters)
    right = _find_abutment(dem, anchor_xy, dam_axis_right, waterline, abutment_search_half_width_meters)

    band_cells: list[tuple[int, int]] = [anchor_cell]
    band_seen = {anchor_cell}
    for cell in list(left["band_cells"]) + list(right["band_cells"]):
        if cell not in band_seen:
            band_seen.add(cell)
            band_cells.append(cell)

    px, py = dem["resolution_meters"]
    cell_step = min(float(px), float(py))
    dam_band_width = float(left["searched_distance_m"]) + float(right["searched_distance_m"]) + cell_step

    # --- 3. cross-section measurements at the upstream stations.
    stations: list[dict] = []
    for index in range(int(station_count)):
        offset = index * float(station_spacing_meters)
        station_xy = (anchor_xy[0] - axis[0] * offset, anchor_xy[1] - axis[1] * offset)
        station_cell = rowcol_for_xy(dem, station_xy[0], station_xy[1])
        if station_cell is None:
            stations.append(
                {
                    "station_index": index,
                    "offset_upstream_m": round(offset, 3),
                    "on_grid": False,
                    "flooded_width_m": None,
                    "flooded_cross_section_area_m2": None,
                    "sample_count": None,
                }
            )
            continue
        offsets, elevations = _sample_perpendicular(
            dem, station_xy, dam_axis_left, abutment_search_half_width_meters
        )
        width, area, count = _flooded_span(offsets, elevations, waterline, cell_step)
        stations.append(
            {
                "station_index": index,
                "offset_upstream_m": round(offset, 3),
                "on_grid": True,
                "flooded_width_m": round(width, 3),
                "flooded_cross_section_area_m2": round(area, 3),
                "sample_count": count,
            }
        )

    zone_cells = list(pool_cells)
    pool_set = set(pool_cells)
    for cell in band_cells:
        if cell not in pool_set:
            pool_set.add(cell)
            zone_cells.append(cell)

    return {
        "anchor_rowcol": anchor_cell,
        "anchor_elevation_m": round(anchor_elevation, 3),
        "waterline_elevation_m": round(waterline, 3),
        "reference_height_meters": float(reference_height_meters),
        "valley_axis_unit": axis,
        "dam_axis_unit": dam_axis_left,
        "pool_cells": pool_cells,
        "pool_cell_distance_m": pool_distance,
        "band_cells": band_cells,
        "zone_cells": zone_cells,
        "abutments": {"left": left, "right": right},
        "abutment_found_left": bool(left["found"]),
        "abutment_found_right": bool(right["found"]),
        "dam_band_width_m": round(dam_band_width, 3),
        "stations": stations,
        "backwater_distance_limited": bool(distance_limited),
        "backwater_cell_count": len(pool_cells),
    }
