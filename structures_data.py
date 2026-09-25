"""
structures_data.py

THE BUILDINGS ON AND AROUND THE PARCEL, for the site overview: building
footprints from FEMA's USA Structures inventory, as Esri serves it.

    get_structures_for_boundary(boundary)  -> the raw response (a fetch)
    parse_structures(raw)                  -> the parsed block

THE SOURCE, AND WHY THIS ONE. USA Structures is the national inventory
of structures larger than 450 sq ft built by FEMA's Response Geospatial
Office, Oak Ridge National Laboratory and the USGS. Branch 17's step 0
compared it with Microsoft's footprints and Overture's, and chose it on
terms: the only queryable copy (Esri Living Atlas item
0ec8512ad21e4bb987d7e848d14e7e24) is licensed CC BY 4.0 -- attribution,
no share-alike. Overture's buildings theme and Microsoft's US release are
ODbL, and NO ODbL SOURCE ENTERS THIS PRODUCT: that a printed report is a
Produced Work is probably right, but "probably" is not a position for a
paid product when a permissive source exists. The attribution is printed
(ATTRIBUTION) wherever a figure from it is.

WHAT IT MISSES, SAID ON THE PAGE. Structures under 450 sq ft -- most
sheds, small barns, well houses -- are not in the inventory. Each State
is produced from imagery of its own date (IMAGE_DATE, PROD_DATE); the
reference parcel's Pennsylvania rows are from 2019 imagery, produced in
2020, and those dates are the vintage the table prints.

ONE ENVELOPE QUERY: the parcel's bounding box plus FETCH_BUFFER_METERS,
the same buffer every other near-parcel layer uses, so the nearest
building outside the line is found as well as any inside it. The service
caps a response at 2,000 records; a response that says it was cut short
raises rather than undercounting.
"""

from datetime import datetime, timezone
from typing import Optional

import requests

import fetch_attempts
from hydrology_data import _bounding_box

STRUCTURES_QUERY = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
                    "USA_Structures_View/FeatureServer/0/query")
ITEM_URL = "https://www.arcgis.com/home/item.html?id=0ec8512ad21e4bb987d7e848d14e7e24"
FETCH_BUFFER_METERS = 150.0
MIN_STRUCTURE_SQFT = 450

OUT_FIELDS = "BUILD_ID,OCC_CLS,PRIM_OCC,SQFEET,IMAGE_DATE,PROD_DATE,SOURCE,VAL_METHOD"

ATTRIBUTION = "USA Structures, FEMA and Oak Ridge National Laboratory, CC BY 4.0"
CITATION = ("FEMA Response Geospatial Office, Oak Ridge National Laboratory and USGS, USA Structures, "
            "served as Esri Living Atlas item 0ec8512ad21e4bb987d7e848d14e7e24.")
TERMS = ('Esri item licence: "This work is licensed under a Creative Commons by Attribution (CC BY 4.0) license." '
         "Attribution printed; no share-alike.")


class StructuresTruncatedError(RuntimeError):
    """The service answered with exceededTransferLimit: a partial list."""


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


@fetch_attempts.publishes
def get_structures_for_boundary(boundary_coordinates: list, buffer_meters: float = FETCH_BUFFER_METERS,
                                max_retries: int = 2) -> dict:
    min_lon, min_lat, max_lon, max_lat = _bounding_box(boundary_coordinates, buffer_meters=buffer_meters)
    params = {
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "outFields": OUT_FIELDS,
        "geometryPrecision": 7,
        "f": "geojson",
    }
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        try:
            response = requests.get(STRUCTURES_QUERY, params=params, timeout=30 + attempt * 30)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "error" in data:
                raise requests.exceptions.HTTPError(f"ArcGIS error: {data['error']}")
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error
        if data.get("exceededTransferLimit") or (data.get("properties") or {}).get("exceededTransferLimit"):
            raise StructuresTruncatedError("USA Structures returned a partial list (exceededTransferLimit)")
        return {"bbox": [min_lon, min_lat, max_lon, max_lat], "buffer_meters": buffer_meters, "response": data}


def _date(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).date().isoformat()
    return str(value)[:10]


def parse_structures(raw: dict) -> dict:
    """
    {'bbox', 'buffer_meters',
     'structures': [{'build_id', 'occupancy', 'primary_occupancy',
                     'sqft', 'image_date', 'production_date', 'source',
                     'geometry'}],     # geometry WGS84 GeoJSON
     'image_dates': [iso, ...], 'production_dates': [iso, ...]}
    """
    structures = []
    for feature in ((raw or {}).get("response") or {}).get("features", []):
        props = feature.get("properties") or {}
        if not feature.get("geometry"):
            continue
        structures.append({
            "build_id": props.get("BUILD_ID"),
            "occupancy": props.get("OCC_CLS"),
            "primary_occupancy": props.get("PRIM_OCC"),
            "sqft": props.get("SQFEET"),
            "image_date": _date(props.get("IMAGE_DATE")),
            "production_date": _date(props.get("PROD_DATE")),
            "source": props.get("SOURCE"),
            "geometry": feature["geometry"],
        })
    return {
        "bbox": raw.get("bbox"),
        "buffer_meters": raw.get("buffer_meters"),
        "structures": structures,
        "image_dates": sorted({s["image_date"] for s in structures if s["image_date"]}),
        "production_dates": sorted({s["production_date"] for s in structures if s["production_date"]}),
    }
