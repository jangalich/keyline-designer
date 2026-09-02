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
SUITABILITY_THRESHOLD and the zone-acre floor from where regions cohere
and dissolve (a presentation cap was tried for one pass and deleted --
all survivors ship). Region layers are flagged, never filtered -- every
region however small appears, carrying its below_min_area flag rather
than being trimmed away.

Layers written:
    survey_zone_embankment / survey_zone_excavated
        -- every SURVEY ZONE envelope, full properties incl. dual
           acreage and member linkage (water_survey_areas.
           survey_areas_to_geojson()'s own features, verbatim)
    survey_zone_member_embankment / survey_zone_member_excavated
        -- every member region footprint, intact, with zone_id linkage
    survey_zone_dropped
        -- zones the ZONE-acreage floor filtered OUT of the pipeline
           output, carried here with status: dropped + drop_reason
           (visible and attributed, never silent)
    embankment_seed / embankment_seed_failed / embankment_pinch /
    embankment_baseline / embankment_transect
        -- the compartment instrument layers (seed points with blend
           scores; failed seeds with reason codes -- the dropped-
           feature pattern, seed edition; pinch points with
           crest-to-crest width and walk distance; baseline and
           transect lines), for surviving AND dropped compartments
    suitability_isoband_embankment / suitability_isoband_excavated
        -- filled contour bands of each RAW blended surface at
           ISOBAND_LEVELS (the excavated one is what extraction
           thresholds; the embankment one is the NOMINATION surface
           the seeding claims from -- pre-threshold smoothing is
           retired, see water_survey_areas.masked_focal_mean())
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

The terminal output additionally prints the SEED LADDER -- every
embankment seed in blend-descending order with its criteria signature
and what became of it (reason code and terminator for a failure; band
and hull acreage plus the floor verdict for a compartment), then the
same seeds bucketed into blend bands with per-band seeded/built/survived
counts. THAT TABLE IS WHERE EMBANKMENT_SEED_MIN_SCORE IS TUNED FROM: it
is the only place a seed that was nominated and produced nothing is
visible at all, which is what makes a lowered seeding minimum
checkable rather than merely asserted. The THRESHOLD COMPARISON
(region count / total acreage / largest region at 0.5/0.6/0.7 on the
RAW EXCAVATED surface, 8-connected -- the 0.5 default stays tunable
from evidence every run; the embankment lines are RETIRED with
extraction and the instrument records why every run -- see
summarize_threshold_comparison()), the depression-depth distribution before/after the
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

THE TWO-BOUNDARY SECTIONS (this file's standing regression against
boundary-dependent scoring -- see the BOUNDARY-STABILITY INSTRUMENT
header further down) run the whole water step against TWO boundaries
over ONE DEM and print:

    TWI CALIBRATION -- the REFERENCE WINDOW each boundary scored
        against, its raw ln(a/tan(beta)) distribution, and the two
        breakpoints derived from it, beside the distribution over GATED
        cells (context only -- no breakpoint is read from the gate).
        There are no hardcoded breakpoints left to calibrate; what this
        section now tunes is the two PERCENTILE choices, and it prints
        the flooring share on both the live and the retired fixed curve
        so the change is readable.
    REFERENCE WINDOW SNAP -- what window each boundary would fetch on
        its OWN (computed, not fetched, via dem_data.
        dem_window_bounds()), what that window snaps to, and whether
        both boundaries land on the same snapped rectangle. This is the
        section that shows the quantization doing its job; the shared-DEM
        sections cannot, because they only ever have one window.
    TWI SCORING: TWO RETIRED CURVES vs THE LIVE ONE -- the retired
        parcel-relative percentile, the retired fixed 6.0/10.0 pair, and
        the live window-referenced curve over the same cells, including
        the per-cell |score change| between the two boundaries under
        each. Every column prints its MEASURED value; none is asserted.
    TWI INDEPENDENT-SIGNAL REPORT -- correlations of the TWI score
        against the criteria it may be re-voting, the clearing share
        with and without TWI's contribution, and every surviving seed's
        criteria signature. EVIDENCE ONLY: no weight is changed by this
        branch, deliberately (seeding is the blend's argmax, so a weight
        change would confound the before/after).
    BOUNDARY STABILITY -- which zones survive both boundaries, which
        appear under only one, the per-criterion blend delta on every
        matched pair, and the per-cell TWI score delta with each
        boundary's reference window printed beside it.

Run:  python diagnose_water_survey_areas.py   (networked -- fetches DEM,
production areas, canopy, roads, soil)

    --boundary PATH          JSON list of [lon, lat] pairs to run as the
                             PRIMARY boundary. DEFAULTS TO THE REFERENCE
                             BOUNDARY, so every pre-existing invocation
                             is unchanged.
    --compare-boundary PATH  the second boundary of the stability check
                             (defaults to the stream-corridor boundary
                             that lost a zone under the retired
                             parcel-relative TWI)
    --single-boundary        primary only; the two-boundary sections say
                             they could not run rather than falling silent
"""

import argparse
import json
import math

import numpy as np
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

import contourpy

from dem_data import dem_window_bounds, get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection, validate_feature_collection
from production_area_ceiling import identify_optimized_production_areas
from raster_grid import cell_area_acres, connected_components, pixel_center_xy
from rasterio.warp import transform_geom
from shapely.geometry import mapping
from water_survey_areas import (
    DEPRESSION_FULL_CREDIT_METERS,
    DEPRESSION_NOISE_FLOOR_METERS,
    DUPLICATE_OF_ZONE_REASON_PREFIX,
    EMBANKMENT_COMPARTMENT_RANK_WEIGHTS,
    EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES,
    EMBANKMENT_DRAINAGE_MIN_ACRES,
    EMBANKMENT_SEED_MIN_SCORE,
    EMBANKMENT_WEIGHTS,
    EXCAVATED_WEIGHTS,
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    MIN_SURVEY_REGION_AREA_ACRES,
    REASON_CATCHMENT_EXCEEDS_CEILING,
    # RETIRED from every scoring path (see the constants' own note).
    # Imported HERE, and only here, so the before/after can score the
    # same cells on the fixed curve the window-referenced one replaced.
    RETIRED_FIXED_TWI_FULL_CREDIT_BREAKPOINT,
    RETIRED_FIXED_TWI_MIN_BREAKPOINT,
    SEED_STATUS_COMPARTMENT,
    SURVEY_TYPE_EMBANKMENT,
    SURVEY_TYPE_EXCAVATED,
    SURVEY_TYPES,
    TWI_REFERENCE_WINDOW_SNAP_METERS,
    TWI_REPORTED_WINDOW_PERCENTILES,
    TWI_WINDOW_FLOOR_PERCENTILE,
    TWI_WINDOW_FULL_CREDIT_PERCENTILE,
    WATER_REGION_CONNECTIVITY,
    WETNESS_TWI_SUBWEIGHT,
    ZONE_STATUS_DROPPED,
    depression_score,
    identify_water_survey_areas,
    # RETIRED from the scoring path (water_survey_areas.
    # parcel_relative_percentile()'s own docstring says why). Imported
    # HERE, and only here, so this branch's before/after comparison can
    # reproduce the OLD parcel-relative scores beside the new absolute
    # ones -- the effect of the change is measured, not asserted. It must
    # never travel back into a scoring path.
    parcel_relative_percentile,
    select_embankment_seeds,
    survey_areas_to_geojson,
    twi_score,
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

# Lower edges of the SEED LADDER's summary bands (see
# summarize_seed_ladder()). The top band is open-ended. These bracket
# EMBANKMENT_SEED_MIN_SCORE's move from 0.50 to 0.30 so the run says
# directly what the newly-admitted range produced: a band below the
# current minimum reads zero by construction, which is the honest way
# for this table to show a raised floor rather than to hide it.
# CONFIGURABLE.
SEED_LADDER_BANDS = (0.30, 0.35, 0.40, 0.45, 0.50)

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
    features = list(
        survey_areas_to_geojson(
            identify_result["zones"], dropped_zones=identify_result.get("dropped_zones")
        )["features"]
    )
    # The embankment compartment instrument layers (seed / pinch /
    # baseline / transect, plus failed seeds with their reason codes --
    # the dropped-feature pattern, seed edition). Surviving AND dropped
    # compartments both get their instruments: a dropped compartment's
    # geometry is exactly what a tuning pass needs to see.
    from wire_translation import water_embankment_detail_features

    features.extend(
        water_embankment_detail_features(
            list(identify_result["zones"]) + list(identify_result.get("dropped_zones", [])),
            identify_result.get("embankment_seeds", []),
        )
    )
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
    """One line per SURVIVING survey zone, per type (ALL survivors --
    the presentation cap is deleted) -- rank, member count, DUAL acreage
    (zone acres to survey, anchored by member acres), member-cell
    mean/max suitability, top two contributing criteria, gravity note,
    envelope overlaps, cross-type agreement, boundary adjacency, flags
    -- followed by the DROPPED zones, each with its reason code and both
    acreages (visible and attributed, never silent). An embankment line
    carries THREE separate claims and never a composite alone: the
    seed's blend (good storage ground?), the pinch cell's catchment and
    drainage score (water above it?), and the rank score the two
    combine into."""
    lines = []
    for survey_type in SURVEY_TYPES:
        zones = identify_result["zones_by_type"][survey_type]
        lines.append(f"=== {survey_type.upper()}-TYPE SURVEY ZONES ({len(zones)} surviving) ===")
        if not zones:
            lines.append(
                "  (no seed produced a surviving compartment)"
                if survey_type == SURVEY_TYPE_EMBANKMENT
                else "  (none cleared the threshold and floor)"
            )
            continue
        for zone in zones:
            top_two = sorted(
                zone["criterion_contributions"].items(),
                key=lambda item: -item[1]["weighted_contribution"],
            )[:2]
            criteria_text = "+".join(f"{name}({entry['mean_score']})" for name, entry in top_two)
            flags = f" flags={','.join(zone['flags'])}" if zone["flags"] else ""
            cross = "".join(
                f", either-type w/ zone {entry['zone_id']} ({entry['fraction']:.0%})"
                for entry in zone.get("cross_type_overlaps", [])
            )
            if survey_type == SURVEY_TYPE_EMBANKMENT:
                # A compartment's line carries the honesty split: the
                # SEED's blend (the rank driver) beside the
                # compartment's own mean over the walked ground -- and
                # DUAL ACREAGE, the drawn hull beside the watershed
                # band anchoring it, so a resurrection-by-hull is
                # readable on this table rather than by eye on the map.
                lines.append(
                    f"  #{zone['rank']} zone {zone['id']}: {zone['zone_acres']:.2f} ac to survey "
                    f"anchored by {zone['compartment_footprint_acres']:.2f} ac compartment, "
                    f"seed blend {zone['seed_blend_score']:.3f} / compartment mean "
                    f"{zone['mean_suitability']:.3f}, pinch {zone['pinch']['width_m']:.0f} m wide at "
                    f"{zone['pinch']['walk_distance_m']:.0f} m draining "
                    f"{zone['pinch_catchment_acres']:.2f} ac "
                    f"(drainage {zone['pinch_drainage_score']:.3f}), "
                    f"rank score {zone['compartment_rank_score']:.3f}, "
                    f"top: {criteria_text}, {_gravity_cell(zone)}, "
                    f"canopy {_overlap_cell(zone['canopy_overlap_pct'])} / road-clip "
                    f"{_overlap_cell(zone['road_overlap_pct'])} / prod "
                    f"{_overlap_cell(zone['production_overlap_pct'])}, "
                    f"boundary-adj {zone['boundary_adjacency_fraction']:.0%}, "
                    f"conf {zone['confidence']}{cross}{flags}"
                )
            else:
                lines.append(
                    f"  #{zone['rank']} zone {zone['id']}: {zone['zone_acres']:.2f} ac to survey "
                    f"anchored by {zone['member_acres']:.2f} ac ({zone['member_count']} member(s)), "
                    f"mean {zone['mean_suitability']:.3f} / max {zone['max_suitability']:.3f}, "
                    f"top: {criteria_text}, {_gravity_cell(zone)}, "
                    f"canopy {_overlap_cell(zone['canopy_overlap_pct'])} / road-clip "
                    f"{_overlap_cell(zone['road_overlap_pct'])} / prod "
                    f"{_overlap_cell(zone['production_overlap_pct'])}, "
                    f"boundary-adj {zone['boundary_adjacency_fraction']:.0%}, "
                    f"conf {zone['confidence']}{cross}{flags}"
                )
    dropped = identify_result.get("dropped_zones", [])
    lines.append(
        f"=== DROPPED ZONES ({len(dropped)}: the catchment ceiling, dedupe and the acreage floor, "
        "each with its reason) ==="
    )
    if not dropped:
        lines.append("  (none)")
    for zone in dropped:
        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
            lines.append(
                f"  DROPPED ({zone['drop_reason']}): embankment zone {zone['id']}, hull "
                f"{zone['zone_acres']:.4f} ac anchored by "
                f"{zone['compartment_footprint_acres']:.4f} ac compartment, "
                f"seed blend {zone['seed_blend_score']:.3f}, "
                f"{zone['pinch_catchment_acres']:.2f} ac at the pinch "
                f"(drainage {zone['pinch_drainage_score']:.3f}) -- "
                "excluded from the pipeline output"
            )
        else:
            lines.append(
                f"  DROPPED ({zone['drop_reason']}): {zone['survey_type']} zone {zone['id']}, envelope "
                f"{zone['zone_acres']:.4f} ac anchored by {zone['member_acres']:.4f} ac, "
                f"mean {zone['mean_suitability']:.3f} -- excluded from the pipeline output"
            )
    # THE SEED ACCOUNTING, one line. The per-failure list that used to
    # sit here is RETIRED INTO summarize_seed_ladder(), which carries
    # every one of these records with strictly more on it (criteria
    # signature, and the acreages and floor verdict for the ones that
    # built something). Printing both would print every failure twice --
    # and at the 0.30 seeding minimum that is dozens of duplicated lines
    # on a real parcel. The counts stay here because they are context for
    # the zone table above them; the detail lives in one place.
    seeds = identify_result.get("embankment_seeds", [])
    failed = [record for record in seeds if record.get("status") == "failed"]
    lines.append(
        f"=== EMBANKMENT SEEDS ({len(seeds)} seeded, uncapped; {len(failed)} produced nothing) "
        "-- per-seed detail in the SEED LADDER below ==="
    )
    if not seeds:
        lines.append(f"  (no gated cell reached {EMBANKMENT_SEED_MIN_SCORE})")
    return "\n".join(lines)


# The band breakdown key a compartment refused by the catchment ceiling
# lands under -- _seed_outcome() composes it as f"dropped:{reason}", and
# the banded summary promotes it to its own column. Named here rather
# than re-typed at the two sites that must agree.
_OVER_CEILING_BREAKDOWN_KEY = f"dropped:{REASON_CATCHMENT_EXCEEDS_CEILING}"


def _collapse_reason(reason) -> str:
    """A reason code as its OUTCOME CLASS: every duplicate_of_zone_<id>
    collapses to duplicate_of_zone. See _seed_outcome()."""
    if not reason:
        return "unknown"
    if reason.startswith(DUPLICATE_OF_ZONE_REASON_PREFIX):
        return DUPLICATE_OF_ZONE_REASON_PREFIX.rstrip("_")
    return reason


def _seed_outcome(record: dict, zone_by_id: dict) -> tuple:
    """One seed's outcome as (bucket, detail, key): `bucket` is the
    coarse class the banded summary counts, `detail` is the ladder
    line's own text, and `key` is how the band breakdown names it.

    THE KEY IS DELIBERATELY COARSER THAN THE DETAIL. A dedupe reason
    names its winning zone (duplicate_of_zone_7), which is exactly right
    on a per-seed line and exactly wrong in a summary, where it would
    split one outcome class across as many keys as there are winners.
    The line keeps the id; the band counts the class.

    THE OUTCOME OF A SEED IS NOT ITS STATUS. A seed whose status is
    'compartment' may still have had that compartment dropped afterwards
    -- by the acre floor, or by compartment-overlap dedupe, neither of
    which touches the seed record. So the compartment's own zone is
    looked up and its status read: 'survived' means a compartment
    exists AND cleared everything downstream, which is the only outcome
    that put ground in front of the user."""
    if record.get("status") != SEED_STATUS_COMPARTMENT:
        reason = record.get("reason_code") or "unknown"
        terminator = record.get("terminator")
        stations = record.get("stations_measured")
        detail = reason
        if terminator is not None:
            detail += f", terminator={terminator}"
        if stations is not None:
            detail += f", {stations} station(s)"
        return ("failed", detail, _collapse_reason(reason))

    zone = zone_by_id.get(record.get("zone_id"))
    if zone is None:
        return ("compartment", "compartment built (zone not found in this result)", "compartment")
    # THE FILL CLAIM RIDES EVERY BUILT SEED'S LINE. The ladder's whole
    # job is to make a seeding decision falsifiable, and since the
    # drainage band moved to the pinch, "what did this seed produce" is
    # only half answerable without "and what water sits above it".
    # Printed for dropped compartments too -- a compartment refused by
    # the catchment ceiling is exactly the row this column exists for.
    acreage = (
        f"band {zone['compartment_footprint_acres']:.4f} ac / hull {zone['zone_acres']:.4f} ac"
        f"; pinch catchment {zone['pinch_catchment_acres']:.2f} ac"
        f" -> drainage {zone['pinch_drainage_score']:.3f}"
        f"; rank score {zone['compartment_rank_score']:.4f}"
    )
    if zone.get("status") == ZONE_STATUS_DROPPED:
        drop_reason = zone.get("drop_reason")
        return (
            "dropped",
            f"{acreage}; DROPPED ({drop_reason})",
            f"dropped:{_collapse_reason(drop_reason)}",
        )
    return (
        "survived",
        f"{acreage}; SURVIVED the {MIN_SURVEY_REGION_AREA_ACRES} ac floor",
        "survived",
    )


def summarize_seed_ladder(identify_result: dict) -> str:
    """
    THE INSTRUMENT THAT EARNS EMBANKMENT_SEED_MIN_SCORE's VALUE: every
    seed this run nominated, in blend-descending order, with what became
    of it -- then the same seeds bucketed by blend band.

    WHY IT EXISTS. The seeding minimum dropped from 0.50 to 0.30 on the
    argument that several real gates sit downstream of nomination and
    should be allowed to do the filtering (see the constant's own note).
    That argument is a PREDICTION, and this table is what tests it. Read
    the banded summary bottom-up:

      * a low band seeding many and surviving NONE -- nothing but
        no_constriction and floor drops -- is the evidence for raising
        the minimum permanently, and says where to raise it TO.
      * a low band producing even one real compartment is the evidence
        the change was right, and that ground would not have been
        nominated at 0.50 at all.

    Neither reading is available from the zone list alone, because a
    zone that was never nominated leaves no trace anywhere else.

    THE COST LINE is printed with it: seeds nominated and pinch walks
    run. Every seed is walked -- none is pre-pruned, per the standing
    no-cap rule -- so the two numbers are equal BY CONSTRUCTION and the
    cost of a lower minimum is exactly linear in the seeds it admits.
    Printing both states that rather than leaving it to be inferred.

    WHAT THIS RUN'S LADDER IS ACTUALLY ASKING. The blend scored here is
    a NEW BLEND: drainage area left the seeding criteria and slope /
    soil / TWI were renormalized to 0.36 / 0.36 / 0.28, because on the
    reference property the drainage criterion was ANTI-CORRELATED with
    producing a survey area -- every high-drainage seed died at
    no_constriction or the floor, and every surviving zone carried
    drainage 0.000. The diagnosis was that drainage was being measured
    AT THE SEED, which under the compartment construction is the storage
    anchor, while what fills a pond is the catchment ABOVE the
    compartment, delivered through the dam reach. So the band moved to
    the PINCH CELL rather than being deleted.

    THE QUESTION THE NEXT RUN ANSWERS, and the reason each built seed's
    line now carries its pinch catchment and drainage score:

        DO THE HIGH-CATCHMENT REACHES THAT PREVIOUSLY DIED AT
        no_constriction NOW APPEAR AS WELL-FILLED COMPARTMENTS ANCHORED
        ON OFF-CHANNEL SEEDS ABOVE THEM? That is: does measuring
        drainage at the pinch RECOVER the catchment signal the
        seed-level measurement was throwing away?

    Read it as two columns of the same table. If off-channel seeds that
    survive now show real pinch catchment, the answer is yes and the
    criterion was asking the right question of the wrong cell, exactly
    as diagnosed. IF THE SURVIVING COMPARTMENTS CARRY NEGLIGIBLE PINCH
    CATCHMENT, THE ANSWER IS NO AND THE FINDING IS ABOUT THE PARCEL, NOT
    THE INSTRUMENT: this ground's walkable storage sites have no water
    above them, which belongs in the narrative as a stated finding
    rather than being scored around.

    ONE CAVEAT TRAVELS WITH EVERY NUMBER HERE: these blend scores are
    NOT comparable to the previous run's. They are the three-criterion
    blend, so every score rises relative to the four-criterion one
    (nothing is dividing credit with a criterion that read 0.000 on most
    of the parcel) and every band edge has moved underneath a constant
    that deliberately did not. See EMBANKMENT_SEED_MIN_SCORE's note.
    """
    result = identify_result["result"]
    seeds = result.get("embankment_seeds", [])
    zone_by_id = {
        zone["id"]: zone for zone in result["zones"] + result["dropped_zones"]
    }

    lines = [
        f"=== EMBANKMENT SEED LADDER (minimum {EMBANKMENT_SEED_MIN_SCORE}, uncapped) ===",
        f"  {len(seeds)} seed(s) nominated; {len(seeds)} pinch walk(s) run "
        "(one per seed -- nothing is pre-pruned, so the cost is linear in the seeds admitted)",
    ]
    if not seeds:
        lines.append(f"  (no gated cell reached {EMBANKMENT_SEED_MIN_SCORE})")
        return "\n".join(lines)

    criterion_names = list(EMBANKMENT_WEIGHTS)
    ordered = sorted(seeds, key=lambda record: record["blend_score"], reverse=True)
    lines.append(
        "   #   blend  cell        "
        + "".join(f"{name:>15}" for name in criterion_names)
        + "   outcome"
    )
    outcomes = []
    for rank, record in enumerate(ordered, start=1):
        bucket, detail, key = _seed_outcome(record, zone_by_id)
        outcomes.append((record["blend_score"], bucket, key))
        signature = record["criteria_signature"]
        row, col = record["rowcol"]
        lines.append(
            f"  {rank:>3}  {record['blend_score']:.3f}  ({row:>3},{col:>3})  "
            + "".join(f"{signature[name]:>15.3f}" for name in criterion_names)
            + f"   {detail}"
        )

    # THE BANDED SUMMARY -- the table the next tuning decision is made
    # from. Bands are half-open [low, high); the top one is open-ended.
    lines.append(
        "  BANDS (seeded / built a compartment / refused by the catchment ceiling / "
        "survived the floor):"
    )
    edges = list(SEED_LADDER_BANDS)
    for index, low in enumerate(edges):
        high = edges[index + 1] if index + 1 < len(edges) else None
        in_band = [
            entry for entry in outcomes
            if entry[0] >= low and (high is None or entry[0] < high)
        ]
        label = f"{low:.2f}-{high:.2f}" if high is not None else f"{low:.2f}+"
        if not in_band:
            note = (
                "  (below the current minimum)"
                if high is not None and high <= EMBANKMENT_SEED_MIN_SCORE
                else ""
            )
            lines.append(f"    {label}: 0 seeded{note}")
            continue
        built = sum(1 for entry in in_band if entry[1] in ("survived", "dropped", "compartment"))
        survived = sum(1 for entry in in_band if entry[1] == "survived")
        breakdown: dict = {}
        for _score, _bucket, key in in_band:
            breakdown[key] = breakdown.get(key, 0) + 1
        # ITS OWN COLUMN, not just a breakdown key: a compartment
        # refused because its dam reach drains more than farm-pond scale
        # is a DIFFERENT finding from one that never formed and from one
        # that formed too small, and this is the branch that made that
        # outcome possible. A column that reads 0 all the way down is
        # itself the answer ("the ceiling never bound on this parcel");
        # buried in a breakdown string it would be neither.
        over_ceiling = breakdown.get(_OVER_CEILING_BREAKDOWN_KEY, 0)
        lines.append(
            f"    {label}: {len(in_band)} seeded / {built} built / "
            f"{over_ceiling} over-ceiling / {survived} survived   "
            + ", ".join(f"{key}={count}" for key, count in sorted(breakdown.items()))
        )
    lines.append(
        "  READ IT BOTTOM-UP: a low band seeding many and surviving none is the evidence for "
        "raising EMBANKMENT_SEED_MIN_SCORE (and says where to); one real compartment down there is "
        "the evidence the lower minimum was right. THESE SCORES ARE NOT COMPARABLE TO THE PREVIOUS "
        f"RUN'S: drainage area left the seeding blend and the remaining three renormalized to "
        + ", ".join(f"{name} {weight}" for name, weight in EMBANKMENT_WEIGHTS.items())
        + " -- band edges moved underneath a minimum that deliberately did not."
    )
    # THE QUESTION THIS RUN EXISTS TO ANSWER, printed with the table so
    # it is asked of the numbers rather than remembered afterwards.
    survived_catchments = [
        zone["pinch_catchment_acres"]
        for zone in result["zones"]
        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
    ]
    lines.append(
        "  THE QUESTION THIS BRANCH ASKS: do the high-catchment reaches that previously died at "
        "no_constriction now appear as WELL-FILLED compartments anchored on off-channel seeds "
        "ABOVE them -- i.e. does measuring drainage at the PINCH recover the catchment signal the "
        f"seed-level measurement was throwing away? The band is {EMBANKMENT_DRAINAGE_MIN_ACRES}-"
        f"{EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES} ac ramp to full credit, hard zero above "
        f"{MAX_VALLEY_CONTRIBUTING_AREA_ACRES} ac; the pinch-catchment column above is the answer."
    )
    if survived_catchments:
        lines.append(
            "    surviving embankment compartments' pinch catchments (ac): "
            + ", ".join(f"{acres:.2f}" for acres in sorted(survived_catchments, reverse=True))
            + " -- IF THESE ARE NEGLIGIBLE the honest finding is about this PARCEL, not the "
            "instrument: its walkable storage ground has no water above it, and that belongs in "
            "the narrative rather than being scored around."
        )
    else:
        lines.append(
            "    (no surviving embankment compartment on this run -- the question has no answer "
            "from this boundary)"
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
    THE THRESHOLD RE-VERIFICATION, EXCAVATED ONLY since the compartment
    change: member-region count, total acreage, and largest-region
    acreage at each THRESHOLD_COMPARISON_LEVELS value, on the RAW
    excavated surface (the one extraction actually thresholds --
    pre-threshold smoothing is retired), 8-connected -- so the 0.5
    default stays a choice made from evidence, re-decided every run.

    THE EMBANKMENT LINES ARE RETIRED WITH EXTRACTION, and the instrument
    says so every run rather than falling silent: the embankment surface
    no longer thresholds into anything -- it is a NOMINATION surface for
    seed-based valley compartments, and the number this instrument
    existed to tune (the embankment extraction threshold) no longer
    exists on that path. What replaced it -- EMBANKMENT_SEED_MIN_SCORE,
    the same 0.5 with a recorded semantic shift -- is tuned from the
    seed/failure accounting in the zones table and the seed layers of
    the export, not from component counts at hypothetical thresholds.
    """
    result = identify_result["result"]
    gate_mask = result["gate_mask"]
    area_per_cell = cell_area_acres(dem)
    lines = ["=== THRESHOLD COMPARISON (raw excavated surface, 8-connected) ==="]
    surface = result["surfaces"][SURVEY_TYPE_EXCAVATED]
    lines.append(f"  {SURVEY_TYPE_EXCAVATED}:")
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
    lines.append(
        "  embankment: RETIRED with extraction -- the embankment surface nominates seeds now "
        "(seed-based valley compartments); the threshold this instrument tuned no longer exists "
        "on that path. See the EMBANKMENT SEEDS section of the zones table instead."
    )
    return "\n".join(lines)


def summarize_depression_instrumentation(identify_result: dict, dem: dict) -> str:
    """
    The excavated-class interrogation, part 1: the depression-depth
    distribution over GATED cells BEFORE and AFTER the noise floor, plus
    the 10 deepest-fill cells' full scoring row (raw depth, floored
    depth, TWI score, wetness criterion, slope score, soil score --
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
            twi = result["screens"]["twi_score"][r, c]
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
    The stated finding, with a verdict CONDITIONAL on what was measured.
    From the instrumentation's own numbers, the largest weighted
    shortfall among the four suspects --
      1. the 0.1 m noise floor (real basins zeroed before scoring),
      2. depth-to-score scaling (real floored depth scoring too little),
      3. the slope classes (moderate ground scored as too steep),
      4. the soil sub-weight ceiling arithmetic-limiting the blend.
    Computed as each criterion's mean weighted SHORTFALL from a perfect
    score at the 10 deepest-fill cells (the marsh proxy: the ground the
    class exists to find), with the wetness shortfall split into its
    TWI/depression halves and the depression half split floor-vs-scaling
    by comparing raw and floored depth.

    The verdict wording branches on excavated survivors. There is
    ALWAYS a largest shortfall by construction -- some criterion tops
    the sorted table on every parcel, producing zones or not -- so
    "EVIDENCE INDICTS" is honest only when the excavated type actually
    failed to produce (zero surviving zones): that failure is the charge
    the shortfall answers. When survivors exist, the same number is
    headroom context on a working class, and an accusatory verdict there
    invites reactive tuning of a system that just delivered; the finding
    prints as "LARGEST REMAINING SHORTFALL" with an explicit line saying
    it is not a defect claim. Same numbers, same table, same ranking --
    only the claim changes to match what was measured.
    """
    excavated_survivors = identify_result["zones_by_type"][SURVEY_TYPE_EXCAVATED]
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
        twi_vals = [screens["twi_score"][r, c] for r, c in deepest]
        mean_twi = float(np.nanmean(twi_vals)) if twi_vals else float("nan")
        lines.append(
            f"  wetness split: mean TWI score {mean_twi:.3f}; mean depression score {mean_dep_score:.3f}; "
            f"{zeroed_by_floor}/10 deepest cells had real fill zeroed by the {DEPRESSION_NOISE_FLOOR_METERS} m floor"
        )
        if zeroed_by_floor >= 5:
            verdict = "the NOISE FLOOR (suspect 1): most of the deepest real fill never reaches the scorer"
        elif mean_dep_score < 0.5 and float(np.mean(floored_vals)) > 0:
            verdict = "DEPTH-TO-SCORE SCALING (suspect 2): real floored depth survives but scores too little"
        else:
            verdict = "the TWI half of wetness: even the deepest fill's neighborhood reads too dry on the absolute curve"
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
    if not excavated_survivors:
        # The type failed to produce: the shortfall answers a real
        # charge, and the accusatory wording is earned.
        lines.append(f"  EVIDENCE INDICTS: {verdict}.")
    else:
        # The type produced. The same largest shortfall exists by
        # construction (some criterion always tops the table), so it
        # prints as headroom, not an accusation.
        lines.append(f"  LARGEST REMAINING SHORTFALL: {verdict}.")
        lines.append(
            f"  The excavated type produced {len(excavated_survivors)} surviving zone(s); this shortfall is "
            "headroom context on a working class, not a defect claim."
        )
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



# ==========================================================================
# THE BOUNDARY-STABILITY INSTRUMENT
# ==========================================================================
# The standing regression against ONE bug class: a criterion that scores
# a cell relative to the parcel makes the whole composite move when the
# USER redraws the boundary, and the failure looks like terrain analysis
# rather than like a bug. It was found by accident once (an embankment
# survey zone existed under one boundary and not under a slightly larger
# one over the same land); this section exists so it cannot be found by
# accident again.
#
# WHAT IS AND IS NOT ALLOWED TO MOVE. Boundary-dependence is not itself
# an error -- the gate mask IS the boundary, geometry clips at it, and
# the embankment pinch walk terminates on it. Those are the boundary
# doing its job. What must NOT move is a CELL'S SCORE: a criterion is a
# claim about ground, and ground does not change when a line is redrawn
# around it. So the report below reads per-criterion deltas on MATCHED
# zones: a delta in `gated cells` is expected, a nonzero delta on a
# criterion mean for the same ground is the bug returning.

# The reference boundary this diagnostic has always run (unchanged).
REFERENCE_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

# THE BOUNDARY THAT LOST THE ZONE: the same property drawn slightly
# larger, reaching further into the stream corridor. Under the retired
# parcel-relative TWI this boundary produced NO embankment survey zone
# where the reference boundary produced one -- the added corridor cells
# were the wettest on the landscape, took the top percentile ranks, and
# pushed every other cell's TWI rank down far enough (0.20 of the
# embankment blend) to drop a ~0.52 seed under the then-0.50 seeding
# minimum. Kept
# here as the second boundary of the standing two-boundary run.
STREAM_CORRIDOR_BOUNDARY = [
    (-79.98395562171937, 40.6460162710763),
    (-79.98374104499818, 40.642584987588364),
    (-79.98047947883607, 40.64432504438868),
    (-79.98097300529480, 40.645089354064524),
    (-79.98150944709779, 40.645170663089445),
    (-79.98266816139223, 40.64596748629134),
]

# How much two zones of the same type must overlap, as
# intersection-over-union of their envelopes, to be called THE SAME ZONE
# across two boundary runs. A compartment clipped by a larger boundary
# genuinely grows, so the match cannot demand near-identity; 0.3 is loose
# enough to survive a real clip difference and tight enough that two
# distinct sites never pair. CONFIGURABLE.
ZONE_MATCH_MIN_IOU = 0.3


def load_boundary(path: str) -> list:
    """A boundary from a JSON file: a list of [lon, lat] pairs, returned
    as the list of tuples every entry point here takes. Ring closure is
    the caller's business exactly as it is for the hardcoded boundaries
    (shapely closes an open ring itself)."""
    with open(path) as handle:
        raw = json.load(handle)
    return [(float(lon), float(lat)) for lon, lat in raw]


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson r, or NaN when either input has no variance (a constant
    criterion has no correlation to report, and saying 0.0 would be a
    fabricated answer)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if x.size < 2 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    """Spearman rho = Pearson on MEAN RANKS (ties averaged, which
    matters here: classed criteria plateau at 0.0 and 1.0 over large
    cell populations, so ties are the common case, not the edge one)."""
    def ranks(values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=np.float64)
        order = np.argsort(values, kind="mergesort")
        sorted_values = values[order]
        result = np.empty(values.size, dtype=np.float64)
        i = 0
        while i < values.size:
            j = i
            while j + 1 < values.size and sorted_values[j + 1] == sorted_values[i]:
                j += 1
            result[order[i:j + 1]] = 0.5 * (i + j)
            i = j + 1
        return result

    return _pearson(ranks(x), ranks(y))


def summarize_twi_calibration(runs: list) -> str:
    """
    THE CURVE, READ OFF THE RUN: the reference window each boundary
    scored against, the raw-TWI distribution over that window, and the
    two breakpoints derived from it -- beside the distribution over
    GATED cells, which is what the curve is NOT referenced to.

    WHY BOTH DISTRIBUTIONS, AND WHY THAT IS THE POINT. The breakpoints
    are percentiles of the WINDOW. Printing the gated distribution
    alongside is the check that the two populations genuinely differ:
    if the gated cells are drier than the window (they usually are -- a
    parcel is a subset of its landscape and the ceiling gate removes the
    wettest, highest-catchment ground), then a curve read off the GATE
    would sit lower and every parcel would flatter itself. The gap
    between the two columns is the size of the mistake not being made.

    WHAT THIS SECTION REPLACED. It used to be the calibration instrument
    for two HARDCODED breakpoints, printing the gated distribution under
    both boundaries so a human could read constants off where the two
    agreed. There are no constants to read off any more: the percentiles
    ARE the calibration and the run derives the values itself. The
    percentile CHOICES (TWI_WINDOW_FLOOR_PERCENTILE /
    TWI_WINDOW_FULL_CREDIT_PERCENTILE) are still v1 priors, and the
    distribution printed here is still what they get tuned against --
    but tuning them now moves a curve on every property at once instead
    of fitting one.
    """
    percentiles = TWI_REPORTED_WINDOW_PERCENTILES
    lines = [
        "=== TWI CALIBRATION: THE WINDOW-REFERENCED CURVE, DERIVED PER RUN ===",
        f"  curve rule: 0.0 at the window's p{TWI_WINDOW_FLOOR_PERCENTILE:g} raw TWI, linear ramp, "
        f"1.0 at its p{TWI_WINDOW_FULL_CREDIT_PERCENTILE:g}"
        f"  (retired fixed pair, for scale: {RETIRED_FIXED_TWI_MIN_BREAKPOINT} / "
        f"{RETIRED_FIXED_TWI_FULL_CREDIT_BREAKPOINT})",
    ]
    for label, identify_result, _boundary in runs:
        result = identify_result["result"]
        breakpoints = result["twi_breakpoints"]
        reference = result["twi_reference_window"]
        raw_grid = result["screens"]["twi_raw"]
        dem_res = identify_result.get("_dem_resolution_meters")

        min_x, min_y, max_x, max_y = reference["bounds"]
        lines.append(
            f"  {label}: reference window {max_x - min_x:.0f} x {max_y - min_y:.0f} m "
            f"({reference['cell_count']} cells, {breakpoints['measured_cell_count']} with measured "
            f"TWI), snapped to {reference['snap_meters']:.0f} m: {reference['snapped']}"
            + (f", DEM resolution {dem_res} m" if dem_res else "")
        )
        if reference["fallback_reason"]:
            lines.append(f"    SNAP DID NOT APPLY -- {reference['fallback_reason']}")
        if breakpoints["floor"] is None:
            lines.append("    (no measured TWI in the reference window -- no curve exists this run)")
            continue

        window_values = [breakpoints["percentiles"][f"p{q:g}"] for q in percentiles]
        lines.append("    pctile " + "".join(f"{q:>8g}" for q in percentiles))
        lines.append("    WINDOW " + "".join(f"{v:>8.2f}" for v in window_values))

        gated_raw = raw_grid[result["gate_mask"]]
        gated_raw = gated_raw[~np.isnan(gated_raw)]
        if gated_raw.size:
            gated_values = np.percentile(gated_raw, percentiles)
            lines.append("    gated  " + "".join(f"{v:>8.2f}" for v in gated_values))
            lines.append(
                f"    ({gated_raw.size} gated cells, against {breakpoints['measured_cell_count']} "
                "measured cells in the reference window -- the two populations OVERLAP but neither "
                "contains the other, since the boundary can reach outside the snapped rectangle. "
                "The gated row is CONTEXT ONLY; no breakpoint is read from it)"
            )
        else:
            lines.append("    gated  (no gated cells with measured TWI)")

        lines.append(
            f"    DERIVED CURVE: floor {breakpoints['floor']:.3f} (p"
            f"{breakpoints['floor_percentile']:g}), full credit {breakpoints['full_credit']:.3f} "
            f"(p{breakpoints['full_credit_percentile']:g}), ramp width "
            f"{breakpoints['full_credit'] - breakpoints['floor']:.3f}"
            + (f"   [FALLBACK CURVE: {breakpoints['curve_fallback']} -- the two percentiles tied "
               "on this window; see twi_window_breakpoints()]"
               if breakpoints["curve_fallback"] else "")
        )
        for population, values in (("window", None), ("gated", gated_raw)):
            source = raw_grid[result["twi_reference_window"]["mask"]] if population == "window" else values
            if source is None or source.size == 0:
                continue
            source = source[~np.isnan(source)]
            if source.size == 0:
                continue
            scored_now = twi_score(source, breakpoints["floor"], breakpoints["full_credit"])
            scored_fixed = twi_score(
                source, RETIRED_FIXED_TWI_MIN_BREAKPOINT, RETIRED_FIXED_TWI_FULL_CREDIT_BREAKPOINT
            )
            lines.append(
                f"    scored over {population:<6} cells -- window-referenced: mean "
                f"{float(np.mean(scored_now)):.3f}, {float(np.mean(scored_now == 0.0)) * 100:.1f}% "
                f"at 0.0, {float(np.mean(scored_now == 1.0)) * 100:.1f}% at 1.0   |   retired "
                f"fixed curve: mean {float(np.mean(scored_fixed)):.3f}, "
                f"{float(np.mean(scored_fixed == 0.0)) * 100:.1f}% at 0.0, "
                f"{float(np.mean(scored_fixed == 1.0)) * 100:.1f}% at 1.0"
            )
    lines.append(
        "  THE FLOORING NUMBER TO WATCH is the '% at 0.0' pair on the GATED row. RECORDED FOR "
        "COMPARISON, from the reference property under the retired fixed pair: ~66% of its gated "
        "cells scored exactly 0.0 (median raw TWI 5.44 against a floor of 6.0), which is what made "
        "TWI a constant subtraction there rather than a criterion. Whatever this run prints is "
        "THIS property's number, not that one."
    )
    return "\n".join(lines)


def summarize_reference_window_snap(runs: list) -> str:
    """
    THE QUANTIZATION, MEASURED: what window each boundary WOULD fetch on
    its own, what that window snaps to, and whether the two boundaries
    land on the same snapped rectangle.

    WHY IT IS COMPUTED AND NOT FETCHED. The stability run deliberately
    shares ONE DEM over the union of both boundaries (see
    summarize_boundary_stability()), so both runs necessarily reference
    the identical window and the per-cell delta below is exactly 0. That
    is the right instrument for "does the boundary move a score", but it
    cannot show the snap doing its job, because there is only one window
    in play. So this section derives each boundary's OWN window
    arithmetically -- dem_data.dem_window_bounds() is the same code path
    get_dem_for_boundary() uses to size its request, with the request
    removed -- and reports whether a real re-fetch under each boundary
    would have produced the same reference rectangle.

    HOW TO READ IT. `same snapped window: True` means a production run
    under either boundary scores every shared cell identically -- the
    snap held, and the quantization did what it exists for. `False`
    means the edit crossed a snap line: the two runs reference different
    populations and shared cells will differ by a small amount. Both
    outcomes are informative; neither is asserted away.
    """
    lines = ["=== REFERENCE WINDOW SNAP: WHAT EACH BOUNDARY WOULD FETCH ON ITS OWN ==="]
    lines.append(
        f"  snap grid: {TWI_REFERENCE_WINDOW_SNAP_METERS:.0f} m in the DEM's CRS, applied INWARD to "
        "the fetched extent (the fetch itself is NOT snapped -- see twi_reference_window())"
    )
    snapped_bounds = {}
    for label, identify_result, boundary in runs:
        if boundary is None:
            # A synthetic run supplies its raster directly and has no
            # lon/lat boundary to size a fetch from. Report the window
            # the run ACTUALLY referenced instead of inventing one, and
            # say which of the two this line is.
            reference = tuple(identify_result["result"]["twi_reference_window"]["bounds"])
            snapped_bounds[label] = reference
            lines.append(
                f"  {label}: no lon/lat boundary in this run (a supplied raster) -- reporting the "
                "reference window the run actually used, not a derived would-be fetch"
            )
        else:
            window = dem_window_bounds(boundary)
            min_x, min_y, max_x, max_y = window["bbox"]
            snap = TWI_REFERENCE_WINDOW_SNAP_METERS
            reference = (
                math.ceil(min_x / snap) * snap,
                math.ceil(min_y / snap) * snap,
                math.floor(max_x / snap) * snap,
                math.floor(max_y / snap) * snap,
            )
            snapped_bounds[label] = reference
            lines.append(
                f"  {label}: would fetch {window['size'][0]}x{window['size'][1]} cells, "
                f"x {min_x:.1f}..{max_x:.1f} ({max_x - min_x:.1f} m), "
                f"y {min_y:.1f}..{max_y:.1f} ({max_y - min_y:.1f} m)"
            )
        lines.append(
            f"    -> snapped reference window x {reference[0]:.0f}..{reference[2]:.0f} "
            f"({reference[2] - reference[0]:.0f} m), y {reference[1]:.0f}..{reference[3]:.0f} "
            f"({reference[3] - reference[1]:.0f} m)"
        )

    if len(snapped_bounds) >= 2:
        (label_a, bounds_a), (label_b, bounds_b) = list(snapped_bounds.items())[:2]
        same = bounds_a == bounds_b
        lines.append(
            f"  SAME SNAPPED WINDOW for '{label_a}' and '{label_b}': {same}"
            + (
                "   -- a real re-fetch under either boundary references the identical population, "
                "so every shared cell scores identically"
                if same
                else "   -- this edit crossed a snap line: a real re-fetch under each boundary "
                "references different populations, and shared cells move by a small amount. The "
                "snap bounds how far apart two windows can be, it does not abolish the difference."
            )
        )
    else:
        lines.append("  ONLY ONE BOUNDARY IN THIS RUN -- the snap comparison needs two.")
    return "\n".join(lines)


def summarize_twi_scoring_comparison(runs: list) -> str:
    """
    THE THREE CURVES, ON THE SAME CELLS: the RETIRED parcel-relative
    percentile, the RETIRED fixed 6.0/10.0 pair, and the LIVE
    window-referenced curve -- so two branches' worth of change is
    measured rather than asserted.

    parcel_relative_percentile() is imported here and nowhere else on
    any scoring path (AST-asserted in test_water_survey_areas.py). The
    percentile population is rebuilt exactly as the retired code built
    it -- ON-PARCEL cells, not just gated ones, since the ceiling gate
    removed cells from play but not from the parcel.

    THE DECISIVE LINE is the cross-boundary one: the SAME cell's score
    under two boundaries, on each curve. IT PRINTS THE ACTUAL NUMBER.
    Under window referencing over one shared DEM the live column is
    exactly 0.0000 because the reference window is a property of that
    raster; if it ever is not, the number says so rather than an
    assertion hiding it. What a real re-fetch under each boundary would
    do is a different question, answered by the snap section above.
    """
    lines = ["=== TWI SCORING: TWO RETIRED CURVES vs THE LIVE WINDOW-REFERENCED ONE ==="]
    per_boundary = {}
    for label, identify_result, _boundary in runs:
        result = identify_result["result"]
        gate_mask = result["gate_mask"]
        screens = result["screens"]
        raw = screens["twi_raw"]
        on_parcel = identify_result["_on_parcel_mask"]
        old_scores = parcel_relative_percentile(raw, on_parcel)
        fixed_scores = twi_score(
            raw, RETIRED_FIXED_TWI_MIN_BREAKPOINT, RETIRED_FIXED_TWI_FULL_CREDIT_BREAKPOINT
        )
        new_scores = screens["twi_score"]
        per_boundary[label] = (old_scores, fixed_scores, new_scores, gate_mask)

        gated = gate_mask & ~np.isnan(raw)
        if not np.any(gated):
            lines.append(f"  {label}: (no gated cells)")
            continue
        breakpoints = result["twi_breakpoints"]
        lines.append(
            f"  {label}: over {int(np.count_nonzero(gated))} gated cells -- retired percentile mean "
            f"{float(np.mean(old_scores[gated])):.3f}, retired fixed-curve mean "
            f"{float(np.mean(fixed_scores[gated])):.3f} "
            f"({float(np.mean(fixed_scores[gated] == 0.0)) * 100:.1f}% floored), "
            f"window-referenced mean {float(np.mean(new_scores[gated])):.3f} "
            f"({float(np.mean(new_scores[gated] == 0.0)) * 100:.1f}% floored) on "
            f"[{breakpoints['floor']:.3f}, {breakpoints['full_credit']:.3f}]"
        )

    if len(per_boundary) >= 2:
        (label_a, (old_a, fixed_a, new_a, mask_a)), (label_b, (old_b, fixed_b, new_b, mask_b)) = (
            list(per_boundary.items())[:2]
        )
        both = mask_a & mask_b & ~np.isnan(old_a) & ~np.isnan(old_b)
        if np.any(both):
            lines.append(
                f"  CELLS GATED UNDER BOTH ({int(np.count_nonzero(both))}) -- per-cell |score change| "
                f"between '{label_a}' and '{label_b}', THE MEASURED NUMBER IN EVERY COLUMN:"
            )
            for name, (a, b), note in (
                ("retired percentile", (old_a, old_b), "the bug: same ground, different score"),
                ("retired fixed curve", (fixed_a, fixed_b), "fixed breakpoints held it still, at the cost of flooring"),
                ("window-referenced", (new_a, new_b), "the live curve; one shared DEM means one reference window"),
            ):
                delta = np.abs(a[both] - b[both])
                lines.append(
                    f"    {name:<20} mean {float(np.mean(delta)):.4f}, max {float(np.max(delta)):.4f}"
                    f"  <- {note}"
                )
    return "\n".join(lines)


def _embankment_surface_without_twi(criteria: dict) -> np.ndarray:
    """The embankment blend with TWI REMOVED and its weight redistributed
    PROPORTIONALLY across the remaining seeding criteria -- slope and
    soil since the drainage band moved to the pinch cell (so the
    remaining weights still sum to 1.0 and their relative emphasis is
    untouched -- the only honest way to ask "what does the blend say
    without this criterion" without also changing what the others
    mean). Reads EMBANKMENT_WEIGHTS rather than naming the criteria, so
    it stays correct across changes to the blend's membership."""
    remaining = {name: weight for name, weight in EMBANKMENT_WEIGHTS.items() if name != "twi"}
    total = sum(remaining.values())
    surface = np.zeros(criteria["twi"].shape, dtype=np.float64)
    for name, weight in remaining.items():
        surface += (weight / total) * criteria[name]
    return surface


def _excavated_surface_without_twi(criteria: dict, depression_depth: np.ndarray) -> np.ndarray:
    """The excavated blend without TWI. TWI is not a top-level excavated
    criterion -- it is HALF of `wetness` (WETNESS_TWI_SUBWEIGHT) -- so
    the proportional redistribution happens at the SUB-BLEND: wetness
    becomes the depression score alone, its subweight renormalized to
    1.0, and the four top-level weights are untouched. Same rule as the
    embankment case, applied at the level TWI actually votes."""
    surface = np.zeros(criteria["wetness"].shape, dtype=np.float64)
    for name, weight in EXCAVATED_WEIGHTS.items():
        grid = depression_score(depression_depth) if name == "wetness" else criteria[name]
        surface += weight * grid
    return surface


def _surfaces_with_substituted_twi(criteria: dict, screens: dict, twi_scores: np.ndarray) -> dict:
    """Both blends rebuilt with a DIFFERENT TWI score grid substituted in
    and every other criterion held exactly as the run computed it -- the
    only honest way to ask "what would this run have looked like on that
    curve" without also re-deriving the criteria the question is not
    about. The excavated side substitutes into the WETNESS blend at
    WETNESS_TWI_SUBWEIGHT, mirroring compute_suitability_surfaces()'s own
    arithmetic, and applies the same NaN -> 0.0 flag-not-poison
    conversion."""
    twi = np.where(np.isnan(twi_scores), 0.0, twi_scores)
    embankment = np.zeros(twi.shape, dtype=np.float64)
    for name, weight in EMBANKMENT_WEIGHTS.items():
        embankment += weight * (twi if name == "twi" else criteria[SURVEY_TYPE_EMBANKMENT][name])

    depression = depression_score(screens["depression_depth"])
    wetness = WETNESS_TWI_SUBWEIGHT * twi + (1.0 - WETNESS_TWI_SUBWEIGHT) * depression
    excavated = np.zeros(twi.shape, dtype=np.float64)
    for name, weight in EXCAVATED_WEIGHTS.items():
        excavated += weight * (wetness if name == "wetness" else criteria[SURVEY_TYPE_EXCAVATED][name])
    return {SURVEY_TYPE_EMBANKMENT: embankment, SURVEY_TYPE_EXCAVATED: excavated}


def summarize_twi_independent_signal(identify_result: dict, label: str) -> str:
    """
    EVIDENCE FOR A LATER WEIGHT DECISION. NOTHING HERE CHANGES ANY
    WEIGHT, and this branch deliberately does not: seeding is the blend's
    argmax, so a weight change would move every seed and confound the
    before/after this branch exists to measure. The weight/removal
    decision is a later branch, where seeding is the thing being
    measured. This section is that branch's input.

    THE CHARGE. TWI is the one criterion in either blend with no external
    anchor -- drainage acres, slope percent, ksat and hydrologic group
    all carry published or physical breakpoints, while TWI's two
    breakpoints are calibration. It is also substantially THE RATIO OF
    TWO CRITERIA THAT ALREADY VOTE: ln(a/tan(beta)) is water arriving
    over gentleness, and both halves are separately weighted in both
    blends. So it may be partly RE-VOTING rather than adding signal.

    THE THREE MEASUREMENTS, per type, over gated cells:
      1. Correlation (Pearson and Spearman) of the absolute TWI score
         against the drainage-area score and against the slope score.
         High correlation with either is re-voting; low with both is
         independent signal. Spearman is the one to read for a classed
         criterion, since the classes plateau.
      2. The share of gated cells clearing EMBANKMENT_SEED_MIN_SCORE WITH
         and WITHOUT TWI's contribution, and how many surviving SEEDS
         change. A criterion that moves neither is not deciding
         anything.
      3. Every surviving seed's full criteria signature, so the
         CHANNEL-ANCHORED (drainage + TWI) and OFF-CHANNEL (slope + TWI +
         soil) archetypes are countable rather than argued about.
    """
    result = identify_result["result"]
    gate_mask = result["gate_mask"]
    criteria = result["surfaces"]["criteria"]
    screens = result["screens"]
    lines = [f"=== TWI INDEPENDENT-SIGNAL REPORT ({label}) -- EVIDENCE ONLY, NOTHING CHANGED ==="]

    if not np.any(gate_mask):
        lines.append("  (no gated cells)")
        return "\n".join(lines)

    twi_gated = criteria[SURVEY_TYPE_EMBANKMENT]["twi"][gate_mask]

    lines.append("  1. CORRELATION of the absolute TWI score against the criteria it may be re-voting")
    # drainage_area is no longer an embankment criterion GRID (the band
    # moved to the pinch cell, where it is measured per compartment, not
    # per cell), so the embankment side correlates TWI against the two
    # criteria it can actually re-vote. The excavated run-on grid stays
    # exactly where it was -- that criterion did not move.
    pairs = (
        (SURVEY_TYPE_EMBANKMENT, "slope", criteria[SURVEY_TYPE_EMBANKMENT]["slope"]),
        (SURVEY_TYPE_EMBANKMENT, "soil", criteria[SURVEY_TYPE_EMBANKMENT]["soil"]),
        (SURVEY_TYPE_EXCAVATED, "drainage_runon", criteria[SURVEY_TYPE_EXCAVATED]["drainage_runon"]),
        (SURVEY_TYPE_EXCAVATED, "slope", criteria[SURVEY_TYPE_EXCAVATED]["slope"]),
    )
    for survey_type, name, grid in pairs:
        other = grid[gate_mask]
        lines.append(
            f"    {survey_type:<11} twi vs {name:<15} "
            f"Pearson {_pearson(twi_gated, other):+.3f}   Spearman {_spearman(twi_gated, other):+.3f}"
        )
    lines.append(
        "    (the embankment TWI grid is the criterion itself; on the excavated side TWI votes at "
        f"{WETNESS_TWI_SUBWEIGHT} of `wetness`, so it is correlated against that type's own "
        "run-on and slope criteria)"
    )

    lines.append(f"  2. CLEARING SHARE at EMBANKMENT_SEED_MIN_SCORE ({EMBANKMENT_SEED_MIN_SCORE}), with vs without TWI")
    gated_count = int(np.count_nonzero(gate_mask))
    without = {
        SURVEY_TYPE_EMBANKMENT: _embankment_surface_without_twi(criteria[SURVEY_TYPE_EMBANKMENT]),
        SURVEY_TYPE_EXCAVATED: _excavated_surface_without_twi(
            criteria[SURVEY_TYPE_EXCAVATED], screens["depression_depth"]
        ),
    }
    # THE SAME SHARE ON THE RETIRED FIXED CURVE, because the sign of this
    # delta is the reading the window-referencing branch has to re-take.
    # Under the fixed 6.0/10.0 pair TWI was floored across most of the
    # reference property, so INCLUDING it REDUCED the embankment clearing
    # share -- a criterion that only ever subtracted. Whether that sign
    # flips is the question, and printing both columns is how the run
    # answers it instead of the reader inferring it.
    fixed_twi = twi_score(
        screens["twi_raw"], RETIRED_FIXED_TWI_MIN_BREAKPOINT, RETIRED_FIXED_TWI_FULL_CREDIT_BREAKPOINT
    )
    fixed_surfaces = _surfaces_with_substituted_twi(criteria, screens, fixed_twi)
    for survey_type in SURVEY_TYPES:
        with_twi = result["surfaces"][survey_type][gate_mask]
        wo_twi = without[survey_type][gate_mask]
        fixed = fixed_surfaces[survey_type][gate_mask]
        n_with = int(np.count_nonzero(with_twi >= EMBANKMENT_SEED_MIN_SCORE))
        n_without = int(np.count_nonzero(wo_twi >= EMBANKMENT_SEED_MIN_SCORE))
        n_fixed = int(np.count_nonzero(fixed >= EMBANKMENT_SEED_MIN_SCORE))
        lines.append(
            f"    {survey_type:<11} with TWI {n_with}/{gated_count} "
            f"({n_with / gated_count * 100:.1f}%)   without TWI {n_without}/{gated_count} "
            f"({n_without / gated_count * 100:.1f}%)   delta {n_without - n_with:+d} cells"
        )
        def _direction(count: int) -> str:
            if count > n_without:
                return "ADDS"
            return "SUBTRACTS" if count < n_without else "is NEUTRAL"

        live_direction, fixed_direction = _direction(n_with), _direction(n_fixed)
        lines.append(
            f"    {'':<11} on the RETIRED FIXED curve: {n_fixed}/{gated_count} "
            f"({n_fixed / gated_count * 100:.1f}%), delta {n_without - n_fixed:+d} vs without-TWI"
        )
        lines.append(
            f"    {'':<11} SIGN: window-referenced TWI {live_direction}, retired fixed TWI "
            f"{fixed_direction}  -> the sign "
            + ("did NOT change" if live_direction == fixed_direction else "CHANGED")
        )

    # Seeds are the thing a weight change would actually move, so the
    # seed set is re-derived on the without-TWI surface with EVERY other
    # input held identical (same gate mask, same road cells, same
    # separation) and the two seed cell sets are compared directly.
    seeds_with = {tuple(record["rowcol"]) for record in result.get("embankment_seeds", [])}
    road_cells = identify_result["_road_cell_mask"]
    seeds_without = {
        tuple(seed["rowcol"])
        for seed in select_embankment_seeds(
            identify_result["_dem"],
            without[SURVEY_TYPE_EMBANKMENT],
            gate_mask,
            road_cells,
            criteria[SURVEY_TYPE_EMBANKMENT],
        )
    }
    lines.append(
        f"    embankment SEEDS: {len(seeds_with)} with TWI, {len(seeds_without)} without; "
        f"{len(seeds_with & seeds_without)} at the same cell, "
        f"{len(seeds_with - seeds_without)} lost, {len(seeds_without - seeds_with)} gained"
    )

    lines.append(
        "  3. SURVIVING SEEDS' FULL CRITERIA SIGNATURES, with the FILL CLAIM beside them. "
        "The channel-anchored/off-channel archetype split is RETIRED AT THE SEED: no seeding "
        "criterion measures channel position any more (drainage area moved to the pinch cell), so "
        "every seed is off-channel by construction and naming it that says nothing. The archetype "
        "column now reports which of the three remaining criteria LED the seed; the pinch-catchment "
        "column answers the channel question where it is actually asked."
    )
    criterion_names = list(EMBANKMENT_WEIGHTS)
    surviving = [
        zone for zone in result["zones"] if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
    ]
    if not surviving:
        lines.append("    (no surviving embankment zone on this boundary)")
    else:
        lines.append(
            "    zone          blend  "
            + "".join(f"{name:>15}" for name in criterion_names)
            + "     pinch ac  drainage   archetype"
        )
        for zone in sorted(surviving, key=lambda z: z["rank"]):
            signature = zone["seed"]["criteria_signature"]
            # The seeding archetype named by its DOMINANT WEIGHTED
            # contribution, since a signature is only readable against
            # the weights that consume it. EVERY SEED IS OFF-CHANNEL BY
            # CONSTRUCTION NOW -- the channel-anchored archetype was
            # defined by a drainage criterion that no longer scores
            # seeds -- so the name reports which of the three remaining
            # criteria led, and the FILL CLAIM is printed beside it
            # rather than inferred from the seed. Those two columns
            # together are the branch's own question: does an
            # off-channel seed sit above a real catchment?
            weighted = {name: EMBANKMENT_WEIGHTS[name] * signature[name] for name in criterion_names}
            top = max(weighted, key=lambda name: weighted[name])
            lines.append(
                f"    embankment {zone['rank']:<2} {zone['seed_blend_score']:>6.3f}  "
                + "".join(f"{signature[name]:>15.3f}" for name in criterion_names)
                + f"  {zone['pinch_catchment_acres']:>11.2f}  {zone['pinch_drainage_score']:>8.3f}"
                + f"   {top}-led"
            )
    lines.append(
        "  STATED, NOT ACTED ON: no weight moved in this branch. The removal/reweight decision "
        "belongs where seeding is the measured thing."
    )
    return "\n".join(lines)


def _zone_key(zone: dict) -> str:
    return f"{zone['survey_type']} {zone['rank']}"


def _match_zones(zones_a: list, zones_b: list) -> tuple:
    """Pair zones across two boundary runs by best envelope
    intersection-over-union WITHIN a survey type, greedily from the best
    pair down (a greedy IoU match cannot produce the crossed pairing a
    per-zone argmax can). Returns (matched pairs, unmatched from a,
    unmatched from b)."""
    candidates = []
    for za in zones_a:
        for zb in zones_b:
            if za["survey_type"] != zb["survey_type"]:
                continue
            union = za["polygon_utm"].union(zb["polygon_utm"]).area
            if union <= 0:
                continue
            iou = za["polygon_utm"].intersection(zb["polygon_utm"]).area / union
            if iou >= ZONE_MATCH_MIN_IOU:
                candidates.append((iou, za, zb))
    candidates.sort(key=lambda entry: entry[0], reverse=True)
    matched, used_a, used_b = [], set(), set()
    for iou, za, zb in candidates:
        if id(za) in used_a or id(zb) in used_b:
            continue
        matched.append((za, zb, iou))
        used_a.add(id(za))
        used_b.add(id(zb))
    return (
        matched,
        [z for z in zones_a if id(z) not in used_a],
        [z for z in zones_b if id(z) not in used_b],
    )


def summarize_boundary_stability(runs: list) -> str:
    """
    THE STANDING REGRESSION (see this section's header): the full water
    step run against TWO boundaries over THE SAME DEM, reporting which
    zones survive both, which appear under only one, and -- per matched
    zone -- the blend delta broken out BY CRITERION.

    ONE DEM, TWO BOUNDARIES, deliberately: the DEM is fetched over the
    UNION of the boundaries and passed to both runs, so the elevation
    grid, the priority-flood fill and the flow accumulation are
    byte-identical between them and the only thing that varies is the
    boundary itself. Fetching a DEM per boundary would let a different
    raster window move the fill and the accumulation, and every delta
    below would then be uninterpretable.

    HOW TO READ IT. A zone appearing under only one boundary is not
    automatically a defect: a larger boundary genuinely contains ground
    the smaller one does not, and can legitimately gain a zone there.
    The defect signature is the other direction and the criterion table:
    a zone LOST by the larger boundary over ground it still contains, or
    any nonzero per-criterion delta on a matched pair whose cells did not
    change. The criterion rows are what separate the two cases, which is
    why the report breaks the blend out rather than printing one number.

    THE HEADLINE NUMBER IS MEASURED, NOT ASSERTED. The per-cell TWI
    delta over the cells gated under both boundaries is printed as a
    value, with each boundary's reference window beside it. Under
    window referencing over one shared DEM it reads exactly 0.0000
    because the reference window is a property of the raster and both
    runs therefore reference the same one -- and the window bounds
    printed alongside are what let a reader SEE that rather than take
    it on faith. If the two windows ever differ, the delta is small and
    nonzero and both facts are on the page.
    """
    lines = ["=== BOUNDARY STABILITY: THE SAME DEM UNDER TWO BOUNDARIES ==="]
    if len(runs) < 2:
        lines.append(
            "  ONLY ONE BOUNDARY IN THIS RUN -- this check needs two. It is the standing regression "
            "against boundary-dependent scoring; a single-boundary run cannot perform it."
        )
        return "\n".join(lines)

    label_a, run_a, _boundary_a = runs[0]
    label_b, run_b, _boundary_b = runs[1]
    zones_a = run_a["result"]["zones"]
    zones_b = run_b["result"]["zones"]
    stats_a = run_a["gate_mask_stats"]
    stats_b = run_b["gate_mask_stats"]
    lines.append(
        f"  '{label_a}': {stats_a['gated_cells']} gated cells, {len(zones_a)} surviving zone(s)"
    )
    lines.append(
        f"  '{label_b}': {stats_b['gated_cells']} gated cells, {len(zones_b)} surviving zone(s)"
    )
    lines.append(
        "  (a gated-cell difference is EXPECTED and legitimate -- the gate mask IS the boundary)"
    )

    # THE REAL PER-CELL NUMBER, with the window that produced it beside
    # it. Printed here as well as in the scoring section because this is
    # the section a reader opens to ask "did the boundary move a score",
    # and a report that answers that with an assertion instead of a
    # measurement is the thing this whole arc exists to stop doing.
    if not all("twi_reference_window" in run["result"] for run in (run_a, run_b)):
        # A hand-built instrument fixture (the classification tests) has
        # no raster behind it. Say so rather than raising or, worse,
        # printing nothing where a number belongs.
        lines.append(
            "  TWI REFERENCE WINDOW / PER-CELL DELTA: not available -- these runs carry no raster "
            "(hand-built zone fixtures exercise the classification, not the scoring)."
        )
        return _finish_boundary_stability(lines, label_a, label_b, zones_a, zones_b)
    for label, run in ((label_a, run_a), (label_b, run_b)):
        reference = run["result"]["twi_reference_window"]
        breakpoints = run["result"]["twi_breakpoints"]
        min_x, min_y, max_x, max_y = reference["bounds"]
        lines.append(
            f"  '{label}' TWI reference window: x {min_x:.1f}..{max_x:.1f}, y {min_y:.1f}..{max_y:.1f} "
            f"({reference['cell_count']} cells, snapped {reference['snapped']}) -> curve "
            f"[{breakpoints['floor']}, {breakpoints['full_credit']}]"
        )
    windows_match = run_a["result"]["twi_reference_window"]["bounds"] == run_b["result"]["twi_reference_window"]["bounds"]
    lines.append(
        f"  SAME REFERENCE WINDOW: {windows_match}"
        + ("  (one shared DEM, so one window -- see the SNAP section for what a real re-fetch "
           "under each boundary would reference)" if windows_match else "")
    )
    twi_a = run_a["result"]["screens"]["twi_score"]
    twi_b = run_b["result"]["screens"]["twi_score"]
    both_gated = (
        run_a["result"]["gate_mask"]
        & run_b["result"]["gate_mask"]
        & ~np.isnan(twi_a)
        & ~np.isnan(twi_b)
    )
    if np.any(both_gated):
        cell_delta = np.abs(twi_a[both_gated] - twi_b[both_gated])
        lines.append(
            f"  PER-CELL TWI SCORE DELTA over the {int(np.count_nonzero(both_gated))} cells gated "
            f"under both: mean {float(np.mean(cell_delta)):.4f}, max {float(np.max(cell_delta)):.4f} "
            "(THE MEASURED VALUE -- 0.0000 when the two runs share a reference window, small and "
            "nonzero when they do not; both are informative)"
        )
    else:
        lines.append("  PER-CELL TWI SCORE DELTA: no cell is gated under both boundaries.")

    return _finish_boundary_stability(lines, label_a, label_b, zones_a, zones_b)


def _finish_boundary_stability(lines: list, label_a: str, label_b: str, zones_a: list, zones_b: list) -> str:
    """The zone-matching half of summarize_boundary_stability(), split
    out so the TWI-window half can report its own unavailability and
    still hand back a complete report rather than a truncated one."""
    matched, only_a, only_b = _match_zones(zones_a, zones_b)
    lines.append(f"  SURVIVES BOTH: {len(matched)} zone(s)")
    for za, zb, iou in matched:
        lines.append(
            f"    {_zone_key(za):<14} <-> {_zone_key(zb):<14} envelope IoU {iou:.3f}   "
            f"blend {za['mean_suitability']:.4f} -> {zb['mean_suitability']:.4f} "
            f"(delta {zb['mean_suitability'] - za['mean_suitability']:+.4f})"
        )
        names = sorted(set(za["criterion_contributions"]) | set(zb["criterion_contributions"]))
        for name in names:
            sa = za["criterion_contributions"].get(name, {}).get("mean_score")
            sb = zb["criterion_contributions"].get(name, {}).get("mean_score")
            if sa is None or sb is None:
                lines.append(f"      {name:<16} n/a under one boundary")
                continue
            flag = ""
            if name == "twi" and abs(sb - sa) > 0:
                # Not proof on its own -- a matched pair is not the same
                # CELL SET, so a mean can move because the compartment
                # grew. It is the line to look at first, and the
                # cross-boundary per-cell comparison in the TWI SCORING
                # section is the one that settles it.
                flag = "   <- TWI moved; check the per-cell comparison above"
            lines.append(f"      {name:<16} {sa:.4f} -> {sb:.4f}  (delta {sb - sa:+.4f}){flag}")
        if za["survey_type"] == SURVEY_TYPE_EMBANKMENT:
            lines.append(
                f"      seed blend       {za['seed_blend_score']:.4f} -> {zb['seed_blend_score']:.4f}  "
                f"(delta {zb['seed_blend_score'] - za['seed_blend_score']:+.4f})   "
                f"[seeding threshold {EMBANKMENT_SEED_MIN_SCORE}]"
            )

    lines.append(f"  ONLY UNDER '{label_a}': {len(only_a)} zone(s)")
    for zone in only_a:
        lines.append(
            f"    {_zone_key(zone):<14} {zone['zone_acres']:.2f} ac, blend {zone['mean_suitability']:.4f}"
            + (
                f", seed blend {zone['seed_blend_score']:.4f}"
                if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
                else ""
            )
            + "   <- LOST by the other boundary: the bug's own signature if the ground is still inside it"
        )
    lines.append(f"  ONLY UNDER '{label_b}': {len(only_b)} zone(s)")
    for zone in only_b:
        lines.append(
            f"    {_zone_key(zone):<14} {zone['zone_acres']:.2f} ac, blend {zone['mean_suitability']:.4f}"
            + (
                f", seed blend {zone['seed_blend_score']:.4f}"
                if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
                else ""
            )
            + "   <- gained; legitimate if this ground is only inside this boundary"
        )
    return "\n".join(lines)


def run_water_step(boundary: list, dem: dict) -> dict:
    """One full water step for one boundary over a SUPPLIED dem, with the
    few internals the comparison sections need attached under underscore
    keys (the dem itself, the on-parcel mask the retired percentile's
    population was built from, and the road cell mask, so the
    without-TWI seed re-derivation holds every other input identical).
    Underscored because they are instrument scaffolding, not part of any
    wire form."""
    production_areas = identify_optimized_production_areas(boundary, dem=dem)["scored_patches"]
    identify_result = identify_water_survey_areas(boundary, dem=dem, production_areas=production_areas)
    result = identify_result["result"]
    identify_result["_dem"] = dem
    identify_result["_dem_resolution_meters"] = dem["resolution_meters"]
    identify_result["_production_areas"] = production_areas
    # READ OFF THE RUN, never rebuilt here: the on-parcel population is
    # the one the retired percentile ranked over, and the road cell mask
    # is the one seeding excluded. A diagnostic that recomputed either
    # would be comparing the run against a second construction of the
    # same thing, which is exactly the mistake this file exists to catch.
    identify_result["_on_parcel_mask"] = result["on_parcel_mask"]
    identify_result["_road_cell_mask"] = result["road_cell_mask"]
    return identify_result


def dem_over_both_boundaries(boundaries: list) -> dict:
    """ONE DEM covering every boundary in the run -- fetched over the
    convex hull of their union, so both runs share one elevation grid,
    one fill and one flow accumulation (see summarize_boundary_stability
    for why that is the whole validity of the comparison)."""
    hull = unary_union([Polygon(boundary) for boundary in boundaries]).convex_hull
    return get_dem_for_boundary(list(hull.exterior.coords))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[3].strip())
    # ARGUMENT PARSING ONLY -- no logic changed here. Omitting
    # --boundary reproduces the reference run exactly as before, so
    # every existing invocation is unchanged.
    parser.add_argument(
        "--boundary",
        default=None,
        metavar="PATH",
        help=(
            "Path to a JSON list of [lon, lat] pairs to run as the PRIMARY boundary. "
            "Defaults to this script's reference boundary."
        ),
    )
    parser.add_argument(
        "--compare-boundary",
        default=None,
        metavar="PATH",
        help=(
            "Path to a JSON list of [lon, lat] pairs to run as the SECOND boundary of the "
            "boundary-stability check. Defaults to the stream-corridor boundary that lost a zone "
            "under the retired parcel-relative TWI."
        ),
    )
    parser.add_argument(
        "--single-boundary",
        action="store_true",
        help="Run the primary boundary only; the two-boundary stability sections report that they could not run.",
    )
    args = parser.parse_args()

    property_boundary = load_boundary(args.boundary) if args.boundary else REFERENCE_BOUNDARY

    compare_boundary = (
        None
        if args.single_boundary
        else (load_boundary(args.compare_boundary) if args.compare_boundary else STREAM_CORRIDOR_BOUNDARY)
    )
    boundaries = [("primary", property_boundary)]
    if compare_boundary is not None:
        boundaries.append(("comparison", compare_boundary))

    print("Identifying water survey areas (networked)...\n")
    # ONE DEM for every boundary in the run -- fetched over their union
    # so the two runs share an elevation grid, a fill and a flow
    # accumulation, which is what makes the per-criterion deltas below
    # attributable to the BOUNDARY rather than to a different raster
    # window. With a single boundary the hull IS that boundary, so the
    # reference run's DEM is unchanged.
    dem = dem_over_both_boundaries([boundary for _label, boundary in boundaries])

    runs = []
    for label, boundary in boundaries:
        print(f"  running '{label}' boundary...")
        runs.append((label, run_water_step(boundary, dem), boundary))
    print()

    # The primary run remains THE subject of every pre-existing section:
    # the export, the tables and the excavated finding are unchanged.
    identify_result = runs[0][1]
    production_areas = identify_result["_production_areas"]

    print(summarize_survey_zones_table(identify_result))
    print()
    print(summarize_seed_ladder(identify_result))
    print()
    print(summarize_gate_and_criteria(identify_result))
    print()
    print(summarize_threshold_comparison(identify_result, dem))
    print()
    print(summarize_depression_instrumentation(identify_result, dem))
    print()
    print(state_excavated_finding(identify_result))
    print()
    print(summarize_twi_calibration(runs))
    print()
    print(summarize_reference_window_snap(runs))
    print()
    print(summarize_twi_scoring_comparison(runs))
    print()
    for label, run, _boundary in runs:
        print(summarize_twi_independent_signal(run, label))
        print()
    print(summarize_boundary_stability(runs))

    result = identify_result["result"]
    # surfaces[type] is the RAW blend (smoothing is retired -- see
    # masked_focal_mean()): the excavated isobands show exactly what
    # extraction thresholds; the embankment isobands show the NOMINATION
    # surface the seeding claims from; the per-criterion bands below
    # show the criterion ground.
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
