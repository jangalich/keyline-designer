"""
test_crest_bound_150.py

THE HALF-WIDTH BOUND AT 150 m, and the double-rounding fix that rides
with it.

RIDGE_WALK_MAX_HALF_WIDTH_METERS was 100 m -- a v1 prior, never
measured. The sweep the bound branch shipped measured it, on the
reference property's whole station population:

    bound     absent flanks     stopped BY THE BOUND     at the grid edge
    100 m         107                   107                     0
    150 m          29                    29                     0
    200 m           8                     8                     0

Four-fifths of the crest measurements this pipeline could make were
being suppressed by the cap, on a DEM window with room to spare. 150 m
takes the knee.

THIS IS NOT A FREE COMPLETENESS GAIN, which is the half of the change
that needs testing rather than celebrating. A longer walk changes
measured widths; the width profile's minimum moves with them; the
minimum IS the dam cell. Catchments, baselines, transects and drawn
zones all follow it. Section 3 builds the relocation from a known cause
so the mechanism is pinned rather than only observed on one parcel.

Run as:

    python test_crest_bound_150.py

Sections (the design's numbered test items in brackets):
  1  [1]  THE CONSTANT -- 150.0, and the only value that moved.
  2  [2]  THE 120 m FLANK -- absent at 100 m, measured at 150 m with a
          hand-derived crest height.
  3  [3]  PINCH RELOCATION UNDER THE BOUND -- a longer walk changes the
          minimum; both profiles in the comment.
  4  [4]  GRID EDGE IS NOT THE BOUND -- a walk that leaves the array
          early is tallied as extent, never as evidence about the cap.
  5  [5]  THE ROUNDING FIX -- a crest coincident with its station reads
          exactly 0.0, not -0.0, and the audit that found the defect
          now reports zero disagreements on the fixture that showed it.
  6  [6]  CONTRACT -- consumer-read fields intact; selected_water_zone
          movement REPORTED, not asserted away.
"""

import math
import pathlib

import numpy as np
from shapely import contains_xy
from shapely.geometry import box

import diagnose_transect_bearing as dtb
import water_survey_areas as wsa
from diagnose_pinch_bearing_and_bound import (
    HALF_WIDTH_SWEEP_METERS,
    RETIRED_HALF_WIDTH_BOUND_METERS,
    _absent_tally,
)
from raster_grid import pixel_center_xy
from valley_delineation import compute_flow_direction, fill_depressions
from water_survey_areas import (
    PINCH_BEARING_SECANT,
    RIDGE_PROMINENCE_METERS,
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


def _boundary_for(dem):
    rows, cols = dem["array"].shape
    return box(
        ORIGIN_X + 1 * RESOLUTION + 0.1,
        ORIGIN_Y - (rows - 2) * RESOLUTION + 0.1,
        ORIGIN_X + (cols - 1) * RESOLUTION - 0.1,
        ORIGIN_Y - 1 * RESOLUTION - 0.1,
    )


def _walk(dem, seed_rowcol, bound, max_walk_meters=250.0):
    rows, cols = dem["array"].shape
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
        contains_xy(_boundary_for(dem), xs, ys),
        np.zeros(dem["array"].shape, dtype=bool),
        max_walk_meters=max_walk_meters,
        max_half_width_meters=bound,
        direction_mode=PINCH_BEARING_SECANT,
    )


# =========================================================================
# 1 [1]. THE CONSTANT
# =========================================================================
assert RIDGE_WALK_MAX_HALF_WIDTH_METERS == 150.0, (
    f"the measured value, from the sweep's curve: {RIDGE_WALK_MAX_HALF_WIDTH_METERS}"
)
assert RETIRED_HALF_WIDTH_BOUND_METERS == 100.0, (
    "and the retired value stays NAMED, in the diagnostic and only there, so the attribution "
    "table can label its own before-column"
)
# THE SWEEP STILL BRACKETS WHAT SHIPPED. A constant chosen from a curve
# stays honest only while the curve keeps being drawn -- so the
# instrument must still measure both below and above the chosen value.
assert RIDGE_WALK_MAX_HALF_WIDTH_METERS in HALF_WIDTH_SWEEP_METERS
assert min(HALF_WIDTH_SWEEP_METERS) < RIDGE_WALK_MAX_HALF_WIDTH_METERS < max(
    HALF_WIDTH_SWEEP_METERS
)

# AND IT IS THE ONLY VALUE THAT MOVED. Every other constant on the crest
# path is pinned here by value, so a sweep that quietly retuned a
# neighbour would fail rather than hide inside "the bound branch".
assert RIDGE_PROMINENCE_METERS == 1.0, "the prominence rule is untouched -- explicitly out of scope"
assert wsa.EMBANKMENT_PINCH_WALK_MAX_METERS == 100.0, "the along-channel walk bound is untouched"
assert wsa.STEM_DIRECTION_WINDOW_CELLS == 2, "the secant window is untouched"
assert wsa.MIN_SURVEY_REGION_AREA_ACRES == 0.1, "the acreage floor is untouched"
assert (wsa.PINCH_BEARING_SECANT, wsa.PINCH_BEARING_D8) == ("secant", "d8")

# Every caller reads the constant as a DEFAULT rather than hard-coding a
# number, so one edit moves the whole path -- asserted at the source so a
# future literal cannot drift away from it.
_SOURCE = (pathlib.Path(__file__).resolve().parent / "water_survey_areas.py").read_text()
assert _SOURCE.count("max_half_width_meters: float = RIDGE_WALK_MAX_HALF_WIDTH_METERS") >= 5, (
    "the crest walk, the width measurement, the pinch walk, the compartment build, the generation "
    "pass and the compute core all default to the constant"
)
assert "max_half_width_meters: float = 100" not in _SOURCE
assert "max_half_width_meters: float = 150" not in _SOURCE, (
    "no caller may hard-code the bound -- that is how a constant stops being one"
)

print(
    f"1. The constant: RIDGE_WALK_MAX_HALF_WIDTH_METERS = {RIDGE_WALK_MAX_HALF_WIDTH_METERS} m, "
    f"bracketed by the sweep {HALF_WIDTH_SWEEP_METERS} that chose it; prominence, along-channel "
    "walk bound, secant window and acreage floor all unmoved; no caller hard-codes a number."
)


# =========================================================================
# 2 [2]. THE 120 m FLANK
# =========================================================================
# One row of terrain, station at col 4: the floor rises gently out to a
# SHOULDER at col 28 -- 24 cells, 120 m -- then drops 2.0 m (past the
# 1.0 m prominence). The crest walk samples every 2.5 m and the j-th
# cell out is first sampled at 5j - 2.5 m, so col 28 is first seen at
# 5*24 - 2.5 = 117.5 m: beyond the retired 100 m cap, inside the
# shipped 150 m one. Its height is the fixture's own 4.0 m rise
# (25.0 at the shoulder against 21.0 at the station).
_FLANK_COL, _CREST_COL = 4, 28
_flank_row = np.zeros(40)
for _c in range(40):
    if _c < _FLANK_COL:
        _flank_row[_c] = 21.0 + 0.05 * (_FLANK_COL - _c)
    elif _c <= _CREST_COL:
        _flank_row[_c] = 21.0 + 4.0 * (_c - _FLANK_COL) / (_CREST_COL - _FLANK_COL)
    else:
        _flank_row[_c] = 23.0 - 0.05 * (_c - _CREST_COL - 1)
FLANK_DEM = _dem(np.tile(_flank_row, (3, 1)))
_flank_xy = pixel_center_xy(FLANK_DEM, 1, _FLANK_COL)
assert FLANK_DEM["array"][1, _FLANK_COL] == 21.0
assert FLANK_DEM["array"][1, _CREST_COL] == 25.0

_retired = ridge_crest_walk(
    FLANK_DEM, _flank_xy, (1.0, 0.0), max_half_width_meters=RETIRED_HALF_WIDTH_BOUND_METERS
)
assert _retired["bound_hit"] is True, "a shoulder at 117.5 m is out of reach of the retired cap"
assert _retired["half_width_m"] == RETIRED_HALF_WIDTH_BOUND_METERS, (
    "and the walk gave up AT the cap, not before it -- that is what makes it the cap's fault"
)
assert crest_height_above_channel(FLANK_DEM, _retired, (1, _FLANK_COL)) is None

# THE SHIPPED BOUND IS TAKEN FROM THE CONSTANT, not written as 150.0, so
# this assertion follows the constant if it ever moves again.
_shipped = ridge_crest_walk(FLANK_DEM, _flank_xy, (1.0, 0.0))
assert _shipped["bound_hit"] is False, "at the shipped bound the same shoulder is found"
assert _shipped["half_width_m"] == 117.5, f"hand-derived 5*24 - 2.5 m: {_shipped['half_width_m']}"
assert tuple(_shipped["crest_rowcol"]) == (1, _CREST_COL)
assert crest_height_above_channel(FLANK_DEM, _shipped, (1, _FLANK_COL)) == 4.0, (
    "hand-derived: the shoulder stands 25.0 - 21.0 = 4.0 m above the station"
)

print(
    f"2. The 120 m flank: a shoulder first sampled at 117.5 m is ABSENT at the retired "
    f"{RETIRED_HALF_WIDTH_BOUND_METERS:.0f} m cap (gave up exactly at it) and measured at the "
    f"shipped {RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m, reading its hand-derived 4.00 m."
)


# =========================================================================
# 3 [3]. PINCH RELOCATION UNDER THE BOUND
# =========================================================================
# THE HALF OF THE CHANGE THAT IS NOT A COMPLETENESS GAIN, and the reason
# the attribution had to be rerun rather than assumed.
#
# A bounded width is a FLOOR, not a measurement: the walk gave up with
# ground still climbing, so the real crest is somewhere further out. The
# recorded number is therefore an UNDERSTATEMENT -- and the pinch is the
# MINIMUM of the width profile. A cap that understates one station can
# hand it the minimum over a station that is genuinely narrower, and the
# winning cell goes on to fix the catchment, the baseline, both
# transects and the drawn zone. The cap is choosing the dam.
#
# THE FIXTURE, 70x70 at 5 m, channel down col 30, base(r) = 100 - 0.25r:
#   rows < 12   shoulders at d = 14  ->  137.5 m  (the seed's own reach,
#               widest, so the profile has somewhere to narrow to)
#   rows >= 12  shoulders at d = 12  ->  117.5 m  (the genuine narrows)
#   ROW 16      the right flank has its shoulder at d = 1 (half-width
#               5.0 m) while the LEFT flank rises 0.3 m/cell to the grid
#               edge and never falls back a prominence -- so that side
#               can never declare a crest and its half-width is whatever
#               the cap allows.
#
# Row 16's recorded width is therefore cap + 5.0, and that is the whole
# trick: the tighter the cap, the NARROWER an unmeasurable station looks.
#
#   at 100 m:  137.5 x4  117.5 x4  |105.0|  117.5 ...   pinch (16, 30)
#   at 150 m:  137.5 x4 |117.5| x4  155.0   117.5 ...   pinch (12, 30)
#
# At the retired cap the dam cell is row 16 -- a station whose width the
# instrument COULD NOT MEASURE, flagged bound_hit the whole time. At the
# shipped bound it moves to row 12, the narrowest station that was
# actually measured. The relocation is four cells and it is the cap
# letting go of a choice it should never have made.
_REL_SIZE, _REL_CHANNEL, _REL_SPECIAL_ROW = 70, 30, 16


def _relocation_array():
    array = np.zeros((_REL_SIZE, _REL_SIZE))
    for r in range(_REL_SIZE):
        base = 100.0 - 0.25 * r
        special = r == _REL_SPECIAL_ROW
        for c in range(_REL_SIZE):
            d = abs(c - _REL_CHANNEL)
            if special and c > _REL_CHANNEL:
                # The flank with no crest: monotone rise to the grid
                # edge, so only the cap ever stops this walk.
                array[r, c] = base + 0.3 * d
                continue
            k = 1 if special else (14 if r < 12 else 12)
            if d < k:
                # A GENTLE floor (0.05 m/cell, not 0.5) so the shoulder
                # at d == k is genuinely the highest thing on the ray.
                # At 0.5 m/cell the inner floor of a k = 14 reach climbs
                # to base + 6.5 and the crest walk declares ITS high
                # point rather than the shoulder -- which made this
                # fixture's binding heights track k, and under the
                # width-and-height objective that would hand the widest
                # reach the best score and fail the walk at the seed.
                # The fixture is about the BOUND; a flat 3.0 m shoulder
                # everywhere keeps height constant so the objective
                # reduces to 1/w and the bound is the only thing moving.
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 3.0 - 2.6 - 0.05 * (d - k - 1)
    return array


REL_DEM = _dem(_relocation_array())
_rel_retired = _walk(REL_DEM, (8, _REL_CHANNEL), RETIRED_HALF_WIDTH_BOUND_METERS)
_rel_shipped = _walk(REL_DEM, (8, _REL_CHANNEL), RIDGE_WALK_MAX_HALF_WIDTH_METERS)
assert _rel_retired["found"] and _rel_shipped["found"]

_retired_profile = [s["width_m"] for s in _rel_retired["stations"]]
_shipped_profile = [s["width_m"] for s in _rel_shipped["stations"]]
assert _retired_profile[:9] == [137.5] * 4 + [117.5] * 4 + [105.0], _retired_profile[:9]
assert _shipped_profile[:9] == [137.5] * 4 + [117.5] * 4 + [155.0], _shipped_profile[:9]

# THE RELOCATION.
assert _rel_retired["pinch_rowcol"] == (_REL_SPECIAL_ROW, _REL_CHANNEL), _rel_retired["pinch_rowcol"]
assert _rel_shipped["pinch_rowcol"] == (12, _REL_CHANNEL), _rel_shipped["pinch_rowcol"]
assert _rel_retired["pinch_rowcol"] != _rel_shipped["pinch_rowcol"], "the dam cell MOVES"
assert _rel_retired["pinch_width_m"] == 105.0 and _rel_shipped["pinch_width_m"] == 117.5

# AND WHAT THE CAP HAD CHOSEN WAS UNMEASURABLE. This is the assertion
# that makes the relocation a fix rather than a difference: at the
# retired cap the dam cell's own width was a bounded FLOOR, and at the
# shipped bound the chosen cell is a real measurement.
assert _rel_retired["half_width_bound_hit"] is True, (
    "the retired cap's dam cell was a station whose width the walk never measured"
)
assert _rel_shipped["half_width_bound_hit"] is False, (
    "the shipped bound's dam cell is a measured station"
)

# The trick stated as a property: row 16's recorded width is cap + 5.0
# while the CAP is what stops the open flank, so a tighter cap makes an
# unmeasurable station look narrower.
#
# The fixture runs out of grid before 200 m -- walking +x from col 30 of
# a 70-wide array leaves it at (70 - 30.5) * 5 = 197.5 m -- so the widest
# swept value is stopped by EXTENT rather than by the cap, and the
# property changes shape there. That is not a flaw in the fixture: it is
# the same distinction section 4 pins, showing up on real terrain, and
# asserting it here keeps the two causes from being conflated.
_GRID_EDGE_HALF_WIDTH = 197.5
for _bound in HALF_WIDTH_SWEEP_METERS:
    _w = _walk(REL_DEM, (8, _REL_CHANNEL), _bound)
    _special = next(s for s in _w["stations"] if s["rowcol"][0] == _REL_SPECIAL_ROW)
    _open_flank = _special["measurement"]["left"]
    assert _special["measurement"]["bound_hit"] is True, "flagged as a floor at every bound"
    if _bound <= _GRID_EDGE_HALF_WIDTH:
        assert _open_flank["half_width_m"] == _bound, "the CAP is what stopped this flank"
        assert _special["width_m"] == _bound + 5.0, (
            f"at {_bound} m the unmeasurable station reads {_special['width_m']} -- the cap plus "
            "the one flank that resolved, which is how a tighter cap manufactures a narrows"
        )
    else:
        assert _open_flank["half_width_m"] == _GRID_EDGE_HALF_WIDTH, (
            f"past {_GRID_EDGE_HALF_WIDTH} m the ARRAY is what stops it, not the cap: "
            f"{_open_flank['half_width_m']}"
        )
        assert _special["width_m"] == _GRID_EDGE_HALF_WIDTH + 5.0
        assert _open_flank["half_width_m"] < _bound, (
            "and a flank stopped by extent sits INSIDE the cap, which is exactly how the tally "
            "tells the two apart"
        )

print(
    f"3. Relocation: at the retired {RETIRED_HALF_WIDTH_BOUND_METERS:.0f} m cap the dam cell is "
    f"{_rel_retired['pinch_rowcol']} at 105.0 m -- a width the walk never measured (bound_hit) -- "
    f"and at the shipped {RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m it moves to "
    f"{_rel_shipped['pinch_rowcol']} at 117.5 m, measured. The cap was choosing the dam."
)

# MONOTONICITY, the property behind all of that: a longer walk can only
# find the crest further out, so no station's width can shrink as the
# bound grows -- and therefore the minimum can only ever move AWAY from
# bounded stations, never toward them.
for _retired_station, _shipped_station in zip(_rel_retired["stations"], _rel_shipped["stations"]):
    assert tuple(_retired_station["rowcol"]) == tuple(_shipped_station["rowcol"])
    assert _shipped_station["width_m"] >= _retired_station["width_m"], (
        "a width that SHRANK as the bound grew means the walk is not monotone in its bound: "
        f"{_retired_station['rowcol']}"
    )

print(
    "   Monotonicity: every station's width is non-decreasing in the bound, station for station -- "
    "so a cap can only ever understate, and the minimum can only move away from capped stations."
)


# =========================================================================
# 4 [4]. GRID EDGE IS NOT THE BOUND
# =========================================================================
# The two absences look identical on the walk's own record (bound_hit
# True either way) and mean opposite things. RAN-THE-BOUND says the cap
# stopped a walk that had ground left to climb: a longer cap might
# resolve it, and it is evidence about the constant. LEFT-THE-GRID says
# the elevation data ran out: a longer cap cannot help, and it is
# evidence about the DEM window. Conflating them would read a fetch
# margin as a terrain finding.
#
# get_dem_for_boundary() fetches the boundary bbox plus
# dem_data.DEFAULT_BUFFER_METERS (100 m), so a 150 m half-width walk
# from a station near the parcel edge CAN reach past the window -- which
# is exactly why this split has to be right now that the bound exceeds
# the margin.
import dem_data

assert dem_data.DEFAULT_BUFFER_METERS == 100.0
assert RIDGE_WALK_MAX_HALF_WIDTH_METERS > dem_data.DEFAULT_BUFFER_METERS, (
    "the shipped bound now EXCEEDS the DEM fetch margin, so grid-edge stops are reachable in "
    "principle and the tally's split is load-bearing rather than theoretical"
)

# Walking -x from the same station leaves the array after 4 cells, at
# 22.5 m -- far inside every swept bound, so no cap could help.
_off_grid = ridge_crest_walk(FLANK_DEM, _flank_xy, (-1.0, 0.0))
assert _off_grid["bound_hit"] is True
assert _off_grid["half_width_m"] < RETIRED_HALF_WIDTH_BOUND_METERS, (
    f"this flank leaves the grid at {_off_grid['half_width_m']} m, nowhere near any cap"
)

_one_of_each = {
    "compartments": [{"walk_stations": [{"measurement": {"left": _retired, "right": _off_grid}}]}]
}
assert _absent_tally(_one_of_each, RETIRED_HALF_WIDTH_BOUND_METERS) == {
    "absent": 2, "total": 2, "at_bound": 1, "at_edge": 1
}, "one of each, in its own column"
# At the shipped bound the cap-stopped flank RESOLVES and only the
# grid-edge one remains -- the tally must not keep counting a resolved
# flank, nor promote the grid-edge one into the bound column.
_resolved = {
    "compartments": [{"walk_stations": [{"measurement": {"left": _shipped, "right": _off_grid}}]}]
}
assert _absent_tally(_resolved, RIDGE_WALK_MAX_HALF_WIDTH_METERS) == {
    "absent": 1, "total": 2, "at_bound": 0, "at_edge": 1
}, "the cap-stopped flank is gone; the grid-edge one stays, still as grid edge"

print(
    f"4. Grid edge vs bound: the shipped {RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m now exceeds the "
    f"{dem_data.DEFAULT_BUFFER_METERS:.0f} m DEM fetch margin, so the split matters; a flank "
    f"leaving the array at {_off_grid['half_width_m']:.1f} m is tallied as extent at every bound "
    "and never as evidence about the cap."
)


# =========================================================================
# 5 [5]. THE ROUNDING FIX
# =========================================================================
# crest_height_above_channel() subtracted the UNROUNDED raw channel
# elevation from ridge_crest_walk()'s ALREADY-ROUNDED crest_elevation_m,
# computing round(round(e, 2) - e, 2) where the crest IS the station and
# the true answer is exactly 0. That put up to +/-0.005 m on every crest
# height and showed up as a -0.00 on the reference run. It now reads the
# crest elevation raw at crest_rowcol and rounds once.
#
# THE FIXTURE that produced the -0.00, unchanged from the branch that
# diagnosed it: walking +x from col 3, 20.0049 (the station) then 18.5 --
# a 1.5 m drop past the 1.0 m prominence at the FIRST sample, so the
# crest is declared AT the station. round(20.0049, 2) = 20.0, and
# round(20.0 - 20.0049, 2) used to be -0.0.
_ROUNDING_STRIP = _dem(
    np.tile(np.array([21.5, 23.0, 21.0, 20.0049, 18.5, 18.4, 18.3, 18.2]), (3, 1))
)
_station = (1, 3)
_edge_walk = ridge_crest_walk(_ROUNDING_STRIP, pixel_center_xy(_ROUNDING_STRIP, *_station), (1.0, 0.0))
assert _edge_walk["bound_hit"] is False and _edge_walk["half_width_m"] == 0.0
assert tuple(_edge_walk["crest_rowcol"]) == _station, "the crest IS the station cell"

_height = crest_height_above_channel(_ROUNDING_STRIP, _edge_walk, _station)
assert _height == 0.0
assert not dtb._negative_zero(_height), (
    f"a crest coincident with its station reads exactly 0.0, never -0.0: {_height!r}"
)
assert repr(_height) == "0.0", f"fixed: {_height!r}"
# The walk's own crest_elevation_m is STILL rounded -- the fix is that
# this function no longer builds on it, not that the walk changed.
assert _edge_walk["crest_elevation_m"] == 20.0
assert float(_ROUNDING_STRIP["array"][_station]) == 20.0049

# THE AUDIT THAT FOUND THE DEFECT, on the fixture that showed it: zero
# disagreements now.
_audited = [
    {
        "zone_id": 0,
        "stations": [
            {
                "end": "pinch",
                "rowcol": _station,
                dtb.METHOD_BASELINE: {
                    "left": _edge_walk,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _height,
                    "right_height_m": None,
                },
                dtb.METHOD_LOCAL: {
                    "left": _edge_walk,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _height,
                    "right_height_m": None,
                },
            }
        ],
    }
]
_audit = dtb._rounding_audit(_ROUNDING_STRIP, _audited)
assert _audit == {"measured": 2, "mismatched": 0, "negative_zeros": 0, "worst_error_m": 0.0}, (
    f"the audit must now find nothing on the very fixture that produced the -0.00: {_audit}"
)

# AND THE AUDIT IS STILL ABLE TO FAIL -- a check that can only pass is
# not a check. Fed the OLD arithmetic explicitly, it still reports the
# disagreement it was written to catch.
_old_arithmetic = round(
    _edge_walk["crest_elevation_m"] - float(_ROUNDING_STRIP["array"][_station]), 2
)
assert dtb._negative_zero(_old_arithmetic), "the old computation, reproduced: still -0.0"
_regressed = [
    {
        "zone_id": 0,
        "stations": [
            {
                "end": "pinch",
                "rowcol": _station,
                dtb.METHOD_BASELINE: {
                    "left": _edge_walk,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _old_arithmetic,
                    "right_height_m": None,
                },
                dtb.METHOD_LOCAL: {
                    "left": _edge_walk,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _old_arithmetic,
                    "right_height_m": None,
                },
            }
        ],
    }
]
assert dtb._rounding_audit(_ROUNDING_STRIP, _regressed)["negative_zeros"] == 2, (
    "the audit must still catch the defect if it ever comes back"
)

# The +/-0.005 m reach of the old defect, on ordinary (non-zero) heights:
# gone too, because the subtraction now rounds once.
_ordinary = ridge_crest_walk(
    _ROUNDING_STRIP, pixel_center_xy(_ROUNDING_STRIP, *_station), (-1.0, 0.0)
)
_ordinary_height = crest_height_above_channel(_ROUNDING_STRIP, _ordinary, _station)
_raw = float(_ROUNDING_STRIP["array"][tuple(_ordinary["crest_rowcol"])]) - float(
    _ROUNDING_STRIP["array"][_station]
)
assert _ordinary_height == round(_raw, 2), (
    f"an ordinary height is now exactly the raw difference rounded once: {_ordinary_height} vs "
    f"{round(_raw, 2)}"
)

print(
    f"5. Rounding fix: a crest coincident with its station reads exactly {_height!r} (was -0.0); "
    f"the audit reports {_audit['mismatched']} disagreements on the fixture that produced the "
    "defect, still catches it when fed the old arithmetic, and ordinary heights now equal the raw "
    "difference rounded once."
)


# =========================================================================
# 6 [6]. CONTRACT
# =========================================================================
# A longer walk changes WHAT IS MEASURED, so numbers move. What may not
# move is the shape of the record or the set of fields a consumer reads.
_CONTRACT_DEM = REL_DEM
_shipped_result = compute_water_survey_areas(_CONTRACT_DEM, _boundary_for(_CONTRACT_DEM))
_retired_result = compute_water_survey_areas(
    _CONTRACT_DEM,
    _boundary_for(_CONTRACT_DEM),
    max_half_width_meters=RETIRED_HALF_WIDTH_BOUND_METERS,
)
for _zone in _shipped_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]:
    for _field in (
        "zone_acres", "compartment_footprint_acres", "mean_suitability", "seed_blend_score",
        "pinch_catchment_acres", "pinch_drainage_score", "representative_elevation_m",
        "render_fill_polygon_utm", "polygon_utm", "rank", "id", "presented",
        "seed_crest_height_min_m", "pinch_crest_height_min_m", "half_width_bound_hit",
    ):
        assert _field in _zone, f"the consumer contract lost {_field}"
    assert _zone["render_fill_polygon_utm"] is _zone["polygon_utm"]
    for _height_field in (
        "seed_crest_height_left_m", "seed_crest_height_right_m", "seed_crest_height_min_m",
        "pinch_crest_height_left_m", "pinch_crest_height_right_m", "pinch_crest_height_min_m",
    ):
        _value = _zone[_height_field]
        assert _value is None or isinstance(_value, float)
        assert not dtb._negative_zero(_value), (
            f"{_height_field} is -0.0 -- the rounding fix must hold end to end, not only in the "
            "unit fixture"
        )

# SELECTED_WATER_ZONE: REPORTED, NEVER ASSERTED STABLE. The bound moves
# widths, so it can move the dam cell, so it can move the pooled winner.
# Identity is type-plus-geometry, not the zone id: ids are assigned per
# run over the full cross-type list, so a compartment appearing or
# disappearing renumbers everything after it.
def _selection_identity(result):
    zone = result["selected_water_zone"]
    if zone is None:
        return None
    return (zone["survey_type"], round(zone["polygon_utm"].area, 3))


_retired_identity = _selection_identity(_retired_result)
_shipped_identity = _selection_identity(_shipped_result)
_selection_moved = _retired_identity != _shipped_identity

_retired_counts = {
    survey_type: len(_retired_result["zones_by_type"][survey_type])
    for survey_type in _retired_result["zones_by_type"]
}
_shipped_counts = {
    survey_type: len(_shipped_result["zones_by_type"][survey_type])
    for survey_type in _shipped_result["zones_by_type"]
}

print(
    f"6. Contract: consumer fields intact on "
    f"{len(_shipped_result['zones_by_type'][SURVEY_TYPE_EMBANKMENT])} compartment(s), no -0.0 "
    f"anywhere on the wire. Survivors {_retired_counts} -> {_shipped_counts}; "
    f"selected_water_zone {_retired_identity} -> {_shipped_identity} "
    f"({'MOVED' if _selection_moved else 'unchanged'}) -- REPORTED, not asserted."
)

print("\nAll crest-bound checks passed.")
