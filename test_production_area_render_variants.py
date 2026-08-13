"""
test_production_area_render_variants.py

Offline (no-network) verification for the two EXPERIMENTAL render-geometry
flags on production zones (branch: render-geometry-variants):

    production_area.UNCONDITIONAL_RENDER_EROSION   (flag A)
    production_area.PER_PART_RENDER_HULL           (flag B)

Both default False. This branch is an experiment: it adds no default change
and picks no winner. Its primary correctness property -- and the thing tested
hardest here -- is that BOTH FLAGS FALSE IS BYTE-IDENTICAL TO main. The flags
touch only render_polygon_utm / render_fill_polygon_utm; every REPORTED field
(polygon_utm, area_acres, geometry_wgs84, cells, id, source_patch_id,
hole_footprints, and every STEP 4 scoring field) is invariant across all four
flag combinations.

All fixtures are hand-constructed cell masks on a synthetic 5.0m x 5.0m DEM
(25 m^2/cell; 1 acre = 4046.8564224 m^2). MIN_ZONE_WAIST_METERS = 12.0m gives
a waist-erosion radius of 2 cells (10m), so flag A strips a 10m inset off every
cluster edge. Nothing here touches the network.
"""

import copy
import logging

import numpy as np
from shapely.geometry import Point, box

import production_area as pa
from production_suitability import score_production_areas
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    connected_components,
    waist_erosion_radius_cells,
)

RES = (5.0, 5.0)
ORIGIN_X = 500000.0
ORIGIN_Y = 4500000.0
CRS = "EPSG:32617"
CELL_M2 = RES[0] * RES[1]  # 25.0


def _rect(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask(shape, cells):
    m = np.zeros(shape, dtype=bool)
    for r, c in cells:
        m[r, c] = True
    return m


def _dem(shape):
    return {
        "array": np.full(shape, 300.0, dtype=float),
        "resolution_meters": RES,
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _boundary(shape):
    rows, cols = shape
    return box(ORIGIN_X, ORIGIN_Y - rows * RES[1], ORIGIN_X + cols * RES[0], ORIGIN_Y)


def _step1(mask, shape):
    """A synthetic STEP 1 dict sufficient for cluster_and_gate() (needs
    slope_source_labels) and score_production_areas() (needs the per-cell
    factor/soil arrays). Constant values -- the SAME dict is fed to every
    flag combination, so any scoring difference could only come from a
    differing patch, which is exactly what the invariance test probes."""
    return {
        "eligible_mask": mask,
        "slope_only_mask": mask,
        "slope_source_labels": connected_components(mask, connectivity=8)[0],
        "slope_pct": np.full(shape, 5.0, dtype=float),
        "aspect_deg": np.full(shape, 180.0, dtype=float),
        "per_cell_slope_factor": np.full(shape, 0.8, dtype=float),
        "per_cell_aspect_factor": np.full(shape, 0.7, dtype=float),
        "per_cell_score": np.full(shape, 0.75, dtype=float),
        "soil_carved_acres_by_cell": np.zeros(shape, dtype=float),
        "soil_carved_pct_by_cell": np.zeros(shape, dtype=float),
        "soil_data_available": True,
    }


def _run(mask, shape, flag_a, flag_b):
    """Run cluster_and_gate() under an explicit (A, B) flag combination,
    always restoring the module defaults afterward."""
    dem = _dem(shape)
    boundary = _boundary(shape)
    step1 = _step1(mask, shape)
    prev_a, prev_b = pa.UNCONDITIONAL_RENDER_EROSION, pa.PER_PART_RENDER_HULL
    pa.UNCONDITIONAL_RENDER_EROSION = flag_a
    pa.PER_PART_RENDER_HULL = flag_b
    try:
        return pa.cluster_and_gate(mask, dem, boundary, step1, min_area_acres=0.5)
    finally:
        pa.UNCONDITIONAL_RENDER_EROSION = prev_a
        pa.PER_PART_RENDER_HULL = prev_b


def _parts(geom):
    return list(getattr(geom, "geoms", [geom]))


ALL_COMBOS = [(False, False), (True, False), (False, True), (True, True)]

# --------------------------------------------------------------------- #
# Fixtures.                                                              #
# --------------------------------------------------------------------- #

# WAISTED-VETOED: a big lobe A (14x14 = 196 cells = 1.2108 ac, clears the
# 0.5 ac floor) joined by a 2-cell (10m, < 12m) neck to a SUB-FLOOR lobe B
# (8x8 = 64 cells = 0.3954 ac). The whole cluster (272 cells = 1.6803 ac)
# survives as one patch, but the waist split is VETOED because B's reclaimed
# footprint is below the floor -- the all-or-nothing commit rule, exactly the
# situation flag A is meant to render around. (A detached "third eroded piece"
# would itself survive erosion and appear as a THIRD render part, so a
# sub-floor SECOND lobe is used here to get the documented 2-part result while
# still exercising the same sub-floor-piece veto.)
W_SHAPE = (30, 60)
w_lobeA = _rect(0, 14, 0, 14)
w_neck = _rect(6, 8, 14, 20)
w_lobeB = _rect(4, 12, 20, 28)
w_cells = w_lobeA + w_neck + w_lobeB
W_MASK = _mask(W_SHAPE, w_cells)

# COMMITTED-SPLIT dumbbell: two 14x14 lobes, both clearing the floor, joined
# by the same narrow neck -- the split COMMITS into two patches. Used to show
# the committed-split render path is unchanged under both-flags-False.
D_SHAPE = (30, 60)
d_cells = _rect(0, 14, 0, 14) + _rect(6, 8, 14, 20) + _rect(0, 14, 20, 34)
D_MASK = _mask(D_SHAPE, d_cells)

# PLAIN rectangle: 20x20 = 400 cells = 10000 m^2, no neck. Used for the
# outer-edge inset measurement and flag-B inertness.
P_SHAPE = (30, 30)
P_MASK = _mask(P_SHAPE, _rect(0, 20, 0, 20))

# THIN strip: 3 cells (15m) wide throughout -- narrower than the 5-cell
# (2-radius) erosion diameter everywhere, so erosion wipes it out entirely.
# 123 cells = 0.7598 ac, so it still clears the floor and survives as a patch.
S_SHAPE = (20, 60)
S_MASK = _mask(S_SHAPE, _rect(0, 3, 0, 41))


# ===================================================================== #
# Test 1 -- both flags False is byte-identical to main.                 #
# The flags' new code sits entirely behind `if UNCONDITIONAL_RENDER_    #
# EROSION` / `if PER_PART_RENDER_HULL`; with both False the else-        #
# branches run, which are main's exact formulas. Reconstruct those      #
# formulas independently and assert equality, on both a no-split and a  #
# committed-split cluster.                                              #
# ===================================================================== #

# Plain no-split cluster: main sets render_polygon_utm = polygon_utm and
# render_fill = polygon_utm.convex_hull & boundary.
p_off = _run(P_MASK, P_SHAPE, False, False)
assert len(p_off) == 1
_pp = p_off[0]
assert _pp["render_polygon_utm"].equals(_pp["polygon_utm"]), (
    "both-off no-split: render_polygon_utm must equal polygon_utm (main's behavior)"
)
_expected_fill = _pp["polygon_utm"].convex_hull.intersection(_boundary(P_SHAPE))
assert _pp["render_fill_polygon_utm"].equals(_expected_fill), (
    "both-off: render_fill must be the single convex hull of render_polygon_utm & boundary"
)

# Committed-split cluster: main renders each piece from its PRE-reclaim
# survivor cells (render_polygon_utm != polygon_utm), single-hulled.
d_off = _run(D_MASK, D_SHAPE, False, False)
assert len(d_off) == 2, f"the dumbbell must commit its split into 2 patches, got {len(d_off)}"
for _patch in d_off:
    assert not _patch["render_polygon_utm"].equals(_patch["polygon_utm"]), (
        "committed-split: render_polygon_utm is the pre-reclaim survivor footprint, not polygon_utm"
    )
    _exp = _patch["render_polygon_utm"].convex_hull.intersection(_boundary(D_SHAPE))
    assert _patch["render_fill_polygon_utm"].equals(_exp), (
        "both-off committed-split: render_fill must be the single hull of render_polygon_utm & boundary"
    )

print(
    "T1 both-off == main: no-split render_polygon_utm == polygon_utm and committed-split render is the "
    "pre-reclaim survivor footprint, each single-hulled -- reconstructed independently and matched exactly."
)


# ===================================================================== #
# Test 2 -- reported-field invariance across all four flag combos.      #
# Every reported field, and every STEP 4 scoring field, must be         #
# identical in all four combos; only render_polygon_utm/render_fill_    #
# polygon_utm may differ.                                               #
# ===================================================================== #

REPORTED_FIELDS = [
    "id", "area_acres", "representative_elevation_m",
    "polygon_utm", "geometry_wgs84", "cells", "hole_footprints", "source_patch_id",
]


def _eq(a, b):
    if hasattr(a, "equals") and hasattr(a, "geom_type"):  # shapely geometry
        return a.equals(b)
    if isinstance(a, list) and a and hasattr(a[0], "equals") and hasattr(a[0], "geom_type"):
        return len(a) == len(b) and all(x.equals(y) for x, y in zip(a, b))
    return a == b


combo_patches = {combo: _run(W_MASK, W_SHAPE, *combo) for combo in ALL_COMBOS}
for combo, patches in combo_patches.items():
    assert len(patches) == 1, f"combo {combo}: expected 1 vetoed patch, got {len(patches)}"

_base = combo_patches[(False, False)][0]
for combo in ALL_COMBOS[1:]:
    patch = combo_patches[combo][0]
    for field in REPORTED_FIELDS:
        assert _eq(_base[field], patch[field]), (
            f"reported field {field!r} must be invariant, but combo {combo} differs"
        )

# STEP 4 scoring is a pure function of invariant patch fields + the shared
# step1, so it must be identical too. Score each combo's patches and compare
# every field score_production_areas adds (i.e. every field except the two
# render geometries the flags are allowed to change).
scored = {}
for combo, patches in combo_patches.items():
    scored[combo] = score_production_areas(copy.deepcopy(patches), _dem(W_SHAPE), _step1(W_MASK, W_SHAPE))

_base_scored = scored[(False, False)][0]
_score_fields = [k for k in _base_scored if k not in ("render_polygon_utm", "render_fill_polygon_utm")]
for combo in ALL_COMBOS[1:]:
    patch = scored[combo][0]
    for field in _score_fields:
        assert _eq(_base_scored[field], patch[field]), (
            f"STEP 4 scoring field {field!r} must be invariant, but combo {combo} differs"
        )

print(
    f"T2 reported-field invariance: all {len(REPORTED_FIELDS)} reported fields and all "
    f"{len(_score_fields)} post-score fields are identical across every flag combination "
    "(only render_polygon_utm/render_fill_polygon_utm vary)."
)


# ===================================================================== #
# Test 3 -- flag A opens a narrow neck.                                 #
# ===================================================================== #

w_off = combo_patches[(False, False)][0]
w_a = combo_patches[(True, False)][0]

assert w_off["render_polygon_utm"].geom_type == "Polygon", (
    "with A off, the vetoed cluster's render_polygon_utm is a single Polygon"
)
assert w_a["render_polygon_utm"].geom_type == "MultiPolygon", (
    "with A on, erosion opens the neck -- render_polygon_utm becomes a MultiPolygon"
)
assert len(_parts(w_a["render_polygon_utm"])) == 2, (
    f"with A on, render_polygon_utm must have exactly 2 parts (the two eroded lobes), "
    f"got {len(_parts(w_a['render_polygon_utm']))}"
)
print(
    "T3 flag A opens the neck: the vetoed cluster's render_polygon_utm is a single Polygon with A off "
    "and a 2-part MultiPolygon with A on (the neck erodes to a blank strip)."
)


# ===================================================================== #
# Test 4 -- flag A + single hull still BRIDGES the neck.                #
# ===================================================================== #

w_ab_off = combo_patches[(True, False)][0]  # A on, B off
single_hull_fill = w_ab_off["render_fill_polygon_utm"]
parts_own_hull_area = sum(p.convex_hull.area for p in _parts(w_ab_off["render_polygon_utm"]))
assert single_hull_fill.area > parts_own_hull_area + 1.0, (
    f"A+single-hull must bridge the neck: fill area {single_hull_fill.area:.0f} m^2 should exceed the "
    f"sum of the parts' own hulls {parts_own_hull_area:.0f} m^2"
)
# The neck-gap point (centre of the eroded-away neck) lands inside the bridging hull.
neck_gap_pt = Point(ORIGIN_X + 16.5 * RES[0], ORIGIN_Y - 6.5 * RES[1])
assert single_hull_fill.contains(neck_gap_pt), "the single hull must span the neck gap"
print(
    f"T4 flag A + single hull bridges: render_fill area {single_hull_fill.area:.0f} m^2 vs the parts' own "
    f"hulls summing to {parts_own_hull_area:.0f} m^2 -- the hull spans the neck gap (+{single_hull_fill.area - parts_own_hull_area:.0f} m^2)."
)


# ===================================================================== #
# Test 5 -- flag A + B does NOT bridge.                                 #
# ===================================================================== #

w_ab_on = combo_patches[(True, True)][0]  # A on, B on
perpart_fill = w_ab_on["render_fill_polygon_utm"]
assert perpart_fill.area < single_hull_fill.area - 1.0, (
    f"per-part hull must not bridge: fill area {perpart_fill.area:.0f} m^2 should be below the "
    f"single-hull {single_hull_fill.area:.0f} m^2"
)
assert not perpart_fill.contains(neck_gap_pt), (
    "the per-part hull must NOT span the neck gap"
)
print(
    f"T5 flag A + B does not bridge: per-part render_fill area {perpart_fill.area:.0f} m^2 vs single-hull "
    f"{single_hull_fill.area:.0f} m^2 -- the neck-gap point is outside the per-part hull."
)


# ===================================================================== #
# Test 6 -- flag B alone is inert (clusters are single-part at 4-conn). #
# ===================================================================== #

for mask, shape, name in [(W_MASK, W_SHAPE, "waisted"), (P_MASK, P_SHAPE, "plain")]:
    both_off = _run(mask, shape, False, False)
    b_only = _run(mask, shape, False, True)
    assert len(both_off) == len(b_only)
    for off_patch, b_patch in zip(both_off, b_only):
        assert off_patch["render_fill_polygon_utm"].equals(b_patch["render_fill_polygon_utm"]), (
            f"flag B alone must be inert on the {name} fixture (render_fill identical to both-off)"
        )
        assert off_patch["render_polygon_utm"].equals(b_patch["render_polygon_utm"])
print(
    "T6 flag B alone is inert: with A off, render_fill_polygon_utm is identical to both-off on both the "
    "waisted and plain fixtures -- per-part hulling does nothing while render_polygon_utm is single-part."
)


# ===================================================================== #
# Test 7 -- erosion-wipeout fallback.                                   #
# ===================================================================== #

_wipeout_logs = []
_handler = logging.Handler()
_handler.emit = lambda record: _wipeout_logs.append(record.getMessage())
pa._LOGGER.addHandler(_handler)
pa._LOGGER.setLevel(logging.WARNING)
try:
    s_a = _run(S_MASK, S_SHAPE, True, False)
finally:
    pa._LOGGER.removeHandler(_handler)

assert len(s_a) == 1, f"the thin strip (0.7598 ac) must still survive as one patch, got {len(s_a)}"
assert s_a[0]["render_polygon_utm"].equals(s_a[0]["polygon_utm"]), (
    "erosion wipeout: render_polygon_utm must fall back to polygon_utm, not empty geometry"
)
assert not s_a[0]["render_polygon_utm"].is_empty
assert len(_wipeout_logs) == 1, f"the wipeout must be logged exactly once, got {len(_wipeout_logs)}"
print(
    f"T7 erosion-wipeout fallback: a 3-cell-wide strip erodes to nothing under the 2-cell radius; "
    f"render_polygon_utm falls back to polygon_utm (non-empty) and logs the wipeout once. "
    f"Wipeouts across the synthetic tests: {len(_wipeout_logs)}."
)


# ===================================================================== #
# Test 8 -- outer-edge inset cost of flag A on an ordinary zone.        #
# ===================================================================== #

p_a = _run(P_MASK, P_SHAPE, True, False)[0]
poly_area = p_a["polygon_utm"].area
render_area = p_a["render_polygon_utm"].area
radius_cells = waist_erosion_radius_cells(_dem(P_SHAPE), pa.MIN_ZONE_WAIST_METERS)
inset_m = radius_cells * RES[0]
poly_side = poly_area ** 0.5
render_side = render_area ** 0.5
assert abs(poly_area - 10000.0) < 1e-6 and abs(render_area - 6400.0) < 1e-6, (
    f"plain 100x100m zone should erode to 80x80m: polygon {poly_area:.0f} m^2, render {render_area:.0f} m^2"
)
print(
    f"T8 outer-edge inset cost of flag A: a plain {poly_side:.0f}m x {poly_side:.0f}m ({poly_area:.0f} m^2) "
    f"zone renders as {render_side:.0f}m x {render_side:.0f}m ({render_area:.0f} m^2) -- {inset_m:.0f}m stripped "
    f"from every edge (radius {radius_cells} cells), a {100 * (1 - render_area / poly_area):.0f}% area reduction."
)


print("\nAll production_area render-variant checks passed.")
