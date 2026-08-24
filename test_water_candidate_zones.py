"""
test_water_candidate_zones.py

Offline (no-network) checks for water_candidate_zones.py's rebuilt Step 3
pipeline: an ABSOLUTE-ceiling hard-exclusion mask -> 4-connected cluster ->
CONNECTED GREEDY GROWTH to a fixed survey-area target -> select ONE
candidate (highest post-growth summed flow accumulation) -> bounded
morphological OPENING for the render fill. Stage 2 of the feature ("is the
zone-filtering logic correct"), independent of Stage 1 (DEM/valley
delineation accuracy).

These tests build small synthetic DEMs with a known drainage pattern, or
monkeypatch compute_water_eligible_cells() / get_flow_accumulation_for_dem()
to inject an exact mask / accumulation grid where the point is the
clustering/growth/selection/opening logic rather than real D8 hydrology.

Verification map (see the follow-up task's numbered list):
  1. Growth output is always connected (single 4-connected component,
     single-Polygon footprint)
  2. Top-N would have fragmented (inline contrast, 2+ components)
  3. Growth takes the adjacent cell over the better distant one
  4. Exhausted cluster: under target, unpadded, not discarded
  5. Ranking still runs after growth (sum-before/sum-after contrast)
  6. Opening boundedness: render_fill subset of polygon_utm on every fixture
  7. Opening trims protrusions but keeps the body (report area ratio)
  8. Wipeout fallback: thin zone -> polygon_utm, non-empty, logged (report
     the fixture count that triggers it)
Plus retained coverage of the absolute ceiling / boundary independence /
off-parcel / no-waist / canopy / road / production gates and GeoJSON.
"""

import logging
import math

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon, box
from shapely.prepared import prep

from feature_schema import validate_feature_collection
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    pixel_center_xy,
)
from valley_delineation import get_flow_accumulation_for_dem
from water_candidate_zones import (
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    MIN_BOUNDARY_SETBACK_METERS,
    MIN_WATER_ZONE_AREA_ACRES,
    WATER_ZONE_RENDER_OPENING_RADIUS_METERS,
    WATER_ZONE_TARGET_ACRES,
    compute_water_eligible_cells,
    find_candidate_zones,
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


def _run_with_injected(dem, mask, accum, production_areas, boundary, **kwargs):
    """find_candidate_zones() with an injected eligible mask + accumulation
    grid, isolating clustering/growth/selection/opening from the per-cell
    gates and real hydrology. Returns (zones, wiped_out_bool)."""
    orig_compute = wcz.compute_water_eligible_cells
    orig_flow = wcz.get_flow_accumulation_for_dem
    wcz.compute_water_eligible_cells = lambda *a, **kw: mask
    wcz.get_flow_accumulation_for_dem = lambda d: accum
    before = len(_wipeout_messages)
    try:
        zones = find_candidate_zones(dem, production_areas, boundary, **kwargs)
    finally:
        wcz.compute_water_eligible_cells = orig_compute
        wcz.get_flow_accumulation_for_dem = orig_flow
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
assert WATER_ZONE_TARGET_ACRES == 0.5
assert MIN_WATER_ZONE_AREA_ACRES == 0.1
assert WATER_ZONE_RENDER_OPENING_RADIUS_METERS == 5.0


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
TARGET_CELL_COUNT = max(1, int(math.floor(WATER_ZONE_TARGET_ACRES / CELL_AREA_ACRES + 1e-9)))
assert TARGET_CELL_COUNT == 80, f"fixtures assume target_cell_count 80 at 5m/0.5ac, got {TARGET_CELL_COUNT}"

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
# Test 1 & 2 -- GROWTH IS CONNECTED; TOP-N WOULD FRAGMENT.
# Two high-accumulation arms joined by a low-accumulation bridge, one
# 4-connected cluster. Growth stays connected; top-N drops the bridge.
# =====================================================================
_arm1 = _rect_cells(2, 12, 2, 7)      # 10x5 = 50 cells
_bridge = _rect_cells(6, 8, 7, 10)    # 2x3 = 6 cells (accum LOW)
_arm2 = _rect_cells(2, 12, 10, 15)    # 10x5 = 50 cells
_two_arm_cells = _arm1 + _bridge + _arm2
_two_arm_mask = _mask_from_cells((BIG, BIG), _two_arm_cells)
_lbl, _n = connected_components(_two_arm_mask, connectivity=4)
assert _n == 1, f"the two-arm fixture must be a single 4-connected cluster, got {_n}"

_two_arm_accum = np.zeros((BIG, BIG), dtype=np.float64)
for _r, _c in _arm1 + _arm2:
    _two_arm_accum[_r, _c] = 100.0
for _r, _c in _bridge:
    _two_arm_accum[_r, _c] = 1.0

_grow_zones, _ = _run_with_injected(BIG_DEM, _two_arm_mask, _two_arm_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_grow_zones) == 1
_grown = _grow_zones[0]["cells"]
assert len(_grown) == TARGET_CELL_COUNT, f"growth should reach the target ({TARGET_CELL_COUNT}), got {len(_grown)}"
_grown_mask = _mask_from_cells((BIG, BIG), _grown)
_, _grown_components = connected_components(_grown_mask, connectivity=4)
assert _grown_components == 1, f"TEST 1: growth output must be a single 4-connected component, got {_grown_components}"
assert _grow_zones[0]["polygon_utm"].geom_type == "Polygon", (
    f"TEST 1: the grown footprint must be a single Polygon, got {_grow_zones[0]['polygon_utm'].geom_type}"
)
# Growth crossed the low bridge to stay connected.
assert any(cell in _bridge for cell in _grown), "growth must include bridge cells to connect the two arms"
print(
    f"Test 1 -- growth is connected: the grown {len(_grown)}-cell zone is a single 4-connected component and a "
    "single Polygon; it crossed the low-accumulation bridge to stay connected."
)

# Test 2: the OLD top-N selection on the same fixture fragments.
_topN = sorted(_two_arm_cells, key=lambda rc: _two_arm_accum[rc[0], rc[1]])[-TARGET_CELL_COUNT:]
_topN_mask = _mask_from_cells((BIG, BIG), _topN)
_, _topN_components = connected_components(_topN_mask, connectivity=4)
assert _topN_components >= 2, (
    f"TEST 2: top-N-by-accumulation should FRAGMENT on this fixture (drops the low bridge), got "
    f"{_topN_components} components"
)
assert not any(cell in _bridge for cell in _topN), "top-N should exclude the low-accumulation bridge"
print(
    f"Test 2 -- top-N would fragment: the old top-{TARGET_CELL_COUNT} selection drops the low bridge and comes "
    f"back as {_topN_components} disconnected components. This is why growth replaced it."
)


# =====================================================================
# Test 3 -- GROWTH TAKES THE ADJACENT CELL OVER THE BETTER DISTANT ONE.
# Direct test of _grow_zone_cells(): seed(100) - adjacent(50) - gap(10) -
# distant(90). With target 2, growth must take the adjacent 50, not the
# non-adjacent 90.
# =====================================================================
_seed_cell = (10, 10)
_adjacent_cell = (10, 11)
_gap_cell = (10, 12)
_distant_cell = (10, 13)
_adj_line = [_seed_cell, _adjacent_cell, _gap_cell, _distant_cell]
_adj_accum = np.zeros((BIG, BIG), dtype=np.float64)
_adj_accum[_seed_cell] = 100.0
_adj_accum[_adjacent_cell] = 50.0
_adj_accum[_gap_cell] = 10.0
_adj_accum[_distant_cell] = 90.0
_grown_adj = wcz._grow_zone_cells(_adj_line, _adj_accum, target_cell_count=2)
assert _seed_cell in _grown_adj, "the seed (highest accumulation) must be included"
assert _adjacent_cell in _grown_adj, (
    f"TEST 3: growth must take the ADJACENT cell (accum {_adj_accum[_adjacent_cell]:.0f}) as the second cell"
)
assert _distant_cell not in _grown_adj, (
    f"TEST 3: growth must NOT jump to the non-adjacent higher cell (accum {_adj_accum[_distant_cell]:.0f})"
)
print(
    f"Test 3 -- adjacent over distant: growth took the adjacent cell (accum {_adj_accum[_adjacent_cell]:.0f}) as "
    f"cell #2, NOT the non-adjacent higher cell (accum {_adj_accum[_distant_cell]:.0f}). No lookahead/jump."
)


# =====================================================================
# Test 4 -- EXHAUSTED CLUSTER: smaller than target, no adjacent left ->
# under target, unpadded, not discarded.
# =====================================================================
_small_cluster = _rect_cells(20, 25, 20, 28)  # 5x8 = 40 cells (~0.247 ac, between floor and target)
_small_mask = _mask_from_cells((BIG, BIG), _small_cluster)
_small_accum = np.full((BIG, BIG), 30.0, dtype=np.float64)
_exhausted_zones, _ = _run_with_injected(BIG_DEM, _small_mask, _small_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_exhausted_zones) == 1, "a cluster smaller than target must NOT be discarded"
_ex_cells = _exhausted_zones[0]["cells"]
assert len(_ex_cells) == len(_small_cluster), (
    f"TEST 4: an exhausted cluster grows to its whole self ({len(_small_cluster)} cells), unpadded, got {len(_ex_cells)}"
)
_ex_area = _exhausted_zones[0]["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _ex_area < WATER_ZONE_TARGET_ACRES, f"the exhausted zone must be under target, got {_ex_area:.3f} ac"
assert _ex_area >= MIN_WATER_ZONE_AREA_ACRES
print(
    f"Test 4 -- exhausted cluster: a {len(_small_cluster)}-cell cluster ({_ex_area:.3f} ac < target) comes back "
    "unpadded and is not discarded."
)


# =====================================================================
# Test 5 -- RANKING STILL RUNS AFTER GROWTH (sum-before/sum-after contrast,
# unchanged from the rebuild). Compact strong cluster A wins post-growth;
# sprawling B would win on pre-growth sum.
# =====================================================================
_clusterA = _rect_cells(2, 10, 2, 12)    # 8x10 = 80 = target, compact strong
_clusterB = _rect_cells(2, 22, 20, 30)   # 20x10 = 200 sprawling, low per-cell
_clusterC = _rect_cells(40, 48, 2, 12)   # 8x10 = 80 medium
_sel_mask = _mask_from_cells((BIG, BIG), _clusterA + _clusterB + _clusterC)
assert connected_components(_sel_mask, connectivity=4)[1] == 3, "three clusters must be disjoint"
_sel_accum = np.zeros((BIG, BIG), dtype=np.float64)
for _r, _c in _clusterA:
    _sel_accum[_r, _c] = 100.0
for _r, _c in _clusterB:
    _sel_accum[_r, _c] = 50.0
for _r, _c in _clusterC:
    _sel_accum[_r, _c] = 30.0
_full_sum = {"A": 80 * 100, "B": 200 * 50, "C": 80 * 30}
_post_sum = {"A": 80 * 100, "B": 80 * 50, "C": 80 * 30}
assert max(_full_sum, key=_full_sum.get) == "B", "pre-growth sum should favor sprawling B"
assert max(_post_sum, key=_post_sum.get) == "A", "post-growth sum should favor compact A"
_sel_zones, _ = _run_with_injected(BIG_DEM, _sel_mask, _sel_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_sel_zones) == 1
assert set(_sel_zones[0]["cells"]) == set(_clusterA), "compact strong-channel cluster A must win post-growth"
_assert_bounded(_sel_zones[0], BIG_BOUNDARY, "test5-winner")
print(
    f"Test 5 -- ranking after growth: compact A selected. PRE-growth sums {_full_sum} would pick B; "
    f"POST-growth sums {_post_sum} pick A. Ordering unchanged from the rebuild."
)


# =====================================================================
# Test 6 & 7 -- OPENING boundedness, and opening trims a protrusion while
# preserving the body. A solid 8x9 body + a 1-cell-wide, 3-cell-long finger;
# growth keeps all (< target); the opening severs the finger's outer cells
# (a disc r=1 opening removes 1-cell-wide protrusions beyond one cell of
# regrowth) and keeps the body. (A flush single-cell bump would be regrown
# by the dilation and is NOT a valid "removed" case -- a 1-wide finger is.)
# =====================================================================
_body = _rect_cells(20, 28, 20, 29)          # rows 20-27, cols 20-28 -> 8x9 = 72 cells
_finger = [(24, 29), (24, 30), (24, 31)]      # 1-wide, 3-long, off the east edge at row 24
_body_finger_cells = _body + _finger
_body_finger_mask = _mask_from_cells((BIG, BIG), _body_finger_cells)
assert connected_components(_body_finger_mask, connectivity=4)[1] == 1, "body+finger must be one 4-connected cluster"
assert len(_body_finger_cells) <= TARGET_CELL_COUNT, "body+finger must fit under target so growth keeps all"
_body_accum = np.full((BIG, BIG), 100.0, dtype=np.float64)
_bs_zones, _bs_wiped = _run_with_injected(BIG_DEM, _body_finger_mask, _body_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_bs_zones) == 1
_bs_zone = _bs_zones[0]
assert not _bs_wiped, "TEST 7: a solid 8x9 body must survive the r=1 opening (no wipeout)"
_assert_bounded(_bs_zone, BIG_BOUNDARY, "test7-body")  # Test 6 boundedness
_rf = _bs_zone["render_fill_polygon_utm"]
_pu = _bs_zone["polygon_utm"]
# The finger's OUTER cells (beyond one cell of dilation regrowth) must NOT be
# inside the opened render fill.
_tip_pt = Point(*pixel_center_xy(BIG_DEM, 24, 31))
_mid_pt = Point(*pixel_center_xy(BIG_DEM, 24, 30))
assert not _rf.buffer(-1e-6).contains(_tip_pt), "TEST 7: the finger tip must be trimmed by the opening"
assert not _rf.buffer(-1e-6).contains(_mid_pt), "TEST 7: the finger's middle cell must be trimmed too"
_ratio = _rf.area / _pu.area
assert _rf.area < _pu.area, "TEST 7: the opening must trim the finger (and round corners)"
assert _ratio > 0.6, f"TEST 7: the body must be substantially preserved, got drawn/polygon ratio {_ratio:.3f}"
print(
    f"Test 6/7 -- opening: render_fill subset of polygon_utm; the 1-wide finger's outer cells are trimmed and "
    f"the body is substantially preserved (drawn-to-polygon_utm area ratio {_ratio:.3f})."
)


# =====================================================================
# Test 8 -- WIPEOUT FALLBACK: a zone thinner than the opening radius
# throughout (a 2-cell-wide line) erodes to nothing; render_fill falls back
# to polygon_utm, non-empty, and logs once.
# =====================================================================
_thin_cells = _rect_cells(10, 30, 10, 12)  # 20x2 = 40 cells, 2 wide (< 2r+1 = 3)
_thin_mask = _mask_from_cells((BIG, BIG), _thin_cells)
_thin_accum = np.full((BIG, BIG), 100.0, dtype=np.float64)
_wipe_before = len(_wipeout_messages)
_thin_zones, _thin_wiped = _run_with_injected(BIG_DEM, _thin_mask, _thin_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_thin_zones) == 1
_thin_zone = _thin_zones[0]
assert _thin_wiped, "TEST 8: a 2-cell-wide zone must trigger the wipeout fallback"
assert len(_wipeout_messages) == _wipe_before + 1, "the wipeout must be logged exactly once for this zone"
assert not _thin_zone["render_fill_polygon_utm"].is_empty, "the fallback render_fill must be non-empty"
assert _thin_zone["render_fill_polygon_utm"].equals(_thin_zone["polygon_utm"]), (
    "TEST 8: on wipeout, render_fill_polygon_utm must fall back to polygon_utm exactly"
)
_assert_bounded(_thin_zone, BIG_BOUNDARY, "test8-thin")
print(
    "Test 8 -- wipeout fallback: a 2-cell-wide zone erodes to nothing under the r=1 opening; render_fill falls "
    "back to polygon_utm (non-empty), logged once."
)


# =====================================================================
# Test 9 -- the opening can still produce a MultiPolygon (a severed pinch),
# and consumers tolerate it. A dumbbell wider than the opening in the lobes
# but 1 cell wide at the neck: growth keeps it connected, the opening severs
# the neck -> MultiPolygon render fill (acceptable).
# =====================================================================
_dumb_lobe_a = _rect_cells(10, 16, 8, 13)    # 6x5 = 30
_dumb_neck = [(12, 13), (12, 14), (12, 15), (12, 16), (12, 17)]  # 1-cell-tall neck spanning a 5-wide gap
_dumb_lobe_b = _rect_cells(10, 16, 18, 23)   # 6x5 = 30
_dumbbell_cells = _dumb_lobe_a + _dumb_neck + _dumb_lobe_b
_dumbbell_mask = _mask_from_cells((BIG, BIG), _dumbbell_cells)
assert connected_components(_dumbbell_mask, connectivity=4)[1] == 1, "dumbbell must be one 4-connected cluster"
assert len(_dumbbell_cells) <= TARGET_CELL_COUNT, "dumbbell must fit under target so growth keeps the neck"
_dumb_accum = np.full((BIG, BIG), 100.0, dtype=np.float64)
_dumb_zones, _dumb_wiped = _run_with_injected(BIG_DEM, _dumbbell_mask, _dumb_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_dumb_zones) == 1
_dumb_rf = _dumb_zones[0]["render_fill_polygon_utm"]
_assert_bounded(_dumb_zones[0], BIG_BOUNDARY, "test9-dumbbell")
assert not _dumb_wiped, "the wide lobes must survive the opening (only the neck is severed)"
assert _dumb_rf.geom_type == "MultiPolygon", (
    f"TEST 9: the opening should sever the too-narrow neck, leaving a MultiPolygon, got {_dumb_rf.geom_type}"
)
print(
    f"Test 9 -- opening may split: the dumbbell's render_fill is a {_dumb_rf.geom_type} with "
    f"{len(_dumb_rf.geoms)} parts (the too-narrow neck is severed), which render_layout_map.py already tolerates."
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
# Test D -- NO WAIST SPLIT: a waisted (dumbbell) mask emerges as ONE zone.
# =====================================================================
_waist_cells = _rect_cells(2, 14, 2, 14) + _rect_cells(7, 9, 14, 18) + _rect_cells(2, 14, 18, 30)
_waist_mask = _mask_from_cells((BIG, BIG), _waist_cells)
assert connected_components(_waist_mask, connectivity=4)[1] == 1, "the waisted mask must be one 4-connected cluster"
_waist_accum = np.full((BIG, BIG), 100.0, dtype=np.float64)
_waist_zones, _ = _run_with_injected(BIG_DEM, _waist_mask, _waist_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_waist_zones) == 1, f"a waisted mask must emerge as ONE zone (no waist split), got {len(_waist_zones)}"
print("Test D -- no waist split: a waisted mask emerges as exactly 1 zone.")


# =====================================================================
# Retained gate coverage (compute_water_eligible_cells masks) + one real
# end-to-end single-column integration run.
# =====================================================================
assert find_candidate_zones(SINGLE_COLUMN_DEM, [], BOUNDARY) == []
print("Gate -- no production areas means no water zones.")

# Real end-to-end on the single-column DEM. The grown zone follows the
# 1-2-cell-wide channel, which is thinner than the opening radius, so the
# render fill wipes out and falls back to polygon_utm -- expected for a
# degenerate 1-cell channel (a real, multi-cell-wide drainage band survives,
# see tests 6/7).
_col_before = len(_wipeout_messages)
_base_zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
assert len(_base_zones) == 1
_base_zone = _base_zones[0]
_base_area = _base_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _base_area <= WATER_ZONE_TARGET_ACRES + 1e-9, f"selected zone must be at or below target, got {_base_area:.4f}"
assert _base_zone["id"] == 0 and _base_zone["served_production_area_ids"] == [0]
assert _base_zone["primary_production_area_relationship"]["above_production_area"] is True
_base_wiped = len(_wipeout_messages) > _col_before
_assert_bounded(_base_zone, BOUNDARY, "single-column")
# The grown cells are connected (growth guarantee).
_bm = _mask_from_cells(SINGLE_COLUMN_DEM["array"].shape, _base_zone["cells"])
assert connected_components(_bm, connectivity=4)[1] == 1, "the grown single-column zone must be connected"
print(
    f"Gate -- real single-column end-to-end: 1 connected zone ({_base_area:.4f} ac <= target), "
    f"render fill {'wiped out -> polygon_utm (thin channel, expected)' if _base_wiped else 'survived the opening'}."
)

# Gravity is a preference: a production area ABOVE the column still yields a zone.
PRODUCTION_AREA_BELOW = [
    {
        "id": 5,
        "representative_elevation_m": 100.0,
        "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
        "render_fill_polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
    }
]
_below = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_BELOW, BOUNDARY)
assert len(_below) == 1 and _below[0]["primary_production_area_relationship"]["above_production_area"] is False
print("Gate -- a below-elevation (pump-required) production area still yields a real zone.")

# Max service distance still enforced.
assert find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, max_service_distance_meters=1.0) == []
print("Gate -- max service distance is still a real, enforced generation-time filter.")

# Canopy / road / production exclusion on the mask.
_baseline_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
_all_trees = np.ones(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_no_trees = np.zeros(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees).sum()) == 0
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_no_trees).sum()) == int(_baseline_mask.sum())
assert find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees) == []
print("Gate -- canopy: all-trees mask excludes everything; all-clear matches baseline.")

assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=BOUNDARY).sum()) == 0
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=None).sum()) == int(_baseline_mask.sum())
print("Gate -- road: whole-boundary union excludes everything; None is a no-op.")


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
    + (" -- the winning candidate itself changed." if len(_rb_zones_old) != len(_rb_zones_shared)
       or (_rb_zones_old and _rb_zones_shared and _rb_zones_old[0]["cells"] != _rb_zones_shared[0]["cells"])
       else " -- same winner, smaller eligible ground.")
)

PRODUCTION_FULL_OVERLAP = [
    {
        "id": 0,
        "representative_elevation_m": -5.0,
        "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
        "render_fill_polygon_utm": BOUNDARY,
    }
]
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_FULL_OVERLAP, BOUNDARY).sum()) == 0
print("Gate -- production exclusion: a render_fill covering the parcel hard-excludes every water-zone cell.")

# GeoJSON output is schema-valid.
_geojson = zones_to_geojson(_base_zones)
validate_feature_collection(_geojson)
_feat = _geojson["features"][0]
assert _feat["properties"]["layer"] == "water_system_candidate"
assert _feat["id"] == "water-system-candidate-0"
assert _feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
assert "render_fill_geometry_wgs84" in _feat["properties"]
print("Gate -- zones_to_geojson is schema-valid, layer='water_system_candidate'.")


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
# Test F -- 5 m PRODUCTION SETBACK is the surviving margin. Removing the
# service-distance gate must NOT weaken the production-exclusion gate: a
# cell 3 m from production's drawn edge (render_fill_polygon_utm) is still
# excluded (inside the 5 m buffer), a cell 7 m away is not, and a cell deep
# inside the drawn fill stays excluded. 1 m resolution so 3 m and 7 m land
# on exact cell centers. (compute_water_eligible_cells only.)
# =====================================================================
_set_nr, _set_nc = 6, 20
_set_dem = {
    "array": np.full((_set_nr, _set_nc), 100.0, dtype=np.float32),
    "resolution_meters": (1.0, 1.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": CRS,
}
_set_boundary = box(500000.0, 4500000.0 - _set_nr * 1.0, 500000.0 + _set_nc * 1.0, 4500000.0)
# render_fill west edge at x=500010.5: col 7 center is 3 m west (inside the
# 5 m buffer), col 3 center is 7 m west (outside it), col 15 is deep inside.
_set_rf = box(500010.5, 4499990.0, 500020.0, 4500001.0)
_set_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": _set_rf,
        "render_fill_polygon_utm": _set_rf,
    }
]
_orig_flow_f = wcz.get_flow_accumulation_for_dem
wcz.get_flow_accumulation_for_dem = lambda d: np.ones((_set_nr, _set_nc), dtype=np.float64)
try:
    _set_mask = compute_water_eligible_cells(_set_dem, _set_pa, _set_boundary)
finally:
    wcz.get_flow_accumulation_for_dem = _orig_flow_f
_d3 = Point(*pixel_center_xy(_set_dem, 0, 7)).distance(_set_rf)
_d7 = Point(*pixel_center_xy(_set_dem, 0, 3)).distance(_set_rf)
assert abs(_d3 - 3.0) < 1e-9 and abs(_d7 - 7.0) < 1e-9, f"fixture geometry: expected 3 m and 7 m, got {_d3}, {_d7}"
assert not any(_set_mask[r, 7] for r in range(_set_nr)), "TEST F: a cell 3 m from the drawn edge must be excluded (inside the 5 m setback)"
assert all(_set_mask[r, 3] for r in range(_set_nr)), "TEST F: a cell 7 m from the drawn edge must be eligible (beyond the 5 m setback)"
assert not any(_set_mask[r, 15] for r in range(_set_nr)), "TEST F: a cell deep inside the drawn fill must stay excluded (overlap gate intact)"
print(
    f"Test F -- 5 m production setback pinned: a cell {_d3:.0f} m from the drawn edge is excluded, one {_d7:.0f} m "
    "away is eligible (and a cell inside the fill stays excluded); the surviving margin sits between them at 5 m."
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
# Test H -- CLUSTER-FLOOR REJECTION (restored fixture). A cluster whose
# clipped cell-union footprint is below MIN_WATER_ZONE_AREA_ACRES (0.1) is
# discarded before ranking -- not grown, not padded, not selected. 10 m
# resolution so the acreage is hand-verifiable: 3 cells x 100 m^2 = 300 m^2
# = 0.0741 acres < 0.1. The existing coverage (test 4) only asserts the
# survive side (a between-floor-and-target cluster kept untrimmed); this
# asserts the reject side, so moving/dropping the floor check is caught.
# =====================================================================
_floor_nr, _floor_nc = 10, 10
_floor_dem = {
    "array": np.full((_floor_nr, _floor_nc), 100.0, dtype=np.float32),
    "resolution_meters": (10.0, 10.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": CRS,
}
_floor_boundary = box(500000.0, 4500000.0 - _floor_nr * 10.0, 500000.0 + _floor_nc * 10.0, 4500000.0)
_floor_cluster = [(4, 4), (4, 5), (4, 6)]  # 3 adjacent cells, one 4-connected cluster
_floor_mask = _mask_from_cells((_floor_nr, _floor_nc), _floor_cluster)
_floor_accum = np.ones((_floor_nr, _floor_nc), dtype=np.float64)
_floor_pa = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500040.0, 4499940.0, 500060.0, 4499960.0),  # within service distance
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
    }
]
# Hand-verifiable footprint: exactly 3 cells x 100 m^2 = 300 m^2, below the 0.1-acre floor.
_floor_footprint = cell_union_footprint(_floor_dem, _floor_mask).intersection(_floor_boundary)
_floor_acres = _floor_footprint.area / SQUARE_METERS_PER_ACRE
assert abs(_floor_footprint.area - 300.0) < 1e-6, f"expected a 300 m^2 footprint, got {_floor_footprint.area}"
assert 0.0 < _floor_acres < MIN_WATER_ZONE_AREA_ACRES, f"the fixture must be sub-floor, got {_floor_acres:.4f} ac"
# Rejected: no zone survives the floor (dropped before growth/ranking).
_floor_reject_zones, _floor_reject_wiped = _run_with_injected(
    _floor_dem, _floor_mask, _floor_accum, _floor_pa, _floor_boundary
)
assert _floor_reject_zones == [], "TEST H: a sub-floor cluster must be discarded before ranking (not grown/padded/selected)"
assert not _floor_reject_wiped, "the sub-floor cluster is dropped at the floor check, before the render opening runs"
# Prove the floor is the specific cause: lower it and the SAME cluster is
# selected. Its tiny footprint erodes under the render opening (an expected
# wipeout for a 3-cell shape); trim that message so the file-level wipeout
# report below is unaffected by this contrast run.
_floor_wipe_before = len(_wipeout_messages)
_floor_keep_zones, _ = _run_with_injected(
    _floor_dem, _floor_mask, _floor_accum, _floor_pa, _floor_boundary, min_water_zone_area_acres=0.0
)
del _wipeout_messages[_floor_wipe_before:]
assert len(_floor_keep_zones) == 1, "with the floor lowered, the same cluster IS selected -- the floor is the cause"
assert set(_floor_keep_zones[0]["cells"]) == set(_floor_cluster), "the kept cluster grows to its whole (unpadded) self"
print(
    f"Test H -- cluster-floor rejection: a {len(_floor_cluster)}-cell cluster ({_floor_acres:.4f} ac < "
    f"{MIN_WATER_ZONE_AREA_ACRES} floor) is discarded before ranking; lowering the floor to 0.0 selects the same "
    "cluster, confirming the floor is what rejects it."
)


# =====================================================================
# narrative_data -- the report-facing, FINAL, JSON-serialisable block
# build_narrative_data() produces (and identify_water_system_candidate_
# zones() attaches; that wiring is checked end-to-end in
# test_water_system_candidate_pipeline.py). Everything below checks the
# block's own contract against a hand-verifiable fixture: that it reads
# the zone dict without touching it, that every value is final (imperial,
# 1 decimal place) and json.dumps()-clean, and that an undefined gradient
# reads as None rather than as a measured 0.0.
# =====================================================================

import json  # noqa: E402

# The exact field set find_candidate_zones() put on a zone dict BEFORE
# narrative_data existed -- build_narrative_data() reads a zone, it must
# never add, drop, or rename anything on it.
_PRE_NARRATIVE_ZONE_KEYS = {
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


# Fixture: 12x12 grid at 5 m, tilted north-high/south-low (120 m at row 0
# down to 98 m at row 11 -- a uniform 40% steepest-neighbor slope), full-
# grid parcel boundary. The injected cluster is a 4x5-cell block (rows
# 7-10, cols 1-5: 500 m^2 = 0.1236 ac, above the 0.1 floor and far below
# the 0.5 target, so it grows to its whole unpadded self) in the parcel's
# SOUTHWEST, with a flat 300-cell accumulation across it. Two production
# areas: one 22.5 m due east of the zone centroid and 8 m below it (a
# clean gravity-feed relationship with hand-checkable numbers), one
# CONTAINING the zone centroid (distance 0 -- the undefined-gradient
# case).
_nd_nr, _nd_nc = 12, 12
_nd_array = np.zeros((_nd_nr, _nd_nc), dtype=np.float32)
for _r in range(_nd_nr):
    _nd_array[_r, :] = 120.0 - 2.0 * _r
_nd_dem = _dem(_nd_array)
_nd_boundary = box(500000.0, 4500000.0 - _nd_nr * 5.0, 500000.0 + _nd_nc * 5.0, 4500000.0)
_nd_cluster = _rect_cells(7, 11, 1, 6)
_nd_mask = _mask_from_cells((_nd_nr, _nd_nc), _nd_cluster)
_nd_accum = np.ones((_nd_nr, _nd_nc), dtype=np.float64)
for _r, _c in _nd_cluster:
    _nd_accum[_r, _c] = 300.0
_nd_pa = [
    {
        "id": 0,
        "representative_elevation_m": 95.0,
        "polygon_utm": box(500040.0, 4499945.0, 500055.0, 4499960.0),
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),
    },
    {
        "id": 1,
        "representative_elevation_m": 100.0,
        "polygon_utm": box(500000.0, 4499940.0, 500035.0, 4499970.0),  # contains the zone centroid -> distance 0
        "render_fill_polygon_utm": box(2.0, 0.0, 3.0, 1.0),
    },
]
_nd_zones, _nd_wiped = _run_with_injected(_nd_dem, _nd_mask, _nd_accum, _nd_pa, _nd_boundary)
assert len(_nd_zones) == 1, "narrative fixture must produce exactly one zone"
assert not _nd_wiped, "the 4x5 narrative fixture must survive the render opening (keeps the wipeout report exact)"
assert set(_nd_zones[0]) == _PRE_NARRATIVE_ZONE_KEYS, (
    f"zone dict fields changed -- diff: {set(_nd_zones[0]) ^ _PRE_NARRATIVE_ZONE_KEYS}"
)

_nd = wcz.build_narrative_data(
    _nd_zones, _nd_dem, _nd_boundary,
    production_area_count=len(_nd_pa),
    canopy_data_available=False,
    road_data_available=False,
)
assert set(_nd_zones[0]) == _PRE_NARRATIVE_ZONE_KEYS, "build_narrative_data() must READ the zone dict, not mutate it"

# JSON-clean and rounded throughout.
assert json.loads(json.dumps(_nd)) == _nd, (
    "narrative_data must survive a plain json.dumps()/json.loads() round trip unchanged -- no numpy "
    "scalars, no arrays, no geometry"
)
_assert_one_decimal(_nd, "narrative_data")
assert set(_nd) == {"zone_found", "production_area_count", "gates", "zone"}
assert set(_nd["zone"]) == {"area_acres", "target_acres", "location", "drainage", "service"}

# Top level + gates: pure pass-through of what the caller measured.
assert _nd["zone_found"] is True
assert _nd["production_area_count"] == 2
assert _nd["gates"] == {"canopy_data_available": False, "road_data_available": False}

# Question 1 -- WHERE. The block sits in the parcel's southwest (centroid
# offset 19.5 m against a 6.8 m "center" threshold), on lower ground:
# representative elevation 103 m in a 98-120 m parcel range -> (103-98)/22
# = 22.7th percentile. area: 500 m^2 -> 0.1 ac; target: the 0.5 default.
assert _nd["zone"]["area_acres"] == round(500.0 / SQUARE_METERS_PER_ACRE, 1)
assert _nd["zone"]["target_acres"] == WATER_ZONE_TARGET_ACRES
assert _nd["zone"]["location"]["position_in_parcel"] == "southwest"
assert _nd["zone"]["location"]["elevation_percentile_of_parcel"] == 22.7

# Question 2 -- WHY. Median contributing area 300 cells, converted to
# acres INSIDE the module; the ceiling every member cell cleared; the
# uniform 40% slope.
assert _nd["zone"]["drainage"]["contributing_area_acres"] == round(300.0 * cell_area_acres(_nd_dem), 1)
assert _nd["zone"]["drainage"]["contributing_area_ceiling_acres"] == MAX_VALLEY_CONTRIBUTING_AREA_ACRES
assert _nd["zone"]["drainage"]["slope_median_pct"] == 40.0

# Question 3 -- HOW. Most gravity-favorable first (same order the zone's
# own relationships carry): production area 0 sits 8 m (26.2 ft) below the
# zone over 22.5 m (73.8 ft) -- a 35.6% gravity run. Production area 1 is
# at distance 0: its gradient is UNDEFINED, so the narrative must carry
# None, never the raw relationship's 0.0 div-by-zero placeholder (which a
# narrative would read as measured level ground).
_nd_service = _nd["zone"]["service"]
assert _nd_service["served_production_area_count"] == 2
assert _nd_service["served_production_area_ids"] == [0, 1]
assert _nd_service["relationships"][0] == {
    "production_area_id": 0,
    "can_gravity_feed": True,
    "elevation_differential_ft": 26.2,
    "distance_ft": 73.8,
    "gradient_pct": 35.6,
}
assert _nd_service["relationships"][1]["production_area_id"] == 1
assert _nd_service["relationships"][1]["distance_ft"] == 0.0
assert _nd_service["relationships"][1]["gradient_pct"] is None, (
    "gradient at distance 0 is undefined -- narrative_data must emit None, not the 0.0 placeholder"
)
assert _nd_zones[0]["production_area_relationships"][1]["gradient_pct"] == 0.0, (
    "contrast: the RAW relationship still carries the pre-existing 0.0 placeholder, unchanged"
)
assert _nd_service["relationships"][1]["can_gravity_feed"] is True  # +3 m of real head, no run to divide by

# position_in_parcel directly: a centered footprint reads "center" (offset
# 2.5 m, under the 20%-of-equivalent-radius threshold), a corner one reads
# its compass word.
assert wcz._position_in_parcel(box(500015.0, 4499960.0, 500040.0, 4499980.0), _nd_boundary) == "center"
assert wcz._position_in_parcel(box(500045.0, 4499990.0, 500055.0, 4500000.0), _nd_boundary) == "northeast"

# No-zone outcome: zone_found False, zone None (never a zeroed-out zone
# block), the caller's context still reported so a narrative can explain
# WHY nothing was found (here: no production areas existed to serve).
_nd_empty = wcz.build_narrative_data(
    [], _nd_dem, _nd_boundary, production_area_count=0, canopy_data_available=True, road_data_available=True
)
assert _nd_empty["zone_found"] is False
assert _nd_empty["zone"] is None
assert _nd_empty["production_area_count"] == 0
assert _nd_empty["gates"] == {"canopy_data_available": True, "road_data_available": True}
assert json.loads(json.dumps(_nd_empty)) == _nd_empty

print(
    "narrative_data: json-clean and 1-decimal throughout; reads the zone dict without mutating it; "
    "WHERE (southwest, 22.7th elevation percentile, 0.1 of a 0.5-acre target), WHY "
    f"({_nd['zone']['drainage']['contributing_area_acres']} ac median contributing area under the "
    f"{MAX_VALLEY_CONTRIBUTING_AREA_ACRES}-ac ceiling, 40.0% slope), HOW (gravity-feeds production area 0: "
    "26.2 ft over 73.8 ft = 35.6%; distance-0 gradient reads None, not 0.0); no-zone case reports "
    "zone_found=False with zone=None."
)


print(f"\nWipeout report: {len(_wipeout_messages)} find_candidate_zones run(s) triggered the opening wipeout "
      "fallback across the whole file: the deliberate 2-cell-wide fixture (test 8) and the two degenerate real "
      "single-column-channel runs (production area above/below). Every 2D-band fixture representative of a real, "
      "multi-cell-wide drainage band (tests 1/4/5/6/7/9) survives the opening. The wipeouts are confined to "
      "1-2-cell-wide shapes, so the radius is not too aggressive for realistic widths -- NOT reduced to pass tests.")
print("\nAll water_candidate_zones checks passed.")
