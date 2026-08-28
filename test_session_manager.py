"""
test_session_manager.py

Integration checks for session_manager.py and session_cache.py's warm-up
and rebuild paths, run as:

    python test_session_manager.py

REAL COORDINATES, REAL TERRAIN CODE. The boundary is the actual drawn
property from generate_full_report.py -- 5614 N Montour Rd, Gibsonia, PA
(~13.23 acres, UTM 17N) -- not a synthetic rectangle. valley_delineation.
delineate_valleys(), keypoint_detection.detect_keypoints() and
exclusion_zones.identify_exclusion_zones() all RUN, for real, over that
boundary: they are wrapped (wraps=) to be counted, never replaced.

What IS mocked is the network, and only the network: parcel_data.
fetch_parcel_data() returns a ParcelData built over a DEM fixture for
this boundary's own UTM extent, and the two SDA queries the exclusion
module USED to reach on its own are canned -- they are now asserted at
ZERO on the warm-up path (the fixture ParcelData carries those same rows,
and the warm-up forwards them), the canned values remaining so section 10
can still exercise the self-fetch fallback directly. That is what makes
the call-count assertions below meaningful -- the same discipline test_pipeline_context.
py applies, asserted at EXACT counts rather than upper bounds, because
one more of any of them means something is re-fetching.

Sections:
  1. CREATION -- succeeds on real coordinates; document persists; cache
     populated; the three warm-up products are real.
  2. CALL COUNT -- exactly ONE fetch_parcel_data() per creation, and zero
     second fetches of canopy/roads/SSURGO rows (the forwarded
     overrides).
  3. WARM-UP -- delineate_valleys() runs exactly ONCE across a creation,
     summed over BOTH import bindings; keypoints carry no
     'feature_relationships'.
  4. FETCH CACHE HIT -- two sessions on one boundary, ONE fetch total.
  5. KEY STABILITY -- a boundary differing only by sub-precision float
     noise hits the same entry.
  6. HARD FAIL -- a failed fetch creates NO session: nothing persisted,
     nothing cached.
  7. REBUILD -- drop the cache entry, ask again: it rebuilds with ZERO
     network calls of ANY kind (Layer 1 served by the fetch cache, and no
     Layer-2-internal SDA query either), into an entry equivalent to the
     original.
  8. EVICTION -- LRU cap and idle timeout, each resolved by rebuild.
  9. VALIDATION -- open ring, too few vertices, self-intersecting,
     absurd area.
 10. SELF-FETCH FALLBACK -- with the raw-row soil overrides omitted, the
     hydric gate still fetches for itself and produces a BIT-IDENTICAL
     result. The zeros in section 2 only mean something if the None path
     is still a real, working one.
"""

import tempfile
from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

import canopy_height_data
import exclusion_zones
import farm_roads_data
import keypoint_detection
import parcel_data
import production_area
import session_cache
import session_manager
import valley_delineation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore, SessionNotFoundError
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- the real property -----------------------------------------------

# The user's real, drawn property boundary, copied from generate_full_
# report.py's __main__ block. Implicitly closed: its last vertex sits
# ~0.9 m from its first, which is what a browser map hands back and what
# soil_data.coordinates_to_wkt_polygon() already closes for itself.
REAL_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

_mean_lon = sum(lon for lon, _ in REAL_BOUNDARY) / len(REAL_BOUNDARY)
_mean_lat = sum(lat for _, lat in REAL_BOUNDARY) / len(REAL_BOUNDARY)
CRS = f"EPSG:{_utm_epsg_for_lonlat(_mean_lon, _mean_lat)}"
_xs, _ys = warp_transform(
    "EPSG:4326",
    CRS,
    [lon for lon, _ in REAL_BOUNDARY],
    [lat for _, lat in REAL_BOUNDARY],
)
BOUNDARY_POLYGON_UTM = Polygon(zip(_xs, _ys))
PARCEL_ACRES = BOUNDARY_POLYGON_UTM.area / SQUARE_METERS_PER_ACRE

# --- the DEM fixture: this boundary's own extent ---------------------
#
# Real coordinates, real UTM zone, real 5 m grid at dem_data's own
# DEFAULT_RESOLUTION_METERS and DEFAULT_BUFFER_METERS -- so the grid this
# terrain code runs over is the grid a live fetch would have produced for
# this parcel. Only the ELEVATIONS are a fixture, because there is no
# network here.
#
# The landform is one V-shaped valley draining north to south through the
# parcel centroid, with a deliberate slope BREAK at the centroid row:
# ~30% above it, ~3% below. That break is what a keypoint IS, so this
# fixture produces a real one, inside the parcel and clear of
# keypoint_detection's own boundary margin. The cross-valley walls are
# gentle (5%) on purpose: walls steeper than the channel pull the traced
# stem up a side wall at the head, which moves the two-segment split into
# the buffer ring and off the parcel (observed while building this).
RESOLUTION_METERS = 5.0
BUFFER_METERS = 100.0
_minx, _miny, _maxx, _maxy = BOUNDARY_POLYGON_UTM.bounds
ORIGIN_X = _minx - BUFFER_METERS
ORIGIN_Y = _maxy + BUFFER_METERS
COLS = int(np.ceil((_maxx - _minx + 2 * BUFFER_METERS) / RESOLUTION_METERS))
ROWS = int(np.ceil((_maxy - _miny + 2 * BUFFER_METERS) / RESOLUTION_METERS))
_centroid = BOUNDARY_POLYGON_UTM.centroid
CHANNEL_COL = int(round((_centroid.x - ORIGIN_X) / RESOLUTION_METERS))
KNEE_ROW = int(round((ORIGIN_Y - _centroid.y) / RESOLUTION_METERS))

WALL_RISE_PER_COL_M = 0.25  # 5% cross slope
STEEP_DROP_PER_ROW_M = 1.5  # 30% above the knee
GENTLE_DROP_PER_ROW_M = 0.15  # 3% below the knee


def _build_dem() -> dict:
    array = np.zeros((ROWS, COLS), dtype=np.float32)
    for row in range(ROWS):
        if row < KNEE_ROW:
            drop = row * STEEP_DROP_PER_ROW_M
        else:
            drop = KNEE_ROW * STEEP_DROP_PER_ROW_M + (
                row - KNEE_ROW
            ) * GENTLE_DROP_PER_ROW_M
        for col in range(COLS):
            array[row, col] = (
                1000.0 + abs(col - CHANNEL_COL) * WALL_RISE_PER_COL_M - drop
            )
    return {
        "array": array,
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _build_canopy(dem: dict) -> dict:
    """A stand of trees BELOW the knee, in ground the slope gate passes --
    a canopy patch in the steep half would be excluded by slope first and
    the canopy layer would read 0.0 acres, proving nothing."""
    hag = np.zeros((ROWS, COLS), dtype=np.float32)
    hag[KNEE_ROW + 8 : KNEE_ROW + 22, CHANNEL_COL - 14 : CHANNEL_COL - 2] = 14.0
    return {
        "array": hag,
        "resolution_meters": dem["resolution_meters"],
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
        "source_item_id": "fixture-hag",
    }


# One hydric map unit inside the gentle half, so the hydric gate has real
# content, and one road crossing it, so the road gate does too.
HYDRIC_COMPONENTS = [
    {
        "mukey": "111111",
        "comppct_r": "85",
        "hydricrating": "Yes",
        "compname": "Fixture silt loam",
    }
]
HYDRIC_GEOMETRIES = {
    "111111": {
        "type": "Polygon",
        "coordinates": [
            [
                [-79.9830, 40.6434],
                [-79.9822, 40.6434],
                [-79.9822, 40.6439],
                [-79.9830, 40.6439],
                [-79.9830, 40.6434],
            ]
        ],
    }
}
FIXTURE_ROADS = [
    {
        "name": "Fixture Rd",
        "geometry": {
            "type": "LineString",
            "coordinates": [[-79.9840, 40.6436], [-79.9805, 40.6436]],
        },
    }
]


def _build_parcel_data(_boundary=None) -> ParcelData:
    """A fresh ParcelData every call -- so object IDENTITY across two
    creations proves the fetch cache served the second one, rather than
    two fetches happening to return equal values."""
    dem = _build_dem()
    return ParcelData(
        dem=dem,
        boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
        soil_components=HYDRIC_COMPONENTS,
        farmland_classification=[],
        erosion_factor=[],
        saturated_hydraulic_conductivity=[],
        soil_geometries=HYDRIC_GEOMETRIES,
        water_features={"features": []},
        farm_roads=FIXTURE_ROADS,
        climate_summary={},
        elevation_grid=[],
        canopy_height=_build_canopy(dem),
        imagery_summary={},
        irradiance={"status": "ok"},
    )


# --- the mock harness ------------------------------------------------


class Harness:
    """
    Every network boundary mocked, every real computation wrapped and
    counted. Used as a context manager so each section gets fresh counts.
    """

    def __init__(self, fetch_side_effect=None):
        self._stack = ExitStack()
        self._fetch_side_effect = fetch_side_effect or _build_parcel_data

    def __enter__(self):
        patch = self._stack.enter_context

        # THE Layer 1 boundary. This is the call the fetch cache exists to
        # avoid, and the one every count below is really about.
        self.fetch_parcel_data = patch(
            mock_patch.object(
                parcel_data, "fetch_parcel_data", side_effect=self._fetch_side_effect
            )
        )

        # The two SDA queries identify_exclusion_zones()'s hydric gate
        # used to reach on its own. Both must now stay at ZERO on every
        # warm-up: the warm-up forwards ParcelData's soil_components/
        # soil_geometries, so a nonzero count means an override stopped
        # being passed -- see session_cache.py's THE RESIDUAL SOIL FETCH:
        # CLOSED section. They still return the fixture rows so the
        # fall-back path stays exercisable (section 10 calls it directly).
        self.soil_components = patch(
            mock_patch.object(
                production_area,
                "get_soil_data_for_polygon",
                return_value=HYDRIC_COMPONENTS,
            )
        )
        self.soil_geometries = patch(
            mock_patch.object(
                production_area,
                "get_soil_geometries_for_polygon",
                return_value=HYDRIC_GEOMETRIES,
            )
        )

        # Layers whose overrides the warm-up forwards. Each of these must
        # stay at ZERO: a nonzero count means an override stopped being
        # passed and that layer is being fetched a second time.
        self.canopy_refetch = patch(
            mock_patch.object(
                production_area, "get_canopy_height_for_boundary", return_value=None
            )
        )
        self.canopy_module_refetch = patch(
            mock_patch.object(
                canopy_height_data, "get_canopy_height_for_boundary", return_value=None
            )
        )
        self.roads_refetch = patch(
            mock_patch.object(
                farm_roads_data, "get_farm_roads_for_boundary", return_value=[]
            )
        )
        self.roads_helper_refetch = patch(
            mock_patch.object(
                production_area, "_fetch_road_exclusion_union_utm", return_value=None
            )
        )

        # Real computation, wrapped so it RUNS and is counted.
        #
        # delineate_valleys is counted at BOTH import bindings: session_
        # cache reaches it through the valley_delineation module, while
        # keypoint_detection did `from valley_delineation import
        # delineate_valleys` and would self-compute through ITS binding if
        # valleys= were not forwarded. Summing the two is the only honest
        # total -- exactly how test_pipeline_context.py sums a function's
        # separate bindings.
        self.delineate_valleys = patch(
            mock_patch.object(
                valley_delineation,
                "delineate_valleys",
                wraps=valley_delineation.delineate_valleys,
            )
        )
        self.keypoint_delineate_valleys = patch(
            mock_patch.object(
                keypoint_detection,
                "delineate_valleys",
                wraps=keypoint_detection.delineate_valleys,
            )
        )
        self.detect_keypoints = patch(
            mock_patch.object(
                keypoint_detection,
                "detect_keypoints",
                wraps=keypoint_detection.detect_keypoints,
            )
        )
        self.identify_exclusion_zones = patch(
            mock_patch.object(
                exclusion_zones,
                "identify_exclusion_zones",
                wraps=exclusion_zones.identify_exclusion_zones,
            )
        )
        return self

    def __exit__(self, *exc_info):
        self._stack.close()
        return False

    @property
    def total_delineate_valleys_calls(self) -> int:
        return (
            self.delineate_valleys.call_count
            + self.keypoint_delineate_valleys.call_count
        )

    @property
    def total_soil_queries(self) -> int:
        return self.soil_components.call_count + self.soil_geometries.call_count


def _fresh_caches(max_sessions=8, idle_timeout_seconds=1800.0, time_function=None):
    kwargs = {
        "max_sessions": max_sessions,
        "idle_timeout_seconds": idle_timeout_seconds,
    }
    if time_function is not None:
        kwargs["time_function"] = time_function
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(**kwargs)


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="session_manager_test_"))


# --- equivalence ------------------------------------------------------


def equivalent(left, right, path="context"):
    """
    Deep structural equality across the shapes a SessionContext actually
    holds: numpy arrays, shapely geometries, dicts, sequences, scalars.
    Returns None on a match, or a string naming the first difference --
    a bare `==` would raise on the arrays and silently pass on geometry
    identity, so neither is usable here.
    """
    if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
        if not isinstance(left, np.ndarray) or not isinstance(right, np.ndarray):
            return f"{path}: array vs non-array"
        if left.shape != right.shape:
            return f"{path}: array shape {left.shape} != {right.shape}"
        if not np.array_equal(left, right, equal_nan=True):
            return f"{path}: array values differ"
        return None
    if isinstance(left, BaseGeometry) or isinstance(right, BaseGeometry):
        if not isinstance(left, BaseGeometry) or not isinstance(right, BaseGeometry):
            return f"{path}: geometry vs non-geometry"
        if left.is_empty and right.is_empty:
            return None
        if not left.equals(right):
            return f"{path}: geometries are not equal"
        return None
    if isinstance(left, dict):
        if not isinstance(right, dict):
            return f"{path}: dict vs {type(right).__name__}"
        if set(left) != set(right):
            return f"{path}: keys differ ({sorted(set(left) ^ set(right))})"
        for key in left:
            difference = equivalent(left[key], right[key], f"{path}[{key!r}]")
            if difference:
                return difference
        return None
    if isinstance(left, (list, tuple)):
        if not isinstance(right, (list, tuple)) or len(left) != len(right):
            return f"{path}: sequence shape differs"
        for index, (a, b) in enumerate(zip(left, right)):
            difference = equivalent(a, b, f"{path}[{index}]")
            if difference:
                return difference
        return None
    if isinstance(left, float) and isinstance(right, float):
        if left != right and not (left != left and right != right):  # NaN == NaN
            return f"{path}: {left!r} != {right!r}"
        return None
    if left != right:
        return f"{path}: {left!r} != {right!r}"
    return None


# --- 1. CREATION on real coordinates ---------------------------------

print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} "
    f"vertices, {PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at "
    f"{RESOLUTION_METERS:.0f} m.\n"
)

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()

with Harness() as h:
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )

    session_id = document["session_id"]
    assert document["document_revision"] == 0
    assert len(document["boundary"]) == len(REAL_BOUNDARY)
    assert all(step["status"] == "not_started" for step in document["steps"].values())

    # The document PERSISTED -- read it back through the store, not from
    # the returned value.
    stored = store.get(session_id)
    assert stored == document, "the persisted document must match the returned one"
    assert store.list_sessions() == [session_id]

    # The cache is POPULATED, with real warm-up products.
    context = sessions.get(session_id)
    assert context is not None, "creation must leave a live cache entry"
    assert context.session_id == session_id
    assert context.parcel_data is fetch_cache.get_or_fetch(REAL_BOUNDARY)

    assert len(context.valleys) == 1, (
        f"the fixture landform is one valley through the parcel, got "
        f"{len(context.valleys)}"
    )
    assert context.valleys[0]["branches_utm"], "the valley must carry real geometry"
    assert len(context.keypoints) == 1, (
        f"the fixture's slope break is a real keypoint inside the parcel, got "
        f"{len(context.keypoints)}"
    )
    assert context.keypoints[0]["on_parcel"] is True

    layers = context.exclusion_zones["layers"]
    assert set(layers) == {"canopy", "slope", "hydric", "roads", "setback"}
    assert all(layer["data_available"] for layer in layers.values()), (
        "every gate must report as genuinely evaluated, not skipped"
    )
    acres = {name: layer["acres"] for name, layer in layers.items()}
    assert acres["slope"] > 0 and acres["canopy"] > 0 and acres["hydric"] > 0, (
        f"the eligibility mask must have real content on this fixture, got {acres}"
    )
    assert not context.exclusion_zones["eligible_union_utm"].is_empty, (
        "the frontend's ineligible-area overlay needs a real eligible complement"
    )
    assert isinstance(context.existing_roads, BaseGeometry)

print(
    f"1. CREATION: session {session_id[:12]}... created on the real boundary; "
    f"document persisted (revision 0, 6 steps not_started); cache populated with "
    f"{len(context.valleys)} valley, {len(context.keypoints)} keypoint, and a "
    f"5-gate exclusion result ({acres})."
)


# --- 2. CALL COUNT: exactly one fetch, and no second fetch of anything ---

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()

with Harness() as h:
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )

    assert h.fetch_parcel_data.call_count == 1, (
        f"fetch_parcel_data() must be called EXACTLY ONCE per creation, got "
        f"{h.fetch_parcel_data.call_count} -- more than one means Layer 1 is "
        f"being fetched twice for one session"
    )
    fetch_count = h.fetch_parcel_data.call_count

    # It was called with THIS boundary, not some re-derived one.
    (called_boundary,) = h.fetch_parcel_data.call_args_list[0].args
    assert [tuple(p) for p in called_boundary] == [
        tuple(p) for p in REAL_BOUNDARY
    ], "the fetch must receive the document's own boundary"

    # The forwarded overrides: each of these is a layer ParcelData already
    # holds, so a nonzero count is a second fetch of data in hand.
    assert h.canopy_refetch.call_count == 0 and h.canopy_module_refetch.call_count == 0, (
        f"canopy_height is forwarded from ParcelData, so no consumer may fetch "
        f"it again: got {h.canopy_refetch.call_count} + "
        f"{h.canopy_module_refetch.call_count}"
    )
    assert h.roads_refetch.call_count == 0, (
        f"farm_roads is forwarded from ParcelData, so get_farm_roads_for_boundary "
        f"must not run: got {h.roads_refetch.call_count}"
    )
    assert h.roads_helper_refetch.call_count == 0, (
        f"the road exclusion union is built once and passed into "
        f"identify_exclusion_zones(), which reuses it (including a real None): "
        f"got {h.roads_helper_refetch.call_count} second builds"
    )

    assert h.identify_exclusion_zones.call_count == 1, (
        f"identify_exclusion_zones() must run exactly once per creation, got "
        f"{h.identify_exclusion_zones.call_count}"
    )

    # THE GAP THAT USED TO BE REPORTED HERE, NOW CLOSED. This assertion
    # read == 2 for as long as identify_exclusion_zones() had only a pre-
    # derived disqualifying_soil_union_utm= override and no raw-row one,
    # so ParcelData's already-fetched SSURGO rows could not reach its
    # hydric gate and it self-served two SDA queries per warm-up. _fetch_
    # disqualifying_soil_union() now takes soil_components=/soil_
    # geometries=, identify_exclusion_zones() passes them through, and
    # run_terrain_warm_up() forwards ParcelData's own two fields -- so
    # this is 0, deliberately, exactly as the old comment here said it
    # should become. It is now the same kind of assertion as the canopy
    # and roads ones above: a nonzero count means an override stopped
    # being forwarded.
    assert h.total_soil_queries == 0, (
        f"soil_components/soil_geometries are forwarded from ParcelData, so "
        f"the hydric gate must issue NO SDA query: got "
        f"{h.total_soil_queries} (components "
        f"{h.soil_components.call_count} + geometries "
        f"{h.soil_geometries.call_count})"
    )
    soil_queries = h.total_soil_queries

print(
    f"2. CALL COUNT: fetch_parcel_data() == {fetch_count} (exactly one per "
    f"creation). Second fetches of forwarded layers: canopy == "
    f"{h.canopy_refetch.call_count + h.canopy_module_refetch.call_count}, "
    f"farm roads == {h.roads_refetch.call_count}, road union == "
    f"{h.roads_helper_refetch.call_count}, SDA soil queries == "
    f"{soil_queries} (was 2 before the raw-row override existed)."
)


# --- 3. WARM-UP: delineate_valleys runs exactly ONCE ------------------

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()

with Harness() as h:
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )

    assert h.detect_keypoints.call_count == 1, (
        f"detect_keypoints() must run exactly once per warm-up, got "
        f"{h.detect_keypoints.call_count}"
    )

    # THE ASSERTION THIS SECTION EXISTS FOR. One delineation across the
    # whole creation, counted at both bindings -- and specifically ZERO at
    # keypoint_detection's own, which is the binding a missing valleys=
    # forward would light up.
    assert h.keypoint_delineate_valleys.call_count == 0, (
        f"detect_keypoints() self-computes valleys when the override is absent; "
        f"this session's valleys ARE forwarded, so its binding must stay at 0, "
        f"got {h.keypoint_delineate_valleys.call_count}"
    )
    assert h.total_delineate_valleys_calls == 1, (
        f"delineate_valleys() must run EXACTLY ONCE across a creation "
        f"(session_cache's own call, forwarded onward), got "
        f"{h.total_delineate_valleys_calls} across both import bindings"
    )
    valleys_calls = h.total_delineate_valleys_calls

    # It ran against ParcelData's own DEM, not a re-derived one.
    context = sessions.get(document["session_id"])
    assert h.delineate_valleys.call_args_list[0].args[0] is context.parcel_data.dem

    # detect_keypoints() got THIS session's valleys, by identity -- the
    # forward is a real pass-through of the one delineation above, not a
    # separately-computed list that happens to be equal.
    assert h.detect_keypoints.call_args_list[0].kwargs["valleys"] is context.valleys

    # _attach_keypoint_feature_relationships() is NOT part of the warm-up:
    # it needs committed production areas and a selected water zone,
    # neither of which exists at creation. Warm-up keypoints are bare.
    for keypoint in context.keypoints:
        assert "feature_relationships" not in keypoint, (
            "warm-up must produce keypoints WITHOUT feature_relationships -- "
            "that pass is a post-commit hook in a later branch"
        )

print(
    f"3. WARM-UP: delineate_valleys() == {valleys_calls} across BOTH bindings "
    f"(session_cache {h.delineate_valleys.call_count}, keypoint_detection "
    f"{h.keypoint_delineate_valleys.call_count} -- the forwarded valleys= "
    f"suppressed the second run); detect_keypoints() == "
    f"{h.detect_keypoints.call_count}; keypoints carry no feature_relationships."
)


# --- 4. FETCH CACHE HIT: two sessions, one fetch ---------------------

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()

with Harness() as h:
    first_doc = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert h.fetch_parcel_data.call_count == 1

    second_doc = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert h.fetch_parcel_data.call_count == 1, (
        f"a second session on the SAME land must be served by the fetch cache: "
        f"expected 1 fetch total, got {h.fetch_parcel_data.call_count}"
    )
    shared_fetches = h.fetch_parcel_data.call_count

    assert first_doc["session_id"] != second_doc["session_id"], "distinct sessions"
    first_context = sessions.get(first_doc["session_id"])
    second_context = sessions.get(second_doc["session_id"])
    assert first_context.parcel_data is second_context.parcel_data, (
        "both sessions share ONE ParcelData -- the object, not a copy"
    )
    # ...but their warm-up products are their own, freshly computed.
    assert first_context is not second_context
    assert first_context.valleys is not second_context.valleys
    assert h.total_delineate_valleys_calls == 2, (
        f"each session runs its OWN warm-up; only the fetch is shared. Expected "
        f"2 delineations for 2 sessions, got {h.total_delineate_valleys_calls}"
    )
    assert len(store.list_sessions()) == 2

print(
    f"4. FETCH CACHE HIT: 2 sessions on one boundary -> fetch_parcel_data() == "
    f"{shared_fetches} total; both hold the same ParcelData object; each ran "
    f"its own warm-up ({h.total_delineate_valleys_calls} delineations)."
)


# --- 5. KEY STABILITY under float noise ------------------------------

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()

# 1e-9 degrees is ~0.1 mm -- two orders of magnitude below the 7-decimal
# hashing precision, i.e. exactly the reprojection/serialization noise the
# rounding exists to absorb.
NOISY_BOUNDARY = [(lon + 1e-9, lat - 1e-9) for lon, lat in REAL_BOUNDARY]
assert NOISY_BOUNDARY != REAL_BOUNDARY, "the noise must be real, not a no-op"

with Harness() as h:
    session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )
    session_manager.create_session(
        NOISY_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert h.fetch_parcel_data.call_count == 1, (
        f"boundaries differing only below the hashing precision must hit ONE "
        f"cache entry: expected 1 fetch, got {h.fetch_parcel_data.call_count}"
    )
    noisy_fetches = h.fetch_parcel_data.call_count
    assert len(fetch_cache) == 1, f"one entry expected, got {len(fetch_cache)}"

    # And the boundary that is genuinely different does NOT share it.
    one_metre_off = [(lon + 1e-5, lat) for lon, lat in REAL_BOUNDARY]
    session_manager.create_session(
        one_metre_off, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert h.fetch_parcel_data.call_count == 2, (
        f"a ~1 m difference is a different parcel and must fetch its own Layer "
        f"1: expected 2 fetches, got {h.fetch_parcel_data.call_count}"
    )

print(
    f"5. KEY STABILITY: a 1e-9 deg (~0.1 mm) perturbation shares one entry "
    f"(fetch_parcel_data() == {noisy_fetches}); a 1e-5 deg (~1 m) offset does "
    f"not (== {h.fetch_parcel_data.call_count})."
)


# --- 6. HARD FAIL: a failed fetch creates NO session -----------------

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()


def _outage(_boundary):
    raise parcel_data.ParcelDataIncompleteError(
        "get_canopy_height_for_boundary() found no LiDAR HAG coverage"
    )


with Harness(fetch_side_effect=_outage) as h:
    try:
        session_manager.create_session(
            REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
        )
        raise AssertionError(
            "a hard-failed Layer 1 fetch must propagate, not create a degraded "
            "session"
        )
    except parcel_data.ParcelDataIncompleteError as error:
        assert "no LiDAR HAG coverage" in str(error), "the real failure propagates"

    assert h.fetch_parcel_data.call_count == 1, "it tried exactly once"
    assert store.list_sessions() == [], (
        f"NO document may be persisted for a failed fetch, found "
        f"{store.list_sessions()}"
    )
    assert len(sessions) == 0, "NO cache entry may exist for a failed fetch"
    assert len(fetch_cache) == 0, "a failed fetch is not itself cached"

    # The warm-up never started, so nothing downstream ran either.
    assert h.total_delineate_valleys_calls == 0
    assert h.identify_exclusion_zones.call_count == 0

    # A retry after the outage is a REAL retry, and succeeds.
    h.fetch_parcel_data.side_effect = _build_parcel_data
    recovered = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert store.list_sessions() == [recovered["session_id"]]
    assert h.fetch_parcel_data.call_count == 2

print(
    "6. HARD FAIL: ParcelDataIncompleteError propagated; 0 documents persisted, "
    "0 cache entries, 0 warm-up runs; the retry afterwards created a real session."
)


# --- 7. REBUILD: the one that makes eviction safe --------------------

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()

with Harness() as h:
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
    )
    session_id = document["session_id"]
    original = sessions.get(session_id)
    assert original is not None
    fetches_after_creation = h.fetch_parcel_data.call_count
    assert fetches_after_creation == 1
    soil_after_creation = h.total_soil_queries

    # DROP the entry -- the eviction this whole section exists to make safe.
    assert sessions.discard(session_id) is True
    assert sessions.get(session_id) is None, "the entry is genuinely gone"

    rebuilt = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=sessions
    )

    # ZERO Layer 1 network calls during the rebuild: the fetch cache
    # served it outright. This is the assertion that makes evicting a
    # session cheap rather than a re-download of the whole parcel.
    rebuild_fetches = h.fetch_parcel_data.call_count - fetches_after_creation
    assert rebuild_fetches == 0, (
        f"a rebuild must make ZERO fetch_parcel_data() calls while the fetch "
        f"cache holds this boundary, got {rebuild_fetches}"
    )

    # ...AND zero network calls of any OTHER kind. This half used to be
    # impossible to assert: the warm-up's hydric gate self-served two SDA
    # queries every time it ran, and re-running the warm-up is what a
    # rebuild IS -- so the zero-network claim was split ("zero Layer 1
    # fetches, but two Layer-2-internal soil queries"). With the raw-row
    # override forwarded, the claim is UNCONDITIONAL: a rebuild is
    # compute, full stop, which is the property pipeline_context.py's
    # architecture states for it.
    rebuild_soil_queries = h.total_soil_queries - soil_after_creation
    assert rebuild_soil_queries == 0, (
        f"a rebuild must reach the network ZERO times, SDA included: got "
        f"{rebuild_soil_queries} soil queries during the rebuild"
    )
    assert h.total_soil_queries == 0, (
        f"...and neither the creation nor the rebuild may have made one: "
        f"{h.total_soil_queries} total"
    )
    assert h.canopy_refetch.call_count == 0 and h.roads_refetch.call_count == 0, (
        "no canopy or farm-road fetch during creation or rebuild either"
    )
    assert rebuilt.parcel_data is original.parcel_data, (
        "the rebuild reuses the SAME cached ParcelData object"
    )

    # EQUIVALENT to the original, product by product.
    assert rebuilt is not original, "a genuinely new context object"
    assert rebuilt.session_id == original.session_id
    for field in ("boundary", "valleys", "keypoints", "existing_roads",
                  "exclusion_zones", "step_proposals"):
        difference = equivalent(
            getattr(original, field), getattr(rebuilt, field), f"context.{field}"
        )
        assert difference is None, f"rebuilt entry differs from the original: {difference}"

    # ...and it is back in the cache, so the next read is a plain hit.
    assert sessions.get(session_id) is rebuilt
    before = h.fetch_parcel_data.call_count
    again = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert again is rebuilt and h.fetch_parcel_data.call_count == before

    # The rebuild read the BOUNDARY from the document, not from anywhere
    # ambient -- so it cannot drift from what the session actually is.
    assert [tuple(p) for p in rebuilt.boundary] == [
        tuple(p) for p in store.get(session_id)["boundary"]
    ]

    # An unknown session is a real error, not a cache miss to paper over.
    try:
        session_manager.get_session_context(
            "no-such-session", store, fetch_cache=fetch_cache, cache=sessions
        )
        raise AssertionError("an unknown session_id must raise")
    except SessionNotFoundError:
        pass

print(
    f"7. REBUILD: cache entry dropped, get_session_context() rebuilt it with "
    f"{rebuild_fetches} Layer 1 fetches and {rebuild_soil_queries} SDA queries "
    f"-- ZERO network calls of any kind (was: zero Layer 1, two SDA); rebuilt "
    f"context equivalent to the original across boundary, valleys, keypoints, "
    f"existing_roads, exclusion_zones and step_proposals; unknown session still "
    f"raises SessionNotFoundError."
)


# --- 8. EVICTION: LRU cap and idle timeout, each healed by rebuild ----

store = _fresh_store()
clock = {"now": 5000.0}
fetch_cache, sessions = _fresh_caches(
    max_sessions=2, idle_timeout_seconds=900.0, time_function=lambda: clock["now"]
)

with Harness() as h:
    docs = []
    for _ in range(3):
        docs.append(
            session_manager.create_session(
                REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=sessions
            )
        )
        clock["now"] += 1.0

    # The cap held, and the oldest fell out.
    assert len(sessions) == 2, f"cap of 2 must hold, got {len(sessions)}"
    evicted_id = docs[0]["session_id"]
    assert sessions.get(evicted_id) is None, "the least recently used session evicted"
    assert len(store.list_sessions()) == 3, (
        "eviction touches the CACHE only -- all three documents are still stored"
    )

    fetches_before = h.fetch_parcel_data.call_count
    healed = session_manager.get_session_context(
        evicted_id, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert healed.session_id == evicted_id
    assert h.fetch_parcel_data.call_count == fetches_before, (
        "rebuilding an LRU-evicted session must not touch the network"
    )
    cap_evicted_rebuild_fetches = h.fetch_parcel_data.call_count - fetches_before

    # IDLE TIMEOUT, on the injected clock.
    live_id = healed.session_id
    clock["now"] += 901.0
    assert sessions.get(live_id) is None, (
        "a session idle past idle_timeout_seconds must be dropped"
    )
    fetches_before = h.fetch_parcel_data.call_count
    revived = session_manager.get_session_context(
        live_id, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert revived.session_id == live_id
    idle_rebuild_fetches = h.fetch_parcel_data.call_count - fetches_before
    assert idle_rebuild_fetches == 0
    difference = equivalent(healed.exclusion_zones, revived.exclusion_zones)
    assert difference is None, f"a timed-out session rebuilds identically: {difference}"

    total_fetches = h.fetch_parcel_data.call_count

print(
    f"8. EVICTION: LRU cap held at 2 of 3 sessions (all 3 documents still "
    f"stored); the evicted session rebuilt with {cap_evicted_rebuild_fetches} "
    f"network calls; a session idle past the timeout rebuilt with "
    f"{idle_rebuild_fetches}; {total_fetches} fetch_parcel_data() call total "
    f"across the whole section."
)


# --- 9. VALIDATION: rejects, before any network call -----------------

store = _fresh_store()
fetch_cache, sessions = _fresh_caches()

_EDGE_START, _EDGE_END = REAL_BOUNDARY[0], REAL_BOUNDARY[3]


def _along_edge(fraction: float) -> tuple:
    return (
        _EDGE_START[0] + (_EDGE_END[0] - _EDGE_START[0]) * fraction,
        _EDGE_START[1] + (_EDGE_END[1] - _EDGE_START[1]) * fraction,
    )


REJECTIONS = {
    # A HALF-DRAWN BOUNDARY: four points walked along one real edge of
    # this very property (its first vertex to its fourth, interpolated),
    # so they are exactly collinear. The ring closes into a line rather
    # than around ground -- the open-ring case, in the only form a
    # coordinate list can actually express it.
    "open ring (half-drawn -- 4 collinear points along one real edge)": [
        _EDGE_START,
        _along_edge(0.25),
        _along_edge(0.5),
        _EDGE_END,
    ],
    "too few vertices": [(-79.9838154, 40.6458343), (-79.9804741, 40.6445667)],
    "self-intersecting (bow tie)": [
        (-79.9838154, 40.6458343),
        (-79.9804741, 40.6428581),
        (-79.9838154, 40.6428581),
        (-79.9804741, 40.6458343),
    ],
    "absurd area (multi-county polygon)": [
        (-80.5, 40.2),
        (-79.5, 40.2),
        (-79.5, 41.0),
        (-80.5, 41.0),
    ],
    "absurdly small area": [
        (-79.98380, 40.64583),
        (-79.98379, 40.64583),
        (-79.98379, 40.64584),
    ],
}

with Harness() as h:
    for description, bad_boundary in REJECTIONS.items():
        try:
            session_manager.create_session(
                bad_boundary, store, fetch_cache=fetch_cache, cache=sessions
            )
            raise AssertionError(f"{description}: must be rejected, was not")
        except session_manager.BoundaryValidationError as error:
            print(f"   rejected -- {description}\n     -> {error}")

    # Malformed vertices, not just malformed rings.
    for description, bad_boundary in {
        "not a pair": [(-79.98, 40.64), (-79.97,), (-79.96, 40.65)],
        "non-numeric": [(-79.98, 40.64), ("west", 40.65), (-79.96, 40.65)],
        "non-finite": [(-79.98, 40.64), (float("nan"), 40.65), (-79.96, 40.65)],
        "off Earth": [(-79.98, 40.64), (-79.97, 95.0), (-79.96, 40.65)],
        "empty": [],
    }.items():
        try:
            session_manager.create_session(
                bad_boundary, store, fetch_cache=fetch_cache, cache=sessions
            )
            raise AssertionError(f"{description}: must be rejected, was not")
        except session_manager.BoundaryValidationError:
            pass

    # NOTHING was fetched, persisted or cached for any of them: validation
    # runs before the first network byte.
    assert h.fetch_parcel_data.call_count == 0, (
        f"a rejected boundary must never reach the network, got "
        f"{h.fetch_parcel_data.call_count} fetches"
    )
    assert store.list_sessions() == [] and len(sessions) == 0

    # An explicitly closed ring IS accepted -- both closure forms are the
    # same land, and the codebase already closes open rings itself.
    explicitly_closed = list(REAL_BOUNDARY) + [REAL_BOUNDARY[0]]
    accepted = session_manager.create_session(
        explicitly_closed, store, fetch_cache=fetch_cache, cache=sessions
    )
    assert len(accepted["boundary"]) == len(explicitly_closed)
    assert h.fetch_parcel_data.call_count == 1

print(
    "\n9. VALIDATION: every malformed boundary rejected before any network call "
    "(fetch_parcel_data() == 0 across all 10 rejections); an explicitly closed "
    "ring is accepted, since implicit and explicit closure are the same land."
)


# --- 10. SELF-FETCH FALLBACK: with the overrides omitted, unchanged ----
#
# Section 2 asserts the warm-up path at ZERO SDA queries because it
# forwards ParcelData's soil_components/soil_geometries. That number is
# only meaningful if the OTHER path -- the parameters omitted -- still
# works exactly as it did before this branch: None is a real supported
# value (every caller outside this pipeline still passes nothing), not a
# deprecated one.
#
# Asserted the only way that means anything: identify_exclusion_zones()
# run twice on the SAME ParcelData -- once the way run_terrain_warm_up()
# calls it, once with the two parameters omitted -- and the two results
# compared BIT-IDENTICALLY, masks as ARRAYS. The self-fetch path's canned
# SDA queries return the very rows ParcelData holds, which is what makes
# "identical" the correct expectation rather than a coincidence.

_fb_parcel = _build_parcel_data()
_fb_boundary_polygon_utm = _fb_parcel.boundary_polygon_utm
_fb_roads = None

with Harness() as h:
    _fb_existing_roads = farm_roads_data.get_road_exclusion_union_utm(
        REAL_BOUNDARY, _fb_parcel.dem, farm_roads=_fb_parcel.farm_roads
    )

    # (a) the parameters OMITTED -- the pre-branch call, self-fetching.
    _fb_self_fetched = exclusion_zones.identify_exclusion_zones(
        REAL_BOUNDARY,
        dem=_fb_parcel.dem,
        boundary_polygon_utm=_fb_boundary_polygon_utm,
        canopy_height=_fb_parcel.canopy_height,
        road_exclusion_union_utm=_fb_existing_roads,
    )
    _fb_self_fetch_queries = h.total_soil_queries
    assert _fb_self_fetch_queries == 2, (
        f"with soil_components=/soil_geometries= omitted the hydric gate must still self-fetch its two "
        f"SDA queries, exactly as it did before this branch: got {_fb_self_fetch_queries}"
    )

    # (b) the parameters SUPPLIED -- what run_terrain_warm_up() does.
    _fb_before = h.total_soil_queries
    _fb_overridden = exclusion_zones.identify_exclusion_zones(
        REAL_BOUNDARY,
        dem=_fb_parcel.dem,
        boundary_polygon_utm=_fb_boundary_polygon_utm,
        canopy_height=_fb_parcel.canopy_height,
        soil_components=_fb_parcel.soil_components,
        soil_geometries=_fb_parcel.soil_geometries,
        road_exclusion_union_utm=_fb_existing_roads,
    )
    _fb_override_queries = h.total_soil_queries - _fb_before
    assert _fb_override_queries == 0, (
        f"with ParcelData's own rows supplied the hydric gate must issue ZERO SDA queries, got "
        f"{_fb_override_queries}"
    )

# BIT-IDENTICAL. equivalent() already compares numpy arrays element-wise
# and geometries by .equals() -- it is what section 7 uses to prove a
# rebuilt context matches the original -- so the whole result dict goes
# through it in one pass rather than being spot-checked key by key.
_fb_difference = equivalent(_fb_self_fetched, _fb_overridden, "exclusion_result")
assert _fb_difference is None, (
    f"supplying rows the gate would otherwise have fetched must change NOTHING about the result: "
    f"{_fb_difference}"
)
for _fb_layer, _fb_layer_result in _fb_self_fetched["layers"].items():
    assert np.array_equal(_fb_layer_result["mask"], _fb_overridden["layers"][_fb_layer]["mask"]), (
        f"the {_fb_layer} gate's mask must be bit-identical, compared as an ARRAY -- a mask with the "
        f"same cell count in different places is a different answer"
    )
assert _fb_self_fetched["layers"]["hydric"]["mask"].any(), (
    "the fixture's hydric map unit must actually hit ground, or this comparison proves nothing"
)

print(
    f"10. SELF-FETCH FALLBACK: identify_exclusion_zones() on one ParcelData with the overrides OMITTED "
    f"({_fb_self_fetch_queries} SDA queries -- the pre-branch path, intact) and SUPPLIED "
    f"({_fb_override_queries} SDA queries) returns BIT-IDENTICAL results across every layer mask, the "
    f"closed union, the eligible geometry and narrative_data (hydric layer "
    f"{_fb_self_fetched['layers']['hydric']['acres']} acres, so the comparison has real content)."
)


print("\nAll session_manager checks passed.")
