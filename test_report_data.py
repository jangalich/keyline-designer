"""
test_report_data.py

Offline checks for report_data.py -- the site data report's own fetch
layer, its REQUIRED/DEGRADABLE table, its failure shape and its cache.
The network is refused by offline_harness; every fetch is mocked on
report_data's OWN namespace (it imports the fetch functions directly, so
patching their modules would leave its bound references alone). The
mocks answer with the reference fixtures: Daymet at the parcel, Daymet
at the five nearest normals stations, Atlas 14 and POWER.

  1. THE TABLE: four layers, each named once with a policy the module
     defines, each a ReportData field, and the fetch function times every
     one of them (the diagnostics self-check's own test, run here against
     the compiled function so it cannot pass on a stale checkout).
  2. HAPPY PATH: every mocked answer -> a ReportData with the centroid the
     fetches were made at, every block derived once, POWER asked for the
     Daymet block's own years, the precipitation factor applied to the
     climate block, severe weather off the bundle, nothing unavailable.
  3. A REQUIRED FAILURE RAISES ReportDataIncompleteError carrying the
     (layer, label, reason) triple -- source_unavailable for a
     RequestException, no_data_for_parcel for an incomplete answer -- for
     Daymet at the parcel and for Daymet at the stations; a station Daymet
     has no data for is dropped, not fatal; the real fetch under the
     offline harness raises the first kind.
  4. A DEGRADABLE FAILURE is recorded, not raised: Atlas 14 down ->
     design_storms None and unavailable['atlas14'] source_unavailable;
     Atlas 14 outside coverage -> no_data_for_parcel with the server's
     message; POWER down -> wind None; the climate block survives both.
  5. THE CACHE: a second call on the same boundary is a hit, not a fetch;
     a failure is never cached; a different boundary is a miss.
"""

import os
from unittest import mock

import offline_harness

offline_harness.install()

import requests

import report_data
import run_diagnostics
import session_cache
from atlas14_data import Atlas14IncompleteError, parse_atlas14_csv
from daymet_data import DaymetIncompleteError, parse_daymet_csv
from power_wind_data import PowerIncompleteError, parse_power_csv
from reference_fixture import REAL_BOUNDARY
from report_data import (
    DEGRADABLE,
    LAYER_ATLAS14,
    LAYER_CLIMATE,
    LAYER_POWER_WIND,
    LAYER_STATIONS,
    REPORT_FETCH_LAYERS,
    REQUIRED,
    ReportData,
    ReportDataIncompleteError,
    boundary_centroid_lat_lon,
    fetch_report_data,
)

with open("daymet_reference_fixture.csv", encoding="utf-8") as _handle:
    FIXTURE_DAILY = parse_daymet_csv(_handle.read())
with open("atlas14_reference_fixture.csv", encoding="utf-8") as _handle:
    FIXTURE_ATLAS14 = parse_atlas14_csv(_handle.read())
with open("power_wind_reference_fixture.csv", encoding="utf-8") as _handle:
    FIXTURE_POWER = parse_power_csv(_handle.read())
STATION_IDS = ["USC00362574", "USC00360022", "USC00363343", "USC00366151", "USC00361139"]
FIXTURE_STATIONS = {}
for _sid in STATION_IDS:
    with open(os.path.join("daymet_station_fixtures", _sid + ".csv"), encoding="utf-8") as _handle:
        FIXTURE_STATIONS[_sid] = parse_daymet_csv(_handle.read(), required_variables=("prcp",))
_STATION_BY_POINT = {
    (round(d["latitude"], 3), round(d["longitude"], 3)): d for d in FIXTURE_STATIONS.values()
}

# ======================================================================
# 1. The table
# ======================================================================
print("1. REPORT_FETCH_LAYERS names each layer once with a known policy, and each is timed")
assert REPORT_FETCH_LAYERS == {
    "daymet_daily": REQUIRED,
    "daymet_at_stations": REQUIRED,
    "atlas14": DEGRADABLE,
    "power_wind": DEGRADABLE,
}, REPORT_FETCH_LAYERS
assert set(REPORT_FETCH_LAYERS.values()) <= {REQUIRED, DEGRADABLE}
for layer in REPORT_FETCH_LAYERS:
    assert layer in ReportData.__dataclass_fields__, f"{layer} is not a ReportData field"
for bundled in ("severe_weather", "precipitation_correction"):
    assert bundled in ReportData.__dataclass_fields__ and bundled not in REPORT_FETCH_LAYERS
sites = run_diagnostics._fetch_hook_sites()
assert sites["report_data.fetch_report_data calls time_layer"] is True, sites
coverage = [k for k in sites if k.startswith("report_data.fetch_report_data times")]
assert coverage == ["report_data.fetch_report_data times 4 of 4 declared report layers"], sites
assert sites[coverage[0]] is True
# Layer 1's own rows are untouched by the addition.
assert sites["parcel_data.fetch_parcel_data calls time_layer"] is True
print(f"   {coverage[0]}")

# ======================================================================
# 2. Happy path
# ======================================================================
print("2. every answer becomes a ReportData with each block derived once")
daymet_calls = []
power_calls = []
atlas_calls = []


def _fake_daymet(lat, lon, years=None, variables=None, **kwargs):
    daymet_calls.append((lat, lon, tuple(years) if years else None, variables))
    if variables == ("prcp",):
        return _STATION_BY_POINT[(round(lat, 3), round(lon, 3))]
    return FIXTURE_DAILY


def _fake_atlas14(lat, lon, **kwargs):
    atlas_calls.append((lat, lon))
    return FIXTURE_ATLAS14


def _fake_power(lat, lon, years, **kwargs):
    power_calls.append((lat, lon, list(years)))
    return FIXTURE_POWER


def _all_mocked():
    return (
        mock.patch.object(report_data, "get_daymet_daily_for_point", _fake_daymet),
        mock.patch.object(report_data, "get_atlas14_for_point", _fake_atlas14),
        mock.patch.object(report_data, "get_power_wind_for_point", _fake_power),
    )


def _fetch_all(boundary=REAL_BOUNDARY):
    a, b, c = _all_mocked()
    with a, b, c:
        return fetch_report_data(boundary)


data = _fetch_all()
assert isinstance(data, ReportData)
assert data.boundary == list(REAL_BOUNDARY)
assert data.centroid == boundary_centroid_lat_lon(REAL_BOUNDARY)
# One Daymet call at the parcel (the module's default window and variables, so neither is passed),
# then one per station with prcp only over 1991-2020.
assert daymet_calls[0] == (data.centroid[0], data.centroid[1], None, None)
assert len(daymet_calls) == 6
assert all(call[2] == tuple(range(1991, 2021)) and call[3] == ("prcp",) for call in daymet_calls[1:])
assert atlas_calls == [data.centroid] and power_calls == [(data.centroid[0], data.centroid[1], list(range(1995, 2025)))]
# The fixture was extracted at (40.6443, -79.9821), about 45 m from this
# polygon centroid (40.64455, -79.98260) -- a different hand-picked point,
# not this function's. Both fall inside the same 1 km Daymet cell.
assert abs(data.centroid[0] - FIXTURE_DAILY["latitude"]) < 0.005, data.centroid
assert abs(data.centroid[1] - FIXTURE_DAILY["longitude"]) < 0.005, data.centroid
assert data.daymet_daily is FIXTURE_DAILY
assert set(data.daymet_at_stations) == set(STATION_IDS)
assert data.precipitation_correction["applied"] is True and abs(data.precipitation_correction["factor"] - 0.96) < 0.002
assert data.climate["prcp_factor"] == data.precipitation_correction["factor"]
assert data.climate["year_count"] == 30 and data.climate["frost"]["frost_free_days"] > 0
assert data.climate["annual"]["pet_mm"] > 0 and data.climate["annual"]["deficit_months"] == [6, 7, 8]
assert data.atlas14 is FIXTURE_ATLAS14 and data.design_storms["rows"][1]["depths"][100] == 4.98
assert data.power_wind is FIXTURE_POWER and data.wind["seasons"]["winter"]["prevailing_sector"] == "SW"
assert data.wind["period"] == {"start": 1995, "end": 2024} == data.climate["period"]
# The polygon centroid, 45 m from the fixture point, takes one more hail and one more wind report into the circle.
assert data.severe_weather["counts"] == {"hail": 615, "wind": 1917, "tornado": 36} and data.severe_weather["radius_miles"] == 25.0
assert data.unavailable == {}
print(f"   centroid {data.centroid[0]:.4f}, {data.centroid[1]:.4f}; factor {data.climate['prcp_factor']:.3f}; "
      f"zone {data.climate['hardiness']['zone']}; wind from {data.wind['seasons']['winter']['prevailing_sector']}")

# ======================================================================
# 3. A required failure raises with the wire triple
# ======================================================================
print("3. a REQUIRED layer failing raises ReportDataIncompleteError with layer/label/reason")
with mock.patch.object(report_data, "get_daymet_daily_for_point", side_effect=requests.exceptions.ConnectionError("down")):
    try:
        fetch_report_data(REAL_BOUNDARY)
    except ReportDataIncompleteError as exc:
        assert (exc.layer, exc.label) == LAYER_CLIMATE
        assert exc.reason == ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE
        assert "daymet_daily" in str(exc)
    else:
        raise AssertionError("a required layer failing must raise")

with mock.patch.object(report_data, "get_daymet_daily_for_point", side_effect=DaymetIncompleteError("short")):
    try:
        fetch_report_data(REAL_BOUNDARY)
    except ReportDataIncompleteError as exc:
        assert exc.reason == ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL
    else:
        raise AssertionError("an incomplete answer must raise")


def _daymet_stations_down(lat, lon, years=None, variables=None, **kwargs):
    if variables == ("prcp",):
        raise requests.exceptions.ConnectionError("down at the station")
    return FIXTURE_DAILY


with mock.patch.object(report_data, "get_daymet_daily_for_point", _daymet_stations_down):
    try:
        fetch_report_data(REAL_BOUNDARY)
    except ReportDataIncompleteError as exc:
        assert (exc.layer, exc.label) == LAYER_STATIONS and "daymet_at_stations" in str(exc)
        assert exc.reason == ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE
    else:
        raise AssertionError("Daymet down at the stations must raise")


def _one_station_uncovered(lat, lon, years=None, variables=None, **kwargs):
    if variables == ("prcp",):
        station = _STATION_BY_POINT[(round(lat, 3), round(lon, 3))]
        if station["latitude"] == FIXTURE_STATIONS["USC00361139"]["latitude"]:
            raise DaymetIncompleteError("no cell for this station")
        return station
    return FIXTURE_DAILY


a, b, c = _all_mocked()
with mock.patch.object(report_data, "get_daymet_daily_for_point", _one_station_uncovered), b, c:
    four = fetch_report_data(REAL_BOUNDARY)
assert set(four.daymet_at_stations) == set(STATION_IDS) - {"USC00361139"}
assert four.precipitation_correction["applied"] is True and four.precipitation_correction["station_count"] == 4

# The REAL fetch, no mock: the harness refuses daymet.ornl.gov and the
# error that reaches a caller is the wire-shaped one, source_unavailable.
try:
    fetch_report_data(REAL_BOUNDARY)
except ReportDataIncompleteError as exc:
    assert exc.reason == ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE
    assert isinstance(exc.__cause__, requests.exceptions.RequestException)
else:
    raise AssertionError("with no network the real fetch must raise")
assert any(host == "daymet.ornl.gov" for _, host in offline_harness.refused())
import parcel_data
assert ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE == parcel_data.ParcelDataIncompleteError.REASON_SOURCE_UNAVAILABLE
assert ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL == parcel_data.ParcelDataIncompleteError.REASON_NO_DATA_FOR_PARCEL
print("   source_unavailable on a RequestException, no_data_for_parcel on an incomplete answer; an uncovered station is dropped")

# ======================================================================
# 4. The degradable path
# ======================================================================
print("4. a DEGRADABLE layer failing is recorded under `unavailable`; the climate block survives")
a, b, c = _all_mocked()
with a, mock.patch.object(report_data, "get_atlas14_for_point", side_effect=requests.exceptions.ConnectionError("down")), c:
    degraded = fetch_report_data(REAL_BOUNDARY)
assert degraded.atlas14 is None and degraded.design_storms is None
assert degraded.unavailable == {
    "atlas14": {"label": LAYER_ATLAS14[1], "reason": ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE, "error": "down"}
}
assert degraded.climate is not None and degraded.wind is not None and degraded.severe_weather is not None

outside = Atlas14IncompleteError("Atlas 14 server: Error 3.0: Selected location is not within a project area")
with a, mock.patch.object(report_data, "get_atlas14_for_point", side_effect=outside), c:
    uncovered = fetch_report_data(REAL_BOUNDARY)
assert uncovered.unavailable["atlas14"]["reason"] == ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL
assert "not within a project area" in uncovered.unavailable["atlas14"]["error"]

with a, b, mock.patch.object(report_data, "get_power_wind_for_point", side_effect=PowerIncompleteError("no 1995")):
    windless = fetch_report_data(REAL_BOUNDARY)
assert windless.power_wind is None and windless.wind is None
assert windless.unavailable["power_wind"] == {
    "label": LAYER_POWER_WIND[1], "reason": ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL, "error": "no 1995"
}
assert windless.design_storms is not None and windless.climate["prcp_factor"] == data.climate["prcp_factor"]
assert REPORT_FETCH_LAYERS["atlas14"] == DEGRADABLE and REPORT_FETCH_LAYERS["daymet_daily"] == REQUIRED
print("   atlas14 down -> source_unavailable; outside coverage -> no_data_for_parcel; POWER short -> wind None")

# ======================================================================
# 5. The cache
# ======================================================================
print("5. the report fetch cache: hit on the same boundary, nothing cached on failure")
fetches = []


def _counting(boundary):
    fetches.append(list(boundary))
    return _fetch_all(boundary)


cache = session_cache.FetchCache(fetch_function=_counting)
first = cache.get_or_fetch(REAL_BOUNDARY)
second = cache.get_or_fetch(REAL_BOUNDARY)
assert first is second and len(fetches) == 1
assert cache.hits == 1 and cache.misses == 1
shifted = [(lon + 0.01, lat) for lon, lat in REAL_BOUNDARY]
third = cache.get_or_fetch(shifted)
assert third is not first and len(fetches) == 2

failing = session_cache.FetchCache(fetch_function=lambda b: fetch_report_data(b))
with mock.patch.object(report_data, "get_daymet_daily_for_point", side_effect=requests.exceptions.ConnectionError("down")):
    try:
        failing.get_or_fetch(REAL_BOUNDARY)
    except ReportDataIncompleteError:
        pass
assert not failing.contains(REAL_BOUNDARY) and len(failing) == 0
default = report_data.default_report_fetch_cache()
assert isinstance(default, session_cache.FetchCache) and default is report_data.default_report_fetch_cache()
assert default._fetch_function is fetch_report_data
print("   1 fetch for 2 calls on one boundary; a failed fetch leaves the cache empty")

print("\ntest_report_data.py: all sections passed")
print(offline_harness.summary())
