"""
test_water_candidate_zones.py

Offline (no-network) checks for water_candidate_zones.py's rebuilt Step 3
pipeline: an ABSOLUTE-ceiling hard-exclusion mask -> 4-connected cluster ->
per-cluster greedy trim to a fixed survey-area target -> select ONE
candidate (highest post-trim summed flow accumulation) -> plain bounded
cell-union footprint. This is Stage 2 of the feature ("is the zone-
filtering logic correct"), deliberately independent of Stage 1 (DEM/valley
delineation accuracy -- see test_valley_delineation.py /
test_production_area.py).

These tests build small synthetic DEMs with a known, hand-verifiable
drainage pattern, or monkeypatch compute_water_eligible_cells() /
get_flow_accumulation_for_dem() to inject an exact mask / accumulation grid
where the point of a test is the clustering/trim/selection logic rather
than real D8 hydrology. production_areas are hand-built dicts in the same
shape identify_production_areas() actually produces (representative
elevation + real UTM polygons).

Verification map (see the task's numbered verification list):
  1. Boundary independence (the headline property) + old-band contrast
  2. Absolute contributing-area ceiling, no lower bound
  3. Selection happens AFTER the trim, and the order matters
  4. Greedy trim: at/below target, removes lowest-accumulation cells,
     accepts a disconnected result
  5. Boundedness: render_fill_polygon_utm subset of polygon_utm
  6. Cluster floor + undersized-but-above-floor cluster
  7. Off-parcel exclusion survives the zeroed setback
  8. No waist split: a waisted mask stays one zone
Plus retained coverage of the canopy / road / production exclusion gates,
the max-service-distance filter, no-production-areas, and the schema-valid
GeoJSON output.
"""

import math

import numpy as np
from shapely.geometry import Point, box
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
    WATER_ZONE_TARGET_ACRES,
    compute_water_eligible_cells,
    find_candidate_zones,
    zones_to_geojson,
)
import water_candidate_zones as wcz

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)


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


# The MIN_BOUNDARY_SETBACK_METERS constant is retained but zeroed.
assert MIN_BOUNDARY_SETBACK_METERS == 0.0, (
    "MIN_BOUNDARY_SETBACK_METERS must be zeroed (the constant is kept for a possible future "
    f"reintroduction, but its value is 0.0 in this design) -- got {MIN_BOUNDARY_SETBACK_METERS}"
)
assert MAX_VALLEY_CONTRIBUTING_AREA_ACRES == 20.0
assert WATER_ZONE_TARGET_ACRES == 0.5
assert MIN_WATER_ZONE_AREA_ACRES == 0.1


# =====================================================================
# A single straight drainage column at col=20 on a 40x40 grid (200x200m
# at 5m). Elevation rises with distance from col=20 AND with row, so flow
# converges onto col=20 and heads toward row 0. Accumulation along the
# column decreases from ~1600 (row 0 outlet) down to ~40 (row 39).
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


# =====================================================================
# Test 1 -- BOUNDARY INDEPENDENCE (the headline property, and the reason
# this branch exists). The absolute-ceiling gate does not depend on where
# the boundary was drawn; the old percentile band did.
# =====================================================================


def _new_eligible_set(dem, production_areas, boundary):
    mask = compute_water_eligible_cells(dem, production_areas, boundary)
    return {(int(r), int(c)) for r, c in np.argwhere(mask)}


def _old_percentile_band_set(dem, boundary, floor_acres=0.4, p_low=25.0, p_high=75.0):
    """The OLD boundary-dependent selection, computed inline as a contrast:
    a low floor defines the on-parcel 'drainage-qualifying population,'
    then a [p_low, p_high] percentile band of THAT population's own
    accumulation values decides membership. Because the population is 'the
    cells inside the drawn boundary,' the band moves when the boundary
    moves -- the bug this rewrite fixes."""
    flow = get_flow_accumulation_for_dem(dem)
    area_per_cell = cell_area_acres(dem)
    floor_mask = flow >= (floor_acres / area_per_cell)
    bp = prep(boundary)
    population = [
        float(flow[r, c])
        for r, c in np.argwhere(floor_mask)
        if bp.contains(Point(*pixel_center_xy(dem, int(r), int(c))))
    ]
    if not population:
        return set()
    lo = np.percentile(population, p_low)
    hi = np.percentile(population, p_high)
    band = floor_mask & (flow >= lo) & (flow <= hi)
    return {
        (int(r), int(c))
        for r, c in np.argwhere(band)
        if bp.contains(Point(*pixel_center_xy(dem, int(r), int(c))))
    }


# Two boundaries that both fully contain the col=20 drainage channel.
# BOUNDARY_WIDE is the full extent; BOUNDARY_NARROW drops the western and
# southern margins, so it is a strict subset -- the shared area is exactly
# BOUNDARY_NARROW.
BOUNDARY_WIDE = box(500000.0, 4499800.0, 500200.0, 4500000.0)
BOUNDARY_NARROW = box(500060.0, 4499860.0, 500200.0, 4500000.0)
_shared = BOUNDARY_WIDE.intersection(BOUNDARY_NARROW)
_shared_prepared = prep(_shared)
_shared_cells = {
    (int(r), int(c))
    for r, c in np.argwhere(np.ones((SIZE, SIZE), dtype=bool))
    if _shared_prepared.contains(Point(*pixel_center_xy(SINGLE_COLUMN_DEM, int(r), int(c))))
}
assert _shared_cells, "test setup: the two boundaries must share a nonempty area"
# The channel (col=20) must be inside the shared area, or this proves nothing.
assert any(c == MID_COL for _r, c in _shared_cells), "test setup: shared area must contain the col=20 channel"

_new_wide = _new_eligible_set(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY_WIDE) & _shared_cells
_new_narrow = _new_eligible_set(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY_NARROW) & _shared_cells
assert _new_wide == _new_narrow, (
    "BOUNDARY INDEPENDENCE VIOLATED: the absolute-ceiling eligible set must be identical within the shared "
    f"area regardless of boundary. wide-only={sorted(_new_wide - _new_narrow)[:5]}, "
    f"narrow-only={sorted(_new_narrow - _new_wide)[:5]}"
)

_old_wide = _old_percentile_band_set(SINGLE_COLUMN_DEM, BOUNDARY_WIDE) & _shared_cells
_old_narrow = _old_percentile_band_set(SINGLE_COLUMN_DEM, BOUNDARY_NARROW) & _shared_cells
assert _old_wide != _old_narrow, (
    "test contrast failed: the OLD percentile band should DIFFER within the shared area between the two "
    "boundaries (that is the boundary-dependence bug). If this fires, pick boundaries whose populations "
    "differ more."
)
print(
    "Test 1 -- boundary independence: NEW absolute-ceiling gate gives the IDENTICAL eligible cell set within "
    f"the shared area under both boundaries ({len(_new_wide)} cells each). The OLD percentile band gives "
    f"DIFFERENT sets ({len(_old_wide)} vs {len(_old_narrow)} cells in the shared area; symmetric difference "
    f"{len(_old_wide ^ _old_narrow)} cells) -- the boundary-dependence bug, reproduced inline as a contrast."
)


# =====================================================================
# Test 2 -- ABSOLUTE CEILING GATE, no lower bound. Inject a known
# accumulation grid via monkeypatch so the exact cells above/below the
# 20-acre ceiling are controlled. A production area covering the whole
# extent makes every cell pass the service-distance / on-parcel gates, so
# only the ceiling gate differentiates.
# =====================================================================
_ceiling_shape = (1, 6)
_ceiling_dem = _dem(np.zeros(_ceiling_shape, dtype=np.float32))
_ceiling_boundary = box(500000.0, 4500000.0 - _ceiling_shape[0] * 5.0, 500000.0 + _ceiling_shape[1] * 5.0, 4500000.0)
_ceiling_area_per_cell = cell_area_acres(_ceiling_dem)
# ceiling in cell-count units: a cell with this many contributing cells is
# exactly at 20 acres.
_ceiling_cells = MAX_VALLEY_CONTRIBUTING_AREA_ACRES / _ceiling_area_per_cell
# Build accumulation so cols map to: 1 (far below), just-below, exactly-at,
# just-above, far-above, and another below.
_ceiling_accum = np.array(
    [[1.0, _ceiling_cells - 5.0, _ceiling_cells, _ceiling_cells + 5.0, _ceiling_cells * 3.0, 50.0]],
    dtype=np.float64,
)
_ceiling_pa = [
    {
        "id": 0,
        "representative_elevation_m": -5.0,
        "polygon_utm": _ceiling_boundary,  # covers everything -> every cell touches (distance 0)
        "render_fill_polygon_utm": box(0.0, 0.0, 1.0, 1.0),  # tiny/off to the side -> no production exclusion
    }
]

_orig_flow = wcz.get_flow_accumulation_for_dem
wcz.get_flow_accumulation_for_dem = lambda dem: _ceiling_accum
try:
    _ceiling_mask = compute_water_eligible_cells(_ceiling_dem, _ceiling_pa, _ceiling_boundary)
finally:
    wcz.get_flow_accumulation_for_dem = _orig_flow

assert bool(_ceiling_mask[0, 0]), "a cell far BELOW the ceiling (accum=1) must be eligible -- there is NO lower bound"
assert bool(_ceiling_mask[0, 1]), "a cell just below the ceiling must be eligible"
assert bool(_ceiling_mask[0, 2]), "a cell exactly AT the 20-acre ceiling must be eligible (<= is inclusive)"
assert not bool(_ceiling_mask[0, 3]), "a cell just ABOVE the ceiling must be excluded"
assert not bool(_ceiling_mask[0, 4]), "a cell far above the ceiling must be excluded"
assert bool(_ceiling_mask[0, 5]), "another below-ceiling cell (accum=50) must be eligible -- no lower bound anywhere"
print(
    "Test 2 -- absolute ceiling: cells at/below 20 acres of contributing area are eligible (including "
    "accum=1, proving there is NO lower bound), cells above 20 acres are excluded. A physical, boundary-"
    "independent threshold."
)


# =====================================================================
# Helpers for the trim/selection tests: run find_candidate_zones() with an
# injected eligible mask + accumulation grid, isolating the clustering /
# trim / selection logic from the per-cell gates and real hydrology.
# =====================================================================
def _run_with_injected(dem, mask, accum, production_areas, boundary):
    orig_compute = wcz.compute_water_eligible_cells
    orig_flow = wcz.get_flow_accumulation_for_dem
    wcz.compute_water_eligible_cells = lambda *a, **kw: mask
    wcz.get_flow_accumulation_for_dem = lambda d: accum
    try:
        return find_candidate_zones(dem, production_areas, boundary)
    finally:
        wcz.compute_water_eligible_cells = orig_compute
        wcz.get_flow_accumulation_for_dem = orig_flow


# A big flat grid + one production area within service distance of every
# cluster, so relationships are always non-empty and clustering/trim is
# what's exercised.
BIG = 60
_big_array = np.full((BIG, BIG), 100.0, dtype=np.float32)
BIG_DEM = _dem(_big_array)
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
# Test 3 -- SELECTION HAPPENS AFTER THE TRIM, AND THE ORDER MATTERS.
# Three disjoint clusters:
#   A: compact, on a strong channel (80 cells @ accum 100) -> post-trim sum 8000
#   B: large and sprawling, low per-cell (200 cells @ accum 50) -> full sum
#      10000 (highest PRE-trim), post-trim top-80 sum 4000
#   C: medium (80 cells @ accum 30) -> sum 2400
# Post-trim winner is A; a PRE-trim sum ranking would pick B.
# =====================================================================
assert TARGET_CELL_COUNT == 80, f"fixtures assume target_cell_count 80 at 5m/0.5ac, got {TARGET_CELL_COUNT}"

_clusterA = _rect_cells(2, 10, 2, 12)    # 8x10 = 80 cells, compact
_clusterB = _rect_cells(2, 22, 20, 30)   # 20x10 = 200 cells, sprawling
_clusterC = _rect_cells(40, 48, 2, 12)   # 8x10 = 80 cells, medium
# Guard: genuinely disjoint under 4-connectivity.
_sel_mask = _mask_from_cells((BIG, BIG), _clusterA + _clusterB + _clusterC)
_lbl, _ncomp = connected_components(_sel_mask, connectivity=4)
assert _ncomp == 3, f"test setup: three clusters must be disjoint, got {_ncomp} components"

_sel_accum = np.zeros((BIG, BIG), dtype=np.float64)
for _r, _c in _clusterA:
    _sel_accum[_r, _c] = 100.0
for _r, _c in _clusterB:
    _sel_accum[_r, _c] = 50.0
for _r, _c in _clusterC:
    _sel_accum[_r, _c] = 30.0

# Inline contrast: sums BEFORE the trim vs AFTER the trim.
_full_sum = {
    "A": sum(_sel_accum[r, c] for r, c in _clusterA),
    "B": sum(_sel_accum[r, c] for r, c in _clusterB),
    "C": sum(_sel_accum[r, c] for r, c in _clusterC),
}
_post_trim_sum = {
    "A": sum(sorted(_sel_accum[r, c] for r, c in _clusterA)[-TARGET_CELL_COUNT:]),
    "B": sum(sorted(_sel_accum[r, c] for r, c in _clusterB)[-TARGET_CELL_COUNT:]),
    "C": sum(sorted(_sel_accum[r, c] for r, c in _clusterC)[-TARGET_CELL_COUNT:]),
}
assert max(_full_sum, key=_full_sum.get) == "B", "test setup: B must win on PRE-trim sum"
assert max(_post_trim_sum, key=_post_trim_sum.get) == "A", "test setup: A must win on POST-trim sum"

_sel_zones = _run_with_injected(BIG_DEM, _sel_mask, _sel_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_sel_zones) == 1, f"exactly one zone must be selected, got {len(_sel_zones)}"
# The winning zone must be cluster A: its cells all lie in A's row/col span.
_winner_cells = set(_sel_zones[0]["cells"])
_A_set = set(_clusterA)
assert _winner_cells == _A_set, (
    "SELECTION-AFTER-TRIM VIOLATED: the compact strong-channel cluster A must win on post-trim sum. "
    f"Selected {len(_winner_cells)} cells; A has {len(_A_set)}; overlap {len(_winner_cells & _A_set)}."
)
print(
    "Test 3 -- selection AFTER trim: the compact strong-channel cluster (A) is selected. "
    f"PRE-trim sums {{A:{_full_sum['A']:.0f}, B:{_full_sum['B']:.0f}, C:{_full_sum['C']:.0f}}} would pick B "
    f"(the sprawling cluster); POST-trim sums {{A:{_post_trim_sum['A']:.0f}, B:{_post_trim_sum['B']:.0f}, "
    f"C:{_post_trim_sum['C']:.0f}}} correctly pick A. Ordering enforced in executable form."
)


# =====================================================================
# Test 4 -- GREEDY TRIM: a single cluster above target trims to at/below
# the target, removing EXACTLY the lowest-accumulation cells, and a
# DISCONNECTED retained set is accepted rather than raising.
# =====================================================================
_trim_cluster = _rect_cells(5, 25, 5, 25)  # 20x20 = 400 cells, well above target
_trim_mask = _mask_from_cells((BIG, BIG), _trim_cluster)
# Give every cell a DISTINCT accumulation so "lowest removed" is unambiguous,
# and arrange the highest values on a scattered (checkerboard) subset so the
# top-N retained set is genuinely disconnected.
_trim_accum = np.zeros((BIG, BIG), dtype=np.float64)
_ordered_cells = list(_trim_cluster)
for _i, (_r, _c) in enumerate(_ordered_cells):
    # base distinct value; boost checkerboard cells so the retained top-N is scattered
    _trim_accum[_r, _c] = 1.0 + _i + (100000.0 if (_r + _c) % 2 == 0 else 0.0)

_trim_zones = _run_with_injected(BIG_DEM, _trim_mask, _trim_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_trim_zones) == 1
_trim_zone = _trim_zones[0]
_retained = _trim_zone["cells"]
assert len(_retained) == TARGET_CELL_COUNT, (
    f"trim must keep exactly target_cell_count ({TARGET_CELL_COUNT}) cells, kept {len(_retained)}"
)
_area = _trim_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _area <= WATER_ZONE_TARGET_ACRES + 1e-9, (
    f"trimmed zone area ({_area:.4f} ac) must be at or below the target ({WATER_ZONE_TARGET_ACRES})"
)
# The retained cells must be exactly the target_cell_count HIGHEST by accumulation.
_expected_top = set(
    sorted(_trim_cluster, key=lambda rc: _trim_accum[rc[0], rc[1]])[-TARGET_CELL_COUNT:]
)
assert set(_retained) == _expected_top, "the removed cells must be exactly the lowest-accumulation ones"

# Equivalence: the implemented "sort by accumulation, take top N" is
# identical to iterative "remove the single lowest-accumulation cell until
# at or below target." Computed inline here as an executable contrast.
_iterative = list(_trim_cluster)
while len(_iterative) > TARGET_CELL_COUNT:
    _iterative.remove(min(_iterative, key=lambda rc: (_trim_accum[rc[0], rc[1]], rc[0], rc[1])))
assert set(_iterative) == set(_retained), (
    "top-N-by-accumulation must be identical to iterative lowest-first removal on this fixture"
)
# Accepts a disconnected result: the retained checkerboard set is not 4-connected.
_ret_mask = _mask_from_cells((BIG, BIG), _retained)
_, _ret_components = connected_components(_ret_mask, connectivity=4)
assert _ret_components > 1, "test setup: retained top-N should be genuinely disconnected to exercise the case"
print(
    f"Test 4 -- greedy trim: a 400-cell cluster trims to exactly {TARGET_CELL_COUNT} cells "
    f"({_area:.4f} ac <= {WATER_ZONE_TARGET_ACRES}), keeping precisely the highest-accumulation cells; the "
    f"retained set is {_ret_components} disconnected pieces and is ACCEPTED (no connectivity enforced, no raise)."
)


# =====================================================================
# Test 5 -- BOUNDEDNESS: render_fill_polygon_utm is a subset of polygon_utm
# (and of the boundary) on every produced zone. Checked here and reasserted
# on every other fixture that yields a zone.
# =====================================================================
def _assert_bounded(zone, boundary, label):
    rf = zone["render_fill_polygon_utm"]
    pu = zone["polygon_utm"]
    assert rf.area <= pu.area * (1 + 1e-9) + 1e-6, f"{label}: render_fill must not exceed polygon_utm area"
    assert boundary.buffer(1e-6).contains(rf), f"{label}: render_fill must stay within the boundary"
    # Plain footprint == polygon_utm (no hull, no opening).
    assert abs(rf.area - pu.area) < 1e-6, f"{label}: render_fill must equal the plain polygon_utm footprint"


_assert_bounded(_sel_zones[0], BIG_BOUNDARY, "test3-winner")
_assert_bounded(_trim_zones[0], BIG_BOUNDARY, "test4-trim")
print("Test 5 -- boundedness: render_fill_polygon_utm subset of polygon_utm (equal, plain footprint) on every fixture.")


# =====================================================================
# Test 6 -- CLUSTER FLOOR + UNDERSIZED cluster. A cluster below
# MIN_WATER_ZONE_AREA_ACRES (0.1) is discarded BEFORE ranking, even with
# very high per-cell accumulation; a cluster between the floor and the
# target survives, passes through UNTRIMMED, and is ranked on its own
# smaller post-trim sum with no padding.
# =====================================================================
_floor_cells = MIN_WATER_ZONE_AREA_ACRES / CELL_AREA_ACRES  # ~16.2 cells worth of footprint
_tiny_cluster = _rect_cells(2, 4, 2, 7)     # 2x5 = 10 cells (~0.062 ac) -- BELOW the 0.1 floor
_mid_cluster = _rect_cells(20, 26, 20, 30)  # 6x10 = 40 cells (~0.247 ac) -- between floor and target
_floor_mask = _mask_from_cells((BIG, BIG), _tiny_cluster + _mid_cluster)
_lbl6, _n6 = connected_components(_floor_mask, connectivity=4)
assert _n6 == 2, "test setup: tiny and mid clusters must be disjoint"

_floor_accum = np.zeros((BIG, BIG), dtype=np.float64)
for _r, _c in _tiny_cluster:
    _floor_accum[_r, _c] = 10000.0  # very high per-cell -- would win on any per-cell metric, but is below the floor
for _r, _c in _mid_cluster:
    _floor_accum[_r, _c] = 20.0

# Sanity: the tiny cluster is genuinely below the floor, the mid one above it and below the target.
_tiny_area = cell_union_footprint(BIG_DEM, _mask_from_cells((BIG, BIG), _tiny_cluster)).intersection(BIG_BOUNDARY).area / SQUARE_METERS_PER_ACRE
_mid_area = cell_union_footprint(BIG_DEM, _mask_from_cells((BIG, BIG), _mid_cluster)).intersection(BIG_BOUNDARY).area / SQUARE_METERS_PER_ACRE
assert _tiny_area < MIN_WATER_ZONE_AREA_ACRES, f"tiny cluster must be below the floor, got {_tiny_area:.3f} ac"
assert MIN_WATER_ZONE_AREA_ACRES < _mid_area < WATER_ZONE_TARGET_ACRES, (
    f"mid cluster must be between floor and target, got {_mid_area:.3f} ac"
)

_floor_zones = _run_with_injected(BIG_DEM, _floor_mask, _floor_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_floor_zones) == 1, "the tiny cluster is discarded pre-ranking, leaving exactly the mid cluster"
_floor_zone = _floor_zones[0]
assert set(_floor_zone["cells"]) == set(_mid_cluster), (
    "the surviving zone must be the mid cluster (the tiny, high-accumulation cluster is dropped by the size floor "
    "BEFORE ranking, not ranked and beaten)"
)
assert len(_floor_zone["cells"]) == len(_mid_cluster), "the mid cluster is UNTRIMMED (below target), not padded or cut"
_expected_mid_sum = float(sum(_floor_accum[r, c] for r, c in _mid_cluster))
_actual_mid_sum = float(sum(_floor_accum[r, c] for r, c in _floor_zone["cells"]))
assert _actual_mid_sum == _expected_mid_sum, "ranked on its own raw post-trim sum, no padding/normalisation"
_assert_bounded(_floor_zone, BIG_BOUNDARY, "test6-mid")
print(
    f"Test 6 -- cluster floor: the 10-cell tiny cluster ({_tiny_area:.3f} ac, per-cell accum 10000) is DISCARDED "
    f"before ranking; the 40-cell mid cluster ({_mid_area:.3f} ac) survives UNTRIMMED and is ranked on its own raw "
    f"post-trim sum ({_actual_mid_sum:.0f}) with no padding."
)


# =====================================================================
# Test 7 -- OFF-PARCEL exclusion survives the zeroed setback. A DEM whose
# extent is larger than the boundary means some cell centers sit OFF the
# parcel; they must be excluded even though MIN_BOUNDARY_SETBACK_METERS is
# 0.0. Uses the REAL compute_water_eligible_cells so the on-parcel gate runs.
# =====================================================================
_op_size = 20
_op_array = np.zeros((_op_size, _op_size), dtype=np.float32)
for _row in range(_op_size):
    for _col in range(_op_size):
        _op_array[_row, _col] = abs(_col - 10) * 2.0 + _row * 0.5
_OP_DEM = _dem(_op_array)  # extent: x [500000, 500100], y [4499900, 4500000]
# Boundary covers only the EASTERN half (cols ~10-19); the western half is off-parcel.
_OP_BOUNDARY = box(500050.0, 4499900.0, 500100.0, 4500000.0)
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
_off_parcel_eligible = [
    (int(r), int(c))
    for r, c in np.argwhere(_op_mask)
    if not _op_prepared.contains(Point(*pixel_center_xy(_OP_DEM, int(r), int(c))))
]
assert not _off_parcel_eligible, (
    f"OFF-PARCEL exclusion failed with the zeroed setback: {_off_parcel_eligible[:5]} lie outside the boundary "
    "but were marked eligible"
)
# And there ARE eligible on-parcel cells (so the mask isn't trivially empty).
assert int(_op_mask.sum()) > 0, "test setup: some on-parcel cells should still be eligible"
# Cross-check: cells whose centers are west of the boundary exist and are all excluded.
_west_cells = [
    (r, c)
    for r in range(_op_size)
    for c in range(_op_size)
    if not _op_prepared.contains(Point(*pixel_center_xy(_OP_DEM, r, c)))
]
assert _west_cells and all(not _op_mask[r, c] for r, c in _west_cells)
print(
    f"Test 7 -- off-parcel exclusion survives the zeroed setback (MIN_BOUNDARY_SETBACK_METERS=0.0): "
    f"{int(_op_mask.sum())} on-parcel cells eligible, 0 of {len(_west_cells)} off-parcel cells eligible."
)


# =====================================================================
# Test 8 -- NO WAIST SPLIT. A waisted ("dumbbell") eligible mask whose two
# lobes are joined by a thin 4-connected strip clusters into ONE component
# and emerges as ONE zone (the old design split it into two).
# =====================================================================
_lobe_a = _rect_cells(0, 12, 0, 12)
_lobe_b = _rect_cells(0, 12, 16, 28)
_narrow_strip = _rect_cells(5, 7, 12, 16)  # 2-row strip bridging the lobes
_dumbbell = _mask_from_cells((BIG, BIG), _lobe_a + _lobe_b + _narrow_strip)
_lbl8, _n8 = connected_components(_dumbbell, connectivity=4)
assert _n8 == 1, "test setup: the dumbbell must be a single 4-connected component (the strip bridges the lobes)"

_dumbbell_accum = np.zeros((BIG, BIG), dtype=np.float64)
for _r, _c in _lobe_a + _lobe_b + _narrow_strip:
    _dumbbell_accum[_r, _c] = 100.0

_waist_zones = _run_with_injected(BIG_DEM, _dumbbell, _dumbbell_accum, CENTER_PA, BIG_BOUNDARY)
assert len(_waist_zones) == 1, (
    f"a waisted mask must emerge as ONE zone (no waist split anymore), got {len(_waist_zones)}"
)
_assert_bounded(_waist_zones[0], BIG_BOUNDARY, "test8-waist")
print("Test 8 -- no waist split: a waisted (dumbbell) mask emerges as exactly 1 zone, not 2.")


# =====================================================================
# Retained gate coverage (unchanged behaviour): canopy / road / production
# exclusion, max-service-distance, no-production-areas, GeoJSON schema.
# =====================================================================

# --- no production areas -> no zones ---
assert find_candidate_zones(SINGLE_COLUMN_DEM, [], BOUNDARY) == []
print("Gate -- no production-area candidates means no water zones.")

# --- the single-column fixture yields exactly one trimmed zone ---
_base_zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
assert len(_base_zones) == 1
_base_zone = _base_zones[0]
_base_area = _base_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert _base_area <= WATER_ZONE_TARGET_ACRES + 1e-9, f"the selected zone must be trimmed to <= target, got {_base_area:.4f}"
assert _base_zone["id"] == 0
assert _base_zone["served_production_area_ids"] == [0]
assert _base_zone["optimal_subarea_polygon_utm"] is None, "select_optimal_survey_subarea() is dead on this path -> None"
assert _base_zone["optimal_subarea_acres"] is None
assert _base_zone["primary_production_area_relationship"]["above_production_area"] is True
assert _base_zone["contributing_area_cells"] > 0 and _base_zone["slope_pct"] >= 0
_assert_bounded(_base_zone, BOUNDARY, "single-column")
print(
    f"Gate -- single drainage column yields exactly 1 trimmed zone ({_base_area:.4f} ac <= {WATER_ZONE_TARGET_ACRES}), "
    "id 0, above_production_area True, optimal_subarea fields None."
)

# --- gravity is a preference: a production area ABOVE the column (pump-required) still yields a zone ---
PRODUCTION_AREA_BELOW = [
    {
        "id": 5,
        "representative_elevation_m": 100.0,
        "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
        "render_fill_polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
    }
]
_below_zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_BELOW, BOUNDARY)
assert len(_below_zones) == 1
assert _below_zones[0]["primary_production_area_relationship"]["above_production_area"] is False
assert _below_zones[0]["primary_production_area_relationship"]["elevation_differential_m"] < 0
print("Gate -- a below-elevation (pump-required) production area still yields a real zone, negative differential.")

# --- max service distance still enforced ---
_too_far = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, max_service_distance_meters=1.0)
assert _too_far == [], "a max_service_distance tighter than every cell's distance must exclude everything"
print("Gate -- max service distance is still a real, enforced generation-time filter.")

# --- canopy gate: all-trees mask excludes everything; all-clear matches baseline ---
_all_trees = np.ones(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_no_trees = np.zeros(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_canopy_excluded = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees
)
_baseline_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
_canopy_clear = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_no_trees
)
assert int(_canopy_excluded.sum()) == 0
assert int(_canopy_clear.sum()) == int(_baseline_mask.sum())
assert find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees) == []
print("Gate -- canopy: an all-trees mask hard-excludes every cell; an all-clear mask matches baseline.")

# --- road gate: whole-boundary union excludes everything; None is a no-op ---
_road_excluded = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=BOUNDARY
)
_road_none = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=None
)
assert int(_road_excluded.sum()) == 0
assert int(_road_none.sum()) == int(_baseline_mask.sum())
print("Gate -- road: a whole-boundary road union excludes every cell; a real None is a no-op.")

# --- production exclusion gate: render_fill covering the parcel excludes everything ---
PRODUCTION_FULL_OVERLAP = [
    {
        "id": 0,
        "representative_elevation_m": -5.0,
        "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0),
        "render_fill_polygon_utm": BOUNDARY,  # covers the whole parcel
    }
]
_full_overlap = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_FULL_OVERLAP, BOUNDARY)
assert int(_full_overlap.sum()) == 0, "a production render_fill covering the parcel must exclude every cell"
print("Gate -- production exclusion: a render_fill covering the parcel hard-excludes every water-zone cell.")

# --- GeoJSON output is schema-valid on the required layer ---
_geojson = zones_to_geojson(_base_zones)
validate_feature_collection(_geojson)
_feat = _geojson["features"][0]
assert _feat["properties"]["layer"] == "water_system_candidate"
assert _feat["id"] == "water-system-candidate-0"
assert _feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
assert "production_area_relationships" in _feat["properties"]
assert "primary_production_area_relationship" in _feat["properties"]
assert "contributing_area_cells" in _feat["properties"]
assert "slope_pct" in _feat["properties"]
assert _feat["properties"]["optimal_subarea_acres"] is None
print("Gate -- zones_to_geojson output is schema-valid, layer='water_system_candidate', polygon geometry.")


print("\nAll water_candidate_zones checks passed.")
