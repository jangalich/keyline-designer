"""
production_area_ceiling.py

Global, cross-patch trim of production_area.py's identified production-
area candidates down toward a documented ceiling on how much of the
PARCEL (not the originally-identified eligible acreage) may be claimed as
production area at all.

Why this exists: production_area.py's slope-threshold heuristic, on a
real reference property, identifies ~95% of the parcel as a single
connected eligible patch. That's a real number, but 95% of a property
being "production area" reads as implausible for a Scale of Permanence
design that also needs room for water systems, tree/windbreak zones,
roads, structures, and fencing. This module does NOT change
production_area.py's slope-eligibility detection (untouched, imported
as-is) or production_suitability.py's soil-carving/slope/size/aspect
scoring math (also untouched, reused as-is via score_production_areas()'s
existing cells_by_patch_id parameter) -- it only changes what GEOMETRY
feeds into that scoring, by removing the worst-quality individual cells
from the combined eligible pool first, regardless of which original
patch they came from.

    STEP 1 -- combined pool (build_combined_pool()): every on-parcel cell
        across EVERY currently-identified patch, pooled together as one
        flat list. A cell's origin patch doesn't matter from here on --
        there is no protected zone; only each cell's own quality counts.

    STEP 2 -- per-cell scoring (per_cell_score()): each cell's own real
        slope (production_area.py's compute_slope_percent) and aspect
        (terrain_metrics.py's Horn-method aspect) run through the SAME
        _slope_factor/aspect_score functions production_suitability.py's
        zone-level composite already uses -- reused directly, just
        applied to one cell (a length-1 list) instead of averaged across
        a whole patch. See the weighting section below for exactly how
        slope and aspect combine here.

    STEP 3 -- trim to ceiling (trim_to_ceiling()): sort the pool
        worst-to-best by per-cell score, remove cells one at a time
        (worst first) until the remaining total is at or just above
        PRODUCTION_CEILING_PCT_OF_PARCEL percent of the REAL, FULL parcel
        boundary's area -- not a percent of the pre-trim eligible
        acreage. If the pool's pre-trim acreage is already at or under
        that ceiling, there aren't enough (or any) poor-quality cells to
        trim away without going the wrong direction -- see that
        function's docstring for exactly how that's reported rather than
        silently claimed as a hit.

    STEP 4 -- rebuild geometry (rebuild_patches_from_survivors()):
        fresh 8-connected-component labeling of the SURVIVING cell mask.
        Worst-first removal can fragment what was one contiguous patch
        into several disconnected pieces -- every piece that clears
        MIN_PRODUCTION_AREA_ACRES becomes its own independent output
        candidate (same dict shape identify_production_areas() itself
        returns), not forced back into one shape, and small legitimate
        fragments are not discarded.

    STEP 5 -- existing carving + scoring (identify_optimized_production_areas()):
        each STEP 4 patch is fed into score_production_areas() UNCHANGED
        (its own existing hydric soil-carving and slope/size/aspect
        scoring math runs exactly as it does for production_suitability.py's
        own un-trimmed patches) via that function's cells_by_patch_id
        parameter, so scoring recovers each patch's cells directly from
        this module's own STEP 4 labeling rather than (incorrectly)
        recomputing membership against the ORIGINAL, pre-trim low-slope
        mask.

--- per-cell weighting (STEP 2) ---

production_suitability.py's zone-level composite score weights three
factors: SLOPE_FACTOR_WEIGHT (0.55), SIZE_FACTOR_WEIGHT (0.30, acreage +
Polsby-Popper shape compactness), ASPECT_FACTOR_WEIGHT (0.15). size_factor
is a ZONE-level shape property -- "is this patch's footprint compact or a
thin sliver" has no meaning for a single grid cell, so it's excluded from
per-cell scoring entirely rather than forced onto cells it can't
describe (there's nothing to differ on: the other two factors ARE real
per-cell physical quantities, so no substitute weighting was invented for
them either). The remaining two factors' EXISTING relative weight
(0.55 : 0.15, i.e. 11 : 3) is preserved here, just renormalized to sum to
1.0 on its own -- not a new ratio chosen for this pass:

    PER_CELL_SLOPE_WEIGHT  = 0.55 / (0.55 + 0.15)  ~= 0.7857
    PER_CELL_ASPECT_WEIGHT = 0.15 / (0.55 + 0.15)  ~= 0.2143

This is a self-contained, standalone pass, same "validate on its own
first" framing as production_suitability.py's own module docstring: NOT
wired into generate_full_report.py/report_generator.py in this pass, and
does not touch water/tree/road/solar/fencing logic, which still consume
whatever production-area output currently exists.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import MultiPoint, Point, Polygon, box, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from production_area import (
    MAX_PRODUCTION_SLOPE_PCT,
    MIN_PRODUCTION_AREA_ACRES,
    compute_slope_percent,
    identify_production_areas,
)
from production_suitability import (
    ASPECT_FACTOR_WEIGHT,
    SLOPE_FACTOR_WEIGHT,
    _fetch_disqualifying_soil_union,
    _slope_factor,
    production_suitability_to_geojson,
    score_production_areas,
)
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres, connected_components, pixel_center_xy
from soil_data import coordinates_to_wkt_polygon
from terrain_metrics import aspect_score, compute_slope_and_aspect

# Ceiling on how much of the FULL PARCEL boundary's own area may be
# claimed as production area, after the global worst-first trim below --
# NOT a percent of production_area.py's originally-identified eligible
# acreage (which, on a real reference property, was itself ~95% of the
# parcel -- see module docstring). CONFIGURABLE -- tune against a real
# property; 80% is a documented starting ceiling, not a derived value.
PRODUCTION_CEILING_PCT_OF_PARCEL = 80.0

# --- per-cell weighting -- see module docstring for the full reasoning ---
_PER_CELL_WEIGHT_SUM = SLOPE_FACTOR_WEIGHT + ASPECT_FACTOR_WEIGHT
PER_CELL_SLOPE_WEIGHT = SLOPE_FACTOR_WEIGHT / _PER_CELL_WEIGHT_SUM
PER_CELL_ASPECT_WEIGHT = ASPECT_FACTOR_WEIGHT / _PER_CELL_WEIGHT_SUM

assert math.isclose(PER_CELL_SLOPE_WEIGHT + PER_CELL_ASPECT_WEIGHT, 1.0, abs_tol=1e-9), (
    "per-cell slope/aspect weights must sum to 1.0"
)


def per_cell_score(slope_pct: float, aspect_deg: float, max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT) -> float:
    """
    0-1 per-cell quality score for one DEM cell's own real slope/aspect,
    reusing production_suitability.py's own _slope_factor (called on a
    length-1 list -- the SAME formula it already uses, just applied to a
    single cell instead of averaged across a whole patch) and aspect_score
    (already a per-value function, not an aggregate, so it needs no
    adaptation at all -- NaN/flat ground already scores a neutral 1.0
    there, same as it does at the zone level). See module docstring for
    the slope:aspect weighting used here and why size_factor has no
    per-cell counterpart.
    """
    slope_factor = _slope_factor([slope_pct], max_slope_pct)
    aspect_factor = aspect_score(aspect_deg)
    return PER_CELL_SLOPE_WEIGHT * slope_factor + PER_CELL_ASPECT_WEIGHT * aspect_factor


def _recover_patch_cells(patch: dict, labels: np.ndarray, boundary_prepared, dem: dict) -> list[tuple[int, int]]:
    """Recovers one patch's own on-parcel constituent DEM cells by
    filtering the recomputed low-slope-mask connected-component labeling
    down to patch['id'] and the real parcel boundary -- the exact same
    recompute pattern score_production_areas() already uses for this
    reason (see that function's own docstring): production_area.py's
    patches don't carry their own cell list, so it has to be re-derived
    deterministically from the same mask/labeling it was built from."""
    raw_cells = [(int(r), int(c)) for r, c in np.argwhere(labels == patch["id"])]
    cells = [(r, c) for r, c in raw_cells if boundary_prepared.contains(Point(pixel_center_xy(dem, r, c)))]
    return cells if cells else raw_cells


def build_combined_pool(
    patches: list[dict],
    dem: dict,
    boundary_polygon_utm: Polygon,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
) -> list[tuple[int, int]]:
    """
    STEP 1: every on-parcel cell belonging to ANY of `patches`
    (production_area.py's identify_production_areas() output), combined
    into one flat list -- not grouped or kept separate by which original
    patch a cell came from. Every patch's connected-component label is
    distinct by construction (raster_grid.connected_components()), so no
    cell can be recovered twice across different patches.
    """
    array = dem["array"]
    slope = compute_slope_percent(array, dem["resolution_meters"])
    candidate_mask = (~np.isnan(slope)) & (slope <= max_slope_pct)
    labels, _ = connected_components(candidate_mask)
    boundary_prepared = prep(boundary_polygon_utm)

    pool: list[tuple[int, int]] = []
    for patch in patches:
        pool.extend(_recover_patch_cells(patch, labels, boundary_prepared, dem))
    return pool


def score_pool_cells(
    pool: list[tuple[int, int]], dem: dict, max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT
) -> list[tuple[int, int, float]]:
    """STEP 2: (row, col, per_cell_score) for every pooled cell, from real
    per-cell slope (production_area.py's compute_slope_percent -- the
    same slope values that already decided pool membership at all) and
    real per-cell aspect (terrain_metrics.py's Horn-method aspect --
    the same computation production_suitability.py's zone-level
    aspect_factor already uses, just read per-cell instead of
    circular-mean-averaged across a whole patch)."""
    array = dem["array"]
    resolution = dem["resolution_meters"]
    slope = compute_slope_percent(array, resolution)
    _, aspect_deg = compute_slope_and_aspect(array, resolution)

    return [
        (r, c, per_cell_score(float(slope[r, c]), float(aspect_deg[r, c]), max_slope_pct))
        for r, c in pool
    ]


def trim_to_ceiling(
    pool_scored: list[tuple[int, int, float]],
    dem: dict,
    boundary_polygon_utm: Polygon,
    ceiling_pct: float = PRODUCTION_CEILING_PCT_OF_PARCEL,
) -> dict:
    """
    STEP 3: sorts the pool worst-to-best by per-cell score and removes
    cells one at a time (worst first), stopping the INSTANT removing the
    next-worst cell would drop the remaining total below ceiling_pct
    percent of the real, FULL parcel boundary's area (not the pre-trim
    eligible acreage -- see module docstring). This lands the result at
    the closest point AT OR JUST ABOVE the ceiling that whole-cell
    removal can reach, never below it.

    If the pool's pre-trim total is already at or under the ceiling (not
    enough -- or any -- poor-quality cells exist to trim toward it
    without going the wrong direction, which only happens when the
    originally-identified eligible acreage was already a modest fraction
    of the parcel), the loop's very first check fails immediately: zero
    cells are removed, and the achieved percentage is reported exactly as
    it naturally is (which may be below ceiling_pct) rather than forced
    or padded to look like the ceiling was hit.
    `production_ceiling_target_met` is False in that case -- "met" here
    specifically means "the ceiling was actually approached from above via
    real trimming," not merely "the parcel isn't over-claimed," so an
    already-under-ceiling starting point is flagged honestly rather than
    silently reported as a successful 80% trim.
    """
    parcel_acres = boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE
    target_acres = parcel_acres * ceiling_pct / 100.0
    area_per_cell = cell_area_acres(dem)

    ordered = sorted(pool_scored, key=lambda cell: cell[2])  # worst (lowest score) first
    survivors = {(r, c) for r, c, _ in ordered}

    pre_trim_acres = len(survivors) * area_per_cell
    remaining_count = len(survivors)
    removed_count = 0

    for r, c, _ in ordered:
        if (remaining_count - 1) * area_per_cell < target_acres - 1e-9:
            break  # removing this cell would undershoot the ceiling -- stop, keep it (and every better cell after it)
        survivors.discard((r, c))
        remaining_count -= 1
        removed_count += 1

    achieved_acres = remaining_count * area_per_cell
    achieved_pct = (achieved_acres / parcel_acres * 100.0) if parcel_acres > 0 else 0.0
    # The loop above only ever removes a cell when doing so keeps the
    # remainder >= target_acres, so target_met is exactly equivalent to
    # "the pool started at/above the ceiling in the first place" -- see
    # docstring.
    target_met = pre_trim_acres >= target_acres - 1e-9

    return {
        "survivor_cells": survivors,
        "cells_removed": removed_count,
        "parcel_acres": round(parcel_acres, 2),
        "target_acres": round(target_acres, 2),
        "pre_trim_acres": round(pre_trim_acres, 2),
        "achieved_acres": round(achieved_acres, 2),
        "achieved_pct_of_parcel": round(achieved_pct, 2),
        "production_ceiling_target_met": target_met,
    }


def _cell_union_footprint(cells: list[tuple[int, int]], dem: dict):
    """
    The REAL footprint of a set of DEM cells: the union of each cell's own
    ground square at the DEM's resolution -- NOT a convex hull of cell
    CENTER points. Same technique already established in
    production_suitability.py's _compactness_score() (see that function's
    own docstring), reused here for the same reason: a convex hull of
    centers fills in any concave gaps between the actual qualifying
    cells with ground that was never actually a surviving cell at all --
    for a large, roughly-solid patch (the common, important case) this
    means the hull reports MORE area than the true footprint, sometimes
    by a lot (a real, confirmed bug -- see rebuild_patches_from_survivors()'s
    docstring). It isn't a one-directional error in general (a sparse,
    scattered fragment's hull can under-report instead, since it misses
    each individual cell's own half-cell-width margin that the real
    union always includes) -- either way, the hull is an approximation
    and this function is the accurate one.
    """
    px, py = dem["resolution_meters"]
    squares = []
    for r, c in cells:
        x, y = pixel_center_xy(dem, r, c)
        squares.append(box(x - px / 2, y - py / 2, x + px / 2, y + py / 2))
    return unary_union(squares)


def rebuild_patches_from_survivors(
    survivor_cells: set,
    dem: dict,
    boundary_polygon_utm: Polygon,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
) -> list[dict]:
    """
    STEP 4: fresh 8-connected-component labeling of the surviving
    (post-trim) cell mask -- may naturally produce one or several
    disconnected pieces, since worst-first removal can fragment what was
    one contiguous patch. Every resulting piece that clears
    min_area_acres becomes its own independent output candidate, small
    legitimate fragments are kept rather than discarded, and pieces are
    never forced back into one shape.

    polygon_utm/area_acres/geometry_wgs84 are built from the REAL
    per-cell-square UNION footprint (_cell_union_footprint(), reusing
    production_suitability.py's _compactness_score() technique) -- NOT a
    convex hull of cell centers. A prior version of this function used
    MultiPoint(...).convex_hull, the same construction
    identify_production_areas() itself still uses -- deliberately
    unchanged there, see this module's README entry -- but that hull
    fills in concave gaps between actual surviving cells with ground that
    was never actually low-slope/surviving at all, which for a large,
    roughly-solid patch means it over-reports area (see
    _cell_union_footprint()'s own docstring for why this isn't strictly
    one-directional in general). Confirmed live on the real reference
    property: an unchanged (0-cells-removed) survivor set whose true
    cell-summed area was 9.98 acres reported as an 11.9-acre hull
    instead -- enough to flip percent_of_parcel from correctly under the
    ceiling to (incorrectly) reading as over it. Since polygon_utm is the
    actual geometry every downstream spatial consumer (water zones,
    solar, trees, road corridors) treats as "the real production zone,"
    not just a display number, this must be the real cell-accurate shape.

    A convex hull IS still exposed, separately, as display_polygon_utm --
    useful purely for rendering smoothness -- but nothing reads that
    field for area/spatial-relationship purposes; polygon_utm/area_acres
    are the accurate ones every existing and future consumer should use.
    It is ALWAYS a single Polygon (never Multi -- the buffer fallback
    below guarantees this even for a degenerate hull), which is also why
    identify_optimized_production_areas() builds its soil-fetch query
    polygon from THIS field rather than the real (possibly MultiPolygon --
    see below) polygon_utm.

    Returns dicts in the SAME shape identify_production_areas() itself
    returns (id/area_acres/representative_elevation_m/polygon_utm/
    geometry_wgs84), PLUS display_polygon_utm and each patch's own
    'cells' list -- consumed directly by score_production_areas()'s
    cells_by_patch_id parameter downstream, so STEP 5's scoring runs
    against these exact post-trim cells instead of (incorrectly)
    recomputing membership from the original, pre-trim low-slope mask.

    polygon_utm/geometry_wgs84 CAN legitimately come back as a
    MultiPolygon here, unlike identify_production_areas()'s own patches:
    connected_components() is 8-connected (diagonal neighbors count as
    one component), but two cells whose real ground SQUARES touch only
    at a shared corner do not merge into one solid Polygon under
    unary_union -- their true combined footprint really is disconnected.
    feature_schema.py's GeoJSON schema already accepts MultiPolygon, and
    score_production_areas()'s soil-carving already flattens
    Polygon/MultiPolygon/GeometryCollection alike via its own
    _polygon_pieces() helper, so this needs no special handling
    downstream -- see test_production_area_ceiling.py's dedicated
    MultiPolygon regression case.
    """
    rows, cols = dem["array"].shape
    survivor_mask = np.zeros((rows, cols), dtype=bool)
    for r, c in survivor_cells:
        survivor_mask[r, c] = True

    labels, num_components = connected_components(survivor_mask)
    area_per_cell = cell_area_acres(dem)

    patches = []
    for component_id in range(num_components):
        cells = [(int(r), int(c)) for r, c in np.argwhere(labels == component_id)]
        if not cells:
            continue

        elevations = [float(dem["array"][r, c]) for r, c in cells]

        footprint = _cell_union_footprint(cells, dem)
        polygon_utm = footprint.intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
        if area_acres < min_area_acres:
            continue

        # Display-only convex hull -- NOT used for area_acres or any
        # spatial relationship; see docstring above.
        utm_points = [pixel_center_xy(dem, r, c) for r, c in cells]
        display_polygon_utm = MultiPoint(utm_points).convex_hull
        if display_polygon_utm.geom_type != "Polygon":
            px, py = dem["resolution_meters"]
            display_polygon_utm = display_polygon_utm.buffer(max(px, py) / 2)
        display_polygon_utm = display_polygon_utm.intersection(boundary_polygon_utm)

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

        patches.append(
            {
                "id": int(component_id),
                "area_acres": round(float(area_acres), 2),
                "representative_elevation_m": float(np.median(elevations)),
                "polygon_utm": polygon_utm,
                "display_polygon_utm": display_polygon_utm,
                "geometry_wgs84": geometry_wgs84,
                "cells": cells,
            }
        )

    return patches


def optimize_production_areas(
    patches: list[dict],
    dem: dict,
    boundary_polygon_utm: Polygon,
    ceiling_pct: float = PRODUCTION_CEILING_PCT_OF_PARCEL,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
) -> dict:
    """
    Pure logic core (no network I/O) chaining STEPS 1-4: combined pool
    (build_combined_pool()) -> per-cell scoring (score_pool_cells()) ->
    worst-first trim to ceiling_pct (trim_to_ceiling()) -> connected-
    component rebuild of the survivors (rebuild_patches_from_survivors()).

    Returns:
        {
            'patches': list[dict],  # STEP 4 output, ready for
                                     # score_production_areas() via its
                                     # cells_by_patch_id parameter
            'cells_removed': int,
            'parcel_acres': float,
            'target_acres': float,
            'pre_trim_acres': float,
            'achieved_acres': float,
            'achieved_pct_of_parcel': float,
            'production_ceiling_target_met': bool,
        }
    """
    pool = build_combined_pool(patches, dem, boundary_polygon_utm, max_slope_pct)
    pool_scored = score_pool_cells(pool, dem, max_slope_pct)
    trim_result = trim_to_ceiling(pool_scored, dem, boundary_polygon_utm, ceiling_pct)
    new_patches = rebuild_patches_from_survivors(
        trim_result["survivor_cells"], dem, boundary_polygon_utm, min_area_acres
    )

    result = {k: v for k, v in trim_result.items() if k != "survivor_cells"}
    result["patches"] = new_patches
    return result


def identify_optimized_production_areas(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_soil: bool = True,
    ceiling_pct: float = PRODUCTION_CEILING_PCT_OF_PARCEL,
    **score_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    identifies production-area candidates (production_area.py, unchanged),
    runs the global worst-first trim toward ceiling_pct (STEPS 1-4 above),
    then fetches real disqualifying-soil geometry and (re)scores each
    surviving connected piece via production_suitability.py's EXISTING,
    UNCHANGED score_production_areas() (STEP 5) -- same
    fetch-degrades-independently-and-gracefully reasoning as
    identify_production_area_suitability().

    Returns the same "production_area_candidate" GeoJSON FeatureCollection
    / scored_patches shape identify_production_area_suitability() does,
    plus top-level summary fields describing the global trim:
        {
            'zones_geojson': dict,
            'scored_patches': list[dict],
            'total_selected_acreage': float,   # final, post-soil-carve total
            'percent_of_parcel': float,        # of the FULL parcel, not the
                                                 # pre-trim eligible acreage
            'production_ceiling_target_met': bool,
            'total_cells_removed': int,        # in the STEP 3 global trim only
                                                 # (soil carving is separate and
                                                 # unaffected by this count)
        }
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    original_patches = identify_production_areas(dem, boundary_polygon_utm)
    optimized = optimize_production_areas(original_patches, dem, boundary_polygon_utm, ceiling_pct=ceiling_pct)
    new_patches = optimized["patches"]

    disqualifying_soil_by_patch_id: dict = {}
    if check_soil:
        for patch in new_patches:
            # The real polygon_utm/geometry_wgs84 (the accurate, per-cell-square-
            # union footprint -- see rebuild_patches_from_survivors()) can
            # legitimately come back as a MultiPolygon: worst-first trimming
            # can leave cells that are only DIAGONALLY (8-connected) touching,
            # whose real ground squares only meet at a corner point rather than
            # merging into one solid shape. coordinates_to_wkt_polygon() expects
            # a single ring, so the soil QUERY polygon here uses
            # display_polygon_utm (the convex hull -- always a single Polygon,
            # same as production_area.py's own patches) instead: a
            # conservative, slightly-larger query region is fine for "what
            # SSURGO geometry intersects this footprint at all" -- the actual
            # carving math downstream still intersects/subtracts against the
            # real, precise polygon_utm, not this query shape.
            query_geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(patch["display_polygon_utm"]))
            wkt_polygon = coordinates_to_wkt_polygon(query_geometry_wgs84["coordinates"][0])
            try:
                disqualifying_soil_by_patch_id[patch["id"]] = _fetch_disqualifying_soil_union(wkt_polygon, dem)
            except Exception:
                pass  # left absent from the dict -- score_production_areas() treats that as "never checked"

    cells_by_patch_id = {p["id"]: p["cells"] for p in new_patches}

    scored = score_production_areas(
        new_patches,
        dem,
        boundary_polygon_utm,
        disqualifying_soil_by_patch_id,
        cells_by_patch_id=cells_by_patch_id,
        **score_kwargs,
    )

    total_selected_acreage = round(sum(p["area_acres"] for p in scored), 2)
    parcel_acres = optimized["parcel_acres"]
    percent_of_parcel = round(total_selected_acreage / parcel_acres * 100.0, 2) if parcel_acres > 0 else 0.0

    return {
        "zones_geojson": production_suitability_to_geojson(scored),
        "scored_patches": scored,
        "total_selected_acreage": total_selected_acreage,
        "percent_of_parcel": percent_of_parcel,
        "production_ceiling_target_met": optimized["production_ceiling_target_met"],
        "total_cells_removed": optimized["cells_removed"],
    }


def summarize_optimized_production_areas(result: dict) -> str:
    lines = [
        f"Global trim: {result['total_cells_removed']} cell(s) removed toward a "
        f"{PRODUCTION_CEILING_PCT_OF_PARCEL}% of parcel ceiling "
        f"(target {'met' if result['production_ceiling_target_met'] else 'NOT met'})",
        f"Final selected acreage: {result['total_selected_acreage']} acres "
        f"({result['percent_of_parcel']}% of parcel)",
    ]
    if not result["scored_patches"]:
        lines.append("No production-area candidates survived.")
        return "\n".join(lines)

    lines.append(f"Surviving candidates: {len(result['scored_patches'])}")
    for patch in sorted(result["scored_patches"], key=lambda p: p["rank"]):
        lines.append(
            f"  - Rank {patch['rank']}: patch {patch['id']}, score {patch['suitability_score']}/100, "
            f"{patch['area_acres']} acres"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Optimizing production-area candidates toward the parcel ceiling...\n")

    try:
        result = identify_optimized_production_areas(property_boundary)
        print(summarize_optimized_production_areas(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
