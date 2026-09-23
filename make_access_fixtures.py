"""
make_access_fixtures.py

CAPTURES THE ACCESS SECTION'S REFERENCE FIXTURES from the live services --
one real response per source for the reference parcel
(reference_fixture.REAL_BOUNDARY), written under assets/reference/access/
so every Access test runs from them with the network refused
(access_reference_fixture.py loads them).

    python3 make_access_fixtures.py

What it writes, and the fetch each one is the verbatim answer to:

    farm_roads.json          farm_roads_data.get_farm_roads_for_boundary()
                             -- the road rows Layer 1 holds on
                             ParcelData.farm_roads, with the properties
                             dict branch 10 added (name, geometry,
                             properties). The test fixtures every step
                             test runs on carry a synthetic "Fixture Rd";
                             this is the parcel's own mapped roads, never
                             captured before this branch.
    soil_road_ratings.json   soil_road_ratings.get_road_ratings_for_boundary()
    capture.json             when, and how big each response was

Run from a machine with network; the sandbox the tests run in has none.
"""

import json
import os
import sys
import time
from datetime import date

import farm_roads_data
import soil_road_ratings
from reference_fixture import REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "reference", "access")


def _write_json(name, payload):
    path = os.path.join(OUT, name)
    text = json.dumps(payload, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return os.path.getsize(path)


def _capture(name, fetch, tries: int = 4):
    for attempt in range(tries):
        try:
            return fetch()
        except Exception as exc:  # noqa: BLE001 -- the capture reports and retries
            if attempt == tries - 1:
                raise
            print(f"  {name}: {type(exc).__name__}, retrying in {5 * (attempt + 1)} s")
            time.sleep(5 * (attempt + 1))


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    record = {"boundary": [list(p) for p in REAL_BOUNDARY], "captured_on": date.today().isoformat(), "files": {}}
    steps = [
        ("farm_roads.json", lambda: farm_roads_data.get_farm_roads_for_boundary(REAL_BOUNDARY)),
        ("soil_road_ratings.json", lambda: soil_road_ratings.get_road_ratings_for_boundary(REAL_BOUNDARY)),
    ]
    for name, fetch in steps:
        started = time.time()
        payload = _capture(name, fetch)
        elapsed = time.time() - started
        on_disk = _write_json(name, payload)
        record["files"][name] = {"bytes_on_disk": on_disk, "rows": len(payload), "seconds": round(elapsed, 1)}
        print(f"{name:24s} {len(payload):>5} rows  {on_disk:>9,} B  {elapsed:5.1f} s")
    _write_json("capture.json", record)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
