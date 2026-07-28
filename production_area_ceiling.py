"""
production_area_ceiling.py

STEP 2 of the consolidated production-zone pipeline (this file /
production_area.py / production_suitability.py together), plus this
pipeline's own full orchestration entry point.

Why this exists: production_area.py's slope+hydric eligibility gate, on a
real reference property, can identify a very large fraction of the parcel
as eligible production ground. That's a real number, but a Scale of
Permanence design also needs room for water systems, tree/windbreak
zones, roads, structures, and fencing -- so this module trims the
ELIGIBLE cell pool (STEP 1's own output, already hydric-excluded) down
toward a documented ceiling on how much of the PARCEL (not the eligible
acreage) may be claimed as production area at all, before STEP 3
(production_area.cluster_and_gate()) ever runs.

    STEP 2 -- trim_to_ceiling(): sort STEP 1's eligible cells worst-to-best
        by their own per-cell score (production_area.compute_step1_
        eligible_cells()'s per_cell_score -- slope+aspect, the SAME
        composite that decided eligibility at all) and remove cells one at
        a time (worst first) until the remaining total is at or just above
        PRODUCTION_CEILING_PCT_OF_PARCEL percent of the REAL, FULL parcel
        boundary's area. If the pool's pre-trim acreage is already at or
        under that ceiling, nothing is removed and
        production_ceiling_target_met is reported False honestly, exactly
        as before this consolidation -- this conditional behavior is
        UNCHANGED, just now operating on a mask that already has hydric
        cells excluded (which it didn't, pre-consolidation).

STEP 3 (clustering the post-trim survivor mask) and STEP 4 (advisory
scoring) are NOT reimplemented here -- this module calls directly into
production_area.cluster_and_gate() and production_suitability.
score_production_areas(), the same functions production_area.py's own
(un-trimmed) identify_production_areas() uses, just fed the post-trim
survivor mask instead of STEP 1's raw eligible mask. One implementation of
each step, reused by both entry points.

STEP 1 itself (compute_step1_eligible_cells()) is likewise not
reimplemented -- optimize_production_areas() calls the exact same
function identify_production_areas() does, with the exact same gates:
slope, hydric soil, the woody-vegetation tree-root-zone mask, and the
fixed boundary setback. The soil and canopy inputs are passed IN (already
fetched) rather than fetched here, so this stays "pure logic, no network
I/O" and can be exercised directly against synthetic data; see
optimize_production_areas()'s own docstring.

identify_optimized_production_areas() is the fetch-and-score entry point:
fetches the DEM (unless one is passed in), real disqualifying-soil
geometry, and the required tree-root-zone mask, ONCE each for the whole
parcel boundary (STEP 1 needs all of it before clustering ever happens --
no per-patch fetch, unlike the pre-consolidation architecture, since
patches don't exist yet at this point), then chains STEP 1 -> STEP 2 ->
STEP 3 -> STEP 4. Soil degrades gracefully on fetch failure; canopy does
NOT -- see this function's own docstring for why, and production_area.py's
identify_production_areas() docstring for the shared reasoning.

This is a self-contained, standalone pass, same "validate on its own
first" framing as the rest of this pipeline: NOT wired into
generate_full_report.py/report_generator.py's prompt in this pass, but
render_layout_map.py and tree_zone_candidates.py both already consume its
output shape directly.
"""

from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform

from dem_data import get_dem_for_boundary
from production_area import (
    MAX_PRODUCTION_SLOPE_PCT,
    MIN_PRODUCTION_AREA_ACRES,
    _CANOPY_CHECK_UNCHECKED,
    _SOIL_CHECK_UNCHECKED,
    _fetch_disqualifying_soil_union,
    cluster_and_gate,
    compute_step1_eligible_cells,
    get_required_tree_root_zone_mask_utm,
)
from production_suitability import (
    REFERENCE_MAX_AREA_ACRES,
    production_suitability_to_geojson,
    score_production_areas,
)
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres
from shapely.geometry import Polygon
from soil_data import coordinates_to_wkt_polygon

# Ceiling on how much of the FULL PARCEL boundary's own area may be
# claimed as production area, after the global worst-first trim below --
# NOT a percent of the eligible acreage STEP 1 identified (which, on a
# real reference property, was itself a very large fraction of the
# parcel). CONFIGURABLE -- tune against a real property; 80% is a
# documented starting ceiling, not a derived value.
PRODUCTION_CEILING_PCT_OF_PARCEL = 80.0


def trim_to_ceiling(
    step1: dict,
    dem: dict,
    boundary_polygon_utm: Polygon,
    ceiling_pct: float = PRODUCTION_CEILING_PCT_OF_PARCEL,
) -> dict:
    """
    STEP 2: sorts STEP 1's eligible cells worst-to-best by their own
    per-cell score (step1['per_cell_score']) and removes cells one at a
    time (worst first), stopping the INSTANT removing the next-worst cell
    would drop the remaining total below ceiling_pct percent of the real,
    FULL parcel boundary's area (not the pre-trim eligible acreage). This
    lands the result at the closest point AT OR JUST ABOVE the ceiling
    that whole-cell removal can reach, never below it.

    If the pool's pre-trim total is already at or under the ceiling, the
    loop's very first check fails immediately: zero cells are removed, and
    the achieved percentage is reported exactly as it naturally is (which
    may be below ceiling_pct) rather than forced or padded to look like
    the ceiling was hit. `production_ceiling_target_met` is False in that
    case -- "met" here specifically means "the ceiling was actually
    approached from above via real trimming," not merely "the parcel isn't
    over-claimed," so an already-under-ceiling starting point is flagged
    honestly rather than silently reported as a successful 80% trim.
    """
    parcel_acres = boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE
    target_acres = parcel_acres * ceiling_pct / 100.0
    area_per_cell = cell_area_acres(dem)

    eligible_cells = [(int(r), int(c)) for r, c in np.argwhere(step1["eligible_mask"])]
    per_cell_score = step1["per_cell_score"]
    scored = [(r, c, float(per_cell_score[r, c])) for r, c in eligible_cells]
    ordered = sorted(scored, key=lambda cell: cell[2])  # worst (lowest score) first
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
    # "the pool started at/above the ceiling in the first place".
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


def optimize_production_areas(
    dem: dict,
    boundary_polygon_utm: Polygon,
    disqualifying_soil_union_utm=_SOIL_CHECK_UNCHECKED,
    ceiling_pct: float = PRODUCTION_CEILING_PCT_OF_PARCEL,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
    tree_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
) -> dict:
    """
    Pure logic core (no network I/O) chaining STEP 1 (production_area.
    compute_step1_eligible_cells()) -> STEP 2 (trim_to_ceiling()) -> STEP 3
    (production_area.cluster_and_gate(), on the post-trim survivor mask).

    tree_root_zone_mask_utm follows compute_step1_eligible_cells()'s own
    convention exactly (a pre-fetched boolean mask, or the default
    _CANOPY_CHECK_UNCHECKED sentinel meaning "skip this gate") -- this
    function does no fetching of its own, same as disqualifying_soil_
    union_utm above; it stays true to its own "pure logic, no network I/O"
    contract precisely so it (and trim_to_ceiling()) can still be called
    directly against a synthetic DEM/mask with no canopy data at all, e.g.
    in tests. The real, MANDATORY canopy fetch (fail hard if unavailable)
    lives in identify_optimized_production_areas() below, the actual
    network entry point -- same split as the soil fetch already has.

    Returns:
        {
            'patches': list[dict],  # STEP 3 output, ready for
                                     # production_suitability.score_
                                     # production_areas() via its step1 param
            'step1': dict,          # STEP 1's own return dict -- reused
                                     # directly by STEP 4, never recomputed
            'cells_removed': int,
            'parcel_acres': float,
            'target_acres': float,
            'pre_trim_acres': float,
            'achieved_acres': float,
            'achieved_pct_of_parcel': float,
            'production_ceiling_target_met': bool,
        }
    """
    step1 = compute_step1_eligible_cells(
        dem, boundary_polygon_utm, disqualifying_soil_union_utm, max_slope_pct, tree_root_zone_mask_utm
    )
    trim_result = trim_to_ceiling(step1, dem, boundary_polygon_utm, ceiling_pct)

    rows, cols = dem["array"].shape
    survivor_mask = np.zeros((rows, cols), dtype=bool)
    for r, c in trim_result["survivor_cells"]:
        survivor_mask[r, c] = True

    patches = cluster_and_gate(survivor_mask, dem, boundary_polygon_utm, step1, min_area_acres)

    result = {k: v for k, v in trim_result.items() if k != "survivor_cells"}
    result["patches"] = patches
    result["step1"] = step1
    return result


def identify_optimized_production_areas(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_soil: bool = True,
    ceiling_pct: float = PRODUCTION_CEILING_PCT_OF_PARCEL,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
    reference_max_area_acres: float = REFERENCE_MAX_AREA_ACRES,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    real disqualifying-soil geometry ONCE for the whole parcel boundary
    (STEP 1 needs it before any patch exists -- unlike the pre-
    consolidation architecture's per-patch soil fetch), and the required
    woody-vegetation tree-root-zone mask, then runs STEP 1 -> STEP 2 (the
    global worst-first trim toward ceiling_pct) -> STEP 3 (cluster_and_
    gate() on the survivors) -> STEP 4 (production_suitability.score_
    production_areas(), advisory ranking only).

    The soil fetch degrades gracefully -- a USDA SDA outage doesn't block
    scoring, it just means hydric exclusion couldn't be verified
    (soil_data_available=False on every result) -- same reasoning as
    every other optional network layer in this pipeline. The canopy fetch
    does NOT: it is mandatory (no check_canopy flag), via production_
    area.get_required_tree_root_zone_mask_utm() -- the SAME shared
    fetch-or-raise helper production_area.identify_production_areas()
    itself calls, so this entry point (the one render_layout_map.py and
    tree_zone_candidates.py actually use) produces the identical
    eligible-cell geometry that function does, rather than silently
    omitting the woody-vegetation gate on this path the way it used to (a
    real bug: this function's own STEP 1 call previously passed
    compute_step1_eligible_cells() only 4 positional arguments, leaving
    tree_root_zone_mask_utm on its "skip this gate" sentinel default). A
    fetch failure -- retries exhausted, or canopy_height_data.
    CanopyCoverageIncompleteError for coverage too sparse to trust --
    propagates up UNCAUGHT, same hard-fail behavior as production_area.
    identify_production_areas(); callers (render_layout_map.py, tree_
    zone_candidates.py) are expected to let this raise, not catch and
    degrade it.

    Returns the same "production_area_candidate" GeoJSON FeatureCollection
    / scored_patches shape this pipeline has always returned, plus
    top-level summary fields describing the global trim:
        {
            'zones_geojson': dict,
            'scored_patches': list[dict],
            'total_selected_acreage': float,
            'percent_of_parcel': float,        # of the FULL parcel
            'production_ceiling_target_met': bool,
            'total_cells_removed': int,        # STEP 2's global trim only
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

    disqualifying_soil_union_utm = _SOIL_CHECK_UNCHECKED
    if check_soil:
        try:
            wkt_polygon = coordinates_to_wkt_polygon(list(boundary_coordinates))
            disqualifying_soil_union_utm = _fetch_disqualifying_soil_union(wkt_polygon, dem)
        except Exception:
            disqualifying_soil_union_utm = _SOIL_CHECK_UNCHECKED

    tree_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(boundary_polygon_utm, dem)

    optimized = optimize_production_areas(
        dem,
        boundary_polygon_utm,
        disqualifying_soil_union_utm,
        ceiling_pct=ceiling_pct,
        max_slope_pct=max_slope_pct,
        min_area_acres=min_area_acres,
        tree_root_zone_mask_utm=tree_root_zone_mask_utm,
    )

    scored = score_production_areas(
        optimized["patches"], dem, optimized["step1"], reference_max_area_acres=reference_max_area_acres
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
