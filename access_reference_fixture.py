"""
access_reference_fixture.py

THE REFERENCE PARCEL'S REAL ROADS AND SOIL ROAD RATINGS, OFFLINE.

Every source the Access section reads beyond the Water fixtures, captured
once from the live services for reference_fixture.REAL_BOUNDARY by
make_access_fixtures.py (assets/reference/access/), so an Access test
reads the same responses a report would and the network stays refused.

    load(name)                 -> one captured response, parsed from JSON
    real_farm_roads()          -> ParcelData.farm_roads' rows, the parcel's own
    raw_soil_road_ratings()    -> the SDA rows soil_road_ratings parses
    real_parcel_data()         -> water_reference_fixture's ParcelData with the
                                  REAL road rows in place of the synthetic one
    Harness()                  -> the offline session harness on that ParcelData
    Session                    -> roads_step_fixture.Session, re-exported
    report_data()              -> a ReportData carrying every Water layer and
                                  the soil road ratings from the fixtures

WHY REAL ROADS HERE. Every step test runs on a synthetic "Fixture Rd"
drawn straight across the parcel, because the roads step's tests want a
road where they put it. The Access section reads the parcel's own mapped
roads off the same session, and its frontage, its track and its
agreement with the exclusion mask are only honest on the rows the
service actually returns: five local-road segments, one of which runs
along the north edge and enters the parcel.

Under Harness(), session_manager.create_session() runs the real terrain
warm-up on the real DEM with the real rows: the existing-roads union and
the exclusion masks are the parcel's own.
"""

import json
import os
from unittest.mock import patch as mock_patch

import farm_roads_data
import parcel_data
import water_reference_fixture as _water
from parcel_data import ParcelData
from reference_fixture import REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIRECTORY = os.path.join(HERE, "assets", "reference", "access")

Session = _water.Session


def load(name: str):
    with open(os.path.join(FIXTURE_DIRECTORY, name), encoding="utf-8") as handle:
        return json.load(handle)


def capture_record() -> dict:
    return load("capture.json")


def real_farm_roads() -> list:
    return load("farm_roads.json")


def raw_soil_road_ratings() -> list:
    return load("soil_road_ratings.json")


def real_parcel_data(_boundary=None) -> ParcelData:
    """water_reference_fixture's ParcelData (the real DEM, the real SSURGO
    and NHD rows) with the real road rows."""
    base = _water.real_parcel_data(_boundary)
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields["farm_roads"] = real_farm_roads()
    return ParcelData(**fields)


def report_data(**overrides):
    """water_reference_fixture.report_data() plus the soil road ratings;
    `overrides` pass through, so soil_road_ratings_rows=None is the
    degraded case."""
    layers = {"soil_road_ratings_rows": raw_soil_road_ratings()}
    layers.update(overrides)
    return _water.report_data(**layers)


class Harness(_water.Harness):
    """The Water harness, then the parcel fetch and the roads module's
    self-fetch re-pointed at the real rows. The later patch wins while
    both are active."""

    def __enter__(self):
        super().__enter__()
        enter = self._stack.enter_context
        self.fetch_parcel_data = enter(mock_patch.object(parcel_data, "fetch_parcel_data", side_effect=real_parcel_data))
        self.roads_refetch = enter(
            mock_patch.object(farm_roads_data, "get_farm_roads_for_boundary", return_value=real_farm_roads())
        )
        return self


__all__ = ["Harness", "Session", "REAL_BOUNDARY", "load", "report_data", "real_farm_roads", "raw_soil_road_ratings",
           "real_parcel_data", "capture_record"]
