"""
terrain_reference_fixture.py

THE REFERENCE PARCEL'S REAL TERRAIN, OFFLINE.

The 5 m 3DEP grid over reference_fixture.REAL_BOUNDARY, captured once by
make_terrain_reference_fixture.py (terrain_reference_fixture.npz) with the
valleys and keypoints the derivation code of that day produced on it
(terrain_reference_fixture.json). Every step test builds its session on a
synthetic DEM -- parallel valleys, a knee, no keypoint on the parcel --
because its terrain is part of what it asserts; the Landform section has
to be read against ground that is nothing like that, so this module
exists.

    load_dem()            -> the DEM dict, the shape dem_data returns
    load_derivations()    -> the captured valleys and keypoints
    Harness()             -> roads_step_fixture's offline session harness
                             with THIS dem in ParcelData
    Session               -> roads_step_fixture.Session, re-exported

Under Harness(), session_manager.create_session() runs the real terrain
warm-up -- delineate_valleys(), detect_keypoints(), the exclusion grid --
on the real DEM with every network call mocked, so a test reads the
section off a session exactly as the report job does.
"""

import json
import os
from unittest.mock import patch as mock_patch

import numpy as np

import parcel_data
import roads_step_fixture as _roads
from parcel_data import ParcelData
from reference_fixture import BOUNDARY_POLYGON_UTM

HERE = os.path.dirname(os.path.abspath(__file__))
NPZ_PATH = os.path.join(HERE, "terrain_reference_fixture.npz")
JSON_PATH = os.path.join(HERE, "terrain_reference_fixture.json")

Session = _roads.Session


def load_record() -> dict:
    with open(JSON_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def load_dem() -> dict:
    with np.load(NPZ_PATH) as stored:
        array = stored["array"].astype(np.float32)
        origin_x, origin_y = (float(v) for v in stored["origin"])
        resolution = tuple(float(v) for v in stored["resolution"])
    return {
        "array": array,
        "resolution_meters": resolution,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "crs": load_record()["crs"],
    }


def load_derivations() -> dict:
    """The captured valleys and keypoints, with (row, col) cells as tuples
    so they compare equal to what the modules return."""
    record = load_record()
    return {
        "retrieved_on": record["retrieved_on"],
        "valleys": [
            dict(v, branches_rowcol=[[tuple(cell) for cell in branch] for branch in v["branches_rowcol"]])
            for v in record["valleys"]
        ],
        "keypoints": [dict(kp, rowcol=tuple(kp["rowcol"])) for kp in record["keypoints"]],
        "keypoint_diagnostics": record["keypoint_diagnostics"],
    }


def _zero_canopy(dem: dict) -> dict:
    return {
        "array": np.zeros(dem["array"].shape, dtype=np.float32),
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "fixture-hag",
    }


def real_parcel_data(_boundary=None) -> ParcelData:
    """roads_step_fixture's ParcelData with the real DEM in place of the
    synthetic one and a bare canopy grid of the same shape; every other
    row (soil, roads, water) is the synthetic fixture's own."""
    synthetic = _roads._build_parcel_data(_boundary)
    dem = load_dem()
    fields = {name: getattr(synthetic, name) for name in synthetic.__dataclass_fields__}
    fields["dem"] = dem
    fields["boundary_polygon_utm"] = BOUNDARY_POLYGON_UTM
    fields["canopy_height"] = _zero_canopy(dem)
    return ParcelData(**fields)


class Harness(_roads.Harness):
    """The roads harness, then the parcel fetch re-pointed at the real DEM.
    The later patch wins while both are active; everything else the
    harness mocks or counts is untouched."""

    def __enter__(self):
        super().__enter__()
        self.fetch_parcel_data = self._stack.enter_context(
            mock_patch.object(parcel_data, "fetch_parcel_data", side_effect=real_parcel_data)
        )
        return self
