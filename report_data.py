"""
report_data.py

THE REPORT-TIME DATA LAYER: every source the SITE DATA REPORT reads that no
design step consumes, fetched when a report is asked for, cached by
boundary, and governed by its own failure table.

    fetch_report_data(boundary)   -> ReportData, or raises
    REPORT_FETCH_LAYERS           -> {layer: REQUIRED | DEGRADABLE}
    default_report_fetch_cache()  -> the process-wide FetchCache for it

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

Daymet daily weather is REQUIRED: a site report without climate is not a
site report. It is the only layer in this branch; later sources (FEMA,
NWI, occurrence records, ...) join this table, not Layer 1.

THE SAME INSTRUMENTATION AS LAYER 1. Every fetch sits in a
run_diagnostics.time_layer() block naming its REPORT_FETCH_LAYERS entry,
and run_diagnostics._fetch_hook_sites() cross-checks this module's
compiled fetch_report_data() against REPORT_FETCH_LAYERS exactly as it
checks parcel_data -- a second layer added without a timer shows up in
self_check() as "1 of 2". time_layer() is a no-op until a probe is opened
on the thread (run_diagnostics.begin_fetch); opening one on the report job
is the job's wiring, not this module's.

THE CACHE. default_report_fetch_cache() is a session_cache.FetchCache
built over fetch_report_data (lazily, on first use -- session_cache pulls
in the whole Layer 1 import graph and this module must stay importable by
the diagnostics self-check on its own): keyed by the normalised boundary,
LRU-capped, per-key in-flight lock, failures never cached. A second report on the same
land pays no second fetch. It holds a ReportData, which is a value: the
climate block is derived once here, so no consumer recomputes it.

THE POINT. Daymet is 1 km gridded data, so one point represents the
parcel: the boundary polygon's centroid in WGS84 (shapely over lon/lat --
at parcel scale the difference from a projected centroid is metres, and
the Daymet cell is a kilometre). ReportData.centroid records it so the
footer and the cover can print the same point the fetch used.
"""

from dataclasses import dataclass, field
from typing import Optional

import requests
from shapely.geometry import Polygon

import run_diagnostics
from climate_report import derive_climate
from daymet_data import DaymetIncompleteError, get_daymet_daily_for_point

REQUIRED = "required"
DEGRADABLE = "degradable"

# THE TABLE. Layer name -> policy, in fetch order. Names are ReportData
# field names, so a diagnostic row points at the field the fetch filled.
# Cross-checked against the compiled fetch_report_data() by
# run_diagnostics._fetch_hook_sites().
REPORT_FETCH_LAYERS = {
    "daymet_daily": REQUIRED,
}

# The (type, label) pair a Daymet failure reports as -- the same split
# every other layer error carries: a stable type to branch on, display
# prose to print.
LAYER_CLIMATE = ("climate", "climate records")


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
    # climate_report.derive_climate() over daymet_daily -- derived ONCE.
    climate: Optional[dict]
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
    a RequestException is the source not answering; a DaymetIncompleteError
    is the source answering without the data."""
    layer, label = wire_pair
    if isinstance(exc, DaymetIncompleteError):
        reason = ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL
    else:
        reason = ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE
    return ReportDataIncompleteError(
        f"report layer '{field_name}' ({label}) failed: {exc}", layer, label, reason
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

    daymet_daily = None
    try:
        with run_diagnostics.time_layer("daymet_daily", get_daymet_daily_for_point):
            daymet_daily = get_daymet_daily_for_point(centroid[0], centroid[1])
    except (requests.exceptions.RequestException, DaymetIncompleteError) as exc:
        error = _failure("daymet_daily", LAYER_CLIMATE, exc)
        if REPORT_FETCH_LAYERS["daymet_daily"] == REQUIRED:
            raise error from exc
        unavailable["daymet_daily"] = {"label": LAYER_CLIMATE[1], "reason": error.reason, "error": str(exc)}

    climate = derive_climate(daymet_daily) if daymet_daily is not None else None

    return ReportData(
        boundary=list(boundary),
        centroid=centroid,
        daymet_daily=daymet_daily,
        climate=climate,
        unavailable=unavailable,
    )


def report_data_from_daily(boundary, daymet_daily: dict) -> ReportData:
    """
    A ReportData from daily arrays ALREADY IN HAND -- a parsed Daymet CSV
    (the reference fixture, a diagnostic run) -- with the climate block
    derived exactly as fetch_report_data() derives it. No network. What a
    test or a diagnostic uses to render a report offline; the production
    path is fetch_report_data().
    """
    return ReportData(
        boundary=list(boundary),
        centroid=boundary_centroid_lat_lon(boundary),
        daymet_daily=daymet_daily,
        climate=derive_climate(daymet_daily),
        unavailable={},
    )


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
