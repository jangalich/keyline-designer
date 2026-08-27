"""
valley_level_pool.py

DEMOTED, NOT DELETED: this module is no longer on the pipeline path.
water_survey_areas.py is the water step now (typed survey areas from
suitability surfaces -- see that module's docstring for the redesign);
the level-pool arc this module powered proved the reference property
lacks keyline-dam geometry. RETAINED as a diagnostic-consumed module:
the exploration scripts still import it through water_candidate_zones.py
(itself demoted alongside), and level-pool delineation remains a future
"verify this survey area" SECOND stage -- something to run at a specific
anchor inside a chosen survey area once one is ground-truthed, not the
thing that nominates areas. Do not re-wire it into the pipeline; do not
delete it while those consumers stand.

Level-pool delineation at a single anchor cell: given an already-fetched
DEM plus precomputed D8 flow arrays, this module answers "if a dam wall
stood on this cell, what ground would sit under the waterline, and what
would the wall have to span?" -- as GEOMETRY AND RELATIVE MEASUREMENTS,
never as a design.

    dem + filled + (flow_to_row, flow_to_col) + upstream map + anchor cell
        --> the traced STEM through the anchor (_stem_through_anchor)
        --> backwater region   (walk the upstream map, raw-elevation test)
        --> dam-axis band      (perpendicular to the stem's local direction
                                at the anchor, raw-elevation test)
        --> cross-section measurements at 3 stations ALONG THE STEM, each
            facing the stem's local direction at its own position
        --> one dict: pool cells, band cells, abutment results, station
            measurements, flags

EVERY DIRECTION COMES FROM THE STEM, AT THE PLACE IT IS NEEDED. Nothing is
fitted. An earlier version derived a single straight valley axis at the
anchor by total least squares through a short walk and used it for the dam
band AND for placing every station -- and that straight-line assumption is
what this module no longer makes. Where a valley is emphatic the fit was
harmless; where it is subtle -- flat marshy ground, or a reach whose flow
field is thinned by valley_delineation.py's flat-tie sentinel -- it had
almost no signal and could settle up to 90 degrees off, putting the
"perpendicular" ALONG the channel and marching the stations up a side
slope. On the reference property five of six candidates reported a
station-0 flooded width of exactly the full sampling window with the next
station bone dry: not a valley profile, the signature of a rotated axis.
See local_stem_direction() for the replacement and STEM_DIRECTION_WINDOW_
CELLS for the one remaining tuning knob.

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

UNREACHABLE IS NOT DRY. A station past the end of the traced stem reports
status 'unreachable_stem_end' with the along-stem distance actually
reached, and its width and area are absent (None) -- never 0.0. Zero width
is a real measurement, "the ground rises above the waterline here";
unreachable is the absence of one, "there is no channel here to measure."
Both statuses are part of the measurement contract the scoring branch
reads. This is also where valley_delineation.py's known flat-tie
limitation now shows itself honestly -- as short stems and unreachable
stations rather than as fabricated dry ones. It is FLAGGED here, not fixed
here; the fix belongs in the hydrology layer.

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
# THE VALUE IS BRACKETED BY PUBLISHED THRESHOLDS rather than merely
# assumed. It has not moved, and every hand-derived test fixture in
# test_valley_level_pool.py is built on it; what follows is the sourcing
# that was previously missing.
#
#   LOWER BOUND, ~0.9 m. NRCS Conservation Practice Standard 378 (Pond)
#   defines an embankment pond as one impounding at least 3 ft of water
#   against the embankment. Below that the survey would not be measuring
#   an embankment pond at all, so a reference height under ~0.9 m would
#   rank sites against a structure outside the practice being surveyed.
#   Independently, ~0.9 m is roughly 25x the vertical RMSE of the USGS
#   3DEP LiDAR this pipeline's DEM comes from -- below it the waterline
#   would be competing with the elevation data's own noise.
#
#   UPPER BOUND, ~4.6 m. Pennsylvania 25 Pa. Code Ch. 105 requires a dam
#   permit once depth at the upstream toe exceeds 15 ft (or the drainage
#   area exceeds 100 acres, or capacity exceeds 50 acre-feet). At 2.5 m
#   (~8.2 ft) the reference stays well inside the unregulated farm-pond
#   envelope, which is the scale this survey is about: it simulates a farm
#   pond, not a regulated dam. THIS CITATION IS JURISDICTION-SPECIFIC to
#   the reference property -- NRCS above is the national primary, and a
#   property in another state needs its own threshold checked before this
#   bound means anything there.
#
#   THE MIDDLE. Typical farm-pond design depths run roughly 6-12 ft, and
#   Yeomans' own built keyline dams generally ran larger than that. So
#   2.5 m sits inside common practice and is CONSERVATIVE relative to the
#   tradition this feature is modelled on -- it will under-report, rather
#   than over-report, what a site could hold.
#
# Still NOT validated against a real built pond on this or any property:
# the bracket says the number is defensible, not that it is calibrated.
# CONFIGURABLE.
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

# Station status values -- part of the measurement contract the scoring
# branch reads. 'measured' means the numbers alongside describe real
# ground; 'unreachable_stem_end' means the traced stem ended before this
# station and there was no channel here to measure, so width and area are
# ABSENT (None) rather than zero. Collapsing the two would let a marsh
# whose flow field died score like a ridge.
STATION_MEASURED = "measured"
STATION_UNREACHABLE_STEM_END = "unreachable_stem_end"

# Half-width, in stem cells, of the SECANT WINDOW used to read the stem's
# local direction at a point (so the secant spans up to 2 * 2 + 1 = 5 cell
# centers). Used at the anchor for the dam-axis perpendicular, and again at
# each cross-section station for that station's own perpendicular.
#
# A window exists at all because RAW D8 DIRECTION IS TOO QUANTIZED TO
# DEFINE A PERPENDICULAR: D8 only ever names one of 8 directions, so a
# perpendicular built off a single step snaps to 45 degree increments and
# could be up to 22.5 degrees off the real channel line -- at
# ABUTMENT_SEARCH_HALF_WIDTH_METERS that is tens of meters of lateral error
# in where a section is taken. Averaging over a few cells recovers a
# continuous direction from the same quantized steps.
#
# 2 cells (a 20 m secant at 5 m resolution) is the smallest window that
# does that, and deliberately small: the window must stay SHORT enough to
# follow a bend. It replaced a 4-cell half-window feeding a straight-line
# fit through the whole neighbourhood, which is exactly the assumption this
# design removed (see local_stem_direction()). Widen it and curved valleys
# start being cut corners; narrow it to 1 and the 45 degree quantization
# comes back. NOT validated beyond the reference property. CONFIGURABLE.
STEM_DIRECTION_WINDOW_CELLS = 2


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


def _stem_through_anchor(
    dem: dict,
    anchor: tuple[int, int],
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
    upstream_map: dict,
    flow_accumulation: np.ndarray,
    upstream_steps: int,
    downstream_steps: int,
) -> tuple[list[tuple[int, int]], int, list[float]]:
    """
    The traced stem polyline THROUGH `anchor`, as
    (stem_cells, anchor_index, along_stem_distance_m).

    stem_cells is ordered DOWNSTREAM-FIRST, so the index increases going
    UPSTREAM and `anchor_index` is the anchor's position in it. Both halves
    reuse the module's existing walks -- _downstream_walk() through the D8
    flow field, _upstream_stem_walk() taking the highest-accumulation feeder
    over the upstream map -- which are themselves keypoint_detection.py's
    own conventions rather than a second definition of "the stem".

    along_stem_distance_m is SIGNED, measured from the anchor along the
    polyline: negative downstream, 0 at the anchor, positive upstream. It
    accumulates REAL ground distance between successive cell centers, so a
    diagonal D8 step correctly costs sqrt(2)x a cardinal one -- the same
    accumulation keypoint_detection._profile_along_stem() performs.

    NO PRECOMPUTED STEM IS ACCEPTED, and that is a decision rather than an
    omission. The obvious candidate would be keypoint_detection.
    trace_stem_from_outlet()'s stem, but it traces from a VALLEY'S OUTLET,
    not through an arbitrary cell: a keypoint sits somewhere along it (so
    the caller would still have to locate the anchor within it and handle
    the case where it does not appear at all), and a FAMILY-2 anchor has no
    such stem in existence. Tracing locally is both families' only common
    path, costs a bounded handful of array lookups, and keeps this function
    pure. If a caller ever does hold a real stem through its anchor, an
    override belongs here and must be forwarded per the nested-forwarding
    rule; nothing in the pipeline holds one today.
    """
    downstream = _downstream_walk(anchor, flow_to_row, flow_to_col, downstream_steps)
    upstream = _upstream_stem_walk(anchor, upstream_map, flow_accumulation, upstream_steps)

    stem_cells = list(reversed(downstream)) + [anchor] + upstream
    anchor_index = len(downstream)

    along: list[float] = [0.0] * len(stem_cells)
    for i in range(anchor_index + 1, len(stem_cells)):
        along[i] = along[i - 1] + _cell_center_distance(dem, stem_cells[i - 1], stem_cells[i])
    for i in range(anchor_index - 1, -1, -1):
        along[i] = along[i + 1] - _cell_center_distance(dem, stem_cells[i + 1], stem_cells[i])
    return stem_cells, anchor_index, along


def local_stem_direction(
    dem: dict,
    stem_cells: list[tuple[int, int]],
    index: int,
    window_cells: int = STEM_DIRECTION_WINDOW_CELLS,
) -> tuple[tuple[float, float], bool]:
    """
    The DOWNSTREAM-pointing unit direction of the stem at `index`, as
    (unit_vector, degenerate_flag).

    Method: the SECANT across a window of +/- window_cells stem cells
    centered on `index`, clamped at the stem's ends. Because the stem is
    ordered downstream-first, "downstream" is the direction of DECREASING
    index, so the vector runs from the upper-index endpoint to the
    lower-index one.

    WHY A SECANT AND NOT THE RAW STEP: a single D8 step names one of only
    eight directions, so a perpendicular built from it snaps to 45 degree
    increments -- up to 22.5 degrees of error, which at
    ABUTMENT_SEARCH_HALF_WIDTH_METERS is tens of meters of lateral error in
    where a cross-section is taken. Averaging over a few cells recovers a
    continuous direction from the same quantized steps.

    WHY LOCAL AND NOT A LINE THROUGH THE WHOLE WALK: this REPLACED a global
    total-least-squares fit of a straight line through the anchor's
    neighbourhood, and the replacement is the point of this design. A
    straight axis is only as good as the assumption that the valley IS
    straight there. Where the valley is emphatic (a confluence, a sharp V)
    that assumption is harmless; where it is subtle -- flat, marshy ground,
    or a reach whose flow field is thinned by the flat-tie sentinel -- the
    fit has almost no signal to work with and can settle up to 90 degrees
    off, which puts the "perpendicular" ALONG the channel and marches the
    stations up a side slope. Observed on the reference property: five of
    six candidates reported a station-0 flooded width of exactly the full
    sampling window with the next station bone dry, which is not a valley
    profile at all. Nothing is fitted now: every direction comes from the
    stem itself, at the place it is needed, so a curved valley is followed
    rather than approximated.

    DEGENERATE WINDOWS. If the clamped endpoints coincide (they can only do
    so on a stem of length 1, or when both clamp to the same cell), the
    window is WIDENED until they do not. If no width separates them, the
    second return value is True and the caller is handed a fallback
    direction rather than a zero vector -- an honest "this stem has no
    direction" rather than a silent (0, 0).
    """
    n = len(stem_cells)
    if n == 0:
        return (0.0, -1.0), True
    for width in range(max(1, int(window_cells)), n + 1):
        lo = max(0, index - width)
        hi = min(n - 1, index + width)
        if lo == hi:
            continue
        ax, ay = pixel_center_xy(dem, *stem_cells[hi])   # upstream end
        bx, by = pixel_center_xy(dem, *stem_cells[lo])   # downstream end
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        if norm > 0.0:
            return (dx / norm, dy / norm), False
    _LOGGER.debug(
        "local_stem_direction: no non-degenerate window at index %d of a %d-cell stem", index, n
    )
    return (0.0, -1.0), True


def bearing_degrees(direction: tuple[float, float]) -> float:
    """
    A direction vector as a COMPASS BEARING: 0 = north, increasing
    clockwise, in [0, 360). UTM axes are +x east / +y north, so
    atan2(dx, dy) is already that bearing. Reported on stations and the dam
    band so a diagnostic (or a test) can compare directions in the units a
    person reads a map in, rather than in raw vector components.
    """
    return round(math.degrees(math.atan2(direction[0], direction[1])) % 360.0, 2)


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
    flow_accumulation: Optional[np.ndarray] = None,
    max_contributing_cells: Optional[float] = None,
) -> dict:
    """
    Walks one side of the dam axis from the anchor outward, at cell
    resolution, until one of three things happens: RAW terrain rises to at
    least waterline_m (the abutment -- where a wall at this waterline would
    key into the hillside), the walk crosses a cell carrying more
    contributing area than max_contributing_cells (a NEIGHBOURING major
    drainage), or half_width_meters runs out.

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
            'crosses_major_drainage': bool,        # see below
            'major_drainage_distance_m': float or None,
            'major_drainage_contributing_cells': float or None,
        }

    found=False IS A REAL FINDING, not a failure: it says the ground stays
    below the waterline for half_width_meters, so no cheap wall keys in on
    this side. Nothing is retried at a wider radius (see
    ABUTMENT_SEARCH_HALF_WIDTH_METERS).

    THE CONTRIBUTING-AREA CEILING ON THE BAND is a separate, and separately
    reported, outcome. The lateral search can cross a second channel that
    happens to sit below the waterline within the half-width -- and a dam
    axis crossing a 20+ acre drainage is a real siting problem, not a
    longer wall. The band is truncated at that cell (it is NOT included)
    and crosses_major_drainage is set, with the distance and the
    contributing-cell count that tripped it.

    THAT FLAG IS DELIBERATELY DISTINCT FROM found=False, and the caller
    must keep them distinct: "no shoulder within range" and "the wall would
    dam a second creek" are different survey findings with different
    remedies, and a truncated-at-a-creek side reports crosses_major_
    drainage WITHOUT reporting an abutment that was never looked for past
    that point.

    The abutment cell IS included in band_cells: a dam wall lands ON its
    abutment, so the cell where terrain reaches the waterline is part of
    the structure's footprint, not past its end. A major-drainage cell is
    NOT included, for the mirror-image reason: the band stops before it.

    flow_accumulation / max_contributing_cells are the ceiling's two
    inputs, passed IN rather than derived: this module stays pure and
    network-free, and the caller already holds both (the same accumulation
    grid its nomination mask was built from, so the band and the mask
    cannot be gated against two different grids). Omitting either disables
    the ceiling check entirely -- the honest "not asked" behaviour, used by
    the synthetic fixtures that only care about geometry.

    Walking off the grid, or onto nodata, ends the walk with found=False
    and left_grid=True -- "we could not see far enough here", which must
    never be reported as "there is no abutment here".
    """
    px, py = dem["resolution_meters"]
    step = min(float(px), float(py))
    array = dem["array"]
    x0, y0 = anchor_xy
    ux, uy = direction
    ceiling_checked = flow_accumulation is not None and max_contributing_cells is not None

    band_cells: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    steps_out = int(math.floor(half_width_meters / step + 1e-9))
    searched = 0.0

    def _result(**overrides) -> dict:
        base = {
            "found": False,
            "lateral_distance_m": None,
            "rowcol": None,
            "elevation_m": None,
            "band_cells": band_cells,
            "searched_distance_m": round(searched, 3),
            "left_grid": False,
            "crosses_major_drainage": False,
            "major_drainage_distance_m": None,
            "major_drainage_contributing_cells": None,
        }
        base.update(overrides)
        return base

    for i in range(1, steps_out + 1):
        distance = i * step
        cell = rowcol_for_xy(dem, x0 + ux * distance, y0 + uy * distance)
        if cell is None:
            return _result(left_grid=True)
        elevation = float(array[cell[0], cell[1]])
        if not math.isfinite(elevation):
            return _result(left_grid=True)

        # The ceiling is tested BEFORE the cell joins the band: a cell that
        # trips it is where the band stops, not its last member.
        if ceiling_checked:
            contributing = float(flow_accumulation[cell[0], cell[1]])
            if contributing > float(max_contributing_cells):
                return _result(
                    crosses_major_drainage=True,
                    major_drainage_distance_m=round(distance, 3),
                    major_drainage_contributing_cells=contributing,
                )

        searched = distance
        if cell not in seen:
            seen.add(cell)
            band_cells.append(cell)
        if elevation >= waterline_m:
            return _result(
                found=True,
                lateral_distance_m=round(distance, 3),
                rowcol=cell,
                elevation_m=round(elevation, 3),
            )

    return _result()


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
    stem_direction_window_cells: int = STEM_DIRECTION_WINDOW_CELLS,
    max_contributing_cells: Optional[float] = None,
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

    2. THE DAM-AXIS BAND. The stem's LOCAL direction at the anchor (a
       secant over +/- stem_direction_window_cells stem cells, see
       local_stem_direction()) gives the downstream direction; the dam axis
       is its perpendicular through the anchor.
       Both sides are walked at cell resolution until raw terrain reaches
       z_w (abutment found, distance recorded) or
       abutment_search_half_width_meters runs out (abutment_found_<side> =
       False -- a real finding, see _find_abutment()). Sampled points are
       mapped to cells and deduped.

       LEFT and RIGHT are named LOOKING DOWNSTREAM, the standard
       hydrological convention: left is the +90 degree rotation of the
       downstream axis (counter-clockwise in the +x-east/+y-north UTM
       frame), right is -90.

       THE CONTRIBUTING-AREA CEILING BINDS HERE, AND ONLY HERE. When
       max_contributing_cells is supplied, a lateral walk that reaches a
       cell carrying more contributing area than that stops there and
       reports crosses_major_drainage on its side -- the dam axis would
       cross a second drainage. This is the one place in the whole
       delineation where the ceiling can genuinely bind: it is a NO-OP on
       the backwater by construction, because every backwater cell is
       upstream of the anchor and flow accumulation decreases strictly
       upstream, so no backwater cell can exceed an anchor that already
       cleared the ceiling. That is a structural guarantee, asserted in
       test_valley_level_pool.py rather than re-checked per cell at
       runtime.

    3. CROSS-SECTION MEASUREMENTS at `station_count` stations spaced
       station_spacing_meters apart ALONG THE TRACED STEM, upstream of the
       anchor (station 0 is the anchor itself). Each records the flooded
       width and flooded cross-sectional area of the contiguous
       below-waterline span around the channel. These are computed HERE,
       in the same pass that produced the pool geometry, specifically so
       the geometry and the numbers describing it cannot drift apart --
       a later scoring branch reading these is reading measurements of the
       exact polygon this run drew.

       STATIONS SIT ON THE STEM, AND EACH FACES ITS OWN WAY. A station is
       placed by WALKING the stem polyline and accumulating real ground
       distance, then taking the stem cell nearest the target distance; its
       perpendicular comes from the stem's LOCAL direction at that cell,
       not from any single direction shared across the run. That is what
       makes a curved valley work, and no straight axis can do it: past a
       bend, a fixed direction walks the stations off the channel and
       cross-sections them at the wrong angle. (A nearest-CELL placement,
       rather than interpolating a point along the segment, is deliberate:
       it guarantees the station is a real channel cell whose elevation is
       the DEM's own, which is what makes these numbers hand-checkable.)

       UNREACHABLE IS NOT DRY. The stem walk can end before the last
       station -- a flat-tie -1 sentinel in the flow field, the grid edge,
       or simply a stem shorter than station_count * spacing. A station
       past the stem's end reports status "unreachable_stem_end" with the
       along-stem distance actually reached, and its width/area are None.
       IT IS NEVER REPORTED AS 0.0 m WIDE. Those are different facts:
       0.0 m is a MEASUREMENT ("the ground rises above the waterline
       here"), unreachable is the ABSENCE of one ("there is no channel
       here to measure"). A scoring layer that averaged them together
       would score a marsh whose flow field died as though it were a
       ridge. This is also where valley_delineation.py's known flat-tie
       limitation now surfaces honestly -- as short stems and unreachable
       stations rather than as fabricated dry ones. It is FLAGGED here,
       not fixed here.

       NO VOLUME IS COMPUTED, STORED, OR REPORTED, here or downstream. See
       the module docstring for why. No key below names a capacity.

    Returns one dict:

        {
          'anchor_rowcol': (row, col),
          'anchor_elevation_m': float,       # RAW
          'waterline_elevation_m': float,    # anchor + reference height
          'reference_height_meters': float,
          'anchor_direction_unit': (ux, uy), # the STEM's local downstream
                                             #   direction at the anchor
          'anchor_bearing_deg': float,       # the same, as a compass bearing
          'dam_axis_unit': (ux, uy),         # its perpendicular (left-pointing)
          'stem_cells': [(row, col), ...],   # the traced polyline through the
                                             #   anchor, downstream-first
          'anchor_stem_index': int,          # the anchor's position in it
          'stem_upstream_length_m': float,   # along-stem reach above the anchor
          'stem_downstream_length_m': float,
          'stem_direction_degenerate': bool, # no window separated two cells
          'pool_cells': [(row, col), ...],       # backwater, anchor included
          'pool_cell_distance_m': {(row, col): float},  # along-path distance
                                                 #   from the anchor -- what the
                                                 #   caller's area cap truncates by
          'band_cells': [(row, col), ...],       # dam axis, anchor included
          'zone_cells': [(row, col), ...],       # pool UNION band
          'abutments': {'left': {...}, 'right': {...}},   # _find_abutment() dicts
          'abutment_found_left': bool,
          'abutment_found_right': bool,
          'dam_band_crosses_major_drainage_left': bool,   # distinct from
          'dam_band_crosses_major_drainage_right': bool,  #   abutment_found_*
          'dam_band_width_m': float,             # left + right searched extent
                                                 #   plus the anchor's own cell
          'stations': [ {                        # measurements, NOT a design
              'station_index': int,
              'offset_upstream_m': float,        # the TARGET along-stem offset
              'status': 'measured' | 'unreachable_stem_end',
              'along_stem_distance_m': float,    # what the walk actually reached
              'stem_rowcol': (row, col) or None,
              'channel_elevation_m': float or None,
              'bearing_deg': float or None,      # local downstream bearing here
              'on_grid': bool,
              'flooded_width_m': float or None,  # None when unreachable --
              'flooded_cross_section_area_m2': float or None,   # NEVER 0.0
              'sample_count': int or None,
            }, ... ],
            # STATUS IS PART OF THE MEASUREMENT CONTRACT the scoring branch
            # reads: 'measured' means these numbers describe real ground,
            # 'unreachable_stem_end' means there was no channel to measure
            # and the numbers are absent rather than zero.
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

    # --- 2. the traced stem through the anchor, then the dam-axis band
    # --- perpendicular to the stem's LOCAL direction there.
    px, py = dem["resolution_meters"]
    cell_step = min(float(px), float(py))
    window = max(1, int(stem_direction_window_cells))
    # Trace far enough upstream to reach the last station plus the secant
    # window that station needs, and far enough downstream to give the
    # ANCHOR a symmetric window. A cardinal step is the shortest a D8 walk
    # can take, so dividing by cell_step bounds the cells required.
    max_station_offset = max(0, int(station_count) - 1) * float(station_spacing_meters)
    upstream_steps = int(math.ceil(max_station_offset / cell_step)) + window + 1
    stem_cells, anchor_index, stem_along = _stem_through_anchor(
        dem, anchor_cell, flow_to_row, flow_to_col, upstream_map, flow_accumulation,
        upstream_steps=upstream_steps, downstream_steps=window,
    )
    anchor_direction, direction_degenerate = local_stem_direction(
        dem, stem_cells, anchor_index, window
    )
    # +90 degrees from downstream = river-left, looking downstream.
    dam_axis_left = (-anchor_direction[1], anchor_direction[0])
    dam_axis_right = (anchor_direction[1], -anchor_direction[0])
    anchor_xy = pixel_center_xy(dem, r0, c0)

    left = _find_abutment(
        dem, anchor_xy, dam_axis_left, waterline, abutment_search_half_width_meters,
        flow_accumulation=flow_accumulation, max_contributing_cells=max_contributing_cells,
    )
    right = _find_abutment(
        dem, anchor_xy, dam_axis_right, waterline, abutment_search_half_width_meters,
        flow_accumulation=flow_accumulation, max_contributing_cells=max_contributing_cells,
    )

    band_cells: list[tuple[int, int]] = [anchor_cell]
    band_seen = {anchor_cell}
    for cell in list(left["band_cells"]) + list(right["band_cells"]):
        if cell not in band_seen:
            band_seen.add(cell)
            band_cells.append(cell)

    dam_band_width = float(left["searched_distance_m"]) + float(right["searched_distance_m"]) + cell_step

    # --- 3. cross-section measurements at stations ALONG THE STEM.
    stem_upstream_length = stem_along[-1] if stem_cells else 0.0
    stem_downstream_length = -stem_along[0] if stem_cells else 0.0
    stations: list[dict] = []
    for index in range(int(station_count)):
        offset = index * float(station_spacing_meters)
        # The stem cell nearest this along-stem offset, searching only the
        # UPSTREAM half (index >= anchor_index) so a station can never fall
        # downstream of the dam line.
        if offset > stem_upstream_length + 1e-9:
            # The stem ran out before this station. UNREACHABLE, not dry --
            # see the docstring: a 0.0 m width here would be a fabricated
            # measurement of ground the walk never saw.
            stations.append(
                {
                    "station_index": index,
                    "offset_upstream_m": round(offset, 3),
                    "status": STATION_UNREACHABLE_STEM_END,
                    "along_stem_distance_m": round(stem_upstream_length, 3),
                    "stem_rowcol": stem_cells[-1] if stem_cells else None,
                    "channel_elevation_m": None,
                    "bearing_deg": None,
                    "on_grid": True,
                    "flooded_width_m": None,
                    "flooded_cross_section_area_m2": None,
                    "sample_count": None,
                }
            )
            continue

        station_index_in_stem = min(
            range(anchor_index, len(stem_cells)),
            key=lambda i: (abs(stem_along[i] - offset), i),
        )
        station_cell = stem_cells[station_index_in_stem]
        station_xy = pixel_center_xy(dem, station_cell[0], station_cell[1])
        # THIS station's own perpendicular, from the stem's direction HERE.
        station_direction, _station_degenerate = local_stem_direction(
            dem, stem_cells, station_index_in_stem, window
        )
        station_perpendicular = (-station_direction[1], station_direction[0])
        offsets, elevations = _sample_perpendicular(
            dem, station_xy, station_perpendicular, abutment_search_half_width_meters
        )
        width, area, count = _flooded_span(offsets, elevations, waterline, cell_step)
        stations.append(
            {
                "station_index": index,
                "offset_upstream_m": round(offset, 3),
                "status": STATION_MEASURED,
                "along_stem_distance_m": round(stem_along[station_index_in_stem], 3),
                "stem_rowcol": station_cell,
                "channel_elevation_m": round(float(raw[station_cell[0], station_cell[1]]), 3),
                "bearing_deg": bearing_degrees(station_direction),
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
        "anchor_direction_unit": anchor_direction,
        "anchor_bearing_deg": bearing_degrees(anchor_direction),
        "dam_axis_unit": dam_axis_left,
        "stem_cells": stem_cells,
        "anchor_stem_index": anchor_index,
        "stem_upstream_length_m": round(stem_upstream_length, 3),
        "stem_downstream_length_m": round(stem_downstream_length, 3),
        "stem_direction_degenerate": bool(direction_degenerate),
        "pool_cells": pool_cells,
        "pool_cell_distance_m": pool_distance,
        "band_cells": band_cells,
        "zone_cells": zone_cells,
        "abutments": {"left": left, "right": right},
        "abutment_found_left": bool(left["found"]),
        "abutment_found_right": bool(right["found"]),
        "dam_band_crosses_major_drainage_left": bool(left["crosses_major_drainage"]),
        "dam_band_crosses_major_drainage_right": bool(right["crosses_major_drainage"]),
        "dam_band_width_m": round(dam_band_width, 3),
        "stations": stations,
        "backwater_distance_limited": bool(distance_limited),
        "backwater_cell_count": len(pool_cells),
    }
