"""
diagnose_report_progress.py

WHAT THE REPORT'S PROGRESS BAR SAYS THROUGH A REAL RUN, LIVE -- every
change of the job's `progress`, timestamped, as a client polling it would
have seen it.

    python3 diagnose_report_progress.py warm  [out_dir]
    python3 diagnose_report_progress.py cold  [out_dir]
    python3 diagnose_report_progress.py stall [out_dir] [seconds]

THE SESSION is diagnose_report_generation_time.py's: a real session on
reference_fixture.REAL_BOUNDARY, created live, with design_record_fixture.
json's committed steps grafted on.

  warm   the session context cached, as it is the moment the last step
         is committed; the report layer fetched at report time.
  cold   the session cache AND the Layer 1 fetch cache emptied first --
         the user who comes back the next day. The rebuild is in the plan.
  stall  warm, with the flood-map fetch held `seconds` (default 40) before
         it runs: the bar must hold and the label must keep naming it.

THE REPORT RUNS AS A JOB (session_report.submit_report), and this polls
job.snapshot() every 250 ms -- the wire shape GET /api/jobs/<id> returns --
recording each snapshot whose progress differs from the last. Writes
progress-<scenario>.json: [{"t", "status", "progress"}], and prints the
sequence with the time each stage took, beside the share of the bar the
plan gave it -- the check on the weights.

LIVE NETWORK, BY DESIGN, like its sibling: this must not install
offline_harness.
"""

import json
import os
import sys
import tempfile
import time

import requests

import diagnose_report_generation_time as timing
import document_store
import job_runner
import nfhl_data
import session_cache
import session_report

POLL_SECONDS = 0.25


def make_session(out_dir):
    store = document_store.JSONFileStore(os.path.join(out_dir, "store"))
    fetch_cache = session_cache.FetchCache()
    cache = session_cache.SessionCache()
    for attempt in range(1, 11):
        try:
            session_id, created = timing.make_session(store, fetch_cache, cache)
            return store, fetch_cache, cache, session_id, created
        except requests.exceptions.RequestException as exc:
            print(f"setup attempt {attempt}: session creation failed ({type(exc).__name__}); waiting 20 s")
            time.sleep(20)
    raise SystemExit("could not create the session")


def run(scenario, out_dir, hold_seconds=40.0):
    os.makedirs(out_dir, exist_ok=True)
    store, fetch_cache, cache, session_id, created = make_session(out_dir)
    print(f"session {session_id} created in {created:.1f}s")
    if scenario == "cold":
        cache.clear()
        fetch_cache.clear()

    restore = None
    if scenario == "stall":
        real = nfhl_data.get_flood_hazard_for_boundary

        def held(*args, **kwargs):
            time.sleep(hold_seconds)
            return real(*args, **kwargs)

        nfhl_data.get_flood_hazard_for_boundary = held
        restore = lambda: setattr(nfhl_data, "get_flood_hazard_for_boundary", real)  # noqa: E731

    runner = job_runner.JobRunner(max_workers=1)
    reports = session_report.ReportStore(directory=out_dir)
    report_cache = session_cache.FetchCache(fetch_function=timing.report_data.fetch_report_data)
    started = time.perf_counter()
    job = session_report.submit_report(
        session_id, store, fetch_cache=fetch_cache, cache=cache, runner=runner, reports=reports,
        report_fetch_cache=report_cache, property_label="5614 N Montour Rd",
    )
    sequence = []
    last = None
    while True:
        snap = job.snapshot()
        progress = snap["progress"]
        key = (snap["status"], json.dumps(progress, sort_keys=True))
        if key != last:
            sequence.append({"t": round(time.perf_counter() - started, 2), "status": snap["status"],
                             "progress": progress})
            last = key
        if snap["status"] != job_runner.STATUS_RUNNING:
            break
        time.sleep(POLL_SECONDS)
    total = time.perf_counter() - started
    if restore:
        restore()
    runner.shutdown()

    with open(os.path.join(out_dir, f"progress-{scenario}.json"), "w", encoding="utf-8") as handle:
        json.dump({"scenario": scenario, "total_seconds": round(total, 2), "status": snap["status"],
                   "error": snap.get("error"), "sequence": sequence}, handle, indent=2)

    print(f"\n{scenario}: {snap['status']} in {total:.1f}s, {len(sequence)} distinct snapshots")
    print(f"{'t':>7}  {'%':>4}  {'units':>7}  {'fetches':>7}  stage / detail")
    previous = -1.0
    for row in sequence:
        p = row["progress"]
        assert p["fraction"] >= previous, f"BACKWARDS at {row}"
        assert p["fraction"] <= 1.0
        previous = p["fraction"]
        print(f"{row['t']:7.2f}  {p['percent']:4d}  {p['completed']:3d}/{p['total']:<3d}  "
              f"{p['fetches']['completed']:3d}/{p['fetches']['total']:<3d}  {p['stage']} / {p['detail']}")

    # Time per stage against the share of the bar that stage covered: each
    # interval between snapshots, and each step the bar took at its end,
    # belongs to the stage the run was in during it. "maps" is a label
    # inside the section stage, so it is counted there.
    spans = {}
    for row, nxt in zip(sequence, sequence[1:]):
        stage = row["progress"]["stage"] or "(before the plan)"
        stage = "terrain" if stage == "maps" else stage
        seconds, bar = spans.get(stage, (0.0, 0.0))
        spans[stage] = (seconds + nxt["t"] - row["t"], bar + nxt["progress"]["fraction"] - row["progress"]["fraction"])
    print("\nstage               seconds  share of run   share of bar")
    for stage, (seconds, bar) in spans.items():
        print(f"{stage:<19} {seconds:7.1f}  {seconds / total:11.0%}   {bar:11.0%}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("warm", "cold", "stall"):
        print(__doc__)
        sys.exit(2)
    out = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="report-progress-")
    hold = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
    sys.exit(run(sys.argv[1], out, hold))
