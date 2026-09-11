"""
trees_step_fixture.py

THE TREES STEP'S FIXTURE, IMPORTABLE WITHOUT RUNNING ITS TESTS.

The real parcel (reference_fixture.py), the DEM and canopy block, the
mocked network rows, the Harness and the Session -- verbatim from
test_trees_step.py, which now imports them from here.
test_display_outline.py and test_tree_zone_geometry_validity.py used to
`import test_trees_step as fixture` and paid for the whole trees suite at
import (about 15 s each); this module is the fixture and nothing else.
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
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box, mapping, shape
from shapely.ops import unary_union

import canopy_height_data
import commit_validation
import design_document
import display_outline
import exclusion_zones
import farm_roads_data
import fencing
import job_runner
import parcel_data
import production_area
import production_area_ceiling
import production_zone_payload
import road_corridors
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
from raster_grid import SQUARE_METERS_PER_ACRE, pixel_center_xy

# --- the real property, verbatim from B2, B4, B5a, B5b, water and roads -

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
# three-branch network on this fixture.
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
        water_features={"streams": [], "water_bodies": []},
        farm_roads=FIXTURE_ROADS,
        climate_summary={},
        canopy_height=_build_canopy(dem),
        imagery_summary={},
        irradiance={"status": "ok"},
    )


class Harness:
    """
    The roads step's harness plus the trees module's own boundaries. Every
    network call is mocked; every real computation is wrapped (wraps=) so it
    RUNS and is COUNTED; every self-compute the trees entry point can fall
    into is a COUNTER that returns "ran, found nothing", so a call is
    counted and never performed. An assertion that a count is zero only
    means something if a nonzero count was reachable -- sections 3 and 4
    each prove theirs is.
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
        # --- THE TREES MODULE'S OWN BOUNDARIES ----------------------------
        # Its DEM fetch and Step 2's three network fetches (two SDA, one
        # NHD) RAISE: with scoring_inputs forwarded off the cache none of
        # them may run, and a run is an error rather than a count.
        self.tree_dem_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_dem_for_boundary",
                side_effect=AssertionError("tree_zone_candidates must not fetch a DEM"),
            )
        )
        self.tree_farmland_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_farmland_classification_for_polygon",
                side_effect=AssertionError("tree_zone_candidates must not fetch SSURGO farmland classes"),
            )
        )
        self.tree_soil_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_soil_data_for_polygon",
                side_effect=AssertionError("tree_zone_candidates must not fetch SSURGO components"),
            )
        )
        self.tree_soil_geometry_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_soil_geometries_for_polygon",
                side_effect=AssertionError("tree_zone_candidates must not fetch SSURGO geometry"),
            )
        )
        self.tree_nhd_fetch = patch(
            mock_patch.object(
                tree_zone_candidates, "get_water_features_for_boundary",
                side_effect=AssertionError("tree_zone_candidates must not fetch NHD"),
            )
        )
        # THE THREE SELF-COMPUTES, EACH A COUNTER. identify_tree_zone_
        # candidates() falls into each when the matching override arrives
        # as None: a full routing pass, the whole retired water pipeline,
        # the production optimiser. Each is replaced on the TREES module's
        # own binding by a counter that answers "ran, found nothing" in the
        # shape the entry point reads off the result, so the call is counted
        # and never performed; the roads generate's own binding on
        # road_corridors is untouched and real. Sections 3 and 4 prove each
        # counter is reachable before they rely on its zero.
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
        self.tree_canopy_mask = patch(
            mock_patch.object(
                tree_zone_candidates, "get_required_tree_root_zone_mask_utm",
                wraps=tree_zone_candidates.get_required_tree_root_zone_mask_utm,
            )
        )
        # THE STEP'S OWN GENERATE, patched on ITS module so the orchestrator
        # picks the wrapper up (step_registry.resolve() binds at call time).
        self.identify_trees = patch(
            mock_patch.object(
                tree_zone_candidates, "identify_tree_zone_candidates",
                wraps=tree_zone_candidates.identify_tree_zone_candidates,
            )
        )
        self.rehydrate_trees = patch(
            mock_patch.object(
                wire_translation, "rehydrate_tree_zones", wraps=wire_translation.rehydrate_tree_zones
            )
        )
        self.water_union = patch(
            mock_patch.object(wire_translation, "water_zone_union", wraps=wire_translation.water_zone_union)
        )
        self.road_selection = patch(
            mock_patch.object(
                wire_translation, "selected_road_network", wraps=wire_translation.selected_road_network
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

    def tree_selfcomputes(self) -> dict:
        return {
            "identify_road_corridor_candidates": self.tree_road_selfcompute.call_count,
            "identify_water_suitability": self.tree_water_selfcompute.call_count,
            "identify_optimized_production_areas": self.tree_production_selfcompute.call_count,
        }


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="trees_step_test_"))


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

    def context(self):
        return session_manager.get_session_context(
            self.id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def stored(self):
        return self.store.get(self.id)

    def revision(self, step_id):
        return self.stored()["steps"][step_id].get("revision", 0)

    def assembled(self, step_id="trees"):
        return step_orchestrator.assemble_consumes(
            step_registry.get_step(step_id), self.context(), self.stored()
        )

    def committed(self, step_id):
        return step_orchestrator.committed_internal_value(self.context(), self.stored(), step_id)

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

    def upstream(self, landform=True, water_zone_count=3, access_point=ACCESS_A):
        """Landform committed whole (or empty), water committed with the
        first `water_zone_count` zones (0 for EMPTY), roads committed with
        the network from `access_point` (None for EMPTY)."""
        self.commit_landform(whole=landform)
        self.commit_water(water_zone_count)
        return self.commit_roads(access_point)

    def trees(self):
        return self.generate("trees")


# --- drawn zones ---------------------------------------------------------

LAYER = wire_translation.LAYER_TREE_ZONE


def _drawn(feature_id: str, ring_lon_lat, label="Drawn tree zone"):
    """A user-drawn tree zone in the shape the frontend commits: a schema-
    conformant Feature carrying the layer and the ring, and NOTHING a
    pipeline would have computed -- no rank, no score, no factor."""
    ring = [list(point) for point in ring_lon_lat]
    if ring[0] != ring[-1]:
        ring.append(list(ring_lon_lat[0]))
    return {
        "id": feature_id,
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "layer": LAYER,
            "label": label,
            "confidence": "low",
            "confidence_notes": "Drawn by hand on the map; no survey backs it.",
        },
    }


def _rect(west, east, south, north):
    return [(west, south), (east, south), (east, north), (west, north)]


def _ring_wgs84(polygon_utm) -> list:
    """A UTM polygon's exterior ring as (lon, lat) pairs."""
    xs, ys = zip(*polygon_utm.exterior.coords)
    lons, lats = warp_transform(CRS, "EPSG:4326", list(xs), list(ys))
    return list(zip(lons, lats))


def _box_around(geometry_utm, half_meters: float, clip_utm=None) -> list:
    """A square of side 2*half_meters centred on a point INSIDE the
    geometry, as a lon/lat ring -- a drawn zone that certainly overlaps it.

    `clip_utm` KEEPS THE SQUARE ON THE PARCEL, and it is a correctness
    argument rather than tidiness: the centre is a representative point of
    ground the pipeline chose, so where that ground sits is not this
    fixture's to assume. A ground that happens to hug the boundary yields a
    square hanging over the edge, and commit_validation refuses that with
    outside_boundary -- a rejection about WHERE THE FIXTURE DREW, not about
    the behaviour under test. So the ground is clipped BEFORE the centre is
    taken (the centre must be inside the parcel, not merely inside the
    ground) and the square is clipped after. At 15 m the square is 900 m^2
    against a 0.05 ac (202 m^2) crossing floor, so a clip has room to bite
    without dropping the crossing under it.

    This bit the first time the water step narrowed its payload: the
    committed water ground changed, and a square that had always landed
    inside started hanging off the edge. Nothing about the drawn-zone
    behaviour had changed -- only where the fixture happened to be drawing."""
    source = geometry_utm if clip_utm is None else _largest(geometry_utm.intersection(clip_utm))
    point = source.representative_point()
    square = box(point.x - half_meters, point.y - half_meters, point.x + half_meters, point.y + half_meters)
    if clip_utm is not None:
        square = _largest(square.intersection(clip_utm))
    return _ring_wgs84(square)


def _utm(feature: dict):
    return shape(transform_geom("EPSG:4326", CRS, feature["geometry"]))


def _largest(geometry):
    """The largest polygonal part of a clip result, as one Polygon."""
    if geometry.geom_type == "Polygon":
        return geometry
    return max((g for g in geometry.geoms if g.geom_type == "Polygon"), key=lambda g: g.area)


# From test_step_commit.py: a rectangle inside the hydric footprint, inside
# the parcel -- that branch asserts it crosses the hydric gate by 0.09 ac.
HYDRIC_ZONE_RING = _rect(-79.98303, -79.98291, 40.64342, 40.64390)


print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} vertices, "
    f"{PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at {RESOLUTION_METERS:.0f} m. "
    f"Same boundary and same DEM fixture as the water and roads steps; access point A "
    f"{ACCESS_A} (west edge) is the roads branch's own.\n"
)
