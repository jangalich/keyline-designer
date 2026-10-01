"""
test_host_breaker.py

THE PER-HOST CIRCUIT BREAKER, proved at the transport boundary.

The failure it answers (host_breaker.py): hydro.nationalmap.gov sheds
load in episodes lasting minutes, four fetch layers sit on that one host,
and each used to burn its own full 30/60/90-second retry budget in
sequence against a host the previous layer had just proved down. What
this file holds to account:

    1  An exhausted budget OPENS the circuit: the next call to the same
       host fails instantly, with zero transport calls and zero attempts.
    2  A single failed attempt does NOT open it -- only a whole budget.
    3  The error is a requests ConnectionError, so the existing policies
       (hard-fail at Layer 1, degrade at the report layer) handle it
       unchanged -- proved through report_data's own degrade branch.
    4  A success CLOSES the circuit; after the cooldown it is ajar and a
       clean answer closes it for good.
    5  Hosts are independent: an open NHD circuit refuses nothing on any
       other host.
    6  Under the offline harness the breaker is DISABLED, so the suite's
       attempt counts cannot depend on file order.

The harness is installed (every fetch below must refuse instantly when it
leaks), and the breaker is then re-enabled explicitly per section -- this
file is the one place that runs it for real.
"""

import requests

import offline_harness

offline_harness.install()

import fetch_attempts  # noqa: E402
import host_breaker  # noqa: E402
import hydrology_data  # noqa: E402
import nhdplus_data  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402
from unittest.mock import patch as mock_patch  # noqa: E402

BOUNDARY = [list(p) for p in REAL_BOUNDARY]
NHD_HOST = "hydro.nationalmap.gov"


class Down:
    """A transport that always raises -- the shedding gateway."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        raise requests.exceptions.ConnectionError("induced: host down")


class Up:
    """A transport that always answers empty -- the recovered gateway."""

    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1

        class _R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"features": []}

        return _R()


print("=" * 72)
print("test_host_breaker.py -- an exhausted budget opens the host's circuit; the next budget is not burned")
print("=" * 72)

# The harness disabled the breaker on install (section 6 proves it); every
# section that exercises it turns it on and starts from closed circuits.
host_breaker.set_enabled(True)
host_breaker.reset()

# ======================================================================
# 1. An exhausted budget opens the circuit; the next call costs nothing
# ======================================================================
down = Down()
with mock_patch.object(hydrology_data, "requests", type("M", (), {
        "get": down.get, "exceptions": requests.exceptions})()):
    fetch_attempts.clear()
    try:
        hydrology_data.get_water_features_for_boundary(BOUNDARY)
        raise AssertionError("a host that never answers must raise")
    except requests.exceptions.ConnectionError as exc:
        assert "induced" in str(exc), exc
    first_calls = down.calls
    assert first_calls == 3, "the FIRST layer spends its whole budget: 3 attempts"

    try:
        hydrology_data.get_water_features_for_boundary(BOUNDARY)
        raise AssertionError("the open circuit must refuse the second fetch")
    except host_breaker.HostCircuitOpenError as exc:
        assert NHD_HOST in str(exc), exc
    assert down.calls == first_calls, "the second fetch made ZERO transport calls"
print("1. flowlines exhausted their 3-attempt budget and opened the circuit; the next water fetch "
      "was refused locally with zero transport calls.")

# And the OTHER module on the same host is refused too, with zero calls.
nhdplus_down = Down()
with mock_patch.object(nhdplus_data, "requests", type("M", (), {
        "get": nhdplus_down.get, "exceptions": requests.exceptions})()):
    try:
        nhdplus_data.get_flowline_attributes_for_boundary(BOUNDARY)
        raise AssertionError("the open circuit must refuse nhdplus_data as well")
    except host_breaker.HostCircuitOpenError:
        pass
assert nhdplus_down.calls == 0, "same host, same circuit: nhdplus_data made no transport call"
print("   nhdplus_data, same host: refused by the same circuit, zero transport calls.")

# ======================================================================
# 2. One failed attempt does not open it
# ======================================================================
host_breaker.reset()
host_breaker.record_success("https://hydro.nationalmap.gov/x")  # closed
host_breaker.check("https://hydro.nationalmap.gov/x")  # does not raise
# A single attempt's failure is the retry loop's business, not the
# breaker's: nothing below record_failure touches the state.
host_breaker.check("https://hydro.nationalmap.gov/x")
print("2. a closed circuit stays closed through check() -- only record_failure (a whole exhausted "
      "budget) opens it.")

# ======================================================================
# 3. The open-circuit error reaches the degrade policy unchanged
# ======================================================================
host_breaker.reset()
host_breaker.record_failure("https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer/3/query")
err = None
try:
    nhdplus_data.get_flowline_attributes_for_boundary(BOUNDARY)
except requests.exceptions.RequestException as exc:
    err = exc
assert isinstance(err, host_breaker.HostCircuitOpenError)
assert isinstance(err, requests.exceptions.ConnectionError), \
    "the open circuit is a ConnectionError, so every existing except clause catches it"
print("3. HostCircuitOpenError IS a requests ConnectionError: the report layer's degrade branch and "
      "Layer 1's hard-fail contract both see the failure type they were written for.")

# ======================================================================
# 4. A success closes it; after the cooldown it is ajar
# ======================================================================
host_breaker.reset()
url = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query"
host_breaker.record_failure(url)
try:
    host_breaker.check(url)
    raise AssertionError("open circuit must refuse")
except host_breaker.HostCircuitOpenError:
    pass
# The cooldown elapsing is simulated by rewinding the opened-at clock
# rather than sleeping COOLDOWN_SECONDS.
with host_breaker._LOCK:
    host_breaker._OPENED_AT[NHD_HOST] -= host_breaker.COOLDOWN_SECONDS + 1
host_breaker.check(url)  # ajar: flows again
up = Up()
with mock_patch.object(hydrology_data, "requests", type("M", (), {
        "get": up.get, "exceptions": requests.exceptions})()):
    answer = hydrology_data.get_water_features_for_boundary(BOUNDARY)
assert answer == {"streams": [], "water_bodies": [], "points": []}
host_breaker.check(url)  # the success closed it
with host_breaker._LOCK:
    assert NHD_HOST not in host_breaker._OPENED_AT, "the clean answer deleted the entry"
print(f"4. open for COOLDOWN_SECONDS ({host_breaker.COOLDOWN_SECONDS:.0f}s), ajar after it, and the "
      "first clean answer closed the circuit for good.")

# ======================================================================
# 5. Hosts are independent
# ======================================================================
host_breaker.reset()
host_breaker.record_failure("https://hydro.nationalmap.gov/arcgis/x")
host_breaker.check("https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest")  # not refused
print("5. an open NHD circuit refuses nothing on sdmdataaccess -- the breaker is per host.")

# ======================================================================
# 6. Disabled under the offline harness
# ======================================================================
host_breaker.set_enabled(False)
host_breaker.record_failure("https://hydro.nationalmap.gov/arcgis/x")
host_breaker.check("https://hydro.nationalmap.gov/arcgis/x")  # no raise: disabled
assert not host_breaker.enabled()
# ... which is the state offline_harness.install() put every OTHER test
# file in, so their attempt counts cannot depend on file order; and
# set_enabled(False) cleared the state, so re-enabling starts closed.
host_breaker.set_enabled(True)
host_breaker.check("https://hydro.nationalmap.gov/arcgis/x")
host_breaker.set_enabled(False)  # leave it as the harness set it
print("6. disabled (the offline harness's state), record_failure and check are no-ops, and "
      "re-enabling starts with every circuit closed.")

print("\nAll host-breaker checks passed.")
print(offline_harness.summary())
