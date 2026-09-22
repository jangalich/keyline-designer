"""
precipitation_normals.py

THE PRECIPITATION CORRECTION: Daymet's precipitation at the parcel,
scaled by how Daymet compares with the NCEI 1991-2020 station normals
around it -- class E reference data (the normals, bundled) plus one
Daymet fetch per station at report time.

    nearest_stations(lat, lon)                    -> the five stations to use
    annual_precipitation_mm(daily)                -> mean annual total of a parsed Daymet dict
    precipitation_correction(stations, daymet_mm) -> the correction block
    load_bundle()                                 -> (vintage, station rows)

WHY (branch 7 step 0, decision by the author). Daymet 1991-2020 annual
precipitation, sampled at each station's OWN coordinates, exceeds that
station's 1991-2020 normal at every station near the reference parcel:
by 0.1 to 3.4 inches, a median ratio of normal to Daymet of 0.96 over
the nearest five. The parcel's Daymet figure is the section's first
number and feeds the water balance, where inflated rainfall against an
accurate evaporation understates the summer deficit -- the wrong
direction for a storage decision. So the parcel's Daymet precipitation
is scaled, openly: the factor, the stations and the ratios are on the
block and in the methods note.

THE METHOD. The five nearest stations with an annual precipitation
normal of accepted completeness (S, standard, 24+ years; R,
representative, 10+ years with gaps filled from neighbours) within
MAX_STATION_MILES. For each, Daymet daily precipitation is fetched at
the station's coordinates for the normals period 1991-2020 and its mean
annual total taken. The factor is the MEDIAN of normal / Daymet over
the five -- the median so one odd station (Emsworth, 38.3 in beside
Acmetonia's 44.5) cannot pull it. Every precipitation figure the block
derives -- monthly, annual, driest and wettest year, heavy-rain days,
the largest day -- is computed from the scaled daily series, so the
section is consistent with itself (climate_report.derive_climate's
prcp_factor).

WHEN IT DOES NOT APPLY. Fewer than MIN_STATIONS usable stations within
range (a parcel far from any normals station), or a Daymet answer
missing for one: the factor is 1.0, `applied` is False and `reason`
says why, and the section says the figure is uncorrected. A missing
station is not a report failure; a missing Daymet is (report_data).

THE BUNDLE. assets/reference/ncei_prcp_normals_1991_2020.csv, built by
make_normals_bundle.py from NCEI's archived v1.0.1 by-station files
(NOT the access API, which serves the 1981-2010 normals under the same
dataset name -- see that script). One row per station with an annual
precipitation normal: id, coordinates, elevation, name, the normal in
inches, its completeness flag and years, and the normal count of days
with 1.00 in or more (kept for the heavy-rain cross-check). The first
line is a JSON comment naming the archive, which is the vintage the
footer prints.
"""

import csv
import json
import math
import os
import statistics
import threading
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_PATH = os.path.join(_HERE, "assets", "reference", "ncei_prcp_normals_1991_2020.csv")
BUNDLE_FORMAT = "ncei-prcp-normals-v1"
BUNDLE_COLUMNS = (
    "station", "latitude", "longitude", "elevation_m", "name",
    "prcp_normal_in", "prcp_flag", "prcp_years",
    "days_ge_1in", "days_ge_1in_flag", "days_ge_1in_years",
)

NORMALS_YEARS = list(range(1991, 2021))
NORMALS_PERIOD = "1991-2020"
STATION_COUNT = 5
MIN_STATIONS = 3
MAX_STATION_MILES = 75.0
ACCEPTED_FLAGS = ("S", "R")
MM_PER_INCH = 25.4
EARTH_RADIUS_MILES = 3958.8

_LOCK = threading.Lock()
_LOADED = {}


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def read_bundle(path: str = BUNDLE_PATH) -> tuple:
    """(vintage dict, [station dict, ...]) from disk. Numeric columns are
    parsed; a station whose normal will not parse is dropped."""
    with open(path, encoding="utf-8", newline="") as handle:
        first = handle.readline()
        if not first.startswith("#"):
            raise ValueError(f"{path}: no vintage comment on the first line")
        vintage = json.loads(first[1:].strip())
        if vintage.get("format") != BUNDLE_FORMAT:
            raise ValueError(f"{path}: bundle format {vintage.get('format')!r}, expected {BUNDLE_FORMAT!r}")
        reader = csv.DictReader(handle)
        missing = [c for c in BUNDLE_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        stations = []
        for row in reader:
            try:
                station = {
                    "station": row["station"],
                    "name": row["name"],
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "elevation_m": float(row["elevation_m"]) if row["elevation_m"] else None,
                    "prcp_normal_in": float(row["prcp_normal_in"]),
                    "prcp_flag": row["prcp_flag"],
                    "prcp_years": int(row["prcp_years"]) if row["prcp_years"] else None,
                    "days_ge_1in": float(row["days_ge_1in"]) if row["days_ge_1in"] else None,
                    "days_ge_1in_flag": row["days_ge_1in_flag"],
                }
            except ValueError:
                continue
            stations.append(station)
    return vintage, stations


def load_bundle(path: str = BUNDLE_PATH) -> tuple:
    with _LOCK:
        if path not in _LOADED:
            _LOADED[path] = read_bundle(path)
        return _LOADED[path]


def nearest_stations(
    lat: float,
    lon: float,
    count: int = STATION_COUNT,
    max_miles: float = MAX_STATION_MILES,
    accepted_flags=ACCEPTED_FLAGS,
    path: str = BUNDLE_PATH,
) -> list:
    """The `count` nearest stations with an accepted-flag annual normal
    within `max_miles`, nearest first, each with `distance_miles`."""
    _, stations = load_bundle(path)
    candidates = []
    for station in stations:
        if station["prcp_flag"] not in accepted_flags:
            continue
        distance = haversine_miles(lat, lon, station["latitude"], station["longitude"])
        if distance <= max_miles:
            candidates.append(dict(station, distance_miles=distance))
    candidates.sort(key=lambda s: (s["distance_miles"], s["station"]))
    return candidates[:count]


def annual_precipitation_mm(daily: dict) -> float:
    """Mean over years of the annual precipitation total of a parsed
    Daymet dict (daymet_data.parse_daymet_csv) -- mm."""
    totals = {}
    for year, prcp in zip(daily["year"], daily["prcp"]):
        totals[year] = totals.get(year, 0.0) + prcp
    if not totals:
        raise ValueError("annual_precipitation_mm: no rows")
    return statistics.fmean(totals.values())


def precipitation_correction(stations: list, daymet_annual_mm: dict, vintage: Optional[dict] = None) -> dict:
    """
    The correction block from the nearest stations and Daymet's mean
    annual precipitation (mm) at each, keyed by station id:

        {
          'applied': bool, 'factor': float,        # 1.0 when not applied
          'reason': str | None,                     # why not, when not
          'method': 'median of station normal / Daymet at station',
          'normals_period': '1991-2020', 'station_count': int,
          'stations': [ {'station', 'name', 'distance_miles', 'elevation_m',
                         'normal_mm', 'normal_in', 'daymet_mm', 'ratio',
                         'flag', 'years'}, ... ],   # nearest first
          'vintage': the bundle's vintage dict,
        }
    """
    rows = []
    for station in stations:
        daymet_mm = daymet_annual_mm.get(station["station"])
        if daymet_mm is None or daymet_mm <= 0:
            continue
        normal_mm = station["prcp_normal_in"] * MM_PER_INCH
        rows.append(
            {
                "station": station["station"],
                "name": station["name"],
                "distance_miles": station["distance_miles"],
                "elevation_m": station.get("elevation_m"),
                "normal_mm": normal_mm,
                "normal_in": station["prcp_normal_in"],
                "daymet_mm": daymet_mm,
                "ratio": normal_mm / daymet_mm,
                "flag": station["prcp_flag"],
                "years": station.get("prcp_years"),
            }
        )
    block = {
        "applied": False,
        "factor": 1.0,
        "reason": None,
        "method": "median of station normal / Daymet at station",
        "normals_period": NORMALS_PERIOD,
        "station_count": len(rows),
        "stations": rows,
        "vintage": vintage or {},
    }
    if len(rows) < MIN_STATIONS:
        block["reason"] = (
            f"{len(rows)} usable normals station(s) within {MAX_STATION_MILES:.0f} miles; "
            f"at least {MIN_STATIONS} needed"
        )
        return block
    block["applied"] = True
    block["factor"] = statistics.median(row["ratio"] for row in rows)
    return block


def summarize_correction(block: dict) -> str:
    if not block["applied"]:
        return f"Precipitation correction not applied: {block['reason']}"
    parts = ", ".join(
        f"{s['name']} {s['distance_miles']:.1f} mi normal {s['normal_in']:.2f} in / Daymet "
        f"{s['daymet_mm'] / MM_PER_INCH:.2f} in = {s['ratio']:.3f}" for s in block["stations"]
    )
    return f"Precipitation factor {block['factor']:.3f} (median of {block['station_count']}): {parts}"
