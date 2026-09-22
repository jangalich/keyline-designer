"""
test_report_data.py

Offline checks for report_data.py -- the site data report's own fetch
layer, its REQUIRED/DEGRADABLE table, its failure shape and its cache.
The network is refused by offline_harness; Daymet is mocked on
report_data's OWN namespace (it imports get_daymet_daily_for_point
directly, so patching daymet_data would leave its bound reference alone).

  1. THE TABLE: every layer is named once with a policy the module
     defines, and the fetch function times every one of them (the
     diagnostics self-check's own test, run here against the compiled
     function so it cannot pass on a stale checkout).
  2. HAPPY PATH: a mocked Daymet answer -> ReportData with the centroid
     the fetch was made at, the daily dict, the climate block derived
     once, and nothing unavailable.
  3. A REQUIRED FAILURE RAISES ReportDataIncompleteError carrying the
     (layer, label, reason) triple -- source_unavailable for a
     RequestException, no_data_for_parcel for a DaymetIncompleteError --
     and the real fetch under the offline harness raises the first kind.
  4. THE CACHE: a second call on the same boundary is a hit, not a fetch;
     a failure is never cached; a different boundary is a miss.
  5. THE DEGRADABLE PATH is exercised by flipping the table entry for the
     duration of one call, so the code path exists before a second layer
     needs it: the field is None, `unavailable` names the layer, and no
     climate block is derived from nothing.
"""

from unittest import mock

import offline_harness

offline_harness.install()

import requests

import report_data
import run_diagnostics
import session_cache
from daymet_data import DaymetIncompleteError, parse_daymet_csv
from reference_fixture import REAL_BOUNDARY
from report_data import (
    DEGRADABLE,
    LAYER_CLIMATE,
    REPORT_FETCH_LAYERS,
    REQUIRED,
    ReportData,
    ReportDataIncompleteError,
    boundary_centroid_lat_lon,
    fetch_report_data,
)

with open("daymet_reference_fixture.csv", encoding="utf-8") as _handle:
    FIXTURE_DAILY = parse_daymet_csv(_handle.read())

# ======================================================================
# 1. The table
# ======================================================================
print("1. REPORT_FETCH_LAYERS names each layer once with a known policy, and each is timed")
assert REPORT_FETCH_LAYERS == {"daymet_daily": REQUIRED}
assert set(REPORT_FETCH_LAYERS.values()) <= {REQUIRED, DEGRADABLE}
for layer in REPORT_FETCH_LAYERS:
    assert layer in ReportData.__dataclass_fields__, f"{layer} is not a ReportData field"
sites = run_diagnostics._fetch_hook_sites()
assert sites["report_data.fetch_report_data calls time_layer"] is True, sites
coverage = [k for k in sites if k.startswith("report_data.fetch_report_data times")]
assert coverage == ["report_data.fetch_report_data times 1 of 1 declared report layers"], sites
assert sites[coverage[0]] is True
# Layer 1's own rows are untouched by the addition.
assert sites["parcel_data.fetch_parcel_data calls time_layer"] is True
print(f"   {coverage[0]}")

# ======================================================================
# 2. Happy path
# ======================================================================
print("2. a Daymet answer becomes a ReportData with the climate block derived once")
calls = []


def _fake_daymet(lat, lon, **kwargs):
    calls.append((lat, lon))
    return FIXTURE_DAILY


with mock.patch.object(report_data, "get_daymet_daily_for_point", _fake_daymet):
    data = fetch_report_data(REAL_BOUNDARY)
assert isinstance(data, ReportData)
assert data.boundary == list(REAL_BOUNDARY)
assert data.centroid == boundary_centroid_lat_lon(REAL_BOUNDARY)
assert calls == [data.centroid]
# The fixture was extracted at (40.6443, -79.9821), about 45 m from this
# polygon centroid (40.64455, -79.98260) -- a different hand-picked point,
# not this function's. Both fall inside the same 1 km Daymet cell (roughly
# 0.009 deg of latitude), which is the only agreement the data can carry.
assert abs(data.centroid[0] - FIXTURE_DAILY["latitude"]) < 0.005, data.centroid
assert abs(data.centroid[1] - FIXTURE_DAILY["longitude"]) < 0.005, data.centroid
assert data.daymet_daily is FIXTURE_DAILY
assert data.climate["year_count"] == 30 and data.climate["frost"]["frost_free_days"] > 0
assert data.unavailable == {}
print(f"   centroid {data.centroid[0]:.4f}, {data.centroid[1]:.4f}; zone {data.climate['hardiness']['zone']}")

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
# A ParcelDataIncompleteError-shaped error, deliberately: same three names.
import parcel_data
for name in ("layer", "label", "reason", "REASON_SOURCE_UNAVAILABLE", "REASON_NO_DATA_FOR_PARCEL"):
    assert hasattr(ReportDataIncompleteError, name) or name in ("layer", "label", "reason")
assert ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE == parcel_data.ParcelDataIncompleteError.REASON_SOURCE_UNAVAILABLE
assert ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL == parcel_data.ParcelDataIncompleteError.REASON_NO_DATA_FOR_PARCEL
print("   source_unavailable on a RequestException, no_data_for_parcel on an incomplete answer; reasons match Layer 1's")

# ======================================================================
# 4. The cache
# ======================================================================
print("4. the report fetch cache: hit on the same boundary, nothing cached on failure")
fetches = []


def _counting(boundary):
    fetches.append(list(boundary))
    with mock.patch.object(report_data, "get_daymet_daily_for_point", _fake_daymet):
        return fetch_report_data(boundary)


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
# The module default is built lazily and is a FetchCache over fetch_report_data.
default = report_data.default_report_fetch_cache()
assert isinstance(default, session_cache.FetchCache) and default is report_data.default_report_fetch_cache()
assert default._fetch_function is fetch_report_data
print("   1 fetch for 2 calls on one boundary; a failed fetch leaves the cache empty")

# ======================================================================
# 5. The degradable path
# ======================================================================
print("5. a DEGRADABLE layer failing yields a ReportData that names it under `unavailable`")
with mock.patch.dict(REPORT_FETCH_LAYERS, {"daymet_daily": DEGRADABLE}):
    with mock.patch.object(report_data, "get_daymet_daily_for_point", side_effect=requests.exceptions.ConnectionError("down")):
        degraded = fetch_report_data(REAL_BOUNDARY)
assert degraded.daymet_daily is None and degraded.climate is None
assert degraded.unavailable == {
    "daymet_daily": {"label": LAYER_CLIMATE[1], "reason": ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE, "error": "down"}
}
assert REPORT_FETCH_LAYERS["daymet_daily"] == REQUIRED, "the table is restored after the test"
print("   climate None, unavailable['daymet_daily'] carries label and reason")

print("\ntest_report_data.py: all sections passed")
print(offline_harness.summary())
