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
      and flagged; 60 m outside is dropped.
  7.  Short stem: a valley whose stem is shorter than 2*MIN_RUN+2 returns no
      keypoint, no exception.
  8.  Empty result is honest: a DEM with no primary valley returns [].
  9.  dem-only dependency: detect_keypoints needs no soil, canopy, road, or
      climate input, and the module imports without parcel_data.
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
# The detect-level tracer cannot cross the filled bowl (D8 does not resolve
# drainage across a perfectly flat filled plateau -- a documented limitation
# in valley_delineation.py), so it stops at the bowl rim and never profiles
# through the depression. The raw-vs-filled contrast is therefore shown by
# profiling the KNOWN center-column stem directly, which is what exposes the
# artifact deterministically.
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

# detect (RAW, the real path): a real keypoint is found, and it is NOT inside
# the depression.
_kps1 = detect_keypoints(_dem1, _full_boundary(_rows1, _cols1))
assert _kps1, "expected a keypoint on the steep upper reach above the bowl"
for _k in _kps1:
    assert _k["rowcol"][0] not in _BOWL_ROWS, (
        f"RAW profiling must not return a keypoint inside the depression, got {_k['rowcol']}"
    )
print(
    f"Test 1: detect (raw) returned keypoint(s) at {[k['rowcol'] for k in _kps1]}, "
    f"none inside the bowl rows {_BOWL_ROWS.start}-{_BOWL_ROWS.stop - 1}."
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
# TEST 6 -- Boundary margin: 10 m and 20 m kept and flagged; 60 m dropped.
#
# The keypoint location is fixed by terrain (independent of the boundary,
# which only feeds the margin gate). Find it once, then place boundaries whose
# nearest edge is 10 m, 20 m, and 60 m from it.
# ============================================================================
_kp6 = detect_keypoints(_dem1, _full_boundary(_rows1, _cols1))[0]
_P6 = _kp6["point_utm"]
_kept6 = {}
for _offset in (10.0, 20.0, 60.0):
    # A box lying entirely to the north of P, its southern edge _offset metres away.
    _bnd = box(_P6.x - 100.0, _P6.y + _offset, _P6.x + 100.0, _P6.y + _offset + 300.0)
    assert not _bnd.contains(_P6)
    _res = detect_keypoints(_dem1, _bnd)
    _kept6[_offset] = _res

assert len(_kept6[10.0]) == 1 and _kept6[10.0][0]["on_parcel"] is False
assert abs(_kept6[10.0][0]["distance_outside_boundary_m"] - 10.0) < 0.01
assert len(_kept6[20.0]) == 1 and _kept6[20.0][0]["on_parcel"] is False
assert abs(_kept6[20.0][0]["distance_outside_boundary_m"] - 20.0) < 0.01
assert _kept6[60.0] == [], "a keypoint 60 m outside the boundary (> 25 m margin) must be dropped"
print(
    "Test 6: keypoint kept at 10 m outside (on_parcel=False, distance="
    f"{_kept6[10.0][0]['distance_outside_boundary_m']} m) and at 20 m outside (distance="
    f"{_kept6[20.0][0]['distance_outside_boundary_m']} m); dropped at 60 m outside (empty)."
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


print("\nAll keypoint_detection checks passed.")
