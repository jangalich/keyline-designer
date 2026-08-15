"""
test_tree_zone_render_footprint.py

Offline (no-network) regression coverage for score_tree_search_space()'s
render_fill_polygon_utm being the patch's real cell-union footprint, UNMODIFIED
(identical to polygon_utm): no hull, no opening, no smoothing of any kind.

This replaces the earlier render-opening tests. The tree layer deliberately
diverges from production_area.py/water_candidate_zones.py, which DO open/hull
their own render geometry: tree candidates are the thin, branching leftover
ground threading between production/water/road, and those narrow arms and
interior pockets are exactly the geometry this layer exists to find, so they are
drawn verbatim.

Hand-verifiable, round-number synthetic geometry on a 5m DEM grid (the reference
property's own resolution). Three behaviors:
  1. a single-cell-wide diagonal chain SURVIVES as a candidate and renders its
     full geometry (the direct inverse of the removed survival-gate test);
  2. a patch with a 1-cell interior pocket retains the hole;
  3. drawn area equals footprint area exactly.
"""

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

from raster_grid import SQUARE_METERS_PER_ACRE
from tree_zone_candidates import MIN_TREE_ZONE_ACRES, score_tree_search_space

CRS = "EPSG:32617"
RES = (5.0, 5.0)  # 5m grid -> the reference property's own resolution
CELL_SQM = RES[0] * RES[1]  # 25 sqm per cell

# The opening this layer used to apply no longer exists; assert the constant is
# gone so this file also guards against it being reintroduced.
import tree_zone_candidates as _tzc
assert not hasattr(_tzc, "TREE_ZONE_RENDER_OPENING_RADIUS_METERS"), (
    "TREE_ZONE_RENDER_OPENING_RADIUS_METERS must not exist -- the tree layer draws its raw footprint, no opening"
)
print(f"MIN_TREE_ZONE_ACRES = {MIN_TREE_ZONE_ACRES} ac = {round(MIN_TREE_ZONE_ACRES * SQUARE_METERS_PER_ACRE, 2)} sqm "
      f"= {round(MIN_TREE_ZONE_ACRES * SQUARE_METERS_PER_ACRE / CELL_SQM, 2)} cells on this grid.")
print("No render opening is applied -- render_fill_polygon_utm is the real cell-union footprint, unmodified.\n")


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


# =====================================================================
# Test 1: a single-cell-wide DIAGONAL CHAIN SURVIVES and renders its full
# geometry -- the direct inverse of the removed opening/survival-gate test.
#
# 20x20 grid, candidate = the 20 corner-touching cells (i, i). 8-connected
# labeling makes them ONE component. The chain is 20 cells = 500 sqm = 0.1236 ac,
# above the 0.1 ac floor, so it clears the size gate; with the opening removed
# there is no survival gate, so it is kept and drawn verbatim.
# =====================================================================

ORIGIN1 = (0.0, 100.0)
dem1 = _make_dem(20, 20, *ORIGIN1)
boundary1 = box(0, 0, 100, 100)
diag_cells = [(i, i) for i in range(20)]
search1 = unary_union([_cell_box(*ORIGIN1, r, c) for (r, c) in diag_cells])
assert abs(search1.area - 20 * CELL_SQM) < 1e-6, f"test setup: 20-cell diagonal must be {20 * CELL_SQM} sqm, got {search1.area}"
assert search1.area > MIN_TREE_ZONE_ACRES * SQUARE_METERS_PER_ACRE, "test setup: the chain must clear the size floor"

patches1 = score_tree_search_space(dem1, search1, boundary1, hydric_union=search1)
assert len(patches1) == 1, (
    f"a single-cell-wide diagonal chain must SURVIVE as a candidate -- the thin branching arms are exactly what "
    f"this layer exists to find -- got {len(patches1)} candidate(s)"
)
p1 = patches1[0]
render1 = p1["render_fill_polygon_utm"]
footprint1 = p1["polygon_utm"]
# full geometry preserved: render fill is the real footprint, unmodified.
assert render1.equals(footprint1), "render_fill_polygon_utm must be geometrically identical to the real footprint"
assert abs(render1.area - 20 * CELL_SQM) < 1e-6, (
    f"the full 20-cell diagonal must be drawn (nothing eroded away), got {render1.area} sqm"
)
# non-vacuous: this really is a thin diagonal -- its convex hull is far larger,
# so an opening (or hull) would have grossly transformed it.
assert footprint1.convex_hull.area > footprint1.area * 1.5, (
    "sanity check: the chain must be genuinely thin/sparse (convex hull well above footprint), so an opening "
    "would have destroyed it -- confirming the raw-footprint behavior is doing real work"
)
print(
    f"Test 1 -- 1-cell-wide diagonal chain: 20 cells = {render1.area} sqm "
    f"({round(render1.area / SQUARE_METERS_PER_ACRE, 4)} ac) SURVIVES and renders its full geometry verbatim "
    f"(convex hull would be {round(footprint1.convex_hull.area)} sqm) -- the inverse of the removed survival gate."
)


# =====================================================================
# Test 2: a 1-cell interior POCKET is RETAINED as a real hole.
#
# 9x9 grid, candidate = a 7x7 block (rows/cols 1..7) MINUS its center cell
# (4, 4): a 48-cell, 1200 sqm frame with a single-cell hole. The raw footprint
# excludes the pocket, and render_fill_polygon_utm (identical to it) keeps the
# hole open -- the zone genuinely wraps around the excluded cell.
# =====================================================================

ORIGIN2 = (0.0, 45.0)
dem2 = _make_dem(9, 9, *ORIGIN2)
boundary2 = box(0, 0, 45, 45)
block7 = box(5, 5, 40, 40)  # rows/cols 1..7 -> 35x35 = 1225 sqm = 49 cells
pocket_cell = _cell_box(*ORIGIN2, 4, 4)  # center cell -> box(20,20,25,25)
pocket_center = pocket_cell.centroid  # (22.5, 22.5)
search2 = block7.difference(pocket_cell)
assert abs(search2.area - 1200.0) < 1e-6, f"test setup: 7x7-minus-center must be 1200 sqm, got {search2.area}"

patches2 = score_tree_search_space(dem2, search2, boundary2, hydric_union=search2)
assert len(patches2) == 1, f"the framed block must be one surviving candidate, got {len(patches2)}"
p2 = patches2[0]
render2 = p2["render_fill_polygon_utm"]
footprint2 = p2["polygon_utm"]

assert footprint2.intersection(pocket_cell).area < 1e-6, "footprint must carry the 1-cell hole"
assert render2.equals(footprint2), "render_fill_polygon_utm must be geometrically identical to the real footprint"
assert not render2.contains(pocket_center), "render_fill_polygon_utm must RETAIN the interior pocket as a real hole"
assert render2.intersection(pocket_cell).area < 1e-6, (
    f"the whole pocket cell must stay open, got {render2.intersection(pocket_cell).area} sqm filled"
)
# non-vacuous: a convex hull (deliberately NOT used) would have closed the pocket.
assert footprint2.convex_hull.contains(pocket_center), (
    "sanity check: a convex hull would have closed this pocket -- the raw footprint is drawn precisely so it doesn't"
)
print(
    f"Test 2 -- 1-cell interior pocket: render_fill ({render2.area} sqm) RETAINS the hole at "
    f"{(pocket_center.x, pocket_center.y)} -- identical to the footprint, whereas a hull would have closed it."
)


# =====================================================================
# Test 3: drawn area equals footprint area EXACTLY (a compact 5x5 block).
#
# 9x9 grid, a solid 5x5 block at rows/cols 1..5 (625 sqm). With no hull, no
# opening and no corner trimming, render_fill_polygon_utm equals the real
# footprint area exactly -- all 25 cells, corners included.
# =====================================================================

ORIGIN3 = (0.0, 45.0)
dem3 = _make_dem(9, 9, *ORIGIN3)
boundary3 = box(0, 0, 45, 45)
# rows 1..5 -> y in [15, 40]; cols 1..5 -> x in [5, 30] (origin_y is at the top).
search3 = box(5, 15, 30, 40)
assert abs(search3.area - 625.0) < 1e-6, "test setup: the 5x5 block must be exactly 625 sqm"

patches3 = score_tree_search_space(dem3, search3, boundary3, hydric_union=search3)
assert len(patches3) == 1, f"the compact 5x5 hydric block must be one candidate, got {len(patches3)}"
p3 = patches3[0]
render3 = p3["render_fill_polygon_utm"]
footprint3 = p3["polygon_utm"]

assert abs(footprint3.area - 625.0) < 1e-6, f"footprint must be the exact 5x5 block (625 sqm), got {footprint3.area}"
assert render3.equals(footprint3), "render_fill_polygon_utm must be geometrically identical to the real footprint"
assert abs(render3.area - footprint3.area) < 1e-6, (
    f"drawn area must equal footprint area EXACTLY (no corners trimmed): footprint {footprint3.area} sqm, "
    f"render {render3.area} sqm"
)
# spot-check: all four corner cells are drawn (an opening would have removed them).
for (r, c) in [(1, 1), (1, 5), (5, 1), (5, 5)]:
    assert render3.contains(_cell_box(*ORIGIN3, r, c).centroid), f"corner cell ({r},{c}) must be drawn, not trimmed"
print(
    f"Test 3 -- compact 5x5 block: drawn area ({render3.area} sqm) equals footprint area ({footprint3.area} sqm) "
    f"exactly, all four corners included -- no opening, no trimming."
)

print("\nAll tree-zone render-footprint checks passed.")
