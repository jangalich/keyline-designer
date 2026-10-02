"""
test_nhd_error_body.py

AN ARCGIS ERROR BODY ON HTTP 200 IS A FAILURE, NEVER ZERO FEATURES.

THE DEFECT. hydrology_data._query_layer -- the one loop behind the NHD
flowline, waterbody and point queries, and behind the context map's water
through them -- read a 200 answer with .get("features", []). ArcGIS reports
a failed query (a gateway shedding load, "Error performing query
operation", code 500; a bad parameter; a non-queryable layer) as HTTP 200
with {"error": {...}} and no "features" key, so that read returned [] and
the report printed "no mapped stream" for a parcel that has one, with
nothing retried, nothing degraded and nothing recorded. nhdplus_data, nwi_
data, nfhl_data, structures_data and transmission_lines already checked
for the body; this loop did not.

WHAT THIS FILE HOLDS TO ACCOUNT, offline, at the transport boundary:

    1  An injected error body on every attempt: the loop RETRIES through its
       whole budget (3 attempts, 30/60/90 s timeouts, the pause measured)
       and then RAISES a RequestException -- Layer 1's hard-fail path --
       with ArcGIS's own code and message in it. Never an empty list.
    2  A transient error body -- one bad answer, then a clean one -- recovers
       with the clean answer's features.
    3  A GENUINE EMPTY ANSWER ({"features": []}) is taken on its first
       attempt, not retried, and reads as "none mapped" ([]).
    4  The DEGRADABLE point query: an error body there degrades the points
       to None after its budget, and the flowlines and waterbodies fetched
       beside it are kept.
    5  END TO END through report_data.fetch_report_data(): an error body on
       the context map's water query retries, then DEGRADES context_water
       (source_unavailable, the error text carrying ArcGIS's message) --
       the layer is absent, not present-and-empty; and a genuine empty
       answer leaves it present with no streams and not in `unavailable`.
    6  An exhausted budget of error bodies opens the host's circuit exactly
       as an exhausted budget of timeouts does (host_breaker), so the next
       layer on the host fails instantly instead of burning its own budget.
    7  Every other ArcGIS JSON query loop in the codebase checks the body
       too -- read off the LOADED modules' transports, not the checkout.
"""

import copy
import types
from unittest.mock import patch as mock_patch

import requests

import offline_harness

offline_harness.install()

import context_map_data  # noqa: E402
import daymet_data  # noqa: E402
import fetch_attempts  # noqa: E402
import host_breaker  # noqa: E402
import hydrology_data  # noqa: E402
import nhdplus_data  # noqa: E402
import report_data  # noqa: E402
import run_diagnostics  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402

fetch_attempts.RETRY_PAUSE_SECONDS = 0.01

BOUNDARY = [list(p) for p in REAL_BOUNDARY]

# The body ArcGIS REST returns, on HTTP 200, when the query operation
# fails at the gateway -- the shape seen live on carto.nationalmap.gov's
# layer 0 and the one the NHD probes were watching for.
ERROR_BODY = {"error": {"code": 500, "message": "Error performing query operation", "details": []}}
EMPTY_BODY = {"type": "FeatureCollection", "features": []}


def stream(name, pid):
    return {"type": "Feature",
            "properties": {"permanent_identifier": pid, "gnis_name": name, "fcode": 46006, "reachcode": "05010009000139"},
            "geometry": {"type": "LineString", "coordinates": [[-79.985, 40.644], [-79.982, 40.646]]}}


STREAMS_BODY = {"type": "FeatureCollection", "features": [stream("Montour Run", "123973372"), stream("Montour Run", "123973363")]}


class Response:
    def __init__(self, payload):
        self._payload = copy.deepcopy(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class Transport:
    """hydrology_data's `requests`, replaced: each call is answered by
    `script[layer_id]`, a list of bodies consumed in order (the last one
    repeats), so an error body can be injected on chosen attempts of one
    layer's query while the others answer cleanly."""

    def __init__(self, script):
        self.script = {layer: list(bodies) for layer, bodies in script.items()}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        layer = int(url.rsplit("/", 2)[-2])
        self.calls.append((layer, timeout))
        bodies = self.script[layer]
        body = bodies.pop(0) if len(bodies) > 1 else bodies[0]
        return Response(body)

    def module(self):
        return types.SimpleNamespace(get=self.get, exceptions=requests.exceptions)

    def timeouts(self, layer):
        return [t for l, t in self.calls if l == layer]


FLOW, WB, PT = hydrology_data.FLOWLINE_LAYER, hydrology_data.WATERBODY_LAYER, hydrology_data.POINT_LAYER

print("=" * 72)
print("test_nhd_error_body.py -- an ArcGIS error body on HTTP 200 is a failure, never zero features")
print("=" * 72)

# ======================================================================
# 1. An error body on every attempt: retried through the budget, then raised
# ======================================================================
down = Transport({FLOW: [ERROR_BODY], WB: [EMPTY_BODY], PT: [EMPTY_BODY]})
with mock_patch.object(hydrology_data, "requests", down.module()):
    fetch_attempts.clear()
    try:
        hydrology_data.get_water_features_for_boundary(BOUNDARY)
        raise AssertionError("an error body on every attempt must raise -- it was read as zero features")
    except requests.exceptions.HTTPError as exc:
        message = str(exc)
    attempts = getattr(hydrology_data, run_diagnostics.ATTEMPTS_ATTRIBUTE)
    slept = getattr(hydrology_data, run_diagnostics.RETRY_SLEEP_ATTRIBUTE)
assert isinstance(requests.exceptions.HTTPError(), requests.exceptions.RequestException)
assert down.timeouts(FLOW) == [30, 60, 90], down.calls
assert down.timeouts(WB) == [] and down.timeouts(PT) == [], "the flowline failure stops the pass before the waterbodies"
assert "500" in message and "Error performing query operation" in message and f"/{FLOW}/query" in message, message
assert attempts == 3 and slept > 0, (attempts, slept)
print(f"1. flowlines answered an error body three times: timeouts {down.timeouts(FLOW)} s, {attempts} attempts "
      f"published, {slept:.1f} ms asleep between them, then HTTPError: {message[:80]}...")

# ======================================================================
# 2. A transient error body recovers
# ======================================================================
flaky = Transport({FLOW: [ERROR_BODY, STREAMS_BODY], WB: [EMPTY_BODY], PT: [EMPTY_BODY]})
with mock_patch.object(hydrology_data, "requests", flaky.module()):
    fetch_attempts.clear()
    answer = hydrology_data.get_water_features_for_boundary(BOUNDARY)
    attempts = getattr(hydrology_data, run_diagnostics.ATTEMPTS_ATTRIBUTE)
assert [s["name"] for s in answer["streams"]] == ["Montour Run", "Montour Run"], answer["streams"]
assert [s["permanent_identifier"] for s in answer["streams"]] == ["123973372", "123973363"]
assert answer["water_bodies"] == [] and answer["points"] == []
assert flaky.timeouts(FLOW) == [30, 60] and flaky.timeouts(WB) == [30] and flaky.timeouts(PT) == [30], flaky.calls
assert attempts == 4, attempts
print(f"2. one error body, then a clean answer: both Montour Run reaches came back on the second attempt "
      f"(timeouts {flaky.timeouts(FLOW)} s); 4 attempts over the three queries.")

# ======================================================================
# 3. A genuine empty answer is not retried
# ======================================================================
empty = Transport({FLOW: [EMPTY_BODY], WB: [EMPTY_BODY], PT: [EMPTY_BODY]})
with mock_patch.object(hydrology_data, "requests", empty.module()):
    fetch_attempts.clear()
    answer = hydrology_data.get_water_features_for_boundary(BOUNDARY)
    attempts = getattr(hydrology_data, run_diagnostics.ATTEMPTS_ATTRIBUTE)
assert answer == {"streams": [], "water_bodies": [], "points": []}, answer
assert [t for _, t in empty.calls] == [30, 30, 30], empty.calls
assert attempts == 3, attempts
assert "No mapped streams" in hydrology_data.summarize_water_features(answer)
print("3. a genuine empty answer: one attempt per layer (timeouts 30/30/30 s), [] for each, 'none mapped'.")

# ======================================================================
# 4. The degradable point query
# ======================================================================
points_down = Transport({FLOW: [STREAMS_BODY], WB: [EMPTY_BODY], PT: [ERROR_BODY]})
with mock_patch.object(hydrology_data, "requests", points_down.module()):
    fetch_attempts.clear()
    answer = hydrology_data.get_water_features_for_boundary(BOUNDARY)
    attempts = getattr(hydrology_data, run_diagnostics.ATTEMPTS_ATTRIBUTE)
assert len(answer["streams"]) == 2 and answer["water_bodies"] == []
assert answer["points"] is None, "an error body on the point layer is 'not fetched' (None), never 'nothing mapped' ([])"
assert points_down.timeouts(PT) == [30, 60, 90], points_down.calls
assert attempts == 5, attempts
print("4. the point layer answered error bodies through its budget (30/60/90 s): points None -- springs "
      "'unfetched' -- with the two reaches and the empty waterbodies kept.")

# ======================================================================
# 5. End to end: the context map's water, through fetch_report_data()
# ======================================================================
with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
    DAYMET = daymet_data.parse_daymet_csv(handle.read())


def fetch(transport):
    with mock_patch.object(report_data, "get_daymet_daily_for_point", return_value=DAYMET), \
            mock_patch.object(hydrology_data, "requests", transport.module()):
        return report_data.fetch_report_data(BOUNDARY)


context_down = Transport({FLOW: [STREAMS_BODY], WB: [ERROR_BODY], PT: [EMPTY_BODY]})
degraded = fetch(context_down)
assert degraded.context_water is None, "an error body must not leave the layer present with zero waterbodies"
row = degraded.unavailable["context_water"]
assert row["reason"] == report_data.ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE, row
assert "Error performing query operation" in row["error"] and "500" in row["error"], row
assert context_down.timeouts(WB) == [30, 60, 90] and context_down.timeouts(FLOW) == [30], context_down.calls
assert "nhdplus_hr" in degraded.unavailable, "the refused network degrades the other NHD-host layer as before"

context_empty = Transport({FLOW: [EMPTY_BODY], WB: [EMPTY_BODY], PT: [EMPTY_BODY]})
present = fetch(context_empty)
assert "context_water" not in present.unavailable, present.unavailable.get("context_water")
assert present.context_water == {"streams": [], "water_bodies": [], "points": []}, present.context_water
assert [t for _, t in context_empty.calls] == [30, 30, 30], context_empty.calls
print("5. END TO END: an error body on the context map's waterbody query retried (30/60/90 s) and then "
      "DEGRADED context_water -- absent, source_unavailable, ArcGIS's message in the record -- while a genuine "
      "empty answer left it present with no streams and not unavailable.")

# ======================================================================
# 6. An exhausted budget of error bodies opens the host's circuit
# ======================================================================
host_breaker.set_enabled(True)
host_breaker.reset()
try:
    breaker_down = Transport({FLOW: [ERROR_BODY], WB: [EMPTY_BODY], PT: [EMPTY_BODY]})
    with mock_patch.object(hydrology_data, "requests", breaker_down.module()):
        try:
            hydrology_data.get_water_features_for_boundary(BOUNDARY)
            raise AssertionError("must raise")
        except requests.exceptions.HTTPError:
            pass
        assert len(breaker_down.calls) == 3
        try:
            hydrology_data.get_water_features_for_boundary(BOUNDARY)
            raise AssertionError("the open circuit must refuse the second fetch")
        except host_breaker.HostCircuitOpenError:
            pass
        assert len(breaker_down.calls) == 3, "the refused fetch made no transport call"
    nhdplus_calls = []
    with mock_patch.object(nhdplus_data, "requests", types.SimpleNamespace(
            get=lambda *a, **k: nhdplus_calls.append(1) or Response(ERROR_BODY), exceptions=requests.exceptions)):
        try:
            nhdplus_data.get_flowline_attributes_for_boundary(BOUNDARY)
            raise AssertionError("same host, same circuit")
        except host_breaker.HostCircuitOpenError:
            pass
    assert nhdplus_calls == []
finally:
    host_breaker.set_enabled(False)
print("6. three error bodies exhausted the budget and opened hydro.nationalmap.gov's circuit: the next water "
      "fetch and nhdplus_data were refused locally with zero transport calls.")

# ======================================================================
# 7. Every ArcGIS JSON query loop checks the body -- on the loaded modules
# ======================================================================
import nfhl_data  # noqa: E402
import nwi_data  # noqa: E402
import structures_data  # noqa: E402
import transmission_lines  # noqa: E402
import farm_roads_data  # noqa: E402

checked = {}
for module, call in (
    (hydrology_data, lambda: hydrology_data.get_water_features_for_boundary(BOUNDARY)),
    (nhdplus_data, lambda: nhdplus_data.get_flowline_attributes_for_boundary(BOUNDARY)),
    (nwi_data, lambda: nwi_data.get_wetlands_for_boundary(BOUNDARY)),
    (nfhl_data, lambda: nfhl_data.get_flood_hazard_for_boundary(BOUNDARY)),
    (structures_data, lambda: structures_data.get_structures_for_boundary(BOUNDARY)),
    (transmission_lines, lambda: transmission_lines.get_transmission_lines_near_boundary(BOUNDARY)),
    (farm_roads_data, lambda: farm_roads_data.get_farm_roads_for_boundary(BOUNDARY)),
):
    calls = []
    with mock_patch.object(module, "requests", types.SimpleNamespace(
            get=lambda *a, **k: calls.append(1) or Response(ERROR_BODY), exceptions=requests.exceptions)):
        try:
            call()
            checked[module.__name__] = "returned a value"
        except (requests.exceptions.RequestException, RuntimeError) as exc:
            checked[module.__name__] = f"{type(exc).__name__} after {len(calls)} call(s)"
bad = {name: how for name, how in checked.items() if how == "returned a value"}
assert not bad, f"these loops read an error body as a result: {bad}"
for name, how in checked.items():
    print(f"   {name:22s} {how}")
print("7. every ArcGIS JSON query loop raises on the error body (farm_roads_data raises at once, by its own "
      "design; the rest retry first).")

print(f"\n{offline_harness.summary()}")
print("\ntest_nhd_error_body.py: all sections passed")
