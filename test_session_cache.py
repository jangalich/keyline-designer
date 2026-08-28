"""
test_session_cache.py

Offline checks for session_cache.py's two tiers, run as:

    python test_session_cache.py

Everything here is MECHANISM: hashing, eviction, locking, lifetime. The
warm-up and rebuild paths are exercised against real terrain code in
test_session_manager.py, which is where the call-count discipline lives.
No network, no KSOP computation -- the fetch function is injected, so a
"ParcelData" here is a stand-in object.

Sections:
  1. BOUNDARY KEY -- precision, stability, closing-vertex normalization,
     and the collisions that must NOT happen.
  2. FETCH CACHE -- memoization, LRU eviction, failures uncached.
  3. FETCH CACHE CONCURRENCY -- one fetch for two threads on the same
     boundary; two boundaries genuinely fetch in parallel.
  4. SESSION CACHE -- miss returns None, LRU cap, recency, idle timeout
     on an injected clock.
  5. SESSION CONTEXT -- the derived-only shape, and the empty slot later
     branches fill.
"""

import threading

import session_cache
from session_cache import (
    BOUNDARY_HASH_PRECISION,
    FetchCache,
    SessionCache,
    SessionContext,
    boundary_cache_key,
    normalized_boundary,
)

# The real drawn property boundary from generate_full_report.py --
# 5614 N Montour Rd, Gibsonia, PA. Implicitly closed (its last vertex is
# ~0.9 m from its first, not on it), which is the shape a browser map
# hands back and the shape the rest of this pipeline already accepts.
REAL_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]


class _StubParcel:
    """Stands in for a ParcelData -- identity is all these tests read."""

    def __init__(self, label):
        self.label = label

    def __repr__(self):
        return f"_StubParcel({self.label!r})"


# --- 1. BOUNDARY KEY -------------------------------------------------

assert BOUNDARY_HASH_PRECISION == 7, (
    "the documented precision is 7 decimal places (~1.1 cm); changing it "
    "changes which boundaries share a fetch, so it changes a contract"
)

key = boundary_cache_key(REAL_BOUNDARY)
assert isinstance(key, str) and len(key) == 64, "sha256 hex digest expected"
assert boundary_cache_key(REAL_BOUNDARY) == key, "the key must be deterministic"
# Not Python's salted hash(): the same digest in any process.
assert key == boundary_cache_key(list(REAL_BOUNDARY)), "key must not depend on identity"

# Float noise BELOW the precision must land on the same key -- the whole
# reason the rounding exists. 1e-9 degrees is ~0.1 mm.
noisy = [(lon + 1e-9, lat - 1e-9) for lon, lat in REAL_BOUNDARY]
assert boundary_cache_key(noisy) == key, (
    "boundaries differing only by sub-precision float noise must share a key"
)

# A difference ABOVE the precision must not. 1e-5 degrees is ~1 m -- two
# parcels sharing a fence line, which must never share a ParcelData.
one_metre_off = [(lon + 1e-5, lat) for lon, lat in REAL_BOUNDARY]
assert boundary_cache_key(one_metre_off) != key, (
    "a ~1 m difference is a different parcel and must key differently"
)

# An explicit closing duplicate is normalized away: soil_data.
# coordinates_to_wkt_polygon() and shapely both close an open ring
# themselves, so the two forms are the SAME fetch input.
explicitly_closed = list(REAL_BOUNDARY) + [REAL_BOUNDARY[0]]
assert boundary_cache_key(explicitly_closed) == key, (
    "an explicitly closed ring and its implicitly closed twin are one fetch"
)
assert len(normalized_boundary(explicitly_closed)) == len(REAL_BOUNDARY)
# ...and a closing vertex that differs only by noise is caught too, since
# the duplicate is detected AFTER rounding.
noisy_close = list(REAL_BOUNDARY) + [
    (REAL_BOUNDARY[0][0] + 1e-10, REAL_BOUNDARY[0][1])
]
assert boundary_cache_key(noisy_close) == key

# Negative zero must not key two ways.
assert boundary_cache_key([(-0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]) == boundary_cache_key(
    [(0.0, -0.0), (1.0, 0.0), (1.0, 1.0)]
)

# Order is part of the key: a different vertex order is a different
# argument to every fetch in fetch_parcel_data(), so it is not silently
# treated as the same land.
assert boundary_cache_key(list(reversed(REAL_BOUNDARY))) != key

print(
    f"BOUNDARY KEY: deterministic sha256 at {BOUNDARY_HASH_PRECISION} dp; "
    "sub-precision noise and a duplicate closing vertex collapse to one key; "
    "a ~1 m offset and a reversed ring do not."
)


# --- 2. FETCH CACHE: memoization, LRU, uncached failures -------------

calls = []


def _counting_fetch(boundary):
    calls.append(boundary_cache_key(boundary))
    return _StubParcel(len(calls))


cache = FetchCache(max_entries=2, fetch_function=_counting_fetch)

first = cache.get_or_fetch(REAL_BOUNDARY)
assert len(calls) == 1, f"first fetch must go through, got {len(calls)} calls"
second = cache.get_or_fetch(REAL_BOUNDARY)
assert len(calls) == 1, f"a hit must not re-fetch, got {len(calls)} calls"
assert second is first, "the cache hands back the SAME shared object, not a copy"
assert cache.hits == 1 and cache.misses == 1

# Sub-precision noise hits the same entry (the key test above, now
# through the cache itself).
assert cache.get_or_fetch(noisy) is first
assert len(calls) == 1, "a noise-only difference must not trigger a second fetch"

# LRU eviction at the cap. Touch `first` so it is the most recent, then
# add two more: `first` survives the first insert and falls out on the
# second, and the one never touched goes first.
boundary_b = [(-80.10, 40.10), (-80.09, 40.10), (-80.09, 40.11), (-80.10, 40.11)]
boundary_c = [(-80.20, 40.20), (-80.19, 40.20), (-80.19, 40.21), (-80.20, 40.21)]
cache.get_or_fetch(boundary_b)
assert len(cache) == 2
cache.get_or_fetch(REAL_BOUNDARY)  # refresh recency on `first`
cache.get_or_fetch(boundary_c)  # evicts boundary_b, the least recent
assert len(cache) == 2, f"cap of 2 must hold, got {len(cache)}"
assert cache.contains(REAL_BOUNDARY), "the recently used entry must survive"
assert not cache.contains(boundary_b), "the least recently used entry is evicted"
assert len(calls) == 3, f"exactly 3 real fetches so far, got {len(calls)}"

# An evicted boundary re-fetches -- slower, never wrong.
cache.get_or_fetch(boundary_b)
assert len(calls) == 4

# A FAILED fetch is not cached: parcel_data.py's own "raises, uncached"
# posture. A retry must be a real retry.
failures = {"count": 0}


def _failing_once(boundary):
    failures["count"] += 1
    if failures["count"] == 1:
        raise RuntimeError("simulated Layer 1 outage")
    return _StubParcel("recovered")


flaky = FetchCache(max_entries=4, fetch_function=_failing_once)
try:
    flaky.get_or_fetch(REAL_BOUNDARY)
    raise AssertionError("the fetch failure must propagate uncaught")
except RuntimeError as error:
    assert "simulated Layer 1 outage" in str(error)
assert len(flaky) == 0, "a failed fetch must leave NOTHING cached"
assert not flaky.contains(REAL_BOUNDARY)
recovered = flaky.get_or_fetch(REAL_BOUNDARY)
assert recovered.label == "recovered" and failures["count"] == 2, (
    "the retry after a failure must reach the fetch function again"
)

print(
    "FETCH CACHE: memoized by boundary, shared object handed back, LRU at the "
    "cap, evicted entries re-fetch, failures never cached."
)


# --- 3. FETCH CACHE CONCURRENCY --------------------------------------

# Two threads, ONE boundary: exactly one fetch. The second thread waits
# on the key's in-flight lock and then finds the filled entry.
same_boundary_calls = []
released = threading.Event()


def _slow_fetch(boundary):
    same_boundary_calls.append(boundary_cache_key(boundary))
    released.wait(timeout=10)
    return _StubParcel("shared")


single_flight = FetchCache(max_entries=4, fetch_function=_slow_fetch)
results = {}


def _racer(name):
    results[name] = single_flight.get_or_fetch(REAL_BOUNDARY)


racers = [threading.Thread(target=_racer, args=(n,)) for n in ("a", "b")]
for thread in racers:
    thread.start()
released.set()
for thread in racers:
    thread.join(timeout=30)
    assert not thread.is_alive(), "single-flight fetch deadlocked"
assert len(same_boundary_calls) == 1, (
    f"two threads on ONE boundary must collapse into ONE fetch, got "
    f"{len(same_boundary_calls)}"
)
assert results["a"] is results["b"], "both threads get the same shared ParcelData"

# Two threads, TWO boundaries: they must genuinely overlap. The barrier
# only clears if both fetches are in flight at once -- if the structure
# lock were held across a fetch, this times out.
barrier = threading.Barrier(2, timeout=15)
parallel_errors = []


def _barrier_fetch(boundary):
    try:
        barrier.wait()
    except threading.BrokenBarrierError as error:  # pragma: no cover
        parallel_errors.append(error)
    return _StubParcel("parallel")


parallel_cache = FetchCache(max_entries=4, fetch_function=_barrier_fetch)
movers = [
    threading.Thread(target=parallel_cache.get_or_fetch, args=(b,))
    for b in (boundary_b, boundary_c)
]
for thread in movers:
    thread.start()
for thread in movers:
    thread.join(timeout=30)
    assert not thread.is_alive(), "parallel fetches on different boundaries stalled"
assert parallel_errors == [], (
    "fetches on different boundaries must run concurrently -- the structure "
    "lock is never held across a fetch"
)

print(
    "FETCH CACHE CONCURRENCY: two threads on one boundary share a single "
    "fetch; two boundaries fetch in parallel (barrier cleared)."
)


# --- 4. SESSION CACHE: cap, recency, idle timeout --------------------

clock = {"now": 1000.0}


def _fake_clock():
    return clock["now"]


def _context(session_id):
    return SessionContext(
        session_id=session_id,
        boundary=[list(p) for p in REAL_BOUNDARY],
        parcel_data=_StubParcel(session_id),
        existing_roads=None,
        valleys=[],
        keypoints=[],
        exclusion_zones={},
    )


sessions = SessionCache(
    max_sessions=2, idle_timeout_seconds=600.0, time_function=_fake_clock
)

# A MISS IS NOT AN ERROR -- it returns None so the caller can rebuild.
assert sessions.get("never-created") is None, "a miss must return None, not raise"

sessions.put(_context("s1"))
clock["now"] += 10.0
sessions.put(_context("s2"))
assert len(sessions) == 2 and sessions.get("s1") is not None

# LRU CAP. s1 was just read, so s2 is now the least recent and falls out.
clock["now"] += 10.0
sessions.put(_context("s3"))
assert len(sessions) == 2, f"cap of 2 must hold, got {len(sessions)}"
assert sessions.get("s1") is not None, "the recently used session survives"
assert sessions.get("s2") is None, "the least recently used session is evicted"
assert sessions.get("s3") is not None
assert "s3" in sessions

# IDLE TIMEOUT, on the injected clock -- no sleeping. s1 is refreshed
# just before the jump; s3 is not, so only s3 expires.
clock["now"] += 100.0
sessions.get("s1")
clock["now"] += 550.0  # s1 idle 550s, s3 idle 650s, timeout 600s
assert sessions.get("s3") is None, "a session idle past the timeout is dropped"
assert sessions.get("s1") is not None, "a session inside the timeout is kept"
assert sessions.session_ids() == ["s1"]

# The timeout applies to every reader, including len() and `in`.
clock["now"] += 10_000.0
assert len(sessions) == 0 and "s1" not in sessions
assert sessions.get("s1") is None

# discard()/clear() are explicit drops -- both safe, both rebuildable.
sessions.put(_context("s4"))
assert sessions.discard("s4") is True
assert sessions.discard("s4") is False, "discarding a missing session is not an error"
sessions.put(_context("s5"))
sessions.clear()
assert len(sessions) == 0

# Construction guards.
for bad in ({"max_sessions": 0}, {"idle_timeout_seconds": 0}):
    try:
        SessionCache(**bad)
        raise AssertionError(f"SessionCache({bad}) should be refused")
    except ValueError:
        pass
try:
    FetchCache(max_entries=0)
    raise AssertionError("FetchCache(max_entries=0) should be refused")
except ValueError:
    pass

print(
    "SESSION CACHE: miss returns None; LRU cap evicts the least recent; idle "
    "timeout expires on an injected clock for get/len/contains."
)


# --- 5. SESSION CONTEXT ----------------------------------------------

context = _context("shape-check")
assert context.step_proposals == {}, (
    "warm-up creates no proposals -- this slot stays empty until the Step "
    "Registry branch fills it"
)
# Two contexts must not share the mutable slot (a dataclass default_factory
# thing that would be a real bug if it regressed).
assert context.step_proposals is not _context("other").step_proposals

# Nothing on the context is a user decision: every field is derived from
# the boundary plus a ParcelData, which is what makes eviction safe.
assert set(vars(context)) == {
    "session_id",
    "boundary",
    "parcel_data",
    "existing_roads",
    "valleys",
    "keypoints",
    "exclusion_zones",
    "step_proposals",
}, (
    "a new SessionContext field needs a decision first: if it cannot be "
    "rebuilt from the document it belongs in the document, not here"
)

# dem/boundary_polygon_utm read through to the shared ParcelData rather
# than being stored twice.
parcel = _StubParcel("derived")
parcel.dem = {"crs": "EPSG:32617"}
parcel.boundary_polygon_utm = "polygon-sentinel"
context.parcel_data = parcel
assert context.dem is parcel.dem
assert context.boundary_polygon_utm is parcel.boundary_polygon_utm

# The module-level defaults exist and are the right tiers.
assert isinstance(session_cache.DEFAULT_FETCH_CACHE, FetchCache)
assert isinstance(session_cache.DEFAULT_SESSION_CACHE, SessionCache)

print(
    "SESSION CONTEXT: derived-only fields, empty per-session proposal slot, "
    "dem/boundary_polygon_utm read through to the shared ParcelData."
)

print("\nAll session_cache checks passed.")
