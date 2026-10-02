"""
host_slots.py

A PER-HOST CAP ON CONCURRENT FETCHES -- the companion to host_breaker.py
for the concurrent report-layer fetch.

THE QUESTION THIS ANSWERS. report_data.fetch_report_data() runs its
twenty layers at once rather than one after another (the audit measured
the serial stage at 42-55 s, about 80% of a 53 s run; concurrent, the
stage takes roughly its slowest single fetch). Twenty at once is not
twenty hosts at once: four of the layers are five SQL queries against
the one Soil Data Access endpoint, two are on the NHD gateway the probes
measured shedding load, two on one imagery server, two on one Esri
feature service. A GLOBAL cap would either strangle the stage (a cap of
four leaves sixteen fast fetches queued behind four slow ones) or let
one host see every query this process can make at once. The cap is
PER HOST: each host has its own number of slots, and a layer waits for a
slot on ITS host only.

PROCESS-WIDE, LIKE THE BREAKER, AND FOR THE SAME REASON. Host load is a
fact about the host, not about any one job: two report jobs running at
once on job_runner's pool would otherwise each open their full quota
against the soil survey, and the cap would mean nothing exactly when it
mattered. The semaphores are keyed by netloc and shared by every caller
in the process.

WHAT IT IS NOT. Not a rate limiter (nothing here counts requests per
second), not a queue with an order (a waiting layer acquires when a slot
frees, in the lock's order), not a timeout (a slot is waited for as long
as it takes -- the fetch behind it is bounded by its own retry budget,
so the wait is bounded by that). Nothing here retries, sleeps or decides
budgets; the layer's own loop does all of that once it holds the slot.

    with host_slots.slot(url):
        ...the layer's fetch...

`slot()` takes a URL or a bare host; the host's cap comes from
HOST_CONCURRENCY, or DEFAULT_CONCURRENCY for a host not named there.
"""

from __future__ import annotations

import contextlib
import threading
from urllib.parse import urlsplit

# How many fetches may be in flight against one host at once, from this
# process. The named hosts are the ones several report layers share;
# every other host carries at most one report layer today, so the
# default is what two concurrent report jobs would do to it.
#
#   sdmdataaccess.sc.egov.usda.gov  Soil Data Access: four report layers,
#       five SQL queries, each answering in about a second on the
#       reference parcel. One shared SQL endpoint with no published
#       rate limit; two at once keeps the stage short (the soil layers
#       finish inside the slowest other fetch) without presenting the
#       service with every soil query this process can make.
#   hydro.nationalmap.gov  NHD and NHDPlus HR: the gateway the
#       reliability probes measured shedding load. Two slots, one per
#       report layer on it, so the two run side by side as the breaker
#       expects (see "THE BREAKER UNDER CONCURRENCY" in report_data.py).
#   imagery.geoplatform.gov  two exportImage layers (NLCD, forest type).
#   services2.arcgis.com  two Esri feature-service layers (structures,
#       transmission lines).
DEFAULT_CONCURRENCY = 2
HOST_CONCURRENCY = {
    "sdmdataaccess.sc.egov.usda.gov": 2,
    "hydro.nationalmap.gov": 2,
    "imagery.geoplatform.gov": 2,
    "services2.arcgis.com": 2,
}

_LOCK = threading.Lock()
_SEMAPHORES: dict = {}
# {host: the most fetches seen in flight at once} -- what the cap actually
# held to, for a diagnostic or a test to read. See peak_in_flight().
_IN_FLIGHT: dict = {}
_PEAK: dict = {}


def host_of(url_or_host: str) -> str:
    """The netloc of a URL, or the string itself when it has no scheme."""
    parts = urlsplit(url_or_host)
    return parts.netloc or url_or_host


def concurrency_for(host: str) -> int:
    return HOST_CONCURRENCY.get(host, DEFAULT_CONCURRENCY)


def _semaphore(host: str) -> threading.BoundedSemaphore:
    with _LOCK:
        semaphore = _SEMAPHORES.get(host)
        if semaphore is None:
            semaphore = _SEMAPHORES[host] = threading.BoundedSemaphore(concurrency_for(host))
        return semaphore


@contextlib.contextmanager
def slot(url_or_host: str):
    """Hold one of the host's slots for the duration of the block."""
    host = host_of(url_or_host)
    semaphore = _semaphore(host)
    semaphore.acquire()
    with _LOCK:
        _IN_FLIGHT[host] = _IN_FLIGHT.get(host, 0) + 1
        _PEAK[host] = max(_PEAK.get(host, 0), _IN_FLIGHT[host])
    try:
        yield
    finally:
        with _LOCK:
            _IN_FLIGHT[host] -= 1
        semaphore.release()


def peak_in_flight() -> dict:
    """{host: the most slots held at once since reset()} -- the measured
    answer to "what did the cap hold each host to"."""
    with _LOCK:
        return dict(_PEAK)


def reset() -> None:
    """Forget the semaphores and the peaks. For tests, and for a changed
    HOST_CONCURRENCY to take effect: a semaphore is sized when first
    asked for."""
    with _LOCK:
        _SEMAPHORES.clear()
        _IN_FLIGHT.clear()
        _PEAK.clear()
