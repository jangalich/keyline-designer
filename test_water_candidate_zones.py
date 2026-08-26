"""
test_water_candidate_zones.py

Offline (no-network) checks for water_candidate_zones.py's nomination +
level-pool pipeline: an ABSOLUTE-ceiling hard-exclusion mask -> TWO-FAMILY
NOMINATION of anchor cells (keypoints by catchment, then the highest
remaining flow accumulation) -> per-anchor level-pool delineation
(valley_level_pool.py) -> boundary clip, area floor/cap, overlap trim ->
bounded morphological OPENING for the render fill. Stage 2 of the feature
("is the zone-filtering logic correct"), independent of Stage 1 (DEM/
valley delineation accuracy).

These tests build small synthetic DEMs with a known drainage pattern, or
inject an exact eligibility mask / accumulation grid where the point is
the nomination and finishing logic rather than real D8 hydrology.

Verification map:
  1. Nomination reason codes: an eligible keypoint, one needing a snap,
     one with no eligible cell in range, and two within the separation
     distance of each other -- the exact reason codes, and that CATCHMENT
     ordering (not keypoint_detection.py's slope-drop ordering) decides
     which of the pair wins.
  2. Non-overlap invariant across a mixed family-1/family-2 run, and
     overlap_trimmed behaviour.
  3. Contract preservation: every field a downstream consumer reads off a
     zone dict is present (the list is built from the consumers, not from
     memory).
  4. Area cap: a flat plain floods absurdly, truncated_by_cap fires, and
     the truncated zone stays connected to its anchor.
  5. Area floor: a sub-floor delineation is dropped with below_min_area.
  6. Boundary clip is the ONLY clip -- canopy/road/production never
     reshape a pool -- and truncated_by_boundary fires when it bites.
  7. Opening boundedness / wipeout fallback / MultiPolygon tolerance
     (unchanged from the previous design; the render fill did not change).
  8. The production-overlap gate is GONE: a cell inside a production
     area's render fill is eligible now.
Plus retained coverage of the absolute ceiling / boundary independence /
off-parcel / canopy / road / service-distance gates and GeoJSON.

WHAT WAS DELETED AND WHERE ITS COVERAGE WENT. The connected-components +
greedy-growth mechanism and its fixed survey-area target are retired, so
the tests that pinned them (growth connectivity, top-N fragmentation
contrast, adjacent-over-distant growth, exhausted-cluster padding,
rank-after-growth, the 5 m production setback) are deleted rather than
adapted -- they tested code that no longer exists. Each was REPLACED, not
dropped: connectivity is now guaranteed by the level pool's own
flow-tree construction and asserted in test_valley_level_pool.py's
downstream-guarantee test; selection-among-candidates is replaced by
nomination ordering (test 1 here); size control is replaced by the area
cap (test 4); and the production setback is replaced by test 8, which
asserts the gate is gone.
"""

import json
import logging
import math

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.prepared import prep

from feature_schema import validate_feature_collection
from production_area import METERS_PER_FOOT
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    pixel_center_xy,
)
from valley_delineation import get_flow_accumulation_for_dem
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    fill_depressions,
)
from valley_level_pool import POOL_REFERENCE_HEIGHT_METERS
from water_candidate_zones import (
    FLAG_OVERLAP_TRIMMED,
    FLAG_SEED_SNAPPED,
    FLAG_TRUNCATED_BY_BOUNDARY,
    FLAG_TRUNCATED_BY_CAP,
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    MAX_WATER_ZONE_AREA_ACRES,
    MAX_WATER_ZONE_CANDIDATES,
    MIN_BOUNDARY_SETBACK_METERS,
    MIN_WATER_SEED_SEPARATION_METERS,
    MIN_WATER_ZONE_AREA_ACRES,
    NOMINATED_BY_ACCUMULATION,
    NOMINATED_BY_KEYPOINT,
    REASON_BELOW_MIN_AREA,
    REASON_NO_ELIGIBLE_CELL_WITHIN_SNAP,
    REASON_NOMINATED,
    WATER_KEYPOINT_SEED_SNAP_METERS,
    WATER_ZONE_RENDER_OPENING_RADIUS_METERS,
    compute_water_eligible_cells,
    find_candidate_zones,
    reason_too_close_to_candidate,
    zones_to_geojson,
)
import water_candidate_zones as wcz

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)


# --- capture the wipeout-fallback warnings the render opening logs, so ---
# --- test 8 can assert one fired and the end-of-file report can count  ---
# --- how many fixtures across the whole run triggered it.              ---
_wipeout_messages: list[str] = []


class _WipeoutHandler(logging.Handler):
    def emit(self, record):
        msg = record.getMessage()
        if "eroded" in msg and "falling back to polygon_utm" in msg:
            _wipeout_messages.append(msg)


wcz._LOGGER.addHandler(_WipeoutHandler())
wcz._LOGGER.setLevel(logging.WARNING)


def _rect_cells(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask_from_cells(shape, cells):
    mask = np.zeros(shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return mask


def _dem(array):
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": CRS,
    }


def _hydrology(dem):
    """The four D8 arrays find_candidate_zones() would otherwise derive
    itself -- computed here so a fixture can hand them in and be sure the
    nomination is running against the same flow field the test reasons
    about."""
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)
    return filled, flow_to_row, flow_to_col, accumulation


def _run_with_injected(dem, mask, accum, production_areas, boundary, **kwargs):
    """find_candidate_zones() with an injected eligible mask, isolating
    nomination/delineation/finishing from the per-cell gates. The
    accumulation grid is injected through the real `flow_accumulation`
    OVERRIDE (not by monkeypatching a module attribute), which is also
    what pins that the override is genuinely forwarded rather than
    recomputed. Returns (zones, wiped_out_bool)."""
    orig_compute = wcz.compute_water_eligible_cells
    wcz.compute_water_eligible_cells = lambda *a, **kw: mask
    before = len(_wipeout_messages)
    kwargs.setdefault("keypoints", [])
    kwargs.setdefault("flow_accumulation", accum)
    try:
        zones = find_candidate_zones(dem, production_areas, boundary, **kwargs)
    finally:
        wcz.compute_water_eligible_cells = orig_compute
    return zones, (len(_wipeout_messages) > before)


def _assert_bounded(zone, boundary, label):
    """render_fill_polygon_utm must be a SUBSET of polygon_utm (the opening
    is clipped to it) and stay within the boundary. NOT equal -- the
    opening is smaller than polygon_utm (unless it wiped out and fell back)."""
    rf = zone["render_fill_polygon_utm"]
    pu = zone["polygon_utm"]
    assert rf.area <= pu.area * (1 + 1e-9) + 1e-6, f"{label}: render_fill must not exceed polygon_utm area"
    assert boundary.buffer(1e-6).contains(rf), f"{label}: render_fill must stay within the boundary"


assert MIN_BOUNDARY_SETBACK_METERS == 0.0
assert MAX_VALLEY_CONTRIBUTING_AREA_ACRES == 20.0
assert MIN_WATER_ZONE_AREA_ACRES == 0.1
assert MAX_WATER_ZONE_AREA_ACRES == 2.0
assert MAX_WATER_ZONE_CANDIDATES == 3
assert WATER_KEYPOINT_SEED_SNAP_METERS == 15.0
assert MIN_WATER_SEED_SEPARATION_METERS == 30.0
assert POOL_REFERENCE_HEIGHT_METERS == 2.5
assert WATER_ZONE_RENDER_OPENING_RADIUS_METERS == 5.0
assert not hasattr(wcz, "WATER_ZONE_TARGET_ACRES"), (
    "the fixed survey-area target is RETIRED -- zone size emerges from the terrain now"
)
assert not hasattr(wcz, "WATER_ZONE_PRODUCTION_SETBACK_METERS"), (
    "the production-overlap setback constant is DELETED, not zeroed -- see the module docstring"
)
assert not hasattr(wcz, "_grow_zone_cells"), "the greedy-growth helper is DELETED"


# =====================================================================
# Single straight drainage column at col=20 on a 40x40 grid (200x200m at
# 5m). Accumulation decreases from ~1600 (row 0 outlet) to ~40 (row 39).
# =====================================================================
SIZE = 40
MID_COL = 20
_single_column_array = np.zeros((SIZE, SIZE), dtype=np.float32)
for _row in range(SIZE):
    for _col in range(SIZE):
        _single_column_array[_row, _col] = abs(_col - MID_COL) * 2.0 + _row * 0.5
SINGLE_COLUMN_DEM = _dem(_single_column_array)
BOUNDARY = box(500000.0, 4499800.0, 500200.0, 4500000.0)

PRODUCTION_AREA_ABOVE = [
    {
        "id": 0,
        "representative_elevation_m": -5.0,
        "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
        "render_fill_polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
    }
]
CELL_AREA_ACRES = cell_area_acres(SINGLE_COLUMN_DEM)

# A big flat grid + one production area within service distance of every
# cluster, for the injected-mask growth/opening tests.
BIG = 60
BIG_DEM = _dem(np.full((BIG, BIG), 100.0, dtype=np.float32))
BIG_BOUNDARY = box(500000.0, 4500000.0 - BIG * 5.0, 500000.0 + BIG * 5.0, 4500000.0)
CENTER_PA = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500140.0, 4499840.0, 500160.0, 4499860.0),
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),  # off-grid -> no production exclusion
    }
]


# =====================================================================
# Test A -- BOUNDARY INDEPENDENCE (headline property) + old-band contrast.
# (compute_water_eligible_cells only -- no growth/opening involved.)
# =====================================================================
def _new_eligible_set(dem, pa, boundary):
    m = compute_water_eligible_cells(dem, pa, boundary)
    return {(int(r), int(c)) for r, c in np.argwhere(m)}


def _old_percentile_band_set(dem, boundary, floor_acres=0.4, p_low=25.0, p_high=75.0):
    flow = get_flow_accumulation_for_dem(dem)
    apc = cell_area_acres(dem)
    floor_mask = flow >= (floor_acres / apc)
    bp = prep(boundary)
    pop = [
        float(flow[r, c])
        for r, c in np.argwhere(floor_mask)
        if bp.contains(Point(*pixel_center_xy(dem, int(r), int(c))))
    ]
    if not pop:
        return set()
    lo, hi = np.percentile(pop, p_low), np.percentile(pop, p_high)
    band = floor_mask & (flow >= lo) & (flow <= hi)
    return {
        (int(r), int(c))
        for r, c in np.argwhere(band)
        if bp.contains(Point(*pixel_center_xy(dem, int(r), int(c))))
    }


BOUNDARY_WIDE = box(500000.0, 4499800.0, 500200.0, 4500000.0)
BOUNDARY_NARROW = box(500060.0, 4499860.0, 500200.0, 4500000.0)
_shared = prep(BOUNDARY_WIDE.intersection(BOUNDARY_NARROW))
_shared_cells = {
    (int(r), int(c))
    for r, c in np.argwhere(np.ones((SIZE, SIZE), dtype=bool))
    if _shared.contains(Point(*pixel_center_xy(SINGLE_COLUMN_DEM, int(r), int(c))))
}
assert any(c == MID_COL for _r, c in _shared_cells), "shared area must contain the channel"

_new_wide = _new_eligible_set(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY_WIDE) & _shared_cells
_new_narrow = _new_eligible_set(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY_NARROW) & _shared_cells
assert _new_wide == _new_narrow, "BOUNDARY INDEPENDENCE VIOLATED: absolute-ceiling set must match within shared area"
_old_wide = _old_percentile_band_set(SINGLE_COLUMN_DEM, BOUNDARY_WIDE) & _shared_cells
_old_narrow = _old_percentile_band_set(SINGLE_COLUMN_DEM, BOUNDARY_NARROW) & _shared_cells
assert _old_wide != _old_narrow, "contrast: old percentile band should differ within the shared area"
print(
    f"Test A -- boundary independence: NEW gate identical within shared area ({len(_new_wide)} cells each); "
    f"OLD band differs ({len(_old_wide)} vs {len(_old_narrow)}, symdiff {len(_old_wide ^ _old_narrow)})."
)


# =====================================================================
# Test B -- ABSOLUTE CEILING, no lower bound. (compute_water_eligible_cells.)
# =====================================================================
_ceiling_dem = _dem(np.zeros((1, 6), dtype=np.float32))
_ceiling_boundary = box(500000.0, 4500000.0 - 5.0, 500000.0 + 6 * 5.0, 4500000.0)
_cc = MAX_VALLEY_CONTRIBUTING_AREA_ACRES / cell_area_acres(_ceiling_dem)
_ceiling_accum = np.array([[1.0, _cc - 5.0, _cc, _cc + 5.0, _cc * 3.0, 50.0]], dtype=np.float64)
_ceiling_pa = [
    {
        "id": 0,
        "representative_elevation_m": -5.0,
        "polygon_utm": _ceiling_boundary,
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
    }
]
_orig_flow = wcz.get_flow_accumulation_for_dem
wcz.get_flow_accumulation_for_dem = lambda dem: _ceiling_accum
try:
    _ceiling_mask = compute_water_eligible_cells(_ceiling_dem, _ceiling_pa, _ceiling_boundary)
finally:
    wcz.get_flow_accumulation_for_dem = _orig_flow
assert bool(_ceiling_mask[0, 0]) and bool(_ceiling_mask[0, 5]), "cells far below the ceiling must be eligible (no lower bound)"
assert bool(_ceiling_mask[0, 1]) and bool(_ceiling_mask[0, 2]), "just-below and exactly-at the ceiling must be eligible"
assert not bool(_ceiling_mask[0, 3]) and not bool(_ceiling_mask[0, 4]), "above the ceiling must be excluded"
print("Test B -- absolute ceiling: cells at/below 20 acres eligible (incl. accum=1, no lower bound), above excluded.")

# =====================================================================
# Tests 6-9 -- THE RENDER OPENING. Unchanged behaviour (the bounded disc
# opening that produces render_fill_polygon_utm did not change in this
# rewrite), but driven DIRECTLY against wcz._render_opening() now rather
# than through an injected eligible mask: zone shape is no longer the
# caller's to choose -- it comes from the level pool -- so the only honest
# way to pin the opening's own behaviour on a chosen shape is to hand it
# that shape.
# =====================================================================
def _open(cells, dem, grid_shape):
    """wcz._render_opening() on an arbitrary cell set, with its own
    polygon_utm built the same way find_candidate_zones() builds it.
    Returns (render_fill, polygon_utm, wiped_out_bool)."""
    mask = _mask_from_cells(grid_shape, cells)
    polygon_utm = cell_union_footprint(dem, mask)
    before = len(_wipeout_messages)
    render_fill = wcz._render_opening(mask, list(cells), grid_shape, dem, polygon_utm)
    return render_fill, polygon_utm, len(_wipeout_messages) > before


# Test 6 & 7 -- boundedness, and the opening trims a 1-wide protrusion
# while preserving the body. A solid 8x9 body plus a 1-cell-wide,
# 3-cell-long finger: a disc r=1 opening removes 1-wide protrusions beyond
# one cell of dilation regrowth, so the finger's outer two cells go and the
# body stays. (A flush single-cell bump WOULD be regrown by the dilation
# and is not a valid "removed" case -- hence a 1-wide finger.)
_body = _rect_cells(20, 28, 20, 29)          # rows 20-27, cols 20-28 -> 8x9 = 72 cells
_finger = [(24, 29), (24, 30), (24, 31)]      # 1-wide, 3-long, off the east edge at row 24
_bs_rf, _bs_pu, _bs_wiped = _open(_body + _finger, BIG_DEM, (BIG, BIG))
assert not _bs_wiped, "TEST 7: a solid 8x9 body must survive the r=1 opening (no wipeout)"
assert _bs_rf.area <= _bs_pu.area * (1 + 1e-9) + 1e-6, "TEST 6: render_fill must not exceed polygon_utm"
_tip_pt = Point(*pixel_center_xy(BIG_DEM, 24, 31))
_mid_pt = Point(*pixel_center_xy(BIG_DEM, 24, 30))
assert not _bs_rf.buffer(-1e-6).contains(_tip_pt), "TEST 7: the finger tip must be trimmed by the opening"
assert not _bs_rf.buffer(-1e-6).contains(_mid_pt), "TEST 7: the finger's middle cell must be trimmed too"
_ratio = _bs_rf.area / _bs_pu.area
assert _bs_rf.area < _bs_pu.area, "TEST 7: the opening must trim the finger (and round corners)"
assert _ratio > 0.6, f"TEST 7: the body must be substantially preserved, got ratio {_ratio:.3f}"
print(
    f"Test 6/7 -- opening: render_fill is a subset of polygon_utm; the 1-wide finger's outer cells are "
    f"trimmed and the body is substantially preserved (drawn-to-polygon_utm area ratio {_ratio:.3f})."
)

# Test 8 -- WIPEOUT FALLBACK: a shape thinner than the opening radius
# throughout (a 2-cell-wide line, under 2r+1 = 3) erodes to nothing;
# render_fill falls back to polygon_utm, non-empty, logged once.
_wipe_before = len(_wipeout_messages)
_thin_rf, _thin_pu, _thin_wiped = _open(_rect_cells(10, 30, 10, 12), BIG_DEM, (BIG, BIG))
assert _thin_wiped, "TEST 8: a 2-cell-wide shape must trigger the wipeout fallback"
assert len(_wipeout_messages) == _wipe_before + 1, "the wipeout must be logged exactly once"
assert not _thin_rf.is_empty, "the fallback render_fill must be non-empty"
assert _thin_rf.equals(_thin_pu), "TEST 8: on wipeout, render_fill must fall back to polygon_utm exactly"
print(
    "Test 8 -- wipeout fallback: a 2-cell-wide shape erodes to nothing under the r=1 opening; render_fill "
    "falls back to polygon_utm (non-empty), logged once."
)

# Test 9 -- the opening can still produce a MultiPolygon (a severed
# pinch), which render_layout_map.py already tolerates.
_dumb_lobe_a = _rect_cells(10, 16, 8, 13)    # 6x5 = 30
_dumb_neck = [(12, 13), (12, 14), (12, 15), (12, 16), (12, 17)]  # 1-cell-tall neck across a 5-wide gap
_dumb_lobe_b = _rect_cells(10, 16, 18, 23)   # 6x5 = 30
_dumb_rf, _dumb_pu, _dumb_wiped = _open(_dumb_lobe_a + _dumb_neck + _dumb_lobe_b, BIG_DEM, (BIG, BIG))
assert not _dumb_wiped, "the wide lobes must survive the opening (only the neck is severed)"
assert _dumb_rf.geom_type == "MultiPolygon", (
    f"TEST 9: the opening should sever the too-narrow neck, leaving a MultiPolygon, got {_dumb_rf.geom_type}"
)
print(
    f"Test 9 -- opening may split: the dumbbell's render_fill is a {_dumb_rf.geom_type} with "
    f"{len(_dumb_rf.geoms)} parts (the too-narrow neck is severed), which render_layout_map.py tolerates."
)
# =====================================================================
# Test C -- OFF-PARCEL exclusion survives the zeroed setback.
# (compute_water_eligible_cells only.)
# =====================================================================
_op_size = 20
_op_array = np.zeros((_op_size, _op_size), dtype=np.float32)
for _row in range(_op_size):
    for _col in range(_op_size):
        _op_array[_row, _col] = abs(_col - 10) * 2.0 + _row * 0.5
_OP_DEM = _dem(_op_array)
_OP_BOUNDARY = box(500050.0, 4499900.0, 500100.0, 4500000.0)  # eastern half only
_OP_PA = [
    {
        "id": 0,
        "representative_elevation_m": -5.0,
        "polygon_utm": box(500060.0, 4499910.0, 500090.0, 4499940.0),
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
    }
]
_op_mask = compute_water_eligible_cells(_OP_DEM, _OP_PA, _OP_BOUNDARY)
_op_prepared = prep(_OP_BOUNDARY)
_off = [
    (int(r), int(c))
    for r, c in np.argwhere(_op_mask)
    if not _op_prepared.contains(Point(*pixel_center_xy(_OP_DEM, int(r), int(c))))
]
assert not _off, f"OFF-PARCEL exclusion failed with zeroed setback: {_off[:5]}"
assert int(_op_mask.sum()) > 0, "some on-parcel cells should still be eligible"
print(f"Test C -- off-parcel exclusion survives the zeroed setback: {int(_op_mask.sum())} on-parcel eligible, 0 off-parcel.")

# =====================================================================
# Retained gate coverage (compute_water_eligible_cells masks) + one real
# end-to-end single-column integration run.
# =====================================================================
assert find_candidate_zones(SINGLE_COLUMN_DEM, [], BOUNDARY) == []
print("Gate -- no production areas means no water zones.")

# Real end-to-end on the single-column DEM, with NOTHING injected: real
# hydrology, real keypoint detection, real nomination, real delineation.
# The channel is 1-2 cells wide, thinner than the opening radius, so the
# render fill wipes out and falls back to polygon_utm -- expected for a
# degenerate 1-cell channel (a real, multi-cell-wide band survives, tests
# 6/7).
_col_before = len(_wipeout_messages)
_base_diag: dict = {}
_base_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, diagnostics=_base_diag
)
assert _base_zones, "the single-column fixture must produce at least one candidate"
assert len(_base_zones) <= MAX_WATER_ZONE_CANDIDATES
_base_zone = _base_zones[0]
_base_area = _base_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _base_zone["id"] == 0 and _base_zone["served_production_area_ids"] == [0]
assert _base_zone["primary_production_area_relationship"]["above_production_area"] is True
assert _base_zone["anchor_rowcol"] in _base_zone["cells"], "the anchor must be a member of its own zone"
assert _base_area <= MAX_WATER_ZONE_AREA_ACRES + 1e-9
_base_wiped = len(_wipeout_messages) > _col_before
_assert_bounded(_base_zone, BOUNDARY, "single-column")
print(
    f"Gate -- real single-column end-to-end (no injection): {len(_base_zones)} candidate(s), zone 0 at "
    f"{_base_area:.4f} ac anchored on {_base_zone['anchor_rowcol']} by {_base_zone['nominated_by']}, "
    f"render fill {'wiped out -> polygon_utm (thin channel, expected)' if _base_wiped else 'survived the opening'}."
)

# Gravity is a preference: a production area ABOVE the column still yields zones.
PRODUCTION_AREA_BELOW = [
    {
        "id": 5,
        "representative_elevation_m": 100.0,
        "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
        "render_fill_polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
    }
]
_below = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_BELOW, BOUNDARY)
assert _below and _below[0]["primary_production_area_relationship"]["above_production_area"] is False
print("Gate -- a below-elevation (pump-required) production area still yields real zones.")

_baseline_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)

# Max service distance still enforced. NOTE the fixture change: this used
# to be asserted by shrinking max_service_distance_meters to 1.0 against a
# production area sitting ON the grid, which returned [] only because the
# now-DELETED production-overlap gate also excluded the handful of cells
# inside that patch. With that gate gone those cells are eligible (they are
# 0 m from the ground they serve), so the honest fixture is a production
# area genuinely out of range: ~1000 m east of the grid, past the 800 m
# default.
PRODUCTION_AREA_OUT_OF_RANGE = [
    {
        "id": 9,
        "representative_elevation_m": -5.0,
        "polygon_utm": box(501200.0, 4499850.0, 501230.0, 4499900.0),
        "render_fill_polygon_utm": box(501200.0, 4499850.0, 501230.0, 4499900.0),
    }
]
assert find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_OUT_OF_RANGE, BOUNDARY) == []
# And the threshold itself still binds: shrinking it to 1.0 m against the
# in-range patch leaves only the cells at/inside that patch eligible.
_tight_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, max_service_distance_meters=1.0
)
assert 0 < int(_tight_mask.sum()) < int(_baseline_mask.sum()) / 10, int(_tight_mask.sum())
print(
    f"Gate -- max service distance is still a real, enforced generation-time filter: a production area "
    f"~1000 m away yields no zones at all, and shrinking the threshold to 1.0 m against the in-range patch "
    f"cuts the eligible set from {int(_baseline_mask.sum())} to {int(_tight_mask.sum())} cells."
)

# Canopy / road exclusion on the mask.
_all_trees = np.ones(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_no_trees = np.zeros(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees).sum()) == 0
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_no_trees).sum()) == int(_baseline_mask.sum())
assert find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees) == []
print("Gate -- canopy: all-trees mask excludes everything; all-clear matches baseline.")

assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=BOUNDARY).sum()) == 0
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=None).sum()) == int(_baseline_mask.sum())
print("Gate -- road: whole-boundary union excludes everything; None is a no-op.")

# The flow_accumulation override is genuinely FORWARDED, not recomputed:
# a grid of ones puts every cell under the ceiling, so the eligible count
# must match the whole on-parcel set rather than the real-hydrology one.
# a 0.05-acre ceiling (8 cells at 5 m) that real hydrology mostly fails,
# against an all-ones grid (0.0062 acres per cell) that every cell passes.
_TIGHT_CEILING_ACRES = 0.05
_ones = np.ones(SINGLE_COLUMN_DEM["array"].shape, dtype=np.float64)
_ones_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY,
    max_valley_contributing_area_acres=_TIGHT_CEILING_ACRES, flow_accumulation=_ones,
)
_real_tight_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY,
    max_valley_contributing_area_acres=_TIGHT_CEILING_ACRES,
)
assert int(_real_tight_mask.sum()) < int(_ones_mask.sum()), (
    "the injected accumulation grid must actually change the ceiling gate's answer -- otherwise this "
    "asserts nothing about forwarding"
)
assert int(_ones_mask.sum()) == int(_baseline_mask.sum()), (
    "with every cell at accumulation 1 the ceiling cannot bind, so the tight-ceiling mask must match the "
    "wide-open baseline"
)
print(
    f"Override -- flow_accumulation is forwarded, not recomputed: at a {_TIGHT_CEILING_ACRES}-acre ceiling, "
    f"real hydrology leaves {int(_real_tight_mask.sum())} eligible cells while an injected all-ones grid "
    f"leaves {int(_ones_mask.sum())}."
)
# =====================================================================
# Road gate at the SHARED buffer: water zones now clear existing roads at
# farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS (5.0m), not the deleted
# per-module 3.048m value -- a REAL behaviour change to water-zone
# eligibility, not just a constant rename, demonstrated inline. A road
# centerline is placed 3.75m from the channel's eligible cell centers:
# outside the old 3.048m buffer (those cells stayed eligible), inside the
# new 5.0m one (they are excluded now). Both unions are built by the real
# producer (farm_roads_data.get_road_exclusion_union_utm) from the SAME
# road line -- only the buffer differs.
# =====================================================================

from rasterio.warp import transform as _rb_warp_transform  # noqa: E402

import farm_roads_data  # noqa: E402

assert farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS == 5.0, (
    "this section demonstrates the 3.048m -> 5.0m water-zone road-clearance change -- update it if the "
    f"shared constant is retuned (currently {farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS})"
)
_OLD_WATER_ROAD_BUFFER_M = 3.048  # the deleted per-module value, kept here only as the contrast

# Vertical road line 3.75m east of the channel column's cell centers.
_rb_channel_x = 500000.0 + (MID_COL + 0.5) * RESOLUTION[0]
_rb_road_x = _rb_channel_x + 3.75
_rb_lons, _rb_lats = _rb_warp_transform(CRS, "EPSG:4326", [_rb_road_x, _rb_road_x], [4500000.0 + 50.0, 4499800.0 - 50.0])
_rb_farm_roads = [{
    "name": "Synthetic Water-Adjacent Rd",
    "geometry": {"type": "LineString", "coordinates": [list(pt) for pt in zip(_rb_lons, _rb_lats)]},
}]
_rb_boundary_lons, _rb_boundary_lats = _rb_warp_transform(CRS, "EPSG:4326", *[list(c) for c in BOUNDARY.exterior.coords.xy])
_rb_boundary_coords = list(zip(_rb_boundary_lons, _rb_boundary_lats))

_rb_union_shared = farm_roads_data.get_road_exclusion_union_utm(_rb_boundary_coords, SINGLE_COLUMN_DEM, farm_roads=_rb_farm_roads)
_rb_union_old = farm_roads_data.get_road_exclusion_union_utm(
    _rb_boundary_coords, SINGLE_COLUMN_DEM, buffer_meters=_OLD_WATER_ROAD_BUFFER_M, farm_roads=_rb_farm_roads
)

_rb_mask_shared = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=_rb_union_shared)
_rb_mask_old = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=_rb_union_old)

# Under the old 3.048m buffer the 3.75m-away channel cells stayed eligible...
assert int((_rb_mask_old & _baseline_mask)[:, MID_COL].sum()) == int(_baseline_mask[:, MID_COL].sum()), (
    "contrast precondition: at the old 3.048m buffer, channel cells 3.75m from the road centerline must "
    "remain eligible"
)
# ...and at the shared 5.0m they are excluded.
assert int(_rb_mask_shared[:, MID_COL].sum()) == 0, (
    "channel cells whose centers sit 3.75m from the road centerline -- outside the old 3.048m buffer, "
    "inside the shared 5.0m one -- must now be road-excluded"
)
_rb_newly_excluded = _baseline_mask & _rb_mask_old & ~_rb_mask_shared
_rb_acres_diff = float(_rb_newly_excluded.sum()) * CELL_AREA_ACRES
assert _rb_acres_diff > 0.0
# ...and the change is real at the ZONE level too, not just per-cell: report
# whether a candidate still forms once the road claims the channel's flank.
_rb_zones_old = _run_with_injected(SINGLE_COLUMN_DEM, _rb_mask_old, get_flow_accumulation_for_dem(SINGLE_COLUMN_DEM), PRODUCTION_AREA_ABOVE, BOUNDARY)[0]
_rb_zones_shared = _run_with_injected(SINGLE_COLUMN_DEM, _rb_mask_shared, get_flow_accumulation_for_dem(SINGLE_COLUMN_DEM), PRODUCTION_AREA_ABOVE, BOUNDARY)[0]
print(
    f"Road gate at the SHARED 5.0m buffer: {int(_rb_newly_excluded.sum())} cell(s) / {_rb_acres_diff:.3f} "
    f"acres of water-eligible ground 3.75m from a road centerline are excluded now that were NOT under the "
    f"deleted 3.048m per-module buffer; candidate zones on this fixture: {len(_rb_zones_old)} (old buffer) "
    f"-> {len(_rb_zones_shared)} (shared buffer)"
    + (" -- the candidates themselves changed." if len(_rb_zones_old) != len(_rb_zones_shared)
       or any(a["cells"] != b["cells"] for a, b in zip(_rb_zones_old, _rb_zones_shared))
       else " -- same candidates, smaller eligible ground.")
)

# =====================================================================
# Test E -- SERVICE-DISTANCE GATE REMOVAL: cells adjacent to production
# (2-8 m) are now eligible. Under the removed 10 m min-service-distance
# gate they would have been rejected as "too close." The contrast is
# computed inline so the change is demonstrated, not just asserted.
# (compute_water_eligible_cells only.)
# =====================================================================
_adj_nr, _adj_nc = 6, 12
_adj_dem = _dem(np.full((_adj_nr, _adj_nc), 100.0, dtype=np.float32))
_adj_boundary = box(500000.0, 4500000.0 - _adj_nr * 5.0, 500000.0 + _adj_nc * 5.0, 4500000.0)
# Production polygon_utm's west edge at x=500030.5: cells in col 5 sit 3 m
# west of it and col 4 sit 8 m west -- both inside the removed 10 m gate.
# render_fill is off-grid so the production-exclusion gate excludes nothing
# (this fixture isolates the service-distance change).
_adj_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500030.5, 4499960.0, 500200.0, 4500010.0),
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
    }
]
_orig_flow_e = wcz.get_flow_accumulation_for_dem
wcz.get_flow_accumulation_for_dem = lambda d: np.ones((_adj_nr, _adj_nc), dtype=np.float64)
try:
    _adj_mask = compute_water_eligible_cells(_adj_dem, _adj_pa, _adj_boundary)
finally:
    wcz.get_flow_accumulation_for_dem = _orig_flow_e
# The adjacent columns (3 m and 8 m from production) are eligible now.
assert all(_adj_mask[r, 4] for r in range(_adj_nr)), "col 4 (8 m from production) must be eligible now"
assert all(_adj_mask[r, 5] for r in range(_adj_nr)), "col 5 (3 m from production) must be eligible now"
# Inline contrast: re-apply the REMOVED gate (reject a cell whose only
# in-range production area sits 0 < distance < 10 m away). This is exactly
# what compute_water_eligible_cells() used to do and no longer does.
_REMOVED_MIN_SERVICE = 10.0
_adj_patch_poly = _adj_pa[0]["polygon_utm"]
_adj_old_excluded = [
    (int(r), int(c))
    for r, c in np.argwhere(_adj_mask)
    if 0 < Point(*pixel_center_xy(_adj_dem, int(r), int(c))).distance(_adj_patch_poly) < _REMOVED_MIN_SERVICE
]
_adj_new_count = int(_adj_mask.sum())
_adj_old_count = _adj_new_count - len(_adj_old_excluded)
assert len(_adj_old_excluded) == _adj_nr * 2, (
    f"the 2 adjacent columns ({_adj_nr * 2} cells) are the ones the removed gate rejected, got {len(_adj_old_excluded)}"
)
assert all(_adj_mask[r, c] for r, c in _adj_old_excluded), "cells the old gate would drop are all eligible under the new mask"
print(
    f"Test E -- service-distance gate removed: {_adj_new_count} cells eligible now vs {_adj_old_count} under the "
    f"removed 10 m min-service gate (delta {len(_adj_old_excluded)} cells at 3 m and 8 m from production, "
    "previously rejected as too close)."
)

# =====================================================================
# Test F -- THE PRODUCTION-OVERLAP GATE IS GONE. This REPLACES the old
# "5 m production setback is the surviving margin" test, which pinned a
# constant, a parameter and a gate that are all deleted now.
#
# Same fixture geometry as that test, at 1 m resolution so the distances
# land on exact cell centers: a production render fill covering columns
# 10.5-20 of a 6x20 grid. Under the deleted gate, a cell 3 m from that
# fill's west edge was excluded (inside the 5 m buffer) and a cell DEEP
# INSIDE the fill was excluded outright. Both are eligible now.
# =====================================================================
_pg_nr, _pg_nc = 6, 20
_pg_dem = {
    "array": np.full((_pg_nr, _pg_nc), 100.0, dtype=np.float32),
    "resolution_meters": (1.0, 1.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": CRS,
}
_pg_boundary = box(500000.0, 4500000.0 - _pg_nr * 1.0, 500000.0 + _pg_nc * 1.0, 4500000.0)
_pg_rf = box(500010.5, 4499990.0, 500020.0, 4500001.0)
_pg_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": _pg_rf,
        "render_fill_polygon_utm": _pg_rf,
    }
]
_pg_mask = compute_water_eligible_cells(
    _pg_dem, _pg_pa, _pg_boundary, flow_accumulation=np.ones((_pg_nr, _pg_nc), dtype=np.float64)
)
_d3 = Point(*pixel_center_xy(_pg_dem, 0, 7)).distance(_pg_rf)
assert abs(_d3 - 3.0) < 1e-9, f"fixture geometry: expected 3 m, got {_d3}"
assert all(_pg_mask[r, 7] for r in range(_pg_nr)), (
    "TEST F: a cell 3 m from the production fill's edge -- inside the DELETED 5 m setback -- is eligible now"
)
assert all(_pg_mask[r, 15] for r in range(_pg_nr)), (
    "TEST F: a cell DEEP INSIDE the production fill is eligible now; production overlap is the designer's "
    "call, not a generation-time gate"
)
assert int(_pg_mask.sum()) == _pg_nr * _pg_nc, "with no production gate, every on-parcel cell is eligible here"
# The old whole-parcel-overlap fixture, which used to leave ZERO eligible cells.
PRODUCTION_FULL_OVERLAP = [
    {
        "id": 0,
        "representative_elevation_m": -5.0,
        "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
        "render_fill_polygon_utm": BOUNDARY,
    }
]
_full_overlap_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_FULL_OVERLAP, BOUNDARY)
assert int(_full_overlap_mask.sum()) == int(_baseline_mask.sum()) > 0, (
    "TEST F: a production render fill covering the WHOLE parcel used to leave zero eligible cells; with "
    "the gate deleted it must leave exactly the baseline set"
)
print(
    f"Test F -- production-overlap gate DELETED: a cell 3 m from a production fill's edge and a cell deep "
    f"inside it are both eligible ({int(_pg_mask.sum())}/{_pg_nr * _pg_nc} cells eligible on that fixture), "
    f"and a render fill covering the whole parcel now leaves the full baseline {int(_baseline_mask.sum())} "
    "eligible cells instead of zero."
)
# =====================================================================
# Test G -- MAX SERVICE DISTANCE unaffected: a cell beyond 800 m from every
# production area is still excluded (default max_service_distance_meters);
# an in-range production area leaves cells eligible (contrast).
# (compute_water_eligible_cells only.)
# =====================================================================
_far_dem = _dem(np.full((6, 6), 100.0, dtype=np.float32))
_far_boundary = box(500000.0, 4500000.0 - 6 * 5.0, 500000.0 + 6 * 5.0, 4500000.0)
_far_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500900.0, 4499960.0, 500930.0, 4500010.0),  # ~872 m east of the nearest cell
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
    }
]
_near_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500100.0, 4499960.0, 500130.0, 4500010.0),  # comfortably within 800 m
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
    }
]
_orig_flow_g = wcz.get_flow_accumulation_for_dem
wcz.get_flow_accumulation_for_dem = lambda d: np.ones((6, 6), dtype=np.float64)
try:
    _far_mask = compute_water_eligible_cells(_far_dem, _far_pa, _far_boundary)
    _near_mask = compute_water_eligible_cells(_far_dem, _near_pa, _far_boundary)
finally:
    wcz.get_flow_accumulation_for_dem = _orig_flow_g
assert int(_far_mask.sum()) == 0, "TEST G: a production area ~872 m away (beyond 800 m) must leave no eligible cell"
assert int(_near_mask.sum()) > 0, "an in-range production area must leave eligible cells (contrast)"
print(f"Test G -- max service distance enforced: 0 eligible at ~872 m vs {int(_near_mask.sum())} within range.")

# =====================================================================
# NOMINATION FIXTURE -- three parallel V-valleys on one 40x40 grid at 5 m.
#
#     z(r, c) = 100.0 - 0.1 * r + 1.0 * min(|c-6|, |c-20|, |c-34|)
#
# i.e. channels down columns 6, 20 and 34, each with 20% side slopes, all
# running north -> south at a 2% grade, separated by ridges at columns 13
# and 27 that stand 7 m above the channel floors -- far higher than the
# 2.5 m reference waterline, so a pool on one channel can never reach
# another.
#
# Five synthetic keypoints, deliberately built so that ID ORDER IS NOT
# CATCHMENT ORDER (keypoint_detection.py assigns ids by slope_drop_pct
# descending, which is right for ITS layer and is not what this module
# nominates by):
#
#   id  rowcol     catchment   what it is here
#   --  --------   ---------   ---------------------------------------
#    0  (34,  6)      3.0 ac   30 m DOWNSTREAM of keypoint 1, on the same
#                              channel -- the "too close" half of the pair
#    1  (30,  6)      8.0 ac   the pair's winner on catchment
#    2  (30, 34)      6.0 ac   sits on an INELIGIBLE cell -> must snap
#    3  ( 5, 13)      4.0 ac   on a ridge, with every cell within 25 m
#                              made ineligible -> nothing to snap to
#    4  (30, 20)      7.0 ac   plainly eligible, no snap
#
# The injected eligibility mask is "everything, except (30, 34) and
# except a 5-cell block around (5, 13)". So:
#
# EXPECTED OUTCOMES, in catchment order (8.0, 7.0, 6.0, 4.0, 3.0):
#   keypoint 1 -> nominated,  candidate 0, anchored at (30, 6)
#   keypoint 4 -> nominated,  candidate 1, anchored at (30, 20)
#   keypoint 2 -> nominated,  candidate 2, seed_snapped, anchored at
#                 (29, 34): four cells sit exactly 5 m from (30, 34) and
#                 the (row, col) tie-break takes the smallest, so the snap
#                 distance is 5.0 m
#   keypoint 3 -> no_eligible_cell_within_snap  (nearest eligible cell is
#                 25 m away, past the 15 m snap radius)
#   keypoint 0 -> too_close_to_candidate_0      (its own cell is 20 m from
#                 candidate 0's footprint, inside the 30 m separation)
#
# IF ORDERING WERE BY ID (or by slope drop) instead of by catchment,
# keypoint 0 would be delineated FIRST and keypoint 1 would be the one
# rejected -- so asserting which of the pair survives IS the ordering
# assertion.
#
# The candidate cap is raised to 5 for this fixture so the cap does not
# mask the two rejection codes; family 2 then fills the remaining slots,
# which is what makes this a mixed-family run for the non-overlap
# invariant below.
# =====================================================================
_nom_n = 40
_nom_array = np.zeros((_nom_n, _nom_n), dtype=np.float64)
for _r in range(_nom_n):
    for _c in range(_nom_n):
        _nom_array[_r, _c] = 100.0 - 0.1 * _r + 1.0 * min(abs(_c - 6), abs(_c - 20), abs(_c - 34))
NOM_DEM = _dem(_nom_array)
NOM_BOUNDARY = box(500000.0, 4500000.0 - _nom_n * 5.0, 500000.0 + _nom_n * 5.0, 4500000.0)
NOM_PA = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500090.0, 4499890.0, 500110.0, 4499910.0),
        "render_fill_polygon_utm": box(500090.0, 4499890.0, 500110.0, 4499910.0),
    }
]
_nom_filled, _nom_ftr, _nom_ftc, _nom_acc = _hydrology(NOM_DEM)

_nom_mask = np.ones((_nom_n, _nom_n), dtype=bool)
_nom_mask[30, 34] = False                      # keypoint 2 must snap off this cell
_nom_mask[0:11, 8:19] = False                  # a 25 m-plus dead zone around keypoint 3 at (5, 13)


def _kp(kp_id, valley_id, rowcol, contributing_acres):
    x, y = pixel_center_xy(NOM_DEM, *rowcol)
    return {
        "id": kp_id,
        "valley_id": valley_id,
        "rowcol": rowcol,
        "point_utm": Point(x, y),
        "contributing_acres": contributing_acres,
    }


NOM_KEYPOINTS = [
    _kp(0, 0, (34, 6), 3.0),
    _kp(1, 0, (30, 6), 8.0),
    _kp(2, 2, (30, 34), 6.0),
    _kp(3, 1, (5, 13), 4.0),
    _kp(4, 1, (30, 20), 7.0),
]
# Precondition: the nearest eligible cell to keypoint 3 really is outside
# the snap radius, so the reason code below is earned rather than assumed.
_kp3_nearest = min(
    (
        math.hypot((c - 13) * 5.0, (r - 5) * 5.0)
        for r, c in np.argwhere(_nom_mask)
    )
)
assert _kp3_nearest > WATER_KEYPOINT_SEED_SNAP_METERS, _kp3_nearest

_nom_diag: dict = {}
_nom_zones, _ = _run_with_injected(
    NOM_DEM,
    _nom_mask,
    _nom_acc,
    NOM_PA,
    NOM_BOUNDARY,
    keypoints=NOM_KEYPOINTS,
    filled=_nom_filled,
    flow_to_row=_nom_ftr,
    flow_to_col=_nom_ftc,
    max_water_zone_candidates=5,
    diagnostics=_nom_diag,
)

_outcomes = {o["keypoint_id"]: o for o in _nom_diag["keypoint_outcomes"]}
_order = [o["keypoint_id"] for o in _nom_diag["keypoint_outcomes"]]
assert _order == [1, 4, 2, 3, 0], f"keypoints must be processed by CATCHMENT descending, got {_order}"

assert _outcomes[1]["outcome"] == REASON_NOMINATED and _outcomes[1]["candidate_id"] == 0
assert _outcomes[1]["anchor_rowcol"] == (30, 6)
assert _outcomes[1]["seed_snapped"] is False

assert _outcomes[4]["outcome"] == REASON_NOMINATED and _outcomes[4]["candidate_id"] == 1
assert _outcomes[4]["anchor_rowcol"] == (30, 20)

assert _outcomes[2]["outcome"] == REASON_NOMINATED and _outcomes[2]["candidate_id"] == 2
assert _outcomes[2]["seed_snapped"] is True
assert _outcomes[2]["anchor_rowcol"] == (29, 34), _outcomes[2]["anchor_rowcol"]
assert _outcomes[2]["seed_snap_distance_m"] == 5.0
assert FLAG_SEED_SNAPPED in _outcomes[2]["flags"]

assert _outcomes[3]["outcome"] == REASON_NO_ELIGIBLE_CELL_WITHIN_SNAP, _outcomes[3]
assert _outcomes[3]["candidate_id"] is None
assert _outcomes[3]["anchor_rowcol"] is None

assert _outcomes[0]["outcome"] == reason_too_close_to_candidate(0), _outcomes[0]
assert _outcomes[0]["candidate_id"] is None
assert _outcomes[0]["anchor_rowcol"] == (34, 6), "the seed was found; it was the SEPARATION rule that stopped it"

# The keypoint-nominated zones carry BOTH positions, separately.
_snapped_zone = _nom_zones[2]
assert _snapped_zone["nominated_by"] == NOMINATED_BY_KEYPOINT
assert _snapped_zone["keypoint_id"] == 2 and _snapped_zone["valley_id"] == 2
assert _snapped_zone["keypoint_rowcol"] == (30, 34), "the keypoint's OWN position must survive the snap"
assert _snapped_zone["anchor_rowcol"] == (29, 34), "the anchor is where the pool was actually delineated"
assert _snapped_zone["keypoint_rowcol"] != _snapped_zone["anchor_rowcol"]
assert _snapped_zone["keypoint_point_utm"].equals(Point(*pixel_center_xy(NOM_DEM, 30, 34)))
assert _snapped_zone["anchor_point_utm"].equals(Point(*pixel_center_xy(NOM_DEM, 29, 34)))

print(
    "Test 1 -- nomination reason codes: keypoints are processed in CATCHMENT order "
    f"{_order} (not id/slope-drop order); keypoint 1 (8.0 ac) wins the too-close pair and keypoint 0 "
    f"(3.0 ac, 20 m away) is rejected with '{_outcomes[0]['outcome']}'; keypoint 2 snaps "
    f"{_outcomes[2]['seed_snap_distance_m']} m off an ineligible cell and keeps BOTH positions "
    f"({_snapped_zone['keypoint_rowcol']} detected, {_snapped_zone['anchor_rowcol']} delineated); "
    f"keypoint 3 has nothing within {WATER_KEYPOINT_SEED_SNAP_METERS} m and reports "
    f"'{_outcomes[3]['outcome']}'."
)


# =====================================================================
# Test 2 -- NON-OVERLAP INVARIANT across a mixed family-1/family-2 run,
# and overlap_trimmed behaviour.
#
# The nomination fixture above produces 3 keypoint-nominated candidates
# and then lets family 2 fill the remaining slots of a 5-candidate cap, so
# both families are present in one run. find_candidate_zones() itself
# RAISES on an overlap, so this re-checks it independently here (a bug
# that removed the internal assertion would otherwise go unnoticed).
# =====================================================================
assert len(_nom_zones) > 3, f"the fixture must reach family 2 to be a mixed run, got {len(_nom_zones)}"
_families = {z["nominated_by"] for z in _nom_zones}
assert _families == {NOMINATED_BY_KEYPOINT, NOMINATED_BY_ACCUMULATION}, _families
_seen: set = set()
for _z in _nom_zones:
    assert not _seen.intersection(_z["cells"]), f"candidate {_z['id']} overlaps an earlier candidate"
    _seen.update(_z["cells"])
    assert _z["anchor_rowcol"] in _z["cells"], "every candidate must contain its own anchor"
    _z_mask = _mask_from_cells((_nom_n, _nom_n), _z["cells"])
    assert connected_components(_z_mask, connectivity=8)[1] == 1, (
        f"candidate {_z['id']} must be a single 8-connected component (the pool is a flow tree)"
    )
assert _nom_zones[0]["overlap_trimmed"] is False, "the first candidate has nothing to be trimmed against"

# overlap_trimmed itself. Pools only ever grow UPSTREAM, so two family-2
# seeds on one channel can never collide (family 2 takes the most
# downstream, highest-accumulation cell first, and every later seed sits
# further up). The collision that DOES happen is a keypoint pair: an
# upstream keypoint with the bigger catchment is delineated first, and a
# DOWNSTREAM keypoint nominated afterwards has a backwater that runs
# straight up over the first one's ground.
#
# Fixture: the fixture-1 V-valley (2% grade, 20% side slopes, channel down
# column 20) with two keypoints on it --
#   keypoint 0 at (30, 20), 9.0 ac  -> delineated first, pool = rows 6-30
#   keypoint 1 at (38, 20), 4.0 ac  -> seed is 37.5 m from candidate 0's
#                                      footprint, so it CLEARS the 30 m
#                                      separation rule; but its own
#                                      waterline (96.2 + 2.5 = 98.7 m)
#                                      floods rows 14-38, i.e. 17 rows of
#                                      ground candidate 0 already claimed.
# EXPECTED: candidate 1 comes back overlap_trimmed=True, holding only the
# component containing its own anchor (rows 31-38), sharing no cell with
# candidate 0.
_ot_n = 40
_ot_array = np.zeros((_ot_n, _ot_n), dtype=np.float64)
for _r in range(_ot_n):
    for _c in range(_ot_n):
        _ot_array[_r, _c] = 100.0 - 0.1 * _r + 1.0 * abs(_c - 20)
OT_DEM = _dem(_ot_array)
OT_BOUNDARY = box(500000.0, 4500000.0 - _ot_n * 5.0, 500000.0 + _ot_n * 5.0, 4500000.0)
_ot_filled, _ot_ftr, _ot_ftc, _ot_acc = _hydrology(OT_DEM)


def _ot_kp(kp_id, rowcol, acres):
    x, y = pixel_center_xy(OT_DEM, *rowcol)
    return {
        "id": kp_id, "valley_id": 0, "rowcol": rowcol,
        "point_utm": Point(x, y), "contributing_acres": acres,
    }


_ot_zones, _ = _run_with_injected(
    OT_DEM,
    np.ones((_ot_n, _ot_n), dtype=bool),
    _ot_acc,
    NOM_PA,
    OT_BOUNDARY,
    keypoints=[_ot_kp(0, (30, 20), 9.0), _ot_kp(1, (38, 20), 4.0)],
    filled=_ot_filled,
    flow_to_row=_ot_ftr,
    flow_to_col=_ot_ftc,
    max_water_zone_candidates=2,
)
assert len(_ot_zones) == 2, f"the collision fixture must produce two candidates, got {len(_ot_zones)}"
assert _ot_zones[0]["anchor_rowcol"] == (30, 20) and _ot_zones[1]["anchor_rowcol"] == (38, 20)
assert _ot_zones[0]["overlap_trimmed"] is False, "the first candidate has nothing to be trimmed against"
assert _ot_zones[1]["overlap_trimmed"] is True, (
    "the downstream candidate's backwater runs up over the first one's ground and must be trimmed"
)
assert FLAG_OVERLAP_TRIMMED in _ot_zones[1]["flags"]
assert not set(_ot_zones[0]["cells"]).intersection(_ot_zones[1]["cells"])
assert min(r for r, _c in _ot_zones[1]["cells"]) == 31, (
    f"only the component containing the anchor survives -- expected rows 31+, got "
    f"{min(r for r, _c in _ot_zones[1]['cells'])}"
)
assert _ot_zones[1]["level_pool"]["delineated_cell_count"] > _ot_zones[1]["level_pool"]["retained_cell_count"]
_ot_mask1 = _mask_from_cells((_ot_n, _ot_n), _ot_zones[1]["cells"])
assert connected_components(_ot_mask1, connectivity=8)[1] == 1, (
    "after the trim, only the component containing the anchor is kept -- so the survivor is connected"
)
print(
    f"Test 2 -- non-overlap invariant: a mixed run of {len(_nom_zones)} candidates "
    f"({sorted(_families)}) shares not one cell, and every candidate is a single 8-connected component "
    f"containing its own anchor. On the two-keypoint collision fixture the downstream candidate's "
    f"backwater runs up over the first's claimed ground: it comes back overlap_trimmed=True, cut from "
    f"{_ot_zones[1]['level_pool']['delineated_cell_count']} delineated cells to "
    f"{_ot_zones[1]['level_pool']['retained_cell_count']}, still connected around its anchor and sharing "
    "no cell with candidate 0."
)

# =====================================================================
# Test 4 -- AREA CAP ON FLAT GROUND. A 2.5 m waterline on a nearly level
# plain floods absurdly far, which is exactly what MAX_WATER_ZONE_AREA_
# ACRES exists to bound. 60x60 at 5 m, z = 100 - 0.001 * r (a 0.02%
# grade): every cell on the grid is below the anchor's waterline, so
# nothing but the reach cap and the area cap can stop the delineation.
#
# EXPECTED: truncated_by_cap fires; the final footprint is at or under
# 2.0 acres; the survivors stay 8-connected around the anchor (truncation
# drops the farthest-upstream cells by along-path distance, and a cell's
# distance is strictly greater than its downstream parent's, so a suffix
# removal cannot disconnect anything); and the dam band is never dropped.
# With the cap lowered to 0.05 acres the SAME fixture truncates harder,
# which is what pins the cap as the cause.
# =====================================================================
_cap_n = 60
_cap_array = np.zeros((_cap_n, _cap_n), dtype=np.float64)
for _r in range(_cap_n):
    _cap_array[_r, :] = 100.0 - 0.001 * _r
CAP_DEM = _dem(_cap_array)
CAP_BOUNDARY = box(500000.0, 4500000.0 - _cap_n * 5.0, 500000.0 + _cap_n * 5.0, 4500000.0)
_cap_filled, _cap_ftr, _cap_ftc, _cap_acc = _hydrology(CAP_DEM)
_cap_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500140.0, 4499840.0, 500160.0, 4499860.0),
        "render_fill_polygon_utm": box(500140.0, 4499840.0, 500160.0, 4499860.0),
    }
]


def _cap_run(**kwargs):
    return _run_with_injected(
        CAP_DEM,
        np.ones((_cap_n, _cap_n), dtype=bool),
        _cap_acc,
        _cap_pa,
        CAP_BOUNDARY,
        filled=_cap_filled,
        flow_to_row=_cap_ftr,
        flow_to_col=_cap_ftc,
        max_water_zone_candidates=1,
        **kwargs,
    )[0]


# A wider abutment search than the grid can satisfy is the point of the
# fixture: nothing rises to the waterline anywhere, so the pool is bounded
# only by the caps.
_cap_zones = _cap_run(max_water_zone_area_acres=0.2)
assert len(_cap_zones) == 1
_cap_zone = _cap_zones[0]
assert _cap_zone["abutment_found_left"] is False and _cap_zone["abutment_found_right"] is False, (
    "precondition: on a plain, terrain never rises to the waterline -- both abutment flags must be False"
)
assert _cap_zone["truncated_by_cap"] is True, "TEST 4: the area cap must fire on flat ground"
assert FLAG_TRUNCATED_BY_CAP in _cap_zone["flags"]
_cap_acres = _cap_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _cap_acres <= 0.2 + 1e-9, f"TEST 4: the capped footprint must be at or under the cap, got {_cap_acres:.4f}"
_cap_mask = _mask_from_cells((_cap_n, _cap_n), _cap_zone["cells"])
assert connected_components(_cap_mask, connectivity=8)[1] == 1, (
    "TEST 4: truncating by along-path distance must leave the survivors connected"
)
assert _cap_zone["anchor_rowcol"] in _cap_zone["cells"]

# The cap is genuinely what bounds it: raise the cap and the same fixture
# returns a bigger zone; lower it and a smaller one.
_cap_big = _cap_run(max_water_zone_area_acres=0.5)[0]
_cap_small = _cap_run(max_water_zone_area_acres=0.05)[0]
_big_acres = _cap_big["polygon_utm"].area / SQUARE_METERS_PER_ACRE
_small_acres = _cap_small["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _small_acres < _cap_acres < _big_acres, (_small_acres, _cap_acres, _big_acres)
print(
    f"Test 4 -- area cap on flat ground: a 2.5 m waterline on a 0.02% plain floods without limit, so the "
    f"cap is what bounds the zone -- {_small_acres:.3f} / {_cap_acres:.3f} / {_big_acres:.3f} acres at caps "
    "of 0.05 / 0.2 / 0.5, truncated_by_cap set every time, survivors still 8-connected around the anchor, "
    "and both abutment flags honestly False."
)


# =====================================================================
# Test 5 -- AREA FLOOR. The same flat fixture with the floor raised above
# what the cap allows: the delineation happens, then the footprint is
# rejected with REASON_BELOW_MIN_AREA rather than being padded or kept.
# (Cap 0.05 ac, floor 0.5 ac -- the cap truncates first, then the floor
# rejects the truncated result, which is the exact order the docstring
# describes.)
# =====================================================================
_floor_diag: dict = {}
_floor_zones = _run_with_injected(
    CAP_DEM,
    np.ones((_cap_n, _cap_n), dtype=bool),
    _cap_acc,
    _cap_pa,
    CAP_BOUNDARY,
    filled=_cap_filled,
    flow_to_row=_cap_ftr,
    flow_to_col=_cap_ftc,
    max_water_zone_candidates=1,
    max_water_zone_area_acres=0.05,
    min_water_zone_area_acres=0.5,
    diagnostics=_floor_diag,
)[0]
assert _floor_zones == [], "TEST 5: a sub-floor delineation must be dropped, not padded"
_floor_seeds = _floor_diag["accumulation_seeds"]
assert _floor_seeds, "the family-2 seed log must record the attempt that was rejected"
assert _floor_seeds[0]["outcome"] == REASON_BELOW_MIN_AREA, _floor_seeds[0]
assert _floor_seeds[0]["candidate_id"] is None
print(
    f"Test 5 -- area floor: with the cap at 0.05 ac and the floor at 0.5 ac the delineation is truncated "
    f"and then REJECTED, reporting '{_floor_seeds[0]['outcome']}' in the family-2 seed log rather than a "
    "padded zone."
)


# =====================================================================
# Test 6 -- THE BOUNDARY IS THE ONLY CLIP, and truncated_by_boundary
# fires when it bites.
#
# The V-valley single-channel fixture on a boundary covering only the
# SOUTHERN half of the grid: the anchor sits on the channel just inside
# that boundary, and its backwater runs north straight across the line.
# Cells past it must be gone AND the flag must be set. Then the SAME
# anchor is delineated with an all-canopy mask and a whole-grid road union
# supplied as REPORTED overlap inputs -- and the resulting footprint must
# be IDENTICAL, because canopy and roads gate eligibility, never geometry.
# =====================================================================
_bc_n = 40
_bc_array = np.zeros((_bc_n, _bc_n), dtype=np.float64)
for _r in range(_bc_n):
    for _c in range(_bc_n):
        _bc_array[_r, _c] = 100.0 - 0.1 * _r + 1.0 * abs(_c - 20)
BC_DEM = _dem(_bc_array)
_bc_filled, _bc_ftr, _bc_ftc, _bc_acc = _hydrology(BC_DEM)
# Rows 24-39 only (the southern, downstream half).
BC_BOUNDARY = box(500000.0, 4500000.0 - _bc_n * 5.0, 500000.0 + _bc_n * 5.0, 4500000.0 - 24 * 5.0)
_bc_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500090.0, 4499790.0, 500110.0, 4499810.0),
        "render_fill_polygon_utm": box(500090.0, 4499790.0, 500110.0, 4499810.0),
    }
]
_bc_mask = np.zeros((_bc_n, _bc_n), dtype=bool)
_bc_mask[30, 20] = True  # exactly one eligible cell, so the anchor is pinned


def _bc_run(**kwargs):
    return _run_with_injected(
        BC_DEM, _bc_mask, _bc_acc, _bc_pa, BC_BOUNDARY,
        filled=_bc_filled, flow_to_row=_bc_ftr, flow_to_col=_bc_ftc, **kwargs,
    )[0]


_bc_zones = _bc_run()
assert len(_bc_zones) == 1
_bc_zone = _bc_zones[0]
assert _bc_zone["anchor_rowcol"] == (30, 20)
assert _bc_zone["truncated_by_boundary"] is True, (
    "TEST 6: backwater reaching the property line must be flagged -- it means flooding a neighbour"
)
assert FLAG_TRUNCATED_BY_BOUNDARY in _bc_zone["flags"]
assert min(r for r, _c in _bc_zone["cells"]) >= 24, (
    f"no zone cell may sit north of the boundary, got row {min(r for r, _c in _bc_zone['cells'])}"
)
assert _bc_zone["level_pool"]["delineated_cell_count"] > _bc_zone["level_pool"]["retained_cell_count"], (
    "the flag must reflect a real reduction, not be set unconditionally"
)

# Canopy/road are REPORTED, never clipping.
_bc_all_canopy = np.ones((_bc_n, _bc_n), dtype=bool)
_bc_reported = _bc_run(canopy_root_zone_mask_utm=_bc_all_canopy, road_exclusion_union_utm=BC_BOUNDARY)
assert len(_bc_reported) == 1
assert set(_bc_reported[0]["cells"]) == set(_bc_zone["cells"]), (
    "TEST 6: an all-canopy mask and a whole-parcel road union must NOT reshape the pool -- water does not "
    "stop at a canopy edge"
)
assert _bc_reported[0]["canopy_overlap_pct"] == 100.0
assert _bc_reported[0]["road_overlap_pct"] == 100.0
assert _bc_zone["canopy_overlap_pct"] is None and _bc_zone["road_overlap_pct"] is None, (
    "unchecked must report None, never 0.0 -- 'never looked' and 'looked, found nothing' are different"
)
_bc_clear = _bc_run(canopy_root_zone_mask_utm=np.zeros((_bc_n, _bc_n), dtype=bool))
assert _bc_clear[0]["canopy_overlap_pct"] == 0.0, "checked-and-clear is 0.0, distinct from unchecked's None"
# A REAL None road union is the road fetch's own CLEAN answer ("checked,
# and genuinely no mapped road nearby" -- the common case on a rural
# parcel), so it must report 0.0, not None. Reporting None there would say
# "we don't know" about a parcel we do know is clear -- the same trap
# _ROAD_UNION_NOT_SUPPLIED exists to avoid one layer up.
_bc_road_none = _bc_run(road_exclusion_union_utm=None)
assert _bc_road_none[0]["road_overlap_pct"] == 0.0, (
    "a real None road union means CHECKED-and-clear and must report 0.0, never the unchecked None"
)
print(
    f"Test 6 -- boundary is the only clip: the pool at (30, 20) loses "
    f"{_bc_zone['level_pool']['delineated_cell_count'] - _bc_zone['level_pool']['retained_cell_count']} cells "
    "to the property line and is flagged truncated_by_boundary; an all-canopy mask plus a whole-parcel road "
    "union leave the footprint byte-identical and are REPORTED as 100.0% / 100.0% overlap, while an "
    "unchecked gate reports None, a checked-and-clear one reports 0.0, and a real None road union (the "
    "road fetch's own clean 'no mapped road nearby') reports 0.0 rather than None."
)


# =====================================================================
# Test 3 -- CONTRACT PRESERVATION. The exact field set every downstream
# consumer reads off a zone dict, built by grepping the consumers rather
# than from memory:
#
#   water_suitability.score_water_zones / water_suitability_to_geojson /
#     _fetch_water_holding_data_for_zone
#       -> id, polygon_utm, geometry_wgs84, served_production_area_ids,
#          production_area_relationships,
#          primary_production_area_relationship, representative_elevation_m
#   water_candidate_zones.zones_to_geojson
#       -> + contributing_area_cells, slope_pct, render_fill_geometry_wgs84
#   water_candidate_zones.build_narrative_data  -> the same subset
#   render_layout_map / road_corridors / fencing / tree_zone_candidates /
#     solar_suitability / pipeline_context._attach_keypoint_feature_
#     relationships                              -> render_fill_polygon_utm
#   diagnose_water_zone_mask                     -> cells
#
# This branch may ADD zone fields; it may never remove or rename one a
# consumer reads.
# =====================================================================
_CONSUMER_READ_ZONE_KEYS = {
    "id",
    "served_production_area_ids",
    "polygon_utm",
    "geometry_wgs84",
    "render_fill_polygon_utm",
    "render_fill_geometry_wgs84",
    "cells",
    "production_area_relationships",
    "primary_production_area_relationship",
    "contributing_area_cells",
    "slope_pct",
    "representative_elevation_m",
}
_ADDED_ZONE_KEYS = {
    "nominated_by",
    "keypoint_id",
    "valley_id",
    "keypoint_rowcol",
    "keypoint_point_utm",
    "seed_snapped",
    "seed_snap_distance_m",
    "anchor_rowcol",
    "anchor_point_utm",
    "anchor_elevation_m",
    "level_pool",
    "abutments",
    "abutment_found_left",
    "abutment_found_right",
    "flags",
    "truncated_by_boundary",
    "truncated_by_cap",
    "overlap_trimmed",
    "canopy_overlap_pct",
    "road_overlap_pct",
}
for _z in _nom_zones + _base_zones:
    _missing = _CONSUMER_READ_ZONE_KEYS - set(_z)
    assert not _missing, f"zone {_z['id']} is missing consumer-read field(s): {sorted(_missing)}"
    assert set(_z) == _CONSUMER_READ_ZONE_KEYS | _ADDED_ZONE_KEYS, (
        f"zone dict fields drifted -- diff: {set(_z) ^ (_CONSUMER_READ_ZONE_KEYS | _ADDED_ZONE_KEYS)}"
    )
    _assert_bounded(_z, NOM_BOUNDARY if _z in _nom_zones else BOUNDARY, f"contract-zone-{_z['id']}")
print(
    f"Test 3 -- contract preservation: all {len(_CONSUMER_READ_ZONE_KEYS)} consumer-read fields are present "
    f"on every one of the {len(_nom_zones) + len(_base_zones)} zones built above, alongside exactly the "
    f"{len(_ADDED_ZONE_KEYS)} fields this branch adds -- nothing removed, nothing renamed."
)

# The GeoJSON layer carries the same contract plus the new, purely
# additive provenance/measurement properties.
_geojson = zones_to_geojson(_nom_zones)
validate_feature_collection(_geojson)
_feat = _geojson["features"][0]
assert _feat["properties"]["layer"] == "water_system_candidate"
assert _feat["id"] == "water-system-candidate-0"
assert _feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
for _key in (
    "render_fill_geometry_wgs84", "served_production_area_ids", "production_area_relationships",
    "primary_production_area_relationship", "contributing_area_cells", "slope_pct",
    "nominated_by", "keypoint_id", "valley_id", "anchor_rowcol", "level_pool", "flags",
    "canopy_overlap_pct", "road_overlap_pct",
):
    assert _key in _feat["properties"], _key
# json.dumps() must SUCCEED with no custom encoder -- the real property.
# (Round-trip EQUALITY is deliberately not asserted: GeoJSON coordinate
# tuples come back as lists, which is a JSON fact, not a leak.)
json.dumps(_geojson)
for _f in _geojson["features"]:
    for _k, _v in _f["properties"].items():
        assert not hasattr(_v, "geom_type"), f"shapely geometry leaked onto feature property {_k!r}"
print("Gate -- zones_to_geojson is schema-valid, layer='water_system_candidate', and JSON-clean.")

# =====================================================================
# narrative_data -- the report-facing, FINAL, JSON-serialisable block
# build_narrative_data() produces (and identify_water_system_candidate_
# zones() attaches; that wiring is checked end-to-end in
# test_water_system_candidate_pipeline.py). Everything below checks the
# block's own contract: that it reads the zone dicts without touching
# them, that every value is final (imperial, 1 decimal place) and
# json.dumps()-clean, that an undefined gradient reads as None rather than
# as a measured 0.0, and that the nomination record (reason codes and
# flags) travels through verbatim.
#
# The block describes N candidates now, not one: 'zones' is the full list
# and 'zone' is candidates[0] as the headline. The retired survey-area
# target parameter and its 'target_acres' field are GONE.
# =====================================================================
def _assert_one_decimal(value, path):
    """Every number narrative_data emits is rounded to 1 decimal place --
    the precision emitted is the precision narrated (integer counts/ids
    pass trivially)."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        assert round(float(value), 1) == float(value), f"{path} = {value!r} is not rounded to 1 decimal place"
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_one_decimal(v, f"{path}.{k}")
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _assert_one_decimal(v, f"{path}[{i}]")
        return
    raise AssertionError(f"{path} holds a non-JSON type: {type(value)!r}")


import inspect as _nd_inspect  # noqa: E402

assert "target_acres" not in _nd_inspect.signature(wcz.build_narrative_data).parameters, (
    "the retired survey-area target parameter must be gone from build_narrative_data()"
)

_nd_before = [dict(z) for z in _nom_zones]
_nd = wcz.build_narrative_data(
    _nom_zones, NOM_DEM, NOM_BOUNDARY,
    production_area_count=len(NOM_PA),
    canopy_data_available=False,
    road_data_available=False,
    nomination_diagnostics=_nom_diag,
)
assert [dict(z) for z in _nom_zones] == _nd_before, "build_narrative_data() must READ the zones, not mutate them"

assert json.loads(json.dumps(_nd)) == _nd, (
    "narrative_data must survive a plain json.dumps()/json.loads() round trip unchanged -- no numpy "
    "scalars, no arrays, no geometry"
)
_assert_one_decimal(_nd, "narrative_data")
assert set(_nd) == {
    "zone_found", "candidate_count", "production_area_count", "gates", "nomination", "zones", "zone"
}, set(_nd)
assert _nd["zone_found"] is True
assert _nd["candidate_count"] == len(_nom_zones)
assert len(_nd["zones"]) == len(_nom_zones)
assert _nd["zone"] == _nd["zones"][0], "'zone' is the headline candidate, i.e. candidates[0]"
assert _nd["production_area_count"] == 1
assert _nd["gates"] == {"canopy_data_available": False, "road_data_available": False}

_nd_zone = _nd["zones"][0]
assert set(_nd_zone) == {
    "id", "area_acres", "provenance", "flags", "location", "drainage", "level_pool", "overlap", "service"
}, set(_nd_zone)
assert "target_acres" not in _nd_zone, "the retired survey-area target field must be gone"
assert _nd_zone["provenance"]["nominated_by"] == NOMINATED_BY_KEYPOINT
assert _nd_zone["provenance"]["keypoint_id"] == 1 and _nd_zone["provenance"]["valley_id"] == 0

# The snapped candidate reports its snap in FEET at this boundary.
_nd_snapped = _nd["zones"][2]
assert _nd_snapped["provenance"]["seed_snapped"] is True
assert _nd_snapped["provenance"]["seed_snap_distance_ft"] == round(5.0 / METERS_PER_FOOT, 1)

# Level-pool measurements: imperial, final, and NEVER a volume.
_nd_pool = _nd_zone["level_pool"]
assert _nd_pool["reference_height_ft"] == round(POOL_REFERENCE_HEIGHT_METERS / METERS_PER_FOOT, 1)
assert len(_nd_pool["stations"]) == 3
for _st in _nd_pool["stations"]:
    assert set(_st) == {
        "station_index", "offset_upstream_ft", "flooded_width_ft", "flooded_cross_section_area_sqft"
    }, set(_st)
_nd_keys_flat: list = []


def _collect_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            _nd_keys_flat.append(k)
            _collect_keys(v)
    elif isinstance(node, list):
        for v in node:
            _collect_keys(v)


_collect_keys(_nd)
_capacity_words = ("volume", "capacity", "storage", "acre_feet", "acrefeet", "gallons", "cubic")
_offenders = [k for k in _nd_keys_flat if any(w in k.lower() for w in _capacity_words)]
assert not _offenders, (
    f"narrative_data must never name a storage quantity -- offending key(s): {sorted(set(_offenders))}"
)

# An abutment that was not found reports None, never a 0.0 that would read
# as "the abutment is right at the anchor".
_nd_no_abutment = [z for z in _nd["zones"] if not z["level_pool"]["abutment_found_left"]]
for _z in _nd_no_abutment:
    assert _z["level_pool"]["abutment_distance_left_ft"] is None

# The nomination record travels verbatim: same reason codes, same order.
_nd_nom = _nd["nomination"]
assert _nd_nom["keypoints_considered"] == len(NOM_KEYPOINTS)
assert [o["keypoint_id"] for o in _nd_nom["keypoint_outcomes"]] == [1, 4, 2, 3, 0]
assert [o["outcome"] for o in _nd_nom["keypoint_outcomes"]] == [
    REASON_NOMINATED,
    REASON_NOMINATED,
    REASON_NOMINATED,
    REASON_NO_ELIGIBLE_CELL_WITHIN_SNAP,
    reason_too_close_to_candidate(0),
]
assert _nd_nom["accumulation_seeds"], "the family-2 seed log must reach the narrative block too"

# position_in_parcel directly: a centred footprint reads "center", a
# corner one reads its compass word.
assert wcz._position_in_parcel(
    box(500090.0, 4499890.0, 500110.0, 4499910.0), NOM_BOUNDARY
) == "center"
assert wcz._position_in_parcel(
    box(500180.0, 4499980.0, 500200.0, 4500000.0), NOM_BOUNDARY
) == "northeast"

# The undefined-gradient rule is unchanged: distance 0 reads None, never
# the raw relationship's 0.0 div-by-zero placeholder.
_ug_rel = {
    "production_area_id": 3,
    "elevation_differential_m": 3.0,
    "distance_m": 0.0,
    "gradient_pct": 0.0,
    "above_production_area": True,
}
assert wcz._relationship_narrative(_ug_rel)["gradient_pct"] is None, (
    "gradient at distance 0 is undefined -- narrative_data must emit None, not the 0.0 placeholder"
)
assert wcz._relationship_narrative(_ug_rel)["can_gravity_feed"] is True

# No-candidate outcome: zone_found False, zone None, zones [], and the
# caller's context still reported so a narrative can explain WHY.
_nd_empty = wcz.build_narrative_data(
    [], NOM_DEM, NOM_BOUNDARY, production_area_count=0,
    canopy_data_available=True, road_data_available=True,
)
assert _nd_empty["zone_found"] is False
assert _nd_empty["zone"] is None and _nd_empty["zones"] == []
assert _nd_empty["candidate_count"] == 0
assert _nd_empty["production_area_count"] == 0
assert _nd_empty["nomination"] == {
    "keypoints_considered": 0, "keypoint_outcomes": [], "accumulation_seeds": []
}
assert json.loads(json.dumps(_nd_empty)) == _nd_empty

print(
    f"narrative_data: json-clean and 1-decimal throughout; reads the zone dicts without mutating them; "
    f"describes {_nd['candidate_count']} candidates with provenance (zone 0 from keypoint "
    f"{_nd_zone['provenance']['keypoint_id']}, zone 2 snapped "
    f"{_nd_snapped['provenance']['seed_snap_distance_ft']} ft), the per-keypoint outcome list with its "
    f"reason codes {[o['outcome'] for o in _nd_nom['keypoint_outcomes']]}, level-pool measurements at a "
    f"{_nd_pool['reference_height_ft']} ft reference waterline with NO capacity-named key anywhere, and "
    "an unfound abutment reported as None rather than 0.0; no-candidate case reports zone_found=False "
    "with zone=None and zones=[]."
)

print(
    f"\nWipeout report: {len(_wipeout_messages)} render-opening run(s) fell back to polygon_utm across the "
    "whole file. Every one is a shape thinner than the opening radius throughout: the deliberate "
    "2-cell-wide fixture (test 8), and the level pools delineated on the synthetic 1-2-cell-wide channels "
    "these fixtures use (the single-column DEM, the nomination valleys, the collision fixture). The solid "
    "2D shapes representative of a real, multi-cell-wide drainage band (tests 6/7/9's body and lobes) all "
    "survive the opening, so the radius is not too aggressive for realistic widths -- NOT reduced to pass "
    "tests."
)

# ===========================================================================
# THE ROAD UNION IS PASSED IN, NOT RE-FETCHED (ROAD FETCH #4, CLOSED)
# ===========================================================================
#
# identify_water_system_candidate_zones() used to fetch its own road-exclusion
# union unconditionally -- the fourth computation of the same union in one
# build_pipeline_context() run, after this context's own existing_roads, the
# exclusion-zones gate and production's. It now takes one, so
# build_pipeline_context() hands over the union it already built.
#
# THAT SUBSTITUTION IS ONLY LEGITIMATE IF THE TWO UNIONS ARE THE SAME UNION,
# which comes down to one thing: the BUFFER. A union built at a different
# buffer is a different answer wearing the same name, and would silently move
# the water-zone road gate. The two paths read the same shared constant
# today, which is asserted here against the two SIGNATURE DEFAULTS (captured
# at def time, so this catches a divergence a live-attribute comparison would
# miss) rather than assumed from a comment -- the same check
# test_exclusion_zones.py already makes for the same union on its own side.

import inspect as _r_inspect  # noqa: E402
from unittest.mock import patch as _r_patch  # noqa: E402

import farm_roads_data as _r_farm_roads  # noqa: E402
import production_area as _r_production_area  # noqa: E402

_r_producer_default = _r_inspect.signature(
    _r_farm_roads.get_road_exclusion_union_utm
).parameters["buffer_meters"].default
_r_consumer_default = _r_inspect.signature(
    _r_production_area._fetch_road_exclusion_union_utm
).parameters["buffer_meters"].default
assert _r_producer_default == _r_consumer_default == _r_farm_roads.ROAD_EXCLUSION_BUFFER_METERS, (
    f"the union build_pipeline_context() passes into identify_water_system_candidate_zones() is built by "
    f"get_road_exclusion_union_utm() at its default buffer ({_r_producer_default}m), while this module's "
    f"own self-fetch would use _fetch_road_exclusion_union_utm()'s default ({_r_consumer_default}m) -- "
    f"both must be the single shared farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS "
    f"({_r_farm_roads.ROAD_EXCLUSION_BUFFER_METERS}m); a divergence means the pass-through would silently "
    "substitute a union built at the wrong buffer and move the water-zone road gate"
)
print(
    f"BUFFERS MATCH BY DEFINITION, ASSERTED: the passed union's producer default and this module's "
    f"self-fetch default are both the shared ROAD_EXCLUSION_BUFFER_METERS = {_r_producer_default}m, so a "
    "supplied union is genuinely interchangeable with a self-fetched one rather than coincidentally equal."
)

# --- "not supplied" is a sentinel, and a real None is REUSED --------------
#
# A real None is get_road_exclusion_union_utm()'s own clean answer ("checked,
# and genuinely no mapped road nearby") -- the COMMON case on a rural parcel.
# Treating it as "not supplied" would re-fetch on exactly the parcels the
# pass-through exists to spare, which is the whole trap.

_r_default = _r_inspect.signature(
    wcz.identify_water_system_candidate_zones
).parameters["road_exclusion_union_utm"].default
assert _r_default is wcz._ROAD_UNION_NOT_SUPPLIED and _r_default is not None, (
    "identify_water_system_candidate_zones()'s road_exclusion_union_utm default must be an explicit "
    "sentinel, never None -- None is a real, reusable answer here"
)
assert wcz._ROAD_UNION_NOT_SUPPLIED is not wcz._ROAD_CHECK_UNCHECKED, (
    "'the caller supplied nothing' and 'the check never ran' are different states and must not share a "
    "sentinel -- the first self-fetches, the second skips the gate entirely"
)

_r_rows = _r_cols = 24
_r_dem = _dem(np.zeros((_r_rows, _r_cols), dtype=np.float64))
_r_boundary = box(
    _r_dem["origin_x"] + 2 * RESOLUTION[0],
    _r_dem["origin_y"] - (_r_rows - 2) * RESOLUTION[1],
    _r_dem["origin_x"] + (_r_cols - 2) * RESOLUTION[0],
    _r_dem["origin_y"] - 2 * RESOLUTION[1],
)
_r_union = box(
    _r_dem["origin_x"] + 4 * RESOLUTION[0],
    _r_dem["origin_y"] - 13 * RESOLUTION[1],
    _r_dem["origin_x"] + 20 * RESOLUTION[0],
    _r_dem["origin_y"] - 11 * RESOLUTION[1],
)


def _r_run(**kwargs):
    """One identify_water_system_candidate_zones() run with every network leaf
    stubbed, returning (result, road-fetch count, the union find_candidate_
    zones() was actually handed)."""
    seen = {"fetches": 0, "union": "NOT CALLED"}

    def _count_fetch(*_a, **_k):
        seen["fetches"] += 1
        return _r_union

    def _capture(*_a, **_k):
        seen["union"] = _k.get("road_exclusion_union_utm", "NOT PASSED")
        return []

    with _r_patch.object(wcz, "_fetch_road_exclusion_union_utm", side_effect=_count_fetch), _r_patch.object(
        wcz, "get_required_tree_root_zone_mask_utm",
        return_value=np.zeros((_r_rows, _r_cols), dtype=bool),
    ), _r_patch.object(wcz, "delineate_valleys", return_value=[]), _r_patch.object(
        wcz, "identify_production_areas", return_value=[]
    ), _r_patch.object(wcz, "find_candidate_zones", side_effect=_capture):
        result = wcz.identify_water_system_candidate_zones(
            [(-80.0, 40.0)], dem=_r_dem, boundary_polygon_utm=_r_boundary, **kwargs
        )
    return result, seen["fetches"], seen["union"]

# omitted -> self-fetches, exactly as before this parameter existed
_r_res_self, _r_n_self, _r_u_self = _r_run()
assert _r_n_self == 1, f"with nothing supplied the union must still be self-fetched exactly once, got {_r_n_self}"
assert _r_u_self is _r_union

# a real geometry -> reused, no fetch
_r_res_sup, _r_n_sup, _r_u_sup = _r_run(road_exclusion_union_utm=_r_union)
assert _r_n_sup == 0, f"a supplied union must not be re-fetched, got {_r_n_sup} fetch(es)"
assert _r_u_sup is _r_union, "the supplied union must be the exact object the road gate is run against"
assert _r_res_sup["narrative_data"]["gates"]["road_data_available"] is True

# a real None -> REUSED as "checked, genuinely no roads nearby", not re-fetched
_r_res_none, _r_n_none, _r_u_none = _r_run(road_exclusion_union_utm=None)
assert _r_n_none == 0, (
    f"a caller-supplied real None means 'checked, genuinely no roads nearby' and must be REUSED, not "
    f"treated as 'not supplied' -- got {_r_n_none} fetch(es), which is the redundant fetch this "
    "pass-through exists to remove on exactly the commonest kind of parcel"
)
assert _r_u_none is None
assert _r_res_none["narrative_data"]["gates"]["road_data_available"] is True, (
    "a reused real None means the check genuinely ran and found nothing -- not that it never ran"
)

# a fetch failure on the self-fetch path still degrades gracefully, unchanged
def _r_boom(*_a, **_k):
    raise RuntimeError("simulated road-service outage")


with _r_patch.object(wcz, "_fetch_road_exclusion_union_utm", side_effect=_r_boom), _r_patch.object(
    wcz, "get_required_tree_root_zone_mask_utm", return_value=np.zeros((_r_rows, _r_cols), dtype=bool)
), _r_patch.object(wcz, "delineate_valleys", return_value=[]), _r_patch.object(
    wcz, "identify_production_areas", return_value=[]
), _r_patch.object(wcz, "find_candidate_zones", return_value=[]):
    _r_res_fail = wcz.identify_water_system_candidate_zones(
        [(-80.0, 40.0)], dem=_r_dem, boundary_polygon_utm=_r_boundary
    )
assert _r_res_fail["narrative_data"]["gates"]["road_data_available"] is False, (
    "a road fetch failure on the self-fetch path must still degrade gracefully to road_data_available=False"
)
print(
    "ROAD FETCH #4 CLOSED: a supplied union (a real None included) is reused with ZERO fetches and is the "
    "exact object the gate runs against; nothing supplied still self-fetches exactly once; a fetch failure "
    "on that path still degrades to road_data_available=False."
)

print("\nAll water_candidate_zones checks passed.")
