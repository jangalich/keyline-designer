"""
test_twi_boundary_independence.py

THE STANDING TEST FOR ONE BUG CLASS: a criterion scored RELATIVE TO THE
PARCEL makes the whole composite move when the user redraws the
boundary, and the failure looks like terrain analysis rather than like a
defect.

It was found once, by accident, on a real run: the same land produced an
embankment survey zone under one boundary and none under a slightly
larger one reaching further into a stream corridor. TWI was the lone
parcel-relative input in either blend -- a percentile rank over the
on-parcel cells -- so adding the wettest cells on the landscape took the
top ranks, pushed every other cell's rank DOWN, and at 0.20 of the
embankment blend that was enough to drop a ~0.52 seed under the
then-0.50 seeding minimum with the terrain unchanged. The direction is
the tell: INCLUDING MORE WATER MADE THE WATER SITES SCORE WORSE.

water_survey_areas.twi_score() replaced that percentile with a fixed
absolute curve over raw ln(a/tan(beta)). This file pins the fix:

    1. THE BUG, PINNED -- one synthetic DEM, two boundaries (the smaller
       one, and a larger one that adds a high-TWI stream corridor).
       A fixed cell's TWI score is IDENTICAL under both, and a zone that
       qualifies under the smaller boundary still qualifies under the
       larger. The retired percentile is computed alongside on the same
       cells, so the test states the size of what was fixed rather than
       asserting that something was.
    2. RETIREMENT -- parcel_relative_percentile() is absent from the TWI
       path at the AST level, and no user-facing string anywhere claims
       parcel-relative TWI.
    3. THE --boundary ARGUMENT -- the default reproduces the reference
       run; a supplied boundary is the one that runs.
    4. THE BOUNDARY-STABILITY REPORT -- survive-both vs appear-in-one
       classification and per-criterion deltas, on a two-boundary
       synthetic.

Run:  python test_twi_boundary_independence.py   (no network)
"""

import ast
import inspect
import io
import json
import math
import os
import tempfile

import numpy as np
from shapely import contains_xy
from shapely.geometry import box

import diagnose_water_survey_areas as diag
import report_generator
import water_survey_areas as wsa
from water_survey_areas import (
    EMBANKMENT_SEED_MIN_SCORE,
    SURVEY_TYPE_EMBANKMENT,
    build_narrative_data,
    compute_water_survey_areas,
    parcel_relative_percentile,
    twi_score,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"


# =========================================================================
# 1. THE BUG, PINNED: ONE DEM, TWO BOUNDARIES
# =========================================================================
# THE FIXTURE, and why each piece of it is there.
#
#   cols 0-24   THE CANDIDATE VALLEY. A V-section draining south with a
#               waist at rows 18-21 (the pinched-valley construction
#               test_embankment_compartments.py uses), channel at col
#               12. Its channel carries CANDIDATE_ACCUMULATION cells,
#               which is REAL but ordinary convergence -- raw TWI 8.99.
#   cols 25-40  A RIDGE, 20 m up, so the two halves are hydrologically
#               separate and the corridor cannot feed the candidate.
#   cols 41-119 THE STREAM CORRIDOR. Wide, nearly flat, and genuinely
#               wet: every cell carries CORRIDOR_ACCUMULATION. This is
#               the ground the larger boundary reaches into.
#
# THE TWO BOUNDARIES. SMALL covers the candidate valley only. LARGE is
# the same line extended east across the ridge to take in the corridor.
# The candidate valley is INSIDE BOTH, cell for cell -- so every
# statement below about it is a statement about identical ground.
#
# Flow accumulation is a hand-built OVERRIDE (the established fixture
# pattern here) so raw TWI is exactly computable: a = accumulation *
# cell_area / cell_width = 5 * cells metres, over the fixture's own
# tan(beta).
ROWS, COLS = 40, 120
CANDIDATE_CHANNEL, CORRIDOR_CENTER = 12, 80
CANDIDATE_ACCUMULATION = 170
CORRIDOR_ACCUMULATION = 1500
# The cell every per-cell claim below is made about: on the candidate
# channel, well inside the SMALL boundary.
PROBE = (25, CANDIDATE_CHANNEL)


def _dem(array):
    return {
        "array": array,
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _k_of_row(row):
    """Half-width of the candidate valley floor: a waist at rows 18-21,
    so the compartment machinery has a real narrowing to walk to."""
    if 18 <= row <= 21:
        return 2
    return 4 if row < 18 else 5


def _build_array():
    array = np.zeros((ROWS, COLS))
    for r in range(ROWS):
        base = 100.0 - 0.25 * r
        k = _k_of_row(r)
        for c in range(COLS):
            if c <= 24:
                d = abs(c - CANDIDATE_CHANNEL)
                if d < k:
                    array[r, c] = base + 0.5 * d
                elif d == k:
                    array[r, c] = base + 3.0
                else:
                    array[r, c] = base + 1.0 - 0.05 * (d - k - 1)
            elif c <= 40:
                array[r, c] = base + 20.0 + 0.5 * min(c - 25, 40 - c)
            else:
                array[r, c] = base - 2.0 + 0.02 * abs(c - CORRIDOR_CENTER)
    return array


TWO_BOUNDARY_DEM = _dem(_build_array())
TWO_BOUNDARY_ACCUMULATION = np.ones((ROWS, COLS))
for _r in range(ROWS):
    TWO_BOUNDARY_ACCUMULATION[_r, CANDIDATE_CHANNEL] = CANDIDATE_ACCUMULATION
    for _c in range(41, COLS):
        TWO_BOUNDARY_ACCUMULATION[_r, _c] = CORRIDOR_ACCUMULATION

SMALL_BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 39 * RESOLUTION + 0.1,
    ORIGIN_X + 24 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
LARGE_BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 39 * RESOLUTION + 0.1,
    ORIGIN_X + 119 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
assert SMALL_BOUNDARY.within(LARGE_BOUNDARY), (
    "the smaller boundary must be wholly inside the larger, or the comparison is not about "
    "identical ground"
)


def _run(boundary):
    return compute_water_survey_areas(
        TWO_BOUNDARY_DEM, boundary, flow_accumulation=TWO_BOUNDARY_ACCUMULATION
    )


def _retired_percentile_scores(result):
    """The RETIRED parcel-relative scores over the population the retired
    code used -- the ON-PARCEL cells, not just the gated ones (the
    ceiling gate took cells out of play, not out of the parcel). The run
    returns that mask, so this is the same population, not a second
    construction of it. The before half of the before/after."""
    return parcel_relative_percentile(result["screens"]["twi_raw"], result["on_parcel_mask"])


small = _run(SMALL_BOUNDARY)
large = _run(LARGE_BOUNDARY)

small_pct = _retired_percentile_scores(small)
large_pct = _retired_percentile_scores(large)

# --- the raw index is untouched by this branch: same terrain, same value
raw_small = float(small["screens"]["twi_raw"][PROBE])
raw_large = float(large["screens"]["twi_raw"][PROBE])
assert raw_small == raw_large, "raw ln(a/tan(beta)) is a property of the cell; both runs must agree"
assert math.isclose(raw_small, 8.9889, abs_tol=5e-4), f"hand-checked raw TWI at the probe, got {raw_small}"

# --- THE CENTRAL ASSERTION: the SCORE is identical under both boundaries
score_small = float(small["screens"]["twi_score"][PROBE])
score_large = float(large["screens"]["twi_score"][PROBE])
assert score_small == score_large, (
    f"THE WHOLE POINT: the same cell's TWI score must be byte-identical under both boundaries "
    f"(got {score_small} vs {score_large})"
)
assert math.isclose(score_small, (raw_small - wsa.TWI_SCORE_MIN_BREAKPOINT) / (
    wsa.TWI_SCORE_FULL_CREDIT_BREAKPOINT - wsa.TWI_SCORE_MIN_BREAKPOINT
)), "and it is the curve's own value, not a coincidence"
assert math.isclose(score_small, 0.7472, abs_tol=5e-4)

# --- WHAT IT WOULD HAVE BEEN: the retired percentile, on the same cells.
# THE OLD VALUES, STATED: 0.9788 under the SMALL boundary and 0.3432
# under the LARGE one. Same cell, same terrain, same DEM -- a 0.6356
# collapse produced entirely by which OTHER cells the user enclosed.
old_small = float(small_pct[PROBE])
old_large = float(large_pct[PROBE])
assert math.isclose(old_small, 0.9788, abs_tol=5e-4), f"retired percentile under SMALL, got {old_small}"
assert math.isclose(old_large, 0.3432, abs_tol=5e-4), f"retired percentile under LARGE, got {old_large}"
assert old_small - old_large > 0.6, (
    "the retired scoring moved this cell by more than 0.6 for no terrain reason at all -- that "
    "gap is the bug, and it is what the identical absolute scores above replace"
)

# --- and the blend consequence: seeds inside the SMALL boundary, counted
# under both scorings. The absolute curve keeps every one; the percentile
# loses ALL of them the moment the corridor joins the parcel.
col_x = ORIGIN_X + (np.arange(COLS) + 0.5) * RESOLUTION
row_y = ORIGIN_Y - (np.arange(ROWS) + 0.5) * RESOLUTION
_xs, _ys = np.meshgrid(col_x, row_y)
IN_SMALL_BOX = contains_xy(SMALL_BOUNDARY, _xs, _ys)


# THE COMPARISON THRESHOLD IS PINNED, NOT READ FROM THE LIVE CONSTANT.
# EMBANKMENT_SEED_MIN_SCORE is a SEEDING POLICY number that moves for
# reasons of its own (it has since dropped to 0.30 to admit the
# off-channel archetype). This test is about the TWI SCORE, and it must
# keep measuring the same thing when that policy moves -- so it states
# the value the constant held WHEN THE BUG WAS FOUND and compares
# against that. Reading the live constant here would let an unrelated
# tuning change quietly rewrite what this test demonstrates, which is
# precisely the coupling the branch exists to remove.
BUG_ERA_SEED_MINIMUM = 0.50


def _clearing_count(result, surface, threshold=BUG_ERA_SEED_MINIMUM):
    return int(np.count_nonzero((surface >= threshold) & result["gate_mask"] & IN_SMALL_BOX))


def _retired_surface(result, percentile):
    criteria = result["surfaces"]["criteria"][SURVEY_TYPE_EMBANKMENT]
    surface = np.zeros(TWO_BOUNDARY_DEM["array"].shape, dtype=np.float64)
    for name, weight in wsa.EMBANKMENT_WEIGHTS.items():
        grid = np.where(np.isnan(percentile), 0.0, percentile) if name == "twi" else criteria[name]
        surface += weight * grid
    return np.where(result["gate_mask"], surface, 0.0)


absolute_small = _clearing_count(small, small["surfaces"][SURVEY_TYPE_EMBANKMENT])
absolute_large = _clearing_count(large, large["surfaces"][SURVEY_TYPE_EMBANKMENT])
retired_small = _clearing_count(small, _retired_surface(small, small_pct))
retired_large = _clearing_count(large, _retired_surface(large, large_pct))
assert absolute_small == absolute_large == 38, (
    f"every cell that clears the bug-era seeding bar inside the small boundary still clears it "
    f"when the boundary grows (got {absolute_small} then {absolute_large})"
)
assert retired_small == 442 and retired_large == 0, (
    f"under the retired percentile the SAME ground went from {retired_small} qualifying cells to "
    f"{retired_large} -- the entire candidate valley disqualified by ground outside it"
)
# The same invariant at whatever the seeding minimum is TODAY: the count
# is a different number at a different bar, and it must still be the
# SAME number under both boundaries. This is the half that tracks the
# live constant; the pinned numbers above are the half that records the
# bug.
live_small = _clearing_count(small, small["surfaces"][SURVEY_TYPE_EMBANKMENT], EMBANKMENT_SEED_MIN_SCORE)
live_large = _clearing_count(large, large["surfaces"][SURVEY_TYPE_EMBANKMENT], EMBANKMENT_SEED_MIN_SCORE)
assert live_small == live_large, (
    f"at the live seeding minimum ({EMBANKMENT_SEED_MIN_SCORE}) the candidate valley must still "
    f"qualify identically under both boundaries (got {live_small} then {live_large})"
)

# --- THE ZONE ITSELF SURVIVES BOTH, which is the acceptance question
small_embankment = {
    tuple(zone["seed"]["rowcol"]): zone for zone in small["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
}
large_embankment = {
    tuple(zone["seed"]["rowcol"]): zone for zone in large["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
}
# NOTHING THE SMALLER BOUNDARY FOUND MAY GO MISSING. That is the
# acceptance question in its exact shape -- the real failure was a zone
# present under one boundary and absent under a larger one over the same
# land. The larger boundary GAINING compartments is legitimate (it holds
# ground the smaller one does not), so the assertion is one-directional.
assert set(small_embankment) <= set(large_embankment), (
    f"the larger boundary lost {set(small_embankment) - set(large_embankment)} -- the bug's own "
    "signature, on ground it still contains"
)
# WHICH CELLS BECOME SEEDS IS ALLOWED TO DIFFER, and only that. Seeding
# is separation-claimed over the GATED population, and the gate mask IS
# the boundary, so a larger gated population can claim in a different
# order and expose a cell the smaller run's discs had covered. That is
# the boundary deciding what is in play. What may never differ is what a
# cell in play is WORTH -- asserted exhaustively per cell above, and per
# criterion on every shared compartment here.
CANDIDATE_SEED = (1, CANDIDATE_CHANNEL)
assert CANDIDATE_SEED in small_embankment, "the candidate valley's compartment exists at all"
for seed_cell, small_zone in small_embankment.items():
    large_zone = large_embankment[seed_cell]
    assert small_zone["seed_blend_score"] == large_zone["seed_blend_score"], (
        f"{seed_cell}: the seed blend score moved between boundaries -- the number that vanished "
        "under the boundary the real run lost the zone on"
    )
    assert math.isclose(small_zone["zone_acres"], large_zone["zone_acres"]), (
        f"{seed_cell}: the walkable claim moved between boundaries"
    )
    for name in wsa.EMBANKMENT_WEIGHTS:
        a = small_zone["criterion_contributions"][name]["mean_score"]
        b = large_zone["criterion_contributions"][name]["mean_score"]
        assert a == b, (
            f"{seed_cell}: criterion '{name}' moved between boundaries ({a} -> {b}) on identical ground"
        )

print(
    f"1. The bug, pinned: probe cell raw TWI {raw_small:.4f} scores {score_small:.4f} under BOTH "
    f"boundaries (retired percentile: {old_small:.4f} -> {old_large:.4f}); cells clearing the "
    f"bug-era {BUG_ERA_SEED_MINIMUM} bar in the candidate valley {absolute_small} -> "
    f"{absolute_large} absolute vs {retired_small} -> {retired_large} retired, and "
    f"{live_small} -> {live_large} at the live {EMBANKMENT_SEED_MIN_SCORE} minimum; the "
    f"{len(small_embankment)} compartment(s) survive both (the larger boundary gains "
    f"{len(large_embankment) - len(small_embankment)} and loses none), every criterion mean unchanged."
)


# =========================================================================
# 2. RETIREMENT: OFF THE PATH, AND OUT OF THE PROSE
# =========================================================================
_module_ast = ast.parse(inspect.getsource(wsa))
_called_names = {
    node.func.id
    for node in ast.walk(_module_ast)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
}
assert "parcel_relative_percentile" not in _called_names, (
    "parcel_relative_percentile() is RETIRED from the TWI path: nothing in water_survey_areas.py "
    "may call it. It survives as a function only so the diagnostic can reproduce the old scores "
    "for comparison (docstring history stays legal -- this check is AST-level)."
)
assert "twi_score" in _called_names, "and the absolute scorer IS the one on the path"
assert hasattr(wsa, "parcel_relative_percentile"), (
    "retired, not deleted -- the house convention, and the before/after comparison needs it"
)

# The compute function that used to call it must now call twi_score()
# instead. Checked on THAT function's own source, not just the module's.
_compute_ast = ast.parse(inspect.getsource(wsa.compute_water_survey_areas))
_compute_calls = {
    node.func.id
    for node in ast.walk(_compute_ast)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
}
assert "twi_score" in _compute_calls and "parcel_relative_percentile" not in _compute_calls, (
    "the swap happened at the call site the bug lived at"
)

# THE CAVEAT IS GONE FROM THE WIRE, not renamed in place.
assert not hasattr(wsa, "TWI_PARCEL_RELATIVE_NOTE"), "the parcel-relative note constant is gone"
assert not hasattr(wsa, "twi_is_parcel_relative"), "and so is any module-level flag by that name"

_narrative = build_narrative_data(small)
assert _narrative["twi_is_absolute"] is True
assert "twi_is_parcel_relative" not in _narrative, "the retired flag does not ride the wire"

# NO USER-FACING STRING MAY CLAIM PARCEL-RELATIVE TWI. Checked over
# everything a reader can actually see: the narrative block (note, panel
# rows and their labels, scales) and the report prose built from it.
_FORBIDDEN = ("parcel-relative", "PARCEL-RELATIVE", "THIS parcel", "wettest on this parcel", "percentile rank")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


for _text in _strings(_narrative):
    for _claim in _FORBIDDEN:
        assert _claim not in _text, f"a surviving user-facing string still claims parcel-relative TWI: {_text!r}"
assert "resolution-dependent" in _narrative["twi_note"] and "calibrated" in _narrative["twi_note"], (
    "what replaced it is the RESOLUTION-CALIBRATION caveat, which is still true and still warranted"
)

# THE REPORT PROSE, the other place a reader meets this claim: it is
# built from the narrative block above and must carry the caveat that is
# now true and none of the one that is not.
_prose = report_generator._format_water_survey_areas_summary(_narrative)
for _claim in _FORBIDDEN:
    assert _claim not in _prose, f"the report prose still claims parcel-relative TWI: {_claim!r}"
assert "FIXED ABSOLUTE curve" in _prose and "calibrated at the 5 m reference DEM" in _prose, (
    "and it does carry the resolution-calibration caveat that replaced it"
)
print(
    "2. Retirement: parcel_relative_percentile() is AST-absent from water_survey_areas (call site "
    "swapped to twi_score), the note constant and the twi_is_parcel_relative flag are gone, and no "
    "user-facing string in narrative_data OR in the report prose claims parcel-relative TWI."
)


# =========================================================================
# 3. THE --boundary ARGUMENT (parsing only -- no logic changed)
# =========================================================================
# THE DEFAULT REPRODUCES THE REFERENCE RUN: the constant the diagnostic
# defaults to is exactly the boundary it used to hardcode, vertex for
# vertex, so every pre-existing invocation is unchanged.
HISTORICAL_REFERENCE_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
assert diag.REFERENCE_BOUNDARY == HISTORICAL_REFERENCE_BOUNDARY, (
    "the --boundary default IS the boundary this diagnostic has always run"
)
# And the second boundary is the one the real run lost a zone under.
assert diag.STREAM_CORRIDOR_BOUNDARY == [
    (-79.98395562171937, 40.6460162710763),
    (-79.98374104499818, 40.642584987588364),
    (-79.98047947883607, 40.64432504438868),
    (-79.98097300529480, 40.645089354064524),
    (-79.98150944709779, 40.645170663089445),
    (-79.98266816139223, 40.64596748629134),
]

# load_boundary reads a JSON list of [lon, lat] pairs into the tuple
# list every entry point here takes.
with tempfile.TemporaryDirectory() as _tmp:
    _path = os.path.join(_tmp, "boundary.json")
    _supplied = [[-80.1, 40.5], [-80.0, 40.5], [-80.0, 40.6]]
    io.open(_path, "w").write(json.dumps(_supplied))
    assert diag.load_boundary(_path) == [(-80.1, 40.5), (-80.0, 40.5), (-80.0, 40.6)]

    # main() with the fetch and the water step stubbed: what matters is
    # WHICH boundary reaches the run, and nothing else changed.
    _seen = []

    def _fake_dem(boundaries):
        return {"array": np.zeros((2, 2)), "resolution_meters": (5.0, 5.0)}

    def _fake_run(boundary, dem):
        _seen.append(list(boundary))
        raise SystemExit("stopped after boundary resolution")

    _real_dem_fn, _real_run_fn, _real_argv = diag.dem_over_both_boundaries, diag.run_water_step, None
    import sys

    _real_argv = sys.argv
    try:
        diag.dem_over_both_boundaries = _fake_dem
        diag.run_water_step = _fake_run

        _seen.clear()
        sys.argv = ["diagnose_water_survey_areas.py"]
        try:
            diag.main()
        except SystemExit:
            pass
        assert _seen == [HISTORICAL_REFERENCE_BOUNDARY], (
            f"with no --boundary the reference boundary runs, unchanged: {_seen}"
        )

        _seen.clear()
        sys.argv = ["diagnose_water_survey_areas.py", "--boundary", _path]
        try:
            diag.main()
        except SystemExit:
            pass
        assert _seen == [[(-80.1, 40.5), (-80.0, 40.5), (-80.0, 40.6)]], (
            f"a supplied boundary is the one that runs: {_seen}"
        )
    finally:
        diag.dem_over_both_boundaries = _real_dem_fn
        diag.run_water_step = _real_run_fn
        sys.argv = _real_argv
print(
    "3. --boundary: the default IS the historical reference boundary (unchanged invocations), a "
    "supplied JSON boundary is the one that runs, and the stream-corridor comparison boundary is "
    "pinned to the coordinates the zone was lost under."
)


# =========================================================================
# 4. THE BOUNDARY-STABILITY REPORT
# =========================================================================
# Hand-built runs, so the classification and the deltas are checked
# against known answers rather than against whatever the pipeline
# happened to produce.
def _zone(zone_id, survey_type, rank, geometry, criteria, mean, seed_blend=None, acres=1.0):
    zone = {
        "id": zone_id,
        "survey_type": survey_type,
        "rank": rank,
        "polygon_utm": geometry,
        "mean_suitability": mean,
        "zone_acres": acres,
        "criterion_contributions": {
            name: {"weight": 0.25, "mean_score": score, "weighted_contribution": 0.25 * score}
            for name, score in criteria.items()
        },
    }
    if seed_blend is not None:
        zone["seed_blend_score"] = seed_blend
    return zone


_shared_a = _zone(1, SURVEY_TYPE_EMBANKMENT, 1, box(0, 0, 100, 100), {"twi": 0.60, "slope": 0.80}, 0.70, 0.55)
# The same zone under the other boundary: a slightly larger envelope (a
# real clip difference), the SAME twi mean, a moved slope mean.
_shared_b = _zone(1, SURVEY_TYPE_EMBANKMENT, 1, box(0, 0, 110, 105), {"twi": 0.60, "slope": 0.75}, 0.68, 0.53)
_only_a = _zone(2, SURVEY_TYPE_EMBANKMENT, 2, box(500, 500, 560, 560), {"twi": 0.40, "slope": 0.50}, 0.45, 0.51)
_only_b = _zone(3, SURVEY_TYPE_EMBANKMENT, 2, box(900, 900, 960, 960), {"twi": 0.90, "slope": 0.60}, 0.75, 0.72)


def _fake_identify(zones, gated):
    return {"gate_mask_stats": {"gated_cells": gated}, "result": {"zones": zones}}


_runs = [
    ("reference", _fake_identify([_shared_a, _only_a], 800), None),
    ("comparison", _fake_identify([_shared_b, _only_b], 1500), None),
]

_matched, _left, _right = diag._match_zones([_shared_a, _only_a], [_shared_b, _only_b])
assert len(_matched) == 1 and _matched[0][0] is _shared_a and _matched[0][1] is _shared_b, (
    "the overlapping pair matches; the two disjoint zones do not"
)
assert _matched[0][2] > diag.ZONE_MATCH_MIN_IOU
assert _left == [_only_a] and _right == [_only_b], "each unmatched zone is reported under its own boundary"

# A pair whose overlap is under the IoU floor must NOT be called the
# same zone -- a loose match would hide a lost zone as a survivor.
_grazing = _zone(9, SURVEY_TYPE_EMBANKMENT, 1, box(95, 95, 300, 300), {"twi": 0.1, "slope": 0.1}, 0.2, 0.5)
assert diag._match_zones([_shared_a], [_grazing])[0] == [], "a grazing overlap is not a match"

# Cross-type pairs never match, however well they overlap: the two
# surfaces are kept separate end to end and are not comparable.
_excavated_twin = _zone(8, "excavated", 1, box(0, 0, 100, 100), {"twi": 0.60}, 0.70)
assert diag._match_zones([_shared_a], [_excavated_twin])[0] == [], (
    "an embankment zone and an excavated zone on the same ground are different answers, not one zone"
)

_report = diag.summarize_boundary_stability(_runs)
assert "SURVIVES BOTH: 1 zone(s)" in _report
assert "ONLY UNDER 'reference': 1 zone(s)" in _report
assert "ONLY UNDER 'comparison': 1 zone(s)" in _report
assert "800 gated cells" in _report and "1500 gated cells" in _report
# The per-criterion breakout: twi flat, slope moved, both reported with
# their deltas. The blend delta rides beside them.
assert "twi              0.6000 -> 0.6000  (delta +0.0000)" in _report, _report
assert "slope            0.8000 -> 0.7500  (delta -0.0500)" in _report, _report
assert "delta -0.0200" in _report, "the blend delta on the matched pair"
assert "seed blend       0.5500 -> 0.5300" in _report, "the seeding number is broken out for embankment"
assert "<- TWI moved" not in _report, "a flat TWI mean raises no flag"

# ...and when TWI DOES move on a matched pair, the report says so.
_moved = _zone(1, SURVEY_TYPE_EMBANKMENT, 1, box(0, 0, 110, 105), {"twi": 0.42, "slope": 0.75}, 0.68, 0.53)
_moved_report = diag.summarize_boundary_stability(
    [("reference", _fake_identify([_shared_a], 800), None), ("comparison", _fake_identify([_moved], 1500), None)]
)
assert "<- TWI moved; check the per-cell comparison above" in _moved_report

# One boundary is not a stability check, and the report says that rather
# than printing a clean-looking empty result.
_single = diag.summarize_boundary_stability([_runs[0]])
assert "ONLY ONE BOUNDARY IN THIS RUN" in _single and "needs two" in _single
print(
    "4. Boundary-stability report: survive-both / appear-in-one classified by envelope IoU (never "
    "across types, never on a grazing overlap), per-criterion and seed-blend deltas broken out, TWI "
    "movement flagged, and a single-boundary run reports that it could not run the check."
)


# =========================================================================
# 5. THE CALIBRATION, BEFORE/AFTER AND INDEPENDENT-SIGNAL SECTIONS
# =========================================================================
# These three run against the real two-boundary fixture above, not
# hand-built dicts: they read the pipeline's own arrays, and a section
# that silently stopped reading them would still print something.
def _as_run(result, boundary):
    """The underscore scaffolding run_water_step() attaches on the
    networked path, assembled here from the fixture -- and, like the real
    one, READING the run's own masks rather than rebuilding them."""
    return {
        "gate_mask_stats": result["gate_mask_stats"],
        "result": result,
        "_dem": TWO_BOUNDARY_DEM,
        "_dem_resolution_meters": TWO_BOUNDARY_DEM["resolution_meters"],
        "_road_cell_mask": result["road_cell_mask"],
        "_on_parcel_mask": result["on_parcel_mask"],
    }


_diag_runs = [
    ("small", _as_run(small, SMALL_BOUNDARY), None),
    ("large", _as_run(large, LARGE_BOUNDARY), None),
]

# --- calibration: the distribution under each boundary, and the
#     per-percentile AGREEMENT that makes the calibration itself
#     boundary-independent.
_calibration = diag.summarize_twi_calibration(_diag_runs)
assert f"0.0 at/below {wsa.TWI_SCORE_MIN_BREAKPOINT}" in _calibration
assert "874 gated cells with measured TWI" in _calibration
assert "AGREEMENT between 'small' and 'large'" in _calibration
assert "DEM resolution" in _calibration, "the resolution rides beside the distribution it calibrates"
_single_calibration = diag.summarize_twi_calibration([_diag_runs[0]])
assert "ONLY ONE BOUNDARY IN THIS RUN" in _single_calibration, (
    "one boundary cannot calibrate boundary-independently, and the instrument says so"
)

# --- before/after: the decisive line is the per-cell change across
#     boundaries. The absolute row must read exactly zero.
_comparison = diag.summarize_twi_scoring_comparison(_diag_runs)
assert "CELLS GATED UNDER BOTH (874)" in _comparison
assert "absolute curve:     mean 0.0000, max 0.0000" in _comparison, (
    f"the absolute curve must not move ANY cell between boundaries:\n{_comparison}"
)
assert "retired percentile: mean 0.3035, max 0.6356" in _comparison, (
    f"and the retired percentile's measured movement is stated, not asserted:\n{_comparison}"
)

# --- independent signal: evidence only. Every number is reported and
#     nothing is changed -- asserted by re-running the fixture and
#     checking the surfaces are untouched.
_before = small["surfaces"][SURVEY_TYPE_EMBANKMENT].copy()
_signal = diag.summarize_twi_independent_signal(_diag_runs[0][1], "small")
assert np.array_equal(_before, small["surfaces"][SURVEY_TYPE_EMBANKMENT]), (
    "the independent-signal report is a READING; it must not mutate the surfaces it reads"
)
assert "EVIDENCE ONLY, NOTHING CHANGED" in _signal
assert "Pearson" in _signal and "Spearman" in _signal
assert f"CLEARING SHARE at EMBANKMENT_SEED_MIN_SCORE ({EMBANKMENT_SEED_MIN_SCORE})" in _signal
assert "with TWI 466/874" in _signal and "without TWI 466/874" in _signal, (
    f"the with/without-TWI clearing share is measured on this fixture:\n{_signal}"
)
assert "embankment SEEDS:" in _signal and "lost," in _signal
assert "archetype" in _signal and "off-channel" in _signal
assert "no weight moved in this branch" in _signal

# The without-TWI surfaces redistribute proportionally and stay valid
# blends: weights still sum to 1.0, so the surface stays inside 0-1.
_criteria = small["surfaces"]["criteria"]
_wo_emb = diag._embankment_surface_without_twi(_criteria[SURVEY_TYPE_EMBANKMENT])
assert float(np.max(_wo_emb)) <= 1.0 + 1e-9 and float(np.min(_wo_emb)) >= -1e-9, (
    "removing TWI and renormalizing must leave a 0-1 blend, not a rescaled one"
)
_all_ones = {name: np.ones((2, 2)) for name in wsa.EMBANKMENT_WEIGHTS}
assert np.allclose(diag._embankment_surface_without_twi(_all_ones), 1.0), (
    "with every remaining criterion perfect the redistributed blend is exactly 1.0 -- the "
    "redistribution is proportional, and it conserves the total weight"
)
print(
    "5. Diagnostic sections: the calibration prints both distributions plus their per-percentile "
    "agreement (and refuses to calibrate from one boundary), the before/after measures the retired "
    "percentile moving cells 0.3035 on average while the absolute curve moves none, and the "
    "independent-signal report states correlations, clearing shares and seed archetypes without "
    "touching a surface."
)


# =========================================================================
# 6. THE SEED LADDER (the instrument EMBANKMENT_SEED_MIN_SCORE is tuned from)
# =========================================================================
# The seeding minimum dropped to 0.30 on the argument that downstream
# gates should do the filtering. That is a prediction, and the ladder is
# what tests it -- so the ladder itself has to be right about what
# became of each seed. Checked against the fixture's own zones rather
# than against the ladder's own words.
_ladder = diag.summarize_seed_ladder(_as_run(small, SMALL_BOUNDARY))
_seed_records = small["embankment_seeds"]
assert f"minimum {EMBANKMENT_SEED_MIN_SCORE}" in _ladder
assert f"{len(_seed_records)} seed(s) nominated; {len(_seed_records)} pinch walk(s) run" in _ladder, (
    "the cost line states seeds and walks -- equal by construction, because nothing is pre-pruned"
)
# One ladder row per seed, in blend-DESCENDING order.
_ranked = sorted(_seed_records, key=lambda r: r["blend_score"], reverse=True)
for _rank, _record in enumerate(_ranked, start=1):
    _row, _col = _record["rowcol"]
    assert f"  {_rank:>3}  {_record['blend_score']:.3f}  ({_row:>3},{_col:>3})" in _ladder, (
        f"seed {_record['rowcol']} must appear at ladder rank {_rank}, blend-descending"
    )

# EVERY OUTCOME CLASS IS DISTINGUISHED, and each is checked against the
# zone the seed actually produced -- a seed whose compartment was
# dropped afterwards is NOT a survivor, and the ladder must not read
# like one.
_zone_by_id = {z["id"]: z for z in small["zones"] + small["dropped_zones"]}
_survived = _dropped = _failed = 0
for _record in _seed_records:
    _bucket, _detail, _key = diag._seed_outcome(_record, _zone_by_id)
    if _record["status"] != wsa.SEED_STATUS_COMPARTMENT:
        assert _bucket == "failed" and _record["reason_code"] in _detail
        _failed += 1
        continue
    _zone = _zone_by_id[_record["zone_id"]]
    if _zone["status"] == wsa.ZONE_STATUS_DROPPED:
        assert _bucket == "dropped" and "DROPPED" in _detail and _zone["drop_reason"] in _detail
        assert _key.startswith("dropped:")
        _dropped += 1
    else:
        assert _bucket == "survived" and "SURVIVED" in _detail and _key == "survived"
        _survived += 1
    assert f"{_zone['compartment_footprint_acres']:.4f} ac" in _detail, "band acreage on the line"
    assert f"{_zone['zone_acres']:.4f} ac" in _detail, "and hull acreage beside it"
assert _survived == len(small["zones_by_type"][SURVEY_TYPE_EMBANKMENT]), (
    "the ladder's survivor count is the run's own surviving embankment zone count"
)
assert _survived and _failed, "this fixture exercises both a survivor and a failure"

# THE DROPPED CLASS, exercised directly since this fixture happens to
# produce none: a seed that BUILT a compartment which was then dropped
# must read as dropped, never as a survivor. This is the distinction the
# banded summary's "built" and "survived" columns turn on, so it is
# checked rather than assumed.
_built_then_dropped = {
    "status": wsa.SEED_STATUS_COMPARTMENT,
    "zone_id": 99,
    "rowcol": (4, 4),
    "blend_score": 0.31,
    "criteria_signature": {name: 0.5 for name in wsa.EMBANKMENT_WEIGHTS},
}
_floor_dropped_zone = {
    "id": 99,
    "status": wsa.ZONE_STATUS_DROPPED,
    "drop_reason": wsa.FLAG_BELOW_MIN_AREA,
    "compartment_footprint_acres": 0.0412,
    "zone_acres": 0.0688,
}
_bucket, _detail, _key = diag._seed_outcome(_built_then_dropped, {99: _floor_dropped_zone})
assert _bucket == "dropped" and _key == f"dropped:{wsa.FLAG_BELOW_MIN_AREA}"
assert "0.0412 ac" in _detail and "0.0688 ac" in _detail and wsa.FLAG_BELOW_MIN_AREA in _detail
assert "SURVIVED" not in _detail, "a compartment dropped after it was built is not a survivor"
# ...and the same seed whose compartment lost an OVERLAP dedupe collapses
# to the class, not to the winner's id.
_dedupe_dropped = dict(_floor_dropped_zone, drop_reason=wsa.duplicate_of_zone_reason(3))
assert diag._seed_outcome(_built_then_dropped, {99: _dedupe_dropped})[2] == "dropped:duplicate_of_zone"
# A seed whose zone is missing from the result degrades to a named
# outcome rather than raising.
assert diag._seed_outcome(_built_then_dropped, {})[0] == "compartment"

# A DEDUPE REASON NAMES ITS WINNER ON THE LINE AND ITS CLASS IN THE
# BAND, so one outcome class cannot be split across as many keys as
# there are winning zones.
assert diag._collapse_reason("duplicate_of_zone_7") == "duplicate_of_zone"
assert diag._collapse_reason(wsa.REASON_NO_CONSTRICTION) == wsa.REASON_NO_CONSTRICTION
assert diag._collapse_reason(None) == "unknown"

# THE BANDS: every seed lands in exactly one, the counts reconcile with
# the ladder, and a band wholly below the live minimum is labelled as
# such rather than left looking like ground that failed.
assert "BANDS (seeded / built a compartment / survived the floor):" in _ladder
_band_lines = [line.strip() for line in _ladder.split("\n") if "seeded" in line and ":" in line]
_band_lines = [line for line in _band_lines if line[0].isdigit()]
assert len(_band_lines) == len(diag.SEED_LADDER_BANDS), "one line per band, always printed"
_counted = 0
for _line in _band_lines:
    _counted += int(_line.split(":")[1].strip().split(" ")[0])
assert _counted == len(_seed_records), (
    f"every seed is counted in exactly one band ({_counted} banded vs {len(_seed_records)} seeded)"
)
assert f"{sum(1 for r in _seed_records if r['blend_score'] >= 0.50)} seeded" in _ladder

# A raised minimum must show as an EMPTY low band saying why, never as
# a silently missing row.
_high = diag.summarize_seed_ladder(_as_run(small, SMALL_BOUNDARY))
assert "(below the current minimum)" not in _high, "at 0.30 no band sits below the minimum"
_saved = wsa.EMBANKMENT_SEED_MIN_SCORE
try:
    wsa.EMBANKMENT_SEED_MIN_SCORE = 0.45
    diag.EMBANKMENT_SEED_MIN_SCORE = 0.45
    _raised = diag.summarize_seed_ladder(_as_run(small, SMALL_BOUNDARY))
    assert "0.30-0.35: 0 seeded  (below the current minimum)" in _raised, (
        f"a band under a raised minimum reads zero AND says why:\n{_raised}"
    )
finally:
    wsa.EMBANKMENT_SEED_MIN_SCORE = _saved
    diag.EMBANKMENT_SEED_MIN_SCORE = _saved

# An empty run says so instead of printing an empty table.
_no_seeds = _as_run(small, SMALL_BOUNDARY)
_no_seeds["result"] = dict(small, embankment_seeds=[])
assert "no gated cell reached" in diag.summarize_seed_ladder(_no_seeds)
print(
    f"6. Seed ladder: {len(_seed_records)} seeds, one row each in blend-descending order with "
    f"signature and outcome ({_survived} survived / {_dropped} dropped / {_failed} failed, each "
    "verified against the zone it produced), the cost line states seeds == walks, bands reconcile "
    "to the seed count, and a raised minimum shows as an empty band that says why."
)

print("\nAll TWI boundary-independence checks passed.")
