"""
test_raster_grid.py

Offline (no-network) checks for raster_grid.py's shared, dependency-free
grid helpers.
"""

import numpy as np

from raster_grid import cell_area_acres, cell_union_footprint, connected_components

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

print("\nAll raster_grid checks passed.")
