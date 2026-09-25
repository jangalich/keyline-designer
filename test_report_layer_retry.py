"""
test_report_layer_retry.py

THE VOLATILE REPORT LAYERS RETRY BEFORE THEY DEGRADE, AND A REAL "NOTHING
HERE" IS NOT RETRIED. Offline, at the transport boundary.

The report-generation audit measured FEMA NFHL at 3.5-14.5 s, the context
map's roads at 4.3-13.2 s and NWI steady at 6.4 s. All three are DEGRADABLE
(report_data.REPORT_FETCH_LAYERS): a failure leaves a visible statement in
the section, never a failed report. What this file holds to account is
that a TRANSIENT failure does not reach that statement -- each request
these layers make runs in a bounded, progressive-timeout retry loop, the
convention every network-backed module here uses (fetch_attempts.py):

    fema_nfhl      nfhl_data._get                    3 requests per fetch
    nwi            nwi_data._get                     2-4 requests per fetch
    context_roads  farm_roads_data._query_road_layer 3 requests per fetch

    each request:  max_retries=2 -> at most 3 attempts, timeouts 30/60/90 s,
                   RETRY_PAUSE_SECONDS (2 s) between attempts, the attempts
                   published through fetch_attempts.

THE RETRY IS PER REQUEST, NOT PER LAYER, and deliberately not both: a
layer-level retry wrapped around these loops would multiply the worst case
(three layer attempts of three request attempts of up to 90 s) for no
failure the inner loop does not already cover.

A SOURCE THAT ANSWERED IS NOT RETRIED. An empty NWI answer ("no mapped
wetland") is a successful response: one attempt, parsed to an empty
feature list, and never recorded as unavailable.

THE REPLAY. The reference parcel's own captured responses
(water_reference_fixture) are served in the order each fetch asks for
them, by a stub installed as THAT module's `requests` -- so the real
_get() loops run, count and pause -- with failures injected at chosen
calls. Section 6 runs the whole report_data.fetch_report_data() over it.

    1  FEMA: the zone query fails once and recovers; raw answer identical.
    2  NWI: the attribute query fails twice and recovers on its last attempt.
    3  NWI: the attribute query fails three times -- the budget -- and the
       fetch raises after exactly three transport calls. Bounded.
    4  NWI: an EMPTY answer is fetched once, not retried, and parses to an
       empty feature list.
    5  context_roads: one road layer fails once and recovers; every layer's
       features come back.
    6  END TO END through fetch_report_data(): a recovered failure leaves the
       layer present and NOT in `unavailable`; an exhausted budget degrades
       it (source_unavailable); an empty answer is present, not unavailable.
"""

import copy
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
# as two seconds (test_fetch_attempts.py's reason).
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

    def __init__(self, route, fail_on=()):
        self.route = route
        self.fail_on = set(fail_on)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        if len(self.calls) in self.fail_on:
            raise requests.exceptions.ConnectionError(f"induced at transport call {len(self.calls)}")
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
assert timeouts(fema) == [30, 30, 60, 30], timeouts(fema)
assert fema_attempts == 4, fema_attempts
assert nfhl_data.parse_flood_hazard(fema_answer)["zones"], "and it parses to the parcel's flood zones"
print(f"1. FEMA NFHL: the zone query failed once and recovered on its second attempt (timeouts "
      f"{timeouts(fema)} s); 4 attempts published over 3 requests; the answer is the fixture's own.")

# ======================================================================
# 2. NWI: the attribute query fails twice, and recovers on its last attempt
# ======================================================================
nwi = Transport(nwi_route(NWI_RAW), fail_on={1, 2})
with mock_patch.object(nwi_data, "requests", nwi.module()):
    fetch_attempts.clear()
    nwi_answer = nwi_data.get_wetlands_for_boundary(BOUNDARY)
    nwi_attempts = published_attempts(nwi_data)
assert {k: nwi_answer[k] for k in ("attributes", "fine", "coarse", "project")} == \
    {k: NWI_RAW[k] for k in ("attributes", "fine", "coarse", "project")}
assert timeouts(nwi)[:3] == [30, 60, 90], timeouts(nwi)
assert len(nwi.calls) == 6 and nwi_attempts == 6, (len(nwi.calls), nwi_attempts)
assert nwi_data.parse_wetlands(nwi_answer)["features"], "and it parses to the parcel's wetlands"
print(f"2. NWI: the attribute query failed twice and answered on its third and last attempt (timeouts "
      f"{timeouts(nwi)[:3]} s); the fine, coarse and project queries then ran once each -- 6 attempts "
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
        assert "transport call 3" in str(exc), exc
assert len(nwi_down.calls) == 3, "three attempts, then stop -- the retry is bounded"
assert timeouts(nwi_down) == [30, 60, 90], timeouts(nwi_down)
print("3. NWI, never answering: exactly 3 attempts (30/60/90 s timeouts), then the fetch raises -- "
      "no fourth attempt, and no request after the failed one.")

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
# 5. context_roads: one road layer fails once, and recovers
# ======================================================================
roads = Transport(roads_route, fail_on={2})
with mock_patch.object(farm_roads_data, "requests", roads.module()):
    fetch_attempts.clear()
    road_rows = context_map_data.get_context_roads_for_boundary(BOUNDARY)
    road_attempts = published_attempts(farm_roads_data)
assert len(road_rows) == len(farm_roads_data.ROAD_LAYERS) == 3, road_rows
assert len(roads.calls) == 4 and road_attempts == 4, (len(roads.calls), road_attempts)
assert timeouts(roads) == [30, 30, 60, 30], timeouts(roads)
print(f"5. context_roads: layer {farm_roads_data.ROAD_LAYERS[1]} failed once and recovered; all "
      f"{len(road_rows)} layers' roads came back, 4 attempts over 3 requests.")

# ======================================================================
# 6. END TO END through report_data.fetch_report_data()
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

exhausted = fetch(Transport(nwi_route(NWI_RAW), fail_on={1, 2, 3}), Transport(fema_route))
assert exhausted.nwi is None and exhausted.unavailable["nwi"]["reason"] == \
    report_data.ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE, exhausted.unavailable.get("nwi")
assert "fema_nfhl" not in exhausted.unavailable

empty = fetch(Transport(nwi_route(EMPTY_NWI)), Transport(fema_route))
assert "nwi" not in empty.unavailable and empty.nwi is not None and empty.nwi["features"] == [], empty.nwi
print("6. END TO END: a failure that recovers leaves NWI and FEMA present and out of `unavailable`; "
      "an exhausted budget degrades NWI (source_unavailable) and nothing else; an empty NWI answer "
      "is present, with no features, and not unavailable.")

print("\nAll report-layer retry checks passed.")
