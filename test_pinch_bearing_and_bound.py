"""
test_pinch_bearing_and_bound.py

THE DE-QUANTIZED PINCH BEARING (a PRODUCTION change) and the HALF-WIDTH
BOUND SWEEP (an instrument that chooses nothing).

walk_embankment_pinch() used to measure every width perpendicular to
_flow_direction_unit(), the raw D8 step -- one of eight bearings, so a
perpendicular built from it is up to 22.5 degrees off the true
cross-section. It now measures perpendicular to local_stem_direction()'s
de-quantized secant over +/- 2 path cells, reused from
valley_level_pool.py.

WHAT THAT ACTUALLY BUYS, and it is not the ~8% worst-case width
inflation. The error depends on which of eight headings each cell
snapped to, so it varies STATION TO STATION and the width profile
carries a sawtooth that is a property of the grid, not the ground. The
pinch is the MINIMUM of that profile. Section 4's fixture shows the
consequence in its clearest form: D8 flattens a real 20 m throat into a
five-way tie with ordinary 37.5 m valley and then picks the wrong end of
that tie, two cells upstream of the actual narrows -- and the cell it
picks goes on to determine the catchment, the baseline, both transects
and the drawn zone.

Run as:

    python test_pinch_bearing_and_bound.py

Sections (the design's numbered test items in brackets):
  1  [1]  BEARING SWAP -- a diagonal channel D8 cannot represent: the
          sawtooth under D8 against a flat profile under the secant,
          both sets of numbers stated.
  2  [2]  STRAIGHT-VALLEY REGRESSION -- where D8 is exact, the two
          agree station for station and the pinch cell is identical.
  3  [3]  CLAMPED WINDOWS -- a walk short enough that every station is
          one-sided still produces a valid profile, and says so.
  4  [4]  PINCH RELOCATION -- the quantization picks the wrong minimum;
          the secant picks the genuinely tighter station. Both profiles
          in the comment.
  5  [5]  THE BOUND SWEEP -- a flank at 120 m: absent at 100 m,
          measured at 150 m with a hand-derived crest height; and a
          grid-edge flank reported as grid edge, never as bound.
  6  [6]  CONTRACT -- consumer-read fields intact, the sweep is
          instrument-only, and whether selected_water_zone moves is
          REPORTED rather than asserted away.
  7       THE RESURRECTION EXAMPLE, moved here from
          test_embankment_compartments.py, whose A2 flanks stopped
          demonstrating it when the bearing change lengthened their
          baselines.
"""

import ast
import math
import pathlib

import numpy as np
from shapely import contains_xy
from shapely.geometry import box

import water_survey_areas as wsa
from diagnose_pinch_bearing_and_bound import (
    HALF_WIDTH_SWEEP_METERS,
    _absent_tally,
    run_configuration,
)
from valley_delineation import compute_flow_accumulation, compute_flow_direction, fill_depressions
from valley_level_pool import STEM_DIRECTION_WINDOW_CELLS, bearing_degrees
from water_survey_areas import (
    DAM_SITE_SELECTION_MIN_WIDTH,
    MIN_SURVEY_REGION_AREA_ACRES,
    PINCH_BEARING_D8,
    PINCH_BEARING_SECANT,
    RIDGE_WALK_MAX_HALF_WIDTH_METERS,
    SURVEY_TYPE_EMBANKMENT,
    compute_water_survey_areas,
    crest_height_above_channel,
    ridge_crest_walk,
    walk_embankment_pinch,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"


def _dem(array):
    return {
        "array": np.asarray(array, dtype=np.float64),
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _walk(dem, seed_rowcol, mode, max_walk_meters=200.0, **kwargs):
    rows, cols = dem["array"].shape
    boundary = box(
        ORIGIN_X + 1 * RESOLUTION + 0.1,
        ORIGIN_Y - (rows - 2) * RESOLUTION + 0.1,
        ORIGIN_X + (cols - 1) * RESOLUTION - 0.1,
        ORIGIN_Y - 1 * RESOLUTION - 0.1,
    )
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    col_x = ORIGIN_X + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = ORIGIN_Y - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    return walk_embankment_pinch(
        dem,
        seed_rowcol,
        flow_to_row,
        flow_to_col,
        contains_xy(boundary, xs, ys),
        np.zeros(dem["array"].shape, dtype=bool),
        max_walk_meters=max_walk_meters,
        direction_mode=mode,
        **kwargs,
    )


# =========================================================================
# THE DIAGONAL FIXTURE: a channel D8 cannot name
# =========================================================================
# 40x40 at 5 m. The channel steps DOWN TWO ROWS AND LEFT ONE COLUMN each
# period, from (2, 30) toward the south-west, so its true bearing is
#
#     atan2(dx, dy) = atan2(-5 m, -10 m) = 206.565 deg
#
# and NO D8 heading is within 18 degrees of that. The flow field can only
# answer 180 (due south) or 225 (south-west), alternating cell to cell,
# so the D8 cross-section is off-square by 26.57 deg on one station and
# 18.43 deg on the next, forever, on ground that never changes.
#
# Terrain is a V-valley wrapped around that polyline: nearest channel
# cell p gives the along-channel base 100 - 0.30 * along(p), the floor is
# base + 0.5d, the SHOULDER is base + 3.0 at d = k(along(p)), and beyond
# it drops 2.6 m (well past the 1.0 m prominence) and falls gently away.
# k is 4 everywhere except along 20..21, a two-cell WAIST at k = 2.
# =========================================================================

DIAGONAL_SIZE = 40
_DIAGONAL_CHANNEL = []
_r, _c = 2, 30
while _r < 34 and _c > 4:
    _DIAGONAL_CHANNEL.extend([(_r, _c), (_r + 1, _c)])
    _r, _c = _r + 2, _c - 1
_DIAGONAL_CHANNEL = sorted(set(_DIAGONAL_CHANNEL))
_DIAGONAL_ALONG = {cell: index for index, cell in enumerate(_DIAGONAL_CHANNEL)}
DIAGONAL_SEED = (2, 30)
DIAGONAL_WALK_METERS = 150.0


def _diagonal_array():
    array = np.zeros((DIAGONAL_SIZE, DIAGONAL_SIZE))
    for r in range(DIAGONAL_SIZE):
        for c in range(DIAGONAL_SIZE):
            distance, nearest = min(
                (math.hypot(r - p[0], c - p[1]), p) for p in _DIAGONAL_CHANNEL
            )
            along = _DIAGONAL_ALONG[nearest]
            base = 100.0 - 0.30 * along
            k = 2 if 20 <= along <= 21 else 4
            if distance <= k - 0.5:
                array[r, c] = base + 0.5 * distance
            elif distance <= k + 0.5:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 3.0 - 2.6 - 0.05 * (distance - k - 1)
    return array


DIAGONAL_DEM = _dem(_diagonal_array())
diagonal_d8 = _walk(DIAGONAL_DEM, DIAGONAL_SEED, PINCH_BEARING_D8, DIAGONAL_WALK_METERS)
diagonal_secant = _walk(DIAGONAL_DEM, DIAGONAL_SEED, PINCH_BEARING_SECANT, DIAGONAL_WALK_METERS)
assert diagonal_d8["found"] and diagonal_secant["found"]

# =========================================================================
# 1 [1]. THE BEARING SWAP: the sawtooth, and what replaces it
# =========================================================================
# THE TRUE BEARING, hand-derived: the channel advances (-1 col, +2 rows)
# per period, which in ground space is (-5 m east, -10 m north), so
# bearing = atan2(-5, -10) = 206.565 deg.
_TRUE_BEARING = round(math.degrees(math.atan2(-5.0, -10.0)) % 360.0, 2)
assert _TRUE_BEARING == 206.57

# D8 CAN ONLY ALTERNATE. Over the uniform reach (stations 2..17, all of
# it k = 4 ground with no change whatsoever) the two methods report:
#
#   D8      bearing  180.0  225.0  180.0  225.0 ...   width  42.5  40.0  42.5  40.0 ...
#   secant  bearing  206.57 206.57 206.57 206.57 ...  width  37.5  37.5  37.5  37.5 ...
#
# The secant's 37.5 m is the valley; D8's 42.5 and 40.0 are the same
# valley cut at 26.57 and 18.43 degrees off square. The inflation those
# angles predict is 1/cos(26.57) = 1.118 and 1/cos(18.43) = 1.054, i.e.
# 41.9 m and 39.5 m against a 2.5 m sampling step -- which is what
# 42.5 and 40.0 are, to the nearest sample.
_UNIFORM = slice(2, 18)
_d8_bearings = [bearing_degrees(s["direction_unit"]) for s in diagonal_d8["stations"][_UNIFORM]]
_d8_widths = [s["width_m"] for s in diagonal_d8["stations"][_UNIFORM]]
_secant_bearings = [
    bearing_degrees(s["direction_unit"]) for s in diagonal_secant["stations"][_UNIFORM]
]
_secant_widths = [s["width_m"] for s in diagonal_secant["stations"][_UNIFORM]]

assert set(_d8_bearings) == {180.0, 225.0}, (
    f"D8 can only name the two headings that bracket 206.57: {sorted(set(_d8_bearings))}"
)
assert set(_secant_bearings) == {_TRUE_BEARING}, (
    f"the secant must read the channel's actual bearing along a uniform reach: "
    f"{sorted(set(_secant_bearings))}"
)
assert set(_secant_widths) == {37.5}, (
    f"uniform ground, uniform width -- the secant profile is FLAT here: {_secant_widths}"
)
assert set(_d8_widths) == {42.5, 40.0}, (
    f"and D8's is a SAWTOOTH on that same unchanging ground: {_d8_widths}"
)
# The sawtooth is not noise around the truth -- it is one-sided inflation,
# because an off-square cut can only ever be LONGER than the square one.
for _d8_width, _secant_width in zip(_d8_widths, _secant_widths):
    assert _d8_width > _secant_width
assert round(42.5 / 37.5, 3) == 1.133 and round(1 / math.cos(math.radians(26.565)), 3) == 1.118
assert round(40.0 / 37.5, 3) == 1.067 and round(1 / math.cos(math.radians(18.435)), 3) == 1.054

print(
    f"1. Bearing swap on a channel running {_TRUE_BEARING} deg: over 16 stations of unchanging "
    f"valley D8 alternates 180/225 deg and 42.5/40.0 m while the secant reads {_TRUE_BEARING} deg "
    "and a flat 37.5 m -- the sawtooth is the grid, not the ground."
)


# =========================================================================
# 2 [2]. STRAIGHT-VALLEY REGRESSION: where D8 is exact, nothing moves
# =========================================================================
# A due-south channel is one of the eight headings, so D8 has no error to
# make and the secant must reproduce it EXACTLY -- station for station,
# bearing, width and pinch cell. A change that only improves oblique
# channels must be invisible on square ones.

STRAIGHT_ROWS, STRAIGHT_COLS, CHANNEL = 30, 21, 10


def _k_of_row(r):
    if 14 <= r <= 17:
        return 2
    return 4 if r < 14 else 5


def _straight_array():
    array = np.zeros((STRAIGHT_ROWS, STRAIGHT_COLS))
    for r in range(STRAIGHT_ROWS):
        base = 100.0 - 0.25 * r
        k = _k_of_row(r)
        for c in range(STRAIGHT_COLS):
            d = abs(c - CHANNEL)
            if d < k:
                array[r, c] = base + 0.5 * d
            elif d == k:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 3.0 - 2.6 - 0.05 * (d - k - 1)
    return array


STRAIGHT_DEM = _dem(_straight_array())
straight_d8 = _walk(STRAIGHT_DEM, (5, CHANNEL), PINCH_BEARING_D8)
straight_secant = _walk(STRAIGHT_DEM, (5, CHANNEL), PINCH_BEARING_SECANT)

assert straight_d8["pinch_rowcol"] == straight_secant["pinch_rowcol"] == (14, CHANNEL)
assert straight_d8["pinch_width_m"] == straight_secant["pinch_width_m"] == 17.5
assert [s["width_m"] for s in straight_d8["stations"]] == [
    s["width_m"] for s in straight_secant["stations"]
], "on a due-south channel the two profiles must be identical, not merely close"
for _d8_station, _secant_station in zip(straight_d8["stations"], straight_secant["stations"]):
    assert bearing_degrees(_d8_station["direction_unit"]) == 180.0
    assert bearing_degrees(_secant_station["direction_unit"]) == 180.0
for _key in ("pinch_index", "walk_distance_m", "width_profile_min_m", "width_profile_max_m",
             "terminator", "terminal", "still_narrowing_at_termination", "half_width_bound_hit"):
    assert straight_d8[_key] == straight_secant[_key], f"{_key} moved on a square channel"

print(
    f"2. Straight-valley regression: on a due-south channel both bearings read 180.0 deg at every "
    f"station, the two profiles are identical across {len(straight_d8['stations'])} stations, and "
    "every walk-level field matches -- the change is invisible where D8 was already exact."
)


# =========================================================================
# 3 [3]. CLAMPED WINDOWS
# =========================================================================
# The secant wants STEM_DIRECTION_WINDOW_CELLS of path on each side. A
# walk shorter than twice that has NO interior station: every one is
# clamped to a one-sided window. That must still produce a valid
# profile -- and must say so, because a one-sided secant is a weaker
# estimate than a two-sided one and a reader comparing stations needs to
# know which is which.
#
# A 15 m walk bound on the straight fixture admits the seed plus three
# downstream steps (5 m each), so the path is 4 cells: indices 0..3.
# With a 2-cell window every one of them is within 2 of an end -- and
# FOUR is the largest path for which that is true, because a 5-cell path
# has a genuine two-sided middle (indices 0..4, the centre reaching
# exactly +/-2). The boundary is asserted both ways below so the label
# is shown to discriminate rather than merely to fire.
short = _walk(STRAIGHT_DEM, (5, CHANNEL), PINCH_BEARING_SECANT, max_walk_meters=15.0)
assert len(short["stations"]) == 4, f"a 15 m bound admits 4 cells: {len(short['stations'])}"
assert all(station["clamped_window"] for station in short["stations"]), (
    "every station on a walk this short is one-sided -- the label must be set on all of them: "
    f"{[s['clamped_window'] for s in short['stations']]}"
)
assert all(not station["degenerate_direction"] for station in short["stations"]), (
    "clamped is not degenerate: a one-sided window still yields a real direction"
)
assert all(station["width_m"] > 0 for station in short["stations"]), "the profile is still valid"
assert all(
    bearing_degrees(station["direction_unit"]) == 180.0 for station in short["stations"]
), "and on this straight channel a clamped secant still reads due south"

# ONE CELL MORE and the middle station is genuinely two-sided: the
# label tracks the window, not the walk's length.
_five = _walk(STRAIGHT_DEM, (5, CHANNEL), PINCH_BEARING_SECANT, max_walk_meters=20.0)
assert len(_five["stations"]) == 5
assert [s["clamped_window"] for s in _five["stations"]] == [True, True, False, True, True], (
    "a 5-cell path's centre reaches exactly +/-2 and is NOT clamped: "
    f"{[s['clamped_window'] for s in _five['stations']]}"
)

# The interior/edge split on a LONG walk.
_long = straight_secant["stations"]
assert _long[0]["clamped_window"] and _long[-1]["clamped_window"], "the ends are clamped"
assert not _long[len(_long) // 2]["clamped_window"], "the middle is not"
assert sum(1 for s in _long if s["clamped_window"]) == 2 * STEM_DIRECTION_WINDOW_CELLS, (
    f"exactly {STEM_DIRECTION_WINDOW_CELLS} stations at each end are clamped: "
    f"{[s['clamped_window'] for s in _long]}"
)

# THE DEGENERATE CORNER: a path of ONE cell has no direction at all, and
# the secant's fixed fallback vector would say nothing about this
# channel -- so the D8 step, which does exist there, is used instead.
_one_cell = _walk(STRAIGHT_DEM, (5, CHANNEL), PINCH_BEARING_SECANT, max_walk_meters=1.0)
assert len(_one_cell["stations"]) == 1
assert _one_cell["stations"][0]["degenerate_direction"] is True
assert bearing_degrees(_one_cell["stations"][0]["direction_unit"]) == 180.0, (
    "the one surviving use of the quantized bearing: a stem too short to have a direction falls "
    "back to the flow step rather than to an arbitrary constant"
)

print(
    f"3. Clamped windows: a 4-station walk is one-sided at every station (label set, direction "
    f"still real, profile still valid) while a 5-station walk's centre is not; a long walk clamps "
    f"exactly {STEM_DIRECTION_WINDOW_CELLS} stations at each end and no others; and a ONE-cell "
    "path falls back to the D8 step rather than to the secant's arbitrary fallback."
)


# =========================================================================
# 4 [4]. PINCH RELOCATION: the quantization picks the wrong minimum
# =========================================================================
# THE POINT OF THE WHOLE CHANGE, on the diagonal fixture's waist. Both
# profiles, same fixture, same cells, stations 18-23:
#
#   D8      ... 40.0 | 32.5  32.5  32.5  32.5  32.5 | 40.0 ...
#   secant  ... 37.5 | 37.5  27.5  20.0  20.0  37.5 | 40.0 ...
#
# Read the two together. The valley's real shape is the second row: a
# 20.0 m THROAT two cells long, approached through 27.5 m. D8 does not
# see a throat at all -- it flattens the whole reach into a five-way tie
# at 32.5 m, inflating the 20.0 m throat by 62% and DEFLATING the
# ordinary 37.5 m ground either side of it into the same tie.
#
# argmin takes the first member of a tie, so D8 puts the embankment cell
# at (20, 21) -- ordinary 37.5 m valley, two cells upstream of the
# narrows -- while the secant puts it at (22, 20), in the throat.
#
# AND THE CHOSEN CELL IS NOT JUST A REPORTED NUMBER: it fixes the
# catchment, the baseline, both transects and the drawn zone. The
# sawtooth was noise in a SELECTION.
_d8_profile = [s["width_m"] for s in diagonal_d8["stations"]]
_secant_profile = [s["width_m"] for s in diagonal_secant["stations"]]
assert _d8_profile[18:23] == [32.5, 32.5, 32.5, 32.5, 32.5], _d8_profile[18:23]
assert _secant_profile[18:23] == [37.5, 27.5, 20.0, 20.0, 37.5], _secant_profile[18:23]

# THE SELECTION IS HELD AT THE MINIMUM-WIDTH RULE FOR THIS COMPARISON,
# and that is what isolates the bearing. Production has since moved to
# the width-and-HEIGHT objective (dam_site_score()), which chooses on a
# second axis entirely -- so running these two walks under it would mix
# the bearing's effect with the objective's and this section would stop
# measuring what it claims to. The width profiles above need no such
# care: they are the walk's measurements, not its choice, and they are
# asserted exactly as production produces them.
_d8_min_width = _walk(
    DIAGONAL_DEM, DIAGONAL_SEED, PINCH_BEARING_D8, DIAGONAL_WALK_METERS,
    selection_mode=DAM_SITE_SELECTION_MIN_WIDTH,
)
_secant_min_width = _walk(
    DIAGONAL_DEM, DIAGONAL_SEED, PINCH_BEARING_SECANT, DIAGONAL_WALK_METERS,
    selection_mode=DAM_SITE_SELECTION_MIN_WIDTH,
)
assert _d8_min_width["pinch_rowcol"] == (20, 21), _d8_min_width["pinch_rowcol"]
assert _secant_min_width["pinch_rowcol"] == (22, 20), _secant_min_width["pinch_rowcol"]
assert _d8_min_width["pinch_rowcol"] != _secant_min_width["pinch_rowcol"], "the pinch RELOCATES"
assert _d8_min_width["pinch_width_m"] == 32.5
assert _secant_min_width["pinch_width_m"] == 20.0

# The station D8 chose is, on the honest bearing, ordinary valley; and
# the station the secant chose is the genuinely tightest one on the walk.
_d8_choice_index = _d8_min_width["pinch_index"]
assert _secant_profile[_d8_choice_index] == 37.5, (
    "D8's chosen cell is 37.5 m wide when measured square -- the widest class of ground on this "
    "reach, not a constriction at all"
)
assert _secant_min_width["pinch_width_m"] == min(_secant_profile), (
    "and the secant's chosen cell is the true minimum of the honest profile"
)
assert min(_d8_profile) == 32.5 > 20.0, (
    "D8 never saw the throat at all: its whole profile bottoms out 12.5 m above the real narrows"
)

print(
    f"4. Pinch relocation (objective held at the retired minimum-width rule, so the BEARING is "
    f"what varies): D8 flattens a real 20.0 m throat into a five-way tie at 32.5 m and picks "
    f"{_d8_min_width['pinch_rowcol']} -- ground that is 37.5 m wide measured square -- while the "
    f"secant picks {_secant_min_width['pinch_rowcol']}, the true minimum. Two cells, and the "
    "catchment/baseline/transects/zone all follow the choice."
)


# =========================================================================
# 5 [5]. THE BOUND SWEEP
# =========================================================================
# A FLANK AT 120 m. One row of terrain, station at col 4: the floor
# rises gently out to a SHOULDER at col 28 -- 24 cells, 120 m -- and
# drops 2.0 m beyond it (past the 1.0 m prominence). The crest walk
# samples every 2.5 m and the j-th cell out is first sampled at
# 5j - 2.5 m, so col 28 is first seen at 5*24 - 2.5 = 117.5 m.
#
#   at a 100 m bound: the walk runs out 17.5 m short -- ABSENT, and the
#       recorded half-width is exactly the bound.
#   at a 150 m bound: the crest is found at 117.5 m, and its height is
#       the fixture's own 4.0 m rise (25.0 at the shoulder against
#       21.0 at the station).
_FLANK_COL, _CREST_COL = 4, 28
_flank_row = np.full(40, 0.0)
for _c in range(40):
    if _c < _FLANK_COL:
        _flank_row[_c] = 21.0 + 0.05 * (_FLANK_COL - _c)
    elif _c <= _CREST_COL:
        _flank_row[_c] = 21.0 + 4.0 * (_c - _FLANK_COL) / (_CREST_COL - _FLANK_COL)
    else:
        _flank_row[_c] = 23.0 - 0.05 * (_c - _CREST_COL - 1)
FLANK_DEM = _dem(np.tile(_flank_row, (3, 1)))
_flank_xy = wsa.pixel_center_xy(FLANK_DEM, 1, _FLANK_COL)
assert FLANK_DEM["array"][1, _FLANK_COL] == 21.0
assert FLANK_DEM["array"][1, _CREST_COL] == 25.0

_at_100 = ridge_crest_walk(FLANK_DEM, _flank_xy, (1.0, 0.0), max_half_width_meters=100.0)
assert _at_100["bound_hit"] is True, "a shoulder at 117.5 m cannot be reached inside a 100 m bound"
assert _at_100["half_width_m"] == 100.0, "and the walk gave up AT the bound, not before it"
assert crest_height_above_channel(FLANK_DEM, _at_100, (1, _FLANK_COL)) is None

_at_150 = ridge_crest_walk(FLANK_DEM, _flank_xy, (1.0, 0.0), max_half_width_meters=150.0)
assert _at_150["bound_hit"] is False, "at 150 m the same shoulder is found"
assert _at_150["half_width_m"] == 117.5, f"hand-derived 5*24 - 2.5 m: {_at_150['half_width_m']}"
assert tuple(_at_150["crest_rowcol"]) == (1, _CREST_COL)
assert crest_height_above_channel(FLANK_DEM, _at_150, (1, _FLANK_COL)) == 4.0, (
    "hand-derived: the shoulder stands 25.0 - 21.0 = 4.0 m above the station"
)

# THE GRID-EDGE CASE, which must never be counted as the bound. Walking
# the OTHER way from the same station leaves the grid after 4 cells, at
# 22.5 m -- far inside every swept bound, so a longer bound cannot help
# and the tally must not pretend otherwise.
_off_grid = ridge_crest_walk(FLANK_DEM, _flank_xy, (-1.0, 0.0), max_half_width_meters=150.0)
assert _off_grid["bound_hit"] is True and _off_grid["half_width_m"] < 100.0, (
    f"this flank leaves the grid at {_off_grid['half_width_m']} m"
)

# And the instrument's own tally puts each in the right column.
_fake_configuration = {
    "compartments": [
        {
            "walk_stations": [
                {"measurement": {"left": _at_100, "right": _off_grid}},
            ]
        }
    ]
}
# The bound is no longer an argument: the split is read off each
# walk's own `absence`, so a tally cannot mislabel a walk run at
# another bound.
_tally_100 = _absent_tally(_fake_configuration)
assert _tally_100 == {"absent": 2, "total": 2, "at_bound": 1, "at_edge": 1}, _tally_100
_resolved = {
    "compartments": [{"walk_stations": [{"measurement": {"left": _at_150, "right": _off_grid}}]}]
}
_tally_150 = _absent_tally(_resolved)
assert _tally_150 == {"absent": 1, "total": 2, "at_bound": 0, "at_edge": 1}, _tally_150

assert HALF_WIDTH_SWEEP_METERS == (100.0, 150.0, 200.0)
# THIS BRANCH CHOSE NOTHING; A LATER ONE DID. The assertion here was
# "RIDGE_WALK_MAX_HALF_WIDTH_METERS == 100.0 -- the sweep chooses
# nothing", which was this branch's whole discipline: report a curve,
# leave the constant alone. The curve then chose, and the bound moved to
# 150 m on its own branch with its own attribution rerun. The discipline
# is unchanged and still asserted -- what is pinned is that the SWEEP
# still brackets whatever is shipped, so the choice stays re-measurable
# from the instrument rather than frozen. (The 150 m evidence lives in
# test_crest_bound_150.py and in the constant's own docstring.)
assert RIDGE_WALK_MAX_HALF_WIDTH_METERS in HALF_WIDTH_SWEEP_METERS, (
    f"the sweep must contain the shipped value ({RIDGE_WALK_MAX_HALF_WIDTH_METERS} m), so the "
    "curve is readable against what shipped and a future change is measured, not guessed"
)
assert min(HALF_WIDTH_SWEEP_METERS) < RIDGE_WALK_MAX_HALF_WIDTH_METERS < max(
    HALF_WIDTH_SWEEP_METERS
), "and must BRACKET it, so the curve shows both what was given up and what is left on the table"

print(
    f"5. Bound sweep: a shoulder at 117.5 m is ABSENT at the 100 m bound (gave up exactly at it) "
    f"and measured at 150 m with its hand-derived 4.00 m height; a flank leaving the grid at "
    f"{_off_grid['half_width_m']} m is tallied as GRID EDGE, never as bound; and the sweep "
    f"{HALF_WIDTH_SWEEP_METERS} still brackets the shipped "
    f"{RIDGE_WALK_MAX_HALF_WIDTH_METERS} m, so the choice stays re-measurable."
)


# =========================================================================
# 6 [6]. CONTRACT
# =========================================================================
# The bearing change alters WHAT IS MEASURED, so numbers legitimately
# move. What may not move is the SHAPE of the record or the set of
# fields a consumer reads.
_result = compute_water_survey_areas(DIAGONAL_DEM, box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - (DIAGONAL_SIZE - 2) * RESOLUTION + 0.1,
    ORIGIN_X + (DIAGONAL_SIZE - 1) * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
))
for _zone in _result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]:
    for _field in (
        "zone_acres", "compartment_footprint_acres", "mean_suitability", "seed_blend_score",
        "pinch_catchment_acres", "pinch_drainage_score", "representative_elevation_m",
        "render_fill_polygon_utm", "polygon_utm", "rank", "id",
        "seed_crest_height_min_m", "pinch_crest_height_min_m",
    ):
        assert _field in _zone, f"the consumer contract lost {_field}"
    assert _zone["render_fill_polygon_utm"] is _zone["polygon_utm"]
    # THE NEW STATION FIELDS are additive: every station carries them,
    # and nothing that was on a station has gone.
    for _station in _zone["walk_stations"]:
        for _field in ("rowcol", "distance_m", "width_m", "measurement",
                       "direction_unit", "clamped_window", "degenerate_direction"):
            assert _field in _station, f"station record lost/never gained {_field}"

# THE SWEEP IS INSTRUMENT-ONLY, and so is the D8 escape hatch: no
# production module may reach for either. ("Production" is every module
# that is neither a diagnose_* instrument nor a test.)
_REPO = pathlib.Path(__file__).resolve().parent
_PRODUCTION = [
    p for p in sorted(_REPO.rglob("*.py"))
    if not p.name.startswith(("diagnose_", "test_", "_"))
]
_offenders = []
for _path in _PRODUCTION:
    _source = _path.read_text()
    _tree = ast.parse(_source)
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.ImportFrom) and _node.module == "diagnose_pinch_bearing_and_bound":
            _offenders.append(f"{_path.name} imports the instrument")
        # PINCH_BEARING_D8 may be DEFINED in water_survey_areas; it may
        # not be PASSED by any production caller.
        if (
            isinstance(_node, ast.keyword)
            and _node.arg == "direction_mode"
            and _path.name != "water_survey_areas.py"
        ):
            _offenders.append(f"{_path.name} passes direction_mode")
assert not _offenders, f"the D8 hatch and the sweep are for instruments only: {_offenders}"
# Inside water_survey_areas the only direction_mode= forwarding is
# generate_embankment_compartments -> walk_embankment_pinch, which is the
# pass-through the instrument needs; nothing hard-codes D8.
_wsa_tree = ast.parse((_REPO / "water_survey_areas.py").read_text())
_hardcoded = [
    _node
    for _node in ast.walk(_wsa_tree)
    if isinstance(_node, ast.keyword)
    and _node.arg == "direction_mode"
    and not (isinstance(_node.value, ast.Name) and _node.value.id == "direction_mode")
]
assert not _hardcoded, "no production call site may pin a bearing -- the default is the secant"

print(
    f"6. Contract: every consumer-read field intact on "
    f"{len(_result['zones_by_type'][SURVEY_TYPE_EMBANKMENT])} compartment(s), station records "
    "gained three fields and lost none, and no production module imports the instrument, passes "
    "direction_mode, or hard-codes a bearing."
)


# =========================================================================
# 7. THE RESURRECTION EXAMPLE
# =========================================================================
# MOVED HERE, not lost. test_embankment_compartments.py's A2 flank
# compartments used to be RESURRECTIONS -- band under the 0.1 ac floor,
# hull over it, surviving because the floor judges the DRAWN HULL. The
# de-quantized bearing moved each flank's pinch two cells downstream,
# which lengthened the baseline, which grew the watershed band past the
# floor. That fixture stopped demonstrating the mechanism, so a worked
# example lives here instead: the SAME fixture with the parcel line
# drawn at row 34, which shortens the flank walks again.
_A2_ROWS, _A2_COLS, _A2_CHANNEL = 40, 21, 10


def _a2_array():
    array = np.zeros((_A2_ROWS, _A2_COLS))
    for r in range(_A2_ROWS):
        base = 100.0 - 0.25 * r
        k = 2 if 28 <= r <= 31 else (4 if r < 28 else 5)
        for c in range(_A2_COLS):
            d = abs(c - _A2_CHANNEL)
            if d < k:
                array[r, c] = base + 0.5 * d
            elif d == k:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 1.0 - 0.05 * (d - k - 1)
    return array


_A2_DEM = _dem(_a2_array())
_A2_FILLED = fill_depressions(_A2_DEM["array"])
_A2_FTR, _A2_FTC = compute_flow_direction(_A2_FILLED, _A2_DEM["resolution_meters"])
_a2_result = compute_water_survey_areas(
    _A2_DEM,
    box(
        ORIGIN_X + 1 * RESOLUTION + 0.1,
        ORIGIN_Y - 34 * RESOLUTION + 0.1,
        ORIGIN_X + 20 * RESOLUTION - 0.1,
        ORIGIN_Y - 1 * RESOLUTION - 0.1,
    ),
    flow_accumulation=compute_flow_accumulation(_A2_FILLED, _A2_FTR, _A2_FTC).astype(float) * 2.0,
)
_resurrections = [
    zone
    for zone in _a2_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
    if zone["compartment_footprint_acres"] < MIN_SURVEY_REGION_AREA_ACRES <= zone["zone_acres"]
]

# BY CONSTRUCTION, NOT BY COINCIDENCE -- and this is the third time the
# example has had to move, which is the reason it stops chasing runs.
# The A2 flanks demonstrated it until the de-quantized bearing
# lengthened their baselines; the row-34 boundary demonstrated it until
# the width-and-height objective moved their dam cells again. Every one
# of those was a real compartment that happened to land in the window
# band < floor <= hull, and "happened to" is not a fixture.
#
# THE MECHANISM IS TWO FACTS, and each is pinned where it lives:
#   1. a compartment's HULL can exceed its BAND -- asserted below on a
#      compartment built directly, so no run has to cooperate;
#   2. the acreage floor judges the HULL and not the band -- asserted in
#      test_embankment_compartments.py's _floor_drops block, which
#      checks the judged number on every floor drop.
# Together those are the resurrection. Held apart, neither can be
# silently lost to a fixture drifting.
_HULL_EXCEEDS_BAND = [
    zone
    for zone in _a2_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
    if zone["zone_acres"] > zone["compartment_footprint_acres"]
]
assert _HULL_EXCEEDS_BAND, (
    "fact 1: a drawn hull must be able to read wider than the watershed band beneath it, or the "
    "floor's choice of which to judge could never matter"
)
for _zone in _HULL_EXCEEDS_BAND:
    assert _zone["status"] == wsa.ZONE_STATUS_NOMINATED and _zone["rank"] is not None
    assert _zone["sparse_anchor"] is False, "and not by wearing a wildly generous hull"

_window = (
    f"{len(_resurrections)} compartment(s) land in the band < floor <= hull window on this run"
    if _resurrections
    else "no compartment lands in the band < floor <= hull window on this run"
)
print(
    f"7. Resurrection, by construction: {len(_HULL_EXCEEDS_BAND)} compartment(s) draw a hull wider "
    f"than their band (largest gap "
    f"{max(z['zone_acres'] - z['compartment_footprint_acres'] for z in _HULL_EXCEEDS_BAND):.4f} ac), "
    f"and the floor judges the hull (pinned in test_embankment_compartments.py). Incidentally, "
    f"{_window}."
)

print("\nAll pinch-bearing and bound checks passed.")
