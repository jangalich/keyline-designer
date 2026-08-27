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
    survey_region_embankment / survey_region_excavated
        -- every region, full properties (water_survey_areas.
           survey_areas_to_geojson()'s own features, verbatim)
    suitability_isoband_embankment / suitability_isoband_excavated
        -- filled contour bands of each surface at ISOBAND_LEVELS
    survey_context_production_area
        -- the optimized production patches the gravity/overlap
           measurements ran against
    survey_context_boundary
        -- the parcel boundary, carrying the gate-mask summary as
           properties (grid/on-parcel/ceiling-removed/gated cell counts)

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
from raster_grid import pixel_center_xy
from rasterio.warp import transform_geom
from shapely.geometry import mapping
from water_survey_areas import (
    SURVEY_TYPES,
    identify_water_survey_areas,
    survey_areas_to_geojson,
)

# Where the export lands, beside this script's terminal output. Passed
# explicitly at the call site rather than read as a bound default -- a
# module constant used as a default argument is bound once at import, so
# it stops being configurable the moment anything wants to change it.
WATER_SURVEY_AREAS_GEOJSON_PATH = "water_survey_areas.geojson"

# The isoband edges: bands are [0.2,0.4), [0.4,0.6), [0.6,0.8), and
# [0.8, top]. These are THE threshold-tuning instrument -- the 0.4/0.6
# edges bracket the provisional SUITABILITY_THRESHOLD (0.5) so a viewer
# sees at a glance which ground a nudged threshold would gain or lose.
# CONFIGURABLE.
ISOBAND_LEVELS = (0.2, 0.4, 0.6, 0.8)

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
                    f"{survey_type}-type suitability surface. Overlay these bands on imagery and pick "
                    "the extraction threshold and region floor from where regions cohere and "
                    "dissolve -- every weight and table behind this surface is a provisional v1 "
                    "prior (TUNE FROM FIRST RUN, water_survey_areas.py)."
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
) -> dict:
    """
    Writes the full tuning export to one feature_schema-compliant
    GeoJSON file: every survey region (both typed layers, flagged not
    filtered), the isoband layers, and the context layers. Consumes ONLY
    stored wire forms -- identify_result's own zones_geojson features,
    prebuilt isoband dicts, the caller's WGS84 boundary coordinates, and
    each production patch's stored geometry_wgs84. Validates before
    writing; returns {'path', 'feature_count', 'by_layer'}.
    """
    features = list(survey_areas_to_geojson(identify_result["regions"])["features"])
    for survey_type in SURVEY_TYPES:
        features.extend(_isoband_features(survey_type, isobands_by_type[survey_type]))
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


def summarize_survey_regions_table(identify_result: dict) -> str:
    """One line per region, per type -- rank, acreage, mean/max
    suitability, top two contributing criteria, gravity note, overlaps,
    boundary adjacency, flags. Every region appears (flagged, never
    filtered)."""
    lines = []
    for survey_type in SURVEY_TYPES:
        regions = identify_result["regions_by_type"][survey_type]
        lines.append(f"=== {survey_type.upper()}-TYPE SURVEY REGIONS ({len(regions)}) ===")
        if not regions:
            lines.append("  (none cleared the threshold)")
            continue
        for region in regions:
            top_two = sorted(
                region["criterion_contributions"].items(),
                key=lambda item: -item[1]["weighted_contribution"],
            )[:2]
            criteria_text = "+".join(f"{name}({entry['mean_score']})" for name, entry in top_two)
            flags = f" flags={','.join(region['flags'])}" if region["flags"] else ""
            lines.append(
                f"  #{region['rank']} region {region['id']}: {region['area_acres']:.2f} ac, "
                f"mean {region['mean_suitability']:.3f} / max {region['max_suitability']:.3f}, "
                f"top: {criteria_text}, {_gravity_cell(region)}, "
                f"canopy {_overlap_cell(region['canopy_overlap_pct'])} / road "
                f"{_overlap_cell(region['road_overlap_pct'])} / prod "
                f"{_overlap_cell(region['production_overlap_pct'])}, "
                f"boundary-adj {region['boundary_adjacency_fraction']:.0%}, "
                f"conf {region['confidence']}{flags}"
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

    print(summarize_survey_regions_table(identify_result))
    print()
    print(summarize_gate_and_criteria(identify_result))

    result = identify_result["result"]
    isobands_by_type = {
        survey_type: compute_suitability_isobands(dem, result["surfaces"][survey_type])
        for survey_type in SURVEY_TYPES
    }

    export = export_water_survey_areas_geojson(
        identify_result,
        property_boundary,
        production_areas,
        isobands_by_type,
        path=WATER_SURVEY_AREAS_GEOJSON_PATH,
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
