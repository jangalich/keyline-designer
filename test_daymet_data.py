"""
test_daymet_data.py

Offline checks for daymet_data.py -- the Daymet single-pixel parser, its
calendar, and the fetch's retry/shape contract. The fixture is REAL
service output for the reference parcel (daymet_reference_fixture.csv,
1995-2024); the network is refused by offline_harness.

  1. The fixture's shape: 7 header lines, 10,950 rows, 30 years x 365,
     lat/lon, the 1 km cell elevation, the software version, and the
     citation + DOI read off the header verbatim.
  2. COLUMNS BY NAME, NOT POSITION (data fact: the service orders columns
     alphabetically, not in request order). A CSV with the same rows in a
     different column order, and one with an extra column inserted, parse
     to the SAME per-variable values; a missing required column raises and
     names it.
  3. THE 365-DAY YEAR (data fact 1). yday 60 is Feb 29 in a leap year and
     Mar 1 in a common year; yday 365 is Dec 30 in a leap year and Dec 31
     otherwise; yday 366 is refused. On the fixture, every leap year has
     a Feb 29 row and no Dec 31 row.
  4. THE FETCH: request parameters name the variables and years; the
     window is the 30 years ending last year; a service that does not
     answer raises the RequestException after the retry loop, attempts
     published; a response missing the most recent year shifts the window
     back one year and asks once more; a response still short raises
     DaymetIncompleteError naming the years.
"""

from datetime import date
from unittest import mock

import offline_harness

offline_harness.install()

import daymet_data
import fetch_attempts
from daymet_data import (
    DAYMET_VARIABLES,
    DaymetIncompleteError,
    daymet_date,
    get_daymet_daily_for_point,
    incomplete_years,
    most_recent_complete_years,
    parse_daymet_csv,
)

FIXTURE = "daymet_reference_fixture.csv"
with open(FIXTURE, encoding="utf-8") as _handle:
    FIXTURE_TEXT = _handle.read()

# ======================================================================
# 1. The fixture's shape
# ======================================================================
print("1. the fixture parses to the shape the brief states")

daily = parse_daymet_csv(FIXTURE_TEXT)
assert len(daily["header_lines"]) == 6, daily["header_lines"]          # 6 prose lines + the column line = 7
assert daily["row_count"] == 10950, daily["row_count"]
assert daily["years"] == list(range(1995, 2025)), daily["years"]
assert incomplete_years(daily, daily["years"]) == []
assert daily["latitude"] == 40.6443 and daily["longitude"] == -79.9821
assert daily["cell_elevation_m"] == 355.0
assert daily["software_version"] == "4.0"
assert daily["citation"].startswith("Thornton; M.M.; R. Shrestha;"), daily["citation"]
assert daily["citation"].endswith("https://doi.org/10.3334/ORNLDAAC/2129"), daily["citation"]
assert daily["doi"] == "https://doi.org/10.3334/ORNLDAAC/2129"
assert daily["columns"] == [
    "year", "yday", "dayl (s)", "prcp (mm/day)", "srad (W/m^2)", "tmax (deg c)", "tmin (deg c)"
], daily["columns"]
assert daily["units"] == {"dayl": "s", "prcp": "mm/day", "srad": "W/m^2", "tmax": "deg c", "tmin": "deg c"}
for name in DAYMET_VARIABLES:
    assert len(daily[name]) == 10950, name
# The first row of the fixture, read by name.
assert (daily["year"][0], daily["yday"][0]) == (1995, 1)
assert daily["dayl"][0] == 32935.61 and daily["prcp"][0] == 5.33 and daily["srad"][0] == 144.81
assert daily["tmax"][0] == 3.98 and daily["tmin"][0] == -3.47
print(f"   6 header lines + column line, {daily['row_count']} rows, {len(daily['years'])} years, DOI {daily['doi']}")

# ======================================================================
# 2. Columns by name, never by position
# ======================================================================
print("2. columns are looked up by name -- a reordered or widened CSV parses identically")

_header = FIXTURE_TEXT.splitlines()[:7]
_rows = FIXTURE_TEXT.splitlines()[7:200]


def _rewrite(column_order, extra=None):
    """The fixture's first rows, with columns permuted (and optionally one
    extra column inserted at index 2) -- what a positional parser would get
    wrong."""
    names = _header[6].split(",")
    index = {n: i for i, n in enumerate(names)}
    out = list(_header[:6])
    new_names = list(column_order)
    if extra:
        new_names.insert(2, extra)
    out.append(",".join(new_names))
    for row in _rows:
        cells = row.split(",")
        rebuilt = [cells[index[n]] for n in column_order]
        if extra:
            rebuilt.insert(2, "999")
        out.append(",".join(rebuilt))
    return "\n".join(out)


reference = parse_daymet_csv("\n".join(_header + _rows))
reordered = parse_daymet_csv(
    _rewrite(["tmin (deg c)", "tmax (deg c)", "srad (W/m^2)", "prcp (mm/day)", "dayl (s)", "year", "yday"])
)
widened = parse_daymet_csv(
    _rewrite(["year", "yday", "dayl (s)", "prcp (mm/day)", "srad (W/m^2)", "tmax (deg c)", "tmin (deg c)"],
             extra="swe (kg/m^2)")
)
for name in ("year", "yday", *DAYMET_VARIABLES):
    assert reordered[name] == reference[name], f"reordered columns changed {name}"
    assert widened[name] == reference[name], f"an extra column shifted {name}"
assert widened["swe"] == [999.0] * len(_rows), "the extra column is kept under its own name"
# The positional reading of the reordered file would have put tmin where
# year is -- assert the values genuinely differ so the test above means
# something.
assert reordered["prcp"] != reordered["dayl"]

try:
    parse_daymet_csv(_rewrite(["year", "yday", "dayl (s)", "prcp (mm/day)", "srad (W/m^2)", "tmax (deg c)"]))
except DaymetIncompleteError as exc:
    assert "tmin" in str(exc), exc
else:
    raise AssertionError("a CSV missing a required column must raise, not parse")

try:
    parse_daymet_csv("\n".join(_header[:6]) + "\nyear,yday,tmax (deg c)\n")
except DaymetIncompleteError:
    pass
else:
    raise AssertionError("a CSV with no data rows must raise")

try:
    parse_daymet_csv("\n".join(_header[:5] + [_header[6]] + _rows[:3]))
except DaymetIncompleteError as exc:
    assert "cite" in str(exc)
else:
    raise AssertionError("a CSV with no citation line must raise -- the footer prints the fetched citation")
print("   reordered, widened, missing-column and no-citation cases all behave")

# ======================================================================
# 3. The 365-day year
# ======================================================================
print("3. Daymet's 365-day year: Feb 29 kept, Dec 31 dropped, built from year + yday only")

assert daymet_date(1996, 60) == date(1996, 2, 29)     # leap year: yday 60 IS Feb 29
assert daymet_date(1995, 60) == date(1995, 3, 1)      # common year: yday 60 is Mar 1
assert daymet_date(1996, 365) == date(1996, 12, 30)   # leap year: the last row is Dec 30
assert daymet_date(1995, 365) == date(1995, 12, 31)   # common year: Dec 31
assert daymet_date(2000, 1) == date(2000, 1, 1)
for bad in (0, 366):
    try:
        daymet_date(1996, bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"yday {bad} must be refused")

# A day-of-month table would put yday 60 on Mar 1 every year and yday 365
# on Dec 31 every year; the fixture's leap years say otherwise.
leap_years = [y for y in daily["years"] if y % 4 == 0]
assert leap_years == [1996, 2000, 2004, 2008, 2012, 2016, 2020, 2024]
dates = [daymet_date(y, d) for y, d in zip(daily["year"], daily["yday"])]
by_year = {}
for d in dates:
    by_year.setdefault(d.year, set()).add((d.month, d.day))
for y in daily["years"]:
    if y in leap_years:
        assert (2, 29) in by_year[y] and (12, 31) not in by_year[y], y
    else:
        assert (2, 29) not in by_year[y] and (12, 31) in by_year[y], y
    assert len(by_year[y]) == 365
print(f"   {len(leap_years)} leap years each carry Feb 29 and lack Dec 31; every year has 365 distinct dates")

# ======================================================================
# 4. The fetch
# ======================================================================
print("4. the fetch: window, parameters, retry, the one-year shift, and the incomplete raise")

assert most_recent_complete_years(date(2026, 9, 21)) == list(range(1996, 2026))
assert most_recent_complete_years(date(2026, 1, 1), 3) == [2023, 2024, 2025]

# 4a. Service does not answer: the harness refuses every request; the
# RequestException surfaces after the retry loop and the attempts are
# published on the module.
fetch_attempts.clear(daymet_data.__name__)
try:
    get_daymet_daily_for_point(40.6443, -79.9821, years=[2023, 2024])
except offline_harness.OfflineNetworkError:
    pass
else:
    raise AssertionError("with no network the fetch must raise the RequestException")
assert daymet_data.LAST_FETCH_ATTEMPTS == 3, daymet_data.LAST_FETCH_ATTEMPTS
assert any(host == "daymet.ornl.gov" for _, host in offline_harness.refused())

# 4b. Parameters: the request names every variable, the years as a list,
# csv format, and the point.
captured = []


def _fake_get(url, params=None, timeout=None):
    captured.append((url, dict(params), timeout))
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.text = FIXTURE_TEXT
    return response


with mock.patch.object(daymet_data.requests, "get", _fake_get):
    parsed = get_daymet_daily_for_point(40.6443, -79.9821, today=date(2025, 6, 1))
url, params, timeout = captured[0]
assert url == daymet_data.DAYMET_SINGLE_PIXEL_ENDPOINT
assert params["vars"] == "tmax,tmin,prcp,srad,dayl"
assert params["years"] == ",".join(str(y) for y in range(1995, 2025))
assert params["format"] == "csv" and params["lat"] == "40.644300" and params["lon"] == "-79.982100"
assert timeout == 30
assert parsed["years_requested"] == list(range(1995, 2025)) and "years_requested_first" not in parsed
assert parsed["row_count"] == 10950

# 4c. The one-year shift: asked for 1996-2025 (2025 not yet published), the
# fixture answers 1995-2024; the fetch notices 2025 missing, shifts the
# window back one year and asks again -- two requests, the second for
# 1995-2024, and the dict records both windows.
captured.clear()
with mock.patch.object(daymet_data.requests, "get", _fake_get):
    parsed = get_daymet_daily_for_point(40.6443, -79.9821, today=date(2026, 3, 1))
assert len(captured) == 2, [c[1]["years"][-4:] for c in captured]
assert captured[0][1]["years"].endswith("2025") and captured[1][1]["years"].endswith("2024")
assert parsed["years_requested_first"] == list(range(1996, 2026))
assert parsed["years_requested"] == list(range(1995, 2025))
assert parsed["years"] == list(range(1995, 2025))

# 4d. Still short after the shift: raises, naming years, never a 29-year
# mean under a 30-year label.
captured.clear()
with mock.patch.object(daymet_data.requests, "get", _fake_get):
    try:
        get_daymet_daily_for_point(40.6443, -79.9821, today=date(2027, 3, 1))
    except DaymetIncompleteError as exc:
        assert "2026" in str(exc) and "2025" in str(exc), exc
    else:
        raise AssertionError("a window the service cannot fill after one shift must raise")
assert len(captured) == 2

# 4e. A partial year (a trimmed December) is incomplete, not silently a
# short year.
trimmed = "\n".join(FIXTURE_TEXT.splitlines()[:-5])
partial = parse_daymet_csv(trimmed)
assert incomplete_years(partial, partial["years"]) == [2024]
print("   window ends last year; vars/years/format on the request; 3 attempts published; shift and raise both proven")

print("\ntest_daymet_data.py: all sections passed")
print(offline_harness.summary())
