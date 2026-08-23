"""
diagnose_exclusion_footprints.py

Standing, read-only diagnostic: measures the REAL per-gate exclusion
footprints STEP 1 of the production pipeline
(production_area.compute_step1_eligible_cells()) actually produces for the
reference property boundaries, and measures what a small disc-element
MORPHOLOGICAL CLOSING would do to each of them.

WHY THIS EXISTS
---------------
Production zone selection is being reconsidered as an interactive step:
the frontend would show the user the parcel's EXCLUSION layers (slope,
canopy, hydric soil, existing farm roads, boundary setback) and let them
pick production ground out of what is left. That needs each exclusion to be a
coherent, readable polygon rather than a scatter of cells with pinholes
in it -- hence the closing proposal (dilate then erode, disc element,
applied PER GATE, so single-cell gaps inside an excluded region are
absorbed and the exclusion reads as one shape).

Closing an EXCLUSION is extensive: exclusions grow slightly. That is the
safe direction -- over-excluding by a cell beats letting someone select
ground with trees on it, which is what filling holes in the ELIGIBLE
layer would have done.

Nothing about that has been decided. This script only measures. It adds
no constants to any production module, changes no pipeline module, and is
wired into nothing -- the closing radii are CLI parameters here and
nowhere else.

WHAT IT ANSWERS
---------------
1. What each gate's raw cell footprint actually looks like: polygon count,
   interior-ring (hole) count and hole-size distribution, area, exterior
   vertex count (the transport figure), and fragmentation (the largest
   polygon's share of the gate's total area).
2. Whether a small closing CONSOLIDATES that footprint or OVER-MERGES it.
   The over-merge detector is the DISTRIBUTION of newly-excluded ground,
   not its total: a closing that only absorbs pinholes gains many tiny
   regions; a closing that merges two canopy stands 30 feet apart gains
   ONE BIG region -- the farmable strip between them. Both can total the
   same acreage, which is exactly why the total alone cannot tell them
   apart. Every radius reports the full gained-region size distribution.
3. Whether the boundary setback is even a candidate for this, or is
   already a clean ring with no interior gaps.
4. Whether the SLOPE layer -- the largest exclusion on both reference
   boundaries, larger than canopy, hydric, roads and setback combined --
   is scattered steep patches (which a closing would consolidate a great
   deal) or one large steep region along a valley side (which a closing
   would not touch, meaning the eligible layer's holes are simply real).

--- WHY SLOPE IS A LAYER HERE ---

Structurally slope is PRIOR to the other gates: it is half of what defines
slope_only_mask, which canopy, hydric and road then operate within. An
earlier version of this diagnostic reported only those four and treated
slope as scenery, which made the headline result misleading -- closing the
four barely moved the eligible layer's holes, and in the slope-intersected
variant did not move them at all. The holes were slope, and slope was not
being closed.

From the user's point of view slope is not prior to anything; it is one
more reason a piece of ground cannot be selected, and it is the dominant
one. So it is reported as a fifth layer with exactly the same treatment as
the other four: same footprint stats, same closing sweep, same over-merge
reading, same GeoJSON export.

    slope_fail = on_parcel & ~slope_ok

derived from what STEP 1 APPLIED -- its returned 'slope_pct' and the
max_slope_pct the run was actually given -- never from
MAX_PRODUCTION_SLOPE_PCT, so the layer stays correct under
--max-slope-pct.

SLOPE AND SETBACK ARE DISJOINT BY CONSTRUCTION. The setback layer requires
slope_ok; the slope layer requires ~slope_ok. Ring ground that also fails
slope therefore lands wholly in the slope layer, which is exactly why the
setback figure is a LOWER BOUND on the real ring. The pairs that DO
overlap are canopy/hydric/road with each other. The report measures every
pairwise overlap and prints it next to a naive sum, so the "do not add
these up" caution is checkable rather than merely stated.

--- STEP 0 FINDINGS (verified against the current source, not assumed) ---

Per-gate masks on compute_step1_eligible_cells()'s return dict, all
np.ndarray[bool] the same shape as dem['array'] (rows, cols):

    'tree_root_zone_hit'  -- canopy gate hits
    'hydric_hit'          -- hydric-soil gate hits
    'road_hit'            -- existing-road right-of-way gate hits
    'slope_only_mask'     -- slope-eligible AND on-parcel-post-setback,
                             BEFORE the three gates above
    'eligible_mask'       -- slope_only_mask & ~hydric_hit
                             & ~tree_root_zone_hit & ~road_hit

raster_grid.binary_erode()/binary_dilate() both take element="square"
(the default, unchanged) or element="disc" (added by the production
render-opening work; a single radius-r Euclidean pass over
_disc_offsets()). raster_grid.cell_union_footprint() builds exact
cell-square geometry (each cell's own ground square, corners computed
from `origin +/- N * resolution` so shared edges are bit-for-bit
identical), NOT a hull of centers and NOT a buffer.

THE BOUNDARY SETBACK HAS NO MASK OF ITS OWN. It is folded into
slope_only_mask, which is built as a single combined test:

    slope_ok = (~isnan(slope_pct)) & (slope_pct <= max_slope_pct)
    on_parcel_boundary_utm = boundary_polygon_utm.buffer(-setback)
    slope_only_mask[r, c] = slope_ok[r, c] and <center inside the shrunk ring>

and every other gate is evaluated ONLY inside slope_only_mask: hydric and
road iterate `np.argwhere(slope_only_mask)`, and canopy is literally
`slope_only_mask & tree_root_zone_mask_utm`. So cells outside the shrunk
boundary are NEVER TESTED by the canopy, hydric or road gates at all --
confirmed, and it is the reason for the "unevaluated ring" section below.

The setback-only footprint is therefore DERIVED, from what STEP 1 itself
applied rather than from the PRODUCTION_BOUNDARY_SETBACK_METERS module
constant:

    setback_only = on_parcel & slope_ok & ~slope_only_mask

-- on-parcel ground (cell center inside the FULL, unshrunk boundary) that
clears the slope gate and is excluded anyway, which can only be because
it sits in the setback ring. `slope_ok` is reconstructed from the
returned 'slope_pct' array and the max_slope_pct actually passed to
compute_step1_eligible_cells(); `on_parcel` uses the same
pixel_center_xy() + prepared-geometry containment test STEP 1 itself
uses, against the unshrunk boundary. No negative buffer is recomputed
here.

KNOWN LIMIT of that derivation: it recovers only the part of the ring
that CLEARS SLOPE. Ring ground that also fails the slope gate is
indistinguishable, from STEP 1's outputs alone, from off-ring ground that
fails slope -- slope_only_mask collapses both tests into one array. The
ring acreage reported below is thus a LOWER BOUND on the true ring, and
is labelled as such where it is printed.

--- DATA FETCHING ---

The DEM and all three gate inputs are fetched DIRECTLY -- dem_data.
get_dem_for_boundary() plus production_area's own _fetch_tree_root_zone_
mask_utm() / _fetch_disqualifying_soil_union() / _fetch_road_exclusion_
union_utm() (the same helpers the real pipeline uses, not reimplemented
here). parcel_data.fetch_parcel_data() is deliberately NOT used: its
Layer 1 gate also pulls climate, and an unrelated Open-Meteo outage has
already blocked terrain-only diagnostics in this work twice. No input
here needs the full parcel fetch.

Each gate fetch degrades gracefully and says so -- a gate whose fetch
failed is reported as UNCHECKED, never as "clean". A run with a failed
canopy fetch still produces real hydric/road/setback numbers, and the
slope and setback layers need no fetch beyond the DEM at all.

--- THE ELIGIBLE LAYER, IN THREE FORMS ---

  FORM A  boundary - (four closed exclusions; slope NOT subtracted)
  FORM B  FORM A intersected with the RAW, unclosed slope gate
  FORM C  boundary - (all FIVE closed exclusions, slope included)

FORM C is what a frontend would actually clamp against: every reason
ground is unselectable, each consolidated by its own closing. A and B are
each missing something -- A hands the user steep ground; B subtracts slope
but leaves every scattered steep cell as an unclosed hole -- and are kept
alongside C so the three can be compared directly.

--- CLOSING RADIUS, IN CELLS ---

binary_dilate()/binary_erode() take a radius in CELLS. The conversion
(raster_grid.closing_radius_cells(): `round(radius_m / cell_size)`,
cell_size = (px + py) / 2) and the padded dilate-then-erode itself
(raster_grid.disc_closing()) are SHARED with exclusion_zones.py, the
module that applies in production the radii this script measures -- they
were extracted from here when that module was written, verbatim and with
no output change. This script prints both the requested metres and the
effective radius actually applied. The conversion deliberately does NOT
use raster_grid.waist_erosion_radius_cells(): that converts a minimum
WAIST WIDTH into a radius and therefore HALVES it (a waist of w is
severed by eroding w/2).
A closing radius is already a radius. At the pipeline's 5 m DEM
resolution this quantization is coarse and matters: r=5 m is a ONE-cell
disc (the 4-neighbourhood) and r=10 m is a two-cell disc, so every
reported radius is really one of a very small number of distinct
operations. The printed "effective" line is the one to read.

The closing is computed on a mask PADDED by radius+1 cells and then
cropped back, because raster_grid's _shift() treats everything outside
the array bounds as background: without the pad, the erosion half of the
closing would eat into any exclusion touching the grid edge and the
result would not be extensive. With the pad, closed >= raw always (the
synthetic fixtures in test_diagnose_exclusion_footprints.py assert this).

--- USAGE ---

    python3 diagnose_exclusion_footprints.py
    python3 diagnose_exclusion_footprints.py --closing-radii 0 5 10 15
    python3 diagnose_exclusion_footprints.py --boundary new
    python3 diagnose_exclusion_footprints.py --geojson footprints
    python3 diagnose_exclusion_footprints.py --dry-run

--dry-run exercises every code path in this file against a synthetic DEM
and synthetic gate inputs, with no network at all -- for checking the
report renders and the arithmetic runs, NOT for any real conclusion about
the property.

Requires real network access otherwise (USGS 3DEP for the DEM and the
lidar HAG canopy, USDA SSURGO for hydric soil, the road layer for
existing farm roads) -- this is a live diagnostic against a real property,
not an offline test.
"""

import argparse
import json
import math
import statistics

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import MultiPolygon, Point, Polygon, mapping
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from production_area import (
    MAX_PRODUCTION_SLOPE_PCT,
    _CANOPY_CHECK_UNCHECKED,
    _ROAD_CHECK_UNCHECKED,
    _SOIL_CHECK_UNCHECKED,
    _fetch_disqualifying_soil_union,
    _fetch_road_exclusion_union_utm,
    _fetch_tree_root_zone_mask_utm,
    compute_step1_eligible_cells,
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

# The two reference property boundaries every diagnostic in this thread
# runs against. OLD is the original digitization; NEW is the corrected
# one. Both are run by default so a difference between them is visible
# rather than hidden behind whichever one a given script happened to pick.
OLD_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
NEW_BOUNDARY = [
    (-79.98395562171937, 40.6460162710763),
    (-79.98374104499818, 40.642584987588364),
    (-79.98047947883607, 40.64432504438868),
    (-79.98097300529480, 40.645089354064524),
    (-79.98150944709779, 40.645170663089445),
    (-79.98266816139223, 40.64596748629134),
]

REFERENCE_BOUNDARIES = {"old": OLD_BOUNDARY, "new": NEW_BOUNDARY}

DEFAULT_CLOSING_RADII_METERS = [0.0, 5.0, 10.0]

# Hole-size threshold the report counts against. Purely a REPORTING
# bucket for this diagnostic -- not a decision, not a threshold anything
# acts on, and deliberately not a constant in any production module.
SMALL_HOLE_ACRES = 0.1

# Gained-region size buckets, in acres, for the over-merge distribution.
# Same status as SMALL_HOLE_ACRES: reporting only.
GAIN_BUCKET_EDGES_ACRES = [0.05, 0.25, 1.0]

# Layer labels used both as the printed gate heading and as the key the
# eligible-layer section selects gates by, so the two can never drift apart.
# Diagnostic-local, like every other constant in this file.
SETBACK_GATE_LABEL = "boundary setback (derived)"
SLOPE_GATE_LABEL = "slope (derived)"

# The largest a gained region can be, IN CELLS, and still plausibly be an
# absorbed pinhole rather than a bridge between two separate regions --
# 4 cells is a 2x2 gap. Used only by the one-line reading aid below, in
# cells rather than acres so it means the same thing at any DEM
# resolution. Reporting only, like the two above.
PINHOLE_MAX_CELLS = 4


# ---------------------------------------------------------------------------
# geometry / measurement helpers
# ---------------------------------------------------------------------------


# closing_radius_cells(), effective_radius_meters() and disc_closing() used to
# live here, inline. They were EXTRACTED to raster_grid.py verbatim when
# exclusion_zones.py needed the same closing: this script MEASURED the per-gate
# radii that module now APPLIES in production, and two copies of the conversion
# and the padded dilate-then-erode could drift apart silently. Imported above,
# not reimplemented -- this script's output is unchanged by the move.


def _polygon_parts(geom) -> list[Polygon]:
    """Every Polygon part of a Polygon/MultiPolygon/empty geometry."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [g for g in geom.geoms if not g.is_empty]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon) and not g.is_empty]


def _acres(square_meters: float) -> float:
    return square_meters / SQUARE_METERS_PER_ACRE


def footprint_metrics(dem: dict, mask: np.ndarray) -> dict:
    """
    Every reported figure for ONE boolean cell mask, measured off the
    exact-cell-square footprint cell_union_footprint() builds:

        cell_count / acres_from_cells   -- the raster truth
        polygon_count                   -- parts after the union
        hole_count + hole_acres         -- interior rings, every one of them
        exterior_vertex_count           -- the transport figure: total
                                           vertices across every part's
                                           EXTERIOR ring (shapely repeats
                                           the first point to close a ring;
                                           that duplicate is not counted)
        interior_vertex_count           -- same, for interior rings, so the
                                           real transport cost of keeping
                                           holes is visible next to the
                                           cost of dropping them
        largest_polygon_share           -- largest part's area / total
                                           polygon area, the fragmentation
                                           measure (1.0 = one dominant
                                           shape, small = scattered)
    """
    cell_count = int(mask.sum())
    acres_from_cells = cell_count * cell_area_acres(dem)

    footprint = cell_union_footprint(dem, mask)
    parts = _polygon_parts(footprint)

    hole_acres = []
    exterior_vertices = 0
    interior_vertices = 0
    part_areas = []
    for part in parts:
        exterior_vertices += max(0, len(part.exterior.coords) - 1)
        part_areas.append(part.area)
        for ring in part.interiors:
            interior_vertices += max(0, len(ring.coords) - 1)
            hole_acres.append(_acres(Polygon(ring).area))

    total_polygon_area = sum(part_areas)

    return {
        "cell_count": cell_count,
        "acres_from_cells": acres_from_cells,
        "polygon_acres": _acres(total_polygon_area),
        "polygon_count": len(parts),
        "hole_count": len(hole_acres),
        "hole_acres": sorted(hole_acres),
        "small_hole_count": sum(1 for a in hole_acres if a < SMALL_HOLE_ACRES),
        "exterior_vertex_count": exterior_vertices,
        "interior_vertex_count": interior_vertices,
        "largest_polygon_share": (max(part_areas) / total_polygon_area) if total_polygon_area > 0 else 0.0,
        "largest_polygon_acres": _acres(max(part_areas)) if part_areas else 0.0,
        "footprint": footprint,
    }


def gained_region_distribution(dem: dict, raw: np.ndarray, closed: np.ndarray) -> dict:
    """
    The over-merge detector.

    `gained` is the ground the closing newly excludes (closed & ~raw) --
    the COST of the closing, and the number that decides whether a radius
    is safe. It is broken into 8-connected regions and the SIZE
    DISTRIBUTION of those regions is what is returned, because the total
    alone cannot distinguish the two failure modes:

      * absorbing pinholes  -> many tiny regions, largest region tiny
      * merging two stands  -> few regions, ONE of them large (the
                               farmable strip that just got swallowed)

    Both can total identical acreage. `largest_region_acres` and the
    bucket histogram are what tell them apart.
    """
    gained = closed & (~raw)
    area_per_cell = cell_area_acres(dem)

    labels, num = connected_components(gained)
    region_cell_counts = []
    for label in range(num):
        region_cells = int((labels == label).sum())
        if region_cells:
            region_cell_counts.append(region_cells)
    region_cell_counts.sort(reverse=True)
    region_acres = [n * area_per_cell for n in region_cell_counts]

    buckets = {}
    lo = 0.0
    for edge in GAIN_BUCKET_EDGES_ACRES:
        buckets[f"{lo:g}-{edge:g} ac"] = sum(1 for a in region_acres if lo <= a < edge)
        lo = edge
    buckets[f">={lo:g} ac"] = sum(1 for a in region_acres if a >= lo)

    return {
        "gained_mask": gained,
        "gained_cells": int(gained.sum()),
        "gained_acres": int(gained.sum()) * area_per_cell,
        "region_count": len(region_acres),
        "region_acres_desc": region_acres,
        "region_cells_desc": region_cell_counts,
        "largest_region_acres": region_acres[0] if region_acres else 0.0,
        "largest_region_cells": region_cell_counts[0] if region_cell_counts else 0,
        "median_region_acres": statistics.median(region_acres) if region_acres else 0.0,
        "buckets": buckets,
    }


def on_parcel_mask(dem: dict, boundary_polygon_utm: Polygon) -> np.ndarray:
    """
    Cells whose CENTER lies inside the FULL, unshrunk parcel boundary --
    the same pixel_center_xy() + prepared-containment test STEP 1 itself
    uses for its own (shrunk) on-parcel test, applied to the unshrunk
    polygon. This is the only place this script computes a containment
    test of its own, and it deliberately applies NO setback: the setback's
    effect is recovered from STEP 1's own slope_only_mask instead.
    """
    rows, cols = dem["array"].shape
    prepared = prep(boundary_polygon_utm)
    mask = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        for c in range(cols):
            if prepared.contains(Point(pixel_center_xy(dem, r, c))):
                mask[r, c] = True
    return mask


def derive_setback_only_mask(step1: dict, on_parcel: np.ndarray, max_slope_pct: float) -> np.ndarray:
    """
    The setback-only exclusion footprint, derived exactly as the module
    docstring describes: on-parcel ground that CLEARS THE SLOPE GATE and
    is still outside slope_only_mask, which can only be because the
    shrunk-boundary half of that combined test rejected it.

    slope_ok is rebuilt from STEP 1's own returned 'slope_pct' array using
    the max_slope_pct actually passed to it -- so this reflects what STEP
    1 APPLIED, not what PRODUCTION_BOUNDARY_SETBACK_METERS currently says.

    Recovers only the slope-CLEARING part of the ring; see the module
    docstring's KNOWN LIMIT.
    """
    return on_parcel & derive_slope_ok(step1, max_slope_pct) & (~step1["slope_only_mask"])


def derive_slope_ok(step1: dict, max_slope_pct: float) -> np.ndarray:
    """
    STEP 1's own slope gate, rebuilt from the 'slope_pct' array it returns
    and the max_slope_pct it was actually CALLED with -- exactly the
    expression compute_step1_eligible_cells() uses internally
    (production_area.py:709). Not MAX_PRODUCTION_SLOPE_PCT: taking the
    module constant would silently disagree with the run whenever
    --max-slope-pct overrides it.
    """
    slope_pct = step1["slope_pct"]
    return (~np.isnan(slope_pct)) & (slope_pct <= max_slope_pct)


def derive_slope_fail_mask(step1: dict, on_parcel: np.ndarray, max_slope_pct: float) -> np.ndarray:
    """
    The slope exclusion footprint: on-parcel ground that FAILS the slope
    gate. Derived the same way the setback layer is -- from what STEP 1
    APPLIED (its returned 'slope_pct' plus the max_slope_pct passed to it),
    never from the module constant.

    Slope has no per-gate hit mask of its own for the same reason the
    setback does not: both are folded into slope_only_mask as one combined
    test. But unlike the setback, slope IS separable on its own, because
    'slope_pct' is returned in full and the threshold is known.

    WHY THIS IS A LAYER AT ALL. Structurally slope is PRIOR to the other
    gates -- it is half of what defines slope_only_mask, which canopy,
    hydric and road then operate within. That is a fact about the code. It
    is not a fact about the user: to someone picking production ground out
    of what is left, slope is simply one more reason a piece of ground is
    not selectable, and on both reference boundaries it is the LARGEST such
    reason -- bigger than canopy, hydric, roads and setback combined. A
    per-gate exclusion view that omits it is not showing the user why most
    of their unselectable ground is unselectable.

    NOTE the nodata case: a cell whose slope_pct is NaN (no DEM coverage)
    fails `slope_pct <= max_slope_pct` and so lands in this layer. That
    matches what STEP 1 does -- such a cell is excluded from
    slope_only_mask too -- but it means this layer is "fails or cannot be
    evaluated for slope", not purely "too steep". On-parcel NaN slope is
    reported separately in the layer-relationship section below so the two
    are never confused.
    """
    return on_parcel & (~derive_slope_ok(step1, max_slope_pct))


def layer_overlap_matrix(dem: dict, layers: list[tuple[str, np.ndarray]]) -> list[tuple[str, str, float]]:
    """
    Pairwise overlap acreage between every pair of exclusion layers.

    This exists because the layers MUST NOT BE SUMMED, and prose saying so
    is not checkable. Printing the measured pairwise overlap lets a reader
    see exactly which pairs share ground and by how much, rather than
    taking a comment's word for it. Same caution the narrative_data work
    handles with its paired `*_excluded` / `*_only_excluded` figures.

    Returns (layer_a, layer_b, overlap_acres) for every pair, in the order
    given.
    """
    area_per_cell = cell_area_acres(dem)
    pairs = []
    for i in range(len(layers)):
        for j in range(i + 1, len(layers)):
            name_a, mask_a = layers[i]
            name_b, mask_b = layers[j]
            pairs.append((name_a, name_b, int((mask_a & mask_b).sum()) * area_per_cell))
    return pairs


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------


def _hole_summary(metrics: dict) -> str:
    holes = metrics["hole_acres"]
    if not holes:
        return "holes: 0"
    return (
        f"holes: {len(holes)}  "
        f"(acres min {min(holes):.4f} / median {statistics.median(holes):.4f} / max {max(holes):.4f}; "
        f"{metrics['small_hole_count']} below {SMALL_HOLE_ACRES:g} ac)"
    )


def print_metrics(title: str, metrics: dict, indent: str = "    ") -> None:
    print(f"{indent}{title}")
    print(
        f"{indent}  cells {metrics['cell_count']:>7}   "
        f"acres {metrics['acres_from_cells']:>9.3f}   "
        f"polygons {metrics['polygon_count']:>4}"
    )
    print(f"{indent}  {_hole_summary(metrics)}")
    print(
        f"{indent}  exterior vertices {metrics['exterior_vertex_count']:>6}   "
        f"interior (hole) vertices {metrics['interior_vertex_count']:>6}"
    )
    print(
        f"{indent}  largest polygon {metrics['largest_polygon_acres']:.3f} ac = "
        f"{metrics['largest_polygon_share'] * 100:.1f}% of this layer's area "
        f"(fragmentation: 100% = one shape)"
    )


def print_gain(gain: dict, baseline: dict, metrics: dict, indent: str = "    ") -> None:
    absorbed_polygons = baseline["polygon_count"] - metrics["polygon_count"]
    absorbed_holes = baseline["hole_count"] - metrics["hole_count"]
    print(
        f"{indent}  vs raw: polygons {baseline['polygon_count']} -> {metrics['polygon_count']} "
        f"({absorbed_polygons:+d} absorbed)   "
        f"holes {baseline['hole_count']} -> {metrics['hole_count']} ({absorbed_holes:+d} absorbed)"
    )
    print(
        f"{indent}  COST -- acres newly excluded: {gain['gained_acres']:.3f} ac "
        f"across {gain['region_count']} contiguous region(s)"
    )
    if gain["region_count"]:
        top = ", ".join(f"{a:.3f}" for a in gain["region_acres_desc"][:5])
        print(
            f"{indent}  largest single gained region: {gain['largest_region_acres']:.3f} ac   "
            f"median {gain['median_region_acres']:.4f} ac   top 5: [{top}]"
        )
        print(f"{indent}  gained-region size distribution: {gain['buckets']}")
        print(f"{indent}  {_over_merge_read(gain)}")


def _print_layer_relationships(
    dem: dict, gates: list, on_parcel: np.ndarray, step1: dict, max_slope_pct: float
) -> None:
    """
    How the five exclusion layers relate to each other, MEASURED rather
    than asserted -- because the one thing a reader must not do with them
    is add them up, and a comment saying so is not checkable.

    Prints each layer's own acreage, then the pairwise overlap acreage for
    every pair. The narrative_data work handles the same hazard with paired
    `*_excluded` / `*_only_excluded` figures; this is the per-gate
    equivalent, and it is computed live so it stays honest if any
    derivation ever changes.
    """
    area_per_cell = cell_area_acres(dem)
    layers = [(name, mask) for name, mask, _ in gates]

    print("-" * 78)
    print("HOW THE FIVE LAYERS RELATE (measured -- DO NOT SUM THESE)")
    print("-" * 78)
    total_if_summed = 0.0
    for name, mask in layers:
        acres = int(mask.sum()) * area_per_cell
        total_if_summed += acres
        print(f"    {name:<28} {acres:>8.3f} ac")
    union = np.zeros_like(on_parcel)
    for _, mask in layers:
        union |= mask
    union_acres = int(union.sum()) * area_per_cell
    print(
        f"    {'--- naive sum':<28} {total_if_summed:>8.3f} ac   vs the real UNION "
        f"{union_acres:.3f} ac  (difference = double-counted overlap)"
    )
    print()

    print("    pairwise overlap:")
    overlaps = layer_overlap_matrix(dem, layers)
    for name_a, name_b, overlap_acres in overlaps:
        marker = "" if overlap_acres > 0 else "   (disjoint)"
        print(f"      {name_a:<28} & {name_b:<28} {overlap_acres:>7.3f} ac{marker}")
    print()

    # The two derived layers are disjoint BY CONSTRUCTION, and that fact is
    # the whole reason the setback figure is a lower bound. Say which way
    # the shared ground was attributed rather than leaving the reader to
    # infer it from a zero.
    print(
        "    SLOPE AND SETBACK ARE DISJOINT BY CONSTRUCTION, NOT BY LUCK. The setback layer is\n"
        "    derived as `on_parcel & slope_ok & ~slope_only_mask` -- it REQUIRES slope_ok -- while\n"
        "    the slope layer is `on_parcel & ~slope_ok`. Ring ground that ALSO fails slope\n"
        "    therefore lands wholly in the SLOPE layer and not at all in the setback layer, which\n"
        "    is exactly why the setback figure is a LOWER BOUND on the real ring rather than a\n"
        "    measurement of it. Neither layer can be corrected for this from STEP 1's returned\n"
        "    arrays alone: slope_only_mask collapses the slope and shrunk-boundary tests into one\n"
        "    array, so the ring's steep part is not recoverable."
    )
    print(
        "    THE PAIRS THAT DO OVERLAP are canopy/hydric/road with each other -- one cell can be\n"
        "    both wooded and hydric, and each gate is evaluated independently over the same\n"
        "    slope_only_mask. Those are the layers a summed 'total excluded' figure would\n"
        "    double-count."
    )

    # Slope's NaN component, kept separate so "too steep" is never confused
    # with "no DEM coverage" -- both fail `slope_pct <= max_slope_pct`.
    nan_slope = on_parcel & np.isnan(step1["slope_pct"])
    nan_acres = int(nan_slope.sum()) * area_per_cell
    print(
        f"    of the slope layer, {nan_acres:.3f} ac is on-parcel ground with NO SLOPE VALUE at all\n"
        f"    (NaN slope_pct -- no DEM coverage), not ground measured as too steep. STEP 1 excludes\n"
        f"    both the same way; this diagnostic reports them apart so they are not conflated."
    )
    print()


def _over_merge_read(gain: dict) -> str:
    """
    A one-line plain reading of the distribution. This is a READING AID,
    not a verdict -- the numbers above it are the evidence; whether a
    radius is acceptable is a decision for whoever reads this, not for
    this script.

    The primary test is the SIZE IN CELLS of the largest gained region,
    not its share of the total and not its acreage. Share alone is
    useless at the low end (a single absorbed pinhole is trivially 100%
    of its own gain), and acreage alone depends on the DEM resolution.
    A pinhole is by definition a gap of at most a few cells -- anything
    bigger is the closing bridging something, and a bridge is farmable
    ground being swallowed.
    """
    if gain["region_count"] == 0:
        return "read: nothing gained -- this layer was already closed at this radius."

    share = gain["largest_region_acres"] / gain["gained_acres"] if gain["gained_acres"] > 0 else 0.0
    largest_cells = gain["largest_region_cells"]

    if largest_cells <= PINHOLE_MAX_CELLS:
        return (
            f"read: PINHOLE SHAPE -- the largest gained region is {largest_cells} cell(s), "
            f"at most a {PINHOLE_MAX_CELLS}-cell gap, consistent with absorbing pinholes rather "
            "than bridging separate regions."
        )
    if gain["largest_region_acres"] >= GAIN_BUCKET_EDGES_ACRES[-1] or share >= 0.5:
        return (
            f"read: OVER-MERGE SHAPE -- one region is {largest_cells} cells "
            f"({gain['largest_region_acres']:.3f} ac, {share * 100:.0f}% of the gain), which is what "
            "merging two separate stands (and swallowing the farmable ground between them) looks "
            "like, not what absorbing pinholes looks like."
        )
    return (
        f"read: MIXED -- the largest gained region is {largest_cells} cells "
        f"({gain['largest_region_acres']:.3f} ac, {share * 100:.0f}% of the gain), too big for a "
        "pinhole but not dominant; check the top-5 list above for whether any single one is real "
        "farmable ground."
    )


# ---------------------------------------------------------------------------
# data fetching
# ---------------------------------------------------------------------------


def fetch_gate_inputs(boundary_coordinates: list, dem: dict) -> dict:
    """
    The three optional gate inputs, fetched DIRECTLY through
    production_area's own helpers -- no parcel_data.fetch_parcel_data(),
    so no climate fetch is in the path (see module docstring).

    Each failure degrades to that gate's own "not checked at all" sentinel
    and is reported, never silently treated as a clean gate. Returns the
    three values plus a per-gate status string for the report header.
    """
    result = {
        "soil": _SOIL_CHECK_UNCHECKED,
        "canopy": _CANOPY_CHECK_UNCHECKED,
        "road": _ROAD_CHECK_UNCHECKED,
        "status": {},
    }

    try:
        canopy_mask = _fetch_tree_root_zone_mask_utm(boundary_coordinates, dem)
        if canopy_mask is None:
            result["status"]["canopy"] = (
                "UNCHECKED -- no USGS 3DEP lidar HAG coverage for this boundary at all "
                "(the real pipeline hard-fails here; this diagnostic reports and continues)"
            )
        else:
            result["canopy"] = canopy_mask
            result["status"]["canopy"] = f"fetched ({int(canopy_mask.sum())} root-zone cells on the DEM grid)"
    except Exception as e:  # noqa: BLE001 -- a diagnostic reports every failure and keeps going
        result["status"]["canopy"] = f"UNCHECKED -- fetch failed: {e}"

    try:
        soil_union = _fetch_disqualifying_soil_union(
            coordinates_to_wkt_polygon(boundary_coordinates), dem
        )
        result["soil"] = soil_union
        result["status"]["soil"] = (
            "fetched -- no disqualifying (hydric) soil found (checked, genuinely clean)"
            if soil_union is None
            else "fetched (disqualifying hydric soil union present)"
        )
    except Exception as e:  # noqa: BLE001
        result["status"]["soil"] = f"UNCHECKED -- fetch failed: {e}"

    try:
        road_union = _fetch_road_exclusion_union_utm(boundary_coordinates, dem)
        result["road"] = road_union
        result["status"]["road"] = (
            "fetched -- no roads found nearby (checked, genuinely none)"
            if road_union is None
            else "fetched (road right-of-way exclusion union present)"
        )
    except Exception as e:  # noqa: BLE001
        result["status"]["road"] = f"UNCHECKED -- fetch failed: {e}"

    return result


def synthetic_run_inputs() -> tuple[list, dict, dict]:
    """
    A synthetic DEM and synthetic gate inputs for --dry-run: exercises
    every code path in this file with no network.

    The terrain is deliberately ROLLING rather than a plane: a gentle
    regional grade with a sinusoidal ripple tuned so roughly a fifth of the
    grid fails a 20% slope gate in scattered bands. A flat synthetic DEM
    (what this used before slope became a layer) leaves the slope layer
    empty and every pairwise overlap zero, which means the dry run silently
    fails to exercise the two sections that exist to report them.

    The canopy mask is two blocks with pinholes in them, and the hydric and
    road gates are left at their "not checked" sentinels -- so a dry run
    also exercises the unchecked-gate and empty-layer-export paths.

    NOTHING here is a statement about any real property; the ripple is
    chosen to exercise code, not to resemble terrain.
    """
    rows = cols = 60
    resolution = 5.0
    yy, xx = np.mgrid[0:rows, 0:cols]
    array = (
        100.0
        + 0.02 * resolution * yy
        + 5.0 * np.sin(yy / 5.0) * np.cos(xx / 4.0)
    ).astype(np.float32)

    dem = {
        "array": array,
        "resolution_meters": (resolution, resolution),
        "origin_x": 600000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    canopy = np.zeros((rows, cols), dtype=bool)
    canopy[10:25, 10:25] = True
    canopy[10:25, 30:45] = True
    for r, c in [(14, 14), (18, 20), (21, 12), (13, 35), (19, 40), (22, 33)]:
        canopy[r, c] = False

    gate_inputs = {
        "soil": _SOIL_CHECK_UNCHECKED,
        "canopy": canopy,
        "road": _ROAD_CHECK_UNCHECKED,
        "status": {
            "canopy": "SYNTHETIC (--dry-run): two blocks with six single-cell pinholes",
            "soil": "UNCHECKED -- synthetic run supplies no soil union",
            "road": "UNCHECKED -- synthetic run supplies no road union",
        },
    }

    # A boundary polygon in the DEM's own CRS, inset from the grid edge so
    # the setback ring and the on-parcel test both have something to bite on.
    x0 = dem["origin_x"] + 5 * resolution
    x1 = dem["origin_x"] + (cols - 5) * resolution
    y1 = dem["origin_y"] - 5 * resolution
    y0 = dem["origin_y"] - (rows - 5) * resolution
    boundary_polygon_utm = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])

    return boundary_polygon_utm, dem, gate_inputs


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def _empty_layer_note(metrics: dict, available: bool) -> str:
    """
    Why a layer came out empty, so an empty feature in the export is
    self-explaining. The three cases a reader would otherwise have to guess
    between: the gate was never checked, the gate was checked and found
    nothing, or the layer has real geometry and nothing needs saying.
    """
    if metrics["cell_count"] > 0:
        return ""
    if not available:
        return (
            "EMPTY because this gate's input was UNAVAILABLE for the run -- not checked, "
            "NOT verified clean. Do not read this as an absence of the hazard."
        )
    return "EMPTY because the gate was checked and matched no cell on this parcel."


def _layer_properties(
    gate: str,
    boundary: str,
    radius_m: float,
    radius_cells: int,
    layer_kind: str,
    metrics: dict,
    note: str = "",
) -> dict:
    """
    The property bag every exported feature carries.

    Deliberately self-describing: a reader who opens the file on geojson.io
    and clicks a shape must be able to tell WHICH layer and WHICH closing
    radius it is, and how big it is, without opening this source file. So
    the gate name is spelled out rather than abbreviated, the radius is
    given in both metres and cells (they differ -- see the metres-to-cells
    note in the module docstring), and the headline figures travel with the
    geometry.
    """
    properties = {
        "gate": gate,
        "layer_kind": layer_kind,
        "boundary": boundary,
        "closing_radius_m": radius_m,
        "closing_radius_cells": radius_cells,
        "acres": round(metrics["acres_from_cells"], 4),
        "cell_count": metrics["cell_count"],
        "polygon_count": metrics["polygon_count"],
        "hole_count": metrics["hole_count"],
        "exterior_vertex_count": metrics["exterior_vertex_count"],
        "empty": metrics["cell_count"] == 0,
    }
    if note:
        properties["note"] = note
    return properties


def _geojson_feature(dem: dict, geom, properties: dict) -> dict:
    """
    One GeoJSON Feature.

    AN EMPTY LAYER STILL GETS A FEATURE, with `"geometry": null` (valid
    GeoJSON) and `"empty": true` in its properties. Silently omitting it
    was the old behavior and it is the wrong one: a reader opening the file
    and finding no road layer cannot tell whether roads were genuinely zero
    cells, whether the fetch failed, or whether the export dropped them.
    A null-geometry feature renders as nothing on a map but is listed in
    the properties table, which is precisely the distinction wanted -- and
    its `note` property says which of those three it was.
    """
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": (
            None
            if geom is None or geom.is_empty
            else transform_geom(dem["crs"], "EPSG:4326", mapping(geom))
        ),
    }


def report_for_boundary(
    label: str,
    boundary_polygon_utm: Polygon,
    dem: dict,
    gate_inputs: dict,
    closing_radii_meters: list[float],
    max_slope_pct: float,
    geojson_prefix: str | None,
) -> None:
    print("=" * 78)
    print(f"BOUNDARY: {label}")
    print("=" * 78)
    px, py = dem["resolution_meters"]
    print(
        f"DEM: {dem['array'].shape[0]}x{dem['array'].shape[1]} cells at "
        f"{px:.2f}x{py:.2f} m ({cell_area_acres(dem):.5f} ac/cell), crs {dem['crs']}"
    )
    print(f"Parcel polygon area (unshrunk): {_acres(boundary_polygon_utm.area):.3f} ac")
    for gate in ("canopy", "soil", "road"):
        print(f"  {gate:>6} input: {gate_inputs['status'].get(gate, 'unknown')}")
    print()

    step1 = compute_step1_eligible_cells(
        dem,
        boundary_polygon_utm,
        disqualifying_soil_union_utm=gate_inputs["soil"],
        max_slope_pct=max_slope_pct,
        tree_root_zone_mask_utm=gate_inputs["canopy"],
        road_exclusion_union_utm=gate_inputs["road"],
    )
    print(
        f"STEP 1 ran with: soil_data_available={step1['soil_data_available']}  "
        f"canopy_data_available={step1['canopy_data_available']}  "
        f"road_data_available={step1['road_data_available']}  "
        f"max_slope_pct={max_slope_pct}"
    )
    print()

    on_parcel = on_parcel_mask(dem, boundary_polygon_utm)
    setback_only = derive_setback_only_mask(step1, on_parcel, max_slope_pct)
    slope_fail = derive_slope_fail_mask(step1, on_parcel, max_slope_pct)
    slope_ok = derive_slope_ok(step1, max_slope_pct)

    gates = [
        ("canopy (tree root zone)", step1["tree_root_zone_hit"], step1["canopy_data_available"]),
        ("hydric soil", step1["hydric_hit"], step1["soil_data_available"]),
        ("existing farm roads", step1["road_hit"], step1["road_data_available"]),
        (SETBACK_GATE_LABEL, setback_only, True),
        (SLOPE_GATE_LABEL, slope_fail, True),
    ]

    _print_layer_relationships(dem, gates, on_parcel, step1, max_slope_pct)

    # Indexed by POSITION, not keyed by the radius value, so a repeated
    # radius on the command line stays two independent sweep entries
    # instead of silently piling both runs' masks into one bucket.
    radii_cells = []
    for radius_m in closing_radii_meters:
        rc = closing_radius_cells(dem, radius_m)
        radii_cells.append((radius_m, rc, effective_radius_meters(dem, rc)))

    print("CLOSING RADII FOR THIS DEM")
    for radius_m, rc, eff_m in radii_cells:
        note = "  (no-op: rounds to 0 cells)" if rc == 0 and radius_m > 0 else ""
        print(f"  requested {radius_m:>6.1f} m -> {rc} cell(s) -> effective {eff_m:.2f} m{note}")
    print()

    geojson_features = []
    # Per radius, the closed mask of EACH gate kept under its own label --
    # not pooled into one union -- because the eligible layer is now reported
    # in three forms, two of which need the slope layer separable from the
    # other four.
    closed_by_radius_index: list[dict[str, np.ndarray]] = [{} for _ in radii_cells]

    for gate_label, raw_mask, available in gates:
        print("-" * 78)
        print(f"GATE: {gate_label}")
        if not available:
            print(
                "  NOT CHECKED for this run -- the mask below is all-False because the input "
                "was unavailable, NOT because the parcel is clean. Every figure here is a "
                "placeholder, not a result."
            )
        print("-" * 78)

        baseline = footprint_metrics(dem, raw_mask)
        print_metrics("raw (radius 0.0 m)", baseline)
        print()

        if geojson_prefix:
            geojson_features.append(
                _geojson_feature(
                    dem,
                    baseline["footprint"],
                    _layer_properties(
                        gate=gate_label,
                        boundary=label,
                        radius_m=0.0,
                        radius_cells=0,
                        layer_kind="exclusion",
                        metrics=baseline,
                        note=_empty_layer_note(baseline, available),
                    ),
                )
            )

        for index, (radius_m, rc, eff_m) in enumerate(radii_cells):
            closed = disc_closing(raw_mask, rc)
            closed_by_radius_index[index][gate_label] = closed
            if rc == 0:
                # No closing actually happened -- the figures would repeat the
                # raw block verbatim, and the geojson would repeat the raw
                # feature. The "rounds to 0 cells" note above already said so.
                continue
            metrics = footprint_metrics(dem, closed)
            gain = gained_region_distribution(dem, raw_mask, closed)
            print_metrics(f"closed at {radius_m:.1f} m (disc, {rc} cell(s), effective {eff_m:.2f} m)", metrics)
            print_gain(gain, baseline, metrics)
            print()

            if geojson_prefix:
                geojson_features.append(
                    _geojson_feature(
                        dem,
                        metrics["footprint"],
                        _layer_properties(
                            gate=gate_label,
                            boundary=label,
                            radius_m=radius_m,
                            radius_cells=rc,
                            layer_kind="exclusion",
                            metrics=metrics,
                            note=_empty_layer_note(metrics, available),
                        ),
                    )
                )

    # ---- the eligible layer -------------------------------------------
    print("-" * 78)
    print("ELIGIBLE LAYER")
    print("-" * 78)
    eligible_baseline = footprint_metrics(dem, step1["eligible_mask"])
    print_metrics("eligible_mask, raw (STEP 1's own output)", eligible_baseline)
    print()

    on_parcel_acres = int(on_parcel.sum()) * cell_area_acres(dem)
    slope_fail_on_parcel_acres = int(slope_fail.sum()) * cell_area_acres(dem)

    print(
        f"    on-parcel cells (unshrunk boundary, cell centers): {int(on_parcel.sum())} "
        f"= {on_parcel_acres:.3f} ac"
    )
    print(
        f"    of which FAIL the slope gate: {slope_fail_on_parcel_acres:.3f} ac -- now reported "
        f"as its own layer above, and subtracted by FORM C below."
    )
    print()
    print(
        "    THREE FORMS, because each of the first two is missing something:\n"
        "      FORM A  boundary - (four closed exclusions: canopy, hydric, road, setback)\n"
        "              Does NOT subtract slope at all, so it hands the user steep ground.\n"
        "      FORM B  FORM A intersected with the RAW slope gate\n"
        "              Subtracts slope, but UNCLOSED -- so every scattered steep cell stays a\n"
        "              hole, which is why this form's hole count never moved with radius.\n"
        "      FORM C  boundary - (all FIVE closed exclusions, slope included)\n"
        "              Every reason ground is unselectable, each consolidated by its own\n"
        "              closing. THIS is what the frontend would actually clamp against, and its\n"
        "              polygon/hole/vertex counts are the transport and interaction figures\n"
        "              that matter."
    )
    print()

    for index, (radius_m, rc, eff_m) in enumerate(radii_cells):
        closed_by_gate = closed_by_radius_index[index]

        four_gate_union = np.zeros_like(on_parcel)
        for gate_label, closed in closed_by_gate.items():
            if gate_label != SLOPE_GATE_LABEL:
                four_gate_union |= closed
        all_five_union = four_gate_union | closed_by_gate[SLOPE_GATE_LABEL]

        form_a = on_parcel & (~four_gate_union)
        form_b = form_a & slope_ok
        form_c = on_parcel & (~all_five_union)

        for form_name, form_mask, form_note in (
            ("FORM A: boundary - (four closed exclusions, slope NOT subtracted)", form_a, ""),
            ("FORM B: FORM A intersected with the RAW (unclosed) slope gate", form_b, ""),
            (
                "FORM C: boundary - (all FIVE closed exclusions, slope included)",
                form_c,
                "  <-- what the frontend would clamp against",
            ),
        ):
            form_metrics = footprint_metrics(dem, form_mask)
            print_metrics(
                f"{form_name} at {radius_m:.1f} m ({rc} cell(s), effective {eff_m:.2f} m){form_note}",
                form_metrics,
            )
            print()

            if geojson_prefix:
                geojson_features.append(
                    _geojson_feature(
                        dem,
                        form_metrics["footprint"],
                        _layer_properties(
                            gate=f"eligible {form_name.split(':')[0]}",
                            boundary=label,
                            radius_m=radius_m,
                            radius_cells=rc,
                            layer_kind="eligible",
                            metrics=form_metrics,
                            note=form_name.split(": ", 1)[1],
                        ),
                    )
                )

    # ---- the unevaluated ring -----------------------------------------
    print("-" * 78)
    print("THE UNEVALUATED RING")
    print("-" * 78)
    ring_acres = int(setback_only.sum()) * cell_area_acres(dem)
    print(
        f"    setback-only ground (derived: on_parcel & slope_ok & ~slope_only_mask): "
        f"{int(setback_only.sum())} cells = {ring_acres:.3f} ac"
    )
    print(
        "    THIS GROUND IS UNEVALUATED, NOT CLEAN. STEP 1 evaluates the canopy, hydric and\n"
        "    road gates ONLY inside slope_only_mask, so no cell in this ring was ever tested\n"
        "    by any of them. A frontend showing per-gate layers would present it as 'setback\n"
        "    only' when the truth is 'setback, and the rest was not checked'."
    )
    print(
        "    LOWER BOUND: this recovers only the part of the ring that CLEARS the slope gate.\n"
        "    Ring ground that also fails slope is not separable from off-ring slope failures\n"
        "    using STEP 1's returned arrays alone (slope_only_mask collapses both tests)."
    )
    print()

    if geojson_prefix and geojson_features:
        slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")
        path = f"{geojson_prefix}_{slug}.geojson"
        with open(path, "w") as f:
            json.dump({"type": "FeatureCollection", "features": geojson_features}, f)
        empty = [f for f in geojson_features if f["properties"]["empty"]]
        print("-" * 78)
        print("GEOJSON EXPORT")
        print("-" * 78)
        print(f"    wrote {len(geojson_features)} features to {path} (WGS84, drop on geojson.io)")
        print(
            "    every feature carries gate, layer_kind, closing radius in BOTH metres and cells, "
            "acres,\n    cell/polygon/hole/vertex counts and an `empty` flag -- readable on "
            "geojson.io without\n    opening this source."
        )
        if empty:
            names = sorted({f"{f['properties']['gate']}" for f in empty})
            print(
                f"    {len(empty)} of them are EMPTY layers, exported with `\"geometry\": null` "
                f"and `empty: true`\n    rather than dropped, so a reader can tell an absent layer "
                f"from an unrendered one: {', '.join(names)}"
            )
        print()


def main(
    boundaries: list[str],
    closing_radii_meters: list[float],
    max_slope_pct: float,
    geojson_prefix: str | None,
    dry_run: bool,
) -> None:
    if dry_run:
        print("*** --dry-run: SYNTHETIC DEM AND SYNTHETIC GATE INPUTS, NO NETWORK. ***")
        print("*** Nothing below says anything about any real property.            ***\n")
        boundary_polygon_utm, dem, gate_inputs = synthetic_run_inputs()
        report_for_boundary(
            "synthetic (dry run)",
            boundary_polygon_utm,
            dem,
            gate_inputs,
            closing_radii_meters,
            max_slope_pct,
            geojson_prefix,
        )
        return

    for name in boundaries:
        boundary_coordinates = REFERENCE_BOUNDARIES[name]
        dem = get_dem_for_boundary(boundary_coordinates)
        xs, ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(xs, ys))
        gate_inputs = fetch_gate_inputs(boundary_coordinates, dem)
        report_for_boundary(
            f"{name.upper()} reference boundary",
            boundary_polygon_utm,
            dem,
            gate_inputs,
            closing_radii_meters,
            max_slope_pct,
            geojson_prefix,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the per-gate exclusion footprints STEP 1 of the production pipeline "
            "produces (slope, canopy, hydric soil, existing farm roads, boundary setback) and what a "
            "small disc morphological closing does to each of them. Read-only: changes no "
            "pipeline module and adds no constants to one."
        ),
        epilog=(
            "The closing is applied PER GATE and is extensive -- exclusions grow. The number "
            "that decides whether a radius is safe is not the total acres gained but the SIZE "
            "DISTRIBUTION of the gained regions: many tiny gains = pinholes absorbed; one large "
            "gain = two separate stands merged and the farmable ground between them swallowed."
        ),
    )
    parser.add_argument(
        "--closing-radii",
        type=float,
        nargs="+",
        default=DEFAULT_CLOSING_RADII_METERS,
        metavar="METERS",
        help=(
            "Closing radii to sweep, in metres; 0 means the raw footprint "
            f"(default: {' '.join(f'{r:g}' for r in DEFAULT_CLOSING_RADII_METERS)}). At the "
            "pipeline's 5 m DEM resolution these quantize hard to whole cells -- the report "
            "prints the effective radius actually applied for each one."
        ),
    )
    parser.add_argument(
        "--boundary",
        choices=["old", "new", "both"],
        default="both",
        help="Which reference boundary to run (default: both, as every diagnostic in this thread does).",
    )
    parser.add_argument(
        "--max-slope-pct",
        type=float,
        default=MAX_PRODUCTION_SLOPE_PCT,
        help=(
            "Override the slope gate STEP 1 applies, for this run only (default: the current "
            f"production_area module constant, {MAX_PRODUCTION_SLOPE_PCT}). Both DERIVED layers "
            "-- slope and setback -- are rebuilt from whatever is passed here rather than from "
            "the module constant, so all three stay consistent under an override."
        ),
    )
    parser.add_argument(
        "--geojson",
        metavar="PREFIX",
        default=None,
        help=(
            "Write each of the five gates' raw AND closed footprints, plus all three forms of the "
            "derived eligible layer, to "
            "PREFIX_<boundary>.geojson in WGS84 -- for dropping on geojson.io and actually "
            "looking at. The numbers say whether the closing consolidates; only looking says "
            "whether the result reads as a sensible picture of the parcel."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the whole report against a SYNTHETIC DEM and synthetic gate inputs with no "
            "network at all. For checking the report renders and the arithmetic runs -- not "
            "for any conclusion about a real property."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    selected = ["old", "new"] if args.boundary == "both" else [args.boundary]
    try:
        main(
            boundaries=selected,
            closing_radii_meters=args.closing_radii,
            max_slope_pct=args.max_slope_pct,
            geojson_prefix=args.geojson,
            dry_run=args.dry_run,
        )
    except Exception as e:  # noqa: BLE001
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National Map ImageServer "
            "(the DEM and the lidar HAG canopy), USDA SSURGO (hydric soil) and the road layer "
            "(existing farm roads) -- not a fully sandboxed environment. Use --dry-run to "
            "exercise the report itself against a synthetic DEM with no network."
        )
