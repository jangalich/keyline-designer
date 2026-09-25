"""
make_overview_fixtures.py

CAPTURES THE SITE OVERVIEW'S REFERENCE FIXTURES from the live services --
one real response per report-layer source for the reference parcel
(reference_fixture.REAL_BOUNDARY), written under assets/reference/overview/
so every overview test runs from them with the network refused
(overview_reference_fixture.py loads them).

    python3 make_overview_fixtures.py

What it writes, and the fetch each one is the verbatim answer to:

    context_dem.npz / .json   context_map_data.get_context_dem_for_boundary()
                              -- the 300-cell DEM over the parcel's extent
                              plus one mile: the array, and its geometry
    context_water.json        context_map_data.get_context_water_for_boundary()
    context_roads.json        context_map_data.get_context_roads_for_boundary()
    county_state.json         census_geography.get_county_state_for_point()
                              at report_data's centroid
    structures.json           structures_data.get_structures_for_boundary()
    transmission_lines.json   transmission_lines.get_transmission_lines_near_boundary()
    capture.json              when, how big, how long, and how many requests

Run from a machine with network; the sandbox the tests run in has none.
"""

import json
import os
import sys
import time
from datetime import date

import numpy as np
import requests

import census_geography
import context_map_data
import structures_data
import transmission_lines
from reference_fixture import REAL_BOUNDARY
from report_data import boundary_centroid_lat_lon

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "reference", "overview")


def _write_json(name, payload):
    path = os.path.join(OUT, name)
    text = json.dumps(payload, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def _write_dem(dem):
    np.savez_compressed(os.path.join(OUT, "context_dem.npz"), array=dem["array"])
    meta = {k: dem[k] for k in ("resolution_meters", "origin_x", "origin_y", "crs")}
    return _write_json("context_dem.json", meta) + os.path.getsize(os.path.join(OUT, "context_dem.npz"))


class _Counter:
    """Counts and sizes every requests.get made during a capture."""

    def __init__(self):
        self.calls = []
        self._get = requests.get

    def __enter__(self):
        def counting(url, *args, **kwargs):
            response = self._get(url, *args, **kwargs)
            self.calls.append({"url": url.split("?")[0], "bytes": len(response.content)})
            return response
        requests.get = counting
        return self

    def __exit__(self, *exc):
        requests.get = self._get


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    lat, lon = boundary_centroid_lat_lon(REAL_BOUNDARY)
    record = {"boundary": [list(p) for p in REAL_BOUNDARY], "captured_on": date.today().isoformat(),
              "centroid": [lat, lon], "files": {}}
    captures = (
        ("context_dem", lambda: context_map_data.get_context_dem_for_boundary(REAL_BOUNDARY), _write_dem,
         lambda v: f"{v['array'].shape[1]}x{v['array'].shape[0]} cells at {v['resolution_meters'][0]:.2f} m"),
        ("context_water", lambda: context_map_data.get_context_water_for_boundary(REAL_BOUNDARY),
         lambda v: _write_json("context_water.json", v),
         lambda v: f"{len(v['streams'])} flowlines, {len(v['water_bodies'])} waterbodies"),
        ("context_roads", lambda: context_map_data.get_context_roads_for_boundary(REAL_BOUNDARY),
         lambda v: _write_json("context_roads.json", v), lambda v: f"{len(v)} road segments"),
        ("county_state", lambda: census_geography.get_county_state_for_point(lat, lon),
         lambda v: _write_json("county_state.json", v),
         lambda v: census_geography.county_state_label(census_geography.parse_county_state(v))),
        ("structures", lambda: structures_data.get_structures_for_boundary(REAL_BOUNDARY),
         lambda v: _write_json("structures.json", v), lambda v: f"{len(v['response']['features'])} structures"),
        ("transmission_lines", lambda: transmission_lines.get_transmission_lines_near_boundary(REAL_BOUNDARY),
         lambda v: _write_json("transmission_lines.json", v), lambda v: f"{len(v['response']['features'])} lines"),
    )
    for name, fetch, write, describe in captures:
        with _Counter() as counter:
            started = time.time()
            payload = fetch()
            elapsed = time.time() - started
        size = write(payload)
        record["files"][name] = {"bytes_on_disk": size, "seconds": round(elapsed, 1), "describes": describe(payload),
                                 "requests": len(counter.calls), "response_bytes": sum(c["bytes"] for c in counter.calls)}
        print(f"{name:20s} {len(counter.calls)} request(s) {sum(c['bytes'] for c in counter.calls):>9,} B received "
              f"{size:>9,} B on disk {elapsed:5.1f} s  {describe(payload)}")
    _write_json("capture.json", record)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
