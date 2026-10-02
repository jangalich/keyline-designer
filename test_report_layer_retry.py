"""
test_report_layer_retry.py

THE VOLATILE REPORT LAYERS RETRY BEFORE THEY DEGRADE, UNDER A BUDGET SET
BY CLASS AND A DEADLINE SET PER LAYER -- and a real "nothing here" is not
retried. Offline, at the transport boundary.

The report-generation audit measured FEMA NFHL at 3.5-14.5 s, the context
map's roads at 4.3-13.2 s and NWI steady at 6.4 s. All three are DEGRADABLE
(report_data.REPORT_FETCH_LAYERS): a failure leaves a visible statement in
the section, never a failed report. The budgets were set on probe data
(report_data.py, "A DEGRADABLE LAYER RETRIES BEFORE IT DEGRADES"): on the
transportation host and NWI no request that failed a 30 s attempt ever
answered at 60 or 90, and the retry after the pause recovered every
failure, so the timeouts no longer escalate and the classes differ:

    fema_nfhl      nfhl_data._get                    3 requests per fetch
                   two attempts of 30 s each, fetch deadline 120 s
    nwi            nwi_data._get                     2-4 requests per fetch
                   two attempts of 30 s each, fetch deadline 120 s
    context_roads  farm_roads_data._query_road_layer 3 requests, AT ONCE
                   ONE attempt of 30 s each, fetch deadline 30 s
    farm_roads     the same helper at Layer 1 (hard-fail)
                   two attempts of 30 s each, fetch deadline 75 s

    RETRY_PAUSE_SECONDS (15 s) between attempts; attempts published
    through fetch_attempts; the deadline shared by every request the
    layer makes (fetch_attempts.deadline), so the layer's worst case is
    its deadline whatever its request count.

A SOURCE THAT ANSWERED IS NOT RETRIED. An empty NWI answer ("no mapped
wetland") is a successful response: one attempt, parsed to an empty
feature list, and never recorded as unavailable.

THE REPLAY. The reference parcel's own captured responses
(water_reference_fixture) are served in the order each fetch asks for
them, by a stub installed as THAT module's `requests` -- so the real
_get() loops run, count and pause -- with failures injected at chosen
calls. Section 7 runs the whole report_data.fetch_report_data() over it.

    1  FEMA: the zone query fails once and recovers; raw answer identical;
       every attempt waited 30 s, none longer.
    2  NWI: the attribute query fails once and recovers on its second and
       last attempt.
    3  NWI: the attribute query fails twice -- the budget -- and the fetch
       raises after exactly two transport calls. Bounded.
    4  NWI: an EMPTY answer is fetched once, not retried, and parses to an
       empty feature list.
    5  context_roads: one road layer fails and is NOT retried (the cosmetic
       class); the other two layers' features come back. The same helper
       at Layer 1 retries that failure once and recovers.
    6  THE DEADLINE: a layer whose deadline has passed makes no further
       request -- NWI's geometry queries are refused after a slow
       attribute query, FetchDeadlineExceeded is a requests Timeout the
       layer's except arm already catches, and the per-attempt timeout
       is clamped to the time remaining.
    7  END TO END through fetch_report_data(): a recovered failure leaves the
       layer present and NOT in `unavailable`; an exhausted budget degrades
       it (source_unavailable); an empty answer is present, not unavailable.
"""

import copy
import threading
import time
import types

import requests

import offline_harness

offline_harness.install()

import context_map_data  # noqa: E402
import daymet_data  # noqa: E402
import farm_roads_data  # noqa: E402
import fetch_attempts  # noqa: E402
import nfhl_data  # noqa: E402
import nwi_data  # noqa: E402
import report_data  # noqa: E402
import run_diagnostics  # noqa: E402
import water_reference_fixture  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402
from unittest.mock import patch as mock_patch  # noqa: E402

# The pause is measured and published by the loop; 10 ms proves that as well
# as fifteen seconds (test_fetch_attempts.py's reason).
fetch_attempts.RETRY_PAUSE_SECONDS = 0.01

RAW = water_reference_fixture.raw_water_layers()
NWI_RAW = RAW["nwi"]
FEMA_RAW = RAW["fema_nfhl"]
BOUNDARY = [list(p) for p in REAL_BOUNDARY]


class Response:
    def __init__(self, payload):
        self._payload = copy.deepcopy(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class Transport:
    """
    One module's `requests`, replaced: `get` answers from `route(url,
    params)` and raises ConnectionError on the calls numbered in `fail_on`
    (1-based, counting every transport call this stub sees). `exceptions`
    is the real requests.exceptions, so the module's own except clauses
    catch exactly what they catch in production.
    """

    def __init__(self, route, fail_on=(), fail_url=None, fail_url_times=1, delay=None):
        self.route = route
        self.fail_on = set(fail_on)
        # fail_url: fail requests to this URL, the first `fail_url_times`
        # of them -- for the road queries, which run on three threads at
        # once, so a call NUMBER would land on any layer.
        self.fail_url = fail_url
        self.fail_url_times = fail_url_times
        self.url_failures = 0
        # delay: (url -> seconds) the transport sleeps before answering,
        # to spend a layer's deadline.
        self.delay = delay or {}
        self.calls = []
        self._lock = threading.Lock()

    def get(self, url, params=None, timeout=None):
        with self._lock:
            self.calls.append((url, dict(params or {}), timeout))
            n = len(self.calls)
            if n in self.fail_on:
                raise requests.exceptions.ConnectionError(f"induced at transport call {n}")
            if url == self.fail_url and self.url_failures < self.fail_url_times:
                self.url_failures += 1
                raise requests.exceptions.ConnectionError(f"induced for {url}")
        if url in self.delay:
            time.sleep(self.delay[url])
        return Response(self.route(url, params or {}))

    def module(self):
        return types.SimpleNamespace(get=self.get, exceptions=requests.exceptions)


def fema_route(url, params):
    for layer, key in ((nfhl_data.LAYER_AVAILABILITY, "availability"), (nfhl_data.LAYER_FLOOD_ZONES, "zones"),
                       (nfhl_data.LAYER_FIRM_PANELS, "panels")):
        if url == f"{nfhl_data.NFHL_BASE}/{layer}/query":
            return FEMA_RAW[key]
    raise AssertionError(f"unexpected FEMA request {url}")


def nwi_route(raw):
    def route(url, params):
        if url == nwi_data.NWI_DATA_SOURCE_QUERY:
            return raw["project"]
        assert url == nwi_data.NWI_WETLANDS_QUERY, url
        if "objectIds" not in params:
            return raw["attributes"]
        return raw["fine"] if params["maxAllowableOffset"] == nwi_data.NWI_FINE_OFFSET_METERS else raw["coarse"]
    return route


def road_feature(layer_id):
    return {"type": "Feature", "properties": {"FULL_STREET_NAME": f"Layer {layer_id} Rd"},
            "geometry": {"type": "LineString", "coordinates": [[-79.99, 40.64 + layer_id * 1e-4],
                                                               [-79.98, 40.64 + layer_id * 1e-4]]}}


def roads_route(url, params):
    layer_id = int(url.rsplit("/", 2)[-2])
    return {"type": "FeatureCollection", "features": [road_feature(layer_id)]}


def published_attempts(module):
    return getattr(module, run_diagnostics.ATTEMPTS_ATTRIBUTE)


def timeouts(transport):
    return [call[2] for call in transport.calls]


print("=" * 72)
print("test_report_layer_retry.py -- the volatile report layers retry, and an empty answer does not")
print("=" * 72)

# ======================================================================
# 1. FEMA: the zone query fails once, and recovers
# ======================================================================
fema = Transport(fema_route, fail_on={2})
with mock_patch.object(nfhl_data, "requests", fema.module()):
    fetch_attempts.clear()
    fema_answer = nfhl_data.get_flood_hazard_for_boundary(BOUNDARY)
    fema_attempts = published_attempts(nfhl_data)
assert [fema_answer[k] for k in ("availability", "zones", "panels")] == \
    [FEMA_RAW[k] for k in ("availability", "zones", "panels")], "the recovered answer is the service's own"
assert len(fema.calls) == 4, fema.calls
assert timeouts(fema) == [30, 30, 30, 30], timeouts(fema)
assert fema_attempts == 4, fema_attempts
assert nfhl_data.parse_flood_hazard(fema_answer)["zones"], "and it parses to the parcel's flood zones"
print(f"1. FEMA NFHL: the zone query failed once and recovered on its second attempt (timeouts "
      f"{timeouts(fema)} s -- flat, no escalation); 4 attempts published over 3 requests; the answer is "
      f"the fixture's own.")

# ======================================================================
# 2. NWI: the attribute query fails once, and recovers on its last attempt
# ======================================================================
nwi = Transport(nwi_route(NWI_RAW), fail_on={1})
with mock_patch.object(nwi_data, "requests", nwi.module()):
    fetch_attempts.clear()
    nwi_answer = nwi_data.get_wetlands_for_boundary(BOUNDARY)
    nwi_attempts = published_attempts(nwi_data)
assert {k: nwi_answer[k] for k in ("attributes", "fine", "coarse", "project")} == \
    {k: NWI_RAW[k] for k in ("attributes", "fine", "coarse", "project")}
assert timeouts(nwi) == [30, 30, 30, 30, 30], timeouts(nwi)
assert len(nwi.calls) == 5 and nwi_attempts == 5, (len(nwi.calls), nwi_attempts)
assert nwi_data.parse_wetlands(nwi_answer)["features"], "and it parses to the parcel's wetlands"
print(f"2. NWI: the attribute query failed once and answered on its second and last attempt (timeouts "
      f"{timeouts(nwi)} s); the fine, coarse and project queries then ran once each -- 5 attempts "
      f"over 4 requests.")

# ======================================================================
# 3. NWI: the budget is exhausted -- bounded
# ======================================================================
nwi_down = Transport(nwi_route(NWI_RAW), fail_on={1, 2, 3, 4, 5, 6})
with mock_patch.object(nwi_data, "requests", nwi_down.module()):
    fetch_attempts.clear()
    try:
        nwi_data.get_wetlands_for_boundary(BOUNDARY)
        raise AssertionError("a source that never answers must raise, so the layer degrades")
    except requests.exceptions.ConnectionError as exc:
        assert "transport call 2" in str(exc), exc
assert len(nwi_down.calls) == 2, "two attempts, then stop -- the retry is bounded"
assert timeouts(nwi_down) == [30, 30], timeouts(nwi_down)
print("3. NWI, never answering: exactly 2 attempts (30 s timeouts), then the fetch raises -- "
      "no third attempt, and no request after the failed one.")

# ======================================================================
# 4. NWI: an EMPTY answer is an answer
# ======================================================================
EMPTY_NWI = dict(NWI_RAW, attributes={"features": []}, fine=None, coarse=None)
nwi_empty = Transport(nwi_route(EMPTY_NWI))
with mock_patch.object(nwi_data, "requests", nwi_empty.module()):
    fetch_attempts.clear()
    empty_answer = nwi_data.get_wetlands_for_boundary(BOUNDARY)
    empty_attempts = published_attempts(nwi_data)
assert len(nwi_empty.calls) == 2 and empty_attempts == 2, (nwi_empty.calls, empty_attempts)
assert [call[2] for call in nwi_empty.calls] == [30, 30], "each request answered on its FIRST attempt"
assert empty_answer["fine"] is None and empty_answer["coarse"] is None
assert nwi_data.parse_wetlands(empty_answer)["features"] == [], "parsed: no mapped wetland"
print("4. NWI, answering EMPTY: 2 requests (attributes, mapping project), one attempt each -- no "
      "retry, no geometry query, parsed to an empty feature list: 'no mapped wetland'.")

# ======================================================================
# 5. context_roads: one road layer fails and is NOT retried; Layer 1 retries it
# ======================================================================
FAILING_LAYER = farm_roads_data.ROAD_LAYERS[1]
FAILING_URL = f"{farm_roads_data.TRANSPORTATION_BASE}/{FAILING_LAYER}/query"

roads = Transport(roads_route, fail_url=FAILING_URL)
with mock_patch.object(farm_roads_data, "requests", roads.module()):
    fetch_attempts.clear()
    road_rows = context_map_data.get_context_roads_for_boundary(BOUNDARY)
    road_attempts = published_attempts(farm_roads_data)
assert sorted(r["properties"]["layer"] for r in road_rows) == [i for i in farm_roads_data.ROAD_LAYERS if i != FAILING_LAYER]
assert len(roads.calls) == 3 and road_attempts == 3, (len(roads.calls), road_attempts)
assert all(t <= 30 for t in timeouts(roads)), timeouts(roads)
assert len({url for url, _, _ in roads.calls}) == 3, "each layer asked once, together"

layer1 = Transport(roads_route, fail_url=FAILING_URL)
with mock_patch.object(farm_roads_data, "requests", layer1.module()):
    fetch_attempts.clear()
    layer1_rows = farm_roads_data.get_farm_roads_for_boundary(BOUNDARY)
    layer1_attempts = published_attempts(farm_roads_data)
assert sorted(r["properties"]["layer"] for r in layer1_rows) == list(farm_roads_data.ROAD_LAYERS)
assert len(layer1.calls) == 4 and layer1_attempts == 4, (len(layer1.calls), layer1_attempts)
assert all(t <= 30 for t in timeouts(layer1)), timeouts(layer1)
print(f"5. context_roads: layer {FAILING_LAYER} failed and was not retried (one attempt per query, the "
      f"cosmetic class); the other {len(road_rows)} layers' roads came back, 3 attempts over 3 requests "
      f"issued together. The same helper at Layer 1 retried it once and recovered: all 3 layers, "
      f"4 attempts.")

# ======================================================================
# 6. THE DEADLINE: a layer whose time is spent makes no further request
# ======================================================================
#
# NWI's attribute query is made to take longer than the layer's whole
# deadline; the geometry and project queries that follow are then refused
# before they are issued, as FetchDeadlineExceeded -- a requests Timeout,
# so the report layer's `except RequestException` degrades it exactly as
# it would a service that never answered.
slow = Transport(nwi_route(NWI_RAW), delay={nwi_data.NWI_WETLANDS_QUERY: 0.3})
with mock_patch.object(nwi_data, "requests", slow.module()), \
        mock_patch.object(nwi_data, "NWI_DEADLINE_SECONDS", 0.2):
    fetch_attempts.clear()
    try:
        nwi_data.get_wetlands_for_boundary(BOUNDARY)
        raise AssertionError("the deadline passed during the first request; the fetch must raise")
    except fetch_attempts.FetchDeadlineExceeded as exc:
        assert isinstance(exc, requests.exceptions.Timeout) and isinstance(exc, requests.exceptions.RequestException)
        deadline_message = str(exc)
assert len(slow.calls) == 1, slow.calls
assert timeouts(slow) == [0.2] or 0 < timeouts(slow)[0] <= 0.2, "the attempt's timeout was clamped to the deadline"

# A deadline with time left changes nothing: the whole chain runs.
with mock_patch.object(nwi_data, "requests", Transport(nwi_route(NWI_RAW)).module()):
    fetch_attempts.clear()
    assert nwi_data.get_wetlands_for_boundary(BOUNDARY)["project"] == NWI_RAW["project"]

# The pause will not be slept into a deadline either: a failure with less
# than a pause left raises instead of waiting for a retry that cannot run.
fetch_attempts.RETRY_PAUSE_SECONDS = 0.5
try:
    paused = Transport(nwi_route(NWI_RAW), fail_on={1})
    with mock_patch.object(nwi_data, "requests", paused.module()), \
            mock_patch.object(nwi_data, "NWI_DEADLINE_SECONDS", 0.3):
        fetch_attempts.clear()
        try:
            nwi_data.get_wetlands_for_boundary(BOUNDARY)
            raise AssertionError("no retry fits before the deadline; the fetch must raise")
        except fetch_attempts.FetchDeadlineExceeded:
            pass
    assert len(paused.calls) == 1, paused.calls
finally:
    fetch_attempts.RETRY_PAUSE_SECONDS = 0.01
print(f"6. THE DEADLINE: with 0.2 s for the layer and a 0.3 s attribute query, NWI made 1 request (timeout "
      f"clamped to {timeouts(slow)[0]:.2g} s) and raised FetchDeadlineExceeded -- a requests Timeout: "
      f"'{deadline_message[:60]}...'. A pause that would outlast the deadline is refused the same way.")

# ======================================================================
# 7. END TO END through report_data.fetch_report_data()
# ======================================================================
#
# Daymet, the one REQUIRED layer, answers from its fixture; every other
# source the harness refuses instantly (so it degrades, as it would with the
# network down); NWI and FEMA answer through the replay.
with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
    DAYMET = daymet_data.parse_daymet_csv(handle.read())


def fetch(nwi_transport, fema_transport):
    with mock_patch.object(report_data, "get_daymet_daily_for_point", return_value=DAYMET), \
            mock_patch.object(nwi_data, "requests", nwi_transport.module()), \
            mock_patch.object(nfhl_data, "requests", fema_transport.module()):
        return report_data.fetch_report_data(BOUNDARY)


recovered = fetch(Transport(nwi_route(NWI_RAW), fail_on={1}), Transport(fema_route, fail_on={3}))
assert "nwi" not in recovered.unavailable and "fema_nfhl" not in recovered.unavailable, recovered.unavailable
assert recovered.nwi["features"] and recovered.fema_nfhl["zones"]

exhausted = fetch(Transport(nwi_route(NWI_RAW), fail_on={1, 2}), Transport(fema_route))
assert exhausted.nwi is None and exhausted.unavailable["nwi"]["reason"] == \
    report_data.ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE, exhausted.unavailable.get("nwi")
assert "fema_nfhl" not in exhausted.unavailable

empty = fetch(Transport(nwi_route(EMPTY_NWI)), Transport(fema_route))
assert "nwi" not in empty.unavailable and empty.nwi is not None and empty.nwi["features"] == [], empty.nwi
print("7. END TO END: a failure that recovers leaves NWI and FEMA present and out of `unavailable`; "
      "an exhausted budget degrades NWI (source_unavailable) and nothing else; an empty NWI answer "
      "is present, with no features, and not unavailable.")

print("\nAll report-layer retry checks passed.")
