"""
test_diagnose_exclusion_footprints.py

Offline (no-network) verification for diagnose_exclusion_footprints.py's
MEASUREMENT LOGIC -- the part that has to be right for the live-run numbers
to be worth reading.

The diagnostic itself needs the network (USGS 3DEP, SSURGO, the road
layer) and prints for a human rather than asserting. This file removes
the trust gap that leaves: every figure the report prints is computed here
against synthetic masks whose hole count, hole sizes, polygon count and
closing behavior are known BY HAND, and asserted against those
hand-computed values.

The fixtures use a 1 m x 1 m DEM so one cell is exactly one square metre
and every acreage is exactly `cells / SQUARE_METERS_PER_ACRE` -- no
rounding to reason about.

Two checks carry most of the weight.

The first is that the acres-gained DISTRIBUTION actually separates the two
cases the diagnostic exists to tell apart --

    many pinholes absorbed  -> many tiny gained regions
    two regions merged      -> ONE large gained region

-- because the gained TOTAL alone cannot distinguish them, which is
precisely why the report prints the distribution.

The second is that the SLOPE and SETBACK layers are disjoint BY
CONSTRUCTION (the setback derivation requires slope_ok; the slope
derivation requires ~slope_ok), and that together with slope_only_mask
they exactly partition the on-parcel ground. That disjointness is why the
setback figure is a lower bound on the real ring rather than a measurement
of it, and an assertion is the only thing that would catch it if either
derivation drifted.

Run: python3 test_diagnose_exclusion_footprints.py
"""

import numpy as np

from diagnose_exclusion_footprints import (
    _empty_layer_note,
    _geojson_feature,
    _layer_properties,
    _over_merge_read,
    closing_radius_cells,
    derive_setback_only_mask,
    derive_slope_fail_mask,
    derive_slope_ok,
    disc_closing,
    layer_overlap_matrix,
    effective_radius_meters,
    footprint_metrics,
    gained_region_distribution,
    on_parcel_mask,
)
from production_area import compute_step1_eligible_cells
from raster_grid import SQUARE_METERS_PER_ACRE, waist_erosion_radius_cells
from shapely.geometry import Polygon

# A 1 m x 1 m grid: one cell == one square metre, so every expected acreage
# below is a hand-computed cell count over SQUARE_METERS_PER_ACRE.
UNIT_DEM = {
    "array": np.zeros((60, 60), dtype=np.float32),
    "resolution_meters": (1.0, 1.0),
    "origin_x": 600000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
ACRE_PER_CELL = 1.0 / SQUARE_METERS_PER_ACRE


def _blank(dem=UNIT_DEM) -> np.ndarray:
    return np.zeros(dem["array"].shape, dtype=bool)


def _close(a, b, tol=1e-9) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# 1. hole count, hole areas, polygon count, acres -- all hand-computed
# ---------------------------------------------------------------------------

# A solid 20x20 block (rows/cols 5..24) with two holes punched in it:
#   * one SINGLE cell at (10, 10)          -> 1 m^2
#   * one 2x2 block at rows 15..16, cols 15..16 -> 4 m^2
# By hand: 400 - 1 - 4 = 395 cells, ONE polygon (the holes are interior, so
# they do not split it), TWO interior rings, of exactly 1 and 4 square metres.
holes_mask = _blank()
holes_mask[5:25, 5:25] = True
holes_mask[10, 10] = False
holes_mask[15:17, 15:17] = False

holes_metrics = footprint_metrics(UNIT_DEM, holes_mask)

assert holes_metrics["cell_count"] == 395, (
    f"hand count: 20*20 minus a 1-cell hole minus a 2x2 hole == 395 cells, "
    f"got {holes_metrics['cell_count']}"
)
assert _close(holes_metrics["acres_from_cells"], 395 * ACRE_PER_CELL), (
    f"395 one-square-metre cells is {395 * ACRE_PER_CELL} ac, "
    f"got {holes_metrics['acres_from_cells']}"
)
assert holes_metrics["polygon_count"] == 1, (
    f"a block with only INTERIOR holes is still one polygon, got {holes_metrics['polygon_count']}"
)
assert holes_metrics["hole_count"] == 2, (
    f"expected exactly the 2 punched holes to be reported as interior rings, "
    f"got {holes_metrics['hole_count']}"
)
assert all(_close(a, b) for a, b in zip(holes_metrics["hole_acres"], [1 * ACRE_PER_CELL, 4 * ACRE_PER_CELL])), (
    f"hole areas should be exactly 1 m^2 and 4 m^2 in acres "
    f"({[1 * ACRE_PER_CELL, 4 * ACRE_PER_CELL]}), got {holes_metrics['hole_acres']}"
)
assert holes_metrics["small_hole_count"] == 2, (
    "both holes are far below the 0.1 ac reporting threshold, so both should be counted small; "
    f"got {holes_metrics['small_hole_count']}"
)
assert _close(holes_metrics["polygon_acres"], 395 * ACRE_PER_CELL), (
    "the polygon's own area must agree with the raster cell count -- if these ever diverge, "
    "cell_union_footprint() is losing or double-counting cells; "
    f"got {holes_metrics['polygon_acres']} vs {395 * ACRE_PER_CELL}"
)
# THE VERTEX COUNTS ARE PER CELL CORNER, NOT PER GEOMETRIC CORNER. A
# cell-square footprint keeps every cell boundary point along a straight
# run -- unary_union() dissolves the shared EDGES but does not drop the
# now-collinear vertices, and this diagnostic deliberately applies no
# simplification. So a 20-cell side is 20 vertices, not 1, and the 20x20
# block's exterior is 4 * 20 == 80. That is precisely why the report
# calls this the TRANSPORT figure: it is what a frontend would actually
# have to ship, not an idealized corner count.
assert holes_metrics["exterior_vertex_count"] == 80, (
    "a 20x20 cell block's exterior is 4 sides * 20 cell-boundary vertices == 80 (shapely's repeated "
    f"closing point is not counted); got {holes_metrics['exterior_vertex_count']}"
)
assert holes_metrics["interior_vertex_count"] == 4 + 8, (
    "the 1-cell hole is 4 vertices and the 2x2 hole is 4 sides * 2 == 8, for 12 total; "
    f"got {holes_metrics['interior_vertex_count']}"
)
assert _close(holes_metrics["largest_polygon_share"], 1.0), (
    f"one polygon means it holds 100% of the layer's area, got {holes_metrics['largest_polygon_share']}"
)

print(
    "footprint_metrics(): a hand-built 20x20 block with a 1-cell hole and a 2x2 hole reports "
    f"exactly {holes_metrics['cell_count']} cells / {holes_metrics['acres_from_cells']:.6f} ac, "
    f"{holes_metrics['polygon_count']} polygon, {holes_metrics['hole_count']} interior rings of "
    f"{[round(a, 8) for a in holes_metrics['hole_acres']]} ac, "
    f"{holes_metrics['exterior_vertex_count']} exterior + {holes_metrics['interior_vertex_count']} "
    "interior vertices -- every one matching the hand-computed value."
)


# ---------------------------------------------------------------------------
# 2. polygon count and fragmentation across separated regions
# ---------------------------------------------------------------------------

# Two well-separated blocks (no shared edge, no shared corner): a 10x10 and
# a 5x5. By hand: 2 polygons, 125 cells, largest share == 100/125 == 0.8.
frag_mask = _blank()
frag_mask[5:15, 5:15] = True
frag_mask[30:35, 30:35] = True

frag_metrics = footprint_metrics(UNIT_DEM, frag_mask)

assert frag_metrics["polygon_count"] == 2, (
    f"two spatially separate blocks are two polygons, got {frag_metrics['polygon_count']}"
)
assert frag_metrics["cell_count"] == 125, f"10*10 + 5*5 == 125, got {frag_metrics['cell_count']}"
assert frag_metrics["hole_count"] == 0, f"neither block has a hole, got {frag_metrics['hole_count']}"
assert _close(frag_metrics["largest_polygon_share"], 100 / 125), (
    f"the 10x10 block is 100 of 125 cells == 80% of the layer, "
    f"got {frag_metrics['largest_polygon_share']}"
)
assert _close(frag_metrics["largest_polygon_acres"], 100 * ACRE_PER_CELL)

print(
    "footprint_metrics(): two separated blocks (10x10 and 5x5) report 2 polygons and a largest-polygon "
    f"share of {frag_metrics['largest_polygon_share'] * 100:.1f}% -- the fragmentation measure tracks the "
    "hand-computed 100/125 exactly."
)


# ---------------------------------------------------------------------------
# 3. metres -> cell radius, and why it is NOT the waist conversion
# ---------------------------------------------------------------------------

FIVE_M_DEM = dict(UNIT_DEM, resolution_meters=(5.0, 5.0))

assert closing_radius_cells(FIVE_M_DEM, 0.0) == 0
assert closing_radius_cells(FIVE_M_DEM, 5.0) == 1
assert closing_radius_cells(FIVE_M_DEM, 10.0) == 2
assert closing_radius_cells(FIVE_M_DEM, 2.0) == 0, (
    "2 m at a 5 m resolution rounds to no cells at all -- reported honestly as a no-op rather than "
    "silently inflated to a full cell"
)
assert _close(effective_radius_meters(FIVE_M_DEM, 2), 10.0)

assert waist_erosion_radius_cells(FIVE_M_DEM, 10.0) == 1, (
    "sanity-check of the function this deliberately does NOT reuse: it converts a waist WIDTH and "
    "therefore halves it"
)
assert closing_radius_cells(FIVE_M_DEM, 10.0) == 2 * waist_erosion_radius_cells(FIVE_M_DEM, 10.0), (
    "a closing radius is already a radius, so it must be twice what the waist-width converter gives -- "
    "reusing waist_erosion_radius_cells() here would silently apply half the requested closing"
)

print(
    "closing_radius_cells(): 5 m -> 1 cell and 10 m -> 2 cells at the pipeline's 5 m resolution, twice "
    "what waist_erosion_radius_cells() returns for the same metres -- confirming the diagnostic applies "
    "the full requested radius rather than the halved waist-width conversion."
)


# ---------------------------------------------------------------------------
# 4. closing is EXTENSIVE, including at the grid edge (the padding fix)
# ---------------------------------------------------------------------------

edge_mask = _blank()
edge_mask[0:6, 0:6] = True  # flush against row 0 and col 0
edge_closed = disc_closing(edge_mask, 2)

assert bool((edge_mask & ~edge_closed).sum() == 0), (
    "closing an exclusion must be EXTENSIVE (closed >= raw) -- without the pre-pad, raster_grid._shift()'s "
    "out-of-bounds-is-background convention would let the erosion half eat into a region touching the "
    "grid edge, and the closing would SHRINK the exclusion instead of growing it"
)
assert bool((edge_closed & ~edge_mask).sum() == 0), (
    "a solid convex block is already closed, so a closing must return it unchanged -- any growth here "
    "would mean the pad is leaking material in from outside the grid"
)

print(
    "disc_closing(): a block flush against the grid edge survives a 2-cell closing unchanged -- extensive "
    "(closed >= raw) and not inflated, confirming the pre-pad/crop handles _shift()'s out-of-bounds "
    "background convention correctly."
)


# ---------------------------------------------------------------------------
# 5. VERIFICATION 3, CASE A: many pinholes -> many SMALL gained regions
# ---------------------------------------------------------------------------

# A solid 30x30 block (rows/cols 5..34) with NINE single-cell holes, each
# well separated from the others so each is its own connected region.
#
# By hand, at a 1-cell disc closing: the dilation fills every single-cell
# hole (each hole cell has mask on all four of its orthogonal neighbours,
# which is exactly the 1-cell disc element), and the erosion gives the
# rectangle back unchanged (a closing returns a convex set as-is). So the
# gain is EXACTLY the 9 hole cells: 9 regions, 1 cell each.
PINHOLE_CELLS = [(10, 10), (10, 20), (10, 30), (20, 10), (20, 20), (20, 30), (30, 10), (30, 20), (30, 30)]

pinhole_mask = _blank()
pinhole_mask[5:35, 5:35] = True
for r, c in PINHOLE_CELLS:
    pinhole_mask[r, c] = False

pinhole_raw = footprint_metrics(UNIT_DEM, pinhole_mask)
assert pinhole_raw["hole_count"] == 9, f"expected the 9 punched pinholes, got {pinhole_raw['hole_count']}"
assert pinhole_raw["cell_count"] == 30 * 30 - 9

pinhole_closed = disc_closing(pinhole_mask, 1)
pinhole_gain = gained_region_distribution(UNIT_DEM, pinhole_mask, pinhole_closed)
pinhole_closed_metrics = footprint_metrics(UNIT_DEM, pinhole_closed)

assert pinhole_gain["gained_cells"] == 9, (
    f"hand count: the closing should newly exclude exactly the 9 hole cells, got {pinhole_gain['gained_cells']}"
)
assert _close(pinhole_gain["gained_acres"], 9 * ACRE_PER_CELL), (
    f"9 one-square-metre cells is {9 * ACRE_PER_CELL} ac, got {pinhole_gain['gained_acres']}"
)
assert pinhole_gain["region_count"] == 9, (
    f"the 9 pinholes are spatially separate, so they are 9 distinct gained regions, "
    f"got {pinhole_gain['region_count']}"
)
assert _close(pinhole_gain["largest_region_acres"], 1 * ACRE_PER_CELL), (
    f"the LARGEST gained region is a single cell -- this is the pinhole signature; "
    f"got {pinhole_gain['largest_region_acres']}"
)
assert pinhole_gain["buckets"]["0-0.05 ac"] == 9, (
    f"every gained region falls in the smallest size bucket, got {pinhole_gain['buckets']}"
)
assert pinhole_closed_metrics["hole_count"] == 0, (
    f"all 9 holes should be absorbed by the closing, got {pinhole_closed_metrics['hole_count']} left"
)
assert pinhole_closed_metrics["polygon_count"] == 1
assert bool((pinhole_mask & ~pinhole_closed).sum() == 0), "closing must be extensive"


# ---------------------------------------------------------------------------
# 6. VERIFICATION 3, CASE B: two regions merge -> ONE LARGE gained region
# ---------------------------------------------------------------------------

# Two 10x10 blocks on the same rows (5..14), separated by a 2-cell gap:
#   block A: cols 5..14      gap: cols 15..16      block B: cols 17..26
#
# Hand derivation at a 2-cell disc closing (element: every (dr, dc) with
# dr^2 + dc^2 <= 4).
#
# DILATION. Gap col 15 is reached from A by dc = +1 (so |dr| <= 1, rows
# 4..15) and from B by dc = -2 (so dr = 0, rows 5..14); the union is rows
# 4..15. Col 16 is the mirror image: rows 4..15. So after the dilation both
# gap columns are filled for rows 4..15.
#
# EROSION. A gap cell (r, 15) survives only if all 13 disc offsets are in
# the dilated set, and the binding ones are dr = +/-2 at dc = 0: they need
# rows r-2 and r+2 to be within the dilated col-15 span of 4..15, i.e.
# 6 <= r <= 13. Same for col 16.
#
# So the gain is EXACTLY rows 6..13 x cols 15..16 == 8 * 2 == 16 cells, in
# ONE contiguous region -- farmable ground between two separate stands,
# swallowed whole. Note the closing does NOT fill the gap's top and bottom
# rows: a closing bridges a gap, it does not square it off.
merge_mask = _blank()
merge_mask[5:15, 5:15] = True
merge_mask[5:15, 17:27] = True

merge_raw = footprint_metrics(UNIT_DEM, merge_mask)
assert merge_raw["polygon_count"] == 2, f"the two blocks start separate, got {merge_raw['polygon_count']}"
assert merge_raw["hole_count"] == 0, "neither block has a hole -- there is nothing pinhole-like to absorb here"

merge_closed = disc_closing(merge_mask, 2)
merge_gain = gained_region_distribution(UNIT_DEM, merge_mask, merge_closed)
merge_closed_metrics = footprint_metrics(UNIT_DEM, merge_closed)

assert bool((merge_mask & ~merge_closed).sum() == 0), "closing must be extensive"
assert merge_closed_metrics["polygon_count"] == 1, (
    f"the 2-cell closing should merge the two blocks into one polygon, "
    f"got {merge_closed_metrics['polygon_count']}"
)
assert merge_gain["region_count"] == 1, (
    f"the swallowed strip is contiguous, so the gain is ONE region -- this is the over-merge signature; "
    f"got {merge_gain['region_count']}"
)

# The gain must be exactly the hand-derived 8-row x 2-col bridge.
assert merge_gain["gained_cells"] == 16, (
    f"hand derivation above: rows 6..13 x cols 15..16 == 16 cells, got {merge_gain['gained_cells']}"
)
assert _close(merge_gain["gained_acres"], 16 * ACRE_PER_CELL), (
    f"16 one-square-metre cells is {16 * ACRE_PER_CELL} ac, got {merge_gain['gained_acres']}"
)
expected_bridge = np.zeros_like(merge_mask)
expected_bridge[6:14, 15:17] = True
assert bool((merge_gain["gained_mask"] ^ expected_bridge).sum() == 0), (
    "the newly-excluded ground must be exactly the hand-derived bridge (rows 6..13, cols 15..16) -- "
    "not the full 10-row gap, since a closing bridges a gap rather than squaring it off"
)
assert merge_gain["buckets"]["0-0.05 ac"] == 1, (
    "at 1 m cells a 16-cell bridge is still a small absolute acreage -- which is the trap: the BUCKET "
    f"histogram alone does not flag it, only largest-region-vs-total does. got {merge_gain['buckets']}"
)
assert _close(merge_gain["largest_region_acres"], merge_gain["gained_acres"]), (
    "with a single gained region, the largest region IS the whole gain -- 100% concentration, the exact "
    "opposite of the pinhole case"
)

print()
print("VERIFICATION 3 -- the acres-gained distribution separates the two cases:")
print(
    f"  CASE A, 9 pinholes at a 1-cell closing: {pinhole_gain['gained_acres']:.6f} ac gained across "
    f"{pinhole_gain['region_count']} regions; largest {pinhole_gain['largest_region_acres']:.6f} ac "
    f"({pinhole_gain['largest_region_acres'] / pinhole_gain['gained_acres'] * 100:.1f}% of the gain); "
    f"buckets {pinhole_gain['buckets']}"
)
print(
    f"  CASE B, two blocks merging at a 2-cell closing: {merge_gain['gained_acres']:.6f} ac gained across "
    f"{merge_gain['region_count']} region; largest {merge_gain['largest_region_acres']:.6f} ac "
    f"({merge_gain['largest_region_acres'] / merge_gain['gained_acres'] * 100:.1f}% of the gain); "
    f"buckets {merge_gain['buckets']}"
)

assert merge_gain["largest_region_acres"] > 10 * pinhole_gain["largest_region_acres"], (
    "the whole point of reporting the DISTRIBUTION rather than the total: the merge case's largest single "
    "gained region must dwarf the pinhole case's, even though both totals are small. If this ever fails, "
    "the report can no longer tell 'absorbed pinholes' from 'swallowed a farmable strip'."
)
print(
    f"  -> the merge case's largest single gained region is "
    f"{merge_gain['largest_region_acres'] / pinhole_gain['largest_region_acres']:.0f}x the pinhole case's, "
    "so the distribution distinguishes them. The TOTALS alone would not: both are a fraction of an acre, "
    f"and both land in the SAME acreage bucket ({list(merge_gain['buckets'])[0]})."
)

# ...and the one-line reading aid the report prints must actually classify
# the two cases correctly, since that is the line a reader sees first.
pinhole_read = _over_merge_read(pinhole_gain)
merge_read = _over_merge_read(merge_gain)
assert "PINHOLE SHAPE" in pinhole_read, f"9 single-cell gains should read as pinholes, got: {pinhole_read}"
assert "OVER-MERGE SHAPE" in merge_read, f"one 16-cell bridge should read as an over-merge, got: {merge_read}"

# The read must be driven by REGION SIZE IN CELLS, not by share of the
# total: a lone absorbed pinhole is trivially 100% of its own gain, and a
# share-based rule would misread it as an over-merge.
lone_pinhole_mask = _blank()
lone_pinhole_mask[5:15, 5:15] = True
lone_pinhole_mask[10, 10] = False
lone_pinhole_gain = gained_region_distribution(
    UNIT_DEM, lone_pinhole_mask, disc_closing(lone_pinhole_mask, 1)
)
assert lone_pinhole_gain["region_count"] == 1 and lone_pinhole_gain["largest_region_cells"] == 1
lone_read = _over_merge_read(lone_pinhole_gain)
assert "PINHOLE SHAPE" in lone_read, (
    "a SINGLE absorbed pinhole is 100% of its own gain -- a share-based rule would call that an "
    f"over-merge. The read must key off region size in cells. Got: {lone_read}"
)
print(f"  -> reading aid, pinholes:  {pinhole_read}")
print(f"  -> reading aid, merge:     {merge_read}")
print(f"  -> reading aid, 1 pinhole: {lone_read}")
print()


# ---------------------------------------------------------------------------
# 7. the setback derivation, against a REAL compute_step1_eligible_cells() run
# ---------------------------------------------------------------------------

# A flat synthetic DEM (every cell clears any slope gate) and a square
# parcel, run through the REAL STEP 1 with a real setback -- no network, no
# gate inputs, so the ONLY thing that can exclude on-parcel ground here is
# the setback itself. The derived setback-only mask must therefore be
# exactly the on-parcel cells that STEP 1's slope_only_mask dropped.
SETBACK_DEM = {
    "array": np.full((40, 40), 100.0, dtype=np.float32),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 600000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
_x0 = SETBACK_DEM["origin_x"] + 5 * 5.0
_x1 = SETBACK_DEM["origin_x"] + 35 * 5.0
_y1 = SETBACK_DEM["origin_y"] - 5 * 5.0
_y0 = SETBACK_DEM["origin_y"] - 35 * 5.0
SETBACK_PARCEL = Polygon([(_x0, _y0), (_x1, _y0), (_x1, _y1), (_x0, _y1)])

setback_step1 = compute_step1_eligible_cells(
    SETBACK_DEM, SETBACK_PARCEL, max_slope_pct=20.0, boundary_setback_meters=10.0
)
setback_on_parcel = on_parcel_mask(SETBACK_DEM, SETBACK_PARCEL)
setback_only = derive_setback_only_mask(setback_step1, setback_on_parcel, max_slope_pct=20.0)

_slope_pct = setback_step1["slope_pct"]
_slope_ok = (~np.isnan(_slope_pct)) & (_slope_pct <= 20.0)

assert int(setback_only.sum()) > 0, (
    "a 10 m setback on a 5 m grid must exclude a real ring of on-parcel ground; the derivation returned "
    "nothing, which would mean it is not recovering the setback at all"
)
assert bool((setback_only & setback_step1["slope_only_mask"]).sum() == 0), (
    "setback-only ground is by construction OUTSIDE slope_only_mask -- any overlap means the derivation is wrong"
)
assert bool((setback_only & ~setback_on_parcel).sum() == 0), (
    "setback-only ground must all be on-parcel (inside the UNSHRUNK boundary)"
)
expected_ring = setback_on_parcel & _slope_ok & (~setback_step1["slope_only_mask"])
assert bool((setback_only ^ expected_ring).sum() == 0)

# On this flat DEM nothing fails the slope gate inside the parcel, so the
# derived ring is the WHOLE ring, and it accounts for every on-parcel cell
# STEP 1 dropped -- the exact identity the diagnostic relies on.
_on_parcel_slope_ok = setback_on_parcel & _slope_ok
assert bool(((_on_parcel_slope_ok & ~setback_step1["slope_only_mask"]) ^ setback_only).sum() == 0)
assert int(setback_only.sum()) == int(_on_parcel_slope_ok.sum()) - int(
    (_on_parcel_slope_ok & setback_step1["slope_only_mask"]).sum()
), "the ring must account for exactly the on-parcel, slope-clearing cells slope_only_mask excluded"

print(
    "derive_setback_only_mask(): against a REAL compute_step1_eligible_cells() run (flat synthetic DEM, "
    f"10 m setback, no gate inputs), `on_parcel & slope_ok & ~slope_only_mask` recovers "
    f"{int(setback_only.sum())} ring cells = "
    f"{int(setback_only.sum()) * 5.0 * 5.0 / SQUARE_METERS_PER_ACRE:.4f} ac, disjoint from slope_only_mask "
    "and fully on-parcel -- confirming the setback footprint is derivable from what STEP 1 APPLIED, with "
    "no separate mask and no recomputed negative buffer."
)

# And the reason the report labels that ring UNEVALUATED rather than clean:
# STEP 1 evaluates the three gates only inside slope_only_mask, so no ring
# cell can ever appear in any per-gate hit mask.
for _gate_name in ("tree_root_zone_hit", "hydric_hit", "road_hit"):
    assert bool((setback_step1[_gate_name] & ~setback_step1["slope_only_mask"]).sum() == 0), (
        f"{_gate_name} should never contain a cell outside slope_only_mask -- this is the structural fact "
        "that makes the setback ring UNEVALUATED ground rather than clean ground"
    )
print(
    "  ...and no per-gate hit mask (canopy/hydric/road) ever contains a cell outside slope_only_mask, which "
    "is exactly why the ring is reported as UNEVALUATED rather than as 'setback only'."
)


# ---------------------------------------------------------------------------
# 8. the SLOPE layer: derived from what STEP 1 applied, and disjoint from
#    the setback layer by construction
# ---------------------------------------------------------------------------

# A ROLLING synthetic DEM -- a sinusoidal ripple steep enough that a real
# fraction of the parcel fails a 20% slope gate in scattered bands. The
# flat DEM used by check 7 above cannot exercise any of this: with nothing
# failing slope, the slope layer is empty and every overlap is trivially
# zero.
_rows = _cols = 40
_res = 5.0
_yy, _xx = np.mgrid[0:_rows, 0:_cols]
ROLLING_DEM = {
    "array": (100.0 + 5.0 * np.sin(_yy / 5.0) * np.cos(_xx / 4.0)).astype(np.float32),
    "resolution_meters": (_res, _res),
    "origin_x": 600000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
_rx0 = ROLLING_DEM["origin_x"] + 5 * _res
_rx1 = ROLLING_DEM["origin_x"] + 35 * _res
_ry1 = ROLLING_DEM["origin_y"] - 5 * _res
_ry0 = ROLLING_DEM["origin_y"] - 35 * _res
ROLLING_PARCEL = Polygon([(_rx0, _ry0), (_rx1, _ry0), (_rx1, _ry1), (_rx0, _ry1)])

rolling_step1 = compute_step1_eligible_cells(
    ROLLING_DEM, ROLLING_PARCEL, max_slope_pct=20.0, boundary_setback_meters=10.0
)
rolling_on_parcel = on_parcel_mask(ROLLING_DEM, ROLLING_PARCEL)
rolling_slope_fail = derive_slope_fail_mask(rolling_step1, rolling_on_parcel, max_slope_pct=20.0)
rolling_setback = derive_setback_only_mask(rolling_step1, rolling_on_parcel, max_slope_pct=20.0)
rolling_slope_ok = derive_slope_ok(rolling_step1, max_slope_pct=20.0)

assert int(rolling_slope_fail.sum()) > 0, (
    "the rolling fixture must actually produce slope-failing ground, or none of the checks below "
    "mean anything"
)
assert bool((rolling_slope_fail & ~rolling_on_parcel).sum() == 0), (
    "the slope layer must be confined to on-parcel ground"
)
assert bool((rolling_slope_fail & rolling_step1["slope_only_mask"]).sum() == 0), (
    "slope-failing ground can never be in slope_only_mask -- that mask REQUIRES slope_ok"
)

# THE OVERLAP CLAIM, TESTED. The slope and setback layers are disjoint by
# construction: setback requires slope_ok, slope requires ~slope_ok. This
# is the assertion that would catch it if either derivation ever changed
# such that they started sharing ground.
assert bool((rolling_slope_fail & rolling_setback).sum() == 0), (
    "slope and setback must be DISJOINT: derive_setback_only_mask() requires slope_ok and "
    "derive_slope_fail_mask() requires ~slope_ok, so ring ground that also fails slope is "
    "attributed wholly to slope -- which is exactly why the setback figure is a LOWER BOUND "
    "on the real ring"
)

# ...and the three groups exactly PARTITION the on-parcel ground: every
# on-parcel cell is either slope-failing, in the setback ring, or inside
# slope_only_mask. Nothing is double-counted and nothing is missed.
_partition = rolling_slope_fail | rolling_setback | (rolling_step1["slope_only_mask"] & rolling_on_parcel)
assert bool((_partition ^ rolling_on_parcel).sum() == 0), (
    "slope-fail, setback-ring and slope_only_mask must exactly partition the on-parcel cells"
)

print(
    f"derive_slope_fail_mask(): on a rolling synthetic DEM, the slope layer is "
    f"{int(rolling_slope_fail.sum())} cells vs the setback layer's {int(rolling_setback.sum())}, they "
    "share ZERO cells (disjoint by construction, not by luck), and together with slope_only_mask they "
    "exactly partition the on-parcel ground."
)

# The derivation must follow the max_slope_pct the run was GIVEN, not the
# module constant -- otherwise --max-slope-pct would silently report a
# layer that disagrees with the STEP 1 run beside it.
_relaxed = derive_slope_fail_mask(rolling_step1, rolling_on_parcel, max_slope_pct=60.0)
_strict = derive_slope_fail_mask(rolling_step1, rolling_on_parcel, max_slope_pct=5.0)
assert int(_relaxed.sum()) < int(rolling_slope_fail.sum()) < int(_strict.sum()), (
    "relaxing max_slope_pct must shrink the slope layer and tightening it must grow it; got "
    f"{int(_relaxed.sum())} / {int(rolling_slope_fail.sum())} / {int(_strict.sum())}"
)
print(
    f"  ...and it tracks the max_slope_pct the run was GIVEN, not the module constant: "
    f"{int(_strict.sum())} cells at 5%, {int(rolling_slope_fail.sum())} at 20%, {int(_relaxed.sum())} at 60%."
)


# ---------------------------------------------------------------------------
# 9. the overlap matrix, against layers built to overlap
# ---------------------------------------------------------------------------

# canopy/hydric/road DO overlap each other -- one cell can be both wooded
# and hydric. That is the pair a summed "total excluded" figure would
# double-count, and the matrix has to surface it.
_a = _blank()
_a[5:15, 5:15] = True          # 100 cells
_b = _blank()
_b[10:20, 10:20] = True        # 100 cells, overlapping _a on rows/cols 10..14
_c = _blank()
_c[30:35, 30:35] = True        # 25 cells, disjoint from both

overlaps = layer_overlap_matrix(UNIT_DEM, [("a", _a), ("b", _b), ("c", _c)])
overlap_by_pair = {(x, y): v for x, y, v in overlaps}

assert len(overlaps) == 3, f"three layers make three pairs, got {len(overlaps)}"
assert _close(overlap_by_pair[("a", "b")], 25 * ACRE_PER_CELL), (
    f"a and b share a 5x5 block == 25 cells, got {overlap_by_pair[('a', 'b')]}"
)
assert _close(overlap_by_pair[("a", "c")], 0.0)
assert _close(overlap_by_pair[("b", "c")], 0.0)

# The whole point: the naive sum exceeds the true union by exactly the
# double-counted overlap.
_naive_sum = (100 + 100 + 25) * ACRE_PER_CELL
_union_acres = int((_a | _b | _c).sum()) * ACRE_PER_CELL
assert _close(_naive_sum - _union_acres, 25 * ACRE_PER_CELL), (
    "the gap between the naive sum and the real union must equal the overlap -- this is the "
    f"figure the report prints to make 'do not sum these' checkable; got {_naive_sum - _union_acres}"
)
print(
    f"layer_overlap_matrix(): three layers with one hand-built 25-cell overlap report it exactly, and "
    f"the naive sum ({_naive_sum:.6f} ac) exceeds the true union ({_union_acres:.6f} ac) by precisely "
    "that overlap."
)


# ---------------------------------------------------------------------------
# 10. the three eligible forms differ in the way the report claims
# ---------------------------------------------------------------------------

# Built directly from the rolling fixture: FORM A omits slope entirely,
# FORM B subtracts raw (unclosed) slope, FORM C subtracts the CLOSED slope
# layer along with everything else.
_closed_setback = disc_closing(rolling_setback, 1)
_closed_slope = disc_closing(rolling_slope_fail, 1)

form_a = rolling_on_parcel & ~_closed_setback
form_b = form_a & rolling_slope_ok
form_c = rolling_on_parcel & ~(_closed_setback | _closed_slope)

assert int(form_a.sum()) > int(form_c.sum()), (
    "FORM A does not subtract slope at all, so it must be strictly larger than FORM C, which does -- "
    "that difference IS the steep ground FORM A would hand the user"
)
assert bool((form_c & ~form_a).sum() == 0), "FORM C must be a subset of FORM A"
assert bool((form_c & rolling_slope_fail).sum() == 0), (
    "FORM C must contain no slope-failing ground at all"
)
assert bool((form_b & rolling_slope_fail).sum() == 0), "nor must FORM B"

# B vs C: C subtracts the CLOSED slope layer, so it can only be smaller
# than or equal to B -- and strictly smaller whenever the closing gained
# anything, which is the entire reason FORM C exists.
assert bool((form_c & ~form_b).sum() == 0), (
    "FORM C subtracts a closed (extensive) slope layer where FORM B subtracts the raw one, so C "
    "must be a subset of B"
)
_slope_closing_gain = int((_closed_slope & ~rolling_slope_fail).sum())
assert int(form_b.sum()) - int(form_c.sum()) == int(
    (rolling_on_parcel & _closed_slope & ~rolling_slope_fail & ~_closed_setback).sum()
), "the B-to-C difference must be exactly the on-parcel ground the slope closing newly excluded"

print(
    f"the three eligible forms: FORM A {int(form_a.sum())} cells (slope not subtracted) > "
    f"FORM B {int(form_b.sum())} (raw slope subtracted) >= FORM C {int(form_c.sum())} (closed slope "
    f"subtracted); A - C == {int(form_a.sum()) - int(form_c.sum())} cells of steep ground FORM A "
    f"would hand the user, and the slope closing itself gained {_slope_closing_gain} cells."
)

# The rolling fixture's steep bands are solid, so its slope closing gains
# nothing and B == C there -- which demonstrates the ordering but not the
# REASON FORM C exists. A pinholed slope layer, built directly, shows the
# strict case: the closing absorbs the pinholes, and every absorbed pinhole
# is a hole FORM B would still be carrying.
pinholed_slope = _blank()
pinholed_slope[5:25, 5:25] = True
for _r, _c in [(10, 10), (10, 18), (18, 10), (18, 18)]:
    pinholed_slope[_r, _c] = False
pinholed_parcel = _blank()
pinholed_parcel[0:40, 0:40] = True

pinholed_closed = disc_closing(pinholed_slope, 1)
pinholed_form_b = pinholed_parcel & ~pinholed_slope
pinholed_form_c = pinholed_parcel & ~pinholed_closed

assert int(pinholed_form_b.sum()) - int(pinholed_form_c.sum()) == 4, (
    "the four absorbed pinholes are exactly the cells FORM C excludes and FORM B does not; got "
    f"{int(pinholed_form_b.sum()) - int(pinholed_form_c.sum())}"
)
b_metrics = footprint_metrics(UNIT_DEM, pinholed_form_b)
c_metrics = footprint_metrics(UNIT_DEM, pinholed_form_c)

# NOTE THE TOPOLOGY, which is not what "holes" suggests. A pinhole in an
# EXCLUSION layer does not become a hole in the eligible layer -- it
# becomes a stranded one-cell ISLAND of selectable ground, marooned inside
# the excluded region. So the figure it inflates is the POLYGON COUNT, not
# the hole count: FORM B is the surrounding frame plus 4 one-cell specks
# (5 polygons); FORM C is the frame alone (1 polygon). Both keep the single
# real hole where the block sits.
#
# For an interactive frontend that is the worse of the two failure modes:
# a hole is ground the user cannot select, but a one-cell island is ground
# they CAN select and should not be able to -- a speck of "eligible" land
# in the middle of a steep region.
assert b_metrics["polygon_count"] == 5, (
    "FORM B is the frame plus one stranded island per unclosed pinhole; got "
    f"{b_metrics['polygon_count']}"
)
assert c_metrics["polygon_count"] == 1, (
    f"FORM C absorbs the pinholes, leaving the frame alone; got {c_metrics['polygon_count']}"
)
assert b_metrics["hole_count"] == c_metrics["hole_count"] == 1, (
    "both forms keep the one real hole where the excluded block sits -- the pinholes were never "
    "holes in the eligible layer to begin with"
)
assert c_metrics["exterior_vertex_count"] < b_metrics["exterior_vertex_count"], (
    "dropping four stranded islands is a real transport saving, not just a cosmetic one"
)
print(
    f"  ...and with a PINHOLED slope layer the reason FORM C exists is visible, though not in the "
    f"figure the name suggests: an unclosed pinhole becomes a stranded one-cell ISLAND of selectable "
    f"ground, not a hole. FORM B is {b_metrics['polygon_count']} polygons "
    f"({b_metrics['exterior_vertex_count']} exterior vertices) -- a frame plus 4 specks a user could "
    f"select in the middle of steep ground; FORM C is {c_metrics['polygon_count']} "
    f"({c_metrics['exterior_vertex_count']} vertices), at a cost of exactly the 4 absorbed cells."
)


# ---------------------------------------------------------------------------
# 11. GeoJSON: self-describing properties, and empty layers kept not dropped
# ---------------------------------------------------------------------------

empty_metrics = footprint_metrics(UNIT_DEM, _blank())
assert empty_metrics["cell_count"] == 0

# An empty layer must still produce a Feature -- with null geometry -- so a
# reader can tell "this layer is genuinely empty" from "this layer was
# dropped from the export".
empty_feature = _geojson_feature(
    UNIT_DEM,
    empty_metrics["footprint"],
    _layer_properties(
        gate="existing farm roads",
        boundary="fixture",
        radius_m=5.0,
        radius_cells=1,
        layer_kind="exclusion",
        metrics=empty_metrics,
        note=_empty_layer_note(empty_metrics, available=True),
    ),
)
assert empty_feature is not None, "an empty layer must NOT be silently dropped from the export"
assert empty_feature["geometry"] is None, (
    "an empty layer's feature carries null geometry (valid GeoJSON), not a fabricated shape"
)
assert empty_feature["properties"]["empty"] is True
assert "checked and matched no cell" in empty_feature["properties"]["note"], (
    f"a checked-but-empty layer must say so; got {empty_feature['properties'].get('note')!r}"
)

unchecked_note = _empty_layer_note(empty_metrics, available=False)
assert "UNAVAILABLE" in unchecked_note and "NOT verified clean" in unchecked_note, (
    "an empty layer whose input was never fetched must NOT read as an absence of the hazard; "
    f"got {unchecked_note!r}"
)
assert _empty_layer_note(holes_metrics, available=True) == "", (
    "a non-empty layer needs no note"
)

# Every property a reader needs to identify a shape on geojson.io without
# opening this source.
real_properties = _layer_properties(
    gate="slope (derived)",
    boundary="NEW reference boundary",
    radius_m=10.0,
    radius_cells=2,
    layer_kind="exclusion",
    metrics=holes_metrics,
)
for key in (
    "gate",
    "layer_kind",
    "boundary",
    "closing_radius_m",
    "closing_radius_cells",
    "acres",
    "cell_count",
    "polygon_count",
    "hole_count",
    "exterior_vertex_count",
    "empty",
):
    assert key in real_properties, f"exported features must carry {key!r} to be self-describing"
assert real_properties["closing_radius_m"] == 10.0 and real_properties["closing_radius_cells"] == 2, (
    "the radius must travel in BOTH metres and cells -- they differ, and a reader cannot infer one "
    "from the other without knowing the DEM resolution"
)
assert real_properties["empty"] is False

print(
    "GeoJSON export: empty layers are exported as null-geometry features carrying `empty: true` and a "
    "note distinguishing 'checked, found nothing' from 'never checked' -- not silently dropped; every "
    "feature carries gate, layer_kind, radius in both metres and cells, acres and the polygon/hole/"
    "vertex counts."
)


print("\nAll diagnose_exclusion_footprints checks passed.")
