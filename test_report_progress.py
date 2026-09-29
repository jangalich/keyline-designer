"""
test_report_progress.py

THE REPORT'S PROGRESS BAR NEVER LIES -- completed work over total work,
asserted from the counter up to the wire.

Offline. Part A is report_progress.ReportProgress on its own; part B runs
real reports through the real routes -- the real fetch_report_data() loop
over offline_harness (so every degradable layer degrades through its own
retry path and its own tick), the real session context, eight real
sections, the real WeasyPrint pass -- with a thread polling GET
/api/jobs/<id> the whole time and every snapshot kept.

  A1. THE PLAN: warm = 21 fetches + 15 section units + 3 pages units, 100
      weight; cold adds the rebuild (10 Layer 1 layers + the warm-up, 20);
      a Layer 1 cache hit makes a rebuild warm-up only; a cached report
      layer plans no fetches. The kind table covers REPORT_FETCH_LAYERS
      exactly, and the rebuild counts parcel_data.FETCH_LAYERS.
  A2. MONOTONIC AND BOUNDED under any order, duplicate ticks, ticks for
      units the plan does not hold, settles, and eight threads ticking at
      once -- the parallel-fetch branch's case.
  A3. THE LABEL names the longest-outstanding unit, so a stall keeps
      naming what everything is waiting on; "maps" while an SVG is drawn.
  A4. A FAILURE freezes the fraction and the label; it never completes.
  A5. The plan is fixed once.
  B1. A WARM RUN: never a rebuild stage, every fetch counted one by one,
      never backwards, never over 100, 100 only at done.
  B2. A COLD RUN: the rebuild is in the total from the first planned poll,
      so the bar never goes backwards and the total never changes.
  B3. A STALLED LAYER: the flood-map fetch held for 2.5 s -- the bar
      does not move and the label names flood maps the whole time.
  B4. FAILURES: a required layer (climate) fails at 0% naming climate; the
      page layout failing mid-run leaves the bar where it stopped, under
      100, in the pages stage. Neither completes.
  B5. A GENERATE'S JOB CARRIES NO PROGRESS KEY -- it reports none.
"""

import random
import threading
import time
from unittest.mock import patch as mock_patch

import offline_harness

offline_harness.install()

import job_runner  # noqa: E402
import nfhl_data  # noqa: E402
import overview_reference_fixture  # noqa: E402
import parcel_data  # noqa: E402
import report_data  # noqa: E402
import report_progress  # noqa: E402
import session_api  # noqa: E402
import session_cache  # noqa: E402
import session_report  # noqa: E402
from fencing_step_fixture import Harness, Session  # noqa: E402
from report_progress import (  # noqa: E402
    STAGE_MAPS, STAGE_PAGES, STAGE_REBUILD, STAGE_RECORDS, STAGE_TERRAIN, ReportProgress, Unit,
)


def assert_honest(snapshots, label):
    """THE TWO INVARIANTS, over a whole recorded sequence: the fraction
    never decreases and never exceeds 1, the total never changes once
    planned, and 100% is never shown before the last unit is done."""
    previous = 0.0
    planned_total = None
    for index, snap in enumerate(snapshots):
        fraction = snap["fraction"]
        assert 0.0 <= fraction <= 1.0, f"{label}: fraction {fraction} out of range at poll {index}"
        assert fraction >= previous, f"{label}: went BACKWARDS {previous} -> {fraction} at poll {index}: {snap}"
        assert 0 <= snap["percent"] <= 100, snap
        if snap["percent"] == 100:
            assert snap["completed"] == snap["total"], f"{label}: 100% before every unit: {snap}"
        if snap["total"]:
            if planned_total is None:
                planned_total = snap["total"]
            assert snap["total"] == planned_total, f"{label}: the total changed mid-run: {snap}"
        previous = fraction


# ======================================================================
# A. The counter
# ======================================================================

print("=" * 72)
print("test_report_progress.py -- completed work over total work, never time")
print("=" * 72)

REPORT_LAYERS = list(report_data.REPORT_FETCH_LAYERS)
LAYER1 = list(parcel_data.FETCH_LAYERS)


def plan(**overrides):
    args = dict(report_cached=False, context_cached=True, layer1_cached=True, design=True)
    args.update(overrides)
    return report_progress.plan_session_report(REPORT_LAYERS, LAYER1, **args)


print("\n[A1] THE PLAN")
assert len(REPORT_LAYERS) == 21, REPORT_LAYERS
assert set(report_progress.REPORT_LAYER_KINDS) == set(REPORT_LAYERS), (
    set(report_progress.REPORT_LAYER_KINDS) ^ set(REPORT_LAYERS)
)
# NO MODULE NAME IS A KIND: the sub-label names data, not a service.
assert not set(report_progress.REPORT_LAYER_KINDS.values()) & set(REPORT_LAYERS)

WARM = plan()
COLD = plan(context_cached=False, layer1_cached=False)
REBUILD_ONLY_WARM_UP = plan(context_cached=False, layer1_cached=True)
REPORT_CACHED = plan(report_cached=True)
NO_DESIGN = plan(design=False)


def weight(units, stage=None):
    return sum(u.weight for u in units if stage is None or u.stage == stage)


assert len(WARM) == 21 + 15 + 3, len(WARM)
assert abs(weight(WARM) - 100.0) < 1e-9, weight(WARM)
assert abs(weight(WARM, STAGE_RECORDS) - 80.0) < 1e-9
assert abs(weight(WARM, STAGE_TERRAIN) - 10.0) < 1e-9
assert abs(weight(WARM, STAGE_PAGES) - 10.0) < 1e-9
assert not [u for u in WARM if u.stage == STAGE_REBUILD], "a warm run plans no rebuild"
assert len(COLD) == len(WARM) + len(LAYER1) + 1
assert abs(weight(COLD, STAGE_REBUILD) - 20.0) < 1e-9
assert 0.15 < weight(COLD, STAGE_REBUILD) / weight(COLD) < 0.2, "the rebuild is ~17% of a cold run, as measured"
assert [u.name for u in REBUILD_ONLY_WARM_UP if u.stage == STAGE_REBUILD] == [report_progress.WARM_UP_UNIT]
assert not [u for u in REPORT_CACHED if u.stage == STAGE_RECORDS], "a cached report layer plans no fetches"
assert len(NO_DESIGN) == len(WARM) - 2
assert all(u.kind for u in WARM if u.stage == STAGE_RECORDS)
print(f"   warm {len(WARM)} units / {weight(WARM):.0f}; cold {len(COLD)} units / {weight(COLD):.0f} "
      f"(rebuild {weight(COLD, STAGE_REBUILD) / weight(COLD):.0%}); warm-up-only rebuild; cached layer: no fetches")
print("   PASS")


print("\n[A2] MONOTONIC AND BOUNDED -- any order, duplicates, strangers, settles, threads")
rng = random.Random(23)
for trial in range(200):
    units = COLD if trial % 2 else WARM
    progress = ReportProgress(units)
    keys = [u.key for u in units] + ["records:not_planned", "rebuild:nope"]
    rng.shuffle(keys)
    seen = [progress.snapshot()]
    for key in keys:
        roll = rng.random()
        if roll < 0.1:
            progress.settle(rng.choice(report_progress.STAGES))
        progress.begin(key)
        progress.complete(key)
        if roll > 0.9:
            progress.complete(key)  # a duplicate counts once
        seen.append(progress.snapshot())
    progress.finish()
    seen.append(progress.snapshot())
    assert_honest(seen, f"trial {trial}")
    assert seen[-1]["fraction"] == 1.0 and seen[-1]["percent"] == 100

# EIGHT THREADS AT ONCE, and a reader polling throughout: the concurrent
# fetch stage the parallel branch will run. Completion is membership, so
# the order the threads land in cannot matter.
progress = ReportProgress(COLD)
progress.enter_stage(STAGE_RECORDS)
reads = []
stop = threading.Event()


def reader():
    while not stop.is_set():
        reads.append(progress.snapshot())


def worker(keys):
    for key in keys:
        progress.begin(key)
        time.sleep(0.0005)
        progress.complete(key)


keys = [u.key for u in COLD]
rng.shuffle(keys)
poller = threading.Thread(target=reader)
poller.start()
workers = [threading.Thread(target=worker, args=(keys[i::8],)) for i in range(8)]
for w in workers:
    w.start()
for w in workers:
    w.join()
stop.set()
poller.join()
reads.append(progress.snapshot())
assert_honest(reads, "eight threads")
assert reads[-1]["completed"] == len(COLD) and reads[-1]["percent"] == 100
print(f"   200 shuffled runs with duplicates, unplanned keys and settles; 8 threads, {len(reads)} reads: "
      f"never backwards, never over 100")
print("   PASS")


print("\n[A3] THE LABEL -- the longest-outstanding unit; maps while drawing")
clock = [0.0]
progress = ReportProgress(WARM, clock=lambda: clock[0])
progress.enter_stage(STAGE_RECORDS)
progress.begin("records:fema_nfhl")
clock[0] = 1.0
progress.begin("records:nwi")
clock[0] = 2.0
progress.begin("records:soil_survey")
progress.complete("records:nwi")
progress.complete("records:soil_survey")
snap = progress.snapshot()
assert (snap["stage"], snap["detail"]) == (STAGE_RECORDS, "flood"), snap
assert snap["fetches"] == {"completed": 2, "total": 21}, snap
progress.complete("records:fema_nfhl")
assert progress.snapshot()["detail"] is None
progress.enter_stage(STAGE_TERRAIN)
progress.drawing_started()
assert progress.snapshot()["stage"] == STAGE_MAPS
progress.drawing_finished()
assert progress.snapshot()["stage"] == STAGE_TERRAIN
print("   flood maps named while two later fetches finish around it; 'maps' only while an SVG draws")
print("   PASS")


print("\n[A4] A FAILURE FREEZES -- the fraction stays, the label stays, nothing completes")
progress = ReportProgress(WARM)
with report_progress.active(progress):
    with report_progress.stage(STAGE_RECORDS):
        for u in WARM[:5]:
            with report_progress.unit(u.stage, u.name):
                pass
        before = progress.snapshot()
        try:
            with report_progress.unit(STAGE_RECORDS, "bedrock_geology"):
                raise RuntimeError("the service fell over")
        except RuntimeError:
            progress.fail()
after = progress.snapshot()
assert after["fraction"] == before["fraction"] and after["completed"] == 5, (before, after)
assert after["failed"] is True and (after["stage"], after["detail"]) == (STAGE_RECORDS, "geology"), after
progress.fail()  # idempotent
assert progress.snapshot() == after
print(f"   stopped at {after['percent']}%, label frozen on {after['stage']}/{after['detail']}, failed=True")
print("   PASS")


print("\n[A5] THE PLAN IS FIXED ONCE")
progress = ReportProgress(WARM)
try:
    progress.set_plan(COLD)
    raise AssertionError("a second plan must be refused")
except RuntimeError:
    pass
try:
    ReportProgress([Unit(STAGE_PAGES, "x", 1.0), Unit(STAGE_PAGES, "x", 1.0)])
    raise AssertionError("a duplicate unit must be refused")
except ValueError:
    pass
print("   set_plan() twice refused; duplicate units refused")
print("   PASS")


# ======================================================================
# B. Real reports, polled
# ======================================================================

FIXTURE = overview_reference_fixture.report_data(nlcd_landcover=None, forest_type_group=None)


def fixture_daymet(lat, lon):
    return FIXTURE.daymet_daily


class Report:
    """A report over the real routes with a thread polling its job every
    10 ms, keeping every snapshot. A fresh report-layer cache each time,
    over the REAL fetch_report_data(), so the fetch stage really runs."""

    def __init__(self, session):
        self.session = session
        self.report_cache = session_cache.FetchCache(fetch_function=report_data.fetch_report_data)
        deps = session_api.Dependencies(
            store=session.store, fetch_cache=session.fetch_cache, cache=session.cache,
            runner=session.runner, reports=session_report.ReportStore(max_reports=4),
            report_fetch_cache=self.report_cache,
        )
        self.client = session_api.create_app(deps).test_client()

    def run(self):
        accepted = self.client.post(f"/api/sessions/{self.session.id}/report", json={})
        assert accepted.status_code == 202, accepted.get_json()
        job_id = accepted.get_json()["job_id"]
        snapshots = []
        deadline = time.time() + 900
        while True:
            body = self.client.get(f"/api/jobs/{job_id}").get_json()
            assert "progress" in body, f"every report poll carries progress: {body}"
            snapshots.append({**body["progress"], "status": body["status"], "t": time.time()})
            if body["status"] != job_runner.STATUS_RUNNING:
                return body, snapshots
            assert time.time() < deadline
            time.sleep(0.01)


def commit_everything(session):
    """Every step committed through the real orchestrator --
    test_session_report_route.py's helper, for its reasons."""
    def fencing():
        payload = session.generate("fencing")
        features = [f for f in payload["fence_lines"]["features"] if f["properties"]["fence_type"] == "boundary"]
        return session.commit("fencing", features, {f["id"]: "generated" for f in features})

    commit = {
        "landform": lambda: session.commit_landform(whole=True),
        "water": lambda: session.commit_water(2),
        "roads": lambda: session.commit_roads(),
        "trees": lambda: session.commit_trees(1),
        "structures": lambda: session.commit_structures(1),
        "fencing": fencing,
    }
    for entry in session_report.uncommitted_steps(session.stored()):
        commit[entry["step_id"]]()
    assert not session_report.uncommitted_steps(session.stored())


def stages_seen(snapshots):
    order = []
    for snap in snapshots:
        if snap["stage"] and (not order or order[-1] != snap["stage"]):
            order.append(snap["stage"])
    return order


def describe(snapshots):
    fractions = sorted({s["percent"] for s in snapshots})
    return f"{len(snapshots)} polls, {len(fractions)} distinct percentages, stages {stages_seen(snapshots)}"


with Harness(), mock_patch.object(report_data, "get_daymet_daily_for_point", fixture_daymet):
    SESSION = Session()
    commit_everything(SESSION)

    print("\n[B1] A WARM RUN")
    assert SESSION.id in SESSION.cache
    body, snaps = Report(SESSION).run()
    assert body["status"] == job_runner.STATUS_DONE, body
    assert_honest(snaps, "warm")
    final = snaps[-1]
    assert final["percent"] == 100 and final["completed"] == final["total"] == 39, final
    assert STAGE_REBUILD not in stages_seen(snaps), stages_seen(snaps)
    assert all(s["percent"] < 100 for s in snaps if s["status"] == job_runner.STATUS_RUNNING and
               s["completed"] < s["total"])
    fetch_counts = [s["fetches"]["completed"] for s in snaps if s["total"]]
    assert fetch_counts == sorted(fetch_counts) and fetch_counts[-1] == 21, fetch_counts
    assert all(s["fetches"]["total"] == 21 for s in snaps if s["total"])
    print(f"   {describe(snaps)}")
    print(f"   fetch counts observed: {sorted(set(fetch_counts))}")
    print("   PASS")

    print("\n[B2] A COLD RUN -- session cache and Layer 1 cache emptied")
    SESSION.cache.discard(SESSION.id)
    SESSION.fetch_cache.clear()
    body, snaps = Report(SESSION).run()
    assert body["status"] == job_runner.STATUS_DONE, body
    assert_honest(snaps, "cold")
    planned = [s for s in snaps if s["total"]]
    assert planned[0]["total"] == 39 + len(parcel_data.FETCH_LAYERS) + 1, planned[0]
    assert STAGE_REBUILD in stages_seen(snaps), stages_seen(snaps)
    assert snaps[-1]["percent"] == 100
    print(f"   {describe(snaps)}; total {planned[0]['total']} units from the first planned poll")
    print("   PASS")

    print("\n[B3] A STALLED LAYER -- flood maps held for 2.5 s")
    real_flood = nfhl_data.get_flood_hazard_for_boundary
    HOLD = 2.5

    def slow_flood(*args, **kwargs):
        time.sleep(HOLD)
        return real_flood(*args, **kwargs)  # offline: then it degrades

    with mock_patch.object(nfhl_data, "get_flood_hazard_for_boundary", slow_flood):
        body, snaps = Report(SESSION).run()
    assert body["status"] == job_runner.STATUS_DONE, body
    assert_honest(snaps, "stall")
    held = [s for s in snaps if s["detail"] == "flood"]
    span = held[-1]["t"] - held[0]["t"]
    assert span > HOLD * 0.8, f"flood maps were named for only {span:.2f}s"
    assert len({s["fraction"] for s in held}) == 1, "the bar moved while the flood maps were outstanding"
    assert all(s["stage"] == STAGE_RECORDS for s in held)
    print(f"   {len(held)} polls over {span:.2f}s: bar held at {held[0]['percent']}% "
          f"({held[0]['fetches']['completed']} of 21 fetches), label 'flood' throughout")
    print("   PASS")

    print("\n[B4] FAILURES -- the bar stays where the run stopped")

    def daymet_down(lat, lon):
        raise report_data.requests.exceptions.ConnectionError("daymet is down")

    with mock_patch.object(report_data, "get_daymet_daily_for_point", daymet_down):
        body, snaps = Report(SESSION).run()
    assert body["status"] == job_runner.STATUS_FAILED and "failed_layer" in body["error"], body
    assert_honest(snaps, "required failure")
    last = snaps[-1]
    assert last["failed"] and last["percent"] == 0 and last["detail"] == "climate", last
    print(f"   climate required and down: failed at {last['percent']}%, label {last['stage']}/{last['detail']}")

    import weasyprint  # noqa: E402

    def layout_falls_over(self, *args, **kwargs):
        raise RuntimeError("layout fell over")

    with mock_patch.object(weasyprint.HTML, "render", layout_falls_over):
        body, snaps = Report(SESSION).run()
    assert body["status"] == job_runner.STATUS_FAILED, body
    assert_honest(snaps, "layout failure")
    last = snaps[-1]
    assert last["failed"] and 85 <= last["percent"] < 100 and last["completed"] < last["total"], last
    assert last["stage"] == STAGE_PAGES, last
    running_max = max(s["fraction"] for s in snaps if s["status"] == job_runner.STATUS_RUNNING)
    assert last["fraction"] == running_max, "a failure must not move the bar"
    print(f"   page layout fails: failed at {last['percent']}% ({last['completed']}/{last['total']}), "
          f"stage {last['stage']}")
    print("   PASS")

    print("\n[B5] A GENERATE'S JOB CARRIES NO PROGRESS")
    runner = job_runner.JobRunner(max_workers=1)
    job = runner.submit(lambda: 1).wait(5)
    assert "progress" not in job.snapshot(), job.snapshot()
    runner.shutdown()
    print("   no 'progress' key on a job submitted without one")
    print("   PASS")

print("\n" + "=" * 72)
print("test_report_progress.py: the bar moves only when work completes, never goes\n"
      "backwards, never passes 100, holds on a stall naming it, and stops on failure.")
print("=" * 72)
