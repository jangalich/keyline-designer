#!/usr/bin/env python3
"""
make_terrain_reference_fixture.py

CAPTURES THE REFERENCE PARCEL'S REAL TERRAIN FROM 3DEP, the one place the
elevation fetch runs against the network outside a session. Run by hand
when the fixture must be refreshed (a new 3DEP resample, a changed
boundary, a change to the valley or keypoint derivation whose effect on
the real parcel must be pinned again); the tests never do.

    python3 make_terrain_reference_fixture.py

What it writes:

    terrain_reference_fixture.npz    the 5 m DEM over the buffered
                                     boundary, float32, with its origin
                                     and resolution (~33 KB compressed)
    terrain_reference_fixture.json   the DEM's CRS and retrieval date, and
                                     the derivations the code of the day
                                     produced on it: every primary valley's
                                     branches as (row, col) cells with its
                                     contributing area, and every keypoint
                                     with the fields the detector sets
                                     (~17 KB)

The derivations are stored so a test can hold the CURRENT code to them:
test_landform_derivations.py recomputes valleys and keypoints from the
stored DEM and asserts they match cell for cell. A change to either
module that moves a keypoint on the real parcel therefore fails a test
and is re-captured here on purpose, with the reason in the commit.
"""

import json
import sys
from datetime import date

import numpy as np

import dem_data
import keypoint_detection
import valley_delineation
from reference_fixture import BOUNDARY_POLYGON_UTM, REAL_BOUNDARY

NPZ_PATH = "terrain_reference_fixture.npz"
JSON_PATH = "terrain_reference_fixture.json"

KEYPOINT_FIELDS = (
    "id", "valley_id", "rowcol", "elevation_m", "contributing_acres", "slope_above_pct",
    "slope_below_pct", "slope_drop_pct", "stem_length_cells", "position_along_stem",
    "on_parcel", "distance_outside_boundary_m",
)


def derivations(dem: dict) -> dict:
    valleys = valley_delineation.delineate_valleys(dem)
    diagnostics = {}
    keypoints = keypoint_detection.detect_keypoints(
        dem, BOUNDARY_POLYGON_UTM, valleys=valleys, diagnostics=diagnostics
    )
    return {
        "valleys": [
            {
                "id": v["id"],
                "max_contributing_area_acres": v["max_contributing_area_acres"],
                "branches_rowcol": [[list(cell) for cell in branch] for branch in v["branches_rowcol"]],
            }
            for v in valleys
        ],
        "keypoints": [{field: (list(kp[field]) if field == "rowcol" else kp[field]) for field in KEYPOINT_FIELDS}
                      for kp in keypoints],
        "keypoint_diagnostics": diagnostics,
    }


def main(argv) -> int:
    dem = dem_data.get_dem_for_boundary(REAL_BOUNDARY)
    array = dem["array"]
    print(f"3DEP: {array.shape[0]}x{array.shape[1]} at {dem['resolution_meters']}, {dem['crs']}, "
          f"{np.nanmin(array):.1f}-{np.nanmax(array):.1f} m, nodata cells {int(np.isnan(array).sum())}")
    np.savez_compressed(
        NPZ_PATH,
        array=array.astype(np.float32),
        origin=np.array([dem["origin_x"], dem["origin_y"]], dtype=np.float64),
        resolution=np.array(dem["resolution_meters"], dtype=np.float64),
    )
    derived = derivations(dem)
    record = {
        "boundary": [list(point) for point in REAL_BOUNDARY],
        "crs": dem["crs"],
        "retrieved_on": date.today().isoformat(),
        "source": "USGS 3DEP via dem_data.get_dem_for_boundary(), 5 m, 100 m buffer",
        **derived,
    }
    with open(JSON_PATH, "w", encoding="utf-8") as handle:
        json.dump(record, handle, separators=(",", ":"))
    import os
    print(f"{NPZ_PATH}: {os.path.getsize(NPZ_PATH):,} bytes; {JSON_PATH}: {os.path.getsize(JSON_PATH):,} bytes")
    print(f"valleys {len(derived['valleys'])}: " + ", ".join(
        f"{v['id']} ({v['max_contributing_area_acres']} ac)" for v in derived["valleys"]))
    for kp in derived["keypoints"]:
        where = "on parcel" if kp["on_parcel"] else f"{kp['distance_outside_boundary_m']} m outside"
        print(f"keypoint {kp['id']}: valley {kp['valley_id']} cell {kp['rowcol']} {kp['elevation_m']} m, "
              f"{kp['slope_above_pct']}% -> {kp['slope_below_pct']}%, {where}")
    print(f"detector: {derived['keypoint_diagnostics']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
