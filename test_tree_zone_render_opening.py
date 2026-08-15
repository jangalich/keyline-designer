"""
test_tree_zone_render_opening.py

Offline (no-network) regression coverage for score_tree_search_space()'s
render_fill_polygon_utm being a bounded morphological OPENING of each patch's
own cell mask (clipped to its real footprint), and for the erode-to-nothing
SURVIVAL GATE that drops a candidate whose mask fully erodes. Replaces the
old convex-hull render fill (see test_tree_zone_render_fill_search_space_clip.py,
which covered the superseded hull behavior).

Hand-verifiable, round-number synthetic geometry throughout, on a 5m DEM grid
so the opening radius is exactly r = 1 cell -- the same granularity the
reference property has -- matching this pipeline's other tree_zone_candidates.py
fixture tests.

Three behaviors, one per section:
  1. a compact block survives the opening -- the drawn geometry is a non-empty
     subset of the real footprint;
  2. a single-cell-wide diagonal chain fully erodes -- the candidate is DROPPED
     (survival gate), not rendered, even with the size floor disabled;
  3. a patch with a 1-cell interior pocket keeps the pocket OPEN -- the opening
     does not close it (a real behavior change from the old hull, which did).
"""

import numpy as np
from shapely.geometry import Point, box
from shapely.ops import unary_union

from raster_grid import SQUARE_METERS_PER_ACRE, waist_erosion_radius_cells
from tree_zone_candidates import (
    MIN_TREE_ZONE_ACRES,
    TREE_ZONE_RENDER_OPENING_RADIUS_METERS,
    score_tree_search_space,
)

CRS = "EPSG:32617"
RES = (5.0, 5.0)  # 5m grid -> the reference property's own resolution
CELL_SQM = RES[0] * RES[1]  # 25 sqm per cell


def _make_dem(rows, cols, origin_x, origin_y):
    return {
        "array": np.full((rows, cols), 100.0, dtype=np.float32),
        "resolution_meters": RES,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "crs": CRS,
    }


def _cell_box(origin_x, origin_y, r, c):
    """Ground square of grid cell (r, c) -- same corner convention as
    raster_grid.cell_union_footprint()/pixel_center_xy()."""
    x0 = origin_x + c * RES[0]
    x1 = origin_x + (c + 1) * RES[0]
    y1 = origin_y - r * RES[1]
    y0 = origin_y - (r + 1) * RES[1]
    return box(x0, y0, x1, y1)


# On this 5m grid the render opening is exactly r = 1 cell: an opening removes
# features narrower than 2r = 2 cells, so a 1-cell arm/chain is removed while a
# 2-cell-wide block survives. Assert it up front so the fixtures below rest on a
# checked number, not an assumption.
_R = waist_erosion_radius_cells(_make_dem(1, 1, 0.0, 0.0), TREE_ZONE_RENDER_OPENING_RADIUS_METERS)
assert _R == 1, f"test setup: at 5m resolution the {TREE_ZONE_RENDER_OPENING_RADIUS_METERS}m opening must be r=1 cell, got {_R}"
print(f"Opening radius on a 5m grid: r = {_R} cell (features narrower than 2r = 2 cells are removed).")
print(f"MIN_TREE_ZONE_ACRES = {MIN_TREE_ZONE_ACRES} ac = {round(MIN_TREE_ZONE_ACRES * SQUARE_METERS_PER_ACRE, 2)} sqm "
      f"= {round(MIN_TREE_ZONE_ACRES * SQUARE_METERS_PER_ACRE / CELL_SQM, 2)} cells on this grid.\n")


# =====================================================================
# Test 1: a compact 5x5 block SURVIVES the opening.
#
# 9x9 grid, block at rows/cols 1..5 (a 25-cell, 625 sqm = 0.1544 ac square,
# comfortably above the 0.1 ac floor). The disc opening at r=1 erodes the block
# to its 3x3 interior, then dilates back to a plus-shape: the full 5x5 minus its
# four single-cell corners (each a corner protrusion narrower than 2r). So the
# drawn fill is 21 cells = 525 sqm, a non-empty strict subset of the 625 sqm
# footprint.
# =====================================================================

ORIGIN1 = (0.0, 45.0)
dem1 = _make_dem(9, 9, *ORIGIN1)
boundary1 = box(0, 0, 45, 45)
# search space = exactly the 5x5 block at rows 1..5, cols 1..5. origin_y is at
# the TOP, so row r spans y in [45-(r+1)*5, 45-r*5]: rows 1..5 -> y in [15, 40],
# cols 1..5 -> x in [5, 30]. Cell centers of rows/cols 1..5 fall inside; rows 0
# and 6 (centers y=42.5, 12.5) and cols 0 and 6 (centers x=2.5, 32.5) fall out.
search1 = box(5, 15, 30, 40)
assert abs(search1.area - 625.0) < 1e-6, "test setup: the 5x5 block must be exactly 625 sqm"

patches1 = score_tree_search_space(dem1, search1, boundary1, hydric_union=search1)
assert len(patches1) == 1, f"the compact 5x5 hydric block must be exactly one surviving candidate, got {len(patches1)}"
p1 = patches1[0]

# Real footprint is untouched: the exact 5x5 block, 625 sqm.
assert abs(p1["polygon_utm"].area - 625.0) < 1e-6, f"footprint must be the exact 5x5 block (625 sqm), got {p1['polygon_utm'].area}"

render1 = p1["render_fill_polygon_utm"]
# --- non-empty AND a subset of the footprint (the two required invariants) ---
assert not render1.is_empty and render1.area > 0, "the opening of a compact block must be non-empty"
assert render1.difference(p1["polygon_utm"]).area < 1e-6, (
    f"render_fill_polygon_utm must be a subset of the footprint -- got "
    f"{render1.difference(p1['polygon_utm']).area} sqm outside it"
)
# --- hand-verified exact shape: 21 cells (5x5 minus 4 corners) = 525 sqm ---
assert abs(render1.area - 525.0) < 1e-6, (
    f"the disc opening of a solid 5x5 block trims its 4 corner cells, leaving 21 cells = 525 sqm, got {render1.area}"
)
# corners removed, center kept -- spot-check both.
for (r, c) in [(1, 1), (1, 5), (5, 1), (5, 5)]:
    center = _cell_box(*ORIGIN1, r, c).centroid
    assert not render1.contains(center), f"corner cell ({r},{c}) must be trimmed by the opening"
assert render1.contains(_cell_box(*ORIGIN1, 3, 3).centroid), "the block's center cell must survive the opening"

print(
    f"Test 1 -- compact 5x5 block: footprint {p1['polygon_utm'].area} sqm, render_fill (opening) {render1.area} sqm "
    f"(21 cells, 4 corners trimmed), a non-empty subset of the footprint. area_acres={p1['area_acres']}."
)


# =====================================================================
# Test 2: a single-cell-wide DIAGONAL CHAIN fully erodes -> DROPPED.
#
# 20x20 grid, candidate = the 20 corner-touching cells (i, i). 8-connected
# labeling (this module's behavior) makes them ONE component, but no cell has an
# orthogonally-adjacent neighbor in the set, so a disc (plus) erosion at r=1
# strips every cell -> the opening is empty -> the survival gate drops it.
#
# The chain's footprint is 20 cells = 500 sqm = 0.1236 ac, ABOVE the 0.1 ac
# floor, so the size gate would have PASSED it -- proving the drop is the
# survival gate, not the area filter. Re-run with the floor disabled to make
# that unambiguous.
# =====================================================================

ORIGIN2 = (0.0, 100.0)
dem2 = _make_dem(20, 20, *ORIGIN2)
boundary2 = box(0, 0, 100, 100)
diag_cells = [(i, i) for i in range(20)]
search2 = unary_union([_cell_box(*ORIGIN2, r, c) for (r, c) in diag_cells])
diag_area = search2.area
assert abs(diag_area - 20 * CELL_SQM) < 1e-6, f"test setup: 20-cell diagonal must be {20 * CELL_SQM} sqm, got {diag_area}"
assert diag_area > MIN_TREE_ZONE_ACRES * SQUARE_METERS_PER_ACRE, (
    "test setup: the diagonal chain must be ABOVE the size floor, so a drop can only be the survival gate"
)

patches2_default = score_tree_search_space(dem2, search2, boundary2, hydric_union=search2)
assert patches2_default == [], (
    f"a single-cell-wide diagonal chain (above the size floor) must be DROPPED by the survival gate, got "
    f"{len(patches2_default)} candidate(s)"
)
# Disable the size floor: still dropped, so it is unambiguously the survival
# gate (a fully-eroded opening), never the area filter.
patches2_nofloor = score_tree_search_space(dem2, search2, boundary2, hydric_union=search2, min_area_acres=0.0)
assert patches2_nofloor == [], (
    "even with the size floor disabled, the diagonal chain must be dropped -- the opening erodes it to nothing"
)
print(
    f"Test 2 -- 1-cell-wide diagonal chain: 20 cells = {diag_area} sqm "
    f"({round(diag_area / SQUARE_METERS_PER_ACRE, 4)} ac, above the {MIN_TREE_ZONE_ACRES} ac floor) is DROPPED by the "
    f"survival gate, with and without the size floor -- a corner-touching thread is not a plantable block."
)


# =====================================================================
# Test 3: a 1-cell interior POCKET stays OPEN (the opening does not close it).
#
# 9x9 grid, candidate = a 7x7 block (rows/cols 1..7) MINUS its center cell
# (4, 4): a 48-cell, 1200 sqm = 0.2965 ac frame with a single-cell hole. The
# hole's four orthogonal neighbors each touch the hole, so they all erode; none
# survives to dilate back over the hole -> the opening leaves the pocket open.
# The OLD convex hull provably WOULD have closed it -- asserted here so the test
# is a real behavior-change guard, not a vacuous one.
# =====================================================================

ORIGIN3 = (0.0, 45.0)
dem3 = _make_dem(9, 9, *ORIGIN3)
boundary3 = box(0, 0, 45, 45)
block7 = box(5, 5, 40, 40)  # rows/cols 1..7 -> 35x35 = 1225 sqm = 49 cells
pocket_cell = _cell_box(*ORIGIN3, 4, 4)  # center cell -> box(20,20,25,25)
pocket_center = pocket_cell.centroid  # (22.5, 22.5)
search3 = block7.difference(pocket_cell)
assert abs(search3.area - 1200.0) < 1e-6, f"test setup: 7x7-minus-center must be 1200 sqm, got {search3.area}"

patches3 = score_tree_search_space(dem3, search3, boundary3, hydric_union=search3)
assert len(patches3) == 1, f"the framed block must be one surviving candidate, got {len(patches3)}"
p3 = patches3[0]

# footprint already has the hole (the pocket cell was never eligible).
assert p3["polygon_utm"].intersection(pocket_cell).area < 1e-6, "footprint must carry the 1-cell hole"

render3 = p3["render_fill_polygon_utm"]
assert not render3.is_empty and render3.area > 0, "the framed block's opening must be non-empty"
assert render3.difference(p3["polygon_utm"]).area < 1e-6, "render_fill must stay within the footprint"

# --- the behavior change under test: the opening does NOT close the pocket ---
assert not render3.contains(pocket_center), (
    "render_fill_polygon_utm (an opening) must NOT close over the 1-cell interior pocket"
)
assert render3.intersection(pocket_cell).area < 1e-6, (
    f"the opening must leave the whole pocket cell open, got {render3.intersection(pocket_cell).area} sqm filled"
)

# --- non-vacuous: the OLD hull provably WOULD have closed it ---
old_hull = p3["polygon_utm"].convex_hull
assert old_hull.contains(pocket_center), (
    "sanity check: the OLD convex-hull behavior would have closed the pocket (its hull spans the whole outer block) -- "
    "if it didn't, this fixture wouldn't be testing a real change"
)

print(
    f"Test 3 -- 1-cell interior pocket: render_fill (opening, {render3.area} sqm) leaves the pocket at "
    f"{ (pocket_center.x, pocket_center.y) } OPEN, whereas the old convex hull "
    f"({round(old_hull.area, 1)} sqm) would have closed it -- confirms the opening preserves interior pockets."
)

print("\nAll tree-zone render-opening + survival-gate checks passed.")
