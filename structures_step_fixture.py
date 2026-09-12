"""
structures_step_fixture.py

THE STRUCTURES STEP'S FIXTURE, IMPORTABLE WITHOUT RUNNING ITS TESTS.

The real parcel (reference_fixture.py), the DEM and canopy block, the
mocked network rows, the Harness and the Session -- verbatim from
test_structures_step.py, which now imports them from here. Same
extraction trees_step_fixture.py and fencing_step_fixture.py already
made, and for the same reason: a probe or a second test file that needs
the committed four-step upstream should not pay for the whole structures
suite at import.
"""

import copy
import socket
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, shape
from shapely.ops import unary_union

import canopy_height_data
import commit_validation
import design_document
import farm_roads_data
import job_runner
import parcel_data
import production_area
import production_area_ceiling
import production_zone_payload
import road_corridors
import session_api
import session_cache
import session_manager
import solar_suitability
import step_orchestrator
import step_registry
import tree_zone_candidates
import water_suitability
import water_survey_areas
import wire_translation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore
from feature_schema import validate_feature_collection
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- the real property, verbatim from B2, B4, B5a, B5b, water, roads, trees

# The parcel, its UTM CRS, the projected polygon and its acreage -- one
# definition shared by every step test (see reference_fixture.py).
from reference_fixture import BOUNDARY_POLYGON_UTM, CRS, PARCEL_ACRES, REAL_BOUNDARY  # noqa: E402


def _boundary_point(edge_index: int, fraction: float) -> tuple:
    """A (lon, lat) EXACTLY on the parcel's own edge -- test_roads_step.py's
    own helper, so the access point here is the one that branch surveyed."""
    ring = BOUNDARY_POLYGON_UTM.exterior
    start = ring.project(Point(ring.coords[edge_index]))
    end = ring.project(Point(ring.coords[edge_index + 1]))
    point = ring.interpolate(start + (end - start) * fraction)
    lons, lats = warp_transform(CRS, "EPSG:4326", [point.x], [point.y])
    return (float(lons[0]), float(lats[0]))


# ACCESS_A from test_roads_step.py: the west edge (N Montour Rd side), a
# multi-branch network on this fixture.
ACCESS_A = _boundary_point(0, 0.85)

# --- the DEM fixture, verbatim from test_water_step.py / test_roads_step.py

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

# The fixture's one canopy block, in grid terms (see _build_canopy).
CANOPY_ROWS = (KNEE_ROW - 6, KNEE_ROW + 8)
CANOPY_COLS = (CHANNEL_COL + 14, CHANNEL_COL + 26)


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
    hag[CANOPY_ROWS[0] : CANOPY_ROWS[1], CANOPY_COLS[0] : CANOPY_COLS[1]] = 15.0
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
FIXTURE_WATER_FEATURES = {"streams": [], "water_bodies": []}
FIXTURE_FARMLAND = []


def _build_parcel_data(_boundary=None) -> ParcelData:
    dem = _build_dem()
    return ParcelData(
        dem=dem,
        boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
        soil_components=HYDRIC_COMPONENTS,
        farmland_classification=FIXTURE_FARMLAND,
        erosion_factor=[],
        saturated_hydraulic_conductivity=FIXTURE_KSAT,
        soil_geometries=HYDRIC_GEOMETRIES,
        water_features=FIXTURE_WATER_FEATURES,
        farm_roads=FIXTURE_ROADS,
        climate_summary={},
        canopy_height=_build_canopy(dem),
        imagery_summary={},
        irradiance={"status": "ok"},
    )


class Harness:
    """
    The trees step's harness plus the solar module's own boundaries. Every
    network call is mocked and COUNTED; every real computation is wrapped
    (wraps=) so it RUNS and is counted; every self-compute the solar entry
    point can fall into is a COUNTER that answers "ran, found nothing" in
    the shape the entry point reads, so a call is counted and never
    performed. An assertion that a count is zero only means something if a
    nonzero count was reachable -- sections 3 and 7 each prove theirs is.

    THE TREE MODULE'S THREE STEP-2 FETCHES RETURN THE FIXTURE'S OWN ROWS
    here (they RAISE in test_trees_step.py). Section 4 needs the nested
    tree generate inside solar to RUN and produce a set to compare against
    the committed one, and that nested call forwards no scoring_inputs --
    so it fetches. Returning ParcelData's own rows is what makes the
    regenerated set a fair comparison: it is computed from the same soil
    and stream data the committed set was. The fetches are still counted,
    because they are still network calls the override exists to close.
    """

    def __enter__(self):
        self._stack = ExitStack()
        patch = self._stack.enter_context

        # --- Layer 1 and the shared warm-up boundaries -------------------
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
        self.dem_refetch = patch(
            mock_patch.object(
                production_area_ceiling, "get_dem_for_boundary",
                side_effect=AssertionError("get_dem_for_boundary() must not run"),
            )
        )
        # --- the water step's own fetches, closed by its registry edges ---
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
        # --- the roads module's own fetches and self-computes -------------
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
        self.road_water_selfcompute = patch(
            mock_patch.object(
                road_corridors, "fetch_and_select_optimal_water_zone",
                new=MagicMock(return_value=None),
            )
        )
        # --- THE TREES MODULE: its DEM fetch raises; its three Step-2
        # fetches are COUNTERS returning the fixture's own rows (see the
        # class docstring); its three self-computes are counters. ---------
        self.tree_dem_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_dem_for_boundary",
                side_effect=AssertionError("tree_zone_candidates must not fetch a DEM"),
            )
        )
        self.tree_farmland_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_farmland_classification_for_polygon",
                new=MagicMock(return_value=FIXTURE_FARMLAND),
            )
        )
        self.tree_soil_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_soil_data_for_polygon",
                new=MagicMock(return_value=HYDRIC_COMPONENTS),
            )
        )
        self.tree_soil_geometry_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_soil_geometries_for_polygon",
                new=MagicMock(return_value=HYDRIC_GEOMETRIES),
            )
        )
        self.tree_nhd_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_water_features_for_boundary",
                new=MagicMock(return_value=FIXTURE_WATER_FEATURES),
            )
        )
        self.tree_road_selfcompute = patch(
            mock_patch.object(
                tree_zone_candidates, "identify_road_corridor_candidates",
                new=MagicMock(return_value={"selected_road_corridor": None}),
            )
        )
        self.tree_water_selfcompute = patch(
            mock_patch.object(
                tree_zone_candidates, "identify_water_suitability",
                new=MagicMock(return_value={"selected_water_zone": None}),
            )
        )
        self.tree_production_selfcompute = patch(
            mock_patch.object(
                tree_zone_candidates, "identify_optimized_production_areas",
                new=MagicMock(return_value={"scored_patches": []}),
            )
        )
        # --- THE SOLAR MODULE'S OWN BOUNDARIES --------------------------
        self.solar_dem_fetch = patch(
            mock_patch.object(
                solar_suitability, "get_dem_for_boundary",
                side_effect=AssertionError("solar_suitability must not fetch a DEM"),
            )
        )
        # The three self-computes, each a COUNTER answering "ran, found
        # nothing" in the shape the entry point reads off the result.
        self.solar_production_selfcompute = patch(
            mock_patch.object(
                solar_suitability, "identify_optimized_production_areas",
                new=MagicMock(return_value={"scored_patches": []}),
            )
        )
        self.solar_water_selfcompute = patch(
            mock_patch.object(
                solar_suitability, "identify_water_suitability",
                new=MagicMock(return_value={"selected_water_zone": None}),
            )
        )
        self.solar_road_selfcompute = patch(
            mock_patch.object(
                solar_suitability, "identify_road_corridor_candidates",
                new=MagicMock(return_value={"selected_road_corridor": None}),
            )
        )
        # THE NESTED TREE GENERATE: wrapped and REAL, so section 3 counts
        # it and section 4 can run it.
        self.solar_tree_nested = patch(
            mock_patch.object(
                solar_suitability, "identify_tree_zone_candidates",
                wraps=tree_zone_candidates.identify_tree_zone_candidates,
            )
        )
        # Tier 2's road fetch and the SSURGO farmland check: counters that
        # return the fixture rows, so a run that reaches them still works
        # and the count says whether the cache closure held.
        self.solar_roads_fetch = patch(
            mock_patch.object(
                solar_suitability, "get_farm_roads_for_boundary",
                new=MagicMock(return_value=FIXTURE_ROADS),
            )
        )
        self.solar_farmland_fetch = patch(
            mock_patch.object(
                solar_suitability, "get_farmland_classification_for_polygon",
                new=MagicMock(return_value=FIXTURE_FARMLAND),
            )
        )
        self.solar_canopy_mask = patch(
            mock_patch.object(
                solar_suitability, "get_required_tree_root_zone_mask_utm",
                wraps=solar_suitability.get_required_tree_root_zone_mask_utm,
            )
        )
        # THE STEP'S OWN GENERATE, patched on ITS module so the orchestrator
        # picks the wrapper up (step_registry.resolve() binds at call time).
        self.identify_solar = patch(
            mock_patch.object(
                solar_suitability, "identify_solar_candidate_zones",
                wraps=solar_suitability.identify_solar_candidate_zones,
            )
        )
        self.rehydrate_sites = patch(
            mock_patch.object(
                wire_translation, "rehydrate_structure_sites", wraps=wire_translation.rehydrate_structure_sites
            )
        )
        self.rehydrate_trees = patch(
            mock_patch.object(
                wire_translation, "rehydrate_tree_zones", wraps=wire_translation.rehydrate_tree_zones
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
            + self.tree_farmland_fetch.call_count
            + self.tree_soil_fetch.call_count
            + self.tree_soil_geometry_fetch.call_count
            + self.tree_nhd_fetch.call_count
            + self.solar_roads_fetch.call_count
            + self.solar_farmland_fetch.call_count
        )

    def solar_selfcomputes(self) -> dict:
        return {
            "identify_optimized_production_areas": self.solar_production_selfcompute.call_count,
            "identify_water_suitability": self.solar_water_selfcompute.call_count,
            "identify_road_corridor_candidates": self.solar_road_selfcompute.call_count,
            "identify_tree_zone_candidates": self.solar_tree_nested.call_count,
        }


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="structures_step_test_"))


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

    def layers(self, step_id):
        return step_orchestrator.step_payload(
            self.id, step_id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def commit(self, step_id, features, provenance, inputs=None):
        return step_orchestrator.commit_step(
            self.id, step_id, {"type": "FeatureCollection", "features": list(features)},
            provenance, self.revision(step_id), self.store, inputs=inputs,
            fetch_cache=self.fetch_cache, cache=self.cache,
        )

    def reopen(self, step_id):
        return step_orchestrator.reopen_step(
            self.id, step_id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def score(self, lon_lat, step_id="structures"):
        return step_orchestrator.score_placed_feature(
            self.id, step_id, self.store, params={"site": list(lon_lat)},
            fetch_cache=self.fetch_cache, cache=self.cache,
        )

    def context(self):
        return session_manager.get_session_context(
            self.id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def stored(self):
        return self.store.get(self.id)

    def revision(self, step_id):
        return self.stored()["steps"][step_id].get("revision", 0)

    def assembled(self, step_id="structures"):
        return step_orchestrator.assemble_consumes(
            step_registry.get_step(step_id), self.context(), self.stored()
        )

    def committed(self, step_id):
        return step_orchestrator.committed_internal_value(self.context(), self.stored(), step_id)

    def result(self, step_id="structures"):
        return self.context().step_proposals[step_id]

    def commit_landform(self, whole=True):
        payload = self.generate("landform")
        features = payload["suggested_zones"]["features"] if whole else []
        if whole:
            assert features, "the fixture must produce production zones to commit"
        return self.commit("landform", features, {f["id"]: "generated" for f in features})

    def water_zones(self):
        payload = self.generate("water")
        return [
            f for f in payload["survey_zones"]["features"]
            if f["properties"]["layer"] in wire_translation.LAYER_SURVEY_ZONES
        ]

    def commit_water(self, count=3):
        zones = self.water_zones()
        assert len(zones) >= count, f"only {len(zones)} water zones on the fixture"
        zones = zones[:count]
        return self.commit("water", zones, {f["id"]: "generated" for f in zones})

    def commit_roads(self, access_point=ACCESS_A):
        """Roads generated from `access_point` and the network committed
        whole; None commits roads EMPTY with no access point tried."""
        if access_point is None:
            return self.commit("roads", [], {}, inputs={"access_points": []})
        payload = self.generate("roads", {"access_point": list(access_point)})
        network = payload["networks"][0]
        assert network["network_found"], f"the fixture must route a network from A: {network['stop_reason']!r}"
        features = [
            f for f in payload["road_corridors"]["features"]
            if f["properties"]["network_id"] == network["network_id"]
        ]
        return self.commit(
            "roads", features, {f["id"]: "generated" for f in features},
            inputs={"access_points": [list(access_point)]},
        )

    def commit_trees(self, count=None):
        """Trees generated and the first `count` candidates committed (all
        when None; 0 commits trees EMPTY)."""
        payload = self.generate("trees")
        candidates = payload["tree_zones"]["features"]
        if count is None:
            count = len(candidates)
        assert len(candidates) >= count, f"only {len(candidates)} tree zones on the fixture"
        features = candidates[:count]
        return self.commit("trees", features, {f["id"]: "generated" for f in features})

    def upstream(self, landform=True, water_zone_count=3, access_point=ACCESS_A, tree_count=None):
        self.commit_landform(whole=landform)
        self.commit_water(water_zone_count)
        self.commit_roads(access_point)
        return self.commit_trees(tree_count)

    def structures(self):
        return self.generate("structures")


def _utm(geometry_wgs84: dict):
    return shape(transform_geom("EPSG:4326", CRS, geometry_wgs84))


def _lon_lat(point_utm) -> tuple:
    lons, lats = warp_transform(CRS, "EPSG:4326", [point_utm.x], [point_utm.y])
    return (float(lons[0]), float(lats[0]))


def _patch_set(patches):
    """(count, sorted ids, total render-fill acres) of a tree patch list."""
    ids = sorted(int(p["id"]) for p in patches)
    acres = sum(p["render_fill_polygon_utm"].area for p in patches) / SQUARE_METERS_PER_ACRE
    return (len(patches), ids, round(acres, 3))


def _candidate_signature(candidates):
    """What a solar candidate list IS, for comparison: (rank, score,
    footprint centroid) per candidate."""
    return [
        (c["rank"], c["suitability_score"], round(c["polygon_utm"].centroid.x, 3), round(c["polygon_utm"].centroid.y, 3))
        for c in candidates
    ]


