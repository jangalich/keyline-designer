"""
test_production_area_render_opening.py

Offline (no-network) verification for render_fill_polygon_utm being built as a
BOUNDED MORPHOLOGICAL OPENING clipped to polygon_utm, replacing the old
unbounded convex hull (branch: replace-hull-with-opening).

The opening is erode-by-r then dilate-back-by-r (r = RENDER_OPENING_RADIUS_
METERS), clipped to polygon_utm. Its defining property -- the whole reason the
hull was removed -- is BOUNDEDNESS: render_fill_polygon_utm is always contained
within polygon_utm, so the drawn shape (which four-plus modules consume as
production EXCLUSION geometry) can never claim ground the cell gate excluded.

An opening is anti-extensive (opening(A) <= A), so it TRIMS thin protrusions and
SEVERS narrow necks but never FILLS anything: notches, pockets, and wide
concavities all stay open. That is the correct, intended behavior here -- a fill
bounded by polygon_utm cannot cover excluded ground by definition, so the hull's
old notch-closing job is deliberately not preserved (see the branch history:
the original "notch closure" check was dropped as incompatible with the
boundedness guarantee).

All fixtures are hand-constructed cell masks on a synthetic flat 5.0m x 5.0m DEM
(25 m^2/cell; 1 acre = 4046.8564224 m^2). RENDER_OPENING_RADIUS_METERS = 12.0m
gives a 2-cell (10m) opening radius. Nothing here touches the network.
"""

import copy
import logging

import numpy as np
from shapely.geometry import Point, box

import production_area as pa
from production_area import (
    RENDER_OPENING_RADIUS_METERS,
    cluster_and_gate,
    compute_step1_eligible_cells,
)
from production_suitability import score_production_areas
from raster_grid import SQUARE_METERS_PER_ACRE, waist_erosion_radius_cells

_PAD = pa.PRODUCTION_BOUNDARY_SETBACK_METERS + 1.0


def _dem(rows, cols):
    return {
        "array": np.full((rows, cols), 100.0, dtype=np.float32),
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _boundary(dem):
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    return box(x0 - _PAD, y0 - rows * py - _PAD, x0 + cols * px + _PAD, y0 + _PAD)


def _rect(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask(shape, cells):
    m = np.zeros(shape, dtype=bool)
    for r, c in cells:
        m[r, c] = True
    return m


def _gate(mask, dem):
    boundary = _boundary(dem)
    step1 = compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=None)
    return cluster_and_gate(mask, dem, boundary, step1), step1


# The opening radius the fill actually uses, in cells (for hand-reasoning).
_OPEN_RADIUS_CELLS = waist_erosion_radius_cells(_dem(4, 4), RENDER_OPENING_RADIUS_METERS)
assert _OPEN_RADIUS_CELLS == 2, f"expected a 2-cell opening radius at 12m/5m, got {_OPEN_RADIUS_CELLS}"


# ===================================================================== #
# Fixtures.                                                             #
# ===================================================================== #

# PLAIN: a solid 20x20 block (100m x 100m), no neck, notch, or bay.
PLAIN_SHAPE = (30, 30)
PLAIN_MASK = _mask(PLAIN_SHAPE, _rect(2, 22, 2, 22))

# WAISTED-VETO: a big lobe A (14x14 = 1.2108 ac) joined by a 2-cell (10m) neck
# to a SUB-FLOOR lobe B (8x8 = 0.3954 ac). The whole cluster survives as one
# patch but the waist split is VETOED (B is below the 0.5 ac floor), so this
# exercises the no-split render path where the opening does the work.
WAIST_SHAPE = (30, 60)
WAIST_MASK = _mask(WAIST_SHAPE, _rect(0, 14, 0, 14) + _rect(6, 8, 14, 20) + _rect(4, 12, 20, 28))

# NOTCH: a solid 24x24 block with a 1-cell-wide (5m) slot cut 10 cells deep from
# one edge -- an interior notch NARROWER than the opening radius.
NOTCH_SHAPE = (24, 24)
NOTCH_SLOT = _rect(0, 10, 12, 13)
NOTCH_MASK = _mask(NOTCH_SHAPE, [c for c in _rect(0, 24, 0, 24) if c not in set(NOTCH_SLOT)])

# BAY: a 30x30 block with a WIDE (10-cell = 50m) rectangular bay open at one
# edge -- a concavity far wider than the opening radius (the case the convex
# hull filled in wrongly).
BAY_SHAPE = (30, 30)
BAY_MOUTH = _rect(0, 18, 10, 20)
BAY_MASK = _mask(BAY_SHAPE, [c for c in _rect(0, 30, 0, 30) if c not in set(BAY_MOUTH)])

# STRIP: a 3-cell-wide (15m) strip -- thinner than the opening's 5-cell diameter
# throughout, so the opening erodes it to nothing. 123 cells = 0.7598 ac, still
# above the 0.5 ac floor, so it survives as a patch.
STRIP_SHAPE = (20, 60)
STRIP_MASK = _mask(STRIP_SHAPE, _rect(0, 3, 0, 41))

ALL_FIXTURES = [
    ("plain", PLAIN_MASK, PLAIN_SHAPE),
    ("waisted-veto", WAIST_MASK, WAIST_SHAPE),
    ("notch", NOTCH_MASK, NOTCH_SHAPE),
    ("bay", BAY_MASK, BAY_SHAPE),
    ("strip", STRIP_MASK, STRIP_SHAPE),
]


# ===================================================================== #
# Verification 1 -- BOUNDEDNESS, exhaustively.                          #
# render_fill_polygon_utm is contained within polygon_utm on every       #
# fixture. This is the branch's core guarantee and the reason the hull   #
# was removed.                                                          #
# ===================================================================== #

_bounded_count = 0
for name, mask, shape in ALL_FIXTURES:
    patches, _ = _gate(mask, _dem(*shape))
    for patch in patches:
        fill = patch["render_fill_polygon_utm"]
        poly = patch["polygon_utm"]
        # area bound
        assert fill.area <= poly.area + 1e-6, (
            f"[{name}] render_fill area {fill.area} exceeds polygon_utm area {poly.area}"
        )
        # containment: nothing of the fill lies outside the footprint
        assert fill.difference(poly).area < 1e-6, (
            f"[{name}] render_fill has {fill.difference(poly).area} m^2 outside polygon_utm -- must be bounded"
        )
        _bounded_count += 1
print(
    f"V1 boundedness (exhaustive): render_fill_polygon_utm is contained within polygon_utm on all "
    f"{_bounded_count} patch(es) across {len(ALL_FIXTURES)} fixtures -- by area and by geometric difference."
)


# ===================================================================== #
# Verification 2 -- reported-field invariance.                          #
# The change touches only render_fill_polygon_utm. Every reported field  #
# is derived from polygon_utm/cells, and STEP 4 scoring reads none of    #
# the render fields -- so stripping the render fields leaves scoring     #
# byte-identical, and the reported fields equal their independent        #
# derivations.                                                          #
# ===================================================================== #

inv_dem = _dem(*WAIST_SHAPE)
inv_patches, inv_step1 = _gate(WAIST_MASK, inv_dem)
assert len(inv_patches) == 1

_p = inv_patches[0]
# Reported fields derive from polygon_utm/cells, independent of render_fill.
assert _p["area_acres"] == round(_p["polygon_utm"].area / SQUARE_METERS_PER_ACRE, 2), (
    "area_acres must reflect polygon_utm, independent of the render fill"
)

# STEP 4 scoring must not depend on the render fill at all: score once as-is,
# once with the render fill stripped off, and require byte-identical results.
scored_full = score_production_areas(copy.deepcopy(inv_patches), inv_dem, inv_step1)
stripped = copy.deepcopy(inv_patches)
for patch in stripped:
    patch.pop("render_fill_polygon_utm")
scored_stripped = score_production_areas(stripped, inv_dem, inv_step1)

_cmp_fields = [k for k in scored_full[0] if k != "render_fill_polygon_utm"]
for i in range(len(scored_full)):
    for k in _cmp_fields:
        if k not in scored_stripped[i]:
            continue
        a, b = scored_full[i][k], scored_stripped[i][k]
        same = a.equals(b) if hasattr(a, "geom_type") else a == b
        assert same, f"STEP 4 field {k!r} changed when the render fill was stripped -- scoring must not read it"
print(
    f"V2 reported-field invariance: area_acres derives from polygon_utm, and all {len(_cmp_fields)} STEP 4 "
    f"scoring fields are byte-identical with and without the render fill -- scoring does not read "
    "render_fill_polygon_utm."
)


# ===================================================================== #
# Verification 3 -- the opening OPENS a narrow neck.                    #
# ===================================================================== #

waist_patch = _gate(WAIST_MASK, _dem(*WAIST_SHAPE))[0][0]
w_fill = waist_patch["render_fill_polygon_utm"]
assert w_fill.geom_type == "MultiPolygon" and len(list(w_fill.geoms)) == 2, (
    f"the opening must sever the narrow neck into a 2-part MultiPolygon, got {w_fill.geom_type} "
    f"with {len(list(getattr(w_fill, 'geoms', [w_fill])))} part(s)"
)
# the eroded-away neck's centre falls outside the fill
neck_gap = Point(500000.0 + 16.5 * 5.0, 4500000.0 - 6.5 * 5.0)
assert not w_fill.contains(neck_gap), "the severed neck gap must fall OUTSIDE the opening's fill"
print(
    "V3 neck opens: a vetoed waisted cluster's render_fill_polygon_utm is a 2-part MultiPolygon with the "
    "eroded-away neck open (the neck-gap point falls outside the fill)."
)


# ===================================================================== #
# Verification 4/5 -- the outer edge insets by EXACTLY the lead erode.  #
# The opening is now ASYMMETRIC (erode r + RENDER_LEAD_ERODE_CELLS,      #
# dilate r) with a DISC element, so an ordinary rectangle's straight     #
# edges pull in by exactly the lead (one cell) and its corners round.    #
# This is the cost of the lead erode on ordinary zones -- report it.     #
# ===================================================================== #

plain_patch = _gate(PLAIN_MASK, _dem(*PLAIN_SHAPE))[0][0]
plain_poly = plain_patch["polygon_utm"]
plain_fill = plain_patch["render_fill_polygon_utm"]
plain_ratio = plain_fill.area / plain_poly.area
assert plain_fill.area <= plain_poly.area + 1e-6, "the opening stays bounded by the footprint"
_pb, _fb = plain_poly.bounds, plain_fill.bounds
_inset_x = ((_fb[0] - _pb[0]) + (_pb[2] - _fb[2])) / 2
_inset_y = ((_fb[1] - _pb[1]) + (_pb[3] - _fb[3])) / 2
_one_cell = 5.0  # RENDER_LEAD_ERODE_CELLS (1) * cell size (5m)
assert abs(_inset_x - _one_cell) < 1e-6 and abs(_inset_y - _one_cell) < 1e-6, (
    f"the lead erode (RENDER_LEAD_ERODE_CELLS={pa.RENDER_LEAD_ERODE_CELLS}) must inset each straight edge by "
    f"exactly one cell ({_one_cell}m); measured inset ({_inset_x}, {_inset_y})"
)
print(
    f"V4/V5 asymmetric inset: a plain 100m x 100m zone's straight edges inset by exactly one cell ({_one_cell}m, "
    f"the lead erode) with disc-rounded corners -- render_fill/polygon_utm area ratio {plain_ratio:.4f}."
)


# ===================================================================== #
# Verification 5 (inverse) -- a narrow interior notch stays OPEN.       #
# The opening is anti-extensive and the fill is clipped to polygon_utm,  #
# so a notch -- excluded ground OUTSIDE the footprint -- is never closed #
# over, even when it is narrower than the opening radius. This documents #
# in executable form that the fill is bounded and never claims excluded  #
# ground (replacing the original, incompatible "notch closes" check).    #
# ===================================================================== #

notch_patch = _gate(NOTCH_MASK, _dem(*NOTCH_SHAPE))[0][0]
notch_footprint = pa._cell_union_footprint(NOTCH_SLOT, _dem(*NOTCH_SHAPE))
notch_covered = notch_patch["render_fill_polygon_utm"].intersection(notch_footprint).area
assert notch_covered < 1e-6, (
    f"a narrow interior notch (5m wide, < the opening radius) must stay OPEN -- the bounded opening cannot "
    f"close over excluded ground; render_fill covers {notch_covered} m^2 of the notch (expected 0)"
)
assert notch_patch["render_fill_polygon_utm"].area <= notch_patch["polygon_utm"].area + 1e-6
print(
    "V5 (inverse) narrow notch stays open: a 5m-wide interior notch (narrower than the opening radius) is "
    "NOT closed over -- the bounded opening leaves excluded ground open, unlike a closing or a convex hull."
)


# ===================================================================== #
# Verification 6 -- a WIDE concavity (C-shaped bay) stays open.         #
# The case the convex hull got wrong (it filled the bay in).            #
# ===================================================================== #

bay_patch = _gate(BAY_MASK, _dem(*BAY_SHAPE))[0][0]
bay_footprint = pa._cell_union_footprint(BAY_MOUTH, _dem(*BAY_SHAPE))
bay_covered = bay_patch["render_fill_polygon_utm"].intersection(bay_footprint).area
assert bay_covered < 1e-6, (
    f"a wide (50m) bay must stay OPEN, not be filled like the convex hull did -- render_fill covers "
    f"{bay_covered} m^2 of the bay (expected 0)"
)
bay_centre = Point(500000.0 + 15 * 5.0, 4500000.0 - 9 * 5.0)
assert not bay_patch["render_fill_polygon_utm"].contains(bay_centre), "the bay centre must fall outside the fill"
print(
    "V6 wide concavity stays open: a 50m C-shaped bay is not filled by the opening (its centre falls outside "
    "the fill) -- the exact over-claim the convex hull produced and the reason it was removed."
)


# ===================================================================== #
# Verification 7 -- erosion-wipeout fallback.                           #
# ===================================================================== #

_wipeout_logs = []
_handler = logging.Handler()
_handler.emit = lambda record: _wipeout_logs.append(record.getMessage())
pa._LOGGER.addHandler(_handler)
pa._LOGGER.setLevel(logging.WARNING)
try:
    strip_patches, _ = _gate(STRIP_MASK, _dem(*STRIP_SHAPE))
finally:
    pa._LOGGER.removeHandler(_handler)

assert len(strip_patches) == 1, f"the thin strip (0.7598 ac) must survive as one patch, got {len(strip_patches)}"
strip_patch = strip_patches[0]
assert strip_patch["render_fill_polygon_utm"].equals(strip_patch["polygon_utm"]), (
    "erosion wipeout: render_fill_polygon_utm must fall back to polygon_utm, not empty geometry"
)
assert not strip_patch["render_fill_polygon_utm"].is_empty
assert len(_wipeout_logs) == 1, f"the wipeout must be logged exactly once, got {len(_wipeout_logs)}"
print(
    f"V7 wipeout fallback: a 3-cell-wide (15m) strip erodes to nothing under the opening; "
    f"render_fill_polygon_utm falls back to polygon_utm (non-empty) and logs the wipeout once. "
    f"Wipeouts across this suite: {len(_wipeout_logs)}."
)


print("\nAll production_area render-opening checks passed.")
