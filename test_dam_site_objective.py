"""
test_dam_site_objective.py

THE DAM SITE CHOOSES ON WIDTH AND HEIGHT.

The embankment cell used to be the MINIMUM-WIDTH station of the
downstream walk. Width alone is insufficient, and the reason is
structural rather than incidental: ridge_crest_walk() stops the moment
ground falls RIDGE_PROMINENCE_METERS behind its running maximum, so a
declared crest certifies only that a local high point EXISTS -- it may
stand 5 cm above the channel or 5 m. A 10 m pinch between 5 cm
shoulders is a narrow spot on a flat, not a dam site, and the retired
rule could not tell those apart.

The objective is now dam_site_score()'s h**exponent / w: impoundment per
unit of wall, with h the BINDING (lower) shoulder -- the side that
limits how high water can rise before spilling around the abutment.

Run as:

    python test_dam_site_objective.py

Sections (the design's numbered test items in brackets):
  1  [1]  WIDTH AND RATIO DISAGREE -- the ratio picks the deeper
          station; both profiles and both heights in the comment.
  2  [2]  THE EXPONENT, chosen by table -- a fixture where h/w and
          h**2/w disagree, both selections asserted, so the shipped
          choice is documented by a test and not by a preference.
  3  [3]  ABSENT SHOULDERS -- never win, never score 0.0; a walk with
          nothing measurable gets its own reason code.
  4  [4]  ZERO WIDTH -- refused, and the reasoning asserted.
  5  [5]  THE DEGENERATE SEED CASE keeps its own code.
  6  [6]  THE RETIRED NAME is gone (AST); the new codes reach the
          diagnostics, the export and the narrative.
  7  [7]  CONTRACT -- consumer fields intact; selected_water_zone
          movement REPORTED, not asserted away.
"""

import ast
import pathlib

import numpy as np
from shapely import contains_xy
from shapely.geometry import box

import water_survey_areas as wsa
from raster_grid import pixel_center_xy
from valley_delineation import compute_flow_direction, fill_depressions
from water_survey_areas import (
    DAM_SITE_HEIGHT_EXPONENT,
    DAM_SITE_HEIGHT_EXPONENT_LINEAR,
    DAM_SITE_HEIGHT_EXPONENT_STORAGE,
    DAM_SITE_SELECTION_MIN_WIDTH,
    DAM_SITE_SELECTION_RATIO,
    REASON_BEST_SITE_AT_SEED,
    REASON_NO_CHANNEL_FROM_SEED,
    REASON_NO_MEASURABLE_SHOULDER,
    SURVEY_TYPE_EMBANKMENT,
    compute_water_survey_areas,
    dam_site_score,
    ridge_crest_walk,
    walk_embankment_pinch,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"
_REPO = pathlib.Path(__file__).resolve().parent


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


def _walk(dem, seed_rowcol, **kwargs):
    rows, cols = dem["array"].shape
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    col_x = ORIGIN_X + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = ORIGIN_Y - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    kwargs.setdefault("max_walk_meters", 250.0)
    return walk_embankment_pinch(
        dem,
        seed_rowcol,
        flow_to_row,
        flow_to_col,
        contains_xy(_boundary_for(dem), xs, ys),
        np.zeros(dem["array"].shape, dtype=bool),
        **kwargs,
    )


# =========================================================================
# THE REACH FIXTURE, used by sections 1 and 2
# =========================================================================
# 56x31 at 5 m, channel down col 15, base(r) = 100 - 0.25r. The channel
# runs through four REACHES, each with its own shoulder half-width k and
# its own shoulder RISE above the channel:
#
#     rows  0-11   k = 7   rise 0.40 m     (the seed's own reach)
#     rows 12-23   k = 3   rise 1.00 m     NARROW and shallow
#     rows 24-35   k = 5   rise 1.50 m     WIDER and deeper
#     rows 36-55   k = 7   rise 0.40 m
#
# The cross-profile inside a reach is base + 0.05d -- a deliberately
# gentle floor, so the shoulder RISE is always the highest thing on the
# ray and the crest walk declares the crest at the shoulder rather than
# part-way up the floor. Beyond the shoulder the ground drops 2.6 m,
# well past the 1.0 m prominence, so every crest is confirmed.
#
# HAND-DERIVED WIDTHS. The walk samples every 2.5 m, so the j-th cell
# out is first seen at 5j - 2.5 m walking +x and at 5j walking -x:
# width = (5k - 2.5) + 5k = 10k - 2.5.
#     k = 7 -> 67.5 m    k = 3 -> 27.5 m    k = 5 -> 47.5 m
# HAND-DERIVED HEIGHTS: the shoulder is base + rise and the channel is
# base, so base cancels and the binding height IS the rise.
#
#     station      w        h       h/w        h**2/w
#     seed reach  67.5 m   0.40 m   0.00593    0.00237
#     narrow      27.5 m   1.00 m   0.03636    0.03636
#     wide-deep   47.5 m   1.50 m   0.03158    0.04737
# =========================================================================

REACH_ROWS, REACH_COLS, REACH_CHANNEL = 56, 31, 15
_REACHES = ((0, 11, 7, 0.40), (12, 23, 3, 1.00), (24, 35, 5, 1.50), (36, 55, 7, 0.40))
REACH_SEED = (4, REACH_CHANNEL)
NARROW_STATION = (12, REACH_CHANNEL)
WIDE_DEEP_STATION = (24, REACH_CHANNEL)


def _reach_spec(r):
    for low, high, k, rise in _REACHES:
        if low <= r <= high:
            return k, rise
    raise AssertionError(f"row {r} is outside every reach")


def _reach_array():
    array = np.zeros((REACH_ROWS, REACH_COLS))
    for r in range(REACH_ROWS):
        base = 100.0 - 0.25 * r
        k, rise = _reach_spec(r)
        for c in range(REACH_COLS):
            d = abs(c - REACH_CHANNEL)
            if d < k:
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    return array


REACH_DEM = _dem(_reach_array())

reach_min_width = _walk(REACH_DEM, REACH_SEED, selection_mode=DAM_SITE_SELECTION_MIN_WIDTH)
reach_linear = _walk(
    REACH_DEM, REACH_SEED, height_exponent=DAM_SITE_HEIGHT_EXPONENT_LINEAR
)
reach_storage = _walk(
    REACH_DEM, REACH_SEED, height_exponent=DAM_SITE_HEIGHT_EXPONENT_STORAGE
)
for _result in (reach_min_width, reach_linear, reach_storage):
    assert _result["found"] is True, _result.get("reason_code")

# The fixture's own numbers, asserted before anything is concluded from
# them -- a table nobody has checked is not evidence.
_by_rowcol = {tuple(s["rowcol"]): s for s in reach_storage["stations"]}
assert (_by_rowcol[REACH_SEED]["width_m"], _by_rowcol[REACH_SEED]["binding_height_m"]) == (67.5, 0.4)
assert (_by_rowcol[NARROW_STATION]["width_m"], _by_rowcol[NARROW_STATION]["binding_height_m"]) == (
    27.5, 1.0
)
assert (
    _by_rowcol[WIDE_DEEP_STATION]["width_m"],
    _by_rowcol[WIDE_DEEP_STATION]["binding_height_m"],
) == (47.5, 1.5)


# =========================================================================
# 1 [1]. WIDTH AND THE RATIO DISAGREE
# =========================================================================
# The retired rule picks the NARROW station: 27.5 m is the smallest
# width on the walk, and its 1.00 m shoulder never enters the decision.
# The shipped objective picks the WIDE-DEEP station: 50% more shoulder
# for 73% more wall. A 1.00 m shoulder is barely an impoundment.
assert reach_min_width["pinch_rowcol"] == NARROW_STATION, reach_min_width["pinch_rowcol"]
assert reach_min_width["pinch_width_m"] == 27.5
assert reach_storage["pinch_rowcol"] == WIDE_DEEP_STATION, reach_storage["pinch_rowcol"]
assert reach_storage["pinch_width_m"] == 47.5
assert reach_storage["pinch_binding_height_m"] == 1.5
assert reach_storage["pinch_rowcol"] != reach_min_width["pinch_rowcol"], "the dam cell MOVES"

# THE DEEPER STATION IS WHAT THE RATIO CHOSE, not merely a different
# one: the chosen station's binding shoulder beats the retired choice's.
assert (
    reach_storage["pinch_binding_height_m"] > reach_min_width["pinch_binding_height_m"]
), "the objective must move TOWARD depth, or it is not doing what it claims"
# And it pays for that depth in width, knowingly.
assert reach_storage["pinch_width_m"] > reach_min_width["pinch_width_m"]

# The selection is the argmax of the stored per-station score -- checked
# against the profile rather than taken on trust.
_scores = [s["dam_site_score"] for s in reach_storage["stations"] if s["dam_site_score"] is not None]
assert reach_storage["pinch_dam_site_score"] == max(_scores)
assert reach_storage["pinch_dam_site_score"] == dam_site_score(1.5, 47.5)

print(
    f"1. Width vs ratio: the retired rule picks {NARROW_STATION} (27.5 m wide, 1.00 m shoulder -- "
    f"the narrowest station, height unconsidered); the objective picks {WIDE_DEEP_STATION} "
    "(47.5 m, 1.50 m) -- 50% more shoulder for 73% more wall."
)


# =========================================================================
# 2 [2]. THE EXPONENT, CHOSEN BY TABLE
# =========================================================================
# THIS IS THE FIXTURE THE SHIPPED EXPONENT RESTS ON, and the reason it
# is a test rather than a comment: the two candidates disagree here, so
# the choice is recorded as an asserted fact about ground rather than as
# a preference.
#
#     station      w        h       h/w        h**2/w
#     narrow      27.5 m   1.00 m   0.03636    0.03636
#     wide-deep   47.5 m   1.50 m   0.03158    0.04737
#
# h/w picks NARROW -- the SAME cell the retired minimum-width rule
# picks. On a fixture built so depth is the deciding variable, the
# linear exponent reproduces the answer of the rule this whole branch
# exists to replace. That is the argument against it: it is too weak to
# do the work asked of it.
#
# h**2/w picks WIDE-DEEP. Storage grows faster than linearly with depth
# (a pool's surface spreads as it deepens), and for a farm pond depth is
# what makes stored water useful.
assert reach_linear["pinch_rowcol"] == NARROW_STATION, (
    f"h/w must pick the narrow station: {reach_linear['pinch_rowcol']}"
)
assert reach_storage["pinch_rowcol"] == WIDE_DEEP_STATION, (
    f"h**2/w must pick the wide-deep station: {reach_storage['pinch_rowcol']}"
)
assert reach_linear["pinch_rowcol"] != reach_storage["pinch_rowcol"], (
    "the exponents must actually disagree here, or this fixture decides nothing"
)
# AND h/w AGREES WITH THE RETIRED RULE ON THIS FIXTURE, which is the
# specific observation the choice was made from.
assert reach_linear["pinch_rowcol"] == reach_min_width["pinch_rowcol"], (
    "the argument for h**2/w is that h/w reproduces the minimum-width answer where depth is the "
    "deciding variable -- if that ever stops being true here, the choice needs re-making"
)

# The arithmetic behind the disagreement, so the table is checkable.
assert round(dam_site_score(1.0, 27.5, 1), 5) == 0.03636
assert round(dam_site_score(1.5, 47.5, 1), 5) == 0.03158
assert round(dam_site_score(1.0, 27.5, 2), 5) == 0.03636
assert round(dam_site_score(1.5, 47.5, 2), 5) == 0.04737
# The general rule the two exponents differ by: A beats B under h/w when
# hA/hB > wA/wB, and under h**2/w when (hA/hB)**2 > wA/wB. Here
# hA/hB = 1.5 and wA/wB = 47.5/27.5 = 1.727, which sits between 1.5 and
# 2.25 -- exactly the window where they part.
assert 1.5 < (47.5 / 27.5) < 1.5 ** 2

assert DAM_SITE_HEIGHT_EXPONENT == DAM_SITE_HEIGHT_EXPONENT_STORAGE == 2, (
    "the shipped exponent, pinned so a change to it has to come here and face the table above"
)

print(
    f"2. The exponent: on the deciding fixture h/w picks {NARROW_STATION} -- the same cell the "
    f"retired width rule picks -- while h**2/w picks {WIDE_DEEP_STATION}. Shipped: "
    f"h**{DAM_SITE_HEIGHT_EXPONENT}/w, because a linear exponent reproduces the answer of the rule "
    "this branch replaces on ground built to separate them."
)


# =========================================================================
# 3 [3]. ABSENT SHOULDERS NEVER WIN AND NEVER SCORE ZERO
# =========================================================================
# The absent-is-not-zero rule, applied to the objective. A station whose
# binding shoulder is ABSENT was never measured for depth; scoring it
# 0.0 would make it lose on merit it was never measured for, and
# scoring it at all would let an unmeasured station be compared with a
# measured one.
assert dam_site_score(None, 27.5) is None, "an absent shoulder is not scoreable"
assert dam_site_score(0.0, 27.5) == 0.0, (
    "a MEASURED zero IS scored, at zero -- 'the shoulder is level with the channel' is a finding, "
    "and it loses to any real shoulder on a measurement rather than on an absence"
)

# ON A FIXTURE: one reach whose flanks rise forever (no crest inside the
# half-width bound) sits between the seed and a real dam site. Those
# stations must be skipped, not zeroed, and must not win.
_ABSENT_ROWS = range(12, 24)


def _absent_array():
    array = np.zeros((REACH_ROWS, REACH_COLS))
    for r in range(REACH_ROWS):
        base = 100.0 - 0.25 * r
        if r in _ABSENT_ROWS:
            # Both flanks climb to the grid edge and never fall back a
            # prominence: no crest, so no height, at every station here.
            for c in range(REACH_COLS):
                array[r, c] = base + 0.6 * abs(c - REACH_CHANNEL)
            continue
        k, rise = _reach_spec(r)
        for c in range(REACH_COLS):
            d = abs(c - REACH_CHANNEL)
            if d < k:
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    return array


ABSENT_DEM = _dem(_absent_array())
absent_walk = _walk(ABSENT_DEM, REACH_SEED)
assert absent_walk["found"] is True, absent_walk.get("reason_code")
_absent_stations = [
    s for s in absent_walk["stations"] if s["rowcol"][0] in _ABSENT_ROWS
]
assert _absent_stations, "the fixture must actually produce unmeasurable stations"
for _station in _absent_stations:
    assert _station["binding_height_m"] is None, (
        f"a flank with no crest leaves no binding height: {_station['rowcol']}"
    )
    assert _station["dam_site_score"] is None, (
        "and therefore no score -- NOT 0.0, which would be losing on merit it was never measured "
        f"for: {_station['rowcol']}"
    )
    assert tuple(_station["rowcol"]) != tuple(absent_walk["pinch_rowcol"]), (
        "an unscoreable station can never be chosen"
    )
assert absent_walk["unscoreable_station_count"] == len(_absent_stations), (
    "the skipped population is counted and reportable, not left to be inferred"
)

# EVERY STATION UNMEASURABLE -> its own reason code, and NO fallback to
# the retired width rule.
def _all_absent_array():
    array = np.zeros((REACH_ROWS, REACH_COLS))
    for r in range(REACH_ROWS):
        base = 100.0 - 0.25 * r
        for c in range(REACH_COLS):
            array[r, c] = base + 0.6 * abs(c - REACH_CHANNEL)
    return array


all_absent = _walk(_dem(_all_absent_array()), REACH_SEED)
assert all_absent["found"] is False
assert all_absent["reason_code"] == REASON_NO_MEASURABLE_SHOULDER, all_absent["reason_code"]
assert all_absent["stations"], "stations WERE walked and widths measured -- only depth was missing"
assert all_absent["unscoreable_station_count"] == len(all_absent["stations"])
assert all_absent["width_profile_min_m"] is not None, (
    "the widths are on the record, which is exactly why falling back to them would be so easy -- "
    "and the objective deliberately does not"
)

print(
    f"3. Absent shoulders: {len(_absent_stations)} unmeasurable stations score None (never 0.0) and "
    "none is ever chosen; a walk where every station is unmeasurable fails with "
    f"{REASON_NO_MEASURABLE_SHOULDER} rather than falling back to the widths it did measure."
)


# =========================================================================
# 4 [4]. ZERO WIDTH IS REFUSED, AND WHY
# =========================================================================
# THE DECISION, and its reasoning, asserted rather than asserted-about.
# w == 0 means BOTH crest walks declared their crest at distance zero --
# both crests ARE the station's own channel cell, because ground fell a
# full prominence immediately on both sides. That is not a narrows
# between two shoulders; it is a channel cell that is a local HIGH POINT
# in cross-section. Treating w -> 0 as an infinitely good dam site would
# be exactly backwards.
#
# THE GUARD DECIDES NOTHING ON ITS OWN, and that is worth showing: both
# crests coinciding with the station forces both heights to 0.0, so the
# station's height is 0 and it would score 0 and lose to any real
# shoulder anyway. The guard keeps the division defined and states the
# reasoning; it does not rescue a selection.
assert dam_site_score(0.0, 0.0) is None, "0/0 is not a dam site"
assert dam_site_score(1.0, 0.0) is None, "and no width is refused whatever height is claimed"

# The coincidence, on a real walk: a strip where ground falls 1.5 m
# immediately on both sides of the station. Both crests land ON the
# station, so both half-widths are 0.0 and both heights are 0.0.
_SPUR = _dem(np.tile(np.array([18.0, 18.2, 20.0, 18.5, 18.4, 18.3]), (3, 1)))
_spur_station = (1, 2)
_spur_xy = pixel_center_xy(_SPUR, *_spur_station)
_left = ridge_crest_walk(_SPUR, _spur_xy, (1.0, 0.0))
_right = ridge_crest_walk(_SPUR, _spur_xy, (-1.0, 0.0))
assert _left["half_width_m"] == 0.0 and _right["half_width_m"] == 0.0
assert tuple(_left["crest_rowcol"]) == tuple(_right["crest_rowcol"]) == _spur_station, (
    "both crests ARE the station: a local high point in cross-section, not a narrows"
)
assert wsa.crest_height_above_channel(_SPUR, _left, _spur_station) == 0.0
assert wsa.crest_height_above_channel(_SPUR, _right, _spur_station) == 0.0
assert wsa.lower_crest_height(0.0, 0.0) == 0.0, (
    "so a zero-width station necessarily has zero height -- the two always arrive together"
)
assert dam_site_score(0.0, 0.0) is None

print(
    "4. Zero width: refused, not treated as the perfect site. Both crests coinciding with the "
    "station means a local HIGH POINT in cross-section, and it forces the height to 0.0 as well -- "
    "so the guard keeps the division defined and changes no selection."
)


# =========================================================================
# 5 [5]. THE DEGENERATE SEED CASE KEEPS ITS OWN CODE
# =========================================================================
# The seed being its own best dam site is a statement about the SHAPE of
# a measured profile, not about a missing measurement, so it stays
# distinct from section 3's code. A channel whose best reach is the
# seed's own: a deep narrow seed reach opening out downstream.
def _seed_best_array():
    array = np.zeros((REACH_ROWS, REACH_COLS))
    for r in range(REACH_ROWS):
        base = 100.0 - 0.25 * r
        k, rise = (3, 2.0) if r < 8 else (7, 0.4)
        for c in range(REACH_COLS):
            d = abs(c - REACH_CHANNEL)
            if d < k:
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    return array


seed_best = _walk(_dem(_seed_best_array()), (0, REACH_CHANNEL))
assert seed_best["found"] is False
assert seed_best["reason_code"] == REASON_BEST_SITE_AT_SEED, seed_best["reason_code"]
assert seed_best["reason_code"] != REASON_NO_MEASURABLE_SHOULDER, (
    "the degenerate-baseline case must NOT be confused with 'nothing was measurable' -- here "
    "everything was measured and the answer was simply the seed"
)
assert seed_best["stations"][0]["dam_site_score"] is not None, "the seed itself scored fine"

# THE THREE CODES ARE THREE DIFFERENT FINDINGS, and their values are
# pinned so a rename has to come through here.
assert (REASON_NO_CHANNEL_FROM_SEED, REASON_NO_MEASURABLE_SHOULDER, REASON_BEST_SITE_AT_SEED) == (
    "no_channel_from_seed", "no_measurable_shoulder", "best_site_at_seed"
)
assert len({REASON_NO_CHANNEL_FROM_SEED, REASON_NO_MEASURABLE_SHOULDER, REASON_BEST_SITE_AT_SEED}) == 3

print(
    f"5. Degenerate seed: a channel whose best site is its own anchor fails with "
    f"{REASON_BEST_SITE_AT_SEED} -- distinct from {REASON_NO_MEASURABLE_SHOULDER}, because here "
    "everything WAS measured and the answer was the seed."
)


# =========================================================================
# 6 [6]. THE RETIRED NAME IS GONE
# =========================================================================
# The AST-absence pattern this repo already uses for retired vocabulary:
# the name may survive in DOCSTRINGS, where the history is worth
# narrating, and nowhere else.
_RETIRED = "REASON_NO_CONSTRICTION"
assert not hasattr(wsa, _RETIRED), "the attribute is gone"

_PRODUCTION = [
    p for p in sorted(_REPO.rglob("*.py")) if not p.name.startswith(("test_", "_"))
]
_offenders = []
for _path in _PRODUCTION:
    _tree = ast.parse(_path.read_text())
    _docstrings = set()
    for _node in ast.walk(_tree):
        if isinstance(_node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _doc = ast.get_docstring(_node, clean=False)
            if _doc:
                _docstrings.add(_doc)
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Name) and _node.id == _RETIRED:
            _offenders.append(f"{_path.name}:{_node.lineno} name")
        if (
            isinstance(_node, ast.Constant)
            and isinstance(_node.value, str)
            and "no_constriction" in _node.value
            and _node.value not in _docstrings
        ):
            _offenders.append(f"{_path.name}:{_node.lineno} string")
assert not _offenders, (
    f"the retired name survives outside docstrings: {_offenders}"
)

# AND THE NEW CODES REACH THE CONSUMERS. The seed ladder, the export's
# failed-seed layer and the narrative all carry reason codes verbatim
# off the seed record, so a code that exists is a code that ships --
# asserted end to end on a fixture that produces one.
_flat = _dem(np.tile(np.full(REACH_COLS, 50.0), (REACH_ROWS, 1)))
_flat_result = compute_water_survey_areas(_flat, _boundary_for(_flat))
_codes = {
    record["reason_code"]
    for record in _flat_result["embankment_seeds"]
    if record.get("status") == wsa.SEED_STATUS_FAILED and "reason_code" in record
}
assert _codes, "the flat fixture must fail some seeds, or this proves nothing"
assert _codes <= {
    REASON_NO_CHANNEL_FROM_SEED, REASON_NO_MEASURABLE_SHOULDER, REASON_BEST_SITE_AT_SEED
}, f"only the new vocabulary reaches the seed records: {_codes}"

from diagnose_water_survey_areas import summarize_seed_ladder
from wire_translation import water_survey_zones_to_feature_collection

# summarize_seed_ladder() reads an identify_* return (the wire shape),
# not the compute core's, so the fixture is wrapped the way the real
# entry point wraps it rather than the instrument being loosened.
_ladder = summarize_seed_ladder(
    {
        "result": _flat_result,
        "embankment_seeds": _flat_result["embankment_seeds"],
        "zones_by_type": _flat_result["zones_by_type"],
    }
)
assert any(code in _ladder for code in _codes), (
    f"the seed ladder must print the new codes: {_codes}"
)
_collection = water_survey_zones_to_feature_collection(
    _flat_result["zones"], _flat_result["dropped_zones"]
)
assert _collection["type"] == "FeatureCollection", "the export still builds with the new vocabulary"

print(
    f"6. Retirement: {_RETIRED} is absent as an attribute, as an AST name and as a non-docstring "
    f"string across {len(_PRODUCTION)} production module(s); the new codes {sorted(_codes)} reach "
    "the seed records and print on the seed ladder."
)


# =========================================================================
# 7 [7]. CONTRACT
# =========================================================================
# The objective changes WHICH station is chosen, so numbers move. What
# may not move is the shape of the record or the set of fields a
# consumer reads.
_shipped = compute_water_survey_areas(REACH_DEM, _boundary_for(REACH_DEM))
_retired_rule = compute_water_survey_areas(
    REACH_DEM, _boundary_for(REACH_DEM), selection_mode=DAM_SITE_SELECTION_MIN_WIDTH
)
for _zone in _shipped["zones_by_type"][SURVEY_TYPE_EMBANKMENT]:
    for _field in (
        "zone_acres", "compartment_footprint_acres", "mean_suitability", "seed_blend_score",
        "pinch_catchment_acres", "pinch_drainage_score", "representative_elevation_m",
        "render_fill_polygon_utm", "polygon_utm", "rank", "id", "presented",
        "seed_crest_height_min_m", "pinch_crest_height_min_m", "half_width_bound_hit",
    ):
        assert _field in _zone, f"the consumer contract lost {_field}"
    assert _zone["render_fill_polygon_utm"] is _zone["polygon_utm"]
    # THE OBJECTIVE'S OWN NUMBERS ride the pinch record, so the choice
    # is checkable off the zone.
    for _field in ("binding_height_m", "dam_site_score", "height_exponent"):
        assert _field in _zone["pinch"], f"the pinch record lost {_field}"
    assert _zone["pinch"]["height_exponent"] == DAM_SITE_HEIGHT_EXPONENT
    for _station in _zone["walk_stations"]:
        for _field in ("binding_height_m", "dam_site_score", "crest_height_left_m",
                       "crest_height_right_m"):
            assert _field in _station, f"station record never gained {_field}"


def _selection_identity(result):
    zone = result["selected_water_zone"]
    return None if zone is None else (zone["survey_type"], round(zone["polygon_utm"].area, 3))


_before = _selection_identity(_retired_rule)
_after = _selection_identity(_shipped)
_counts_before = {t: len(v) for t, v in _retired_rule["zones_by_type"].items()}
_counts_after = {t: len(v) for t, v in _shipped["zones_by_type"].items()}

# THE TERRAIN CHECK, restated: the objective RELOCATES dam cells toward
# the best available shoulders. It cannot create shoulders that are not
# there, and a surviving zone crossing 1.0 m of binding depth would be
# a result worth naming rather than a vindication.
_crossings = [
    (zone["id"], zone["pinch"]["binding_height_m"])
    for zone in _shipped["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
    if zone["pinch"]["binding_height_m"] is not None
    and zone["pinch"]["binding_height_m"] >= 1.0
]

print(
    f"7. Contract: consumer fields intact on "
    f"{len(_shipped['zones_by_type'][SURVEY_TYPE_EMBANKMENT])} compartment(s), the objective's "
    f"numbers ride the pinch and every station. Survivors {_counts_before} -> {_counts_after}; "
    f"selected_water_zone {_before} -> {_after} "
    f"({'MOVED' if _before != _after else 'unchanged'}) -- REPORTED, not asserted. "
    f"Binding depths at or above 1.0 m on survivors: {_crossings or 'none'}."
)

print("\nAll dam-site objective checks passed.")
