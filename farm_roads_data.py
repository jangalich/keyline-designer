"""
farm_roads_data.py

Fetches existing road geometry near a property boundary from USGS's
National Map Transportation dataset — the "existing farm roads" input the
solar suitability constraint stack (solar_suitability.py) proximity-
buffers against.

Same USGS National Map family as hydrology_data.py's NHD fetch (same
free/no-key REST convention, same ArcGIS "query" operation, same bounding-
box + buffer approach), just a different data theme (transportation
instead of hydro) hosted on the map-service host for that theme. Real,
public road/right-of-way data — this deliberately does NOT try to invent
or infer a road network from terrain the way production_area.py infers
production land from slope; report_generator.py's Farm Roads section
already carries the "no dedicated existing-access data" caveat for
inferred/proposed routing, but this module gives it something to
proximity-check *existing* access against where public road data covers
the parcel.

Real caveat, stated plainly: this dataset is built from public
road/right-of-way sources. A private farm track, driveway, or internal
access lane that was never surveyed into a public transportation dataset
won't appear here — see FARM_ROAD_CONFIDENCE_NOTES.

Docs: https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer
"""

import math
import time

import requests

from feature_schema import CONFIDENCE_MEDIUM, make_feature, make_feature_collection

TRANSPORTATION_BASE = "https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer"
ROAD_LAYER = 0  # "Road" — see the MapServer's layer listing at the URL above

FARM_ROAD_CONFIDENCE_NOTES = (
    "Road geometry is USGS National Map Transportation data — public "
    "road/right-of-way sources, not a survey of this specific property. "
    "Private farm tracks, driveways, or internal access lanes that were "
    "never captured in a public transportation dataset will not appear "
    "here; treat an absence of nearby roads as 'no mapped public road,' "
    "not necessarily 'no access.'"
)


def _bounding_box(
    coordinates: list[tuple[float, float]], buffer_meters: float = 150
) -> tuple[float, float, float, float]:
    """Same bbox+buffer approach as hydrology_data.py — see that module
    for why a buffer matters (a road along the property line is a very
    common real case, and a tight box would miss it)."""
    lons = [pt[0] for pt in coordinates]
    lats = [pt[1] for pt in coordinates]

    min_lat, max_lat = min(lats), max(lats)
    avg_lat = (min_lat + max_lat) / 2

    meters_per_degree_lat = 111_320
    meters_per_degree_lon = 111_320 * abs(math.cos(math.radians(avg_lat)))

    lat_buffer = buffer_meters / meters_per_degree_lat
    lon_buffer = buffer_meters / meters_per_degree_lon

    return (
        min(lons) - lon_buffer,
        min_lat - lat_buffer,
        max(lons) + lon_buffer,
        max_lat + lat_buffer,
    )


def _query_road_layer(
    bbox: tuple[float, float, float, float], max_retries: int = 2
) -> list[dict]:
    """Same retry-with-increasing-timeout pattern as hydrology_data.py's
    _query_layer — USGS's map services are occasionally slow, not down."""
    min_lon, min_lat, max_lon, max_lat = bbox

    params = {
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "outFields": "*",
        "f": "geojson",
    }

    url = f"{TRANSPORTATION_BASE}/{ROAD_LAYER}/query"
    last_error = None

    for attempt in range(max_retries + 1):
        timeout = 30 + (attempt * 30)
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            return data.get("features", [])
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise last_error


def get_farm_roads_for_boundary(
    boundary_coordinates: list[tuple[float, float]], buffer_meters: float = 150
) -> list[dict]:
    """
    Returns nearby/intersecting road segments as a list of
    {'name', 'geometry'} dicts, geometry already GeoJSON LineString/
    MultiLineString in WGS84 (lon/lat) — the same raw shape
    hydrology_data.get_water_features_for_boundary uses for streams.
    """
    bbox = _bounding_box(boundary_coordinates, buffer_meters=buffer_meters)
    road_features = _query_road_layer(bbox)

    return [
        {
            "name": f["properties"].get("name") or f["properties"].get("fullname") or "Unnamed road",
            "geometry": f["geometry"],
        }
        for f in road_features
        if f.get("geometry") is not None
    ]


def get_farm_roads_geojson(
    boundary_coordinates: list[tuple[float, float]], buffer_meters: float = 150
) -> dict:
    """Same fetch as get_farm_roads_for_boundary, wrapped as a
    schema-conformant FeatureCollection (layer="farm_road") — a
    diagnostic layer, useful for checking road proximity input
    independently of the solar suitability logic built on top of it."""
    roads = get_farm_roads_for_boundary(boundary_coordinates, buffer_meters=buffer_meters)

    features = [
        make_feature(
            feature_id=f"farm-road-{i}",
            geometry=road["geometry"],
            layer="farm_road",
            label=road["name"],
            confidence=CONFIDENCE_MEDIUM,
            confidence_notes=FARM_ROAD_CONFIDENCE_NOTES,
        )
        for i, road in enumerate(roads)
    ]
    return make_feature_collection(features)


def summarize_farm_roads(roads: list[dict]) -> str:
    if not roads:
        return (
            "No mapped public roads found near this property. This may "
            "mean genuinely limited public access, or simply that a "
            "private farm track/driveway isn't captured in this dataset "
            "— see confidence_notes."
        )

    names = sorted({r["name"] for r in roads})
    lines = [f"Mapped roads found nearby: {len(roads)} segment(s)"]
    for name in names[:5]:
        lines.append(f"  - {name}")
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

    print("Fetching farm road data for property boundary...\n")

    try:
        roads = get_farm_roads_for_boundary(property_boundary)
        print(summarize_farm_roads(roads))
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map transportation service — not a fully sandboxed environment."
        )
