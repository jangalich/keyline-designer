"""
session_cache.py

The two-tier in-process cache behind an interactive design session, and
the rebuild path that makes evicting from either tier safe.

THE ONE RULE THIS MODULE EXISTS TO UPHOLD. The Design Document
(design_document.py, persisted by document_store.py) is authoritative and
durable. NOTHING here is authoritative. Any entry, or the whole cache,
can be dropped at any moment and reconstructed from the document plus the
fetch cache. A miss must therefore degrade to SLOWER, never to data loss
and never to a different answer -- which is why rebuild_session_context()
and session creation both go through the SAME build_session_context()
below rather than each assembling a context their own way. Equivalence
between a fresh entry and a rebuilt one is structural here, not a
property somebody has to remember to preserve.

The corollary, and the test to apply to any future addition: if a value
cannot be recomputed from the document, it does not belong in this cache
-- it belongs in the document. Nothing stored here is a user decision.

--- TIER 1: THE FETCH CACHE (cross-session) -------------------------

Memoizes parcel_data.fetch_parcel_data() -- Layer 1, the whole raw
network-backed layer set -- keyed by the boundary itself, so two sessions
drawn on the same land pay for one fetch, and a rebuild after a session
eviction is COMPUTE, not network. Keyed by boundary rather than by
session precisely so it survives the session it was fetched for.

The ParcelData it hands out is SHARED, not copied: the DEM array alone is
hundreds of thousands of float32 cells, and every consumer in this
pipeline already treats Layer 1 as read-only input. Treat a ParcelData
from this cache as immutable -- mutating one mutates it for every session
on that boundary.

--- TIER 2: THE SESSION CACHE (per-session) -------------------------

Holds the heavy native objects for one live session: the ParcelData
reference (not a copy -- see above) plus the terrain warm-up products,
which are real Python objects (numpy masks, shapely geometries) that
cannot be persisted to JSON and would cost a full terrain pass to
recompute on every request. Bounded two ways, because the two failure
modes are different: an LRU cap bounds MEMORY (many concurrent sessions),
and an idle timeout bounds STALENESS/LEAKAGE (a browser tab closed and
never returned to). Both evictions are safe by the rule above.

--- THE TERRAIN WARM-UP --------------------------------------------

Three products, all computable the moment a boundary exists because none
of them depends on any user decision:

    valleys          valley_delineation.delineate_valleys(dem)
    keypoints        keypoint_detection.detect_keypoints(...)
    exclusion_zones  exclusion_zones.identify_exclusion_zones(...)

The overrides forwarded into each are exactly the ones pipeline_context.
build_pipeline_context() forwards, and for the same reason: an override
left None is that consumer's "fetch/derive it yourself" value, so
forwarding matters for call counts, not just for tidiness. In particular
detect_keypoints() gets this session's already-computed valleys=, so
delineate_valleys() runs ONCE per warm-up, not twice; and
identify_exclusion_zones() gets the already-fetched canopy_height= and
the already-built road_exclusion_union_utm=, so neither layer is fetched
a second time. test_session_manager.py asserts these at exact counts, the
discipline test_pipeline_context.py established.

exclusion_zones is here rather than deferred because it is the
eligibility mask the frontend renders as its ineligible-area overlay: it
has to exist before the first step can generate anything. pipeline_
context.py's own docstring identifies it as the FIRST Layer 2
computation, depending only on Layer 1 products -- which is exactly what
makes it warm-up-able with no user input in hand.

NOT part of the warm-up: pipeline_context._attach_keypoint_feature_
relationships(). It depends on committed production areas and the
selected water zone, neither of which exists at creation, so it runs as a
post-commit hook in a later branch. Keypoints produced here therefore
carry NO 'feature_relationships' key, deliberately -- asserted in
test_session_manager.py so a future warm-up change can't quietly add it.

--- KNOWN RESIDUAL FETCH (reported, not patched) --------------------

identify_exclusion_zones() self-computes its hydric-soil gate via
production_area._fetch_disqualifying_soil_union(), which issues its own
two SDA queries (component rows, then those mukeys' geometries). It
accepts a pre-derived `disqualifying_soil_union_utm=` override but has NO
raw-row override -- there is no soil_components=/soil_geometries=
parameter -- so ParcelData's ALREADY-FETCHED soil_components and
soil_geometries cannot be handed to it. Deriving the union out here
instead would mean reimplementing that helper outside the module that
owns it, and would let this module's exclusion result drift from
build_pipeline_context()'s. So the warm-up pays those two SDA queries,
exactly as build_pipeline_context() does today. This is a gap in
exclusion_zones.py's override surface, reported rather than patched --
closing it is that module's own work, on its own branch.

Consequence to know when reading the rebuild test: a rebuild makes ZERO
Layer 1 fetches (the fetch cache serves fetch_parcel_data() outright),
but it does re-run this Layer-2-internal soil fetch, because re-running
the warm-up is what a rebuild IS.

In-process only. Both tiers are plain in-memory structures guarded by
locks; nothing here survives a restart, and nothing here needs to.
"""

import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

import exclusion_zones
import farm_roads_data
import keypoint_detection
import parcel_data
import valley_delineation

# --- boundary hashing ------------------------------------------------

# Decimal places every boundary coordinate is rounded to before it is
# hashed. WHY 7:
#
#   * 7 decimal degrees is ~1.1 cm of latitude. It is BELOW anything that
#     could distinguish two real parcels -- no survey, no drawn boundary,
#     and certainly no 5 m DEM cell (dem_data.DEFAULT_RESOLUTION_METERS)
#     resolves a centimetre -- so two boundaries that collide at this
#     precision are the same ground, and serving one ParcelData for both
#     is caching, not substitution. That is the property that has to
#     hold: the cap on how WRONG a hit can be.
#
#   * It is also the precision the real inputs already carry. The drawn
#     boundary in generate_full_report.py is given to 7 decimals, which
#     is what a browser map hands back. Rounding there is lossless for
#     genuine input while still collapsing the sub-centimetre noise this
#     normalization exists for: float64 arithmetic, a WGS84 -> UTM ->
#     WGS84 round trip, JSON re-serialization by a frontend.
#
#   * Coarser (5 dp, ~1.1 m) would start merging boundaries that are
#     genuinely a metre apart -- two adjacent parcels sharing a fence
#     line, which is exactly the case that must NOT share a fetch. Finer
#     (9 dp) sits below the float noise floor, so it would miss the cache
#     on precisely the reprojection round-trips the rounding is for.
#
# A pair of values straddling a rounding boundary still lands in
# different buckets; that is inherent to fixed-precision bucketing and it
# is the SAFE direction of failure -- a cache miss, i.e. slower, never a
# wrong ParcelData.
BOUNDARY_HASH_PRECISION = 7


def _normalized_coordinate(value) -> float:
    rounded = round(float(value), BOUNDARY_HASH_PRECISION)
    # round() preserves the sign of a negative zero, and -0.0 formats
    # differently from 0.0 while comparing equal -- collapse it so a
    # boundary that crosses the equator or the prime meridian cannot key
    # two ways.
    return 0.0 if rounded == 0.0 else rounded


def normalized_boundary(boundary) -> list:
    """
    The boundary as it is hashed: every coordinate rounded to
    BOUNDARY_HASH_PRECISION, and a duplicated closing vertex dropped.

    Dropping the closing duplicate is a real normalization, not a
    convenience: soil_data.coordinates_to_wkt_polygon() closes an open
    ring itself, and shapely's Polygon() does the same, so an explicitly
    closed ring and its implicitly closed twin are the SAME input to
    every fetch in fetch_parcel_data(). They must not key differently.
    The duplicate is detected AFTER rounding, so a closing vertex that
    differs from the first only by float noise is caught too.
    """
    points = []
    for point in boundary:
        lon, lat = point[0], point[1]
        points.append((_normalized_coordinate(lon), _normalized_coordinate(lat)))
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    return points


def boundary_cache_key(boundary) -> str:
    """
    A deterministic hex digest of the normalized boundary. Stable across
    processes and runs (sha256 over a canonical text form, NOT Python's
    salted hash()), so it can back a shared cache later without changing
    meaning.
    """
    canonical = ";".join(
        f"{lon:.{BOUNDARY_HASH_PRECISION}f},{lat:.{BOUNDARY_HASH_PRECISION}f}"
        for lon, lat in normalized_boundary(boundary)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- tier 1: the fetch cache -----------------------------------------

DEFAULT_FETCH_CACHE_SIZE = 32


class FetchCache:
    """
    Memoizes fetch_parcel_data() by boundary. In-process, LRU-capped,
    thread-safe.

    CONCURRENCY SHAPE. The structure lock is never held across a fetch --
    a fetch is a long network call, and holding it would serialize every
    session in the process behind one parcel's SDA queries. Instead each
    KEY gets its own in-flight lock, so two threads asking for the SAME
    boundary collapse into one fetch (the second waits, then finds the
    filled entry) while threads on DIFFERENT boundaries proceed in
    parallel.

    FAILURES ARE NOT CACHED, deliberately -- parcel_data.py's own
    contract is "raises, uncached". A retried boundary gets a real retry,
    and a session is never created against a remembered failure.
    """

    def __init__(
        self,
        max_entries: int = DEFAULT_FETCH_CACHE_SIZE,
        fetch_function: Optional[Callable] = None,
    ):
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._max_entries = max_entries
        # None means "resolve parcel_data.fetch_parcel_data at call time"
        # rather than binding it at construction -- a module-level default
        # cache built at import time must still see a test's patch of that
        # attribute.
        self._fetch_function = fetch_function
        self._entries = OrderedDict()  # key -> ParcelData
        self._lock = threading.Lock()
        self._inflight = {}  # key -> (threading.Lock, waiter count)
        self.hits = 0
        self.misses = 0

    def _fetch(self, boundary):
        if self._fetch_function is not None:
            return self._fetch_function(boundary)
        return parcel_data.fetch_parcel_data(boundary)

    def _peek(self, key):
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key], True
            return None, False

    def _key_lock(self, key) -> threading.Lock:
        with self._lock:
            lock, waiters = self._inflight.get(key, (None, 0))
            if lock is None:
                lock = threading.Lock()
            self._inflight[key] = (lock, waiters + 1)
            return lock

    def _release_key_lock(self, key) -> None:
        with self._lock:
            lock, waiters = self._inflight[key]
            if waiters <= 1:
                del self._inflight[key]
            else:
                self._inflight[key] = (lock, waiters - 1)

    def get_or_fetch(self, boundary):
        """
        The ParcelData for this boundary: cached if present, fetched
        exactly once if not. A fetch failure propagates uncaught and
        leaves nothing cached.
        """
        key = boundary_cache_key(boundary)

        value, found = self._peek(key)
        if found:
            with self._lock:
                self.hits += 1
            return value

        key_lock = self._key_lock(key)
        try:
            with key_lock:
                # Re-check: another thread may have filled this key while
                # we waited on its lock. That thread's fetch is ours too.
                value, found = self._peek(key)
                if found:
                    with self._lock:
                        self.hits += 1
                    return value

                with self._lock:
                    self.misses += 1
                value = self._fetch(boundary)

                with self._lock:
                    self._entries[key] = value
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
                return value
        finally:
            self._release_key_lock(key)

    def contains(self, boundary) -> bool:
        with self._lock:
            return boundary_cache_key(boundary) in self._entries

    def discard(self, boundary) -> bool:
        """Drop one boundary's entry. Returns whether it was there."""
        with self._lock:
            return self._entries.pop(boundary_cache_key(boundary), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# --- the session context (tier 2's payload) --------------------------


@dataclass
class SessionContext:
    """
    One live session's heavy native objects. Every field is DERIVED --
    reconstructible from the Design Document's boundary plus a
    ParcelData -- which is what makes dropping this entry safe.

    parcel_data is the SHARED object from the fetch cache, not a copy;
    see this module's docstring. Do not mutate it.
    """

    session_id: str
    boundary: list
    parcel_data: object
    # Layer-1-derived, computed above the exclusion call exactly as
    # build_pipeline_context() computes it, and kept here because the
    # exclusion gate, the water step and the production step all read the
    # same union -- a later branch that drops it would re-fetch roads.
    # Legitimately None on a parcel with no mapped road nearby (the
    # common, clean case, not an error).
    existing_roads: object
    valleys: list
    keypoints: list
    exclusion_zones: dict
    # THE SLOT FOR LATER BRANCHES, deliberately empty here. Per-step
    # generate proposals (step_id -> proposal) live here once the Step
    # Registry exists: they are heavy, native, and regenerable from the
    # document, so they belong in this tier and not in the document,
    # which records only decisions. Warm-up creates none, and rebuild
    # restores none -- replaying committed steps is the registry's job.
    step_proposals: dict = field(default_factory=dict)

    @property
    def dem(self) -> dict:
        return self.parcel_data.dem

    @property
    def boundary_polygon_utm(self):
        return self.parcel_data.boundary_polygon_utm


# --- the terrain warm-up ---------------------------------------------


def run_terrain_warm_up(boundary_coordinates: list, parcel: object) -> dict:
    """
    The three creation-time terrain products, plus the road exclusion
    union they are built over. Pure compute against `parcel` (a
    ParcelData) apart from the one Layer-2-internal soil fetch documented
    in this module's KNOWN RESIDUAL FETCH section.

    Every override forwarded below is the one build_pipeline_context()
    forwards, at the same value, in the same order -- the ordering is a
    real dependency chain (valleys feed keypoints; the road union feeds
    the exclusion gate), not a preference.
    """
    # ON A FETCH-CACHE HIT these come from the boundary that was fetched
    # FIRST, while boundary_coordinates is this session's own. The two can
    # differ, but only below BOUNDARY_HASH_PRECISION -- ~1.1 cm, which is
    # the guarantee that constant exists to make. The polygon is what every
    # mask below is actually computed against, so a sub-centimetre
    # disagreement with the coordinate list (which only reaches the WKT
    # soil query) cannot move a 5 m DEM cell into or out of any gate.
    dem = parcel.dem
    boundary_polygon_utm = parcel.boundary_polygon_utm

    valleys = valley_delineation.delineate_valleys(dem)

    # valleys= is forwarded so delineate_valleys() is NOT run a second
    # time inside detect_keypoints() -- it self-computes valleys when the
    # override is absent. dem/boundary_polygon_utm come straight off
    # ParcelData, which already derived the UTM polygon.
    keypoints = keypoint_detection.detect_keypoints(
        dem, boundary_polygon_utm, valleys=valleys
    )

    # farm_roads= is ParcelData's own already-fetched road rows, so this
    # reprojects and buffers rather than re-fetching. None here is a real
    # answer ("no mapped roads nearby"), and identify_exclusion_zones()
    # reuses a supplied None as exactly that rather than re-fetching --
    # see its OVERRIDES docstring.
    existing_roads = farm_roads_data.get_road_exclusion_union_utm(
        boundary_coordinates, dem, farm_roads=parcel.farm_roads
    )

    exclusion_result = exclusion_zones.identify_exclusion_zones(
        boundary_coordinates,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        canopy_height=parcel.canopy_height,
        road_exclusion_union_utm=existing_roads,
    )

    return {
        "valleys": valleys,
        "keypoints": keypoints,
        "existing_roads": existing_roads,
        "exclusion_zones": exclusion_result,
    }


def build_session_context(
    session_id: str,
    boundary_coordinates: list,
    fetch_cache: "FetchCache",
) -> SessionContext:
    """
    THE single constructor for a SessionContext. Session creation calls
    it; rebuild_session_context() calls it. That is not a style choice --
    it is what makes "a rebuilt entry is equivalent to the original" true
    by construction rather than by careful maintenance of two paths.

    Layer 1 arrives through the fetch cache, so this is one network fetch
    on a cold boundary and none on a warm one.
    """
    parcel = fetch_cache.get_or_fetch(boundary_coordinates)
    warm = run_terrain_warm_up(boundary_coordinates, parcel)
    return SessionContext(
        session_id=session_id,
        boundary=[list(point) for point in boundary_coordinates],
        parcel_data=parcel,
        existing_roads=warm["existing_roads"],
        valleys=warm["valleys"],
        keypoints=warm["keypoints"],
        exclusion_zones=warm["exclusion_zones"],
    )


def rebuild_session_context(document: dict, fetch_cache: "FetchCache") -> SessionContext:
    """
    Reconstruct a dropped cache entry from the authoritative document.

    Reads the boundary and session_id off the document -- the document is
    the source of truth, so a rebuild cannot drift from what the session
    actually is. On a boundary still in the fetch cache this makes ZERO
    network calls for Layer 1; on an evicted one it re-fetches, which is
    slower and still correct.

    SCOPE. This restores what session creation produces, which is all a
    session HAS at this point. Replaying committed steps into
    step_proposals belongs to the Step Registry branch; it will extend
    this function rather than replace it.
    """
    return build_session_context(
        document["session_id"], document["boundary"], fetch_cache
    )


# --- tier 2: the session cache ---------------------------------------

DEFAULT_MAX_LIVE_SESSIONS = 8
DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60


@dataclass
class _Entry:
    """
    Cache bookkeeping, kept OFF SessionContext on purpose: the context is
    a pure derived product, and an equivalence check between a fresh and
    a rebuilt context must not have to step around a timestamp that is
    guaranteed to differ.
    """

    context: SessionContext
    last_used: float


class SessionCache:
    """
    Per-session storage for SessionContext objects. LRU-capped AND
    idle-expiring, thread-safe.

    A MISS IS NOT AN ERROR. get() returns None and the caller rebuilds
    (session_manager.get_session_context()). Nothing in this class
    raises for a missing session -- a missing session_id in the STORE
    is a different thing entirely, and that is where the raise belongs.

    The clock is injectable (time_function) so idle expiry is testable
    without sleeping. It defaults to time.monotonic, which cannot jump
    backwards on an NTP correction the way time.time can.
    """

    def __init__(
        self,
        max_sessions: int = DEFAULT_MAX_LIVE_SESSIONS,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        time_function: Callable[[], float] = time.monotonic,
    ):
        if max_sessions < 1:
            raise ValueError(f"max_sessions must be >= 1, got {max_sessions}")
        if idle_timeout_seconds <= 0:
            raise ValueError(
                f"idle_timeout_seconds must be > 0, got {idle_timeout_seconds}"
            )
        self._max_sessions = max_sessions
        self._idle_timeout = idle_timeout_seconds
        self._now = time_function
        self._entries = OrderedDict()  # session_id -> _Entry
        self._lock = threading.Lock()

    def _expire_locked(self) -> list:
        cutoff = self._now() - self._idle_timeout
        expired = [
            session_id
            for session_id, entry in self._entries.items()
            if entry.last_used <= cutoff
        ]
        for session_id in expired:
            del self._entries[session_id]
        return expired

    def get(self, session_id: str) -> Optional[SessionContext]:
        """The live context, or None. Never raises for a miss."""
        with self._lock:
            self._expire_locked()
            entry = self._entries.get(session_id)
            if entry is None:
                return None
            entry.last_used = self._now()
            self._entries.move_to_end(session_id)
            return entry.context

    def put(self, context: SessionContext) -> None:
        with self._lock:
            self._expire_locked()
            self._entries[context.session_id] = _Entry(
                context=context, last_used=self._now()
            )
            self._entries.move_to_end(context.session_id)
            while len(self._entries) > self._max_sessions:
                # Oldest by last use. Safe unconditionally: everything in
                # here is rebuildable from the document.
                self._entries.popitem(last=False)

    def discard(self, session_id: str) -> bool:
        """Drop one session's entry. Returns whether it was there."""
        with self._lock:
            return self._entries.pop(session_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def session_ids(self) -> list:
        """Live session ids, least recently used first. Expires first."""
        with self._lock:
            self._expire_locked()
            return list(self._entries)

    def __contains__(self, session_id: str) -> bool:
        with self._lock:
            self._expire_locked()
            return session_id in self._entries

    def __len__(self) -> int:
        with self._lock:
            self._expire_locked()
            return len(self._entries)


# The process-wide defaults. session_manager.py's entry points fall back
# to these when a caller supplies none, and take explicit caches when a
# caller (a test, or a future multi-tenant arrangement) has its own.
DEFAULT_FETCH_CACHE = FetchCache()
DEFAULT_SESSION_CACHE = SessionCache()
