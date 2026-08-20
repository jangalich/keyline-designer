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
slope, hydric soil, the woody-vegetation tree-root-zone mask, existing-
road exclusion, and the fixed boundary setback. The soil, canopy, and
road inputs are all passed IN (already fetched) rather than fetched here,
so this stays "pure logic, no network I/O" and can be exercised directly
against synthetic data; see optimize_production_areas()'s own docstring.

identify_optimized_production_areas() is the fetch-and-score entry point:
fetches the DEM (unless one is passed in), real disqualifying-soil
geometry, real existing-road exclusion geometry, and the required tree-
root-zone mask, ONCE each for the whole parcel boundary (STEP 1 needs all
of it before clustering ever happens -- no per-patch fetch, unlike the
pre-consolidation architecture, since patches don't exist yet at this
point), then chains STEP 1 -> STEP 2 -> STEP 3 -> STEP 4. Soil and road
both degrade gracefully on fetch failure; canopy does NOT -- see this
function's own docstring for why, and production_area.py's identify_
production_areas() docstring for the shared reasoning.

This is a self-contained, standalone pass, same "validate on its own
first" framing as the rest of this pipeline: NOT wired into
generate_full_report.py/report_generator.py's prompt in this pass, but
render_layout_map.py and tree_zone_candidates.py both already consume its
output shape directly.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely import contains_xy

from dem_data import get_dem_for_boundary
from production_area import (
    MAX_PRODUCTION_SLOPE_PCT,
    METERS_PER_FOOT,
    MIN_PRODUCTION_AREA_ACRES,
    PRODUCTION_BOUNDARY_SETBACK_METERS,
    _CANOPY_CHECK_UNCHECKED,
    _ROAD_CHECK_UNCHECKED,
    _SOIL_CHECK_UNCHECKED,
    _fetch_disqualifying_soil_union,
    _fetch_road_exclusion_union_utm,
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
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
) -> dict:
    """
    Pure logic core (no network I/O) chaining STEP 1 (production_area.
    compute_step1_eligible_cells()) -> STEP 2 (trim_to_ceiling()) -> STEP 3
    (production_area.cluster_and_gate(), on the post-trim survivor mask).

    tree_root_zone_mask_utm and road_exclusion_union_utm both follow
    compute_step1_eligible_cells()'s own conventions exactly (a pre-
    fetched value, or the default sentinel meaning "skip this gate") --
    this function does no fetching of its own, same as disqualifying_
    soil_union_utm above; it stays true to its own "pure logic, no
    network I/O" contract precisely so it (and trim_to_ceiling()) can
    still be called directly against a synthetic DEM/mask with no canopy
    or road data at all, e.g. in tests. The real fetches -- canopy
    MANDATORY (fail hard if unavailable), road exclusion optional
    (degrades gracefully, same as soil) -- live in identify_optimized_
    production_areas() below, the actual network entry point, same split
    the soil fetch already has.

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
        dem,
        boundary_polygon_utm,
        disqualifying_soil_union_utm=disqualifying_soil_union_utm,
        max_slope_pct=max_slope_pct,
        tree_root_zone_mask_utm=tree_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
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


# =====================================================================
# NARRATIVE DATA -- report-facing, FINAL values only
# =====================================================================
# Everything below exists to answer ONE report question: "what makes the
# selected area(s) suitable for production?" -- the second of the two
# questions the Landform section puts to the narrative model. The first
# (the property's general shape and topography) is parcel-wide terrain
# description, NOT a property of production zones, and is deliberately
# not sourced from here.
#
# Two hard rules govern every value in this block:
#
#   1. FINAL. The consumer must never convert, calculate, or relate two
#      values to get a third. Imperial at this boundary (acres, feet --
#      never metres, square metres, or cell counts); slope in percent
#      (already how the gate expresses it and how a farmer reads it);
#      aspect as a compass word, not degrees; everything rounded to 1
#      decimal place, because the precision emitted is the precision
#      narrated.
#   2. DERIVED, NEVER RECOMPUTED. Every figure here is read off values
#      STEP 1 / STEP 3 / STEP 4 already produced. No gate, clustering
#      pass, or scoring pass is re-run to report on itself -- that is the
#      redundant-computation problem this consolidated pipeline exists to
#      eliminate. The single exception is the on-parcel cell mask
#      (_on_parcel_cell_mask() below), which no pipeline step ever
#      computes and which two requested figures genuinely need; it is one
#      vectorised containment test, not a re-run of anything.
#
# The output is plain JSON: numbers, booleans, strings, dicts, lists.
# json.dumps() must work on it with no custom encoder -- no numpy
# scalars, no arrays, no shapely geometry, no masks.
#
# UNAVAILABLE IS None, NEVER 0.0. A narrative reading hydric_pct: 0.0
# will write "no hydric soils here", which is a confident falsehood when
# the truth is "the soil check never ran". Every soil-, canopy-, and
# road-derived field is explicitly None when its own data_available flag
# is False.

# 8-point compass words for dominant_aspect -- whole words, not the
# abbreviations terrain_metrics.aspect_to_compass_label() returns ("SE"),
# because this feeds narrative prose directly and "southeast" is what a
# reader reads. 8 points means 45-degree sectors, deliberately matching
# _ASPECT_CONSISTENCY_WINDOW_DEG below: a patch reporting 100%
# consistency is exactly one whose every cell falls in the named sector.
_COMPASS_WORDS = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
)

# Half-width of the window aspect_consistency_pct measures against the
# patch's own dominant aspect. 45 degrees = the 8-point compass sector
# the dominant aspect names.
_ASPECT_CONSISTENCY_WINDOW_DEG = 45.0

# A patch whose drawn-footprint centroid sits within this fraction of the
# parcel's equivalent-circle radius of the parcel's own centroid reads as
# "center" rather than a compass direction (position_in_parcel below) --
# naming a bearing for a near-central offset would narrate precision the
# position doesn't have. Deliberately its OWN constant, NOT aliased to
# water_candidate_zones.py's numerically-identical
# _CENTER_POSITION_MAX_OFFSET_FRACTION (same "constants stay separate even
# when identical" convention this pipeline already applies). CONFIGURABLE.
_CENTER_POSITION_MAX_OFFSET_FRACTION = 0.2

# How every score and factor in this block is to be read -- declared ONCE,
# here, rather than repeated per field or explained in prose next to each
# number. A declared scale plus "higher is better" is everything a
# narrative needs to interpret a value; the better form of "why" is the
# decomposition itself (size_factor into area_score and compactness_score),
# which is data the narrative reasons FROM rather than a sentence this
# module writes FOR it.
#
# BAND BOUNDS: lower-inclusive, upper-EXCLUSIVE, with the top band closing
# at 100. Every score here is a float rounded to 1 decimal place, so closed
# integer bands ([0, 39], [40, 59], ...) would leave 39.5 belonging to no
# band at all. The cut points -- 40, 60, 80 -- are unchanged; only the
# interval convention is stated explicitly so the bands are gapless and
# non-overlapping across the whole continuous range.
#
# The cuts are deliberately NOT even quartiles: 25/50/75 would call a 62.4
# merely "fair", which understates genuinely workable ground. Putting the
# "good" floor at 60 matches how these composites actually distribute.
# CONFIGURABLE -- tune against a real property, same as every other
# threshold in this pipeline.
_SCORE_BANDS = {
    "poor": [0.0, 40.0],
    "fair": [40.0, 60.0],
    "good": [60.0, 80.0],
    "excellent": [80.0, 100.0],
}

_SCALES = {
    "range": [0.0, 100.0],
    "direction": "higher_is_better",
    "bands": _SCORE_BANDS,
    "band_bounds": "lower_inclusive_upper_exclusive_last_band_inclusive",
    "applies_to": ["score", "factors.*", "area_score", "compactness_score"],
}


def _round1(value):
    """1 decimal place, or None passed straight through -- the single
    rounding boundary for this whole block. None means 'not known', and
    must never be silently rounded into a 0.0 that reads as a
    measurement."""
    return None if value is None else round(float(value), 1)


def _compass_word(aspect_deg) -> Optional[str]:
    """8-point compass word for a bearing in degrees, or None for an
    undefined aspect (flat ground has no orientation to name)."""
    if aspect_deg is None or math.isnan(float(aspect_deg)):
        return None
    return _COMPASS_WORDS[int(round((float(aspect_deg) % 360.0) / 45.0)) % 8]


def _angular_difference_deg(a: float, b: float) -> float:
    """Smallest absolute angle between two compass bearings (0-180)."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _position_in_parcel(patch_polygon_utm, boundary_polygon_utm: Polygon) -> str:
    """
    Where a patch sits within the parcel, as an 8-point compass word (or
    "center") -- the bearing from the parcel's own centroid to the patch
    footprint's centroid. UTM axes are +x east / +y north, so
    atan2(dx, dy) IS a compass bearing (0 = north, clockwise). The map
    legend labels feature CLASSES, not individual features, so this is
    what lets a narrative tell two Production Areas apart on the map.

    Deliberately this module's OWN copy of water_candidate_zones.py's
    private helper of the same name rather than an import: that module is
    a DOWNSTREAM KSOP step (water builds on production), so importing it
    here would invert the pipeline's layering -- same duplicated-with-
    documented-reason convention as dem_data.py's/fencing.py's/
    road_corridors.py's own _utm_epsg_for_lonlat() copies.
    """
    parcel_centroid = boundary_polygon_utm.centroid
    patch_centroid = patch_polygon_utm.centroid
    dx = patch_centroid.x - parcel_centroid.x
    dy = patch_centroid.y - parcel_centroid.y
    equivalent_radius = math.sqrt(boundary_polygon_utm.area / math.pi)
    if equivalent_radius <= 0 or math.hypot(dx, dy) <= equivalent_radius * _CENTER_POSITION_MAX_OFFSET_FRACTION:
        return "center"
    bearing_deg = math.degrees(math.atan2(dx, dy)) % 360.0
    return _COMPASS_WORDS[int(round(bearing_deg / 45.0)) % 8]


def _on_parcel_cell_mask(dem: dict, boundary_polygon_utm: Polygon) -> np.ndarray:
    """
    Boolean mask of DEM cells whose CENTER falls inside the real, FULL
    parcel boundary -- the same cell-center convention
    compute_step1_eligible_cells() uses for its own on-parcel test.

    This is the one thing in the narrative block that no pipeline step
    already computes, and it is not a re-run of anything: STEP 1 only ever
    tests cells against the SETBACK-SHRUNK boundary, and only among
    slope-passing cells, so neither the parcel's own elevation range nor
    the acreage the setback itself costs is recoverable from STEP 1's
    output. One vectorised shapely.contains_xy() call over the whole grid,
    once per pipeline run.
    """
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    col_centers = dem["origin_x"] + (np.arange(cols) + 0.5) * px
    row_centers = dem["origin_y"] - (np.arange(rows) + 0.5) * py
    xs, ys = np.meshgrid(col_centers, row_centers)
    return np.asarray(contains_xy(boundary_polygon_utm, xs, ys), dtype=bool)


def _waist_split_flags(scored: list[dict]) -> dict[int, bool]:
    """
    Which patches came out of STEP 3's Part-1 waist split, keyed by patch
    id -- DERIVED, because cluster_and_gate() does not record it and this
    branch does not change cluster_and_gate().

    The derivation is exact in the direction that matters: STEP 3 labels
    clusters 4-CONNECTED, so two cells in different clusters can never be
    edge-adjacent... unless the waist splitter cut one 4-connected
    component into pieces and reclaim_stripped_cells() pushed the stripped
    cells back out to the pinch, leaving the pieces touching. An
    edge-adjacency between two surviving patches is therefore proof of a
    waist split.

    Known limitation, documented rather than papered over: if a split's
    other piece(s) were then dropped by the min-area gate or the parcel
    clip, the lone survivor has no neighbour left to touch and reports
    False. This under-reports splits; it never invents one.
    """
    owner: dict[tuple[int, int], int] = {}
    for patch in scored:
        for cell in patch["cells"]:
            owner[(int(cell[0]), int(cell[1]))] = int(patch["id"])

    flags = {int(patch["id"]): False for patch in scored}
    for (r, c), patch_id in owner.items():
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            neighbour = owner.get((r + dr, c + dc))
            if neighbour is not None and neighbour != patch_id:
                flags[patch_id] = True
                flags[neighbour] = True
    return flags


def _patch_narrative_data(
    patch: dict,
    dem: dict,
    step1: dict,
    boundary_polygon_utm: Polygon,
    parcel_acres: float,
    parcel_elevation_range: Optional[tuple[float, float]],
    from_waist_split: bool,
) -> dict:
    """
    One patch's entry in narrative_data['patches'] -- see build_narrative_
    data()'s docstring for the field-by-field contract. Every value is
    read off arrays STEP 1 already produced and fields STEP 4 already
    attached to this patch; nothing here re-derives slope, aspect, the
    composite, or cluster membership.
    """
    cells = patch["cells"]
    slope_pct = step1["slope_pct"]
    aspect_deg = step1["aspect_deg"]

    patch_slopes = [float(slope_pct[r, c]) for r, c in cells]

    # dominant_aspect reuses STEP 4's OWN circular mean (patch['aspect_deg'],
    # already computed by score_production_areas()) rather than averaging
    # aspect a second time -- the narrated direction is the same direction
    # the patch's aspect_factor was scored on, not a parallel estimate of it.
    dominant_aspect_deg = patch["aspect_deg"]
    dominant_aspect = _compass_word(dominant_aspect_deg)

    # aspect_consistency_pct: what share of the patch's own cells actually
    # face the direction dominant_aspect names. This is what separates a
    # bench where nearly every cell faces one way from a patch wrapping a
    # spur -- both can report the same dominant aspect, and the two want
    # very different narration. Cells with no defined aspect (flat ground)
    # are excluded from BOTH sides of the ratio: flat ground doesn't
    # disagree with the dominant direction, it has no direction at all.
    # An all-flat patch has no dominant aspect to be consistent with, so
    # this is None rather than a misleading 100.
    defined_aspects = [
        float(aspect_deg[r, c]) for r, c in cells if not math.isnan(float(aspect_deg[r, c]))
    ]
    if dominant_aspect_deg is None or not defined_aspects:
        aspect_consistency_pct = None
    else:
        within = sum(
            1
            for a in defined_aspects
            if _angular_difference_deg(a, float(dominant_aspect_deg)) <= _ASPECT_CONSISTENCY_WINDOW_DEG
        )
        aspect_consistency_pct = int(round(within / len(defined_aspects) * 100.0))

    # source_region_hydric_pct: what share of the STEP 1 source region this
    # patch traces back to was hydric soil and excluded before the patch
    # could ever form. NAMED for what it measures, deliberately: the
    # patch's OWN cells are hydric-free by construction (the gate ran
    # cell-by-cell at STEP 1), so a hydric figure over the patch itself
    # would be a tautological 0.0. The meaningful number is how much wet
    # ground the patch is sitting NEXT TO -- and the field name has to
    # carry that, because a docstring will not survive contact with
    # whoever wires the narration. None -- never 0.0 -- when the SSURGO
    # fetch never ran.
    if not step1["soil_data_available"]:
        source_region_hydric_pct = None
    else:
        source_region = step1["slope_source_labels"] == patch["source_patch_id"]
        source_cells = int(source_region.sum())
        hydric_cells = int((source_region & step1["hydric_hit"]).sum())
        source_region_hydric_pct = _round1(hydric_cells / source_cells * 100.0) if source_cells else None

    # hole_count / hole_acres: real ground fully enclosed by this patch's
    # own eligible cells and excluded from it -- STEP 3's Part-2 true-hole
    # detection, already computed and attached as 'hole_footprints'. It
    # alters no geometry and feeds nothing downstream; it exists precisely
    # to answer "what is wrong with this otherwise-suitable ground" (a wet
    # spot mid-field, a rock outcrop), which is exactly the narrative's
    # question. Read off the footprints, not re-detected.
    hole_footprints = patch["hole_footprints"]
    hole_acres = _round1(sum(f.area for f in hole_footprints) / SQUARE_METERS_PER_ACRE)

    # elevation_percentile_of_parcel: where this patch's mean elevation
    # sits within the parcel's own low-to-high range, as a linear position
    # (0 = the parcel's lowest ground, 100 = its highest). Lets the
    # narrative place the zone as upper or lower ground without being
    # handed parcel-wide elevation data to reason over. None on a parcel
    # with no elevation relief at all, where "upper" and "lower" mean
    # nothing.
    mean_elevation = float(np.mean([float(dem["array"][r, c]) for r, c in cells]))
    if parcel_elevation_range is None:
        elevation_percentile = None
    else:
        low, high = parcel_elevation_range
        elevation_percentile = _round1(max(0.0, min(100.0, (mean_elevation - low) / (high - low) * 100.0)))

    return {
        "id": int(patch["id"]),
        "rank": int(patch["rank"]),
        "area_acres": _round1(patch["area_acres"]),
        "percent_of_parcel": _round1(patch["area_acres"] / parcel_acres * 100.0) if parcel_acres > 0 else None,
        "slope_min_pct": _round1(min(patch_slopes)),
        "slope_max_pct": _round1(max(patch_slopes)),
        "slope_median_pct": _round1(float(np.median(patch_slopes))),
        # The mean is what slope_factor was actually computed from, so it
        # belongs beside it -- min/max/median describe the spread, this one
        # explains the score. STEP 4 already computed and rounded it.
        "avg_slope_pct": _round1(patch["avg_slope_pct"]),
        "dominant_aspect": dominant_aspect,
        "aspect_consistency_pct": aspect_consistency_pct,
        # WHERE this patch sits on the map -- the map legend labels
        # feature CLASSES ("Production Areas"), not individual features,
        # so a cardinal position is what lets a narrative tell two
        # patches apart. Measured on the patch's DRAWN geometry
        # (render_fill_polygon_utm -- what the map actually shows and
        # what keypoint relationships/tree-zone claims already measure
        # against), not the raw cell-union footprint.
        "position_in_parcel": _position_in_parcel(patch["render_fill_polygon_utm"], boundary_polygon_utm),
        # aspect_available distinguishes a measured aspect_factor from the
        # neutral 1.0 STEP 4 defaults to on ground too flat for a
        # well-defined downhill direction. Without it an aspect_factor of
        # 100.0 cannot be told from "not measured" -- the same class of
        # silent falsehood the null-not-zero rule exists to prevent, and
        # equally undetectable by the reader.
        "aspect_available": bool(patch["aspect_available"]),
        "score": _round1(patch["suitability_score"]),
        # Every component of STEP 4's composite, all three of them, each
        # rescaled from its native 0-1 to 0-100 so they are directly
        # comparable to each other and to `score` with no scale
        # explanation needed. Higher is better for all three. Deliberately
        # NOT pre-selecting a "dominant" or "limiting" factor -- the
        # narrative reads them and decides.
        "factors": {
            "slope_factor": _round1(patch["slope_factor"] * 100.0),
            "size_factor": _round1(patch["size_factor"] * 100.0),
            "aspect_factor": _round1(patch["aspect_factor"] * 100.0),
        },
        # size_factor's own two halves, on the same 0-100 higher-is-better
        # scale. Without them a size_factor of 35 is ambiguous between
        # "this patch is small" and "this patch is a sliver" -- different
        # sentences with different management implications. Both were
        # already computed inside score_production_areas(); this reads
        # them, it does not recompute the geometry.
        "area_score": _round1(patch["area_score"] * 100.0),
        "compactness_score": _round1(patch["compactness_score"] * 100.0),
        # SSURGO component names and drainage class are NOT available on
        # this path: _fetch_disqualifying_soil_union() fetches the
        # component rows, reduces them to a set of disqualifying mukeys,
        # and returns only the unioned geometry -- the rows themselves are
        # discarded before STEP 1 ever sees them, and no per-cell mukey
        # attribution is kept. Reporting them would require plumbing the
        # component rows and their geometry through the soil fetch into
        # STEP 1, which is outside this branch. None (not 0.0, not an
        # empty list) so a consumer cannot read absence as a measurement.
        "soil_components": None,
        "drainage_class": None,
        "source_region_hydric_pct": source_region_hydric_pct,
        "elevation_percentile_of_parcel": elevation_percentile,
        "hole_count": len(hole_footprints),
        "hole_acres": hole_acres,
        "from_waist_split": bool(from_waist_split),
        "source_patch_id": int(patch["source_patch_id"]),
    }


def build_narrative_data(
    dem: dict,
    boundary_polygon_utm: Polygon,
    optimized: dict,
    scored: list[dict],
    ceiling_pct: float,
    max_slope_pct: float,
    total_selected_acreage: float,
    percent_of_parcel: float,
) -> dict:
    """
    The 'narrative_data' block identify_optimized_production_areas()
    attaches to its result -- pre-computed, FINAL, JSON-serialisable
    values answering "what makes the selected area(s) suitable for
    production?". Data only: no prose, no interpretation, no "this
    suggests" strings. See this section's own header comment for the two
    rules (FINAL; DERIVED, NEVER RECOMPUTED) every field obeys.

    SELF-SUFFICIENT BY DESIGN. A caller wiring the report reads this block
    and nothing else -- it never has to reach into 'scored_patches',
    'parcel_acres', 'percent_of_parcel', 'total_selected_acreage',
    'production_ceiling_target_met', or 'total_cells_removed' to finish a
    sentence. The overlap with those top-level keys is INTENDED, not
    redundancy to collapse: they serve existing KSOP consumers and must
    keep their own shape, this block serves the report and must keep its
    own, and neither may come to depend on the other. The one deliberate
    non-equivalence is 'ceiling.bound' -- see that field.

    Shape:

        {
          'scales': {                 # how to read every score/factor below,
                                      #   declared once instead of per field
            'range', 'direction', 'bands', 'band_bounds', 'applies_to',
          },
          'parcel': {
            'total_acres',            # the real, full parcel boundary
            'slope_passing_acres',    # STEP 1's slope_only_mask: cleared
                                      #   the slope gate AND sits inside
                                      #   the boundary setback
            'eligible_acres',         # STEP 1's eligible_mask: cleared
                                      #   every gate
            'selected_acres',         # what actually survived to STEP 4
            'selected_pct_of_parcel',
          },
          'ceiling': {
            'cap_pct_of_parcel',      # the configured cap
            'bound',                  # did the cap actually remove ground.
                                      #   DELIBERATELY not the existing
                                      #   'production_ceiling_target_met'
                                      #   flag: the two differ at exact
                                      #   equality, and "did it take ground
                                      #   away" is the question a narrative
                                      #   needs answered
            'acres_trimmed',          # 0.0 when it did not
          },
          'gates': { ... see the OVERLAP contract below ... },
          'patches': [ one entry per patch, in rank order ],
        }

    OVERLAP -- READ BEFORE USING THE GATE FIGURES. A single cell can be
    rejected by more than one gate at once: ground can be both hydric and
    under canopy. That makes the two per-gate figures answer two DIFFERENT
    questions, and only one of them may be added up.

        '<gate>_excluded_acres' counts EVERY cell that gate rejects,
            whether or not another gate rejects it too. It answers "how
            much of this ground carries canopy / is hydric / is road".
            THESE MUST NOT BE SUMMED -- ground rejected by two gates is
            counted in both, so their sum over-states the loss.

        '<gate>_only_excluded_acres' counts only cells NO other gate also
            rejects. It answers "how much would come back if this gate did
            not apply". These never double-count. They do not, on their
            own, close the books either: acreage rejected by two or more
            gates at once appears in none of them, so

                sum(*_only_excluded_acres) <= slope_passing_acres - eligible_acres

            with equality exactly when no cell is rejected by two gates.
            The shortfall is multiply-rejected ground, not missing ground.

    UNIVERSE OF THE GATE FIGURES: the canopy, hydric, and road gates are
    only ever evaluated on cells that already cleared the slope gate and
    the boundary setback (STEP 1 tests them against slope_only_mask), so
    these acreages are shares of SLOPE-PASSING, ON-PARCEL ground -- not of
    the whole parcel. Extending them parcel-wide would mean re-running
    three gates over ground the pipeline already rejected.

    BOUNDARY SETBACK: the setback is not a separate pass -- STEP 1 folds
    it into a single combined test (slope_only_mask = slope-ok AND inside
    the shrunk boundary), so setback-rejected cells are not separable from
    slope-rejected ones cell by cell. What IS recoverable, and what
    'boundary_setback_excluded_acres' reports, is on-parcel ground that
    CLEARS the slope gate and is excluded anyway because it lies in the
    setback ring: exactly the ground the setback alone costs, among ground
    that could otherwise have been used. Its '_only_' counterpart is None,
    not 0.0: the canopy, hydric, and road gates are never evaluated inside
    the setback ring at all, so what the setback alone costs net of those
    three is genuinely unknown here rather than known to be zero.

    UNAVAILABLE IS None: when soil_data_available / canopy_data_available /
    road_data_available is False, that gate's acreages and every
    soil-derived per-patch field are explicitly None. 0.0 would read as a
    measured absence.

    NO REASON STRINGS. Nothing here explains itself in prose. A declared
    scale plus higher-is-better tells a narrative how to read any number,
    and decomposing a composite into its parts (size_factor into
    area_score and compactness_score) is the better form of "why" --
    data the narrative reasons FROM, not a sentence this module writes FOR
    it. This module emits values; the report writes prose.
    """
    step1 = optimized["step1"]
    area_per_cell = cell_area_acres(dem)
    parcel_acres = float(optimized["parcel_acres"])

    slope_only_mask = step1["slope_only_mask"]
    eligible_mask = step1["eligible_mask"]
    canopy_hit = step1["tree_root_zone_hit"]
    hydric_hit = step1["hydric_hit"]
    road_hit = step1["road_hit"]

    soil_available = bool(step1["soil_data_available"])
    canopy_available = bool(step1["canopy_data_available"])
    road_available = bool(step1["road_data_available"])

    def _mask_acres(mask) -> float:
        return round(int(np.count_nonzero(mask)) * area_per_cell, 1)

    # A gate's "only" set: rejected here and by neither of the other two
    # cell-level gates. All three are already-computed masks -- this is
    # boolean algebra over them, not a second pass of any gate.
    only_canopy = canopy_hit & ~hydric_hit & ~road_hit
    only_hydric = hydric_hit & ~canopy_hit & ~road_hit
    only_road = road_hit & ~canopy_hit & ~hydric_hit

    on_parcel = _on_parcel_cell_mask(dem, boundary_polygon_utm)

    # The setback ring, recovered WITHOUT assuming which setback distance
    # STEP 1 used: slope_only_mask is (slope-ok AND inside the shrunk
    # boundary), so on-parcel slope-ok cells missing from it are exactly
    # the slope-ok cells the setback removed. Pure numpy over slope_pct,
    # an array STEP 1 already returned -- no geometry test, no gate re-run.
    slope_pct = step1["slope_pct"]
    slope_ok = (~np.isnan(slope_pct)) & (slope_pct <= max_slope_pct)
    setback_excluded = on_parcel & slope_ok & ~slope_only_mask

    parcel_elevations = dem["array"][on_parcel]
    parcel_elevations = parcel_elevations[~np.isnan(parcel_elevations)]
    if parcel_elevations.size and float(parcel_elevations.max()) > float(parcel_elevations.min()):
        parcel_elevation_range = (float(parcel_elevations.min()), float(parcel_elevations.max()))
    else:
        parcel_elevation_range = None

    waist_flags = _waist_split_flags(scored)

    # cells_removed and the per-cell acreage are both already in hand --
    # STEP 2's own trim result. Nothing is re-trimmed to report this.
    acres_trimmed = round(int(optimized["cells_removed"]) * area_per_cell, 1)

    return {
        # How to read every score and factor below -- declared once, so no
        # value needs a scale explanation attached to it.
        "scales": _SCALES,
        "parcel": {
            "total_acres": _round1(parcel_acres),
            "slope_passing_acres": _mask_acres(slope_only_mask),
            "eligible_acres": _mask_acres(eligible_mask),
            "selected_acres": _round1(total_selected_acreage),
            "selected_pct_of_parcel": _round1(percent_of_parcel),
        },
        "ceiling": {
            "cap_pct_of_parcel": _round1(ceiling_pct),
            "bound": bool(int(optimized["cells_removed"]) > 0),
            "acres_trimmed": acres_trimmed,
        },
        "gates": {
            # Every acreage in this block is a share of ON-PARCEL GROUND
            # THAT CLEARS THE SLOPE GATE -- not of the whole parcel. That
            # is parcel.slope_passing_acres PLUS
            # boundary_setback_excluded_acres; it is NOT parcel.total_acres.
            # Named here rather than folded into each field name because
            # the suffix would be factually wrong on the setback figure,
            # whose own exclusions sit OUTSIDE slope_passing by definition.
            "universe": "slope_passing_on_parcel",
            "canopy_excluded_acres": _mask_acres(canopy_hit) if canopy_available else None,
            "canopy_only_excluded_acres": _mask_acres(only_canopy) if canopy_available else None,
            "hydric_excluded_acres": _mask_acres(hydric_hit) if soil_available else None,
            "hydric_only_excluded_acres": _mask_acres(only_hydric) if soil_available else None,
            "farm_roads_excluded_acres": _mask_acres(road_hit) if road_available else None,
            "farm_roads_only_excluded_acres": _mask_acres(only_road) if road_available else None,
            "boundary_setback_excluded_acres": _mask_acres(setback_excluded),
            "boundary_setback_only_excluded_acres": None,
            # The constraint's own size, so a narrative can name it rather
            # than only report what it cost. Imperial, per this block's
            # units rule -- and PRODUCTION_BOUNDARY_SETBACK_METERS is
            # itself defined as 10 * METERS_PER_FOOT, so feet is the
            # constant's native unit, not a conversion of it.
            # identify_optimized_production_areas() never overrides the
            # setback, so this is the distance actually applied on this path.
            "boundary_setback_feet": _round1(PRODUCTION_BOUNDARY_SETBACK_METERS / METERS_PER_FOOT),
            "soil_data_available": soil_available,
            "canopy_data_available": canopy_available,
            "road_data_available": road_available,
        },
        "patches": [
            _patch_narrative_data(
                patch,
                dem,
                step1,
                boundary_polygon_utm,
                parcel_acres,
                parcel_elevation_range,
                waist_flags.get(int(patch["id"]), False),
            )
            for patch in sorted(scored, key=lambda p: p["rank"])
        ],
    }


def identify_optimized_production_areas(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_soil: bool = True,
    check_roads: bool = True,
    ceiling_pct: float = PRODUCTION_CEILING_PCT_OF_PARCEL,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
    reference_max_area_acres: float = REFERENCE_MAX_AREA_ACRES,
    canopy_height: Optional[dict] = None,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    real disqualifying-soil geometry, real existing-road exclusion
    geometry, and the required woody-vegetation tree-root-zone mask, ONCE
    each for the whole parcel boundary (STEP 1 needs all of it before any
    patch exists -- unlike the pre-consolidation architecture's per-patch
    soil fetch), then runs STEP 1 -> STEP 2 (the global worst-first trim
    toward ceiling_pct) -> STEP 3 (cluster_and_gate() on the survivors) ->
    STEP 4 (production_suitability.score_production_areas(), advisory
    ranking only).

    The soil and road fetches both degrade gracefully -- a USDA SDA or
    USGS transportation-service outage doesn't block scoring, it just
    means hydric/road exclusion couldn't be verified (soil_data_
    available=False / road_data_available=False on every result) -- same
    reasoning as every other optional network layer in this pipeline
    (farm_roads_data.py is itself already a known-incomplete, public-ROW-
    only signal, not a newly-closed detection gap the way canopy was, so
    a fetch failure here doesn't change what kind of answer this pipeline
    is already giving). The canopy fetch does NOT degrade: it is
    mandatory (no check_canopy flag), via production_area.get_required_
    tree_root_zone_mask_utm() -- the SAME shared fetch-or-raise helper
    production_area.identify_production_areas() itself calls, so this
    entry point (the one render_layout_map.py and tree_zone_candidates.py
    actually use) produces the identical eligible-cell geometry that
    function does, rather than silently omitting the woody-vegetation
    gate on this path the way it used to (a real bug: this function's own
    STEP 1 call previously passed compute_step1_eligible_cells() only 4
    positional arguments, leaving tree_root_zone_mask_utm on its "skip
    this gate" sentinel default). A canopy fetch failure -- retries
    exhausted, or canopy_height_data.CanopyCoverageIncompleteError for
    coverage too sparse to trust -- propagates up UNCAUGHT, same hard-fail
    behavior as production_area.identify_production_areas(); callers
    (render_layout_map.py, tree_zone_candidates.py) are expected to let
    this raise, not catch and degrade it.

    canopy_height is an optional pre-fetched override forwarded straight to
    get_required_tree_root_zone_mask_utm(): the SAME dict canopy_height_
    data.get_canopy_height_for_boundary() returns (e.g. parcel_data.
    ParcelData.canopy_height). When supplied, the mandatory canopy gate
    uses it rather than issuing its own Planetary Computer fetch; when None
    (the default) canopy is fetched here as before, leaving this gate's
    hard-fail semantics unchanged.

    Returns the same "production_area_candidate" GeoJSON FeatureCollection
    / scored_patches shape this pipeline has always returned, plus
    top-level summary fields describing the global trim:
        {
            'zones_geojson': dict,
            'scored_patches': list[dict],
            'total_selected_acreage': float,
            'percent_of_parcel': float,        # of the FULL parcel
            'parcel_acres': float,             # total parcel area (from STEP 2's optimizer)
            'production_ceiling_target_met': bool,
            'total_cells_removed': int,        # STEP 2's global trim only
            'narrative_data': dict,            # report-facing, FINAL, JSON-
                                               #   serialisable values --
                                               #   see build_narrative_data()
        }

    'narrative_data' is PURELY ADDITIVE: every other key above, and every
    field on every scored patch, is byte-identical to what this function
    returned before it existed. It answers one report question ("what
    makes the selected area(s) suitable for production?") with pre-
    computed, imperial, rounded values a narrative can quote directly
    without converting or relating anything -- and it is derived entirely
    from values STEP 1 through STEP 4 already produced, so adding it
    re-runs no gate, no clustering pass, and no scoring pass. See
    build_narrative_data()'s own docstring for the field contract, and in
    particular for which per-gate acreages may be summed and which may
    not.
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

    tree_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, canopy_height=canopy_height
    )

    road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED
    if check_roads:
        try:
            road_exclusion_union_utm = _fetch_road_exclusion_union_utm(list(boundary_coordinates), dem)
        except Exception:
            road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    optimized = optimize_production_areas(
        dem,
        boundary_polygon_utm,
        disqualifying_soil_union_utm,
        ceiling_pct=ceiling_pct,
        max_slope_pct=max_slope_pct,
        min_area_acres=min_area_acres,
        tree_root_zone_mask_utm=tree_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
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
        "parcel_acres": parcel_acres,
        "production_ceiling_target_met": optimized["production_ceiling_target_met"],
        "total_cells_removed": optimized["cells_removed"],
        "narrative_data": build_narrative_data(
            dem,
            boundary_polygon_utm,
            optimized,
            scored,
            ceiling_pct,
            max_slope_pct,
            total_selected_acreage,
            percent_of_parcel,
        ),
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
