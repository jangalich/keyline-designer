"""
probe_transportation_host.py

HOW carto.nationalmap.gov BEHAVES, MEASURED -- the reliability probe for
the National Map transportation service, the host behind TWO fetch layers:

    Layer 1   farm_roads      farm_roads_data.get_farm_roads_for_boundary,
                              bbox + 150 m, HARD-FAIL on the design path
    report    context_roads   context_map_data.get_context_roads_for_
                              boundary, bbox + 1 mile, DEGRADABLE

Each is three ArcGIS queries, layers 30, 31 and 32 (ROAD_LAYERS), issued
one after another. This probe issues those six requests -- the same URL,
the same parameters, the same bbox arithmetic, taken from farm_roads_data
rather than re-typed -- once per round, over as long as it is left
running, and records what the host did with each one.

WHY THIS LIVES IN THE REPOSITORY. The NHD probe that measured
hydro.nationalmap.gov lived in a session's scratchpad and has been
rebuilt from the production query code twice since. A probe is only
worth its numbers if the next person can run the same one, so this one
is committed. It is a measurement harness in the diagnose_*.py family:
it imports the real module for its constants and its bbox and nothing on
the pipeline path imports it.

WHAT IT RECORDS, per request, as one JSON line:

    round, when, layer (30/31/32), extent ("parcel" = bbox+150 m,
    "mile" = bbox+1 mile), outcome, seconds (to the whole body), ttfb
    (seconds to the response headers -- the server's thinking time, which
    is what a production attempt's timeout actually bounds, since
    requests' timeout is between bytes and not a total), http status,
    bytes, feature count, and whether the row is a first request or the
    single RETRY issued after a failed one.

OUTCOMES, bucketed the way the NHD probe bucketed them so the two hosts
read side by side:

    ok_fast    answered 200 with a parseable body within 30 s
               (the production loop's FIRST-attempt timeout)
    ok_slow    answered, but took longer than 30 s
    http_5xx   a 5xx status
    http_4xx   a 4xx status, or an ArcGIS error in a 200 body
    timeout    no answer within --timeout seconds (default 150, past the
               90 s third-attempt timeout, so every "would the third
               attempt have made it" question is answerable)
    conn_error connection reset, TLS failure, DNS

THE RETRY ROW. Production retries a failed request after
fetch_attempts.RETRY_PAUSE_SECONDS with a longer timeout. When a first
request fails here, the probe pauses that long and issues the SAME
request once more, recorded with retry=true. That measures directly how
often the retry the budget pays for actually succeeds -- the number the
budget decision needs -- without re-implementing the production loop.

Run (an hour, a round every 150 s, results to a JSONL file):

    python3 probe_transportation_host.py --minutes 70 --interval 150 \
        --out roads_probe.jsonl

Summarise a results file (the tables the investigation reports):

    python3 probe_transportation_host.py --summarize roads_probe.jsonl

REQUIRES REAL NETWORK ACCESS to carto.nationalmap.gov. Like every other
network-backed script here it says so plainly if it cannot connect
rather than inventing numbers. It changes nothing: no budget, no query,
no layer list. The decision belongs to whoever reads the summary.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone

import requests

import context_map_data
import farm_roads_data
import fetch_attempts
import reference_fixture

# The reference parcel -- the same drawn boundary every timing in this
# repo is quoted on (5614 N Montour Rd, Gibsonia, PA; ~13.23 acres).
REFERENCE_PARCEL = reference_fixture.REAL_BOUNDARY

# The two extents the host is asked for, named as the layers that ask.
EXTENTS = {
    "parcel": 150.0,                                   # Layer 1 farm_roads (the default buffer)
    "mile": context_map_data.CONTEXT_BUFFER_METERS,    # report context_roads
}

FAST_SECONDS = 30.0   # the production loop's first-attempt timeout
DEFAULT_TIMEOUT = 150.0
DEFAULT_INTERVAL = 150.0


def query_params(bbox: tuple) -> dict:
    """farm_roads_data._query_road_layer's request, parameter for
    parameter. Kept in step with it by test_probe_transportation_host.py
    would be ideal; there is no such test, so if that function's params
    change, change these."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "outFields": "*",
        "f": "geojson",
    }


def the_requests(boundary=REFERENCE_PARCEL) -> list:
    """The six (layer, extent, url, params) a round issues, in the order
    production issues them: the parcel's three, then the mile's three."""
    out = []
    for extent, buffer_m in EXTENTS.items():
        bbox = farm_roads_data._bounding_box(boundary, buffer_meters=buffer_m)
        for layer in farm_roads_data.ROAD_LAYERS:
            out.append((layer, extent, f"{farm_roads_data.TRANSPORTATION_BASE}/{layer}/query", query_params(bbox)))
    return out


def issue(url: str, params: dict, timeout: float) -> dict:
    """One request, one row's worth of facts about what the host did."""
    started = time.monotonic()
    row = {"status": None, "bytes": 0, "features": None, "error": None, "ttfb": None}
    try:
        # stream=True so the headers' arrival is timed on its own: requests'
        # `timeout` is a connect-and-between-bytes timeout, not a total, so
        # the production attempt fails when the SERVER is silent for 30 s,
        # which is the time to first byte here, not the whole transfer.
        response = requests.get(url, params=params, timeout=timeout, stream=True)
        row["ttfb"] = round(time.monotonic() - started, 3)
        content = response.content
        row["seconds"] = round(time.monotonic() - started, 3)
        row["status"] = response.status_code
        row["bytes"] = len(content)
        if 500 <= response.status_code:
            row["outcome"] = "http_5xx"
        elif 400 <= response.status_code:
            row["outcome"] = "http_4xx"
        else:
            try:
                data = response.json()
            except ValueError as e:
                row["outcome"], row["error"] = "http_4xx", f"unparseable body: {e}"
            else:
                if "error" in data:
                    row["outcome"], row["error"] = "http_4xx", json.dumps(data["error"])[:200]
                else:
                    row["features"] = len(data.get("features", []))
                    row["outcome"] = "ok_fast" if row["seconds"] <= FAST_SECONDS else "ok_slow"
    except requests.exceptions.Timeout:
        row["seconds"] = round(time.monotonic() - started, 3)
        row["outcome"] = "timeout"
    except requests.exceptions.RequestException as e:
        row["seconds"] = round(time.monotonic() - started, 3)
        row["outcome"], row["error"] = "conn_error", f"{type(e).__name__}: {e}"[:200]
    return row


def run(minutes: float, interval: float, timeout: float, out_path: str, retry: bool) -> None:
    deadline = time.monotonic() + minutes * 60
    plan = the_requests()
    rnd = 0
    print(f"probing {farm_roads_data.TRANSPORTATION_BASE} for {minutes:g} min, a round every {interval:g} s, "
          f"{len(plan)} requests a round, {timeout:g} s timeout, retry after a failure: {retry}", flush=True)
    with open(out_path, "a") as out:
        while True:
            rnd += 1
            round_started = time.monotonic()
            for layer, extent, url, params in plan:
                for attempt in ("first", "retry"):
                    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    row = issue(url, params, timeout)
                    row.update({"round": rnd, "when": when, "layer": layer, "extent": extent,
                                "retry": attempt == "retry"})
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(f"  r{rnd:03d} L{layer} {extent:6s} {'retry ' if row['retry'] else ''}"
                          f"{row['outcome']:10s} {row['seconds']:7.2f}s (first byte "
                          f"{'--' if row['ttfb'] is None else f'{row[chr(116)+chr(116)+chr(102)+chr(98)]:.2f}'} s)  {row['bytes']:>7d} B  "
                          f"{'' if row['features'] is None else row['features']} features", flush=True)
                    failed = row["outcome"] not in ("ok_fast", "ok_slow")
                    if attempt == "first" and failed and retry:
                        time.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                        continue
                    break
                time.sleep(2)
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.0, interval - (time.monotonic() - round_started)))
    print(f"done: {rnd} rounds -> {out_path}", flush=True)


# ----------------------------------------------------------------------
# The summary: the tables the investigation reports
# ----------------------------------------------------------------------

def _pct(n: int, d: int) -> str:
    return f"{n} ({100.0 * n / d:.1f}%)" if d else "0"


def _quantiles(values: list) -> str:
    if not values:
        return "--"
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
    return f"{statistics.median(values):.1f} / {p90:.1f} / {values[-1]:.1f} s"


def summarize(path: str) -> None:
    rows = [json.loads(line) for line in open(path) if line.strip()]
    firsts = [r for r in rows if not r["retry"]]
    retries = [r for r in rows if r["retry"]]
    answered = ("ok_fast", "ok_slow")
    if not firsts:
        print("no rows")
        return
    whens = sorted(r["when"] for r in rows)
    print(f"{path}: {len(firsts)} first requests, {len(retries)} retries, {firsts[-1]['round']} rounds, "
          f"{whens[0]} .. {whens[-1]}\n")

    def table(title: str, groups: dict) -> None:
        print(title)
        print(f"  {'group':22s} {'n':>4s} {'ok<=30s':>14s} {'ok slow':>12s} {'5xx':>10s} {'4xx':>10s} "
              f"{'timeout':>12s} {'conn':>10s}   median / p90 / max (answered)   features")
        for name, g in groups.items():
            n = len(g)
            counts = {k: sum(1 for r in g if r["outcome"] == k)
                      for k in ("ok_fast", "ok_slow", "http_5xx", "http_4xx", "timeout", "conn_error")}
            secs = [r["seconds"] for r in g if r["outcome"] in answered]
            feats = sorted({r["features"] for r in g if r["features"] is not None})
            fstr = f"{feats[0]}" if len(feats) == 1 else f"{feats[0]}..{feats[-1]}" if feats else "--"
            print(f"  {name:22s} {n:>4d} {_pct(counts['ok_fast'], n):>14s} {_pct(counts['ok_slow'], n):>12s} "
                  f"{_pct(counts['http_5xx'], n):>10s} {_pct(counts['http_4xx'], n):>10s} "
                  f"{_pct(counts['timeout'], n):>12s} {_pct(counts['conn_error'], n):>10s}   "
                  f"{_quantiles(secs):>28s}   {fstr}")
        print()

    table("ALL FIRST REQUESTS, BY QUERY", {
        f"layer {layer} @ {extent}": [r for r in firsts if r["layer"] == layer and r["extent"] == extent]
        for extent in EXTENTS for layer in farm_roads_data.ROAD_LAYERS})
    table("BY EXTENT (Layer 1 farm_roads = parcel; report context_roads = mile)", {
        extent: [r for r in firsts if r["extent"] == extent] for extent in EXTENTS})
    table("BY LAYER", {f"layer {layer}": [r for r in firsts if r["layer"] == layer] for layer in farm_roads_data.ROAD_LAYERS})
    table("HOST, ALL FIRST REQUESTS", {"carto.nationalmap.gov": firsts})

    # What the progressive timeouts would have bought: of the first
    # requests that would have FAILED a 30 s attempt (answered after 30 s,
    # or did not answer), how many answered by 60 s, by 90 s.
    print("WHAT A LONGER TIMEOUT BUYS (first requests whose headers took over 30 s, or that never answered)")
    ttfb = lambda r: r["ttfb"] if r.get("ttfb") is not None else r["seconds"]
    late = [r for r in firsts if not (r["outcome"] in answered and ttfb(r) <= 30)]
    by60 = sum(1 for r in late if r["outcome"] in answered and ttfb(r) <= 60)
    by90 = sum(1 for r in late if r["outcome"] in answered and ttfb(r) <= 90)
    ever = sum(1 for r in late if r["outcome"] in answered)
    print(f"  would fail at 30 s: {len(late)} of {len(firsts)}; of those answered by 60 s: {by60}, "
          f"by 90 s: {by90}, ever (<= {max([r['seconds'] for r in rows]) if rows else 0:.0f} s): {ever}; "
          f"never: {len(late) - ever}\n")

    # What the retry bought: of failed first requests, how many retries
    # after RETRY_PAUSE_SECONDS answered, and how fast.
    print(f"WHAT THE RETRY BUYS (one re-issue {fetch_attempts.RETRY_PAUSE_SECONDS:g} s after a failed first request)")
    failed_firsts = [r for r in firsts if r["outcome"] not in answered]
    rt_ok = [r for r in retries if r["outcome"] in answered]
    print(f"  failed first requests: {len(failed_firsts)}; retries issued: {len(retries)}; retries answered: "
          f"{len(rt_ok)} ({_quantiles([r['seconds'] for r in rt_ok])}); retries answered within 30 s: "
          f"{sum(1 for r in rt_ok if r['seconds'] <= 30)}\n")

    # Whole-layer outcome per round: the three sequential queries of each
    # layer, as production issues them, under three candidate budgets.
    print("PER-ROUND LAYER OUTCOME UNDER CANDIDATE BUDGETS (each layer = its three queries, sequential)")
    budgets = {
        "current 30/60/90 + 2x15 s pause": (30, 60, 90),
        "one attempt, 30 s": (30,),
        "two attempts, 30/60": (30, 60),
        "one attempt, 15 s": (15,),
        "one attempt, 60 s": (60,),
    }
    rounds = sorted({r["round"] for r in firsts})
    for extent in EXTENTS:
        print(f"  {extent}:")
        for name, timeouts in budgets.items():
            degraded = 0
            stage_secs = []
            for rnd in rounds:
                layer_secs = 0.0
                any_failed = False
                for layer in farm_roads_data.ROAD_LAYERS:
                    first = [r for r in firsts if r["round"] == rnd and r["extent"] == extent and r["layer"] == layer]
                    if not first:
                        continue
                    first = first[0]
                    rts = [r for r in retries if r["round"] == rnd and r["extent"] == extent and r["layer"] == layer]
                    # Simulate: attempt k answers if the observed request
                    # (first, then the retry if one was issued) answered
                    # within attempt k's timeout. A first request that
                    # never answered is charged the timeout; the retry
                    # row stands in for every later attempt.
                    chain = [first] + rts
                    spent, ok = 0.0, False
                    for k, t in enumerate(timeouts):
                        obs = chain[min(k, len(chain) - 1)]
                        # The attempt survives its timeout when the server
                        # answered (headers) inside it; the body then streams.
                        waited = obs.get("ttfb") if obs.get("ttfb") is not None else obs["seconds"]
                        if obs["outcome"] in answered and waited <= t:
                            spent += obs["seconds"]
                            ok = True
                            break
                        spent += min(obs["seconds"], t) if obs["outcome"] in answered or obs["outcome"] == "timeout" else obs["seconds"]
                        if k + 1 < len(timeouts):
                            spent += fetch_attempts.RETRY_PAUSE_SECONDS
                    layer_secs += spent
                    any_failed = any_failed or not ok
                degraded += 1 if any_failed else 0
                stage_secs.append(layer_secs)
            print(f"    {name:34s} degraded in {degraded:>2d} of {len(rounds)} rounds; "
                  f"layer wall time median / p90 / max {_quantiles(stage_secs)}")
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=70.0)
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="seconds between round starts")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--out", default="roads_probe.jsonl")
    ap.add_argument("--no-retry", action="store_true", help="do not re-issue a failed request after the pause")
    ap.add_argument("--summarize", metavar="JSONL", help="print the tables for a results file and exit")
    args = ap.parse_args(argv)
    if args.summarize:
        summarize(args.summarize)
        return 0
    try:
        requests.get(farm_roads_data.TRANSPORTATION_BASE, params={"f": "json"}, timeout=30).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"cannot reach {farm_roads_data.TRANSPORTATION_BASE}: {type(e).__name__}: {e}\n"
              f"This probe needs real network access to carto.nationalmap.gov; nothing was measured.", file=sys.stderr)
        return 2
    run(args.minutes, args.interval, args.timeout, args.out, retry=not args.no_retry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
