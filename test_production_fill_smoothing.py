"""
test_production_fill_smoothing.py

Offline (no-network) verification for the DISPLAY-TIME smoothing of the
production zone fill used as the contour clip mask -- render_layout_map.py's
own call to raster_grid.angular_smooth_polygon(), which is where the four ring
smoothers and this polygon-level wrapper now live (they were private to the
renderer until exclusion_zones.py needed them too). This is still a Layer-3
display transform at the CALL SITE: it
softens the 5m cell-union staircase of render_fill_polygon_utm into a clean
curve so contour lines terminate along a field edge rather than a frayed comb.
The STORED render_fill_polygon_utm the four consumer modules read is never
touched.

test_render_layout_map.py itself cannot run here because contextily is not
installed (a pre-existing, unrelated environment gap). render_layout_map.py
imports contextily at module load but uses it only inside the plotting call, so
these tests stub it if absent and exercise the smoothing helper directly. All
fixtures are synthetic; nothing touches the network.
"""

import sys
import types

# Make the import portable whether or not contextily is installed: the smoothing
# helper does not use it, so a stub is sufficient to import render_layout_map.
if "contextily" not in sys.modules:
    try:
        import contextily  # noqa: F401
    except ImportError:
        sys.modules["contextily"] = types.ModuleType("contextily")

import numpy as np
from shapely.geometry import MultiPolygon, Polygon, box
from unittest.mock import patch as mock_patch

from production_area import cluster_and_gate, compute_step1_eligible_cells
from production_suitability import score_production_areas
import raster_grid
from raster_grid import angular_smooth_polygon
from render_layout_map import (
    PRODUCTION_FILL_CHAIKIN_ITERATIONS,
    PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS,
)

_CELL = 5.0
_TOL = PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS * _CELL  # one cell at 5m resolution
_ITERS = PRODUCTION_FILL_CHAIKIN_ITERATIONS
_OX, _OY = 500000.0, 4500000.0


def _rect(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _gate(cells, shape):
    """Build a real scored production patch (its render_fill_polygon_utm is a
    genuine bounded-opening cell-union boundary -- the geometry this smoother
    actually operates on)."""
    m = np.zeros(shape, dtype=bool)
    for r, c in cells:
        m[r, c] = True
    dem = {"array": np.full(shape, 300.0), "resolution_meters": (_CELL, _CELL),
           "origin_x": _OX, "origin_y": _OY, "crs": "EPSG:32617"}
    boundary = box(_OX, _OY - shape[0] * _CELL, _OX + shape[1] * _CELL, _OY)
    step1 = compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=None)
    return score_production_areas(cluster_and_gate(m, dem, boundary, step1), dem, step1)


def _exterior_vertices(geom):
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords)
    return sum(len(p.exterior.coords) for p in geom.geoms)


# ===================================================================== #
# Test 1 -- a 5m cell staircase collapses to a curve.                   #
# A 45-degree diagonal boundary built from unit (5m) cell steps: the    #
# angular-simplify pass collapses the staircase's collinear runs down    #
# to the shape's real turns, so the smoothed ring has far fewer vertices.#
# ===================================================================== #

_steps = []
_x = _y = 0.0
for _ in range(20):
    _steps.append((_x, _y)); _x += _CELL
    _steps.append((_x, _y)); _y += _CELL
_staircase = Polygon(_steps + [(_x, 100.0), (0.0, 100.0), (0.0, 0.0)])
_before = _exterior_vertices(_staircase)
_smoothed_stair = angular_smooth_polygon(_staircase, _TOL, _ITERS)
_after = _exterior_vertices(_smoothed_stair)
assert _smoothed_stair.is_valid and _smoothed_stair.geom_type == "Polygon"
assert _after < _before // 2, (
    f"a 45-degree 5m staircase must collapse to far fewer vertices, got {_before} -> {_after}"
)
print(f"T1 staircase -> curve: a 45-degree 5m cell staircase drops from {_before} exterior vertices to "
      f"{_after} after simplify+Chaikin (the collinear cell steps collapse to the shape's real turns).")


# ===================================================================== #
# Test 2 -- boundedness after smoothing (with a reflex vertex).         #
# On a real opened fill shaped as an L (a reflex/concave corner, exactly #
# where Chaikin pushes OUTWARD), the smoothed-then-clipped geometry must  #
# be contained within polygon_utm.                                      #
# ===================================================================== #

_L_patch = _gate(list(set(_rect(2, 42, 2, 42)) - set(_rect(2, 22, 22, 42))), (46, 46))[0]
_L_fill = _L_patch["render_fill_polygon_utm"]
_L_poly = _L_patch["polygon_utm"]
_L_smoothed = angular_smooth_polygon(_L_fill, _TOL, _ITERS)
_L_clipped = _L_smoothed.intersection(_L_poly)

# The reflex corner is where Chaikin genuinely pushes past the footprint, so the
# clip is doing real work -- confirm the UN-clipped smooth exceeds polygon_utm.
_pushout = _L_smoothed.difference(_L_poly).area
assert _pushout > 1.0, (
    f"the smoothed fill should push OUTWARD past polygon_utm at the reflex corner (making the clip "
    f"necessary), measured {_pushout:.2f} m^2"
)
# After clipping, nothing may lie outside polygon_utm -- the invariant the opening branch protects.
assert _L_clipped.difference(_L_poly).area < 1e-6, (
    f"the smoothed-then-clipped fill must be contained within polygon_utm, "
    f"{_L_clipped.difference(_L_poly).area} m^2 leaked outside"
)
print(f"T2 boundedness: on an L-shaped opened fill the smooth pushes {_pushout:.1f} m^2 outward at the reflex "
      f"corner, but the clip to polygon_utm removes all of it -- the clipped fill is fully within polygon_utm.")


# ===================================================================== #
# Test 3 -- area change is small on a realistic staircase fill.         #
# ===================================================================== #

_ratio_unclipped = _L_smoothed.area / _L_fill.area
_ratio_clipped = _L_clipped.area / _L_fill.area
assert _L_clipped.area <= _L_poly.area + 1e-6
assert abs(_ratio_clipped - 1.0) < 0.05, (
    f"a light smooth must move the fill area by only a small fraction on realistic (staircase) geometry; "
    f"clipped ratio {_ratio_clipped:.4f} moved more than 5% -- reporting rather than tuning the constants"
)
print(f"T3 area change: smoothed/unsmoothed area ratio is {_ratio_unclipped:.4f} (unclipped) and "
      f"{_ratio_clipped:.4f} (after clip) on a 200m staircase L-fill -- a {abs(_ratio_clipped-1)*100:.2f}% move.")


# ===================================================================== #
# Test 4 -- MultiPolygon with an interior ring survives smoothing.      #
# ===================================================================== #

_part_with_hole = Polygon(
    [(0, 0), (100, 0), (100, 100), (0, 100)],
    [[(30, 30), (70, 30), (70, 70), (30, 70)]],  # a real interior ring (hole)
)
_part_two = box(140, 0, 240, 100)
_mp = MultiPolygon([_part_with_hole, _part_two])
_mp_smoothed = angular_smooth_polygon(_mp, _TOL, _ITERS)
assert _mp_smoothed.geom_type == "MultiPolygon", f"a 2-part fill must stay a MultiPolygon, got {_mp_smoothed.geom_type}"
assert _mp_smoothed.is_valid and not _mp_smoothed.is_empty
assert len(list(_mp_smoothed.geoms)) == 2, "both parts must survive smoothing"
_holes = [len(p.interiors) for p in _mp_smoothed.geoms]
assert sum(_holes) == 1, f"the interior ring must survive smoothing on its part, got interiors {_holes}"
print(f"T4 MultiPolygon: a 2-part fill with an interior ring in one part smooths to a valid 2-part "
      f"MultiPolygon with the interior ring preserved (interiors per part: {_holes}).")


# ===================================================================== #
# Test 5 -- invalid-geometry fallback returns the input unchanged.      #
# ===================================================================== #

# A degenerate, effectively zero-width sliver (collinear ring): smoothing
# collapses it to empty/invalid geometry, so the helper must return the input
# object unchanged rather than raising or emitting a broken shape.
_sliver = Polygon([(0.0, 0.0), (200.0, 0.0), (400.0, 0.0)])  # collinear, zero-area
_sliver_result = angular_smooth_polygon(_sliver, _TOL, _ITERS)
assert _sliver_result is _sliver, (
    "when smoothing collapses to invalid/empty geometry, the helper must return the INPUT object unchanged"
)
# And the exception path itself: if the ring smoother raises, still return input.
_good = box(0, 0, 100, 100)
with mock_patch.object(raster_grid, "angular_then_smooth_closed_ring", side_effect=ValueError("boom")):
    _raise_result = angular_smooth_polygon(_good, _TOL, _ITERS)
assert _raise_result is _good, "a raising ring smoother must degrade to the input, not propagate the exception"
print("T5 fallback: a collapsing degenerate sliver and a forced ring-smoother exception both return the input "
      "geometry unchanged -- a bad smooth degrades to the unsmoothed shape, never a failed render.")


# ===================================================================== #
# Test 6 -- the stored render_fill_polygon_utm is not mutated.          #
# ===================================================================== #

_patch = _gate(_rect(2, 22, 2, 22), (30, 30))[0]
_stored_before = _patch["render_fill_polygon_utm"]
_wkt_before = _stored_before.wkt
# Run the smoothing exactly as render_layout_map does (result goes to a local).
_ = angular_smooth_polygon(_stored_before, _TOL, _ITERS).intersection(_patch["polygon_utm"])
assert _patch["render_fill_polygon_utm"] is _stored_before, (
    "smoothing must not replace the patch dict's render_fill_polygon_utm object"
)
assert _patch["render_fill_polygon_utm"].wkt == _wkt_before, (
    "smoothing must not mutate the stored render_fill_polygon_utm geometry"
)
print("T6 stored field untouched: after smoothing, patch['render_fill_polygon_utm'] is the same object with "
      "identical geometry -- the four consumer modules still read the exact stored field.")


print("\nAll production-fill display-smoothing checks passed.")
