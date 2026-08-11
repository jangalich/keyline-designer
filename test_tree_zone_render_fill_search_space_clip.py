"""
test_tree_zone_render_fill_search_space_clip.py

Offline (no-network) regression coverage for score_tree_search_space()'s
render_fill_polygon_utm field being clipped against search_space_utm rather
than boundary_polygon_utm. Hand-verifiable, round-number synthetic geometry
throughout -- same standard as this pipeline's other tree_zone_candidates.py
fixture tests.

The bug this guards against: a tree-zone candidate's real footprint wraps
AROUND claimed (production/water/road) ground by construction -- it's
leftover land, shaped by whatever isn't already claimed. A convex hull of a
shape that wraps around a claimed region will, geometrically, swallow that
claimed region back up unless it's clipped against something narrower than
the raw parcel boundary. Clipping against search_space_utm (Step 1's own
"boundary minus buffered claimed union" output) instead of boundary_polygon_utm
fixes this while still preserving the hull's real purpose: canopy-gate and
sub-threshold interior pockets are never subtracted from search_space_utm
itself, so they still get closed over.
"""

import numpy as np
from shapely.geometry import box

from raster_grid import SQUARE_METERS_PER_ACRE
from tree_zone_candidates import MIN_TREE_ZONE_ACRES, score_tree_search_space

CRS = "EPSG:32617"


# =====================================================================
# Test 1: L-shaped search space wrapped around a claimed (production-shaped)
# square -- render_fill_polygon_utm must NOT reclaim the claimed square, even
# though its own convex hull, uncorrected, provably would.
#
# boundary = box(0,0,100,100) (10,000 sqm). claimed_square = box(0,0,50,50)
# (2,500 sqm, bottom-left corner) is NOT part of search_space_utm at all --
# search_space_utm = boundary.difference(claimed_square), an exact L-shape
# (7,500 sqm): a full-width top strip (0,50)-(100,100) plus a right-bottom
# strip (50,0)-(100,50).
#
# A 10m-resolution DEM (10x10 cells, each 100 sqm) exactly tiles both the
# claimed square (5x5=25 cells) and the L-shaped search space (75 cells), so
# every cell's real footprint square lands entirely inside or entirely
# outside search_space_utm -- no partial-cell rounding to account for.
# =====================================================================

RESOLUTION = (10.0, 10.0)
ROWS, COLS = 10, 10
ORIGIN_X, ORIGIN_Y = 0.0, 100.0

flat_array = np.full((ROWS, COLS), 100.0, dtype=np.float32)
dem_l_shape = {
    "array": flat_array, "resolution_meters": RESOLUTION, "origin_x": ORIGIN_X, "origin_y": ORIGIN_Y, "crs": CRS,
}

boundary_l = box(0, 0, 100, 100)
claimed_square = box(0, 0, 50, 50)
search_space_l = boundary_l.difference(claimed_square)

assert abs(search_space_l.area - 7500.0) < 1e-6, "test setup: L-shaped search space must be exactly 7500 sqm by construction"

# hydric_union = the search space itself -- every qualifying cell reads as
# hydric (hydric_overlap_factor=1.0), which alone (0.4 weight * 100 = 40,
# plus soil_marginality's default non-prime 1.0 * 0.2 weight * 100 = 20,
# totaling 60) clears MIN_TREE_SUITABILITY_SCORE (31.0) on every cell inside
# the search space, so the ENTIRE search space becomes one qualifying,
# connected candidate patch.
patches_l = score_tree_search_space(dem_l_shape, search_space_l, boundary_l, hydric_union=search_space_l)

assert len(patches_l) == 1, f"expected exactly 1 candidate patch (the whole L-shaped search space qualifies), got {len(patches_l)}"
patch_l = patches_l[0]

# The real footprint (polygon_utm) is untouched by this fix -- it's the DEM
# cell union, exactly the L-shape, 7500 sqm (10m-resolution cells tile it
# exactly).
assert abs(patch_l["polygon_utm"].area - 7500.0) < 1e-6, (
    f"real footprint (polygon_utm) must still be the exact L-shape (7500 sqm), unaffected by this fix, "
    f"got {patch_l['polygon_utm'].area}"
)
assert abs(patch_l["area_acres"] - round(7500.0 / SQUARE_METERS_PER_ACRE, 2)) < 1e-6

# --- the fix under test: render_fill_polygon_utm must not reclaim the claimed square ---
render_fill_l = patch_l["render_fill_polygon_utm"]
assert render_fill_l.intersection(claimed_square).area < 1e-6, (
    f"render_fill_polygon_utm must NOT overlap the claimed production square at all -- got "
    f"{render_fill_l.intersection(claimed_square).area} sqm of overlap"
)
assert abs(render_fill_l.area - 7500.0) < 1e-6, (
    f"render_fill_polygon_utm, clipped against search_space_utm, must equal the search space itself exactly "
    f"here (the hull of the full L-shape footprint is a strict superset of the L-shape, so clipping it back "
    f"against search_space_utm returns exactly the L-shape) -- got {render_fill_l.area} sqm"
)

# --- prove this is a REAL regression test, not a vacuous one: hand-verify that the
# OLD behavior (hull clipped against the raw boundary instead) provably DOES overlap
# the claimed square, by computing that same old expression directly here. ---
old_behavior_render_fill = patch_l["polygon_utm"].convex_hull.intersection(boundary_l)
# Hand-computed via the shoelace formula on the hull's own vertices
# (0,50),(0,100),(100,100),(100,0),(50,0) -- the L-shape's concave vertex
# (50,50) is interior to this hull, so it drops out: 8750 sqm exactly.
assert abs(old_behavior_render_fill.area - 8750.0) < 1e-6, (
    f"sanity check on the test fixture itself: the OLD (boundary-clipped) hull must be exactly the "
    f"hand-computed 8750 sqm pentagon, got {old_behavior_render_fill.area}"
)
old_overlap_with_claimed = old_behavior_render_fill.intersection(claimed_square).area
# Hand-computed: the hull's cut-corner diagonal from (50,0) to (0,50) leaves the triangle
# (50,0)-(50,50)-(0,50) -- entirely inside claimed_square -- INSIDE the hull. Area = 0.5*50*50 = 1250 sqm.
assert abs(old_overlap_with_claimed - 1250.0) < 1e-6, (
    f"sanity check: the OLD behavior must overlap the claimed square by exactly the hand-computed 1250 sqm "
    f"triangle, got {old_overlap_with_claimed} -- if this fixture doesn't reproduce a real, nonzero old-behavior "
    f"overlap, it isn't actually testing the fix"
)

print(
    f"L-shaped search space around a claimed 50x50 square: render_fill_polygon_utm (clipped against "
    f"search_space_utm, {render_fill_l.area} sqm) has ZERO overlap with the claimed square, versus the OLD "
    f"boundary-clipped behavior's hand-verified {old_overlap_with_claimed} sqm overlap ({old_behavior_render_fill.area} "
    f"sqm hull total) -- confirms the fix, against a fixture that provably would have failed before it."
)


# =====================================================================
# Test 2: a real interior canopy pocket is STILL closed over by the hull
# (this fix must not defeat the hull's actual purpose), even in the SAME
# fixture as a real claimed-ground exclusion right next to it -- proving the
# fix tells the two apart rather than just shrinking the hull uniformly.
#
# boundary = box(0,0,200,100) (20,000 sqm). claimed_square = box(150,0,200,100)
# (a 50x100 strip along the right edge, 5,000 sqm) -> search_space =
# box(0,0,150,100) exactly (15,000 sqm), a clean rectangle. A canopy pocket
# (box(60,40,90,60), 600 sqm) sits well inside the search space, far from the
# claimed edge -- cells there are HARD-excluded from the real footprint via
# tree_root_zone_mask_utm, but search_space_utm itself has no hole there.
# =====================================================================

ROWS2, COLS2 = 10, 20
array2 = np.full((ROWS2, COLS2), 100.0, dtype=np.float32)
dem_pocket = {
    "array": array2, "resolution_meters": RESOLUTION, "origin_x": 0.0, "origin_y": 100.0, "crs": CRS,
}

boundary_rect = box(0, 0, 200, 100)
claimed_strip = box(150, 0, 200, 100)
search_space_rect = boundary_rect.difference(claimed_strip)
assert abs(search_space_rect.area - 15000.0) < 1e-6

canopy_pocket_utm = box(60, 40, 90, 60)  # 30x20 = 600 sqm, entirely inside search_space_rect

# Build tree_root_zone_mask_utm: True for every cell whose center falls in the canopy pocket.
tree_root_zone_mask = np.zeros((ROWS2, COLS2), dtype=bool)
for r in range(ROWS2):
    for c in range(COLS2):
        cell_x = ORIGIN_X + (c + 0.5) * RESOLUTION[0]
        cell_y = ORIGIN_Y - (r + 0.5) * RESOLUTION[1]
        if canopy_pocket_utm.contains(box(cell_x, cell_y, cell_x, cell_y).centroid):
            tree_root_zone_mask[r, c] = True

assert tree_root_zone_mask.sum() == 6, f"canopy pocket (30x20 at 10m resolution) must cover exactly 6 cells, got {tree_root_zone_mask.sum()}"

patches_pocket = score_tree_search_space(
    dem_pocket, search_space_rect, boundary_rect,
    hydric_union=search_space_rect, tree_root_zone_mask_utm=tree_root_zone_mask,
)
assert len(patches_pocket) == 1, f"expected exactly 1 candidate patch (a frame around the canopy hole), got {len(patches_pocket)}"
patch_pocket = patches_pocket[0]

# Real footprint: search space minus the canopy pocket, 15000 - 600 = 14400 sqm.
assert abs(patch_pocket["polygon_utm"].area - 14400.0) < 1e-6, (
    f"real footprint must be the search space minus the canopy pocket (14400 sqm), got {patch_pocket['polygon_utm'].area}"
)

render_fill_pocket = patch_pocket["render_fill_polygon_utm"]

# --- the hull's real purpose survives: the canopy pocket is fully closed over ---
pocket_center = canopy_pocket_utm.centroid
assert render_fill_pocket.contains(pocket_center), (
    "render_fill_polygon_utm must still CLOSE OVER the interior canopy pocket -- it's inside search_space_utm, "
    "so clipping against the search space (rather than the boundary) must not reopen it"
)
assert abs(render_fill_pocket.area - 15000.0) < 1e-6, (
    f"render_fill_polygon_utm here must equal search_space_rect exactly (15000 sqm): the frame-shaped "
    f"footprint's own convex hull is the full outer rectangle, which clipped against search_space_utm "
    f"(also the full outer rectangle) returns the whole rectangle, hole and all -- got {render_fill_pocket.area}"
)

# --- AND the claimed strip right next door stays correctly excluded ---
assert render_fill_pocket.intersection(claimed_strip).area < 1e-6, (
    f"render_fill_polygon_utm must still have ZERO overlap with the claimed production strip, even though "
    f"it's now generously closing over an unrelated interior canopy pocket -- got "
    f"{render_fill_pocket.intersection(claimed_strip).area} sqm of overlap"
)

print(
    f"Interior canopy pocket (600 sqm) right next to a real claimed exclusion (5000 sqm strip): "
    f"render_fill_polygon_utm ({render_fill_pocket.area} sqm) fully closes over the canopy pocket "
    f"(contains its centroid) while still having zero overlap with the claimed strip -- confirms the fix "
    f"distinguishes 'inside the search space' (closed over) from 'genuinely claimed' (excluded), rather than "
    f"just shrinking the hull uniformly."
)

print(f"\nMIN_TREE_ZONE_ACRES = {MIN_TREE_ZONE_ACRES} (both patches above clear it comfortably; not the focus of this file).")
print("\nAll render_fill_polygon_utm search-space-clip checks passed.")
