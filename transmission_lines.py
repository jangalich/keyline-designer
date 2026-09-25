"""
transmission_lines.py

THE NEAREST ELECTRIC TRANSMISSION LINE, for the site overview: HIFLD's
national transmission lines, as Esri's archived copy serves them.

    get_transmission_lines_near_boundary(boundary)  -> the raw response (a fetch)
    parse_transmission_lines(raw)                   -> the parsed block

THE SOURCE IS AN ARCHIVE, AND THE PAGE SAYS SO. HIFLD Open's own service
is gone. The live copy is Esri's "U.S. Electric Power Transmission Lines
(Archive)" (item d4090758322c4d32a4cd002ffaa0aa12): the HIFLD layer as of
its last data update, 30 September 2024, "archived... no longer updated
or maintained". A transmission corridor is about as permanent as
anything on a map, so a distance to one is stable information; the
archive date is printed beside it (ARCHIVE_LABEL) so a reader can weigh
it.

TERMS, AS FOUND, TWO STATEMENTS. The HIFLD metadata for the layer states
"Use limitations: None (Public Use)" -- U.S. Government data. The Esri
item that serves it carries "This work is licensed under the Esri Master
License Agreement." Both are recorded (TERMS) and the vintage table prints
them as found; which governs the hosted copy is not something this module
decides.

A DISTANCE QUERY, NOT AN ENVELOPE. The parcel polygon with
`distance=SEARCH_RADIUS_METERS`: an envelope search finds the nearest
line inside a square and misses a closer one just outside it (step 0
measured exactly that). Five miles, because "none within five miles" is
itself the useful answer for a farm and the query stays one small
response. Voltage -999999 and owner "NOT AVAILABLE" are the source's
unknowns and are parsed as None, so the nearest line with a KNOWN voltage
can be named beside the nearest line.
"""

import json
from datetime import datetime, timezone
from typing import Optional

import requests

import fetch_attempts

LINES_QUERY = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
               "US_Electric_Power_Transmission_Lines/FeatureServer/0/query")
ITEM_URL = "https://www.arcgis.com/home/item.html?id=d4090758322c4d32a4cd002ffaa0aa12"
METERS_PER_MILE = 1609.344
SEARCH_RADIUS_MILES = 5
SEARCH_RADIUS_METERS = SEARCH_RADIUS_MILES * METERS_PER_MILE

OUT_FIELDS = "ID,TYPE,STATUS,OWNER,VOLTAGE,VOLT_CLASS,INFERRED,SUB_1,SUB_2,SOURCEDATE,VAL_DATE,VAL_METHOD"

ARCHIVE_LABEL = "archived September 2024"
ARCHIVE_DATE = "2024-09-30"
CITATION = ("Homeland Infrastructure Foundation-Level Data (HIFLD), Electric Power Transmission Lines, "
            "as Esri's U.S. Electric Power Transmission Lines (Archive), last data update 30 September 2024.")
TERMS = ('HIFLD metadata: "Use limitations: None (Public Use)". The Esri item serving it: '
         '"This work is licensed under the Esri Master License Agreement." Both as found.')

_UNKNOWN_VOLTAGE = -999999
_UNKNOWN_TEXT = ("NOT AVAILABLE", "")


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


@fetch_attempts.publishes
def get_transmission_lines_near_boundary(boundary_coordinates: list, radius_meters: float = SEARCH_RADIUS_METERS,
                                         max_retries: int = 2) -> dict:
    geometry = json.dumps({"rings": [[[float(lon), float(lat)] for lon, lat in boundary_coordinates]],
                           "spatialReference": {"wkid": 4326}})
    params = {
        "geometry": geometry,
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_meters,
        "units": "esriSRUnit_Meter",
        "inSR": 4326,
        "outSR": 4326,
        "outFields": OUT_FIELDS,
        "geometryPrecision": 6,
        "f": "geojson",
    }
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        try:
            response = requests.get(LINES_QUERY, params=params, timeout=30 + attempt * 30)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "error" in data:
                raise requests.exceptions.HTTPError(f"ArcGIS error: {data['error']}")
            return {"radius_meters": radius_meters, "response": data}
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def _date(value) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).date().isoformat()
    return str(value)[:10]


def _text(value) -> Optional[str]:
    if value is None or str(value).strip().upper() in _UNKNOWN_TEXT:
        return None
    return str(value).strip()


def parse_transmission_lines(raw: dict) -> dict:
    """
    {'radius_meters', 'lines': [{'id', 'voltage_kv' (None = unknown),
     'volt_class', 'owner', 'status', 'type', 'inferred', 'source_date',
     'validated_on', 'geometry'}]}  -- geometry WGS84 GeoJSON
    """
    lines = []
    for feature in ((raw or {}).get("response") or {}).get("features", []):
        props = feature.get("properties") or {}
        if not feature.get("geometry"):
            continue
        voltage = props.get("VOLTAGE")
        lines.append({
            "id": props.get("ID"),
            "voltage_kv": None if voltage in (None, _UNKNOWN_VOLTAGE) or (voltage or 0) <= 0 else float(voltage),
            "volt_class": _text(props.get("VOLT_CLASS")),
            "owner": _text(props.get("OWNER")),
            "status": _text(props.get("STATUS")),
            "type": _text(props.get("TYPE")),
            "inferred": _text(props.get("INFERRED")),
            "source_date": _date(props.get("SOURCEDATE")),
            "validated_on": _date(props.get("VAL_DATE")),
            "geometry": feature["geometry"],
        })
    return {"radius_meters": raw.get("radius_meters"), "lines": lines}
