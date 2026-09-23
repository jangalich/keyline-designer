"""
trees_reference_fixture.py

THE REFERENCE PARCEL'S REAL CANOPY, FOREST TYPE AND WOODLAND RATINGS, OFFLINE.

Every source the Trees & forestry section reads beyond the Access
fixtures, captured once from the live services for
reference_fixture.REAL_BOUNDARY by make_trees_fixtures.py
(assets/reference/trees/), so a Trees test reads the same responses a
report would and the network stays refused.

    load(name)                 -> one captured file
    real_canopy_hag()          -> ParcelData.canopy_height as Layer 1 holds it
                                  on the lidar path: the parcel's own HAG on
                                  the captured DEM grid
    real_canopy_tcc()          -> the NLCD TCC dict the fallback would have
                                  held: percent cover on the same grid
    raw_forest_type()          -> forest_type_data's raw {'window', 'tiff'}
    raw_soil_woodland()        -> the two SDA row sets soil_woodland parses
    real_parcel_data()         -> access_reference_fixture's ParcelData with
                                  the REAL canopy in place of the zero grid
    Harness(canopy=...)        -> the offline session harness on that
                                  ParcelData: "hag" (the default) or "tcc"
    Session                    -> roads_step_fixture.Session, re-exported
    report_data()              -> a ReportData carrying every Water and
                                  Access layer and the two Trees layers

WHY THE REAL CANOPY HERE. Every session fixture before this branch ran on
a canopy grid of zeros (terrain_reference_fixture._zero_canopy) or one
synthetic block, because Landform, Water and Access read no canopy. The
Trees section reads the canopy the design consumed, and its extent, its
heights and its agreement with the exclusion gate are only honest on the
parcel's own lidar. Under Harness(), session_manager.create_session()
runs the real terrain warm-up on the real DEM with the real canopy: the
exclusion result's canopy layer is the parcel's own, and its recorded
source is the dict's.

THE FALLBACK SESSION. Harness(canopy="tcc") builds the same session on
the captured TCC dict instead -- the session a parcel with no lidar
coverage gets -- so the fallback report is computed end to end on a
session whose exclusion gate really ran on percent cover.
"""

import json
import os
from unittest.mock import patch as mock_patch

import numpy as np

import access_reference_fixture as _access
import parcel_data
from canopy_height_data import CANOPY_SOURCE_LIDAR_HAG, CANOPY_SOURCE_NLCD_TCC
from parcel_data import ParcelData
from reference_fixture import REAL_BOUNDARY
from terrain_reference_fixture import load_dem

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIRECTORY = os.path.join(HERE, "assets", "reference", "trees")

Session = _access.Session


def load(name: str):
    path = os.path.join(FIXTURE_DIRECTORY, name)
    if name.endswith(".tif"):
        with open(path, "rb") as handle:
            return handle.read()
    if name.endswith(".npz"):
        with np.load(path) as stored:
            return stored["array"].astype(np.float32)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def capture_record() -> dict:
    return load("capture.json")


def _canopy(stem: str, expected_source: str) -> dict:
    """The captured dict rebuilt: the array off the npz, the grid keys off
    the DEM it was fetched on, every other key off the JSON."""
    dem = load_dem()
    meta = load(stem + ".json")
    assert meta.get("source") == expected_source, (stem, meta.get("source"))
    return dict(
        meta,
        array=load(stem + ".npz"),
        resolution_meters=dem["resolution_meters"],
        origin_x=dem["origin_x"],
        origin_y=dem["origin_y"],
        crs=dem["crs"],
    )


def real_canopy_hag() -> dict:
    return _canopy("canopy_hag", CANOPY_SOURCE_LIDAR_HAG)


def real_canopy_tcc() -> dict:
    return _canopy("canopy_tcc", CANOPY_SOURCE_NLCD_TCC)


def raw_forest_type() -> dict:
    meta = load("forest_type.json")
    window = dict(meta["window"])
    for key in ("bbox", "size", "resolution_meters"):
        window[key] = tuple(window[key])
    return {"window": window, "tiff": load("forest_type.tif")}


def raw_soil_woodland() -> dict:
    return load("soil_woodland.json")


def real_parcel_data(_boundary=None, canopy: str = "hag") -> ParcelData:
    """access_reference_fixture's ParcelData (the real DEM, the real
    SSURGO, NHD and road rows) with the real canopy."""
    base = _access.real_parcel_data(_boundary)
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields["canopy_height"] = real_canopy_hag() if canopy == "hag" else real_canopy_tcc()
    return ParcelData(**fields)


def real_power_wind() -> dict:
    """The POWER wind fixture every Climate test reads, parsed: the winter
    rose the windthrow caption names."""
    from power_wind_data import parse_power_csv

    with open(os.path.join(HERE, "power_wind_reference_fixture.csv"), encoding="utf-8") as handle:
        return parse_power_csv(handle.read())


def report_data(**overrides):
    """access_reference_fixture.report_data() plus the two Trees layers
    and the wind block (the Trees caption reads Climate's winter rose);
    `overrides` pass through, so forest_type_group=None,
    soil_woodland_rows=None or power_wind=None is a degraded case."""
    layers = {"forest_type_group": raw_forest_type(), "soil_woodland_rows": raw_soil_woodland(),
              "power_wind": real_power_wind()}
    layers.update(overrides)
    return _access.report_data(**layers)


class Harness(_access.Harness):
    """The Access harness, then the parcel fetch re-pointed at the real
    canopy. The later patch wins while both are active."""

    def __init__(self, canopy: str = "hag"):
        if canopy not in ("hag", "tcc"):
            raise ValueError(f"canopy must be 'hag' or 'tcc', got {canopy!r}")
        self.canopy = canopy

    def __enter__(self):
        super().__enter__()
        enter = self._stack.enter_context
        self.fetch_parcel_data = enter(mock_patch.object(
            parcel_data, "fetch_parcel_data", side_effect=lambda boundary=None: real_parcel_data(boundary, self.canopy)
        ))
        return self


__all__ = ["Harness", "Session", "REAL_BOUNDARY", "load", "report_data", "real_canopy_hag", "real_canopy_tcc",
           "raw_forest_type", "raw_soil_woodland", "real_parcel_data", "capture_record"]
