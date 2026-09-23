"""
test_one_sided_binding_shoulder.py

A ONE-SIDED STATION HAS NO BINDING SHOULDER.

lower_crest_height() used to reduce over "the sides that reported": a
station with one flank absent was credited with its MEASURED side's
height as the binding shoulder. That credit carried the station past the
enclosure gate and -- because the dam-site objective is h**2 / w --
actively REWARDED it, since a large h over a very large w still wins.
On the second test parcel a 233 m-wide ribbon ranked #1 that way, and on
the reference run three of eight embankment survivors were selected on a
one-sided station:

    zone  7   pinch L absent (bound@150.0m) / R 2.66 m -> min 2.66 m, w 267.4 m
    zone 10   pinch L absent (bound@150.0m) / R 2.67 m -> min 2.67 m, w 209.9 m
    zone  8   pinch L absent (edge@134.8m)  / R 2.07 m -> min 2.07 m, w 154.8 m

THE RULE IS WRONG FOR A MINIMUM SPECIFICALLY. Excluding an absent side
from a MAXIMUM would be right -- an unmeasured side can only raise a
maximum, and the measured sides already floor it. A MINIMUM asks the
opposite question, "which side gives way first", and an unmeasured side
can only LOWER the answer, without bound: no shoulder within the
half-width means water leaves that way at ANY pool height. "Absent is not
zero" protects a station from LOSING on unmeasured merit; it must not
become "absent is ignored", which lets a station WIN on unmeasured merit.

So lower_crest_height() returns None when EITHER side is absent, and
every consequence falls out of machinery that already existed:
dam_site_score() refuses a station with no binding height, the selection
skips it, and a seed with no scoreable station fails at
REASON_NO_MEASURABLE_SHOULDER.

Run as:

    python test_one_sided_binding_shoulder.py

Sections (the design's numbered test items in brackets):
  1  [1]  THE REDUCTION -- the full truth table, hand-derived, with the
          asymmetry against a maximum stated where the fixture is.
  2  [2]  A ONE-SIDED STATION CANNOT BE SELECTED -- a profile whose best
          h**2/w station is one-sided; the objective takes the next best
          TWO-SIDED station instead.
  3  [3]  THE ZONE-7 SHAPE, REPRODUCED -- one flank at the 150 m bound,
          the other a 2.67 m shoulder, 270 m wide: no longer a
          compartment. Closing that flank -- the only change -- puts the
          station back in front, which is what proves the refusal is
          about the ABSENCE and not about the width.
  4  [4]  AN ALL-ONE-SIDED WALK -- the existing no_measurable_shoulder
          code, no new one, and no fallback to the measured side.
  5  [5]  THE ENCLOSURE GATE'S None BRANCH IS STILL UNREACHABLE,
          asserted structurally, because this change enlarges the
          unscoreable set considerably.
  6  [6]  CONTRACT -- consumer-read fields unchanged, the absence reason
          stays OFF the wire, and selected_water_zone movement REPORTED.
"""

import ast
import inspect

import numpy as np
from shapely import contains_xy
from shapely.geometry import box

import water_survey_areas as wsa
from valley_delineation import compute_flow_direction, fill_depressions
from water_survey_areas import (
    CREST_ABSENCE_AT_BOUND,
    CREST_ABSENCE_AT_GRID_EDGE,
    MIN_BINDING_SHOULDER_METERS,
    REASON_NO_MEASURABLE_SHOULDER,
    RIDGE_WALK_MAX_HALF_WIDTH_METERS,
    SURVEY_TYPE_EMBANKMENT,
    compute_water_survey_areas,
    dam_site_score,
    lower_crest_height,
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


def _boundary(rows, cols):
    return box(
        ORIGIN_X + 1 * RESOLUTION + 0.1,
        ORIGIN_Y - (rows - 2) * RESOLUTION + 0.1,
        ORIGIN_X + (cols - 1) * RESOLUTION - 0.1,
        ORIGIN_Y - 1 * RESOLUTION - 0.1,
    )


def _walk(dem, seed, **kwargs):
    rows, cols = dem["array"].shape
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    col_x = ORIGIN_X + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = ORIGIN_Y - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    return walk_embankment_pinch(
        dem,
        seed,
        flow_to_row,
        flow_to_col,
        contains_xy(_boundary(rows, cols), xs, ys),
        np.zeros(dem["array"].shape, dtype=bool),
        max_walk_meters=200.0,
        **kwargs,
    )


def _station(walk, rowcol):
    return next(s for s in walk["stations"] if tuple(s["rowcol"]) == tuple(rowcol))


def _retired_reduction(station):
    """THE RULE THIS BRANCH REMOVED, reproduced here and NOWHERE else so
    the before-column of every comparison below is the old arithmetic
    rather than a description of it: the minimum over the sides that
    reported, with an absent side skipped."""
    measured = [
        height
        for height in (station["crest_height_left_m"], station["crest_height_right_m"])
        if height is not None
    ]
    if not measured or station["width_m"] <= 0.0:
        return None
    return round(min(measured) ** wsa.DAM_SITE_HEIGHT_EXPONENT / station["width_m"], 5)


# =========================================================================
# 1 [1]. THE REDUCTION: THE FULL TRUTH TABLE
# =========================================================================
# HAND-DERIVED, all four cases, with the numbers chosen so a wrong answer
# is a different number rather than a coincidence:
#
#   left    right   answer   why
#   2.66    4.00    2.66     both measured -> the LOWER side, which is
#                            the one water spills over first
#   4.00    2.66    2.66     order-independent
#   None    2.66    None     the left flank has no shoulder inside the
#                            bound, so water leaves that way at any
#                            height: there is no limiting side to name
#   2.66    None    None     the same, mirrored
#   None    None    None     nothing measured at all
#
# THE ASYMMETRY WITH A MAXIMUM, which is the reason this cannot be
# "made consistent" later. Over a MAXIMUM, skipping an absent side is
# sound: a maximum asks how high the station reaches, an unmeasured side
# can only raise that, and the measured sides already establish a floor
# under the answer. Over a MINIMUM it is unsound for exactly the mirrored
# reason: the question is which side gives way FIRST, and an unmeasured
# side can only lower the answer, with no bound on how far. Same
# arithmetic, opposite question. max(2.66, absent) == 2.66 is a floor on
# the truth; min(2.66, absent) == 2.66 is a CLAIM about ground nobody
# walked.
assert lower_crest_height(2.66, 4.00) == 2.66
assert lower_crest_height(4.00, 2.66) == 2.66, "order-independent"
assert lower_crest_height(None, 2.66) is None, (
    "the left flank is UNBOUNDED, not tall: an absent side cannot be skipped over in a minimum"
)
assert lower_crest_height(2.66, None) is None, "the same, mirrored"
assert lower_crest_height(None, None) is None

# A MEASURED 0.0 STILL COMPETES AND STILL WINS -- the other half of
# "absent is not zero", unchanged by this branch. 0.0 is a crest the walk
# CONFIRMED at the station itself: no shoulder above the channel on that
# side, the hardest binding constraint there is. Against an ABSENT side
# even it yields None, because the question is about a flank nobody
# measured and not about this one.
assert lower_crest_height(0.0, 3.00) == 0.0
assert lower_crest_height(3.00, 0.0) == 0.0
assert lower_crest_height(0.0, None) is None
assert lower_crest_height(None, 0.0) is None

# AND THE SCORE FOLLOWS: no binding height, no score. dam_site_score()
# already refused a None height -- nothing was added for this branch.
assert dam_site_score(None, 267.4) is None
assert dam_site_score(2.66, 267.4) is not None

print(
    "1. The reduction: both sides measured -> the lower (2.66 against 4.00, either order); EITHER "
    "side absent -> None, including beside a measured 0.0; both absent -> None. A measured 0.0 "
    "still wins against a measured side. The asymmetry with a maximum is stated at the fixture: "
    "skipping an absence floors a maximum and fabricates a minimum."
)


# =========================================================================
# 2 [2]. A ONE-SIDED STATION CANNOT BE SELECTED
# =========================================================================
# THE FIXTURE, 32 x 41 at 5 m, channel down col 20, base(r) = 100 - 0.25r,
# walked under a 30 m half-width bound so the open flank runs the BOUND
# rather than the grid edge:
#
#   rows  0-13, 22-31   k = 5, rise 0.50 m   the seed's own reach
#   rows 14-17          k = 2, rise 1.00 m   the two-sided narrows
#   rows 18-21          RIGHT k = 1, rise 3.00 m; LEFT rises 0.6 m/cell
#                       to the grid edge and never falls back a
#                       prominence -- so the left walk runs its whole
#                       30 m allowance and declares nothing
#
# THE TWO STATIONS THE SELECTION IS BETWEEN, hand-derived:
#
#   station (18, 20)  w = 30.0 (bound) + 5.0 = 35.0 m
#                     L absent, R 3.00 m
#                     RETIRED score  3.00**2 / 35.0  = 0.25714   <- wins
#                     SHIPPED score  None (unscoreable)
#   station (14, 20)  w = 7.5 + 10.0 = 17.5 m
#                     L 1.00 m, R 1.00 m
#                     score  1.00**2 / 17.5 = 0.05714           <- wins now
#
# The one-sided station scores 4.5x the two-sided one under the retired
# reduction, on a shoulder that was never measured. That is the defect in
# one number.
ONE_SIDED_ROWS, ONE_SIDED_COLS, ONE_SIDED_CHANNEL = 32, 41, 20
ONE_SIDED_BOUND_M = 30.0
OPEN_FLANK_ROWS = (18, 21)
NARROWS_ROWS = (14, 17)


def _selection_array():
    array = np.zeros((ONE_SIDED_ROWS, ONE_SIDED_COLS))
    for r in range(ONE_SIDED_ROWS):
        base = 100.0 - 0.25 * r
        open_flank = OPEN_FLANK_ROWS[0] <= r <= OPEN_FLANK_ROWS[1]
        narrows = NARROWS_ROWS[0] <= r <= NARROWS_ROWS[1]
        for c in range(ONE_SIDED_COLS):
            d = abs(c - ONE_SIDED_CHANNEL)
            if open_flank and c > ONE_SIDED_CHANNEL:
                # THE OPEN FLANK: monotone rise to the grid edge, so only
                # the half-width bound ever stops this walk.
                array[r, c] = base + 0.6 * d
                continue
            if open_flank:
                k, rise = 1, 3.0
            elif narrows:
                k, rise = 2, 1.0
            else:
                k, rise = 5, 0.5
            if d < k:
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    return array


_selection_walk = _walk(
    _dem(_selection_array()),
    (4, ONE_SIDED_CHANNEL),
    max_half_width_meters=ONE_SIDED_BOUND_M,
)
_one_sided = _station(_selection_walk, (18, ONE_SIDED_CHANNEL))
_two_sided = _station(_selection_walk, (14, ONE_SIDED_CHANNEL))

# The one-sided station, measured exactly as the comment derives it.
assert _one_sided["width_m"] == 35.0
assert _one_sided["crest_height_left_m"] is None
assert _one_sided["crest_height_right_m"] == 3.0
assert _one_sided["crest_absence_left"] == CREST_ABSENCE_AT_BOUND, (
    "the BOUND stopped this flank, not the grid -- the fixture is about terrain, not extent"
)
assert _one_sided["crest_absence_right"] is None
assert _one_sided["measurement"]["left"]["half_width_m"] == ONE_SIDED_BOUND_M
assert _one_sided["binding_height_m"] is None
assert _one_sided["dam_site_score"] is None

# The two-sided station it loses to now.
assert _two_sided["width_m"] == 17.5
assert (_two_sided["crest_height_left_m"], _two_sided["crest_height_right_m"]) == (1.0, 1.0)
assert _two_sided["binding_height_m"] == 1.0
assert round(_two_sided["dam_site_score"], 5) == 0.05714

# THE RETIRED ARITHMETIC, RUN: the one-sided station would have won, and
# by a wide margin. Without this the section only shows that a station
# was skipped, not that the skip changed the answer.
assert _retired_reduction(_one_sided) == 0.25714
assert _retired_reduction(_two_sided) == 0.05714
assert _retired_reduction(_one_sided) > _retired_reduction(_two_sided) * 4, (
    "the fixture has to put the one-sided station clearly in front under the retired reduction, "
    "or the selection below could be explained by a tie"
)

# AND THE SELECTION TAKES THE TWO-SIDED ONE.
assert _selection_walk["found"] is True
assert tuple(_selection_walk["pinch_rowcol"]) == (14, ONE_SIDED_CHANNEL), (
    f"the objective must choose the next best TWO-SIDED station: {_selection_walk['pinch_rowcol']}"
)
assert _selection_walk["pinch_width_m"] == 17.5
assert _selection_walk["pinch_binding_height_m"] == 1.0
# Every station on the open-flank reach is skipped, and the skipped
# population is reportable off the existing counter.
_open_flank_stations = [
    s for s in _selection_walk["stations"]
    if OPEN_FLANK_ROWS[0] <= s["rowcol"][0] <= OPEN_FLANK_ROWS[1]
]
assert len(_open_flank_stations) == 4
assert all(s["dam_site_score"] is None for s in _open_flank_stations)
assert _selection_walk["unscoreable_station_count"] >= len(_open_flank_stations)

print(
    f"2. Selection: a station reading L absent (bound@{ONE_SIDED_BOUND_M:.0f} m) / R 3.00 m at "
    f"35.0 m wide scored {_retired_reduction(_one_sided)} under the retired reduction -- 4.5x the "
    f"{_retired_reduction(_two_sided)} of the two-sided 17.5 m narrows -- and won. It is now "
    f"unscoreable, all {len(_open_flank_stations)} stations on that reach with it, and the "
    f"objective takes {tuple(_selection_walk['pinch_rowcol'])}: 17.5 m wide, 1.00 m of shoulder on "
    "BOTH flanks."
)


# =========================================================================
# 3 [3]. THE ZONE-7 SHAPE, REPRODUCED
# =========================================================================
# The reference run's zone 7 at full scale, under the PRODUCTION 150 m
# bound: one flank running the whole allowance without a crest, the other
# a real 2.67 m shoulder, and a width no dam would ever be built across.
#
# THE FIXTURE, 30 x 121 at 5 m (605 m wide, so a 150 m walk stays on the
# grid), channel down col 60:
#
#   rows  6-9    k = 5,  rise 1.00 m    the two-sided alternative
#   rows 12-17   RIGHT k = 24, rise 2.67 m; LEFT rises 0.05 m/cell for
#                60 cells -- 300 m, twice the bound -- and never falls
#   elsewhere    k = 10, rise 0.40 m    the seed's own reach
#
# HAND-DERIVED, and matched against the reference run beside it:
#
#   ribbon station (12, 60)   L absent (bound@150.0 m) / R 2.67 m
#                             w = 150.0 + 120.0 = 270.0 m
#                             RETIRED score 2.67**2 / 270.0 = 0.02640
#                             (reference zone 7: R 2.66 m, w 267.4 m)
#   alternative    (6, 60)    L 1.00 / R 1.00 m, w = 22.5 + 25.0 = 47.5 m
#                             score 1.00**2 / 47.5 = 0.02105
#
# The ribbon beats the alternative by 25% under the retired reduction --
# on a flank nobody measured, at 270 m wide. That is the ranking the
# second test parcel showed at #1.
RIBBON_ROWS, RIBBON_COLS, RIBBON_CHANNEL = 30, 121, 60
RIBBON_STATION_ROWS = (12, 17)
RIBBON_ALT_ROWS = (6, 9)


def _ribbon_array(close_the_open_flank=False):
    """The ribbon fixture. close_the_open_flank=True gives the open flank
    the SAME 2.67 m shoulder the other side has, at the same distance --
    the one-variable control for section 3's second half."""
    array = np.zeros((RIBBON_ROWS, RIBBON_COLS))
    for r in range(RIBBON_ROWS):
        base = 100.0 - 0.25 * r
        ribbon = RIBBON_STATION_ROWS[0] <= r <= RIBBON_STATION_ROWS[1]
        alternative = RIBBON_ALT_ROWS[0] <= r <= RIBBON_ALT_ROWS[1]
        for c in range(RIBBON_COLS):
            d = abs(c - RIBBON_CHANNEL)
            if ribbon and c > RIBBON_CHANNEL and not close_the_open_flank:
                array[r, c] = base + 0.05 * d
                continue
            if ribbon:
                k, rise = 24, 2.67
            elif alternative:
                k, rise = 5, 1.0
            else:
                k, rise = 10, 0.4
            if d < k:
                array[r, c] = base + 0.02 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.02 * (d - k - 1)
    return array


_ribbon_walk = _walk(_dem(_ribbon_array()), (2, RIBBON_CHANNEL))
_ribbon = _station(_ribbon_walk, (12, RIBBON_CHANNEL))
_alternative = _station(_ribbon_walk, (6, RIBBON_CHANNEL))

assert _ribbon["width_m"] == 270.0, f"the ribbon's recorded width: {_ribbon['width_m']}"
assert _ribbon["measurement"]["left"]["half_width_m"] == RIDGE_WALK_MAX_HALF_WIDTH_METERS
assert _ribbon["crest_absence_left"] == CREST_ABSENCE_AT_BOUND
assert _ribbon["crest_height_left_m"] is None
assert _ribbon["crest_height_right_m"] == 2.67
assert _retired_reduction(_ribbon) == 0.0264
assert _retired_reduction(_alternative) == 0.02105
assert _retired_reduction(_ribbon) > _retired_reduction(_alternative), (
    "the ribbon must be the retired rule's winner, or this is not the zone-7 shape"
)
assert _ribbon["binding_height_m"] is None and _ribbon["dam_site_score"] is None
assert tuple(_ribbon_walk["pinch_rowcol"]) == (6, RIBBON_CHANNEL)
assert _ribbon_walk["pinch_width_m"] == 47.5, (
    f"a 270 m ribbon is no longer the dam site: {_ribbon_walk['pinch_width_m']} m"
)
assert _ribbon_walk["pinch_width_m"] < 250.0

# THE CONTROL, one variable: give the open flank the same shoulder the
# other side has and nothing else changes. The station becomes two-sided,
# its binding height is a MEASUREMENT, and it wins -- which is what shows
# the refusal above is about the ABSENCE and not about the width. A rule
# that simply disliked wide stations would refuse this one too.
_closed_walk = _walk(_dem(_ribbon_array(close_the_open_flank=True)), (2, RIBBON_CHANNEL))
_closed = _station(_closed_walk, (12, RIBBON_CHANNEL))
assert _closed["crest_height_left_m"] == 2.67 and _closed["crest_height_right_m"] == 2.67
assert _closed["crest_absence_left"] is None
assert _closed["binding_height_m"] == 2.67
assert _closed["width_m"] == 237.5, f"117.5 + 120.0, hand-derived: {_closed['width_m']}"
assert tuple(_closed_walk["pinch_rowcol"]) == (12, RIBBON_CHANNEL), (
    "with BOTH flanks measured the wide station is selectable again -- the refusal is about the "
    "missing measurement, not about the width"
)

print(
    f"3. The zone-7 shape: L absent (bound@{RIDGE_WALK_MAX_HALF_WIDTH_METERS:.0f} m) / R 2.67 m at "
    f"{_ribbon['width_m']:.1f} m wide scored {_retired_reduction(_ribbon)} and beat the two-sided "
    f"47.5 m station's {_retired_reduction(_alternative)}; it is now unscoreable and the dam site "
    f"is {tuple(_ribbon_walk['pinch_rowcol'])} at {_ribbon_walk['pinch_width_m']:.1f} m. Closing "
    f"that one flank -- and changing nothing else -- makes the same 237.5 m station a MEASURED "
    "2.67 m site and it wins again."
)


# =========================================================================
# 4 [4]. AN ALL-ONE-SIDED WALK KEEPS THE EXISTING CODE
# =========================================================================
# Every station one-sided: the left flank rises to the GRID EDGE on every
# row (a different absence from section 2's, and the walk says which).
# The objective has nothing to compare, and the failure is the code the
# absent-shoulder case has always carried -- not a new one, and not a
# fallback to the measured side.
def _all_one_sided_array():
    array = np.zeros((ONE_SIDED_ROWS, ONE_SIDED_COLS))
    for r in range(ONE_SIDED_ROWS):
        base = 100.0 - 0.25 * r
        for c in range(ONE_SIDED_COLS):
            d = abs(c - ONE_SIDED_CHANNEL)
            if c > ONE_SIDED_CHANNEL:
                array[r, c] = base + 0.6 * d
                continue
            k, rise = 2, 3.0
            if d < k:
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    return array


_all_one_sided = _walk(_dem(_all_one_sided_array()), (4, ONE_SIDED_CHANNEL))
assert _all_one_sided["found"] is False
assert _all_one_sided["reason_code"] == REASON_NO_MEASURABLE_SHOULDER, (
    f"the EXISTING code, not a new one: {_all_one_sided['reason_code']}"
)
assert _all_one_sided["reason_code"] in (
    wsa.REASON_NO_MEASURABLE_SHOULDER,
    wsa.REASON_BEST_SITE_AT_SEED,
    wsa.REASON_NO_CHANNEL_FROM_SEED,
), "and it is one of the walk's three existing failures"
assert "pinch_rowcol" not in _all_one_sided, "a failed walk names no dam site at all"
assert _all_one_sided["unscoreable_station_count"] == len(_all_one_sided["stations"])
for _s in _all_one_sided["stations"]:
    assert _s["crest_height_right_m"] is not None, "the RIGHT flank measured on every station"
    assert _s["binding_height_m"] is None, "and not one of them falls back to it"
    assert _s["crest_absence_left"] == CREST_ABSENCE_AT_GRID_EDGE, (
        "these flanks left the GRID -- a DEM-extent statement, reported as one and not conflated "
        "with a bound that is too short"
    )
# The two absences are distinct values, so a tally can never merge them.
assert CREST_ABSENCE_AT_BOUND != CREST_ABSENCE_AT_GRID_EDGE

print(
    f"4. All-one-sided: {len(_all_one_sided['stations'])} stations, every one with a measured "
    f"right flank and a left flank off the grid edge, fails at {REASON_NO_MEASURABLE_SHOULDER} -- "
    "the existing code, with no dam site named and no fallback to the side that measured."
)


# =========================================================================
# 5 [5]. THE GATE'S None BRANCH IS STILL UNREACHABLE
# =========================================================================
# The enclosure gate reads `binding_height is not None and binding_height
# < MIN_BINDING_SHOULDER_METERS`, so a None height means "cannot judge,
# do not gate". That branch has always been unreachable, and this branch
# ENLARGES the unscoreable set considerably -- so the argument is
# re-asserted rather than assumed.
#
# THE ARGUMENT IS STRUCTURAL, in three links, and none of them depends on
# how large the refused set is:
#   dam_site_score() returns None whenever the binding height is None;
#   the selection compares only stations whose score is not None;
#   a walk with no such station returns REASON_NO_MEASURABLE_SHOULDER
#   instead of a pinch.
# Growing the refused set moves seeds into that failure, never into the
# gate's None branch.
_score_source = inspect.getsource(wsa.dam_site_score)
assert "if binding_height_m is None:\n        return None" in _score_source, (
    "link 1: no binding height, no score"
)
_walk_source = inspect.getsource(wsa.walk_embankment_pinch)
assert 'if station["dam_site_score"] is not None' in _walk_source, (
    "link 2: the selection's population is the SCOREABLE stations"
)
assert "REASON_NO_MEASURABLE_SHOULDER" in _walk_source, "link 3: and no such station is a failure"
_gate_source = inspect.getsource(wsa.build_embankment_compartment)
assert (
    "binding_height is not None and binding_height < MIN_BINDING_SHOULDER_METERS"
    in _gate_source
), "the gate still guards on `is not None` rather than firing on a missing measurement"
# NO SECOND GUARD WAS ADDED. The consequence had to fall out of the
# existing machinery; a fresh refusal in the compartment builder would
# hide the very argument above.
_gate_tree = ast.parse(inspect.getsource(wsa.build_embankment_compartment).lstrip())
_reason_names = {
    node.id
    for node in ast.walk(_gate_tree)
    if isinstance(node, ast.Name) and node.id.startswith("REASON_")
}
assert REASON_NO_MEASURABLE_SHOULDER not in _reason_names, (
    "the compartment builder must not have grown its own absent-shoulder refusal: the walk owns "
    f"that failure. Found {_reason_names}"
)

# AND THE PROPERTY, on every walk this file builds: a walk that FOUND a
# dam site always has a binding height for it.
for _label, _found_walk in (
    ("selection", _selection_walk),
    ("ribbon", _ribbon_walk),
    ("ribbon control", _closed_walk),
):
    assert _found_walk["found"] is True
    assert _found_walk["pinch_binding_height_m"] is not None, _label
    assert _found_walk["pinch_dam_site_score"] is not None, _label

print(
    "5. The gate's None branch: unreachable by the same three-link argument as before -- no score "
    "without a binding height, no selection without a score, no pinch without a scoreable station "
    "-- re-checked at the source because this change enlarges the refused set; no second guard was "
    "added, and every walk that found a dam site has a binding height for it."
)


# =========================================================================
# 6 [6]. CONTRACT
# =========================================================================
# The rule changes WHICH compartments exist, so counts move. What may not
# move is the shape of the record a consumer reads.
_full_dem = _dem(_ribbon_array())
_full_result = compute_water_survey_areas(_full_dem, _boundary(RIBBON_ROWS, RIBBON_COLS))
_control_dem = _dem(_ribbon_array(close_the_open_flank=True))
_control_result = compute_water_survey_areas(_control_dem, _boundary(RIBBON_ROWS, RIBBON_COLS))

_compartments = [
    zone
    for result in (_full_result, _control_result)
    for zone in result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
    + [z for z in result["dropped_zones"] if z["survey_type"] == SURVEY_TYPE_EMBANKMENT]
]
assert _compartments, "the fixtures must build compartments, or the contract is unexercised"
for _zone in _compartments:
    for _field in (
        "zone_acres", "compartment_footprint_acres", "mean_suitability", "seed_blend_score",
        "pinch_catchment_acres", "pinch_drainage_score", "representative_elevation_m",
        "render_fill_polygon_utm", "polygon_utm", "rank", "id", "presented", "flags",
        "seed_crest_height_left_m", "seed_crest_height_right_m", "seed_crest_height_min_m",
        "pinch_crest_height_left_m", "pinch_crest_height_right_m", "pinch_crest_height_min_m",
        "half_width_bound_hit", "shoulder_below_minimum", "pinch_binding_height_m",
        "min_binding_shoulder_m",
    ):
        assert _field in _zone, f"the consumer contract lost {_field} on a {_zone['status']} zone"
    assert _zone["render_fill_polygon_utm"] is _zone["polygon_utm"]
    # THE ABSENCE REASON IS AN INTERNAL FIELD and stays one: it rides the
    # station and transect records and the diagnostic, and NOT the wire.
    # (The wire's full key set is pinned literally in
    # test_compartment_crest_height.py; this asserts only that this
    # branch added nothing to it.)
    _properties = wsa._zone_feature_properties(_zone)
    assert not [key for key in _properties if "absence" in key], sorted(_properties)
    assert "crest_absence_left" not in _properties

# WHERE IT DOES RIDE: every walked station and every transect, so a
# reader can tell a bound-stopped flank from one the DEM window cut off
# wherever a height is missing.
for _zone in _compartments:
    for _record in list(_zone["walk_stations"]) + list(_zone["transects"]):
        for _side in ("left", "right"):
            _height_key = f"crest_height_{_side}_m"
            _absence = _record[f"crest_absence_{_side}"]
            assert _absence in (None, CREST_ABSENCE_AT_BOUND, CREST_ABSENCE_AT_GRID_EDGE), _absence
            assert (_record[_height_key] is None) == (_absence is not None), (
                "the absence reason and the missing height must agree exactly: one is the reason "
                f"for the other. {_record[_height_key]!r} / {_absence!r}"
            )

# SELECTED_WATER_ZONE: REPORTED, NEVER ASSERTED STABLE. The two runs here
# differ in ONE thing -- whether the ribbon's far flank has a shoulder --
# so any movement between them is the rule doing its job.
def _identity(result):
    zone = result["selected_water_zone"]
    return None if zone is None else (zone["survey_type"], round(zone["polygon_utm"].area, 3))


_open_identity = _identity(_full_result)
_closed_identity = _identity(_control_result)
_open_counts = {t: len(v) for t, v in _full_result["zones_by_type"].items()}
_closed_counts = {t: len(v) for t, v in _control_result["zones_by_type"].items()}

print(
    f"6. Contract: every consumer field intact on {len(_compartments)} compartment record(s), the "
    "absence reason on the station and transect records and NOT on the wire, and each absence "
    f"agreeing exactly with the height it explains. Survivors {_open_counts} (open flank) -> "
    f"{_closed_counts} (flank closed); selected_water_zone {_open_identity} -> {_closed_identity} "
    f"({'MOVED' if _open_identity != _closed_identity else 'unchanged'}) -- REPORTED, not asserted."
)

print("\nAll one-sided binding-shoulder checks passed.")
