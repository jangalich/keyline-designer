"""
nwi_data.py

USFWS NATIONAL WETLANDS INVENTORY for the site data report's Water &
hydrology section: the mapped wetlands on and around a parcel, with the
imagery vintage of the mapping project that drew them.

    get_wetlands_for_boundary(boundary)   -> the raw responses (a fetch)
    parse_wetlands(raw)                   -> the parsed block

THE WINDOW is the parcel's bounding box plus NWI_FETCH_BUFFER_METERS, in
the parcel's UTM zone -- dem_data.dem_window_bounds() at that buffer, so
"adjacent" here means the same 150 m hydrology_data.py's NHD fetch means.
A wetland on the parcel and a wetland 60 m from it are both real
findings for a section that describes the water that is there.

THE FETCH IS TWO-STAGE, AND IT HAS TO BE. The Wetlands layer is joined to
the Cowardin code table on the server (every field arrives prefixed
"Wetlands." or "NWI_Wetland_Codes."), and a riverine polygon can be one
feature for a whole stream network: on the reference parcel the R2UBH
feature 85 m from the boundary is 44,740 acres and 1,024,370 vertices,
42.7 MB as GeoJSON (step 0, verified live). So:

  1. ATTRIBUTES ONLY for every feature intersecting the window
     (returnGeometry=false; 1.1 KB on the reference parcel).
  2. GEOMETRY BY OBJECTID at NWI_FINE_OFFSET_METERS for every feature
     under NWI_LARGE_FEATURE_ACRES (8.4 KB for the reference parcel's
     four palustrine polygons).
  3. GEOMETRY BY OBJECTID at NWI_COARSE_OFFSET_METERS for the oversize
     features (650 KB for the riverine network at 10 m), clipped to the
     window on receipt and run through make_valid -- the generalised
     polygon came back self-intersecting at 5 m.

A generalisation offset moves a line by up to that distance, so a coarse
feature's acreage on the parcel is measured from a boundary that may be
10 m out, and the parsed feature says which stage drew it
(`geometry_detail`). Quantized "view mode" was tried and does not clip
(6.9 MB); it is not used.

THE VINTAGE. The Data_Source service answers, for a point, which mapping
project covers it and the year and kind of imagery it interpreted -- the
reference parcel is "PA NWI Research Methods", 2023 colour-infrared at
1 ft. That is the one fact the caption's "mapped from imagery
interpretation" needs a date on, so it is fetched with the wetlands
(one point query, at the parcel centroid) and carried on the block.

TERMS, from the record where they live. The FGDC metadata record the
Data_Source service links (FWS_Wetlands_Project_Metadata.xml) states
Access_Constraints "None" and Use_Constraints "None. Acknowledgement of
the U.S. Fish and Wildlife Service and (or) the National Wetlands
Inventory would be appreciated in products derived from these data", with
a no-warranty Distribution_Liability. The service carries no copyright
text and the Wetlands Mapper page states no licence; the methods note
records the metadata's own words (NWI_USE_CONSTRAINTS) and the disclaimer
wording the page does state: mapped by a biological definition, no
attempt to define jurisdiction, not the presence or absence of wetlands
covered under law.

RETRIES follow hydrology_data._query_layer(): progressive timeouts,
attempts published through fetch_attempts.
"""

import json

import requests
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon, box, shape
from shapely.validation import make_valid

import fetch_attempts
from dem_data import dem_window_bounds

NWI_BASE = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services"
NWI_WETLANDS_QUERY = f"{NWI_BASE}/Wetlands/MapServer/0/query"
NWI_DATA_SOURCE_QUERY = f"{NWI_BASE}/Data_Source/MapServer/0/query"

NWI_FETCH_BUFFER_METERS = 150.0
# A feature at or above this many acres is a network or a lake, not a
# wetland the parcel could hold; its geometry is fetched coarsely.
NWI_LARGE_FEATURE_ACRES = 1000.0
NWI_FINE_OFFSET_METERS = 1.0
NWI_COARSE_OFFSET_METERS = 10.0

FIELD_PREFIX = "Wetlands."
ATTRIBUTE_FIELDS = ("OBJECTID", "ATTRIBUTE", "WETLAND_TYPE", "ACRES")
CODE_FIELDS = ("SYSTEM_NAME", "CLASS_NAME", "WATER_REGIME_NAME")

DETAIL_FINE = "fine"
DETAIL_COARSE = "coarse"

# The FGDC metadata record's own words (step 0 / phase 1, fetched from
# documentst.ecosphere.fws.gov/wetlands/data/metadata/FWS_Wetlands_Project_Metadata.xml).
NWI_METADATA_URL = "https://documentst.ecosphere.fws.gov/wetlands/data/metadata/FWS_Wetlands_Project_Metadata.xml"
NWI_ACCESS_CONSTRAINTS = "None"
NWI_USE_CONSTRAINTS = (
    "None. Acknowledgement of the U.S. Fish and Wildlife Service and (or) the National Wetlands Inventory "
    "would be appreciated in products derived from these data"
)

NWI_CITATION = (
    "U.S. Fish and Wildlife Service, National Wetlands Inventory, Wetlands geodatabase, "
    "served by the Wetlands Mapper map service (Cowardin et al. 1979 classification)."
)


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


def fetch_window(boundary_coordinates: list, buffer_meters: float = NWI_FETCH_BUFFER_METERS) -> dict:
    """The parcel's bbox plus the buffer, in its UTM zone:
    {'bbox': (minx, miny, maxx, maxy), 'epsg', 'crs'}. dem_window_bounds()
    at this buffer, so the window is computed the way the DEM's is."""
    window = dem_window_bounds(boundary_coordinates, buffer_meters=buffer_meters)
    return {"bbox": window["bbox"], "epsg": window["epsg"], "crs": window["crs"]}


def _get(url: str, params: dict, max_retries: int = 2) -> dict:
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        timeout = 30 + attempt * 30
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "error" in data:
                raise requests.exceptions.HTTPError(f"ArcGIS error: {data['error']}")
            return data
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def _out_fields() -> str:
    return ",".join([FIELD_PREFIX + f for f in ATTRIBUTE_FIELDS] + ["NWI_Wetland_Codes." + f for f in CODE_FIELDS])


def _attribute_query(window: dict) -> dict:
    minx, miny, maxx, maxy = window["bbox"]
    return _get(NWI_WETLANDS_QUERY, {
        "geometry": f"{minx},{miny},{maxx},{maxy}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": window["epsg"],
        "outFields": _out_fields(),
        "returnGeometry": "false",
        "f": "json",
    })


def _geometry_query(object_ids: list, window: dict, offset_meters: float) -> dict:
    return _get(NWI_WETLANDS_QUERY, {
        "objectIds": ",".join(str(i) for i in object_ids),
        "outSR": window["epsg"],
        "outFields": FIELD_PREFIX + "OBJECTID",
        "maxAllowableOffset": offset_meters,
        "geometryPrecision": 1,
        "f": "geojson",
    })


def _project_query(boundary_coordinates: list) -> dict:
    lons = [p[0] for p in boundary_coordinates]
    lats = [p[1] for p in boundary_coordinates]
    point = {"x": sum(lons) / len(lons), "y": sum(lats) / len(lats), "spatialReference": {"wkid": 4326}}
    return _get(NWI_DATA_SOURCE_QUERY, {
        "geometry": json.dumps(point),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "outFields": "PROJECT_NAME,IMAGE_YR,IMAGE_DATE,SOURCE_TYPE,EMULSION,ALL_SCALES,STATUS",
        "returnGeometry": "false",
        "f": "json",
    })


def _strip(attributes: dict) -> dict:
    return {key.split(".", 1)[-1]: value for key, value in (attributes or {}).items()}


@fetch_attempts.publishes
def get_wetlands_for_boundary(boundary_coordinates: list, buffer_meters: float = NWI_FETCH_BUFFER_METERS) -> dict:
    """
    THE RAW RESPONSES of the two-stage fetch for this boundary's window,
    plus the mapping-project row -- exactly what a fixture stores:

        {'window': {'bbox', 'epsg', 'crs'},
         'attributes': <stage 1 JSON>,
         'fine': <stage 2 GeoJSON or None>,
         'coarse': <stage 3 GeoJSON or None>,
         'project': <Data_Source JSON>}

    Raises requests.RequestException when the service does not answer;
    the report layer records that as unavailable.
    """
    window = fetch_window(boundary_coordinates, buffer_meters)
    attributes = _attribute_query(window)
    small, large = [], []
    for feature in attributes.get("features", []):
        row = _strip(feature.get("attributes"))
        acres = row.get("ACRES")
        (large if acres is not None and float(acres) >= NWI_LARGE_FEATURE_ACRES else small).append(int(row["OBJECTID"]))
    fine = _geometry_query(small, window, NWI_FINE_OFFSET_METERS) if small else None
    coarse = _geometry_query(large, window, NWI_COARSE_OFFSET_METERS) if large else None
    project = _project_query(boundary_coordinates)
    return {"window": window, "attributes": attributes, "fine": fine, "coarse": coarse, "project": project}


def _clipped_valid(geometry, window_polygon: Polygon):
    valid = make_valid(geometry)
    clipped = valid.intersection(window_polygon)
    if clipped.is_empty:
        return None
    parts = [clipped] if clipped.geom_type in ("Polygon", "MultiPolygon") else [
        g for g in getattr(clipped, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon")
    ]
    if not parts:
        return None
    from shapely.ops import unary_union

    return unary_union(parts)


def parse_wetlands(raw: dict) -> dict:
    """
    get_wetlands_for_boundary()'s responses -> the block the section
    reads:

        {'window': {...},
         'features': [{'objectid', 'code', 'wetland_type', 'system',
                       'wetland_class', 'water_regime', 'acres_mapped',
                       'geometry_utm', 'geometry_detail'}],
         'project': {'name', 'image_year', 'image_date', 'source_type',
                     'emulsion', 'scales', 'status'} | None}

    Geometry is in the window's UTM zone, clipped to the window and made
    valid; `acres_mapped` is NWI's own acreage for the whole feature,
    which the section prints beside the part inside the window.
    """
    window = raw["window"]
    minx, miny, maxx, maxy = window["bbox"]
    window_polygon = box(minx, miny, maxx, maxy)
    rows = {}
    for feature in raw.get("attributes", {}).get("features", []):
        row = _strip(feature.get("attributes"))
        rows[int(row["OBJECTID"])] = row
    geometries = {}
    for detail, key in ((DETAIL_FINE, "fine"), (DETAIL_COARSE, "coarse")):
        for feature in (raw.get(key) or {}).get("features", []):
            props = _strip(feature.get("properties"))
            objectid = int(props.get("OBJECTID", feature.get("id")))
            geometry = _clipped_valid(shape(feature["geometry"]), window_polygon)
            geometries[objectid] = (geometry, detail)
    features = []
    for objectid, row in rows.items():
        geometry, detail = geometries.get(objectid, (None, None))
        features.append({
            "objectid": objectid,
            "code": row.get("ATTRIBUTE"),
            "wetland_type": row.get("WETLAND_TYPE"),
            "system": row.get("SYSTEM_NAME"),
            "wetland_class": row.get("CLASS_NAME"),
            "water_regime": row.get("WATER_REGIME_NAME"),
            "acres_mapped": float(row["ACRES"]) if row.get("ACRES") is not None else None,
            "geometry_utm": geometry,
            "geometry_detail": detail,
        })
    project = None
    project_rows = (raw.get("project") or {}).get("features") or []
    if project_rows:
        attributes = project_rows[0].get("attributes") or {}
        project = {
            "name": attributes.get("PROJECT_NAME"),
            "image_year": attributes.get("IMAGE_YR"),
            "image_date": attributes.get("IMAGE_DATE"),
            "source_type": attributes.get("SOURCE_TYPE"),
            "emulsion": attributes.get("EMULSION"),
            "scales": attributes.get("ALL_SCALES"),
            "status": attributes.get("STATUS"),
        }
    return {"window": window, "features": features, "project": project}


def boundary_polygon_in_window(boundary_coordinates: list, window: dict) -> Polygon:
    """The parcel in the window's CRS."""
    xs, ys = warp_transform("EPSG:4326", window["crs"], [p[0] for p in boundary_coordinates], [p[1] for p in boundary_coordinates])
    return Polygon(zip(xs, ys))


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    raw = get_wetlands_for_boundary(REAL_BOUNDARY)
    block = parse_wetlands(raw)
    parcel = boundary_polygon_in_window(REAL_BOUNDARY, block["window"])
    print("project:", block["project"])
    for f in block["features"]:
        g = f["geometry_utm"]
        print(f["code"], f["wetland_type"], f"{f['acres_mapped']:.2f} ac mapped", f["geometry_detail"],
              "distance", None if g is None else round(parcel.distance(g), 1), "m")
