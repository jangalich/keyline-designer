"""
test_raster_grid.py

Offline (no-network) checks for raster_grid.py's shared, dependency-free
grid helpers.
"""

import math

import numpy as np

from raster_grid import (
    attempt_waist_split,
    build_disc_kernel_offsets,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
)

# --- build_disc_kernel_offsets(): non-square resolution produces a  ---
# --- genuinely elliptical kernel in cell terms, not a circle        ---
#
# Moved here from test_topographic_position.py along with the function
# itself (build_disc_kernel_offsets() is shared raster/grid geometry, not
# a TPI-specific concept -- see raster_grid.py's own docstring for it).

non_square_resolution = (5.0, 10.0)  # finer in x than y
offsets = build_disc_kernel_offsets(non_square_resolution, radius_meters=20.0)

# Hand-derived: condition is hypot(dc*5, dr*10) <= 20.
#   dr=0:  |dc*5| <= 20            -> dc in -4..4  -> 9 offsets
#   dr=+-1: (dc*5)^2 <= 400-100=300 -> |dc| <= 3.46 -> dc in -3..3 -> 7 offsets each
#   dr=+-2: (dc*5)^2 <= 400-400=0   -> dc == 0 only  -> 1 offset each
#   total = 9 + 2*7 + 2*1 = 25
count_dr0 = sum(1 for dr, dc in offsets if dr == 0)
count_dr_extreme = sum(1 for dr, dc in offsets if dr == 2)
total_count = len(offsets)

assert count_dr0 == 9, f"dr=0 row should include 9 cells (dc in -4..4), got {count_dr0}"
assert count_dr_extreme == 1, f"dr=2 row should include exactly 1 cell (dc == 0 only), got {count_dr_extreme}"
assert total_count == 25, f"expected 25 total kernel cells at radius=20.0, resolution=(5.0, 10.0), got {total_count}"
assert count_dr0 > count_dr_extreme, (
    "kernel should be wider along the finer-resolution (x) axis than tall along the coarser (y) axis -- "
    f"an ellipse in cell terms, not a circle (dr=0 count {count_dr0} vs dr=2 count {count_dr_extreme})"
)
print(
    f"Non-square resolution (5.0, 10.0) kernel at radius=20.0: {total_count} total cells, "
    f"{count_dr0} at dr=0 vs {count_dr_extreme} at dr=+-2 -- confirmed elliptical, not circular."
)


# --- build_disc_kernel_offsets(): radius=50.0 at square (5.0, 5.0)  ---
# --- resolution matches an independently-derived lattice-point count ---
#
# At square resolution, hypot(dc*px, dr*py) <= radius reduces to
# dc**2 + dr**2 <= (radius / px)**2 -- the standard discrete
# lattice-point-in-a-circle count (Gauss circle problem), computed here
# via a completely separate per-row sqrt method, not by re-running
# build_disc_kernel_offsets()'s own loop.

square_offsets = build_disc_kernel_offsets((5.0, 5.0), radius_meters=50.0)
R = 10  # radius_meters / px = 50.0 / 5.0
expected_lattice_count = sum(2 * math.isqrt(R * R - dr * dr) + 1 for dr in range(-R, R + 1) if R * R - dr * dr >= 0)
assert len(square_offsets) == expected_lattice_count, (
    f"radius=50.0 at (5.0, 5.0) resolution should match the independent lattice-point count "
    f"{expected_lattice_count}, got {len(square_offsets)}"
)
print(
    f"build_disc_kernel_offsets() at radius=50.0, resolution=(5.0, 5.0): {len(square_offsets)} cells, "
    f"matching the independently-derived lattice-point count {expected_lattice_count}."
)

# --- cell_union_footprint(): a solid NxN block of True cells dissolves ---
# --- into ONE clean polygon, not a fragmented/sliver-gapped shape       ---
#
# Realistic, "ugly" large-magnitude UTM origin/resolution values, same
# convention test_production_area.py's own grid-seam regression uses --
# the sliver-gap bug this function's corner-snapping fix addresses only
# reproduces at realistic DEM coordinate magnitudes, not round test
# numbers like origin_x=0.0.
GRID_SEAM_DEM = {
    "resolution_meters": (0.9144, 0.9144),  # ~3 ft, a real lidar-derived resolution
    "origin_x": 583412.371,
    "origin_y": 4498217.933,
    "crs": "EPSG:32617",
}

size = 40
solid_block_mask = np.zeros((size, size), dtype=bool)
solid_block_mask[:, :] = True

footprint = cell_union_footprint(GRID_SEAM_DEM, solid_block_mask)

assert footprint.geom_type == "Polygon", (
    f"a solid NxN block of True cells must dissolve into a single clean Polygon (no grid-seam sliver "
    f"fragmentation), got {footprint.geom_type}"
)

px, py = GRID_SEAM_DEM["resolution_meters"]
expected_area = size * size * px * py
assert abs(footprint.area - expected_area) < 1e-6, (
    f"the dissolved footprint's area must exactly match the real cell-count area (no slivers double-counted "
    f"or lost), got {footprint.area}, expected {expected_area}"
)
print(
    f"cell_union_footprint() dissolves a solid {size}x{size} block into one clean Polygon with no "
    f"fragmentation, area exactly matching {expected_area} sq m."
)


# --- cell_union_footprint(): two disconnected blocks stay two separate ---
# --- pieces (fragmentation is real ground-truth here, not a bug)       ---

two_blocks_mask = np.zeros((20, 20), dtype=bool)
two_blocks_mask[0:5, 0:5] = True
two_blocks_mask[15:20, 15:20] = True
two_blocks_footprint = cell_union_footprint(GRID_SEAM_DEM, two_blocks_mask)
assert two_blocks_footprint.geom_type == "MultiPolygon", (
    "two genuinely disconnected 5x5 blocks should NOT dissolve into one Polygon"
)
assert len(list(two_blocks_footprint.geoms)) == 2
print("cell_union_footprint() correctly keeps two genuinely disconnected blocks as two separate pieces.")


# --- cell_union_footprint(): an all-False mask returns an empty Polygon ---

empty_mask = np.zeros((10, 10), dtype=bool)
empty_footprint = cell_union_footprint(GRID_SEAM_DEM, empty_mask)
assert empty_footprint.is_empty, "an all-False cell mask should produce an empty footprint, not raise"
print("cell_union_footprint() returns an empty Polygon for an all-False mask, without raising.")


# --- connected_components() / cell_area_acres() smoke checks (already ---
# --- covered indirectly elsewhere, but exercised directly here too)    ---

labels, num_components = connected_components(two_blocks_mask)
assert num_components == 2
print("connected_components() finds 2 components for the two disconnected blocks above.")

acres_per_cell = cell_area_acres(GRID_SEAM_DEM)
assert acres_per_cell > 0
print(f"cell_area_acres() reports a positive per-cell area ({acres_per_cell:.6f} acres).")

# --- attempt_waist_split(): cell-count invariant, normal split -------
# --- (every cell that goes in comes back out across the pieces)      ---
#
# A dumbbell: two 10x10 lobes joined by a 2-row-tall, 4-col-wide neck --
# narrower than the ~12m (2-cell-radius) erosion this test uses, so it
# genuinely pinches into 2 components. Same fixture shape test_water_
# candidate_zones.py's own waist-split test uses, exercised here
# directly against attempt_waist_split() rather than through
# find_candidate_zones()'s full clustering.
WAIST_DEM = {
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
WAIST_GRID_SHAPE = (12, 24)


def _rect_cells(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


_lobe_a = _rect_cells(0, 10, 0, 10)
_lobe_b = _rect_cells(0, 10, 14, 24)
_neck = _rect_cells(4, 6, 10, 14)
_dumbbell_cells = _lobe_a + _lobe_b + _neck

_split_result = attempt_waist_split(_dumbbell_cells, WAIST_GRID_SHAPE, WAIST_DEM, min_area_acres=0.01, min_waist_meters=12.0)
assert len(_split_result) == 2, f"expected the pinched dumbbell to split into 2 pieces, got {len(_split_result)}"
_total_cells_out = sum(len(piece["cells"]) for piece in _split_result)
assert _total_cells_out == len(_dumbbell_cells), (
    f"attempt_waist_split() must preserve every cell across a real split -- {len(_dumbbell_cells)} cells in, "
    f"{_total_cells_out} cells out"
)
print(
    f"attempt_waist_split(): a genuine waist split preserves every cell exactly -- {len(_dumbbell_cells)} cells "
    f"in, {_total_cells_out} cells out across {len(_split_result)} piece(s), no reclaim loss."
)


# --- attempt_waist_split(): cell-count invariant VIOLATION is caught  ---
# --- loudly, with diagnostic counts, rather than silently dropping    ---
# --- cells                                                            ---
#
# Simulates the exact failure mode this invariant guards against: a
# caller passing a `cells` argument that ISN'T actually one single
# 8-connected component (a real bug upstream, e.g. in how a caller builds
# its own cluster_cells from connected_components()'s labeling) --
# here, a tiny, spatially SEPARATE 2x2 island (small enough to fully
# erode away to nothing under this same 2-cell-radius erosion) is
# tacked onto the dumbbell's own cell list. The dumbbell still splits
# into its own 2 real sub-components as before, but the island's 4
# cells have no reachable surviving sub-component at all (they're not
# 8-adjacent to anything in the dumbbell) -- exactly the scenario
# reclaim_stripped_cells() cannot recover from, and attempt_waist_split()
# must refuse to silently drop rather than return fewer cells than it
# was given.
# A taller grid than the dumbbell's own (rows 0-9) so the island can sit
# at rows 11-12 -- a real gap of 2 rows from lobe_a's own last row (row 9),
# which is enough for NO 8-adjacency at all (row 10 is empty; row 9 to
# row 11 differ by 2, beyond D8's 1-cell reach) -- genuinely spatially
# separate, not just a different (row, col) tuple.
BROKEN_GRID_SHAPE = (14, 24)
_stray_island = _rect_cells(11, 13, 0, 2)  # 2x2, 2 full rows away from lobe_a -- not 8-adjacent to anything
assert not any(cell in _dumbbell_cells for cell in _stray_island), "test setup: island must not overlap the dumbbell"
assert not any(
    abs(ir - dr) <= 1 and abs(ic - dc) <= 1 for ir, ic in _stray_island for dr, dc in _dumbbell_cells
), "test setup: island must not be 8-adjacent to the dumbbell either, or it isn't a genuinely separate component"

_broken_cells = _dumbbell_cells + _stray_island
try:
    attempt_waist_split(_broken_cells, BROKEN_GRID_SHAPE, WAIST_DEM, min_area_acres=0.01, min_waist_meters=12.0)
    raise AssertionError(
        "attempt_waist_split() should have raised RuntimeError for a `cells` argument that isn't one single "
        "connected component (the stray island has no reachable surviving sub-component)"
    )
except RuntimeError as e:
    message = str(e)
    assert "4" in message, f"expected the message to report the 4 unreachable island cell(s), got: {message}"
    for cell in _stray_island:
        assert str(cell) in message, f"expected unreachable cell {cell} to be named in the error message: {message}"
    print(
        "attempt_waist_split(): a `cells` argument that isn't one single connected component (simulated via a "
        "spatially-separate, fully-eroded 2x2 island tacked onto the dumbbell) is caught loudly via RuntimeError, "
        f"naming the exact unreachable cell(s), rather than silently returning fewer cells than it was given:\n"
        f"    {message}"
    )


print("\nAll raster_grid checks passed.")
