"""
test_keypoint_detection.py

EXPERIMENTAL (branch: keypoint-sited-water-zones). Offline (no-network)
checks for keypoint_detection.py -- the detection stage only. All fixtures
are synthetic; nothing here reproduces the real reference-property numbers
(there is no network, and the prototype figures in the task are context,
not targets). Script-style (module-level asserts + prints), run as
`python test_keypoint_detection.py`, matching this codebase's other tests.

Verification map (task's numbered list, detection subset):
  2.  Raw vs filled profile: a filled depression manufactures a steep-to-flat
      break the raw profile does not -- the artifact fix #1 exists to avoid,
      shown as an inline contrast at the profiling-helper level (deterministic,
      free of D8-tie-on-plateau noise), plus the marsh gate as second defense.
  3.  Upstream extension reaches an inflection that sits ABOVE the stream
      threshold: found with the extension, missed without it.
  4.  Catchment window: below the floor and above the ceiling both rejected;
      counts reported from the diagnostics dict.
  5.  Production exclusion uses render_fill_polygon_utm (the drawn fill), NOT
      polygon_utm: inside the fill rejected, outside kept.
  6.  Off-parcel rejection: a keypoint in the DEM buffer outside the boundary.
  7.  Dedup: convergent branches through one keypoint yield one candidate;
      removal count reported.
  10. Empty result is honest: a valley with no inflection -> [], no exception,
      no relaxed gate.
"""

import numpy as np
from shapely.geometry import Point, box

from feature_schema import validate_feature_collection
from raster_grid import cell_area_acres, pixel_center_xy
from valley_delineation import fill_depressions
import keypoint_detection as kd
from keypoint_detection import (
    KEYPOINT_FILL_ARTIFACT_THRESHOLD_M,
    KEYPOINT_MIN_FLAT_RUN_CELLS,
    KEYPOINT_MIN_SLOPE_DROP_PCT,
    KEYPOINT_MIN_STEEP_RUN_CELLS,
    KEYPOINT_PROFILE_SMOOTH_CELLS,
    detect_keypoints,
    extend_thalweg_upstream,
    keypoints_to_geojson,
)

CRS = "EPSG:32617"
RES = (5.0, 5.0)
ORIGIN_X = 500000.0
ORIGIN_Y = 4500000.0


def _dem(array):
    return {
        "array": array,
        "resolution_meters": RES,
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _full_boundary(size):
    return box(ORIGIN_X, ORIGIN_Y - size * RES[1], ORIGIN_X + size * RES[0], ORIGIN_Y)


# A production patch whose render_fill is off-grid, so the production gate
# excludes nothing unless a test says otherwise. (Detection has no
# service-distance gate, so polygon_utm placement is irrelevant here.)
def _offgrid_pa():
    return [
        {
            "id": 0,
            "representative_elevation_m": -99.0,
            "polygon_utm": box(ORIGIN_X + 100.0, ORIGIN_Y - 120.0, ORIGIN_X + 150.0, ORIGIN_Y - 100.0),
            "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
        }
    ]


# Assert the constants are the calibrated prototype values (a change here
# should be a deliberate retune, not an accident).
assert KEYPOINT_PROFILE_SMOOTH_CELLS == 5
assert KEYPOINT_MIN_STEEP_RUN_CELLS == 6
assert KEYPOINT_MIN_FLAT_RUN_CELLS == 6
assert KEYPOINT_MIN_SLOPE_DROP_PCT == 4.0
assert KEYPOINT_FILL_ARTIFACT_THRESHOLD_M == 0.15
assert kd.KEYPOINT_MIN_CATCHMENT_ACRES == 5.0
assert kd.KEYPOINT_MAX_CATCHMENT_ACRES == 20.0
assert kd.KEYPOINT_DEDUP_RADIUS_METERS == 25.0


# =====================================================================
# Canonical clean valley: steep upper slope (3 m/cell) breaking to a flat
# floor (0.3 m/cell) at row 25, V cross-section converging on col 25. Used
# by tests 4/5/6/7. The single surviving keypoint sits at the break with
# ~8 ac catchment.
# =====================================================================
CANON_SIZE = 50
CANON_MID = 25


def _canon_array():
    arr = np.zeros((CANON_SIZE, CANON_SIZE), dtype=np.float32)
    for r in range(CANON_SIZE):
        along = (100.0 - 3.0 * r) if r < 25 else (100.0 - 75.0 - 0.3 * (r - 25))
        for c in range(CANON_SIZE):
            arr[r, c] = along + abs(c - CANON_MID) * 2.0
    return arr


CANON_DEM = _dem(_canon_array())
CANON_BOUNDARY = _full_boundary(CANON_SIZE)

_canon_kps = detect_keypoints(CANON_DEM, CANON_BOUNDARY, _offgrid_pa())
assert len(_canon_kps) == 1, f"canonical valley should yield exactly one keypoint, got {len(_canon_kps)}"
_CANON_KP = _canon_kps[0]
assert _CANON_KP["rowcol"] == (25, 25), f"keypoint should sit at the break (25,25), got {_CANON_KP['rowcol']}"
assert 5.0 <= _CANON_KP["contributing_acres"] <= 20.0
assert _CANON_KP["slope_drop_pct"] >= KEYPOINT_MIN_SLOPE_DROP_PCT
print(
    f"Canonical valley: 1 keypoint at {_CANON_KP['rowcol']}, "
    f"{_CANON_KP['contributing_acres']:.1f} ac catchment, "
    f"slope drop {_CANON_KP['slope_drop_pct']:.1f}% "
    f"({_CANON_KP['slope_above_pct']:.1f}% -> {_CANON_KP['slope_below_pct']:.1f}%)."
)


# =====================================================================
# TEST 2 -- RAW vs FILLED PROFILE (failure mode 1, in executable form).
# A V-bowl (closed depression) on a single-channel valley. fill_depressions
# raises the bowl to a flat plateau; profiling that filled plateau fires a
# steep-to-flat break AT THE PLATEAU ENTRANCE, where fill depth is ~0 -- so
# the marsh gate cannot catch it. Profiling RAW instead puts the strongest
# break at the bowl BOTTOM, where fill depth is large -- exactly where the
# marsh gate does catch it. Shown at the profiling-helper level so the
# contrast is deterministic (the full pipeline's D8 flow over the flat filled
# plateau is tie-sensitive and not the point being tested here).
# =====================================================================
_T2_SIZE = 60
_T2_MID = 30


def _t2_thalweg_elev(r):
    if r < 26:
        return 150.0 - 2.0 * r                       # steep straight approach
    if r <= 31:
        return (150.0 - 52.0) - 2.0 * (r - 26)       # descend INTO the bowl (bottom at row 31)
    if r <= 36:
        return (150.0 - 52.0 - 10.0) + 2.0 * (r - 31)  # ascend out of the bowl
    return (150.0 - 52.0) - 2.0 * (r - 36)           # resume descent, continuous with the rim


_t2_arr = np.zeros((_T2_SIZE, _T2_SIZE), dtype=np.float32)
for _r in range(_T2_SIZE):
    for _c in range(_T2_SIZE):
        _t2_arr[_r, _c] = _t2_thalweg_elev(_r) + abs(_c - _T2_MID) * 6.0
_T2_DEM = _dem(_t2_arr)
_t2_filled = fill_depressions(_t2_arr)

# The bowl really is a closed depression: fill raises its interior.
_t2_bowl_fill = float(_t2_filled[31, _T2_MID] - _t2_arr[31, _T2_MID])
assert _t2_bowl_fill > 1.0, f"the bowl must be a real filled depression, got fill depth {_t2_bowl_fill}"

_t2_thalweg = [(r, _T2_MID) for r in range(0, 45)]


def _t2_inflection(source):
    _, slope = kd._profile_along_thalweg(_t2_thalweg, source, _T2_DEM, KEYPOINT_PROFILE_SMOOTH_CELLS)
    return kd._best_inflection(slope, KEYPOINT_MIN_STEEP_RUN_CELLS, KEYPOINT_MIN_FLAT_RUN_CELLS)


_t2_raw = _t2_inflection(_t2_arr)
_t2_fill = _t2_inflection(_t2_filled)
_t2_raw_cell = _t2_thalweg[_t2_raw[0]]
_t2_fill_cell = _t2_thalweg[_t2_fill[0]]
_t2_raw_depth = float(_t2_filled[_t2_raw_cell] - _t2_arr[_t2_raw_cell])
_t2_fill_depth = float(_t2_filled[_t2_fill_cell] - _t2_arr[_t2_fill_cell])

# Profiling source changes the answer.
assert _t2_raw_cell != _t2_fill_cell, "raw and filled profiles must disagree on the keypoint location"
# RAW: strongest break at the bowl bottom -> HIGH fill depth -> the marsh gate rejects it.
assert _t2_raw_depth > KEYPOINT_FILL_ARTIFACT_THRESHOLD_M, (
    f"raw keypoint should land on filled ground (fill depth {_t2_raw_depth:.2f} m) so the marsh gate catches it"
)
# FILLED: break at the plateau entrance -> ~0 fill depth -> would pass the marsh gate.
assert _t2_fill_depth <= KEYPOINT_FILL_ARTIFACT_THRESHOLD_M, (
    f"filled keypoint should land at the plateau entrance (fill depth {_t2_fill_depth:.2f} m)"
)
assert _t2_fill[3] >= KEYPOINT_MIN_SLOPE_DROP_PCT, "the filled artifact clears the slope-drop gate (it is a strong break)"

# End-to-end confirmation: on the REAL (raw) path, no surviving keypoint ever
# sits on filled ground -- the marsh gate holds.
_t2_full = detect_keypoints(_T2_DEM, _full_boundary(_T2_SIZE), _offgrid_pa(), min_catchment_acres=0.0, max_catchment_acres=1e9)
_t2_on_fill = [k for k in _t2_full if float(_t2_filled[k["rowcol"]] - _t2_arr[k["rowcol"]]) > KEYPOINT_FILL_ARTIFACT_THRESHOLD_M]
assert not _t2_on_fill, f"raw path must never return a keypoint on filled ground, got {[k['rowcol'] for k in _t2_on_fill]}"
print(
    "Test 2 -- raw vs filled profile: RAW keypoint at "
    f"{_t2_raw_cell} (fill depth {_t2_raw_depth:.2f} m -> marsh gate REJECTS); "
    f"FILLED keypoint at {_t2_fill_cell} (fill depth {_t2_fill_depth:.2f} m, drop {_t2_fill[3]:.1f}% "
    "-> would SURVIVE). Profiling filled manufactures a keypoint the raw profile + marsh gate correctly exclude."
)


# =====================================================================
# TEST 3 -- UPSTREAM EXTENSION reaches an inflection above the stream
# threshold. A narrow steep headwater (inflection at row 8, catchment well
# under the 0.5 ac stream threshold) widening into a broad apron far below,
# so the delineated branch begins only at the apron -- BELOW the inflection.
# With the upstream walk the inflection is found; without it, it is missed.
# (Catchment gate relaxed to isolate the extension effect.)
# =====================================================================
_T3_SIZE = 70
_T3_MID = 35
_t3_arr = np.full((_T3_SIZE, _T3_SIZE), 500.0, dtype=np.float32)
for _r in range(_T3_SIZE):
    for _c in range(_T3_SIZE):
        _along = (300.0 - 6.0 * _r) if _r < 8 else (300.0 - 48.0 - 0.4 * (_r - 8))
        _width = 3 if _r < 18 else _T3_SIZE
        _d = abs(_c - _T3_MID)
        if _d <= _width:
            _t3_arr[_r, _c] = _along + _d * (10.0 if _r < 18 else 1.0)
        else:
            _t3_arr[_r, _c] = _along + _width * 10.0 + (_d - _width) * 0.05
_T3_DEM = _dem(_t3_arr)
_T3_BOUNDARY = _full_boundary(_T3_SIZE)

_t3_with = detect_keypoints(_T3_DEM, _T3_BOUNDARY, _offgrid_pa(), min_catchment_acres=0.0, max_catchment_acres=1e9, extend_upstream=True)
_t3_without = detect_keypoints(_T3_DEM, _T3_BOUNDARY, _offgrid_pa(), min_catchment_acres=0.0, max_catchment_acres=1e9, extend_upstream=False)
_t3_with_near = [k["rowcol"] for k in _t3_with if 6 <= k["rowcol"][0] <= 10]
_t3_without_near = [k["rowcol"] for k in _t3_without if 6 <= k["rowcol"][0] <= 10]
assert _t3_with_near, f"WITH extension the inflection near row 8 must be found, got rows {[k['rowcol'][0] for k in _t3_with]}"
assert not _t3_without_near, f"WITHOUT extension the inflection near row 8 must be missed, got {_t3_without_near}"
print(
    f"Test 3 -- upstream extension: WITH extension the inflection is found at {_t3_with_near[0]} "
    f"(above the stream threshold); WITHOUT it, the branch begins below the inflection and it is missed "
    f"(rows found: {sorted(set(k['rowcol'][0] for k in _t3_without))})."
)


# =====================================================================
# TEST 4 -- CATCHMENT WINDOW. The canonical keypoint sits at ~8 ac. Move the
# floor above it (rejected: below floor) and the ceiling below it (rejected:
# above ceiling); the default window keeps it. Counts from the diagnostics.
# =====================================================================
_c_ac = _CANON_KP["contributing_acres"]
_d_low, _d_high, _d_ok = {}, {}, {}
_below = detect_keypoints(CANON_DEM, CANON_BOUNDARY, _offgrid_pa(), min_catchment_acres=_c_ac + 1.0, diagnostics=_d_low)
_above = detect_keypoints(CANON_DEM, CANON_BOUNDARY, _offgrid_pa(), max_catchment_acres=_c_ac - 1.0, diagnostics=_d_high)
_ok = detect_keypoints(CANON_DEM, CANON_BOUNDARY, _offgrid_pa(), diagnostics=_d_ok)
assert _below == [] and _d_low["rejected_catchment_low"] >= 1, "raising the floor above the keypoint must reject it"
assert _above == [] and _d_high["rejected_catchment_high"] >= 1, "lowering the ceiling below the keypoint must reject it"
assert len(_ok) == 1, "the default window keeps the keypoint"
print(
    f"Test 4 -- catchment window: keypoint at {_c_ac:.1f} ac. Floor raised to {_c_ac + 1.0:.1f} ac rejects "
    f"{_d_low['rejected_catchment_low']} below floor; ceiling lowered to {_c_ac - 1.0:.1f} ac rejects "
    f"{_d_high['rejected_catchment_high']} above ceiling; default [5, 20] ac keeps {len(_ok)}."
)


# =====================================================================
# TEST 5 -- PRODUCTION EXCLUSION uses render_fill_polygon_utm (the drawn
# fill), NOT polygon_utm. Cover the keypoint's cell centre with a render fill
# -> rejected. Then set polygon_utm over the keypoint but render_fill away
# from it -> kept (confirming the gate reads the drawn fill, not polygon_utm).
# =====================================================================
_kp_x, _kp_y = pixel_center_xy(CANON_DEM, 25, 25)
_cover = box(_kp_x - 6.0, _kp_y - 6.0, _kp_x + 6.0, _kp_y + 6.0)
_away = box(ORIGIN_X + 5.0, ORIGIN_Y - 40.0, ORIGIN_X + 15.0, ORIGIN_Y - 30.0)

_pa_render_covers = [{"id": 0, "representative_elevation_m": -99.0, "polygon_utm": _away, "render_fill_polygon_utm": _cover}]
_pa_polygon_only = [{"id": 0, "representative_elevation_m": -99.0, "polygon_utm": _cover, "render_fill_polygon_utm": _away}]
_d5 = {}
_rej = detect_keypoints(CANON_DEM, CANON_BOUNDARY, _pa_render_covers, diagnostics=_d5)
_kept = detect_keypoints(CANON_DEM, CANON_BOUNDARY, _pa_polygon_only)
assert _rej == [] and _d5["rejected_production"] >= 1, "a keypoint inside render_fill_polygon_utm must be rejected"
assert len(_kept) == 1 and _kept[0]["rowcol"] == (25, 25), (
    "a keypoint inside polygon_utm but OUTSIDE render_fill_polygon_utm must be kept -- the gate reads the drawn fill"
)
assert _cover.contains(Point(_kp_x, _kp_y)) and _cover.contains(_pa_polygon_only[0]["polygon_utm"].centroid), (
    "sanity: the same geometry that rejects as render_fill does NOT reject as polygon_utm"
)
print(
    "Test 5 -- production exclusion: keypoint inside render_fill_polygon_utm rejected; the SAME geometry as "
    "polygon_utm (render_fill placed elsewhere) keeps it -- the gate weighs the drawn fill, not polygon_utm."
)


# =====================================================================
# TEST 6 -- OFF-PARCEL REJECTION. A boundary clipped to the lower floor
# leaves the keypoint (row 25) off-parcel, in the DEM buffer -> rejected.
# =====================================================================
_clip = box(ORIGIN_X, ORIGIN_Y - CANON_SIZE * 5.0, ORIGIN_X + CANON_SIZE * 5.0, ORIGIN_Y - 32 * 5.0)
assert not _clip.contains(Point(_kp_x, _kp_y)), "sanity: the clipped boundary must exclude the keypoint cell"
_d6 = {}
_off = detect_keypoints(CANON_DEM, _clip, _offgrid_pa(), diagnostics=_d6)
assert all(k["rowcol"][0] >= 32 for k in _off), "no surviving keypoint may sit above the clipped boundary"
assert _d6["rejected_off_parcel"] >= 1, "the row-25 keypoint must be rejected as off-parcel"
print(
    f"Test 6 -- off-parcel: with the boundary clipped below the break, the row-25 keypoint is rejected "
    f"({_d6['rejected_off_parcel']} off-parcel); on the full boundary it is kept."
)


# =====================================================================
# TEST 7 -- DEDUP. The canonical component's three convergent branches all
# pass through the one break at (25,25); dedup collapses them to a single
# candidate. Report how many it removed.
# =====================================================================
_d7 = {}
_dedup_kps = detect_keypoints(CANON_DEM, CANON_BOUNDARY, _offgrid_pa(), diagnostics=_d7)
assert len(_dedup_kps) == 1, "convergent branches must dedup to one keypoint"
assert _d7["raw_candidates"] >= 2, "the convergent component must emit more than one raw candidate to dedup"
assert _d7["dedup_removed"] >= 1, "dedup must have removed at least the convergent duplicates"
assert _d7["surviving"] + _d7["dedup_removed"] == _d7["raw_candidates"], (
    "on this un-gated fixture every raw candidate either survives or is a dedup duplicate"
)
print(
    f"Test 7 -- dedup: {_d7['raw_candidates']} raw candidate(s) from convergent branches collapse to "
    f"{_d7['surviving']} keypoint; dedup removed {_d7['dedup_removed']}."
)


# =====================================================================
# TEST 10 -- HONEST EMPTY. A planar constant-slope hillside (elevation a
# function of row only, no cross-valley convergence): flow runs straight
# downhill in parallel, nothing concentrates into a drainage line, so there
# is no valley and no inflection anywhere. Detection returns [] cleanly --
# no exception, no gate relaxed to manufacture a result.
# =====================================================================
_flat_arr = np.zeros((CANON_SIZE, CANON_SIZE), dtype=np.float32)
for _r in range(CANON_SIZE):
    for _c in range(CANON_SIZE):
        _flat_arr[_r, _c] = 100.0 - 2.0 * _r  # planar: no cross term, no convergence
_FLAT_DEM = _dem(_flat_arr)
_d10 = {}
_empty = detect_keypoints(_FLAT_DEM, _full_boundary(CANON_SIZE), _offgrid_pa(), diagnostics=_d10)
assert _empty == [], f"a featureless planar slope must yield no keypoints, got {len(_empty)}"
assert _d10["surviving"] == 0 and _d10["raw_candidates"] == 0
print(
    f"Test 10 -- honest empty: a featureless planar slope yields [] "
    f"(no valley, {_d10['raw_candidates']} raw candidates); no exception, no relaxed gate."
)


# =====================================================================
# keypoints_to_geojson conforms to the shared feature schema.
# =====================================================================
_fc = keypoints_to_geojson(_canon_kps)
validate_feature_collection(_fc)
assert _fc["features"][0]["properties"]["layer"] == "keypoint"
assert _fc["features"][0]["geometry"]["type"] == "Point"
print("keypoints_to_geojson: schema-valid, layer='keypoint', Point geometry.")

print("\nAll keypoint_detection checks passed.")
