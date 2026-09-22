"""
test_power_wind_data.py

Offline checks for power_wind_data.py -- the NASA POWER daily CSV
parser, the direction convention, the vector mean, and the seasonal
derivation. Two fixtures are REAL service output for the reference
parcel: power_wind_reference_fixture.csv (daily WS10M/WD10M, 1995-2024)
and power_hourly_reference_fixture.json (hourly, January 2020). The
network is refused by offline_harness.

  1. The fixture's shape: 10,958 rows (30 years, 8 leap days), the point,
     the MERRA-2 cell description and elevation, the parameter units, no
     fill values.
  2. THE CONVENTION IS "FROM": compass_sector() puts 0 in N and 270 in W,
     sector boundaries fall at 22.5 degrees either side, and a season of
     days all from 270 is reported as prevailing from W, never E.
  3. THE VECTOR MEAN: 350 and 10 average to 0, not 180; weights pull the
     mean toward the stronger day; balanced winds give None.
  4. DAILY WD10M IS A VECTOR MEAN -- the step 0 finding, tested: for every
     day of the hourly fixture, the speed-weighted vector mean of the 24
     hourly directions reproduces the daily WD10M to within 0.15 degrees,
     and the arithmetic mean does not; the daily WS10M is the scalar mean
     of hourly speeds.
  5. THE CALENDAR: DOY 366 exists in 2024 and is refused in 2023.
  6. derive_wind() on synthetic data: winter is Dec/Jan/Feb and summer
     Jun/Jul/Aug, sector frequencies sum to one, a fill day is dropped and
     counted, and the prevailing sector follows the vector mean.
  7. THE FETCH: the request names both parameters, the community and the
     window; a missing year raises PowerIncompleteError; no network raises
     the RequestException with attempts published.
"""

import json
import math
from datetime import date
from unittest import mock

import offline_harness

offline_harness.install()

import requests

import power_wind_data
from power_wind_data import (
    SECTORS,
    PowerIncompleteError,
    compass_sector,
    derive_wind,
    get_power_wind_for_point,
    parse_power_csv,
    power_date,
    prevailing_direction_degrees,
)

with open("power_wind_reference_fixture.csv", encoding="utf-8") as _handle:
    FIXTURE_TEXT = _handle.read()
with open("power_hourly_reference_fixture.json", encoding="utf-8") as _handle:
    HOURLY = json.load(_handle)


def _close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def _angle_error(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


# ======================================================================
# 1. The fixture's shape
# ======================================================================
print("1. the daily fixture parses to 30 years of two parameters at the parcel's MERRA-2 cell")
daily = parse_power_csv(FIXTURE_TEXT)
assert daily["row_count"] == 10958, daily["row_count"]          # 30 x 365 + 8 leap days
assert daily["years"] == list(range(1995, 2025))
assert (daily["latitude"], daily["longitude"]) == (40.6443, -79.9821)
assert daily["cell_elevation_m"] == 332.12
assert daily["cell_description"] == "Average for 0.5 x 0.625 degree lat/lon region", daily["cell_description"]
assert daily["parameters"]["WS10M"]["units"] == "m/s" and daily["parameters"]["WD10M"]["units"] == "Degrees"
assert daily["columns"] == ["YEAR", "DOY", "WS10M", "WD10M"]
assert daily["fill_value"] == -999.0
assert not any(v == -999.0 for v in daily["WS10M"]) and not any(v == -999.0 for v in daily["WD10M"])
assert (daily["year"][0], daily["doy"][0], daily["WS10M"][0], daily["WD10M"][0]) == (1995, 1, 2.47, 248.6)
# POWER serves the range 0.0..360.0 inclusive (one day in the fixture is exactly 360.0); the sector function folds it.
assert all(0.0 <= d <= 360.0 for d in daily["WD10M"]) and all(s >= 0.0 for s in daily["WS10M"])
assert sum(1 for d in daily["WD10M"] if d == 360.0) == 1
print(f"   {daily['row_count']} rows, cell {daily['cell_description']}, elevation {daily['cell_elevation_m']} m")

# ======================================================================
# 2. The convention
# ======================================================================
print("2. direction is where the wind comes FROM; sectors are 45 degrees centred on their names")
assert SECTORS == ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
for degrees, sector in ((0, "N"), (45, "NE"), (90, "E"), (135, "SE"), (180, "S"), (225, "SW"), (270, "W"), (315, "NW"), (360, "N")):
    assert compass_sector(degrees) == sector, (degrees, compass_sector(degrees))
assert compass_sector(22.49) == "N" and compass_sector(22.5) == "NE"
assert compass_sector(337.49) == "NW" and compass_sector(337.5) == "N"
assert compass_sector(-10.0) == "N"
westerly = {k: [] for k in ("year", "doy", "WS10M", "WD10M")}
for doy in range(1, 60):
    westerly["year"].append(2001)
    westerly["doy"].append(doy)
    westerly["WS10M"].append(3.0)
    westerly["WD10M"].append(270.0)
westerly.update({"fill_value": -999.0, "years": [2001]})
block = derive_wind(westerly)
assert block["convention"] == "from"
winter = block["seasons"]["winter"]
assert winter["prevailing_sector"] == "W" and _close(winter["prevailing_degrees"], 270.0)
assert winter["sector_frequency"]["W"] == 1.0 and winter["sector_frequency"]["E"] == 0.0
print("   0 -> N, 270 -> W, boundary 337.5 -> N; a westerly season is 'from W', not E")

# ======================================================================
# 3. The vector mean
# ======================================================================
print("3. the prevailing direction is a speed-weighted vector mean")
assert _close(prevailing_direction_degrees([350.0, 10.0], [1.0, 1.0]), 0.0, 1e-6)
arithmetic = (350.0 + 10.0) / 2
assert arithmetic == 180.0 and not _close(prevailing_direction_degrees([350.0, 10.0], [1.0, 1.0]), arithmetic)
weighted = prevailing_direction_degrees([0.0, 90.0], [3.0, 1.0])
assert _close(weighted, math.degrees(math.atan2(1.0, 3.0)), 1e-6), weighted   # 18.43, toward the stronger day
assert prevailing_direction_degrees([0.0, 180.0], [2.0, 2.0]) is None
assert prevailing_direction_degrees([], []) is None
print(f"   350 & 10 -> 0 (arithmetic says 180); 0 at 3 m/s & 90 at 1 m/s -> {weighted:.2f}; balanced -> None")

# ======================================================================
# 4. Daily WD10M is a vector mean of the hourly values
# ======================================================================
print("4. POWER's daily WD10M reproduces the speed-weighted vector mean of its hourly values")
hourly = HOURLY["properties"]["parameter"]
by_day = {}
for key in hourly["WS10M"]:
    by_day.setdefault(key[:8], []).append(key)
daily_index = {(y, d): i for i, (y, d) in enumerate(zip(daily["year"], daily["doy"]))}
vector_errors, arithmetic_errors, speed_errors = [], [], []
for day, keys in sorted(by_day.items()):
    assert len(keys) == 24, (day, len(keys))
    speeds = [hourly["WS10M"][k] for k in keys]
    directions = [hourly["WD10M"][k] for k in keys]
    d = date(int(day[:4]), int(day[4:6]), int(day[6:]))
    row = daily_index[(d.year, d.timetuple().tm_yday)]
    served_direction, served_speed = daily["WD10M"][row], daily["WS10M"][row]
    vector_errors.append(_angle_error(prevailing_direction_degrees(directions, speeds), served_direction))
    arithmetic_errors.append(_angle_error(sum(directions) / 24.0, served_direction))
    speed_errors.append(abs(sum(speeds) / 24.0 - served_speed))
assert len(vector_errors) == 31
assert max(vector_errors) < 0.15, max(vector_errors)
assert max(speed_errors) < 0.01, max(speed_errors)
assert sum(arithmetic_errors) / 31 > 10.0, sum(arithmetic_errors) / 31    # the arithmetic mean is not what POWER serves
print(f"   31 days: vector mean within {max(vector_errors):.2f} deg, arithmetic mean off by {sum(arithmetic_errors) / 31:.1f} deg on average")

# ======================================================================
# 5. The calendar
# ======================================================================
print("5. the real calendar: DOY 366 is Dec 31 2024 and does not exist in 2023")
assert power_date(2024, 366) == date(2024, 12, 31) and power_date(2024, 60) == date(2024, 2, 29)
assert power_date(2023, 365) == date(2023, 12, 31)
for bad in ((2023, 366), (2024, 0), (2024, 367)):
    try:
        power_date(*bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"{bad} must be refused")
assert sum(1 for y, d in zip(daily["year"], daily["doy"]) if d == 366) == 8
print("   8 leap days in the fixture, each a real Dec 31")

# ======================================================================
# 6. derive_wind on synthetic data
# ======================================================================
print("6. seasons are Dec/Jan/Feb and Jun/Jul/Aug; frequencies sum to 1; fill days are dropped and counted")
synthetic = {k: [] for k in ("year", "doy", "WS10M", "WD10M")}
for year in (2001, 2002):
    for doy in range(1, 366):
        d = power_date(year, doy)
        synthetic["year"].append(year)
        synthetic["doy"].append(doy)
        if d.month in (12, 1, 2):
            synthetic["WS10M"].append(4.0)
            synthetic["WD10M"].append(300.0 if doy % 2 else 240.0)   # NW / SW alternating, equal
        elif d.month in (6, 7, 8):
            synthetic["WS10M"].append(1.0 if doy % 3 else 5.0)
            synthetic["WD10M"].append(90.0 if doy % 3 else 180.0)    # E (light) / S (strong)
        else:
            synthetic["WS10M"].append(2.0)
            synthetic["WD10M"].append(0.0)
synthetic["WS10M"][0] = -999.0     # Jan 1 2001: a fill day
synthetic.update({"fill_value": -999.0, "years": [2001, 2002]})
block = derive_wind(synthetic)
assert block["period"] == {"start": 2001, "end": 2002} and block["fill_days"] == 1
winter, summer = block["seasons"]["winter"], block["seasons"]["summer"]
assert winter["months"] == (12, 1, 2) and summer["months"] == (6, 7, 8)
assert winter["days"] == 2 * (31 + 31 + 28) - 1 and summer["days"] == 2 * (30 + 31 + 31)
assert _close(sum(winter["sector_frequency"].values()), 1.0) and _close(sum(summer["sector_frequency"].values()), 1.0)
assert winter["sector_frequency"]["NW"] + winter["sector_frequency"]["SW"] == 1.0
assert _close(winter["mean_speed_m_s"], 4.0)
assert winter["prevailing_sector"] == "W", winter      # halfway between 300 and 240 is 270
# Summer: two light days from E for every strong day from S; the vector
# mean leans S (5 m/s once vs 1 m/s twice), the sector count leans E.
assert summer["sector_counts"]["E"] > summer["sector_counts"]["S"]
assert summer["prevailing_sector"] == "S", summer["prevailing_degrees"]
print(f"   winter {winter['days']} days prevailing {winter['prevailing_sector']}; summer count-leader E but vector-mean S")

# ======================================================================
# 7. The fetch
# ======================================================================
print("7. the request names the parameters and window; a missing year raises; no network raises")
captured = []


def _fake_get(url, params=None, timeout=None):
    captured.append((url, dict(params), timeout))
    response = mock.Mock()
    response.raise_for_status = lambda: None
    response.text = FIXTURE_TEXT
    return response


with mock.patch.object(power_wind_data.requests, "get", _fake_get):
    fetched = get_power_wind_for_point(40.64455, -79.98260, range(1995, 2025))
assert fetched["row_count"] == 10958 and fetched["years_requested"] == list(range(1995, 2025))
url, params, timeout = captured[0]
assert url == power_wind_data.POWER_DAILY_ENDPOINT
assert params == {
    "parameters": "WS10M,WD10M", "community": "AG", "longitude": "-79.982600", "latitude": "40.644550",
    "start": "19950101", "end": "20241231", "format": "CSV",
}, params

with mock.patch.object(power_wind_data.requests, "get", _fake_get):
    try:
        get_power_wind_for_point(40.6443, -79.9821, range(1994, 2025))
    except PowerIncompleteError as exc:
        assert "1994" in str(exc)
    else:
        raise AssertionError("a year the service did not serve must raise")

for text, cause in (
    (FIXTURE_TEXT.replace("YEAR,DOY,WS10M,WD10M", "YEAR,DOY,WS10M,WDX"), "WD10M"),
    (FIXTURE_TEXT.split("YEAR,DOY")[0], "column line"),
):
    try:
        parse_power_csv(text)
    except PowerIncompleteError as exc:
        assert cause in str(exc), exc
    else:
        raise AssertionError(f"{cause}: must be refused")

try:
    get_power_wind_for_point(40.6443, -79.9821, range(1995, 2025), max_retries=1)
except requests.exceptions.RequestException:
    pass
else:
    raise AssertionError("with no network the fetch must raise")
assert power_wind_data.LAST_FETCH_ATTEMPTS == 2
assert any(host == "power.larc.nasa.gov" for _, host in offline_harness.refused())
print(f"   params {params['parameters']} {params['start']}-{params['end']}; {power_wind_data.LAST_FETCH_ATTEMPTS} attempts published on refusal")

print("\ntest_power_wind_data.py: all sections passed")
print(offline_harness.summary())
