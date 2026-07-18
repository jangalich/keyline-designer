"""
test_road_corridor_fetch_diagnostics.py

Offline (no-network, mocked) checks for road_corridors.py's
_log_fetch_failure() — the diagnostic added after
get_erosion_factor_for_polygon()'s chorizon/component schema bug (a real
SDA HTTP 400 on every single run) degraded silently, identically to
ordinary network flakiness, via a bare `except Exception: return None,
True`. These checks confirm a genuine query/schema error (HTTP 4xx) now
prints a distinctly different message than a transient network failure
(timeout/connection error) or an unexpected non-network bug — while still
degrading gracefully (never raising) in every case, since that graceful-
degradation behavior itself is unchanged and still relied on.
"""

import io
from contextlib import redirect_stdout
from unittest.mock import patch

import requests

import road_corridors

BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
DEM = {"crs": "EPSG:32617"}  # _fetch_erosion_prone_union doesn't touch the array for a failed fetch


def _run_with_captured_output(mock_exception):
    buffer = io.StringIO()
    with patch.object(road_corridors, "get_erosion_factor_for_polygon", side_effect=mock_exception):
        with redirect_stdout(buffer):
            result = road_corridors._fetch_erosion_prone_union(BOUNDARY, DEM)
    return result, buffer.getvalue()


# --- a genuine schema/query error (HTTP 400) is distinguished from network flakiness ---

response_400 = requests.Response()
response_400.status_code = 400
http_error = requests.exceptions.HTTPError("400 Client Error", response=response_400)

(union, data_unavailable), output = _run_with_captured_output(http_error)
assert union is None and data_unavailable is True, "must still degrade gracefully, not raise"
assert "HTTP 400" in output
assert "not transient network unavailability" in output or "schema bug" in output
print("A real HTTP 400 (query/schema error) is flagged distinctly in the diagnostic output.")


# --- a transient network failure (timeout) reads differently from a schema error ---

(union, data_unavailable), output = _run_with_captured_output(requests.exceptions.Timeout("timed out"))
assert union is None and data_unavailable is True
assert "HTTP 400" not in output
assert "not transient network unavailability" not in output
assert "network request failed" in output
print("A timeout is reported as ordinary network failure, not mistaken for a schema/query bug.")


# --- a non-network bug (e.g. a malformed response causing a KeyError) is also distinguished ---

(union, data_unavailable), output = _run_with_captured_output(KeyError("kwfact"))
assert union is None and data_unavailable is True
assert "unexpected failure, not a network error" in output
assert "KeyError" in output
print("A non-network exception (a real bug) is flagged as such, not reported as a network issue.")

print("\nAll road corridor fetch-diagnostics checks passed.")
