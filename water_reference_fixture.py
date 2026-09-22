"""
water_reference_fixture.py

THE REFERENCE PARCEL'S REAL WATER, OFFLINE.

Every source the Water & hydrology section reads, captured once from the
live services for reference_fixture.REAL_BOUNDARY by make_water_fixtures.py
(assets/reference/water/), so a Water test reads the same responses a
report would and the network stays refused.

    load(name)             -> one captured response, parsed from JSON
    real_parcel_data()     -> terrain_reference_fixture's ParcelData with the
                              REAL SSURGO rows and the REAL NHD rows in place
                              of the synthetic ones
    Harness()              -> the offline session harness on that ParcelData
    Session                -> roads_step_fixture.Session, re-exported
    report_data()          -> a ReportData carrying every Water layer from
                              the fixtures (report_data.report_data_from_fixtures)
    water_layers()         -> the six Water blocks alone, parsed

WHY REAL SOILS AND REAL STREAMS HERE. terrain_reference_fixture.py puts
the real 3DEP grid under the synthetic fixture's soil and stream rows,
because Landform reads only the terrain. The Water section reads the
hydric map units, the map-unit polygons and the NHD rows off the same
session, and its wet-ground comparison is only honest on the parcel's
own soils -- the water standards finding it demonstrates (the wettest
ground mapped non-hydric) is a fact about THIS parcel's survey.

Under Harness(), session_manager.create_session() runs the real terrain
warm-up on the real DEM with the real rows: the hydric union, the
floodplain union and the exclusion masks are the parcel's own.
"""

import gzip
import json
import os
from unittest.mock import patch as mock_patch

import parcel_data
import production_area
import roads_step_fixture as _roads
import terrain_reference_fixture as _terrain
from parcel_data import ParcelData
from reference_fixture import BOUNDARY_POLYGON_UTM, REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIRECTORY = os.path.join(HERE, "assets", "reference", "water")

Session = _roads.Session


def load(name: str):
    """One captured file, parsed: JSON (gzipped or not), or bytes for the
    NLCD TIFF."""
    path = os.path.join(FIXTURE_DIRECTORY, name)
    if name.endswith(".tif"):
        with open(path, "rb") as handle:
            return handle.read()
    if name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def capture_record() -> dict:
    return load("capture.json")


def raw_water_layers() -> dict:
    """The captured responses in the shape each fetch function returns --
    what report_data.report_data_from_fixtures() takes."""
    nlcd = load("nlcd.json")
    return {
        "nhd_points": load("nhd_points.json"),
        "nhdplus_hr": load("nhdplus_hr.json"),
        "nwi": load("nwi.json.gz"),
        "fema_nfhl": load("nfhl.json.gz"),
        "nlcd_landcover": {"window": nlcd["window"], "year": nlcd["year"], "tiff": load("nlcd.tif")},
        "soil_water_table_rows": load("soil_water_table.json"),
    }


def water_layers() -> dict:
    """The six blocks, parsed by their modules."""
    import nfhl_data
    import nlcd_landcover_data
    import nwi_data
    import nhdplus_data
    import soil_water_table

    raw = raw_water_layers()
    return {
        "nhd_points": raw["nhd_points"],
        "nhdplus_hr": nhdplus_data.parse_flowline_attributes(raw["nhdplus_hr"]),
        "nwi": nwi_data.parse_wetlands(raw["nwi"]),
        "fema_nfhl": nfhl_data.parse_flood_hazard(raw["fema_nfhl"]),
        "nlcd_landcover": nlcd_landcover_data.parse_land_cover(raw["nlcd_landcover"]),
        "soil_water_table": soil_water_table.parse_seasonal_water_table(raw["soil_water_table_rows"]),
    }


def report_data(**overrides):
    """A ReportData: Climate from the Daymet fixture (the way every report
    test builds it), every Water layer from these fixtures. `overrides`
    pass straight through to report_data_from_fixtures -- a Water layer
    set to None is absent, as if it degraded."""
    from daymet_data import parse_daymet_csv
    from report_data import report_data_from_fixtures

    with open(os.path.join(HERE, "daymet_reference_fixture.csv"), encoding="utf-8") as handle:
        daily = parse_daymet_csv(handle.read())
    layers = raw_water_layers()
    layers.update(overrides)
    return report_data_from_fixtures(REAL_BOUNDARY, daily, **layers)


def real_soil_components() -> list:
    return load("soil_components.json")


def real_soil_geometries() -> dict:
    return load("soil_geometries.json")


def real_parcel_data(_boundary=None) -> ParcelData:
    """terrain_reference_fixture's ParcelData (the real DEM, a bare canopy
    grid, the synthetic roads) with the real SSURGO rows and the real NHD
    rows."""
    base = _terrain.real_parcel_data(_boundary)
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields["soil_components"] = real_soil_components()
    fields["farmland_classification"] = load("soil_farmland.json")
    fields["saturated_hydraulic_conductivity"] = load("soil_ksat.json")
    fields["soil_geometries"] = real_soil_geometries()
    fields["water_features"] = load("water_features.json")
    fields["boundary_polygon_utm"] = BOUNDARY_POLYGON_UTM
    return ParcelData(**fields)


class Harness(_terrain.Harness):
    """The terrain harness, then the parcel fetch and the production
    module's soil mocks re-pointed at the real rows. The later patch wins
    while both are active."""

    def __enter__(self):
        super().__enter__()
        enter = self._stack.enter_context
        self.fetch_parcel_data = enter(mock_patch.object(parcel_data, "fetch_parcel_data", side_effect=real_parcel_data))
        self.soil_components = enter(
            mock_patch.object(production_area, "get_soil_data_for_polygon", return_value=real_soil_components())
        )
        self.soil_geometries = enter(
            mock_patch.object(production_area, "get_soil_geometries_for_polygon", return_value=real_soil_geometries())
        )
        return self


__all__ = ["Harness", "Session", "REAL_BOUNDARY", "load", "report_data", "water_layers", "raw_water_layers",
           "real_parcel_data", "capture_record"]
