"""
test_keypoint_detection.py

Offline (no-network) checks for keypoint_detection.py. Every fixture is
synthetic; nothing here reproduces the real reference-property numbers
(there is no network, and the prototype figures in the task are context,
not targets). Script-style (module-level asserts + prints), run as
`python test_keypoint_detection.py`, matching this codebase's other tests.

Verification map (task's numbered list):
  1.  Raw vs filled profile: an enclosed depression manufactures a steep-to-
      flat break on the FILLED profile that the RAW profile does not, at the
      pit rim where fill depth is ~0 (so the fill-depth gate alone would miss
      it) -- the artifact fix #1 (raw profiling) exists to avoid. Shown at
      the detect level (no keypoint returned inside the depression) plus an
      inline raw-vs-filled contrast along the known stem.
  2.  Outlet-traced stem, not branch selection: a multi-tributary valley --
      the stem passes through the confluences, reaches the outlet, and is not
      equal to any single delineated branch.
  3.  Two-segment fit beats largest-drop: on a profile with a localized cliff
      above the true inflection, the fit finds the inflection and the largest
      single-step slope drop does not.
  4.  Slope-drop constraint: a profile whose best UNCONSTRAINED two-segment
      split has slope INCREASING downstream is rejected, and a legitimate
      slope-dropping split is chosen instead.
  5.  One per valley: a multi-tributary valley yields exactly one keypoint.
  6.  Boundary margin: keypoints 10 m and 20 m outside a boundary are kept
      and flagged; at 60 m the terrain's own split is inadmissible and is not
      chosen, and no returned keypoint ever sits past the margin.
  6b. The margin constrains the CHOICE, not just the result: with the
      terrain's best split outside it, the constrained fit returns the
      lowest-residual ADMISSIBLE split rather than nothing. And a valley with
      no stem cell within the margin is dropped before it is profiled,
      counted as rejected_valley_off_margin.
  7.  Short stem: a valley whose stem is shorter than 2*MIN_RUN+2 returns no
      keypoint, no exception.
  8.  Empty result is honest: a DEM with no primary valley returns [].
  9.  dem-only dependency: detect_keypoints needs no soil, canopy, road, or
      climate input, and the module imports without parcel_data.

Candidates -> filters -> selection (fix #6 in the module docstring):
  10. Regression: the global-argmin candidate is the split the retired
      single-split search returned (a frozen copy of it is the oracle); the
      filters are unchanged -- the survivor SET is byte-identical to the
      candidate-set commit's (d1f3ee2), synthetic and on the real reference
      terrain -- and only the selection within it moved.
  11. Fall-through: the bowl's best split is a fill artifact; the gate
      rejects it, a surviving split answers, the valley keeps a keypoint.
  12. All candidates rejected: no keypoint, and every candidate's reasons
      recorded.
  13. Candidate validity: no split violating the minimum-segment-length
      constraint ever enters the set.
  14. Highest-elevation selection: the highest survivor is selected over
      the residual-best one (stated), at the top edge of its block.
  15. Output contract: one keypoint per valley, and every field a
      downstream consumer reads is present and unchanged in type.
  16. Tie on elevation: the lowest residual wins, in every ordering.
  17. A single survivor is selected, whatever its elevation or residual.
"""

import inspect

import numpy as np
from shapely.geometry import Point, box

from feature_schema import validate_feature_collection
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    delineate_valleys,
    fill_depressions,
)
from raster_grid import pixel_center_xy
import keypoint_detection as kd
from keypoint_detection import (
    KEYPOINT_BOUNDARY_MARGIN_METERS,
    KEYPOINT_FILL_ARTIFACT_THRESHOLD_M,
    KEYPOINT_MIN_RUN_CELLS,
    KEYPOINT_MIN_SLOPE_DROP_PCT,
    KEYPOINT_PROFILE_SMOOTH_CELLS,
    detect_keypoints,
    keypoints_to_geojson,
    two_segment_keypoint_split,
)

CRS = "EPSG:32617"
RES = (5.0, 5.0)
ORIGIN_X = 500000.0
ORIGIN_Y = 4500000.0


def _dem(array):
    return {
        "array": np.asarray(array, dtype=np.float64),
        "resolution_meters": RES,
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _full_boundary(rows, cols):
    """A boundary that comfortably encloses the whole DEM grid."""
    return box(
        ORIGIN_X - 1000.0,
        ORIGIN_Y - rows * RES[1] - 1000.0,
        ORIGIN_X + cols * RES[0] + 1000.0,
        ORIGIN_Y + 1000.0,
    )


def _v_valley(rows, cols, profile_fn, cross=2.0):
    """Channel down the center column; profile_fn(r) sets the channel-floor
    elevation at row r, cross-slope adds |c - c0| * cross so flow converges to
    the channel."""
    array = np.zeros((rows, cols), dtype=np.float64)
    c0 = cols // 2
    for r in range(rows):
        base = profile_fn(r)
        for c in range(cols):
            array[r, c] = base + cross * abs(c - c0)
    return array


def _flow(dem):
    filled = fill_depressions(dem["array"])
    ftr, ftc = compute_flow_direction(filled, dem["resolution_meters"])
    facc = compute_flow_accumulation(filled, ftr, ftc)
    return filled, ftr, ftc, facc


def _build_slope_profile(slopes, step=5.0):
    """Elevation profile CONSISTENT with a given per-cell descent-slope
    percent list: elevation[i+1] = elevation[i] - slope_i/100 * step. Returns
    (distance, elevation, slope) all as float arrays -- the exact inputs
    two_segment_keypoint_split() consumes, with no smoothing applied (the
    caller supplies the already-smoothed slopes)."""
    n = len(slopes) + 1
    distance = np.arange(n, dtype=float) * step
    elevation = np.empty(n, dtype=float)
    elevation[0] = 100.0
    for i, s in enumerate(slopes):
        elevation[i + 1] = elevation[i] - s / 100.0 * step
    return distance, elevation, np.asarray(slopes, dtype=float)


# ============================================================================
# Constants are the calibrated values (a change here should be a deliberate
# retune, not an accident).
# ============================================================================
assert KEYPOINT_PROFILE_SMOOTH_CELLS == 5
assert KEYPOINT_MIN_RUN_CELLS == 6
assert KEYPOINT_MIN_SLOPE_DROP_PCT == 3.0
assert KEYPOINT_FILL_ARTIFACT_THRESHOLD_M == 0.15
assert KEYPOINT_BOUNDARY_MARGIN_METERS == 25.0
print("Constants are the calibrated reference values.")


# ============================================================================
# TEST 1 -- Raw vs filled profile (failure mode #1, executable).
#
# A steep upper slope descending into an enclosed BOWL near the outlet. On the
# RAW profile the steepest-to-gentle break sits on real landform above the
# bowl; the bowl bottom is the lowest ground (deeply filled) so any break
# there is caught by the fill-depth gate. On the FILLED profile the bowl reads
# as a dead-flat plateau, and the steep-to-flat break lands at the plateau
# ENTRANCE (the rim), where fill depth is ~0 -- so the fill-depth gate alone
# would NOT catch it. Profiling RAW is what prevents that false keypoint.
#
# The raw-vs-filled contrast is shown by profiling the KNOWN center-column
# stem directly, which is what exposes the artifact deterministically.
#
# EXPECTATIONS CORRECTED BY THE EPSILON-FILL BRANCH, and why. This block
# used to assert that detect_keypoints() returns a keypoint here, at
# (22, 7). It no longer does, and the change is entirely explained by
# routing that previously died:
#
#   BEFORE. valley_delineation.fill_depressions() was the plain
#   priority-flood, so the filled bowl was dead level, every one of its
#   cells took compute_flow_direction()'s -1 sentinel, and the stem tracer
#   STOPPED AT THE BOWL RIM. The profile it fitted therefore covered only
#   the steep upper reach and the rim: the fit's break landed at (22, 7)
#   on real landform, with fill depth 0.0 m, and the fill-artifact gate
#   never fired (rejected_fill_artifact = 0). The gate was not doing its
#   job here -- the TRUNCATION was, accidentally, by keeping the artifact
#   ground out of reach.
#
#   AFTER. The epsilon fill gives every bowl cell a direction, so the stem
#   now runs through the depression to the valley outlet, exactly as
#   intended. The raw profile it fits now contains the bowl's own 12 m
#   plunge, which is a far larger slope drop than the real steep-to-gentle
#   break at row 24 -- so the two-segment fit selects the bowl, and the
#   fill-artifact gate REJECTS it (rejected_fill_artifact = 1). The gate is
#   doing more work than before, not less, and it is doing exactly the work
#   it was written for.
#
#   THE CONSEQUENCE, REPORTED NOT FIXED. detect_keypoints() emits at most
#   ONE keypoint per valley, so a single gate rejection now costs this
#   valley its keypoint entirely, where the old truncation quietly handed
#   back the upper-reach break instead. On a valley whose stem crosses a
#   marsh that is a real behaviour change: the honest answer ("the
#   strongest break on this stem is fill artifact") replaces a break found
#   only because the stem was cut short. Whether the fit should be
#   restricted to non-filled ground, or a rejected split should fall back
#   to the next candidate, is DOWNSTREAM TUNING and belongs to a later
#   branch -- flagged here, not acted on.
# ============================================================================
_rows1, _cols1 = 40, 15
_c0_1 = _cols1 // 2


def _profile1(r):
    return 200.0 - 4.0 * r if r <= 24 else 200.0 - 4.0 * 24 - 0.5 * (r - 24)


_arr1 = _v_valley(_rows1, _cols1, _profile1, cross=2.0)
# Carve an enclosed bowl on the lower reach (rows 28-36, center columns).
_BOWL_ROWS = range(28, 37)
for _r in _BOWL_ROWS:
    for _c in range(_c0_1 - 2, _c0_1 + 3):
        _arr1[_r, _c] -= 12.0
_dem1 = _dem(_arr1)

#
#   AND THEN FALL-THROUGH. The candidates -> filters -> selection branch
#   is that later branch's second option: the fill gate now rejects the
#   bowl's splits one by one rather than the valley, and the best split on
#   real ground answers. TEST 11 asserts the mechanism in full. Under the
#   best-residual selection that answer was (27, 7): the last unfilled cell
#   above the bowl, its RIM, three rows below the true break at row 24,
#   because the residual surface itself is shaped by the depression and
#   falls steadily toward it. That contamination is unchanged -- (27, 7) is
#   still the best-residual survivor, and scoping the fit to unfilled ground
#   is still its own branch.
#
#   AND THEN THE HIGHEST SURVIVOR. The keypoint is now the highest of the
#   survivors (rows 17-27, one contiguous block), so the valley answers
#   (17, 7) -- the top edge of that block, the cell directly below the first
#   slope-drop rejection, and seven rows ABOVE the true break. The rim is no
#   longer selected; the edge-cell consequence recorded at
#   select_keypoint_candidate() is what replaced it.
#
# detect (RAW, the real path). What must hold, whatever the valley returns,
# is that nothing inside the depression is ever RETURNED: the fill gate is
# what guarantees that.
_diag1 = {}
_kps1 = detect_keypoints(_dem1, _full_boundary(_rows1, _cols1), diagnostics=_diag1)
for _k in _kps1:
    assert _k["rowcol"][0] not in _BOWL_ROWS, (
        f"RAW profiling must not return a keypoint inside the depression, got {_k['rowcol']}"
    )
assert [tuple(_k["rowcol"]) for _k in _kps1] == [(17, 7)], (
    "the fill gate rejects the bowl's splits and the highest split on real ground answers -- expected "
    f"(17, 7), got {[k['rowcol'] for k in _kps1]}"
)
assert _diag1["valleys"] == 1, _diag1
assert _diag1["rejected_fill_artifact"] == 0 and _diag1["fill_fall_through_valleys"] == 1, (
    f"the valley is no longer lost to the fill gate; it falls through instead -- {_diag1}"
)
assert _diag1["candidate_rejections"]["fill_artifact"] > 0, _diag1
print(
    f"Test 1: detect (raw) never returns a cell inside the bowl. The stem crosses it (the epsilon fill "
    f"routes it), the fill gate rejects {_diag1['candidate_rejections']['fill_artifact']} bowl splits, "
    f"and the valley falls through to {tuple(_kps1[0]['rowcol'])} -- the top of its real-ground block "
    f"(see TEST 11). Under the plain fill the tracer stopped at the rim and returned (22, 7); under the "
    f"single-split search it returned nothing; under best-residual selection, the rim (27, 7)."
)

# Inline raw-vs-filled contrast along the known center-column stem.
_filled1 = fill_depressions(_dem1["array"])
_stem1 = [(r, _c0_1) for r in range(_rows1)]
_dist1, _elev_raw, _slope_raw = kd._profile_along_stem(
    _stem1, _dem1["array"], _dem1, KEYPOINT_PROFILE_SMOOTH_CELLS
)
_, _elev_fill, _slope_fill = kd._profile_along_stem(
    _stem1, _filled1, _dem1, KEYPOINT_PROFILE_SMOOTH_CELLS
)
_split_raw = two_segment_keypoint_split(
    _dist1, _elev_raw, _slope_raw, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
)
_split_fill = two_segment_keypoint_split(
    _dist1, _elev_fill, _slope_fill, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
)
_k_raw, _k_fill = _split_raw[0], _split_fill[0]
_filldepth_raw = float(_filled1[_stem1[_k_raw]]) - float(_dem1["array"][_stem1[_k_raw]])
_filldepth_fill = float(_filled1[_stem1[_k_fill]]) - float(_dem1["array"][_stem1[_k_fill]])

assert _k_raw != _k_fill, "raw and filled profiling must place the break at different stem positions"
assert _filldepth_fill <= KEYPOINT_FILL_ARTIFACT_THRESHOLD_M, (
    "the FILLED break sits at the pit rim with ~0 fill depth -- it would sail through the fill gate "
    "(exactly the artifact fix #1 prevents)"
)
assert _filldepth_raw > KEYPOINT_FILL_ARTIFACT_THRESHOLD_M, (
    "the RAW break sits at the bowl bottom, deep in filled ground -- caught by the fill gate"
)
print(
    f"Test 1: filled profiling places the break at stem index {_k_fill} (rim, fill depth "
    f"{_filldepth_fill:.2f} m -- would pass the fill gate); raw profiling places it at index {_k_raw} "
    f"(bowl bottom, fill depth {_filldepth_raw:.2f} m -- rejected). Profiling raw avoids the artifact."
)


# ============================================================================
# TEST 2 -- Outlet-traced stem, not branch selection (failure mode #2).
#
# A dendritic valley with two tributaries converging into a shared trunk. The
# traced main stem must reach the valley outlet, pass through the confluence
# cells (shared by more than one branch), and NOT equal any single delineated
# branch.
# ============================================================================
_rows2, _cols2 = 44, 31
_c0_2 = _cols2 // 2
_arr2 = np.full((_rows2, _cols2), 500.0, dtype=np.float64)
for _r in range(_rows2):
    for _c in range(_cols2):
        if _r < 22:
            _left = _c0_2 - (22 - _r)
            _right = _c0_2 + (22 - _r)
            _d = min(abs(_c - _left), abs(_c - _right))
        else:
            _d = abs(_c - _c0_2)
        _arr2[_r, _c] = 300.0 - 3.0 * _r + 2.5 * _d
_dem2 = _dem(_arr2)
_valleys2 = delineate_valleys(_dem2)
assert len(_valleys2) == 1, f"expected a single dendritic valley, got {len(_valleys2)}"
_valley2 = _valleys2[0]
assert len(_valley2["branches_rowcol"]) >= 2, "the dendritic valley must decompose into multiple branches"

_filled2, _ftr2, _ftc2, _facc2 = _flow(_dem2)
_upstream2 = kd.build_upstream_map(_ftr2, _ftc2)
_stem2 = kd.trace_stem_from_outlet(_valley2, _upstream2, _facc2)

# outlet = the highest-accumulation branch endpoint
_endpoints2 = [b[-1] for b in _valley2["branches_rowcol"]]
_outlet2 = max(_endpoints2, key=lambda cell: (float(_facc2[cell]), -cell[0], -cell[1]))
assert _stem2[-1] == _outlet2, "the stem must terminate at the valley outlet"

# confluence cells = cells shared by >= 2 branches
from collections import Counter

_cell_counts2 = Counter()
for _b in _valley2["branches_rowcol"]:
    for _cell in _b:
        _cell_counts2[_cell] += 1
_confluence_cells2 = {cell for cell, n in _cell_counts2.items() if n >= 2}
_stem_set2 = set(_stem2)
assert _confluence_cells2, "the dendritic valley must have confluence cells"
assert _stem_set2 & _confluence_cells2, "the stem must pass through the confluence(s)"

# not equal to any single branch, and longer than the longest one (it spans the whole main stem)
_branch_sets2 = [set(b) for b in _valley2["branches_rowcol"]]
assert all(_stem_set2 != bs for bs in _branch_sets2), "the stem must not equal any single branch"
assert len(_stem2) > max(len(b) for b in _valley2["branches_rowcol"]), (
    "the traced stem must be longer than the longest single branch -- it is the whole main stem"
)
print(
    f"Test 2: stem of {len(_stem2)} cells reaches the outlet {_outlet2}, passes through "
    f"{len(_stem_set2 & _confluence_cells2)} confluence cell(s), and equals no single branch "
    f"(branch lengths {[len(b) for b in _valley2['branches_rowcol']]})."
)


# ============================================================================
# TEST 3 -- Two-segment fit beats largest-drop (failure mode #3).
#
# A steep upper reach (18%) with a localized CLIFF (40% for 3 cells) high up,
# then a gentle lower reach (5%). The true inflection is the 18% -> 5% break;
# the largest single-step slope drop is at the cliff, well above it.
# ============================================================================
_slopes3 = [18] * 10 + [40] * 3 + [18] * 10 + [5] * 16  # cliff at 10-12; true inflection at k=23
_dist3, _elev3, _slope3 = _build_slope_profile(_slopes3)
_TRUE_INFLECTION_3 = 23

_fit3 = two_segment_keypoint_split(
    _dist3, _elev3, _slope3, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
)
assert _fit3 is not None
_fit_k3 = _fit3[0]

# largest absolute single-step slope drop (slope_i - slope_{i+1}), computed inline
_single_step_drops3 = _slope3[:-1] - _slope3[1:]
_largest_drop_k3 = int(np.argmax(_single_step_drops3))

assert abs(_fit_k3 - _TRUE_INFLECTION_3) <= 1, (
    f"the two-segment fit must land on the true inflection (~{_TRUE_INFLECTION_3}), got {_fit_k3}"
)
assert abs(_largest_drop_k3 - 11) <= 1, (
    f"the largest single-step drop must land on the cliff (~11), got {_largest_drop_k3}"
)
assert _fit_k3 != _largest_drop_k3, "the two methods must disagree here"
print(
    f"Test 3: two-segment fit -> stem index {_fit_k3} (the true inflection); "
    f"largest-absolute-drop -> stem index {_largest_drop_k3} (the cliff). "
    f"They differ because the fit treats the keypoint as a global property of the whole profile "
    f"(best split into a steep line and a gentle one), while largest-drop chases the sharpest local "
    f"step -- the cliff -- which is not the landform break."
)


# ============================================================================
# TEST 4 -- Slope-drop constraint (failure mode #4).
#
# A long gentle upper reach (6%), then a steep reach (34%), then a short gentle
# tail (6%). The globally best UNCONSTRAINED two-segment split sits at the
# gentle->steep corner, where slope INCREASES downstream -- the opposite of a
# keypoint. The constraint rejects it and picks a legitimate slope-dropping
# split instead.
# ============================================================================
_slopes4 = [6] * 16 + [34] * 14 + [6] * 7
_dist4, _elev4, _slope4 = _build_slope_profile(_slopes4)

_unconstrained4 = two_segment_keypoint_split(
    _dist4, _elev4, _slope4, KEYPOINT_MIN_RUN_CELLS, -1.0e9  # slope-drop gate effectively off
)
_constrained4 = two_segment_keypoint_split(
    _dist4, _elev4, _slope4, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
)
assert _unconstrained4 is not None and _constrained4 is not None
_unc_k4, _unc_above4, _unc_below4 = _unconstrained4[0], _unconstrained4[1], _unconstrained4[2]
_con_k4, _con_above4, _con_below4 = _constrained4[0], _constrained4[1], _constrained4[2]

assert _unc_above4 < _unc_below4, (
    "the unconstrained best split must have slope INCREASING downstream (a non-keypoint), "
    f"got above={_unc_above4:.1f}% below={_unc_below4:.1f}%"
)
assert _con_above4 > _con_below4, (
    "the constrained split must have slope DROPPING downstream (a real inflection), "
    f"got above={_con_above4:.1f}% below={_con_below4:.1f}%"
)
assert _con_above4 - _con_below4 >= KEYPOINT_MIN_SLOPE_DROP_PCT
assert _con_k4 != _unc_k4, "the constraint must move the chosen split off the increasing-slope corner"
print(
    f"Test 4: unconstrained best split at index {_unc_k4} has slope INCREASING "
    f"({_unc_above4:.1f}% -> {_unc_below4:.1f}%) and is rejected; the constrained search picks index "
    f"{_con_k4} with slope dropping ({_con_above4:.1f}% -> {_con_below4:.1f}%)."
)


# ============================================================================
# TEST 5 -- One keypoint per valley.
# Reuses the dendritic multi-tributary valley from test 2.
# ============================================================================
_kps5 = detect_keypoints(_dem2, _full_boundary(_rows2, _cols2))
assert len(_kps5) == 1, f"a single multi-tributary valley must yield exactly one keypoint, got {len(_kps5)}"
_kp5 = _kps5[0]
# The returned dict carries every field the return-shape contract names.
_EXPECTED_KEYS = {
    "id", "valley_id", "rowcol", "point_utm", "geometry_wgs84", "elevation_m",
    "contributing_acres", "slope_above_pct", "slope_below_pct", "slope_drop_pct",
    "stem_length_cells", "position_along_stem", "on_parcel",
    "distance_outside_boundary_m", "confidence", "confidence_notes",
}
assert set(_kp5) == _EXPECTED_KEYS, f"unexpected keypoint keys: {set(_kp5) ^ _EXPECTED_KEYS}"
assert isinstance(_kp5["point_utm"], Point)
assert _kp5["geometry_wgs84"]["type"] == "Point"
assert _kp5["slope_above_pct"] > _kp5["slope_below_pct"]
# GeoJSON wrapping is schema-conformant.
_fc5 = keypoints_to_geojson(_kps5)
validate_feature_collection(_fc5)
assert _fc5["features"][0]["properties"]["layer"] == "keypoint"
print(
    f"Test 5: the multi-tributary valley yields exactly one keypoint (valley {_kp5['valley_id']}, "
    f"drop {_kp5['slope_drop_pct']}%), and its GeoJSON is schema-conformant."
)


# ============================================================================
# TEST 6 -- Boundary margin: 10 m and 20 m kept and flagged; at 60 m the
# terrain's own split is never chosen, and nothing outside the margin is
# EVER returned.
#
# The unconstrained keypoint location is fixed by terrain. Find it once, then
# place boundaries whose nearest edge is 10 m, 20 m, and 60 m from it.
#
# FIXTURE CORRECTED BY THE EPSILON-FILL BRANCH. This test used to borrow
# TEST 1's DEM, which no longer yields a keypoint at all: the epsilon fill
# routes the stem through TEST 1's bowl and the fill-artifact gate rejects
# the resulting break (see TEST 1's own note). The dependence was always
# incidental -- this test is about the MARGIN gate and needs only some
# valley with a keypoint in it -- so it now builds TEST 1's DEM WITHOUT the
# bowl: the same profile and cross-section, pure landform, whose keypoint
# lands at (24, 7), the profile's true steep-to-gentle inflection at row 24.
#
# EXPECTATION CORRECTED BY THE MARGIN-CONSTRAINS-THE-CHOICE BRANCH. The 60 m
# case used to assert an EMPTY result: the fit picked (24, 7) regardless of
# the boundary and the margin then threw the whole valley away. The margin is
# now part of the choice, so the valley instead offers its best split from
# among the admissible ones -- here (17, 7), sitting at exactly the 25 m
# margin. The INVARIANT the gate exists for is unchanged and is what is
# asserted now: nothing further than the margin is ever returned. The old
# assertion tested the implementation's order of operations, not that
# invariant, which is why it moved.
#
# ANCHOR MOVED BY THE HIGHEST-SURVIVOR BRANCH. With the whole grid inside the
# boundary the detector now selects (17, 7) -- the highest of the fifteen
# surviving splits, rows 17-31 -- not the fit's (24, 7) (TEST 14 is that
# change). The margin behaviour is anchored on whatever the unconstrained
# selection is, so the boundaries now lie SOUTH of (17, 7): at 10 m and 20 m
# it is kept and flagged; at 60 m it is inadmissible, and the highest
# admissible survivor answers instead. (24, 7), the fit's own split, stays
# named as _P6 because TEST 6b's fit-level checks are anchored on it.
# ============================================================================
_arr6 = _v_valley(_rows1, _cols1, _profile1, cross=2.0)
_dem6 = _dem(_arr6)
_kps6_full = detect_keypoints(_dem6, _full_boundary(_rows1, _cols1))
assert len(_kps6_full) == 1, [k["rowcol"] for k in _kps6_full]
_kp6 = _kps6_full[0]
assert tuple(_kp6["rowcol"]) == (17, 7), _kp6["rowcol"]
_P6_TOP = _kp6["point_utm"]
_P6 = Point(*pixel_center_xy(_dem6, 24, 7))
_kept6 = {}
for _offset in (10.0, 20.0, 60.0):
    # A box lying entirely to the SOUTH of the selection, its northern edge _offset metres away.
    _bnd = box(_P6_TOP.x - 100.0, _P6_TOP.y - _offset - 300.0, _P6_TOP.x + 100.0, _P6_TOP.y - _offset)
    assert not _bnd.contains(_P6_TOP)
    _res = detect_keypoints(_dem6, _bnd)
    _kept6[_offset] = _res

# Inside the margin: the unconstrained selection is returned, flagged and measured.
assert len(_kept6[10.0]) == 1 and _kept6[10.0][0]["on_parcel"] is False
assert tuple(_kept6[10.0][0]["rowcol"]) == (17, 7)
assert abs(_kept6[10.0][0]["distance_outside_boundary_m"] - 10.0) < 0.01
assert len(_kept6[20.0]) == 1 and _kept6[20.0][0]["on_parcel"] is False
assert tuple(_kept6[20.0][0]["rowcol"]) == (17, 7)
assert abs(_kept6[20.0][0]["distance_outside_boundary_m"] - 20.0) < 0.01

# Beyond it: (17, 7) is NOT returned, and whatever is returned is inside the
# margin -- the gate's actual guarantee, asserted over every case at once.
assert all(
    tuple(_k["rowcol"]) != (17, 7) for _k in _kept6[60.0]
), "the split 60 m outside the boundary must never be the one chosen"
for _offset, _res in _kept6.items():
    for _k in _res:
        assert _k["distance_outside_boundary_m"] <= KEYPOINT_BOUNDARY_MARGIN_METERS + 1e-9, (
            f"boundary {_offset} m away returned a keypoint "
            f"{_k['distance_outside_boundary_m']} m outside, past the "
            f"{KEYPOINT_BOUNDARY_MARGIN_METERS} m margin"
        )
_far6 = _kept6[60.0][0]
print(
    f"Test 6: on the bowl-free landform DEM the unconstrained keypoint lands at {tuple(_kp6['rowcol'])} "
    "(the highest survivor). Kept at 10 m outside (on_parcel=False, distance="
    f"{_kept6[10.0][0]['distance_outside_boundary_m']} m) and at 20 m outside (distance="
    f"{_kept6[20.0][0]['distance_outside_boundary_m']} m). With the boundary 60 m away row 17 is "
    f"inadmissible and is not chosen; the valley offers {tuple(_far6['rowcol'])} instead, at "
    f"{_far6['distance_outside_boundary_m']} m -- inside the {KEYPOINT_BOUNDARY_MARGIN_METERS} m margin, "
    "which no returned keypoint in any case exceeds."
)


# ============================================================================
# TEST 6b -- The margin constrains the CHOICE, and a valley wholly outside it
# is dropped before it is ever profiled.
#
# TWO behaviours, one fixture, because they are two halves of one rule.
#
# (a) THE CHOICE. A boundary is placed so that the terrain's best split
#     (24, 7) is outside the margin but admissible splits remain. The
#     unconstrained fit and the constrained fit must disagree, and the
#     constrained answer must be the best-fitting ADMISSIBLE split -- not
#     merely some admissible split. Checked against two_segment_keypoint_split
#     run directly over the same profile, so the assertion is on the choice
#     rule itself and not on a hard-coded row.
#
# (b) THE VALLEY GATE. A boundary far enough away that NO stem cell is within
#     the margin returns nothing, and says so as rejected_valley_off_margin
#     rather than as rejected_off_margin: delineate_valleys() runs on the
#     buffered DEM, so a valley in the window that never comes near the drawn
#     boundary has nothing to say about this property, and the diagnostics
#     distinguish "not this parcel's valley" from "this parcel's valley, but
#     its inflections are all too far out".
# ============================================================================
_stem6b = kd.trace_stem_from_outlet(
    delineate_valleys(_dem6)[0],
    kd.build_upstream_map(*_flow(_dem6)[1:3]),
    _flow(_dem6)[3],
)
_dist6b, _elev6b, _slope6b = kd._profile_along_stem(
    _stem6b, _dem6["array"], _dem6, KEYPOINT_PROFILE_SMOOTH_CELLS
)
_bnd6b = box(_P6.x - 100.0, _P6.y + 60.0, _P6.x + 100.0, _P6.y + 360.0)
_margin6b = kd._stem_boundary_margin(_stem6b, _dem6, _bnd6b)


def _admissible6b(index):
    _point, _on, _out = _margin6b[index]
    return _on or _out <= KEYPOINT_BOUNDARY_MARGIN_METERS


_free6b = two_segment_keypoint_split(
    _dist6b, _elev6b, _slope6b, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
)
_held6b = two_segment_keypoint_split(
    _dist6b, _elev6b, _slope6b, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT,
    position_is_eligible=_admissible6b,
)
assert _free6b is not None and _held6b is not None
assert _free6b[0] != _held6b[0], "the fixture must make the two fits disagree, or it proves nothing"
assert not _admissible6b(_free6b[0]), "the unconstrained winner must be the inadmissible one"
assert _admissible6b(_held6b[0])
# The constrained winner is the LOWEST-residual admissible split, not just any.
_best_admissible6b = min(
    (
        two_segment_keypoint_split(
            _dist6b, _elev6b, _slope6b, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT,
            position_is_eligible=lambda i, _k=_k6: i == _k,
        )
        for _k6 in range(len(_elev6b))
        if _admissible6b(_k6)
    ),
    key=lambda _s: float("inf") if _s is None else _s[4],
)
assert _held6b[0] == _best_admissible6b[0], (
    f"constrained fit chose split {_held6b[0]} (residual {_held6b[4]:.4f}) but the best "
    f"admissible split is {_best_admissible6b[0]} (residual {_best_admissible6b[4]:.4f})"
)
# detect_keypoints() selects the HIGHEST admissible survivor, not the fit's
# answer; on this boundary the only admissible survivor is the fit's
# constrained split, so the two coincide -- asserted as such, not as the
# selection rule.
_det6b = {}
_kps6b = detect_keypoints(_dem6, _bnd6b, diagnostics=_det6b)
_surv6b = [_c for _c in _det6b["valley_candidates"][0]["candidates"] if not _c["rejected_by"]]
assert [_c["index"] for _c in _surv6b] == [_held6b[0]], [_c["index"] for _c in _surv6b]
assert tuple(_stem6b[_held6b[0]]) == tuple(_kps6b[0]["rowcol"])

_diag6b = {}
_far_boundary6b = box(_P6.x - 100.0, _P6.y + 500.0, _P6.x + 100.0, _P6.y + 800.0)
_none6b = detect_keypoints(_dem6, _far_boundary6b, diagnostics=_diag6b)
assert _none6b == [], _none6b
assert _diag6b["rejected_valley_off_margin"] == 1, _diag6b
assert _diag6b["rejected_off_margin"] == 0, (
    "a valley with no stem cell near the boundary is dropped as a valley, not as a split: "
    f"{_diag6b}"
)
assert _diag6b["rejected_no_slope_drop"] == 0, _diag6b
print(
    f"Test 6b: with the boundary 60 m out the unconstrained fit picks split {_free6b[0]} "
    f"(inadmissible, {_margin6b[_free6b[0]][2]:.1f} m outside) and the constrained fit picks "
    f"{_held6b[0]} -- the lowest-residual admissible split of "
    f"{sum(1 for _i in range(len(_elev6b)) if _admissible6b(_i))} cells within the margin, and the "
    f"only admissible survivor, so the cell detect_keypoints returns. With the boundary 500 m out no stem cell is within the margin: "
    f"[] returned, counted as rejected_valley_off_margin=1 (off_margin=0, no_slope_drop=0)."
)


# ============================================================================
# TEST 7 -- Short stem returns no keypoint, no exception.
#
# A 324-cell primary valley inherently traces a stem far longer than the
# profile window (its main flow path spans the whole concentration area), so a
# genuinely short stem is exercised on a small DEM whose (supplied) valley
# traces a stem below 2*MIN_RUN+2 cells. The gate must reject it cleanly.
# ============================================================================
_rows7, _cols7 = 7, 11
_c0_7 = _cols7 // 2
_arr7 = _v_valley(_rows7, _cols7, lambda r: 100.0 - 2.0 * r, cross=3.0)
_dem7 = _dem(_arr7)
_, _ftr7, _ftc7, _facc7 = _flow(_dem7)
_short_branch = [(r, _c0_7) for r in range(_rows7)]
_short_valley = {
    "id": 0,
    "max_contributing_area_acres": 2.5,
    "branches_rowcol": [_short_branch],
    "branches_utm": [],
    "geometry_wgs84": {"type": "LineString", "coordinates": []},
}
_stem7 = kd.trace_stem_from_outlet(
    _short_valley, kd.build_upstream_map(_ftr7, _ftc7), _facc7
)
_min_stem = 2 * KEYPOINT_MIN_RUN_CELLS + 2
assert len(_stem7) < _min_stem, f"fixture stem must be shorter than {_min_stem}, got {len(_stem7)}"
_diag7 = {}
_kps7 = detect_keypoints(_dem7, _full_boundary(_rows7, _cols7), valleys=[_short_valley], diagnostics=_diag7)
assert _kps7 == [], "a stem shorter than the profile window must yield no keypoint"
assert _diag7["rejected_short_stem"] == 1
print(
    f"Test 7: a {len(_stem7)}-cell stem (< {_min_stem}) is rejected as too short -- no keypoint, "
    "no exception."
)


# ============================================================================
# TEST 8 -- Empty result is honest: a DEM with no primary valley returns [].
# A planar slope: flow runs straight down each column with no concentration,
# so nothing clears the primary-valley threshold.
# ============================================================================
_rows8, _cols8 = 30, 20
_arr8 = np.zeros((_rows8, _cols8), dtype=np.float64)
for _r in range(_rows8):
    _arr8[_r, :] = 100.0 - 2.0 * _r
_dem8 = _dem(_arr8)
assert delineate_valleys(_dem8) == [], "the planar slope must have no primary valley"
_kps8 = detect_keypoints(_dem8, _full_boundary(_rows8, _cols8))
assert _kps8 == [], "no primary valley must yield an empty keypoint list, not a placeholder"
assert keypoints_to_geojson(_kps8) == {"type": "FeatureCollection", "features": []}
print("Test 8: a DEM with no primary valley returns an empty keypoint list (honest empty answer).")


# ============================================================================
# TEST 9 -- dem-only dependency: no soil/canopy/road/climate/parcel_data.
# ============================================================================
_detect_params = inspect.signature(detect_keypoints).parameters
_FORBIDDEN = ("parcel_data", "canopy", "soil", "road", "climate")
for _name in _detect_params:
    for _f in _FORBIDDEN:
        assert _f not in _name.lower(), f"detect_keypoints must not take a {_f} parameter, found {_name}"
# The only required positional inputs are the DEM and the boundary polygon.
_required = [
    n for n, p in _detect_params.items()
    if p.default is inspect._empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
]
assert _required == ["dem", "boundary_polygon_utm"], f"unexpected required params: {_required}"

# identify_keypoints (the network entry point) likewise takes no such input.
_identify_params = inspect.signature(kd.identify_keypoints).parameters
for _name in _identify_params:
    for _f in ("parcel_data", "soil", "road", "climate", "canopy"):
        assert _f not in _name.lower(), f"identify_keypoints must not take a {_f} parameter, found {_name}"

# And it genuinely RUNS on a DEM + boundary alone, with no ParcelData anywhere.
_kps9 = detect_keypoints(_dem2, _full_boundary(_rows2, _cols2))
assert isinstance(_kps9, list) and len(_kps9) == 1
print(
    "Test 9: detect_keypoints requires only (dem, boundary_polygon_utm) -- no soil, canopy, road, "
    "or climate input -- and runs with no ParcelData."
)


# ============================================================================
# CANDIDATES -> FILTERS -> SELECTION (fix #6 in the module docstring).
#
# THE RETIRED SEARCH, FROZEN AS THE ORACLE. This is two_segment_keypoint_
# split() exactly as it stood at 1af9c3e, before candidate generation
# replaced it: one pass, filters applied inline, only the running best kept.
# It is copied here rather than reached through the module so the
# regression below compares the new mechanism against the OLD one, not
# against itself.
# ============================================================================
def _retired_two_segment_split(distance, elevation, slope_pct, min_run_cells, min_slope_drop_pct,
                               position_is_eligible=None):
    n = len(elevation)
    best = None
    for k in range(min_run_cells, n - min_run_cells):
        slope_above = float(np.mean(slope_pct[k - min_run_cells:k]))
        slope_below = float(np.mean(slope_pct[k:k + min_run_cells]))
        slope_drop = slope_above - slope_below
        if slope_drop < min_slope_drop_pct:
            continue
        if position_is_eligible is not None and not position_is_eligible(k):
            continue
        residual = (
            kd._line_residual_sum_of_squares(distance[:k + 1], elevation[:k + 1])
            + kd._line_residual_sum_of_squares(distance[k:], elevation[k:])
        )
        if best is None or residual < best[4]:
            best = (k, slope_above, slope_below, slope_drop, residual)
    return best


def _stem_profile(dem):
    """The single valley's traced stem and its RAW profile, as detect uses them."""
    _filled, _ftr, _ftc, _facc = _flow(dem)
    _stem = kd.trace_stem_from_outlet(delineate_valleys(dem)[0], kd.build_upstream_map(_ftr, _ftc), _facc)
    return _stem, kd._profile_along_stem(_stem, dem["array"], dem, KEYPOINT_PROFILE_SMOOTH_CELLS)


# ============================================================================
# TEST 10 -- Regression: generation changed how many splits are considered,
# not what a qualifying split IS; the selection rule is the only thing that
# moved.
#
# (a) On the bowl-free landform valley (TEST 6's DEM), the global argmin of
#     the full candidate set -- no filter applied -- is the split the retired
#     search returned.
# (b) The retired oracle and two_segment_keypoint_split() (the fit's own
#     answer) agree, tuple for tuple, on every hand-built profile in this
#     file, with and without the margin predicate.
# (c) FILTERS UNCHANGED. The SURVIVOR SET is byte-identical to the one the
#     candidate-set commit (d1f3ee2, best-residual selection) produced:
#     indices below, and every survivor's residual, slope drop and elevation
#     repr-exact, captured from that commit. Only the selection within the
#     set moves.
# (d) On REAL terrain: the reference property's survivor sets are the ones
#     d1f3ee2 produced, and the three keypoints move exactly from its
#     selections to the highest survivors.
# ============================================================================
_stem10, (_dist10, _elev10, _slope10) = _stem_profile(_dem6)
_cands10 = kd.keypoint_split_candidates(_dist10, _elev10, _slope10, KEYPOINT_MIN_RUN_CELLS)
_argmin10 = kd.global_argmin_candidate(_cands10)
_retired10 = _retired_two_segment_split(
    _dist10, _elev10, _slope10, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
)
assert _argmin10["index"] == _retired10[0] == 24, (_argmin10["index"], _retired10)
assert _argmin10["residual"] == _retired10[4], "the same split, scored by the same residual, bit for bit"
assert two_segment_keypoint_split(
    _dist10, _elev10, _slope10, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
) == _retired10

for _label, (_d, _e, _s), _pred in (
    ("test 1 raw", (_dist1, _elev_raw, _slope_raw), None),
    ("test 1 filled", (_dist1, _elev_fill, _slope_fill), None),
    ("test 3", (_dist3, _elev3, _slope3), None),
    ("test 4", (_dist4, _elev4, _slope4), None),
    ("test 6b unconstrained", (_dist6b, _elev6b, _slope6b), None),
    ("test 6b margin", (_dist6b, _elev6b, _slope6b), _admissible6b),
):
    for _min_drop in (KEYPOINT_MIN_SLOPE_DROP_PCT, -1.0e9):
        _old = _retired_two_segment_split(_d, _e, _s, KEYPOINT_MIN_RUN_CELLS, _min_drop, _pred)
        _new = two_segment_keypoint_split(_d, _e, _s, KEYPOINT_MIN_RUN_CELLS, _min_drop, position_is_eligible=_pred)
        assert _old == _new, (_label, _min_drop, _old, _new)


def _survivor_record(diag, valley_index=0):
    return [
        (_c["index"], _c["rowcol"], repr(_c["residual"]), repr(_c["slope_drop_pct"]), repr(_c["elevation_m"]))
        for _c in diag["valley_candidates"][valley_index]["candidates"]
        if not _c["rejected_by"]
    ]


_diag10 = {}
_kps10 = detect_keypoints(_dem6, _full_boundary(_rows1, _cols1), diagnostics=_diag10)
# Captured from d1f3ee2 (best-residual selection), repr-exact.
_D1F3_DEM6_SURVIVORS = [
    (17, (17, 7), '511.38090580794363', '4.666666666666671', '132.0'),
    (18, (18, 7), '382.47847662907617', '9.333333333333329', '128.0'),
    (19, (19, 7), '270.14687240718484', '16.333333333333336', '124.0'),
    (20, (20, 7), '177.2636217767742', '25.199999999999996', '120.0'),
    (21, (21, 7), '106.41913456269499', '34.53333333333333', '116.0'),
    (22, (22, 7), '59.27223531742346', '42.933333333333344', '112.0'),
    (23, (23, 7), '35.12310601359781', '49.0', '108.0'),
    (24, (24, 7), '26.76143934611997', '51.33333333333334', '104.0'),
    (25, (25, 7), '32.7738472940079', '49.0', '103.5'),
    (26, (26, 7), '58.151744854018226', '42.93333333333334', '103.0'),
    (27, (27, 7), '114.38500331966407', '34.53333333333333', '102.5'),
    (28, (28, 7), '205.93021121497296', '25.199999999999996', '102.0'),
    (29, (29, 7), '334.62046487278496', '16.333333333333332', '101.5'),
    (30, (30, 7), '500.27999305177076', '9.499999999999998', '101.0'),
    (31, (31, 7), '701.4668351784678', '5.166666666666666', '100.5'),
]
assert _survivor_record(_diag10) == _D1F3_DEM6_SURVIVORS, _survivor_record(_diag10)
assert [_c[0] for _c in _survivor_record(_diag1)] == list(range(17, 28)), "the bowl's survivors, as at d1f3ee2"
assert _diag10["valley_candidates"][0]["fit_best_survivor_index"] == 24, "d1f3ee2's selection, still on record"
assert _kps10[0]["position_along_stem"] == 17 and _diag10["selection_moved_valleys"] == 1

import terrain_reference_fixture  # noqa: E402  (the real reference terrain, offline)
from reference_fixture import BOUNDARY_POLYGON_UTM as _REF_BOUNDARY  # noqa: E402

_ref_dem = terrain_reference_fixture.load_dem()
_ref_stored = terrain_reference_fixture.load_derivations()
_ref_diag = {}
_ref_kps = detect_keypoints(_ref_dem, _REF_BOUNDARY, diagnostics=_ref_diag)
# d1f3ee2's survivor sets per valley, and its (best-residual) selections.
_D1F3_REF = {2: (56, 40, [40, 56]), 4: (34, 33, [33, 34, 35, 36, 37, 38, 51, 52, 53, 54, 55, 56, 57]),
             8: (25, 13, list(range(13, 26)))}
for _rec in _ref_diag["valley_candidates"]:
    _old_sel, _new_sel, _surv = _D1F3_REF[_rec["valley_id"]]
    assert [_c["index"] for _c in _rec["candidates"] if not _c["rejected_by"]] == _surv, _rec["valley_id"]
    assert _rec["fit_best_survivor_index"] == _old_sel and _rec["selected_index"] == _new_sel, (
        _rec["valley_id"], _rec["fit_best_survivor_index"], _rec["selected_index"]
    )
assert [(_k["valley_id"], _k["rowcol"], _k["elevation_m"]) for _k in _ref_kps] == [
    (4, (25, 37), 358.85), (8, (54, 34), 342.26), (2, (40, 72), 346.92)
], [(_k["valley_id"], _k["rowcol"], _k["elevation_m"]) for _k in _ref_kps]
# ...and terrain_reference_fixture.json was re-derived on its own stored DEM
# (make_terrain_reference_fixture.py --from-stored) to pin exactly this.
assert len(_ref_kps) == len(_ref_stored["keypoints"]) == 3
for _got, _stored in zip(_ref_kps, _ref_stored["keypoints"]):
    for _field, _value in _stored.items():
        assert _got[_field] == _value and type(_got[_field]) is type(_value), (_field, _got[_field], _value)
for _counter, _value in _ref_stored["keypoint_diagnostics"].items():
    assert _ref_diag[_counter] == _value, (_counter, _ref_diag[_counter], _value)
print(
    f"Test 10: the landform valley's global argmin is split {_argmin10['index']}, the retired search's "
    f"answer (residual {_argmin10['residual']:.4f} both); the oracle and the fit agree on every profile "
    f"here; the survivor sets -- synthetic and the reference property's -- are byte-identical to "
    f"d1f3ee2's, and only the selection within them moved (reference: valley 8 idx 25 -> 13, valley 4 "
    f"34 -> 33, valley 2 56 -> 40)."
)


# ============================================================================
# TEST 11 -- Fall-through: a fill-artifact best split no longer costs the
# valley its keypoint.
#
# TEST 1's bowl. Global argmin: stem index 30, cell (30, 7), residual
# 134.637, 8.51 m of fill -- inside the bowl, rejected as a fill artifact.
# It is also the split the retired single-split search returned (and then
# lost the valley over). Every candidate fitting better than the best
# survivor is a bowl cell. The survivors are rows 17-27 on real ground; the
# best-residual survivor is (27, 7) at 211.180 (the rim), and the SELECTED
# one is the highest, (17, 7) at 132.0 m, residual 1073.302.
# ============================================================================
_rec11 = _diag1["valley_candidates"][0]
_by11 = {_c["index"]: _c for _c in _rec11["candidates"]}
_argmin11 = _by11[_rec11["global_argmin_index"]]
_fit11 = _by11[_rec11["fit_best_survivor_index"]]
_sel11 = _by11[_rec11["selected_index"]]
assert _argmin11["rowcol"] == (30, 7) and _argmin11["rejected_by"] == [kd.REJECT_FILL_ARTIFACT], _argmin11
assert _argmin11["fill_depth_m"] > KEYPOINT_FILL_ARTIFACT_THRESHOLD_M and _argmin11["outcome"] == "rejected"
assert _rec11["pre_fill_best_index"] == _argmin11["index"], "the split the retired search would have returned"
assert _retired_two_segment_split(
    _dist1, _elev_raw, _slope_raw, KEYPOINT_MIN_RUN_CELLS, KEYPOINT_MIN_SLOPE_DROP_PCT
)[0] == _argmin11["index"], "and the retired oracle confirms it: that split is what used to be tested and lost"
assert abs(_argmin11["residual"] - 134.637) < 1e-3 and abs(_fit11["residual"] - 211.180) < 1e-3
assert _fit11["rowcol"] == (27, 7), "the best-residual survivor is still the rim -- the residual is still contaminated"
for _c in _rec11["candidates"]:
    if _c["residual"] < _fit11["residual"]:
        assert kd.REJECT_FILL_ARTIFACT in _c["rejected_by"], _c
assert _sel11["rowcol"] == (17, 7) and _sel11["rejected_by"] == [] and _sel11["outcome"] == "selected"
assert _rec11["fall_through"] and _rec11["fill_fall_through"] and _rec11["outcome"] == "selected"
assert tuple(_kps1[0]["rowcol"]) == _sel11["rowcol"] and _kps1[0]["position_along_stem"] == _sel11["index"]
print(
    f"Test 11: the bowl's global argmin {_argmin11['rowcol']} (residual {_argmin11['residual']:.3f}, fill "
    f"{_argmin11['fill_depth_m']:.2f} m) is rejected as {_argmin11['rejected_by']}; the valley keeps a "
    f"keypoint, the highest survivor {_sel11['rowcol']} (the best-residual survivor would be the rim, "
    f"{_fit11['rowcol']} at {_fit11['residual']:.3f})."
)


# ============================================================================
# TEST 12 -- Every candidate rejected: no keypoint, every reason recorded.
# (Unchanged by the selection rule: with no survivor there is nothing to
# select.)
#
# The bowl DEM again, with the boundary drawn only over the ground south of
# row 33's centre. Every real-ground split that drops slope (rows 17-27) is
# now more than the margin outside it; the bowl splits the margin reaches
# are fill artifacts; the rest never drop slope. Nothing survives, and all
# three reasons appear.
# ============================================================================
_x12, _y12 = pixel_center_xy(_dem1, 33, _c0_1)
_diag12 = {}
_kps12 = detect_keypoints(_dem1, box(_x12 - 200.0, _y12 - 400.0, _x12 + 200.0, _y12), diagnostics=_diag12)
assert _kps12 == [], [k["rowcol"] for k in _kps12]
_rec12 = _diag12["valley_candidates"][0]
assert _rec12["selected_index"] is None and _rec12["outcome"] == "rejected_fill_artifact"
assert _rec12["survivor_blocks"] == [] and _rec12["fit_best_survivor_index"] is None
assert _diag12["rejected_fill_artifact"] == 1 and _diag12["surviving"] == 0
assert _rec12["candidates"], "a profiled valley with no survivor still reports its candidates"
for _c in _rec12["candidates"]:
    assert _c["rejected_by"] and _c["outcome"] == "rejected", _c
    assert set(_c["rejected_by"]) <= set(kd.KEYPOINT_REJECTION_REASONS), _c
    # every reason recorded is TRUE of the candidate, and every true one is recorded
    assert (kd.REJECT_SLOPE_DROP in _c["rejected_by"]) == (_c["slope_drop_pct"] < KEYPOINT_MIN_SLOPE_DROP_PCT)
    assert (kd.REJECT_OFF_MARGIN in _c["rejected_by"]) == (
        not _c["on_parcel"] and _c["distance_outside_boundary_m"] > KEYPOINT_BOUNDARY_MARGIN_METERS
    )
    assert (kd.REJECT_FILL_ARTIFACT in _c["rejected_by"]) == (_c["fill_depth_m"] > KEYPOINT_FILL_ARTIFACT_THRESHOLD_M)
_tally12 = {_r: sum(_r in _c["rejected_by"] for _c in _rec12["candidates"]) for _r in kd.KEYPOINT_REJECTION_REASONS}
assert _tally12 == _diag12["candidate_rejections"] and all(_tally12.values()), (_tally12, _diag12)
assert kd.select_keypoint_candidate(_rec12["candidates"]) is None
print(
    f"Test 12: with the boundary south of row 33 all {len(_rec12['candidates'])} candidates are rejected "
    f"-- {_tally12} -- so the valley yields no keypoint (outcome {_rec12['outcome']}), every reason "
    f"recorded on its candidate and tallied for the run."
)


# ============================================================================
# TEST 13 -- Candidate validity: nothing violating the minimum-segment-
# length constraint (KEYPOINT_MIN_RUN_CELLS profile cells either side) ever
# enters the set. Checked on bare profiles at the boundary lengths, and on
# every profiled valley detect_keypoints() has reported in this file.
# ============================================================================
_run = KEYPOINT_MIN_RUN_CELLS
for _n in (2 * _run - 1, 2 * _run, 2 * _run + 1, 2 * _run + 2, 40):
    _d13, _e13, _s13 = _build_slope_profile([10.0] * (_n // 2) + [2.0] * (_n - 1 - _n // 2))
    assert len(_e13) == _n
    _idx13 = [_c["index"] for _c in kd.keypoint_split_candidates(_d13, _e13, _s13, _run)]
    assert _idx13 == list(range(_run, _n - _run)), (_n, _idx13)
    assert all(_k >= _run and (_n - 1 - _k) >= _run for _k in _idx13)
for _diag in (_diag1, _diag10, _diag12, _ref_diag):
    for _rec in _diag["valley_candidates"]:
        _idx = [_c["index"] for _c in _rec["candidates"]]
        assert _idx == list(range(_run, _rec["stem_length_cells"] - _run)), (_rec["valley_id"], _idx[:3], _idx[-3:])
print(
    f"Test 13: candidates run exactly over [{_run}, n-1-{_run}] -- none at a profile of {2 * _run} cells, "
    f"one at {2 * _run + 1} -- and every valley reported in this file (reference terrain included) "
    f"holds exactly that range."
)


# ============================================================================
# TEST 14 -- Highest-elevation selection (the change of basis).
#
# The landform valley (TEST 6's DEM): fifteen candidates survive every
# filter, one block, (17, 7) at 132.0 m down to (31, 7) at 100.5 m. The
# RESIDUAL-best survivor is (24, 7) at 104.0 m, residual 26.76 -- the
# selection before this change, and the profile's true steep-to-gentle
# break. The HIGHEST survivor is (17, 7) at 132.0 m, residual 511.38, and it
# is selected: the survivors are all qualifying inflections, and among them
# the choice is by use -- the ground a keypoint commands by gravity -- which
# is monotone in elevation (the reasoning, and the position it reverses, at
# select_keypoint_candidate()).
#
# THE EDGE-CELL CONSEQUENCE, asserted so it cannot drift silently: (17, 7)
# is the TOP of its block, directly below index 16, rejected for a 1.87%
# drop; its own drop, 4.67%, is the WEAKEST in the block, while the
# block's strongest is (24, 7) at 51.33%.
# ============================================================================
_rec14 = _diag10["valley_candidates"][0]
_surv14 = [_c for _c in _rec14["candidates"] if not _c["rejected_by"]]
_by14 = {_c["index"]: _c for _c in _rec14["candidates"]}
_highest14 = max(_surv14, key=lambda _c: _c["elevation_m"])
_fitbest14 = min(_surv14, key=lambda _c: _c["residual"])
assert _highest14["rowcol"] == (17, 7) and _highest14["elevation_m"] == 132.0
assert _fitbest14["rowcol"] == (24, 7) and abs(_fitbest14["residual"] - 26.761) < 1e-3
assert kd.select_keypoint_candidate(_rec14["candidates"]) is _highest14
assert tuple(_kps10[0]["rowcol"]) == (17, 7) and _kps10[0]["elevation_m"] == 132.0
assert _rec14["survivor_blocks"] == [(17, 31)]
assert _by14[16]["rejected_by"] == [kd.REJECT_SLOPE_DROP], "the cell above the selection is a slope-drop rejection"
assert _rec14["selected_slope_drop_pct"] == min(_c["slope_drop_pct"] for _c in _surv14)
assert _rec14["best_survivor_slope_drop_index"] == 24
# Reordering the candidates changes nothing: the rule reads the values, not the order.
assert kd.select_keypoint_candidate(sorted(_rec14["candidates"], key=lambda _c: _c["residual"])) is _highest14
print(
    f"Test 14: of {len(_surv14)} survivors the residual-best is {_fitbest14['rowcol']} at "
    f"{_fitbest14['elevation_m']} m (residual {_fitbest14['residual']:.2f}); the highest, "
    f"{_highest14['rowcol']} at {_highest14['elevation_m']} m (residual {_highest14['residual']:.2f}), is "
    f"selected -- the top edge of block {_rec14['survivor_blocks'][0]}, drop "
    f"{_rec14['selected_slope_drop_pct']:.2f}% against the block's best {_rec14['best_survivor_slope_drop_pct']:.2f}%."
)


# ============================================================================
# TEST 16 -- Tie on elevation: the lowest residual wins, deterministically.
# (Numbered after 15 to keep the existing numbers stable.)
#
# Three survivors share the top elevation (120.0 m) with residuals 9.0, 4.0
# and 7.0; a fourth, lower one (110.0 m) fits best of all (1.0) and must not
# win; a fifth at 130.0 m fits best but is REJECTED and must be ignored. The
# selection is the 4.0 one, whatever order the candidates arrive in, run
# after run. A further exact tie on residual falls to the lowest index.
# ============================================================================
def _cand(index, elevation, residual, rejected_by=()):
    return {"index": index, "elevation_m": elevation, "residual": residual, "rejected_by": list(rejected_by)}


_tie16 = [
    _cand(3, 130.0, 0.5, [kd.REJECT_SLOPE_DROP]),
    _cand(5, 120.0, 9.0), _cand(6, 120.0, 4.0), _cand(7, 120.0, 7.0), _cand(9, 110.0, 1.0),
]
import itertools  # noqa: E402

for _perm in itertools.permutations(_tie16):
    assert kd.select_keypoint_candidate(list(_perm))["index"] == 6
_exact16 = [_cand(8, 120.0, 4.0), _cand(6, 120.0, 4.0), _cand(7, 119.0, 0.1)]
for _perm in itertools.permutations(_exact16):
    assert kd.select_keypoint_candidate(list(_perm))["index"] == 6
print(
    "Test 16: three survivors tied at 120.0 m -> the one with the lowest residual (4.0) is selected in "
    "every one of 120 orderings, over a lower best-fitting survivor and a higher rejected one; an exact "
    "tie on residual too falls to the lowest index."
)


# ============================================================================
# TEST 17 -- A single survivor is selected, whatever its elevation or residual.
#
# The bowl DEM with the boundary south of row 32's centre: of the real-
# ground block (rows 17-27) only (27, 7) is within the 25 m margin, so it is
# the one survivor. It is the LOWEST cell of that block -- ten others stand
# higher on the full-boundary run -- and it is returned, because there is
# nothing else to choose. The synthetic lone survivor makes the same point
# with an absurd elevation and residual.
# ============================================================================
_x17, _y17 = pixel_center_xy(_dem1, 32, _c0_1)
_diag17 = {}
_kps17 = detect_keypoints(_dem1, box(_x17 - 200.0, _y17 - 400.0, _x17 + 200.0, _y17), diagnostics=_diag17)
_rec17 = _diag17["valley_candidates"][0]
_surv17 = [_c for _c in _rec17["candidates"] if not _c["rejected_by"]]
assert [_c["rowcol"] for _c in _surv17] == [(27, 7)], [_c["rowcol"] for _c in _surv17]
assert [tuple(_k["rowcol"]) for _k in _kps17] == [(27, 7)]
assert _surv17[0]["elevation_m"] < _highest14["elevation_m"], "not high by any standard of this valley"
assert kd.select_keypoint_candidate([_cand(4, -50.0, 1.0e9)])["index"] == 4
print(
    f"Test 17: with the boundary south of row 32 the only survivor is (27, 7) at "
    f"{_surv17[0]['elevation_m']} m, and it is the keypoint; a lone synthetic survivor at -50 m with a "
    f"residual of 1e9 is selected too."
)


# ============================================================================
# TEST 15 -- Output contract: one keypoint per valley, and every field a
# downstream consumer reads is present with its retired type.
#
# The consumers, from a grep of the backend: pipeline_context._attach_
# keypoint_feature_relationships (point_utm, elevation_m); landform_section
# (id, on_parcel, point_utm, distance_outside_boundary_m, elevation_m);
# landform_derivations (id, valley_id, point_utm, elevation_m, on_parcel,
# position_along_stem, rowcol, slope_above_pct, slope_below_pct);
# wire_translation.keypoints_to_feature_collection (id, geometry_wgs84,
# confidence, confidence_notes, valley_id, elevation_m, contributing_acres,
# slope_above/below/drop_pct, stem_length_cells, position_along_stem,
# on_parcel, distance_outside_boundary_m); session_cache / session_design /
# make_terrain_reference_fixture carry the list through. The water step no
# longer reads keypoints (pipeline_context stopped forwarding them; only the
# demoted water_candidate_zones.py and its diagnostic still can).
# ============================================================================
_CONSUMER_FIELDS = {
    "id": int, "valley_id": int, "rowcol": tuple, "point_utm": Point, "geometry_wgs84": dict,
    "elevation_m": float, "contributing_acres": float, "slope_above_pct": float,
    "slope_below_pct": float, "slope_drop_pct": float, "stem_length_cells": int,
    "position_along_stem": int, "on_parcel": bool, "distance_outside_boundary_m": float,
    "confidence": str, "confidence_notes": str,
}
assert set(_CONSUMER_FIELDS) == _EXPECTED_KEYS
for _kps in (_kps1, _kps5, _kps10, _kps17, _ref_kps):
    _vids = [_k["valley_id"] for _k in _kps]
    assert len(_vids) == len(set(_vids)), f"more than one keypoint for a valley: {_vids}"
    assert [_k["id"] for _k in _kps] == list(range(len(_kps)))
    for _k in _kps:
        assert set(_k) == _EXPECTED_KEYS, set(_k) ^ _EXPECTED_KEYS
        for _field, _type in _CONSUMER_FIELDS.items():
            assert isinstance(_k[_field], _type), (_field, type(_k[_field]))
validate_feature_collection(keypoints_to_geojson(_ref_kps))
print(
    "Test 15: one keypoint per valley on every fixture (reference terrain included), each carrying "
    f"exactly the {len(_CONSUMER_FIELDS)} fields its consumers read, with their retired types."
)

print("\nAll keypoint_detection checks passed.")
