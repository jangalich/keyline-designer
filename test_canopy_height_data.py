"""
test_canopy_height_data.py

Offline (no-network) checks for canopy_height_data.py's network-free
tree_root_zone_mask() (threshold + raster-dilate cell-mask logic) and for
the two new gates it and the boundary setback wire into production_area.
compute_step1_eligible_cells(). Runs against small synthetic HAG arrays /
DEMs / boundary polygons built by hand, same "pure logic, independent of
real data fetches" philosophy as test_production_area.py/
test_erosion_hydric_soil.py. Live fetch validation against a real
reference property (canopy_height_data.get_canopy_height_for_boundary())
is a separate follow-up step -- this sandbox has no network egress to
Planetary Computer, same documented gap the hydric/slope gates' own live
validation already has.
"""

import math

import numpy as np
from shapely.geometry import Point, box

import production_area as pa
from canopy_height_data import (
    CANOPY_HEIGHT_THRESHOLD_METERS,
    TREE_ROOT_ZONE_BUFFER_METERS,
    tree_root_zone_mask,
)
from production_area import compute_step1_eligible_cells
from raster_grid import pixel_center_xy

RESOLUTION = (5.0, 5.0)

# TREE_ROOT_ZONE_BUFFER_METERS converted to a whole-cell radius at this
# resolution, computed here the same way tree_root_zone_mask() computes it
# internally -- so the adjacency checks below track the real function's
# current buffer distance instead of a hand-guessed cell count that would
# go stale the next time TREE_ROOT_ZONE_BUFFER_METERS is retuned.
_BUFFER_RADIUS_CELLS = max(1, math.ceil(TREE_ROOT_ZONE_BUFFER_METERS / ((RESOLUTION[0] + RESOLUTION[1]) / 2.0)))


# =====================================================================
# tree_root_zone_mask(): threshold + dilate, against synthetic HAG arrays
# =====================================================================

# --- Bare cell: every cell below the height threshold -> no tree, nothing excluded ---
bare = np.full((7, 7), 1.0, dtype=np.float32)  # 1m -- brush/tall grass, not a tree
bare_mask = tree_root_zone_mask(bare, RESOLUTION)
assert not bare_mask.any(), "an entirely below-threshold HAG array must produce an all-False tree mask"
print("tree_root_zone_mask(): a bare (below-threshold) HAG array excludes nothing.")

# --- Tree cell: a single cell at/above the threshold reads as a tree cell itself ---
# Grid sized with enough margin around the center for _BUFFER_RADIUS_CELLS worth of
# dilation plus one extra ring beyond it, so the "further out" checks below stay
# meaningfully in-bounds regardless of how large TREE_ROOT_ZONE_BUFFER_METERS is tuned to.
_CENTER_GRID_MARGIN = _BUFFER_RADIUS_CELLS + 2
_CENTER_GRID_SIZE = 2 * _CENTER_GRID_MARGIN + 1
_CENTER = _CENTER_GRID_MARGIN

single_tree = np.full((_CENTER_GRID_SIZE, _CENTER_GRID_SIZE), 1.0, dtype=np.float32)
single_tree[_CENTER, _CENTER] = CANOPY_HEIGHT_THRESHOLD_METERS  # exactly at the threshold -- must count (>=)
single_tree_mask = tree_root_zone_mask(single_tree, RESOLUTION)
assert single_tree_mask[_CENTER, _CENTER], "a cell at exactly CANOPY_HEIGHT_THRESHOLD_METERS must count as a tree cell (>= not >)"
print("tree_root_zone_mask(): a cell at exactly the height threshold counts as a tree cell.")

# --- Tree-adjacent buffered cell: dilation reaches exactly _BUFFER_RADIUS_CELLS out
#     (Chebyshev/8-connected distance), including diagonals -- cells within that radius
#     but NOT themselves tall must still be excluded (root zone); cells one cell further
#     out must not. ---
r = _BUFFER_RADIUS_CELLS
assert single_tree_mask[_CENTER, _CENTER + r] and single_tree_mask[_CENTER, _CENTER - r], (
    "orthogonal cells exactly at the buffer radius must be excluded (root zone)"
)
assert single_tree_mask[_CENTER + r, _CENTER + r] and single_tree_mask[_CENTER - r, _CENTER - r], (
    "diagonal cells exactly at the buffer radius must also be excluded (8-connected/Chebyshev dilation)"
)
assert not single_tree_mask[_CENTER, _CENTER + r + 1], "a cell one column beyond the buffer radius must NOT be excluded"
assert not single_tree_mask[_CENTER - r - 1, _CENTER], "a cell one row beyond the buffer radius must NOT be excluded"
assert not single_tree_mask[_CENTER + r + 1, _CENTER + r + 1], "a cell one diagonal step beyond the buffer radius must NOT be excluded"
print(
    f"tree_root_zone_mask(): dilation correctly buffers a single tree cell out to its own {r}-cell root-zone "
    "radius (including diagonals) and no further."
)

# --- buffer_meters <= 0: threshold with no dilation at all ---
no_buffer_mask = tree_root_zone_mask(single_tree, RESOLUTION, buffer_meters=0.0)
assert no_buffer_mask[_CENTER, _CENTER] and not no_buffer_mask[_CENTER, _CENTER + 1], "buffer_meters=0 must threshold without dilating"
print("tree_root_zone_mask(): buffer_meters=0 thresholds without any dilation.")

# --- Edge-of-grid case: a tree cell right at the grid's own edge must dilate only
#     within bounds -- no wraparound, no crash, correct shape preserved. Grid sized to
#     _BUFFER_RADIUS_CELLS plus margin so there's still a genuinely far, unaffected
#     corner to check regardless of how large the buffer radius is tuned to. ---
edge_grid_size = _BUFFER_RADIUS_CELLS + 3
edge_array = np.full((edge_grid_size, edge_grid_size), 1.0, dtype=np.float32)
edge_array[0, 0] = CANOPY_HEIGHT_THRESHOLD_METERS  # top-left corner cell
edge_mask = tree_root_zone_mask(edge_array, RESOLUTION)
assert edge_mask.shape == edge_array.shape
assert edge_mask[0, 0] and edge_mask[0, 1] and edge_mask[1, 0] and edge_mask[1, 1], (
    "the corner tree cell and its in-bounds neighbors (right, down, diagonal) must all be excluded"
)
far_corner = edge_grid_size - 1
far_chebyshev_distance = far_corner  # distance from (0, 0) to (far_corner, far_corner)
assert far_chebyshev_distance > r, "test grid must be large enough that its far corner sits genuinely beyond the buffer radius"
assert not edge_mask[far_corner, far_corner] and not edge_mask[0, far_corner] and not edge_mask[far_corner, 0], (
    "dilation at a grid edge must never wrap around to the opposite edge, and must not reach a cell "
    "genuinely beyond its own buffer radius"
)
expected_corner_cells = (r + 1) ** 2  # a (r+1)x(r+1) in-bounds block from the corner, per Chebyshev dilation
assert int(edge_mask.sum()) == expected_corner_cells, (
    f"a corner tree cell should excuse exactly its own in-bounds {r + 1}x{r + 1} corner block "
    f"({expected_corner_cells} cells), got {int(edge_mask.sum())} cells"
)
print("tree_root_zone_mask(): a tree cell at the grid's own edge dilates only within bounds, no wraparound.")

# --- NaN (no-HAG-data) cells never count as trees themselves ---
# Grid sized so the tree cell's own buffer radius doesn't reach the far corner --
# that corner needs to stay genuinely out of range to prove NaN alone isn't what
# keeps it excluded.
nan_grid_size = _BUFFER_RADIUS_CELLS + 5
nan_array = np.full((nan_grid_size, nan_grid_size), np.nan, dtype=np.float32)
nan_array[2, 2] = 10.0  # one real, tall tree cell amid otherwise no-data ground
nan_mask = tree_root_zone_mask(nan_array, RESOLUTION)
assert nan_mask[2, 2], "a real tree cell amid NaN neighbors must still be thresholded correctly"
far_nan_corner = nan_grid_size - 1
assert far_nan_corner - 2 > _BUFFER_RADIUS_CELLS, "test grid must be large enough that its far corner sits genuinely beyond the buffer radius"
assert not nan_mask[far_nan_corner, far_nan_corner], (
    "a NaN (no-data) cell genuinely outside the real tree cell's buffer radius must not be excluded"
)
print("tree_root_zone_mask(): NaN (no-HAG-data) cells are never mistaken for trees by the threshold itself.")


# =====================================================================
# Boundary setback: a plain shapely negative buffer on a synthetic parcel
# polygon, and its wiring into compute_step1_eligible_cells()'s existing
# on-parcel cell filter.
# =====================================================================

# --- Standalone geometry sanity: shrinking a known square by PRODUCTION_BOUNDARY_SETBACK_METERS
#     removes exactly a border strip of that width, all the way around. ---
setback_m = pa.PRODUCTION_BOUNDARY_SETBACK_METERS
square_parcel = box(0.0, 0.0, 100.0, 100.0)
shrunk_parcel = square_parcel.buffer(-setback_m)
expected_side = 100.0 - 2 * setback_m
assert shrunk_parcel.geom_type == "Polygon" and not shrunk_parcel.is_empty
assert abs(shrunk_parcel.area - expected_side**2) < 1e-6, (
    f"shrinking a 100x100 square inward by {setback_m}m on every side should leave a "
    f"{expected_side}x{expected_side} square ({expected_side**2} sq m), got {shrunk_parcel.area} sq m"
)
print(
    f"Boundary setback: a plain negative buffer of {setback_m:.2f}m on a synthetic 100x100 parcel square "
    f"leaves exactly the expected {expected_side:.2f}x{expected_side:.2f} interior ({shrunk_parcel.area:.2f} sq m)."
)

# --- Wired into compute_step1_eligible_cells()'s on-parcel filter: a boundary matching
#     a DEM's exact extent (no padding) should now exclude every cell whose center falls
#     within the setback of the true edge. The grid is sized dynamically off setback_m
#     itself (comfortably larger than the setback, so a genuine, non-degenerate interior
#     survives) and the expected result is computed directly from the same geometric
#     definition compute_step1_eligible_cells() itself uses -- a cell's center tested
#     against the boundary shrunk by setback_m -- rather than a hand-derived ring count,
#     so this test stays correct no matter how PRODUCTION_BOUNDARY_SETBACK_METERS is
#     retuned. ---
px, py = RESOLUTION
_setback_margin_cells = math.ceil(setback_m / ((px + py) / 2.0)) + 2
setback_grid_size = 2 * _setback_margin_cells
setback_extent = setback_grid_size * px  # px == py here, so this is exact in both dimensions

setback_dem = {
    "array": np.full((setback_grid_size, setback_grid_size), 100.0, dtype=np.float32),  # flat -- slope excludes nothing
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0 + setback_extent,
    "crs": "EPSG:32617",
}
exact_extent_boundary = box(500000.0, 4500000.0, 500000.0 + setback_extent, 4500000.0 + setback_extent)  # unpadded

step1_setback = compute_step1_eligible_cells(setback_dem, exact_extent_boundary, disqualifying_soil_union_utm=None)

shrunk_boundary = exact_extent_boundary.buffer(-setback_m)
expected_slope_only_mask = np.zeros((setback_grid_size, setback_grid_size), dtype=bool)
for r_idx in range(setback_grid_size):
    for c_idx in range(setback_grid_size):
        if shrunk_boundary.contains(Point(*pixel_center_xy(setback_dem, r_idx, c_idx))):
            expected_slope_only_mask[r_idx, c_idx] = True

on_parcel_count = int(expected_slope_only_mask.sum())
assert 0 < on_parcel_count < setback_grid_size * setback_grid_size, (
    f"test setup should produce a genuine partial exclusion (some cells survive the setback, some don't) -- "
    f"got {on_parcel_count} of {setback_grid_size * setback_grid_size}"
)
assert np.array_equal(step1_setback["slope_only_mask"], expected_slope_only_mask), (
    "compute_step1_eligible_cells()'s on-parcel test must match testing each cell's own center against the "
    "boundary shrunk by boundary_setback_meters exactly"
)
center = setback_grid_size // 2
assert step1_setback["slope_only_mask"][center, center], "the grid's own center cell (well clear of the setback) must remain eligible"
assert not step1_setback["slope_only_mask"][0, 0], "the corner cell (closest to the true boundary edge) must be excluded by the setback"
print(
    f"Boundary setback wired into compute_step1_eligible_cells(): an exact-extent boundary correctly excludes "
    f"every cell within the {setback_m:.2f}m setback, leaving {on_parcel_count} of "
    f"{setback_grid_size * setback_grid_size} cells eligible -- matching a direct cell-center-vs-shrunk-boundary "
    "test exactly."
)

# --- boundary_setback_meters=0 opts out entirely -- every on-parcel cell survives, same as before this gate ---
step1_no_setback = compute_step1_eligible_cells(
    setback_dem, exact_extent_boundary, disqualifying_soil_union_utm=None, boundary_setback_meters=0.0
)
total_setback_cells = setback_grid_size * setback_grid_size
assert int(step1_no_setback["slope_only_mask"].sum()) == total_setback_cells, (
    "boundary_setback_meters=0 must disable the setback entirely, leaving every on-parcel cell eligible"
)
print(f"Boundary setback: boundary_setback_meters=0 correctly disables the setback (all {total_setback_cells} cells survive).")


# =====================================================================
# Canopy (tree-root-zone) gate wired into compute_step1_eligible_cells():
# same sentinel-based "unchecked vs checked" convention as the existing
# hydric-soil gate.
# =====================================================================

FLAT_DEM = {
    "array": np.full((10, 10), 100.0, dtype=np.float32),
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500050.0,
    "crs": "EPSG:32617",
}
# Padded well past the setback (dynamically, off the real constant) so this section's
# assertions are about the canopy gate specifically, not incidentally about the boundary
# setback tested above.
_canopy_test_padding = setback_m + 10.0
CANOPY_TEST_BOUNDARY = box(
    500000.0 - _canopy_test_padding,
    4500000.0 - _canopy_test_padding,
    500050.0 + _canopy_test_padding,
    4500050.0 + _canopy_test_padding,
)

step1_canopy_unchecked = compute_step1_eligible_cells(FLAT_DEM, CANOPY_TEST_BOUNDARY, disqualifying_soil_union_utm=None)
assert step1_canopy_unchecked["canopy_data_available"] is False, "omitting tree_root_zone_mask_utm must read as 'never checked'"
assert not step1_canopy_unchecked["tree_root_zone_hit"].any(), "an unchecked canopy gate must exclude nothing"
assert np.array_equal(step1_canopy_unchecked["eligible_mask"], step1_canopy_unchecked["slope_only_mask"]), (
    "with soil checked-clean and canopy unchecked, eligible_mask must equal slope_only_mask exactly"
)
print("compute_step1_eligible_cells(): omitting the tree-root-zone mask reads as 'never checked' and excludes nothing (degrades gracefully).")

# A partial synthetic tree-root-zone mask -- only the west half of the grid -- must
# exclude only those cells, leaving the east half eligible (partial exclusion, not a
# whole-region veto, same shape as the existing hydric partial-overlap test).
partial_tree_mask = np.zeros(FLAT_DEM["array"].shape, dtype=bool)
partial_tree_mask[:, :5] = True  # west half (columns 0-4)

step1_canopy_partial = compute_step1_eligible_cells(
    FLAT_DEM, CANOPY_TEST_BOUNDARY, disqualifying_soil_union_utm=None, tree_root_zone_mask_utm=partial_tree_mask
)
assert step1_canopy_partial["canopy_data_available"] is True
eligible_before = int(step1_canopy_partial["slope_only_mask"].sum())
eligible_after = int(step1_canopy_partial["eligible_mask"].sum())
assert eligible_before == 100
assert eligible_after == 50, f"the west-half tree-root-zone mask should exclude exactly the 50 west-half cells, got {100 - eligible_after} excluded"
assert np.array_equal(step1_canopy_partial["tree_root_zone_hit"], partial_tree_mask), (
    "tree_root_zone_hit should exactly match the input mask when every hit cell is also on-parcel and slope-eligible"
)
assert step1_canopy_partial["eligible_mask"][5, 9], "an east-half (non-treed) cell must remain eligible"
assert not step1_canopy_partial["eligible_mask"][5, 0], "a west-half (treed) cell must be excluded"
print(
    f"compute_step1_eligible_cells(): a PARTIAL tree-root-zone mask (west half) excludes only the "
    f"{eligible_before - eligible_after} overlapping cells, leaving the east half eligible -- a partial "
    "canopy hit doesn't veto the whole region."
)

# A fully-clean (all-False) but CHECKED mask must read as available and exclude nothing --
# distinct from the unchecked-sentinel case above, even though both leave eligible_mask unchanged.
clean_tree_mask = np.zeros(FLAT_DEM["array"].shape, dtype=bool)
step1_canopy_clean = compute_step1_eligible_cells(
    FLAT_DEM, CANOPY_TEST_BOUNDARY, disqualifying_soil_union_utm=None, tree_root_zone_mask_utm=clean_tree_mask
)
assert step1_canopy_clean["canopy_data_available"] is True, "a real (all-False) mask must read as checked, unlike the sentinel default"
assert int(step1_canopy_clean["eligible_mask"].sum()) == 100
print("compute_step1_eligible_cells(): a checked, genuinely tree-free mask reads as available and excludes nothing.")


print("\nAll canopy_height_data / boundary-setback / canopy-gate checks passed.")
