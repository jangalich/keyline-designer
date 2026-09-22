"""
nfhl_data.py

FEMA'S NATIONAL FLOOD HAZARD LAYER for the site data report's Water &
hydrology section: whether the parcel's county has a digital flood map at
all, the flood zones on and around the parcel, and the FIRM panel and its
effective date.

    get_flood_hazard_for_boundary(boundary)  -> the raw responses (a fetch)
    parse_flood_hazard(raw)                  -> the parsed block

THREE QUERIES against hazards.fema.gov's NFHL MapServer (step 0, verified
live for the reference parcel):

  layer 0, NFHL Availability -- the parcel polygon, attributes only. A
      STUDY_ID here means the county has a digital FIRM in the NFHL; no
      row means IT DOES NOT, and the section says "no digital flood map"
      in those words. Large parts of rural America have no detailed
      study, and "not mapped" must never read as "not at risk".
  layer 28, Flood Hazard Zones -- the parcel's bbox plus
      NFHL_FETCH_BUFFER_METERS, geometry in the parcel's UTM zone,
      generalised at NFHL_GEOMETRY_OFFSET_METERS. A Zone X polygon is
      county-wide (29.2 MB ungeneralised on the reference parcel, 427 KB
      at 5 m); the buffer is so a Zone A along the stream next door
      draws on the map, as the NHD fetch's 150 m does.
  layer 3, FIRM Panels -- the parcel polygon, attributes only: the
      panel number, its effective date and scale for the footer.

THE HOST DROPS CONNECTIONS. In step 0 hazards.fema.gov failed the TLS
handshake four times in a row once and answered every other time; every
query here runs in the same progressive-timeout retry loop as
hydrology_data._query_layer(), attempts published through fetch_attempts.

TERMS, as found. The map service's own item description carries an
EMPTY licenseInfo and accessInformation (its /info/itemInfo, step 0 /
phase 1); the NFHL page states no licence (it says only that FEMA provides
the data to support the National Flood Insurance Program and that the
NFHL covers over 90% of the U.S. population); FEMA's website-information
page says "Most material on FEMA.gov is free of copyright and may be
copied and distributed without permission. Citation of FEMA.gov and a
link back is much appreciated." The NFHL database's own FGDC record ships
inside the downloadable geodatabase and was not reachable from the
service. The methods note records exactly that (NFHL_TERMS_BASIS): a U.S.
federal work with no stated restriction, citation appreciated.
"""

import json
from datetime import date, datetime, timezone
from typing import Optional

import requests
from shapely.geometry import box, shape
from shapely.ops import unary_union
from shapely.validation import make_valid

import fetch_attempts
from dem_data import dem_window_bounds

NFHL_BASE = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
LAYER_AVAILABILITY = 0
LAYER_FLOOD_ZONES = 28
LAYER_FIRM_PANELS = 3

NFHL_FETCH_BUFFER_METERS = 150.0
NFHL_GEOMETRY_OFFSET_METERS = 5.0

ZONE_FIELDS = "FLD_ZONE,ZONE_SUBTY,SFHA_TF,STUDY_TYP,STATIC_BFE,DFIRM_ID,FLD_AR_ID"
PANEL_FIELDS = "FIRM_PAN,EFF_DATE,SCALE,PANEL_TYP,DFIRM_ID"

# What was found where, for the methods note (phase 1).
NFHL_TERMS_BASIS = (
    "U.S. federal work. The NFHL map service's item description carries no licence or access statement; "
    "FEMA's website information page states that most material on FEMA.gov is free of copyright and may be "
    "copied and distributed without permission, citation appreciated. No use-constraint statement specific to "
    "the NFHL was found; the database's FGDC record ships with the downloadable geodatabase."
)

NFHL_CITATION = (
    "Federal Emergency Management Agency, National Flood Hazard Layer (NFHL), served by the "
    "FEMA Flood Map Service Center's NFHL map service."
)


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


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


def _parcel_geometry(boundary_coordinates: list) -> str:
    return json.dumps({"rings": [[[float(lon), float(lat)] for lon, lat in boundary_coordinates]],
                       "spatialReference": {"wkid": 4326}})


def _polygon_attribute_query(layer: int, boundary_coordinates: list, out_fields: str) -> dict:
    return _get(f"{NFHL_BASE}/{layer}/query", {
        "geometry": _parcel_geometry(boundary_coordinates),
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outFields": out_fields,
        "returnGeometry": "false",
        "f": "json",
    })


def _zone_query(window: dict) -> dict:
    minx, miny, maxx, maxy = window["bbox"]
    return _get(f"{NFHL_BASE}/{LAYER_FLOOD_ZONES}/query", {
        "geometry": f"{minx},{miny},{maxx},{maxy}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": window["epsg"],
        "outSR": window["epsg"],
        "outFields": ZONE_FIELDS,
        "maxAllowableOffset": NFHL_GEOMETRY_OFFSET_METERS,
        "geometryPrecision": 1,
        "f": "geojson",
    })


@fetch_attempts.publishes
def get_flood_hazard_for_boundary(boundary_coordinates: list, buffer_meters: float = NFHL_FETCH_BUFFER_METERS) -> dict:
    """
    THE RAW RESPONSES -- what a fixture stores:

        {'window': {'bbox', 'epsg', 'crs'},
         'availability': <layer 0 JSON>, 'zones': <layer 28 GeoJSON>,
         'panels': <layer 3 JSON>}

    Availability first: when the parcel has no digital FIRM the zone and
    panel queries are still made (they answer empty, cheaply) so a
    fixture holds every response the parser reads.
    """
    window = dem_window_bounds(boundary_coordinates, buffer_meters=buffer_meters)
    window = {"bbox": window["bbox"], "epsg": window["epsg"], "crs": window["crs"]}
    availability = _polygon_attribute_query(LAYER_AVAILABILITY, boundary_coordinates, "STUDY_ID")
    zones = _zone_query(window)
    panels = _polygon_attribute_query(LAYER_FIRM_PANELS, boundary_coordinates, PANEL_FIELDS)
    return {"window": window, "availability": availability, "zones": zones, "panels": panels}


def _epoch_ms_to_date(value) -> Optional[date]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def parse_flood_hazard(raw: dict) -> dict:
    """
    get_flood_hazard_for_boundary()'s responses -> the block:

        {'window': {...},
         'available': bool,               # a digital FIRM covers the parcel
         'study_ids': [str, ...],
         'zones': [{'zone', 'subtype', 'sfha', 'study_type', 'static_bfe',
                    'dfirm_id', 'fld_ar_id', 'geometry_utm'}],
         'panels': [{'firm_pan', 'effective_on': date | None, 'scale',
                     'panel_type', 'dfirm_id'}]}

    Zone geometry is in the window's UTM zone, clipped to the window and
    made valid. `sfha` is True for a Special Flood Hazard Area (the 1%
    annual-chance floodplain); `static_bfe` is the base flood elevation
    where a detailed study set one, else None (the service's -9999).
    """
    window = raw["window"]
    minx, miny, maxx, maxy = window["bbox"]
    window_polygon = box(minx, miny, maxx, maxy)
    study_ids = []
    for feature in (raw.get("availability") or {}).get("features", []):
        study = (feature.get("attributes") or {}).get("STUDY_ID")
        if study and study not in study_ids:
            study_ids.append(study)
    zones = []
    for feature in (raw.get("zones") or {}).get("features", []):
        props = feature.get("properties") or {}
        geometry = None
        if feature.get("geometry"):
            clipped = make_valid(shape(feature["geometry"])).intersection(window_polygon)
            parts = [g for g in getattr(clipped, "geoms", [clipped]) if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
            geometry = unary_union(parts) if parts else None
        bfe = props.get("STATIC_BFE")
        zones.append({
            "zone": props.get("FLD_ZONE"),
            "subtype": props.get("ZONE_SUBTY") or None,
            "sfha": str(props.get("SFHA_TF") or "").upper() == "T",
            "study_type": props.get("STUDY_TYP"),
            "static_bfe": None if bfe is None or float(bfe) <= -9999 else float(bfe),
            "dfirm_id": props.get("DFIRM_ID"),
            "fld_ar_id": props.get("FLD_AR_ID"),
            "geometry_utm": geometry,
        })
    panels = []
    for feature in (raw.get("panels") or {}).get("features", []):
        attributes = feature.get("attributes") or {}
        panels.append({
            "firm_pan": attributes.get("FIRM_PAN"),
            "effective_on": _epoch_ms_to_date(attributes.get("EFF_DATE")),
            "scale": attributes.get("SCALE"),
            "panel_type": attributes.get("PANEL_TYP"),
            "dfirm_id": attributes.get("DFIRM_ID"),
        })
    return {"window": window, "available": bool(study_ids), "study_ids": study_ids, "zones": zones, "panels": panels}


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    block = parse_flood_hazard(get_flood_hazard_for_boundary(REAL_BOUNDARY))
    print("available:", block["available"], block["study_ids"])
    for z in block["zones"]:
        print(z["zone"], z["subtype"], "sfha", z["sfha"], z["study_type"], "area in window ac",
              None if z["geometry_utm"] is None else round(z["geometry_utm"].area / 4046.8564224, 2))
    print("panels:", block["panels"])
