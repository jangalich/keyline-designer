"""
exclusion_zones.py

The parcel's UNSELECTABLE GROUND, as a first-class KSOP layer: the five
reasons a piece of this property cannot carry a production zone, each
computed as its own cell mask, each morphologically CLOSED at its own
measured radius, and the five unioned into one geometry that renders on
the layout map alongside every other layer and carries its own
narrative_data.

The five layers, and the gate each one mirrors:

    canopy   -- woody-vegetation root zone (canopy_height_data.
                tree_root_zone_mask(), at TREE_ROOT_ZONE_BUFFER_METERS)
    slope    -- ground steeper than max_slope_pct, PLUS ground with no
                DEM coverage at all (see NODATA below)
    hydric   -- disqualifying (hydric) SSURGO soil
    roads    -- existing road right-of-way, at ROAD_EXCLUSION_BUFFER_METERS
    setback  -- the ring inside PRODUCTION_BOUNDARY_SETBACK_METERS of the
                drawn boundary

--- LAYER PLACEMENT: THIS IS LAYER 2, AND THE FIRST LAYER 2 STEP ---

It is NOT Layer 1. Layer 1 in this codebase means network-fetched RAW
data behind a hard-fail gate (parcel_data.py's twelve mandatory raw
layers). This module fetches no raw layer of its own -- it DERIVES from
four things Layer 1 already produced: `dem`, the canopy root-zone mask,
the disqualifying-soil union, and the road-exclusion union. A reader
looking for a fetch in here will not find one; the three gate fetches
below are calls to production_area.py's own shared fetch helpers, the
same ones every other consumer of those layers calls, not new sources.

It runs FIRST among the Layer 2 steps -- before production areas rather
than after them -- because it depends only on Layer 1 products and on
nothing any other Layer 2 step computes. Placing it first is also what
makes the deferred production integration below a one-line wiring
change rather than a reordering.

--- DELIBERATE, TIME-LIMITED REDUNDANCY (READ THIS BEFORE "FIXING" IT) ---

This module computes the same five gates production_area.
compute_step1_eligible_cells() computes, and production_area.py is NOT
modified: it keeps computing its own gates exactly as it does today.
Two modules, the same five gates, on the same DEM, in the same pipeline
run. That is the OPPOSITE of the redundancy this pipeline's
architecture normally eliminates (see pipeline_context.py's whole
reason for existing), and it is deliberate, not an oversight.

WHY IT IS DELIBERATE. Feeding these CLOSED exclusions into production
would change production's results. A closing is EXTENSIVE -- it only
ever adds excluded cells -- so production would lose the pinhole cells
the closing absorbs (measured at roughly 0.35 ac on the OLD reference
boundary and 0.60 ac on the NEW one). Those cells are stranded specks
of selectable ground sitting inside excluded terrain, and there is a
real argument that production should never have been able to claim
them. But that is a decision to make once these radii are validated on
terrain beyond the two reference boundaries -- not one to bundle into
the branch that introduces the module.

THE OPEN QUESTION, NAMED: should compute_step1_eligible_cells() take
this module's `eligible_mask` as an optional override (the standard
override pattern every other cross-module input in this pipeline uses),
falling back to self-compute when it isn't supplied? If yes, this
module's `eligible_mask` return key is already the thing to wire in and
the redundancy below collapses to one computation. If no, this module
should stop computing gates and start reading production's per-gate hit
masks instead, the way diagnose_exclusion_footprints.py does. Either
resolution ends the duplication; leaving it as-is indefinitely is not
one of the options.

THE COST, MEASURED -- and what the measurement actually counts: calls
to the shared GATE HELPERS (get_required_tree_root_zone_mask_utm(),
_fetch_disqualifying_soil_union(), _fetch_road_exclusion_union_utm(),
compute_slope_percent()) at this module's and production's bindings,
NOT every road/canopy/soil touch in the whole pipeline (e.g. build_
pipeline_context()'s own separate existing_roads fetch is outside this
count). Because production still self-computes, one build_pipeline_
context() run reaches the canopy and soil gate helpers TWICE and
computes the slope grid TWICE -- once here, once in production. The
ROAD gate helper is the exception since build_pipeline_context()
started passing its own already-fetched road union into this module
(road_exclusion_union_utm= -- see identify_exclusion_zones()'s
OVERRIDES section): this module's road self-fetch no longer fires on
that path, so the helper runs exactly ONCE (production's own
self-compute; production is untouched and keeps it). test_exclusion_
zones.py asserts these exact counts, not upper bounds: one more of any
would mean something is re-fetching beyond the known duplication and
the override pattern is failing somewhere else.

--- CLOSING: PER-GATE RADII, MEASURED NOT GUESSED ---

Each gate closes at its OWN radius (the five constants below), because
diagnose_exclusion_footprints.py measured all five layers on both
reference boundaries, raw and closed at 5 m and 10 m, and they do not
behave alike:

    canopy   18 -> 13 polygons, +0.117 ac (OLD) / +0.228 ac (NEW)  -> close
    slope    19 -> 12 (OLD) and 32 -> 18 (NEW), +0.234 / +0.370 ac -> close
    hydric   3 -> 3 / 1 -> 1, +0.000 / +0.012 ac                   -> no
    roads    0 cells on both boundaries                            -> no
    setback  43 -> 43 / 41 -> 41, +0.000 ac on both                -> NO

Slope is both the largest exclusion (3.165 ac / 19 polygons OLD, 4.062
ac / 32 NEW, against canopy's 1.030 and 2.416) and the most fragmented,
and it gains the most from closing -- absorbing 7 and 14 polygons, with
the largest single gained region only 6 and 8 cells. That is PINHOLE
ABSORPTION, not region merging, which is exactly what makes it safe.

THE SETBACK MUST NOT BE CLOSED, and its 0.0 is a measured decision, not
an untuned placeholder. At 10 m the diagnostic raised OVER-MERGE SHAPE
on both boundaries -- a single region taking 100% and 83% of the whole
gain. The setback is a RING, fragmented into 41-43 pieces by the cell
grid alone; closing a ring does not tidy it up, it bridges across the
middle of the parcel. At 5 m it gains nothing at all. Both radii are
wrong for it, in opposite ways.

QUANTIZATION. At the pipeline's 5 m DEM resolution these radii quantize
hard: 5 m is a ONE-cell disc and there is no gentler setting available
(raster_grid.closing_radius_cells() rounds, so 2 m is 0 cells -- a
no-op, honestly reported rather than inflated to a full cell). If a 5 m
closing ever proves too aggressive on other terrain, the answer is NOT
a smaller radius, because there isn't one. It is a different operation.

--- NODATA: "TOO STEEP" AND "NOT MEASURED" ARE DIFFERENT FACTS ---

A cell with no DEM coverage has slope_pct = NaN, fails
`slope_pct <= max_slope_pct`, and therefore lands in the slope layer --
which is what production does too, so the layer matches the gate. But
it means the slope layer is "fails OR cannot be evaluated for slope",
not purely "too steep". narrative_data reports the NaN share
SEPARATELY (`nan_slope_acres`) so a narrative can never conflate the
two. Same split diagnose_exclusion_footprints.py already makes.

--- render_fill_polygon_utm IS NOT BOUNDED HERE. THIS INVERTS. ---

For a production zone, render_fill_polygon_utm is a morphological
OPENING clipped to polygon_utm, and production_area.cluster_and_gate()
asserts `render_fill_polygon_utm.area <= polygon_utm.area` -- the
render geometry can never exceed the real footprint. DO NOT COPY THAT
ASSERTION HERE. It is backwards for this module and it will look
missing to anyone reading the two files side by side.

An opening is ANTI-EXTENSIVE: it removes, so there is always a larger
true footprint to bound it against. A closing is EXTENSIVE: the closed
geometry is deliberately LARGER than the raw union, and there is no
smaller footprint to clip back to, because the closed geometry IS the
answer -- the pinholes it absorbed are the point of the operation, not
overreach to be trimmed. The only clip that applies is to
boundary_polygon_utm: exclusions are a fact about THIS parcel.

The invariant is therefore the INVERSE, and it is asserted in both
directions in test_exclusion_zones.py:

    raw_excluded_union_utm  ⊆  render_fill_polygon_utm  ⊆  boundary_polygon_utm

--- EXCLUSION SMOOTHING: MEASURED AND REJECTED ---

render_fill_polygon_utm is a 5 m cell-union staircase and reads as one.
A branch set out to fix that by running the angular-simplify + Chaikin
pass (raster_grid.angular_smooth_polygon(), the same treatment the
production fill already gets) over the closed union, and deriving
eligible_polygon_utm from the smoothed result so the two layers would
share a boundary by construction.

IT WAS NOT APPLIED, and the reason is measured, not aesthetic. The
design rested on the direction of error being the safe one: Chaikin
pushes outward at reflex vertices, and for an exclusion outward means
MORE excluded -- over-exclude by a metre rather than leave ground with
trees on it selectable. That premise is wrong here, for two compounding
reasons (both measured in test_exclusion_smoothing.py):

  A closed-ring Chaikin pass is net area-REDUCING, always. Corner-
  cutting does push outward at reflex vertices and inward at convex
  ones, but a simple closed ring turns a net +360 degrees, so the
  convex cuts always outweigh the reflex ones. There is no shape for
  which it comes out net-outward. The intuition transfers from the
  OPEN-polyline case (a road corridor), where it is fine.

  The simplify pass amplifies exactly that term. Chaikin's cut is
  proportional to the length of the edges meeting at a vertex.
  Collapsing the staircase's collinear runs is what makes the shape
  readable, and it is also what turns hundreds of 5 m edges into a few
  long ones -- so every corner cut afterwards removes a far bigger
  triangle. Run alone neither pass moves the union more than 1.3%;
  composed in the only sensible order they move it 4.7%.

On a reference-shaped fixture (7.7 ac across 18 polygons) at the
GENTLEST supportable settings -- one DEM cell of tolerance, one Chaikin
pass, with nothing gentler available -- the smoothed union cuts 2652 m²
= 0.655 acres INSIDE the closed union. That is gate-excluded ground
republished as selectable. For scale, the largest gain any closing
radius in this module produces on the reference boundaries is +0.370
acres, and every one of those radii was measured at cell-level
precision. Smoothing would give back 1.8x what all five closings
together were tuned to add, in the one direction this layer cannot
afford to be wrong in.

The obvious repair does not work either: unioning the smoothed result
back with the closed union makes containment exact, but it restores the
original steps everywhere the smooth cut inward and keeps the smoothed
arcs everywhere else, leaving 122% of the vertex count of the staircase
it was meant to remove.

What DID survive the branch is the half that was sound independently:
eligible_polygon_utm is DERIVED as boundary_polygon_utm minus the
published exclusion rather than computed on its own. Two layers derived
from one geometry share a boundary by construction -- no sliver
belonging to neither, no overlap claimed by both -- and that is a
property of deriving, not of smoothing, so it holds for the exact
closed union just as it would have for a smoothed one. It is asserted
for both in test_exclusion_smoothing.py.

If this is revisited, the thing to change is the OPERATION, not the
constants -- there is no tolerance below one cell to retreat to. An
outward-biased smoother (one whose error direction can be pinned) is
the shape of the answer.

--- ACREAGE IS COUNTED IN CELLS ---

Every acreage this module reports -- per layer, the union, the pairwise
overlaps, the NaN split -- is CELL-COUNT acreage (cells * raster_grid.
cell_area_acres()), not `polygon_utm.area / SQUARE_METERS_PER_ACRE`.
The layers ARE cell masks, overlap between them is only meaningful cell
by cell, and narrative_data's arithmetic has to close exactly (the
naive sum of the five layers exceeds the union by precisely the
measured overlap -- asserted). A `polygon_utm` clipped to the drawn
boundary trims the outer half of every boundary-straddling cell, so its
area runs slightly UNDER the cell-count figure. Both are correct
answers to different questions; they are not interchangeable and this
module does not mix them.

--- WHAT THIS MODULE DOES NOT DO ---

No interactive/frontend concern lives here: no clamp endpoint, no
display simplification, no WGS84 transport beyond the standard
`geometry_wgs84` key. This is an ordinary pipeline step. The
interactive use and the production integration are both later, separate
work.
"""

import math

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.prepared import prep

import dem_data
from production_area import (
    MAX_PRODUCTION_SLOPE_PCT,
    METERS_PER_FOOT,
    PRODUCTION_BOUNDARY_SETBACK_METERS,
    _fetch_disqualifying_soil_union,
    _fetch_road_exclusion_union_utm,
    compute_slope_percent,
    get_required_tree_root_zone_mask_utm,
)
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    cell_area_acres,
    cell_union_footprint,
    closing_radius_cells,
    connected_components,
    disc_closing,
    effective_radius_meters,
    pixel_center_xy,
)
from soil_data import coordinates_to_wkt_polygon

# Sentinel default for identify_exclusion_zones()'s road_exclusion_union_utm
# parameter, distinguishing "not supplied -- self-fetch" from a caller-
# supplied real None ("checked, and genuinely no roads found nearby" --
# farm_roads_data.get_road_exclusion_union_utm()'s own clean-result
# convention). Same distinction production_area.compute_step1_eligible_
# cells()'s _ROAD_CHECK_UNCHECKED draws, needed here since build_pipeline_
# context() passes its own already-fetched (and legitimately-None-able)
# existing_roads union through -- see identify_exclusion_zones()'s
# OVERRIDES docstring section.
_ROAD_UNION_NOT_SUPPLIED = object()

# ---------------------------------------------------------------------------
# CLOSING RADII -- one constant per gate, defined separately
#
# Separate constants even where the value is identical, per this codebase's
# standing convention: two gates that happen to agree today are not one
# tunable, and collapsing them would silently retune four layers the day
# anyone edits the shared value. Every value below is CONFIGURABLE, and
# every one of them records the measurement it came from -- see the module
# docstring's CLOSING section for the full per-boundary table.
# ---------------------------------------------------------------------------

CANOPY_EXCLUSION_CLOSING_RADIUS_METERS = 5.0
"""Canopy root-zone closing radius. MEASURED: 18 -> 13 polygons on both
reference boundaries, gaining 0.117 ac (OLD) / 0.228 ac (NEW). Pinhole
absorption -- the gaps closed are single-cell holes punched through
continuous woodland by the HAG threshold, not real clearings. One cell
at the pipeline's 5 m DEM resolution."""

SLOPE_EXCLUSION_CLOSING_RADIUS_METERS = 5.0
"""Slope closing radius. MEASURED: the largest and most fragmented of the
five layers (3.165 ac / 19 polygons OLD, 4.062 ac / 32 NEW) and the one
that gains most from closing -- 19 -> 12 and 32 -> 18 polygons, +0.234 /
+0.370 ac, with the largest single gained region just 6 and 8 cells. A
largest-gain that small is what distinguishes pinhole absorption from
region merging, and it is why 5 m is safe here despite this being the
biggest layer."""

HYDRIC_EXCLUSION_CLOSING_RADIUS_METERS = 0.0
"""Hydric soil: NO closing. MEASURED: 3 -> 3 polygons (OLD) and 1 -> 1
(NEW) at 5 m, gaining 0.000 and 0.012 ac. SSURGO map units arrive as
already-clean survey polygons, not as a thresholded raster, so there are
no pinholes to absorb. A closing here would buy nothing and cost real
ground."""

ROAD_EXCLUSION_CLOSING_RADIUS_METERS = 0.0
"""Existing road right-of-way: NO closing. MEASURED: 0 cells on BOTH
reference boundaries -- the layer is empty there, so no radius is
supportable from measurement at all. 0.0 is the honest configuration:
this one is genuinely unvalidated rather than measured-and-rejected (the
distinction the setback below does NOT share), and the first boundary
with real on-parcel road exclusion is the one that should set it."""

SETBACK_EXCLUSION_CLOSING_RADIUS_METERS = 0.0
"""Boundary setback: NO closing, and this 0.0 is a MEASURED DECISION, not
a not-yet-tuned placeholder. At 10 m the diagnostic flagged OVER-MERGE
SHAPE on BOTH boundaries -- one region took 100% (OLD) and 83% (NEW) of
the entire gain. The setback is a RING, split into 41-43 pieces by the
cell grid alone; closing a ring never tidies the ring, it bridges
straight across the parcel interior. At 5 m it gains exactly 0.000 ac on
both boundaries, so there is no radius that helps. Do not raise this
because the other layers close."""

# Fixed layer order for `layers`, narrative_data, and the pairwise overlap
# matrix -- declared once so the three can never disagree. Ordered by
# measured footprint on the reference boundaries (slope largest), except
# canopy leads because it is the gate a reader looks for first.
LAYER_ORDER = ("canopy", "slope", "hydric", "roads", "setback")
"""The five gates, and ALSO the stable TYPE IDENTIFIERS the wire ships (see
_wire_layers()). These five strings are an API: the frontend keys on them to
decide which caution to raise for a drawn polygon that crosses a layer. They
must never be renamed, reordered into a different meaning, or localised. The
human-readable wording lives in _display_label() and is expected to change."""


# ---------------------------------------------------------------------------
# ELIGIBLE UNION -- one highlight of every selectable cell
#
# Separate from the five exclusion layers and from eligible_polygon_utm; see
# identify_exclusion_zones()'s docstring for what distinguishes all three.
# ---------------------------------------------------------------------------

ELIGIBLE_UNION_MIN_CLUSTER_ACRES = 0.05
"""Clusters of selectable ground smaller than this are dropped from the
eligible union. 0.05 ac is 8 cells at the pipeline's 5 m DEM resolution --
enough to clear stranded single- and double-cell specks that would render as
unselectable-looking confetti, and small enough not to cut into a real pocket
a user might legitimately want to plant. CONFIGURABLE.

Applied ONLY to the eligible union. The per-gate layers and every acreage in
narrative_data are untouched by it: those figures become user-facing caution
numbers and must stay exact."""

ELIGIBLE_UNION_CONNECTIVITY = 8
"""8-connected labelling for the eligible union's cluster floor -- a
DELIBERATE REVERSAL of the direction production zone labelling went, and not
an oversight to "fix" back to 4 on the general principle.

production_area.cluster_and_gate() was moved from 8- to 4-connectivity in an
earlier branch for a specific reason: 8-connectivity treats two corner-
touching cells as one cluster, while cell_union_footprint() renders that same
pair as a disjoint MultiPolygon. That labeller-versus-geometry disagreement
broke waist splitting and hull containment, because a production patch is a
RECORD -- it carries its own footprint, acreage and score, and a patch whose
geometry is two disjoint pieces is a broken record.

None of that applies here. The eligible union is drawn as ONE highlight and
carries no per-cluster record; a point-touching junction is a visual pinch in
a highlight, not a broken patch. What 8-connectivity buys is the thing that
matters for a cluster FLOOR: it merges diagonal chains of cells rather than
fragmenting them, so fewer real runs of selectable ground fall under the
floor and get dropped. See test_eligible_union.py for the measured 4-vs-8
comparison at three floors. CONFIGURABLE."""

ELIGIBLE_UNION_SIMPLIFY_TOLERANCE_CELLS = 1.0
"""Angular simplify tolerance for the eligible union, in DEM CELLS --
multiplied by the DEM's own cell size at the point of use, the same pattern
render_layout_map.PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS uses, so it stays
"one cell" at any resolution. A metres constant would hardcode the grid.

WHAT THIS BUYS, MEASURED (diagnose_eligible_union_staircase.py, two
boundary-shaped fixtures): the 5 m cell staircase goes from 690 and 1743
one-cell axis-aligned boundary segments down to 3 on both; exterior vertices
drop 200 -> 41 and 1304 -> 258; area moves by +0.09% and -1.29%; polygon and
interior-ring counts are untouched. The error is BOUNDED and the bound is the
tolerance itself: Douglas-Peucker never moves a ring further than the
tolerance it was given, measured at exactly 5.00 m against a 5 m tolerance on
both fixtures and asserted in test_eligible_union.py.

NO CHAIKIN PASS HERE, and this is a measured rejection rather than an
omission -- someone will otherwise add one later on the reasonable-sounding
grounds that simplify leaves hard corners. Measured on the same two fixtures:

  Chaikin ALONE does not remove the staircase (3 one-cell segments still
  present on the fingered fixture, the same count simplify alone leaves) and
  INFLATES vertex count rather than reducing it -- 200 -> 399 and
  1304 -> 2600, the opposite of what this geometry needs, since it is
  transported to the frontend and clamped against.

  Chaikin COMPOSED WITH simplify costs more area than either pass alone
  (ratio 0.9955 and 0.9836, against 1.0009 / 0.9871 for simplify alone) and
  it forfeits the bound: max excursion 10.50 m and 14.58 m against a 5 m
  tolerance. This reproduces the interaction test_exclusion_smoothing.py
  found on the exclusion union -- Chaikin's corner cut scales with edge
  length, and collapsing the staircase is exactly what turns hundreds of 5 m
  edges into a few long ones. The two passes are not independent and
  composing them is worse than either.

A vector opening (buffer(-r).buffer(+r)) was measured and rejected too: it
raises vertex count 4-6x because round buffers emit arcs, and on ground with
narrow fingers it is destructive (r = 3 cells removed 51% of the eligible
area and turned 8 polygons into 15). Its advertised bounded-error guarantee
also does not hold as stated -- see diagnose_eligible_union_staircase.
max_inward_excursion(). CONFIGURABLE.
"""

CLOSING_RADIUS_METERS_BY_LAYER = {
    "canopy": CANOPY_EXCLUSION_CLOSING_RADIUS_METERS,
    "slope": SLOPE_EXCLUSION_CLOSING_RADIUS_METERS,
    "hydric": HYDRIC_EXCLUSION_CLOSING_RADIUS_METERS,
    "roads": ROAD_EXCLUSION_CLOSING_RADIUS_METERS,
    "setback": SETBACK_EXCLUSION_CLOSING_RADIUS_METERS,
}


def _round1(value: float) -> float:
    """narrative_data's single rounding rule: 1 decimal, plain float (never
    np.float32/np.float64 -- those are not JSON-serialisable and this block
    is handed straight to the report)."""
    return round(float(value), 1)


def _on_parcel_mask(dem: dict, polygon_utm: Polygon) -> np.ndarray:
    """
    Cells whose CENTER lies inside polygon_utm -- the same
    pixel_center_xy() + prepared-containment test production_area.
    compute_step1_eligible_cells() uses, so a cell lands on the same side
    of the boundary in both modules by construction rather than by
    coincidence. Called twice per run: once on the full drawn boundary
    (the slope and setback layers' universe) and once on the
    setback-shrunk boundary.
    """
    rows, cols = dem["array"].shape
    prepared = prep(polygon_utm)
    mask = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        for c in range(cols):
            if prepared.contains(Point(pixel_center_xy(dem, r, c))):
                mask[r, c] = True
    return mask


def _union_hit_mask(dem: dict, universe: np.ndarray, union_utm) -> np.ndarray:
    """
    Cells of `universe` whose center falls inside `union_utm` -- the
    hydric and road gates' containment test, identical to the one STEP 1
    runs (prepared geometry, one Point per candidate cell, tested only
    over the cells that are still in play). Returns an all-False mask
    when union_utm is None, which is the real "checked, genuinely nothing
    there" outcome, not an error.
    """
    hit = np.zeros(dem["array"].shape, dtype=bool)
    if union_utm is None:
        return hit
    prepared = prep(union_utm)
    for r, c in np.argwhere(universe):
        r, c = int(r), int(c)
        if prepared.contains(Point(pixel_center_xy(dem, r, c))):
            hit[r, c] = True
    return hit


def _mask_polygon(dem: dict, mask: np.ndarray, boundary_polygon_utm: Polygon):
    """
    A cell mask's exact ground footprint, clipped to the drawn boundary.

    raster_grid.cell_union_footprint() builds each cell's real ground
    square with corners computed from `origin +/- N * resolution` (so
    shared edges are bit-for-bit identical and adjacent squares actually
    dissolve) -- NOT a hull of cell centers and NOT a buffer.

    The clip to boundary_polygon_utm is the ONLY clip applied anywhere in
    this module. See the module docstring: there is deliberately no clip
    back to a raw footprint, because the closing is extensive and the
    closed geometry is the answer.

    KNOWN FOLLOW-UP -- NOT FIXED HERE, DELIBERATELY. This intersection can
    return a GeometryCollection carrying zero-area LINE pieces wherever a
    cell edge runs exactly along the boundary. That alignment is not exotic:
    the footprint and a grid-aligned boundary come off the same grid, and it
    crashed the first run of diagnose_eligible_union_staircase.py through the
    identical code path in build_eligible_union(), which now filters to
    polygonal parts via _polygonal_only().

    The same filter belongs here. It is not applied on this branch because
    every pre-existing return key had to stay byte-identical, and this
    function feeds five of them (layers[*]['polygon_utm'], excluded_union_
    utm, render_fill_polygon_utm, raw_excluded_union_utm, and through them
    geometry_wgs84). Changing it is a one-line edit plus a re-baseline of
    those keys, and it should be its own change so the re-baseline is
    reviewable on its own.
    """
    if not mask.any():
        return Polygon()
    return cell_union_footprint(dem, mask).intersection(boundary_polygon_utm)


def _pairwise_overlap_acres(dem: dict, masks: dict[str, np.ndarray]) -> list[dict]:
    """
    Measured overlap acreage for every pair of closed layers, in
    LAYER_ORDER order.

    This exists because the five per-layer acreages MUST NOT BE SUMMED
    and a comment saying so is not checkable. One cell can be both wooded
    and hydric -- on the NEW reference boundary canopy and hydric share
    0.394 ac, a real 5% double-count -- and every gate is evaluated
    independently over the same universe, so a summed "total excluded"
    would overstate the loss. Reporting the measured overlap lets a
    narrative see exactly which pairs share ground and by how much
    instead of taking a caveat's word for it.

    Same reasoning as diagnose_exclusion_footprints.layer_overlap_matrix()
    and as production_area_ceiling.build_narrative_data()'s paired
    `*_excluded` / `*_only_excluded` figures.
    """
    area_per_cell = cell_area_acres(dem)
    pairs = []
    for i, name_a in enumerate(LAYER_ORDER):
        for name_b in LAYER_ORDER[i + 1:]:
            shared = int((masks[name_a] & masks[name_b]).sum())
            pairs.append(
                {
                    "layers": [name_a, name_b],
                    "overlap_acres": _round1(shared * area_per_cell),
                }
            )
    return pairs


def build_eligible_union(
    dem: dict,
    step1_eligible_mask: np.ndarray,
    boundary_polygon_utm: Polygon,
    min_cluster_acres: float = ELIGIBLE_UNION_MIN_CLUSTER_ACRES,
    connectivity: int = ELIGIBLE_UNION_CONNECTIVITY,
    simplify_tolerance_cells: float = ELIGIBLE_UNION_SIMPLIFY_TOLERANCE_CELLS,
):
    """
    One geometry covering every piece of ground a user may select: the
    physical-gate eligible cells, grouped into clusters, specks below
    `min_cluster_acres` dropped, each survivor turned into its real cell
    footprint, unioned, and clipped to the drawn boundary.

    BUILT FROM STEP 1's GATES, BEFORE THE CEILING TRIM -- deliberately, and
    the alternative was rejected. production_area_ceiling.trim_to_ceiling()
    removes worst-scoring cells until production claims no more than
    PRODUCTION_CEILING_PCT_OF_PARCEL of the parcel, leaving room for the
    other KSOP layers. That ground passed every physical gate; it was
    dropped by a DESIGN JUDGEMENT, and in the interactive flow that
    judgement is advisory -- the panel says so. Branching after the trim
    would make an advisory ceiling binding on what the user is even allowed
    to draw on. So the union is built from the gate output and the ceiling
    does not narrow it.

    The consequence is worth stating plainly rather than discovering later:
    on a parcel where the ceiling fires, the eligible highlight extends
    beyond the production zones the tool proposed. That is the honest read,
    and it is what "advisory" is supposed to look like. On the reference
    boundaries the ceiling never fires (0 cells removed, at 63% of parcel),
    so this makes no observable difference there -- it will on flatter land.

    `step1_eligible_mask` is production_area.compute_step1_eligible_cells()'s
    own eligible_mask. identify_exclusion_zones() passes its own UNCLOSED
    gate complement, which is byte-identical to it (asserted directly
    against a real compute_step1_eligible_cells() call in
    test_eligible_union.py) -- taking it from the masks this module already
    computed avoids adding a sixth redundant gate computation to a module
    whose docstring is already an argument about redundancy. Note this is
    the UNCLOSED complement: the closed one (`eligible_mask` on the return)
    is smaller by exactly the pinholes the closing absorbed, and using it
    here would silently apply the closing radii to the highlight.

    SIMPLIFIED, NOT SMOOTHED. The staircase is removed with a single angular
    simplify pass at ELIGIBLE_UNION_SIMPLIFY_TOLERANCE_CELLS (see that
    constant for the measurement, and for why a Chaikin pass and a vector
    opening were both measured and rejected). Pass
    simplify_tolerance_cells=0.0 for the exact, unsimplified cell footprint --
    diagnose_eligible_union_staircase.py does, since it measures operations
    against the raw staircase as its baseline.

    The simplify is applied LAST, after the cluster floor and the boundary
    clip. Order matters: simplifying first would move the boundary before the
    clip and let the union bleed a tolerance-width past the parcel edge.

    POLYGONAL PARTS ONLY. Clipping a cell footprint to the boundary can emit
    zero-area line pieces in the intersection wherever a cell edge runs exactly
    along the boundary -- which is not a rare alignment, since both are built
    from the same grid on a synthetic or grid-aligned parcel. The declared type
    of this field is a Polygon/MultiPolygon, so those pieces are dropped rather
    than shipped inside a GeometryCollection a consumer would have to unpack.
    (identify_exclusion_zones()'s older _mask_polygon() has the same exposure
    and is deliberately NOT changed here -- every existing return key must stay
    byte-identical on this branch. Flagged, not silently fixed.)

    Returns an empty Polygon when nothing survives the floor.
    """
    area_per_cell = cell_area_acres(dem)
    min_cells = math.ceil(min_cluster_acres / area_per_cell) if min_cluster_acres > 0 else 0

    labels, count = connected_components(step1_eligible_mask, connectivity=connectivity)

    surviving = np.zeros(step1_eligible_mask.shape, dtype=bool)
    for label in range(count):
        cluster = labels == label
        if int(cluster.sum()) >= min_cells:
            surviving |= cluster

    clipped = _polygonal_only(cell_union_footprint(dem, surviving).intersection(boundary_polygon_utm))
    if simplify_tolerance_cells <= 0 or clipped.is_empty:
        return clipped

    tolerance_m = simplify_tolerance_cells * max(dem["resolution_meters"])
    simplified = _polygonal_only(clipped.simplify(tolerance_m, preserve_topology=True))
    # preserve_topology=True already rules out self-intersection and dropped
    # rings, but this geometry is clamped against, so a degraded result falls
    # back to the exact footprint rather than shipping something invalid --
    # the same degrade-never-raise contract raster_grid.angular_smooth_
    # polygon() carries.
    if simplified.is_empty or not simplified.is_valid:
        return clipped
    return simplified


def _polygonal_only(geometry):
    """Every Polygon part of `geometry`, reassembled; line/point pieces
    dropped. A no-op on geometry that is already polygonal."""
    if geometry.is_empty or geometry.geom_type in ("Polygon", "MultiPolygon"):
        return geometry
    polygons = [g for g in getattr(geometry, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]
    if not polygons:
        return Polygon()
    return polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)


def _display_label(name: str, max_slope_pct: float, boundary_setback_meters: float) -> str:
    """
    The human-readable half of the wire's per-layer pair: what the user is
    told they are overriding. Built from the thresholds this run ACTUALLY
    used, never from hardcoded reference numbers -- a caller that raises
    max_slope_pct must not be handed a caption still claiming 20%.

    These STATE THE TEST rather than name the layer: "slope above 20%", not
    "steep slope"; "within 10 ft of the boundary", not "boundary setback".
    A user overriding an exclusion is entitled to know what it measured.

    This wording is expected to be reworded and is NOT an API. The stable
    half of the pair is the type identifier (see LAYER_ORDER).
    """
    if name == "slope":
        return f"slope above {_round1(max_slope_pct)}%"
    if name == "setback":
        return f"within {_round1(boundary_setback_meters / METERS_PER_FOOT)} ft of the boundary"
    return {
        "canopy": "tree canopy root zone",
        "hydric": "wet (hydric) soil",
        "roads": "existing farm road right-of-way",
    }[name]


def _wire_layers(
    dem: dict,
    layers: dict[str, dict],
    layer_availability: dict[str, bool],
    max_slope_pct: float,
    boundary_setback_meters: float,
) -> list[dict]:
    """
    Everything the frontend needs to intersect a drawn polygon against ONE
    gate and caption what it crossed, per gate, in LAYER_ORDER.

    `type` AND `label` ARE TWO FIELDS ON PURPOSE. `type` is the stable
    identifier the frontend branches on and must never change; `label` is
    display prose that will be reworded. A consumer that keys on the label
    instead is broken by the first copy edit, which is exactly the failure
    this split exists to prevent.

    `geometry_wgs84` is what makes the per-layer split useful at all: without
    it only the eligible union ships in WGS84, and a caution naming WHICH gate
    a drawing crossed is impossible to compute. None when the layer excludes
    nothing on this parcel.

    `data_available` is NOT decoration and must not be collapsed into "is the
    geometry empty". A layer that was never checked (an unreachable SSURGO
    endpoint, no lidar coverage) and a layer that genuinely excludes nothing
    both produce a null geometry, and they must not produce the same caution:
    one means "clear", the other means "unknown". Same class of distinction as
    null-versus-zero in narrative_data.
    """
    wire_layers = []
    for name in LAYER_ORDER:
        polygon_utm = layers[name]["polygon_utm"]
        wire_layers.append(
            {
                "type": name,
                "label": _display_label(name, max_slope_pct, boundary_setback_meters),
                "data_available": bool(layer_availability[name]),
                "geometry_wgs84": (
                    transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))
                    if not polygon_utm.is_empty
                    else None
                ),
            }
        )
    return wire_layers


def build_narrative_data(
    dem: dict,
    closed_masks: dict[str, np.ndarray],
    raw_masks: dict[str, np.ndarray],
    union_mask: np.ndarray,
    eligible_mask: np.ndarray,
    on_parcel: np.ndarray,
    slope_pct: np.ndarray,
    parcel_acres: float,
    max_slope_pct: float,
    layer_availability: dict[str, bool],
) -> dict:
    """
    The 'narrative_data' block identify_exclusion_zones() attaches to its
    result -- pre-digested, FINAL, JSON-serialisable values answering
    "why can't this ground be used?". Data only: no prose, no
    interpretation, no "this suggests" strings. Every acreage is imperial
    and rounded to 1 decimal; every number is a plain Python float/int/
    bool, never a numpy scalar.

    SELF-SUFFICIENT BY DESIGN, same contract every other narrative_data
    block in this pipeline carries: a caller wiring the report reads this
    and nothing else. Its overlap with the top-level return keys is
    INTENDED, not redundancy to collapse.

    Shape:

        {
          'parcel': {'total_acres', 'excluded_acres', 'eligible_acres',
                     'excluded_pct_of_parcel'},
          'layers': [ one entry per gate, in LAYER_ORDER:
                      {'layer', 'acres', 'closing_radius_ft',
                       'effective_closing_radius_ft', 'closing_radius_cells',
                       'closed', 'acres_gained_by_closing', 'data_available'} ],
          'overlap': {'pairs': [...], 'naive_sum_acres', 'union_acres',
                      'double_counted_acres'},
          'slope_detail': {'max_slope_pct', 'too_steep_acres',
                           'nan_slope_acres'},
          'setback_is_lower_bound': True,
          'setback_lower_bound_reason': 'steep_ring_ground_counted_in_slope_layer',
        }

    OVERLAP -- READ BEFORE ADDING THE LAYER FIGURES UP. 'layers[].acres'
    counts every cell that layer excludes whether or not another layer
    excludes it too. THESE MUST NOT BE SUMMED. 'overlap.naive_sum_acres'
    is that forbidden sum, reported ON PURPOSE next to
    'overlap.union_acres' (the real total) and their difference
    'overlap.double_counted_acres', so a narrative that reaches for a
    total finds the right number and the size of the mistake side by side
    rather than having to compute either.

    THE SETBACK FIGURE IS A LOWER BOUND, and the two flags at the bottom
    say so in a form a consumer can branch on. The setback layer is
    derived as `on_parcel & slope_ok & ~slope_only_mask` -- it REQUIRES
    slope_ok -- while the slope layer is `on_parcel & ~slope_ok`. Ring
    ground that ALSO fails slope therefore lands WHOLLY in the slope
    layer and not at all in the setback layer. The two are disjoint by
    construction, not by luck, and neither can be corrected for this from
    STEP 1's own arrays: slope_only_mask collapses the slope test and the
    shrunk-boundary test into one array, so the ring's steep part is not
    recoverable. Reported as a flag rather than a sentence because this
    block carries no prose -- the full wording lives in this module's
    docstring and in diagnose_exclusion_footprints.py's own output.

    ACRES GAINED BY CLOSING is per layer and is the honest cost line for
    the extensive operation: closed cells minus raw cells, so a layer
    configured at 0.0 m reports exactly 0.0 and a layer that closed
    reports what the closing actually absorbed on THIS boundary -- not
    what it absorbed on the reference boundaries the radius was tuned
    against.
    """
    area_per_cell = cell_area_acres(dem)
    excluded_cells = int(union_mask.sum())
    excluded_acres = excluded_cells * area_per_cell

    layers = []
    for name in LAYER_ORDER:
        radius_m = CLOSING_RADIUS_METERS_BY_LAYER[name]
        radius_cells = closing_radius_cells(dem, radius_m)
        gained = int(closed_masks[name].sum()) - int(raw_masks[name].sum())
        layers.append(
            {
                "layer": name,
                "acres": _round1(int(closed_masks[name].sum()) * area_per_cell),
                "closing_radius_ft": _round1(radius_m / METERS_PER_FOOT),
                # The radius ASKED FOR and the radius APPLIED differ whenever
                # the requested metres do not land on a whole cell -- at 5 m
                # DEM resolution they differ often. Reporting only the request
                # would misstate what ran.
                "effective_closing_radius_ft": _round1(
                    effective_radius_meters(dem, radius_cells) / METERS_PER_FOOT
                ),
                "closing_radius_cells": int(radius_cells),
                "closed": bool(radius_cells > 0),
                "acres_gained_by_closing": _round1(gained * area_per_cell),
                "data_available": bool(layer_availability[name]),
            }
        )

    naive_sum_acres = sum(int(closed_masks[name].sum()) for name in LAYER_ORDER) * area_per_cell
    nan_slope_cells = int((on_parcel & np.isnan(slope_pct)).sum())
    too_steep_cells = int(raw_masks["slope"].sum()) - nan_slope_cells

    return {
        "parcel": {
            "total_acres": _round1(parcel_acres),
            "excluded_acres": _round1(excluded_acres),
            "eligible_acres": _round1(int(eligible_mask.sum()) * area_per_cell),
            "excluded_pct_of_parcel": _round1(
                excluded_acres / parcel_acres * 100 if parcel_acres > 0 else 0.0
            ),
        },
        "layers": layers,
        "overlap": {
            "pairs": _pairwise_overlap_acres(dem, closed_masks),
            "naive_sum_acres": _round1(naive_sum_acres),
            "union_acres": _round1(excluded_acres),
            "double_counted_acres": _round1(naive_sum_acres - excluded_acres),
        },
        "slope_detail": {
            "max_slope_pct": _round1(max_slope_pct),
            # "too steep" and "not measured" are different facts about the
            # ground and a narrative must not merge them -- see the module
            # docstring's NODATA section. Both are inside the slope layer;
            # only their sum is.
            "too_steep_acres": _round1(too_steep_cells * area_per_cell),
            "nan_slope_acres": _round1(nan_slope_cells * area_per_cell),
        },
        "setback_is_lower_bound": True,
        "setback_lower_bound_reason": "steep_ring_ground_counted_in_slope_layer",
    }


def identify_exclusion_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: dict | None = None,
    boundary_polygon_utm: Polygon | None = None,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    boundary_setback_meters: float = PRODUCTION_BOUNDARY_SETBACK_METERS,
    canopy_height: dict | None = None,
    tree_root_zone_mask_utm: np.ndarray | None = None,
    disqualifying_soil_union_utm=None,
    road_exclusion_union_utm=_ROAD_UNION_NOT_SUPPLIED,
    check_soil: bool = True,
    check_roads: bool = True,
) -> dict:
    """
    Computes this parcel's unselectable ground: five per-gate exclusion
    masks, each closed at its own measured radius, unioned into one
    geometry, with the per-layer detail kept alongside so the union can
    be explained rather than just drawn.

    OVERRIDES. dem/boundary_polygon_utm/canopy_height/
    tree_root_zone_mask_utm/disqualifying_soil_union_utm follow this
    pipeline's standard None-falls-back-to-self-compute convention: a
    caller that already has one passes it and no second fetch happens.
    road_exclusion_union_utm is the ONE exception: its default is a
    private sentinel ("not supplied"), and a caller-supplied real None
    means what farm_roads_data.get_road_exclusion_union_utm()'s own None
    means -- "checked, and genuinely no roads found nearby" -- so it is
    REUSED (road_available=True, no second fetch) rather than treated as
    missing. Same distinction production_area.compute_step1_eligible_
    cells()'s _ROAD_CHECK_UNCHECKED sentinel already draws, adopted here
    the day a real caller existed for it: build_pipeline_context() now
    passes its own already-fetched existing_roads union straight through,
    and that union is legitimately None on any parcel with no mapped road
    nearby -- treating that None as "not supplied" would quietly
    reintroduce the second fetch on exactly the parcels the pass-through
    exists to spare. (An earlier version of this note declined to invent
    the distinction "before a caller with that value to pass exists";
    that caller now exists.) disqualifying_soil_union_utm keeps the plain
    None convention -- no caller passes a real None for it yet.
    check_soil/check_roads are the explicit way to say "do not check this
    gate at all".

    max_slope_pct and boundary_setback_meters default to production's own
    MAX_PRODUCTION_SLOPE_PCT and PRODUCTION_BOUNDARY_SETBACK_METERS. They
    are real parameters, but the DEFAULTS matching production exactly is
    the point: these five layers are meant to be production's five gates,
    and a layer computed against a different threshold would be a
    different question wearing the same name.

    GRACEFUL DEGRADATION matches production's own: soil and roads are
    optional (a fetch failure leaves that layer empty and
    data_available False -- the ground is reported as selectable, the
    same way production would treat it), canopy is MANDATORY and a
    missing HAG layer raises RuntimeError via get_required_tree_root_
    zone_mask_utm(). Slope and setback need no fetch and are always
    available.

    Returns:
        {
            'layers': {                      # per gate, keyed by LAYER_ORDER
                '<name>': {
                    'mask': np.ndarray[bool],       # CLOSED at this gate's radius
                    'raw_mask': np.ndarray[bool],   # before closing
                    'polygon_utm': Polygon/MultiPolygon,  # closed, clipped to boundary
                    'acres': float,                 # cell-count acres of 'mask'
                    'data_available': bool,
                },
                ...
            },
            'excluded_union_utm': ...,       # all five CLOSED masks, unioned,
                                             #   clipped to the boundary
            'render_fill_polygon_utm': ...,  # what the map draws -- see below
            'raw_excluded_union_utm': ...,   # the UNCLOSED union; exists so the
                                             #   extensive invariant is checkable
                                             #   against something real
            'eligible_polygon_utm': ...,     # boundary - excluded_union -- a
                                             #   COMPLEMENT; see below
            'eligible_union_utm': ...,       # MultiPolygon of selectable ground,
                                             #   built from the CELL MASK; see below
            'eligible_union_wgs84': ...,     # the same, GeoJSON, for the wire
            'wire': {...},                   # frontend-facing metadata; see below
            'eligible_mask': np.ndarray[bool],
            'excluded_union_mask': np.ndarray[bool],
            'geometry_wgs84': GeoJSON dict,  # excluded_union_utm in EPSG:4326
            'parcel_acres': float,
            'narrative_data': {...},
        }

    THREE THINGS NAMED "ELIGIBLE", AND WHY NONE OF THEM IS REDUNDANT.
    A future reader will assume two of these are the same and delete one.
    They are not:

      eligible_polygon_utm -- boundary_polygon_utm MINUS the CLOSED
        exclusion union. A pure complement, computed in geometry space.
        It covers every square metre the closed exclusions do not,
        including stranded single-cell specks, and its boundary is by
        construction the exact inverse of what the map draws as excluded.

      eligible_union_utm -- built from the CELL MASK forward: STEP 1's
        gate-eligible cells, 8-connected clustered, clusters under
        ELIGIBLE_UNION_MIN_CLUSTER_ACRES dropped, footprints unioned,
        clipped to the boundary. This is the DISPLAY AND CLAMPING
        geometry: what the interactive flow highlights and what a drawn
        polygon is eventually constrained against.

      eligible_mask -- the np.ndarray the other two are reasoned about
        in, and the closed-gate complement in cell space.

    They differ in two ways that matter and neither is an accident.
    FIRST, the CLUSTER FLOOR: eligible_polygon_utm keeps a stranded
    4-cell speck, eligible_union_utm drops it. Highlighting confetti a
    user cannot meaningfully plant is a worse answer than not
    highlighting it. SECOND, the CLOSING: eligible_polygon_utm is the
    complement of the CLOSED union, so the pinhole cells the closing
    absorbed are excluded from it; eligible_union_utm is built from the
    UNCLOSED gate output, so it keeps them. The closing radii exist to
    make the EXCLUSION read as one shape -- applying them to what a user
    is allowed to draw on is a different decision and was not made here.

    Consequence: eligible_union_utm is NOT the exact complement of
    render_fill_polygon_utm, and it is not supposed to be. The overlap
    is the pinholes; the shortfall is the specks.

    render_fill_polygon_utm IS excluded_union_utm, the same geometry --
    deliberately, and NOT an oversight to correct by adding an opening.
    A production zone's render_fill is an OPENING of its polygon_utm, so
    the two differ and `render_fill.area <= polygon_utm.area` is asserted
    there. Here there is no display-only reduction to apply: the closing
    is the whole operation, the closed geometry is what the map should
    draw, and shrinking it for display would draw a different answer than
    the one computed. The key exists so this result is KSOP-shaped like
    every other layer's; it carries the same geometry on purpose. See the
    module docstring for the inverted invariant that replaces
    production's containment assertion.

    eligible_mask IS NOT CONSUMED ANYWHERE IN THIS BRANCH. It is emitted
    so the deferred production integration (module docstring, DELIBERATE
    REDUNDANCY) is a wiring change rather than a rewrite. With all five
    radii at 0.0 it is byte-identical to compute_step1_eligible_cells()'
    own eligible_mask; with the measured radii it is smaller by exactly
    the pinhole cells the closing absorbs.
    """
    if dem is None:
        dem = dem_data.get_dem_for_boundary(boundary_coordinates)
    if boundary_polygon_utm is None:
        # Same warp_transform + Polygon() pattern production_area_ceiling.py,
        # water_candidate_zones.py, road_corridors.py and pipeline_context.
        # _boundary_polygon_utm() each use. Not imported from pipeline_context:
        # that module imports THIS one (this is its first Layer 2 step), so
        # taking the helper from there would be a circular import.
        xs, ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(xs, ys))

    # ---- the five RAW gate masks --------------------------------------
    #
    # Computed here, independently of production, and matching production's
    # own logic exactly -- including its UNIVERSE. compute_step1_eligible_
    # cells() evaluates canopy/hydric/road only over slope_only_mask (ground
    # that already cleared BOTH the slope gate and the setback), so these
    # three layers are shares of slope-passing on-parcel ground, not of the
    # whole parcel. Widening them to the full parcel would make them
    # different layers than the gates they mirror, and would stop matching
    # the footprints diagnose_exclusion_footprints.py measured.

    slope_pct = compute_slope_percent(dem["array"], dem["resolution_meters"])
    slope_ok = (~np.isnan(slope_pct)) & (slope_pct <= max_slope_pct)

    on_parcel = _on_parcel_mask(dem, boundary_polygon_utm)
    shrunk_boundary_utm = (
        boundary_polygon_utm.buffer(-boundary_setback_meters)
        if boundary_setback_meters > 0
        else boundary_polygon_utm
    )
    on_parcel_post_setback = (
        _on_parcel_mask(dem, shrunk_boundary_utm)
        if boundary_setback_meters > 0
        else on_parcel
    )

    # STEP 1's slope_only_mask, reproduced: slope-ok AND inside the shrunk
    # boundary, as one combined test.
    slope_only_mask = slope_ok & on_parcel_post_setback

    # Slope: on-parcel ground that FAILS the slope gate. NaN-slope cells are
    # in here too (see the module docstring's NODATA section) and are split
    # back out in narrative_data.
    slope_fail = on_parcel & (~slope_ok)

    # Setback: on-parcel ground that CLEARS slope and is still outside
    # slope_only_mask, which can only be the shrunk-boundary half of the
    # combined test rejecting it. Disjoint from the slope layer BY
    # CONSTRUCTION -- which is exactly why it is a lower bound on the real
    # ring (narrative_data.setback_is_lower_bound).
    setback_fail = on_parcel & slope_ok & (~slope_only_mask)

    if tree_root_zone_mask_utm is None:
        tree_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
            boundary_polygon_utm, dem, canopy_height=canopy_height
        )
    canopy_fail = slope_only_mask & tree_root_zone_mask_utm

    soil_available = False
    if check_soil:
        if disqualifying_soil_union_utm is None:
            try:
                xs, ys = boundary_polygon_utm.exterior.coords.xy
                lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
                wkt_polygon = coordinates_to_wkt_polygon(list(zip(lons, lats)))
                disqualifying_soil_union_utm = _fetch_disqualifying_soil_union(wkt_polygon, dem)
                soil_available = True
            except Exception:
                # Same degrade-don't-crash contract identify_production_areas()
                # applies to this gate: an unreachable SSURGO endpoint reports
                # the ground as selectable rather than failing the render.
                disqualifying_soil_union_utm = None
        else:
            soil_available = True
    hydric_fail = _union_hit_mask(dem, slope_only_mask, disqualifying_soil_union_utm)

    road_available = False
    if check_roads:
        if road_exclusion_union_utm is _ROAD_UNION_NOT_SUPPLIED:
            try:
                xs, ys = boundary_polygon_utm.exterior.coords.xy
                lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
                road_exclusion_union_utm = _fetch_road_exclusion_union_utm(
                    list(zip(lons, lats)), dem
                )
                road_available = True
            except Exception:
                road_exclusion_union_utm = None
        else:
            # Supplied -- including a real None ("checked, genuinely no
            # roads found nearby"): reused, never re-fetched. See the
            # OVERRIDES docstring section for why this parameter alone
            # draws that distinction.
            road_available = True
    if road_exclusion_union_utm is _ROAD_UNION_NOT_SUPPLIED:
        # check_roads=False with nothing supplied -- gate skipped entirely.
        road_exclusion_union_utm = None
    road_fail = _union_hit_mask(dem, slope_only_mask, road_exclusion_union_utm)

    raw_masks = {
        "canopy": canopy_fail,
        "slope": slope_fail,
        "hydric": hydric_fail,
        "roads": road_fail,
        "setback": setback_fail,
    }
    layer_availability = {
        "canopy": True,
        "slope": True,
        "hydric": soil_available,
        "roads": road_available,
        "setback": True,
    }

    # ---- close each gate at ITS OWN radius, independently --------------
    #
    # One disc_closing() per layer at that layer's own constant. A layer
    # configured at 0.0 m gets radius_cells 0 and disc_closing() hands back
    # an unchanged copy -- the raw footprint, which for the setback is the
    # measured right answer and not a missed configuration.
    closed_masks = {}
    for name in LAYER_ORDER:
        radius_cells = closing_radius_cells(dem, CLOSING_RADIUS_METERS_BY_LAYER[name])
        closed_masks[name] = disc_closing(raw_masks[name], radius_cells)

    # A closing can push cells past the drawn boundary; the union is clipped
    # in cell space here and again in geometry space below, since an
    # exclusion is a fact about THIS parcel and off-parcel ground was never
    # selectable to begin with.
    union_mask = np.zeros(dem["array"].shape, dtype=bool)
    for name in LAYER_ORDER:
        union_mask |= closed_masks[name]
    union_mask &= on_parcel

    raw_union_mask = np.zeros(dem["array"].shape, dtype=bool)
    for name in LAYER_ORDER:
        raw_union_mask |= raw_masks[name]
    raw_union_mask &= on_parcel

    # Everything on the parcel the five closed gates do not take. NOT
    # consumed in this branch -- see this function's docstring.
    eligible_mask = on_parcel & (~union_mask)

    excluded_union_utm = _mask_polygon(dem, union_mask, boundary_polygon_utm)
    raw_excluded_union_utm = _mask_polygon(dem, raw_union_mask, boundary_polygon_utm)
    eligible_polygon_utm = boundary_polygon_utm.difference(excluded_union_utm)

    # STEP 1's own eligible_mask, reproduced from the masks already computed
    # here rather than by a sixth gate computation: the UNCLOSED gate
    # complement is byte-identical to compute_step1_eligible_cells()'s
    # eligible_mask (asserted against a real call in test_eligible_union.py).
    # UNCLOSED is the point -- the closed complement below is smaller by the
    # pinholes the closing absorbed, and highlighting that instead would apply
    # the closing radii to what the user is allowed to draw on.
    step1_eligible_mask = on_parcel & (~raw_union_mask)
    eligible_union_utm = build_eligible_union(dem, step1_eligible_mask, boundary_polygon_utm)

    layers = {}
    area_per_cell = cell_area_acres(dem)
    for name in LAYER_ORDER:
        layers[name] = {
            "mask": closed_masks[name],
            "raw_mask": raw_masks[name],
            "polygon_utm": _mask_polygon(dem, closed_masks[name], boundary_polygon_utm),
            "acres": round(int(closed_masks[name].sum()) * area_per_cell, 2),
            "data_available": layer_availability[name],
        }

    parcel_acres = boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE

    return {
        "layers": layers,
        "excluded_union_utm": excluded_union_utm,
        # The SAME geometry as excluded_union_utm, on purpose -- see this
        # function's docstring. There is no opening to apply to a closing.
        "render_fill_polygon_utm": excluded_union_utm,
        "raw_excluded_union_utm": raw_excluded_union_utm,
        "eligible_polygon_utm": eligible_polygon_utm,
        # NOT the same thing as eligible_polygon_utm above, and neither is
        # redundant -- see this function's docstring for the three-way split.
        "eligible_union_utm": eligible_union_utm,
        "eligible_union_wgs84": (
            transform_geom(dem["crs"], "EPSG:4326", mapping(eligible_union_utm))
            if not eligible_union_utm.is_empty
            else None
        ),
        "wire": {
            "layers": _wire_layers(
                dem, layers, layer_availability, max_slope_pct, boundary_setback_meters
            ),
            # BOTH dimensions, in metres. Every acreage the frontend computes
            # from an intersection is cells x cell_area, and DEM resolution is
            # not square -- the two reference DEMs are 4.99 x 5.00 and
            # 5.00 x 4.99. One number would be wrong on both.
            "cell_size_meters": [float(dem["resolution_meters"][0]), float(dem["resolution_meters"][1])],
            # Carried out of narrative_data so a wire consumer can branch on it
            # without parsing the report block. Ground in the setback ring was
            # never tested for canopy, hydric or roads, so a caution reading
            # "boundary setback" would imply proximity is the only problem when
            # the truth is that the other gates never ran there.
            "setback_is_lower_bound": True,
            "setback_lower_bound_reason": "steep_ring_ground_counted_in_slope_layer",
            # The thresholds as NUMBERS, not only as prose inside the labels
            # above. The label is for display; these are for logic -- sorting,
            # re-wording, or recomputing a caution against the value that was
            # actually tested. A consumer parsing "20.0" back out of a label
            # string is a consumer broken by the next copy edit.
            "max_slope_pct": float(max_slope_pct),
            "boundary_setback_meters": float(boundary_setback_meters),
            # Every acreage the frontend computes from an intersection is in
            # the projected metres of THIS UTM zone -- cell_size_meters above
            # is meaningless without it. Leaving it implicit is how a parcel
            # near a zone boundary produces quietly wrong numbers.
            "crs": str(dem["crs"]),
        },
        "eligible_mask": eligible_mask,
        "excluded_union_mask": union_mask,
        "geometry_wgs84": (
            transform_geom(dem["crs"], "EPSG:4326", mapping(excluded_union_utm))
            if not excluded_union_utm.is_empty
            else None
        ),
        "parcel_acres": parcel_acres,
        "narrative_data": build_narrative_data(
            dem,
            closed_masks,
            raw_masks,
            union_mask,
            eligible_mask,
            on_parcel,
            slope_pct,
            parcel_acres,
            max_slope_pct,
            layer_availability,
        ),
    }
