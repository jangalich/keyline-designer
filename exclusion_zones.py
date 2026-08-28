"""
exclusion_zones.py

The parcel's UNSELECTABLE GROUND, as a first-class KSOP layer: the five
reasons a piece of this property cannot carry a production zone, each
computed as its own cell mask and published as that mask's EXACT ground
footprint, plus one union of every cell that is still selectable.

The five layers, and the gate each one mirrors:

    canopy   -- woody-vegetation root zone (canopy_height_data.
                tree_root_zone_mask(), at TREE_ROOT_ZONE_BUFFER_METERS)
    slope    -- ground steeper than max_slope_pct, PLUS ground with no
                DEM coverage at all (see NODATA below)
    hydric   -- disqualifying (hydric) SSURGO soil
    roads    -- existing road right-of-way, at ROAD_EXCLUSION_BUFFER_METERS
    setback  -- the ring inside PRODUCTION_BOUNDARY_SETBACK_METERS of the
                drawn boundary

--- WHAT THESE LAYERS ARE FOR, AND WHY IT CHANGED WHAT THEY ARE ---

This module was originally built to render a consolidated exclusion
layer on the map, and every display-driven decision in it followed from
that. The interactive design direction changed the requirement:

    NOTHING RENDERS THE EXCLUSIONS AT REST.

They ship so the frontend can intersect a user-drawn polygon against
ONE named gate and caption what the drawing crossed -- "0.4 acres of
tree canopy." What gets highlighted instead is all ELIGIBLE ground, as
one union (`eligible_union_utm`), built separately and floored
separately.

That inverts the requirements the module was designed against, and it
is why the module is SMALLER than its history suggests it should be.
The two consequences, both deliberate:

  THE PER-GATE CLOSING IS GONE. See the next section.

  THE CLUSTER FLOOR MOVED. Stranded single-cell specks were a problem
  for a rendered EXCLUSION layer; they are now a problem for the
  ELIGIBLE highlight, which has its own floor
  (ELIGIBLE_UNION_MIN_CLUSTER_ACRES) and is the only place one is
  applied.

--- NO CLOSING. THE LAYERS ARE RAW CELL FOOTPRINTS, EXACT ---

Each layer used to be morphologically CLOSED at its own measured radius
(canopy and slope at 5 m, the other three at 0). That closing has been
removed entirely: constants, calls, and the separate closed/raw pair of
every geometry it produced.

WHY IT WAS RIGHT BEFORE. Two DISPLAY arguments justified it. Fragmented
exclusions read badly as a rendered layer, and a pinhole left inside an
exclusion becomes a stranded selectable speck. diagnose_exclusion_
footprints.py measured both effects on two reference boundaries and the
radii came from that measurement, not from taste.

WHY IT IS WRONG NOW. Neither argument survives the change above. The
layers render nothing, so there is no rendered fragmentation to tidy;
and the stranded-speck problem belongs to the eligible union, which is
built from the gate output separately and gets its own cluster floor.

AND FOR THE LAYERS' ACTUAL PURPOSE, A CLOSING IS ACTIVELY WRONG. It is
an EXTENSIVE operation -- it only ever ADDS cells -- and what it added
was measured: +0.117 ac (OLD reference boundary) / +0.228 ac (NEW) on
canopy, +0.234 / +0.370 ac on slope. Those cells are not canopy and are
not steep. A caution reading "0.5 acres of tree canopy" when the truth
is 0.4 is a FALSE STATEMENT TO THE USER about their own land, and it is
false by an amount large enough to see.

This is the same reasoning that has always kept narrative_data's
per-gate acreages unclosed and unsmoothed. Those figures are
user-facing, so they must be exact. The wire figures are now user-facing
in the same way, and the same rule applies to the geometry itself.

THE DIAGNOSTIC STAYS. diagnose_exclusion_footprints.py still measures
closings at 5 m and 10 m on both reference boundaries, and is what
established every number quoted above. Nothing in the pipeline closes
anymore and it is kept working anyway: if rendering the exclusions ever
returns, the question returns with it, and that script is the answer.
It shares no constant with this module -- it takes raster_grid.
disc_closing() directly -- so removing the radii here did not touch it.

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
made the production integration below a one-line wiring change rather
than a reordering, the day it landed.

--- THIS MODULE IS PRODUCTION'S GATE COMPUTATION (THE REDUNDANCY IS GONE) ---

This section used to be headed DELIBERATE, TIME-LIMITED REDUNDANCY and
explain why two modules computed the same five gates on the same DEM in
the same pipeline run. They no longer do. production_area.compute_step1_
eligible_cells() takes an exclusion_result= override, production_area_
ceiling.py forwards it through both hops, and build_pipeline_context()
passes this module's own result in. STEP 1 reads all five gates off it.
One canopy fetch, one soil fetch, one road union and one slope grid per
run, all of them this module's.

WHY IT COULD BE DONE, AND WHY IT COULD NOT BE DONE EARLIER. While the
per-gate closing existed, feeding this module's masks into production
would have CHANGED PRODUCTION'S RESULTS: the closed exclusions were
larger than the raw gates by the pinholes they absorbed (measured at
roughly 0.35 ac on the OLD reference boundary and 0.60 ac on the NEW),
so production would have lost exactly those cells. Deferring the
integration was deferring a decision about production's output, which
is why it waited.

The closing was then removed -- not for production's benefit, but
because the frontend intersects a drawn polygon against these layers
and captions the acreage it crossed ("0.4 acres of tree canopy"), and a
layer that reports more ground than the gate actually hit is a false
statement about someone's land. Raw and exact is also precisely what
production's own gates are. So the integration became a pure
de-duplication with no behavioural change available to it, and that is
asserted from both sides rather than argued: test_eligible_union.py
section 0 checks this module's masks against a real compute_step1_
eligible_cells() call, and test_production_area.py's "STEP 1 CONSUMES
THE EXCLUSION RESULT" section checks all thirteen arrays and three
availability flags STEP 1 returns, on a fixture firing all five gates.

WHAT THIS MAKES THIS MODULE. A producer for TWO consumers with very
different needs: the frontend reads `layers`, `wire`, `eligible_union_
utm` and the acreages; production reads the raw cell grids (eligible_
mask, slope_pct, slope_only_mask, and the three gate layer masks). The
consequence is that a change to a gate here is now a change to what
gets planted, not only to what gets drawn. The five thresholds live in
production_area.py and are imported, never redeclared, so they cannot
drift; and compute_step1_eligible_cells() CHECKS the two it was handed
against the ones this module recorded on `wire` rather than trusting
them to match.

THE COST, MEASURED -- and what the measurement actually counts: calls
to the shared GATE HELPERS (get_required_tree_root_zone_mask_utm(),
_fetch_disqualifying_soil_union(), _fetch_road_exclusion_union_utm(),
compute_slope_percent()) at this module's and production's bindings,
NOT every road/canopy/soil touch in the whole pipeline (e.g. build_
pipeline_context()'s own separate existing_roads fetch, and water_
candidate_zones.py's own canopy gate at a different buffer, are outside
this count). One build_pipeline_context() run now reaches canopy, soil
and the slope grid EXACTLY ONCE each -- this module's own -- and the
road helper ZERO times at both bindings, because build_pipeline_context()
supplies that union here (road_exclusion_union_utm= -- see identify_
exclusion_zones()'s OVERRIDES section) and production reads the road
layer off the result. Previously 2/2/2 and 1. test_exclusion_zones.py
asserts these exact counts, not upper bounds: one MORE of any would mean
something is re-fetching; one FEWER on canopy/soil/slope would mean this
module stopped computing its own gates, which production now depends on
it doing.

--- NODATA: "TOO STEEP" AND "NOT MEASURED" ARE DIFFERENT FACTS ---

A cell with no DEM coverage has slope_pct = NaN, fails
`slope_pct <= max_slope_pct`, and therefore lands in the slope layer --
which is what production does too, so the layer matches the gate. But
it means the slope layer is "fails OR cannot be evaluated for slope",
not purely "too steep". narrative_data reports the NaN share
SEPARATELY (`nan_slope_acres`) so a narrative can never conflate the
two. Same split diagnose_exclusion_footprints.py already makes.

--- ONE EXCLUSION GEOMETRY, NOT A CLOSED/RAW PAIR ---

While the closing existed there were two of everything: a closed mask
and a raw_mask per layer, and `excluded_union_utm` alongside
`raw_excluded_union_utm`. The raw halves existed ONLY so the extensive
invariant (`raw ⊆ closed`) was checkable against something real.

With no closing the two halves are the same array and the same
geometry, so the pair is collapsed to one of each. Publishing both
would leave two byte-identical keys with no stated difference, which is
exactly the trap the three-way "eligible" split below is documented
against.

The invariant that replaced it is the plain one:

    excluded_union_utm  ⊆  boundary_polygon_utm

and it holds because the clip to boundary_polygon_utm is now the ONLY
geometric operation applied to an exclusion layer at all. There is no
longer anything that can push a layer outward, which is also why
`render_fill_polygon_utm` is simply the same geometry -- see
identify_exclusion_zones()'s docstring for why that key still exists.

DO NOT ADD A `render_fill.area <= polygon_utm.area` ASSERTION HERE on
the strength of production_area.cluster_and_gate() having one. There it
guards an OPENING (anti-extensive, so there is a larger true footprint
to bound against). Here the two geometries are identical by
construction and the assertion would be vacuous rather than wrong.

--- EXCLUSION SMOOTHING: MEASURED AND REJECTED (AND NOW MOOT) ---

Kept because the measurement is the reason nobody should try it again,
and because the same interaction it found was re-measured on the
eligible union from different geometry (see ELIGIBLE_UNION_SIMPLIFY_
TOLERANCE_CELLS). Note the geometry it was measured on -- the CLOSED
union -- no longer exists; the rejection is recorded, not active.

A branch set out to remove the 5 m cell staircase from the published
exclusion union by running the angular-simplify + Chaikin pass
(raster_grid.angular_smooth_polygon(), the same treatment the
production fill already gets) over it, and deriving eligible_polygon_utm
from the smoothed result so the two layers would share a boundary by
construction.

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
= 0.655 acres INSIDE the union it was applied to. That is gate-excluded
ground republished as selectable, in the one direction an exclusion
cannot afford to be wrong in.

The obvious repair does not work either: unioning the smoothed result
back with the original makes containment exact, but it restores the
original steps everywhere the smooth cut inward and keeps the smoothed
arcs everywhere else, leaving 122% of the vertex count of the staircase
it was meant to remove.

What DID survive that branch is the half that was sound independently:
eligible_polygon_utm is DERIVED as boundary_polygon_utm minus the
published exclusion rather than computed on its own. Two layers derived
from one geometry share a boundary by construction -- no sliver
belonging to neither, no overlap claimed by both -- and that is a
property of deriving, not of smoothing, so it holds for the exact union
just as it would have for a smoothed one. It is asserted for both in
test_exclusion_smoothing.py.

WHAT THE ELIGIBLE UNION DOES INSTEAD, and why it is not the same
question: it is not an exclusion, so an inward error is a display
shortfall rather than a false "you may plant here". It gets a plain
Douglas-Peucker simplify with no Chaikin pass, whose error is bounded
by its own tolerance and measured at exactly that bound. See
ELIGIBLE_UNION_SIMPLIFY_TOLERANCE_CELLS.

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

The `wire` block is frontend-facing METADATA AND GEOMETRY only -- see
_wire_layers() and identify_exclusion_zones()'s docstring for the four
things it carries and why each one exists. There is still no clamp
endpoint, no session state, and no interaction logic here: this remains
an ordinary pipeline step that publishes what the interactive flow
needs, not the interactive flow itself. The production integration is
later, separate work.
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
    connected_components,
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


# Fixed layer order for `layers`, the wire, narrative_data and the pairwise
# overlap matrix -- declared once so the four can never disagree. Ordered by
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

ELIGIBLE_UNION_MIN_CLUSTER_ACRES = 0.1
"""Clusters of selectable ground smaller than this are dropped from the
eligible union. 0.1 ac is 17 cells at the pipeline's 5 m DEM resolution --
enough to clear stranded single-, double- and small-run specks that would
highlight as unselectable-looking confetti, and small enough not to cut into a
real pocket a user might legitimately want to plant. CONFIGURABLE.

AND IT IS CLOSE TO INERT, WHICH IS WORTH SAYING SO THE NUMBER DOES NOT ACQUIRE
FALSE IMPORTANCE. Measured on realistic gate output (test_eligible_union.py's
standard fixture, its own STEP 1 eligible mask): the floor costs 0.0062 acres
-- 11.2433 ac at no floor against 11.2371 ac at this one, one cluster dropped
out of three. The decision is defensible and the measurement supports it; it
is not load-bearing. What IS load-bearing on this geometry is the simplify
bound below.

IT DROPS ISLANDS, NOT HOLES, AND THAT DISTINCTION IS THE WHOLE POINT.
Clustering labels connected components of the ELIGIBLE mask, so what falls
under the floor is a small isolated patch of highlighted ground surrounded by
unhighlighted ground -- visual noise a user cannot act on.

An unhighlighted island INSIDE a highlighted region is a different thing
entirely: it is a hole in the union, it is ground the gates excluded -- a
canopy pocket, a wet spot mid-field -- and a user who sees it is being told
something true. Filling small holes would mean highlighting ground the gates
excluded, which is exactly what the exclusion-smoothing branch was measured
and rejected for (see the module docstring). Nothing in build_eligible_union()
touches interior rings at any stage, and test_eligible_union.py asserts a
sub-floor hole survives a floor that drops a sub-floor island of the same
size.

Applied ONLY to the eligible union. The per-gate layers and every acreage in
narrative_data are untouched by it: those figures are user-facing caution
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
floor and get dropped. CONFIGURABLE.

AND IT IS IMMATERIAL IN PRACTICE. KEEP THIS NOTE -- without it the constant
acquires an importance the measurement does not support. test_eligible_union.
py runs both connectivities at three floors on two fixtures:

  On REALISTIC gate output the two are IDENTICAL at every floor (11.2433 ac at
  no floor, 11.2371 ac at 0.05 and at 0.1 -- the same figure for 4 and for 8).
  Only the cluster COUNT differs, and here not even that.

  Only a DELIBERATELY-DIAGONAL fixture separates them -- corner-touching blocks
  plus a one-cell diagonal chain, built to make them disagree. There the
  largest separation the choice produces anywhere in the measurement is
  0.0741 ac, and it appears at the 0.05 floor (8-connected 0.9637 ac against
  4-connected 0.8896 ac). At the 0.1 acre floor this module actually ships,
  even that fixture comes out identical (0.7907 ac both ways) -- the diagonal
  chain is under the floor whether it is labelled as one cluster or twelve.

So: the reasoning above is sound and worth keeping, and on ground that looks
like ground the choice does not currently change a single square metre. Both
of the eligible union's tuning decisions -- this one and the floor above --
are defensible and close to inert. Neither is where the risk lives."""

ELIGIBLE_UNION_SIMPLIFY_TOLERANCE_CELLS = 1.0
"""Angular simplify tolerance for the eligible union, in DEM CELLS --
multiplied by the DEM's own cell size at the point of use, the same pattern
render_layout_map.PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS uses, so it stays
"one cell" at any resolution. A metres constant would hardcode the grid.

WHAT THIS BUYS, MEASURED (diagnose_eligible_union_staircase.py, two
boundary-shaped fixtures -- A "rolling ground", B "ridge with fingers"): the
5 m cell staircase goes from 690 and 1729 one-cell axis-aligned boundary
segments down to 3 and 1; exterior vertices drop 200 -> 41 and 1289 -> 251;
area moves by +0.09% and -0.89%; polygon and interior-ring counts are
untouched (1/11 and 7/7). The error is BOUNDED and the bound is the tolerance
itself: Douglas-Peucker never moves a ring further than the tolerance it was
given, measured at exactly 5.00 m against a 5 m tolerance on both fixtures and
asserted in test_eligible_union.py.

(Fixture B's baseline moved when ELIGIBLE_UNION_MIN_CLUSTER_ACRES went from
0.05 to 0.1 -- the diagnostic builds its baseline through build_eligible_union()
at the module default, so the higher floor drops one more cluster there and the
whole row shifts with it: 8 polygons to 7, 1743 one-cell segments to 1729, 1304
exterior vertices to 1289. Every figure quoted here is from a re-run at the
floor this module actually ships, not carried over.)

NO CHAIKIN PASS HERE, and this is a measured rejection rather than an
omission -- someone will otherwise add one later on the reasonable-sounding
grounds that simplify leaves hard corners. Measured on the same two fixtures:

  Chaikin ALONE does not remove the staircase -- it leaves 3 one-cell
  segments on the fingered fixture where simplify alone leaves 1 -- and it
  INFLATES vertex count rather than reducing it: 200 -> 399 and 1289 -> 2571,
  the opposite of what this geometry needs, since it is transported to the
  frontend and clamped against.

  Chaikin COMPOSED WITH simplify costs more area than either pass alone
  (ratio 0.9955 and 0.9870, against 1.0009 / 0.9911 for simplify alone) and
  it forfeits the bound: max excursion 10.50 m and 14.58 m against a 5 m
  tolerance. This reproduces the interaction test_exclusion_smoothing.py
  found on the exclusion union -- Chaikin's corner cut scales with edge
  length, and collapsing the staircase is exactly what turns hundreds of 5 m
  edges into a few long ones. The two passes are not independent and
  composing them is worse than either. That is the same interaction measured
  twice now, on different geometry; it is a property of the composition, not
  of one fixture.

A VECTOR OPENING (buffer(-r).buffer(+r)) WAS MEASURED AND REJECTED TOO. It
raises vertex count 4-6x because round buffers emit arcs, and on ground with
narrow fingers it is destructive: r = 3 cells removed 51% of the eligible area
(22.172 -> 10.839 ac) and turned 7 polygons into 15.

Its advertised bounded-error guarantee is also FALSE AS STATED, which is the
reason it looks like the principled choice and is not. An opening deletes any
protrusion too thin to hold a radius-r disc -- ENTIRELY, however long the
protrusion is -- so a one-cell finger's tip ends up its WHOLE LENGTH from the
result, not r. Measured at 45.50 m on the fingered fixture against r = 5 m.
(What IS true of an opening, and is asserted separately, is the weaker
statement that removed ground stays within r of ground that was never
eligible; that is not a bound on how far the published boundary moves.)

Douglas-Peucker's bound is the real one: it never moves a ring further than
the tolerance it was given. Measured at exactly 5.00 m against a 5 m tolerance
on both fixtures, and ASSERTED -- both that it holds and that it is ATTAINED,
since a bound the result sits well inside is an incidental one. This is the
branch's load-bearing guarantee. See diagnose_eligible_union_staircase.
max_inward_excursion(). CONFIGURABLE.
"""


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

    The clip to boundary_polygon_utm is the ONLY geometric operation
    applied to an exclusion layer anywhere in this module. There is nothing
    to clip back to and nothing that can push a layer outward: the mask IS
    the answer and this is its exact ground footprint.

    KNOWN FOLLOW-UP -- NOT FIXED HERE, DELIBERATELY. This intersection can
    return a GeometryCollection carrying zero-area LINE pieces wherever a
    cell edge runs exactly along the boundary. That alignment is not exotic:
    the footprint and a grid-aligned boundary come off the same grid, and it
    crashed the first run of diagnose_eligible_union_staircase.py through the
    identical code path in build_eligible_union(), which now filters to
    polygonal parts via _polygonal_only().

    The same filter belongs here. It is not applied on this branch because
    every pre-existing return key had to stay byte-identical, and this
    function feeds four of them (layers[*]['polygon_utm'], excluded_union_
    utm, render_fill_polygon_utm, and through them geometry_wgs84 and every
    layers[*]['geometry_wgs84'] on the wire). Changing it is a one-line edit
    plus a re-baseline of those keys, and it should be its own change so the
    re-baseline is reviewable on its own.
    """
    if not mask.any():
        return Polygon()
    return cell_union_footprint(dem, mask).intersection(boundary_polygon_utm)


def _pairwise_overlap_acres(dem: dict, masks: dict[str, np.ndarray]) -> list[dict]:
    """
    Measured overlap acreage for every pair of layers, in LAYER_ORDER
    order.

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
    own eligible_mask. identify_exclusion_zones() passes its own gate
    complement, which is byte-identical to it (asserted directly against a
    real compute_step1_eligible_cells() call in test_eligible_union.py) --
    taking it from the masks this module already computed avoids adding a
    sixth redundant gate computation to a module whose docstring is already
    an argument about redundancy.

    THE CLUSTER FLOOR DROPS ISLANDS AND NEVER TOUCHES HOLES. The labelling
    below runs on the ELIGIBLE mask, so a component under the floor is an
    isolated patch of selectable ground surrounded by unselectable ground.
    An unhighlighted island INSIDE the union is an interior ring, not a
    component, and no step here reads or rewrites interior rings -- see
    ELIGIBLE_UNION_MIN_CLUSTER_ACRES for why filling one would be a false
    statement about the ground rather than a tidier highlight.

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
    and is still deliberately NOT changed -- fixing it re-baselines every
    exclusion geometry key at once and should be its own reviewable change.
    Flagged, not silently fixed.)

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
    masks: dict[str, np.ndarray],
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
                      {'layer', 'acres', 'data_available'} ],
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

    EVERY ACREAGE HERE IS RAW, and there is no longer anything that could
    make it otherwise. The per-layer entry used to carry five closing
    fields ('closing_radius_ft', 'effective_closing_radius_ft',
    'closing_radius_cells', 'closed', 'acres_gained_by_closing') reporting
    what the extensive operation added on THIS boundary. The closing is
    gone (module docstring, NO CLOSING) and so are they: a layer's 'acres'
    is now the cell count of the gate's own hit mask and nothing else.

    That is the same rule these figures always followed -- they were the
    one part of the module the closing never reached, precisely because
    they are user-facing. The geometry has now been brought into line with
    the numbers rather than the other way round.
    """
    area_per_cell = cell_area_acres(dem)
    excluded_cells = int(union_mask.sum())
    excluded_acres = excluded_cells * area_per_cell

    layers = []
    for name in LAYER_ORDER:
        layers.append(
            {
                "layer": name,
                "acres": _round1(int(masks[name].sum()) * area_per_cell),
                "data_available": bool(layer_availability[name]),
            }
        )

    naive_sum_acres = sum(int(masks[name].sum()) for name in LAYER_ORDER) * area_per_cell
    nan_slope_cells = int((on_parcel & np.isnan(slope_pct)).sum())
    too_steep_cells = int(masks["slope"].sum()) - nan_slope_cells

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
            "pairs": _pairwise_overlap_acres(dem, masks),
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
    soil_components: list[dict] | None = None,
    soil_geometries: dict | None = None,
) -> dict:
    """
    Computes this parcel's unselectable ground: five per-gate exclusion
    masks, each published as its EXACT cell footprint, unioned into one
    geometry, with the per-layer detail kept alongside so the union can be
    explained rather than just drawn -- plus `eligible_union_utm`, one
    geometry covering every piece of ground that is still selectable.

    NOTHING IS CLOSED, SMOOTHED, BUFFERED OR OPENED HERE. The only
    geometric operation applied to an exclusion layer is the clip to
    boundary_polygon_utm. See the module docstring's NO CLOSING section:
    these acreages are captioned back to the user, so a layer that reports
    more ground than the gate actually hit is a false statement about their
    land.

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

    soil_components/soil_geometries are the RAW-ROW half of the hydric
    gate's override surface, one level below disqualifying_soil_union_
    utm: that parameter skips the derivation entirely, these two let a
    caller keep the derivation here while skipping its two SDA queries.
    They are a PURE PASSTHROUGH to production_area._fetch_disqualifying_
    soil_union() -- this module does not fetch SSURGO rows itself and
    must not start; it only stops FORCING that helper to. Same None-
    falls-back-to-self-fetch convention as everything above (not the
    road union's supplied-None-is-an-answer sentinel: an empty component
    list is a real, meaningful "no map units here" value, but nothing
    passes one today and None stays plainly "not supplied"), and the
    same shapes get_soil_data_for_polygon()/get_soil_geometries_for_
    polygon() return -- i.e. exactly ParcelData's own soil_components/
    soil_geometries fields. Both are ignored when disqualifying_soil_
    union_utm is supplied or check_soil is False, because the helper is
    never reached on those paths. session_cache.run_terrain_warm_up()
    and pipeline_context.build_pipeline_context() both forward them from
    ParcelData, which takes the warm-up and the batch path from two SDA
    queries per run to zero.

    They are APPENDED, not slotted in beside disqualifying_soil_union_utm
    where they would read better -- the same discipline compute_step1_
    eligible_cells()' own exclusion_result= addition followed, because
    inserting a parameter silently re-binds every positional caller. No
    caller passes anything past boundary_coordinates positionally today;
    appending is what keeps that from staying true by luck. test_
    exclusion_zones.py freezes the order.

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
                    'mask': np.ndarray[bool],       # the gate's own hit mask
                    'polygon_utm': Polygon/MultiPolygon,  # its exact cell
                                                    #   footprint, clipped to
                                                    #   the boundary
                    'acres': float,                 # cell-count acres of 'mask'
                    'data_available': bool,
                },
                ...
            },
            'excluded_union_utm': ...,       # all five masks, unioned, clipped
                                             #   to the boundary
            'render_fill_polygon_utm': ...,  # what the map draws -- see below
            'eligible_polygon_utm': ...,     # boundary - excluded_union -- a
                                             #   COMPLEMENT; see below
            'eligible_union_utm': ...,       # MultiPolygon of selectable ground,
                                             #   built from the CELL MASK; see below
            'eligible_union_wgs84': ...,     # the same, GeoJSON, for the wire
            'wire': {...},                   # frontend-facing metadata; see below
            'eligible_mask': np.ndarray[bool],
            'excluded_union_mask': np.ndarray[bool],
            'slope_pct': np.ndarray[float],        # consumed by production; see below
            'slope_only_mask': np.ndarray[bool],   # consumed by production; see below
            'geometry_wgs84': GeoJSON dict,  # excluded_union_utm in EPSG:4326
            'parcel_acres': float,
            'narrative_data': {...},
        }

    THREE THINGS NAMED "ELIGIBLE", AND WHY NONE OF THEM IS REDUNDANT.
    A future reader will assume two of these are the same and delete one.
    They are not:

      eligible_polygon_utm -- boundary_polygon_utm MINUS the published
        exclusion union, in GEOMETRY space. A pure complement: it covers
        every square metre the exclusions do not, down to stranded
        single-cell specks, with the exact cell staircase for an edge.
        Its boundary is by construction the exact inverse of
        excluded_union_utm's, which is the property it exists for -- no
        sliver belongs to neither layer and no ground is claimed by both.

      eligible_union_utm -- built from the CELL MASK forward: STEP 1's
        gate-eligible cells, 8-connected clustered, clusters under
        ELIGIBLE_UNION_MIN_CLUSTER_ACRES dropped, footprints unioned,
        clipped to the boundary, then SIMPLIFIED at
        ELIGIBLE_UNION_SIMPLIFY_TOLERANCE_CELLS. This is the DISPLAY AND
        CLAMPING geometry: what the interactive flow highlights and what a
        drawn polygon is eventually constrained against.

      eligible_mask -- the np.ndarray the other two are reasoned about in,
        and the gate complement in cell space. With no closing it is
        byte-identical to compute_step1_eligible_cells()' own
        eligible_mask (asserted in test_eligible_union.py).

    REMOVING THE CLOSING TOOK ONE OF THE THREE DIFFERENCES AWAY, AND THE
    OTHER TWO ARE STILL REAL. eligible_polygon_utm used to be the
    complement of the CLOSED union, which made it smaller than
    eligible_union_utm by every pinhole the closing had absorbed. That
    difference is gone: both are now built from the same raw gate output.
    What separates them is:

      THE CLUSTER FLOOR. eligible_polygon_utm keeps a stranded 4-cell
      speck; eligible_union_utm drops anything under
      ELIGIBLE_UNION_MIN_CLUSTER_ACRES. Highlighting confetti a user
      cannot meaningfully plant is a worse answer than not highlighting
      it -- but the exact complement of the drawn exclusion is still a
      different and legitimate question.

      THE SIMPLIFY. eligible_polygon_utm carries the exact cell staircase.
      eligible_union_utm has been through one Douglas-Peucker pass at one
      DEM cell, so its ring can sit up to a tolerance off the true cell
      edge -- bounded, measured, and asserted at exactly that bound.

    So they are NOT interchangeable and neither is redundant: one is the
    exact inverse of what is published as excluded, the other is a
    display- and clamping-ready highlight. eligible_union_utm is NOT the
    exact complement of render_fill_polygon_utm and is not supposed to be;
    the shortfall is the specks and the tolerance.

    render_fill_polygon_utm IS excluded_union_utm, the same geometry --
    deliberately, and NOT an oversight to correct by adding an opening or
    a smoothing pass. A production zone's render_fill is an OPENING of its
    polygon_utm, so the two differ there and `render_fill.area <=
    polygon_utm.area` is asserted. Here there is no display-only reduction
    to apply: the mask is the whole answer, and shrinking or smoothing it
    for display would draw a different answer than the one computed (see
    the module docstring's EXCLUSION SMOOTHING section for the measurement
    that settled it). The key exists so this result is KSOP-shaped like
    every other layer's; it carries the same geometry on purpose, and
    render_layout_map.py reads it under that name.

    eligible_mask, slope_pct AND slope_only_mask ARE PRODUCTION'S INPUT.
    They were emitted so the production integration (module docstring,
    THIS MODULE IS PRODUCTION'S GATE COMPUTATION) would be a wiring change
    rather than a rewrite; it now is one. production_area.compute_step1_eligible_
    cells() takes this whole result dict as an optional override and reads
    those three keys plus the canopy/hydric/roads layer masks and their
    data_available flags, instead of computing the same five gates a
    second time. Nothing about how any of them is computed changed to make
    that possible -- with no closing, the masks were already the ones
    production computes (test_eligible_union.py section 0 asserts it
    against a real call, and test_production_area.py asserts the
    integrated path is bit-identical to the self-computed one).

    THAT MAKES THIS MODULE A PRODUCER FOR TWO CONSUMERS, NOT ONE. The
    frontend reads `layers`, `wire`, `eligible_union_utm` and the
    acreages; production reads the raw cell grids. The two must not be
    allowed to drift apart: the day a gate here stops matching
    production's, production's own answer changes silently. That is what
    the bit-identity assertions exist to prevent, and why the five gate
    thresholds live in production_area.py and are imported rather than
    redeclared.
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
                disqualifying_soil_union_utm = _fetch_disqualifying_soil_union(
                    wkt_polygon,
                    dem,
                    soil_components=soil_components,
                    soil_geometries=soil_geometries,
                )
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

    masks = {
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

    # ---- the union, and the eligible complement -----------------------
    #
    # No morphological pass runs between the gates above and the geometry
    # below -- see the module docstring's NO CLOSING section. Each mask is
    # the gate's own hits and nothing else, so there is exactly ONE union
    # rather than a closed/raw pair, and one cell-space clip to the parcel
    # rather than one guarding a closing that could push cells outward.
    union_mask = np.zeros(dem["array"].shape, dtype=bool)
    for name in LAYER_ORDER:
        union_mask |= masks[name]
    union_mask &= on_parcel

    # Everything on the parcel the five gates do not take. With no closing
    # this is byte-identical to compute_step1_eligible_cells()' own
    # eligible_mask -- asserted against a real call in test_eligible_union.py,
    # not assumed -- which is why the SAME array feeds both the return key and
    # build_eligible_union() below. It is not consumed elsewhere in this
    # branch; see this function's docstring.
    eligible_mask = on_parcel & (~union_mask)

    excluded_union_utm = _mask_polygon(dem, union_mask, boundary_polygon_utm)
    eligible_polygon_utm = boundary_polygon_utm.difference(excluded_union_utm)
    eligible_union_utm = build_eligible_union(dem, eligible_mask, boundary_polygon_utm)

    layers = {}
    area_per_cell = cell_area_acres(dem)
    for name in LAYER_ORDER:
        layers[name] = {
            "mask": masks[name],
            "polygon_utm": _mask_polygon(dem, masks[name], boundary_polygon_utm),
            "acres": round(int(masks[name].sum()) * area_per_cell, 2),
            "data_available": layer_availability[name],
        }

    parcel_acres = boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE

    return {
        "layers": layers,
        "excluded_union_utm": excluded_union_utm,
        # The SAME geometry as excluded_union_utm, on purpose -- see this
        # function's docstring. There is no display-only reduction to apply
        # to an exact cell footprint. render_layout_map.py reads this key.
        "render_fill_polygon_utm": excluded_union_utm,
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
        # --- the two cell-space intermediates production consumes ---------
        #
        # Both are already computed above; publishing them adds no work and
        # changes nothing this module itself does. They exist for the same
        # reason eligible_mask does: production_area.compute_step1_eligible_
        # cells() needs the WHOLE of STEP 1's gate output to stop computing
        # it a second time, and eligible_mask alone is not the whole of it.
        #
        #   slope_pct       -- compute_slope_percent()'s own grid, the single
        #                      most expensive thing either module computes.
        #                      Withholding it would leave production calling
        #                      compute_slope_percent() itself and the "one
        #                      slope grid per run" half of the deduplication
        #                      unachievable.
        #   slope_only_mask -- STEP 1's slope-and-setback survivor set, which
        #                      is also the UNIVERSE the canopy/hydric/road
        #                      layers above were evaluated over. It is exactly
        #                      recoverable as eligible_mask | canopy | hydric
        #                      | roads, but reconstructing a producer's own
        #                      value in the consumer is how the two drift.
        #
        # NOT used by anything in this module, and deliberately NOT part of
        # `wire` or narrative_data -- these are raw numpy grids for an
        # in-process caller, not frontend-facing output.
        "slope_pct": slope_pct,
        "slope_only_mask": slope_only_mask,
        "geometry_wgs84": (
            transform_geom(dem["crs"], "EPSG:4326", mapping(excluded_union_utm))
            if not excluded_union_utm.is_empty
            else None
        ),
        "parcel_acres": parcel_acres,
        "narrative_data": build_narrative_data(
            dem,
            masks,
            union_mask,
            eligible_mask,
            on_parcel,
            slope_pct,
            parcel_acres,
            max_slope_pct,
            layer_availability,
        ),
    }
