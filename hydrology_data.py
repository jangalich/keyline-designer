"""
hydrology_data.py

Fetches surface water features (streams, ponds, lakes) from USGS's
National Hydrography Dataset (NHD) for a given property boundary.

This matters a lot for keyline design specifically — existing water
features (or their absence) directly shape where dams, ponds, and water-
harvesting earthworks make sense, and whether a property has natural
drainage that keyline work should account for.

Docs: https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer
"""

import requests
from typing import Optional

NHD_BASE = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"
FLOWLINE_LAYER = 6   # Flowline - Large Scale (streams, rivers, canals)
WATERBODY_LAYER = 12  # Waterbody - Large Scale (ponds, lakes, reservoirs)


def _bounding_box(
    coordinates: list[tuple[float, float]], buffer_meters: float = 150
) -> tuple[float, float, float, float]:
    """
    Returns (min_lon, min_lat, max_lon, max_lat) for a list of (lon, lat)
    points, expanded outward by buffer_meters in every direction.

    Why buffer at all: a bounding box drawn tight around a property
    boundary has zero margin, so a stream or pond sitting just outside
    the drawn shape (e.g. along a property line, which is a very common
    real-world case) gets missed entirely. A ~150m buffer catches nearby
    features that are clearly relevant to the property without pulling
    in water features from unrelated, distant land.
    """
    lons = [pt[0] for pt in coordinates]
    lats = [pt[1] for pt in coordinates]

    min_lat, max_lat = min(lats), max(lats)
    avg_lat = (min_lat + max_lat) / 2

    # Rough degree-per-meter conversion. Latitude is ~consistent everywhere;
    # longitude degrees shrink as you move away from the equator, so it's
    # scaled by the cosine of latitude. Good enough at this scale — we're
    # not doing survey-grade math, just avoiding an overly tight box.
    meters_per_degree_lat = 111_320
    meters_per_degree_lon = 111_320 * abs(_cos_degrees(avg_lat))

    lat_buffer = buffer_meters / meters_per_degree_lat
    lon_buffer = buffer_meters / meters_per_degree_lon

    return (
        min(lons) - lon_buffer,
        min_lat - lat_buffer,
        max(lons) + lon_buffer,
        max_lat + lat_buffer,
    )


def _cos_degrees(degrees: float) -> float:
    """Cosine of an angle given in degrees (Python's math.cos expects radians)."""
    import math
    return math.cos(math.radians(degrees))


def _query_layer(layer_id: int, bbox: tuple[float, float, float, float]) -> list[dict]:
    """
    Queries a single NHD layer for features intersecting the given
    bounding box. Returns raw GeoJSON-style feature dicts.
    """
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

    url = f"{NHD_BASE}/{layer_id}/query"
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()
    return data.get("features", [])


def get_water_features_for_boundary(
    boundary_coordinates: list[tuple[float, float]], buffer_meters: float = 150
) -> dict:
    """
    Given a property boundary (list of (longitude, latitude) points),
    returns nearby/intersecting streams and standing water bodies.

    buffer_meters controls how far outside the exact drawn boundary to
    also search — useful since water features (like a stream running
    along a property line) often sit just outside the exact shape someone
    drew. Default 150m catches "on or near the property" without pulling
    in unrelated water from farther away.

    Note: like elevation_data.py's grid function, this queries a bounding
    BOX, not a precise clip to the exact shape — for an irregular property
    this may include a bit more area than the exact boundary+buffer would.
    Good enough to know "is there a stream near this property," which is
    what matters at this stage.

    Returns a dict:
        {
            'streams': [ {name, feature_code, geometry}, ... ],
            'water_bodies': [ {name, feature_code, geometry}, ... ]
        }
    """
    bbox = _bounding_box(boundary_coordinates, buffer_meters=buffer_meters)

    stream_features = _query_layer(FLOWLINE_LAYER, bbox)
    waterbody_features = _query_layer(WATERBODY_LAYER, bbox)

    streams = [
        {
            "name": f["properties"].get("gnis_name") or "Unnamed stream",
            "feature_code": f["properties"].get("fcode"),
            "geometry": f["geometry"],
        }
        for f in stream_features
    ]

    water_bodies = [
        {
            "name": f["properties"].get("gnis_name") or "Unnamed water body",
            "feature_code": f["properties"].get("fcode"),
            "geometry": f["geometry"],
        }
        for f in waterbody_features
    ]

    return {"streams": streams, "water_bodies": water_bodies}


def summarize_water_features(features: dict) -> str:
    """Plain-language summary of what water features were found nearby."""
    streams = features["streams"]
    water_bodies = features["water_bodies"]

    if not streams and not water_bodies:
        return (
            "No mapped streams or standing water found in or near this "
            "boundary. This doesn't necessarily mean the land is dry — "
            "small seasonal drainages and springs often aren't captured "
            "in NHD — but there's no major surface water feature to "
            "design around here."
        )

    lines = []

    if streams:
        lines.append(f"Streams/waterways found: {len(streams)}")
        for s in streams[:5]:
            lines.append(f"  - {s['name']}")

    if water_bodies:
        lines.append(f"\nPonds/lakes found: {len(water_bodies)}")
        for w in water_bodies[:5]:
            lines.append(f"  - {w['name']}")

    return "\n".join(lines)


if __name__ == "__main__":
    # Test case: the user's own drawn property boundary
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Fetching water features for property boundary...\n")

    try:
        features = get_water_features_for_boundary(property_boundary)
        print(summarize_water_features(features))
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
