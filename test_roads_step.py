"""
test_roads_step.py

THE ROADS STEP -- the registry's third entry, and the first that breaks the
model the first two share. Run as:

    python test_roads_step.py

REAL COORDINATES, REAL PIPELINE CODE, THE SAME FIXTURE AS THE PRIOR
BRANCHES. The boundary is the actual drawn property from generate_full_
report.py -- 5614 N Montour Rd, Gibsonia, PA (~13.23 acres, UTM 17N) -- and
the DEM is test_water_step.py's bench-and-drainage fixture with its flanking
levees, unchanged, so the water zones committed here are the zones that
branch asserted over. session_manager.create_session(), the terrain warm-up,
the landform and water generates, the commit gate, the rehydrators,
road_corridors.identify_road_corridor_candidates() and the whole payload
assembly all RUN, for real. What is mocked is the NETWORK and only the
network -- plus ONE pipeline function, fetch_and_select_optimal_water_zone,
replaced by a counter because section 10 exists to prove it is never
reached, and a real call would itself go to the network.

HOW ROADS DIFFERS, and why this file's sections look nothing like water's.
identify_road_corridor_candidates() returns ONE network per call; the
branches inside it are a tree, not alternatives. So the candidates are
NETWORKS, one per ACCESS POINT, and the user generates them by trying
different access points. Results accumulate (up to three), any may be
discarded, and exactly one -- or none -- is committed.

Sections (the branch's numbered tests in brackets):
  1  [1]  REGISTRY -- the roads entry's shape, validate_registry() with
          three entries, constants agree with the modules that own them.
  2  [2]  GENERATE with an access point -> one network. Zero network calls.
  3  [3]  ACCUMULATION -- A then B; both exist; A is byte-identical.
  4  [4]  ID STABILITY across accumulated generates and a cache eviction.
  5  [5]  INDEPENDENCE -- A twice is identical; B-then-A == A-then-B.
          THE ONE THAT DECIDES WHETHER THE INTERACTION IS SOUND.
  6  [6]  CAP -- a fourth generate is refused server-side, before a job.
  7  [7]  DISCARD frees a slot.
  8  [8]  max_features 1 -- a two-network commit is rejected; so is half a
          network.
  9  [9]  EMPTY COMMIT is legal and records no road.
 10  [10] WATER EMPTY -> SENTINEL -- fetch_and_select_optimal_water_zone()
          did NOT run. Counted, with a control. THE ONE THAT FAILS SILENTLY.
 11  [11] Water multi-select reaches roads as a UNION.
 12  [12] A commit missing its required input is REJECTED.
 13  [13] REOPEN restores every candidate, not just the committed one.
 14  [14] _API_ERRORS -- a failed POST /api/sessions carries failed_layer.
 15  [15] Regression is the other test files, run separately.
"""

import copy
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, Polygon

import canopy_height_data
import commit_validation
import design_document
import exclusion_zones
import farm_roads_data
import job_runner
import keypoint_detection
import parcel_data
import production_area
import production_area_ceiling
import production_zone_payload
import road_corridors
import session_api
import session_cache
import session_manager
import step_orchestrator
import step_registry
import valley_delineation
import water_suitability
import water_survey_areas
import wire_translation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- the real property, verbatim from B2, B4, B5a, B5b and the water step -

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


def _boundary_point(edge_index: int, fraction: float) -> tuple:
    """A (lon, lat) EXACTLY on the parcel's own edge -- interpolated along
    the UTM boundary ring and warped back -- so it is a real access point by
    validate_access_point_on_boundary()'s own rule, not one that happens to
    fall inside the tolerance."""
    ring = BOUNDARY_POLYGON_UTM.exterior
    start = ring.project(Point(ring.coords[edge_index]))
    end = ring.project(Point(ring.coords[edge_index + 1]))
    point = ring.interpolate(start + (end - start) * fraction)
    lons, lats = warp_transform(CRS, "EPSG:4326", [point.x], [point.y])
    return (float(lons[0]), float(lats[0]))


# FOUR REAL ACCESS POINTS on four different edges, chosen from a survey of
# fifteen edge points on this fixture so that each yields a DIFFERENT
# network: A on the long west edge (N Montour Rd side) a trunk and a spur;
# B on the south-east edge a five-branch network; C on the north edge a
# trunk with a WATER SPUR to the committed zones; D on the south-west edge,
# for the cap test. A fifth, NO_NETWORK, sits where the anchor's own
# service radius already covers the nearest production ground, so the
# router honestly returns no network (stop_reason corridor_too_short).
ACCESS_A = _boundary_point(0, 0.85)
ACCESS_B = _boundary_point(2, 0.50)
ACCESS_C = _boundary_point(4, 0.50)
ACCESS_D = _boundary_point(1, 0.50)
ACCESS_NO_NETWORK = _boundary_point(3, 0.50)
# An interior point, ~40 m inside: not an access point by the validator's
# own rule.
_centroid_lon, _centroid_lat = warp_transform(
    CRS, "EPSG:4326", [BOUNDARY_POLYGON_UTM.centroid.x], [BOUNDARY_POLYGON_UTM.centroid.y]
)
INTERIOR_POINT = (float(_centroid_lon[0]), float(_centroid_lat[0]))

# --- the DEM fixture, verbatim from test_water_step.py -----------------

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
    rows = np.arange(ROWS)[:, None].astype(np.float64)
    cols = np.arange(COLS)[None, :].astype(np.float64)
    array = 300.0 + 0.20 * rows + 0.05 * cols
    array -= 2.5 * np.exp(-((cols - CHANNEL_COL) ** 2) / (2 * 4.0 ** 2))
    levee_offset = 5.0 + 3.0 * (1.0 - np.exp(-((rows - KNEE_ROW) ** 2) / (2 * 8.0 ** 2)))
    for side in (-1, 1):
        array += 2.5 * np.exp(-((cols - (CHANNEL_COL + side * levee_offset)) ** 2) / (2 * 2.0 ** 2))
    return {
        "array": array.astype(np.float32),
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _build_canopy(dem: dict) -> dict:
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
    {"mukey": "111111", "comppct_r": "85", "hydricrating": "Yes", "compname": "Fixture silt loam"}
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
        "geometry": {"type": "LineString", "coordinates": [[-79.9840, 40.6436], [-79.9805, 40.6436]]},
    }
]
FIXTURE_KSAT = [{"mukey": "111111", "ksat_r": "9.0"}]


def _build_parcel_data(_boundary=None) -> ParcelData:
    dem = _build_dem()
    return ParcelData(
        dem=dem,
        boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
        soil_components=HYDRIC_COMPONENTS,
        farmland_classification=[],
        erosion_factor=[],
        saturated_hydraulic_conductivity=FIXTURE_KSAT,
        soil_geometries=HYDRIC_GEOMETRIES,
        # THE SHAPE hydrology_data.get_water_features_for_boundary() returns
        # -- the warm-up's floodplain builder reads both keys.
        water_features={"streams": [], "water_bodies": []},
        farm_roads=FIXTURE_ROADS,
        climate_summary={},
        elevation_grid=[],
        canopy_height=_build_canopy(dem),
        imagery_summary={},
        irradiance={"status": "ok"},
    )


class Harness:
    """
    The water step's harness plus the roads module's own boundaries. Every
    network call is mocked; every real computation is wrapped (wraps=) so it
    RUNS and is COUNTED. An assertion that a count is zero only means
    something if a nonzero count was reachable.
    """

    def __enter__(self):
        self._stack = ExitStack()
        patch = self._stack.enter_context

        self.fetch_parcel_data = patch(
            mock_patch.object(parcel_data, "fetch_parcel_data", side_effect=_build_parcel_data)
        )
        self.soil_components = patch(
            mock_patch.object(production_area, "get_soil_data_for_polygon", return_value=HYDRIC_COMPONENTS)
        )
        self.soil_geometries = patch(
            mock_patch.object(production_area, "get_soil_geometries_for_polygon", return_value=HYDRIC_GEOMETRIES)
        )
        self.canopy_refetch = patch(
            mock_patch.object(production_area, "get_canopy_height_for_boundary", return_value=None)
        )
        self.canopy_module_refetch = patch(
            mock_patch.object(canopy_height_data, "get_canopy_height_for_boundary", return_value=None)
        )
        self.roads_refetch = patch(
            mock_patch.object(farm_roads_data, "get_farm_roads_for_boundary", return_value=FIXTURE_ROADS)
        )
        # The water step's own fetches, closed by its registry edges.
        self.water_soil_fetch = patch(
            mock_patch.object(
                water_survey_areas, "_fetch_soil_inputs",
                side_effect=AssertionError("_fetch_soil_inputs() must not run"),
            )
        )
        self.water_road_fetch = patch(
            mock_patch.object(
                water_survey_areas, "_fetch_road_exclusion_union_utm",
                side_effect=AssertionError("the water step must not fetch roads"),
            )
        )
        self.water_dem_fetch = patch(
            mock_patch.object(
                water_survey_areas, "get_dem_for_boundary",
                side_effect=AssertionError("get_dem_for_boundary() must not run"),
            )
        )
        self.dem_refetch = patch(
            mock_patch.object(
                production_area_ceiling, "get_dem_for_boundary",
                side_effect=AssertionError("get_dem_for_boundary() must not run"),
            )
        )
        # --- THE ROADS MODULE'S OWN FETCHES AND SELF-COMPUTES ------------
        # Each one fires when its registry edge is not forwarded. The three
        # network fetches raise; the three self-computes are wrapped and
        # counted, because each is a real function that would produce a
        # plausible wrong answer rather than an error.
        self.road_dem_fetch = patch(
            mock_patch.object(
                road_corridors, "get_dem_for_boundary",
                side_effect=AssertionError("road_corridors must not fetch a DEM"),
            )
        )
        self.road_nhd_fetch = patch(
            mock_patch.object(
                road_corridors, "get_water_features_for_boundary",
                side_effect=AssertionError("road_corridors must not fetch NHD"),
            )
        )
        self.road_soil_fetch = patch(
            mock_patch.object(
                road_corridors, "get_soil_data_for_polygon",
                side_effect=AssertionError("road_corridors must not fetch SSURGO"),
            )
        )
        self.road_soil_geometry_fetch = patch(
            mock_patch.object(
                road_corridors, "get_soil_geometries_for_polygon",
                side_effect=AssertionError("road_corridors must not fetch SSURGO geometry"),
            )
        )
        self.road_valleys = patch(
            mock_patch.object(road_corridors, "delineate_valleys", wraps=road_corridors.delineate_valleys)
        )
        self.road_production_selfcompute = patch(
            mock_patch.object(
                road_corridors, "identify_optimized_production_areas",
                wraps=road_corridors.identify_optimized_production_areas,
            )
        )
        self.road_floodplain = patch(
            mock_patch.object(
                road_corridors, "_fetch_floodplain_hydric_union",
                wraps=road_corridors._fetch_floodplain_hydric_union,
            )
        )
        self.road_canopy_mask = patch(
            mock_patch.object(
                road_corridors, "get_required_tree_root_zone_mask_utm",
                wraps=road_corridors.get_required_tree_root_zone_mask_utm,
            )
        )
        # THE MEASUREMENT SECTION 10 TURNS ON. identify_road_corridor_
        # candidates()'s `elif selected_water_zone is None` branch calls
        # this; a real call runs the whole water-suitability pipeline
        # (network and all). Replaced by a counter returning None -- "ran,
        # found nothing" -- so a call is COUNTED, never performed. The
        # control in section 10 proves the counter is reachable.
        self.water_selfcompute = patch(
            mock_patch.object(
                road_corridors, "fetch_and_select_optimal_water_zone",
                new=MagicMock(return_value=None),
            )
        )
        # THE STEP'S OWN GENERATE, patched on ITS module.
        self.identify_roads = patch(
            mock_patch.object(
                road_corridors, "identify_road_corridor_candidates",
                wraps=road_corridors.identify_road_corridor_candidates,
            )
        )
        self.rehydrate_roads = patch(
            mock_patch.object(
                wire_translation, "rehydrate_road_networks",
                wraps=wire_translation.rehydrate_road_networks,
            )
        )
        self.water_union = patch(
            mock_patch.object(wire_translation, "water_zone_union", wraps=wire_translation.water_zone_union)
        )
        self.warm_up_floodplain = patch(
            mock_patch.object(
                session_cache.road_corridors, "_fetch_floodplain_hydric_union",
                wraps=session_cache.road_corridors._fetch_floodplain_hydric_union,
            )
        )
        return self

    def __exit__(self, *exc_info):
        self._stack.close()
        return False

    @property
    def total_network_calls(self) -> int:
        return (
            self.fetch_parcel_data.call_count
            + self.soil_components.call_count
            + self.soil_geometries.call_count
            + self.canopy_refetch.call_count
            + self.canopy_module_refetch.call_count
            + self.roads_refetch.call_count
        )

    def road_selfcomputes(self) -> dict:
        return {
            "delineate_valleys": self.road_valleys.call_count,
            "identify_optimized_production_areas": self.road_production_selfcompute.call_count,
            "_fetch_floodplain_hydric_union": self.road_floodplain.call_count,
            "fetch_and_select_optimal_water_zone": self.water_selfcompute.call_count,
        }


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="roads_step_test_"))


def _fresh_runner():
    return job_runner.JobRunner(max_workers=2, max_jobs=64)


class Session:
    """One created session plus the caches and store behind it."""

    def __init__(self):
        self.store = _fresh_store()
        self.fetch_cache, self.cache = _fresh_caches()
        self.runner = _fresh_runner()
        self.document = session_manager.create_session(
            REAL_BOUNDARY, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )
        self.id = self.document["session_id"]

    def job(self, step_id, params=None):
        return step_orchestrator.generate_step(
            self.id, step_id, self.store, params=params, fetch_cache=self.fetch_cache,
            cache=self.cache, runner=self.runner,
        )

    def generate(self, step_id, params=None):
        job = self.job(step_id, params).wait(timeout=900)
        if job.status != job_runner.STATUS_DONE:
            raise AssertionError(f"{step_id} generate failed: {job.error} ({job.exception!r})")
        return job.result["payload"]

    def roads(self, access_point):
        return self.generate("roads", {"access_point": list(access_point)})

    def discard(self, access_point):
        return step_orchestrator.discard_candidate(
            self.id, "roads", self.store, params={"access_point": list(access_point)},
            fetch_cache=self.fetch_cache, cache=self.cache,
        )

    def layers(self, step_id):
        return step_orchestrator.step_payload(
            self.id, step_id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def commit(self, step_id, features, provenance, base_revision, inputs=None):
        return step_orchestrator.commit_step(
            self.id, step_id, {"type": "FeatureCollection", "features": list(features)},
            provenance, base_revision, self.store, inputs=inputs,
            fetch_cache=self.fetch_cache, cache=self.cache,
        )

    def reopen(self, step_id):
        return step_orchestrator.reopen_step(
            self.id, step_id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def context(self):
        return session_manager.get_session_context(
            self.id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def stored(self):
        return self.store.get(self.id)

    def revision(self, step_id):
        return self.stored()["steps"][step_id].get("revision", 0)

    def commit_landform(self):
        payload = self.generate("landform")
        features = payload["suggested_zones"]["features"]
        assert features, "the fixture must produce production zones to commit"
        return self.commit(
            "landform", features, {f["id"]: "generated" for f in features},
            base_revision=self.revision("landform"),
        )

    def water_zones(self):
        payload = self.generate("water")
        return [
            f for f in payload["survey_zones"]["features"]
            if f["properties"]["layer"] in wire_translation.LAYER_SURVEY_ZONES
        ]

    def commit_water(self, zones):
        return self.commit(
            "water", zones, {f["id"]: "generated" for f in zones},
            base_revision=self.revision("water"),
        )

    def upstream(self, water_zone_count=3):
        """Landform committed whole, water generated and committed with the
        first `water_zone_count` zones (0 for an EMPTY water commit). The
        first three are all embankment zones on this fixture; the access
        points above were surveyed against THAT union, and a different
        selection moves the pond exclusion and changes which edges route a
        network at all (section 11 commits a cross-type selection and says
        so)."""
        self.commit_landform()
        zones = self.water_zones()
        assert len(zones) >= water_zone_count, f"only {len(zones)} water zones on the fixture"
        return self.commit_water(zones[:water_zone_count])


def _spanning_types(zones, count):
    """The first `count` zones taken alternately from each survey type, so a
    three-zone selection is two of one type and one of the other."""
    by_type = {}
    for zone in zones:
        by_type.setdefault(zone["properties"]["survey_type"], []).append(zone)
    picked = []
    while len(picked) < count:
        progressed = False
        for survey_type in sorted(by_type):
            if by_type[survey_type] and len(picked) < count:
                picked.append(by_type[survey_type].pop(0))
                progressed = True
        if not progressed:
            break
    return picked


def _network_features(payload, network_id):
    return [f for f in payload["road_corridors"]["features"] if f["properties"]["network_id"] == network_id]


def _network(payload, network_id):
    matches = [n for n in payload["networks"] if n["network_id"] == network_id]
    assert len(matches) == 1, f"network {network_id!r} appears {len(matches)} times"
    return matches[0]


def _commit_body(payload, network_id):
    features = _network_features(payload, network_id)
    return features, {f["id"]: "generated" for f in features}


def _comparable(payload) -> dict:
    """The payload with nothing order- or identity-dependent stripped: what
    two generates must agree on to be 'the same networks'."""
    return {
        "features": sorted(
            (copy.deepcopy(f) for f in payload["road_corridors"]["features"]), key=lambda f: f["id"]
        ),
        "networks": sorted((copy.deepcopy(n) for n in payload["networks"]), key=lambda n: n["network_id"]),
    }


KEY_A = wire_translation.access_point_key(ACCESS_A)
KEY_B = wire_translation.access_point_key(ACCESS_B)
KEY_C = wire_translation.access_point_key(ACCESS_C)
KEY_D = wire_translation.access_point_key(ACCESS_D)
KEY_NO_NETWORK = wire_translation.access_point_key(ACCESS_NO_NETWORK)

print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} "
    f"vertices, {PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at "
    f"{RESOLUTION_METERS:.0f} m. Same boundary and same DEM fixture as the water step.\n"
    f"Access points, each exactly on the parcel edge:\n"
    f"  A (west edge)        {ACCESS_A}  key {KEY_A}\n"
    f"  B (south-east edge)  {ACCESS_B}  key {KEY_B}\n"
    f"  C (north edge)       {ACCESS_C}  key {KEY_C}\n"
    f"  D (south-west edge)  {ACCESS_D}  key {KEY_D}\n"
    f"  NO_NETWORK (east)    {ACCESS_NO_NETWORK}  key {KEY_NO_NETWORK}\n"
)


# --- 1 [test 1]. THE REGISTRY ENTRY ----------------------------------

step_registry.validate_registry()
assert step_registry.registered_steps() == ("landform", "water", "roads"), step_registry.registered_steps()
ROADS = step_registry.get_step("roads")

assert ROADS.generate == "road_corridors.identify_road_corridor_candidates"
assert ROADS.payload == "step_orchestrator.build_roads_payload"
assert ROADS.proposal_collection == "road_corridors"
assert ROADS.produces == ("selected_road_corridor",)
assert ROADS.upstream_steps() == ("landform", "water"), ROADS.upstream_steps()

_consumed = {c.name: c for c in ROADS.consumes}
assert set(_consumed) == {
    "boundary_coordinates", "dem", "boundary_polygon_utm", "valleys", "canopy_height",
    "hydric_floodplain_union", "floodplain_data_is_fallback", "production_areas",
    "selected_water_zone",
}, sorted(_consumed)
_water_edge = _consumed["selected_water_zone"]
assert _water_edge.source == step_registry.SOURCE_COMMITTED and _water_edge.from_step == "water"
assert _water_edge.combine == "wire_translation.water_zone_union"
assert _water_edge.empty_commit == "water_suitability.NO_WATER_ZONE", (
    "THE line section 10 exists for: without it an empty water commit reaches "
    "the entry point as None and the water self-compute runs"
)
assert step_registry.resolve(_water_edge.empty_commit) is water_suitability.NO_WATER_ZONE

# THE ACCESS POINT, DECLARED. The first real use of user_inputs.
assert len(ROADS.user_inputs) == 1
_access = ROADS.user_inputs[0]
assert isinstance(_access, step_registry.UserInput)
assert _access.name == "access_point" and _access.parameter == "anchor_lon_lat"
assert _access.shape == step_registry.INPUT_SHAPE_LON_LAT
assert _access.validate == "road_corridors.validate_access_point_on_boundary"
assert step_registry.resolve(_access.validate) is road_corridors.validate_access_point_on_boundary, (
    "the validator is the pipeline's own, not a second one"
)

# ACCUMULATION, DECLARED.
assert ROADS.accumulate is not None
assert ROADS.accumulate.keyed_by == "access_point"
assert ROADS.accumulate.inputs_list == "access_points"
assert ROADS.accumulate.feature_key_property == "network_id"
assert ROADS.accumulate.max_candidates == 3
assert step_registry.resolve(ROADS.accumulate.key) is wire_translation.access_point_key

# THE COMMIT CONTRACT: one network or none, counted in networks.
_contract = ROADS.commit_contract
assert _contract.layers == (wire_translation.LAYER_ROAD_CORRIDOR,)
assert _contract.geometry_types == ("LineString",)
assert _contract.min_features == 0 and _contract.max_features == 1
assert _contract.feature_group == "network_id"
assert _contract.group_check == "wire_translation.check_road_network_complete"
assert _contract.rehydrate == "wire_translation.rehydrate_road_networks"
assert _contract.internal_id_parameter is None and _contract.requires_provenance
assert callable(step_registry.resolve(_contract.rehydrate))
assert callable(step_registry.resolve(_contract.group_check))
assert callable(ROADS.resolve_generate()) and callable(ROADS.resolve_payload())

# EVERY FORWARDED NAME IS A REAL PARAMETER of the real entry point.
import inspect as _inspect  # noqa: E402

_signature = _inspect.signature(road_corridors.identify_road_corridor_candidates).parameters
for _c in ROADS.consumes:
    if _c.forward_as:
        assert _c.forward_as in _signature, f"{_c.name} forwards as {_c.forward_as!r}, not a parameter"
assert _access.parameter in _signature

# THE CASCADE EDGES, read off the declarations.
assert step_registry.dependents_of("water") == ("roads",)
assert step_registry.dependents_of("landform") == ("water", "roads")
assert step_registry.transitive_dependents("landform") == ("water", "roads")
assert step_registry.transitive_dependents("roads") == ()

# CONSTANTS AGREE with the modules that own them.
assert parcel_data.LAYER_CANOPY == production_zone_payload.LAYER_CANOPY

# THE BARE-STRING SHAPE IS REJECTED BY NAME.
import dataclasses as _dc  # noqa: E402

try:
    with mock_patch.dict(step_registry.STEP_REGISTRY, {"roads": _dc.replace(ROADS, user_inputs=("access_point",))}):
        step_registry.validate_registry()
except step_registry.RegistryError as exc:
    assert "not a UserInput" in str(exc), exc
else:
    raise AssertionError("a bare user input name must be rejected")
try:
    with mock_patch.dict(
        step_registry.STEP_REGISTRY,
        {"roads": _dc.replace(ROADS, accumulate=_dc.replace(ROADS.accumulate, keyed_by="anchor"))},
    ):
        step_registry.validate_registry()
except step_registry.RegistryError as exc:
    assert "does not declare" in str(exc), exc
else:
    raise AssertionError("accumulating by an undeclared input must be rejected")

print(
    f"1 [test 1]. REGISTRY: validate_registry() passes with three entries "
    f"{step_registry.registered_steps()}. The roads entry consumes {len(ROADS.consumes)} "
    f"edges (7 cache, 2 committed: landform's production areas, water's zones as a union "
    f"with empty_commit=NO_WATER_ZONE), declares the access point as a UserInput "
    f"(shape {_access.shape!r}, forward_as {_access.parameter!r}, validated by "
    f"road_corridors' own validator), accumulates by it with a cap of "
    f"{ROADS.accumulate.max_candidates}, and commits at most {_contract.max_features} "
    f"{_contract.feature_group} group. dependents_of('water') == "
    f"{step_registry.dependents_of('water')}."
)


# --- 2 [test 2]. GENERATE WITH AN ACCESS POINT -> ONE NETWORK ----------

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=3)

    # THE ACCESS POINT IS NEVER AUTO-ARMED: a roads generate with no params
    # is refused, synchronously, before a job exists.
    try:
        s.job("roads")
    except step_orchestrator.StepOrchestrationError as exc:
        assert "requires user input(s) ['access_point']" in str(exc), exc
    else:
        raise AssertionError("a roads generate must require its access point")

    # SHAPE: a string, a three-element array, a [lat, lon] out of range.
    for bad in ("here", [1, 2, 3], [40.64, -79.98, 0][:2][::-1], [200.0, 40.6], [True, 1.0]):
        try:
            s.job("roads", {"access_point": bad})
        except step_orchestrator.StepOrchestrationError as exc:
            assert "[lon, lat]" in str(exc) or "rejected" in str(exc), (bad, exc)
        else:
            raise AssertionError(f"{bad!r} must be refused as an access point")

    # ON THE BOUNDARY: the parcel centroid is not where the parcel meets a
    # road. road_corridors' own validator, through the registry.
    try:
        s.job("roads", {"access_point": list(INTERIOR_POINT)})
    except step_orchestrator.StepOrchestrationError as exc:
        assert "from the property boundary edge" in str(exc), exc
    else:
        raise AssertionError("an interior point must be refused as an access point")
    assert h.identify_roads.call_count == 0, "no job was created for any refusal above"

    network_before = h.total_network_calls
    selfcomputes_before = h.road_selfcomputes()
    payload_a = s.roads(ACCESS_A)
    roads_network_calls = h.total_network_calls - network_before
    selfcomputes = {k: v - selfcomputes_before[k] for k, v in h.road_selfcomputes().items()}

    assert sorted(payload_a) == ["networks", "road_corridors", "summary"], sorted(payload_a)
    assert h.identify_roads.call_count == 1
    assert len(payload_a["networks"]) == 1
    NETWORK_A = _network(payload_a, KEY_A)
    assert NETWORK_A["network_found"], (
        f"the fixture must produce a network from A, or every assertion below is "
        f"vacuous: stop_reason {NETWORK_A['stop_reason']!r}"
    )
    FEATURES_A = _network_features(payload_a, KEY_A)
    assert FEATURES_A and len(FEATURES_A) == NETWORK_A["access"]["branch_count"]
    assert len(payload_a["road_corridors"]["features"]) == len(FEATURES_A)
    assert NETWORK_A["feature_ids"] == [f["id"] for f in FEATURES_A]
    assert NETWORK_A["access_point"] == [ACCESS_A[0], ACCESS_A[1]]

    # THE BRANCHES ARE A TREE, not alternatives: exactly one trunk, every
    # other branch joins one that is in the network.
    roles = [f["properties"]["branch_role"] for f in FEATURES_A]
    indexes = {f["properties"]["branch_index"] for f in FEATURES_A}
    assert roles.count("trunk") == 1 and roles[0] == "trunk"
    for f in FEATURES_A[1:]:
        assert f["properties"]["joins_branch_index"] in indexes
    for f in FEATURES_A:
        assert f["properties"]["layer"] == wire_translation.LAYER_ROAD_CORRIDOR
        assert f["geometry"]["type"] == "LineString"
        assert f["properties"]["network_id"] == KEY_A
        assert f["properties"]["access_point"] == [ACCESS_A[0], ACCESS_A[1]]
        assert f["id"].startswith(f"road-corridor-{KEY_A}-"), f["id"]
        assert "unserved_acres" in f["properties"]

    # THE PER-NETWORK NARRATIVE BLOCK, whole, on the network record.
    for key in ("network_found", "stop_reason", "determination", "access", "branches"):
        assert key in NETWORK_A, sorted(NETWORK_A)
    assert NETWORK_A["determination"]["water_zone_excluded"] is True, (
        "three water zones were committed; the union must have been hard-excluded"
    )
    assert isinstance(NETWORK_A["access"]["reaches_water_zone"], bool)
    assert NETWORK_A["determination"]["floodplain_data_available"] is True, (
        "the warm-up's floodplain union (hydric fixture polygon) must have reached routing"
    )
    assert NETWORK_A["determination"]["floodplain_data_is_fallback"] is False
    assert NETWORK_A["determination"]["canopy_data_available"] is True
    assert payload_a["summary"] == {"network_count": 1, "max_networks": 3, "slots_remaining": 2}

    # ZERO NETWORK, ZERO SELF-COMPUTE. Every one of the nine edges did its job.
    assert roads_network_calls == 0, roads_network_calls
    assert selfcomputes == {
        "delineate_valleys": 0,
        "identify_optimized_production_areas": 0,
        "_fetch_floodplain_hydric_union": 0,
        "fetch_and_select_optimal_water_zone": 0,
    }, selfcomputes
    assert h.road_canopy_mask.call_count == 1 and h.canopy_refetch.call_count == 0

    # THE DOCUMENT: generated, no features, and the access point RECORDED.
    entry = s.stored()["steps"]["roads"]
    assert entry["status"] == design_document.STATUS_GENERATED
    assert not entry.get("features")
    assert entry["inputs"] == {"access_points": [[ACCESS_A[0], ACCESS_A[1]]]}, entry.get("inputs")

    # THE READ VERB returns the same payload from the cache.
    assert s.layers("roads") == payload_a

print(
    f"2 [test 2]. GENERATE: with access point A, ONE network -- "
    f"{NETWORK_A['access']['branch_count']} branch(es) "
    f"({', '.join(roles)}), {NETWORK_A['access']['total_length_ft']} ft, "
    f"{NETWORK_A['access']['served_acres']} acres served of "
    f"{NETWORK_A['access']['served_acres'] + NETWORK_A['access']['unserved_acres']:.1f}, "
    f"stop_reason {NETWORK_A['stop_reason']!r}, reaches_water_zone="
    f"{NETWORK_A['access']['reaches_water_zone']}. {roads_network_calls} network calls "
    f"and every road self-compute at zero. A missing, malformed, or interior access "
    f"point is refused before a job exists. The document records the access point."
)


# --- 3-7. ACCUMULATION, ID STABILITY, INDEPENDENCE, CAP, DISCARD -------
#
# One session, because these are five statements about ONE store of
# candidate sets as it grows, is evicted, is regenerated into, hits its cap
# and is discarded from. A fresh session per section would test five
# stores that never held more than one thing.

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=3)

    payload_a = s.roads(ACCESS_A)
    FEATURES_A = copy.deepcopy(_network_features(payload_a, KEY_A))
    NETWORK_A = copy.deepcopy(_network(payload_a, KEY_A))
    IDS_A = [f["id"] for f in FEATURES_A]
    revision_after_a = s.stored()["document_revision"]

    # --- 3 [test 3]. ACCUMULATION: A, then B. Both exist; A is untouched.
    payload_ab = s.roads(ACCESS_B)
    assert [n["network_id"] for n in payload_ab["networks"]] == [KEY_A, KEY_B], (
        "generating for B must ADD a candidate, in the order tried, not replace A"
    )
    assert _network_features(payload_ab, KEY_A) == FEATURES_A, (
        "A's features must be byte-identical after B's generate"
    )
    assert _network(payload_ab, KEY_A) == NETWORK_A, "A's narrative block must be untouched"
    NETWORK_B = _network(payload_ab, KEY_B)
    FEATURES_B = _network_features(payload_ab, KEY_B)
    assert NETWORK_B["network_found"] and FEATURES_B, NETWORK_B["stop_reason"]
    assert len(payload_ab["road_corridors"]["features"]) == len(FEATURES_A) + len(FEATURES_B)
    assert h.identify_roads.call_count == 2, (
        f"B's generate must not recompute A: {h.identify_roads.call_count} entry-point calls"
    )
    assert s.stored()["steps"]["roads"]["inputs"] == {
        "access_points": [list(ACCESS_A), list(ACCESS_B)]
    }
    assert payload_ab["summary"] == {"network_count": 2, "max_networks": 3, "slots_remaining": 1}

    # THE CACHE SHAPE: keyed, in order, one result per access point.
    store = s.context().step_proposals["roads"]
    assert list(store) == [KEY_A, KEY_B]
    assert store[KEY_A]["inputs"] == {"access_point": ACCESS_A}
    assert "road_network" in store[KEY_A]["result"]

    print(
        f"3 [test 3]. ACCUMULATION: A then B -> two candidates in the order tried "
        f"([{KEY_A}, {KEY_B}]); A's {len(FEATURES_A)} features and narrative block are "
        f"byte-identical after B's generate; B has {len(FEATURES_B)} branches "
        f"({NETWORK_B['access']['served_acres']} acres served). The entry point ran "
        f"{h.identify_roads.call_count} times -- once per access point, never again for A. "
        f"The document records both access points; the cache holds a keyed store."
    )

    # --- 4 [test 4]. ID STABILITY across accumulated generates and an
    # eviction. The ids carry the access point's identity, so B's
    # numbering cannot renumber A's.
    assert [f["id"] for f in _network_features(payload_ab, KEY_A)] == IDS_A
    IDS_B = [f["id"] for f in FEATURES_B]
    assert not set(IDS_A) & set(IDS_B), "two networks must never share a feature id"
    assert all(i.startswith(f"road-corridor-{KEY_B}-") for i in IDS_B)
    # A's branch ordinals and B's are both 1..n -- the collision the key
    # exists to prevent, shown rather than assumed.
    ordinals_a = [wire_translation.internal_road_branch_identity(i)[1] for i in IDS_A]
    ordinals_b = [wire_translation.internal_road_branch_identity(i)[1] for i in IDS_B]
    assert ordinals_a[0] == ordinals_b[0] == 0, "both trunks are branch 0 -- only the key tells them apart"

    calls_before_eviction = h.identify_roads.call_count
    assert s.cache.discard(s.id), "the session should have been cached"
    assert s.id not in s.cache
    rebuilt = s.layers("roads")
    assert h.identify_roads.call_count == calls_before_eviction + 2, (
        "a cold read regenerates EVERY recorded access point -- both of them"
    )
    assert _comparable(rebuilt) == _comparable(payload_ab), "the rebuild must reproduce both networks"
    assert [n["network_id"] for n in rebuilt["networks"]] == [KEY_A, KEY_B], "in the document's order"
    assert [f["id"] for f in _network_features(rebuilt, KEY_A)] == IDS_A
    assert [f["id"] for f in _network_features(rebuilt, KEY_B)] == IDS_B

    print(
        f"4 [test 4]. ID STABILITY: A's ids {IDS_A} are unchanged by B's generate, "
        f"disjoint from B's {IDS_B} though both trunks are branch 0, and both id sets "
        f"survive a cache eviction and a rebuild from the document's recorded access "
        f"points ({h.identify_roads.call_count - calls_before_eviction} regenerates)."
    )

    # --- 5 [test 5]. INDEPENDENCE. THE ONE THAT DECIDES WHETHER THE
    # INTERACTION IS SOUND. If a second generate saw the first network as
    # existing infrastructure, the alternatives would not be comparable.
    #
    # (a) The same access point twice is identical -- and replaces, holding
    #     no new slot and writing nothing to the document.
    revision_before = s.stored()["document_revision"]
    payload_aab = s.roads(ACCESS_A)
    assert _network_features(payload_aab, KEY_A) == FEATURES_A
    assert _network(payload_aab, KEY_A) == NETWORK_A
    assert _network_features(payload_aab, KEY_B) == FEATURES_B, "B untouched by A's regenerate"
    assert [n["network_id"] for n in payload_aab["networks"]] == [KEY_A, KEY_B]
    assert s.stored()["document_revision"] == revision_before, (
        "a regenerate for a recorded access point writes nothing"
    )

    # (b) B then A, in a fresh session, equals A then B -- feature for
    #     feature: geometry, length, grade, served acres, ids.
    s2 = Session()
    s2.upstream(water_zone_count=3)
    payload_b_first = s2.roads(ACCESS_B)
    payload_ba = s2.roads(ACCESS_A)
    assert [n["network_id"] for n in payload_ba["networks"]] == [KEY_B, KEY_A], "order tried is kept"
    assert _comparable(payload_ba) == _comparable(payload_ab), (
        "B-then-A must equal A-then-B. It does not: the networks depend on the "
        "order they were generated in, so the alternatives are not comparable "
        "and the accumulate interaction is unsound. STOP."
    )
    # And B generated FIRST (with no A in the session) equals B generated
    # second (with A already there): A was never existing infrastructure.
    assert _network_features(payload_b_first, KEY_B) == FEATURES_B
    assert _network(payload_b_first, KEY_B) == NETWORK_B

    # (c) The geometry is genuinely different between A and B -- so (b) is
    #     not two copies of one network agreeing with themselves.
    assert {f["geometry"]["coordinates"][0] for f in FEATURES_A} != {
        f["geometry"]["coordinates"][0] for f in FEATURES_B
    }, "A and B must start from different cells"
    assert NETWORK_A["access"]["total_length_ft"] != NETWORK_B["access"]["total_length_ft"] or (
        NETWORK_A["branches"] != NETWORK_B["branches"]
    )

    print(
        f"5 [test 5]. INDEPENDENCE: A generated twice is identical and writes "
        f"nothing; B-then-A (fresh session) equals A-then-B feature for feature -- "
        f"same {len(FEATURES_A) + len(FEATURES_B)} features, same geometry, lengths "
        f"({NETWORK_A['access']['total_length_ft']} ft / "
        f"{NETWORK_B['access']['total_length_ft']} ft), served acres "
        f"({NETWORK_A['access']['served_acres']} / {NETWORK_B['access']['served_acres']}) "
        f"and ids; and B generated alone equals B generated after A. Each network "
        f"routes on its own cost surface: an earlier network is never existing "
        f"infrastructure to a later one. ORDER-INDEPENDENT."
    )

    # --- 6 [test 6]. THE CAP, server-side, before a job.
    payload_abc = s.roads(ACCESS_C)
    assert [n["network_id"] for n in payload_abc["networks"]] == [KEY_A, KEY_B, KEY_C]
    NETWORK_C = _network(payload_abc, KEY_C)
    assert NETWORK_C["network_found"], NETWORK_C["stop_reason"]
    assert payload_abc["summary"]["slots_remaining"] == 0
    calls_at_cap = h.identify_roads.call_count
    try:
        s.job("roads", {"access_point": list(ACCESS_D)})
    except step_orchestrator.CandidateCapReachedError as exc:
        assert exc.step_id == "roads" and exc.max_candidates == 3
        assert exc.candidates == [list(ACCESS_A), list(ACCESS_B), list(ACCESS_C)]
    else:
        raise AssertionError("a fourth access point must be refused")
    assert h.identify_roads.call_count == calls_at_cap, "refused BEFORE a job -- nothing ran"
    assert s.stored()["steps"]["roads"]["inputs"]["access_points"] == [
        list(ACCESS_A), list(ACCESS_B), list(ACCESS_C)
    ], "nothing was recorded for D"
    # A regenerate for a RECORDED access point is not a fourth candidate.
    payload_at_cap = s.roads(ACCESS_B)
    assert [n["network_id"] for n in payload_at_cap["networks"]] == [KEY_A, KEY_B, KEY_C]
    # THE CAP IS THE DOCUMENT'S, not the cache's: evict, and it still holds.
    s.cache.discard(s.id)
    try:
        s.job("roads", {"access_point": list(ACCESS_D)})
    except step_orchestrator.CandidateCapReachedError:
        pass
    else:
        raise AssertionError("the cap must hold across an eviction")

    print(
        f"6 [test 6]. CAP: with A, B and C held, a fourth access point (D) is refused "
        f"synchronously with CandidateCapReachedError naming the three candidates -- "
        f"no job, no entry-point call, nothing recorded -- while a regenerate for B "
        f"still replaces B's set. The cap holds against the document, so it survives "
        f"an eviction."
    )

    # --- 7 [test 7]. DISCARD frees a slot.
    discarded = s.discard(ACCESS_B)
    assert discarded["steps"]["roads"]["status"] == design_document.STATUS_GENERATED
    assert discarded["steps"]["roads"]["inputs"] == {"access_points": [list(ACCESS_A), list(ACCESS_C)]}
    payload_ac = s.layers("roads")
    assert [n["network_id"] for n in payload_ac["networks"]] == [KEY_A, KEY_C]
    assert _network_features(payload_ac, KEY_A) == FEATURES_A
    assert _network(payload_ac, KEY_C) == NETWORK_C
    assert payload_ac["summary"]["slots_remaining"] == 1
    assert KEY_B not in s.context().step_proposals["roads"]
    try:
        s.discard(ACCESS_B)
    except step_orchestrator.CandidateNotFoundError:
        pass
    else:
        raise AssertionError("discarding a candidate twice must be refused")
    # A step that does not accumulate has nothing to discard.
    try:
        step_orchestrator.discard_candidate(s.id, "water", s.store, params={}, fetch_cache=s.fetch_cache, cache=s.cache)
    except step_orchestrator.StepOrchestrationError as exc:
        assert "does not accumulate" in str(exc)
    else:
        raise AssertionError("water does not accumulate")
    # The freed slot takes D.
    payload_acd = s.roads(ACCESS_D)
    assert [n["network_id"] for n in payload_acd["networks"]] == [KEY_A, KEY_C, KEY_D]
    assert _network(payload_acd, KEY_D)["network_found"]

    print(
        f"7 [test 7]. DISCARD: discarding B leaves [A, C] in the document and the "
        f"cache (A and C byte-identical), a second discard of B is CandidateNotFoundError, "
        f"water has nothing to discard, and the freed slot takes D -> [A, C, D]."
    )


# --- 8, 12, 13, 9. THE COMMIT: ONE NETWORK OR NONE, ITS INPUT REQUIRED,
# EVERY CANDIDATE RESTORED, EMPTY LEGAL -------------------------------

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=3)
    payload = s.roads(ACCESS_A)
    payload = s.roads(ACCESS_B)
    payload = s.roads(ACCESS_C)
    features_a, provenance_a = _commit_body(payload, KEY_A)
    features_b, provenance_b = _commit_body(payload, KEY_B)
    ALL_INPUTS = {"access_points": [list(ACCESS_A), list(ACCESS_B), list(ACCESS_C)]}
    assert len(features_a) >= 2 and len(features_b) >= 2, "both networks need a spur for section 8"

    # --- 8 [test 8]. max_features 1, COUNTED IN NETWORKS.
    try:
        s.commit("roads", features_a + features_b, {**provenance_a, **provenance_b},
                 base_revision=0, inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        codes = [r.code for r in exc.rejections]
        assert commit_validation.REJECT_TOO_MANY in codes, codes
        reason = next(r.reason for r in exc.rejections if r.code == commit_validation.REJECT_TOO_MANY)
        assert "at most 1 network_id group" in reason and "2 were committed" in reason, reason
    else:
        raise AssertionError("a two-network commit must be rejected")
    # ...and NOT merely "too many features": one whole network of 5
    # branches is fine, because the unit is the network.
    committed_b = s.commit("roads", features_b, provenance_b, base_revision=0, inputs=ALL_INPUTS)
    assert committed_b["steps"]["roads"]["status"] == design_document.STATUS_COMMITTED
    assert len(committed_b["steps"]["roads"]["features"]["features"]) == len(features_b)
    s.reopen("roads")
    # HALF A NETWORK IS INCOHERENT: a spur without its trunk.
    spur_only = [f for f in features_a if f["properties"]["branch_role"] != "trunk"]
    try:
        s.commit("roads", spur_only, {f["id"]: "generated" for f in spur_only},
                 base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        codes = {r.code for r in exc.rejections}
        assert codes == {commit_validation.REJECT_INCOHERENT_GROUP}, codes
        assert "trunk (branch 0) is not in the commit" in exc.rejections[0].reason
    else:
        raise AssertionError("a spur without its trunk must be rejected")
    # A feature with no network id cannot be counted as anything.
    stripped = copy.deepcopy(features_a)
    del stripped[0]["properties"]["network_id"]
    try:
        s.commit("roads", stripped, provenance_a, base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        assert commit_validation.REJECT_MISSING_GROUP in {r.code for r in exc.rejections}
    else:
        raise AssertionError("a feature with no network_id must be rejected")
    # A foreign id is refused by the rehydrator, not invented for.
    foreign = copy.deepcopy(features_a)
    foreign[0]["id"] = "road-corridor-drawn-by-hand"
    try:
        s.commit("roads", foreign, {**provenance_a, "road-corridor-drawn-by-hand": "user_added"},
                 base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        codes = {r.code for r in exc.rejections}
        assert codes & {commit_validation.REJECT_INCOHERENT_GROUP, commit_validation.REJECT_INVALID_GEOMETRY}, codes
    else:
        raise AssertionError("an id this pipeline did not mint must be refused")
    print(
        f"8 [test 8]. max_features 1: a two-network commit ({len(features_a)} + "
        f"{len(features_b)} branches) is rejected as too_many_features counted in "
        f"network_id groups; one whole {len(features_b)}-branch network commits; a spur "
        f"without its trunk is incoherent_feature_group; a branch with no network_id is "
        f"missing_feature_group; a hand-made id is refused."
    )

    # --- 12 [test 12]. A COMMIT MISSING ITS REQUIRED INPUT IS REJECTED.
    for missing in (None, {}, {"access_point": list(ACCESS_A)}, {"access_points": None}):
        try:
            s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"), inputs=missing)
        except step_orchestrator.CommitInputError as exc:
            assert "access_point" in str(exc), exc
        else:
            raise AssertionError(f"inputs={missing!r} must be refused: an absent input is not a decision")
    assert s.stored()["steps"]["roads"]["status"] == design_document.STATUS_GENERATED, "nothing written"
    # Declared, but not the access point this network came from.
    try:
        s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"),
                 inputs={"access_points": [list(ACCESS_B)]})
    except commit_validation.CommitRejectedError as exc:
        codes = {r.code for r in exc.rejections}
        assert codes == {commit_validation.REJECT_INPUT_NOT_DECLARED}, codes
        assert {r.feature_id for r in exc.rejections} == {f["id"] for f in features_a}
    else:
        raise AssertionError("a network whose access point is not declared must be rejected")
    # Declared, but off the boundary.
    try:
        s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"),
                 inputs={"access_points": [list(ACCESS_A), list(INTERIOR_POINT)]})
    except step_orchestrator.StepOrchestrationError as exc:
        assert "from the property boundary edge" in str(exc)
    else:
        raise AssertionError("an interior access point in a commit must be refused")
    # Declared, wrong shape.
    try:
        s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"),
                 inputs={"access_points": [[ACCESS_A[0]]]})
    except step_orchestrator.StepOrchestrationError as exc:
        assert "[lon, lat]" in str(exc)
    else:
        raise AssertionError("a malformed access point in a commit must be refused")
    print(
        f"12 [test 12]. REQUIRED INPUT: a roads commit with inputs absent, empty, "
        f"singular, or null is CommitInputError (400) with nothing written; one "
        f"declaring only B for A's network is rejected per feature as "
        f"input_not_declared; an interior or malformed access point in the list is "
        f"refused."
    )

    # THE REAL COMMIT: network A, every access point declared.
    committed = s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    entry = committed["steps"]["roads"]
    assert entry["status"] == design_document.STATUS_COMMITTED
    assert entry["inputs"] == ALL_INPUTS, "the document carries EVERY access point tried"
    assert [f["id"] for f in entry["features"]["features"]] == [f["id"] for f in features_a]
    for f in entry["features"]["features"]:
        assert "exclusion_crossings" in f["properties"], "crossings are recorded, as for every step"
    # THE REHYDRATED COMMITTED VALUE: one network, the shape consumers read.
    cached = s.context().step_committed["roads"]["value"]
    assert len(cached) == 1 and cached[0]["network_id"] == KEY_A
    network = cached[0]
    assert len(network["branches"]) == len(features_a)
    assert network["cells"] and not network["cell_footprint_polygon_utm"].is_empty
    assert network["branches"][0]["branch_role"] == "trunk"
    assert abs(network["total_length_meters"] - _network(payload, KEY_A)["access"]["total_length_ft"] * 0.3048) < 0.1
    # ...and it round-trips: the rehydrated cells are the cells the router
    # walked, so a consumer excluding the road excludes the real footprint.
    original = s.context().step_proposals["roads"][KEY_A]["result"]["road_network"]
    assert network["cells"] == original["cells"], "the rehydrated cells must be the routed cells, exactly"
    assert network["cell_footprint_polygon_utm"].equals(original["cell_footprint_polygon_utm"])
    assert step_orchestrator.committed_internal_value(s.context(), s.stored(), "roads")[0]["network_id"] == KEY_A
    # A cold read agrees with the warm one.
    s.cache.discard(s.id)
    cold = step_orchestrator.committed_internal_value(s.context(), s.stored(), "roads")
    assert cold[0]["cells"] == original["cells"]

    # --- 13 [test 13]. REOPEN RESTORES EVERY CANDIDATE.
    s.cache.discard(s.id)  # a reopen after an eviction is the harder case
    calls_before = h.identify_roads.call_count
    reopened = s.reopen("roads")
    assert reopened["steps"]["roads"]["status"] == design_document.STATUS_GENERATED
    assert reopened["steps"]["roads"]["inputs"] == ALL_INPUTS
    restored = s.context().step_restored["roads"]
    assert [n["network_id"] for n in restored["payload"]["networks"]] == [KEY_A, KEY_B, KEY_C], (
        "the restore must bring back ALL THREE candidates, not just the committed one"
    )
    assert h.identify_roads.call_count == calls_before + 3
    assert restored["selected_feature_ids"] == [f["id"] for f in features_a], restored["selected_feature_ids"]
    assert restored["missing_feature_ids"] == [], restored["missing_feature_ids"]
    assert restored["user_added"]["features"] == []
    assert _network_features(restored["payload"], KEY_A) == [
        {**f, "properties": {k: v for k, v in f["properties"].items() if k != "exclusion_crossings"}}
        for f in features_a
    ] or _network_features(restored["payload"], KEY_A) == features_a
    assert _network_features(restored["payload"], KEY_B) == features_b
    # The read verb after a reopen serves the same three.
    assert [n["network_id"] for n in s.layers("roads")["networks"]] == [KEY_A, KEY_B, KEY_C]
    print(
        f"13 [test 13]. REOPEN: after committing A (with A, B and C declared) and "
        f"evicting the cache, a reopen restores ALL THREE candidates "
        f"({h.identify_roads.call_count - calls_before} regenerates), re-selects "
        f"A's {len(restored['selected_feature_ids'])} features with nothing missing, and "
        f"the layers read serves the same three."
    )

    # --- 9 [test 9]. AN EMPTY COMMIT IS LEGAL AND RECORDS NO ROAD.
    empty = s.commit("roads", [], {}, base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    entry = empty["steps"]["roads"]
    assert entry["status"] == design_document.STATUS_COMMITTED
    assert entry["features"]["features"] == []
    assert entry["inputs"] == ALL_INPUTS, "the tried access points are still the user's work"
    assert s.context().step_committed["roads"]["value"] == []
    assert step_orchestrator.committed_internal_value(s.context(), s.stored(), "roads") == []
    # And an empty commit with NO access point ever tried -- "no road" with
    # nothing placed -- is legal too: an empty list, not an absent key.
    s3 = Session()
    s3.upstream(water_zone_count=3)
    no_road = s3.commit("roads", [], {}, base_revision=0, inputs={"access_points": []})
    assert no_road["steps"]["roads"]["status"] == design_document.STATUS_COMMITTED
    assert no_road["steps"]["roads"]["inputs"] == {"access_points": []}
    try:
        s3.commit("roads", [], {}, base_revision=1, inputs=None)
    except step_orchestrator.CommitInputError:
        pass
    else:
        raise AssertionError("even an empty roads commit must carry its inputs key")
    print(
        f"9 [test 9]. EMPTY COMMIT: roads committed with zero features is status "
        f"'committed', carries every tried access point, and rehydrates to [] -- no "
        f"road. An empty commit on a never-generated step with access_points=[] is "
        f"legal; one with inputs absent is not."
    )


# --- 10 [test 10]. WATER EMPTY -> SENTINEL. THE ONE THAT FAILS SILENTLY.
#
# A user who commits the water step with nothing selected has DECIDED there
# is no water zone. identify_road_corridor_candidates() line ~1481:
#
#     if selected_water_zone is NO_WATER_ZONE: selected_water_zone = None
#     elif selected_water_zone is None:
#         selected_water_zone = fetch_and_select_optimal_water_zone(...)
#
# Forward None and the second branch runs the whole water pipeline and
# hard-excludes a zone the user never selected. Nothing raises. The
# registry's empty_commit declaration is the only thing between the two.

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=0)
    assert s.stored()["steps"]["water"]["status"] == design_document.STATUS_COMMITTED
    assert s.stored()["steps"]["water"]["features"]["features"] == []

    # READ 1 -- WARM (the cache holds [] for water's rehydration: the trap).
    warm_context = s.context()
    assert warm_context.step_committed["water"]["value"] == []
    warm = step_orchestrator.assemble_consumes(ROADS, warm_context, s.stored())["selected_water_zone"]
    assert warm is water_suitability.NO_WATER_ZONE, f"warm read: {warm!r}"
    # READ 2 -- COLD.
    s.cache.discard(s.id)
    cold = step_orchestrator.assemble_consumes(ROADS, s.context(), s.stored())["selected_water_zone"]
    assert cold is water_suitability.NO_WATER_ZONE, f"cold read: {cold!r}"
    assert h.water_union.call_count == 0, "no union is built for an empty selection"

    # THE MEASUREMENT: a real roads generate with water committed empty.
    assert h.water_selfcompute.call_count == 0
    payload = s.roads(ACCESS_C)
    assert h.water_selfcompute.call_count == 0, (
        f"fetch_and_select_optimal_water_zone() ran {h.water_selfcompute.call_count} "
        f"time(s). The sentinel was not believed: the network just routed was "
        f"hard-excluded from a water zone the user explicitly rejected."
    )
    network = _network(payload, KEY_C)
    assert network["network_found"]
    assert network["determination"]["water_zone_excluded"] is False, (
        "no water zone was committed, so none may have been excluded"
    )
    assert network["access"]["reaches_water_zone"] is False
    assert all(f["properties"]["branch_role"] != "water_spur" for f in _network_features(payload, KEY_C))
    # The entry point received the sentinel BY IDENTITY.
    call = h.identify_roads.call_args
    assert call.kwargs["selected_water_zone"] is water_suitability.NO_WATER_ZONE

    # THE CONTROL: forward None in the sentinel's place and the self-compute
    # DOES run -- which is what makes the zero above a measurement.
    assembled = step_orchestrator.assemble_consumes(ROADS, s.context(), s.stored())
    control = dict(step_orchestrator.forwarded_arguments(ROADS, assembled, {"access_point": ACCESS_C}))
    control["selected_water_zone"] = None
    road_corridors.identify_road_corridor_candidates(**control)
    assert h.water_selfcompute.call_count == 1, (
        "forwarding None must reach the self-compute -- if it does not, the zero above proves nothing"
    )

    # And C with water committed differs from C with three zones committed
    # in the right direction: the water spur exists only when there is
    # water to reach. (Section 11's session generates C with three zones.)
    NETWORK_C_NO_WATER = network

    print(
        f"10 [test 10]. WATER EMPTY -> SENTINEL: with water committed EMPTY, the roads "
        f"consumes edge resolves to water_suitability.NO_WATER_ZONE by identity on a warm "
        f"read and a cold read, the entry point receives it by identity, and "
        f"fetch_and_select_optimal_water_zone() ran {h.water_selfcompute.call_count - 1} "
        f"times during the generate (water_zone_excluded=False, no water spur). The "
        f"control -- None in its place -- ran it once, so the zero is a measurement."
    )


# --- 11 [test 11]. WATER MULTI-SELECT REACHES ROADS AS A UNION ---------

with Harness() as h:
    s = Session()
    s.commit_landform()
    zones = _spanning_types(s.water_zones(), 3)
    types = {z["properties"]["survey_type"] for z in zones}
    assert len(types) == 2, "the union must span both survey types to say anything about multi-select"
    s.commit_water(zones)

    assembled = step_orchestrator.assemble_consumes(ROADS, s.context(), s.stored())
    union = assembled["selected_water_zone"]
    assert h.water_union.call_count == 1
    assert sorted(union) == ["polygon_utm", "render_fill_polygon_utm", "survey_types", "zone_ids"], sorted(union)
    rehydrated = wire_translation.rehydrate_water_survey_zones(
        {"type": "FeatureCollection", "features": zones}, s.context().dem
    )
    from shapely.ops import unary_union as _unary_union

    assert union["render_fill_polygon_utm"].equals(
        _unary_union([z["render_fill_polygon_utm"] for z in rehydrated])
    ), "the value roads reads is the UNION of the three selected zones' render fills"
    assert union["zone_ids"] == [z["id"] for z in rehydrated]
    assert union["survey_types"] == sorted(types)
    # ROADS READS ONLY render_fill_polygon_utm off it. The union carries no id,
    # rank or elevation, so any other read would KeyError -- and the generate
    # below runs clean, which is the assertion.
    payload = s.roads(ACCESS_C)
    network = _network(payload, KEY_C)
    assert network["network_found"]
    assert network["determination"]["water_zone_excluded"] is True
    call = h.identify_roads.call_args
    forwarded = call.kwargs["selected_water_zone"]
    # NOT `is union`: the combine runs on every read by design (a hit and a
    # miss go through the same reduction), so the entry point got an equal
    # union built for its own call, not this section's object.
    assert forwarded["render_fill_polygon_utm"].equals(union["render_fill_polygon_utm"])
    assert forwarded["zone_ids"] == union["zone_ids"]
    # THE WATER SPUR, PER NETWORK: C reaches the union; A (section 2) did not.
    # Each network's own narrative block answers for itself.
    assert network["access"]["reaches_water_zone"] is True, (
        "C's network must run a water spur to the union on this fixture"
    )
    spur = [f for f in _network_features(payload, KEY_C) if f["properties"]["branch_role"] == "water_spur"]
    assert len(spur) == 1
    assert NETWORK_C_NO_WATER["access"]["reaches_water_zone"] is False
    assert NETWORK_C_NO_WATER["access"]["branch_count"] != network["access"]["branch_count"] or (
        NETWORK_C_NO_WATER["access"]["total_length_ft"] != network["access"]["total_length_ft"]
    ), "the committed water ground must change the network"
    # The spur's end sits just outside the pond buffer -- the road stops
    # at the water's edge, not across it.
    end_lon, end_lat = spur[0]["geometry"]["coordinates"][-1]
    ex, ey = warp_transform("EPSG:4326", CRS, [end_lon], [end_lat])
    distance = union["render_fill_polygon_utm"].distance(Point(ex[0], ey[0]))
    assert road_corridors.POND_ZONE_EXCLUSION_BUFFER_METERS <= distance <= (
        road_corridors.POND_ZONE_EXCLUSION_BUFFER_METERS + 2 * RESOLUTION_METERS
    ), distance
    # A network generated with ONE zone committed sees a smaller union.
    s.reopen("water")
    s.commit_water(zones[:1])
    single = step_orchestrator.assemble_consumes(ROADS, s.context(), s.stored())["selected_water_zone"]
    assert single["zone_ids"] == [rehydrated[0]["id"]]
    assert single["render_fill_polygon_utm"].area < union["render_fill_polygon_utm"].area

    print(
        f"11 [test 11]. UNION: three water zones across {sorted(types)} reach the roads "
        f"consumes edge as ONE value whose render_fill_polygon_utm equals the shapely "
        f"union of the three (zone_ids {union['zone_ids']}), carrying no id, rank or "
        f"elevation. The generate from C runs clean on it, hard-excludes it, and its "
        f"OWN narrative block says reaches_water_zone=True with one water spur ending "
        f"{distance:.1f} m from the union (buffer {road_corridors.POND_ZONE_EXCLUSION_BUFFER_METERS} m) "
        f"-- where the same access point with water committed empty said False. A "
        f"one-zone commit reaches roads as a smaller union."
    )


# --- 14 [test 14]. _API_ERRORS: A FAILED POST /api/sessions NAMES ITS LAYER

class Http:
    def __init__(self):
        self.deps = session_api.Dependencies(
            store=JSONFileStore(tempfile.mkdtemp(prefix="roads_api_test_")),
            fetch_cache=session_cache.FetchCache(max_entries=8),
            cache=session_cache.SessionCache(max_sessions=8, idle_timeout_seconds=1800.0),
            runner=job_runner.JobRunner(max_workers=2, max_jobs=64),
        )
        self.client = session_api.create_app(self.deps).test_client()

    def create(self):
        return self.client.post("/api/sessions", json={"boundary": [list(p) for p in REAL_BOUNDARY]})

    def poll(self, job_id):
        import time as _time

        for _ in range(900):
            response = self.client.get(f"/api/jobs/{job_id}")
            if response.get_json()["status"] in ("done", "failed"):
                return response
            _time.sleep(0.2)
        raise AssertionError("job did not finish")

    def generate(self, session_id, step_id, params=None):
        response = self.client.post(
            f"/api/sessions/{session_id}/steps/{step_id}/generate",
            json={"params": params} if params is not None else {},
        )
        if response.status_code != 202:
            return response
        return self.poll(response.get_json()["job_id"])


with Harness() as h:
    api = Http()
    failures = (
        (
            parcel_data.ParcelDataIncompleteError("no HAG coverage", *parcel_data.LAYER_CANOPY),
            {"type": "canopy", "label": "tree canopy height"},
        ),
        (
            parcel_data.ParcelDataIncompleteError("no scene", *parcel_data.LAYER_IMAGERY),
            {"type": "imagery", "label": "satellite imagery"},
        ),
        (
            production_zone_payload.LayerFetchError(*production_zone_payload.LAYER_ELEVATION),
            {"type": "elevation", "label": "elevation data"},
        ),
        (
            canopy_height_data.CanopyCoverageIncompleteError("too sparse"),
            {"type": "canopy", "label": "tree canopy height"},
        ),
    )
    for exc, expected in failures:
        with mock_patch.object(parcel_data, "fetch_parcel_data", side_effect=exc):
            response = api.create()
        assert response.status_code == 502, (type(exc).__name__, response.status_code, response.get_json())
        body = response.get_json()
        assert body["failed_layer"] == expected, (type(exc).__name__, body)
        assert body["error"] == f"The {expected['label']} could not be retrieved.", body
        assert "Traceback" not in body["error"]
    # A raise site that names no layer reports the generic error and NO
    # failed_layer -- the frontend's "the data sources did not respond".
    with mock_patch.object(parcel_data, "fetch_parcel_data", side_effect=parcel_data.ParcelDataIncompleteError("x")):
        response = api.create()
    assert response.status_code == 502 and "failed_layer" not in response.get_json(), response.get_json()
    assert len(api.deps.store.list_sessions()) == 0, "no session is created by a failed fetch"

    # AND THE ROADS VERBS OVER HTTP, end to end: the shapes a frontend will
    # actually receive.
    created = api.create()
    assert created.status_code == 201, created.get_json()
    session_id = created.get_json()["session_id"]

    def http_commit(step_id, features, provenance, inputs=None, **extra):
        body = {"features": {"type": "FeatureCollection", "features": features}, "provenance": provenance,
                "base_revision": api.client.get(f"/api/sessions/{session_id}").get_json()["steps"][step_id].get("revision", 0)}
        if inputs is not None:
            body["inputs"] = inputs
        body.update(extra)
        return api.client.post(f"/api/sessions/{session_id}/steps/{step_id}/commit", json=body)

    landform = api.generate(session_id, "landform").get_json()["result"]["payload"]
    zones = landform["suggested_zones"]["features"]
    assert http_commit("landform", zones, {f["id"]: "generated" for f in zones}).status_code == 200
    water = api.generate(session_id, "water").get_json()["result"]["payload"]
    water_zones = [f for f in water["survey_zones"]["features"] if f["properties"]["layer"] in wire_translation.LAYER_SURVEY_ZONES]
    assert http_commit("water", water_zones[:3], {f["id"]: "generated" for f in water_zones[:3]}).status_code == 200

    # 400: no access point; 400: an interior one; 202 + done: a real one.
    assert api.generate(session_id, "roads").status_code == 400
    interior = api.generate(session_id, "roads", {"access_point": list(INTERIOR_POINT)})
    assert interior.status_code == 400 and "boundary" in interior.get_json()["error"]
    done = api.generate(session_id, "roads", {"access_point": list(ACCESS_A)}).get_json()
    assert done["status"] == "done", done
    assert [n["network_id"] for n in done["result"]["payload"]["networks"]] == [KEY_A]
    assert done["result"]["document"]["steps"]["roads"]["inputs"] == {"access_points": [list(ACCESS_A)]}
    api.generate(session_id, "roads", {"access_point": list(ACCESS_B)})
    api.generate(session_id, "roads", {"access_point": list(ACCESS_C)})
    # 409: the cap, naming the candidates.
    capped = api.generate(session_id, "roads", {"access_point": list(ACCESS_D)})
    assert capped.status_code == 409, capped.get_json()
    assert capped.get_json()["max_candidates"] == 3 and len(capped.get_json()["candidates"]) == 3
    # 200: discard, then the slot is free.
    discarded = api.client.post(
        f"/api/sessions/{session_id}/steps/roads/discard", json={"params": {"access_point": list(ACCESS_B)}}
    )
    assert discarded.status_code == 200, discarded.get_json()
    assert discarded.get_json()["steps"]["roads"]["inputs"]["access_points"] == [list(ACCESS_A), list(ACCESS_C)]
    assert api.client.post(
        f"/api/sessions/{session_id}/steps/roads/discard", json={"params": {"access_point": list(ACCESS_B)}}
    ).status_code == 404
    layers = api.client.get(f"/api/sessions/{session_id}/steps/roads/layers").get_json()
    assert [n["network_id"] for n in layers["networks"]] == [KEY_A, KEY_C]
    # 400: a commit missing its input; 422: two networks; 200: one.
    features_a = _network_features(layers, KEY_A)
    features_c = _network_features(layers, KEY_C)
    missing = http_commit("roads", features_a, {f["id"]: "generated" for f in features_a})
    assert missing.status_code == 400 and "access_point" in missing.get_json()["error"], missing.get_json()
    two = http_commit(
        "roads", features_a + features_c, {f["id"]: "generated" for f in features_a + features_c},
        inputs={"access_points": [list(ACCESS_A), list(ACCESS_C)]},
    )
    assert two.status_code == 422 and any(r["code"] == "too_many_features" for r in two.get_json()["rejections"])
    one = http_commit(
        "roads", features_a, {f["id"]: "generated" for f in features_a},
        inputs={"access_points": [list(ACCESS_A), list(ACCESS_C)]},
    )
    assert one.status_code == 200, one.get_json()
    assert one.get_json()["steps"]["roads"]["status"] == "committed"
    # Reopen over HTTP restores both candidates through the layers read.
    assert api.client.post(f"/api/sessions/{session_id}/steps/roads/reopen").status_code == 200
    restored = api.client.get(f"/api/sessions/{session_id}/steps/roads/layers").get_json()
    assert [n["network_id"] for n in restored["networks"]] == [KEY_A, KEY_C]

    print(
        f"14 [test 14]. _API_ERRORS: a failed POST /api/sessions returns 502 with "
        f"failed_layer for ParcelDataIncompleteError (canopy, imagery), LayerFetchError "
        f"(elevation) and CanopyCoverageIncompleteError (canopy); a raise with no layer "
        f"returns 502 and no failed_layer; no session is created. Over HTTP the roads "
        f"verbs answer 400 (no/interior access point, missing commit input), 202+done "
        f"(generate), 409 (cap, naming {capped.get_json()['max_candidates']} candidates), "
        f"200/404 (discard), 422 (two networks), 200 (one network), and reopen restores both."
    )

print("\nAll roads step checks passed.")
