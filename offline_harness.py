"""
offline_harness.py

THE NETWORK, REFUSED INSTANTLY.

    import offline_harness
    offline_harness.install()

Every regression test in this repo is offline by contract: the boundaries
it exercises are the real fetch layers -- SSURGO, NHD, the National Map
roads service, the NAIP tile cache -- and what it asserts about them is
that the pipeline degrades gracefully when they cannot be reached. What
none of those tests assert is how LONG an unreachable host takes to be
unreachable, and that is what they were paying for: a fetch that leaks
through to `requests` runs the real retry loop, three attempts at 30, 60
and 90 second timeouts with a two-second pause between them. On a host
that refuses the connection outright that is four seconds of pure sleep
per fetch (test_road_corridors_pipeline.py made 76 such fetches: 304 s of
its 365 s were `time.sleep`); on a network that drops the packets instead
it is 184 s per fetch, which is how a suite whose computation takes
minutes came to take two hours.

WHAT THIS CHANGES: `requests.get` / `requests.post` (and the other verbs)
raise `requests.exceptions.ConnectionError` immediately, and the pause
between retry attempts (`fetch_attempts.RETRY_PAUSE_SECONDS`) is zero.

WHAT THIS DOES NOT CHANGE: the retry loops still run, still count their
attempts, still publish them; the graceful-degradation branches still
execute against a real `RequestException`; every mock a test installs
over `<module>.requests.post` still takes precedence, because it is
installed later and on the same module object. The tests assert what they
asserted before -- they just no longer wait to do it.

A test that DOES want the real network (the `--live` flags some files
carry) must not install this. `refused()` reports what was blocked, so a
test can show its zero-network claim was tested rather than assumed.
"""

from __future__ import annotations

import threading
from urllib.parse import urlsplit

import requests

import fetch_attempts

_LOCK = threading.Lock()
_INSTALLED = False
_ORIGINALS: dict = {}
_REFUSED: list = []

_VERBS = ("get", "post", "put", "patch", "delete", "head", "options", "request")


class OfflineNetworkError(requests.exceptions.ConnectionError):
    """Raised in place of any outbound HTTP request while the harness is
    installed. A ConnectionError so every `except RequestException` path
    the fetch layers carry sees exactly the failure it was written for."""


def _refusing(verb: str):
    def call(*args, **kwargs):
        url = None
        if verb == "request":
            url = args[1] if len(args) > 1 else kwargs.get("url")
        else:
            url = args[0] if args else kwargs.get("url")
        host = urlsplit(url).netloc if isinstance(url, str) else "?"
        with _LOCK:
            _REFUSED.append((verb.upper(), host))
        raise OfflineNetworkError(
            f"offline_harness: {verb.upper()} to {host} refused -- the regression suite has no network"
        )

    call.__name__ = f"offline_{verb}"
    return call


def install(*, retry_pause_seconds: float = 0.0) -> None:
    """Refuse every outbound `requests` call from now on and zero the
    retry pause. Idempotent."""
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return
        for verb in _VERBS:
            _ORIGINALS[verb] = getattr(requests, verb)
            setattr(requests, verb, _refusing(verb))
        _ORIGINALS["retry_pause_seconds"] = fetch_attempts.RETRY_PAUSE_SECONDS
        fetch_attempts.RETRY_PAUSE_SECONDS = retry_pause_seconds
        _INSTALLED = True


def uninstall() -> None:
    """Restore `requests` and the retry pause. For the rare test that
    installs the harness for one section only."""
    global _INSTALLED
    with _LOCK:
        if not _INSTALLED:
            return
        for verb in _VERBS:
            setattr(requests, verb, _ORIGINALS.pop(verb))
        fetch_attempts.RETRY_PAUSE_SECONDS = _ORIGINALS.pop("retry_pause_seconds")
        _INSTALLED = False


def installed() -> bool:
    return _INSTALLED


def refused() -> list:
    """Every request refused since install() (or the last clear()), as
    (VERB, host) pairs in the order they were attempted."""
    with _LOCK:
        return list(_REFUSED)


def clear() -> None:
    with _LOCK:
        _REFUSED.clear()


def summary() -> str:
    """One line for a test's closing print: how many fetches the harness
    refused, by host."""
    counts: dict = {}
    for _, host in refused():
        counts[host] = counts.get(host, 0) + 1
    if not counts:
        return "offline_harness: no outbound requests were attempted"
    parts = ", ".join(f"{host} x{n}" for host, n in sorted(counts.items()))
    return f"offline_harness: refused {len(refused())} outbound request(s) instantly ({parts})"
