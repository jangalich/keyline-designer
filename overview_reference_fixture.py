"""
overview_reference_fixture.py

THE REFERENCE PARCEL'S SITE OVERVIEW SOURCES, OFFLINE -- and with them the
whole report's ReportData.

Every report-layer source the Site overview reads, captured once from the
live services for reference_fixture.REAL_BOUNDARY by
make_overview_fixtures.py (assets/reference/overview/), so an overview test
reads the same responses a report would and the network stays refused.

    load(name)             -> one captured JSON response
    context_dem()          -> the 300-cell context DEM dict
    raw_overview_layers()  -> the six overview layers as their fetches return them
    report_data()          -> a ReportData carrying EVERY report layer: the
                              soils chain's Water, Access, Trees and Soils
                              layers, the design storms (Atlas 14) and the
                              layout map's NAIP window, and the overview's six
    Harness, Session       -> soils_reference_fixture's, re-exported
    capture_record()       -> when and what was captured

THE CHAIN, COMPLETED. water -> access -> trees -> soils each added its own
sources; the design storms and the NAIP window had fixtures of their own
that no chain carried, so a report rendered off the chain printed no
design storms. This is the last link: its report_data() is the one a
whole-report render takes, and retrieved_on is the capture date, so the
vintage table has a retrieval date to print for every report-layer source.
"""

import json
import os
from datetime import date

import numpy as np

import naip_reference_fixture
import soils_reference_fixture as _soils
from reference_fixture import REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIRECTORY = os.path.join(HERE, "assets", "reference", "overview")
ATLAS14_FIXTURE = os.path.join(HERE, "atlas14_reference_fixture.csv")

Session = _soils.Session
Harness = _soils.Harness


def load(name: str):
    with open(os.path.join(FIXTURE_DIRECTORY, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def capture_record() -> dict:
    return load("capture.json")


def captured_on() -> date:
    return date.fromisoformat(capture_record()["captured_on"])


def context_dem() -> dict:
    meta = load("context_dem.json")
    with np.load(os.path.join(FIXTURE_DIRECTORY, "context_dem.npz")) as archive:
        array = archive["array"].astype("float32")
    return {"array": array, "resolution_meters": tuple(meta["resolution_meters"]), "origin_x": meta["origin_x"],
            "origin_y": meta["origin_y"], "crs": meta["crs"]}


def raw_overview_layers() -> dict:
    """The six overview layers, keyed as report_data_from_fixtures takes them."""
    return {
        "context_dem": context_dem(),
        "context_water": load("context_water.json"),
        "context_roads": load("context_roads.json"),
        "county_state_raw": load("county_state.json"),
        "structures_raw": load("structures.json"),
        "transmission_lines_raw": load("transmission_lines.json"),
    }


def atlas14() -> dict:
    from atlas14_data import parse_atlas14_csv

    with open(ATLAS14_FIXTURE, encoding="utf-8") as handle:
        return parse_atlas14_csv(handle.read())


def report_data(**overrides):
    """Every report layer from the fixtures; `overrides` pass through, so
    context_dem=None or structures_raw=None is a degraded case."""
    layers = {"atlas14": atlas14(), "naip_imagery_raw": naip_reference_fixture.raw_naip(),
              "retrieved_on": captured_on()}
    layers.update(raw_overview_layers())
    layers.update(overrides)
    return _soils.report_data(**layers)


__all__ = ["Harness", "Session", "REAL_BOUNDARY", "load", "context_dem", "raw_overview_layers", "report_data",
           "atlas14", "capture_record", "captured_on"]
