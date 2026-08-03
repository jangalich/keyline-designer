"""
test_road_corridor_fetch_diagnostics.py

Offline (no-network, mocked) checks for road_corridors.py's
_log_fetch_failure() — the diagnostic that distinguishes a genuine
query/schema error (an HTTP 4xx status, which will fail identically on
every future run) from ordinary transient network flakiness (a timeout
or connection error) or an unexpected non-network bug, rather than
folding all three into one indistinguishable "network request failed"
message. Exercised here via _fetch_floodplain_hydric_union()'s own NHD
stream/water-body fetch — the only fetch-then-degrade function left in
this module now that anchoring/named-road lookup has been removed
entirely (an earlier version of this test used the now-removed
_fetch_existing_road_features_utm() as its vehicle; any fetch-then-degrade
function works equally well, since they all delegate their error
reporting to the same _log_fetch_failure() helper).

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
from shapely.geometry import box

import road_corridors

DEM = {"crs": "EPSG:32617"}  # _fetch_floodplain_hydric_union doesn't touch the array itself
BOUNDARY_COORDINATES = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
BOUNDARY_POLYGON_UTM = box(500000, 4500000, 500100, 4500100)


def _run_with_captured_output(mock_exception):
    buffer = io.StringIO()
    # get_soil_data_for_polygon is mocked to a trivial success (no hydric
    # mukeys) purely so this stays a real, deterministic offline test of
    # the NHD fetch's own failure path -- not because it's the thing under
    # test here; it would otherwise attempt a real (sandbox-unreachable)
    # SSURGO network call of its own after the mocked NHD failure.
    with patch.object(road_corridors, "get_water_features_for_boundary", side_effect=mock_exception), \
         patch.object(road_corridors, "get_soil_data_for_polygon", return_value=[]):
        with redirect_stdout(buffer):
            result = road_corridors._fetch_floodplain_hydric_union(
                BOUNDARY_COORDINATES, DEM, [], BOUNDARY_POLYGON_UTM
            )
    return result, buffer.getvalue()


# --- a genuine schema/query error (HTTP 400) is distinguished from network flakiness ---

response_400 = requests.Response()
response_400.status_code = 400
http_error = requests.exceptions.HTTPError("400 Client Error", response=response_400)

(union, is_fallback), output = _run_with_captured_output(http_error)
assert union is None and is_fallback is True, "must still degrade gracefully (to the fallback path), not raise"
assert "HTTP 400" in output
assert "not transient network unavailability" in output or "schema bug" in output
print("A real HTTP 400 (query/schema error) is flagged distinctly in the diagnostic output.")


# --- a transient network failure (timeout) reads differently from a schema error ---

(union, is_fallback), output = _run_with_captured_output(requests.exceptions.Timeout("timed out"))
assert union is None and is_fallback is True
assert "HTTP 400" not in output
assert "not transient network unavailability" not in output
assert "network request failed" in output
print("A timeout is reported as ordinary network failure, not mistaken for a schema/query bug.")


# --- a non-network bug (e.g. a malformed response causing a KeyError) is also distinguished ---

(union, is_fallback), output = _run_with_captured_output(KeyError("kwfact"))
assert union is None and is_fallback is True
assert "unexpected failure, not a network error" in output
assert "KeyError" in output
print("A non-network exception (a real bug) is flagged as such, not reported as a network issue.")

print("\nAll road corridor fetch-diagnostics checks passed.")
