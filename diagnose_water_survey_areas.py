"""
diagnose_water_survey_areas.py

THE TUNING INSTRUMENT for the survey-area water step
(water_survey_areas.py): a terminal table per survey type -- every
region, one line -- plus gate-mask stats and criteria-layer summaries,
and a feature_schema-compliant GeoJSON export
(WATER_SURVEY_AREAS_GEOJSON_PATH) for visual review over aerial imagery.

The export is where the provisional constants get tuned from. Its
suitability ISOBAND layers (contour bands of each type's surface at
ISOBAND_LEVELS, via contourpy -- the contour_lines.py precedent) are THE
threshold-tuning layer: the user opens the file over imagery and picks
SUITABILITY_THRESHOLD, the region floor, and any future presentation
rules from where regions cohere and dissolve. Region layers are flagged,
never filtered -- every region however small appears, carrying its
below_min_area flag rather than being trimmed away.

Layers written:
    survey_zone_embankment / survey_zone_excavated
        -- every SURVEY ZONE envelope, full properties incl. dual
           acreage and member linkage (water_survey_areas.
           survey_areas_to_geojson()'s own features, verbatim)
    survey_zone_member_embankment / survey_zone_member_excavated
        -- every member region footprint, intact, with zone_id linkage
    suitability_isoband_embankment / suitability_isoband_excavated
        -- filled contour bands of each RAW blended surface at
           ISOBAND_LEVELS (what extraction actually thresholds --
           pre-threshold smoothing is retired, see
           water_survey_areas.masked_focal_mean())
    criterion_isoband_<type>_<criterion>
        -- filled contour bands of every RAW criterion grid, both
           types (the excavated-diagnosis layer: which criterion kills
           the marsh is answerable over imagery)
    survey_context_production_area
        -- the optimized production patches the gravity/overlap
           measurements ran against
    survey_context_boundary
        -- the parcel boundary, carrying the gate-mask summary as
           properties (grid/on-parcel/ceiling-removed/gated cell counts)

The terminal output additionally prints the THRESHOLD COMPARISON
(region count / total acreage / largest region per type at 0.5/0.6/0.7
on the RAW surfaces, 8-connected -- the 0.6 default stays tunable from
evidence every run), the depression-depth distribution before/after the
noise floor with the 10 deepest-fill cells' full scoring rows -- now
including each cell's SSURGO map unit and its three soil sub-signals
(ksat score / hydrologic-group score / hydric share), the soil-oddity
rider: the tuning run put the parcel-MINIMUM soil score (0.279) on
exactly the wettest ground, so the excavated follow-up branch needs to
see, per marsh cell, WHICH sub-signal produced that number -- and the
EXCAVATED FINDING, an evidence-based statement of which suspect (noise
floor / depth scaling / slope classes / soil ceiling) the numbers
indict, as the next branch's input.

STORED WIRE FORMS ONLY: every geometry this file serializes was built as
WGS84 at its object's birth -- regions at region birth
(water_survey_areas.extract_survey_regions()), isobands at band birth
(compute_suitability_isobands() below), the boundary from the caller's
own WGS84 coordinates, production patches from their own stored
geometry_wgs84. No serialization-time reprojection exists in any
*_features()/export function here (grep-asserted in
test_water_survey_areas.py).

The OLD water diagnostic (diagnose_water_zone_mask.py) remains pointed
at the DEMOTED level-pool modules, deliberately -- it diagnoses that
arc; this file diagnoses the survey-area step that replaced it on the
pipeline path.

Run:  python diagnose_water_survey_areas.py   (networked -- fetches DEM,
production areas, canopy, roads, soil for the reference property)
"""

import json

import numpy as np
from shapely.geometry import MultiPolygon, Polygon

import contourpy

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection, validate_feature_collection
from production_area_ceiling import identify_optimized_production_areas
from raster_grid import cell_area_acres, connected_components, pixel_center_xy
from rasterio.warp import transform_geom
from shapely.geometry import mapping
from water_survey_areas import (
    DEPRESSION_FULL_CREDIT_METERS,
    DEPRESSION_NOISE_FLOOR_METERS,
    EXCAVATED_WEIGHTS,
    SURVEY_TYPE_EXCAVATED,
    SURVEY_TYPES,
    WATER_REGION_CONNECTIVITY,
    identify_water_survey_areas,
    survey_areas_to_geojson,
)

# Where the export lands, beside this script's terminal output. Passed
# explicitly at the call site rather than read as a bound default -- a
# module constant used as a default argument is bound once at import, so
# it stops being configurable the moment anything wants to change it.
WATER_SURVEY_AREAS_GEOJSON_PATH = "water_survey_areas.geojson"

# The isoband edges: bands are [0.2,0.4), [0.4,0.6), [0.6,0.8), and
# [0.8, top]. These are THE threshold-tuning instrument -- the 0.4/0.6/
# 0.8 edges bracket the provisional SUITABILITY_THRESHOLD (0.6) so a
# viewer sees at a glance which ground a nudged threshold would gain or
# lose. Blended-surface bands are drawn on the RAW surfaces (exactly
# what extraction thresholds -- pre-threshold smoothing is retired);
# per-criterion bands likewise on the RAW criterion grids.
# CONFIGURABLE.
ISOBAND_LEVELS = (0.2, 0.4, 0.6, 0.8)

# The thresholds the comparison table evaluates on the RAW surfaces --
# printed every run so the 0.6 default remains a choice re-decided
# against evidence (member-region count / acreage / largest region per
# candidate threshold), never trusted forward. CONFIGURABLE.
THRESHOLD_COMPARISON_LEVELS = (0.5, 0.6, 0.7)

# Upper edge of the top band: suitability is capped at 1.0 by
# construction, and contourpy's filled() upper bound is exclusive-ish at
# exact grid values -- a hair above 1.0 keeps perfect-score cells inside
# the top band instead of on its edge.
_ISOBAND_TOP = 1.000001


def _grid_axes(dem: dict) -> tuple[np.ndarray, np.ndarray]:
    """Cell-center x (ascending) / y (DESCENDING) axes in dem['crs']
    meters -- the contour_lines.py precedent verbatim: contourpy accepts
    a descending y axis correctly, so the array is never flipped."""
    rows, cols = dem["array"].shape
    x = np.array([pixel_center_xy(dem, 0, c)[0] for c in range(cols)])
    y = np.array([pixel_center_xy(dem, r, 0)[1] for r in range(rows)])
    return x, y


def compute_suitability_isobands(dem: dict, surface: np.ndarray, levels: tuple = ISOBAND_LEVELS) -> list[dict]:
    """
    Filled contour bands of one suitability surface at `levels`, via
    contourpy (the contour_lines.py precedent: the real, public
    contour_generator API, no plotting machinery). Each band dict carries
    BOTH forms, built here at band birth -- shapely
    polygons_utm (dem['crs'] meters, for any downstream math) and
    geometry_wgs84 (the stored wire form the export serializes) -- the
    same dual-geometry-at-birth pattern contour_lines.compute_contour_
    lines() established.

    FillType.OuterOffset: filled(lower, upper) returns one point array
    per output polygon (outer ring first, holes after, delimited by the
    offsets array), which maps 1:1 onto shapely Polygon(exterior, holes).
    Empty bands are simply absent from the result.
    """
    x, y = _grid_axes(dem)
    generator = contourpy.contour_generator(x=x, y=y, z=surface, fill_type=contourpy.FillType.OuterOffset)

    edges = list(levels) + [_ISOBAND_TOP]
    bands = []
    for lower, upper in zip(edges[:-1], edges[1:]):
        points_list, offsets_list = generator.filled(float(lower), float(upper))
        polygons = []
        for points, offsets in zip(points_list, offsets_list):
            rings = [points[start:end] for start, end in zip(offsets[:-1], offsets[1:])]
            rings = [ring for ring in rings if len(ring) >= 4]
            if not rings:
                continue
            polygons.append(Polygon(rings[0], holes=rings[1:]))
        if not polygons:
            continue
        geometry_utm = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
        bands.append(
            {
                "band_lower": float(lower),
                # The top band reports its nominal 1.0 edge, not the
                # float guard above it.
                "band_upper": float(min(upper, 1.0)),
                "polygons_utm": geometry_utm,
                "geometry_wgs84": transform_geom(dem["crs"], "EPSG:4326", mapping(geometry_utm)),
            }
        )
    return bands


def _isoband_features(survey_type: str, bands: list[dict]) -> list[dict]:
    """Feature-wraps prebuilt isoband dicts -- stored geometry_wgs84
    only, no reprojection here."""
    features = []
    for band in bands:
        features.append(
            make_feature(
                feature_id=f"suitability-isoband-{survey_type}-{band['band_lower']}",
                geometry=band["geometry_wgs84"],
                layer=f"suitability_isoband_{survey_type}",
                label=(
                    f"{survey_type.capitalize()} suitability {band['band_lower']}"
                    f"-{band['band_upper']}"
                ),
                confidence=CONFIDENCE_LOW,
                confidence_notes=(
                    "THE THRESHOLD-TUNING LAYER: a filled contour band of the "
                    f"{survey_type}-type RAW blended suitability surface -- exactly what extraction "
                    "thresholds (pre-threshold smoothing is retired; per-criterion bands ride the "
                    "criterion_isoband_* layers). Overlay these bands on imagery and pick the "
                    "extraction threshold and member floor from where regions cohere and "
                    "dissolve -- every weight and table behind this surface is a provisional v1 "
                    "prior (TUNE FROM RUN, water_survey_areas.py)."
                ),
                extra_properties={
                    "survey_type": survey_type,
                    "band_lower": band["band_lower"],
                    "band_upper": band["band_upper"],
                },
            )
        )
    return features


def _context_features(
    boundary_coordinates: list[tuple[float, float]],
    production_areas: list[dict],
    gate_mask_stats: dict,
) -> list[dict]:
    """The context layers: production patches (their own stored
    geometry_wgs84) and the parcel boundary (the caller's own WGS84
    coordinates, ring closed here -- coordinates, not a reprojection),
    carrying the gate-mask summary as properties."""
    features = []
    for patch in production_areas:
        geometry = patch.get("geometry_wgs84")
        if geometry is None:
            continue
        features.append(
            make_feature(
                feature_id=f"survey-context-production-area-{patch['id']}",
                geometry=geometry,
                layer="survey_context_production_area",
                label=f"Production area {patch['id']} (context)",
                confidence=CONFIDENCE_LOW,
                confidence_notes=(
                    "Context layer: the optimized production patch the survey regions' gravity "
                    "relationships and production-overlap percentages were measured against."
                ),
                extra_properties={"production_area_id": patch["id"], "area_acres": patch.get("area_acres")},
            )
        )

    ring = [list(point) for point in boundary_coordinates]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    features.append(
        make_feature(
            feature_id="survey-context-boundary",
            geometry={"type": "Polygon", "coordinates": [ring]},
            layer="survey_context_boundary",
            label="Parcel boundary (context, with gate-mask summary)",
            confidence=CONFIDENCE_LOW,
            confidence_notes=(
                "Context layer: the parcel boundary, carrying the gate-mask accounting as "
                "properties -- how many cells the unchanged gate trio (on-parcel, contributing-area "
                "ceiling, inert boundary setback) left in play for both surfaces."
            ),
            extra_properties=dict(gate_mask_stats),
        )
    )
    return features


def export_water_survey_areas_geojson(
    identify_result: dict,
    boundary_coordinates: list[tuple[float, float]],
    production_areas: list[dict],
    isobands_by_type: dict,
    path: str,
    criterion_isobands_by_type: dict | None = None,
) -> dict:
    """
    Writes the full tuning export to one feature_schema-compliant
    GeoJSON file: every survey zone envelope and member footprint (both
    typed layer families, flagged not filtered), the blended
    (RAW-surface) isoband layers, the per-criterion isoband layers when
    supplied, and the context layers. Consumes ONLY stored wire forms --
    each zone's and member's own geometry_wgs84, prebuilt isoband dicts,
    the caller's WGS84 boundary coordinates, and each production patch's
    stored geometry_wgs84. Validates before writing; returns {'path',
    'feature_count', 'by_layer'}.
    """
    features = list(survey_areas_to_geojson(identify_result["zones"])["features"])
    for survey_type in SURVEY_TYPES:
        features.extend(_isoband_features(survey_type, isobands_by_type[survey_type]))
    if criterion_isobands_by_type:
        features.extend(_criterion_isoband_features(criterion_isobands_by_type))
    features.extend(_context_features(boundary_coordinates, production_areas, identify_result["gate_mask_stats"]))

    collection = make_feature_collection(features)
    validate_feature_collection(collection)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(collection, handle, indent=2)

    by_layer: dict = {}
    for feature in features:
        layer = feature["properties"]["layer"]
        by_layer[layer] = by_layer.get(layer, 0) + 1
    return {"path": path, "feature_count": len(features), "by_layer": by_layer}


# --- terminal table --------------------------------------------------------

def _gravity_cell(region: dict) -> str:
    primary = region["primary_production_area_relationship"]
    if primary is None:
        return "no service rel."
    label = "gravity-feeds" if primary["above_production_area"] else "PUMP-REQUIRED"
    return f"{label} PA{primary['production_area_id']} ({primary['elevation_differential_m']:+.1f}m)"


def _overlap_cell(value) -> str:
    return "n/c" if value is None else f"{value}%"


def summarize_survey_zones_table(identify_result: dict) -> str:
    """One line per SURVEY ZONE, per type -- rank, member count, DUAL
    acreage (zone acres to survey, anchored by member acres), member-
    cell mean/max suitability, top two contributing criteria, gravity
    note, envelope overlaps, boundary adjacency, flags. Every zone
    appears (flagged, never filtered)."""
    lines = []
    for survey_type in SURVEY_TYPES:
        zones = identify_result["zones_by_type"][survey_type]
        lines.append(f"=== {survey_type.upper()}-TYPE SURVEY ZONES ({len(zones)}) ===")
        if not zones:
            lines.append("  (none cleared the threshold)")
            continue
        for zone in zones:
            top_two = sorted(
                zone["criterion_contributions"].items(),
                key=lambda item: -item[1]["weighted_contribution"],
            )[:2]
            criteria_text = "+".join(f"{name}({entry['mean_score']})" for name, entry in top_two)
            flags = f" flags={','.join(zone['flags'])}" if zone["flags"] else ""
            lines.append(
                f"  #{zone['rank']} zone {zone['id']}: {zone['zone_acres']:.2f} ac to survey "
                f"anchored by {zone['member_acres']:.2f} ac ({zone['member_count']} member(s)), "
                f"mean {zone['mean_suitability']:.3f} / max {zone['max_suitability']:.3f}, "
                f"top: {criteria_text}, {_gravity_cell(zone)}, "
                f"canopy {_overlap_cell(zone['canopy_overlap_pct'])} / road "
                f"{_overlap_cell(zone['road_overlap_pct'])} / prod "
                f"{_overlap_cell(zone['production_overlap_pct'])}, "
                f"boundary-adj {zone['boundary_adjacency_fraction']:.0%}, "
                f"conf {zone['confidence']}{flags}"
            )
    return "\n".join(lines)


def summarize_gate_and_criteria(identify_result: dict) -> str:
    """Gate-mask stats plus a min/mean/max summary of every criteria
    layer over the gated cells -- what each classification table
    actually scored on this property, so a mis-centered table is visible
    in one glance rather than hidden inside a blend."""
    stats = identify_result["gate_mask_stats"]
    result = identify_result["result"]
    mask = result["gate_mask"]
    lines = [
        "=== GATE MASK ===",
        f"  grid {stats['grid_cells']} cells; on-parcel {stats['on_parcel_cells']}; "
        f"ceiling removed {stats['ceiling_removed_cells']} "
        f"(> {stats['max_contributing_area_acres']} ac); "
        f"setback removed {stats['setback_removed_cells']} "
        f"(setback {stats['min_boundary_setback_meters']} m, inert at 0.0); "
        f"in play {stats['gated_cells']}",
        "=== CRITERIA LAYERS (over gated cells: min / mean / max) ===",
    ]
    for survey_type in SURVEY_TYPES:
        lines.append(f"  {survey_type}:")
        for name, grid in result["surfaces"]["criteria"][survey_type].items():
            values = grid[mask]
            values = values[~np.isnan(values)]
            if values.size == 0:
                lines.append(f"    {name}: (no gated cells)")
                continue
            lines.append(
                f"    {name}: {float(np.min(values)):.3f} / {float(np.mean(values)):.3f} / "
                f"{float(np.max(values)):.3f}"
            )
        surface = result["surfaces"][survey_type][mask]
        lines.append(
            f"    -> surface: {float(np.min(surface)):.3f} / {float(np.mean(surface)):.3f} / "
            f"{float(np.max(surface)):.3f} (threshold {result['threshold']})"
        )
    return "\n".join(lines)


def summarize_threshold_comparison(identify_result: dict, dem: dict) -> str:
    """
    THE THRESHOLD RE-VERIFICATION: member-region count, total acreage,
    and largest-region acreage per type at each
    THRESHOLD_COMPARISON_LEVELS value, on the RAW surfaces (the ones
    extraction actually thresholds -- pre-threshold smoothing is
    retired), 8-connected -- so the 0.6 default stays a choice made
    from evidence, re-decided every run.
    """
    result = identify_result["result"]
    gate_mask = result["gate_mask"]
    area_per_cell = cell_area_acres(dem)
    lines = ["=== THRESHOLD COMPARISON (raw surfaces, 8-connected) ==="]
    for survey_type in SURVEY_TYPES:
        surface = result["surfaces"][survey_type]
        lines.append(f"  {survey_type}:")
        for threshold in THRESHOLD_COMPARISON_LEVELS:
            member_mask = gate_mask & (surface >= threshold)
            labels, count = connected_components(member_mask, connectivity=WATER_REGION_CONNECTIVITY)
            total_cells = int(np.count_nonzero(member_mask))
            largest_cells = 0
            for label in range(count):
                largest_cells = max(largest_cells, int(np.count_nonzero(labels == label)))
            marker = "  <- default" if threshold == result["threshold"] else ""
            lines.append(
                f"    t={threshold}: {count} region(s), {total_cells * area_per_cell:.2f} ac total, "
                f"largest {largest_cells * area_per_cell:.2f} ac{marker}"
            )
    return "\n".join(lines)


def summarize_depression_instrumentation(identify_result: dict, dem: dict) -> str:
    """
    The excavated-class interrogation, part 1: the depression-depth
    distribution over GATED cells BEFORE and AFTER the noise floor, plus
    the 10 deepest-fill cells' full scoring row (raw depth, floored
    depth, TWI percentile, wetness criterion, slope score, soil score --
    AND the soil-oddity rider: the SSURGO map unit plus the three soil
    sub-signal values (ksat score / hydrologic-group score / hydric
    share) behind each cell's soil number, so the excavated follow-up
    branch can tell a data surprise from a scorer defect -- and the raw
    blended excavated score) -- the marsh cells, interrogated directly.
    """
    result = identify_result["result"]
    gate_mask = result["gate_mask"]
    screens = result["screens"]
    raw_depth = screens["depression_depth_raw"]
    floored_depth = screens["depression_depth"]
    criteria = result["surfaces"]["criteria"][SURVEY_TYPE_EXCAVATED]

    lines = ["=== DEPRESSION-DEPTH DISTRIBUTION (gated cells) ==="]
    for label, depth in (("BEFORE noise floor (raw fill)", raw_depth), ("AFTER noise floor", floored_depth)):
        values = depth[gate_mask]
        values = values[~np.isnan(values)]
        nonzero = values[values > 0]
        if nonzero.size:
            lines.append(
                f"  {label}: {nonzero.size} nonzero cell(s) of {values.size}; "
                f"min {float(np.min(nonzero)):.3f} / mean {float(np.mean(nonzero)):.3f} / "
                f"max {float(np.max(nonzero)):.3f} m"
            )
        else:
            lines.append(f"  {label}: 0 nonzero cell(s) of {values.size}")
    all_raw = raw_depth[gate_mask]
    all_raw = all_raw[~np.isnan(all_raw)]
    if all_raw.size:
        lines.append(f"  Parcel's deepest fill: {float(np.max(all_raw)):.3f} m (noise floor {DEPRESSION_NOISE_FLOOR_METERS} m)")

    lines.append("=== 10 DEEPEST-FILL CELLS (the marsh, interrogated; soil sub-signals per cell) ===")
    lines.append(
        "  (row,col)      raw_m  floor_m  twi_pct  wetness  slope_sc  soil_sc  ksat_sc  grp_sc  hydric_sh  mukey       exc"
    )
    soil = result["soil"]
    mukey_by_cell = soil.get("mukey_by_cell", {})
    scores_by_mukey = soil.get("scores_by_mukey", {})

    def _sub(value) -> str:
        return "   n/a " if value is None else f"{value:7.3f}"

    gated_cells = np.argwhere(gate_mask)
    if gated_cells.size:
        depths = np.array([raw_depth[r, c] for r, c in gated_cells])
        order = np.argsort(np.nan_to_num(depths, nan=-1.0))[::-1][:10]
        for index in order:
            r, c = (int(v) for v in gated_cells[index])
            twi = result["screens"]["twi_percentile"][r, c]
            mukey = mukey_by_cell.get((r, c))
            subs = scores_by_mukey.get(mukey, {}) if mukey is not None else {}
            lines.append(
                f"  ({r:>3},{c:>3})  "
                f"{raw_depth[r, c]:7.3f}  {floored_depth[r, c]:7.3f}  "
                f"{(twi if not np.isnan(twi) else float('nan')):7.3f}  "
                f"{criteria['wetness'][r, c]:7.3f}  {criteria['slope'][r, c]:8.3f}  "
                f"{criteria['soil'][r, c]:7.3f}  "
                f"{_sub(subs.get('ksat_score'))}  {_sub(subs.get('hydrologic_group_score'))} "
                f"{_sub(subs.get('hydric_score'))}   "
                f"{(mukey if mukey is not None else 'uncovered'):<10}  "
                f"{result['surfaces'][SURVEY_TYPE_EXCAVATED][r, c]:7.3f}"
            )
    return "\n".join(lines)


def state_excavated_finding(identify_result: dict) -> str:
    """
    The stated finding, not a fix: from the instrumentation's own
    numbers, which of the four suspects the evidence indicts for the
    excavated class's failure to produce --
      1. the 0.1 m noise floor (real basins zeroed before scoring),
      2. depth-to-score scaling (real floored depth scoring too little),
      3. the slope classes (moderate ground scored as too steep),
      4. the soil sub-weight ceiling arithmetic-limiting the blend.
    Computed as each criterion's mean weighted SHORTFALL from a perfect
    score at the 10 deepest-fill cells (the marsh proxy: the ground the
    class exists to find), with the wetness shortfall split into its
    TWI/depression halves and the depression half split floor-vs-scaling
    by comparing raw and floored depth. Excavated weight/class retuning
    happens in the NEXT branch, from this statement.
    """
    result = identify_result["result"]
    gate_mask = result["gate_mask"]
    screens = result["screens"]
    criteria = result["surfaces"]["criteria"][SURVEY_TYPE_EXCAVATED]

    gated_cells = np.argwhere(gate_mask)
    if not gated_cells.size:
        return "EXCAVATED FINDING: no gated cells at all -- nothing to diagnose."
    raw_depth = screens["depression_depth_raw"]
    depths = np.array([raw_depth[r, c] for r, c in gated_cells])
    order = np.argsort(np.nan_to_num(depths, nan=-1.0))[::-1][:10]
    deepest = [(int(gated_cells[i][0]), int(gated_cells[i][1])) for i in order]

    shortfalls = {}
    for name, weight in EXCAVATED_WEIGHTS.items():
        mean_score = float(np.mean([criteria[name][r, c] for r, c in deepest]))
        shortfalls[name] = {"weight": weight, "mean_score": mean_score, "weighted_shortfall": weight * (1.0 - mean_score)}

    lines = ["=== EXCAVATED FINDING (stated from evidence, not fixed here) ==="]
    for name, entry in sorted(shortfalls.items(), key=lambda item: -item[1]["weighted_shortfall"]):
        lines.append(
            f"  {name}: mean score {entry['mean_score']:.3f} at the 10 deepest-fill cells -> "
            f"weighted shortfall {entry['weighted_shortfall']:.3f} of the blend's 1.0"
        )

    top_name = max(shortfalls, key=lambda name: shortfalls[name]["weighted_shortfall"])
    if top_name == "wetness":
        # Split the wetness deficit into its halves, and the depression
        # half into floor-vs-scaling.
        floored = screens["depression_depth"]
        raw_vals = [raw_depth[r, c] for r, c in deepest]
        floored_vals = [floored[r, c] for r, c in deepest]
        zeroed_by_floor = sum(1 for raw, flr in zip(raw_vals, floored_vals) if raw > 0 and flr == 0.0)
        mean_dep_score = float(np.mean([min(max(flr / DEPRESSION_FULL_CREDIT_METERS, 0.0), 1.0) for flr in floored_vals]))
        twi_vals = [screens["twi_percentile"][r, c] for r, c in deepest]
        mean_twi = float(np.nanmean(twi_vals)) if twi_vals else float("nan")
        lines.append(
            f"  wetness split: mean TWI percentile {mean_twi:.3f}; mean depression score {mean_dep_score:.3f}; "
            f"{zeroed_by_floor}/10 deepest cells had real fill zeroed by the {DEPRESSION_NOISE_FLOOR_METERS} m floor"
        )
        if zeroed_by_floor >= 5:
            verdict = "the NOISE FLOOR (suspect 1): most of the deepest real fill never reaches the scorer"
        elif mean_dep_score < 0.5 and float(np.mean(floored_vals)) > 0:
            verdict = "DEPTH-TO-SCORE SCALING (suspect 2): real floored depth survives but scores too little"
        else:
            verdict = "the TWI half of wetness: even the deepest fill's neighborhood ranks too dry parcel-relative"
    elif top_name == "slope":
        verdict = "the SLOPE CLASSES (suspect 3): the marsh cells' ground scores as too steep for a dugout"
    elif top_name == "soil":
        soil_max = float(np.max(criteria["soil"][gate_mask])) if np.any(gate_mask) else 0.0
        verdict = (
            f"the SOIL CEILING (suspect 4): soil tops out at {soil_max:.3f} on this parcel and "
            "arithmetic-limits the blend below threshold"
        )
    else:
        verdict = f"the {top_name} criterion (largest weighted shortfall at the marsh cells)"
    lines.append(f"  EVIDENCE INDICTS: {verdict}.")
    lines.append("  This statement is the NEXT branch's input; no weight or class was changed here.")
    return "\n".join(lines)


def compute_criterion_isobands(dem: dict, identify_result: dict) -> dict:
    """
    Per-criterion isobands for BOTH types, from the RAW criterion grids
    (zeroed outside the gate mask, so bands stay on in-play ground) --
    the layer that answers "which criterion kills the marsh" over
    imagery. Returns {survey_type: {criterion: bands}}.
    """
    result = identify_result["result"]
    gate_mask = result["gate_mask"]
    bands_by_type: dict = {}
    for survey_type in SURVEY_TYPES:
        bands_by_type[survey_type] = {}
        for name, grid in result["surfaces"]["criteria"][survey_type].items():
            gated_grid = np.where(gate_mask, grid, 0.0)
            bands_by_type[survey_type][name] = compute_suitability_isobands(dem, gated_grid)
    return bands_by_type


def _criterion_isoband_features(criterion_isobands_by_type: dict) -> list[dict]:
    """Feature-wraps prebuilt per-criterion band dicts -- stored
    geometry_wgs84 only, no reprojection here."""
    features = []
    for survey_type, by_criterion in criterion_isobands_by_type.items():
        for criterion, bands in by_criterion.items():
            for band in bands:
                features.append(
                    make_feature(
                        feature_id=f"criterion-isoband-{survey_type}-{criterion}-{band['band_lower']}",
                        geometry=band["geometry_wgs84"],
                        layer=f"criterion_isoband_{survey_type}_{criterion}",
                        label=f"{survey_type} {criterion} {band['band_lower']}-{band['band_upper']}",
                        confidence=CONFIDENCE_LOW,
                        confidence_notes=(
                            f"Per-criterion tuning layer: the RAW {criterion} criterion of the "
                            f"{survey_type}-type surface (no smoothing -- what the ground actually "
                            "scored), banded so the question 'which criterion kills a candidate area' "
                            "is answerable over imagery."
                        ),
                        extra_properties={
                            "survey_type": survey_type,
                            "criterion": criterion,
                            "band_lower": band["band_lower"],
                            "band_upper": band["band_upper"],
                        },
                    )
                )
    return features


def main() -> None:
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Identifying water survey areas for the reference property (networked)...\n")
    # dem and production are fetched HERE and passed as overrides -- the
    # diagnostic needs both again for the export (isoband axes, context
    # layer), and the override pattern means neither is fetched twice.
    dem = get_dem_for_boundary(property_boundary)
    production_areas = identify_optimized_production_areas(property_boundary, dem=dem)["scored_patches"]
    identify_result = identify_water_survey_areas(
        property_boundary, dem=dem, production_areas=production_areas
    )

    print(summarize_survey_zones_table(identify_result))
    print()
    print(summarize_gate_and_criteria(identify_result))
    print()
    print(summarize_threshold_comparison(identify_result, dem))
    print()
    print(summarize_depression_instrumentation(identify_result, dem))
    print()
    print(state_excavated_finding(identify_result))

    result = identify_result["result"]
    # surfaces[type] is the SMOOTHED blend -- the blended isobands show
    # exactly what extraction thresholds; the per-criterion bands below
    # show the RAW ground.
    isobands_by_type = {
        survey_type: compute_suitability_isobands(dem, result["surfaces"][survey_type])
        for survey_type in SURVEY_TYPES
    }
    criterion_isobands_by_type = compute_criterion_isobands(dem, identify_result)

    export = export_water_survey_areas_geojson(
        identify_result,
        property_boundary,
        production_areas,
        isobands_by_type,
        path=WATER_SURVEY_AREAS_GEOJSON_PATH,
        criterion_isobands_by_type=criterion_isobands_by_type,
    )
    print(f"\nWrote {export['feature_count']} feature(s) to {export['path']} (feature_schema-validated):")
    for layer, count in sorted(export["by_layer"].items()):
        print(f"  {layer}: {count}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # pragma: no cover - networked entry point
        print(f"Request failed: {error}")
        print(
            "\nNote: this requires internet access to reach USGS's National Map, "
            "Planetary Computer (canopy), USDA Soil Data Access, and road data "
            "services -- not a fully sandboxed environment."
        )
