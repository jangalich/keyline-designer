"""
nhdplus_data.py

NHDPLUS HIGH RESOLUTION value-added attributes for the flowlines Layer 1
already fetched: stream order and the reach's total drainage area.

    get_flowline_attributes_for_boundary(boundary) -> the raw response (a fetch)
    parse_flowline_attributes(raw)                 -> {permanent_identifier: {...}}

WHY A SECOND SERVICE FOR THE SAME STREAMS. NHD's flowline layer (the one
hydrology_data.py queries) carries no stream order. NHDPlus HR's
NetworkNHDFlowline layer, on the same hydro.nationalmap.gov host, carries
Strahler order (streamorde), stream level and the reach's TOTAL upstream
drainage area (totdasqkm) -- and it keys them by the same
permanent_identifier NHD's rows carry, so the join is a dictionary
lookup and no geometry is fetched twice (returnGeometry=false; 12 KB on
the reference parcel for three reaches).

TOTDASQKM IS THE UN-TRUNCATED CATCHMENT. The report's DEM window is the
parcel plus 100 m, and any catchment measured on it stops at the window's
edge (step 0 measured 77 rim cells in the parcel outlets' watershed).
The reach's total drainage area is computed on the whole NHDPlus HR
network and is not truncated by anything the report chose; the section
leads with it as the stream's catchment and reports the window-derived
figure beneath it as what the terrain analysis sees.

COVERAGE. NHDPlus HR is not complete everywhere; a flowline NHD returns
that NHDPlus HR does not cover simply has no entry in the parsed dict,
and the section's stream-order column reads a dash for it. The service
is queried over the same bbox-plus-150 m window NHD is.

TERMS. A USGS product, public domain.
"""

import math

import requests

import fetch_attempts

NHDPLUS_HR_BASE = "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer"
LAYER_NETWORK_FLOWLINE = 3
NHDPLUS_FETCH_BUFFER_METERS = 150.0
OUT_FIELDS = "permanent_identifier,nhdplusid,gnis_name,fcode,reachcode,lengthkm,streamorde,streamleve,streamcalc,totdasqkm,divdasqkm,areasqkm"

SQUARE_KM_PER_ACRE = 0.0040468564224

NHDPLUS_CITATION = (
    "U.S. Geological Survey, National Hydrography Dataset Plus High Resolution (NHDPlus HR), "
    "NetworkNHDFlowline value-added attributes, served by The National Map's NHDPlus HR map service."
)


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


def _bounding_box(boundary_coordinates: list, buffer_meters: float) -> tuple:
    lons = [p[0] for p in boundary_coordinates]
    lats = [p[1] for p in boundary_coordinates]
    avg_lat = (min(lats) + max(lats)) / 2
    lat_buffer = buffer_meters / 111_320
    lon_buffer = buffer_meters / (111_320 * abs(math.cos(math.radians(avg_lat))))
    return (min(lons) - lon_buffer, min(lats) - lat_buffer, max(lons) + lon_buffer, max(lats) + lat_buffer)


@fetch_attempts.publishes
def get_flowline_attributes_for_boundary(boundary_coordinates: list, buffer_meters: float = NHDPLUS_FETCH_BUFFER_METERS) -> dict:
    """The raw JSON for every network flowline intersecting the boundary's
    bbox plus the buffer, attributes only."""
    min_lon, min_lat, max_lon, max_lat = _bounding_box(boundary_coordinates, buffer_meters)
    params = {
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "f": "json",
    }
    last_error = None
    for attempt in fetch_attempts.attempts(2):
        timeout = 30 + attempt * 30
        try:
            response = requests.get(f"{NHDPLUS_HR_BASE}/{LAYER_NETWORK_FLOWLINE}/query", params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "error" in data:
                raise requests.exceptions.HTTPError(f"ArcGIS error: {data['error']}")
            return data
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < 2:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def _number(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_flowline_attributes(raw: dict) -> dict:
    """{permanent_identifier (str): {'stream_order', 'stream_level',
    'total_drainage_sqkm', 'total_drainage_acres', 'divergence_drainage_sqkm',
    'catchment_sqkm', 'gnis_name', 'fcode', 'reachcode', 'length_km',
    'nhdplusid'}}"""
    out = {}
    for feature in (raw or {}).get("features", []):
        a = feature.get("attributes") or {}
        pid = a.get("permanent_identifier")
        if pid is None:
            continue
        total = _number(a.get("totdasqkm"))
        order = a.get("streamorde")
        out[str(pid)] = {
            "stream_order": int(order) if order is not None else None,
            "stream_level": a.get("streamleve"),
            "total_drainage_sqkm": total,
            "total_drainage_acres": None if total is None else total / SQUARE_KM_PER_ACRE,
            "divergence_drainage_sqkm": _number(a.get("divdasqkm")),
            "catchment_sqkm": _number(a.get("areasqkm")),
            "gnis_name": a.get("gnis_name"),
            "fcode": a.get("fcode"),
            "reachcode": a.get("reachcode"),
            "length_km": _number(a.get("lengthkm")),
            "nhdplusid": a.get("nhdplusid"),
        }
    return out


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    for pid, row in parse_flowline_attributes(get_flowline_attributes_for_boundary(REAL_BOUNDARY)).items():
        print(pid, row["gnis_name"], "order", row["stream_order"], f"{row['total_drainage_acres']:.0f} ac")
