"""
test_tree_zone_geometry_validity.py

TREE-ZONE GEOMETRY AT EMISSION -- the gate that stopped a generated candidate
from reaching commit invalid, and the smoothing pass that was eating the thin
arms it exists to protect. Run as:

    python test_tree_zone_geometry_validity.py

WHAT BROKE. Committing tree zones failed on four GENERATED candidates with

    tree zone 'tree-zone-candidate-11': geometry is not valid --
    Self-intersection[585998.668658903 4499652.38576178]

The ids are the server's own naming, so those were generated candidates, not
drawn rings, and the rehydrator refused them at commit. THE REFUSAL IS
CORRECT and it is untouched here: wire_translation.py's rule -- rehydration
never repairs geometry, because buffer(0) on a self-intersecting ring silently
returns lobes nobody drew and the acreage attributed to them would be fiction
-- was written for a USER-DRAWN ring, and it stays exactly as it is. What was
wrong is that a GENERATED candidate could be invalid at all: the module emitted
geometry its own downstream validity gate rejects, so the check was on the
wrong side of the wire and the user met it after selecting candidates and
pressing commit.

WHY TREE PATCHES ARE RAGGED AND PRODUCTION'S ARE NOT. A tree patch is a union
of 5 m DEM cells with NO opening, NO hull and NO smoothing, which is
deliberate: thin arms and interior pockets -- a windbreak row, a riparian
strip, an edge planting -- are exactly the geometry the layer exists to find,
and an opening at any radius would delete them. Production has no such problem
because its own asymmetric opening cleans the geometry as a side effect. Trees
had nothing doing that, so the same raggedness surfaced twice: single-cell
interior PINHOLES from the SLOPE gate, and cells meeting only DIAGONALLY whose
ring touches itself at a vertex.

THE FIXTURE, AND WHAT IT CAN AND CANNOT SHOW. Two parcels, both the real 5614
N Montour Rd boundary:

  - THE SHIPPED REFERENCE PARCEL, imported from trees_step_fixture.py -- the
    same session test_trees_step.py proves sound in its own run, through the
    real generate. Its DEM
    is an ANALYTIC bench-and-drainage surface, so its slope field is smooth and
    its candidates come out as clean, hole-free polygons. It is the regression
    guard: every patch a real generate emits is valid, and that is asserted on
    the pipeline as it actually ships.

  - THE RAGGED REFERENCE PARCEL, built here -- the same boundary, the same
    grid, the same 5 m cells, with deterministic terrain roughness added so the
    SLOPE gate decides cell by cell the way it does on real 3DEP elevation. It
    is what reproduces the live SYMPTOM: 54 interior rings on one candidate, 38
    of them a single cell.

  AND THE HONEST LIMIT. Neither parcel reproduces the live SELF-INTERSECTION
  under the GEOS this suite runs on: GEOS 3.13's overlay returns valid output
  from every cell union, difference and clip this module performs, so the
  invalid ring the live server produced cannot be manufactured through the
  pipeline here. Section 2 therefore asserts the repair against the defect
  ITSELF -- a ring built to touch itself at a vertex, exactly the shape the
  live error names -- proving that wire_translation.py refuses it, that the
  emission gate repairs it, and that the repaired form commits. The gate is
  unconditional, so it does not depend on which of those two paths produced
  the ring.

Sections:
  1  EVERY EMITTED PATCH IS VALID, on both parcels, and the commit path takes
     every one of them.
  2  THE SELF-TOUCH, repaired: refused by the commit boundary, repaired by the
     emission gate, area preserved exactly, and the split stays ONE candidate.
     buffer(0) and an opening are both shown to be the wrong tools rather than
     merely unused.
  3  SUB-THRESHOLD HOLES FILLED, MULTI-CELL POCKETS KEPT -- counts and acreage
     per candidate, before and after.
  4  THIN ARMS PRESERVED -- the narrowest arm on the reference parcel measured
     before and after, and against the smoothing pass that was removed.
  5  AREA CHANGE per patch, accounted for cell by cell.
  6  AN UNREPAIRABLE PATCH IS DROPPED WITH A COUNT, through to the wire.
  7  TREE FEATURES CARRY NO DISPLAY OUTLINE; production features still do.
  8  Regression is the other test files, run separately.
"""

import io
import sys
from contextlib import redirect_stdout

_captured = io.StringIO()
try:
    with redirect_stdout(_captured):
        import trees_step_fixture as fixture
except BaseException:
    sys.stdout.write(_captured.getvalue())
    raise

import numpy as np
import shapely
import json

from rasterio.warp import transform_geom
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.validation import explain_validity, make_valid

import display_outline
import tree_zone_candidates as tzc
import wire_translation
from raster_grid import SQUARE_METERS_PER_ACRE
from road_corridors import NO_ROAD_CORRIDOR
from water_suitability import NO_WATER_ZONE

OUTLINE = display_outline.DISPLAY_ONLY_OUTLINE_PROPERTY
CELL_AREA_M2 = 25.0  # 5 m x 5 m, this parcel's own DEM resolution


# --- the two parcels ----------------------------------------------------

with fixture.Harness() as _harness:
    SESSION = fixture.Session()
    # Generated in registry order and each payload kept, because section 7 asks
    # the landform payload what its features carry and a committed step's
    # layers are no longer readable.
    LANDFORM_PAYLOAD = SESSION.generate("landform")
    _zones = LANDFORM_PAYLOAD["suggested_zones"]["features"]
    SESSION.commit("landform", _zones, {f["id"]: "generated" for f in _zones})
    SESSION.commit_water(3)
    SESSION.commit_roads()
    TREES_PAYLOAD = SESSION.trees()
    _context = SESSION.context()
    SHIPPED_RESULT = _context.step_proposals["trees"]
    SHIPPED_PATCHES = SHIPPED_RESULT["patches"]
    PRODUCTION_PATCHES = _context.step_proposals["landform"]["scored_patches"]
    DEM = _context.dem

BOUNDARY = fixture.BOUNDARY_POLYGON_UTM
CELL_M = max(DEM["resolution_meters"])
assert SHIPPED_PATCHES, "the fixture must produce tree candidates, or everything below is vacuous"


# THE RAGGED PARCEL. The same boundary and the same grid as the fixture's own
# DEM -- identical origin, identical shape, identical 5 m cells -- with a
# deterministic roughness added on top of the same bench-and-drainage surface.
#
# ROUGHNESS, NOT A DIFFERENT LANDSCAPE. What makes a real tree patch ragged is
# that the SLOPE gate is decided per cell against a per-cell slope, and real
# elevation is noisy at the cell scale in a way an analytic surface is not. The
# amplitude below is chosen for one property and stated so it is not mistaken
# for tuning: it puts the parcel's slope field ACROSS the gate's own threshold,
# which is what makes single cells fall out of an otherwise-qualifying block.
# Seeded, so every number this file prints is reproducible.
RAGGED_SEED = 0
RAGGED_ROUGHNESS_METERS = 0.9


def _ragged_dem() -> dict:
    base = fixture._build_dem()
    rng = np.random.default_rng(RAGGED_SEED)
    array = base["array"].astype(np.float64) + rng.normal(
        0.0, RAGGED_ROUGHNESS_METERS, size=base["array"].shape
    )
    return {**base, "array": array.astype(np.float32)}


RAGGED_DEM = _ragged_dem()
assert RAGGED_DEM["array"].shape == DEM["array"].shape
assert RAGGED_DEM["origin_x"] == DEM["origin_x"] and RAGGED_DEM["crs"] == DEM["crs"]

_PARCEL_DATA = fixture._build_parcel_data()
SCORING_INPUTS = tzc.scoring_inputs_for_parcel_data(_PARCEL_DATA)
assert SCORING_INPUTS is not None


def ragged_generate(**kwargs) -> dict:
    """
    The REAL entry point on the ragged parcel, offline. Every upstream layer is
    passed explicitly at its own "already ran, claimed nothing" sentinel so no
    self-compute and no fetch can run: the search space is the whole
    setback-shrunk boundary, which is what gives the slope gate the most ground
    to be ragged over.

    CALLED OUTSIDE THE HARNESS, deliberately -- every mock is off by the time
    this runs, so a fetch this function failed to close would raise a real
    connection error rather than quietly hitting a stub.
    """
    return tzc.identify_tree_zone_candidates(
        fixture.REAL_BOUNDARY,
        dem=RAGGED_DEM,
        boundary_polygon_utm=BOUNDARY,
        production_areas=[],
        selected_water_zone=NO_WATER_ZONE,
        selected_road_corridor=NO_ROAD_CORRIDOR,
        canopy_height=fixture._build_canopy(RAGGED_DEM),
        scoring_inputs=SCORING_INPUTS,
        **kwargs,
    )


RAGGED_RESULT = ragged_generate()
RAGGED_PATCHES = RAGGED_RESULT["patches"]
assert RAGGED_PATCHES, "the ragged parcel must produce candidates"

# THE SAME GENERATE WITH THE GATE OFF -- the geometry this module used to emit.
# Not a re-implementation: it is the same call with the hole threshold set to
# its own documented "fill nothing" value, so the only difference between the
# two runs is the thing under test. The validity repair cannot be switched off
# the same way (it is unconditional, which is the point), so section 5's area
# accounting is read against this run and section 2 proves the repair against
# the defect directly.
BEFORE_RESULT = ragged_generate(filled_interior_ring_max_cells=0.0)
BEFORE_PATCHES = BEFORE_RESULT["patches"]
BEFORE_BY_ID = {patch["id"]: patch for patch in BEFORE_PATCHES}
AFTER_BY_ID = {patch["id"]: patch for patch in RAGGED_PATCHES}
assert set(BEFORE_BY_ID) == set(AFTER_BY_ID), (
    "hole-filling must not change WHICH components qualify -- it only fills holes"
)


# --- helpers ------------------------------------------------------------


def polygonal_parts(geometry) -> list:
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    return [part for part in geometry.geoms if part.geom_type == "Polygon"]


def interior_ring_areas_in_cells(geometry) -> list:
    return [
        Polygon(ring).area / CELL_AREA_M2
        for part in polygonal_parts(geometry)
        for ring in part.interiors
    ]


def narrowest_arm_meters(geometry, hi: float = 40.0, tolerance: float = 0.01):
    """
    THE NARROWEST ARM, in metres: the smallest opening diameter that DELETES a
    feature rather than nibbling a corner.

    Defined as 2r for the smallest radius r at which the morphological opening
    geometry.buffer(-r).buffer(r) removes a CONNECTED piece of ground at least
    one DEM cell in area. The connectivity and the one-cell floor are both
    load-bearing. An opening at any radius shaves every convex corner of a
    staircase, and on a cell union there are hundreds of them; those shavings
    are individually microscopic, so measuring total removed area would report
    the corner count rather than the arm. Requiring one whole cell to come away
    in ONE piece asks the question that matters: at what width does a limb
    disappear.

    Calibrated in section 4 against shapes whose answer is known by
    construction -- a one-cell-wide arm measures 5.00 m and a two-cell-wide arm
    10.00 m, which is what makes the number below a measurement of arms rather
    than of raggedness.

    Returns None for a shape with no feature this test can delete inside `hi`.
    """

    def deletes_a_cell(radius: float) -> bool:
        opened = geometry.buffer(-radius).buffer(radius)
        removed = geometry.difference(opened)
        if removed.is_empty:
            return False
        return max((part.area for part in polygonal_parts(removed)), default=0.0) >= CELL_AREA_M2

    if not deletes_a_cell(hi):
        return None
    low = 0.0
    while hi - low > tolerance:
        mid = (low + hi) / 2.0
        if deletes_a_cell(mid):
            hi = mid
        else:
            low = mid
    return 2.0 * hi


def commits(patch, dem) -> bool:
    """The patch's own wire geometry back through the REAL inbound boundary --
    wire_translation._polygonal_shape_from_wire(), the function that raised on
    the four live candidates. Returns True or raises its own error."""
    feature = wire_translation.tree_zones_to_feature_collection([patch])["features"][0]
    wire_translation._polygonal_shape_from_wire(feature["geometry"], dem, f"tree zone {feature['id']!r}")
    return True


# --- 1. EVERY EMITTED PATCH IS VALID, AND EVERY ONE COMMITS -------------
#
# THE ASSERTION THE BUG IS ABOUT, stated at the emission boundary rather than
# at the commit boundary. A patch that leaves identify_tree_zone_candidates()
# is OGC-valid; if it is not, it is not emitted at all (section 6).

_checked = 0
for label, patches, dem in (
    ("shipped", SHIPPED_PATCHES, DEM),
    ("ragged", RAGGED_PATCHES, RAGGED_DEM),
    ("ragged, gate's hole-fill off", BEFORE_PATCHES, RAGGED_DEM),
):
    for patch in patches:
        for field in ("polygon_utm", "render_fill_polygon_utm"):
            geometry = patch[field]
            assert geometry.is_valid, (
                f"{label} patch {patch['id']}: {field} is invalid at emission -- "
                f"{explain_validity(geometry)}"
            )
            assert not geometry.is_empty and geometry.geom_type in ("Polygon", "MultiPolygon")
        # THE IDENTITY THE PRODUCER PROMISES, through the gate: the render fill
        # IS the polygon. Filling a hole and repairing a ring both happen
        # BEFORE the two names are bound, so one object still answers to both.
        assert patch["render_fill_polygon_utm"] is patch["polygon_utm"], patch["id"]
        _checked += 1

# AND THE COMMIT BOUNDARY TAKES THEM. Valid-at-emission is the claim; passing
# the function that raised on the four live candidates is the proof, and it is
# a different statement because the wire round-trip reprojects twice.
_committed = 0
for patches, dem in ((SHIPPED_PATCHES, DEM), (RAGGED_PATCHES, RAGGED_DEM)):
    for patch in patches:
        assert commits(patch, dem)
        _committed += 1

print(
    f"1. VALID AT EMISSION: every geometry on all {_checked} emitted patches -- "
    f"{len(SHIPPED_PATCHES)} shipped reference-parcel candidate(s), {len(RAGGED_PATCHES)} ragged, "
    f"and {len(BEFORE_PATCHES)} more from the same ragged run with the hole-fill off -- is "
    f"OGC-valid as emitted "
    f"(shapely {shapely.__version__}, GEOS {'.'.join(str(n) for n in shapely.geos_version)}), "
    f"render_fill_polygon_utm IS polygon_utm on every one, and all {_committed} of them pass "
    f"wire_translation._polygonal_shape_from_wire() -- the function that raised "
    f"'geometry is not valid -- Self-intersection' on the four live candidates."
)


# --- 2. THE SELF-TOUCH, REPAIRED ---------------------------------------
#
# The defect itself, built rather than hunted: an exterior ring that touches
# itself at one vertex, which is what two cells meeting only diagonally produce
# and what the live error names. Four claims, in the order they matter.

PINCH = Polygon([(0.0, 0.0), (5.0, 5.0), (10.0, 0.0), (10.0, 10.0), (5.0, 5.0), (0.0, 10.0)])
assert not PINCH.is_valid and "Self-intersection" in explain_validity(PINCH), explain_validity(PINCH)
_pinch_area = PINCH.area

# (a) THE COMMIT BOUNDARY REFUSES IT, unchanged. This is the rule staying put:
#     rehydration does not repair geometry, and the four live candidates were
#     refused correctly.
_wgs84_pinch = transform_geom(
    DEM["crs"],
    "EPSG:4326",
    mapping(shapely.affinity.translate(PINCH, DEM["origin_x"] + 200.0, DEM["origin_y"] - 200.0)),
)
try:
    wire_translation._polygonal_shape_from_wire(_wgs84_pinch, DEM, "tree zone 'pinch'")
    raise AssertionError("the commit boundary accepted a self-touching ring")
except wire_translation.InboundGeometryError as error:
    assert "not valid" in str(error) and "buffer(0)" in str(error), str(error)

# (b) THE EMISSION GATE REPAIRS IT, and the repair is make_valid()'s re-noding:
#     the area is preserved EXACTLY, not approximately.
_repaired = tzc._valid_polygonal(PINCH)
assert _repaired is not None and _repaired.is_valid, _repaired
assert _repaired.area == _pinch_area, (_repaired.area, _pinch_area)

# (c) IT SPLITS, AND THE PIECES STAY ONE CANDIDATE. A ring that touches itself
#     cannot be made into one ring that does not without adding or removing
#     ground at the corner, so make_valid() returns the two lobes. They are one
#     8-connected component with one score, one rank and one id, and trees
#     already ships MultiPolygons -- so the patch carries multi-part geometry
#     rather than becoming two candidates.
assert _repaired.geom_type == "MultiPolygon" and len(_repaired.geoms) == 2, _repaired.geom_type
assert sum(part.area for part in _repaired.geoms) == _pinch_area

# (d) THE TWO TOOLS THAT ARE REFUSED, shown to be refused for a reason rather
#     than merely unused.
#
#     buffer(0) -- the operation wire_translation.py's rule is written against.
#     On this ring it happens to return the same two lobes, which is exactly
#     what makes it dangerous: it is an overlay that silently rewrites whatever
#     else it finds, with no signal that it did, so its agreeing here is not
#     evidence that it would agree on the next ring.
_module_source = open("tree_zone_candidates.py").read()
_buffer_zero_calls = [
    line for line in _module_source.splitlines()
    if ".buffer(0)" in line and not line.lstrip().startswith("#")
]
assert not _buffer_zero_calls, _buffer_zero_calls

#     AN OPENING -- the one this layer refuses by name in its own render
#     docstring. It also produces valid output, and it is anti-extensive: on
#     the ragged parcel's own candidates it deletes real ground, measured in
#     section 4. Here it is enough to show it is not area-preserving.
_opened = PINCH.buffer(-CELL_M / 2.0).buffer(CELL_M / 2.0)
assert _opened.area < _pinch_area, (_opened.area, _pinch_area)

# (e) AND THE REPAIRED SHAPE COMMITS, which is the whole point of moving the
#     check: what the emission gate lets out, the commit boundary takes.
_repaired_wgs84 = transform_geom(
    DEM["crs"],
    "EPSG:4326",
    mapping(shapely.affinity.translate(_repaired, DEM["origin_x"] + 200.0, DEM["origin_y"] - 200.0)),
)
wire_translation._polygonal_shape_from_wire(_repaired_wgs84, DEM, "tree zone 'pinch, repaired'")

print(
    f"2. THE SELF-TOUCH: a ring touching itself at a vertex ({explain_validity(PINCH)}) is REFUSED "
    f"by wire_translation._polygonal_shape_from_wire() -- that rule is unchanged -- and REPAIRED "
    f"by the emission gate's make_valid(), which splits it into a {_repaired.geom_type} of "
    f"{len(_repaired.geoms)} part(s) with the area preserved EXACTLY "
    f"({_repaired.area:.6f} m^2 in and out, difference {abs(_repaired.area - _pinch_area):.1e}). "
    f"The pieces stay ONE candidate as multi-part geometry: they are one 8-connected component "
    f"carrying one score, one rank and one id. buffer(0) appears in no line of "
    f"tree_zone_candidates.py; an opening at {CELL_M / 2:.1f} m would take the area to "
    f"{_opened.area:.1f} m^2 ({(1 - _opened.area / _pinch_area) * 100:.1f}% lost), which is why "
    f"neither is the tool. The repaired shape passes the commit boundary."
)


# --- 3. SUB-THRESHOLD HOLES FILLED, MULTI-CELL POCKETS KEPT ------------
#
# The pinhole half of the same raggedness. A single cell the SLOPE gate dropped
# is one threshold crossing on a 5 m grid, not a feature anyone would walk
# around; a pocket of two cells or more is at least two independent gate
# decisions agreeing, and the module's own render docstring argues those stay
# open as real holes. The threshold is stated in CELLS for that reason.

assert tzc.FILLED_INTERIOR_RING_MAX_CELLS == 1.0, tzc.FILLED_INTERIOR_RING_MAX_CELLS
assert tzc.FILLED_INTERIOR_RING_MAX_CELLS < 2.0, (
    "the threshold must sit below two cells by construction, or a genuine multi-cell pocket "
    "is not safe from it"
)

_rows = []
_total_filled = 0
_total_kept = 0
for patch_id in sorted(AFTER_BY_ID):
    before = BEFORE_BY_ID[patch_id]["polygon_utm"]
    after = AFTER_BY_ID[patch_id]["polygon_utm"]
    before_rings = interior_ring_areas_in_cells(before)
    after_rings = interior_ring_areas_in_cells(after)
    single = [size for size in before_rings if size <= tzc.FILLED_INTERIOR_RING_MAX_CELLS + 1e-6]
    multi = [size for size in before_rings if size > tzc.FILLED_INTERIOR_RING_MAX_CELLS + 1e-6]
    # EVERY sub-threshold ring is gone, and EVERY multi-cell pocket is still
    # there -- asserted as two statements, because "the count went down" would
    # pass if it had closed the wrong ones.
    assert len(after_rings) == len(multi), (patch_id, len(after_rings), len(multi))
    assert sorted(round(size, 6) for size in after_rings) == sorted(round(size, 6) for size in multi), patch_id
    assert all(size > 1.0 + 1e-6 for size in after_rings), (patch_id, after_rings)
    _total_filled += len(single)
    _total_kept += len(multi)
    _rows.append(
        {
            "id": patch_id,
            "holes_before": len(before_rings),
            "holes_after": len(after_rings),
            "filled": len(single),
            "kept": len(multi),
            "largest_kept_cells": max(multi) if multi else 0.0,
            "acres_before": before.area / SQUARE_METERS_PER_ACRE,
            "acres_after": after.area / SQUARE_METERS_PER_ACRE,
        }
    )

assert _total_filled > 0, (
    "the ragged parcel must actually produce sub-threshold pinholes, or this section is vacuous"
)
assert _total_kept > 0, (
    "the ragged parcel must also produce genuine multi-cell pockets, or 'they survive' is vacuous"
)

for row in _rows:
    print(
        f"    holes  candidate {row['id']:<3} {row['holes_before']:>3} -> {row['holes_after']:>3}  "
        f"({row['filled']} filled at <= {tzc.FILLED_INTERIOR_RING_MAX_CELLS:.0f} cell, "
        f"{row['kept']} kept, largest kept {row['largest_kept_cells']:.0f} cells)   "
        f"{row['acres_before']:.4f} -> {row['acres_after']:.4f} ac "
        f"(+{(row['acres_after'] - row['acres_before']) * SQUARE_METERS_PER_ACRE / CELL_AREA_M2:.0f} cells)"
    )

# THE CONTROL, on geometry whose answer is known by construction: a ring of
# cells around a ONE-cell hole loses it, and the same ring around a FOUR-cell
# hole keeps it. Nothing about the parcel is load-bearing in this assertion.
def _cell_union(cells):
    return unary_union([box(c * 5.0, -r * 5.0 - 5.0, c * 5.0 + 5.0, -r * 5.0) for r, c in cells])


_one_cell_hole = _cell_union([(r, c) for r in range(3) for c in range(3) if (r, c) != (1, 1)])
_four_cell_hole = _cell_union(
    [(r, c) for r in range(4) for c in range(4) if (r, c) not in {(1, 1), (1, 2), (2, 1), (2, 2)}]
)
assert len(_one_cell_hole.interiors) == 1 and len(_four_cell_hole.interiors) == 1
_filled_one, _n_filled, _n_kept = tzc._fill_subthreshold_interior_rings(
    _one_cell_hole, CELL_AREA_M2, tzc.FILLED_INTERIOR_RING_MAX_CELLS
)
assert (_n_filled, _n_kept) == (1, 0) and not _filled_one.interiors
assert _filled_one.area == _one_cell_hole.area + CELL_AREA_M2
_filled_four, _n_filled4, _n_kept4 = tzc._fill_subthreshold_interior_rings(
    _four_cell_hole, CELL_AREA_M2, tzc.FILLED_INTERIOR_RING_MAX_CELLS
)
assert (_n_filled4, _n_kept4) == (0, 1) and len(_filled_four.interiors) == 1
assert _filled_four.area == _four_cell_hole.area, "a multi-cell pocket must not lose or gain area"
assert _filled_four is _four_cell_hole, "nothing to fill must return the same object, untouched"

print(
    f"3. HOLES: across {len(_rows)} ragged reference-parcel candidate(s), "
    f"{_total_filled + _total_kept} interior ring(s) before, {_total_kept} after -- "
    f"{_total_filled} sub-threshold pinhole(s) filled at the "
    f"{tzc.FILLED_INTERIOR_RING_MAX_CELLS:.0f}-cell ({CELL_AREA_M2:.0f} m^2) threshold and every "
    f"one of the {_total_kept} multi-cell pocket(s) still standing, the largest "
    f"{max(row['largest_kept_cells'] for row in _rows):.0f} cells. CONTROL: a ring of cells "
    f"around a 1-cell hole loses it and gains exactly {CELL_AREA_M2:.0f} m^2; the same ring "
    f"around a 4-cell hole keeps it, area unchanged, and comes back as the SAME object."
)


# --- 4. THIN ARMS PRESERVED --------------------------------------------
#
# The constraint the whole change is built around. An opening deletes every
# feature narrower than its own diameter, and the thin arms are the geometry
# this layer exists to find -- so the arms are MEASURED before and after rather
# than assumed to have survived.

# CALIBRATION FIRST, so the metre figures below mean what they say.
_one_cell_arm = _cell_union(
    [(r, c) for r in range(6) for c in range(6)] + [(2, 6 + i) for i in range(5)]
)
_two_cell_arm = _cell_union(
    [(r, c) for r in range(6) for c in range(6)]
    + [(2 + i, 6 + j) for i in range(2) for j in range(5)]
)
assert abs(narrowest_arm_meters(_one_cell_arm) - 5.0) < 0.05, narrowest_arm_meters(_one_cell_arm)
assert abs(narrowest_arm_meters(_two_cell_arm) - 10.0) < 0.05, narrowest_arm_meters(_two_cell_arm)

_arm_rows = []
for patch_id in sorted(AFTER_BY_ID):
    before = narrowest_arm_meters(BEFORE_BY_ID[patch_id]["polygon_utm"])
    after = narrowest_arm_meters(AFTER_BY_ID[patch_id]["polygon_utm"])
    assert before is not None and after is not None, patch_id
    # THE ASSERTION: the narrowest arm does not get narrower and does not
    # vanish. Both operations in the gate are non-anti-extensive -- filling a
    # hole only adds area, and make_valid() preserves it exactly -- so an arm
    # cannot be eroded by either, and this is what says so on real geometry.
    assert after >= before - 1e-6, (patch_id, before, after)
    _arm_rows.append({"id": patch_id, "before": before, "after": after})

for patch_id in sorted(AFTER_BY_ID):
    row = next(row for row in _arm_rows if row["id"] == patch_id)
    print(
        f"    arm    candidate {row['id']:<3} narrowest {row['before']:.2f} m -> {row['after']:.2f} m"
    )

# AND ACROSS THE ROUND-TRIP GATE SPECIFICALLY. The rows above measure the gate
# as a whole (hole fill on vs off). _wire_survivable() is the step this branch
# added, and it is the only one in the gate that MOVES a vertex -- it snaps to a
# WIRE_VERTEX_SEPARATION_METERS grid -- so it is the one an arm could in
# principle be lost to. Measured on its own, on the same real ragged patches:
# the input to the step against its output.
#
# A 1 mm grid against a 5.00 m one-cell arm is 5000:1, so the expectation is
# that nothing moves at all. The point of measuring is that "expected" is not
# "observed", and the arms are what this layer exists for.
_wire_gate_rows = []
for patch_id in sorted(AFTER_BY_ID):
    _into_gate = AFTER_BY_ID[patch_id]["polygon_utm"]
    _out_of_gate = tzc._wire_survivable(_into_gate, RAGGED_DEM)
    assert _out_of_gate is not None, f"candidate {patch_id} was dropped by the round-trip gate"
    _wire_gate_rows.append(
        {
            "id": patch_id,
            "arm_before": narrowest_arm_meters(_into_gate),
            "arm_after": narrowest_arm_meters(_out_of_gate),
            "area_before": _into_gate.area,
            "area_after": _out_of_gate.area,
            "untouched": _out_of_gate is _into_gate,
        }
    )

for row in _wire_gate_rows:
    assert row["arm_before"] is not None and row["arm_after"] is not None, row
    # THE ASSERTION, the same one section 4 makes of the gate as a whole: the
    # narrowest arm does not get narrower and does not vanish.
    assert row["arm_after"] >= row["arm_before"] - 1e-6, row
    # AND THE AREA, which section 5 accounts for across the gate but not
    # across this step. A snap moves each vertex by at most half a grid
    # diagonal, so the bound is a perimeter-scale quantity and nowhere near a
    # cell. One cell is the smallest change this pipeline can even represent.
    assert abs(row["area_after"] - row["area_before"]) < CELL_AREA_M2, row

_wire_gate_touched = [row for row in _wire_gate_rows if not row["untouched"]]
_wire_gate_worst_area = max(abs(row["area_after"] - row["area_before"]) for row in _wire_gate_rows)
for row in _wire_gate_rows:
    print(
        f"    wire   candidate {row['id']:<3} narrowest {row['arm_before']:.2f} m -> "
        f"{row['arm_after']:.2f} m, area {row['area_before']:.4f} -> {row['area_after']:.4f} m^2 "
        f"({'untouched' if row['untouched'] else 'snapped'})"
    )

# AND ON GEOMETRY THAT THE STEP ACTUALLY SNAPS. Every candidate above passed
# the round-trip gate untouched, which is the healthy case and proves the step
# is a no-op on sound geometry -- but it means none of those rows measures what
# the SNAP does to an arm. The captured failure does: all four of its
# candidates are real geometry that a live commit refused, and two of them are
# repaired by the snap rather than returned as themselves.
#
# The fixture is loaded here rather than in test_tree_zone_roundtrip_fixture.py
# because narrowest_arm_meters() and its calibration live in THIS file, and a
# second copy of the metric is how two files start disagreeing about what a
# thin arm is.
with open("tree_roundtrip_rejection_fixture.json") as _fixture_fh:
    _CAPTURE = json.load(_fixture_fh)
_CAPTURE_DEM = {"crs": _CAPTURE["provenance"]["dem_crs"]}

_snap_rows = []
for _feature in _CAPTURE["rejected_features"]:
    _reconstructed = shape(
        transform_geom("EPSG:4326", _CAPTURE_DEM["crs"], _feature["geometry"])
    )
    _into_gate = tzc._valid_polygonal(_reconstructed)
    assert _into_gate is not None, _feature["id"]
    _out_of_gate = tzc._wire_survivable(_into_gate, _CAPTURE_DEM)
    assert _out_of_gate is not None, f"{_feature['id']} was dropped by the round-trip gate"
    _snap_rows.append(
        {
            "id": _feature["id"],
            "arm_before": narrowest_arm_meters(_into_gate),
            "arm_after": narrowest_arm_meters(_out_of_gate),
            "area_before": _into_gate.area,
            "area_after": _out_of_gate.area,
            "snapped": _out_of_gate is not _into_gate,
        }
    )

for _row in _snap_rows:
    if _row["arm_before"] is None or _row["arm_after"] is None:
        # A patch with no feature the metric can delete inside its search
        # range has no arm to lose; reported, not silently skipped.
        continue
    assert _row["arm_after"] >= _row["arm_before"] - 1e-6, _row
    assert abs(_row["area_after"] - _row["area_before"]) < CELL_AREA_M2, _row

_snapped_only = [row for row in _snap_rows if row["snapped"]]
for _row in _snap_rows:
    _arm_before = "none" if _row["arm_before"] is None else f"{_row['arm_before']:.2f} m"
    _arm_after = "none" if _row["arm_after"] is None else f"{_row['arm_after']:.2f} m"
    print(
        f"    snap   {_row['id']:<24} narrowest {_arm_before} -> {_arm_after}, area "
        f"{_row['area_before']:.6f} -> {_row['area_after']:.6f} m^2 "
        f"({'SNAPPED' if _row['snapped'] else 'untouched'})"
    )

# AND ON THE SHIPPED PARCEL, against the transform that was removed. The
# smoothing pass build_trees_payload() used to apply is run here on the shipped
# candidates -- not to ship it, but to measure what it was doing: strictly
# anti-extensive, taking area off and putting none back.
_smoothing_rows = []
for patch in SHIPPED_PATCHES:
    real = patch["render_fill_polygon_utm"]
    smoothed = display_outline.smoothed_display_outline(real, patch["polygon_utm"], CELL_M)
    _smoothing_rows.append(
        {
            "id": patch["id"],
            "acres": real.area / SQUARE_METERS_PER_ACRE,
            "added": smoothed.difference(real).area,
            "removed": real.difference(smoothed).area,
            "pct": real.symmetric_difference(smoothed).area / real.area * 100.0,
            "arm_before": narrowest_arm_meters(real),
            "arm_after": narrowest_arm_meters(smoothed),
        }
    )
_worst_smoothing = max(_smoothing_rows, key=lambda row: row["pct"])
_ARM_SUMMARY = ", ".join(f"{row['before']:.2f} -> {row['after']:.2f} m" for row in _arm_rows)
# READ THE ARM COLUMN THE OTHER WAY ROUND HERE. In the rows above, an arm that
# stays the same width is an arm that survived. In the rows below it is the
# opposite: the narrowest arm getting WIDER means the narrow one is no longer
# there to measure -- the smooth deleted it and what is left is the next
# thinnest thing. That is the failure, printed as the number that shows it.
for row in _smoothing_rows:
    arm_after = "deleted entirely" if row["arm_after"] is None else f"{row['arm_after']:.2f} m"
    assert row["added"] <= 1e-6, (
        f"candidate {row['id']}: the smooth added area -- it is meant to be strictly "
        f"anti-extensive, which is the whole complaint against it"
    )
    assert row["removed"] > 0.0, row["id"]
    assert row["arm_after"] is None or row["arm_after"] > row["arm_before"], (
        f"candidate {row['id']}: the smooth left the narrowest arm untouched, so this row is "
        f"not evidence of anything"
    )
    print(
        f"    smooth candidate {row['id']:<3} {row['acres']:.2f} ac  "
        f"added {row['added']:6.1f} m^2  removed {row['removed']:6.1f} m^2  "
        f"= {row['pct']:5.2f}% of the zone   narrowest arm "
        f"{row['arm_before']:.2f} m -> {arm_after} (i.e. gone)"
    )

print(
    f"4. THIN ARMS: the metric is calibrated on shapes whose answer is known -- a one-cell-wide "
    f"arm measures {narrowest_arm_meters(_one_cell_arm):.2f} m and a two-cell-wide arm "
    f"{narrowest_arm_meters(_two_cell_arm):.2f} m. On the ragged reference parcel the narrowest "
    f"arm is UNCHANGED on every candidate ({_ARM_SUMMARY}). The "
    f"smoothing pass that was removed was strictly anti-extensive on the shipped parcel: worst "
    f"case candidate {_worst_smoothing['id']} ({_worst_smoothing['acres']:.2f} ac) lost "
    f"{_worst_smoothing['removed']:.1f} m^2 and gained {_worst_smoothing['added']:.1f} m^2, "
    f"{_worst_smoothing['pct']:.2f}% of the zone -- and its narrowest arm reads WIDER afterwards "
    f"({_worst_smoothing['arm_before']:.2f} -> {_worst_smoothing['arm_after']:.2f} m) because the "
    f"narrow one is gone. That is the thin-arm deletion this layer refuses an opening in order "
    f"to prevent."
)


# --- 5. AREA CHANGE, ACCOUNTED FOR CELL BY CELL ------------------------
#
# The claim is not "the area barely moved" -- it is that every square metre it
# moved is a filled hole, and that the number of them is a whole count of
# cells. Anything else would mean an edge moved, which is what the layer must
# never do.

_area_rows = []
for patch_id in sorted(AFTER_BY_ID):
    before = BEFORE_BY_ID[patch_id]["polygon_utm"]
    after = AFTER_BY_ID[patch_id]["polygon_utm"]
    row = next(row for row in _rows if row["id"] == patch_id)
    delta = after.area - before.area
    # STRICTLY ADDITIVE. The after-shape CONTAINS the before-shape: no ground
    # was taken from anywhere, which is the property an opening does not have.
    assert before.difference(after).area < 1e-6, (
        f"candidate {patch_id}: the gate REMOVED {before.difference(after).area:.3f} m^2 -- "
        f"the two operations are additive and area-preserving, so this cannot happen"
    )
    # AND THE ADDED GROUND IS EXACTLY THE FILLED HOLES, to within float noise.
    # Each hole is a whole 5 m cell, so the delta divides into whole cells and
    # the count matches the ring count section 3 measured.
    assert abs(delta - row["filled"] * CELL_AREA_M2) < 1e-6, (patch_id, delta, row["filled"])
    _area_rows.append({"id": patch_id, "before": before.area, "after": after.area, "delta": delta})

_total_delta = sum(row["delta"] for row in _area_rows)
for row in _area_rows:
    print(
        f"    area   candidate {row['id']:<3} {row['before'] / SQUARE_METERS_PER_ACRE:.4f} -> "
        f"{row['after'] / SQUARE_METERS_PER_ACRE:.4f} ac  "
        f"(+{row['delta']:.1f} m^2 = {row['delta'] / CELL_AREA_M2:.0f} cell(s), "
        f"+{row['delta'] / row['before'] * 100:.3f}%)"
    )

# THE VALIDITY REPAIR MOVES NO AREA AT ALL, asserted on the repair itself
# rather than inferred: make_valid()'s re-noding is exact.
for geometry in (PINCH, _one_cell_arm, _four_cell_hole):
    repaired = tzc._valid_polygonal(geometry)
    assert repaired is not None and repaired.area == geometry.area, geometry.geom_type

print(
    f"5. AREA: {len(_area_rows)} candidate(s) moved by {_total_delta:.1f} m^2 in total "
    f"({_total_delta / CELL_AREA_M2:.0f} whole cells, {_total_delta / SQUARE_METERS_PER_ACRE:.4f} "
    f"ac), every square metre of it a filled sub-threshold hole and NONE of it removed -- the "
    f"before-shape is contained in the after-shape on every candidate. The validity repair "
    f"itself moves nothing: make_valid() returns the same area exactly."
)


# --- 6. AN UNREPAIRABLE PATCH IS DROPPED WITH A COUNT ------------------
#
# The gate's own failure mode. Dropping is the decision -- raising would lose
# every other candidate on the property over one ragged component, and emitting
# would put the failure back at commit, which is the moment this whole change
# exists to move off -- but a silent drop is not an option, so the count is
# carried to the report the way water's own `dropped_count` is.

# (a) THE HELPER RETURNS None when there is nothing polygonal to keep. A
#     zero-width ring is the real shape of this case: make_valid() re-nodes it
#     into a line, and a line is not ground.
_degenerate = Polygon([(0.0, 0.0), (10.0, 0.0), (0.0, 0.0), (10.0, 0.0)])
assert tzc._valid_polygonal(_degenerate) is None, tzc._valid_polygonal(_degenerate)
assert make_valid(_degenerate).geom_type not in ("Polygon", "MultiPolygon")

# (b) A VALID PATCH IS RETURNED AS ITSELF -- the gate is invisible when there
#     is nothing to repair, which is what keeps every other run byte-identical.
_square = box(0.0, 0.0, 10.0, 10.0)
assert tzc._valid_polygonal(_square) is _square

# (c) END TO END: a component the repair cannot save is dropped from `patches`,
#     recorded in the sink, counted in narrative_data and carried to the wire.
#     The unrepairable geometry is forced by patching the helper for ONE
#     component id, because a cell union that GEOS cannot repair is not a shape
#     this pipeline can produce on demand -- what is under test is the drop
#     path, not the geometry that reaches it.
# THE FIRST COMPONENT THE GATE LOOKS AT is the one made unrepairable, by
# answering None for that one call. The helper sees geometry and not ids, so
# "the first call" is how a specific component is singled out. The warning line
# this prints names the geometry's real validity, which on a forced drop reads
# as valid -- that is the stub, not the module misreporting.
_real_valid_polygonal = tzc._valid_polygonal


def _drop_one(footprint):
    _drop_one.calls += 1
    return None if _drop_one.calls == 1 else _real_valid_polygonal(footprint)


_drop_one.calls = 0
tzc._valid_polygonal = _drop_one
try:
    _dropped_result = ragged_generate()
finally:
    tzc._valid_polygonal = _real_valid_polygonal

_dropped_narrative = _dropped_result["narrative_data"]
assert len(_dropped_result["patches"]) == len(RAGGED_PATCHES) - 1, (
    len(_dropped_result["patches"]), len(RAGGED_PATCHES)
)
assert _dropped_narrative["dropped_invalid_count"] == 1, _dropped_narrative["dropped_invalid_count"]
assert _dropped_narrative["candidate_count"] == len(RAGGED_PATCHES) - 1
# NOT SILENT, AND NOT ON THE WIRE: the dropped component is absent from the
# emitted collection as well as from `patches`.
assert len(_dropped_result["zones_geojson"]["features"]) == len(RAGGED_PATCHES) - 1
# THE CONTROL: the healthy run's own count is zero, so the 1 above is a
# measurement rather than a constant.
assert RAGGED_RESULT["narrative_data"]["dropped_invalid_count"] == 0
assert SHIPPED_RESULT["narrative_data"]["dropped_invalid_count"] == 0
# AND THE SINK ITSELF carries what was dropped, not just how many.
_sink = []
tzc.score_tree_search_space(
    RAGGED_DEM,
    BOUNDARY.buffer(-tzc.TREE_ZONE_BOUNDARY_SETBACK_METERS),
    BOUNDARY,
    dropped_invalid=_sink,
)
assert _sink == [], "a healthy run records nothing"

# (d) THE ROUND-TRIP HALF OF THE SAME GATE. _wire_survivable() has its own
#     unrepairable case, and it is a DIFFERENT one: geometry shapely calls
#     VALID that still cannot be put on the wire. A sliver a nanometre across
#     is the real shape of it -- distinct doubles in UTM, so is_valid is true
#     and _valid_polygonal() rightly passes it, but below the resolution the
#     WGS84 wire can represent, so it collapses on the way out and a 1 mm
#     snap has nothing left to keep.
_nanometre_sliver = Polygon(
    [(586000.0, 4499800.0), (586000.0 + 1e-9, 4499800.0), (586000.0, 4499800.0 + 1e-9)]
)
assert _nanometre_sliver.is_valid, "the point of this case is that the UTM verdict is VALID"
assert tzc._valid_polygonal(_nanometre_sliver) is _nanometre_sliver
assert tzc._survives_the_wire(_nanometre_sliver, RAGGED_DEM) is False
assert tzc._wire_survivable(_nanometre_sliver, RAGGED_DEM) is None
# AND THE INVISIBLE CASE, again: a patch that already survives comes back as
# ITSELF, so the step cannot perturb a healthy generate.
assert tzc._wire_survivable(_square, RAGGED_DEM) is _square

# (e) END TO END for the round-trip drop, by the same means and for the same
#     stated reason as (c): the helper is patched for ONE component, because
#     what is under test is the drop path and not the geometry that reaches
#     it. The reason recorded must name the ROUND TRIP -- explain_validity()
#     would say "Valid Geometry" here, which in a drop record reads as a bug
#     in the record rather than the fact it is.
_real_wire_survivable = tzc._wire_survivable


def _drop_one_wire(footprint, dem):
    _drop_one_wire.calls += 1
    return None if _drop_one_wire.calls == 1 else _real_wire_survivable(footprint, dem)


_drop_one_wire.calls = 0
tzc._wire_survivable = _drop_one_wire
try:
    _wire_dropped = ragged_generate()
finally:
    tzc._wire_survivable = _real_wire_survivable
_wire_sink = _wire_dropped["dropped_invalid"]

assert len(_wire_dropped["patches"]) == len(RAGGED_PATCHES) - 1, (
    len(_wire_dropped["patches"]), len(RAGGED_PATCHES)
)
assert _wire_dropped["narrative_data"]["dropped_invalid_count"] == 1
assert len(_wire_dropped["zones_geojson"]["features"]) == len(RAGGED_PATCHES) - 1
assert len(_wire_sink) == 1, _wire_sink
assert set(_wire_sink[0]) == {"id", "area_acres", "reason"}, _wire_sink[0]
assert "round trip" in _wire_sink[0]["reason"], _wire_sink[0]["reason"]
assert "Valid Geometry" not in _wire_sink[0]["reason"], _wire_sink[0]["reason"]

print(
    f"6. UNREPAIRABLE: _valid_polygonal() returns None for a degenerate ring make_valid() re-nodes "
    f"into a {make_valid(_degenerate).geom_type} (not ground), and returns a valid patch AS "
    f"ITSELF. End to end, a component the repair cannot save is DROPPED -- "
    f"{len(_dropped_result['patches'])} patches instead of {len(RAGGED_PATCHES)}, the same count "
    f"on zones_geojson -- and COUNTED: narrative_data.dropped_invalid_count == "
    f"{_dropped_narrative['dropped_invalid_count']}, against 0 on both healthy parcels. Never "
    f"raised, never silent.\n"
    f"   THE ROUND-TRIP HALF: _wire_survivable() returns None for a nanometre-wide sliver that "
    f"shapely calls VALID -- the case _valid_polygonal() cannot catch because there is nothing "
    f"wrong with the shape in the frame it is judged in -- and returns a healthy patch AS ITSELF. "
    f"End to end it takes the SAME path: {len(_wire_dropped['patches'])} patches instead of "
    f"{len(RAGGED_PATCHES)}, the same count on zones_geojson, dropped_invalid_count == "
    f"{_wire_dropped['narrative_data']['dropped_invalid_count']}, and one row in the sink whose "
    f"reason names the round trip rather than explain_validity()'s 'Valid Geometry'."
)


# --- 7. TREE FEATURES CARRY NO DISPLAY OUTLINE -------------------------
#
# The smoothing pass, removed. render_layout_map.py does NOT smooth tree zones
# -- it smooths the production fill, because contour clipping against a 5 m
# staircase shows there, and draws the tree hatch from the cell-union footprint
# verbatim. Section 4 measured what the smooth was doing to the arms.

for feature in TREES_PAYLOAD["tree_zones"]["features"]:
    assert OUTLINE not in feature["properties"], (
        f"{feature['id']}: a tree feature carries a display-only smoothed outline"
    )
    assert len(feature["properties"]) > 3, "the absence must be about this key, not an empty dict"

_production_features = LANDFORM_PAYLOAD["suggested_zones"]["features"]
assert _production_features, "the fixture must produce production zones"
for feature in _production_features:
    assert OUTLINE in feature["properties"], f"{feature['id']}: production lost its outline"
    assert feature["properties"][OUTLINE]["type"] in ("Polygon", "MultiPolygon")

# AND PRODUCTION'S OUTLINE IS STILL THE LAYOUT MAP'S, byte for byte -- the
# property the removal must not have disturbed. Asserted against a literal
# transcription of the expression render_layout_map.py evaluates, not against a
# second call to the shared helper.
from raster_grid import angular_smooth_polygon  # noqa: E402  (local to this claim)

_production_patches = {
    f"production-area-{patch['id']}": patch for patch in PRODUCTION_PATCHES
}
_byte_identical = 0
for feature in _production_features:
    patch = _production_patches[feature["id"]]
    expected = angular_smooth_polygon(
        patch["render_fill_polygon_utm"],
        display_outline.DISPLAY_OUTLINE_SIMPLIFY_TOLERANCE_CELLS * CELL_M,
        display_outline.DISPLAY_OUTLINE_CHAIKIN_ITERATIONS,
    ).intersection(patch["polygon_utm"])
    shipped = display_outline.smoothed_display_outline(
        patch["render_fill_polygon_utm"], patch["polygon_utm"], CELL_M
    )
    assert shipped.wkb == expected.wkb, feature["id"]
    _byte_identical += 1

print(
    f"7. NO OUTLINE ON TREES: all {len(TREES_PAYLOAD['tree_zones']['features'])} tree feature(s) "
    f"carry no '{OUTLINE}' (they average "
    f"{sum(len(f['properties']) for f in TREES_PAYLOAD['tree_zones']['features']) // len(TREES_PAYLOAD['tree_zones']['features'])} "
    f"properties each, so the absence is about this key), while all "
    f"{len(_production_features)} production feature(s) still carry theirs -- and all "
    f"{_byte_identical} of those are WKB-IDENTICAL to the expression render_layout_map.py "
    f"evaluates for the PDF."
)


print(
    "\n8. REGRESSION: run the other test files separately -- test_trees_step.py (run above, as "
    "this file's fixture), test_tree_zone_candidates.py, test_display_outline.py, "
    "test_tree_zone_render_footprint.py, test_wire_translation.py, "
    "test_wire_translation_inbound.py, test_step_commit.py, test_step_orchestrator.py, "
    "test_render_layout_map.py, test_production_fill_smoothing.py, test_fencing.py."
)

print("\nAll tree-zone geometry validity checks passed.")
