"""
make_trees_fixtures.py

CAPTURES THE TREES & FORESTRY SECTION'S REFERENCE FIXTURES from the live
services -- one real response per source for the reference parcel
(reference_fixture.REAL_BOUNDARY), written under assets/reference/trees/
so every Trees test runs from them with the network refused
(trees_reference_fixture.py loads them).

    python3 make_trees_fixtures.py

What it writes, and the fetch each one is the verbatim answer to:

    canopy_hag.npz + canopy_hag.json
                         canopy_height_data.get_canopy_height_for_boundary()
                         on the captured DEM grid (terrain_reference_fixture)
                         -- the lidar HAG array Layer 1 holds on
                         ParcelData.canopy_height, and the dict's other
                         keys. The reference session ran on a zero canopy
                         grid before this branch; the Trees section needs
                         the parcel's own.
    canopy_tcc.npz + canopy_tcc.json
                         canopy_cover_data.get_tree_canopy_cover_for_boundary()
                         on the same grid -- what the design's fallback
                         would have consumed had HAG been absent here, for
                         the fallback session and its render.
    forest_type.tif + forest_type.json
                         forest_type_data.get_forest_type_for_boundary()
                         (the window beside the TIFF bytes)
    soil_woodland.json   soil_woodland.get_woodland_for_boundary() -- the
                         two row sets
    capture.json         when, and how big each response was

Run from a machine with network; the sandbox the tests run in has none.
"""

import json
import os
import sys
import time
from datetime import date

import numpy as np

import canopy_cover_data
import canopy_height_data
import forest_type_data
import soil_woodland
from reference_fixture import REAL_BOUNDARY
from terrain_reference_fixture import load_dem

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "reference", "trees")


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


def _write_canopy(stem: str, canopy: dict) -> int:
    """The array as a compressed npz, every other key as JSON; the grid
    keys are the DEM's and are rebuilt from it on load."""
    array_path = os.path.join(OUT, stem + ".npz")
    np.savez_compressed(array_path, array=canopy["array"])
    meta = {k: v for k, v in canopy.items() if k not in ("array", "resolution_meters", "origin_x", "origin_y", "crs")}
    return os.path.getsize(array_path) + _write_json(stem + ".json", meta)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    dem = load_dem()
    record = {"boundary": [list(p) for p in REAL_BOUNDARY], "captured_on": date.today().isoformat(), "files": {}}

    started = time.time()
    hag = _capture("canopy_hag", lambda: canopy_height_data.get_canopy_height_for_boundary(REAL_BOUNDARY, dem))
    assert canopy_height_data.canopy_source(hag) == canopy_height_data.CANOPY_SOURCE_LIDAR_HAG, hag.get("source")
    elapsed = time.time() - started
    size = _write_canopy("canopy_hag", hag)
    record["files"]["canopy_hag"] = {"bytes_on_disk": size, "seconds": round(elapsed, 1), "source_item_id": hag["source_item_id"]}
    print(f"{'canopy_hag':24s} {size:>9,} B on disk  {elapsed:5.1f} s  {hag['source_item_id']}")

    started = time.time()
    tcc = _capture("canopy_tcc", lambda: canopy_cover_data.get_tree_canopy_cover_for_boundary(REAL_BOUNDARY, dem))
    elapsed = time.time() - started
    size = _write_canopy("canopy_tcc", tcc)
    record["files"]["canopy_tcc"] = {"bytes_on_disk": size, "seconds": round(elapsed, 1), "year": tcc["year"]}
    print(f"{'canopy_tcc':24s} {size:>9,} B on disk  {elapsed:5.1f} s  year {tcc['year']}")

    started = time.time()
    forest = _capture("forest_type.tif", lambda: forest_type_data.get_forest_type_for_boundary(REAL_BOUNDARY))
    elapsed = time.time() - started
    with open(os.path.join(OUT, "forest_type.tif"), "wb") as handle:
        handle.write(forest["tiff"])
    window = dict(forest["window"])
    for key in ("bbox", "size", "resolution_meters"):
        window[key] = list(window[key])
    _write_json("forest_type.json", {"window": window})
    record["files"]["forest_type.tif"] = {"bytes_on_disk": len(forest["tiff"]), "seconds": round(elapsed, 1)}
    print(f"{'forest_type.tif':24s} {len(forest['tiff']):>9,} B on disk  {elapsed:5.1f} s")

    started = time.time()
    woodland = _capture("soil_woodland.json", lambda: soil_woodland.get_woodland_for_boundary(REAL_BOUNDARY))
    elapsed = time.time() - started
    size = _write_json("soil_woodland.json", woodland)
    record["files"]["soil_woodland.json"] = {"bytes_on_disk": size, "seconds": round(elapsed, 1),
                                             "rows": {k: len(v) for k, v in woodland.items()}}
    print(f"{'soil_woodland.json':24s} {size:>9,} B on disk  {elapsed:5.1f} s  rows {record['files']['soil_woodland.json']['rows']}")

    _write_json("capture.json", record)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
