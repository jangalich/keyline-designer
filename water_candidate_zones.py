"""
water_candidate_zones.py

Step 3 of valley-based water-system candidate-zone identification: for
each primary valley (valley_delineation.py) and each candidate production
area it could plausibly serve (production_area.py), finds the portion of
that valley within plausible service distance of a production area,
excludes anything too close to the property boundary to actually develop,
and outputs the qualifying segment(s) as a buffered zone polygon — a zone,
not a point. Finding one "best" pond/dam site within that zone is
explicitly out of scope here (see the confidence_notes on the output
feature) — that's future, separate, more detailed work (storage volume,
dam wall geometry).

    DEM (dem_data.py)
        --> valleys (valley_delineation.py)
        --> production areas (production_area.py)
        --> [this module] service-distance + boundary-setback filtering
        --> buffered candidate-zone polygons, one per qualifying valley

Elevation relative to the production area(s) a zone could serve is NOT a
generation-time exclusion here — it used to be (a hard "must clear
MIN_GRAVITY_GRADIENT" gate), but that discarded genuinely well-suited
water-system ground before scoring ever got to weigh it: a site that's
otherwise excellent but sits below its nearest production area (requiring
a pump) is a real, valid candidate — a pump is a cost/maintenance
tradeoff, not a disqualification. This module instead computes and
attaches the raw elevation-differential/gradient data for every candidate
zone's relationship to each production area it could plausibly serve
(see production_area_relationships below), and leaves turning that into a
"gravity is preferred" SCORE to water_suitability.py — the same
gate-to-preference move production_suitability.py already made for soil
(see that module's own docstring) and solar_suitability.py already made
for production-zone proximity.

find_candidate_zones() below is deliberately a pure function over already-
computed valleys/production_areas/boundary — no DEM fetch, no network.
That split is what makes Stage 2 ("is the zone-filtering logic correct")
testable independently of Stage 1 ("is the DEM/valley delineation
accurate") — see test_water_candidate_zones.py, and the module docstrings
on dem_data.py/valley_delineation.py/production_area.py for the same
reasoning applied to the layers underneath this one.
"""

from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import unary_union

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import identify_production_areas, production_areas_to_geojson
from valley_delineation import delineate_valleys, valleys_to_geojson

# Zones within this distance of the property boundary are excluded even
# if geometrically valid — too close to the property line to realistically
# develop (access, neighbor impact, and likely setback/easement rules this
# pipeline has no data on). CONFIGURABLE.
MIN_BOUNDARY_SETBACK_METERS = 15.0

# How far downhill a valley point's elevation advantage is considered
# relevant to a given production-area patch at all. Beyond this, even a
# technically-qualifying gradient isn't a plausible single contour-channel
# run. CONFIGURABLE.
MAX_SERVICE_DISTANCE_METERS = 800.0

# Guards against a valley point sitting immediately adjacent to (but
# genuinely OUTSIDE) a production-area patch, where "above by X% grade
# over Y meters" no longer means anything (Y too small to be meaningful).
# Deliberately NOT applied to a point already INSIDE/touching a patch
# (distance == 0 — see _elevation_relationships_for_branch()): that guard
# is about rejecting a near-but-separate siting as too close for the
# distance math to mean anything, not about rejecting siting inside the
# production area at all. That distinction is real, not academic — a
# single production-area patch can legitimately cover most of a parcel
# (production_area.py's own slope threshold, confirmed live: ~95% of one
# real reference property), and a strict "distance < 10m is always too
# close" reading would then reject nearly every valley point on that
# property outright, since almost everywhere on it genuinely IS inside
# that one patch. Same "gate becomes a genuinely-inapplicable rule at this
# property's real scale, fix it, don't just re-tune the number" pattern as
# road_corridors.py's/production_suitability.py's own earlier softened-
# exclusion fixes documented in README.md. CONFIGURABLE.
MIN_SERVICE_DISTANCE_METERS = 10.0

# Half-width of the buffered zone band drawn around each qualifying valley
# segment — deliberately a zone/band, not the valley centerline itself,
# per the "zone, not a point" framing of this whole feature. CONFIGURABLE.
ZONE_BUFFER_METERS = 20.0

WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES = (
    "This identifies a general candidate zone for water-system "
    "infrastructure (keyline plowing patterns, pond/dam potential, ram "
    "pump routing) — a stretch of valley within plausible service "
    "distance of a candidate production area, outside the boundary "
    "setback. Elevation relative to that production area is NOT a "
    "generation-time filter here: a candidate sitting BELOW its nearest "
    "production area (which would need a pump to deliver water uphill) is "
    "still reported, same as one sitting comfortably above it (which "
    "could gravity-feed) — see properties.production_area_relationships "
    "for the real elevation differential/gradient this candidate was "
    "measured against, and water_suitability.py for how that's turned "
    "into a real, weighted preference score rather than a pass/fail gate. "
    "This is NOT a specific pond or dam site: actual siting requires "
    "separate, more detailed analysis (storage volume, dam wall geometry, "
    "spillway design) not covered here. It also inherits the limitations "
    "of the layers it's built on — DEM-derived valley delineation and a "
    "slope-only production-area heuristic — so treat this as a starting "
    "area to walk and ground-truth, not a final answer."
)


def _elevation_relationships_for_branch(
    branch_utm: list[tuple[float, float, float]],
    production_areas: list[dict],
    max_service_distance: float,
    min_service_distance: float,
) -> list[tuple[float, float, float, Optional[dict]]]:
    """
    For each (x, y, elevation) point along a valley branch, finds the
    production area (within the plausible service-distance window) with
    the most GRAVITY-FAVORABLE elevation relationship to that point —
    "best" = the largest elevation_differential_m, whether or not it's
    actually above the production area. This is a real measurement, not a
    qualification check: no minimum gradient is required to be tagged here
    (see module docstring for why gravity moved from a generation-time
    gate to a water_suitability.py scoring input).

    min_service_distance is only applied to a point genuinely OUTSIDE a
    patch's own polygon (distance > 0) — a point already inside/touching
    the patch (distance == 0) is never rejected by it. Real bug, found
    live: with a single production-area patch covering ~95% of a real
    reference property, "distance < 10m is too close" rejected every
    valley point on that property outright, since a point genuinely
    inside a patch that large has nowhere else to be relative to it.
    MIN_SERVICE_DISTANCE_METERS exists to reject a near-but-SEPARATE
    siting (where "above by X% grade over Y meters" stops meaning
    anything for Y too small) — it was never meant to reject siting
    INSIDE the production area entirely, and shouldn't, per this whole
    feature's "elevation/proximity is a preference, not a gate" direction
    (see module docstring): a water zone genuinely inside/adjacent to the
    production area it serves is a legitimate, common real-world
    scenario, the same way solar_suitability.py now allows a structure
    candidate to sit fully inside a production zone.

    Returns the same points, each tagged with either the closest-to-
    gravity-favorable patch relationship
    ({'id', 'elevation_differential_m', 'distance_m'}) or None (no
    production area at all within the service-distance window).
    """
    results = []
    for x, y, elevation in branch_utm:
        point = Point(x, y)
        best = None
        for patch in production_areas:
            distance = point.distance(patch["polygon_utm"])
            if distance > max_service_distance:
                continue
            if 0 < distance < min_service_distance:
                continue
            elevation_differential_m = elevation - patch["representative_elevation_m"]
            if best is None or elevation_differential_m > best["elevation_differential_m"]:
                best = {
                    "id": patch["id"],
                    "elevation_differential_m": elevation_differential_m,
                    "distance_m": distance,
                }
        results.append((x, y, elevation, best))
    return results


def _runs_of_qualifying_points(
    tagged_points: list[tuple[float, float, float, Optional[dict]]],
    boundary_polygon_utm: Polygon,
    min_boundary_setback: float,
) -> list[tuple[list[tuple[float, float]], list[dict]]]:
    """
    Groups consecutive service-distance-qualifying points along a branch
    into contiguous runs, additionally dropping any point that's outside
    the property boundary or within min_boundary_setback of it — a point
    failing either check breaks the run. Elevation/gradient is NOT part of
    what breaks a run here (see module docstring) — a point is included as
    long as some production area is within the service-distance window,
    regardless of whether that point sits above or below it.

    Returns a list of (points, relationships) per run — relationships is
    the parallel list of each included point's tagged patch relationship
    dict, for water_suitability.py's scoring (and this module's own
    per-zone aggregation) to consume.
    """
    runs = []
    current_points: list[tuple[float, float]] = []
    current_relationships: list[dict] = []

    def _flush():
        if current_points:
            runs.append((list(current_points), list(current_relationships)))

    for x, y, _elevation, relationship in tagged_points:
        point = Point(x, y)
        on_property = boundary_polygon_utm.contains(point)
        far_enough_from_boundary = (
            point.distance(boundary_polygon_utm.boundary) >= min_boundary_setback
        )

        if relationship is not None and on_property and far_enough_from_boundary:
            current_points.append((x, y))
            current_relationships.append(relationship)
        else:
            _flush()
            current_points, current_relationships = [], []

    _flush()
    return runs


def _aggregate_production_area_relationships(relationships: list[dict]) -> list[dict]:
    """
    Rolls up the per-point elevation relationships collected across every
    run/branch contributing to one valley's zone into one entry per served
    production area — the MEDIAN elevation differential/distance across
    every point that picked that production area as its best match
    (median, not mean, for the same "resist a single outlier point
    skewing the reported number" reasoning as production_area.py's own
    representative-elevation choice).

    Returns a list of:
        {
            'production_area_id': int,
            'elevation_differential_m': float,  # + = zone sits above the
                                                  # production area
                                                  # (gravity-favorable);
                                                  # - = below (would need a
                                                  # pump)
            'distance_m': float,
            'gradient_pct': float,               # elevation_differential_m
                                                  # / distance_m * 100 —
                                                  # can be negative
            'above_production_area': bool,
        }
    sorted by elevation_differential_m descending (most gravity-favorable
    first), so callers that just want "the best one" can take index 0.
    """
    points_by_id: dict = {}
    for r in relationships:
        points_by_id.setdefault(r["id"], []).append(r)

    aggregated = []
    for production_area_id, points in points_by_id.items():
        differential = float(np.median([p["elevation_differential_m"] for p in points]))
        distance = float(np.median([p["distance_m"] for p in points]))
        gradient_pct = (differential / distance * 100) if distance > 0 else 0.0
        aggregated.append(
            {
                "production_area_id": production_area_id,
                "elevation_differential_m": round(differential, 2),
                "distance_m": round(distance, 1),
                "gradient_pct": round(gradient_pct, 2),
                "above_production_area": differential > 0,
            }
        )

    aggregated.sort(key=lambda r: -r["elevation_differential_m"])
    return aggregated


def find_candidate_zones(
    valleys: list[dict],
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    dem_crs: str,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
    zone_buffer_meters: float = ZONE_BUFFER_METERS,
) -> list[dict]:
    """
    Pure zone-filtering logic (Step 3) — see module docstring for why this
    takes already-computed valleys/production_areas rather than fetching
    or delineating anything itself, and for why elevation/gradient is no
    longer one of the filters applied here (min_gravity_gradient is gone
    from this signature entirely — it's now water_suitability.py's scoring
    concern, not a generation-time parameter).

    Returns one entry per valley with at least one qualifying segment:
        {
            'valley_id': int,
            'served_production_area_ids': [int, ...],
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'production_area_relationships': [...],   # see
                _aggregate_production_area_relationships()'s docstring —
                one entry per served production area, sorted most-
                gravity-favorable first
            'primary_production_area_relationship': dict,  # same shape as
                one production_area_relationships entry — the single most
                gravity-favorable one, for callers that just want one
                headline number
        }
    """
    if not production_areas:
        return []

    zones = []

    for valley in valleys:
        run_geometries = []
        relationships: list[dict] = []

        for branch in valley["branches_utm"]:
            tagged_points = _elevation_relationships_for_branch(
                branch,
                production_areas,
                max_service_distance_meters,
                min_service_distance_meters,
            )
            for run_points, run_relationships in _runs_of_qualifying_points(
                tagged_points, boundary_polygon_utm, min_boundary_setback_meters
            ):
                geometry = (
                    LineString(run_points).buffer(zone_buffer_meters)
                    if len(run_points) >= 2
                    else Point(run_points[0]).buffer(zone_buffer_meters)
                )
                run_geometries.append(geometry)
                relationships.extend(run_relationships)

        if not run_geometries:
            continue

        polygon_utm = unary_union(run_geometries).intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        geometry_wgs84 = transform_geom(dem_crs, "EPSG:4326", mapping(polygon_utm))

        production_area_relationships = _aggregate_production_area_relationships(relationships)

        zones.append(
            {
                "valley_id": valley["id"],
                "served_production_area_ids": sorted(
                    r["production_area_id"] for r in production_area_relationships
                ),
                "polygon_utm": polygon_utm,
                "geometry_wgs84": geometry_wgs84,
                "production_area_relationships": production_area_relationships,
                "primary_production_area_relationship": production_area_relationships[0],
            }
        )

    return zones


def zones_to_geojson(zones: list[dict]) -> dict:
    """Wraps find_candidate_zones() output as the schema-conformant
    GeoJSON FeatureCollection this feature actually delivers
    (layer="water_system_candidate"). This is the UNSCORED diagnostic
    layer — confidence stays CONFIDENCE_LOW/flat here, same as
    production_area.py's own production_areas_to_geojson() before
    production_suitability.py enriches it; water_suitability.py is where
    real, differentiated confidence/suitability_score get added, on this
    same layer, following that exact precedent."""
    features = [
        make_feature(
            feature_id=f"water-system-candidate-{z['valley_id']}",
            geometry=z["geometry_wgs84"],
            layer="water_system_candidate",
            label=f"Water system candidate zone (valley {z['valley_id']})",
            confidence=CONFIDENCE_LOW,
            confidence_notes=WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES,
            extra_properties={
                "source_valley_id": z["valley_id"],
                "served_production_area_ids": z["served_production_area_ids"],
                "production_area_relationships": z["production_area_relationships"],
                "primary_production_area_relationship": z["primary_production_area_relationship"],
            },
        )
        for z in zones
    ]
    return make_feature_collection(features)


def identify_water_system_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in —
    e.g. reused from generate_full_report.py already fetching it, or
    supplied directly in a test), delineates valleys, identifies
    production-area candidates, and returns:

        {
            'zones_geojson': FeatureCollection,             # layer="water_system_candidate" — the deliverable
            'valleys_geojson': FeatureCollection,            # layer="valley" — diagnostic (Stage 1)
            'production_areas_geojson': FeatureCollection,   # layer="production_area_candidate" — diagnostic
        }

    The valley/production-area layers are returned alongside the final
    zones deliberately, not just internally — per this feature's stated
    debugging goal, being able to inspect "did we find the right valleys"
    and "is the zone logic right" as two separate, independently checkable
    outputs matters more here than it would for a simpler layer.
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

    valleys = delineate_valleys(dem)
    production_areas = identify_production_areas(dem, boundary_polygon_utm)

    zones = find_candidate_zones(
        valleys, production_areas, boundary_polygon_utm, dem["crs"], **zone_kwargs
    )

    return {
        "zones_geojson": zones_to_geojson(zones),
        "valleys_geojson": valleys_to_geojson(valleys),
        "production_areas_geojson": production_areas_to_geojson(production_areas),
    }


def summarize_water_system_candidate_zones(result: dict) -> str:
    zone_count = len(result["zones_geojson"]["features"])
    valley_count = len(result["valleys_geojson"]["features"])
    production_area_count = len(result["production_areas_geojson"]["features"])

    if zone_count == 0:
        return (
            f"{valley_count} primary valley(s) and {production_area_count} "
            "production-area candidate(s) found, but no valley segment falls "
            "within the service-distance/boundary-setback thresholds — no "
            "water system candidate zones identified."
        )

    return (
        f"Water system candidate zones: {zone_count} "
        f"(from {valley_count} primary valley(s) and "
        f"{production_area_count} production-area candidate(s))"
    )


if __name__ == "__main__":
    # Test case: the user's own drawn property boundary.
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Identifying water system candidate zones for property boundary...\n")

    try:
        result = identify_water_system_candidate_zones(property_boundary)
        print(summarize_water_system_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer — not a fully sandboxed environment."
        )
