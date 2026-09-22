"""
power_wind_data.py

Fetches NASA POWER daily wind for one point -- the parcel centroid --
parses the CSV it returns, and derives the report's seasonal wind block.

    get_power_wind_for_point(lat, lon, years)  -> the parsed dict (network)
    parse_power_csv(text)                      -> the parsed dict (pure)
    derive_wind(parsed)                        -> the wind block (pure)

THE SOURCE, AND ITS SCALE. NASA POWER serves MERRA-2 reanalysis
meteorology on a 0.5 x 0.625 degree grid -- the response header says so
in as many words: "Average for 0.5 x 0.625 degree lat/lon region". At
40.6 N that cell is roughly 55 by 53 km. The wind here is the REGION'S,
never the parcel's: valleys and ridges channel local wind, and the page
says so as its one caveat. Daily data run from 1981; the report asks
for the SAME 30 calendar years the Daymet climate block uses, so one
period reads across the whole section. No key; NASA Earth science data
are free and open including for commercial use, with an acknowledgement
of the POWER Project requested (methods note). POWER's team states no
set rate limit but monitors usage; one request returns 30 years of two
parameters in about a second.

WD10M IS A VECTOR MEAN -- VERIFIED, NOT ASSUMED. POWER's parameter
definition says only "the average of the wind direction". Branch 7 step
0 fetched hourly WS10M/WD10M for January 2020 at the reference parcel
and compared three candidate daily means against the served daily
WD10M: the speed-weighted vector mean matched to 0.0 degrees, a
unit-vector mean missed by 6.5 and an arithmetic mean by 21.7. So a
daily WD10M is the direction of the day's resultant wind vector, and the
daily WS10M is the scalar mean of hourly speeds (matched to 0.003 m/s).
A day is therefore sectored by its served direction with no further
vector arithmetic; the SEASONAL prevailing direction is a speed-weighted
vector mean over days (prevailing_direction_degrees), because averaging
directions arithmetically puts 350 and 10 degrees at 180.

DIRECTION IS WHERE THE WIND COMES FROM. Meteorological convention,
degrees clockwise from north, as POWER documents and as every rose
label must say ("wind from"). compass_sector() puts 0 degrees in the N
sector, 45 in NE, and so on; a sector spans 45 degrees centred on its
name, so N is 337.5 up to 22.5.

THE CALENDAR IS THE REAL ONE. Unlike Daymet, POWER's DOY runs 1..366 in
a leap year; power_date() is date(year, 1, 1) + (doy - 1) with no
dropped day. Winter is December, January and February of the calendar
years fetched (so the first January has no preceding December in the
window; the count of days is what it is and is on the block); summer
is June, July and August. A day with a fill value (-999) in either
parameter is dropped and counted under `fill_days`.

UNITS ARE METRIC on the dict and the block: m/s and degrees, as served.
Miles per hour belong to the formatting layer.

Docs: https://power.larc.nasa.gov/docs/services/api/temporal/daily/
      https://power.larc.nasa.gov/docs/methodology/meteorology/wind/
"""

import math
import re
from datetime import date, timedelta
from typing import Optional

import requests

import fetch_attempts

POWER_DAILY_ENDPOINT = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_PARAMETERS = ("WS10M", "WD10M")
POWER_COMMUNITY = "AG"
FILL_VALUE = -999.0

SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
SECTOR_WIDTH_DEGREES = 360.0 / len(SECTORS)

# The two seasons the page shows, as calendar months.
SEASONS = {"winter": (12, 1, 2), "summer": (6, 7, 8)}

_LOCATION_RE = re.compile(r"^Location:\s*latitude\s+(-?[\d.]+)\s+longitude\s+(-?[\d.]+)", re.IGNORECASE)
_ELEVATION_RE = re.compile(r"^elevation from MERRA-2:\s*(.*?)=\s*(-?[\d.]+)\s*meters", re.IGNORECASE)
_DATES_RE = re.compile(r"^Dates \(month/day/year\):\s*(\S+)\s+through\s+(\S+)")
_PARAMETER_RE = re.compile(r"^([A-Z0-9_]+)\s+(.+?)\s*\(([^)]*)\)\s*$")


class PowerIncompleteError(RuntimeError):
    """The service answered, but not with the data asked for: a parameter
    column missing, no data rows, or a requested year absent."""


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


# ======================================================================
# Calendar, sectors, vector mean
# ======================================================================


def power_date(year: int, doy: int) -> date:
    """The real calendar: doy 1..366, no dropped day (contrast
    daymet_data.daymet_date)."""
    if not 1 <= doy <= 366:
        raise ValueError(f"POWER DOY must be 1..366, got {doy!r} (year {year})")
    d = date(year, 1, 1) + timedelta(days=doy - 1)
    if d.year != year:
        # DOY 366 in a common year would land on January 1 of the next
        # year; refused rather than wrapped.
        raise ValueError(f"POWER DOY {doy} does not exist in {year}")
    return d


def compass_sector(degrees_from: float) -> str:
    """The eight-point sector a from-direction falls in: 0 -> N, 45 -> NE,
    337.5 -> N (the boundary belongs to the clockwise sector)."""
    index = int(math.floor((degrees_from % 360.0) / SECTOR_WIDTH_DEGREES + 0.5)) % len(SECTORS)
    return SECTORS[index]


def prevailing_direction_degrees(directions_from, speeds) -> Optional[float]:
    """
    Speed-weighted vector mean of from-directions, in degrees from north.
    Each day contributes a vector of its speed pointing FROM its
    direction; the mean vector's direction is the prevailing one.
    None when there are no days or the mean vector is zero (winds
    balanced all round).
    """
    sin_sum = cos_sum = 0.0
    n = 0
    for d, s in zip(directions_from, speeds):
        sin_sum += s * math.sin(math.radians(d))
        cos_sum += s * math.cos(math.radians(d))
        n += 1
    if n == 0 or (abs(sin_sum) < 1e-12 and abs(cos_sum) < 1e-12):
        return None
    degrees = math.degrees(math.atan2(sin_sum, cos_sum)) % 360.0
    # A mean a hair below north comes out of the modulo as 359.999...;
    # north is 0.
    return 0.0 if 360.0 - degrees < 1e-9 else degrees


# ======================================================================
# The parser -- pure, no network
# ======================================================================


def parse_power_csv(text: str, required_parameters=POWER_PARAMETERS) -> dict:
    """
    POWER's daily CSV -> one dict:

        {
          'latitude', 'longitude'      the point served,
          'cell_elevation_m'           the MERRA-2 cell's mean elevation --
                                       diagnostics only, never the parcel's,
          'cell_description'           'Average for 0.5 x 0.625 degree lat/lon region',
          'parameters'                 {name: {'description', 'units'}} as served,
          'header_lines'               the header block, for diagnostics,
          'columns'                    the column names as served,
          'year', 'doy'                parallel int lists, one per row,
          'WS10M', 'WD10M'             parallel float lists (fill values kept
                                       as served; derive_wind() drops them),
          'years'                      sorted distinct years present,
          'row_count', 'fill_value',
        }

    Columns are found by name. A required parameter column missing, or
    no data rows, raises PowerIncompleteError.
    """
    lines = text.splitlines()
    header = []
    in_header = False
    column_index = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "-BEGIN HEADER-":
            in_header = True
            continue
        if stripped == "-END HEADER-":
            in_header = False
            continue
        if in_header:
            header.append(stripped)
            continue
        if stripped.startswith("YEAR,"):
            column_index = i
            break
    if column_index is None:
        raise PowerIncompleteError("POWER CSV has no 'YEAR,...' column line")

    latitude = longitude = None
    elevation = None
    description = None
    parameters = {}
    fill_value = FILL_VALUE
    for line in header:
        m = _LOCATION_RE.match(line)
        if m:
            latitude, longitude = float(m.group(1)), float(m.group(2))
            continue
        m = _ELEVATION_RE.match(line)
        if m:
            description, elevation = m.group(1).strip(), float(m.group(2))
            continue
        if "missing source data" in line:
            fm = re.search(r"(-?\d+(?:\.\d+)?)\s*$", line)
            if fm:
                fill_value = float(fm.group(1))
            continue
        m = _PARAMETER_RE.match(line)
        if m and m.group(1) not in ("YEAR", "DOY"):
            parameters[m.group(1)] = {"description": m.group(2), "units": m.group(3)}

    columns = [c.strip() for c in lines[column_index].split(",")]
    index_of = {name: i for i, name in enumerate(columns)}
    missing = [p for p in ("YEAR", "DOY", *required_parameters) if p not in index_of]
    if missing:
        raise PowerIncompleteError(f"POWER CSV is missing column(s) {missing}; columns served: {columns}")

    series = {name: [] for name in columns}
    for raw in lines[column_index + 1:]:
        if not raw.strip():
            continue
        cells = raw.split(",")
        if len(cells) != len(columns):
            raise PowerIncompleteError(f"POWER CSV row has {len(cells)} cells for {len(columns)} columns: {raw!r}")
        for name, cell in zip(columns, cells):
            series[name].append(cell)
    if not series["YEAR"]:
        raise PowerIncompleteError("POWER CSV has a column line but no data rows")

    parsed = {
        "latitude": latitude,
        "longitude": longitude,
        "cell_elevation_m": elevation,
        "cell_description": description,
        "parameters": parameters,
        "header_lines": header,
        "columns": columns,
        "fill_value": fill_value,
        "year": [int(v) for v in series["YEAR"]],
        "doy": [int(v) for v in series["DOY"]],
    }
    for name in columns:
        if name in ("YEAR", "DOY"):
            continue
        parsed[name] = [float(v) for v in series[name]]
    parsed["years"] = sorted(set(parsed["year"]))
    parsed["row_count"] = len(parsed["year"])
    return parsed


# ======================================================================
# The derivation -- pure
# ======================================================================


def derive_wind(parsed: dict, seasons=SEASONS) -> dict:
    """
    The wind block:

        {
          'convention': 'from',           # direction the wind comes FROM
          'sectors': ['N', 'NE', ...],
          'years': [...], 'period': {'start', 'end'},
          'fill_days': int,               # rows dropped for a fill value
          'seasons': {
            'winter': {'months': (12, 1, 2), 'days': int,
                       'sector_counts': {'N': n, ...},
                       'sector_frequency': {'N': share, ...},   # sums to 1
                       'mean_speed_m_s': float,
                       'prevailing_degrees': float | None,
                       'prevailing_sector': 'SW' | None},
            'summer': {...},
          },
        }
    """
    fill = parsed["fill_value"]
    by_season = {name: ([], []) for name in seasons}
    month_to_season = {m: name for name, months in seasons.items() for m in months}
    fill_days = 0
    for year, doy, speed, direction in zip(parsed["year"], parsed["doy"], parsed["WS10M"], parsed["WD10M"]):
        if speed == fill or direction == fill:
            fill_days += 1
            continue
        season = month_to_season.get(power_date(year, doy).month)
        if season is None:
            continue
        by_season[season][0].append(speed)
        by_season[season][1].append(direction)

    out = {}
    for name, (speeds, directions) in by_season.items():
        counts = {sector: 0 for sector in SECTORS}
        for d in directions:
            counts[compass_sector(d)] += 1
        days = len(speeds)
        prevailing = prevailing_direction_degrees(directions, speeds)
        out[name] = {
            "months": tuple(seasons[name]),
            "days": days,
            "sector_counts": counts,
            "sector_frequency": {s: (counts[s] / days if days else 0.0) for s in SECTORS},
            "mean_speed_m_s": (sum(speeds) / days) if days else None,
            "prevailing_degrees": prevailing,
            "prevailing_sector": compass_sector(prevailing) if prevailing is not None else None,
        }
    return {
        "convention": "from",
        "sectors": list(SECTORS),
        "years": list(parsed["years"]),
        "period": {"start": parsed["years"][0], "end": parsed["years"][-1]},
        "fill_days": fill_days,
        "seasons": out,
    }


# ======================================================================
# The fetch
# ======================================================================


def _request_csv(latitude: float, longitude: float, start: int, end: int, max_retries: int) -> str:
    params = {
        "parameters": ",".join(POWER_PARAMETERS),
        "community": POWER_COMMUNITY,
        "longitude": f"{longitude:.6f}",
        "latitude": f"{latitude:.6f}",
        "start": f"{start}0101",
        "end": f"{end}1231",
        "format": "CSV",
    }
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        timeout = 30 + (attempt * 30)
        try:
            response = requests.get(POWER_DAILY_ENDPOINT, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


@fetch_attempts.publishes
def get_power_wind_for_point(latitude: float, longitude: float, years, max_retries: int = 2) -> dict:
    """
    POWER daily WS10M/WD10M at one point for the calendar years `years`
    (the Daymet climate block's own list, so the two agree), parsed.
    Raises requests.RequestException if the service does not answer
    (after the retry loop), PowerIncompleteError if it answers without
    every requested year.
    """
    years = sorted(years)
    text = _request_csv(latitude, longitude, years[0], years[-1], max_retries)
    parsed = parse_power_csv(text)
    missing = [y for y in years if y not in set(parsed["years"])]
    if missing:
        raise PowerIncompleteError(f"POWER served no rows for years {missing} of {years[0]}-{years[-1]}")
    parsed["years_requested"] = years
    return parsed


def summarize_wind(block: dict) -> str:
    parts = []
    for name, season in block["seasons"].items():
        speed = season["mean_speed_m_s"]
        parts.append(
            f"{name} {season['days']} days, mean {speed:.2f} m/s, prevailing from "
            f"{season['prevailing_sector']} ({season['prevailing_degrees']:.0f} deg)"
        )
    return f"Wind {block['period']['start']}-{block['period']['end']} (direction FROM): " + "; ".join(parts)


if __name__ == "__main__":
    latitude, longitude = 40.6443, -79.9821
    print(f"Fetching POWER daily wind for ({latitude}, {longitude}), 1995-2024...\n")
    try:
        print(summarize_wind(derive_wind(get_power_wind_for_point(latitude, longitude, range(1995, 2025)))))
    except Exception as e:
        print(f"Request failed: {e}")
