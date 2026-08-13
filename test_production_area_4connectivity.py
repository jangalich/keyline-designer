"""
test_production_area_4connectivity.py

Offline (no-network) verification for the 4-connected production-cluster
labeling change (branch: 4-connected cluster labeling for production zones).

Every fixture here is a hand-constructed cell mask on a synthetic DEM dict
with round-number origin/resolution, so every acreage is verifiable by hand
and nothing touches the network. The DEM resolution is a flat 5.0m x 5.0m
throughout, so ONE grid cell is exactly:

    5.0 * 5.0 = 25 m^2   (0.0061778 acres, since 1 acre = 4046.8564224 m^2)

and MIN_PRODUCTION_AREA_ACRES = 0.5 ac = 2023.428 m^2 = 80.94 cells -- i.e.
a piece needs >= 81 cells (2025 m^2 = 0.50039 ac) to clear the floor, and
<= 80 cells (2000 m^2 = 0.49421 ac) falls below it.

Covers, in order, the six numbered verification items on the branch:
  1. corner-touch labeling: 8-conn=1 vs 4-conn=2, each 4-conn footprint a Polygon
  2. hole detection: a diagonal edge gap is permeable under the new 4/8 pairing
  3. connected_components() default is unchanged (still 8-connected, byte-identical)
  4. synthetic reproduction of the observed waist-split veto (and its clearing)
  5. survival-gate sensitivity: a just-sub-floor 4-conn piece is dropped
(item 6, running the existing suites, is run separately -- see the branch notes.)
"""

import numpy as np
from collections import deque
from shapely.geometry import box

from raster_grid import (
    D4_OFFSETS,
    D8_OFFSETS,
    SQUARE_METERS_PER_ACRE,
    attempt_waist_split,
    binary_erode,
    cell_union_footprint,
    connected_components,
    reclaim_stripped_cells,
    waist_erosion_radius_cells,
)
from production_area import (
    MIN_PRODUCTION_AREA_ACRES,
    _detect_hole_footprints,
    cluster_and_gate,
)

# Round-number synthetic DEM. 5m cells -> 25 m^2 each. The origin is a
# realistic large-magnitude UTM value (same convention the other test
# modules use) but nothing here depends on its exact value.
RES = (5.0, 5.0)
ORIGIN_X = 500000.0
ORIGIN_Y = 4500000.0
CRS = "EPSG:32617"
CELL_M2 = RES[0] * RES[1]  # 25.0
CELL_ACRES = CELL_M2 / SQUARE_METERS_PER_ACRE  # 0.0061778...

BASE_DEM = {
    "resolution_meters": RES,
    "origin_x": ORIGIN_X,
    "origin_y": ORIGIN_Y,
    "crs": CRS,
}


def _rect(r0, r1, c0, c1):
    """Every (r, c) in the half-open row range [r0, r1) x col range [c0, c1)."""
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask(shape, cells):
    m = np.zeros(shape, dtype=bool)
    for r, c in cells:
        m[r, c] = True
    return m


def _dem_with_array(shape):
    """BASE_DEM plus a flat elevation array of the given shape (cluster_and_gate()
    reads dem['array'] for each cluster's representative elevation)."""
    return {**BASE_DEM, "array": np.full(shape, 300.0, dtype=float)}


def _full_grid_boundary(shape):
    """A UTM box covering the whole grid, so boundary clipping never trims a
    fixture's footprint -- keeps every acreage below hand-verifiable."""
    rows, cols = shape
    minx = ORIGIN_X
    maxx = ORIGIN_X + cols * RES[0]
    maxy = ORIGIN_Y
    miny = ORIGIN_Y - rows * RES[1]
    return box(minx, miny, maxx, maxy)


# ===================================================================== #
# Verification 1 -- corner-touch labeling.                              #
# Two solid 4x4 cell blocks that touch ONLY at a single shared corner:  #
# 8-connectivity reads them as one component, 4-connectivity as two,    #
# and each 4-connected block's real ground footprint is a single        #
# Polygon (a 4x4 solid square). The 8-connected union, by contrast, is  #
# a MultiPolygon -- the two squares meet at a point, not an edge, and    #
# unary_union cannot merge them. This is the exact geometry/labeling     #
# disagreement the branch removes.                                       #
# ===================================================================== #

# block A: rows 0-3, cols 0-3.  block B: rows 4-7, cols 4-7.
# They share only the corner between cell (3, 3) and cell (4, 4).
v1_blockA = _rect(0, 4, 0, 4)
v1_blockB = _rect(4, 8, 4, 8)
v1_mask = _mask((8, 8), v1_blockA + v1_blockB)

v1_labels8, v1_n8 = connected_components(v1_mask, connectivity=8)
v1_labels4, v1_n4 = connected_components(v1_mask, connectivity=4)

assert v1_n8 == 1, f"two corner-touching blocks must be ONE 8-connected component, got {v1_n8}"
assert v1_n4 == 2, f"two corner-touching blocks must be TWO 4-connected components, got {v1_n4}"

# Each 4-connected piece: 4x4 = 16 cells = 16 * 25 = 400 m^2 = 400 / 4046.8564224
# = 0.098843 acres, and a single clean Polygon (a solid square).
for label in range(v1_n4):
    piece_fp = cell_union_footprint(BASE_DEM, v1_labels4 == label)
    assert piece_fp.geom_type == "Polygon", (
        f"each 4-connected block's footprint must be a single Polygon, got {piece_fp.geom_type}"
    )
    assert abs(piece_fp.area - 400.0) < 1e-6, (
        f"each 4x4 block footprint must be exactly 400 m^2 (16 cells x 25 m^2), got {piece_fp.area}"
    )

# The 8-connected view's real footprint really is disconnected: a MultiPolygon
# of two 400 m^2 squares meeting at a point.
v1_union_fp = cell_union_footprint(BASE_DEM, v1_mask)
assert v1_union_fp.geom_type == "MultiPolygon", (
    "the two corner-touching squares' true combined footprint is disconnected -- "
    f"expected MultiPolygon, got {v1_union_fp.geom_type}"
)
assert abs(v1_union_fp.area - 800.0) < 1e-6

print(
    "V1 corner-touch labeling: 8-conn=1 component vs 4-conn=2 components; each 4-conn "
    "footprint is a single 400 m^2 (0.0988 ac) Polygon, while the 8-conn union is a "
    "2-piece MultiPolygon -- the labeling/geometry disagreement is resolved by 4-connectivity."
)


# ===================================================================== #
# Verification 2 -- hole detection under the new 4/8 pairing.           #
# cluster_and_gate() now labels its FOREGROUND 4-connected, so           #
# _detect_hole_footprints() must flood the BACKGROUND 8-connected (the  #
# complementary pairing). A diagonal gap in a cluster's edge must read   #
# as PERMEABLE (background escapes through the diagonal) -- not as a     #
# false enclosed hole. The 4/4 pairing the old code would leave behind   #
# would instead seal that diagonal and manufacture a phantom hole.      #
# ===================================================================== #

# A solid 7x7 block with a staircase of four background cells removed along
# the main diagonal: (0,0), (1,1), (2,2), (3,3). (0,0) is the sub-grid's own
# outer corner (an exit to the outside); (3,3) is the innermost pocket. The
# four background cells form a purely DIAGONAL chain -- each connects to the
# next only corner-to-corner, with cluster cells on both orthogonal sides.
v2_diag = _mask((7, 7), [c for c in _rect(0, 7, 0, 7)
                         if c not in {(0, 0), (1, 1), (2, 2), (3, 3)}])
v2_diag_cells = [(int(r), int(c)) for r, c in np.argwhere(v2_diag)]

v2_holes = _detect_hole_footprints(v2_diag_cells, BASE_DEM)
assert v2_holes == [], (
    "a diagonal edge gap must be PERMEABLE under the new 8-connected background flood "
    f"(no false enclosed hole), got {len(v2_holes)} hole(s)"
)


# To show the pairing is what makes this correct, replicate _detect_hole_footprints()'s
# own border flood locally with a selectable connectivity and count enclosed cells.
# This mirrors the production flood exactly (same seeding, same background test);
# only the neighbor offsets differ.
def _enclosed_cell_count(cluster_cells, connectivity):
    rows = [r for r, _ in cluster_cells]
    cols = [c for _, c in cluster_cells]
    r0, r1 = min(rows), max(rows)
    c0, c1 = min(cols), max(cols)
    h, w = r1 - r0 + 1, c1 - c0 + 1
    cm = np.zeros((h, w), dtype=bool)
    for r, c in cluster_cells:
        cm[r - r0, c - c0] = True
    background = ~cm
    reached = np.zeros((h, w), dtype=bool)
    q = deque()

    def seed(r, c):
        if background[r, c] and not reached[r, c]:
            reached[r, c] = True
            q.append((r, c))

    for r in range(h):
        seed(r, 0)
        seed(r, w - 1)
    for c in range(w):
        seed(0, c)
        seed(h - 1, c)
    offsets = D8_OFFSETS if connectivity == 8 else D4_OFFSETS
    while q:
        r, c = q.popleft()
        for dr, dc in offsets:
            nr, nc = r + dr, c + dc
            if 0 <= nr < h and 0 <= nc < w and background[nr, nc] and not reached[nr, nc]:
                reached[nr, nc] = True
                q.append((nr, nc))
    return int((background & ~reached).sum())


v2_old_4conn = _enclosed_cell_count(v2_diag_cells, connectivity=4)
v2_new_8conn = _enclosed_cell_count(v2_diag_cells, connectivity=8)
assert v2_old_4conn == 3, (
    "the OLD 4-connected background flood would wrongly seal the diagonal chain -- "
    f"expected 3 phantom enclosed cells, got {v2_old_4conn}"
)
assert v2_new_8conn == 0, (
    "the NEW 8-connected background flood must escape through the diagonal chain -- "
    f"expected 0 enclosed cells, got {v2_new_8conn}"
)

# Sanity check the other direction: a genuinely sealed pocket (a solid 5x5 with
# its orthogonally-walled center cell removed) is STILL detected as a real hole.
v2_sealed = _mask((5, 5), [c for c in _rect(0, 5, 0, 5) if c != (2, 2)])
v2_sealed_cells = [(int(r), int(c)) for r, c in np.argwhere(v2_sealed)]
v2_real = _detect_hole_footprints(v2_sealed_cells, BASE_DEM)
assert len(v2_real) == 1 and v2_real[0].geom_type == "Polygon", (
    f"a fully-walled interior cell must still be found as one real hole, got {v2_real}"
)
assert abs(v2_real[0].area - 25.0) < 1e-6, "the sealed single-cell hole must be exactly 25 m^2 (one cell)"

print(
    "V2 hole detection: a diagonal edge gap is permeable under the new 4/8 pairing "
    "(_detect_hole_footprints finds 0 holes; the OLD 4/4 pairing would have sealed it into "
    "3 phantom enclosed cells) -- while a genuinely walled 1-cell pocket (25 m^2) is still "
    "correctly detected as one real hole."
)


# ===================================================================== #
# Verification 3 -- connected_components()'s default is unchanged.      #
# The default must remain 8-connected and byte-for-byte identical to    #
# an explicit connectivity=8 call, so every existing 8-connected caller  #
# (valley_delineation, water_candidate_zones, tree_zone_candidates,     #
# attempt_waist_split, and production_area's own source-region and      #
# hole labeling) is completely unaffected.                              #
# ===================================================================== #

# A staircase along the main diagonal is the sharpest possible probe: it is
# ONE component under 8-connectivity but SIX (every cell isolated) under
# 4-connectivity, so any accidental default flip would change the count.
v3_stair = _mask((6, 6), [(i, i) for i in range(6)])
v3_default_labels, v3_default_n = connected_components(v3_stair)
v3_explicit8_labels, v3_explicit8_n = connected_components(v3_stair, connectivity=8)
v3_4conn_n = connected_components(v3_stair, connectivity=4)[1]

assert v3_default_n == 1, f"an 8-connected diagonal staircase must be ONE component by default, got {v3_default_n}"
assert v3_4conn_n == 6, f"the same staircase must be SIX 4-connected components, got {v3_4conn_n}"
assert v3_default_n == v3_explicit8_n
assert np.array_equal(v3_default_labels, v3_explicit8_labels), (
    "the default labeling must equal explicit connectivity=8 labeling"
)
assert v3_default_labels.tobytes() == v3_explicit8_labels.tobytes(), (
    "the default labeling must be BYTE-IDENTICAL to explicit connectivity=8"
)

# Also confirm the two-disconnected-blocks fixture the existing test_raster_grid.py
# uses still labels identically by default (an existing caller's exact result).
v3_two_blocks = _mask((20, 20), _rect(0, 5, 0, 5) + _rect(15, 20, 15, 20))
assert connected_components(v3_two_blocks)[1] == 2

# An out-of-range connectivity is rejected loudly rather than silently mislabeling.
for bad in (0, 3, 6, 7):
    try:
        connected_components(v3_stair, connectivity=bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"connectivity={bad} should raise ValueError")

print(
    "V3 default connectivity: connected_components() defaults to 8-connected and is byte-identical "
    "to explicit connectivity=8 (diagonal staircase -> 1 component by default / explicit-8, vs 6 under "
    "connectivity=4); invalid connectivity values raise ValueError."
)


# ===================================================================== #
# Verification 4 -- synthetic reproduction of the observed waist-split  #
# veto, and its clearing under 4-connectivity.                          #
#                                                                        #
# Fixture: two 14x14 lobes joined by a genuine EDGE-connected waist two  #
# cells (10 m) wide -- narrower than the 12 m waist this test passes    #
# explicitly to attempt_waist_split(), so a                             #
# radius-2 erosion pinches it apart -- PLUS a stray region that touches  #
# the mass at a single CORNER only.                                      #
#                                                                        #
# NOTE on the stray's size. The branch text describes the stray as "one  #
# isolated single cell". A single corner cell does reproduce the         #
# LABELING half (8-conn folds it in, 4-conn drops it) but NOT the veto:  #
# a lone cell erodes away to nothing and simply reclaims back into a     #
# neighbouring lobe, so the split still commits (asserted explicitly     #
# below). The observed VETO needs a stray that SURVIVES the radius-2     #
# erosion and becomes a third, sub-floor eroded piece -- the smallest    #
# such region is a 5x5 block (its centre cell survives). So the faithful #
# reproduction of the veto uses a 5x5 corner-touch block; the single-    #
# cell case is kept alongside it to document exactly why it cannot veto. #
# ===================================================================== #

V4_SHAPE = (40, 60)
v4_lobeA = _rect(0, 14, 0, 14)     # 196 cells
v4_lobeB = _rect(0, 14, 20, 34)    # 196 cells
v4_waist = _rect(6, 8, 14, 20)     # 2 rows x 6 cols = 12 cells, 10 m wide (< 12 m)
v4_mass = v4_lobeA + v4_lobeB + v4_waist  # 404 cells, one edge-connected blob

# 5x5 stray block whose top-left corner (14,14) touches lobe A's corner (13,13)
# diagonally -- and touches the mass at that single point only.
v4_block = _rect(14, 19, 14, 19)   # 25 cells
v4_cells = v4_mass + v4_block      # 429 cells

# waist width sanity: erosion radius for a 12 m waist on a 5 m grid is 2 cells,
# and a 2-cell (10 m) waist is fully stripped by it.
assert waist_erosion_radius_cells(BASE_DEM, 12.0) == 2

v4_mask = _mask(V4_SHAPE, v4_cells)

# --- BEFORE the change: 8-connected labeling folds the stray block in. ---
v4_n8 = connected_components(v4_mask, connectivity=8)[1]
assert v4_n8 == 1, f"8-connected labeling must yield ONE cluster (stray corner block folded in), got {v4_n8}"

# That single 8-connected cluster, run through the waist split, erodes into
# THREE survivors -- the two lobe cores plus the 5x5 block's surviving centre --
# whose reclaimed footprints are 1.2479 ac, 1.2479 ac and a sub-floor 0.1544 ac
# (25 cells). The 0.1544 ac piece is below the 0.5 ac floor, so the split is VETOED.
v4_radius = waist_erosion_radius_cells(BASE_DEM, 12.0)
v4_eroded = binary_erode(v4_mask, v4_radius)
v4_er_labels, v4_er_n = connected_components(v4_eroded)  # default 8-conn, as attempt_waist_split uses
assert v4_er_n == 3, f"erosion of the 8-connected cluster must leave 3 survivors, got {v4_er_n}"

v4_seeds = {(int(r), int(c)): int(v4_er_labels[r, c]) for r, c in np.argwhere(v4_eroded)}
v4_assign = reclaim_stripped_cells(set(v4_cells), v4_seeds)
v4_groups = {}
for cell, label in v4_assign.items():
    v4_groups.setdefault(label, []).append(cell)
v4_group_acres = sorted(
    round(cell_union_footprint(BASE_DEM, _mask(V4_SHAPE, gc)).area / SQUARE_METERS_PER_ACRE, 4)
    for gc in v4_groups.values()
)
assert v4_group_acres == [0.1544, 1.2479, 1.2479], (
    f"expected a sub-floor 0.1544 ac reclaimed piece plus two 1.2479 ac lobes, got {v4_group_acres}"
)
assert v4_group_acres[0] < MIN_PRODUCTION_AREA_ACRES, "the smallest reclaimed piece must be below the 0.5 ac floor"

v4_split_before = attempt_waist_split(v4_cells, V4_SHAPE, BASE_DEM, MIN_PRODUCTION_AREA_ACRES, 12.0)
assert len(v4_split_before) == 1, (
    f"the sub-floor piece must VETO the split (one piece returned), got {len(v4_split_before)}"
)

# --- AFTER the change: 4-connected labeling drops the corner-touch stray. ---
v4_labels4, v4_n4 = connected_components(v4_mask, connectivity=4)
assert v4_n4 == 2, f"4-connected labeling must separate the mass from the stray block (2 components), got {v4_n4}"

# The stray block on its own is 25 cells = 0.1544 ac -- below the 0.5 ac gate.
v4_block_acres = round(cell_union_footprint(BASE_DEM, _mask(V4_SHAPE, v4_block)).area / SQUARE_METERS_PER_ACRE, 4)
assert v4_block_acres == 0.1544 and v4_block_acres < MIN_PRODUCTION_AREA_ACRES

# The main mass alone erodes into exactly TWO above-floor survivors, so the
# waist split now COMMITS into two 1.2479 ac lobes.
v4_split_after = attempt_waist_split(v4_mass, V4_SHAPE, BASE_DEM, MIN_PRODUCTION_AREA_ACRES, 12.0)
assert len(v4_split_after) == 2, f"with the stray dropped, the waist split must commit into 2 pieces, got {len(v4_split_after)}"
for piece in v4_split_after:
    piece_ac = cell_union_footprint(BASE_DEM, _mask(V4_SHAPE, piece["cells"])).area / SQUARE_METERS_PER_ACRE
    assert piece_ac >= MIN_PRODUCTION_AREA_ACRES, f"each committed piece must clear the floor, got {piece_ac} ac"

# End-to-end through the real production entry point (which now labels 4-connected):
# one 429-cell 8-connected blob becomes two clean single-Polygon patches, stray dropped.
v4_dem = _dem_with_array(V4_SHAPE)
v4_boundary = _full_grid_boundary(V4_SHAPE)
v4_step1 = {"slope_source_labels": connected_components(v4_mask, connectivity=8)[0]}
v4_patches = cluster_and_gate(v4_mask, v4_dem, v4_boundary, v4_step1, min_area_acres=MIN_PRODUCTION_AREA_ACRES)
assert len(v4_patches) == 2, f"cluster_and_gate must now return 2 patches, got {len(v4_patches)}"
assert all(p["polygon_utm"].geom_type == "Polygon" for p in v4_patches), "each patch footprint must be a single Polygon"
assert sorted(p["area_acres"] for p in v4_patches) == [1.25, 1.25]

# Documented contrast: a LONE corner cell (not a block) folds in under 8-conn but
# cannot veto -- it erodes to nothing and reclaims into a lobe, so the split still
# commits. This is why the veto reproduction needs an erosion-surviving stray.
v4_single_cells = v4_mass + [(14, 14)]  # (14,14) corner-touches lobe A's (13,13)
assert connected_components(_mask(V4_SHAPE, v4_single_cells), connectivity=8)[1] == 1
assert connected_components(_mask(V4_SHAPE, v4_single_cells), connectivity=4)[1] == 2
assert len(attempt_waist_split(v4_single_cells, V4_SHAPE, BASE_DEM, MIN_PRODUCTION_AREA_ACRES, 12.0)) == 2, (
    "a single corner cell erodes away and reclaims into a lobe -- it cannot veto the split"
)

print(
    "V4 veto reproduction: an 8-connected 429-cell blob (2.6502 ac) erodes into 3 survivors with a "
    "sub-floor 0.1544 ac reclaimed piece -> split VETOED (1 patch). Under 4-connectivity the corner-touch "
    "5x5 block (0.1544 ac) is dropped, the mass erodes into two 1.2479 ac lobes -> split COMMITS, and "
    "cluster_and_gate() returns 2 single-Polygon patches (1.25 ac each). A lone corner cell, by contrast, "
    "cannot veto -- it erodes away and reclaims into a lobe, so that split still commits."
)


# ===================================================================== #
# Verification 5 -- survival-gate sensitivity (the one real risk).      #
# 4-connectivity can carve a corner-touch cluster into a piece that     #
# falls just below MIN_PRODUCTION_AREA_ACRES, and that ground -- which   #
# survived as part of a larger 8-connected cluster -- is now dropped by  #
# the gate. This test pins the exact acreage lost.                       #
# ===================================================================== #

V5_SHAPE = (30, 30)
v5_blockA = _rect(0, 9, 0, 9)       # 9x9 = 81 cells = 2025 m^2 = 0.50039 ac  (>= floor: survives)
v5_blockB = _rect(9, 17, 9, 19)     # 8x10 = 80 cells = 2000 m^2 = 0.49421 ac (<  floor: dropped)
# They touch only at the corner between cell (8,8) and cell (9,9).
v5_cells = v5_blockA + v5_blockB
v5_mask = _mask(V5_SHAPE, v5_cells)

assert len(v5_blockA) == 81 and len(v5_blockB) == 80

v5_acresA = round(len(v5_blockA) * CELL_ACRES, 4)  # 0.5004
v5_acresB = round(len(v5_blockB) * CELL_ACRES, 4)  # 0.4942
assert v5_acresA >= MIN_PRODUCTION_AREA_ACRES > v5_acresB, (
    f"block A ({v5_acresA} ac) must clear the floor and block B ({v5_acresB} ac) must fall below it"
)

# 8-connectivity (the old labeling) keeps both blocks as one 161-cell cluster
# (0.9946 ac) -- all the ground survives. 4-connectivity separates them.
assert connected_components(v5_mask, connectivity=8)[1] == 1
assert connected_components(v5_mask, connectivity=4)[1] == 2
v5_union_acres = round(cell_union_footprint(BASE_DEM, v5_mask).area / SQUARE_METERS_PER_ACRE, 4)
assert v5_union_acres == 0.9946, f"the combined 8-connected footprint should be 0.9946 ac, got {v5_union_acres}"

# Under the new 4-connected labeling, cluster_and_gate() keeps only block A and
# drops block B, whose 0.4942 ac is below the 0.5 ac floor.
v5_dem = _dem_with_array(V5_SHAPE)
v5_boundary = _full_grid_boundary(V5_SHAPE)
v5_step1 = {"slope_source_labels": connected_components(v5_mask, connectivity=8)[0]}
v5_patches = cluster_and_gate(v5_mask, v5_dem, v5_boundary, v5_step1, min_area_acres=MIN_PRODUCTION_AREA_ACRES)
assert len(v5_patches) == 1, f"only the >= 0.5 ac piece should survive the gate, got {len(v5_patches)} patch(es)"
assert len(v5_patches[0]["cells"]) == 81, "the surviving patch must be the 81-cell block"
assert v5_patches[0]["area_acres"] == 0.5

acreage_lost = v5_acresB  # 0.4942 ac, the block-B ground the gate discards
print(
    "V5 survival-gate sensitivity: a corner-touch cluster that was one 0.9946 ac 8-connected patch "
    "splits under 4-connectivity into a surviving 0.5004 ac piece (81 cells) and a sub-floor 0.4942 ac "
    f"piece (80 cells) that the 0.5 ac gate DROPS. Acreage lost to the gate on this fixture: {acreage_lost} ac."
)


print("\nAll production_area 4-connectivity checks passed.")
