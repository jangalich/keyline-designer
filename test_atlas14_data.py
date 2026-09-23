"""
test_atlas14_data.py

Offline checks for atlas14_data.py -- the NOAA Atlas 14 labelled-CSV
parser, the design-storm selection, and the fetch's retry/shape
contract. The fixture is REAL server output for the reference parcel
(atlas14_reference_fixture.csv, partial-duration depths in inches); the
network is refused by offline_harness.

  1. The fixture's shape: Volume 2 Version 3, partial duration, inches,
     ten ARIs, nineteen durations, the point served, records through
     2000; the 24-hour 100-year depth read under its own labels.
  2. The design-storm table: 60-min and 24-hr rows at 2, 10, 25 and 100
     years, exactly the values in the fixture; a duration or ARI the
     server did not return raises rather than leaving a blank.
  3. REFUSALS: the server's outside-coverage answer (result = 'none',
     ErrorMsg ...) raises carrying the server's message; the annual
     series under a partial-duration request is refused; a row with the
     wrong number of values is refused; a header with no unit is refused.
  4. THE FETCH: request parameters name the series, unit and point; a
     server that does not answer raises the RequestException after the
     retry loop with attempts published.
"""

from unittest import mock

import offline_harness

offline_harness.install()

import requests

import atlas14_data
import fetch_attempts
from atlas14_data import (
    DESIGN_ARIS,
    DESIGN_DURATIONS,
    SERIES_PARTIAL_DURATION,
    Atlas14IncompleteError,
    design_storms,
    get_atlas14_for_point,
    parse_atlas14_csv,
)

FIXTURE = "atlas14_reference_fixture.csv"
with open(FIXTURE, encoding="utf-8") as _handle:
    FIXTURE_TEXT = _handle.read()

# ======================================================================
# 1. The fixture's shape
# ======================================================================
print("1. the fixture parses to Volume 2 Version 3, partial duration, inches, 10 ARIs x 19 durations")
parsed = parse_atlas14_csv(FIXTURE_TEXT)
assert (parsed["atlas"], parsed["volume"], parsed["version"]) == ("14", "2", "3"), parsed
assert parsed["source_line"] == "NOAA Atlas 14 Volume 2 Version 3"
assert parsed["series"] == "Partial duration"
assert parsed["project_area"] == "Ohio River Basin"
assert parsed["units"] == "inches"
assert (parsed["latitude"], parsed["longitude"]) == (40.6443, -79.9821)
assert parsed["aris"] == [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000]
assert len(parsed["durations"]) == 19 and parsed["durations"][0] == "5-min" and parsed["durations"][-1] == "60-day"
assert parsed["durations"][4] == "60-min" and parsed["durations"][9] == "24-hr"
assert parsed["depths"]["24-hr"][100] == 4.98 and parsed["depths"]["24-hr"][2] == 2.38
assert parsed["depths"]["60-min"][25] == 2.07 and parsed["depths"]["5-min"][1] == 0.317
assert parsed["record_ends"] == 2000
for duration in parsed["durations"]:
    assert sorted(parsed["depths"][duration]) == parsed["aris"], duration
    row = [parsed["depths"][duration][a] for a in parsed["aris"]]
    assert row == sorted(row), f"{duration}: depths must not decrease with return period"
print(f"   {parsed['source_line']}, {parsed['series']}, 24-hr 100-yr {parsed['depths']['24-hr'][100]} {parsed['units']}")

# ======================================================================
# 2. The design-storm table
# ======================================================================
print("2. design_storms() picks the 60-min and 24-hr rows at 2/10/25/100 years")
storms = design_storms(parsed)
assert storms["units"] == "inches" and storms["aris"] == list(DESIGN_ARIS)
assert [row["duration"] for row in storms["rows"]] == list(DESIGN_DURATIONS)
assert storms["rows"][0]["depths"] == {2: 1.19, 10: 1.74, 25: 2.07, 100: 2.58}
assert storms["rows"][1]["depths"] == {2: 2.38, 10: 3.34, 25: 3.96, 100: 4.98}
for missing_duration in (("24-hr", "3-day-x"),):
    try:
        design_storms(parsed, durations=missing_duration)
    except Atlas14IncompleteError as exc:
        assert "3-day-x" in str(exc)
    else:
        raise AssertionError("a duration the server did not return must raise")
try:
    design_storms(parsed, aris=(2, 10, 75))
except Atlas14IncompleteError as exc:
    assert "75" in str(exc)
else:
    raise AssertionError("an ARI the server did not return must raise")
print("   60-min 2.58 in and 24-hr 4.98 in at 100 years; a missing duration or ARI raises")

# ======================================================================
# 3. Refusals
# ======================================================================
print("3. outside coverage, the wrong series, a short row and a unitless header are refused")
outside = "result = 'none';\nErrorMsg =  'Error 3.0: Selected location is not within a project area';\n"
try:
    parse_atlas14_csv(outside)
except Atlas14IncompleteError as exc:
    assert "not within a project area" in str(exc), exc
else:
    raise AssertionError("the outside-coverage answer must raise")

annual = FIXTURE_TEXT.replace("Time series type: Partial duration", "Time series type: Annual maximum")
try:
    parse_atlas14_csv(annual, expected_series=SERIES_PARTIAL_DURATION)
except Atlas14IncompleteError as exc:
    assert "Annual maximum" in str(exc) and "Partial duration" in str(exc)
else:
    raise AssertionError("the annual series under a partial-duration request must be refused")
assert parse_atlas14_csv(annual, expected_series="ams")["series"] == "Annual maximum"

short = FIXTURE_TEXT.replace("24-hr:, 2.00,2.38,2.91,3.34,3.96,4.46,4.98,5.53,6.31,6.93", "24-hr:, 2.00,2.38")
try:
    parse_atlas14_csv(short)
except Atlas14IncompleteError as exc:
    assert "24-hr" in str(exc)
else:
    raise AssertionError("a short row must be refused")

unitless = FIXTURE_TEXT.replace("Point precipitation frequency estimates (inches)", "Point precipitation frequency estimates")
try:
    parse_atlas14_csv(unitless)
except Atlas14IncompleteError as exc:
    assert "unit" in str(exc)
else:
    raise AssertionError("a header naming no unit must be refused")

metric = FIXTURE_TEXT.replace("(inches)", "(mm)")
assert parse_atlas14_csv(metric)["units"] == "mm"
print("   each refusal names its cause; the unit is read off the header, not assumed")

# ======================================================================
# 4. The fetch
# ======================================================================
print("4. the request names series/units/point; no network raises after the retry loop")
captured = []


def _fake_get(url, params=None, timeout=None):
    captured.append((url, dict(params), timeout))
    response = mock.Mock()
    response.raise_for_status = lambda: None
    response.text = FIXTURE_TEXT
    return response


with mock.patch.object(atlas14_data.requests, "get", _fake_get):
    fetched = get_atlas14_for_point(40.64455, -79.98260)
assert fetched["depths"] == parsed["depths"]
url, params, timeout = captured[0]
assert url == atlas14_data.PFDS_TEXT_ENDPOINT
assert params == {"lat": "40.6446", "lon": "-79.9826", "data": "depth", "units": "english", "series": "pds"}, params
assert timeout == 30

try:
    get_atlas14_for_point(40.6443, -79.9821, max_retries=1)
except requests.exceptions.RequestException:
    pass
else:
    raise AssertionError("with no network the fetch must raise")
assert atlas14_data.LAST_FETCH_ATTEMPTS == 2, atlas14_data.LAST_FETCH_ATTEMPTS
assert any(host == "hdsc.nws.noaa.gov" for _, host in offline_harness.refused())
print(f"   params {params}; {atlas14_data.LAST_FETCH_ATTEMPTS} attempts published on refusal")

print("\ntest_atlas14_data.py: all sections passed")
print(offline_harness.summary())
