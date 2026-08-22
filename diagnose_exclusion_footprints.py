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
the frontend would show the user the parcel's EXCLUSION layers (canopy,
hydric soil, existing farm roads, boundary setback) and let them pick
production ground out of what is left. That needs each exclusion to be a
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
canopy fetch still produces real hydric/road/setback numbers.

--- CLOSING RADIUS, IN CELLS ---

binary_dilate()/binary_erode() take a radius in CELLS. This script
converts metres to cells as `round(radius_m / cell_size)`, cell_size =
(px + py) / 2, and prints both the requested metres and the effective
radius actually applied. It deliberately does NOT use raster_grid.
waist_erosion_radius_cells(): that converts a minimum WAIST WIDTH into a
radius and therefore HALVES it (a waist of w is severed by eroding w/2).
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
    binary_dilate,
    binary_erode,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
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

# The largest a gained region can be, IN CELLS, and still plausibly be an
# absorbed pinhole rather than a bridge between two separate regions --
# 4 cells is a 2x2 gap. Used only by the one-line reading aid below, in
# cells rather than acres so it means the same thing at any DEM
# resolution. Reporting only, like the two above.
PINHOLE_MAX_CELLS = 4


# ---------------------------------------------------------------------------
# geometry / measurement helpers
# ---------------------------------------------------------------------------


def closing_radius_cells(dem: dict, radius_meters: float) -> int:
    """
    Metres -> cell radius for the disc closing. cell_size is the mean of
    the DEM's two pixel dimensions (they are usually equal but computed
    independently upstream, see dem_data.get_dem_for_boundary()).

    Deliberately NOT raster_grid.waist_erosion_radius_cells(): that one
    converts a minimum waist WIDTH and halves it. A closing radius is
    already a radius, so this is a plain round() with no halving. A
    positive radius that rounds to 0 cells is reported by
    effective_radius_meters() below rather than silently becoming a
    no-op -- round() is used, not ceil(), so 2 m at a 5 m resolution
    honestly reports "0 cells, no-op" instead of being inflated to a
    full 5 m cell.
    """
    px, py = dem["resolution_meters"]
    cell_size = (px + py) / 2.0
    return max(0, int(round(radius_meters / cell_size)))


def effective_radius_meters(dem: dict, radius_cells: int) -> float:
    """The ground radius the integer cell radius really corresponds to."""
    px, py = dem["resolution_meters"]
    return radius_cells * (px + py) / 2.0


def disc_closing(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """
    Morphological closing (dilate then erode) with the disc structuring
    element, at radius_cells.

    The mask is PADDED by radius_cells + 1 cells of background before the
    dilation and cropped back afterwards. raster_grid._shift() treats
    everything beyond the array bounds as background, so without the pad
    the erosion half would chew into any region touching the grid edge and
    the operation would not be extensive. With the pad, closed >= mask
    holds for every input (asserted in the fixtures).

    radius_cells <= 0 returns a copy unchanged -- the raw footprint.
    """
    if radius_cells <= 0:
        return mask.copy()

    pad = radius_cells + 1
    padded = np.pad(mask, pad, mode="constant", constant_values=False)
    closed = binary_erode(
        binary_dilate(padded, radius_cells, element="disc"), radius_cells, element="disc"
    )
    return closed[pad:-pad, pad:-pad]


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
    slope_pct = step1["slope_pct"]
    slope_ok = (~np.isnan(slope_pct)) & (slope_pct <= max_slope_pct)
    return on_parcel & slope_ok & (~step1["slope_only_mask"])


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

    The terrain is a gentle plane (so most of it clears the slope gate),
    the canopy mask is two blocks with pinholes in them, and the hydric
    and road gates are left at their "not checked" sentinels -- so a dry
    run also exercises the unchecked-gate reporting path. NOTHING here is
    a statement about any real property.
    """
    rows = cols = 60
    resolution = 5.0
    yy, xx = np.mgrid[0:rows, 0:cols]
    array = (100.0 + 0.02 * resolution * yy + 0.01 * resolution * xx).astype(np.float32)

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


def _geojson_feature(dem: dict, geom, properties: dict) -> dict | None:
    if geom is None or geom.is_empty:
        return None
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": transform_geom(dem["crs"], "EPSG:4326", mapping(geom)),
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

    gates = [
        ("canopy (tree root zone)", step1["tree_root_zone_hit"], step1["canopy_data_available"]),
        ("hydric soil", step1["hydric_hit"], step1["soil_data_available"]),
        ("existing farm roads", step1["road_hit"], step1["road_data_available"]),
        ("boundary setback (derived)", setback_only, True),
    ]

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
    closed_by_radius_index: list[list[np.ndarray]] = [[] for _ in radii_cells]

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
            raw_feature = _geojson_feature(
                dem, baseline["footprint"], {"gate": gate_label, "boundary": label, "closing_radius_m": 0.0}
            )
            if raw_feature:
                geojson_features.append(raw_feature)

        for index, (radius_m, rc, eff_m) in enumerate(radii_cells):
            closed = disc_closing(raw_mask, rc)
            closed_by_radius_index[index].append(closed)
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
                feature = _geojson_feature(
                    dem,
                    metrics["footprint"],
                    {"gate": gate_label, "boundary": label, "closing_radius_m": radius_m},
                )
                if feature:
                    geojson_features.append(feature)

    # ---- the eligible layer -------------------------------------------
    print("-" * 78)
    print("ELIGIBLE LAYER")
    print("-" * 78)
    eligible_baseline = footprint_metrics(dem, step1["eligible_mask"])
    print_metrics("eligible_mask, raw (STEP 1's own output)", eligible_baseline)
    print()

    on_parcel_acres = int(on_parcel.sum()) * cell_area_acres(dem)
    slope_pct = step1["slope_pct"]
    slope_ok = (~np.isnan(slope_pct)) & (slope_pct <= max_slope_pct)
    slope_fail_on_parcel_acres = int((on_parcel & ~slope_ok).sum()) * cell_area_acres(dem)

    print(
        f"    on-parcel cells (unshrunk boundary, cell centers): {int(on_parcel.sum())} "
        f"= {on_parcel_acres:.3f} ac"
    )
    print(
        f"    of which FAIL the slope gate: {slope_fail_on_parcel_acres:.3f} ac -- the derived "
        f"layer below is 'boundary - closed exclusions' as specified, so it does NOT subtract "
        f"this; read its acreage with that in mind."
    )
    print()

    for index, (radius_m, rc, eff_m) in enumerate(radii_cells):
        closed_union = np.zeros_like(on_parcel)
        for closed in closed_by_radius_index[index]:
            closed_union |= closed

        derived = on_parcel & (~closed_union)
        derived_metrics = footprint_metrics(dem, derived)
        print_metrics(
            f"derived: boundary - (closed exclusions) at {radius_m:.1f} m "
            f"({rc} cell(s), effective {eff_m:.2f} m)  <-- what the frontend would clamp against",
            derived_metrics,
        )
        print()

        slope_aware = derived & slope_ok
        slope_aware_metrics = footprint_metrics(dem, slope_aware)
        print_metrics(
            f"  ...and the same intersected with the slope gate (conservative variant) at {radius_m:.1f} m",
            slope_aware_metrics,
        )
        print()

        if geojson_prefix:
            feature = _geojson_feature(
                dem,
                derived_metrics["footprint"],
                {"gate": "eligible (boundary - closed exclusions)", "boundary": label, "closing_radius_m": radius_m},
            )
            if feature:
                geojson_features.append(feature)

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
        print(f"    wrote {len(geojson_features)} features to {path} (WGS84, drop on geojson.io)")
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
            "produces (canopy, hydric soil, existing farm roads, boundary setback) and what a "
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
            f"production_area module constant, {MAX_PRODUCTION_SLOPE_PCT}). The setback "
            "derivation uses whatever is passed here, so the two stay consistent."
        ),
    )
    parser.add_argument(
        "--geojson",
        metavar="PREFIX",
        default=None,
        help=(
            "Write each gate's closed footprint, plus the derived eligible layer, to "
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
