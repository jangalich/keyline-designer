"""
soils_reference_fixture.py

THE REFERENCE PARCEL'S REAL SOILS AND GEOLOGY, OFFLINE.

Every source the Soils & geology section reads, captured once from the
live services for reference_fixture.REAL_BOUNDARY by
make_soils_fixtures.py (assets/reference/soils/), so a Soils test reads
the same responses a report would and the network stays refused.

    load(name)             -> one captured response, parsed from JSON
    real_parcel_data()     -> trees_reference_fixture's ParcelData (the
                              real DEM, canopy, SSURGO components,
                              geometries, farmland, Ksat, streams and
                              roads) with the REAL K-FACTOR rows in place
                              of the empty ones
    Harness()              -> the offline session harness on that ParcelData
    Session                -> roads_step_fixture.Session, re-exported
    report_data()          -> a ReportData carrying every Water, Access,
                              Trees and Soils layer from the fixtures
    soils_layers()         -> the two Soils blocks alone, parsed

THIS EXTENDS THE FIXTURE CHAIN, IT DOES NOT FORK IT. water -> access ->
trees -> soils: each adds its own sources to the one before, so the
report the Soils tests render is the whole report, not a Soils-only
stub. Water's fixture already puts the parcel's REAL SSURGO components,
map unit polygons, farmland classification and Ksat under the real DEM,
and this section reads all four off Layer 1 rather than fetching them
again -- so the honest fixture for this section is that chain plus what
it was missing.

WHAT IT WAS MISSING IS THE K FACTOR. ParcelData.erosion_factor was [] in
every fixture in the repository, because no section had ever printed a
K factor: Layer 1 fetches it for the design's erosion gate, and the
design's tests supply their own. The Soils section prints it, off Layer 1
rather than through a second query, so the fixture has to carry what
Layer 1 really returns -- seven rows, one dominant component per map
unit, captured live.
"""

import json
import os
from unittest.mock import patch as mock_patch

import bedrock_geology
import parcel_data
import soil_survey
import trees_reference_fixture as _trees
from parcel_data import ParcelData
from reference_fixture import REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIRECTORY = os.path.join(HERE, "assets", "reference", "soils")

Session = _trees.Session


def load(name: str):
    with open(os.path.join(FIXTURE_DIRECTORY, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def capture_record() -> dict:
    return load("capture.json")


def raw_soils_layers() -> dict:
    """The two Soils layers as their fetch functions return them --
    report_data.report_data_from_fixtures parses them the way
    fetch_report_data does."""
    return {"soil_survey_rows": load("soil_survey.json"), "bedrock_geology_raw": load("bedrock_geology.json")}


def soils_layers() -> dict:
    """The two Soils blocks, parsed."""
    raw = raw_soils_layers()
    return {
        "soil_survey": soil_survey.parse_survey(raw["soil_survey_rows"]),
        "bedrock_geology": bedrock_geology.parse_geology(raw["bedrock_geology_raw"]),
    }


def real_erosion_factor() -> list:
    return load("soil_erosion.json")


def real_parcel_data(_boundary=None, canopy: str = "hag") -> ParcelData:
    """trees_reference_fixture's ParcelData with the real K-factor rows."""
    base = _trees.real_parcel_data(_boundary, canopy)
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields["erosion_factor"] = real_erosion_factor()
    return ParcelData(**fields)


def report_data(**overrides):
    """trees_reference_fixture.report_data() plus the two Soils layers;
    `overrides` pass through, so soil_survey_rows=None or
    bedrock_geology_raw=None is a degraded case."""
    layers = dict(raw_soils_layers())
    layers.update(overrides)
    return _trees.report_data(**layers)


class Harness(_trees.Harness):
    """The Trees harness -- the real terrain, canopy, SSURGO rows, streams
    and roads -- with the parcel fetch re-pointed at the ParcelData that
    also carries the real K factors. The later patch wins while both are
    active."""

    def __enter__(self):
        super().__enter__()
        self.fetch_parcel_data = self._stack.enter_context(mock_patch.object(
            parcel_data, "fetch_parcel_data", side_effect=lambda boundary=None: real_parcel_data(boundary, self.canopy)
        ))
        return self


__all__ = ["Harness", "Session", "REAL_BOUNDARY", "load", "report_data", "soils_layers", "raw_soils_layers",
           "real_parcel_data", "real_erosion_factor", "capture_record"]
