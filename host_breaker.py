"""
host_breaker.py

A PER-HOST CIRCUIT BREAKER for the fetch layers' retry loops.

THE FAILURE THIS ANSWERS. The NHD reliability probes measured
hydro.nationalmap.gov as an overloaded gateway shedding load in episodes:
17% of first requests answered 502/504 (most in under 11 seconds), an
immediate retry after a 5xx failed again 14 times out of 17, and an
episode spans minutes. Four fetch layers sit on that one host (Layer 1's
flowlines and waterbodies, the report's NHDPlus HR attributes and its
one-mile context water), each with its own 30/60/90-second retry budget.
During an episode they fail IN SEQUENCE, each burning its full budget
against a host the previous layer just proved down -- which is how one
flaky service turned into ~4.5 minutes of report, and what report_data.py
logged as "a degradable layer's worst case should be decided deliberately,
and per host rather than per layer". This module is that decision.

WHAT IT DOES. A retry loop that exhausts its whole budget against a host
reports it here (record_failure); the host's circuit is then OPEN for
COOLDOWN_SECONDS, and check() -- called by the same loops before they
issue a request -- fails instantly with HostCircuitOpenError instead of
letting another budget burn. Any successful request reports record_
success, which closes the circuit at once. After the cooldown the circuit
is ajar: calls flow again, and the next exhausted budget re-opens it while
the next success closes it. A single failed ATTEMPT never opens the
circuit -- only a loop that spent its whole budget does, so one blip
inside an otherwise-answering host changes nothing.

THE ERROR IS A requests ConnectionError SUBCLASS, deliberately: every
fetch layer already catches requests.exceptions.RequestException and
already has a policy for it (hard-fail at Layer 1, degrade at the report
layer). An open circuit reaches each caller as exactly the failure type
its policy was written for, carrying a message that says which host is
open and for how long. Nothing downstream branches on the new type, and
nothing needs to.

KEYED BY NETLOC, SHARED PROCESS-WIDE. Host health is a fact about the
host, not about any one layer, boundary or thread -- two sessions created
concurrently learn from each other's failures, which is the point. The
state is three numbers per host under one lock; nothing here sleeps,
retries or decides budgets.

DISABLED UNDER THE OFFLINE HARNESS. The regression suite's contract is
that every fetch layer runs its real retry loop against an instantly
refused network (offline_harness.py); a breaker that opened on the first
exhausted budget would make every later fetch in the same process
fail with zero attempts, and the suite's attempt counts would depend on
file order. offline_harness.install() therefore disables this module the
same way it zeroes RETRY_PAUSE_SECONDS, and test_host_breaker.py enables
it explicitly to prove the behavior above.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

import requests

# How long an exhausted budget keeps the host's circuit open. The probes'
# shedding episodes span minutes and an immediate retry almost always
# failed, while a request a couple of minutes later was usually clean --
# so two minutes refuses the window where another budget is near-certainly
# wasted, without writing a host off for longer than an episode lasts.
COOLDOWN_SECONDS = 120.0

_LOCK = threading.Lock()
# {netloc: monotonic time the circuit opened}. Present = opened; absent =
# closed. A success deletes the entry, an exhausted budget (re)writes it.
_OPENED_AT: dict = {}
_ENABLED = True


class HostCircuitOpenError(requests.exceptions.ConnectionError):
    """A request was refused locally because its host's circuit is open:
    a retry loop exhausted its whole budget against this host within the
    last COOLDOWN_SECONDS. A ConnectionError so every existing
    `except RequestException` policy handles it unchanged."""


def _netloc(url: str) -> str:
    return urlsplit(url).netloc


def check(url: str) -> None:
    """Raise HostCircuitOpenError when `url`'s host is open; return
    (quietly) otherwise. Call once per request-making helper call, BEFORE
    the retry loop -- an open circuit should cost nothing, not three
    pauses."""
    if not _ENABLED:
        return
    host = _netloc(url)
    with _LOCK:
        opened_at = _OPENED_AT.get(host)
        if opened_at is None:
            return
        remaining = COOLDOWN_SECONDS - (time.monotonic() - opened_at)
        if remaining <= 0:
            # Cooldown over: the circuit is ajar. The entry stays until a
            # success deletes it or another exhausted budget refreshes it;
            # with remaining <= 0 it refuses nothing in the meantime.
            return
    raise HostCircuitOpenError(
        f"host_breaker: {host} refused locally -- a fetch exhausted its retry "
        f"budget against this host {COOLDOWN_SECONDS - remaining:.0f}s ago, so its "
        f"circuit is open for another {remaining:.0f}s rather than burning another "
        f"30/60/90s budget into the same episode"
    )


def record_success(url: str) -> None:
    """Any answered request closes its host's circuit immediately."""
    if not _ENABLED:
        return
    with _LOCK:
        _OPENED_AT.pop(_netloc(url), None)


def record_failure(url: str) -> None:
    """An EXHAUSTED RETRY BUDGET -- not a single failed attempt -- opens
    (or re-opens) its host's circuit for COOLDOWN_SECONDS. The retry loop
    calls this exactly where it gives up and raises."""
    if not _ENABLED:
        return
    with _LOCK:
        _OPENED_AT[_netloc(url)] = time.monotonic()


def set_enabled(enabled: bool) -> None:
    """offline_harness.install()/uninstall()'s switch; see the module
    docstring. Disabling also clears the state, so re-enabling starts
    every circuit closed."""
    global _ENABLED
    with _LOCK:
        _ENABLED = enabled
        if not enabled:
            _OPENED_AT.clear()


def enabled() -> bool:
    return _ENABLED


def reset() -> None:
    """Close every circuit. For tests."""
    with _LOCK:
        _OPENED_AT.clear()
