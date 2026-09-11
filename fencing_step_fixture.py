"""
fencing_step_fixture.py

THE FENCING STEP'S FIXTURE, IMPORTABLE WITHOUT RUNNING ITS TESTS.

The real parcel (reference_fixture.py), the bench-and-drainage DEM with
its canopy block, the mocked network rows, the Harness that patches every
fetch boundary and counts every self-compute, and the Session that runs
the real orchestrator -- verbatim from test_fencing_step.py, which now
imports them from here. test_fence_display_geometry.py used to
`import test_fencing_step as fixture` to reach Session and Harness, and
paid for the whole fencing suite at import (31 s of its 40); this module
is the fixture and nothing else.
"""

import copy
import dataclasses
import inspect
import json
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, box, shape
from shapely.ops import unary_union

import canopy_height_data
import commit_validation
import design_document
import farm_roads_data
import fencing
import hydrology_data
import job_runner
import parcel_data
import production_area
import production_area_ceiling
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

# --- the real property, verbatim from B2, B4, B5a, B5b, water, roads, trees, structures

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


# ACCESS_A from test_roads_step.py: the west edge (N Montour Rd side).
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
# ONE MAPPED STREAM on this fixture -- the prior branches carried no streams,
# and this one needs a stream for stream EXCLUSION fencing to have something
# to buffer (section 7: it must reach the result and produce no candidate).
# A short reach along the parcel's south-east, as the NHD rows ParcelData
# holds them ({name, feature_code, geometry}, WGS84), with an NHD id so the
# schema feature id is the fetch path's own spelling.
FIXTURE_STREAM = {
    "name": "Fixture Run",
    "feature_code": 46006,
    "permanent_identifier": "fixture-run-1",
    "geometry": {
        "type": "LineString",
        "coordinates": [[-79.9822, 40.6431], [-79.9815, 40.6437], [-79.9809, 40.6443]],
    },
}
FIXTURE_WATER_FEATURES = {"streams": [FIXTURE_STREAM], "water_bodies": []}
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
    The structures step's harness plus the fencing module's own boundaries.
    Every network call is mocked and COUNTED; every real computation is
    wrapped (wraps=) so it RUNS and is counted; every self-compute the
    fencing entry point can fall into is a COUNTER that answers "ran, found
    nothing" in the shape the entry point reads, so a call is counted and
    never performed. An assertion that a count is zero only means something
    if a nonzero count was reachable -- section 10 proves each is.

    THE TREE MODULE'S THREE STEP-2 FETCHES RETURN THE FIXTURE'S OWN ROWS
    (the structures harness's choice, kept so the trees generate upstream is
    the same one that branch asserted over). They are still counted.
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
        # --- the water step's own fetches ----------------------------------
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
        # --- THE TREES MODULE ---------------------------------------------
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
        # --- THE SOLAR MODULE (the structures step upstream) ----------------
        self.solar_dem_fetch = patch(
            mock_patch.object(
                solar_suitability, "get_dem_for_boundary",
                side_effect=AssertionError("solar_suitability must not fetch a DEM"),
            )
        )
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
        self.solar_tree_nested = patch(
            mock_patch.object(
                solar_suitability, "identify_tree_zone_candidates",
                new=MagicMock(return_value={"patches": []}),
            )
        )
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
        # --- THE FENCING MODULE'S OWN BOUNDARIES ---------------------------
        # Its two fetches RAISE -- the edges close them, so a call is a
        # failed test rather than a count -- except the NHD fetch, which is a
        # COUNTER: section 10's control has to be able to reach it.
        self.fencing_dem_fetch = patch(
            mock_patch.object(
                fencing, "get_dem_for_boundary",
                side_effect=AssertionError("fencing must not fetch a DEM"),
            )
        )
        self.fencing_nhd_fetch = patch(
            mock_patch.object(
                fencing, "get_water_features_geojson",
                new=MagicMock(return_value=hydrology_data.water_features_to_geojson(FIXTURE_WATER_FEATURES)),
            )
        )
        # The three self-computes, each a COUNTER answering "ran, found
        # nothing" in the shape the entry point reads off the result.
        self.fencing_water_selfcompute = patch(
            mock_patch.object(
                fencing, "fetch_and_select_optimal_water_zone",
                new=MagicMock(return_value=None),
            )
        )
        self.fencing_road_selfcompute = patch(
            mock_patch.object(
                fencing, "identify_road_corridor_candidates",
                new=MagicMock(return_value={"selected_road_corridor": None}),
            )
        )
        self.fencing_tree_selfcompute = patch(
            mock_patch.object(
                fencing, "identify_tree_zone_candidates",
                new=MagicMock(return_value={"patches": []}),
            )
        )
        # THE STEP'S OWN GENERATE, patched on ITS module so the orchestrator
        # picks the wrapper up (step_registry.resolve() binds at call time).
        self.identify_fencing = patch(
            mock_patch.object(fencing, "identify_fencing", wraps=fencing.identify_fencing)
        )
        self.find_boundary_fencing = patch(
            mock_patch.object(fencing, "find_boundary_fencing", wraps=fencing.find_boundary_fencing)
        )
        self.find_stream_exclusion_fencing = patch(
            mock_patch.object(
                fencing, "find_stream_exclusion_fencing", wraps=fencing.find_stream_exclusion_fencing
            )
        )
        self.identify_solar = patch(
            mock_patch.object(
                solar_suitability, "identify_solar_candidate_zones",
                wraps=solar_suitability.identify_solar_candidate_zones,
            )
        )
        self.rehydrate_fences = patch(
            mock_patch.object(
                wire_translation, "rehydrate_fence_lines", wraps=wire_translation.rehydrate_fence_lines
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
            + self.fencing_nhd_fetch.call_count
        )

    def fencing_selfcomputes(self) -> dict:
        return {
            "fetch_and_select_optimal_water_zone": self.fencing_water_selfcompute.call_count,
            "identify_road_corridor_candidates": self.fencing_road_selfcompute.call_count,
            "identify_tree_zone_candidates": self.fencing_tree_selfcompute.call_count,
        }


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="fencing_step_test_"))


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

    def assembled(self, step_id="fencing"):
        return step_orchestrator.assemble_consumes(
            step_registry.get_step(step_id), self.context(), self.stored()
        )

    def forwarded(self, step_id="fencing"):
        return dict(step_orchestrator.forwarded_arguments(step_registry.get_step(step_id), self.assembled(step_id), {}))

    def committed(self, step_id):
        return step_orchestrator.committed_internal_value(self.context(), self.stored(), step_id)

    def result(self, step_id="fencing"):
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
        payload = self.generate("trees")
        candidates = payload["tree_zones"]["features"]
        if count is None:
            count = len(candidates)
        assert len(candidates) >= count, f"only {len(candidates)} tree zones on the fixture"
        features = candidates[:count]
        return self.commit("trees", features, {f["id"]: "generated" for f in features})

    def commit_structures(self, count=1, placed=()):
        """Structures generated and the first `count` candidates committed
        beside `placed` (lon, lat) sites scored through the verb; 0 and no
        placed commits structures EMPTY."""
        payload = self.generate("structures")
        candidates = payload["structure_sites"]["features"]
        assert len(candidates) >= count, f"only {len(candidates)} structure candidates on the fixture"
        features = list(candidates[:count])
        provenance = {f["id"]: "generated" for f in features}
        for lon_lat in placed:
            feature = self.score(lon_lat)
            features.append(feature)
            provenance[feature["id"]] = "user_added"
        return self.commit("structures", features, provenance)

    def upstream(self, landform=True, water_zone_count=3, access_point=ACCESS_A, tree_count=None,
                 structure_count=1, placed=()):
        self.commit_landform(whole=landform)
        self.commit_water(water_zone_count)
        self.commit_roads(access_point)
        self.commit_trees(tree_count)
        return self.commit_structures(structure_count, placed)

    def fencing(self):
        return self.generate("fencing")


def _utm(geometry_wgs84: dict):
    return shape(transform_geom("EPSG:4326", CRS, geometry_wgs84))


def _lon_lat(point_utm) -> tuple:
    lons, lats = warp_transform(CRS, "EPSG:4326", [point_utm.x], [point_utm.y])
    return (float(lons[0]), float(lats[0]))


def _blocks(payload) -> dict:
    return {block["fence_type"]: block for block in payload["fence_types"]}


def _rings(line) -> list:
    return list(line.geoms) if line.geom_type == "MultiLineString" else [line]


_BOUNDARY_CALL_PARAMETERS = (
    "boundary_polygon_utm", "production_zone_polygons_utm", "structure_site_polygon_utm",
    "road_corridor_cell_footprint_polygon_utm", "water_zone_polygon_utm", "tree_zone_polygons_utm",
)


def _boundary_call(h) -> dict:
    """find_boundary_fencing()'s last call, by parameter name -- identify_
    boundary_fencing() passes every input positionally."""
    call = h.find_boundary_fencing.call_args
    named = dict(zip(_BOUNDARY_CALL_PARAMETERS, call.args))
    named.update(call.kwargs)
    return named


print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} vertices, "
    f"{PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at {RESOLUTION_METERS:.0f} m. "
    f"Same boundary and same DEM fixture as the water, roads, trees and structures steps; access "
    f"point A {ACCESS_A} (west edge) is the roads branch's own. One fixture stream ({FIXTURE_STREAM['name']}) "
    f"so stream exclusion fencing has something to buffer.\n"
)
