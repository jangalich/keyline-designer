"""
test_road_corridor_fetch_diagnostics.py

Offline (no-network, mocked) checks for road_corridors.py's
_log_fetch_failure() — the diagnostic that distinguishes a genuine
query/schema error (an HTTP 4xx status, which will fail identically on
every future run) from ordinary transient network flakiness (a timeout
or connection error) or an unexpected non-network bug, rather than
folding all three into one indistinguishable "network request failed"
message. Exercised here via _fetch_existing_road_features_utm() (the
road-anchoring fetch) — any of this module's several fetch-then-degrade
functions would do equally well as the vehicle for these checks, since
they all delegate their error reporting to the same _log_fetch_failure()
helper.

(This test used to exercise the module's now-removed erosion-prone-soil
fetch — that preference was removed outright, not merely relocated;
see road_corridors.py's own module docstring for why. _log_fetch_failure()
itself is unaffected and still needed by every other real-data fetch in
this module.)
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
DEM = {"crs": "EPSG:32617"}  # _fetch_existing_road_features_utm doesn't touch the array for a failed fetch


def _run_with_captured_output(mock_exception):
    buffer = io.StringIO()
    with patch.object(road_corridors, "get_farm_roads_for_boundary", side_effect=mock_exception):
        with redirect_stdout(buffer):
            result = road_corridors._fetch_existing_road_features_utm(BOUNDARY, DEM)
    return result, buffer.getvalue()


# --- a genuine schema/query error (HTTP 400) is distinguished from network flakiness ---

response_400 = requests.Response()
response_400.status_code = 400
http_error = requests.exceptions.HTTPError("400 Client Error", response=response_400)

result, output = _run_with_captured_output(http_error)
assert result is None, "must still degrade gracefully, not raise"
assert "HTTP 400" in output
assert "not transient network unavailability" in output or "schema bug" in output
print("A real HTTP 400 (query/schema error) is flagged distinctly in the diagnostic output.")


# --- a transient network failure (timeout) reads differently from a schema error ---

result, output = _run_with_captured_output(requests.exceptions.Timeout("timed out"))
assert result is None
assert "HTTP 400" not in output
assert "not transient network unavailability" not in output
assert "network request failed" in output
print("A timeout is reported as ordinary network failure, not mistaken for a schema/query bug.")


# --- a non-network bug (e.g. a malformed response causing a KeyError) is also distinguished ---

result, output = _run_with_captured_output(KeyError("kwfact"))
assert result is None
assert "unexpected failure, not a network error" in output
assert "KeyError" in output
print("A non-network exception (a real bug) is flagged as such, not reported as a network issue.")

print("\nAll road corridor fetch-diagnostics checks passed.")
