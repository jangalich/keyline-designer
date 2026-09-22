"""
precipitation_normals.py

THE PRECIPITATION CORRECTION AND THE HEAVY-RAIN NORMALS, from bundled
NCEI 1991-2020 station normals -- class E reference data: read from the
repository, never fetched at report time, so the report makes ONE Daymet
call.

    nearest_stations(lat, lon)            -> the five stations to use
    precipitation_correction(stations)    -> the correction block
    heavy_rain_normals(stations)          -> days >= 1.00 in by month, the median
    load_bundle()                         -> (vintage, station rows)
    annual_precipitation_mm(daily)        -> mean annual total of a parsed Daymet dict

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

THE RATIOS ARE BUNDLED, NOT FETCHED (phase 2 decision). A station's
normal and Daymet's 1991-2020 mean at the station are both fixed-period
figures; neither changes between reports. make_normals_daymet_cache.py
fetches Daymet at every accepted station once, make_normals_bundle.py
stores the ratio beside the normal, and a report reads five ratios off
the bundle. That removed five Daymet calls and five failure points per
report from a REQUIRED source. The ratios are refreshed with the bundle
when Daymet publishes a new version; the bundle's vintage records the
Daymet version behind them.

THE METHOD. The five nearest stations with an annual precipitation
normal of accepted completeness (S, standard, 24+ years; R,
representative, 10+ years with gaps filled from neighbours) AND a Daymet
ratio, within MAX_STATION_MILES. The factor is the MEDIAN of their
ratios -- the median so one odd station (Emsworth, 38.3 in beside
Acmetonia's 44.5) cannot pull it. Every precipitation figure the block
derives -- monthly, annual, driest and wettest year, the largest day --
is computed from the scaled daily series, so the section is consistent
with itself (climate_report.derive_climate's prcp_factor).

HEAVY-RAIN DAYS COME FROM THE STATIONS, NOT DAYMET (phase 2 decision).
Daymet counted 5.6 days a year at or above 1.00 in against 6.3 to 8.7 at
the same five stations: interpolation smooths daily peaks, and an annual
scale factor cannot restore an extreme. NCEI's monthly normals carry the
normal count of days with at least 1.00 in per month
(MLY-PRCP-AVGNDS-GE100HI); heavy_rain_normals() takes the median across
the same stations, month by month, and the median of their annual
normals for the year. Daymet's own count stays on the climate block for
diagnostics.

WHEN IT DOES NOT APPLY. Fewer than MIN_STATIONS usable stations within
range (a parcel far from any normals station, or beside stations Daymet
has no cell for): the factor is 1.0, `applied` is False and `reason`
says why, and the section says the figure is uncorrected. The
heavy-rain normals degrade the same way, to Daymet's count.

THE BUNDLE. assets/reference/ncei_prcp_normals_1991_2020.csv, built by
make_normals_bundle.py from NCEI's archived v1.0.1 by-station files
(NOT the access API, which serves the 1981-2010 normals under the same
dataset name -- see that script) and the Daymet cache. One row per
station with an annual precipitation normal; the first line is a JSON
comment naming the archives and the Daymet version, which is the
vintage the footer prints.
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
BUNDLE_FORMAT = "ncei-prcp-normals-v2"
MONTH_COLUMNS = tuple(f"days_ge_1in_m{m:02d}" for m in range(1, 13))
BUNDLE_COLUMNS = (
    "station", "latitude", "longitude", "elevation_m", "name",
    "prcp_normal_in", "prcp_flag", "prcp_years",
    "daymet_annual_mm", "daymet_version", "ratio",
    "days_ge_1in", "days_ge_1in_flag",
    *MONTH_COLUMNS, "days_ge_1in_monthly_flag",
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


def _float(value: str) -> Optional[float]:
    value = (value or "").strip()
    return float(value) if value else None


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
                months = [_float(row[c]) for c in MONTH_COLUMNS]
                station = {
                    "station": row["station"],
                    "name": row["name"],
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "elevation_m": _float(row["elevation_m"]),
                    "prcp_normal_in": float(row["prcp_normal_in"]),
                    "prcp_flag": row["prcp_flag"],
                    "prcp_years": int(row["prcp_years"]) if row["prcp_years"] else None,
                    "daymet_annual_mm": _float(row["daymet_annual_mm"]),
                    "daymet_version": row["daymet_version"] or None,
                    "ratio": _float(row["ratio"]),
                    "days_ge_1in": _float(row["days_ge_1in"]),
                    "days_ge_1in_flag": row["days_ge_1in_flag"],
                    "days_ge_1in_monthly": months if all(m is not None for m in months) else None,
                    "days_ge_1in_monthly_flag": row["days_ge_1in_monthly_flag"],
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
    require_ratio: bool = True,
    path: str = BUNDLE_PATH,
) -> list:
    """The `count` nearest stations with an accepted-flag annual normal
    (and, by default, a Daymet ratio) within `max_miles`, nearest first,
    each with `distance_miles`."""
    _, stations = load_bundle(path)
    candidates = []
    for station in stations:
        if station["prcp_flag"] not in accepted_flags:
            continue
        if require_ratio and station["ratio"] is None:
            continue
        distance = haversine_miles(lat, lon, station["latitude"], station["longitude"])
        if distance <= max_miles:
            candidates.append(dict(station, distance_miles=distance))
    candidates.sort(key=lambda s: (s["distance_miles"], s["station"]))
    return candidates[:count]


def annual_precipitation_mm(daily: dict) -> float:
    """Mean over years of the annual precipitation total of a parsed
    Daymet dict (daymet_data.parse_daymet_csv) -- mm. The cache builder's
    figure; kept here so the ratio's two halves are defined together."""
    totals = {}
    for year, prcp in zip(daily["year"], daily["prcp"]):
        totals[year] = totals.get(year, 0.0) + prcp
    if not totals:
        raise ValueError("annual_precipitation_mm: no rows")
    return statistics.fmean(totals.values())


def _station_row(station: dict) -> dict:
    return {
        "station": station["station"],
        "name": station["name"],
        "distance_miles": station["distance_miles"],
        "elevation_m": station.get("elevation_m"),
        "normal_mm": station["prcp_normal_in"] * MM_PER_INCH,
        "normal_in": station["prcp_normal_in"],
        "daymet_mm": station["daymet_annual_mm"],
        "daymet_version": station.get("daymet_version"),
        "ratio": station["ratio"],
        "flag": station["prcp_flag"],
        "years": station.get("prcp_years"),
    }


def precipitation_correction(stations: list, vintage: Optional[dict] = None) -> dict:
    """
    The correction block from the nearest stations' bundled ratios:

        {
          'applied': bool, 'factor': float,        # 1.0 when not applied
          'reason': str | None,                     # why not, when not
          'method': 'median of station normal / Daymet at station',
          'normals_period': '1991-2020', 'station_count': int,
          'stations': [ {'station', 'name', 'distance_miles', 'elevation_m',
                         'normal_mm', 'normal_in', 'daymet_mm', 'daymet_version',
                         'ratio', 'flag', 'years'}, ... ],   # nearest first
          'vintage': the bundle's vintage dict,
        }
    """
    rows = [_station_row(s) for s in stations if s.get("ratio")]
    block = {
        "applied": False,
        "factor": 1.0,
        "reason": None,
        "method": "median of station normal / Daymet at station",
        "normals_period": NORMALS_PERIOD,
        "station_count": len(rows),
        "stations": rows,
        "vintage": vintage if vintage is not None else load_bundle()[0],
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


def heavy_rain_normals(stations: list) -> dict:
    """
    Days with at least 1.00 in of precipitation, from the stations' NCEI
    monthly normals:

        {
          'applied': bool, 'reason': str | None,
          'monthly': [12 medians] | None,          # days per month
          'annual': float | None,                  # median of the annual normals
          'station_count': int,
          'stations': [ {'station', 'name', 'distance_miles', 'annual',
                         'monthly': [12], 'flag'}, ... ],
        }

    Medians across the stations that carry all twelve months; fewer than
    MIN_STATIONS of them and the block is not applied.
    """
    rows = []
    for station in stations:
        if station.get("days_ge_1in_monthly") is None or station.get("days_ge_1in") is None:
            continue
        rows.append(
            {
                "station": station["station"],
                "name": station["name"],
                "distance_miles": station["distance_miles"],
                "annual": station["days_ge_1in"],
                "monthly": list(station["days_ge_1in_monthly"]),
                "flag": station.get("days_ge_1in_monthly_flag"),
            }
        )
    block = {"applied": False, "reason": None, "monthly": None, "annual": None, "station_count": len(rows), "stations": rows}
    if len(rows) < MIN_STATIONS:
        block["reason"] = f"{len(rows)} station(s) with monthly heavy-rain normals; at least {MIN_STATIONS} needed"
        return block
    block["applied"] = True
    block["monthly"] = [statistics.median(row["monthly"][m] for row in rows) for m in range(12)]
    block["annual"] = statistics.median(row["annual"] for row in rows)
    return block


def summarize_correction(block: dict) -> str:
    if not block["applied"]:
        return f"Precipitation correction not applied: {block['reason']}"
    parts = ", ".join(
        f"{s['name']} {s['distance_miles']:.1f} mi normal {s['normal_in']:.2f} in / Daymet "
        f"{s['daymet_mm'] / MM_PER_INCH:.2f} in = {s['ratio']:.3f}" for s in block["stations"]
    )
    return f"Precipitation factor {block['factor']:.3f} (median of {block['station_count']}): {parts}"
