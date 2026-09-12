"""
roads_step_fixture.py

THE ROADS STEP'S FIXTURE, IMPORTABLE WITHOUT RUNNING ITS TESTS.

The real parcel (reference_fixture.py), the DEM, the surveyed access
points, the mocked network rows, the Harness and the Session -- verbatim
from test_roads_step.py. The roads sections are split across
test_roads_step.py (sections 1-9: generate, accumulate, cap, discard,
commit rules) and test_roads_step_inputs.py (sections 10-15: the water
sentinel and union, the API error shape, a router failure) so the two
halves run side by side under run_tests.py; both import from here.
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
import road_network_router
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

# The parcel, its UTM CRS, the projected polygon and its acreage -- one
# definition shared by every step test (see reference_fixture.py).
from reference_fixture import BOUNDARY_POLYGON_UTM, CRS, PARCEL_ACRES, REAL_BOUNDARY  # noqa: E402


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


# FOUR REAL ACCESS POINTS on four different edges, RE-SURVEYED for this
# branch's routing constants and moved where the answer moved. Thirty edge
# points were measured again; these are chosen so each yields a DIFFERENT
# network: A on the long west edge (N Montour Rd side) a three-branch
# network; B on the north-east edge the longest and richest one on the
# parcel; C on the north edge, the one with a WATER SPUR to the committed
# zones; D on the south edge a single trunk, for the cap test.
#
# B MOVED, AND WHY IT HAD TO. It sat on the short east edge and grew five
# branches there. PRODUCTION_SERVICE_RADIUS_METERS had gone to 25 m from
# 100, so a road cell served a sixteenth of the ground it used to and every
# network on this parcel was smaller: that edge yielded a lone trunk at
# every fraction measured, and section 8 needs a network with a spur to
# reject half of. The east edge could not supply one, so B is on the
# north-east edge. Its compass labels below were also wrong before this
# (edge 2 is east, not south-east; edge 1 is south, not south-west) and are
# now the measured bearing of each edge's own midpoint.
#
# THE RADIUS HAS SINCE GONE 25 -> 50, and the four points above all still
# stand: each still routes and each still yields a different network, so
# none had to move. The NETWORKS they yield did move, and substantially --
# see PRODUCTION_SERVICE_RADIUS_METERS' own comment for the measured
# before/after on A, B and C. Only the refusing point below had to be
# re-surveyed, because "refuses" is the one property a wider service
# radius can take away.
#
# A FIFTH, NO_NETWORK, IS AN ACCESS POINT THE ROUTER REFUSES, and finding
# one is no longer free. It used to sit on the east edge and be refused by
# road_corridors.MIN_CORRIDOR_LENGTH_METERS -- the router built an 87.4 m
# network there and the length floor threw it away. That floor is gone, so
# that point now routes and the refusal has to be the ROUTER'S OWN.
# Re-surveying found points that route nothing on the SOUTH edge (edge 1),
# and this is one of them: the cheapest extension the router can find from
# here already costs more than MAX_ROAD_METERS_PER_SERVED_ACRE per acre it
# would serve, so it stops before accepting a single branch (stop_reason
# 'cost_per_acre_exceeded', branches=[]). It shares an edge with D as a
# result, which the four above deliberately do not -- there is no fifth
# edge that refuses.
#
# THAT SURVEY WAS MEASURED AT A 200 m/acre CEILING, AND THE CEILING HAS
# SINCE MOVED TO 500. At 500 the router pays two and a half times as much
# real road per acre before it stops, and this parcel no longer contains an
# access point it refuses AT ALL: a re-survey of 114 points -- every edge,
# every 5% of its length -- routed a network from all 114, NO_NETWORK
# included. So section 15 PINS the ceiling to the 200 the survey was run
# at, for its own generate only, exactly the way test_road_network_router.py
# pins the fixture geometry its own sections were measured against. It is a
# CONFIGURABLE constant, and a fixture measured at one value asserts nothing
# once another is substituted under it.
#
# THE REFUSAL IS STILL THE TERRAIN'S, NOT A MOCK'S, which is what makes it
# worth the survey: a full, real routing pass runs over the real exclusions
# and declines every candidate it finds by its own stopping rule -- only the
# threshold that rule compares against is pinned. Section 15 asserts what
# the orchestrator does with an access point that routes nothing, and a
# retry from the same point is refused identically because nothing about it
# is chance.
#
# AND BOTH THE PIN AND THE POINT HAVE MOVED AGAIN, 200 -> 120 AND
# EDGE 1 AT 0.35 -> 0.60, FOR THE SAME REASON THE PIN EXISTS.
# PRODUCTION_SERVICE_RADIUS_METERS went 25 -> 50, which QUADRUPLES the
# ground one road cell serves and so roughly halves every candidate's
# metres-per-served-acre. A ceiling the router used to hit it now
# clears: re-running the same survey at 200 -- all six edges, every 5%
# of each edge's length, 120 real routing passes -- found ZERO refusing
# points on this parcel, the old NO_NETWORK included, so the 200 pin
# stopped pinning anything. That is not a fixture problem; it is the
# constant change working exactly as predicted, measured.
#
# THE SECTION NEEDS TWO THINGS AT ONCE, which is what fixed the new
# pair: A, B, C and D must all still route (the section fills the cap
# with three of them and is refused a fourth), and NO_NETWORK must not.
# Sweeping the ceiling at the new radius:
#
#     ceiling   A B C D all route   points that refuse
#     -------   -----------------   ----------------------------------
#       150            yes          edge 1 @ 0.60
#       130            yes          edge 1 @ 0.60, 0.70
#       120            yes          edge 1 @ 0.60, 0.70
#       110            yes          edge 1 @ 0.60, 0.70
#       100            yes          edge 1 @ 0.20, 0.30, 0.60, 0.70
#        90         NO -- D refuses  edge 1 @ 0.20-0.45, 0.60, 0.70
#
# 120 is chosen mid-band rather than at either edge: D's own flip is
# between 90 and 100, and edge 1 @ 0.60 routes somewhere above 150, so
# neither a small terrain change nor a small tuning change silently
# flips the section's premise. TWO points refuse at 120, not one, so
# the choice does not rest on a single knife-edge cell either.
NO_NETWORK_CEILING_METERS_PER_ACRE = 120.0
ACCESS_A = _boundary_point(0, 0.85)
ACCESS_B = _boundary_point(3, 0.85)
ACCESS_C = _boundary_point(4, 0.50)
ACCESS_D = _boundary_point(1, 0.50)
ACCESS_NO_NETWORK = _boundary_point(1, 0.60)
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

    def upstream(self, water_zone_count=2):
        """Landform committed whole, water generated and committed with the
        first `water_zone_count` EMBANKMENT zones (0 for an EMPTY water
        commit).

        THE SELECTION IS BY TYPE, NOT BY POSITION, and that is the fixture's
        premise rather than a convenience. The access points above were
        surveyed against an EMBANKMENT-ONLY union: a different selection
        moves the pond exclusion and changes which edges route a network at
        all (section 11 commits a cross-type selection and says so). Taking
        a positional slice made that premise depend on the order the water
        payload happened to arrive in, which is not a fact about the ground
        and not this fixture's to rely on -- when the water step began
        shipping its zones in presentation order, `zones[:3]` silently
        became two embankment zones and an excavated one, the pond exclusion
        moved, and ACCESS_B stopped routing. Naming the type says what the
        fixture actually needs.

        THE DEFAULT IS TWO, because two is what the water step now offers of
        a single type: its payload carries the presented set (the top 2 of
        each survey type, backfilled to a fixed count), so an all-embankment
        selection on this parcel is at most those two. Three embankment
        zones are no longer committable here -- not by this fixture and not
        by a user."""
        self.commit_landform()
        zones = [
            zone for zone in self.water_zones()
            if zone["properties"]["survey_type"] == "embankment"
        ]
        assert len(zones) >= water_zone_count, (
            f"only {len(zones)} embankment water zone(s) on the fixture, "
            f"{water_zone_count} needed"
        )
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
    f"  B (north-east edge)  {ACCESS_B}  key {KEY_B}\n"
    f"  C (north edge)       {ACCESS_C}  key {KEY_C}\n"
    f"  D (south edge)       {ACCESS_D}  key {KEY_D}\n"
    f"  NO_NETWORK (south)   {ACCESS_NO_NETWORK}  key {KEY_NO_NETWORK}\n"
)
