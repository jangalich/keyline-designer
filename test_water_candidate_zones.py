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
from inspect import signature as _signature
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
from keypoint_detection import build_upstream_map
from valley_level_pool import POOL_REFERENCE_HEIGHT_METERS, delineate_level_pool
from water_candidate_zones import (
    FLAG_ANCHOR_OFF_PARCEL,
    FLAG_DAM_BAND_CROSSES_MAJOR_DRAINAGE_LEFT,
    FLAG_OVERLAP_TRIMMED,
    FLAG_TRUNCATED_BY_BOUNDARY,
    FLAG_TRUNCATED_BY_CAP,
    MAX_SERVICE_DISTANCE_METERS,
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    MAX_WALL_SEARCH_DOWNSTREAM_METERS,
    MAX_WATER_ZONE_AREA_ACRES,
    MIN_BOUNDARY_SETBACK_METERS,
    MIN_WATER_SEED_SEPARATION_METERS,
    MIN_WATER_ZONE_AREA_ACRES,
    NOMINATED_BY_ACCUMULATION,
    NOMINATED_BY_KEYPOINT,
    REASON_BELOW_MIN_AREA,
    REASON_KEYPOINT_EXCEEDS_CEILING,
    REASON_NOMINATED,
    REASON_WALL_SITE_EXCEEDS_CEILING,
    REASON_WALL_SITE_NOT_FOUND_DOWNSTREAM,
    WATER_ACCUMULATION_SEED_BUDGET,
    WATER_ZONE_CANOPY_BUFFER_METERS,
    compute_water_eligible_cells,
    find_candidate_zones,
    reason_too_close_to_candidate,
    zones_to_geojson,
)
import water_candidate_zones as wcz

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)


# --- THE RENDER OPENING IS GONE, and so is its wipeout fallback. This ---
# --- handler stays, inverted: it now asserts NOTHING ever logs that   ---
# --- warning again, because render_fill_polygon_utm IS polygon_utm.   ---
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
    """find_candidate_zones() with an injected nomination mask, isolating
    nomination/delineation/finishing from the three remaining gates. The
    accumulation grid is injected through the real `flow_accumulation`
    OVERRIDE (not by monkeypatching a module attribute), which is also
    what pins that the override is genuinely forwarded rather than
    recomputed. Returns (zones, False) -- the second element is the
    retired wipeout flag, kept as a constant False so call sites read
    unchanged and any future reintroduction of a display-only reduction
    has to come past this comment."""
    orig_compute = wcz.compute_water_eligible_cells
    wcz.compute_water_eligible_cells = lambda *a, **kw: mask
    kwargs.setdefault("keypoints", [])
    kwargs.setdefault("flow_accumulation", accum)
    try:
        zones = find_candidate_zones(dem, production_areas, boundary, **kwargs)
    finally:
        wcz.compute_water_eligible_cells = orig_compute
    return zones, False


def _assert_bounded(zone, boundary, label):
    """render_fill_polygon_utm IS polygon_utm now -- the identity, not a
    subset and not a copy. The bounded morphological opening that used to
    make them differ is deleted (it erased the one-to-two-cell dam band by
    construction), so this asserts the identity holds and the geometry
    stays inside the parcel."""
    rf = zone["render_fill_polygon_utm"]
    pu = zone["polygon_utm"]
    assert rf is pu, f"{label}: render_fill_polygon_utm must BE polygon_utm, the same object"
    assert zone["render_fill_geometry_wgs84"] is zone["geometry_wgs84"], (
        f"{label}: render_fill_geometry_wgs84 must BE geometry_wgs84, the same object"
    )
    assert boundary.buffer(1e-6).contains(rf), f"{label}: the footprint must stay within the boundary"


assert MIN_BOUNDARY_SETBACK_METERS == 0.0
assert MAX_VALLEY_CONTRIBUTING_AREA_ACRES == 20.0
assert MIN_WATER_ZONE_AREA_ACRES == 0.1
assert MAX_WATER_ZONE_AREA_ACRES == 2.0
assert WATER_ACCUMULATION_SEED_BUDGET == 3
assert MIN_WATER_SEED_SEPARATION_METERS == 30.0
assert POOL_REFERENCE_HEIGHT_METERS == 2.5
assert MAX_SERVICE_DISTANCE_METERS == 800.0
assert MAX_WALL_SEARCH_DOWNSTREAM_METERS == 150.0
assert WATER_ZONE_CANOPY_BUFFER_METERS == 3.048

# --- RETIRED NAMES: asserted absent, grep-style, so a reintroduction ---
# --- fails at import rather than quietly resurrecting a design.      ---
for _retired in (
    "WATER_ZONE_TARGET_ACRES",                   # the fixed survey-area target
    "WATER_ZONE_PRODUCTION_SETBACK_METERS",      # the production-overlap gate's buffer
    "_grow_zone_cells",                          # greedy growth
    "WATER_ZONE_RENDER_OPENING_RADIUS_METERS",   # the render opening's radius
    "_render_opening",                           # the render opening itself
    "WATER_KEYPOINT_SEED_SNAP_METERS",           # the seed snap radius
    "_nearest_eligible_cell",                    # the snap search
    "FLAG_SEED_SNAPPED",                         # the snap's flag
    "MAX_WATER_ZONE_CANDIDATES",                 # the generation cap
    "REASON_CANDIDATE_CAP_REACHED",              # the cap's reason code
    "REASON_NO_ELIGIBLE_CELL_WITHIN_SNAP",       # the snap's failure code
):
    assert not hasattr(wcz, _retired), f"{_retired} is DELETED and must not reappear"

# The three gates that LEFT the nomination mask must be gone from
# compute_water_eligible_cells()'s signature entirely -- not defaulted to
# something inert, which would leave a caller able to re-enable them.
_mask_params = set(_signature(compute_water_eligible_cells).parameters)
assert _mask_params == {
    "dem", "boundary_polygon_utm", "max_valley_contributing_area_acres",
    "min_boundary_setback_meters", "flow_accumulation",
}, f"the nomination mask's signature drifted: {sorted(_mask_params)}"
for _gone in ("canopy_root_zone_mask_utm", "road_exclusion_union_utm", "max_service_distance_meters",
              "production_areas"):
    assert _gone not in _mask_params, (
        f"{_gone} must be GONE from compute_water_eligible_cells() -- canopy/road/service-distance are "
        "measurements now, and no remaining gate reads a production area"
    )
# ...but canopy and road are still ACCEPTED by find_candidate_zones(), as
# measurement inputs. They moved; they did not disappear.
_find_params = set(_signature(find_candidate_zones).parameters)
for _kept in ("canopy_root_zone_mask_utm", "road_exclusion_union_utm", "max_service_distance_meters"):
    assert _kept in _find_params, f"{_kept} must still reach find_candidate_zones()"
assert "keypoint_seed_snap_meters" not in _find_params and "max_water_zone_candidates" not in _find_params


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
    m = compute_water_eligible_cells(dem, boundary)
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
    _ceiling_mask = compute_water_eligible_cells(_ceiling_dem, _ceiling_boundary)
finally:
    wcz.get_flow_accumulation_for_dem = _orig_flow
assert bool(_ceiling_mask[0, 0]) and bool(_ceiling_mask[0, 5]), "cells far below the ceiling must be eligible (no lower bound)"
assert bool(_ceiling_mask[0, 1]) and bool(_ceiling_mask[0, 2]), "just-below and exactly-at the ceiling must be eligible"
assert not bool(_ceiling_mask[0, 3]) and not bool(_ceiling_mask[0, 4]), "above the ceiling must be excluded"
print("Test B -- absolute ceiling: cells at/below 20 acres eligible (incl. accum=1, no lower bound), above excluded.")

# =====================================================================
# THE RENDER OPENING IS DELETED, AND render_fill IS THE IDENTITY.
#
# This REPLACES the four tests (6/7/8/9) that pinned the bounded
# morphological opening's behaviour -- boundedness, protrusion trimming,
# the wipeout fallback, and the severed-pinch MultiPolygon. All four
# tested code that no longer exists, and the behaviour they described is
# exactly what made the opening wrong for a level pool: the dam band is a
# one-to-two-cell strip by construction, so "removes anything narrower
# than 2r" removes the subject.
#
# What replaces them is the identity contract every downstream consumer
# now relies on, asserted structurally (same OBJECT, not merely equal
# geometry) so a future copy-or-reduce cannot slip back in unnoticed.
# _assert_bounded() carries it on every zone built anywhere in this file;
# these assertions pin the module-level facts.
# =====================================================================
assert not hasattr(wcz, "_render_opening"), "the render opening is DELETED"
assert not hasattr(wcz, "WATER_ZONE_RENDER_OPENING_RADIUS_METERS"), "its radius constant is DELETED"
# The opening's raster machinery is no longer imported by this module at
# all -- proof the deletion went past the call site.
for _unused in ("binary_dilate", "eroded_cell_mask", "waist_erosion_radius_cells"):
    assert not hasattr(wcz, _unused), (
        f"water_candidate_zones no longer needs raster_grid.{_unused} -- the opening was its only user"
    )
print(
    "Render opening DELETED: no _render_opening, no radius constant, and the raster erode/dilate helpers "
    "it was the only user of are no longer imported. render_fill_polygon_utm is now the identity, "
    "asserted per-zone by _assert_bounded() throughout this file."
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
_op_mask = compute_water_eligible_cells(_OP_DEM, _OP_BOUNDARY)
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
# Retained gate coverage + one real end-to-end single-column run.
#
# THE MASK IS THREE GATES NOW: ceiling, on-parcel, inert setback. The
# canopy / road / service-distance gate tests that used to live here are
# converted below into MEASUREMENT tests -- the layers did not vanish,
# their role changed, and the tests follow the role.
# =====================================================================
assert find_candidate_zones(SINGLE_COLUMN_DEM, [], BOUNDARY) == []
print("Gate -- no production areas means no water zones.")

# THE MASK IS PRODUCTION-INDEPENDENT. This REPLACES the old Test E
# (min-service-distance removal), Test F (production-overlap gate) and
# Test G (max-service-distance gate) at the mask level: none of those
# concepts is a gate any more, and production_areas is not even a
# parameter. Three production configurations that used to produce three
# different masks -- adjacent, whole-parcel overlap, and 872 m away -- now
# produce the SAME mask, because the mask never looks at them.
_baseline_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, BOUNDARY)
assert int(_baseline_mask.sum()) > 0
print(
    f"Gate -- the nomination mask is production-independent: {int(_baseline_mask.sum())} eligible cells "
    "with no production input at all (the parameter is gone from the signature, asserted at import)."
)

# Real end-to-end on the single-column DEM, with NOTHING injected: real
# hydrology, real keypoint detection, real nomination, real delineation.
_base_diag: dict = {}
_base_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, diagnostics=_base_diag
)
assert _base_zones, "the single-column fixture must produce at least one candidate"
_base_zone = _base_zones[0]
_base_area = _base_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _base_zone["id"] == 0 and _base_zone["served_production_area_ids"] == [0]
assert _base_zone["primary_production_area_relationship"]["above_production_area"] is True
assert _base_zone["anchor_rowcol"] in _base_zone["cells"], "an on-parcel anchor is a member of its own zone"
assert _base_area <= MAX_WATER_ZONE_AREA_ACRES + 1e-9
_assert_bounded(_base_zone, BOUNDARY, "single-column")
print(
    f"Gate -- real single-column end-to-end (no injection): {len(_base_zones)} candidate(s), zone 0 at "
    f"{_base_area:.4f} ac anchored on {_base_zone['anchor_rowcol']} by {_base_zone['nominated_by']}."
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


# =====================================================================
# Test 1 -- CANOPY AND ROADS ARE MEASUREMENTS, NOT GATES.
#
# Converted from the old mask-gate tests. The three things that must hold:
#   a. An all-canopy mask / whole-parcel road union no longer removes a
#      single eligible cell -- they are not gates.
#   b. A candidate whose pool overlaps them reports the correct NONZERO
#      percentage.
#   c. The sentinel distinction survives for BOTH layers: "never checked"
#      is None, "checked and clear" is 0.0. (The road half of this is the
#      bug fixed on the previous branch and must not regress.)
# =====================================================================
_all_trees = np.ones(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_no_trees = np.zeros(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)

# (a) neither layer can be handed to the mask at all any more -- asserted
# at import -- and a run with them supplied returns the SAME candidates as
# a run without, cell for cell.
_m_plain = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
_m_measured = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY,
    canopy_root_zone_mask_utm=_all_trees, road_exclusion_union_utm=BOUNDARY,
)
assert len(_m_plain) == len(_m_measured) and _m_plain, "an all-canopy parcel must still produce candidates"
for _a, _b in zip(_m_plain, _m_measured):
    assert _a["cells"] == _b["cells"], (
        "TEST 1a: a total-canopy mask and a whole-parcel road union must not move a single cell -- "
        "they are measurements now"
    )
# (b) ...and they are REPORTED, at 100% each on this fixture.
assert all(z["canopy_overlap_pct"] == 100.0 for z in _m_measured)
assert all(z["road_overlap_pct"] == 100.0 for z in _m_measured)
# (c) sentinels, both layers.
assert all(z["canopy_overlap_pct"] is None for z in _m_plain), "unchecked canopy reports None"
assert all(z["road_overlap_pct"] is None for z in _m_plain), "unchecked road reports None"
_m_clear = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY,
    canopy_root_zone_mask_utm=_no_trees, road_exclusion_union_utm=None,
)
assert all(z["canopy_overlap_pct"] == 0.0 for z in _m_clear), "checked-and-clear canopy reports 0.0"
assert all(z["road_overlap_pct"] == 0.0 for z in _m_clear), (
    "a real None road union is the road fetch's own CLEAN 'no mapped road nearby' answer -- checked, so "
    "0.0, never the unchecked None (the previous branch's bug fix, pinned here)"
)
print(
    f"Test 1 -- canopy/road are measurements: an all-canopy mask plus a whole-parcel road union leave all "
    f"{len(_m_measured)} candidates cell-for-cell identical and report 100.0% / 100.0% overlap; unchecked "
    "reports None for both; checked-and-clear reports 0.0 for both, including a real None road union."
)


# =====================================================================
# Test 2 -- NOMINATION ON FORMERLY-GATED GROUND.
#
# The headline behavioural change of this branch, asserted directly: an
# anchor standing on ground that the OLD canopy gate would have removed
# from the mask now nominates, and the resulting candidate carries the
# canopy overlap as evidence rather than having been filtered away.
#
# The contrast is computed inline against the deleted gate's own logic
# (mask AND NOT canopy), so the change is demonstrated, not just asserted.
# =====================================================================
_c_all_canopy = np.ones(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_c_old_gate_mask = _baseline_mask & ~_c_all_canopy
assert int(_c_old_gate_mask.sum()) == 0, (
    "precondition: under the DELETED canopy gate this parcel had zero eligible cells, so every candidate "
    "below is one the old design could not have produced at all"
)
_c_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_c_all_canopy
)
assert _c_zones, (
    "TEST 2: a fully-canopied parcel must still nominate -- a keypoint under canopy is a real dam site "
    "behind a clearing cost, not an ineligible one"
)
assert all(z["canopy_overlap_pct"] == 100.0 for z in _c_zones)
print(
    f"Test 2 -- nomination on formerly-gated ground: under the deleted canopy gate this parcel had 0 "
    f"eligible cells and could produce nothing; it now produces {len(_c_zones)} candidate(s), each "
    "carrying canopy_overlap_pct=100.0 as reported evidence."
)


# =====================================================================
# The SERVICE-DISTANCE rule is no longer a gate ANYWHERE. It left the
# per-cell mask on the gates-narrow branch, and the post-delineation drop
# that replaced it -- explicitly marked a TEMPORARY GUARD -- is gone too:
# the scoring branch decided a candidate with no production area in range
# is scored on its landform, not discarded. What survives is an
# informational FLAG plus a None headline relationship, and consumers must
# handle that None.
# =====================================================================
_sd_far = [
    {
        "id": 9,
        "representative_elevation_m": -5.0,
        "polygon_utm": box(501200.0, 4499850.0, 501230.0, 4499900.0),  # ~1000 m east of the grid
        "render_fill_polygon_utm": box(501200.0, 4499850.0, 501230.0, 4499900.0),
    }
]
# The mask is unaffected (it never looked at production)...
assert int(compute_water_eligible_cells(SINGLE_COLUMN_DEM, BOUNDARY).sum()) == int(_baseline_mask.sum())
# ...and every delineated candidate now SURVIVES, carrying the flag.
_sd_diag: dict = {}
_sd_zones = find_candidate_zones(SINGLE_COLUMN_DEM, _sd_far, BOUNDARY, diagnostics=_sd_diag)
assert _sd_zones, "a candidate with no production area in range must no longer be dropped"
for _z in _sd_zones:
    assert wcz.FLAG_NO_SERVICE_RELATIONSHIP in _z["flags"], _z["flags"]
    assert _z["has_service_relationship"] is False
    assert _z["production_area_relationships"] == []
    assert _z["primary_production_area_relationship"] is None, (
        "the headline relationship is an honest None -- never a fabricated relationship to a "
        "production area that is not in range"
    )
    assert _z["served_production_area_ids"] == []
_sd_outcomes = {o["outcome"] for o in _sd_diag["keypoint_outcomes"]} | {
    s["outcome"] for s in _sd_diag["accumulation_seeds"]
}
assert REASON_NOMINATED in _sd_outcomes
# The reason CODE is gone with the drop -- a candidate can no longer die
# of this, so no outcome may name it.
assert not hasattr(wcz, "REASON_NO_SERVICE_RELATIONSHIP"), (
    "the no-service DROP is deleted, so its reason code must be too -- it is a flag now"
)
assert not any("no_service_relationship" == o for o in _sd_outcomes), _sd_outcomes
print(
    "Service distance -- no longer a gate anywhere: a production area ~1000 m away leaves the mask "
    f"untouched at {int(_baseline_mask.sum())} cells AND leaves {len(_sd_zones)} delineated "
    f"candidate(s) alive, each carrying the informational flag '{wcz.FLAG_NO_SERVICE_RELATIONSHIP}' "
    "with an empty relationship list and a None headline relationship. The temporary guard the "
    "gates-narrow branch left behind is gone, and its reason code with it."
)


# =====================================================================
# The ROAD BUFFER still matters -- to the MEASUREMENT now, not to a gate.
#
# This section previously demonstrated that water zones clear existing
# roads at the SHARED farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS rather
# than a deleted per-module 3.048 m value. Roads no longer exclude
# anything, so what the buffer now decides is how much of a candidate is
# REPORTED as road-overlapping. The two unions are still built by the real
# producer from the SAME road line, differing only in buffer.
#
# The signature-default assertion below is unchanged and still load-
# bearing: build_pipeline_context() hands this module a pre-built union,
# and that substitution is only legitimate if both paths build at the same
# buffer.
# =====================================================================

from rasterio.warp import transform as _rb_warp_transform  # noqa: E402

import farm_roads_data  # noqa: E402

assert farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS == 5.0, (
    "this section contrasts the shared 5.0m buffer against the deleted per-module 3.048m one -- update "
    f"it if the shared constant is retuned (currently {farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS})"
)
_OLD_WATER_ROAD_BUFFER_M = 3.048  # the deleted per-module value, kept here only as the contrast

# Vertical road line 3.75m east of the channel column's cell centers:
# outside the old 3.048m buffer, inside the shared 5.0m one.
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

_rb_zones_shared = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=_rb_union_shared
)
_rb_zones_old = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=_rb_union_old
)
# Same GEOMETRY either way -- the buffer cannot move a cell any more.
assert [z["cells"] for z in _rb_zones_shared] == [z["cells"] for z in _rb_zones_old], (
    "a road union must not reshape a candidate at ANY buffer -- roads are a measurement now"
)
_rb_pct_shared = [z["road_overlap_pct"] for z in _rb_zones_shared]
_rb_pct_old = [z["road_overlap_pct"] for z in _rb_zones_old]
assert any(s > o for s, o in zip(_rb_pct_shared, _rb_pct_old)), (
    f"the wider shared buffer must report MORE road overlap on a road 3.75m from the channel -- got "
    f"{_rb_pct_shared} (5.0m) vs {_rb_pct_old} (3.048m)"
)
print(
    f"Road buffer -- now a MEASUREMENT parameter: the same candidates (cell-for-cell identical geometry) "
    f"report road_overlap_pct {_rb_pct_old} at the deleted 3.048m per-module buffer and {_rb_pct_shared} "
    f"at the shared {farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS}m one. The buffer changes what is "
    "REPORTED, and nothing else."
)


# =====================================================================
# NOMINATION FIXTURE -- four parallel V-valleys on one 60x56 grid at 5 m.
#
#     z(r, c) = 100.0 - 0.1 * r + 1.0 * min(|c-6|, |c-20|, |c-34|, |c-48|)
#
# i.e. channels down columns 6, 20, 34 and 48, each with 20% side slopes,
# all running north -> south at a 2% grade, separated by ridges that stand
# 7 m above the channel floors -- far higher than the 2.5 m reference
# waterline, so a pool on one channel can never reach another.
#
# THE GRID IS 60 ROWS DEEP because every keypoint now anchors at its WALL
# SITE, a full 2.5 m below it: at 0.1 m per 5 m cell that is a 25-cell,
# 125 m walk downstream, and the channel has to be long enough to contain
# it. A keypoint at row 20 therefore walls at row 45.
#
# The FOURTH channel exists solely so the off-parcel keypoint below has a
# drainage to itself: on a shared channel it would be rejected by the 30 m
# seed-separation rule, which would test the separation rule a second time
# instead of testing off-parcel nomination.
#
# THE BOUNDARY EXCLUDES THE TOP FOUR ROWS (it starts at row 4), which is
# now the useful side: the wall walk runs DOWNSTREAM, so an off-parcel
# keypoint above the line has its wall -- and therefore its candidate's
# ground -- walk INTO the parcel. That is the reference property's
# 6.29-acre case, and it is why the walk helps there rather than hurting.
#
# Five synthetic keypoints, built so that ID ORDER IS NOT CATCHMENT ORDER
# (keypoint_detection.py assigns ids by slope_drop_pct descending, which
# is right for ITS layer and is not what this module nominates by):
#
#   id  rowcol     catchment   what it is here
#   --  --------   ---------   ---------------------------------------
#    0  (24,  6)      3.0 ac   20 m DOWNSTREAM of keypoint 1, on the same
#                              channel -- the "too close" half of the pair
#    1  (20,  6)      8.0 ac   the pair's winner on catchment
#    2  (20, 34)      6.0 ac   plainly on-parcel
#    3  ( 2, 48)      4.0 ac   OFF-PARCEL (row 2, 7.5 m above the boundary's
#                              own top edge) on the FOURTH channel, which no
#                              other keypoint uses -- its WALL walks INTO
#                              the parcel
#    4  (20, 20)      7.0 ac   plainly on-parcel
#
# EXPECTED OUTCOMES, in catchment order (8.0, 7.0, 6.0, 4.0, 3.0). Every
# wall sits 25 cells / 125.0 m downstream of its keypoint with exactly
# 2.5 m of drop:
#   keypoint 1 -> nominated, candidate 0, WALL at (45,  6)
#   keypoint 4 -> nominated, candidate 1, WALL at (45, 20)
#   keypoint 2 -> nominated, candidate 2, WALL at (45, 34)
#   keypoint 3 -> nominated, candidate 3, WALL at (27, 48) -- an OFF-parcel
#                 keypoint whose wall is comfortably ON the parcel, so the
#                 walk moved its ground onto the parcel rather than off it.
#                 Only the pool's tail (row 3) is clipped away.
#   keypoint 0 -> too_close_to_candidate_0: its own wall at (49, 6) is 20 m
#                 from candidate 0's footprint, inside the 30 m separation.
#                 SEPARATION BINDS AT THE WALLS, which is where the
#                 structures would stand.
#
# IF ORDERING WERE BY ID (or by slope drop) instead of by catchment,
# keypoint 0 would be delineated FIRST and keypoint 1 would be the one
# rejected -- so asserting which of the pair survives IS the ordering
# assertion.
#
# NOTHING IS CAPPED: all five keypoints are attempted, and family 2 then
# adds up to WATER_ACCUMULATION_SEED_BUDGET survivors of its own.
# =====================================================================
_nom_n = 60
_nom_cols = 56
_nom_array = np.zeros((_nom_n, _nom_cols), dtype=np.float64)
for _r in range(_nom_n):
    for _c in range(_nom_cols):
        _nom_array[_r, _c] = 100.0 - 0.1 * _r + 1.0 * min(
            abs(_c - 6), abs(_c - 20), abs(_c - 34), abs(_c - 48)
        )
NOM_DEM = _dem(_nom_array)
# Boundary starts at row 4 (y = 4500000 - 20), leaving rows 0-3 -- the
# upstream head -- off-parcel.
NOM_BOUNDARY = box(500000.0, 4500000.0 - _nom_n * 5.0, 500000.0 + _nom_cols * 5.0, 4500000.0 - 20.0)
NOM_PA = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500090.0, 4499890.0, 500110.0, 4499910.0),
        "render_fill_polygon_utm": box(500090.0, 4499890.0, 500110.0, 4499910.0),
    }
]
_nom_filled, _nom_ftr, _nom_ftc, _nom_acc = _hydrology(NOM_DEM)
_nom_mask = compute_water_eligible_cells(NOM_DEM, NOM_BOUNDARY, flow_accumulation=_nom_acc)


def _kp(kp_id, valley_id, rowcol, contributing_acres):
    """A keypoint dict in detect_keypoints()'s own shape, including the
    on_parcel / distance_outside_boundary_m pair that module measures --
    find_candidate_zones() reads that measurement rather than re-deriving
    it, so the fixture must carry it."""
    x, y = pixel_center_xy(NOM_DEM, *rowcol)
    point = Point(x, y)
    on_parcel = NOM_BOUNDARY.contains(point) or NOM_BOUNDARY.touches(point)
    return {
        "id": kp_id,
        "valley_id": valley_id,
        "rowcol": rowcol,
        "point_utm": point,
        "contributing_acres": contributing_acres,
        "on_parcel": on_parcel,
        "distance_outside_boundary_m": 0.0 if on_parcel else round(point.distance(NOM_BOUNDARY), 2),
    }


NOM_KEYPOINTS = [
    _kp(0, 0, (24, 6), 3.0),
    _kp(1, 0, (20, 6), 8.0),
    _kp(2, 2, (20, 34), 6.0),
    _kp(3, 3, (2, 48), 4.0),
    _kp(4, 1, (20, 20), 7.0),
]
# Preconditions the expectations above depend on.
assert NOM_KEYPOINTS[3]["on_parcel"] is False, "keypoint 3 must genuinely sit off-parcel"
# Row 2's center sits 12.5 m below the grid top; the boundary's own top
# edge is 20 m below it. So the KEYPOINT is 7.5 m outside the drawn line --
# comfortably inside keypoint_detection's own 25 m
# KEYPOINT_BOUNDARY_MARGIN_METERS, which is what bounds how far off a
# keypoint can ever be.
assert abs(NOM_KEYPOINTS[3]["distance_outside_boundary_m"] - 7.5) < 1e-6, (
    NOM_KEYPOINTS[3]["distance_outside_boundary_m"]
)
assert _nom_mask[2, 48] == False, (  # noqa: E712
    "precondition: the off-parcel keypoint's own cell is NOT in the nomination mask -- which is exactly "
    "the point: keypoint anchors are EXEMPT from the on-parcel gate"
)
assert _nom_mask[20, 20] and _nom_mask[20, 6] and _nom_mask[20, 34]

_nom_diag: dict = {}
_nom_zones = find_candidate_zones(
    NOM_DEM,
    NOM_PA,
    NOM_BOUNDARY,
    keypoints=NOM_KEYPOINTS,
    filled=_nom_filled,
    flow_to_row=_nom_ftr,
    flow_to_col=_nom_ftc,
    flow_accumulation=_nom_acc,
    diagnostics=_nom_diag,
)

_outcomes = {o["keypoint_id"]: o for o in _nom_diag["keypoint_outcomes"]}
_order = [o["keypoint_id"] for o in _nom_diag["keypoint_outcomes"]]
assert _order == [1, 4, 2, 3, 0], f"keypoints must be processed by CATCHMENT descending, got {_order}"
assert len(_nom_diag["keypoint_outcomes"]) == len(NOM_KEYPOINTS), (
    "EVERY keypoint must be attempted -- generation is uncapped"
)

assert _outcomes[1]["outcome"] == REASON_NOMINATED and _outcomes[1]["candidate_id"] == 0
assert _outcomes[1]["anchor_rowcol"] == (45, 6), (
    "the anchor is the WALL SITE a full reference height BELOW the keypoint, not the keypoint's cell"
)
assert _outcomes[1]["keypoint_rowcol"] == (20, 6), "the keypoint is retained as the pool's TAIL"
assert _outcomes[4]["outcome"] == REASON_NOMINATED and _outcomes[4]["candidate_id"] == 1
assert _outcomes[4]["anchor_rowcol"] == (45, 20)
assert _outcomes[2]["outcome"] == REASON_NOMINATED and _outcomes[2]["candidate_id"] == 2
assert _outcomes[2]["anchor_rowcol"] == (45, 34)

# Every one of those walks ran the full 25 cells and found the full drop --
# there is no partial-height fallback to hide behind.
for _kid in (1, 4, 2):
    assert _outcomes[_kid]["wall_offset_downstream_m"] == 125.0, _outcomes[_kid]
    assert _outcomes[_kid]["wall_drop_m"] == POOL_REFERENCE_HEIGHT_METERS, _outcomes[_kid]
    assert _outcomes[_kid]["wall_walk_end_reason"] == "reached_full_drop", _outcomes[_kid]

assert _outcomes[0]["outcome"] == reason_too_close_to_candidate(0), _outcomes[0]
assert _outcomes[0]["candidate_id"] is None
assert _outcomes[0]["anchor_rowcol"] == (49, 6), (
    "keypoint 0's OWN wall site was found at (49, 6) -- 20 m from candidate 0's wall at (45, 6). "
    "The SEPARATION rule stopped it, and it binds at the WALLS, where the structures would stand"
)
assert _outcomes[0]["wall_walk_end_reason"] == "reached_full_drop", (
    "the rejection is a separation rejection, NOT a failed walk -- the two must never be confused"
)

# NO CAP ANYWHERE, and no snap anywhere.
_all_outcomes = [o["outcome"] for o in _nom_diag["keypoint_outcomes"]]
assert not any("candidate_cap_reached" in str(o) for o in _all_outcomes), (
    "generation is UNCAPPED -- no keypoint may be turned away for arriving late"
)
assert not any("snap" in str(o) for o in _all_outcomes), "no snap outcome exists any more"

# Every keypoint-nominated zone anchors on the WALL SITE below its
# keypoint, and carries BOTH positions -- the structural statement that the
# keypoint is the pool's tail and the anchor is where the wall stands.
for _z in _nom_zones:
    if _z["nominated_by"] == NOMINATED_BY_KEYPOINT:
        _kr, _kc = _z["keypoint_rowcol"]
        _ar, _ac = _z["anchor_rowcol"]
        assert (_ar, _ac) != (_kr, _kc), (
            f"candidate {_z['id']}: the anchor must be the wall site BELOW the keypoint"
        )
        assert _ac == _kc and _ar == _kr + 25, (
            f"candidate {_z['id']}: the wall must sit downstream on the SAME channel, got {(_ar, _ac)}"
        )
        assert _z["wall_offset_downstream_m"] == 125.0, _z["wall_offset_downstream_m"]
        assert not _z["anchor_point_utm"].equals(_z["keypoint_point_utm"]), (
            "the two positions are carried separately BECAUSE they are different places"
        )
        # The drop between them is exactly the reference height: the walk
        # stops at the FIRST cell a full POOL_REFERENCE_HEIGHT_METERS below
        # the keypoint, so the pool's water surface just reaches its tail.
        assert abs(
            float(_nom_array[_kr, _kc]) - float(_nom_array[_ar, _ac]) - POOL_REFERENCE_HEIGHT_METERS
        ) < 1e-9, _z
        assert _z["level_pool"]["waterline_elevation_m"] == round(
            float(_nom_array[_kr, _kc]), 3
        ), "the waterline lands ON the keypoint -- that is what makes the keypoint the TAIL"

print(
    "Test 1 -- nomination reason codes: keypoints are processed in CATCHMENT order "
    f"{_order} (not id/slope-drop order); every keypoint-nominated candidate anchors on the WALL SITE "
    f"{_nom_zones[0]['wall_offset_downstream_m']} m downstream of its keypoint, with the full "
    f"{POOL_REFERENCE_HEIGHT_METERS} m of drop and the waterline landing back ON the keypoint (the pool's "
    f"TAIL); keypoint 1 (8.0 ac) wins the too-close pair and keypoint 0 (3.0 ac) is rejected with "
    f"'{_outcomes[0]['outcome']}' -- measured WALL-to-wall at {_outcomes[0]['anchor_rowcol']} vs "
    f"{_outcomes[1]['anchor_rowcol']}, not keypoint-to-keypoint; no snap, no relocation; all "
    f"{len(NOM_KEYPOINTS)} keypoints were attempted -- nothing was capped."
)


# =====================================================================
# Test 6 -- AN OFF-PARCEL KEYPOINT, AND WHAT THE WALL WALK DOES TO IT.
#
# Three cases, all resolved by the SAME clip-and-floor rule rather than by
# any relocation:
#   a. keypoint 3 sits 7.5 m ABOVE the drawn boundary's top edge on the
#      fourth channel. Its wall walks 125 m DOWNSTREAM, landing at (27, 48)
#      comfortably ON the parcel -- so anchor_off_parcel is FALSE even
#      though the keypoint is off-parcel. off-parcel status follows the
#      WALL, because the wall is where the structure would stand. What the
#      boundary clips is the pool's TAIL (rows 0-3), which is exactly what
#      truncated_by_boundary now means for this family. The waterline still
#      references the TRUE anchor's elevation, and lands back on the
#      keypoint.
#      (The converse -- an on-parcel keypoint whose wall walks OFF the
#      parcel, which is where anchor_off_parcel does fire -- is Test 16a;
#      the on-parcel-gain argument is Test 16b.)
#   b. the same keypoint with the floor raised above what survives the
#      clip: it drops with below_min_area, and the acreage that was judged
#      is the CLIPPED acreage.
#   c. a keypoint whose own cell exceeds the contributing-area ceiling --
#      the one nomination-mask gate a keypoint IS subject to -- reports
#      keypoint_exceeds_ceiling, and reports it BEFORE any walk happens, so
#      the diagnosis names the keypoint rather than the wall.
# =====================================================================
_off_zone = next((z for z in _nom_zones if z["keypoint_id"] == 3), None)
assert _outcomes[3]["outcome"] == REASON_NOMINATED, (
    f"TEST 6a: the off-parcel keypoint must nominate, got '{_outcomes[3]['outcome']}'"
)
assert _off_zone is not None
assert _off_zone["keypoint_rowcol"] == (2, 48), "TEST 6a: the off-parcel keypoint is retained as the tail"
assert _off_zone["anchor_rowcol"] == (27, 48), (
    "TEST 6a: the wall walked 25 cells downstream, INTO the parcel"
)
assert _off_zone["wall_offset_downstream_m"] == 125.0
assert _off_zone["anchor_off_parcel"] is False, (
    "TEST 6a: off-parcel status follows the WALL, not the keypoint -- the structure would stand at "
    "(27, 48), which is on the parcel"
)
assert FLAG_ANCHOR_OFF_PARCEL not in _off_zone["flags"]
assert _off_zone["anchor_distance_outside_boundary_m"] == 0.0, _off_zone
assert _off_zone["anchor_rowcol"] in _off_zone["cells"], (
    "TEST 6a: the anchor is on-parcel, so it survives the clip"
)
# What the boundary took is the pool's TAIL -- the off-parcel head rows.
assert _off_zone["truncated_by_boundary"] is True
assert FLAG_TRUNCATED_BY_BOUNDARY in _off_zone["flags"]
assert all(r >= 4 for r, _c in _off_zone["cells"]), "every retained cell must be on-parcel"
assert min(r for r, _c in _off_zone["cells"]) == 4, (
    "TEST 6a: the clip bites at the boundary's own first row -- the pool really did reach off-parcel "
    "ground and really was cut there"
)
# The waterline references the TRUE anchor's raw elevation, not some
# boundary-adjacent stand-in's -- and lands exactly on the keypoint.
assert _off_zone["anchor_elevation_m"] == round(float(_nom_array[27, 48]), 3), (
    "TEST 6a: the waterline must reference the real WALL cell's elevation"
)
assert _off_zone["level_pool"]["waterline_elevation_m"] == round(
    float(_nom_array[27, 48]) + POOL_REFERENCE_HEIGHT_METERS, 3
)
assert _off_zone["level_pool"]["waterline_elevation_m"] == round(float(_nom_array[2, 48]), 3), (
    "TEST 6a: and that waterline is the keypoint's own elevation -- the keypoint is the TAIL"
)
_off_acres = _off_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _off_acres >= MIN_WATER_ZONE_AREA_ACRES

# (b) the same keypoint, with the floor raised above what survives the clip.
_off_b_diag: dict = {}
_off_b = find_candidate_zones(
    NOM_DEM, NOM_PA, NOM_BOUNDARY,
    keypoints=[NOM_KEYPOINTS[3]],
    filled=_nom_filled, flow_to_row=_nom_ftr, flow_to_col=_nom_ftc, flow_accumulation=_nom_acc,
    min_water_zone_area_acres=_off_acres * 2.0,
    accumulation_seed_budget=0,
    diagnostics=_off_b_diag,
)
assert _off_b == [], "TEST 6b: too little on-parcel ground must drop the candidate"
assert _off_b_diag["keypoint_outcomes"][0]["outcome"] == REASON_BELOW_MIN_AREA, _off_b_diag
assert FLAG_TRUNCATED_BY_BOUNDARY in _off_b_diag["keypoint_outcomes"][0]["flags"], (
    "the clip flag is recorded even on a dropped nomination -- it explains the drop"
)
assert _off_b_diag["keypoint_outcomes"][0]["wall_offset_downstream_m"] == 125.0, (
    "TEST 6b: the walk still ran and is still reported -- the drop happened AFTER it, at the floor"
)

# (c) a keypoint over the contributing-area ceiling.
_ceil_kp = dict(_kp(7, 0, (20, 20), 5.0))
_ceil_diag: dict = {}
_ceil_zones = find_candidate_zones(
    NOM_DEM, NOM_PA, NOM_BOUNDARY,
    keypoints=[_ceil_kp],
    filled=_nom_filled, flow_to_row=_nom_ftr, flow_to_col=_nom_ftc, flow_accumulation=_nom_acc,
    max_valley_contributing_area_acres=float(_nom_acc[20, 20]) * cell_area_acres(NOM_DEM) / 2.0,
    accumulation_seed_budget=0,
    diagnostics=_ceil_diag,
)
assert _ceil_zones == []
assert _ceil_diag["keypoint_outcomes"][0]["outcome"] == REASON_KEYPOINT_EXCEEDS_CEILING, _ceil_diag
assert _ceil_diag["keypoint_outcomes"][0]["wall_offset_downstream_m"] is None, (
    "TEST 6c: the keypoint's own ceiling is checked BEFORE the walk -- no walk was attempted, and the "
    "sentinel says so rather than reporting a fabricated 0.0"
)
print(
    f"Test 6 -- an off-parcel keypoint under the wall walk: keypoint 3 sits "
    f"{NOM_KEYPOINTS[3]['distance_outside_boundary_m']} m outside the line, but its wall walks "
    f"{_off_zone['wall_offset_downstream_m']} m downstream to {_off_zone['anchor_rowcol']} -- ON the "
    f"parcel, so anchor_off_parcel is False and what the boundary clips is the pool's TAIL. The "
    f"waterline referenced to the wall's {_off_zone['anchor_elevation_m']} m lands back on the "
    f"keypoint's {_off_zone['level_pool']['waterline_elevation_m']} m, and the clipped remainder of "
    f"{_off_acres:.4f} ac clears the {MIN_WATER_ZONE_AREA_ACRES} ac floor. Raising the floor above that "
    f"remainder drops it with '{REASON_BELOW_MIN_AREA}' on the CLIPPED acreage, the walk still reported; "
    f"a keypoint over the contributing-area ceiling reports '{REASON_KEYPOINT_EXCEEDS_CEILING}' with no "
    "walk attempted at all."
)


# =====================================================================
# Test 7 -- UNCAPPED GENERATION + THE FAMILY-2 SURVIVOR BUDGET.
#
# Five qualifying keypoints on five separate channels, all far enough
# apart to clear the separation rule: all five must come back. Family 2
# then adds up to WATER_ACCUMULATION_SEED_BUDGET survivors of its own,
# counting SURVIVORS -- a family-2 nominee dropped at the floor does not
# consume budget, which is asserted by forcing exactly that.
# =====================================================================
# Five channels at columns 5/18/31/44/57 of a 60x66 grid, each with its
# OWN side slope (0.6 / 0.9 / 1.3 / 1.8 / 2.4 m per 5 m cell). The varying
# slopes are load-bearing: they make the five pools genuinely different
# SIZES, which is what lets a single floor value drop some family-2 seeds
# while others survive -- the contrast the survivors-not-attempts
# assertion needs. Uniform slopes would make every pool identical and the
# floor would be all-or-nothing.
_u_n = 60
_u_cols = 66
_U_CHANNELS = (5, 18, 31, 44, 57)
_U_SLOPES = (0.6, 0.9, 1.3, 1.8, 2.4)
_u_array = np.zeros((_u_n, _u_cols), dtype=np.float64)
for _r in range(_u_n):
    for _c in range(_u_cols):
        _u_array[_r, _c] = 100.0 - 0.1 * _r + min(
            _s * abs(_c - _ch) for _ch, _s in zip(_U_CHANNELS, _U_SLOPES)
        )
U_DEM = _dem(_u_array)
U_BOUNDARY = box(500000.0, 4500000.0 - _u_n * 5.0, 500000.0 + _u_cols * 5.0, 4500000.0)
_u_filled, _u_ftr, _u_ftc, _u_acc = _hydrology(U_DEM)
U_PA = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500140.0, 4499840.0, 500160.0, 4499860.0),
        "render_fill_polygon_utm": box(500140.0, 4499840.0, 500160.0, 4499860.0),
    }
]


def _u_kp(kp_id, rowcol, acres):
    x, y = pixel_center_xy(U_DEM, *rowcol)
    return {
        "id": kp_id, "valley_id": kp_id, "rowcol": rowcol, "point_utm": Point(x, y),
        "contributing_acres": acres, "on_parcel": True, "distance_outside_boundary_m": 0.0,
    }


# Row 20, not row 45: each keypoint's WALL sits 25 cells (125 m) below it
# at row 45, which is where these five candidates actually land. Put the
# keypoints at row 45 and every wall walk would run off the bottom of the
# grid -- an honest wall_site_not_found_downstream, but not what this test
# is about.
U_KEYPOINTS = [_u_kp(i, (20, ch), 9.0 - i) for i, ch in enumerate(_U_CHANNELS)]
_u_diag: dict = {}
_u_zones = find_candidate_zones(
    U_DEM, U_PA, U_BOUNDARY,
    keypoints=U_KEYPOINTS,
    filled=_u_filled, flow_to_row=_u_ftr, flow_to_col=_u_ftc, flow_accumulation=_u_acc,
    diagnostics=_u_diag,
)
_u_kp_zones = [z for z in _u_zones if z["nominated_by"] == NOMINATED_BY_KEYPOINT]
_u_f2_zones = [z for z in _u_zones if z["nominated_by"] == NOMINATED_BY_ACCUMULATION]
assert len(_u_kp_zones) == 5, (
    f"TEST 7: all 5 qualifying keypoints must survive -- nothing is capped -- got {len(_u_kp_zones)}"
)
assert all(o["outcome"] == REASON_NOMINATED for o in _u_diag["keypoint_outcomes"])
assert len(_u_f2_zones) <= WATER_ACCUMULATION_SEED_BUDGET
assert _u_diag["accumulation_survivors"] == len(_u_f2_zones)

# The budget counts SURVIVORS, not attempts: with the floor raised so that
# some family-2 seeds are dropped, the run must still deliver a full
# budget of survivors and must have ATTEMPTED more seeds than it kept.
_b_diag: dict = {}
_b_zones = find_candidate_zones(
    U_DEM, U_PA, U_BOUNDARY,
    keypoints=[],
    filled=_u_filled, flow_to_row=_u_ftr, flow_to_col=_u_ftc, flow_accumulation=_u_acc,
    min_water_zone_area_acres=0.20,
    diagnostics=_b_diag,
)
_b_dropped = [s for s in _b_diag["accumulation_seeds"] if s["candidate_id"] is None]
assert _b_dropped, "the raised floor must actually drop some family-2 seeds for this to assert anything"
assert all(s["outcome"] == REASON_BELOW_MIN_AREA for s in _b_dropped), _b_dropped
assert _b_diag["accumulation_survivors"] == len(_b_zones)
assert len(_b_zones) == WATER_ACCUMULATION_SEED_BUDGET, (
    f"TEST 7: a dropped family-2 seed must NOT consume budget -- expected "
    f"{WATER_ACCUMULATION_SEED_BUDGET} survivors, got {len(_b_zones)} from "
    f"{len(_b_diag['accumulation_seeds'])} attempts"
)
assert len(_b_diag["accumulation_seeds"]) > WATER_ACCUMULATION_SEED_BUDGET
assert _b_diag["accumulation_attempt_limit_reached"] is False, (
    "this fixture must exercise the BUDGET, not the runaway-attempt guard"
)

# THE RUNAWAY GUARD ITSELF, asserted separately. The budget counts
# survivors, so a parcel where nothing qualifies has no stopping condition
# from the budget alone -- it would delineate a pool at every eligible
# cell. Measured before the guard existed: 3600 attempts for 0 survivors
# on this class of fixture.
_g_diag: dict = {}
_g_zones = find_candidate_zones(
    U_DEM, U_PA, U_BOUNDARY,
    keypoints=[],
    filled=_u_filled, flow_to_row=_u_ftr, flow_to_col=_u_ftc, flow_accumulation=_u_acc,
    min_water_zone_area_acres=50.0,   # nothing on this parcel can reach it
    diagnostics=_g_diag,
)
assert _g_zones == []
assert _g_diag["accumulation_attempt_limit_reached"] is True
_g_expected = wcz.WATER_ACCUMULATION_SEED_ATTEMPT_LIMIT
assert len(_g_diag["accumulation_seeds"]) == _g_expected, (
    f"the runaway guard must stop family 2 at {_g_expected} attempts, got "
    f"{len(_g_diag['accumulation_seeds'])}"
)
assert int(compute_water_eligible_cells(U_DEM, U_BOUNDARY, flow_accumulation=_u_acc).sum()) > _g_expected * 10, (
    "precondition: there must be far more eligible cells than the guard allows attempts, or the guard "
    "is not what stopped the loop"
)

# Non-overlap across the full combined set.
_u_seen: set = set()
for _z in _u_zones:
    assert not _u_seen.intersection(_z["cells"]), f"candidate {_z['id']} overlaps an earlier one"
    _u_seen.update(_z["cells"])
print(
    f"Test 7 -- uncapped generation: 5 qualifying keypoints yield {len(_u_kp_zones)} candidates (no cap, "
    f"no candidate_cap_reached anywhere), family 2 then adds {len(_u_f2_zones)} more within its "
    f"{WATER_ACCUMULATION_SEED_BUDGET}-SURVIVOR budget, and the non-overlap invariant holds across all "
    f"{len(_u_zones)}. With the floor raised, {len(_b_dropped)} family-2 seed(s) were dropped at the floor "
    f"and did NOT consume budget: {len(_b_diag['accumulation_seeds'])} attempts still delivered "
    f"{len(_b_zones)} survivors. And the separate runaway guard holds: on a parcel where NOTHING can "
    f"qualify, family 2 stops at its absolute {len(_g_diag['accumulation_seeds'])}-attempt limit instead "
    f"of walking all "
    f"{int(compute_water_eligible_cells(U_DEM, U_BOUNDARY, flow_accumulation=_u_acc).sum())} eligible "
    "cells."
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
    # An ON-PARCEL anchor is always a member of its own zone. An OFF-PARCEL
    # anchor -- a WALL SITE that walked past the line -- is not: the
    # boundary clip removes it, and the candidate IS the on-parcel
    # remainder of the pool it anchors (see Test 16a). Asserting membership
    # unconditionally would forbid exactly the case this branch added.
    if not _z["anchor_off_parcel"]:
        assert _z["anchor_rowcol"] in _z["cells"], "an on-parcel anchor must be a member of its own zone"
    _z_mask = _mask_from_cells((_nom_n, _nom_cols), _z["cells"])
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
# column 20) with two keypoints on it. Each keypoint's WALL sits 25 cells
# (125 m) below it -- that is where the candidates are, and the rows below
# are the same two anchors this test has always been about:
#   keypoint 0 at ( 5, 20), 9.0 ac  -> WALL (30, 20), delineated first,
#                                      pool = rows 6-30
#   keypoint 1 at (13, 20), 4.0 ac  -> WALL (38, 20): 37.5 m from
#                                      candidate 0's footprint, so it
#                                      CLEARS the 30 m separation rule;
#                                      but its own waterline
#                                      (96.2 + 2.5 = 98.7 m) floods rows
#                                      14-38, i.e. 17 rows of ground
#                                      candidate 0 already claimed.
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
    keypoints=[_ot_kp(0, (5, 20), 9.0), _ot_kp(1, (13, 20), 4.0)],
    filled=_ot_filled,
    flow_to_row=_ot_ftr,
    flow_to_col=_ot_ftc,
    accumulation_seed_budget=0,   # this fixture is about the two keypoints only
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
        accumulation_seed_budget=1,
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
    accumulation_seed_budget=1,
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
    # The keypoint and the anchor are now genuinely different places, so
    # the distance between them is part of the contract: a consumer that
    # draws both markers needs it to avoid implying they coincide.
    "wall_offset_downstream_m",
    "anchor_off_parcel",
    "anchor_distance_outside_boundary_m",
    "anchor_rowcol",
    "anchor_point_utm",
    "anchor_elevation_m",
    "level_pool",
    "abutments",
    "abutment_found_left",
    "abutment_found_right",
    "dam_band_crosses_major_drainage_left",
    "dam_band_crosses_major_drainage_right",
    "flags",
    "truncated_by_boundary",
    "truncated_by_cap",
    "overlap_trimmed",
    "canopy_overlap_pct",
    "road_overlap_pct",
    # False where no production area is in service range. The candidate is
    # no longer dropped for it, so a consumer needs to be able to see the
    # condition rather than infer it from an empty relationship list.
    "has_service_relationship",
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
# THE WALL-SITE WALK -- the keypoint is the pool's TAIL, not its wall.
#
# FIXTURE: a 45x45 grid at 5 m whose channel down column 20 is STEEP ABOVE
# row 10 and GENTLE BELOW it -- which is what a keypoint IS, so row 10 is
# the keypoint by construction:
#
#     channel z(r) = 95 + 0.5 * (10 - r)   for r <= 10   (10% grade, steep)
#                  = 95 - 0.1 * (r - 10)   for r >= 10   (2% grade, gentle)
#     z(r, c)      = channel z(r) + 1.0 * |c - 20|       (20% side slopes)
#
# EXPECTED WALL WALK from the keypoint (10, 20) at z = 95.0: the gentle
# reach gives up 0.1 m per 5 m cell, so a full 2.5 m of drop takes 25 cells
# = 125.0 m, landing on (35, 20) at z = 92.5. That is inside the 150 m
# search bound. The waterline is then 92.5 + 2.5 = 95.0 -- EXACTLY the
# keypoint's own elevation, which is the whole point of the construction.
#
# EXPECTED POOL, cells with raw z strictly below 95.0 that drain to the
# wall: on the channel, rows 11-35 (94.9 down to 92.5). Off-channel at row
# r, z = 95 - 0.1(r-10) + |k| < 95 requires |k| < 0.1(r - 10):
#     rows 11-20 -> |k| < 0.1..1.0 -> k = 0        -> 1 cell   x 10 = 10
#     rows 21-30 -> |k| < 1.1..2.0 -> |k| <= 1     -> 3 cells  x 10 = 30
#     rows 31-35 -> |k| < 2.1..2.5 -> |k| <= 2     -> 5 cells  x  5 = 25
#     pool total = 65
# The dam band at (35, 20) reaches z >= 95.0 at |k| = 3, i.e. 15.0 m each
# side, 7 cells across columns 17-23; five of those are already pool cells,
# so the zone is 65 + 2 = 67 cells.
#
# THE TAIL REACHES THE KEYPOINT AND STOPS THERE. The pool's uppermost cell
# is row 11, one cell below the keypoint, because the keypoint sits at
# exactly the waterline and the inclusion test is a STRICT inequality. That
# is the correct relationship: the keypoint is the water's upstream LIMIT,
# never submerged ground.
#
# EXPECTED STATIONS, walking the stem upstream from the wall -- the numbers
# that motivated this whole change:
#     station 0 at (35, 20), z 92.5: |k| <= 2 -> 25.0 m; depths
#         2.5,1.5,1.5,0.5,0.5 = 6.5 -> 32.5 m^2
#     station 1 at (30, 20), z 93.0: |k| <= 1 -> 15.0 m; depths
#         2.0,1.0,1.0 = 4.0 -> 20.0 m^2      <-- NONZERO
#     station 2 at (25, 20), z 93.5: |k| <= 1 -> 15.0 m; depths
#         1.5,0.5,0.5 = 2.5 -> 12.5 m^2      <-- NONZERO
#
# THE CONTRAST, computed inline below rather than asserted from memory:
# anchoring AT the keypoint gives a 13-cell pool on the steep reach with
# stations 1 and 2 both reading a real 0.0 m -- exactly the reference
# property's symptom, reproduced in miniature.
# =====================================================================
W_SIZE = 45
W_KEYPOINT = (10, 20)
_w_array = np.zeros((W_SIZE, W_SIZE), dtype=np.float64)
for _r in range(W_SIZE):
    _w_channel = 95.0 + 0.5 * (10 - _r) if _r <= 10 else 95.0 - 0.1 * (_r - 10)
    for _c in range(W_SIZE):
        _w_array[_r, _c] = _w_channel + 1.0 * abs(_c - 20)
W_DEM = _dem(_w_array)
W_BOUNDARY = box(500000.0, 4500000.0 - W_SIZE * 5.0, 500000.0 + W_SIZE * 5.0, 4500000.0)
W_PA = [
    {
        "id": 0,
        "representative_elevation_m": 50.0,
        "polygon_utm": box(500080.0, 4499800.0, 500120.0, 4499840.0),
        "render_fill_polygon_utm": box(500080.0, 4499800.0, 500120.0, 4499840.0),
    }
]
_w_filled, _w_ftr, _w_ftc, _w_acc = _hydrology(W_DEM)


def _w_kp(rowcol, acres=5.0, kp_id=0, on_parcel=True, distance_outside=0.0, dem=None, boundary=None):
    _d = dem if dem is not None else W_DEM
    x, y = pixel_center_xy(_d, *rowcol)
    return {
        "id": kp_id, "valley_id": 0, "rowcol": rowcol, "point_utm": Point(x, y),
        "contributing_acres": acres, "on_parcel": on_parcel,
        "distance_outside_boundary_m": distance_outside,
    }


_w_diag: dict = {}
_w_zones = find_candidate_zones(
    W_DEM, W_PA, W_BOUNDARY, keypoints=[_w_kp(W_KEYPOINT)],
    filled=_w_filled, flow_to_row=_w_ftr, flow_to_col=_w_ftc, flow_accumulation=_w_acc,
    accumulation_seed_budget=0, diagnostics=_w_diag,
)
_w_outcome = _w_diag["keypoint_outcomes"][0]
assert _w_outcome["outcome"] == REASON_NOMINATED, _w_outcome
assert _w_outcome["keypoint_rowcol"] == W_KEYPOINT
assert _w_outcome["anchor_rowcol"] == (35, 20), (
    f"the wall must land 25 cells downstream where 2.5 m of drop accumulates, got "
    f"{_w_outcome['anchor_rowcol']}"
)
assert _w_outcome["wall_offset_downstream_m"] == 125.0, _w_outcome
assert _w_outcome["wall_drop_m"] == 2.5, _w_outcome
assert _w_outcome["keypoint_elevation_m"] == 95.0 and _w_outcome["anchor_elevation_m"] == 92.5
assert _w_outcome["wall_walk_end_reason"] == "reached_full_drop"

assert len(_w_zones) == 1
_w_zone = _w_zones[0]
assert _w_zone["anchor_rowcol"] == (35, 20)
assert _w_zone["keypoint_rowcol"] == W_KEYPOINT, "the keypoint is reported where detection put it"
assert _w_zone["wall_offset_downstream_m"] == 125.0
# THE HEIGHT CORRESPONDENCE: the waterline lands on the keypoint's own
# elevation, so the pool's tail reaches the keypoint and stops.
assert _w_zone["level_pool"]["waterline_elevation_m"] == 95.0, (
    "the waterline must sit at the keypoint's own elevation -- that is what anchoring a full reference "
    "height below it buys"
)
assert _w_zone["level_pool"]["waterline_elevation_m"] == _w_outcome["keypoint_elevation_m"]
_w_tail_row = min(r for r, _c in _w_zone["cells"])
assert _w_tail_row == 11, f"the pool's tail must reach the cell just below the keypoint, got row {_w_tail_row}"
assert W_KEYPOINT not in _w_zone["cells"], (
    "the keypoint sits AT the waterline, so it is the water's upstream limit and never submerged ground"
)
assert _w_zone["level_pool"]["pool_cell_count"] == 65, _w_zone["level_pool"]["pool_cell_count"]
assert len(_w_zone["cells"]) == 67, len(_w_zone["cells"])
# The dam band sits at the WALL, not at the keypoint.
assert _w_zone["abutments"]["left"]["lateral_distance_m"] == 15.0
assert _w_zone["abutments"]["right"]["lateral_distance_m"] == 15.0
_w_stations = _w_zone["level_pool"]["stations"]
assert [(s["flooded_width_m"], s["flooded_cross_section_area_m2"]) for s in _w_stations] == [
    (25.0, 32.5), (15.0, 20.0), (15.0, 12.5)
], _w_stations
assert all(s["flooded_width_m"] > 0.0 for s in _w_stations), (
    "every station must now measure real water -- the pool occupies the GENTLE reach"
)

# THE CONTRAST: what anchoring at the keypoint itself would have produced.
_w_old = delineate_level_pool(
    W_DEM, _w_filled, _w_ftr, _w_ftc, _w_acc, build_upstream_map(_w_ftr, _w_ftc), W_KEYPOINT
)
assert _w_old["waterline_elevation_m"] == 97.5
assert len(_w_old["pool_cells"]) == 13, len(_w_old["pool_cells"])
assert [s["flooded_width_m"] for s in _w_old["stations"]] == [30.0, 0.0, 0.0], (
    "the keypoint-anchored contrast must reproduce the reference property's symptom: a wide station 0 "
    "and dry stations upstream"
)
print(
    f"Test 13 -- the wall sits below the keypoint: the walk runs "
    f"{_w_outcome['wall_offset_downstream_m']} m downstream from the keypoint at "
    f"{_w_outcome['keypoint_elevation_m']} m to the wall at {_w_outcome['anchor_elevation_m']} m, giving "
    f"exactly {_w_outcome['wall_drop_m']} m of drop and a waterline back at the keypoint's own elevation. "
    f"The {_w_zone['level_pool']['pool_cell_count']}-cell pool occupies the GENTLE reach with its tail at "
    f"row {_w_tail_row} (the keypoint itself is the water's limit, not submerged), and all three stations "
    f"measure real water: {[s['flooded_width_m'] for s in _w_stations]} m. Anchoring AT the keypoint "
    f"instead gives {len(_w_old['pool_cells'])} cells on the steep reach with stations "
    f"{[s['flooded_width_m'] for s in _w_old['stations']]} m -- the reference property's symptom exactly."
)


# =====================================================================
# Test 14 -- WALK FAILURES ARE HONEST, and the two kinds are distinct.
#
# (a) FLAT LOWER REACH. The same steep-above fixture with the reach below
#     the keypoint made dead level (z = 95.0 for every r >= 10). The
#     priority-flood leaves a filled flat, this repo's strictly-positive-
#     slope D8 hands every cell the -1 sentinel, and the walk dies AT the
#     keypoint having accumulated 0.0 m. That is valley_delineation.py's
#     flat-tie limitation surfacing at NOMINATION -- reported, not fixed
#     here.
# (b) DISTANCE BOUND. The reach below the keypoint at 0.01 m per cell
#     (0.2%): 2.5 m of drop would need 250 cells = 1250 m, far past the
#     150 m bound. The walk dies at (40, 20) with 0.3 m accumulated.
#
# Both yield REASON_WALL_SITE_NOT_FOUND_DOWNSTREAM and no candidate -- no
# partial-height fallback -- but wall_walk_end_reason tells them apart,
# which matters: one is a data limitation, the other is real terrain.
# =====================================================================
def _w_variant(lower_grade):
    array = np.zeros((W_SIZE, W_SIZE), dtype=np.float64)
    for r in range(W_SIZE):
        channel = 95.0 + 0.5 * (10 - r) if r <= 10 else 95.0 - lower_grade * (r - 10)
        for c in range(W_SIZE):
            array[r, c] = channel + 1.0 * abs(c - 20)
    return _dem(array)


for _grade, _expected_end, _expected_cell, _expected_drop, _label in (
    (0.0, "flat_tie_sentinel", (10, 20), 0.0, "flat lower reach"),
    (0.01, "distance_bound", (40, 20), 0.3, "gentle-but-long lower reach"),
):
    _v_dem = _w_variant(_grade)
    _v_filled, _v_ftr, _v_ftc, _v_acc = _hydrology(_v_dem)
    _v_diag: dict = {}
    _v_zones = find_candidate_zones(
        _v_dem, W_PA, W_BOUNDARY,
        keypoints=[_w_kp(W_KEYPOINT, dem=_v_dem)],
        filled=_v_filled, flow_to_row=_v_ftr, flow_to_col=_v_ftc, flow_accumulation=_v_acc,
        accumulation_seed_budget=0, diagnostics=_v_diag,
    )
    _v_outcome = _v_diag["keypoint_outcomes"][0]
    assert _v_zones == [], f"{_label}: no partial-height fallback -- the keypoint must nominate nothing"
    assert _v_outcome["outcome"] == REASON_WALL_SITE_NOT_FOUND_DOWNSTREAM, _v_outcome
    assert _v_outcome["wall_walk_end_reason"] == _expected_end, _v_outcome
    assert _v_outcome["wall_walk_end_rowcol"] == _expected_cell, _v_outcome
    assert _v_outcome["wall_drop_m"] == _expected_drop, _v_outcome
    assert _v_outcome["anchor_rowcol"] is None
    print(
        f"Test 14 -- {_label}: the walk dies at {_v_outcome['wall_walk_end_rowcol']} "
        f"('{_v_outcome['wall_walk_end_reason']}') with {_v_outcome['wall_drop_m']} m of the "
        f"{POOL_REFERENCE_HEIGHT_METERS} m needed, so the keypoint reports "
        f"'{_v_outcome['outcome']}' and nominates nothing."
    )


# =====================================================================
# Test 15 -- THE CEILING ON THE WALL SITE.
#
# Contributing area grows strictly downstream, so a wall far enough below
# a keypoint can sit on a drainage the ceiling disqualifies even where the
# keypoint itself cleared it. Injected accumulation grid: the keypoint's
# own cell carries 10 cells (well under), the wall site at (35, 20) carries
# 500 (well over a 100-cell ceiling).
#
# EXPECTED: REASON_WALL_SITE_EXCEEDS_CEILING -- distinct from
# REASON_KEYPOINT_EXCEEDS_CEILING, which the second run below still
# produces when the KEYPOINT is the cell over the limit. The two say
# different things and must not share a code.
# =====================================================================
_ceil_acc = np.full((W_SIZE, W_SIZE), 10.0, dtype=np.float64)
_ceil_acc[30:, :] = 500.0
_CEIL_CELLS = 100.0
_ceil_acres = _CEIL_CELLS * cell_area_acres(W_DEM)
_ceil_diag: dict = {}
_ceil_zones = find_candidate_zones(
    W_DEM, W_PA, W_BOUNDARY, keypoints=[_w_kp(W_KEYPOINT)],
    filled=_w_filled, flow_to_row=_w_ftr, flow_to_col=_w_ftc, flow_accumulation=_ceil_acc,
    max_valley_contributing_area_acres=_ceil_acres, accumulation_seed_budget=0, diagnostics=_ceil_diag,
)
_ceil_outcome = _ceil_diag["keypoint_outcomes"][0]
assert _ceil_zones == []
assert _ceil_outcome["outcome"] == REASON_WALL_SITE_EXCEEDS_CEILING, _ceil_outcome
assert _ceil_outcome["anchor_rowcol"] == (35, 20), (
    "the wall site is still REPORTED -- the survey says where the disqualifying wall would have stood"
)
assert _ceil_outcome["wall_offset_downstream_m"] == 125.0

# ...and the keypoint's own ceiling failure keeps its own distinct code.
_kp_ceil_acc = np.full((W_SIZE, W_SIZE), 500.0, dtype=np.float64)
_kp_ceil_diag: dict = {}
assert find_candidate_zones(
    W_DEM, W_PA, W_BOUNDARY, keypoints=[_w_kp(W_KEYPOINT)],
    filled=_w_filled, flow_to_row=_w_ftr, flow_to_col=_w_ftc, flow_accumulation=_kp_ceil_acc,
    max_valley_contributing_area_acres=_ceil_acres, accumulation_seed_budget=0,
    diagnostics=_kp_ceil_diag,
) == []
assert _kp_ceil_diag["keypoint_outcomes"][0]["outcome"] == REASON_KEYPOINT_EXCEEDS_CEILING
print(
    f"Test 15 -- ceiling on the walk: a keypoint on 10 contributing cells whose wall site lands on 500 "
    f"(against a {_CEIL_CELLS:.0f}-cell ceiling) reports '{_ceil_outcome['outcome']}' and still names the "
    f"wall it would have been ({_ceil_outcome['anchor_rowcol']}, "
    f"{_ceil_outcome['wall_offset_downstream_m']} m down); a keypoint over the ceiling ITSELF keeps the "
    f"distinct '{REASON_KEYPOINT_EXCEEDS_CEILING}'."
)


# =====================================================================
# Test 16 -- OFF-PARCEL WALLS, both directions.
#
# (a) An ON-parcel keypoint whose wall walks PAST the boundary: the
#     existing clip-and-floor rule governs, the anchor is flagged
#     anchor_off_parcel, and the distance reported is measured AT THE WALL
#     (not the keypoint's own 0.0, which would mislabel where the
#     structure sits).
# (b) The inverse -- the reference property's 6.29-acre case in miniature.
#     An OFF-parcel keypoint whose wall walks TOWARD the parcel. The walk
#     moves the candidate's ground ONTO the parcel, so the clipped
#     on-parcel remainder is strictly LARGER than anchoring at the keypoint
#     would have given. That is the whole argument for the walk on that
#     keypoint, asserted rather than assumed.
# =====================================================================
# (a) boundary ends at row 30; the wall at row 35 is past it.
W_BOUNDARY_A = box(500000.0, 4500000.0 - 30 * 5.0, 500000.0 + W_SIZE * 5.0, 4500000.0)
_a_diag: dict = {}
_a_zones = find_candidate_zones(
    W_DEM, W_PA, W_BOUNDARY_A, keypoints=[_w_kp(W_KEYPOINT)],
    filled=_w_filled, flow_to_row=_w_ftr, flow_to_col=_w_ftc, flow_accumulation=_w_acc,
    accumulation_seed_budget=0, diagnostics=_a_diag,
)
assert len(_a_zones) == 1, _a_diag["keypoint_outcomes"]
_a_zone = _a_zones[0]
assert _a_zone["anchor_rowcol"] == (35, 20) and _a_zone["anchor_off_parcel"] is True
assert FLAG_ANCHOR_OFF_PARCEL in _a_zone["flags"]
assert _a_diag["keypoint_outcomes"][0]["on_parcel"] is True, "the KEYPOINT is on-parcel; the WALL is not"
assert _a_zone["anchor_distance_outside_boundary_m"] > 0.0, (
    "the distance must be measured at the WALL -- reporting the keypoint's own 0.0 would say the "
    "structure sits on the parcel when it does not"
)
assert _a_zone["truncated_by_boundary"] is True
assert all(r < 30 for r, _c in _a_zone["cells"])

# (b) boundary starts at row 20; the keypoint at row 10 is off-parcel and
#     its wall at row 35 is inside.
W_BOUNDARY_B = box(500000.0, 4500000.0 - W_SIZE * 5.0, 500000.0 + W_SIZE * 5.0, 4500000.0 - 20 * 5.0)
_b_kp = _w_kp(W_KEYPOINT)
_b_point = _b_kp["point_utm"]
_b_kp["on_parcel"] = False
_b_kp["distance_outside_boundary_m"] = round(_b_point.distance(W_BOUNDARY_B), 2)
assert _b_kp["distance_outside_boundary_m"] > 0.0
_b_zones = find_candidate_zones(
    W_DEM, W_PA, W_BOUNDARY_B, keypoints=[_b_kp],
    filled=_w_filled, flow_to_row=_w_ftr, flow_to_col=_w_ftc, flow_accumulation=_w_acc,
    accumulation_seed_budget=0,
)
assert len(_b_zones) == 1
_b_zone = _b_zones[0]
assert _b_zone["anchor_rowcol"] == (35, 20)
assert _b_zone["anchor_off_parcel"] is False, "the WALL is on-parcel even though the keypoint is not"
assert _b_zone["anchor_distance_outside_boundary_m"] == 0.0
_b_acres = _b_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _b_acres >= MIN_WATER_ZONE_AREA_ACRES
# What anchoring at the off-parcel keypoint would have kept, on-parcel.
_b_old_pool = delineate_level_pool(
    W_DEM, _w_filled, _w_ftr, _w_ftc, _w_acc, build_upstream_map(_w_ftr, _w_ftc), W_KEYPOINT
)
_b_old_on_parcel = [c for c in _b_old_pool["zone_cells"] if c[0] >= 20]
assert len(_b_old_on_parcel) < len(_b_zone["cells"]), (
    f"the walk toward the parcel must yield MORE on-parcel ground than anchoring at the keypoint: "
    f"{len(_b_zone['cells'])} cells vs {len(_b_old_on_parcel)}"
)
print(
    f"Test 16 -- off-parcel walls both ways: (a) an on-parcel keypoint whose wall walks past the line "
    f"gives anchor_off_parcel=True with the distance measured AT THE WALL "
    f"({_a_zone['anchor_distance_outside_boundary_m']} m) and the clip flagged; (b) an off-parcel keypoint "
    f"{_b_kp['distance_outside_boundary_m']} m outside whose wall walks INTO the parcel yields "
    f"{len(_b_zone['cells'])} on-parcel cells ({_b_acres:.4f} ac) against the "
    f"{len(_b_old_on_parcel)} that anchoring at the keypoint would have kept -- the reference property's "
    "6.29-acre case in miniature."
)


# =====================================================================
# Test 17 -- PROVENANCE, and separation enforced at the WALL.
#
# THE FIXTURE IS BUILT TO DISCRIMINATE, which is the only thing that makes
# this an assertion about WHERE the rule is applied rather than a
# restatement that the rule exists. Two keypoints on one channel:
#
#   keypoint 0 at (10, 20), 9.0 ac -> delineated first; wall (35, 20),
#                                     footprint rows 11-35
#   keypoint 1 at ( 3, 20), 4.0 ac -> high on the STEEP reach, which gives
#                                     up 0.5 m per cell, so its wall is
#                                     only 5 cells down at (8, 20)
#
# Measured on the fixture below rather than asserted from this comment:
#   keypoint 1's own cell -> candidate 0's footprint  = 37.5 m  (CLEARS 30)
#   keypoint 1's WALL     -> candidate 0's footprint  = 12.5 m  (VIOLATES)
#
# So checking at the keypoint would have ACCEPTED this nomination and put
# a second wall 12.5 m from the first candidate's water. The rule is a
# statement about where the structures sit, so it binds at the walls, and
# the two distances differing across the threshold is what proves it.
# =====================================================================
for _z in _w_zones + _a_zones + _b_zones:
    assert _z["keypoint_rowcol"] is not None and _z["anchor_rowcol"] is not None
    assert _z["keypoint_rowcol"] != _z["anchor_rowcol"], "the two positions are genuinely different places"
    assert _z["wall_offset_downstream_m"] > 0.0
    assert _z["keypoint_point_utm"].equals(Point(*pixel_center_xy(W_DEM, *_z["keypoint_rowcol"])))
    assert _z["anchor_point_utm"].equals(Point(*pixel_center_xy(W_DEM, *_z["anchor_rowcol"])))

# Family 2 carries the field too, at 0.0 -- its anchor IS the wall.
_f2_only = find_candidate_zones(
    W_DEM, W_PA, W_BOUNDARY, keypoints=[], filled=_w_filled, flow_to_row=_w_ftr,
    flow_to_col=_w_ftc, flow_accumulation=_w_acc, accumulation_seed_budget=1,
)
assert _f2_only and all(z["wall_offset_downstream_m"] == 0.0 for z in _f2_only), (
    "a family-2 anchor IS the wall by definition, so its offset is 0.0 -- not absent"
)

# Separation at the wall.
_sep_diag: dict = {}
_sep_zones = find_candidate_zones(
    W_DEM, W_PA, W_BOUNDARY,
    keypoints=[_w_kp((10, 20), acres=9.0, kp_id=0), _w_kp((3, 20), acres=4.0, kp_id=1)],
    filled=_w_filled, flow_to_row=_w_ftr, flow_to_col=_w_ftc, flow_accumulation=_w_acc,
    accumulation_seed_budget=0, diagnostics=_sep_diag,
)
_sep_by_id = {o["keypoint_id"]: o for o in _sep_diag["keypoint_outcomes"]}
assert _sep_by_id[0]["outcome"] == REASON_NOMINATED and _sep_by_id[0]["anchor_rowcol"] == (35, 20)
assert _sep_by_id[1]["anchor_rowcol"] == (8, 20), (
    "keypoint 1 sits on the STEEP reach, which gives up the reference height in 5 cells"
)
_sep_first = _sep_zones[0]["polygon_utm"]
_sep_kp_gap = Point(*pixel_center_xy(W_DEM, 3, 20)).distance(_sep_first)
_sep_wall_gap = Point(*pixel_center_xy(W_DEM, 8, 20)).distance(_sep_first)
# THE DISCRIMINATING PAIR. If either of these fell on the same side of the
# threshold the outcome below would prove nothing about WHERE the check is
# applied, so they are asserted before the outcome is.
assert _sep_kp_gap > MIN_WATER_SEED_SEPARATION_METERS, (
    f"the fixture is only discriminating if a KEYPOINT-based check would have PASSED this nomination; "
    f"keypoint 1 is {_sep_kp_gap} m from candidate 0"
)
assert _sep_wall_gap < MIN_WATER_SEED_SEPARATION_METERS, (
    f"...and only if a WALL-based check rejects it; the wall is {_sep_wall_gap} m from candidate 0"
)
assert _sep_by_id[1]["outcome"] == reason_too_close_to_candidate(0), (
    f"separation must bind at the WALL sites, got {_sep_by_id[1]['outcome']} for keypoint 1 whose wall "
    f"at (8, 20) is {_sep_wall_gap} m from candidate 0 while its keypoint is {_sep_kp_gap} m away"
)
assert len(_sep_zones) == 1, "the rejected nomination must produce no candidate at all"
print(
    f"Test 17 -- provenance and separation: every keypoint candidate carries both positions and a nonzero "
    f"wall_offset_downstream_m, family 2 carries 0.0 (its anchor IS the wall), and separation binds at the "
    f"WALL -- keypoint 1's wall at {_sep_by_id[1]['anchor_rowcol']} sits {_sep_wall_gap:.1f} m from "
    f"candidate 0 and is rejected, while its keypoint is {_sep_kp_gap:.1f} m away and would have been "
    f"ACCEPTED by a keypoint-based check. The two distances straddle the "
    f"{MIN_WATER_SEED_SEPARATION_METERS} m threshold, which is what makes this an assertion about where "
    "the rule is applied."
)


# =====================================================================
# Test 18 -- FAMILY 2 IS BYTE-IDENTICAL.
#
# The wall-site walk is keypoint-only, so a keypoints=[] run must produce
# exactly what it produced before this change. These literals were captured
# from the previous commit and are pasted here verbatim; if the
# construction change leaked into family 2, they fail.
# =====================================================================
def _family2_signature(zones):
    return [
        {
            "id": z["id"], "anchor": list(z["anchor_rowcol"]), "cells": len(z["cells"]),
            "acres": round(z["polygon_utm"].area / SQUARE_METERS_PER_ACRE, 6), "flags": z["flags"],
            "abutL": z["abutments"]["left"]["lateral_distance_m"],
            "abutR": z["abutments"]["right"]["lateral_distance_m"],
            "stations": [
                [s["status"], s["flooded_width_m"], s["flooded_cross_section_area_m2"]]
                for s in z["level_pool"]["stations"]
            ],
        }
        for z in zones
    ]


_F2_SINGLE_COLUMN_BASELINE = [
    {"id": 0, "anchor": [10, 19], "cells": 17, "acres": 0.10502,
     "flags": ["abutment_not_found_left"], "abutL": None, "abutR": 25.0,
     "stations": [["measured", 80.0, 155.0], ["measured", 0.0, 0.0], ["measured", 0.0, 0.0]]},
    {"id": 1, "anchor": [22, 19], "cells": 18, "acres": 0.111197, "flags": [],
     "abutL": 60.0, "abutR": 25.0,
     "stations": [["measured", 80.0, 155.0], ["measured", 0.0, 0.0], ["measured", 0.0, 0.0]]},
    {"id": 2, "anchor": [34, 19], "cells": 18, "acres": 0.111197, "flags": [],
     "abutL": 60.0, "abutR": 25.0,
     "stations": [["measured", 80.0, 155.0], ["measured", 0.0, 0.0], ["measured", 0.0, 0.0]]},
]
_F2_U_BASELINE = [
    {"id": 0, "anchor": [59, 57], "cells": 29, "acres": 0.179151, "flags": [],
     "abutL": 10.0, "abutR": 10.0,
     "stations": [["measured", 15.0, 13.5], ["measured", 5.0, 10.0], ["measured", 5.0, 7.5]]},
    {"id": 1, "anchor": [59, 5], "cells": 107, "acres": 0.661007, "flags": [],
     "abutL": 25.0, "abutR": 25.0,
     "stations": [["measured", 45.0, 52.5], ["measured", 35.0, 34.0], ["measured", 25.0, 19.5]]},
    {"id": 2, "anchor": [59, 18], "cells": 73, "acres": 0.450967, "flags": [],
     "abutL": 15.0, "abutR": 15.0,
     "stations": [["measured", 25.0, 35.5], ["measured", 25.0, 23.0], ["measured", 15.0, 13.5]]},
]
assert _family2_signature(
    find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, keypoints=[])
) == _F2_SINGLE_COLUMN_BASELINE, "family 2 changed on the single-column fixture"
assert _family2_signature(
    find_candidate_zones(U_DEM, U_PA, U_BOUNDARY, keypoints=[])
) == _F2_U_BASELINE, "family 2 changed on the five-channel fixture"
print(
    "Test 18 -- family 2 is byte-identical: both fixtures reproduce the anchors, cell counts, acreages, "
    "flags, abutments and every station measurement captured before the wall-site walk existed. The "
    "construction change is keypoint-only."
)


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

# Both positions reach the narrative, with the distance between them in
# FEET -- a reader must be able to tell the pool's tail from its wall.
for _ndz in _nd["zones"]:
    _ndp = _ndz["provenance"]
    if _ndp["nominated_by"] == NOMINATED_BY_KEYPOINT:
        assert _ndp["wall_offset_downstream_ft"] == round(125.0 / METERS_PER_FOOT, 1), _ndp
        assert _ndp["keypoint_id"] is not None
    else:
        assert _ndp["wall_offset_downstream_ft"] == 0.0, "family 2's anchor IS its wall"

# NOTHING in this fixture is off-parcel: every keypoint's wall walked
# DOWNSTREAM, and on this parcel downstream is inward. That is a real
# result, not a gap in coverage -- the off-parcel-wall case is exercised
# on the Test 16a fixture, whose narrative is built here so the FEET
# conversion is asserted against a genuinely off-parcel anchor.
assert all(z["provenance"]["anchor_off_parcel"] is False for z in _nd["zones"]), (
    "every wall on the nomination fixture lands on-parcel"
)
_nd_a = wcz.build_narrative_data(
    _a_zones, W_DEM, W_BOUNDARY_A,
    production_area_count=len(W_PA),
    canopy_data_available=False,
    road_data_available=False,
    nomination_diagnostics=_a_diag,
)
_nd_off = next(z for z in _nd_a["zones"] if z["provenance"]["anchor_off_parcel"])
assert _nd_off["provenance"]["anchor_distance_outside_boundary_ft"] == round(
    _a_zone["anchor_distance_outside_boundary_m"] / METERS_PER_FOOT, 1
), _nd_off["provenance"]
assert _nd_off["provenance"]["wall_offset_downstream_ft"] == round(125.0 / METERS_PER_FOOT, 1), (
    "the wall that left the parcel is the SAME wall whose offset is reported -- the two facts sit "
    "side by side rather than one standing in for the other"
)
assert json.loads(json.dumps(_nd_a)) == _nd_a

# Level-pool measurements: imperial, final, and NEVER a volume.
_nd_pool = _nd_zone["level_pool"]
assert _nd_pool["reference_height_ft"] == round(POOL_REFERENCE_HEIGHT_METERS / METERS_PER_FOOT, 1)
assert len(_nd_pool["stations"]) == 3
for _st in _nd_pool["stations"]:
    assert set(_st) == {
        "station_index", "offset_upstream_ft", "status", "along_stem_distance_ft", "bearing_deg",
        "flooded_width_ft", "flooded_cross_section_area_sqft",
    }, set(_st)
    # STATUS TRAVELS WITH THE NUMBERS: an unreachable station carries no
    # width or area, so a narrative cannot read a missing measurement as
    # dry ground.
    assert _st["status"] in ("measured", "unreachable_stem_end"), _st
    if _st["status"] != "measured":
        assert _st["flooded_width_ft"] is None and _st["flooded_cross_section_area_sqft"] is None, _st
assert "stem_upstream_length_ft" in _nd_pool and "anchor_bearing_deg" in _nd_pool
# The band-crossing findings reach the narrative alongside (never instead
# of) the abutment findings.
for _k in ("crosses_major_drainage_left", "crosses_major_drainage_right",
           "major_drainage_distance_left_ft", "major_drainage_distance_right_ft"):
    assert _k in _nd_pool, _k
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
    REASON_NOMINATED,
    reason_too_close_to_candidate(0),
]
# The off-parcel keypoint's distance travels WITH its outcome, so a
# narrative can explain a dam-at-the-edge candidate (or a below_min_area
# drop) without a second lookup.
_nd_off_outcome = next(o for o in _nd_nom["keypoint_outcomes"] if o["keypoint_id"] == 3)
assert _nd_off_outcome["on_parcel"] is False
assert _nd_off_outcome["distance_outside_boundary_ft"] == round(7.5 / METERS_PER_FOOT, 1)
assert _nd_nom["accumulation_seeds"], "the family-2 seed log must reach the narrative block too"

# position_in_parcel directly: a centred footprint reads "center", a
# corner one reads its compass word.
# NOM_BOUNDARY spans x 500000-500280, y 4499700-4499980, so its centroid
# is (500140, 4499840) and the "center" threshold is 20% of the
# equivalent-circle radius = 31.6 m.
assert NOM_BOUNDARY.bounds == (500000.0, 4499700.0, 500280.0, 4499980.0), NOM_BOUNDARY.bounds
assert wcz._position_in_parcel(
    box(500130.0, 4499830.0, 500150.0, 4499850.0), NOM_BOUNDARY
) == "center"
assert wcz._position_in_parcel(
    box(500250.0, 4499950.0, 500280.0, 4499980.0), NOM_BOUNDARY
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
    f"{_nd_zone['provenance']['keypoint_id']}, its wall "
    f"{_nd_zone['provenance']['wall_offset_downstream_ft']} ft downstream of that keypoint; the Test 16a "
    f"fixture's off-parcel wall reports "
    f"{_nd_off['provenance']['anchor_distance_outside_boundary_ft']} ft OFF parcel), the per-keypoint "
    f"outcome list with its "
    f"reason codes {[o['outcome'] for o in _nd_nom['keypoint_outcomes']]}, level-pool measurements at a "
    f"{_nd_pool['reference_height_ft']} ft reference waterline with NO capacity-named key anywhere, and "
    "an unfound abutment reported as None rather than 0.0; no-candidate case reports zone_found=False "
    "with zone=None and zones=[]."
)

# The render opening is deleted, so its wipeout fallback can no longer
# fire. This is asserted rather than merely expected: the handler above
# has been listening the whole file, and a single message would mean a
# display-only reduction had crept back in somewhere.
assert _wipeout_messages == [], (
    f"the render opening is DELETED -- no run may log its wipeout fallback, got "
    f"{len(_wipeout_messages)}: {_wipeout_messages[:2]}"
)
print(
    "\nRender-opening wipeout report: 0 fallbacks across the whole file, asserted -- there is no opening "
    "left to erode a zone. Under the previous design the reference run logged one per candidate, because "
    "the dam band is one to two cells wide by construction and an opening deletes anything narrower than "
    "2r. render_fill_polygon_utm IS polygon_utm now."
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
