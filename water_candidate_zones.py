"""
water_candidate_zones.py

Step 3 of valley-based water-system candidate-zone identification: for
each primary valley (valley_delineation.py) and each candidate production
area it could plausibly serve (production_area.py), flags the portion of
that valley sitting far enough above the production area's elevation to
support gravity-fed delivery via a contour channel, excludes anything too
close to the property boundary to actually develop, and outputs the
qualifying segment(s) as a buffered zone polygon — a zone, not a point.
Finding one "best" pond/dam site within that zone is explicitly out of
scope here (see the confidence_notes on the output feature) — that's
future, separate, more detailed work (storage volume, dam wall geometry).

    DEM (dem_data.py)
        --> valleys (valley_delineation.py)
        --> production areas (production_area.py)
        --> [this module] gradient + boundary-setback filtering
        --> buffered candidate-zone polygons, one per qualifying valley

find_candidate_zones() below is deliberately a pure function over already-
computed valleys/production_areas/boundary — no DEM fetch, no network.
That split is what makes Stage 2 ("is the zone-filtering logic correct")
testable independently of Stage 1 ("is the DEM/valley delineation
accurate") — see test_water_candidate_zones.py, and the module docstrings
on dem_data.py/valley_delineation.py/production_area.py for the same
reasoning applied to the layers underneath this one.
"""

from typing import Optional

from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import unary_union

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import identify_production_areas, production_areas_to_geojson
from valley_delineation import delineate_valleys, valleys_to_geojson

# Minimum gradient (elevation drop / horizontal distance) a valley point
# must clear above a candidate production area's representative elevation
# to count as "high enough" for reliable gravity flow through a contour
# channel. 0.01 = 1% fall (~1m per 100m run) — a conservative floor:
# typical keyline/contour channel design commonly works with 0.5-1% fall
# depending on soil and channel finish; erring toward 1% flags zones with
# real, comfortable head rather than a knife-edge elevation win.
# CONFIGURABLE — tune against your own property once its actual channel
# grades and delivery performance are known.
MIN_GRAVITY_GRADIENT = 0.01

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

# Guards against a valley point sitting inside/immediately adjacent to the
# production-area patch itself, where "above by X% grade over Y meters" no
# longer means anything (Y too small to be meaningful). CONFIGURABLE.
MIN_SERVICE_DISTANCE_METERS = 10.0

# Half-width of the buffered zone band drawn around each qualifying valley
# segment — deliberately a zone/band, not the valley centerline itself,
# per the "zone, not a point" framing of this whole feature. CONFIGURABLE.
ZONE_BUFFER_METERS = 20.0

WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES = (
    "This identifies a general candidate zone for water-system "
    "infrastructure (keyline plowing patterns, pond/dam potential, ram "
    "pump routing) — a stretch of valley sitting above a candidate "
    "production area by at least the configured minimum gradient, outside "
    "the boundary setback. It is NOT a specific pond or dam site: actual "
    "siting requires separate, more detailed analysis (storage volume, "
    "dam wall geometry, spillway design) not covered here. It also "
    "inherits the limitations of the layers it's built on — DEM-derived "
    "valley delineation and a slope-only production-area heuristic — so "
    "treat this as a starting area to walk and ground-truth, not a final "
    "answer."
)


def _qualifying_points_for_branch(
    branch_utm: list[tuple[float, float, float]],
    production_areas: list[dict],
    min_gradient: float,
    max_service_distance: float,
    min_service_distance: float,
) -> list[tuple[float, float, float, Optional[dict]]]:
    """
    For each (x, y, elevation) point along a valley branch, finds the best
    production-area patch it clears the minimum gradient against (if any)
    within the plausible service-distance window. "Best" = the patch it
    clears by the largest margin, a simple tie-break when a point could
    plausibly serve more than one nearby patch.

    Returns the same points, each tagged with either the qualifying patch
    info ({'id', 'margin', 'distance_m'}) or None.
    """
    results = []
    for x, y, elevation in branch_utm:
        point = Point(x, y)
        best = None
        for patch in production_areas:
            distance = point.distance(patch["polygon_utm"])
            if distance < min_service_distance or distance > max_service_distance:
                continue
            required_diff = min_gradient * distance
            actual_diff = elevation - patch["representative_elevation_m"]
            if actual_diff < required_diff:
                continue
            margin = actual_diff - required_diff
            if best is None or margin > best["margin"]:
                best = {"id": patch["id"], "margin": margin, "distance_m": distance}
        results.append((x, y, elevation, best))
    return results


def _runs_of_qualifying_points(
    tagged_points: list[tuple[float, float, float, Optional[dict]]],
    boundary_polygon_utm: Polygon,
    min_boundary_setback: float,
) -> list[tuple[list[tuple[float, float]], set]]:
    """
    Groups consecutive gradient-qualifying points along a branch into
    contiguous runs, additionally dropping any point that's outside the
    property boundary or within min_boundary_setback of it — a point
    failing either check breaks the run, same as failing the gradient
    check does.

    Returns a list of (points, served_patch_ids) per run.
    """
    runs = []
    current_points: list[tuple[float, float]] = []
    current_patch_ids: set = set()

    def _flush():
        if current_points:
            runs.append((list(current_points), set(current_patch_ids)))

    for x, y, _elevation, patch in tagged_points:
        point = Point(x, y)
        on_property = boundary_polygon_utm.contains(point)
        far_enough_from_boundary = (
            point.distance(boundary_polygon_utm.boundary) >= min_boundary_setback
        )

        if patch is not None and on_property and far_enough_from_boundary:
            current_points.append((x, y))
            current_patch_ids.add(patch["id"])
        else:
            _flush()
            current_points, current_patch_ids = [], set()

    _flush()
    return runs


def find_candidate_zones(
    valleys: list[dict],
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    dem_crs: str,
    min_gravity_gradient: float = MIN_GRAVITY_GRADIENT,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
    zone_buffer_meters: float = ZONE_BUFFER_METERS,
) -> list[dict]:
    """
    Pure zone-filtering logic (Step 3) — see module docstring for why this
    takes already-computed valleys/production_areas rather than fetching
    or delineating anything itself.

    Returns one entry per valley with at least one qualifying segment:
        {
            'valley_id': int,
            'served_production_area_ids': [int, ...],
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
        }
    """
    if not production_areas:
        return []

    zones = []

    for valley in valleys:
        run_geometries = []
        served_ids: set = set()

        for branch in valley["branches_utm"]:
            tagged_points = _qualifying_points_for_branch(
                branch,
                production_areas,
                min_gravity_gradient,
                max_service_distance_meters,
                min_service_distance_meters,
            )
            for run_points, patch_ids in _runs_of_qualifying_points(
                tagged_points, boundary_polygon_utm, min_boundary_setback_meters
            ):
                geometry = (
                    LineString(run_points).buffer(zone_buffer_meters)
                    if len(run_points) >= 2
                    else Point(run_points[0]).buffer(zone_buffer_meters)
                )
                run_geometries.append(geometry)
                served_ids.update(patch_ids)

        if not run_geometries:
            continue

        polygon_utm = unary_union(run_geometries).intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        geometry_wgs84 = transform_geom(dem_crs, "EPSG:4326", mapping(polygon_utm))

        zones.append(
            {
                "valley_id": valley["id"],
                "served_production_area_ids": sorted(served_ids),
                "polygon_utm": polygon_utm,
                "geometry_wgs84": geometry_wgs84,
            }
        )

    return zones


def zones_to_geojson(zones: list[dict]) -> dict:
    """Wraps find_candidate_zones() output as the schema-conformant
    GeoJSON FeatureCollection this feature actually delivers
    (layer="water_system_candidate")."""
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
            "production-area candidate(s) found, but none of the valleys "
            "clear the minimum gradient/setback thresholds — no water "
            "system candidate zones identified."
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
