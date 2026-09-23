"""
make_water_fixtures.py

CAPTURES THE WATER SECTION'S REFERENCE FIXTURES from the live services --
one real response per source for the reference parcel
(reference_fixture.REAL_BOUNDARY), written under assets/reference/water/
so every Water test runs from them with the network refused
(water_reference_fixture.py loads them).

    python3 make_water_fixtures.py

What it writes, and the fetch each one is the verbatim answer to:

    water_features.json      hydrology_data.get_water_features_for_boundary()
                             -- the NHD flowline and waterbody rows Layer 1
                             holds on ParcelData
    nhd_points.json          hydrology_data.get_nhd_points_for_boundary()
    nhdplus_hr.json          nhdplus_data.get_flowline_attributes_for_boundary()
    nwi.json.gz              nwi_data.get_wetlands_for_boundary() (three
                             stages and the mapping project)
    nfhl.json.gz             nfhl_data.get_flood_hazard_for_boundary()
    nlcd.json + nlcd.tif     nlcd_landcover_data.get_land_cover_for_boundary()
                             (the window and year beside the TIFF bytes)
    soil_water_table.json    soil_water_table.get_seasonal_water_table_for_boundary()
    soil_components.json     soil_data.get_soil_data_for_polygon()      } the real
    soil_geometries.json     soil_data.get_soil_geometries_for_polygon()} SSURGO rows
    soil_ksat.json           soil_data.get_saturated_hydraulic_conductivity_for_polygon()
    soil_farmland.json       soil_data.get_farmland_classification_for_polygon()
    capture.json             when, and how big each response was

The two gzipped files are the two that are large for a reason the module
docstrings explain (a county-wide Zone X polygon; a riverine wetland
network fetched coarsely). Run from a machine with network; the sandbox
the tests run in has none.
"""

import gzip
import json
import os
import sys
import time
from datetime import date

import hydrology_data
import nfhl_data
import nhdplus_data
import nlcd_landcover_data
import nwi_data
import soil_data
import soil_water_table
from reference_fixture import REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "reference", "water")


def _write_json(name, payload, compress=False):
    path = os.path.join(OUT, name)
    text = json.dumps(payload, separators=(",", ":"))
    if compress:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    return os.path.getsize(path), len(text.encode("utf-8"))


def _capture(name, fetch, tries: int = 4):
    """The module's own retry loop runs inside `fetch`; this outer loop
    is for the capture only -- a host that resets the connection three
    times in a row is a fact about today, not about the fixture."""
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
    wkt = soil_data.coordinates_to_wkt_polygon(REAL_BOUNDARY)
    record = {"boundary": [list(p) for p in REAL_BOUNDARY], "captured_on": date.today().isoformat(), "files": {}}
    steps = [
        ("water_features.json", lambda: hydrology_data.get_water_features_for_boundary(REAL_BOUNDARY), False),
        ("nhd_points.json", lambda: hydrology_data.get_nhd_points_for_boundary(REAL_BOUNDARY), False),
        ("nhdplus_hr.json", lambda: nhdplus_data.get_flowline_attributes_for_boundary(REAL_BOUNDARY), False),
        ("nwi.json.gz", lambda: nwi_data.get_wetlands_for_boundary(REAL_BOUNDARY), True),
        ("nfhl.json.gz", lambda: nfhl_data.get_flood_hazard_for_boundary(REAL_BOUNDARY), True),
        ("soil_water_table.json", lambda: soil_water_table.get_seasonal_water_table_for_boundary(REAL_BOUNDARY), False),
        ("soil_components.json", lambda: soil_data.get_soil_data_for_polygon(wkt), False),
        ("soil_geometries.json", lambda: soil_data.get_soil_geometries_for_polygon(wkt), False),
        ("soil_ksat.json", lambda: soil_data.get_saturated_hydraulic_conductivity_for_polygon(wkt), False),
        ("soil_farmland.json", lambda: soil_data.get_farmland_classification_for_polygon(wkt), False),
    ]
    for name, fetch, compress in steps:
        started = time.time()
        payload = _capture(name, fetch)
        elapsed = time.time() - started
        on_disk, raw = _write_json(name, payload, compress)
        record["files"][name] = {"bytes_on_disk": on_disk, "bytes_raw": raw, "seconds": round(elapsed, 1)}
        print(f"{name:24s} {raw:>10,} B raw  {on_disk:>9,} B on disk  {elapsed:5.1f} s")
    started = time.time()
    nlcd = _capture("nlcd.tif", lambda: nlcd_landcover_data.get_land_cover_for_boundary(REAL_BOUNDARY))
    elapsed = time.time() - started
    with open(os.path.join(OUT, "nlcd.tif"), "wb") as handle:
        handle.write(nlcd["tiff"])
    window = dict(nlcd["window"])
    window["bbox"] = list(window["bbox"])
    window["size"] = list(window["size"])
    window["resolution_meters"] = list(window["resolution_meters"])
    _write_json("nlcd.json", {"window": window, "year": nlcd["year"]})
    record["files"]["nlcd.tif"] = {"bytes_on_disk": len(nlcd["tiff"]), "bytes_raw": len(nlcd["tiff"]), "seconds": round(elapsed, 1)}
    print(f"{'nlcd.tif':24s} {len(nlcd['tiff']):>10,} B raw  {len(nlcd['tiff']):>9,} B on disk  {elapsed:5.1f} s")
    _write_json("capture.json", record)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
