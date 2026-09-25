"""
daymet_data.py

Fetches Daymet daily surface weather for one point -- the parcel centroid --
from ORNL DAAC's Single Pixel Extraction service, and parses the CSV it
returns into per-variable daily arrays.

    get_daymet_daily_for_point(lat, lon)      -> the parsed dict (network)
    parse_daymet_csv(text)                    -> the parsed dict (pure)
    daymet_date(year, yday)                   -> datetime.date
    most_recent_complete_years(today, count)  -> [year, ...]

THE SOURCE, AND WHY IT IS THIS ONE. Daymet (Thornton et al., ORNL DAAC) is
a 1 km gridded daily surface weather product for North America, 1980 to the
most recent complete calendar year, distributed under NASA's EOSDIS data use
policy: no restriction on use, citation required. It replaces Open-Meteo for
the SITE DATA REPORT specifically (decision D5 of site-data-report-proposal.md:
Open-Meteo's free tier is non-commercial and the report is a paid product).
Layer 1's Open-Meteo fetch was never replaced by this module; it was
removed outright when the narrated report, its only reader, retired
(decision D4; see parcel_data.py).

WHAT COMES BACK. The service returns a CSV with a header block -- the
point's latitude/longitude, its Lambert Conformal Conic coordinates, the
tile, THE 1 KM CELL'S ELEVATION, the software version, and a "How to cite"
line carrying the citation and DOI of the version actually served --
followed by one column-name line and one row per day. The parsed dict keeps
the citation and DOI so the report's source footer renders the citation of
what was FETCHED, never a string typed into a template.

THE CELL ELEVATION IS NOT THE PARCEL'S. It is the mean elevation of a
1 km x 1 km Daymet cell, kept on the dict as `cell_elevation_m` for
diagnostics only. Nothing may display it as the property's elevation -- the
parcel's own range comes off the DEM (raster_grid.elevation_range_in_polygon).

THREE DATA FACTS EVERY CONSUMER MUST HONOUR (each has a test in
test_daymet_data.py / test_climate_report.py that fails if it is ignored):

  1. DAYMET USES A 365-DAY YEAR. In a leap year it keeps February 29 and
     DROPS December 31. A row is (year, yday) with yday 1..365, and its
     calendar date is `date(year, 1, 1) + (yday - 1) days` -- so yday 60 is
     Feb 29 in a leap year and Mar 1 otherwise, and yday 365 is Dec 30 in a
     leap year and Dec 31 otherwise. daymet_date() is the one place that
     arithmetic lives; no consumer may carry a day-of-month table.

  2. `srad` IS NOT A DAILY TOTAL. It is the mean incident shortwave flux over
     the DAYLIT part of the day, in W/m^2. Daily energy is srad x dayl
     (joules per m^2); divide by 3.6e6 for kWh/m^2/day. Treating srad as a
     24-hour mean overstates energy by roughly the day length in hours and
     still looks plausible. See climate_report.daily_solar_kwh_m2().

  3. EVERYTHING IS METRIC. degC, mm/day, W/m^2, seconds. The dict stores
     what the service returned; unit conversion belongs at the formatting
     boundary, the same rule the retired narrated report applied.

COLUMNS ARE LOOKED UP BY NAME, NEVER BY POSITION. The service orders
columns alphabetically by variable name, not in request order -- the
fixture's header reads `year,yday,dayl (s),prcp (mm/day),srad (W/m^2),tmax
(deg c),tmin (deg c)` for a request of `tmax,tmin,prcp,srad,dayl`. A
positional parser would silently read prcp as day length the day a variable
is added or removed. parse_daymet_csv() maps each column by the variable
name before the unit parenthesis and refuses a file missing any it needs.

THE YEAR WINDOW. `most_recent_complete_years(today, 30)` is the 30 calendar
years ending with last year. Daymet publishes a calendar year some months
into the following year, so early in a year the most recent complete year
may not yet be served -- VERIFY-BEFORE-BUILD: how the service answers a
request for a year it does not have (an error, or the year silently absent)
is not verified from the sandbox. get_daymet_daily_for_point() handles both
the same way: if the response does not contain every requested year as a
complete 365-row year, it retries ONCE with the window shifted back one
year, and raises DaymetIncompleteError if that is still short. The years
actually used are on the dict (`years`), which is what the footer prints.

Docs: https://daymet.ornl.gov/single-pixel/  (Single Pixel Extraction Tool)
      https://daymet.ornl.gov/overview      (365-day calendar, variable
                                              definitions, units)
"""

import re
from datetime import date, timedelta
from typing import Optional

import requests

import fetch_attempts

DAYMET_SINGLE_PIXEL_ENDPOINT = "https://daymet.ornl.gov/single-pixel/api/data"

# The variables the report needs, in the order the request names them. The
# RESPONSE does not honour this order (see the module docstring), which is
# the whole reason the parser keys on names.
DAYMET_VARIABLES = ("tmax", "tmin", "prcp", "srad", "dayl")

# The climate section's window: the 30 most recent complete calendar years.
CLIMATE_YEARS = 30

# Daymet's own calendar. Every year has exactly this many rows.
DAYS_PER_DAYMET_YEAR = 365

# The column-name line is the first line whose comma-separated cells name
# BOTH `year` and `yday` -- found by content, not by position or by a
# leading cell, for the same reason the columns themselves are found by
# name: nothing here assumes the service's ordering.
def _is_column_line(line: str) -> bool:
    names = {_variable_name(cell) for cell in line.split(",")}
    return "year" in names and "yday" in names


# Header-line patterns. Each is anchored at the start of its line so a
# citation that happens to mention "Elevation" cannot be read as one.
_LATLON_RE = re.compile(r"^Latitude:\s*(-?[\d.]+)\s+Longitude:\s*(-?[\d.]+)")
_ELEVATION_RE = re.compile(r"^Elevation:\s*(-?[\d.]+)\s*meters")
_VERSION_RE = re.compile(r"Daymet Software Version\s*([\d.]+)")
_CITE_PREFIX = "How to cite:"
_DOI_RE = re.compile(r"https?://doi\.org/\S+")


class DaymetIncompleteError(RuntimeError):
    """
    The service answered, but not with the data asked for: a requested year
    missing or short of 365 rows, a required variable's column absent, or
    the header block not carrying a citation. Distinct from a
    RequestException (the service did not answer) because the two need
    different words on the wire -- see report_data.py, which maps both.
    """


# --- what a fetch of this module's layer cost, published ---------------
#
# PEP 562, the same plumbing every retrying fetch module carries (see
# fetch_attempts.py): the three LAST_FETCH_* names resolve to the CALLING
# THREAD's totals from the last @fetch_attempts.publishes call.
def __getattr__(name):
    return fetch_attempts.published(__name__, name)


# ======================================================================
# The calendar
# ======================================================================


def daymet_date(year: int, yday: int) -> date:
    """
    The calendar date of a Daymet (year, yday) row -- DATA FACT 1.

    `date(year, 1, 1) + (yday - 1)` is exactly Daymet's convention: yday
    counts calendar days from January 1 inclusive, so in a leap year yday 60
    lands on February 29 and yday 365 on December 30 (December 31 is the day
    Daymet drops); in a common year yday 365 is December 31. yday 366 is
    never valid and is refused rather than wrapped into the next year.
    """
    if not 1 <= yday <= DAYS_PER_DAYMET_YEAR:
        raise ValueError(f"Daymet yday must be 1..{DAYS_PER_DAYMET_YEAR}, got {yday!r} (year {year})")
    return date(year, 1, 1) + timedelta(days=yday - 1)


def most_recent_complete_years(today: Optional[date] = None, count: int = CLIMATE_YEARS) -> list:
    """
    The `count` calendar years ending with LAST year, ascending. Last year
    is the most recent year that is complete on the calendar; whether Daymet
    has published it yet is the service's answer, handled by the fetch.
    """
    if today is None:
        today = date.today()
    end_year = today.year - 1
    return list(range(end_year - count + 1, end_year + 1))


# ======================================================================
# The parser -- pure, no network
# ======================================================================


def _variable_name(column: str) -> str:
    """`tmax (deg c)` -> `tmax`; `year` -> `year`. The unit parenthesis is
    kept on the dict under `units` so a consumer can print it, but the
    lookup key is the bare variable name the request used."""
    return column.strip().split(" ", 1)[0].strip()


def _column_units(column: str) -> Optional[str]:
    match = re.search(r"\((.*)\)", column)
    return match.group(1).strip() if match else None


def parse_daymet_csv(text: str, required_variables=DAYMET_VARIABLES) -> dict:
    """
    The service's CSV -> one dict:

        {
          'latitude', 'longitude'      the point the service reports (floats),
          'cell_elevation_m'           THE 1 KM CELL'S elevation -- diagnostics
                                       only, never the parcel's (float or None),
          'software_version'           e.g. '4.0' (str or None),
          'citation'                   the "How to cite" line's text, verbatim
                                       after the prefix (str),
          'doi'                        the DOI URL inside it (str or None),
          'header_lines'               the raw header block, for diagnostics,
          'columns'                    the column names as served, in order,
          'units'                      {variable: unit string} as served,
          'years'                      sorted distinct years present,
          'year', 'yday'               parallel int lists, one entry per row,
          'tmax', 'tmin', 'prcp',
          'srad', 'dayl'               parallel float lists, one per row,
                                       in the service's own units (DATA FACT 3),
          'row_count'                  len(year),
        }

    Every variable in `required_variables` must be present as a column or
    DaymetIncompleteError is raised naming what is missing. Columns are
    found BY NAME (see the module docstring); any extra column the service
    adds is parsed and kept under its own name, never mistaken for another.
    Rows are kept in file order; nothing is sorted, so a consumer that needs
    year order groups by `year` rather than assuming it.

    A file with no rows, no column line, or no citation line is refused --
    the footer must print the citation of the version fetched, and a parse
    that quietly returned None for it would print nothing where a source
    belongs.
    """
    lines = text.splitlines()
    header_lines = []
    column_line_index = None
    for index, line in enumerate(lines):
        if _is_column_line(line):
            column_line_index = index
            break
        header_lines.append(line)
    if column_line_index is None:
        raise DaymetIncompleteError("Daymet CSV has no column line naming 'year' and 'yday'")

    latitude = longitude = None
    cell_elevation_m = None
    software_version = None
    citation = None
    for line in header_lines:
        match = _LATLON_RE.match(line)
        if match:
            latitude, longitude = float(match.group(1)), float(match.group(2))
            continue
        match = _ELEVATION_RE.match(line)
        if match:
            cell_elevation_m = float(match.group(1))
            continue
        match = _VERSION_RE.search(line)
        if match:
            software_version = match.group(1)
        if line.startswith(_CITE_PREFIX):
            citation = line[len(_CITE_PREFIX):].strip()
    if not citation:
        raise DaymetIncompleteError("Daymet CSV header carries no 'How to cite:' line")
    doi_match = _DOI_RE.search(citation)
    doi = doi_match.group(0).rstrip(".;,") if doi_match else None

    columns = [c.strip() for c in lines[column_line_index].split(",")]
    names = [_variable_name(c) for c in columns]
    units = {name: _column_units(c) for name, c in zip(names, columns) if _column_units(c)}
    index_of = {name: i for i, name in enumerate(names)}
    missing = [v for v in ("year", "yday", *required_variables) if v not in index_of]
    if missing:
        raise DaymetIncompleteError(
            f"Daymet CSV is missing column(s) {missing}; columns served: {columns}"
        )

    series = {name: [] for name in names}
    for raw in lines[column_line_index + 1:]:
        if not raw.strip():
            continue
        cells = raw.split(",")
        if len(cells) != len(columns):
            raise DaymetIncompleteError(
                f"Daymet CSV row has {len(cells)} cells for {len(columns)} columns: {raw!r}"
            )
        for name, cell in zip(names, cells):
            series[name].append(cell)

    if not series["year"]:
        raise DaymetIncompleteError("Daymet CSV has a column line but no data rows")

    parsed = {
        "latitude": latitude,
        "longitude": longitude,
        "cell_elevation_m": cell_elevation_m,
        "software_version": software_version,
        "citation": citation,
        "doi": doi,
        "header_lines": header_lines,
        "columns": columns,
        "units": units,
        "year": [int(v) for v in series["year"]],
        "yday": [int(v) for v in series["yday"]],
    }
    for name in names:
        if name in ("year", "yday"):
            continue
        parsed[name] = [float(v) for v in series[name]]
    parsed["years"] = sorted(set(parsed["year"]))
    parsed["row_count"] = len(parsed["year"])
    return parsed


def incomplete_years(parsed: dict, years) -> list:
    """
    The years in `years` that are NOT present as a complete Daymet year --
    exactly 365 rows with yday 1..365 each once. Empty means every requested
    year is whole. Checked, not assumed: a service that trimmed a year at
    its publication boundary would otherwise produce a December with no
    frost in it and a spring frost median pulled by a partial year.
    """
    seen = {}
    for year, yday in zip(parsed["year"], parsed["yday"]):
        seen.setdefault(year, set()).add(yday)
    full = set(range(1, DAYS_PER_DAYMET_YEAR + 1))
    return [year for year in years if seen.get(year) != full]


# ======================================================================
# The fetch
# ======================================================================


def _request_csv(latitude: float, longitude: float, years, max_retries: int, variables=DAYMET_VARIABLES) -> str:
    params = {
        "lat": f"{latitude:.6f}",
        "lon": f"{longitude:.6f}",
        "vars": ",".join(variables),
        "years": ",".join(str(y) for y in years),
        "format": "csv",
    }
    last_error = None
    # ATTEMPTS ARE PUBLISHED, NOT SWALLOWED -- fetch_attempts.attempts()
    # yields what range(max_retries + 1) yields and counts each pass, and
    # fetch_attempts.sleep() records the pause. Same progressive timeout
    # every other retrying layer uses.
    for attempt in fetch_attempts.attempts(max_retries):
        timeout = 30 + (attempt * 30)
        try:
            response = requests.get(DAYMET_SINGLE_PIXEL_ENDPOINT, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


@fetch_attempts.publishes
def get_daymet_daily_for_point(
    latitude: float,
    longitude: float,
    years=None,
    max_retries: int = 2,
    today: Optional[date] = None,
    variables=DAYMET_VARIABLES,
) -> dict:
    """
    Daymet daily tmax/tmin/prcp/srad/dayl at one point for `years` (default:
    most_recent_complete_years(today)), parsed -- see parse_daymet_csv() for
    the shape. Raises requests.RequestException if the service does not
    answer (after the retry loop), DaymetIncompleteError if it answers with
    less than every requested year complete.

    THE ONE-YEAR SHIFT. If the FIRST request comes back missing any year
    (the most recent complete calendar year not yet published is the
    expected cause -- see the module docstring's VERIFY note) the window is
    shifted back one year and requested once more. A second shortfall is
    raised, naming the years, rather than reported as a 29-year mean under
    a 30-year label. The shift applies ONLY to the default window: a caller
    that named its years (precipitation_normals, fetching 1991-2020 at a
    station) asked for a fixed period, and a shifted answer would be the
    wrong period under the right label.

    `variables` narrows the request (precipitation alone for a station
    check); the parser then requires only those columns.
    """
    shift_on_incomplete = years is None
    if years is None:
        years = most_recent_complete_years(today)
    years = list(years)

    text = _request_csv(latitude, longitude, years, max_retries, variables)
    parsed = parse_daymet_csv(text, required_variables=variables)
    missing = incomplete_years(parsed, years)
    if missing and not shift_on_incomplete:
        raise DaymetIncompleteError(
            f"Daymet served incomplete data: years {missing} missing or partial for the "
            f"requested window {years[0]}-{years[-1]}"
        )
    if missing:
        shifted = [y - 1 for y in years]
        text = _request_csv(latitude, longitude, shifted, max_retries, variables)
        parsed = parse_daymet_csv(text, required_variables=variables)
        still_missing = incomplete_years(parsed, shifted)
        if still_missing:
            raise DaymetIncompleteError(
                f"Daymet served incomplete data: years {missing} missing or partial for the "
                f"window {years[0]}-{years[-1]}, then {still_missing} for the shifted window "
                f"{shifted[0]}-{shifted[-1]}"
            )
        parsed["years_requested_first"] = years
        years = shifted
    parsed["years_requested"] = years
    return parsed


def summarize_daymet(parsed: dict) -> str:
    """Plain-language summary, same purpose as dem_data.summarize_dem()."""
    return (
        f"Daymet v{parsed.get('software_version')} at ({parsed.get('latitude')}, "
        f"{parsed.get('longitude')}): {parsed['row_count']} daily rows over "
        f"{parsed['years'][0]}-{parsed['years'][-1]} ({len(parsed['years'])} years); "
        f"columns {parsed['columns']}; cell elevation {parsed.get('cell_elevation_m')} m "
        f"(the 1 km cell's, not the parcel's). {parsed['citation']}"
    )


if __name__ == "__main__":
    # The reference parcel's centroid (reference_fixture.REAL_BOUNDARY).
    latitude, longitude = 40.6443, -79.9821
    print(f"Fetching {CLIMATE_YEARS} years of Daymet daily data for ({latitude}, {longitude})...\n")
    try:
        print(summarize_daymet(get_daymet_daily_for_point(latitude, longitude)))
    except Exception as e:
        print(f"Request failed: {e}")
        print("\nNote: this requires internet access to reach daymet.ornl.gov.")
