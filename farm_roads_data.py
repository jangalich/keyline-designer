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

get_road_exclusion_union_utm() below is a second consumer of the same
fetch: production_area.py's production-zone eligibility pipeline uses it
as a hard, cell-level exclusion (existing road right-of-way isn't
production ground), same shapely-polygon-union-plus-per-cell-.contains()
pattern hydric soil already uses there — see that function's own
docstring. Both consumers (this proximity-scoring one, and that hard-
exclusion one) share the SAME underlying incomplete-by-construction
caveat above; that's the "known-incomplete signal" this module already
was, not a newly-closed detection gap the way the canopy/HAG gate was.

Docs: https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer

REAL BUG, FOUND LIVE (fixed here): this module originally queried layer 0
("Small-Scale"), which is a CONTAINER/GROUP layer in the MapServer's
layer hierarchy, not a real, queryable Feature Layer. Querying it
directly returns an ArcGIS error embedded in the response body
(`{"error": {"code": 400, "message": "Invalid or missing input
parameters."}}`) while the HTTP status is still 200 OK — `raise_for_
status()` never catches this, since the HTTP layer reports success while
the ArcGIS payload reports failure. The old code only checked
`data.get("features", [])`, so this error silently read as "zero roads
found," identically at every buffer size (150m/300m/500m/800m all tested
live, all zero) — which in hindsight should have been the tell, since a
genuine "no nearby road" case wouldn't behave identically regardless of
how far out the search radius grows. Confirmed against the live service:
querying layer 32 ("Local Roads") with the same bounding box returns real
features immediately, including the road this reference property
actually fronts. Real road data was reachable this whole time; the code
was just asking the wrong layer for it. Fixed two ways, not one:
  1. ROAD_LAYERS below now points at real, confirmed Feature Layers
     (30/31/32), queried together and merged — not a single layer ID,
     so a property fronting a road classified under a different one of
     these isn't silently missed the same way.
  2. _query_road_layer() now checks the JSON body for an "error" key even
     on HTTP 200 and raises explicitly — this exact failure mode (a
     bad/wrong layer ID masquerading as "zero results") can't hide
     silently again, for these layers or any future ones.
"""

import json
import math
import time

import requests
from rasterio.warp import transform_geom
from shapely.geometry import shape
from shapely.ops import unary_union

from feature_schema import CONFIDENCE_MEDIUM, make_feature, make_feature_collection

TRANSPORTATION_BASE = "https://carto.nationalmap.gov/arcgis/rest/services/transportation/MapServer"

# Real, queryable road-FEATURE layers (each confirmed live to be an actual
# Feature Layer via its own `<TRANSPORTATION_BASE>/<id>?f=json` endpoint,
# not a container/group layer like the old, wrong layer 0 — see module
# docstring) — queried together and merged, rather than a single layer ID,
# so a property fronting a road classified under any one of these isn't
# missed. Deliberately excludes layers 8/9 ("Roads," small-scale
# 10M/1M — much coarser than a farm-property use case needs).
#   30: Secondary Highways
#   31: Local Connecting Roads
#   32: Local Roads (this is where a typical rural county road, e.g. the
#       reference property's own frontage road, is classified)
# CONFIGURABLE — re-verify against the MapServer's own layer listing
# (`{TRANSPORTATION_BASE}?f=json`) if USGS ever restructures this service;
# don't assume a layer is a real feature layer from its name alone (that
# assumption is exactly how the original bug happened).
ROAD_LAYERS = [30, 31, 32]

FARM_ROAD_CONFIDENCE_NOTES = (
    "Road geometry is USGS National Map Transportation data — public "
    "road/right-of-way sources, not a survey of this specific property. "
    "Private farm tracks, driveways, or internal access lanes that were "
    "never captured in a public transportation dataset will not appear "
    "here; treat an absence of nearby roads as 'no mapped public road,' "
    "not necessarily 'no access.'"
)

# How far past each fetched road line's own mapped geometry the hard
# production-zone exclusion (get_road_exclusion_union_utm() below) extends.
# Default 0.0 -- we don't yet know whether a buffer beyond the road
# geometry itself is needed; this is deliberately easy to tune once
# visually validated against a real property, same as every other buffer
# constant in this pipeline (canopy_height_data.TREE_ROOT_ZONE_BUFFER_
# METERS, production_area.PRODUCTION_BOUNDARY_SETBACK_METERS). NOTE: at
# the 0.0 default, buffering a LineString produces a zero-area geometry,
# so the exclusion union is effectively empty and this gate is a no-op
# until a real, nonzero value is set here. CONFIGURABLE.
ROAD_EXCLUSION_BUFFER_METERS = 0.0


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
    layer_id: int, bbox: tuple[float, float, float, float], max_retries: int = 2
) -> list[dict]:
    """Same retry-with-increasing-timeout pattern as hydrology_data.py's
    _query_layer — USGS's map services are occasionally slow, not down.

    Also checks the JSON body itself for an "error" key even when the
    HTTP status is 200 — ArcGIS reports some failures (e.g. querying a
    non-queryable container/group layer) this way, and raise_for_status()
    alone never catches it (see module docstring: this exact failure mode
    is how the old ROAD_LAYER=0 bug silently read as "zero roads found"
    for every request). That kind of error is NOT transient — retrying
    the same bad layer/parameters won't fix it — so it's raised
    immediately rather than folded into the retry loop above, which is
    for genuine network/timeout errors only.
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

    url = f"{TRANSPORTATION_BASE}/{layer_id}/query"
    last_error = None

    for attempt in range(max_retries + 1):
        timeout = 30 + (attempt * 30)
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise last_error
        else:
            if "error" in data:
                error_info = data["error"]
                raise RuntimeError(
                    f"ArcGIS query to transportation layer {layer_id} returned an error despite "
                    f"HTTP 200 (code {error_info.get('code', 'unknown')}): "
                    f"{error_info.get('message', error_info)} — likely querying a non-queryable "
                    f"container/group layer (this is exactly how the old ROAD_LAYER=0 bug silently "
                    f"returned zero roads for every request; see module docstring). Verify layer "
                    f"{layer_id} is a real Feature Layer via {TRANSPORTATION_BASE}/{layer_id}?f=json "
                    f"before using it."
                )
            return data.get("features", [])


def _deduplicate_road_features(features: list[dict]) -> list[dict]:
    """
    ROAD_LAYERS are distinct road classifications (secondary highway vs.
    connecting road vs. local road) and shouldn't normally return the
    same real segment twice, but this is a cheap safety net against that
    regardless — exact-geometry duplicates (identical coordinates) are
    dropped, keeping the first occurrence. Keyed on the geometry itself
    (not a name/id field, whose presence/naming can vary by layer) so
    this doesn't depend on any particular schema assumption holding
    across all three layers.
    """
    seen_geometries = set()
    deduped = []
    for feature in features:
        geometry = feature.get("geometry")
        key = json.dumps(geometry, sort_keys=True) if geometry else None
        if key in seen_geometries:
            continue
        seen_geometries.add(key)
        deduped.append(feature)
    return deduped


def get_farm_roads_for_boundary(
    boundary_coordinates: list[tuple[float, float]], buffer_meters: float = 150
) -> list[dict]:
    """
    Returns nearby/intersecting road segments as a list of
    {'name', 'geometry'} dicts, geometry already GeoJSON LineString/
    MultiLineString in WGS84 (lon/lat) — the same raw shape
    hydrology_data.get_water_features_for_boundary uses for streams.

    Queries every layer in ROAD_LAYERS and merges the results (see module
    docstring for why this is multiple layers, not one). Each layer's
    query degrades independently: a real failure (network or the ArcGIS-
    error-on-HTTP-200 case _query_road_layer() detects) on ONE layer
    doesn't discard data successfully fetched from the others — only if
    EVERY layer fails does this raise, so callers' existing "the whole
    fetch failed, fall back" handling still triggers on a genuine total
    outage, not on one flaky/misconfigured layer among several working
    ones.
    """
    bbox = _bounding_box(boundary_coordinates, buffer_meters=buffer_meters)

    all_features = []
    layer_errors = []
    for layer_id in ROAD_LAYERS:
        try:
            all_features.extend(_query_road_layer(layer_id, bbox))
        except Exception as e:
            layer_errors.append((layer_id, e))

    if len(layer_errors) == len(ROAD_LAYERS):
        # every layer failed -- a real, total fetch failure, not a
        # partial-data case; raise so this behaves the same way a single-
        # layer fetch failure always has for callers' own try/except.
        raise layer_errors[0][1]

    for layer_id, error in layer_errors:
        print(f"  farm_roads_data: layer {layer_id} query failed ({error}), continuing with the other layers.")

    road_features = _deduplicate_road_features(all_features)

    return [
        {
            "name": f["properties"].get("name") or f["properties"].get("fullname") or "Unnamed road",
            "geometry": f["geometry"],
        }
        for f in road_features
        if f.get("geometry") is not None
    ]


def get_road_exclusion_union_utm(
    boundary_coordinates: list[tuple[float, float]],
    dem: dict,
    buffer_meters: float = ROAD_EXCLUSION_BUFFER_METERS,
):
    """
    Real road geometry (get_farm_roads_for_boundary()'s own fetch, WGS84
    LineString/MultiLineString), each line reprojected into dem['crs'] and
    buffered by buffer_meters, unioned into a single polygon -- the hard
    production-zone exclusion input production_area.compute_step1_
    eligible_cells() tests each eligible cell's own CENTER POINT against.

    Same shapely-polygon-union-plus-per-cell-.contains() pattern
    production_area._fetch_disqualifying_soil_union() already uses for
    hydric soil, NOT the raster-dilation approach canopy_height_data.py
    uses for the tree-root-zone gate -- road data here is already clean
    vector geometry fetched directly from USGS, not derived from a raster
    source the way HAG is, so there is no reason to rasterize it (and
    doing so would only risk reintroducing the sliver-fragmentation
    concern the raster-based gates exist specifically to avoid on their
    own, raster-sourced inputs).

    Returns None if no roads were found near this boundary -- the common,
    clean case, not an error, same "checked and genuinely nothing there"
    convention _fetch_disqualifying_soil_union() uses. A road fetch
    failure (network exhausted, every ROAD_LAYERS query failing) is left
    to propagate up UNCAUGHT -- this function does no exception handling
    of its own; callers degrade gracefully the same way they already do
    for the hydric-soil fetch (see production_area._ROAD_CHECK_UNCHECKED).
    """
    roads = get_farm_roads_for_boundary(boundary_coordinates)
    if not roads:
        return None

    buffered_pieces = [
        shape(transform_geom("EPSG:4326", dem["crs"], road["geometry"])).buffer(buffer_meters) for road in roads
    ]
    union = unary_union(buffered_pieces)
    return union if not union.is_empty else None


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

        geojson = get_farm_roads_geojson(property_boundary)
        from feature_schema import validate_feature_collection

        validate_feature_collection(geojson)
        print(f"\nGeoJSON FeatureCollection: {len(geojson['features'])} feature(s), schema-valid.")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map transportation service — not a fully sandboxed environment."
        )
