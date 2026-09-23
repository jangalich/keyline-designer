"""
atlas14_data.py

Fetches NOAA Atlas 14 point precipitation-frequency estimates for one
point -- the parcel centroid -- from the Precipitation Frequency Data
Server (PFDS), and parses the labelled CSV it returns.

    get_atlas14_for_point(lat, lon)          -> the parsed dict (network)
    parse_atlas14_csv(text)                  -> the parsed dict (pure)
    design_storms(parsed)                    -> the design-storm block

THE SOURCE. NOAA Atlas 14 is the National Weather Service's precipitation
frequency atlas: depths by duration and average recurrence interval
(ARI), estimated from long station records and published by volume
(region). The reference parcel falls in Volume 2 (Ohio River Basin and
surrounding states), Version 3 -- published 2004, revised 2006, built on
records through December 2000. NWS content is public domain and may be
used for any lawful purpose including a paid product, without implying
NWS endorsement (weather.gov/disclaimer). No key, no published rate
limit; a point query answers in well under a second. NOAA Atlas 15 is
scheduled to supersede Atlas 14 with published estimates in 2027; until
then the page must say WHICH Atlas 14 volume and version it quotes and
that the record ends in 2000 -- both come off the response, never a
template.

THE ENDPOINT. The labelled CSV form, cgi-bin/new/fe_text_mean.csv: a
prose header (units in the first line's parenthesis, the volume and
version line, "Time series type", the project area, the point), then a
"by duration for ARI (years):" line naming the return periods and one
labelled row per duration, 5-min to 60-day. The older cgi-bin/hdsc/new/
path answers with a 301 to this one (branch 7 step 0); the raw
cgi_readH5.py form returns the same quantiles as JavaScript arrays with
confidence bounds but no row labels, which is why the labelled form is
the one parsed -- every value is read under the duration and ARI it was
served with, never by position.

PARTIAL-DURATION SERIES, BY DECISION. NRCS's National Engineering
Handbook Part 630 Chapter 4 (2019): NRCS has historically used the
partial duration series for engineering design because a project is
subject to all storms, not only the largest each year; the two series
differ only at frequent return periods (2- and 5-year, partial higher)
and agree from 10 years on. The request asks for `series=pds` and the
parser CHECKS the header says "Partial duration", so a server that
silently answered with the annual series would be refused rather than
tabled.

UNITS ARE AS SERVED. The request asks for `units=english` and the
parser reads the unit off the first line; depths are stored in that
unit under `units` ("inches"). This is the one report layer whose
native product is imperial -- Atlas 14's own tables are in inches -- and
storing what the service returned is the same rule daymet_data.py
applies (DATA FACT 3); the formatting layer prints inches as inches.

OUTSIDE COVERAGE IS COMMON, NOT EXOTIC. The server answers a point
outside every volume with two lines -- `result = 'none';` and
`ErrorMsg = 'Error 3.0: Selected location is not within a project
area';` (verified live for an ocean point AND for Seattle: Washington,
Oregon, Idaho, Montana and Wyoming are not in any Atlas 14 volume; they
remain on NOAA Atlas 2 of 1973 until Atlas 15). parse_atlas14_csv()
raises Atlas14IncompleteError carrying the server's own message, which
report_data maps to no_data_for_parcel, and the section's unavailable
statement can then say "not covered" rather than "did not answer".

Docs: https://hdsc.nws.noaa.gov/pfds/  (PFDS)
      https://www.weather.gov/media/owp/oh/hdsc/docs/Atlas14_Volume2.pdf
"""

import re
from typing import Optional

import requests

import fetch_attempts

PFDS_TEXT_ENDPOINT = "https://hdsc.nws.noaa.gov/cgi-bin/new/fe_text_mean.csv"

SERIES_PARTIAL_DURATION = "pds"
SERIES_ANNUAL_MAXIMUM = "ams"
SERIES_LABELS = {SERIES_PARTIAL_DURATION: "Partial duration", SERIES_ANNUAL_MAXIMUM: "Annual maximum"}

# The report's design-storm table: these durations (as the server labels
# them) at these return periods, in years.
DESIGN_DURATIONS = ("60-min", "24-hr")
DESIGN_ARIS = (2, 10, 25, 100)

# Atlas 14 Volume 2's record ends in December 2000 (NOAA Atlas 14 Vol. 2
# Version 3, section 4.1). Other volumes end in other years; this table
# is what the footer prints when the served volume is known, and None
# otherwise so nothing invents a year.
RECORD_ENDS = {"2": 2000}

_UNITS_RE = re.compile(r"\((inches|mm|millimeters)\)", re.IGNORECASE)
_VOLUME_RE = re.compile(r"NOAA Atlas (\d+) Volume (\d+) Version (\d+)")
_SERIES_RE = re.compile(r"^Time series type:\s*(.+?)\s*$")
_AREA_RE = re.compile(r"^Project area:\s*(.+?)\s*$")
_LAT_RE = re.compile(r"^Latitude:\s*(-?[\d.]+)")
_LON_RE = re.compile(r"^Longitude:\s*(-?[\d.]+)")
_ARI_LINE_RE = re.compile(r"^by duration for ARI \(years\):\s*,\s*(.+)$")
_ROW_RE = re.compile(r"^([\w\-]+):\s*,\s*(.+)$")
_ERROR_RE = re.compile(r"^ErrorMsg\s*=\s*'(.*)'\s*;?\s*$")


class Atlas14IncompleteError(RuntimeError):
    """The server answered, but not with a table: no ARI line (a point
    outside every volume), a series other than the one asked for, a row
    with the wrong number of values, or no unit in the header."""


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


def parse_atlas14_csv(text: str, expected_series: str = SERIES_PARTIAL_DURATION) -> dict:
    """
    The labelled CSV -> one dict:

        {
          'atlas', 'volume', 'version'   e.g. '14', '2', '3' (str or None),
          'source_line'                  'NOAA Atlas 14 Volume 2 Version 3', verbatim,
          'series'                       'Partial duration' as served,
          'project_area'                 'Ohio River Basin',
          'latitude', 'longitude'        the point served (floats),
          'units'                        'inches' | 'mm', off the first line,
          'aris'                         [1, 2, 5, ...] years, as served,
          'durations'                    ['5-min', ..., '60-day'], as served,
          'depths'                       {duration: {ari: depth}},
          'record_ends'                  2000 for Volume 2, else None,
          'header_lines'                 the prose header, for diagnostics,
        }

    Refuses (Atlas14IncompleteError) a text with no ARI line, a series
    other than `expected_series`, or a row whose count differs from the
    ARI count.
    """
    lines = text.splitlines()
    for line in lines:
        error = _ERROR_RE.match(line.strip())
        if error:
            raise Atlas14IncompleteError(f"Atlas 14 server: {error.group(1)}")
    header = []
    aris = None
    rows = []
    for line in lines:
        stripped = line.strip()
        ari_match = _ARI_LINE_RE.match(stripped)
        if ari_match:
            aris = [int(float(v)) for v in ari_match.group(1).split(",") if v.strip()]
            continue
        if aris is None:
            header.append(line)
            continue
        row_match = _ROW_RE.match(stripped)
        if row_match:
            rows.append((row_match.group(1), [float(v) for v in row_match.group(2).split(",") if v.strip()]))
        elif rows and not stripped:
            break   # the blank line after the table
    if aris is None:
        raise Atlas14IncompleteError("Atlas 14 response carries no 'by duration for ARI (years)' line -- outside coverage?")
    if not rows:
        raise Atlas14IncompleteError("Atlas 14 response has an ARI line but no duration rows")

    units = source_line = series = area = None
    atlas = volume = version = None
    latitude = longitude = None
    for line in header:
        stripped = line.strip()
        m = _UNITS_RE.search(stripped)
        if m and units is None:
            units = "mm" if m.group(1).lower().startswith("m") else "inches"
        m = _VOLUME_RE.search(stripped)
        if m:
            atlas, volume, version = m.group(1), m.group(2), m.group(3)
            source_line = m.group(0)
        m = _SERIES_RE.match(stripped)
        if m:
            series = m.group(1)
        m = _AREA_RE.match(stripped)
        if m:
            area = m.group(1)
        m = _LAT_RE.match(stripped)
        if m:
            latitude = float(m.group(1))
        m = _LON_RE.match(stripped)
        if m:
            longitude = float(m.group(1))
    if units is None:
        raise Atlas14IncompleteError("Atlas 14 response names no unit in its first line")
    expected_label = SERIES_LABELS[expected_series]
    if series is None or series.lower() != expected_label.lower():
        raise Atlas14IncompleteError(f"Atlas 14 response is the {series!r} series, not {expected_label!r}")

    depths = {}
    durations = []
    for label, values in rows:
        if len(values) != len(aris):
            raise Atlas14IncompleteError(f"Atlas 14 row {label!r} has {len(values)} values for {len(aris)} ARIs")
        durations.append(label)
        depths[label] = dict(zip(aris, values))

    return {
        "atlas": atlas,
        "volume": volume,
        "version": version,
        "source_line": source_line,
        "series": series,
        "project_area": area,
        "latitude": latitude,
        "longitude": longitude,
        "units": units,
        "aris": aris,
        "durations": durations,
        "depths": depths,
        "record_ends": RECORD_ENDS.get(volume) if atlas == "14" else None,
        "header_lines": header,
    }


def design_storms(parsed: dict, durations=DESIGN_DURATIONS, aris=DESIGN_ARIS) -> dict:
    """
    {'units', 'aris': [...], 'rows': [{'duration', 'depths': {ari: depth}}]}
    for the report's table. A duration or ARI the server did not return
    raises rather than leaving a hole a template would render as blank.
    """
    rows = []
    for duration in durations:
        if duration not in parsed["depths"]:
            raise Atlas14IncompleteError(f"Atlas 14 response has no {duration!r} row; served {parsed['durations']}")
        served = parsed["depths"][duration]
        missing = [a for a in aris if a not in served]
        if missing:
            raise Atlas14IncompleteError(f"Atlas 14 {duration} row lacks ARIs {missing}; served {sorted(served)}")
        rows.append({"duration": duration, "depths": {a: served[a] for a in aris}})
    return {"units": parsed["units"], "aris": list(aris), "rows": rows}


def _request_csv(latitude: float, longitude: float, series: str, units: str, max_retries: int) -> str:
    params = {"lat": f"{latitude:.4f}", "lon": f"{longitude:.4f}", "data": "depth", "units": units, "series": series}
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        timeout = 30 + (attempt * 30)
        try:
            response = requests.get(PFDS_TEXT_ENDPOINT, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


@fetch_attempts.publishes
def get_atlas14_for_point(
    latitude: float,
    longitude: float,
    series: str = SERIES_PARTIAL_DURATION,
    units: str = "english",
    max_retries: int = 2,
) -> dict:
    """Atlas 14 depths at one point, parsed -- see parse_atlas14_csv().
    Raises requests.RequestException if the server does not answer
    (after the retry loop), Atlas14IncompleteError if it answers without
    a table for this point."""
    text = _request_csv(latitude, longitude, series, units, max_retries)
    return parse_atlas14_csv(text, expected_series=series)


def summarize_atlas14(parsed: dict) -> str:
    storms = design_storms(parsed)
    cells = "; ".join(
        f"{row['duration']} " + ", ".join(f"{a}-yr {row['depths'][a]}" for a in storms["aris"]) for row in storms["rows"]
    )
    return (
        f"{parsed['source_line']} ({parsed['project_area']}), {parsed['series']} series, {parsed['units']}, "
        f"records through {parsed['record_ends']}: {cells}"
    )


if __name__ == "__main__":
    latitude, longitude = 40.6443, -79.9821
    print(f"Fetching Atlas 14 estimates for ({latitude}, {longitude})...\n")
    try:
        print(summarize_atlas14(get_atlas14_for_point(latitude, longitude)))
    except Exception as e:
        print(f"Request failed: {e}")
