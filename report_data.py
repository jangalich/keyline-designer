"""
report_data.py

THE REPORT-TIME DATA LAYER: every source the SITE DATA REPORT reads that no
design step consumes, fetched when a report is asked for, cached by
boundary, and governed by its own failure table.

    fetch_report_data(boundary)   -> ReportData, or raises
    REPORT_FETCH_LAYERS           -> {layer: REQUIRED | DEGRADABLE}
    default_report_fetch_cache()  -> the process-wide FetchCache for it
    report_data_from_fixtures(..) -> a ReportData with no network (tests,
                                     diagnostics)

WHY A SEPARATE LAYER (site-data-report-proposal.md, decision D1, option 3
with 4c). parcel_data.fetch_parcel_data() -- Layer 1 -- hard-fails a
session creation on any missing layer because DESIGN STEPS depend on that
data, and its module docstring names exactly two departures from that
rule and says there are no others. Nothing a design step reads is here.
A site report is a terminal, paid, one-off action over a finished design;
its sources are fetched at report time, on the report job, and a source
that fails fails the REPORT, never the session. Layer 1's contract and
docstring are untouched by this module existing.

ONE TABLE SAYS WHAT A FAILURE MEANS. REPORT_FETCH_LAYERS declares each
source REQUIRED or DEGRADABLE:

  REQUIRED    the report is not a report without it. The fetch raises
              ReportDataIncompleteError carrying the same (layer, label,
              reason) triple parcel_data.ParcelDataIncompleteError and
              production_zone_payload.LayerFetchError carry, so the job's
              failure payload can take the failed_layer shape the frontend
              already renders. Nothing is cached.
  DEGRADABLE  the report renders without it and the affected section
              carries a visible "unavailable" statement. The layer's field
              is None and `unavailable[layer]` records the label and the
              reason, so a section cannot mistake "not fetched" for
              "fetched and empty".

THE TABLE AFTER BRANCH 7 (Climate: additions):

  daymet_daily        REQUIRED    a site report without climate is not a
                                  site report. THE ONE DAYMET CALL: the
                                  precipitation correction's five
                                  station ratios are bundled with the
                                  normals (precipitation_normals.py,
                                  phase 2), not fetched.
  atlas14             DEGRADABLE  design-storm depths. A hole a consultant
                                  can fill from the public server in a
                                  minute, unlike a missing climate; and a
                                  point outside every Atlas 14 volume
                                  (Washington, Oregon, Idaho, Montana,
                                  Wyoming) is a real, common case that
                                  must not fail a report --
                                  no_data_for_parcel, rendered as "not
                                  covered".
  power_wind          DEGRADABLE  regional wind; context, not a decision
                                  input on its own.

THE TABLE AFTER BRANCH 9 (Water & hydrology: six more, all DEGRADABLE --
they are context beside a Layer 1 the section always has (the NHD rows,
the SSURGO rows, the DEM), and a missing wetland layer must not sink a
paid report; each absent layer leaves a visible statement in its place):

  nhd_points          DEGRADABLE  NHD's Point layer: mapped springs and
                                  seeps (hydrology_data). "None mapped" is
                                  the expected answer and a real one.
  nhdplus_hr          DEGRADABLE  NHDPlus HR stream order and the reach's
                                  total drainage area, joined to the NHD
                                  rows by permanent_identifier
                                  (nhdplus_data).
  nwi                 DEGRADABLE  USFWS National Wetlands Inventory, two-
                                  stage (nwi_data). An EMPTY answer is
                                  "no mapped wetland", never a
                                  degradation.
  fema_nfhl           DEGRADABLE  FEMA flood hazard zones and whether a
                                  digital flood map exists at all
                                  (nfhl_data). "No digital flood map" is
                                  a no-data answer with its own statement,
                                  distinct from an outage.
  nlcd_landcover      DEGRADABLE  Annual NLCD land cover on the DEM grid,
                                  the year pinned (nlcd_landcover_data).
  soil_water_table    DEGRADABLE  SSURGO's seasonal water table, flooding
                                  and ponding by month, one report-time
                                  query on the same service Layer 1
                                  already reached (soil_water_table).

THE TABLE AFTER BRANCH 10 (Access: one more, DEGRADABLE -- the section
stands on Layer 1's road rows, which it always has; a missing rating
leaves a visible statement where the soil table would be):

  soil_road_ratings   DEGRADABLE  SSURGO's road-construction
                                  interpretations (local roads and
                                  streets, the unpaved variant, roadfill)
                                  with their limiting features, one
                                  report-time query (soil_road_ratings).

THE TABLE AFTER BRANCH 11 (Trees & forestry: two more, both DEGRADABLE --
the section stands on Layer 1's canopy, which it always has; each absent
layer leaves a visible statement where its table would be):

  forest_type_group   DEGRADABLE  USFS FIA BIGMAP forest type group on
                                  the DEM grid, 30 m (forest_type_data).
                                  A modelled product beside a measured
                                  canopy: context.
  soil_woodland       DEGRADABLE  SSURGO's woodland productivity by
                                  species and its four woodland
                                  management interpretations, two
                                  report-time queries on the same service
                                  Layer 1 already reached (soil_woodland).

THE TABLE AFTER BRANCH 12 (Soils & geology: two more, both DEGRADABLE --
the section stands on Layer 1's map unit polygons, components, farmland
classification, K factor and Ksat, which it always has; each absent layer
leaves a visible statement where its figures would be):

  soil_survey         DEGRADABLE  SSURGO's core survey reading: the map
                                  unit symbol, the surface horizon's
                                  texture and properties, depth to
                                  bedrock, land capability and the T
                                  factor, one report-time query on the
                                  same service Layer 1 already reached
                                  (soil_survey). The section's map unit
                                  POLYGONS come from Layer 1, so a
                                  degraded render still draws them --
                                  outlined and unlabelled, the symbols
                                  being the one thing only this layer
                                  carries.
  bedrock_geology     DEGRADABLE  USGS State Geologic Map Compilation:
                                  the geologic unit under the parcel,
                                  named and dated (bedrock_geology). One
                                  line of the report. A parcel outside
                                  the conterminous compilation is a real
                                  no-data answer, not an outage.

THE TABLE AFTER BRANCH 13 (the design: one more, DEGRADABLE -- the map
draws on white without it):

  naip_imagery        DEGRADABLE  USDA NAIP orthoimagery from Planetary
                                  Computer for the layout map's extent,
                                  the latest year covering the parcel,
                                  with each item's acquisition date --
                                  printed beneath the map (naip_imagery).

THE TABLE AFTER BRANCH 17 (Site overview: six more, all DEGRADABLE --
the overview stands on the boundary and the session's own DEM, and each
absent layer leaves its line or its map layer out with a statement):

  context_dem         DEGRADABLE  3DEP over the parcel's extent plus one
                                  mile, on dem_data's 300-cell grid: the
                                  context map's contours and the
                                  landscape position (context_map_data).
  context_water       DEGRADABLE  NHD flowlines and waterbodies over the
                                  same extent: the context map's streams.
  context_roads       DEGRADABLE  National Map transportation over the
                                  same extent: the context map's roads.
  county_state        DEGRADABLE  Census geocoder, reverse, at the
                                  centroid: the county and State
                                  (census_geography). The cover names the
                                  county when this answered.
  structures          DEGRADABLE  FEMA USA Structures, CC BY 4.0: the
                                  buildings on the parcel
                                  (structures_data).
  transmission_lines  DEGRADABLE  HIFLD transmission lines, Esri's
                                  archived copy: the nearest line within
                                  five miles (transmission_lines).

THE PHYSIOGRAPHIC PROVINCE AND THE LIVESTOCK PREDATOR LINE ARE BUNDLED
(class E: physiography.py, livestock_predators.py) and, like severe
weather, have no row in the table; the overview reads them directly.

RETRIEVED ON. ReportData.retrieved_on is the date this fetch ran -- the
retrieval date of every report-layer source, which the back matter's
vintage table prints as data. The Layer 1 sources' date is the Design
Document's created_at, which every section already reads.

THE WINDOW-BASED LAYERS take the boundary alone: nwi_data, nfhl_data,
nlcd_landcover_data and forest_type_data derive the parcel's UTM window from the boundary with
dem_data.dem_window_bounds(), the function the DEM fetch itself uses, so
the NLCD grid is cell-for-cell the DEM's without the DEM being here.

SEVERE WEATHER, THE NORMALS AND THE STATION RATIOS ARE BUNDLED, NOT
FETCHED (class E: spc_reports.py, precipitation_normals.py). They read
files in the repository and carry no fetch risk, so they have no row in
the table; a bundle that cannot be read is a broken deployment and
raises. The precipitation correction and the heavy-rain normals are
derived from the five nearest bundled stations for the centroid.

THE SAME INSTRUMENTATION AS LAYER 1. Every fetch sits in a
run_diagnostics.time_layer() block naming its REPORT_FETCH_LAYERS entry,
and run_diagnostics._fetch_hook_sites() cross-checks this module's
compiled fetch_report_data() against REPORT_FETCH_LAYERS exactly as it
checks parcel_data -- a layer added without a timer shows up in
self_check() as "2 of 3". time_layer() is a no-op until a probe is opened
on the thread (run_diagnostics.begin_fetch); opening one on the report job
is the job's wiring, not this module's.

THE CACHE. default_report_fetch_cache() is a session_cache.FetchCache
built over fetch_report_data (lazily, on first use -- session_cache pulls
in the whole Layer 1 import graph and this module must stay importable by
the diagnostics self-check on its own): keyed by the normalised boundary,
LRU-capped, per-key in-flight lock, failures never cached. A second report
on the same land pays no second fetch. It holds a ReportData, which is a
value: every block is derived once here, so no consumer recomputes it.

THE POINT. Daymet is 1 km gridded data, so one point represents the
parcel: the boundary polygon's centroid in WGS84 (shapely over lon/lat --
at parcel scale the difference from a projected centroid is metres, and
the Daymet cell is a kilometre). ReportData.centroid records it so the
footer and the cover can print the same point the fetch used. Atlas 14,
POWER and the severe-weather radius all take the same point.

ONE PERIOD ACROSS THE SECTION. POWER is asked for the calendar years the
Daymet block actually used (climate['years']), so the wind roses and the
monthly table describe the same thirty years.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from xml.etree import ElementTree

import requests
from shapely.geometry import Polygon

import precipitation_normals
import run_diagnostics
import spc_reports
import forest_type_data
import hydrology_data
import nfhl_data
import nhdplus_data
import nlcd_landcover_data
import nwi_data
import bedrock_geology
import soil_road_ratings
import soil_survey
import soil_water_table
import soil_woodland
import naip_imagery
import census_geography
import context_map_data
import structures_data
import transmission_lines
from atlas14_data import Atlas14IncompleteError, design_storms, get_atlas14_for_point
from climate_report import derive_climate
from daymet_data import DaymetIncompleteError, get_daymet_daily_for_point
from power_wind_data import PowerIncompleteError, derive_wind, get_power_wind_for_point

REQUIRED = "required"
DEGRADABLE = "degradable"

# THE TABLE. Layer name -> policy, in fetch order. Names are ReportData
# field names, so a diagnostic row points at the field the fetch filled.
# Cross-checked against the compiled fetch_report_data() by
# run_diagnostics._fetch_hook_sites().
REPORT_FETCH_LAYERS = {
    "daymet_daily": REQUIRED,
    "atlas14": DEGRADABLE,
    "power_wind": DEGRADABLE,
    "nhd_points": DEGRADABLE,
    "nhdplus_hr": DEGRADABLE,
    "nwi": DEGRADABLE,
    "fema_nfhl": DEGRADABLE,
    "nlcd_landcover": DEGRADABLE,
    "soil_water_table": DEGRADABLE,
    "soil_road_ratings": DEGRADABLE,
    "forest_type_group": DEGRADABLE,
    "soil_woodland": DEGRADABLE,
    "soil_survey": DEGRADABLE,
    "bedrock_geology": DEGRADABLE,
    "naip_imagery": DEGRADABLE,
    "context_dem": DEGRADABLE,
    "context_water": DEGRADABLE,
    "context_roads": DEGRADABLE,
    "county_state": DEGRADABLE,
    "structures": DEGRADABLE,
    "transmission_lines": DEGRADABLE,
}

# The (type, label) pair each layer's failure reports as -- the same split
# every other layer error carries: a stable type to branch on, display
# prose to print.
LAYER_CLIMATE = ("climate", "climate records")
LAYER_ATLAS14 = ("design_storms", "design storm depths")
LAYER_POWER_WIND = ("wind", "wind records")
LAYER_NHD_POINTS = ("springs", "mapped springs and seeps")
LAYER_NHDPLUS_HR = ("stream_order", "stream order")
LAYER_NWI = ("wetlands", "mapped wetlands")
LAYER_NFHL = ("flood_hazard", "flood hazard zones")
LAYER_NLCD = ("land_cover", "land cover")
LAYER_SOIL_WATER_TABLE = ("soil_water_table", "seasonal water table")
LAYER_SOIL_ROAD_RATINGS = ("soil_road_ratings", "soil road-construction ratings")
LAYER_FOREST_TYPE_GROUP = ("forest_type_group", "forest type group")
LAYER_SOIL_WOODLAND = ("soil_woodland", "soil woodland ratings")
LAYER_SOIL_SURVEY = ("soil_survey", "soil survey properties")
LAYER_BEDROCK_GEOLOGY = ("bedrock_geology", "bedrock geology")
LAYER_NAIP_IMAGERY = ("aerial_imagery", "aerial imagery")
LAYER_CONTEXT_DEM = ("context_elevation", "surrounding elevation")
LAYER_CONTEXT_WATER = ("context_streams", "surrounding streams")
LAYER_CONTEXT_ROADS = ("context_roads", "surrounding roads")
LAYER_COUNTY_STATE = ("county_state", "county and state")
LAYER_STRUCTURES = ("structures", "building footprints")
LAYER_TRANSMISSION = ("transmission_lines", "transmission lines")

# What a Water layer's fetch or parse can raise besides a RequestException:
# a TIFF rasterio cannot open (OSError), a response whose shape the parser
# rejects (ValueError, KeyError, TypeError). All are "the source did not
# answer usefully" and degrade the same way.
_WATER_FETCH_ERRORS = (requests.exceptions.RequestException, OSError, ValueError, KeyError, TypeError)

# The geology fetch adds two: an XML body ElementTree cannot read, and
# the compilation answering that it maps nothing here. The first is "the
# source did not answer usefully"; the second is a no-data answer, and
# _failure() tells them apart through _NO_DATA_ERRORS below.
_GEOLOGY_FETCH_ERRORS = _WATER_FETCH_ERRORS + (ElementTree.ParseError, bedrock_geology.GeologyIncompleteError)

# The exception kinds that mean "the source answered without the data",
# as opposed to a RequestException, "the source did not answer".
_NO_DATA_ERRORS = (DaymetIncompleteError, Atlas14IncompleteError, PowerIncompleteError,
                   bedrock_geology.GeologyIncompleteError, naip_imagery.NaipIncompleteError,
                   census_geography.CensusGeographyIncompleteError)

# The overview's fetches add a geocoder answer with no county in it, a
# building list the service cut short, and what the road fetch raises for
# an ArcGIS error returned on HTTP 200 (a RuntimeError).
_OVERVIEW_FETCH_ERRORS = _WATER_FETCH_ERRORS + (census_geography.CensusGeographyIncompleteError,
                                                structures_data.StructuresTruncatedError, RuntimeError)

# The imagery fetch adds its own no-data answer, and what the STAC client
# and a COG read raise for a service that answers badly -- pystac_client's
# APIError and rasterio's RasterioIOError are both RuntimeError-derived or
# OSError-derived, so they are caught by those bases without this module
# importing either library.
_NAIP_FETCH_ERRORS = _WATER_FETCH_ERRORS + (naip_imagery.NaipIncompleteError, RuntimeError)


class ReportDataIncompleteError(RuntimeError):
    """
    A REQUIRED report layer could not be fetched. Carries `layer`, `label`
    and `reason` (REASON_SOURCE_UNAVAILABLE -- the service did not answer,
    a retry may help; REASON_NO_DATA_FOR_PARCEL -- it answered without
    data for this point, a retry will not) in the same three fields
    parcel_data.ParcelDataIncompleteError defines, so one wire contract
    covers Layer 1 and this layer.
    """

    REASON_SOURCE_UNAVAILABLE = "source_unavailable"
    REASON_NO_DATA_FOR_PARCEL = "no_data_for_parcel"

    def __init__(self, message: str, layer: str, label: str, reason: Optional[str] = None):
        super().__init__(message)
        self.layer = layer
        self.label = label
        self.reason = reason


@dataclass
class ReportData:
    boundary: list
    # (latitude, longitude) of the point the point-based layers were
    # fetched at.
    centroid: tuple
    # daymet_data.parse_daymet_csv()'s dict: the daily arrays, the citation
    # and DOI of the version served, the years used.
    daymet_daily: Optional[dict]
    # precipitation_normals.precipitation_correction()'s block: the factor
    # applied to the parcel's Daymet precipitation, and the stations --
    # off the bundle, no fetch.
    precipitation_correction: Optional[dict]
    # precipitation_normals.heavy_rain_normals()'s block: days >= 1.00 in
    # by month, the median of the same stations' NCEI normals.
    heavy_rain_normals: Optional[dict]
    # climate_report.derive_climate() over daymet_daily with the factor --
    # derived ONCE.
    climate: Optional[dict]
    # atlas14_data.parse_atlas14_csv()'s dict, and the design-storm block
    # drawn from it. None when the layer degraded.
    atlas14: Optional[dict]
    design_storms: Optional[dict]
    # power_wind_data.parse_power_csv()'s dict, and derive_wind() over it.
    # None when the layer degraded.
    power_wind: Optional[dict]
    wind: Optional[dict]
    # spc_reports.reports_within() at the centroid -- bundled, always
    # present.
    severe_weather: Optional[dict]
    # THE WATER LAYERS (branch 9), each None when it degraded. The NHD
    # point rows as hydrology_data returns them; the others PARSED by
    # their modules (nhdplus_data.parse_flowline_attributes,
    # nwi_data.parse_wetlands, nfhl_data.parse_flood_hazard,
    # nlcd_landcover_data.parse_land_cover,
    # soil_water_table.parse_seasonal_water_table).
    nhd_points: Optional[list] = None
    nhdplus_hr: Optional[dict] = None
    nwi: Optional[dict] = None
    fema_nfhl: Optional[dict] = None
    nlcd_landcover: Optional[dict] = None
    soil_water_table: Optional[dict] = None
    # THE ACCESS LAYER (branch 10): soil_road_ratings.parse_road_ratings'
    # block, None when it degraded.
    soil_road_ratings: Optional[dict] = None
    # THE TREES LAYERS (branch 11): forest_type_data.parse_forest_type's
    # block and soil_woodland.parse_woodland's, each None when it degraded.
    forest_type_group: Optional[dict] = None
    soil_woodland: Optional[dict] = None
    # THE SOILS LAYERS (branch 12): soil_survey.parse_survey's block and
    # bedrock_geology.parse_geology's, each None when it degraded.
    soil_survey: Optional[dict] = None
    bedrock_geology: Optional[dict] = None
    # THE DESIGN LAYER (branch 13): naip_imagery.parse_naip's block -- the
    # layout map's photography and its acquisition dates. None when it
    # degraded, and the map draws on white.
    naip_imagery: Optional[dict] = None
    # THE SITE OVERVIEW LAYERS (branch 17), each None when it degraded:
    # the context map's DEM dict, NHD water dict and road rows (the
    # Layer 1 shapes, a mile wider); census_geography.parse_county_state's
    # block; structures_data.parse_structures'; transmission_lines.
    # parse_transmission_lines'.
    context_dem: Optional[dict] = None
    context_water: Optional[dict] = None
    context_roads: Optional[list] = None
    county_state: Optional[dict] = None
    structures: Optional[dict] = None
    transmission_lines: Optional[dict] = None
    # The date the report layer was fetched: every report-layer source's
    # retrieval date. None on a fixture built without one.
    retrieved_on: Optional[date] = None
    # {layer: {"label", "reason", "error"}} for every DEGRADABLE layer that
    # failed. Empty when everything answered. A REQUIRED failure never
    # reaches a ReportData; it raises.
    unavailable: dict = field(default_factory=dict)


def boundary_centroid_lat_lon(boundary) -> tuple:
    """(latitude, longitude) of the boundary polygon's centroid, from the
    (lon, lat) ring every module in this codebase takes."""
    centroid = Polygon([(float(lon), float(lat)) for lon, lat in boundary]).centroid
    return (centroid.y, centroid.x)


def _failure(field_name: str, wire_pair: tuple, exc: BaseException) -> ReportDataIncompleteError:
    """The wire-shaped error for one failed layer: `field_name` is the
    ReportData field (the diagnostic row's name), `wire_pair` the (type,
    label) the frontend renders. The reason is read off the exception kind:
    a RequestException is the source not answering; an *IncompleteError
    is the source answering without the data."""
    layer, label = wire_pair
    if isinstance(exc, _NO_DATA_ERRORS):
        reason = ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL
    else:
        reason = ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE
    return ReportDataIncompleteError(
        f"report layer '{field_name}' ({label}) failed: {exc}", layer, label, reason
    )


def station_blocks(centroid) -> tuple:
    """(precipitation_correction, heavy_rain_normals) for a point, both
    off the bundle -- the one derivation the fetch path and the fixture
    path share. No network."""
    stations = precipitation_normals.nearest_stations(centroid[0], centroid[1])
    vintage, _ = precipitation_normals.load_bundle()
    return (
        precipitation_normals.precipitation_correction(stations, vintage),
        precipitation_normals.heavy_rain_normals(stations),
    )


def fetch_report_data(boundary) -> ReportData:
    """
    Every report-only layer for this boundary, per REPORT_FETCH_LAYERS.
    Raises ReportDataIncompleteError for a REQUIRED layer that fails;
    records a DEGRADABLE one under `unavailable` and continues.

    EVERY LAYER IS TIMED, AND ONLY TIMED -- the time_layer() block names
    the REPORT_FETCH_LAYERS entry and the callable, exactly as
    parcel_data.fetch_parcel_data() does, and changes nothing about what
    runs.
    """
    centroid = boundary_centroid_lat_lon(boundary)
    unavailable = {}

    def _degrade(field_name, wire_pair, exc):
        error = _failure(field_name, wire_pair, exc)
        if REPORT_FETCH_LAYERS[field_name] == REQUIRED:
            raise error from exc
        unavailable[field_name] = {"label": wire_pair[1], "reason": error.reason, "error": str(exc)}

    daymet_daily = None
    try:
        with run_diagnostics.time_layer("daymet_daily", get_daymet_daily_for_point):
            daymet_daily = get_daymet_daily_for_point(centroid[0], centroid[1])
    except (requests.exceptions.RequestException, DaymetIncompleteError) as exc:
        _degrade("daymet_daily", LAYER_CLIMATE, exc)

    # The precipitation correction and the heavy-rain normals: the nearest
    # bundled stations for the point, no fetch.
    correction, heavy_rain = station_blocks(centroid)

    climate = None
    if daymet_daily is not None:
        climate = derive_climate(daymet_daily, prcp_factor=correction["factor"])

    atlas14 = storms = None
    try:
        with run_diagnostics.time_layer("atlas14", get_atlas14_for_point):
            atlas14 = get_atlas14_for_point(centroid[0], centroid[1])
        storms = design_storms(atlas14)
    except (requests.exceptions.RequestException, Atlas14IncompleteError) as exc:
        atlas14 = storms = None
        _degrade("atlas14", LAYER_ATLAS14, exc)

    power_wind = wind = None
    if climate is not None:
        try:
            with run_diagnostics.time_layer("power_wind", get_power_wind_for_point):
                power_wind = get_power_wind_for_point(centroid[0], centroid[1], climate["years"])
            wind = derive_wind(power_wind)
        except (requests.exceptions.RequestException, PowerIncompleteError) as exc:
            power_wind = wind = None
            _degrade("power_wind", LAYER_POWER_WIND, exc)

    severe_weather = spc_reports.reports_within(centroid[0], centroid[1])

    # THE WATER LAYERS, in the order the section reads them. Each is one
    # timed block; each degrades on its own.
    nhd_points = None
    try:
        with run_diagnostics.time_layer("nhd_points", hydrology_data.get_nhd_points_for_boundary):
            nhd_points = hydrology_data.get_nhd_points_for_boundary(boundary)
    except _WATER_FETCH_ERRORS as exc:
        nhd_points = None
        _degrade("nhd_points", LAYER_NHD_POINTS, exc)

    nhdplus_hr = None
    try:
        with run_diagnostics.time_layer("nhdplus_hr", nhdplus_data.get_flowline_attributes_for_boundary):
            nhdplus_hr = nhdplus_data.parse_flowline_attributes(nhdplus_data.get_flowline_attributes_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        nhdplus_hr = None
        _degrade("nhdplus_hr", LAYER_NHDPLUS_HR, exc)

    nwi = None
    try:
        with run_diagnostics.time_layer("nwi", nwi_data.get_wetlands_for_boundary):
            nwi = nwi_data.parse_wetlands(nwi_data.get_wetlands_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        nwi = None
        _degrade("nwi", LAYER_NWI, exc)

    fema_nfhl = None
    try:
        with run_diagnostics.time_layer("fema_nfhl", nfhl_data.get_flood_hazard_for_boundary):
            fema_nfhl = nfhl_data.parse_flood_hazard(nfhl_data.get_flood_hazard_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        fema_nfhl = None
        _degrade("fema_nfhl", LAYER_NFHL, exc)

    nlcd_landcover = None
    try:
        with run_diagnostics.time_layer("nlcd_landcover", nlcd_landcover_data.get_land_cover_for_boundary):
            nlcd_landcover = nlcd_landcover_data.parse_land_cover(nlcd_landcover_data.get_land_cover_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        nlcd_landcover = None
        _degrade("nlcd_landcover", LAYER_NLCD, exc)

    water_table = None
    try:
        with run_diagnostics.time_layer("soil_water_table", soil_water_table.get_seasonal_water_table_for_boundary):
            water_table = soil_water_table.parse_seasonal_water_table(
                soil_water_table.get_seasonal_water_table_for_boundary(boundary)
            )
    except _WATER_FETCH_ERRORS as exc:
        water_table = None
        _degrade("soil_water_table", LAYER_SOIL_WATER_TABLE, exc)

    road_ratings = None
    try:
        with run_diagnostics.time_layer("soil_road_ratings", soil_road_ratings.get_road_ratings_for_boundary):
            road_ratings = soil_road_ratings.parse_road_ratings(soil_road_ratings.get_road_ratings_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        road_ratings = None
        _degrade("soil_road_ratings", LAYER_SOIL_ROAD_RATINGS, exc)

    forest_type = None
    try:
        with run_diagnostics.time_layer("forest_type_group", forest_type_data.get_forest_type_for_boundary):
            forest_type = forest_type_data.parse_forest_type(forest_type_data.get_forest_type_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        forest_type = None
        _degrade("forest_type_group", LAYER_FOREST_TYPE_GROUP, exc)

    woodland = None
    try:
        with run_diagnostics.time_layer("soil_woodland", soil_woodland.get_woodland_for_boundary):
            woodland = soil_woodland.parse_woodland(soil_woodland.get_woodland_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        woodland = None
        _degrade("soil_woodland", LAYER_SOIL_WOODLAND, exc)

    # THE SOILS LAYERS. soil_survey is one query on the service Layer 1
    # already reached; bedrock_geology is a different service entirely.
    survey = None
    try:
        with run_diagnostics.time_layer("soil_survey", soil_survey.get_survey_for_boundary):
            survey = soil_survey.parse_survey(soil_survey.get_survey_for_boundary(boundary))
    except _WATER_FETCH_ERRORS as exc:
        survey = None
        _degrade("soil_survey", LAYER_SOIL_SURVEY, exc)

    geology = None
    try:
        with run_diagnostics.time_layer("bedrock_geology", bedrock_geology.get_geology_for_boundary):
            geology = bedrock_geology.parse_geology(bedrock_geology.get_geology_for_boundary(boundary))
    except _GEOLOGY_FETCH_ERRORS as exc:
        geology = None
        _degrade("bedrock_geology", LAYER_BEDROCK_GEOLOGY, exc)

    imagery = None
    try:
        with run_diagnostics.time_layer("naip_imagery", naip_imagery.get_naip_for_boundary):
            imagery = naip_imagery.parse_naip(naip_imagery.get_naip_for_boundary(boundary))
    except _NAIP_FETCH_ERRORS as exc:
        imagery = None
        _degrade("naip_imagery", LAYER_NAIP_IMAGERY, exc)

    # THE SITE OVERVIEW LAYERS. The context map's three are Layer 1's own
    # fetch functions a mile wider; each degrades on its own.
    context_dem = None
    try:
        with run_diagnostics.time_layer("context_dem", context_map_data.get_context_dem_for_boundary):
            context_dem = context_map_data.get_context_dem_for_boundary(boundary)
    except _OVERVIEW_FETCH_ERRORS as exc:
        context_dem = None
        _degrade("context_dem", LAYER_CONTEXT_DEM, exc)

    context_water = None
    try:
        with run_diagnostics.time_layer("context_water", context_map_data.get_context_water_for_boundary):
            context_water = context_map_data.get_context_water_for_boundary(boundary)
    except _OVERVIEW_FETCH_ERRORS as exc:
        context_water = None
        _degrade("context_water", LAYER_CONTEXT_WATER, exc)

    context_roads = None
    try:
        with run_diagnostics.time_layer("context_roads", context_map_data.get_context_roads_for_boundary):
            context_roads = context_map_data.get_context_roads_for_boundary(boundary)
    except _OVERVIEW_FETCH_ERRORS as exc:
        context_roads = None
        _degrade("context_roads", LAYER_CONTEXT_ROADS, exc)

    county_state = None
    try:
        with run_diagnostics.time_layer("county_state", census_geography.get_county_state_for_point):
            county_state = census_geography.parse_county_state(
                census_geography.get_county_state_for_point(centroid[0], centroid[1])
            )
    except _OVERVIEW_FETCH_ERRORS as exc:
        county_state = None
        _degrade("county_state", LAYER_COUNTY_STATE, exc)

    structures = None
    try:
        with run_diagnostics.time_layer("structures", structures_data.get_structures_for_boundary):
            structures = structures_data.parse_structures(structures_data.get_structures_for_boundary(boundary))
    except _OVERVIEW_FETCH_ERRORS as exc:
        structures = None
        _degrade("structures", LAYER_STRUCTURES, exc)

    lines = None
    try:
        with run_diagnostics.time_layer("transmission_lines", transmission_lines.get_transmission_lines_near_boundary):
            lines = transmission_lines.parse_transmission_lines(
                transmission_lines.get_transmission_lines_near_boundary(boundary)
            )
    except _OVERVIEW_FETCH_ERRORS as exc:
        lines = None
        _degrade("transmission_lines", LAYER_TRANSMISSION, exc)

    return ReportData(
        boundary=list(boundary),
        centroid=centroid,
        daymet_daily=daymet_daily,
        precipitation_correction=correction,
        heavy_rain_normals=heavy_rain,
        climate=climate,
        atlas14=atlas14,
        design_storms=storms,
        power_wind=power_wind,
        wind=wind,
        severe_weather=severe_weather,
        nhd_points=nhd_points,
        nhdplus_hr=nhdplus_hr,
        nwi=nwi,
        fema_nfhl=fema_nfhl,
        nlcd_landcover=nlcd_landcover,
        soil_water_table=water_table,
        soil_road_ratings=road_ratings,
        forest_type_group=forest_type,
        soil_woodland=woodland,
        soil_survey=survey,
        bedrock_geology=geology,
        naip_imagery=imagery,
        context_dem=context_dem,
        context_water=context_water,
        context_roads=context_roads,
        county_state=county_state,
        structures=structures,
        transmission_lines=lines,
        retrieved_on=date.today(),
        unavailable=unavailable,
    )


def report_data_from_fixtures(
    boundary,
    daymet_daily: dict,
    atlas14: Optional[dict] = None,
    power_wind: Optional[dict] = None,
    severe_weather: bool = True,
    unavailable: Optional[dict] = None,
    correct_precipitation: bool = True,
    nhd_points: Optional[list] = None,
    nhdplus_hr: Optional[dict] = None,
    nwi: Optional[dict] = None,
    fema_nfhl: Optional[dict] = None,
    nlcd_landcover: Optional[dict] = None,
    soil_water_table_rows: Optional[list] = None,
    soil_road_ratings_rows: Optional[list] = None,
    forest_type_group: Optional[dict] = None,
    soil_woodland_rows: Optional[dict] = None,
    soil_survey_rows: Optional[list] = None,
    bedrock_geology_raw: Optional[dict] = None,
    naip_imagery_raw: Optional[dict] = None,
    context_dem: Optional[dict] = None,
    context_water: Optional[dict] = None,
    context_roads: Optional[list] = None,
    county_state_raw: Optional[dict] = None,
    structures_raw: Optional[dict] = None,
    transmission_lines_raw: Optional[dict] = None,
    retrieved_on: Optional[date] = None,
) -> ReportData:
    """
    A ReportData from parsed responses ALREADY IN HAND -- the reference
    fixtures, a diagnostic run -- with every block derived exactly as
    fetch_report_data() derives it. No network: the correction and the
    heavy-rain normals come off the bundle for the boundary's centroid
    (`correct_precipitation=False` leaves the factor at 1.0 and both
    blocks None, the branch 5 shape). A layer passed as None is absent,
    as if it degraded; `unavailable` may then name it. The production
    path is fetch_report_data().

    The Water layers take the RAW response each fetch function returns
    (water_reference_fixture.raw_water_layers()) and are parsed here the
    way fetch_report_data() parses them; None is absent. The Access
    layer's rows (access_reference_fixture) the same way, and the Trees
    layers' raw answers (trees_reference_fixture): the forest type TIFF
    dict and the two woodland row sets. The Soils layers' the same way
    (soils_reference_fixture): the survey rows, and the geology fetch's
    three-part raw answer. The layout map's imagery the same way
    (naip_reference_fixture.raw_naip()). The overview's (overview_reference_fixture): the
    context map's three Layer 1-shaped answers as they are, and the
    geocoder, structures and transmission responses raw.
    """
    centroid = boundary_centroid_lat_lon(boundary)
    correction = heavy_rain = None
    if correct_precipitation:
        correction, heavy_rain = station_blocks(centroid)
    climate = derive_climate(daymet_daily, prcp_factor=correction["factor"] if correction else 1.0)
    return ReportData(
        boundary=list(boundary),
        centroid=centroid,
        daymet_daily=daymet_daily,
        precipitation_correction=correction,
        heavy_rain_normals=heavy_rain,
        climate=climate,
        atlas14=atlas14,
        design_storms=design_storms(atlas14) if atlas14 is not None else None,
        power_wind=power_wind,
        wind=derive_wind(power_wind) if power_wind is not None else None,
        severe_weather=spc_reports.reports_within(centroid[0], centroid[1]) if severe_weather else None,
        nhd_points=list(nhd_points) if nhd_points is not None else None,
        nhdplus_hr=nhdplus_data.parse_flowline_attributes(nhdplus_hr) if nhdplus_hr is not None else None,
        nwi=nwi_data.parse_wetlands(nwi) if nwi is not None else None,
        fema_nfhl=nfhl_data.parse_flood_hazard(fema_nfhl) if fema_nfhl is not None else None,
        nlcd_landcover=nlcd_landcover_data.parse_land_cover(nlcd_landcover) if nlcd_landcover is not None else None,
        soil_water_table=soil_water_table.parse_seasonal_water_table(soil_water_table_rows) if soil_water_table_rows is not None else None,
        soil_road_ratings=soil_road_ratings.parse_road_ratings(soil_road_ratings_rows) if soil_road_ratings_rows is not None else None,
        forest_type_group=forest_type_data.parse_forest_type(forest_type_group) if forest_type_group is not None else None,
        soil_woodland=soil_woodland.parse_woodland(soil_woodland_rows) if soil_woodland_rows is not None else None,
        soil_survey=soil_survey.parse_survey(soil_survey_rows) if soil_survey_rows is not None else None,
        bedrock_geology=bedrock_geology.parse_geology(bedrock_geology_raw) if bedrock_geology_raw is not None else None,
        naip_imagery=naip_imagery.parse_naip(naip_imagery_raw) if naip_imagery_raw is not None else None,
        context_dem=context_dem,
        context_water=context_water,
        context_roads=list(context_roads) if context_roads is not None else None,
        county_state=census_geography.parse_county_state(county_state_raw) if county_state_raw is not None else None,
        structures=structures_data.parse_structures(structures_raw) if structures_raw is not None else None,
        transmission_lines=(transmission_lines.parse_transmission_lines(transmission_lines_raw)
                            if transmission_lines_raw is not None else None),
        retrieved_on=retrieved_on,
        unavailable=dict(unavailable or {}),
    )


def report_data_from_daily(boundary, daymet_daily: dict) -> ReportData:
    """The branch 5 shape, kept for its callers: Daymet alone with the
    bundled correction, no fetched layer. See report_data_from_fixtures()."""
    return report_data_from_fixtures(boundary, daymet_daily)


def _build_default_cache():
    # Imported here, not at module top: session_cache imports parcel_data
    # and the whole Layer 1 import graph, and this module is also imported
    # by run_diagnostics' self-check, which must stay importable on its own.
    import session_cache

    return session_cache.FetchCache(fetch_function=fetch_report_data)


# The process-wide default, the same shape session_cache.DEFAULT_FETCH_CACHE
# takes: a caller with its own cache (a test) passes it, everyone else gets
# this one. Built lazily on first use for the import reason above.
_DEFAULT_CACHE = None


def default_report_fetch_cache():
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = _build_default_cache()
    return _DEFAULT_CACHE
