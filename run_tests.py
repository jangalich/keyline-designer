#!/usr/bin/env python3
"""
run_tests.py

THE REGRESSION SUITE, RUN AS ONE COMMAND.

    python3 run_tests.py                 # the full suite, one process per file, all cores
    python3 run_tests.py --tier fast     # the pre-commit tier: every file not in FULL_ONLY
    python3 run_tests.py test_water_step.py test_roads_step.py
    python3 run_tests.py --jobs 2 --timeout 600
    python3 run_tests.py --list          # what would run, and in which tier

Every test_*.py in this directory is a script: top-level asserts that
raise on the first failure and print what they proved. That shape is
deliberate and unchanged -- each file still runs on its own with
`python3 test_x.py`. What was missing was the layer above it: one
command that runs them all, in parallel, with a per-file clock and a
per-file verdict, so "the suite passes" is a fact the machine states
rather than a claim a person assembles from eighty-one terminals.

TIERS. A file is FAST unless it is named in FULL_ONLY. The fast tier is
the pre-commit gate (tens of seconds); the full tier is everything and is
what CI runs. A new test file lands in the fast tier by default, and is
promoted here when it measures as slow -- the runner prints the per-file
times, so the list is maintained from evidence.

NEVER `--live`. Some files accept a `--live` flag that reaches the real
USGS/USDA services; this runner never passes it, so the suite is offline
by construction. Run those by hand when the question is the service.

Exit status is the number of failing files, capped at 1: zero means every
file exited 0 inside its timeout.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))

# Files that measured above ~5 s on a 4-core box with the offline harness
# in place (python3 run_tests.py prints the numbers). Everything else is
# the fast tier. Longest first, so a worker pool starts the long ones
# early and the wall clock is bounded by the longest file, not by the
# order of the alphabet.
FULL_ONLY = (
    "test_run_diagnostics.py",
    "test_roads_step.py",
    "test_water_step.py",
    "test_fencing_step.py",
    "test_road_network_router.py",
    "test_roads_step_inputs.py",
    "test_structures_step.py",
    "test_step_commit.py",
    "test_trees_step.py",
    "test_session_api.py",
    "test_session_manager.py",
    "test_fence_display_geometry.py",
    "test_step_orchestrator.py",
    "test_render_layout_map.py",
    "test_eligible_union.py",
    "test_pipeline_context.py",
    "test_road_corridors_pipeline.py",
    "test_road_corridors.py",
)

TAIL_LINES = 40


def discover() -> list[str]:
    return sorted(
        name for name in os.listdir(HERE)
        if name.startswith("test_") and name.endswith(".py")
    )


def ordered(files: list[str]) -> list[str]:
    """FULL_ONLY files first, in FULL_ONLY's own (longest-first) order;
    then the rest alphabetically."""
    rank = {name: index for index, name in enumerate(FULL_ONLY)}
    return sorted(files, key=lambda name: (rank.get(name, len(FULL_ONLY)), name))


def run_one(name: str, timeout: float, out_dir: str) -> dict:
    out_path = os.path.join(out_dir, name + ".out")
    env = dict(os.environ)
    env.setdefault("MPLBACKEND", "Agg")
    env.setdefault("PYTHONUNBUFFERED", "1")
    started = time.perf_counter()
    with open(out_path, "wb") as out:
        try:
            completed = subprocess.run(
                [sys.executable, name],
                cwd=HERE, env=env, stdout=out, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            )
            code = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            code = None
            timed_out = True
    return {
        "name": name,
        "seconds": time.perf_counter() - started,
        "code": code,
        "timed_out": timed_out,
        "out_path": out_path,
    }


def tail(path: str, lines: int = TAIL_LINES) -> str:
    try:
        with open(path, "r", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError as e:
        return f"(no output captured: {e})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("files", nargs="*", help="specific test files to run (default: the tier)")
    parser.add_argument("--tier", choices=("fast", "full"), default="full")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per file")
    parser.add_argument("--list", action="store_true", help="print the selection and exit")
    parser.add_argument("--keep-output", action="store_true", help="print where every file's output was written")
    args = parser.parse_args(argv)

    available = discover()
    if args.files:
        missing = [f for f in args.files if f not in available]
        if missing:
            parser.error(f"not test files in {HERE}: {', '.join(missing)}")
        selected = list(args.files)
    elif args.tier == "fast":
        selected = [f for f in available if f not in FULL_ONLY]
    else:
        selected = available
    selected = ordered(selected)

    if args.list:
        for name in selected:
            print(f"{'full' if name in FULL_ONLY else 'fast':4}  {name}")
        print(f"{len(selected)} file(s)")
        return 0

    out_dir = tempfile.mkdtemp(prefix="keyline-tests-")
    print(f"running {len(selected)} file(s) with {args.jobs} worker(s), {args.timeout:.0f} s timeout each")
    wall_started = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(run_one, name, args.timeout, out_dir): name for name in selected}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if result["timed_out"]:
                verdict = "TIMEOUT"
            elif result["code"] == 0:
                verdict = "ok"
            else:
                verdict = f"FAIL exit={result['code']}"
            print(f"{result['seconds']:8.1f}s  {verdict:14}  {result['name']}", flush=True)
    wall = time.perf_counter() - wall_started

    failures = [r for r in results if r["timed_out"] or r["code"] != 0]
    for result in failures:
        print(f"\n===== {result['name']}: last {TAIL_LINES} lines ({result['out_path']}) =====")
        print(tail(result["out_path"]))

    total = sum(r["seconds"] for r in results)
    slowest = sorted(results, key=lambda r: -r["seconds"])[:10]
    print("\nslowest:")
    for r in slowest:
        print(f"{r['seconds']:8.1f}s  {r['name']}")
    print(
        f"\n{len(results) - len(failures)} passed, {len(failures)} failed, "
        f"{total:.0f} s of file time in {wall:.0f} s wall"
    )
    if args.keep_output or failures:
        print(f"outputs: {out_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
