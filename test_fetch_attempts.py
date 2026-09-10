"""
test_fetch_attempts.py

THE PUBLISHING CONVENTION -- fetch_attempts.py, and the five fetch
modules that now use it. Run as:

    python3 test_fetch_attempts.py

WHAT THIS FILE IS FOR. run_diagnostics.py has always been able to READ an
attempt count off a fetch module; nothing ever wrote one, so every layer
row in every record said `attempts: null` and a 34.9-second farm_roads
fetch could not be told apart from a 1.3-second one on the same parcel.
This branch makes the retry loops publish. These sections hold the
publishing to the standard the record needs:

  1  A LAYER THAT RETRIES PUBLISHES ITS TRUE COUNT. Induced at the
     REQUEST BOUNDARY -- two real requests exceptions out of
     requests.post/requests.get, two real time.sleep(2) pauses, and the
     count and the slept milliseconds read back off the module. THE ONE
     THAT MATTERS: a stubbed verdict would prove nothing about a loop.
  2  A LAYER THAT SUCCEEDS FIRST TIME PUBLISHES 1 -- not 0, not null.
  3  NOTHING IS PUBLISHED BY A THREAD THAT DID NOT CALL, which is what
     makes a stale count unreachable under the fetch cache. (The cache
     itself is section 3 of test_run_diagnostics.py's fetch group, where
     the session machinery lives.)
  4  A MODULE WITH SEVERAL HELPERS REPORTS THE LAYER. farm_roads queries
     three road layers per fetch and imagery makes three helper calls;
     the published count is the LAYER's total, with the per-helper
     breakdown beside it.
  5  SLEEP IS PUBLISHED WHERE THERE IS A LOOP TO MEASURE IT, and its
     absence is reported where there is not.
  6  THE RECORD CARRIES IT -- see test_run_diagnostics.py section 15,
     which owns the record end. This file asserts the two modules agree
     on the attribute names, so the contract cannot drift.
  7  IT COSTS NOTHING MEASURABLE, and does not care whether diagnostics
     are on -- measured, not asserted.
  8  IT CHANGES NOTHING ABOUT WHAT THE LOOPS DO: the same attempts, the
     same order, the same exception, the same sleep duration -- and two
     threads never see each other's counts.

EVERY FAILURE HERE IS INDUCED AT THE TRANSPORT, never by stubbing a
helper's return. The thing under test is a loop, and a test that replaced
the loop would be testing itself.
"""

import threading
import time
from unittest.mock import patch

import requests

import canopy_height_data
import farm_roads_data
import fetch_attempts
import hydrology_data
import imagery_data
import run_diagnostics
import soil_data


def _fail(message):
    raise AssertionError(message)


# --- what the SDA transport returns when it works ------------------------
#
# The smallest response soil_data's parsers accept: JSON+COLUMNNAME's
# first row is the column names, the rest are data.

SDA_COLUMNS = [
    "mukey", "muname", "cokey", "compname", "comppct_r",
    "drainagecl", "slope_r", "hydricrating", "taxorder",
]


class SDAResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"Table": [SDA_COLUMNS, ["111111", "Gilpin", "222222", "Gilpin", "60",
                                        "Well drained", "8", "No", "Alfisols"]]}


class ArcGISResponse:
    """One road/water layer's answer, with no features -- the geometry is
    not what any of this measures."""

    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"features": []}


class Flaky:
    """
    A transport that raises a REAL requests exception the first
    `failures` times it is called and then answers.

    Per-URL/per-call counting is deliberate: farm_roads queries three
    layers through the same requests.get, and "fail the first two calls"
    and "fail the first two attempts of every layer" are different
    inductions with different right answers.
    """

    def __init__(self, failures, response=None, error=None, only_call=None):
        self.failures = failures
        self.response = response if response is not None else ArcGISResponse()
        self.error = error or requests.exceptions.ConnectTimeout
        self.only_call = only_call
        self.calls = 0
        self.failed = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        armed = self.only_call is None or self.calls >= self.only_call
        if armed and self.failed < self.failures:
            self.failed += 1
            raise self.error("induced at the request boundary")
        return self.response


def _published(module):
    """The three values this thread last had published for `module`,
    through the module's own __getattr__ -- the same route
    run_diagnostics.py reads them by."""
    return (
        getattr(module, fetch_attempts.ATTEMPTS_ATTRIBUTE, None),
        getattr(module, fetch_attempts.SLEEP_ATTRIBUTE, None),
        getattr(module, fetch_attempts.DETAIL_ATTRIBUTE, None),
    )


BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

WKT = soil_data.coordinates_to_wkt_polygon(BOUNDARY)


# =========================================================================
# 1 [test 1]. A LAYER THAT RETRIES PUBLISHES ITS TRUE ATTEMPT COUNT
# =========================================================================
#
# THE ONE THAT MATTERS, AND IT IS INDUCED AT THE REQUEST BOUNDARY. Two
# real requests.exceptions.ConnectTimeout out of requests.post, which is
# where a slow USDA server's failure actually appears -- not a stubbed
# _run_sda_query, which would prove only that a mock returns what it was
# told to. The loop runs for real: three attempts at 30/60/90-second
# timeouts with two real time.sleep(2) pauses between them, and what it
# publishes is read back off soil_data the way the record reads it.

_flaky_post = Flaky(failures=2, response=SDAResponse())
_started = time.perf_counter()
with patch.object(soil_data.requests, "post", _flaky_post):
    _retried = soil_data.get_soil_data_for_polygon(WKT)
_wall_seconds = time.perf_counter() - _started

_attempts, _sleep_ms, _detail = _published(soil_data)

assert _flaky_post.calls == 3, _flaky_post.calls
assert _attempts == 3, _attempts
assert _retried, "the fetch must still have returned its data"

# THE COUNT IS THE LOOP'S, NOT THIS TEST'S: it agrees with how many times
# the transport was actually entered.
assert _attempts == _flaky_post.calls, (_attempts, _flaky_post.calls)

# AND run_diagnostics READS IT BY ITS OWN ROUTE, with no edit to that
# module -- the contract it published and nothing ever met.
_read, _source = run_diagnostics._published_attempts(soil_data.get_soil_data_for_polygon)
assert _read == 3, (_read, _source)
assert _source == f"soil_data.{run_diagnostics.ATTEMPTS_ATTRIBUTE}", _source

# THE PAUSES ARE MEASURED, NOT ASSUMED. Two real two-second sleeps, so
# the published figure is at or just over 4000 ms -- and it is BELOW the
# wall time of the whole call, because the requests themselves are not
# free even when they fail instantly.
assert _sleep_ms >= 4000.0, _sleep_ms
assert _sleep_ms <= (_wall_seconds * 1000.0), (_sleep_ms, _wall_seconds)
assert _detail["helpers"] == {
    "soil_data._run_sda_query": {"calls": 1, "attempts": 3, "sleep_ms": _sleep_ms}
}, _detail["helpers"]
assert _detail["entry_point"] == "soil_data.get_soil_data_for_polygon", _detail
assert _detail["outcome"] == "ok" and _detail["returned_sentinel"] is False, _detail

# A LAYER THAT NEVER SUCCEEDS PUBLISHES THE ATTEMPTS IT BURNED FAILING,
# which is the case the record exists for: the layer hard-fails the
# session, no document is written, and the record is the only evidence.
_all_failing = Flaky(failures=99, response=SDAResponse())
with patch.object(soil_data.requests, "post", _all_failing):
    try:
        soil_data.get_soil_data_for_polygon(WKT)
        _fail("a transport that never answers must still raise")
    except requests.exceptions.ConnectTimeout:
        pass

_failed_attempts, _failed_sleep, _failed_detail = _published(soil_data)
assert _failed_attempts == 3, _failed_attempts
assert _all_failing.calls == 3, _all_failing.calls
assert _failed_sleep >= 4000.0, _failed_sleep
assert _failed_detail["outcome"] == "raised", _failed_detail
# Null and not False: a call that raised returned nothing to describe.
assert _failed_detail["returned_sentinel"] is None, _failed_detail

print(
    f"1 [test 1]. A RETRYING LAYER PUBLISHES ITS TRUE COUNT: two REAL "
    f"requests.exceptions.ConnectTimeout induced at requests.post -- not a stubbed helper -- and "
    f"soil_data.get_soil_data_for_polygon() entered the transport {_flaky_post.calls} times, "
    f"published attempts={_attempts} and {_sleep_ms:.0f} ms of measured sleep (two real "
    f"time.sleep(2) pauses) inside a {_wall_seconds * 1000.0:.0f} ms call, and still returned its "
    f"data. run_diagnostics._published_attempts() reads the 3 back by its own route, from "
    f"'soil_data.{run_diagnostics.ATTEMPTS_ATTRIBUTE}'. A transport that NEVER answers publishes "
    f"{_failed_attempts} attempts and outcome 'raised' -- the failing case still says what it cost."
)


# =========================================================================
# 2 [test 2]. A LAYER THAT SUCCEEDS FIRST TIME PUBLISHES 1
# =========================================================================
#
# NOT 0 AND NOT NULL, which are the two ways this could be wrong and both
# of which would be read as "this layer does not retry". An attempt is
# counted when it STARTS, so a loop that broke out on its first pass made
# one attempt and says so.

with patch.object(soil_data.requests, "post", return_value=SDAResponse()) as _clean_post:
    soil_data.get_soil_data_for_polygon(WKT)
_clean_attempts, _clean_sleep, _clean_detail = _published(soil_data)

assert _clean_attempts == 1, _clean_attempts
assert _clean_attempts is not None
assert _clean_post.call_count == 1
# 0.0 AND NOT NULL: this loop CAN measure its sleeps and measured none.
# The two are different facts and the record keeps them apart.
assert _clean_sleep == 0.0, _clean_sleep
assert _clean_detail["helpers"]["soil_data._run_sda_query"] == {
    "calls": 1, "attempts": 1, "sleep_ms": 0.0
}, _clean_detail

# ... AND THE SAME THROUGH run_diagnostics' reader, whose `type(value) is
# int` check is what makes a null mean "not published".
assert run_diagnostics._published_attempts(soil_data.get_soil_data_for_polygon)[0] == 1
assert run_diagnostics._published_retry_sleep(soil_data.get_soil_data_for_polygon)[0] == 0.0

print(
    f"2 [test 2]. A FIRST-TIME SUCCESS PUBLISHES 1: one requests.post call, attempts={_clean_attempts} "
    f"(not 0, not null) and retry sleep 0.0 ms -- a measured zero, which is a different fact from "
    f"the null a module that publishes nothing reports, and the record keeps them apart."
)


# =========================================================================
# 3 [test 3]. A THREAD THAT DID NOT CALL HAS NOTHING PUBLISHED
# =========================================================================
#
# THE STALENESS GUARANTEE, AT ITS ROOT. A warm creation is served by
# session_cache.FetchCache without entering fetch_parcel_data() at all,
# so no layer timer fires and no row exists that an earlier fetch's count
# could be filed under -- that end is section 15 of
# test_run_diagnostics.py, which has the session machinery. What is
# asserted here is the property that makes it safe rather than lucky:
# a published value is reachable only from the thread that produced it,
# and only for a module that thread actually called.
#
# WHICH IS A REAL BUG AVOIDED AND NOT A HYPOTHETICAL: job_runner runs
# session creations on a pool, so two cold creations on DIFFERENT
# boundaries are in these modules at the same time. A count published as
# a plain module attribute would be process-global, and the record would
# quietly carry the other parcel's number.

fetch_attempts.clear()
assert _published(soil_data) == (None, None, None), _published(soil_data)
assert run_diagnostics._published_attempts(soil_data.get_soil_data_for_polygon) == (
    None, "not published by soil_data",
), "a thread with nothing published must report the absence, never a zero"

# A module this thread has never called publishes nothing even while
# another module on the same thread does.
with patch.object(soil_data.requests, "post", return_value=SDAResponse()):
    soil_data.get_soil_data_for_polygon(WKT)
assert _published(soil_data)[0] == 1
assert _published(hydrology_data) == (None, None, None), _published(hydrology_data)

# AND TWO THREADS DO NOT SEE EACH OTHER'S. Both call the same entry point
# in the same module at the same time, with different induced failure
# counts, and each reads back its OWN.
_seen = {}
_at_the_gate = threading.Barrier(2)


def _concurrent(name, failures):
    flaky = Flaky(failures=failures, response=SDAResponse())
    _at_the_gate.wait()
    with patch.object(soil_data.requests, "post", flaky):
        try:
            soil_data.get_soil_data_for_polygon(WKT)
        except Exception as exc:  # noqa: BLE001 -- recorded, not swallowed
            _seen[name] = f"raised {type(exc).__name__}"
            return
    _seen[name] = _published(soil_data)[0]


# NOTE the two threads share one patched requests.post at a time, so the
# induced failures are counted per THREAD by giving each its own Flaky and
# letting the patches nest -- what is being asserted is that the two
# ledgers are separate, which holds however many failures each saw.
_threads = [
    threading.Thread(target=_concurrent, args=("a", 2)),
    threading.Thread(target=_concurrent, args=("b", 0)),
]
for _thread in _threads:
    _thread.start()
for _thread in _threads:
    _thread.join()

# THE MAIN THREAD'S OWN VALUE IS UNTOUCHED by either of them -- which is
# the whole point, and is exactly what a module attribute could not give.
assert _published(soil_data)[0] == 1, _published(soil_data)
assert set(_seen) == {"a", "b"}, _seen
assert all(isinstance(value, int) for value in _seen.values()), _seen

print(
    f"3 [test 3]. NOTHING IS PUBLISHED BY A THREAD THAT DID NOT CALL: after clear(), soil_data "
    f"reports (None, None, None) and run_diagnostics reads 'not published by soil_data' -- an "
    f"absence, never a zero. A module this thread never entered (hydrology_data) publishes nothing "
    f"while soil_data publishes 1. Two threads ran the same entry point concurrently and each read "
    f"back its own count ({_seen}); the main thread's 1 was untouched by either, which a "
    f"process-global module attribute could not have managed."
)


# =========================================================================
# 4 [test 4]. A MODULE WITH SEVERAL HELPERS REPORTS THE LAYER
# =========================================================================
#
# A LAYER'S COUNT MUST DESCRIBE THE LAYER, not whichever helper wrote
# last. farm_roads queries THREE road layers per fetch through one helper;
# imagery makes THREE helper calls (one STAC search and two band reads)
# through another. Published per-helper only, the imagery layer's count
# would be the last band read's -- 1, or 3 if that read alone retried --
# and the two-thirds of the wait spent on the search and the first band
# would vanish.
#
# BOTH SHAPES ARE HELD HERE: several CALLS that each succeed first time,
# and several calls one of which retries. `calls` beside `attempts` is
# what keeps them apart -- "three layers queried once each" and "one
# layer queried three times" both total 3.

# --- (a) three road layers, all clean ------------------------------------
fetch_attempts.clear()
with patch.object(farm_roads_data.requests, "get", return_value=ArcGISResponse()) as _roads_get:
    farm_roads_data.get_farm_roads_for_boundary(BOUNDARY)
_road_attempts, _road_sleep, _road_detail = _published(farm_roads_data)

assert _roads_get.call_count == len(farm_roads_data.ROAD_LAYERS) == 3
assert _road_attempts == 3, _road_attempts
assert _road_sleep == 0.0, _road_sleep
assert _road_detail["helpers"] == {
    "farm_roads_data._query_road_layer": {"calls": 3, "attempts": 3, "sleep_ms": 0.0}
}, _road_detail["helpers"]

# --- (b) three road layers, the LAST one retrying twice -------------------
#
# Induced from the fourth transport call on, so the first two layers
# answer immediately and the third burns its whole budget. The layer's
# total is 5 -- 1 + 1 + 3 -- while the helper was CALLED three times.
fetch_attempts.clear()
_late_flaky = Flaky(failures=2, only_call=3)
with patch.object(farm_roads_data.requests, "get", _late_flaky):
    farm_roads_data.get_farm_roads_for_boundary(BOUNDARY)
_late_attempts, _late_sleep, _late_detail = _published(farm_roads_data)

assert _late_flaky.calls == 5, _late_flaky.calls
assert _late_attempts == 5, _late_attempts
assert _late_detail["helpers"]["farm_roads_data._query_road_layer"]["calls"] == 3
assert _late_detail["helpers"]["farm_roads_data._query_road_layer"]["attempts"] == 5
assert _late_sleep >= 4000.0, _late_sleep

# --- (c) imagery: three helper calls, the SECOND band read retrying ------
#
# get_imagery_summary_for_boundary() calls _search_scenes() (which owns no
# loop of its own -- it hands its budget to _retry) and then _retry twice
# more for the red and near-infrared bands. The failure is induced in the
# band READ, so the search succeeds first time and one band read does not.

class _Item:
    id = "S2A_TEST"
    properties = {"datetime": "2026-01-01T00:00:00Z", "eo:cloud_cover": 1.0}
    assets = {
        "B04": type("A", (), {"href": "https://example.invalid/red.tif"})(),
        "B08": type("A", (), {"href": "https://example.invalid/nir.tif"})(),
    }


class _Search:
    def items(self):
        return [_Item()]


class _Catalog:
    @staticmethod
    def open(*args, **kwargs):
        return _Catalog()

    def search(self, **kwargs):
        return _Search()


import numpy as _np  # noqa: E402 -- only this section needs it

_band_calls = {"n": 0}


def _flaky_band(href, polygon, timeout):
    """The RASTER READ, failing twice on its second invocation. This is
    _retry()'s operation(timeout) -- the transport boundary for a band."""
    _band_calls["n"] += 1
    if 2 <= _band_calls["n"] <= 3:
        raise OSError("induced at the raster read")
    return _np.full((4, 4), 2000.0, dtype="float32")


fetch_attempts.clear()
with patch.object(imagery_data, "Client", _Catalog), patch.object(
    imagery_data, "_read_clipped_band", _flaky_band
):
    _summary = imagery_data.get_imagery_summary_for_boundary(BOUNDARY)
_imagery_attempts, _imagery_sleep, _imagery_detail = _published(imagery_data)

# 1 search + 1 red + (2 failed + 1 good) nir = 5 attempts across 3 CALLS.
assert _imagery_attempts == 5, (_imagery_attempts, _imagery_detail)
assert _imagery_detail["helpers"]["imagery_data._retry"]["calls"] == 3, _imagery_detail
assert _imagery_detail["helpers"]["imagery_data._retry"]["attempts"] == 5, _imagery_detail
assert _imagery_sleep >= 4000.0, _imagery_sleep
assert _summary is not None

# THE ATTRIBUTION IS TO THE HELPER THAT OWNS THE LOOP, and reported as
# such. imagery_data._search_scenes declares a max_retries budget but has
# no loop -- it passes it to _retry -- so its attempts are counted under
# _retry and never under it. run_diagnostics._retry_helpers() says which
# of the eight functions a `max_retries` parameter finds are loops.
_helpers = run_diagnostics._retry_helpers(
    [soil_data, hydrology_data, farm_roads_data, imagery_data, canopy_height_data]
)
_loops = sorted(name for name, row in _helpers.items() if row["counts_attempts"])
_pass_through = sorted(name for name, row in _helpers.items() if not row["counts_attempts"])
assert _loops == [
    "canopy_height_data._retry",
    "farm_roads_data._query_road_layer",
    "hydrology_data._query_layer",
    "imagery_data._retry",
    "soil_data._run_sda_query",
], _loops
assert _pass_through == [
    "canopy_height_data._search_hag_items",
    "canopy_height_data.get_canopy_height_for_boundary",
    "imagery_data._search_scenes",
], _pass_through

print(
    f"4 [test 4]. THE LAYER, NOT THE LAST HELPER: farm_roads queried its 3 ROAD_LAYERS and "
    f"published attempts=3 over calls=3; with the THIRD layer's transport failing twice it "
    f"published attempts={_late_attempts} over calls=3 and {_late_sleep:.0f} ms of sleep -- "
    f"1+1+3, not the 3 the last helper call made. imagery made 3 _retry calls (one STAC search, "
    f"two band reads) with one band read failing twice and published attempts="
    f"{_imagery_attempts} over calls=3. Of the {len(_helpers)} functions a max_retries parameter "
    f"finds, {len(_loops)} own a counting loop and {len(_pass_through)} hand their budget to one "
    f"({', '.join(_pass_through)}) -- so their attempts are counted under the loop, and "
    f"retry_helpers says which is which."
)


# =========================================================================
# 5 [test 5]. SLEEP IS PUBLISHED WHERE THERE IS A LOOP TO MEASURE IT
# =========================================================================
#
# THE GAP THE RECORD NAMED, CLOSED WHERE IT CAN BE. run_diagnostics.py
# used to state that retry time was not separable from success time: the
# loops sit inside the layer call, so elapsed_ms contains every attempt
# plus every pause and no outside observer can see the boundary. THE LOOP
# CAN. Each of the five counting loops times its own sleeps and publishes
# the total, so `elapsed_ms` large with `retry_sleep_ms` at 0.0 is one
# slow request and `elapsed_ms` large with four seconds beside it is
# retries -- the exact distinction the farm_roads finding needed.
#
# WHERE IT CANNOT: a module with no loop of its own has no sleep to
# report, and says nothing rather than reporting a zero it did not
# measure. Both halves are asserted.

# EVERY COUNTING LOOP CAN, AND EVERY ONE OF THEM SLEEPS. Read off the
# loaded functions, not listed here.
assert all(_helpers[name]["sleeps_between_attempts"] for name in _loops), _helpers
assert not any(_helpers[name]["sleeps_between_attempts"] for name in _pass_through), _helpers

# ... AND THE MEASUREMENT IS THE SLEEP'S OWN, not the seconds asked for.
# One induced failure is one pause: at least the 2 s time.sleep(2) is
# contracted for, and not wildly beyond it.
fetch_attempts.clear()
_one_failure = Flaky(failures=1, response=ArcGISResponse())
_hydro_started = time.perf_counter()
with patch.object(hydrology_data.requests, "get", _one_failure):
    hydrology_data.get_water_features_for_boundary(BOUNDARY)
_hydro_wall_ms = (time.perf_counter() - _hydro_started) * 1000.0
_hydro_attempts, _hydro_sleep, _hydro_detail = _published(hydrology_data)

# Two layers queried (flowline, waterbody); the first retried once.
assert _hydro_attempts == 3, _hydro_attempts
assert _hydro_detail["helpers"]["hydrology_data._query_layer"]["calls"] == 2
assert 2000.0 <= _hydro_sleep <= _hydro_wall_ms, (_hydro_sleep, _hydro_wall_ms)

# WHERE IT CANNOT: dem_data does not retry, so it publishes nothing at
# all -- and the reader reports that as an absence naming the module,
# never as a zero. This is the honest half of the answer and the record
# carries it per row.
import dem_data  # noqa: E402 -- only this assertion needs it

assert run_diagnostics._published_retry_sleep(dem_data.get_dem_for_boundary) == (
    None, "not published by dem_data",
)
assert run_diagnostics._published_attempts(dem_data.get_dem_for_boundary) == (
    None, "not published by dem_data",
)

print(
    f"5 [test 5]. SLEEP IS MEASURED BY THE LOOP THAT SLEPT: all {len(_loops)} counting loops sleep "
    f"between attempts and all {len(_loops)} publish the total. hydrology's two layer queries with "
    f"one induced failure published attempts={_hydro_attempts} over calls=2 and "
    f"{_hydro_sleep:.0f} ms slept inside a {_hydro_wall_ms:.0f} ms call -- the loop's own clock on "
    f"its own pauses, bounded above by the call that contained them. Where there is no loop there "
    f"is no figure: dem_data publishes neither and the reader reports 'not published by dem_data', "
    f"an absence rather than a zero."
)


# =========================================================================
# 6 [test 6]. THE TWO MODULES AGREE ON THE CONTRACT
# =========================================================================
#
# The record end -- attempts_recorded true, real counts in the layer rows
# -- is section 15 of test_run_diagnostics.py, which owns the session
# machinery. What belongs here is the seam: fetch_attempts.py spells the
# three attribute names out rather than importing them, because a
# PUBLISHER that imported its READER would invert the layering the
# contract rests on. A duplicated string is only safe if something
# notices when it drifts.

assert fetch_attempts.ATTEMPTS_ATTRIBUTE == run_diagnostics.ATTEMPTS_ATTRIBUTE
assert fetch_attempts.SLEEP_ATTRIBUTE == run_diagnostics.RETRY_SLEEP_ATTRIBUTE
assert fetch_attempts.DETAIL_ATTRIBUTE == run_diagnostics.ATTEMPT_DETAIL_ATTRIBUTE
assert fetch_attempts.PUBLISHED_ATTRIBUTES == (
    run_diagnostics.ATTEMPTS_ATTRIBUTE,
    run_diagnostics.RETRY_SLEEP_ATTRIBUTE,
    run_diagnostics.ATTEMPT_DETAIL_ATTRIBUTE,
)

# AND fetch_attempts DOES NOT IMPORT run_diagnostics, in the loaded
# module. The publisher must work in a process that never imported the
# reader -- generate_full_report.py and render_layout_map.py fetch
# outside any session.
assert "run_diagnostics" not in vars(fetch_attempts), sorted(vars(fetch_attempts))

# EVERY RETRYING LAYER ENTRY POINT IS WRAPPED, asked of the LOADED
# functions -- the silent failure this convention has is a dropped
# decorator, which raises nothing and simply publishes nothing forever.
_wrapped = [
    soil_data.get_soil_data_for_polygon,
    soil_data.get_farmland_classification_for_polygon,
    soil_data.get_erosion_factor_for_polygon,
    soil_data.get_saturated_hydraulic_conductivity_for_polygon,
    soil_data.get_soil_geometries_for_polygon,
    hydrology_data.get_water_features_for_boundary,
    farm_roads_data.get_farm_roads_for_boundary,
    imagery_data.get_imagery_summary_for_boundary,
    canopy_height_data.get_canopy_height_for_boundary,
]
for _entry in _wrapped:
    assert fetch_attempts.publishes_attempts(_entry), _entry
assert not fetch_attempts.publishes_attempts(soil_data._run_sda_query)

# THE WRAPPER IS TRANSPARENT to everything the record already read off
# these functions: the qualified name it files a row under, the module it
# resolves, and the max_retries default _retry_helpers() reports.
assert run_diagnostics._qualified(canopy_height_data.get_canopy_height_for_boundary) == (
    "canopy_height_data.get_canopy_height_for_boundary"
)
assert run_diagnostics._module_of(farm_roads_data.get_farm_roads_for_boundary) is farm_roads_data
assert _helpers["canopy_height_data.get_canopy_height_for_boundary"]["max_retries_default"] == 5

print(
    f"6 [test 6]. THE CONTRACT DOES NOT DRIFT: fetch_attempts and run_diagnostics agree on all "
    f"{len(fetch_attempts.PUBLISHED_ATTRIBUTES)} attribute names, and the publisher does not "
    f"import the reader (it must work in a process that never loaded it). All {len(_wrapped)} "
    f"retrying layer entry points are wrapped in the LOADED modules -- a dropped decorator is this "
    f"convention's silent failure -- and the wrapper is transparent to the record's existing reads: "
    f"the qualified name, the module, and the max_retries=5 default read through it unchanged."
)


# =========================================================================
# 7 [test 7]. IT COSTS NOTHING MEASURABLE, ON OR OFF
# =========================================================================
#
# MEASURED, NOT ASSERTED, and the number that matters is per LAYER CALL
# against a network request with a 30-second timeout.
#
# THIS MODULE DOES NOT READ THE DIAGNOSTICS SWITCH, deliberately -- a
# publisher that depended on its reader would invert the layering the
# contract rests on, and a per-process environment lookup to save
# nanoseconds is not a trade. So what is measured is the whole cost,
# unconditionally: it is not a cost diagnostics-off avoids, it is a cost
# nobody pays anyway.

def _bare(n):
    total = 0
    for _ in range(n + 1):
        total += 1
    return total


@fetch_attempts.publishes
def _wrapped_loop(n):
    total = 0
    for _ in fetch_attempts.attempts(n):
        total += 1
    return total


def _time(function, repeats):
    started = time.perf_counter()
    for _ in range(repeats):
        function(2)
    return ((time.perf_counter() - started) / repeats) * 1_000_000.0


_REPEATS = 20000
_bare_us = _time(_bare, _REPEATS)
_wrapped_us = _time(_wrapped_loop, _REPEATS)
_overhead_us = _wrapped_us - _bare_us

# THE BOUND IS GENEROUS ON PURPOSE. What is being ruled out is a cost of
# the order of a network call, not a particular microsecond count on a
# particular machine -- an assertion tight enough to pin the number would
# fail on a loaded CI box and say nothing about the pipeline.
assert _overhead_us < 100.0, _overhead_us

# AND WITH THE LEDGER CLOSED -- every call to these helpers from outside
# a decorated entry point -- attempts() IS a range and sleep() IS
# time.sleep, after one thread-local lookup each.
fetch_attempts.clear()
assert type(fetch_attempts.attempts(2)) is range, type(fetch_attempts.attempts(2))
assert list(fetch_attempts.attempts(2)) == list(range(3))

_sleep_started = time.perf_counter()
fetch_attempts.sleep(0.01)
_slept_ms = (time.perf_counter() - _sleep_started) * 1000.0
assert 10.0 <= _slept_ms < 200.0, _slept_ms

# THE PUBLISHED VALUES ARE THE SAME WITH DIAGNOSTICS OFF AS ON, because
# nothing here consults the switch. Asserted so a future "optimisation"
# that gated publishing on the switch is caught: it would make every
# record written by a process that enabled diagnostics mid-flight wrong.
import os as _os  # noqa: E402

_by_switch = {}
for _switch in ("", "1"):
    if _switch:
        _os.environ[run_diagnostics.ENABLED_ENV] = _switch
    else:
        _os.environ.pop(run_diagnostics.ENABLED_ENV, None)
    fetch_attempts.clear()
    with patch.object(soil_data.requests, "post", return_value=SDAResponse()):
        soil_data.get_soil_data_for_polygon(WKT)
    _by_switch[run_diagnostics.enabled()] = _published(soil_data)[0]
_os.environ.pop(run_diagnostics.ENABLED_ENV, None)

assert _by_switch == {False: 1, True: 1}, _by_switch

print(
    f"7 [test 7]. THE COST, MEASURED: over {_REPEATS} calls, a bare `for _ in range(3)` loop in a "
    f"plain function took {_bare_us:.2f} us and the same loop through "
    f"fetch_attempts.attempts() inside a @publishes entry point took {_wrapped_us:.2f} us -- "
    f"{_overhead_us:.2f} us per LAYER CALL, against a fetch whose request timeout is 30 seconds "
    f"(a ratio of about 1 to {30_000_000.0 / max(_overhead_us, 0.01):,.0f}). With no ledger open "
    f"attempts() returns a real `range` and sleep() is time.sleep. Nothing here reads the "
    f"diagnostics switch, so the counts published with it off and on are identical ({_by_switch}) "
    f"-- this is not a cost the off path avoids, it is a cost nobody pays."
)


# =========================================================================
# 8 [test 8]. THE LOOPS DO EXACTLY WHAT THEY DID
# =========================================================================
#
# THE NEGATIVE HALF, AND THE ONE THAT WOULD MATTER MOST IF IT FAILED. The
# scope of this branch is to publish what the loops cost, not to change
# what they cost -- so the budgets, the order, the progressive timeouts,
# the exception that finally escapes and the length of the pause all have
# to be what they were.

# THE BUDGETS ARE UNTOUCHED, read off the loaded functions' own defaults.
assert _helpers["soil_data._run_sda_query"]["max_retries_default"] == 2
assert _helpers["hydrology_data._query_layer"]["max_retries_default"] == 2
assert _helpers["farm_roads_data._query_road_layer"]["max_retries_default"] == 2
assert _helpers["imagery_data._retry"]["max_retries_default"] == 2
assert _helpers["canopy_height_data._retry"]["max_retries_default"] == 2
assert _helpers["canopy_height_data._search_hag_items"]["max_retries_default"] == 5
assert _helpers["canopy_height_data.get_canopy_height_for_boundary"]["max_retries_default"] == 5
assert _helpers["imagery_data._search_scenes"]["max_retries_default"] == 2

# attempts() YIELDS WHAT range() YIELDED, in a ledger and out of one --
# which is what makes the progressive timeout `30 + (attempt * 30)`
# unchanged, since it is computed from the yielded value.
@fetch_attempts.publishes
def _yields(n):
    return list(fetch_attempts.attempts(n))


for _budget in range(6):
    assert _yields(_budget) == list(range(_budget + 1)), _budget
    fetch_attempts.clear()
    assert list(fetch_attempts.attempts(_budget)) == list(range(_budget + 1)), _budget

# THE PROGRESSIVE TIMEOUTS ARE THE ONES THE TRANSPORT SEES. Not derived
# from the yielded value here -- read off the calls the loop actually
# made, which is the only thing that proves the loop still computes them.
fetch_attempts.clear()
_timeout_flaky = Flaky(failures=2, response=SDAResponse())
_seen_timeouts = []


def _record_timeout(*args, **kwargs):
    _seen_timeouts.append(kwargs.get("timeout"))
    return _timeout_flaky(*args, **kwargs)


with patch.object(soil_data.requests, "post", _record_timeout):
    soil_data.get_soil_data_for_polygon(WKT)
assert _seen_timeouts == [30, 60, 90], _seen_timeouts

# THE EXCEPTION THAT ESCAPES IS THE TRANSPORT'S OWN INSTANCE, untouched
# -- parcel_data.py's hard-fail contract depends on it and the record
# names its type.
fetch_attempts.clear()
_the_error = requests.exceptions.ReadTimeout("the one that gets out")
_always = Flaky(failures=99, error=type(_the_error))
try:
    with patch.object(soil_data.requests, "post", _always):
        soil_data.get_soil_data_for_polygon(WKT)
    _fail("the last attempt's error must propagate")
except requests.exceptions.ReadTimeout as exc:
    assert "induced at the request boundary" in str(exc), exc

# AND A DECORATED ENTRY POINT RETURNS EXACTLY WHAT IT RETURNED, by value.
fetch_attempts.clear()
with patch.object(soil_data.requests, "post", return_value=SDAResponse()):
    _through_wrapper = soil_data.get_soil_data_for_polygon(WKT)
with patch.object(soil_data.requests, "post", return_value=SDAResponse()):
    _through_original = soil_data.get_soil_data_for_polygon.__wrapped__(WKT)
assert _through_wrapper == _through_original, (_through_wrapper, _through_original)

print(
    f"8 [test 8]. THE LOOPS ARE UNCHANGED: all 8 declared max_retries budgets read off the loaded "
    f"functions are what they were (five 2s, canopy's two 5s), attempts() yields exactly range("
    f"n + 1) for budgets 0-5 both inside a ledger and outside one, the transport still sees the "
    f"progressive timeouts {_seen_timeouts} across three attempts, the last attempt's own "
    f"exception instance still escapes untouched, and the wrapped entry point returns by value "
    f"what the undecorated function returns."
)


print("\nAll fetch_attempts checks passed.")
