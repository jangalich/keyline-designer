"""
test_step_orchestrator.py

The generic generate path, end to end, run as:

    python test_step_orchestrator.py

REAL COORDINATES, REAL PIPELINE CODE. The boundary is the actual drawn
property from generate_full_report.py -- 5614 N Montour Rd, Gibsonia, PA
(~13.23 acres, UTM 17N) -- the SAME boundary test_session_manager.py (B2) and
test_wire_translation_inbound.py (B4) use, so a payload produced here is
comparable to what those branches asserted over. session_manager.create_
session(), the terrain warm-up, identify_exclusion_zones(),
identify_optimized_production_areas() and the whole payload assembly all RUN,
for real. They are wrapped (wraps=) to be COUNTED, never replaced.

What is mocked is the NETWORK and only the network -- the same harness shape
test_session_manager.py established, reused here rather than reinvented:
parcel_data.fetch_parcel_data() returns a ParcelData built over a DEM
fixture for this boundary's own UTM extent, and every layer fetch the
overrides are supposed to close is patched so it can be COUNTED AT ZERO. An
assertion that a count is zero only means something if a nonzero count was
reachable.

THE TERRAIN IS A FIXTURE and the acreages it produces are meaningless as
statements about the real parcel. Nothing here asserts a real-property
number; every assertion is about SHAPE, COUNTS and INVARIANTS.

Sections:
  1. GENERATE END TO END -- a session, a generate, a payload carrying every
     key the frontend reads, asserted against the WORKING ENDPOINT's own
     assembler rather than a hand-written expectation.
  2. ZERO SDA QUERIES -- the exact count the exclusion_result forward exists
     for. A failure here means the registry is not forwarding the warm-up's
     exclusion result.
  3. NO UPSTREAM RECOMPUTE -- generate twice; delineate_valleys() and
     fetch_parcel_data() each ran exactly ONCE across both, i.e. zero times
     during either generate.
  4. IDEMPOTENCE -- two generates produce equivalent payloads.
  5. CACHE EVICTION MID-SESSION -- evict, generate, get the same payload
     back, with zero network.
  6. THE DOCUMENT -- status becomes "generated"; NO features are written.
  7. THE WIRE ID -- present on every tabular row and equal to the
     corresponding feature's id.
  8. JOB LIFECYCLE -- running -> done; a failing generate -> failed carrying
     failed_layer.
  9. ORCHESTRATION EDGES -- unregistered steps, unexpected params, an
     unknown session.
"""

import json
import tempfile
from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

import canopy_height_data
import design_document
import exclusion_zones
import farm_roads_data
import job_runner
import keypoint_detection
import parcel_data
import production_area
import production_area_ceiling
import production_zone_payload
import session_cache
import session_manager
import step_orchestrator
import step_registry
import valley_delineation
import wire_translation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- comparing two payloads -------------------------------------------


def equivalent(left, right, path="payload"):
    """
    Deep equality over a JSON-shaped value, returning None on a match or a
    string naming the FIRST difference and where it is.

    A plain `==` would be correct here -- the payload is JSON-serialisable by
    contract -- but useless as a report: "the two payloads differ" over a
    37 KB tree of geometry is not something a reader can act on. This walks
    to the differing leaf and names its path.

    Deliberately NOT test_session_manager.py's comparator of the same name.
    That one handles numpy arrays and shapely geometry, because it compares
    SessionContexts; this compares wire payloads, where a numpy array or a
    shapely object arriving would itself be the bug (the wire is
    JSON-native), so encountering one must be reported rather than quietly
    compared elementwise.
    """
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return f"{path}: dict vs {type(right).__name__}"
        if set(left) != set(right):
            return f"{path}: keys differ ({sorted(set(left) ^ set(right))})"
        for key in left:
            difference = equivalent(left[key], right[key], f"{path}[{key!r}]")
            if difference:
                return difference
        return None
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
            return f"{path}: sequence vs {type(right).__name__}"
        if len(left) != len(right):
            return f"{path}: length {len(left)} != {len(right)}"
        for index, (a, b) in enumerate(zip(left, right)):
            difference = equivalent(a, b, f"{path}[{index}]")
            if difference:
                return difference
        return None
    if not isinstance(left, (str, int, float, bool, type(None))):
        return f"{path}: {type(left).__name__} is not a JSON-native value"
    if isinstance(left, float) and isinstance(right, float):
        if left != right and not (left != left and right != right):  # NaN == NaN
            return f"{path}: {left!r} != {right!r}"
        return None
    if left != right:
        return f"{path}: {left!r} != {right!r}"
    return None


# --- the real property, verbatim from B2 and B4 ----------------------

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
    "EPSG:4326", CRS, [lon for lon, _ in REAL_BOUNDARY], [lat for _, lat in REAL_BOUNDARY]
)
BOUNDARY_POLYGON_UTM = Polygon(zip(_xs, _ys))
PARCEL_ACRES = BOUNDARY_POLYGON_UTM.area / SQUARE_METERS_PER_ACRE

# --- the DEM fixture -------------------------------------------------
#
# A bench tilted gently enough to clear the slope gate over a real share of
# the parcel, cut by one incised drainage whose walls the gate rejects, with
# a canopy stand in the gentle half. Shaped so the landform step actually
# PRODUCES zones: a fixture on which nothing clears every gate would make
# every payload assertion below vacuously true.

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


def _build_dem() -> dict:
    """A 4% bench with one incised drainage down CHANNEL_COL. The drainage's
    own walls are steep (the slope gate bites there); everything else is
    gentle (it passes), so the eligible union is real ground with a real
    hole in it rather than the whole parcel or none of it."""
    rows = np.arange(ROWS)[:, None].astype(np.float32)
    cols = np.arange(COLS)[None, :].astype(np.float32)
    array = 300.0 + 0.20 * rows + 0.05 * cols
    # The incision: a narrow Gaussian trench, deep enough that its flanks
    # exceed production_area's slope ceiling.
    array -= 9.0 * np.exp(-((cols - CHANNEL_COL) ** 2) / (2 * 3.0 ** 2))
    return {
        "array": array.astype(np.float32),
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _build_canopy(dem: dict) -> dict:
    """A stand of trees on the GENTLE bench, well clear of the drainage --
    canopy in the steep half would be excluded by slope first and the canopy
    layer would read 0.0 acres, proving nothing."""
    hag = np.zeros((ROWS, COLS), dtype=np.float32)
    hag[KNEE_ROW - 6 : KNEE_ROW + 8, CHANNEL_COL + 14 : CHANNEL_COL + 26] = 15.0
    return {
        "array": hag,
        "resolution_meters": dem["resolution_meters"],
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
        "source_item_id": "fixture-hag",
    }


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
    """A fresh ParcelData every call, so object IDENTITY proves the fetch
    cache served a second request rather than two fetches returning equal
    values."""
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


# --- the harness -----------------------------------------------------


class Harness:
    """
    Every network boundary mocked, every real computation wrapped and
    counted. Same shape as test_session_manager.py's, extended with the
    production entry point and the payload assembler so a generate can be
    counted too. Used as a context manager so each section gets fresh counts.
    """

    def __init__(self, fetch_side_effect=None):
        self._stack = ExitStack()
        self._fetch_side_effect = fetch_side_effect or _build_parcel_data

    def __enter__(self):
        patch = self._stack.enter_context

        self.fetch_parcel_data = patch(
            mock_patch.object(
                parcel_data, "fetch_parcel_data", side_effect=self._fetch_side_effect
            )
        )

        # THE TWO SDA QUERIES. identify_exclusion_zones()'s hydric gate
        # reaches these through production_area._fetch_disqualifying_soil_
        # union() when it is not handed raw rows -- and identify_optimized_
        # production_areas() reaches the SAME helper on its no-exclusion_
        # result path. They return the fixture rows so the self-fetch path
        # stays a real, working one; section 2 asserts the count at ZERO.
        self.soil_components = patch(
            mock_patch.object(
                production_area, "get_soil_data_for_polygon",
                return_value=HYDRIC_COMPONENTS,
            )
        )
        self.soil_geometries = patch(
            mock_patch.object(
                production_area, "get_soil_geometries_for_polygon",
                return_value=HYDRIC_GEOMETRIES,
            )
        )

        # Layers whose overrides are forwarded. Each must stay at ZERO: a
        # nonzero count means an override stopped being passed.
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
        # The road layer, counted at both bindings the session path and the
        # endpoint path reach it through. BOTH ARE WRAPPED RATHER THAN
        # STUBBED, and that matters for section 1's comparison against the
        # endpoint: the session path hands identify_exclusion_zones() the
        # union the warm-up built from ParcelData's own rows, while the
        # endpoint path lets the gate self-fetch. Stubbing the self-fetch to
        # None would make the two paths compute against DIFFERENT road
        # exclusions and the payloads would differ for a reason that is the
        # harness's, not the code's. Returning the same fixture rows through
        # the real helper is what makes the two comparable -- and the counts
        # still bite, because the session path must reach neither.
        self.roads_refetch = patch(
            mock_patch.object(
                farm_roads_data,
                "get_farm_roads_for_boundary",
                return_value=FIXTURE_ROADS,
            )
        )
        self.roads_helper_refetch = patch(
            mock_patch.object(
                production_area,
                "_fetch_road_exclusion_union_utm",
                wraps=production_area._fetch_road_exclusion_union_utm,
            )
        )
        # The DEM fetch. identify_optimized_production_areas() calls it when
        # dem= is not forwarded, and the payload assembler's caller does too.
        self.dem_refetch = patch(
            mock_patch.object(
                production_area_ceiling, "get_dem_for_boundary",
                side_effect=AssertionError("get_dem_for_boundary() must not run"),
            )
        )

        # Real computation, wrapped so it RUNS and is counted. Valleys are
        # counted at BOTH import bindings, exactly as test_session_manager.py
        # does -- keypoint_detection self-computes through its own binding if
        # valleys= is not forwarded.
        self.delineate_valleys = patch(
            mock_patch.object(
                valley_delineation, "delineate_valleys",
                wraps=valley_delineation.delineate_valleys,
            )
        )
        self.keypoint_delineate_valleys = patch(
            mock_patch.object(
                keypoint_detection, "delineate_valleys",
                wraps=keypoint_detection.delineate_valleys,
            )
        )
        self.identify_exclusion_zones = patch(
            mock_patch.object(
                exclusion_zones, "identify_exclusion_zones",
                wraps=exclusion_zones.identify_exclusion_zones,
            )
        )
        # The step's own generate target -- patched on ITS module, which is
        # what step_registry.resolve() looks it up on at call time.
        self.identify_production = patch(
            mock_patch.object(
                production_area_ceiling, "identify_optimized_production_areas",
                wraps=production_area_ceiling.identify_optimized_production_areas,
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

    @property
    def total_network_calls(self) -> int:
        """Every mocked network boundary, summed. The honest total for a
        'zero network' claim -- a per-layer zero says nothing about the
        layer next to it."""
        return (
            self.fetch_parcel_data.call_count
            + self.total_soil_queries
            + self.canopy_refetch.call_count
            + self.canopy_module_refetch.call_count
            + self.roads_refetch.call_count
            + self.roads_helper_refetch.call_count
        )


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="step_orchestrator_test_"))


def _fresh_runner():
    return job_runner.JobRunner(max_workers=2, max_jobs=32)


def _generate(session_id, store, fetch_cache, cache, runner, step_id="landform", params=None):
    """generate_step() + wait, since these assertions are about the payload
    rather than about not blocking. The job's own lifecycle is section 8.

    Returns the PAYLOAD half of the job result. A done job now carries
    {"payload", "document"} (run_generate_job); the document half is section
    8's, and unwrapping it here would put a key every assertion below has to
    step over in front of all of them."""
    job = step_orchestrator.generate_step(
        session_id, step_id, store, params=params,
        fetch_cache=fetch_cache, cache=cache, runner=runner,
    ).wait(timeout=600)
    if job.status != job_runner.STATUS_DONE:
        raise AssertionError(f"generate failed: {job.error} ({job.exception!r})")
    return job.result["payload"]


# The keys the working frontend reads, gathered from App.jsx,
# ProductionZonePanel.jsx and ProductionZoneLayers.jsx. Named here as ONE
# list so a payload that drops any of them fails with the name of what the
# frontend would have lost, not with a KeyError somewhere downstream.
FRONTEND_KEYS = (
    "zones",              # ProductionZonePanel: the ranked list
    "suggested_zones",    # ProductionZoneLayers: the map features
    "eligible_union",     # ProductionZoneLayers: the highlight
    "exclusion_layers",   # App.jsx + panel: the per-gate overlays + caveats
    "summary",            # panel: total_acres, eligible_acres
    "scales",             # panel: bands / band_bounds
)

print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} "
    f"vertices, {PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at "
    f"{RESOLUTION_METERS:.0f} m. Same boundary as test_session_manager.py (B2) "
    f"and test_wire_translation_inbound.py (B4).\n"
)


# --- 1. GENERATE END TO END ------------------------------------------

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()

    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    SESSION_ID = document["session_id"]
    creation_fetches = h.fetch_parcel_data.call_count

    payload = _generate(SESSION_ID, store, fetch_cache, cache, runner)

    missing = [key for key in FRONTEND_KEYS if key not in payload]
    assert not missing, (
        f"the landform payload is missing key(s) the frontend reads: {missing}. "
        f"Got {sorted(payload)}"
    )
    assert isinstance(payload["suggested_zones"], dict)
    assert payload["suggested_zones"]["type"] == "FeatureCollection"
    assert isinstance(payload["suggested_zones"]["features"], list)
    assert isinstance(payload["zones"], list)
    assert isinstance(payload["exclusion_layers"], list)

    # The fixture has to produce real content, or every assertion below is
    # vacuous.
    assert payload["zones"], (
        "the fixture terrain produced no production zones; every payload "
        "assertion below would then be vacuously true"
    )
    assert payload["eligible_union"] is not None, (
        "the fixture terrain produced no eligible ground"
    )
    assert len(payload["suggested_zones"]["features"]) == len(payload["zones"]), (
        "the two representations describe the SAME zones -- a feature without "
        "a row (or a row without a feature) is a zone the map and the panel "
        "disagree about"
    )

    # The per-gate layers, in exclusion_zones.LAYER_ORDER, each with the
    # stable `type` the panel branches on and the `data_available` flag it
    # renders its standing caveat from.
    assert [layer["type"] for layer in payload["exclusion_layers"]] == list(
        exclusion_zones.LAYER_ORDER
    ), (
        f"exclusion_layers must arrive in LAYER_ORDER with stable types: "
        f"{[l['type'] for l in payload['exclusion_layers']]}"
    )
    for layer in payload["exclusion_layers"]:
        assert {"type", "label", "data_available", "geometry_wgs84"} <= set(layer), (
            f"exclusion layer {layer.get('type')} is missing a wire field"
        )
        assert isinstance(layer["data_available"], bool)

    assert {"total_acres", "eligible_acres"} <= set(payload["summary"]), (
        f"the panel reads summary.total_acres and summary.eligible_acres: "
        f"{sorted(payload['summary'])}"
    )
    assert {"bands", "band_bounds"} <= set(payload["scales"]), (
        f"the panel reads scales.bands and scales.band_bounds rather than "
        f"hardcoding thresholds: {sorted(payload['scales'])}"
    )
    for row in payload["zones"]:
        assert {"rank", "score", "slope_min_pct", "slope_max_pct", "dominant_aspect",
                "aspect_available", "area_acres"} <= set(row), (
            f"a tabular row is missing a column the panel prints: {sorted(row)}"
        )
    for feature in payload["suggested_zones"]["features"]:
        assert "area_acres" in feature["properties"], (
            "the map captions each feature from properties.area_acres"
        )

    # AGAINST THE WORKING ENDPOINT'S OWN OUTPUT, not a hand-written
    # expectation. /api/production-zones and this path share ONE assembler
    # (production_zone_payload.assemble_production_zone_payload); feeding it
    # the same two results the endpoint would have fetched for this boundary
    # is the strongest available statement that the frontend sees no
    # difference. equivalent() is B2's own deep comparator.
    _context = session_manager.get_session_context(
        SESSION_ID, store, fetch_cache=fetch_cache, cache=cache
    )
    endpoint_payload = production_zone_payload.build_production_zone_payload(
        REAL_BOUNDARY,
        dem=_context.dem,
        canopy_height=_context.parcel_data.canopy_height,
    )
    difference = equivalent(payload, endpoint_payload, "payload")
    assert difference is None, (
        f"the session payload must be what /api/production-zones returns for "
        f"this boundary: {difference}"
    )

print(
    f"1. GENERATE END TO END: session created, landform generated. Payload "
    f"carries all {len(FRONTEND_KEYS)} keys the frontend reads "
    f"({', '.join(FRONTEND_KEYS)}); {len(payload['zones'])} tabular row(s) and "
    f"{len(payload['suggested_zones']['features'])} map feature(s) describing "
    f"the same zones; {len(payload['exclusion_layers'])} exclusion layers in "
    f"LAYER_ORDER; summary.eligible_acres={payload['summary']['eligible_acres']}, "
    f"summary.total_acres={payload['summary']['total_acres']}. Byte-equivalent "
    f"to production_zone_payload.build_production_zone_payload()'s own output "
    f"for this boundary."
)


# --- 2. ZERO SDA QUERIES DURING GENERATE ------------------------------
#
# THE ASSERTION THE exclusion_result FORWARD EXISTS FOR. Without it,
# identify_optimized_production_areas() takes its no-exclusion_result path,
# reaches production_area._fetch_disqualifying_soil_union(), and issues two
# SDA queries -- per generate, and generate is repeatable by contract.

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    session_id = document["session_id"]

    creation_soil_queries = h.total_soil_queries
    before = h.total_network_calls

    _generate(session_id, store, fetch_cache, cache, runner)

    generate_soil_queries = h.total_soil_queries - creation_soil_queries
    generate_network = h.total_network_calls - before

    assert creation_soil_queries == 0, (
        f"session creation must issue ZERO SDA queries (the warm-up forwards "
        f"ParcelData's own rows); got {creation_soil_queries}"
    )
    assert generate_soil_queries == 0, (
        f"generate must issue ZERO SDA queries, got {generate_soil_queries}. "
        f"The registry is not forwarding the warm-up's exclusion result into "
        f"identify_optimized_production_areas(exclusion_result=) -- see the "
        f"landform entry's exclusion_zones consumes edge."
    )
    assert generate_network == 0, (
        f"generate must make ZERO network calls of any kind, got "
        f"{generate_network}"
    )

    # THE ZERO IS REACHABLE. A count of zero proves nothing unless the same
    # call WITHOUT the forward is nonzero -- so the no-exclusion_result path
    # is exercised directly, on the same DEM, and must pay the two queries.
    context = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=cache
    )
    before_unforwarded = h.total_soil_queries
    production_area_ceiling.identify_optimized_production_areas(
        REAL_BOUNDARY,
        dem=context.dem,
        canopy_height=context.parcel_data.canopy_height,
    )
    unforwarded_soil_queries = h.total_soil_queries - before_unforwarded
    assert unforwarded_soil_queries == 2, (
        f"with exclusion_result= OMITTED the same call must still self-fetch "
        f"its two SDA queries -- otherwise the zero above measures nothing. "
        f"Got {unforwarded_soil_queries}"
    )

print(
    f"2. ZERO SDA QUERIES: creation {creation_soil_queries}, generate "
    f"{generate_soil_queries} (and {generate_network} network calls of ANY "
    f"kind during generate). The same call with exclusion_result= omitted "
    f"issues {unforwarded_soil_queries}, so the zero is a closed fetch and "
    f"not an unreachable path."
)


# --- 3. NO UPSTREAM RECOMPUTE ----------------------------------------

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    session_id = document["session_id"]

    after_creation = {
        "delineate_valleys": h.total_delineate_valleys_calls,
        "fetch_parcel_data": h.fetch_parcel_data.call_count,
        "identify_exclusion_zones": h.identify_exclusion_zones.call_count,
    }

    _generate(session_id, store, fetch_cache, cache, runner)
    _generate(session_id, store, fetch_cache, cache, runner)

    valleys_total = h.total_delineate_valleys_calls
    parcel_total = h.fetch_parcel_data.call_count
    exclusion_total = h.identify_exclusion_zones.call_count
    production_total = h.identify_production.call_count

    assert valleys_total == 1, (
        f"delineate_valleys() must run exactly ONCE across a creation and TWO "
        f"generates -- summed over both import bindings -- got {valleys_total}"
    )
    assert parcel_total == 1, (
        f"fetch_parcel_data() must run exactly ONCE, got {parcel_total}"
    )
    assert exclusion_total == 1, (
        f"identify_exclusion_zones() must run exactly ONCE (at the warm-up); a "
        f"generate reads its result from the cache. Got {exclusion_total}"
    )
    assert valleys_total == after_creation["delineate_valleys"], (
        f"ZERO delineate_valleys() calls during either generate: "
        f"{after_creation['delineate_valleys']} -> {valleys_total}"
    )
    assert parcel_total == after_creation["fetch_parcel_data"], (
        f"ZERO fetch_parcel_data() calls during either generate: "
        f"{after_creation['fetch_parcel_data']} -> {parcel_total}"
    )
    assert production_total == 2, (
        f"the step's OWN entry point runs once per generate -- that is what "
        f"regenerating recomputes. Got {production_total}"
    )

print(
    f"3. NO UPSTREAM RECOMPUTE: across one creation + TWO generates -- "
    f"delineate_valleys() {valleys_total} (both bindings summed), "
    f"fetch_parcel_data() {parcel_total}, identify_exclusion_zones() "
    f"{exclusion_total}; each unchanged from its post-creation count "
    f"({after_creation['delineate_valleys']}, "
    f"{after_creation['fetch_parcel_data']}, "
    f"{after_creation['identify_exclusion_zones']}), i.e. ZERO during either "
    f"generate. identify_optimized_production_areas() ran {production_total} "
    f"times -- only the regenerated step recomputes."
)


# --- 4. IDEMPOTENCE ---------------------------------------------------

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    session_id = document["session_id"]

    first = _generate(session_id, store, fetch_cache, cache, runner)
    revision_after_first = store.get(session_id)["document_revision"]
    second = _generate(session_id, store, fetch_cache, cache, runner)
    revision_after_second = store.get(session_id)["document_revision"]

    difference = equivalent(first, second, "payload")
    assert difference is None, (
        f"two generates of one step must produce equivalent payloads: "
        f"{difference}"
    )
    assert revision_after_second == revision_after_first, (
        f"a regenerate changes no DECISION, so it must not bump "
        f"document_revision: {revision_after_first} -> {revision_after_second}"
    )

print(
    f"4. IDEMPOTENCE: two generates produce equivalent payloads across every "
    f"key, and the document stays at revision {revision_after_second} -- a "
    f"regenerate changes no decision, so it bumps nothing a commit is checked "
    f"against."
)


# --- 5. CACHE EVICTION MID-SESSION ------------------------------------

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    session_id = document["session_id"]

    warm_payload = _generate(session_id, store, fetch_cache, cache, runner)

    assert cache.discard(session_id), "the session should have been cached"
    assert session_id not in cache

    network_before_rebuild = h.total_network_calls
    rebuilt_payload = _generate(session_id, store, fetch_cache, cache, runner)
    rebuild_network = h.total_network_calls - network_before_rebuild

    assert session_id in cache, "the rebuild must repopulate the session cache"
    difference = equivalent(warm_payload, rebuilt_payload, "payload")
    assert difference is None, (
        f"a generate after an eviction must produce the SAME payload -- the "
        f"cache is not authoritative, so a miss degrades to slower and never "
        f"to a different answer: {difference}"
    )
    assert rebuild_network == 0, (
        f"the rebuild must make ZERO network calls (Layer 1 served by the "
        f"fetch cache, the warm-up network-free), got {rebuild_network}"
    )

print(
    f"5. CACHE EVICTION: the tier-2 entry dropped mid-session; the next "
    f"generate rebuilt it from the Design Document with {rebuild_network} "
    f"network calls and produced a payload equivalent to the warm one across "
    f"every key."
)


# --- 6. THE DOCUMENT --------------------------------------------------

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    session_id = document["session_id"]

    assert document["steps"]["landform"]["status"] == design_document.STATUS_NOT_STARTED

    _generate(session_id, store, fetch_cache, cache, runner)
    stored = store.get(session_id)
    entry = stored["steps"]["landform"]

    assert entry["status"] == design_document.STATUS_GENERATED, (
        f"generate must set the step's document status to 'generated', got "
        f"{entry['status']!r}"
    )
    # PROPOSALS ARE NOT DECISIONS. The document holds decisions and nothing
    # derived (proposal section 2.1); a generated step is a STATUS and
    # nothing else.
    assert set(entry) == {"status"}, (
        f"a generated step must carry ONLY its status -- no features, no "
        f"provenance, no proposals. Got {sorted(entry)}"
    )
    assert "features" not in entry
    for other in design_document.STEP_ORDER:
        if other == "landform":
            continue
        assert stored["steps"][other] == {
            "status": design_document.STATUS_NOT_STARTED
        }, f"generating landform must not touch step '{other}'"

    # The proposals live where session_cache.py reserved room for them.
    context = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=cache
    )
    assert "landform" in context.step_proposals, (
        "the generate result is cached on SessionContext.step_proposals"
    )
    assert "scored_patches" in context.step_proposals["landform"], (
        "the cached proposal is the INTERNAL result (native objects), not the "
        "wire payload -- the wire form is rebuildable from it and heavy to keep"
    )

    document_bytes = len(json.dumps(stored).encode("utf-8"))

print(
    f"6. THE DOCUMENT: landform status not_started -> "
    f"{entry['status']!r}, carrying only {sorted(entry)} -- NO features "
    f"written. The other five steps are untouched. The whole document is "
    f"{document_bytes:,} B on disk; the proposals ("
    f"{len(context.step_proposals['landform']['scored_patches'])} scored "
    f"patches, with numpy cell lists and shapely geometry) are on the session "
    f"cache instead."
)


# --- 7. THE WIRE ID ON EVERY TABULAR ROW ------------------------------
#
# The defect this branch fixes. The panel reconstructed a zone's wire
# identity by string prefixing while the map filtered on feature.id directly:
# one identity, two sources of truth, joined by a format string that nothing
# checks. The row now CARRIES the value.

feature_ids = {f["id"] for f in payload["suggested_zones"]["features"]}
by_patch_id = {
    int(str(f["id"]).rsplit("-", 1)[-1]): f["id"]
    for f in payload["suggested_zones"]["features"]
}

for row in payload["zones"]:
    assert "feature_id" in row, (
        f"every tabular row must carry the wire feature id: {sorted(row)}"
    )
    assert row["feature_id"] in feature_ids, (
        f"row feature_id {row['feature_id']!r} matches no map feature"
    )
    assert row["feature_id"] == by_patch_id[int(row["id"])], (
        f"row {row['id']}'s feature_id must be ITS OWN feature's id: "
        f"{row['feature_id']!r} vs {by_patch_id[int(row['id'])]!r}"
    )
    # The existing numeric id STAYS -- the frontend still keys list rows on
    # it, and this branch must not break the working spike.
    assert "id" in row and isinstance(row["id"], int), (
        "the bare numeric id must survive alongside the wire id"
    )
    # The carried value equals what the old format string produced, so the
    # fix is a change of SOURCE, not of value: no frontend behaviour moves.
    assert row["feature_id"] == f"{wire_translation._PRODUCTION_FEATURE_ID_PREFIX}{row['id']}", (
        "the carried id must equal what the panel's template literal built, "
        "or this is a behaviour change rather than a de-duplication"
    )

assert len({row["feature_id"] for row in payload["zones"]}) == len(payload["zones"]), (
    "two rows carrying one feature id would make selection ambiguous"
)

print(
    f"7. THE WIRE ID: all {len(payload['zones'])} tabular row(s) carry "
    f"feature_id, each equal to its own map feature's id "
    f"({sorted(feature_ids)}) and to what the panel's template literal used "
    f"to build; the numeric `id` the frontend keys on is untouched."
)


# --- 8. JOB LIFECYCLE -------------------------------------------------

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    session_id = document["session_id"]

    job = step_orchestrator.generate_step(
        session_id, "landform", store,
        fetch_cache=fetch_cache, cache=cache, runner=runner,
    )
    # RUNNING THE MOMENT IT IS HANDED BACK. There is no "queued" state to
    # observe (job_runner.py's STATUS IS THREE VALUES note).
    assert job.snapshot()["status"] in (
        job_runner.STATUS_RUNNING, job_runner.STATUS_DONE
    ), job.snapshot()

    job.wait(timeout=600)
    done = runner.get_job(job.id)
    assert done["status"] == job_runner.STATUS_DONE, done
    assert "result" in done and "error" not in done, (
        f"a done job carries a result and no error key: {sorted(done)}"
    )
    assert set(done["result"]) == set(job.result)

    # THE DONE RESULT'S TWO HALVES. The payload is the step's layers; the
    # document is the one this generate moved to "generated", shaped by
    # design_document.document_body() exactly as the session routes shape it.
    # The client polls once and knows both -- the GET that used to be the only
    # way to learn the new status is what the second key removes.
    assert set(job.result) == {"payload", "document"}, sorted(job.result)
    carried = job.result["document"]
    assert carried["steps"]["landform"]["status"] == (
        design_document.STATUS_GENERATED
    ), carried["steps"]["landform"]
    assert carried["step_order"] == list(design_document.STEP_ORDER), carried.get(
        "step_order"
    )
    assert carried == design_document.document_body(store.get(session_id)), (
        "the carried document must be the stored document on the wire -- the "
        "same bytes a GET of this session would return"
    )

    # A FAILING GENERATE. The step's declared canopy failure -- the layer
    # /api/production-zones names for the same failure -- raised from the
    # entry point the registry actually calls.
    with mock_patch.object(
        production_area_ceiling,
        "identify_optimized_production_areas",
        side_effect=canopy_height_data.CanopyCoverageIncompleteError(
            "fixture: HAG coverage too sparse"
        ),
    ):
        failing = step_orchestrator.generate_step(
            session_id, "landform", store,
            fetch_cache=fetch_cache, cache=cache, runner=runner,
        ).wait(timeout=600)

    failed = runner.get_job(failing.id)
    assert failed["status"] == job_runner.STATUS_FAILED, failed
    assert "error" in failed and "result" not in failed, (
        f"a failed job carries an error and no result key: {sorted(failed)}"
    )
    assert failed["error"]["failed_layer"] == {
        "type": "canopy", "label": "tree canopy height"
    }, (
        f"the error payload must carry failed_layer as {{type, label}}, the "
        f"shape /api/production-zones sends and the panel branches on: "
        f"{failed['error']}"
    )
    assert (
        failed["error"]["failed_layer"]["type"],
        failed["error"]["failed_layer"]["label"],
    ) == production_zone_payload.LAYER_CANOPY

    # AND NO DOCUMENT ON THE FAILURE. The done result gained one because a
    # successful generate MOVED the step's status; this one moved nothing, so
    # there is no transition to report and the error payload is left exactly
    # as it was. A document here would be a document sent to say that nothing
    # happened -- and a client hydrating on it would rewrite its mirror from
    # a failed generate.
    assert "document" not in failed["error"], sorted(failed["error"])
    assert "document" not in failed, sorted(failed)
    assert store.get(session_id)["steps"]["landform"]["status"] == (
        design_document.STATUS_GENERATED
    ), (
        "the status the EARLIER successful generate set is what stands; the "
        "failure neither advanced it nor rolled it back"
    )
    # NOT A TRACEBACK. The exception is kept for server-side logging and
    # never serialised.
    assert "Traceback" not in str(failed["error"])
    assert "fixture: HAG coverage too sparse" not in str(failed["error"]), (
        "the raw exception text must not cross the wire -- the layer identity "
        "is the only part of the failure a reader can act on"
    )
    assert isinstance(
        failing.exception, canopy_height_data.CanopyCoverageIncompleteError
    ), "the original exception stays on the Job for server-side logging"

    # A LayerFetchError names its own layer, self-describing.
    with mock_patch.object(
        production_area_ceiling,
        "identify_optimized_production_areas",
        side_effect=production_zone_payload.LayerFetchError(
            *production_zone_payload.LAYER_ELEVATION
        ),
    ):
        elevation_failure = step_orchestrator.generate_step(
            session_id, "landform", store,
            fetch_cache=fetch_cache, cache=cache, runner=runner,
        ).wait(timeout=600)
    assert elevation_failure.error["failed_layer"] == {
        "type": "elevation", "label": "elevation data"
    }, elevation_failure.error

    # An UNCLASSIFIED failure: the endpoint's other branch -- prose, and NO
    # failed_layer, which the panel renders as "The data sources did not
    # respond."
    with mock_patch.object(
        production_area_ceiling,
        "identify_optimized_production_areas",
        side_effect=RuntimeError("something else entirely"),
    ):
        generic_failure = step_orchestrator.generate_step(
            session_id, "landform", store,
            fetch_cache=fetch_cache, cache=cache, runner=runner,
        ).wait(timeout=600)
    assert generic_failure.status == job_runner.STATUS_FAILED
    assert "failed_layer" not in generic_failure.error, generic_failure.error
    assert generic_failure.error["error"] == "Production zones could not be generated."

    # A FAILED GENERATE LEAVES THE STATUS ALONE. The document write is the
    # LAST thing run_generate() does, so nothing records proposals that were
    # never produced.
    fresh_store = _fresh_store()
    fresh_fetch, fresh_cache_ = _fresh_caches()
    fresh_document = session_manager.create_session(
        REAL_BOUNDARY, fresh_store, fetch_cache=fresh_fetch, cache=fresh_cache_
    )
    with mock_patch.object(
        production_area_ceiling,
        "identify_optimized_production_areas",
        side_effect=RuntimeError("boom"),
    ):
        step_orchestrator.generate_step(
            fresh_document["session_id"], "landform", fresh_store,
            fetch_cache=fresh_fetch, cache=fresh_cache_, runner=runner,
        ).wait(timeout=600)
    assert fresh_store.get(fresh_document["session_id"])["steps"]["landform"] == {
        "status": design_document.STATUS_NOT_STARTED
    }, "a failed generate must not mark the step generated"

    # Job identity and the unknown-id contract.
    assert job.id != failing.id
    try:
        runner.get_job("no-such-job")
    except job_runner.JobNotFoundError:
        pass
    else:
        raise AssertionError("an unknown job id must raise, not report failed")

print(
    f"8. JOB LIFECYCLE: generate returned a job immediately, went "
    f"running -> done carrying only a result -- {sorted(job.result)}, the "
    f"payload plus the document this generate moved to "
    f"'{carried['steps']['landform']['status']}' (step_order "
    f"{carried['step_order']}), byte-equal to the stored document on the "
    f"wire. A canopy failure went "
    f"running -> failed carrying failed_layer "
    f"{failed['error']['failed_layer']}; a LayerFetchError named its own "
    f"layer ({elevation_failure.error['failed_layer']['type']}); an "
    f"unclassified failure carried prose and NO failed_layer. No traceback "
    f"and no exception text crossed the wire, no failure carried a document, "
    f"a fresh session's step stayed not_started, "
    f"and an unknown job id raises."
)


# --- 9. ORCHESTRATION EDGES ------------------------------------------

with Harness() as h:
    store = _fresh_store()
    fetch_cache, cache = _fresh_caches()
    runner = _fresh_runner()
    document = session_manager.create_session(
        REAL_BOUNDARY, store, fetch_cache=fetch_cache, cache=cache
    )
    session_id = document["session_id"]

    # A step with no registry entry fails BEFORE a job exists: the request
    # was wrong, and there is nothing to poll for.
    edge_failures = 0
    for bad_step in ("water", "orchards"):
        try:
            step_orchestrator.generate_step(
                session_id, bad_step, store,
                fetch_cache=fetch_cache, cache=cache, runner=runner,
            )
        except step_registry.RegistryError:
            edge_failures += 1
    assert edge_failures == 2

    # Landform declares NO user inputs, so any params is an error rather
    # than a value quietly ignored.
    try:
        step_orchestrator.generate_step(
            session_id, "landform", store, params={"access_point": [-79.98, 40.64]},
            fetch_cache=fetch_cache, cache=cache, runner=runner,
        )
    except step_orchestrator.StepOrchestrationError as exc:
        assert "access_point" in str(exc)
    else:
        raise AssertionError("unexpected params must be rejected, not dropped")

    # An unknown session fails inside the job -- the step and params were
    # fine, so a job legitimately exists to carry the answer.
    unknown = step_orchestrator.generate_step(
        "no-such-session", "landform", store,
        fetch_cache=fetch_cache, cache=cache, runner=runner,
    ).wait(timeout=60)
    assert unknown.status == job_runner.STATUS_FAILED
    assert unknown.error == {"error": "Production zones could not be generated."}

    # The committed-source resolver is declared and deliberately unwired.
    assert step_registry.SOURCE_COMMITTED in step_orchestrator._CONSUMES_RESOLVERS, (
        "the committed source must be a registered resolver slot, so adding a "
        "step that reads a commit is a registration rather than a rewrite of "
        "assemble_consumes()"
    )
    # THE COMMITTED RESOLVER REFUSES AN UNCOMMITTED UPSTREAM STEP -- it does
    # not generate anyway and it does not self-compute. The full behaviour
    # (rehydration, the empty-commit sentinel, the revision-keyed cache) is
    # test_step_commit.py's; this asserts the slot is wired and refuses.
    try:
        step_orchestrator._resolve_from_committed(
            step_registry.get_step("landform"),
            step_registry.Consumed(
                name="production_areas",
                source=step_registry.SOURCE_COMMITTED,
                from_step="landform",
                rehydrate="wire_translation.rehydrate_production_zones",
            ),
            None,
            document,
        )
    except step_orchestrator.UpstreamNotCommittedError as exc:
        assert exc.upstream_step == "landform"
        assert exc.upstream_status == design_document.STATUS_NOT_STARTED, exc.upstream_status
    else:
        raise AssertionError(
            "the committed resolver must REFUSE an uncommitted upstream step, "
            "never fall through to a self-compute"
        )

print(
    f"9. ORCHESTRATION EDGES: an unregistered step and an unknown step both "
    f"raise before a job exists ({edge_failures}/2); unexpected params are "
    f"rejected by name; an unknown session fails INSIDE the job; the "
    f"committed-source resolver is registered and REFUSES an uncommitted "
    f"upstream step rather than self-computing it."
)


print("\nAll step_orchestrator checks passed.")
