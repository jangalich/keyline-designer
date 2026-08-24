"""
test_exclusion_smoothing.py

Offline (no-network) MEASUREMENT harness for the proposed angular-simplify +
Chaikin smoothing pass over exclusion_zones.py's unioned exclusion geometry.

WHY THIS FILE EXISTS AND WHAT IT CONCLUDES
------------------------------------------
The branch that added it set out to make the exclusion union read as a shape
rather than a 5 m cell staircase, by running raster_grid.angular_smooth_
polygon() over the closed union and deriving the eligible layer as the
complement of the smoothed result. The design rested on one premise:

    "Chaikin pushes outward at reflex vertices ... here, outward means MORE
     excluded -- over-exclude by a metre rather than leave ground with trees
     on it selectable. That is the safe direction."

THE PREMISE DOES NOT HOLD, and this file is the measurement that shows it.
The smoothing pass is NOT net-outward on this geometry; it is decisively net
INWARD, and the ground it gives back is ground the five gates excluded.
So the switch was NOT made: identify_exclusion_zones() still publishes the
exact closed union as render_fill_polygon_utm, and eligible_polygon_utm is
still its exact complement. See EXCLUSION SMOOTHING: MEASURED AND REJECTED in
exclusion_zones.py's own module docstring.

The two independent reasons, both measured below:

  1. CHAIKIN ON A CLOSED RING IS NET AREA-REDUCING, ALWAYS. Corner-cutting
     pushes outward at reflex vertices and inward at convex ones -- but any
     simple closed ring has a net turning of exactly +360 degrees, so the
     convex (inward) cuts always outweigh the reflex (outward) ones. There is
     no shape for which a closed-ring Chaikin pass is net-outward. The
     open-polyline intuition the premise came from does not transfer.

  2. THE SIMPLIFY PASS AMPLIFIES EXACTLY THAT TERM. Chaikin's corner cut is
     proportional to the length of the edges meeting at the vertex. Collapsing
     the staircase's collinear runs is what makes the shape readable -- and it
     is also what turns hundreds of 5 m edges into a few long ones, so every
     subsequent corner cut removes a much bigger triangle. The two passes are
     not independent: run alone, neither moves the union by more than 1.4%;
     composed in the mandated order, they move it by nearly 5%.

The decisive number is not the area ratio, it is the INWARD EXCURSION: on a
reference-shaped fixture the smoothed union hands back 0.65 acres of
gate-excluded ground as selectable. Every closing radius in exclusion_zones.py
was measured at cell-level precision and the whole closing operation gains
between +0.117 and +0.370 acres on the reference boundaries. Smoothing would
give back roughly TWICE what all five closings together were tuned to add,
in the one direction the module cannot afford to be wrong in.

FIXTURES are synthetic and deterministic (a fixed-seed spatially-correlated
random field thresholded the way a real slope/canopy gate thresholds its
raster, then closed at one cell). The reference-shaped one is tuned to the
real reference range this work quotes -- 4.7-7.2 acres across 10-18 polygons.
Nothing here touches the network or any real-property number.
"""

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon, box

from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    angular_simplify_closed_ring,
    angular_smooth_polygon,
    cell_union_footprint,
    chaikin_smooth_closed_ring,
    disc_closing,
)

CELL = 5.0  # the pipeline's own DEM resolution

# The settings the branch specified: one DEM cell of simplify tolerance, one
# Chaikin pass. Both are the GENTLEST supportable values -- the brief rules out
# going below one cell ("pointless precision") and one iteration is the floor --
# so the numbers below are a best case, not a tuning that could be walked back.
SIMPLIFY_TOLERANCE_CELLS = 1.0
CHAIKIN_ITERATIONS = 1
TOLERANCE_M = SIMPLIFY_TOLERANCE_CELLS * CELL


def _dem(rows: int, cols: int) -> dict:
    return {
        "array": np.zeros((rows, cols), dtype=np.float32),
        "resolution_meters": (CELL, CELL),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _boundary(dem: dict):
    rows, cols = dem["array"].shape
    return box(
        dem["origin_x"],
        dem["origin_y"] - rows * CELL,
        dem["origin_x"] + cols * CELL,
        dem["origin_y"],
    )


def _mask(shape, cells) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for row, col in cells:
        mask[row, col] = True
    return mask


def _parts(geometry) -> list:
    return [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)


def _exterior_vertices(geometry) -> int:
    return sum(len(part.exterior.coords) for part in _parts(geometry))


def _ring_count(geometry) -> int:
    return sum(1 + len(part.interiors) for part in _parts(geometry))


def _smooth(geometry):
    return angular_smooth_polygon(geometry, TOLERANCE_M, CHAIKIN_ITERATIONS)


def _correlated_field(shape, seed: int, passes: int) -> np.ndarray:
    """A spatially correlated random field -- a cheap stand-in for the smooth,
    autocorrelated rasters the real slope and canopy gates threshold. A plain
    white-noise threshold would shatter into single cells and flatter the
    smoothing pass by handing it a shape no real gate produces."""
    rng = np.random.default_rng(seed)
    field = rng.standard_normal(shape)
    for _ in range(passes):
        field = (
            field
            + np.roll(field, 1, 0)
            + np.roll(field, -1, 0)
            + np.roll(field, 1, 1)
            + np.roll(field, -1, 1)
        ) / 5.0
    return (field - field.mean()) / field.std()


def _reference_shaped_union():
    """A closed exclusion union in the real reference range this work quotes:
    4.7-7.2 acres across 10-18 polygons. Built the way the module builds its
    own -- threshold, disc_closing() at one cell, cell_union_footprint(), clip
    to the boundary -- so the geometry under test is a real cell-union
    staircase, not a hand-drawn polygon."""
    dem = _dem(90, 90)
    boundary = _boundary(dem)
    field = _correlated_field((90, 90), seed=11, passes=26)
    closed = disc_closing(field > 1.0, 1)
    return cell_union_footprint(dem, closed).intersection(boundary), boundary


# ===========================================================================
# 2. SMOOTHING REALLY DOES COLLAPSE A STAIRCASE
# ===========================================================================
#
# The one thing the pass unambiguously delivers. A 45-degree diagonal cell-union
# edge is the worst case for staircasing: every single cell contributes its own
# two-segment step, and none of them is a real turn in the underlying shape.

_stair_dem = _dem(40, 40)
_stair_cells = [(r, c) for r in range(2, 34) for c in range(2, 2 + (r - 1))]
_staircase = cell_union_footprint(_stair_dem, _mask((40, 40), _stair_cells)).intersection(
    _boundary(_stair_dem)
)

_stair_coords = list(_staircase.exterior.coords)
assert all(
    abs(a[0] - b[0]) < 1e-9 or abs(a[1] - b[1]) < 1e-9
    for a, b in zip(_stair_coords, _stair_coords[1:])
), "fixture sanity: a cell-union ring must be entirely axis-aligned segments -- a true staircase"

_stair_before = _exterior_vertices(_staircase)
_stair_smoothed = _smooth(_staircase)
_stair_after = _exterior_vertices(_stair_smoothed)

assert _stair_after < _stair_before, "smoothing must reduce the staircase's vertex count"
assert _stair_after < _stair_before / 4, (
    f"the diagonal's collinear runs must actually COLLAPSE, not just get trimmed -- "
    f"{_stair_before} -> {_stair_after} vertices is not a collapse"
)
# The collapse is what proves the simplify pass did its job: a 45-degree run of N
# steps is 2N axis-aligned segments standing in for ONE real edge.
assert _stair_after < 20, (
    f"a single diagonal edge plus three square sides should reduce to a handful of real turns, "
    f"got {_stair_after}"
)
print(
    f"V2 STAIRCASE COLLAPSE: a 45-degree diagonal cell-union edge goes from {_stair_before} to "
    f"{_stair_after} exterior vertices ({100 * (1 - _stair_after / _stair_before):.1f}% reduction) -- "
    f"the collinear runs are genuinely collapsed to the shape's real turns, not merely trimmed."
)


# ===========================================================================
# 3. THE INWARD EXCURSION -- THE NUMBER THAT DECIDED THE BRANCH
# ===========================================================================
#
# "Inward" means the smoothed exclusion sits INSIDE the closed union: ground the
# five gates excluded that the smoothed layer would publish as selectable. This
# is the direction the design assumed would be negligible.

_convex_dem = _dem(60, 60)
_convex_boundary = _boundary(_convex_dem)
# A deliberate sharp convex corner -- a square block with a triangular spike, the
# shape a canopy or slope gate throws off at the tip of a wooded finger.
_convex_cells = [(r, c) for r in range(20, 46) for c in range(10, 36)]
_convex_cells += [(r, c) for r in range(8, 20) for c in range(20, 20 + (r - 7))]
_convex_union = cell_union_footprint(_convex_dem, _mask((60, 60), _convex_cells)).intersection(
    _convex_boundary
)
_convex_smoothed = _smooth(_convex_union).intersection(_convex_boundary)

_convex_inward = _convex_union.difference(_convex_smoothed).area
_convex_outward = _convex_smoothed.difference(_convex_union).area

# THE ASSERTION THE DESIGN EXPECTED TO PASS, INVERTED INTO THE FINDING IT
# ACTUALLY PRODUCES. Stated as an assert, not a print, so that the day someone
# changes the smoothing helpers in a way that DOES make the pass safe, this
# fails loudly and the rejection gets revisited rather than silently outliving
# its reason.
assert not _convex_smoothed.contains(_convex_union), (
    "FINDING NO LONGER HOLDS: the smoothed exclusion now CONTAINS the closed union on the convex-corner "
    "fixture. The reason this branch stopped was that it did not. Re-measure and revisit the rejection "
    "recorded in exclusion_zones.py's EXCLUSION SMOOTHING: MEASURED AND REJECTED section."
)
assert _convex_inward > 0.0, "the inward excursion is the finding -- it must be measurable, not zero"
print(
    f"V3 INWARD EXCURSION (convex-corner fixture): the smoothed exclusion does NOT contain the closed "
    f"union. It cuts {_convex_inward:.1f} m2 INSIDE it ({100 * _convex_inward / _convex_union.area:.2f}% "
    f"of the union) while pushing only {_convex_outward:.1f} m2 outside. The safe direction did not hold: "
    f"the net movement is INWARD, and inward means handing gate-excluded ground back as selectable."
)


# ===========================================================================
# 3b. WHY -- CHAIKIN'S NET-INWARD BIAS, AMPLIFIED BY THE SIMPLIFY PASS
# ===========================================================================
#
# Decomposing the pass is what turns "the numbers are bad" into a reason. Each
# operation is applied on its own, at the same settings, over the same union.

_ref_union, _ref_boundary = _reference_shaped_union()
_ref_parts = len(_parts(_ref_union))
_ref_acres = _ref_union.area / SQUARE_METERS_PER_ACRE
assert 4.0 <= _ref_acres <= 8.0 and 10 <= _ref_parts <= 25, (
    f"fixture sanity: the reference-shaped union must land near the real reference range "
    f"(4.7-7.2 ac, 10-18 polygons), got {_ref_acres:.3f} ac across {_ref_parts} polygons"
)


def _per_ring(geometry, ring_fn):
    """Applies ring_fn to the exterior and every interior ring of every part --
    the same polygon-level lift angular_smooth_polygon() does, reproduced here so
    each HALF of the pass can be measured on its own."""
    def one(poly):
        return Polygon(ring_fn(poly.exterior), [ring_fn(interior) for interior in poly.interiors])

    parts = [one(part) for part in _parts(geometry)]
    out = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    return out if out.is_valid else out.buffer(0)


_simplify_only = _per_ring(
    _ref_union,
    lambda ring: list(angular_simplify_closed_ring(LineString(ring.coords), TOLERANCE_M).coords),
).intersection(_ref_boundary)
_chaikin_only = _per_ring(
    _ref_union, lambda ring: chaikin_smooth_closed_ring(list(ring.coords), CHAIKIN_ITERATIONS)
).intersection(_ref_boundary)
_both = _smooth(_ref_union).intersection(_ref_boundary)

_simplify_ratio = _simplify_only.area / _ref_union.area
_chaikin_ratio = _chaikin_only.area / _ref_union.area
_both_ratio = _both.area / _ref_union.area

# Chaikin alone is net-inward on a closed ring: turning number +1 means the
# convex (inward) cuts always outweigh the reflex (outward) ones.
assert _chaikin_only.area < _ref_union.area, (
    "a closed-ring Chaikin pass must be net area-REDUCING -- any simple closed ring turns a net +360 "
    "degrees, so the inward convex cuts outweigh the outward reflex ones"
)
# And the composition is worse than either half: simplify lengthens the edges
# that Chaikin's corner cut is proportional to.
assert _both_ratio < min(_simplify_ratio, _chaikin_ratio), (
    "the two passes must compound: simplify collapses the staircase into long edges, and Chaikin's cut "
    "scales with edge length, so the composed pass removes more than either alone"
)
print(
    f"V3b DECOMPOSITION (reference-shaped fixture, {_ref_acres:.3f} ac across {_ref_parts} polygons):\n"
    f"   simplify only (1 cell)        area ratio {_simplify_ratio:.4f}   "
    f"inward {_ref_union.difference(_simplify_only).area:7.1f} m2\n"
    f"   Chaikin only (1 iteration)    area ratio {_chaikin_ratio:.4f}   "
    f"inward {_ref_union.difference(_chaikin_only).area:7.1f} m2\n"
    f"   both, in the mandated order   area ratio {_both_ratio:.4f}   "
    f"inward {_ref_union.difference(_both).area:7.1f} m2\n"
    f"   Neither half alone moves the union more than "
    f"{100 * (1 - min(_simplify_ratio, _chaikin_ratio)):.1f}%; composed they move it "
    f"{100 * (1 - _both_ratio):.1f}%. The passes are not independent."
)


# ===========================================================================
# 4. THE COMPLEMENT PROPERTY HOLDS -- THIS HALF OF THE DESIGN WAS SOUND
# ===========================================================================
#
# Deriving the eligible layer from whatever geometry the exclusion publishes
# gives two layers that share a boundary by construction: no sliver belongs to
# neither, none is claimed by both. That is true of the smoothed union and of
# the unsmoothed one alike -- it is a property of DERIVING rather than of
# smoothing, which is exactly why it survives the rejection intact and why
# exclusion_zones.py keeps deriving eligible_polygon_utm this way.

_COMPLEMENT_TOLERANCE_M2 = 1e-6

for _label, _exclusion in (("smoothed union", _both), ("unsmoothed closed union", _ref_union)):
    _eligible = _ref_boundary.difference(_exclusion)
    _covered = _eligible.union(_exclusion)
    _gap = _ref_boundary.difference(_covered).area
    _overlap = _eligible.intersection(_exclusion).area
    assert _gap < _COMPLEMENT_TOLERANCE_M2, (
        f"{_label}: eligible + exclusion must cover the whole boundary with no gap, got {_gap} m2"
    )
    assert _overlap < _COMPLEMENT_TOLERANCE_M2, (
        f"{_label}: eligible and exclusion must not overlap, got {_overlap} m2"
    )
print(
    f"V4 COMPLEMENT: for the smoothed AND the unsmoothed union alike, eligible + exclusion covers the "
    f"whole boundary (gap < {_COMPLEMENT_TOLERANCE_M2} m2) with an empty intersection. The property comes "
    f"from DERIVING the eligible layer, not from smoothing -- so the module keeps deriving it and the "
    f"two layers still share a boundary by construction."
)


# ===========================================================================
# 6. MULTIPOLYGON WITH INTERIOR RINGS SURVIVES THE PASS
# ===========================================================================

_mp_dem = _dem(60, 60)
_mp_boundary = _boundary(_mp_dem)
_holed = [
    (r, c)
    for r in range(4, 24)
    for c in range(4, 24)
    if not (10 <= r < 17 and 10 <= c < 17)  # a real interior hole
]
_solid = [(r, c) for r in range(34, 52) for c in range(34, 52)]
_mp_union = cell_union_footprint(_mp_dem, _mask((60, 60), _holed + _solid)).intersection(_mp_boundary)

assert _mp_union.geom_type == "MultiPolygon" and len(_mp_union.geoms) == 2, "fixture sanity: two parts"
assert sorted(len(p.interiors) for p in _mp_union.geoms) == [0, 1], "fixture sanity: exactly one hole"

_mp_rings_in = _ring_count(_mp_union)
_mp_smoothed = _smooth(_mp_union)
_mp_rings_out = _ring_count(_mp_smoothed)

assert _mp_smoothed.geom_type == "MultiPolygon", "both parts must survive as a MultiPolygon"
assert len(_mp_smoothed.geoms) == 2, f"both parts must survive, got {len(_mp_smoothed.geoms)}"
assert sorted(len(p.interiors) for p in _mp_smoothed.geoms) == [0, 1], "the interior ring must survive"
assert _mp_smoothed.is_valid, "the smoothed MultiPolygon must be valid"
assert _mp_rings_out == _mp_rings_in == 3, f"all {_mp_rings_in} rings must be processed and survive"
print(
    f"V6 MULTIPOLYGON + HOLE: {_mp_rings_in} rings processed (2 exteriors + 1 interior across 2 parts); "
    f"both parts and the hole survive, result is valid, {_mp_rings_out} rings out. On the "
    f"reference-shaped fixture the pass processes {_ring_count(_ref_union)} rings across "
    f"{_ref_parts} polygons."
)


# ===========================================================================
# 7. INVALID GEOMETRY DEGRADES TO THE INPUT, NEVER RAISES
# ===========================================================================

_sliver = Polygon([(0.0, 0.0), (200.0, 0.0), (400.0, 0.0)])  # collinear, zero-area
_sliver_result = _smooth(_sliver)
assert _sliver_result is _sliver, (
    "a sliver that goes invalid under smoothing must come back as the INPUT OBJECT unchanged"
)
_non_polygonal = _sliver.intersection(box(1000.0, 1000.0, 1001.0, 1001.0))  # empty
assert _smooth(_non_polygonal) is _non_polygonal, "non-polygonal input must come back unchanged"
print(
    "V7 FALLBACK: a degenerate zero-width sliver and an empty geometry both come back as the input "
    "object, unchanged and without an exception -- a bad smooth degrades to the exact unsmoothed shape."
)


# ===========================================================================
# 8. AREA RATIO ON THE REFERENCE-SHAPED FIXTURE -- THE STOP CONDITION
# ===========================================================================

_ref_inward = _ref_union.difference(_both).area
_ref_inward_acres = _ref_inward / SQUARE_METERS_PER_ACRE

# The largest gain any closing radius in exclusion_zones.py was measured to
# produce, across both reference boundaries (module docstring's CLOSING table):
# canopy +0.117/+0.228 ac, slope +0.234/+0.370 ac. Smoothing must not undo what
# the closing was tuned at cell-level precision to add.
_LARGEST_MEASURED_CLOSING_GAIN_ACRES = 0.370

assert _ref_inward_acres > _LARGEST_MEASURED_CLOSING_GAIN_ACRES, (
    "FINDING NO LONGER HOLDS: smoothing now gives back less ground than the closing was measured to "
    "add. Revisit the rejection recorded in exclusion_zones.py."
)
print(
    f"V8 AREA RATIO: the smoothed union is {_both_ratio:.4f} of the unsmoothed one -- a "
    f"{100 * (1 - _both_ratio):.2f}% loss at the GENTLEST supportable settings (one DEM cell of "
    f"tolerance, one Chaikin pass; the brief rules out going below either).\n"
    f"   The decisive figure is the inward excursion: {_ref_inward:.1f} m2 = {_ref_inward_acres:.3f} "
    f"acres of gate-excluded ground published as selectable.\n"
    f"   Every closing radius in exclusion_zones.py was measured at cell-level precision, and the "
    f"largest gain any of them produces on the reference boundaries is "
    f"+{_LARGEST_MEASURED_CLOSING_GAIN_ACRES} ac. Smoothing gives back "
    f"{_ref_inward_acres / _LARGEST_MEASURED_CLOSING_GAIN_ACRES:.1f}x that, in the one direction this "
    f"module cannot afford to be wrong in. This is the stop condition, and it is why the switch was "
    f"not made."
)


# ===========================================================================
# 9. THE OBVIOUS REMEDY IS WORSE THAN DOING NOTHING
# ===========================================================================
#
# Unioning the smoothed result back with the closed union makes containment true
# by construction -- the exclusion can then only ever grow. But the union
# restores the original staircase everywhere the smooth cut inward, which on
# this geometry is most of the perimeter, and ADDS the smoothed arcs on top.

_remedy = _both.union(_ref_union)
_remedy_vertices = _exterior_vertices(_remedy)
_raw_vertices = _exterior_vertices(_ref_union)

assert _ref_union.difference(_remedy).area < 1e-9, "the union remedy makes containment exact"
assert _remedy_vertices > _raw_vertices, (
    "and it is measurably MORE staircased than the raw union it was meant to clean up"
)
print(
    f"V9 REMEDY REJECTED: smoothed-UNION-closed makes containment exact (0.0 m2 inward) but leaves "
    f"{_remedy_vertices} exterior vertices against the raw union's own {_raw_vertices} -- "
    f"{100 * _remedy_vertices / _raw_vertices:.0f}% of the staircase it was supposed to remove, because "
    f"the union restores the original steps wherever the smooth cut inward and keeps the smoothed arcs "
    f"everywhere else. It buys the safe direction by giving up the entire point of the pass."
)


print(
    "\nAll exclusion smoothing measurements passed. CONCLUSION: the smoothing pass is net INWARD on "
    "exclusion geometry, not net outward as designed, and the switch was not made -- exclusion_zones.py "
    "still publishes the exact closed union. The relocated helpers in raster_grid.py are unaffected and "
    "remain in use by render_layout_map.py."
)
