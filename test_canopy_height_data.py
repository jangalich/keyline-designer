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
from shapely.geometry import box

import production_area as pa
from canopy_height_data import (
    CANOPY_HEIGHT_THRESHOLD_METERS,
    TREE_ROOT_ZONE_BUFFER_METERS,
    tree_root_zone_mask,
)
from production_area import compute_step1_eligible_cells

RESOLUTION = (5.0, 5.0)

# TREE_ROOT_ZONE_BUFFER_METERS (~3.05m) converted to a whole-cell radius at
# this resolution -- ceil(3.048 / 5.0) = 1, i.e. dilation reaches exactly
# the 8-connected ring of immediate neighbors and no further. Computed
# here the same way tree_root_zone_mask() computes it internally, so this
# test's expectations track the real function instead of a hand-guessed
# number.
_BUFFER_RADIUS_CELLS = max(1, math.ceil(TREE_ROOT_ZONE_BUFFER_METERS / ((RESOLUTION[0] + RESOLUTION[1]) / 2.0)))
assert _BUFFER_RADIUS_CELLS == 1, (
    f"test below assumes a 1-cell buffer radius at 5m resolution -- got {_BUFFER_RADIUS_CELLS}, "
    "update the adjacency assertions if TREE_ROOT_ZONE_BUFFER_METERS or RESOLUTION ever change"
)


# =====================================================================
# tree_root_zone_mask(): threshold + dilate, against synthetic HAG arrays
# =====================================================================

# --- Bare cell: every cell below the height threshold -> no tree, nothing excluded ---
bare = np.full((7, 7), 1.0, dtype=np.float32)  # 1m -- brush/tall grass, not a tree
bare_mask = tree_root_zone_mask(bare, RESOLUTION)
assert not bare_mask.any(), "an entirely below-threshold HAG array must produce an all-False tree mask"
print("tree_root_zone_mask(): a bare (below-threshold) HAG array excludes nothing.")

# --- Tree cell: a single cell at/above the threshold reads as a tree cell itself ---
single_tree = np.full((9, 9), 1.0, dtype=np.float32)
single_tree[4, 4] = CANOPY_HEIGHT_THRESHOLD_METERS  # exactly at the threshold -- must count (>=)
single_tree_mask = tree_root_zone_mask(single_tree, RESOLUTION)
assert single_tree_mask[4, 4], "a cell at exactly CANOPY_HEIGHT_THRESHOLD_METERS must count as a tree cell (>= not >)"
print("tree_root_zone_mask(): a cell at exactly the height threshold counts as a tree cell.")

# --- Tree-adjacent buffered cell: dilation reaches exactly _BUFFER_RADIUS_CELLS out,
#     including diagonals (8-connected) -- cells within that radius but NOT themselves
#     tall must still be excluded (root zone); cells further out must not. ---
assert single_tree_mask[4, 5] and single_tree_mask[4, 3], "orthogonal neighbors within the buffer radius must be excluded (root zone)"
assert single_tree_mask[3, 3] and single_tree_mask[5, 5], "diagonal neighbors within the buffer radius must also be excluded (8-connected dilation)"
assert not single_tree_mask[4, 6], "a cell 2 columns away (beyond the 1-cell buffer radius) must NOT be excluded"
assert not single_tree_mask[2, 4], "a cell 2 rows away (beyond the 1-cell buffer radius) must NOT be excluded"
assert not single_tree_mask[6, 6], "a cell 2 cells away diagonally (beyond the 1-cell buffer radius) must NOT be excluded"
print(
    "tree_root_zone_mask(): dilation correctly buffers a single tree cell out to its own root-zone radius "
    "(including diagonals) and no further."
)

# --- buffer_meters <= 0: threshold with no dilation at all ---
no_buffer_mask = tree_root_zone_mask(single_tree, RESOLUTION, buffer_meters=0.0)
assert no_buffer_mask[4, 4] and not no_buffer_mask[4, 5], "buffer_meters=0 must threshold without dilating"
print("tree_root_zone_mask(): buffer_meters=0 thresholds without any dilation.")

# --- Edge-of-grid case: a tree cell right at the grid's own edge must dilate only
#     within bounds -- no wraparound, no crash, correct shape preserved. ---
edge_array = np.full((5, 5), 1.0, dtype=np.float32)
edge_array[0, 0] = CANOPY_HEIGHT_THRESHOLD_METERS  # top-left corner cell
edge_mask = tree_root_zone_mask(edge_array, RESOLUTION)
assert edge_mask.shape == edge_array.shape
assert edge_mask[0, 0] and edge_mask[0, 1] and edge_mask[1, 0] and edge_mask[1, 1], (
    "the corner tree cell and its in-bounds neighbors (right, down, diagonal) must all be excluded"
)
assert not edge_mask[4, 4] and not edge_mask[0, 4] and not edge_mask[4, 0], (
    "dilation at a grid edge must never wrap around to the opposite edge"
)
assert int(edge_mask.sum()) == 4, f"a corner tree cell should only excuse its own 2x2 in-bounds corner, got {int(edge_mask.sum())} cells"
print("tree_root_zone_mask(): a tree cell at the grid's own edge dilates only within bounds, no wraparound.")

# --- NaN (no-HAG-data) cells never count as trees themselves ---
nan_array = np.full((5, 5), np.nan, dtype=np.float32)
nan_array[2, 2] = 10.0  # one real, tall tree cell amid otherwise no-data ground
nan_mask = tree_root_zone_mask(nan_array, RESOLUTION)
assert nan_mask[2, 2], "a real tree cell amid NaN neighbors must still be thresholded correctly"
assert not nan_mask[0, 0], "a NaN (no-data) cell outside the real tree cell's buffer radius must not be excluded"
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
#     a DEM's exact extent (no padding) should now exclude the outer ring of cells whose
#     centers fall within the setback of the true edge, on a flat (slope-eligible-
#     everywhere) 10x10 grid at 5m resolution. Cell centers sit 2.5m from the nearest
#     true edge (outer ring) or 7.5m+ (interior) -- setback_m (~3.05m) falls strictly
#     between those two distances, so this is a clean, unambiguous ring-vs-interior split. ---
assert 2.5 < setback_m < 7.5, (
    f"this test's ring-vs-interior split assumes PRODUCTION_BOUNDARY_SETBACK_METERS falls strictly between "
    f"2.5m and 7.5m at 5m resolution -- got {setback_m}m, update the grid size/resolution if this ever changes"
)

setback_dem = {
    "array": np.full((10, 10), 100.0, dtype=np.float32),  # perfectly flat -- slope excludes nothing
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500050.0,
    "crs": "EPSG:32617",
}
exact_extent_boundary = box(500000.0, 4500000.0, 500050.0, 4500050.0)  # exactly the DEM's own extent, unpadded

step1_setback = compute_step1_eligible_cells(setback_dem, exact_extent_boundary, disqualifying_soil_union_utm=None)
on_parcel_count = int(step1_setback["slope_only_mask"].sum())
assert on_parcel_count == 64, (
    f"expected the interior 8x8 block (64 cells) to survive the {setback_m:.2f}m setback on a 10x10 grid at "
    f"5m resolution (36 outer-ring cells excluded), got {on_parcel_count} surviving cells"
)
assert not step1_setback["slope_only_mask"][0, 5], "a top-edge-ring cell must be excluded by the boundary setback"
assert not step1_setback["slope_only_mask"][5, 0], "a left-edge-ring cell must be excluded by the boundary setback"
assert not step1_setback["slope_only_mask"][9, 5], "a bottom-edge-ring cell must be excluded by the boundary setback"
assert not step1_setback["slope_only_mask"][5, 9], "a right-edge-ring cell must be excluded by the boundary setback"
assert step1_setback["slope_only_mask"][5, 5], "an interior cell (well clear of the setback) must remain eligible"
print(
    f"Boundary setback wired into compute_step1_eligible_cells(): an exact-extent boundary correctly excludes "
    f"the outer ring (36 cells) via the {setback_m:.2f}m setback, leaving the interior 8x8 block ({on_parcel_count} "
    "cells) eligible."
)

# --- boundary_setback_meters=0 opts out entirely -- every on-parcel cell survives, same as before this gate ---
step1_no_setback = compute_step1_eligible_cells(
    setback_dem, exact_extent_boundary, disqualifying_soil_union_utm=None, boundary_setback_meters=0.0
)
assert int(step1_no_setback["slope_only_mask"].sum()) == 100, (
    "boundary_setback_meters=0 must disable the setback entirely, leaving every on-parcel cell eligible"
)
print("Boundary setback: boundary_setback_meters=0 correctly disables the setback (all 100 cells survive).")


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
# Padded well past the setback so this section's assertions are about the canopy gate
# specifically, not incidentally about the boundary setback tested above.
CANOPY_TEST_BOUNDARY = box(
    500000.0 - 20.0, 4500000.0 - 20.0, 500050.0 + 20.0, 4500050.0 + 20.0
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
