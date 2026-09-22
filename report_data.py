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
from typing import Optional

import requests
from shapely.geometry import Polygon

import precipitation_normals
import run_diagnostics
import spc_reports
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
}

# The (type, label) pair each layer's failure reports as -- the same split
# every other layer error carries: a stable type to branch on, display
# prose to print.
LAYER_CLIMATE = ("climate", "climate records")
LAYER_ATLAS14 = ("design_storms", "design storm depths")
LAYER_POWER_WIND = ("wind", "wind records")

# The exception kinds that mean "the source answered without the data",
# as opposed to a RequestException, "the source did not answer".
_NO_DATA_ERRORS = (DaymetIncompleteError, Atlas14IncompleteError, PowerIncompleteError)


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
