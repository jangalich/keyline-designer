"""
test_report_fetch_concurrency.py

THE REPORT-LAYER FETCH RUNS ITS TWENTY LAYERS AT ONCE, and nothing about
what each layer does, counts or reports changed in the process. Offline,
with every layer's entry point replaced by a stub that records when and
on which thread it ran, holds for a fixed time, and answers or raises as
the section asks.

    1  EXACTLY ONCE. Each of the twenty entry points is called once per
       fetch_report_data(); every ReportData field holds its layer's
       answer; nothing is unavailable.
    2  CONCURRENT. With each stub holding HOLD seconds, the stage takes
       a few HOLDs, not twenty -- and the records show the stubs in
       flight together.
    3  PER HOST, NOT GLOBAL. The four soil layers (one host) are never
       more than host_slots' cap in flight at once, and were at the cap;
       host_slots.peak_in_flight() reports it. The timer measures the
       fetch and not the wait for a slot: a soil layer that queued for
       a slot still records one HOLD, not two.
    4  THE ONE DEPENDENCY. POWER starts after Daymet returned, and is
       asked for the years the derived climate used.
    5  A FORCED FAILURE DEGRADES THAT LAYER ALONE: one layer raising
       through its budget leaves exactly one entry in `unavailable`,
       source_unavailable, every other field present, every stub still
       called once. `unavailable` is in REPORT_FETCH_LAYERS' order
       whatever order the layers finished in.
    6  A REQUIRED FAILURE RAISES ReportDataIncompleteError for the
       climate layer; no stub ran more than once.
    7  THE PROGRESS BAR IS TICKED FROM THE WORKERS (the ContextVar
       carried in): every fetch unit completes, a poller sampling the
       snapshot through the run never sees the fraction go backwards,
       and the label names an outstanding fetch while one is held.
    8  THE DIAGNOSTICS PROBE IS CARRIED IN THE SAME WAY: with a probe
       opened on the calling thread, every layer writes its row, with
       the twenty `order`s distinct and the elapsed times the fetch's.
    9  REPORT_LAYER_HOSTS names every declared layer, as a host.
"""

import contextlib
import os
import tempfile
import threading
import time
from unittest import mock

import requests

import offline_harness

offline_harness.install()

import bedrock_geology  # noqa: E402
import census_geography  # noqa: E402
import context_map_data  # noqa: E402
import daymet_data  # noqa: E402
import forest_type_data  # noqa: E402
import host_slots  # noqa: E402
import naip_imagery  # noqa: E402
import nfhl_data  # noqa: E402
import nhdplus_data  # noqa: E402
import nlcd_landcover_data  # noqa: E402
import nwi_data  # noqa: E402
import report_data  # noqa: E402
import report_progress  # noqa: E402
import run_diagnostics  # noqa: E402
import soil_road_ratings  # noqa: E402
import soil_survey  # noqa: E402
import soil_water_table  # noqa: E402
import soil_woodland  # noqa: E402
import structures_data  # noqa: E402
import transmission_lines  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402

BOUNDARY = [list(p) for p in REAL_BOUNDARY]
HOLD = 0.25
LAYERS = list(report_data.REPORT_FETCH_LAYERS)
SOIL_HOST = report_data.REPORT_LAYER_HOSTS["soil_survey"]
SOIL_LAYERS = [name for name, host in report_data.REPORT_LAYER_HOSTS.items() if host == SOIL_HOST]

with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
    DAYMET = daymet_data.parse_daymet_csv(handle.read())

# (owner, attribute) of every fetch entry point as fetch_report_data() reaches
# it, and of every parser it runs on the answer. The three climate functions
# are imported by name into report_data; the rest are reached off their
# modules.
ENTRY_POINTS = {
    "daymet_daily": (report_data, "get_daymet_daily_for_point"),
    "atlas14": (report_data, "get_atlas14_for_point"),
    "power_wind": (report_data, "get_power_wind_for_point"),
    "nhdplus_hr": (nhdplus_data, "get_flowline_attributes_for_boundary"),
    "nwi": (nwi_data, "get_wetlands_for_boundary"),
    "fema_nfhl": (nfhl_data, "get_flood_hazard_for_boundary"),
    "nlcd_landcover": (nlcd_landcover_data, "get_land_cover_for_boundary"),
    "soil_water_table": (soil_water_table, "get_seasonal_water_table_for_boundary"),
    "soil_road_ratings": (soil_road_ratings, "get_road_ratings_for_boundary"),
    "forest_type_group": (forest_type_data, "get_forest_type_for_boundary"),
    "soil_woodland": (soil_woodland, "get_woodland_for_boundary"),
    "soil_survey": (soil_survey, "get_survey_for_boundary"),
    "bedrock_geology": (bedrock_geology, "get_geology_for_boundary"),
    "naip_imagery": (naip_imagery, "get_naip_for_boundary"),
    "context_dem": (context_map_data, "get_context_dem_for_boundary"),
    "context_water": (context_map_data, "get_context_water_for_boundary"),
    "context_roads": (context_map_data, "get_context_roads_for_boundary"),
    "county_state": (census_geography, "get_county_state_for_point"),
    "structures": (structures_data, "get_structures_for_boundary"),
    "transmission_lines": (transmission_lines, "get_transmission_lines_near_boundary"),
}
PARSERS = (
    (report_data, "design_storms"), (report_data, "derive_wind"),
    (nhdplus_data, "parse_flowline_attributes"), (nwi_data, "parse_wetlands"), (nfhl_data, "parse_flood_hazard"),
    (nlcd_landcover_data, "parse_land_cover"), (soil_water_table, "parse_seasonal_water_table"),
    (soil_road_ratings, "parse_road_ratings"), (forest_type_data, "parse_forest_type"),
    (soil_woodland, "parse_woodland"), (soil_survey, "parse_survey"), (bedrock_geology, "parse_geology"),
    (naip_imagery, "parse_naip"), (census_geography, "parse_county_state"), (structures_data, "parse_structures"),
    (transmission_lines, "parse_transmission_lines"),
)
assert set(ENTRY_POINTS) == set(LAYERS), set(ENTRY_POINTS) ^ set(LAYERS)


class Stubs:
    """Every entry point replaced by a recording stub, every parser by the
    identity. `failing` names layers whose stub raises a ConnectionError
    (after its hold, like a budget spent); `hold` is per layer."""

    def __init__(self, failing=(), hold=None):
        self.failing = set(failing)
        self.hold = dict(hold or {})
        self.calls = {}      # layer -> [{"thread", "start", "end", "args"}]
        self.lock = threading.Lock()
        self._stack = contextlib.ExitStack()

    def _stub(self, name):
        def stub(*args, **kwargs):
            started = time.perf_counter()
            time.sleep(self.hold.get(name, HOLD))
            record = {"thread": threading.current_thread().name, "start": started,
                      "end": time.perf_counter(), "args": args}
            with self.lock:
                self.calls.setdefault(name, []).append(record)
            if name in self.failing:
                raise requests.exceptions.ConnectionError(f"{name} is down")
            if name == "daymet_daily":
                return DAYMET
            return {"layer": name}
        stub.__name__ = f"stub_{name}"
        return stub

    def __enter__(self):
        for name, (owner, attribute) in ENTRY_POINTS.items():
            self._stack.enter_context(mock.patch.object(owner, attribute, self._stub(name)))
        for owner, attribute in PARSERS:
            self._stack.enter_context(mock.patch.object(owner, attribute, lambda raw: raw))
        return self

    def __exit__(self, *exc):
        self._stack.close()

    def count(self, name):
        return len(self.calls.get(name, []))

    def in_flight_peak(self, names):
        """The most of `names` whose stubs were running at one moment."""
        events = []
        for name in names:
            for record in self.calls.get(name, []):
                events.append((record["start"], 1))
                events.append((record["end"], -1))
        peak = current = 0
        for _, delta in sorted(events):
            current += delta
            peak = max(peak, current)
        return peak


def fields(data):
    return {name: getattr(data, name) for name in LAYERS}


print("=" * 72)
print("test_report_fetch_concurrency.py -- twenty layers at once, each exactly once, capped per host")
print("=" * 72)

# ======================================================================
# 1 + 2. Exactly once, and concurrent
# ======================================================================
host_slots.reset()
with Stubs() as stubs:
    started = time.perf_counter()
    data = report_data.fetch_report_data(BOUNDARY)
    elapsed = time.perf_counter() - started
assert {name: stubs.count(name) for name in LAYERS} == {name: 1 for name in LAYERS}, stubs.calls
assert data.unavailable == {}, data.unavailable
assert data.daymet_daily is DAYMET and data.climate is not None
assert data.atlas14 == {"layer": "atlas14"} and data.design_storms == {"layer": "atlas14"}
assert data.power_wind == {"layer": "power_wind"} and data.wind == {"layer": "power_wind"}
for name in LAYERS[3:]:
    assert getattr(data, name) == {"layer": name}, (name, getattr(data, name))
assert data.severe_weather is not None and data.precipitation_correction is not None
threads = {record["thread"] for records in stubs.calls.values() for record in records}
peak = stubs.in_flight_peak(LAYERS)
assert elapsed < HOLD * 8, f"twenty holds of {HOLD}s took {elapsed:.2f}s -- not concurrent"
assert peak >= 8, f"at most {peak} stubs were ever in flight together"
assert all(name.startswith("keyline-report-fetch") for name in threads), threads
print(f"1. every one of the {len(LAYERS)} entry points called exactly once; every field holds its layer's answer.")
print(f"2. twenty fetches holding {HOLD}s each finished in {elapsed:.2f}s on {len(threads)} worker threads, "
      f"up to {peak} in flight together.")

# ======================================================================
# 3. Per host, not global; the timer measures the fetch, not the wait
# ======================================================================
cap = host_slots.concurrency_for(SOIL_HOST)
soil_peak = stubs.in_flight_peak(SOIL_LAYERS)
assert len(SOIL_LAYERS) == 4 and cap == 2, (SOIL_LAYERS, cap)
assert soil_peak == cap, f"{soil_peak} soil queries were in flight at once against a cap of {cap}"
measured = host_slots.peak_in_flight()
assert measured[SOIL_HOST] == cap, measured
assert all(count <= host_slots.concurrency_for(host) for host, count in measured.items()), measured
soil_span = max(r["end"] for n in SOIL_LAYERS for r in stubs.calls[n]) - min(r["start"] for n in SOIL_LAYERS for r in stubs.calls[n])
assert soil_span >= HOLD * (len(SOIL_LAYERS) / cap) * 0.9, f"four soil holds at a cap of {cap} spanned only {soil_span:.2f}s"
print(f"3. {len(SOIL_LAYERS)} soil layers on {SOIL_HOST}: never more than {soil_peak} in flight (cap {cap}), "
      f"spanning {soil_span:.2f}s; peaks measured {dict(sorted(measured.items()))}")

# ======================================================================
# 4. Daymet, then POWER
# ======================================================================
daymet_call, power_call = stubs.calls["daymet_daily"][0], stubs.calls["power_wind"][0]
assert power_call["start"] >= daymet_call["end"], "POWER started before Daymet had answered"
assert power_call["args"][2] == data.climate["years"], (power_call["args"], data.climate["years"])
others = [r["start"] for n in LAYERS if n not in ("daymet_daily", "power_wind") for r in stubs.calls[n]]
assert min(others) < daymet_call["end"], "the independent layers waited for Daymet"
print(f"4. POWER started {power_call['start'] - daymet_call['end']:.3f}s after Daymet returned, asked for "
      f"{data.climate['years'][0]}-{data.climate['years'][-1]}; the other layers did not wait for Daymet.")

# ======================================================================
# 5. A forced failure degrades that layer alone
# ======================================================================
host_slots.reset()
with Stubs(failing={"nwi"}) as stubs:
    degraded = report_data.fetch_report_data(BOUNDARY)
assert list(degraded.unavailable) == ["nwi"], degraded.unavailable
assert degraded.unavailable["nwi"]["reason"] == report_data.ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE
assert degraded.nwi is None
assert all(getattr(degraded, n) is not None for n in LAYERS if n != "nwi")
assert {name: stubs.count(name) for name in LAYERS} == {name: 1 for name in LAYERS}
with Stubs(failing={"transmission_lines", "atlas14", "soil_survey"}) as stubs:
    several = report_data.fetch_report_data(BOUNDARY)
assert list(several.unavailable) == ["atlas14", "soil_survey", "transmission_lines"], list(several.unavailable)
assert several.atlas14 is None and several.design_storms is None and several.soil_survey is None
print("5. NWI down: unavailable == ['nwi'], every other field present, every stub called once; three down: "
      f"{list(several.unavailable)} in the table's order.")

# ======================================================================
# 6. A required failure raises
# ======================================================================
with Stubs(failing={"daymet_daily"}) as stubs:
    try:
        report_data.fetch_report_data(BOUNDARY)
        raise AssertionError("a REQUIRED failure must raise")
    except report_data.ReportDataIncompleteError as exc:
        assert (exc.layer, exc.label, exc.reason) == ("climate", "climate records",
                                                     report_data.ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE), exc
assert all(stubs.count(name) <= 1 for name in LAYERS), stubs.calls
assert stubs.count("power_wind") == 0, "POWER must not be asked for years no climate has"
print(f"6. Daymet down: ReportDataIncompleteError(climate, source_unavailable); POWER never called, "
      f"{sum(stubs.count(n) for n in LAYERS)} other stubs ran at most once each.")

# ======================================================================
# 7. The progress bar, ticked from the workers
# ======================================================================
units = report_progress.plan_session_report(LAYERS, [], report_cached=False, context_cached=True,
                                            layer1_cached=True, design=False)
progress = report_progress.ReportProgress(units)
snapshots, stop = [], threading.Event()


def poll():
    while not stop.is_set():
        snapshots.append(progress.snapshot())
        time.sleep(0.005)


poller = threading.Thread(target=poll)
host_slots.reset()
with Stubs(hold={"fema_nfhl": HOLD * 4}) as stubs, report_progress.active(progress), \
        report_progress.stage(report_progress.STAGE_RECORDS):
    poller.start()
    report_data.fetch_report_data(BOUNDARY)
    stop.set()
    poller.join()
    final = progress.snapshot()
assert final["fetches"] == {"completed": len(LAYERS), "total": len(LAYERS)}, final
fractions = [s["fraction"] for s in snapshots]
assert fractions == sorted(fractions), "the bar went backwards"
assert len(set(fractions)) > 3, f"the bar moved through only {sorted(set(fractions))}"
flood = [s for s in snapshots if s["detail"] == "flood"]
assert flood and all(s["fetches"]["completed"] == len(LAYERS) - 1 for s in flood[-3:]), flood[-3:]
print(f"7. {len(snapshots)} polls: fetches {final['fetches']['completed']} of {final['fetches']['total']} "
      f"completed from the workers, {len(set(fractions))} distinct fractions, never backwards; the held "
      f"flood layer named while the other {len(LAYERS) - 1} finished around it.")

# ======================================================================
# 8. The diagnostics probe, carried into the workers
# ======================================================================
directory = tempfile.mkdtemp(prefix="report_fetch_concurrency_")
saved = {k: os.environ.get(k) for k in (run_diagnostics.ENABLED_ENV, run_diagnostics.DIRECTORY_ENV)}
os.environ[run_diagnostics.ENABLED_ENV] = "1"
os.environ[run_diagnostics.DIRECTORY_ENV] = directory
try:
    class NoCache:
        def contains(self, boundary):
            return False

    probe = run_diagnostics.begin_fetch("concurrency-test-session", BOUNDARY, NoCache(), "report")
    assert probe is not None
    host_slots.reset()
    with Stubs() as stubs:
        report_data.fetch_report_data(BOUNDARY)
    run_diagnostics._FETCH_PROBE.set(None)
finally:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
rows = {row["layer"]: row for row in probe.layers}
assert set(rows) == set(LAYERS), set(rows) ^ set(LAYERS)
assert sorted(row["order"] for row in probe.layers) == list(range(len(LAYERS))), [r["order"] for r in probe.layers]
assert all(row["outcome"] == "ok" for row in probe.layers)
soil_ms = {name: rows[name]["elapsed_ms"] for name in SOIL_LAYERS}
assert all(HOLD * 1000 * 0.9 <= ms < HOLD * 1000 * 1.8 for ms in soil_ms.values()), \
    f"a soil layer's row includes its wait for a slot: {soil_ms}"
assert run_diagnostics.time_layer("anything") is run_diagnostics._NO_LAYER, "the probe was not cleared"
print(f"8. {len(probe.layers)} rows written from the workers, orders 0..{len(LAYERS) - 1} distinct; the queued "
      f"soil layers each recorded one hold ({min(soil_ms.values()):.0f}-{max(soil_ms.values()):.0f} ms), not two.")

# ======================================================================
# 9. The hosts table
# ======================================================================
assert set(report_data.REPORT_LAYER_HOSTS) == set(LAYERS)
for name, host in report_data.REPORT_LAYER_HOSTS.items():
    assert host and "/" not in host and ":" not in host, (name, host)
shared = {}
for name, host in report_data.REPORT_LAYER_HOSTS.items():
    shared.setdefault(host, []).append(name)
multi = {host: names for host, names in shared.items() if len(names) > 1}
assert set(multi) <= set(host_slots.HOST_CONCURRENCY), f"a host with several layers has no named cap: {multi}"
print(f"9. every declared layer has a host; hosts carrying several layers, each with a named cap: "
      f"{ {h: len(n) for h, n in multi.items()} }")

print(f"\n{offline_harness.summary()}")
print("\ntest_report_fetch_concurrency.py: all sections passed")
