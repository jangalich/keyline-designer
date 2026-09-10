"""
diagnose_farm_roads_attempts.py

THE LIVE HALF OF THE ATTEMPT-COUNT BRANCH. The measurement this repo's
offline tests structurally cannot make, because it needs real answers
from real USGS/USDA/Microsoft services -- and the one the branch was
opened to make possible.

THE FINDING THIS ANSWERS. Three real cold creations on the reference
parcel, minutes apart, recorded farm_roads at 34.9 s, 1.3 s and 1.4 s. A
25x swing on one layer of one parcel, and nothing in the record could
explain it, because no fetch module published an attempt count.
farm_roads_data._query_road_layer has a 2-retry budget with a
time.sleep(2) between attempts and is called once per entry in
ROAD_LAYERS, so 34.9 s was consistent with BOTH of:

  A. IT SOMETIMES RETRIES. Up to 9 attempts and 6 two-second pauses per
     fetch, so 12 s of the wait could be sleep alone, with the rest in
     requests that timed out at 30/60/90 s before being retried.
  B. IT MAKES ONE REQUEST THAT IS SOMETIMES VERY SLOW. Three attempts
     total (one per layer), no sleep at all, and one ArcGIS response that
     took half a minute to come back.

Those are completely different problems -- A is a backoff/budget
question, B is a service-latency question -- and the record could not
tell them apart. Now it can: every retry loop publishes its attempt count
and the milliseconds it spent asleep (see fetch_attempts.py), and this
script runs several real cold fetches and prints both, per layer, per
run.

WHAT DECIDES IT. Per farm_roads row, in one run:

    attempts == 3 and retry_sleep_ms == 0    -> hypothesis B
    attempts > 3  and retry_sleep_ms >= 2000 -> hypothesis A

and `elapsed_ms` beside them says how much of the wait each accounts for.
A slow run with attempts 3 and no sleep is one slow request, full stop.

THIS SCRIPT REPORTS. It does not change the retry budget, the backoff or
anything else about how farm_roads is fetched -- that is a decision for
whoever reads the numbers, and it should be made against measurements
rather than against either hypothesis.

Run:  python3 diagnose_farm_roads_attempts.py [--runs=N] [--layer=NAME]

REQUIRES REAL NETWORK ACCESS to carto.nationalmap.gov, elevation.national
map.gov, sdmdataaccess.sc.egov.usda.gov, hydro.nationalmap.gov and
planetarycomputer.microsoft.com -- like every other network-backed module
in this repo, it will not run in a sandbox whose egress policy blocks
them, and it SAYS SO PLAINLY rather than inventing numbers. Pass
--layer=farm_roads to fetch only that layer (which needs only
carto.nationalmap.gov) when the rest are unreachable or you only care
about the finding.

NOTHING HERE IS ON THE PIPELINE PATH. A measurement harness, in the same
standalone family as diagnose_elevation_source_and_fetch_cost.py: it
imports the real modules and calls them, and no pipeline module imports
it.
"""

import sys
import time

import fetch_attempts
import parcel_data
import run_diagnostics

# The reference parcel -- the same real drawn boundary every timing quoted
# in this branch and the last one is measured on (5614 N Montour Rd,
# Gibsonia, PA; ~13.23 acres, UTM 17N).
REFERENCE_PARCEL = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

# The two hypotheses, stated as the rows that would support each. Printed
# beside the measurement rather than left in the docstring, so the output
# is readable on its own.
HYPOTHESIS_A = "it sometimes RETRIES -- attempts above the layer count, with sleep beside them"
HYPOTHESIS_B = "it makes ONE request that is sometimes VERY SLOW -- attempts at the layer count, no sleep"


def _fetch_once(boundary):
    """
    One REAL cold fetch of every layer, timed and counted through
    run_diagnostics' own probe -- the same instrumentation a session
    creation uses, so what is printed here is exactly what a record
    would carry.

    Returns (layer rows, total seconds). The probe is opened directly
    rather than through session_manager because there is no session to
    create: this measures the fetch, and a session would add a document
    store and a cache to the thing being measured.
    """

    class _NoCache:
        """begin_fetch() asks the cache whether this boundary is already
        held -- a cold fetch is the whole point here, so the answer is
        always no and nothing is remembered between runs."""

        @staticmethod
        def contains(_boundary):
            return False

    probe = run_diagnostics.begin_fetch(
        f"diagnose-farm-roads-{int(time.time() * 1000)}", boundary, _NoCache, "diagnostic"
    )
    if probe is None:
        raise RuntimeError(
            f"run_diagnostics.begin_fetch() returned no probe -- set "
            f"{run_diagnostics.ENABLED_ENV}=1 (this script sets it for you; if you see this, "
            f"something else unset it)."
        )
    started = time.perf_counter()
    try:
        parcel_data.fetch_parcel_data(boundary)
    finally:
        elapsed = time.perf_counter() - started
        # Not record_fetch(): that writes a session record to disk and
        # clears the probe. The rows are wanted in hand, here.
        rows = list(probe.layers)
        run_diagnostics._LOCAL.fetch_probe = None
    return rows, elapsed


def _fetch_layer_only(boundary):
    """
    farm_roads ALONE, through the same timer, for an environment that can
    reach carto.nationalmap.gov but not the other four hosts. The
    published values are read off the module the same way the record
    reads them, so this and the full run print the same columns.
    """
    from farm_roads_data import get_farm_roads_for_boundary

    started = time.perf_counter()
    error = None
    try:
        get_farm_roads_for_boundary(boundary)
    except Exception as exc:  # noqa: BLE001 -- a diagnostic reports, it does not swallow
        error = exc
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    import farm_roads_data

    return [
        {
            "layer": "farm_roads",
            "elapsed_ms": elapsed_ms,
            "outcome": "ok" if error is None else "raised",
            "error_message": None if error is None else f"{type(error).__name__}: {error}",
            "attempts": getattr(farm_roads_data, fetch_attempts.ATTEMPTS_ATTRIBUTE, None),
            "retry_sleep_ms": getattr(farm_roads_data, fetch_attempts.SLEEP_ATTRIBUTE, None),
            "attempt_detail": getattr(farm_roads_data, fetch_attempts.DETAIL_ATTRIBUTE, None),
        }
    ], elapsed_ms / 1000.0


def _format_row(row) -> str:
    attempts = row["attempts"]
    sleep_ms = row["retry_sleep_ms"]
    detail = row.get("attempt_detail") or {}
    helpers = detail.get("helpers") or {}
    breakdown = ", ".join(
        f"{name.rpartition('.')[2]} x{body['calls']} calls / {body['attempts']} attempts"
        for name, body in sorted(helpers.items())
    )
    return (
        f"    {row['layer']:<34} {row['elapsed_ms']:>10.1f} ms"
        f"  attempts={'--' if attempts is None else attempts:>3}"
        f"  slept={'--' if sleep_ms is None else f'{sleep_ms:.0f}':>7} ms"
        f"  {row['outcome']:<7}"
        + (f"  [{breakdown}]" if breakdown else "")
    )


def _verdict(rows) -> str:
    """
    What the farm_roads rows across all runs SAY, stated only as far as
    they support it. Deliberately refuses to choose when they do not: a
    diagnostic that guessed would be the thing this branch exists to stop.

    A FETCH THAT RAISED IS NOT A MEASUREMENT OF THE FINDING, and this is
    the trap worth naming. The finding is about fetches that SUCCEEDED
    slowly (34.9 s, then 1.3 s, then 1.4 s -- three real cold creations,
    none of which failed). A blocked egress policy makes every attempt
    fail instantly, which burns the whole 9-attempt budget and six real
    two-second pauses, and would read as a textbook hypothesis A while
    saying nothing at all about the live service. So the verdict is drawn
    ONLY from rows whose outcome is 'ok', and a run with none says so.
    """
    from farm_roads_data import ROAD_LAYERS

    counted = [row for row in rows if row["attempts"] is not None]
    if not counted:
        return (
            "NO COUNTS WERE PUBLISHED for farm_roads. Either the fetch never reached the layer, "
            "or the entry point is not wrapped -- run `python3 run_diagnostics.py` and read the "
            "'retrying layers publish attempts' line."
        )

    succeeded = [row for row in counted if row["outcome"] == "ok"]
    if not succeeded:
        return (
            f"NO VERDICT: all {len(counted)} farm_roads fetches RAISED. Every attempt failing is "
            f"not the thing being diagnosed -- the finding is about fetches that SUCCEEDED and "
            f"were slow. A run like this burns the whole budget "
            f"({max(row['attempts'] for row in counted)} attempts, up to "
            f"{max((row['retry_sleep_ms'] or 0.0) for row in counted):.0f} ms asleep) and would "
            f"read as hypothesis A while saying nothing about the live service. Read the error "
            f"beside each row: an egress policy that blocks carto.nationalmap.gov produces "
            f"exactly this. Re-run where the host is reachable."
        )

    floor = len(ROAD_LAYERS)
    retried = [row for row in succeeded if row["attempts"] > floor]
    slept = [row for row in succeeded if (row["retry_sleep_ms"] or 0.0) > 0.0]
    slowest = max(succeeded, key=lambda row: row["elapsed_ms"])
    caveat = (
        ""
        if len(succeeded) == len(counted)
        else f" ({len(counted) - len(succeeded)} further fetch(es) RAISED and are excluded.)"
    )

    if not retried and not slept:
        return (
            f"HYPOTHESIS B, over {len(succeeded)} successful fetch(es). Every one made exactly "
            f"{floor} attempts -- one per ROAD_LAYERS entry, none retried -- and slept 0 ms. The "
            f"slowest took {slowest['elapsed_ms']:.0f} ms, ALL of it in requests that eventually "
            f"answered. A 34.9 s fetch of this shape is one very slow ArcGIS response, not a "
            f"retry storm.{caveat}"
        )
    if retried:
        return (
            f"HYPOTHESIS A, over {len(succeeded)} successful fetch(es). {len(retried)} of them "
            f"made more than {floor} attempts (up to "
            f"{max(row['attempts'] for row in succeeded)}), spending up to "
            f"{max((row['retry_sleep_ms'] or 0.0) for row in succeeded):.0f} ms asleep between "
            f"them. The slowest took {slowest['elapsed_ms']:.0f} ms with {slowest['attempts']} "
            f"attempts and {slowest['retry_sleep_ms']:.0f} ms of sleep -- so the retries and "
            f"their pauses account for a real share of the wait.{caveat}"
        )
    return (
        f"NEITHER, CLEANLY: {floor} attempts everywhere but non-zero sleep, which should not "
        f"happen (a pause only follows a failed attempt). Report the rows above rather than "
        f"reading a verdict into them.{caveat}"
    )


def measure(boundary, runs: int, layer_only: bool) -> int:
    farm_roads_rows = []
    for index in range(runs):
        print(f"\n  RUN {index + 1} of {runs}")
        # EVERY RUN IS COLD. Nothing is cached between them here -- there
        # is no FetchCache in this script at all -- so each row is a real
        # network fetch, which is the only kind worth counting.
        fetch_attempts.clear()
        rows, elapsed = (
            _fetch_layer_only(boundary) if layer_only else _fetch_once(boundary)
        )
        for row in rows:
            print(_format_row(row))
            if row["layer"] == "farm_roads":
                farm_roads_rows.append(row)
        print(f"    {'total':<34} {elapsed * 1000.0:>10.1f} ms")

    print("\n" + "=" * 78)
    print("  farm_roads, across every run")
    print("=" * 78)
    print(f"    {'run':<6}{'elapsed_ms':>13}{'attempts':>11}{'slept_ms':>11}  outcome")
    for index, row in enumerate(farm_roads_rows, start=1):
        attempts = "--" if row["attempts"] is None else row["attempts"]
        slept = "--" if row["retry_sleep_ms"] is None else f"{row['retry_sleep_ms']:.0f}"
        print(
            f"    {index:<6}{row['elapsed_ms']:>13.1f}{attempts:>11}{slept:>11}  {row['outcome']}"
        )
        if row["error_message"]:
            print(f"           {row['error_message']}")

    print(f"\n  A: {HYPOTHESIS_A}")
    print(f"  B: {HYPOTHESIS_B}\n")
    print("  " + _verdict(farm_roads_rows))
    print(
        "\n  REPORTED, NOT ACTED ON. This branch measures; whether the budget, the backoff or "
        "\n  the layer set should change is a separate decision, to be made against these rows."
    )
    return 0


def main(argv) -> int:
    import os

    runs = 3
    layer_only = False
    for arg in argv:
        if arg.startswith("--runs="):
            runs = int(arg.split("=", 1)[1])
        elif arg == "--layer=farm_roads":
            layer_only = True
        elif arg.startswith("--layer="):
            print(f"only --layer=farm_roads is supported, got {arg!r}")
            return 2

    # The layer timers are no-ops unless diagnostics are on -- see
    # run_diagnostics.time_layer(). Set here rather than asked of the
    # caller, because a run of this script that silently measured nothing
    # would be the worst outcome available.
    os.environ.setdefault(run_diagnostics.ENABLED_ENV, "1")

    print("Reference parcel: 5614 N Montour Rd, Gibsonia, PA (~13.23 acres)")
    print(f"Fetching {'farm_roads only' if layer_only else 'every layer'}, {runs} cold runs.\n")

    try:
        return measure(REFERENCE_PARCEL, runs, layer_only)
    except Exception as exc:  # noqa: BLE001 -- a diagnostic reports, it does not swallow
        print(f"\nMEASUREMENT DID NOT COMPLETE: {type(exc).__name__}: {exc}")
        print(
            "\nThis script needs real access to the USGS/USDA/Planetary Computer hosts listed in\n"
            "the module docstring. In an environment whose egress policy blocks them there is\n"
            "nothing to measure here -- run it on a machine with real network access rather than\n"
            "substituting an estimate. --layer=farm_roads needs only carto.nationalmap.gov."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
