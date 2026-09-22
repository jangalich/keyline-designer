#!/usr/bin/env python3
"""
make_normals_daymet_cache.py

DAYMET AT EVERY NORMALS STATION, ONCE. Fetches Daymet daily precipitation
for the normals period (1991-2020) at the coordinates of every station
in the normals bundle whose annual precipitation normal is of accepted
completeness, and stores each station's mean annual total -- the
denominator of precipitation_normals' ratio -- in a cache JSON that
make_normals_bundle.py folds into the bundle.

    python3 make_normals_daymet_cache.py [--workers 6] [--limit N]

WHY A CACHE, WHY ONCE. The ratio of a station's normal to Daymet at the
station compares two FIXED-PERIOD figures: neither changes between
reports. Fetching them at report time (branch 7 phase 1) cost five
Daymet calls and five failure points per report on a REQUIRED source;
the author's phase 2 decision moved them here. They are refreshed with
the bundle when Daymet publishes a new version -- the cache records the
version served for each station so a partial refresh is visible.

RESUMABLE. The cache is read on start and every station already in it
is skipped; progress is written every WRITE_EVERY stations, so an
interrupted run loses at most that many fetches. A station Daymet
cannot serve (outside its coverage -- American Samoa, Guam, the
Marianas; a coastal station whose cell is sea) is recorded with
`error` so it is not retried forever and so the bundle can say the
station has no ratio.

COURTESY. Six workers against daymet.ornl.gov, each request ~150 KB,
about 9,200 stations: roughly twenty minutes. The service publishes no
rate limit; this is a one-off build, not a report-time load.
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

import daymet_data
import precipitation_normals

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(_HERE, "assets", "reference", "ncei_prcp_normals_daymet_cache.json")
WRITE_EVERY = 50


def load_cache(path: str = CACHE_PATH) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return {"stations": {}}


def save_cache(cache: dict, path: str = CACHE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, sort_keys=True)
    os.replace(tmp, path)


def fetch_station(station: dict) -> dict:
    """{'daymet_annual_mm', 'daymet_version', 'daymet_citation'} or {'error'}."""
    try:
        text = daymet_data._request_csv(
            station["latitude"], station["longitude"], precipitation_normals.NORMALS_YEARS, 1, ("prcp",)
        )
        parsed = daymet_data.parse_daymet_csv(text, required_variables=("prcp",))
        missing = daymet_data.incomplete_years(parsed, precipitation_normals.NORMALS_YEARS)
        if missing:
            return {"error": f"incomplete years {missing[:3]}"}
        return {
            "daymet_annual_mm": precipitation_normals.annual_precipitation_mm(parsed),
            "daymet_version": parsed.get("software_version"),
            "daymet_citation": parsed.get("citation"),
            "daymet_latitude": parsed.get("latitude"),
            "daymet_longitude": parsed.get("longitude"),
        }
    except requests.exceptions.HTTPError as exc:
        return {"error": f"HTTP {exc.response.status_code if exc.response is not None else '?'}"}
    except (requests.exceptions.RequestException, daymet_data.DaymetIncompleteError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None, help="fetch at most this many new stations")
    parser.add_argument("--retry-errors", action="store_true", help="retry stations recorded with an error")
    parser.add_argument("--bundle", default=precipitation_normals.BUNDLE_PATH)
    args = parser.parse_args(argv)

    _, stations = precipitation_normals.read_bundle(args.bundle)
    cache = load_cache()
    done = cache["stations"]
    todo = [
        s for s in stations
        if s["prcp_flag"] in precipitation_normals.ACCEPTED_FLAGS
        and (s["station"] not in done or (args.retry_errors and "error" in done[s["station"]]))
    ]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(stations)} stations in the bundle; {len(done)} cached; {len(todo)} to fetch with {args.workers} workers")

    lock = threading.Lock()
    completed = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_station, s): s for s in todo}
        for future in as_completed(futures):
            station = futures[future]
            result = future.result()
            with lock:
                done[station["station"]] = result
                completed += 1
                if completed % WRITE_EVERY == 0 or completed == len(todo):
                    save_cache(cache)
                    errors = sum(1 for v in done.values() if "error" in v)
                    print(f"  {completed}/{len(todo)} ({time.time() - started:.0f} s), {errors} errors so far", flush=True)
    save_cache(cache)
    errors = {k: v["error"] for k, v in done.items() if "error" in v}
    print(f"cached {len(done)} stations, {len(errors)} without Daymet; wrote {CACHE_PATH}")
    for sid, err in list(errors.items())[:10]:
        print(f"   {sid}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
