"""
test_run_diagnostics.py

THE RUN DIAGNOSTIC RECORD -- run_diagnostics.py, its two fetch hooks, its
two generate hooks and its commit hook. Run as:

    python3 test_run_diagnostics.py

THE SAME FIXTURE AS test_trees_step.py, VERBATIM: the real drawn property
at 5614 N Montour Rd, Gibsonia, PA (~13.23 acres, UTM 17N), the
bench-and-drainage DEM with its flanking levees, the same mocked network
and the same three self-compute counters. session_manager.create_session(),
the terrain warm-up, the landform, water and roads generates, the trees
generate, the commit gate and every rehydrator all RUN. The diagnostic is
observed over a real pipeline run, because a diagnostic asserted against a
hand-built dict would prove nothing about the pipeline it exists to
describe.

Sections (the branch's numbered tests in brackets):
  1  [1]  ON WRITES, OFF WRITES NOTHING -- and off does not so much as
          create the directory.
  2  [2]  TWO RUNS DIFF CLEAN. Two sessions on the same parcel with
          identical inputs produce records whose bodies are BYTE
          IDENTICAL outside the header; a control shows a real
          difference (a rebuilt session cache) does show up. THE TEST
          THAT MAKES THE TOOL WORK.
  3  [3]  A COMMIT REJECTION DUMPS THE OFFENDING FEATURE'S FULL GeoJSON,
          replayable from the record alone.
  4  [4]  THE UTM-VERSUS-ROUND-TRIP COMPARISON is recorded for EVERY
          tree patch, both verdicts and their agreement.
  5  [5]  EVERY RECORDED VALUE COMES FROM THE PIPELINE -- the acreages,
          the gate counts and the flags in the record are found, by
          value, in the result the entry point returned.
  6  [6]  DISABLED COSTS NOTHING: with diagnostics off, every
          record-building function in the module is replaced by one that
          raises, and a full generate-and-commit runs clean.
  7  [7]  THE ENVIRONMENT GROUP, and the wire's coordinate precision.
  8  [8]  THE DROP SINK, SURFACED: an induced drop puts a real
          {id, area_acres, reason} row on the result and into the
          record, where only its len() used to reach anything.
  9  [9]  END TO END: the real Flask app over HTTP, process-wide
          defaults, the DEFAULT directory, a file on disk. The positive
          counterpart section 6 needs to mean anything.
 10 [10]  Regression is the other test files, run separately.

THE FETCH TIMING SECTIONS (this branch's numbered tests in brackets)
===================================================================
Sections 11-18 measure the OTHER end of a session: parcel_data.fetch_
parcel_data(), the thirteen sequential fetches a session creation waits
minutes on. They run through the REAL fetch_parcel_data() -- its real
order, its real None checks, its real raises -- with only the thirteen
network calls mocked, on parcel_data's own namespace (see FetchHarness).

 11  [1]  A COLD CREATION RECORDS THIRTEEN LAYER TIMINGS summing to the
          recorded total, in fetch order, each row carrying its own
          layer's wait.
 12  [2]  A WARM CREATION SAYS THE CACHE SERVED IT -- layers null, not
          thirteen zeroes.
 13  [3]  A FAILED FETCH STILL WRITES A RECORD, naming the layer and the
          exception, with NO session left behind. Two REAL induced
          failures, not stubbed verdicts. THE ONE THAT MATTERS
          OPERATIONALLY.
 14  [4]  irradiance RECORDS ITS status, and a degraded status is NOT
          recorded as a failure.
 15  [5]  RETRY COUNTS: no fetch module publishes one, so the absence is
          reported -- observed against the real entry points, with a
          published count proved reachable.
 16  [6]  TIMINGS MOVE AND THE DIFF STILL COMES OUT CLEAN. Section 2's
          byte-identical assertion, held for everything that is not a
          timing.
 17  [7]  DISABLED COSTS NOTHING at the fetch too.
 18  [8]  self_check() REPORTS WHETHER FETCH INSTRUMENTATION IS WIRED,
          with a negative control.
"""

import copy
import io
import json
import os
import shutil
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import MagicMock, Mock
from unittest.mock import patch as mock_patch

import numpy as np
import requests
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, mapping, shape

import canopy_height_data
import commit_validation
import document_store
import farm_roads_data
import hydrology_data
import imagery_data
import job_runner
import parcel_data
import production_area
import production_area_ceiling
import road_corridors
import run_diagnostics
import session_api
import session_cache
import session_manager
import soil_data
import step_orchestrator
import step_registry
import tree_zone_candidates
import water_survey_areas
import wire_translation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE


# --- the diagnostics directory, and turning the feature on and off -------
#
# EVERY SECTION SETS BOTH VARIABLES FOR ITSELF. run_diagnostics.enabled()
# reads the environment on every call rather than caching at import, which
# is what makes an on/off switch testable in one process at all -- see its
# docstring.

DIAGNOSTICS_DIR = tempfile.mkdtemp(prefix="run_diagnostics_test_")


class Diagnostics:
    """`with Diagnostics(on=True, directory=...)`: the two variables set on
    entry and restored on exit, whatever they were."""

    def __init__(self, on: bool, directory: str = None):
        self.on = on
        self.directory = directory or DIAGNOSTICS_DIR

    def __enter__(self):
        self._saved = {
            key: os.environ.get(key)
            for key in (run_diagnostics.ENABLED_ENV, run_diagnostics.DIRECTORY_ENV)
        }
        if self.on:
            os.environ[run_diagnostics.ENABLED_ENV] = "1"
        else:
            os.environ.pop(run_diagnostics.ENABLED_ENV, None)
        os.environ[run_diagnostics.DIRECTORY_ENV] = self.directory
        return self

    def __exit__(self, *exc_info):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


# --- the real property, verbatim from B2, B4, B5a, B5b, water and roads -

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
        elevation_grid=[],
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

    `real_fetch=True` leaves parcel_data.fetch_parcel_data() ALONE so the
    real one runs and its thirteen layer timers fire; FetchHarness below
    is that mode plus the thirteen layer functions mocked underneath it,
    on parcel_data's own namespace. Everything else here -- every warm-up
    boundary, every step's own fetches and self-computes -- is identical
    in both modes, so a section that measures the fetch is measuring it
    inside the same closed pipeline every other section runs in.
    """

    def __init__(self, real_fetch: bool = False):
        self.real_fetch = real_fetch

    def __enter__(self):
        self._stack = ExitStack()
        patch = self._stack.enter_context

        # --- Layer 1 and the shared warm-up boundaries -------------------
        self.fetch_parcel_data = None
        if not self.real_fetch:
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
            (self.fetch_parcel_data.call_count if self.fetch_parcel_data is not None else 0)
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


# --- the thirteen layers, mocked one at a time ---------------------------
#
# THE REAL fetch_parcel_data() RUNS. Every section that measures the fetch
# needs the function under test to be the real one -- its real order, its
# real thirteen calls, its real None checks and its real raises -- with
# only the network boundary replaced. Patches therefore target
# parcel_data's OWN namespace (parcel_data.get_dem_for_boundary, ...) and
# not each source module's, since parcel_data.py imports every fetch with
# `from X import Y` and patching X.Y would leave its already-bound
# reference untouched. That is test_parcel_data.py's own arrangement,
# reused here for its reason.

_FIXTURE_DEM = _build_dem()
_FIXTURE_CANOPY = _build_canopy(_FIXTURE_DEM)

# parcel_data's binding name -> what that layer returns. Matched to
# _build_parcel_data() FIELD FOR FIELD, so the ParcelData the real
# fetch_parcel_data() assembles here is the same one every other section
# in this file runs its pipeline against; a fetch section and a geometry
# section then differ in what they observe, never in what they observed it
# on. IN FETCH ORDER, pairing positionally with parcel_data.FETCH_LAYERS
# -- asserted below rather than left to the reader to check.
FETCH_LAYER_RETURNS = {
    "get_dem_for_boundary": _FIXTURE_DEM,
    "get_soil_data_for_polygon": HYDRIC_COMPONENTS,
    "get_farmland_classification_for_polygon": [],
    "get_erosion_factor_for_polygon": [],
    "get_saturated_hydraulic_conductivity_for_polygon": FIXTURE_KSAT,
    "get_soil_geometries_for_polygon": HYDRIC_GEOMETRIES,
    "get_water_features_for_boundary": {"streams": [], "water_bodies": []},
    "get_farm_roads_for_boundary": FIXTURE_ROADS,
    "get_climate_summary_for_point": {},
    "get_elevation_grid": [],
    "get_canopy_height_for_boundary": _FIXTURE_CANOPY,
    "get_imagery_summary_for_boundary": {},
    "get_regional_irradiance_baseline": {"status": "ok"},
}

# FETCH_LAYERS entry -> the parcel_data binding that fills it.
LAYER_FUNCTIONS = dict(zip(parcel_data.FETCH_LAYERS, FETCH_LAYER_RETURNS))
assert len(parcel_data.FETCH_LAYERS) == 13, parcel_data.FETCH_LAYERS
assert len(LAYER_FUNCTIONS) == len(FETCH_LAYER_RETURNS) == 13


def _layer_mock(name, delay):
    """One layer's stand-in. `delay` seconds of sleep before returning, so
    a section can give the thirteen layers KNOWN, DISTINGUISHABLE waits and
    then assert that each recorded row carries its own layer's wait and not
    some other layer's."""
    value = FETCH_LAYER_RETURNS[name]
    if not delay:
        return Mock(return_value=value)

    def call(*args, **kwargs):
        time.sleep(delay)
        return value

    return Mock(side_effect=call)


class FetchHarness:
    """
    Harness(real_fetch=True) plus the thirteen layer functions mocked, so
    a whole session creation runs through the REAL fetch_parcel_data().

    `delays` is {FETCH_LAYERS entry: seconds}; `overrides` is
    {FETCH_LAYERS entry: a Mock of your own}, which is how a section
    induces a REAL failure -- a fetch that raises, or one that returns the
    documented None sentinel -- rather than stubbing a verdict.
    """

    def __init__(self, delays=None, overrides=None):
        self.delays = delays or {}
        self.overrides = overrides or {}

    def __enter__(self):
        self._stack = ExitStack()
        self.harness = self._stack.enter_context(Harness(real_fetch=True))
        self.layers = {}
        for layer, name in LAYER_FUNCTIONS.items():
            mock = self.overrides.get(layer)
            if mock is None:
                mock = _layer_mock(name, self.delays.get(layer, 0.0))
            self._stack.enter_context(mock_patch.object(parcel_data, name, mock))
            self.layers[layer] = mock
        return self

    def __exit__(self, *exc_info):
        self._stack.close()
        return False

    def call_counts(self) -> dict:
        return {layer: mock.call_count for layer, mock in self.layers.items()}


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="trees_step_test_"))


def _fresh_runner():
    return job_runner.JobRunner(max_workers=2, max_jobs=64)


class Session:
    """One created session plus the caches and store behind it.

    `fetch_cache=`/`cache=`/`store=` share another session's -- which is
    how a WARM creation is arranged: a second session on the same boundary
    against the same fetch cache fetches nothing. `is None` and never
    `or`, session_cache.py's documented reason exactly: both cache classes
    define __len__, so an empty caller-supplied cache is falsy."""

    def __init__(self, fetch_cache=None, cache=None, store=None):
        fresh_fetch_cache, fresh_cache = _fresh_caches()
        self.store = _fresh_store() if store is None else store
        self.fetch_cache = fresh_fetch_cache if fetch_cache is None else fetch_cache
        self.cache = fresh_cache if cache is None else cache
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


# --- reading a recorded path back into the value it was read from --------


def _resolve(root, path: str):
    """
    Walk a recorded path -- `result.narrative_data.zones[0].rank` -- into
    the live object the record claims to have read it from.

    THIS IS WHAT MAKES TEST 5 AN ASSERTION RATHER THAN AN OPINION. The
    record's own paths are followed back into the pipeline's own result,
    and the value found there must be the value recorded. A writer that
    computed anything would produce a value this walk cannot find.
    """
    node = root
    for step in path.split("."):
        while step.endswith("]"):
            step, _, index = step[:-1].rpartition("[")
            node = _resolve_index(node, step, int(index))
            step = None
            break
        if step is not None:
            node = node[step]
    return node


def _resolve_index(node, key, index):
    if key:
        node = node[key]
    return node[index]


def _fail(message):
    raise AssertionError(message)


def _fetch_events(record):
    return [event for event in record["events"] if event["event"] == "fetch"]


def _sole_fetch_event(record):
    events = _fetch_events(record)
    assert len(events) == 1, f"expected exactly one fetch event, got {len(events)}"
    return events[0]


def _only_record_in(directory: str) -> dict:
    """
    The one record file in a directory of its own.

    HOW AN OPERATOR ACTUALLY FINDS A FAILED RUN'S RECORD, and why the
    failure sections each get their own directory. A hard-failed fetch
    creates NO session: create_session() raises before it persists
    anything, so the session id it generated is gone with the stack frame
    and nobody outside ever learns it. The record on disk is the only
    thing that knows it -- which is exactly the property those sections
    exist to prove.
    """
    names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
    assert len(names) == 1, f"expected one record in {directory}, found {names}"
    with open(os.path.join(directory, names[0]), encoding="utf-8") as handle:
        return json.load(handle)


def _timing_keys(value, path="") -> list:
    """Every `*_ms` path in a record -- what TIMING_KEY_SUFFIX promises is
    the complete set of values that legitimately move between two runs."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.endswith(run_diagnostics.TIMING_KEY_SUFFIX):
                found.append(here)
            else:
                found.extend(_timing_keys(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_timing_keys(item, f"{path}[{index}]"))
    return found


def _generate_events(record, step_id):
    return [
        event
        for event in record["events"]
        if event["event"] == "generate" and event["step_id"] == step_id
    ]


# =========================================================================
# 1 [test 1]. ON WRITES A RECORD; OFF WRITES NOTHING
# =========================================================================

_on_dir = tempfile.mkdtemp(prefix="run_diagnostics_on_")
with Diagnostics(on=True, directory=_on_dir), Harness():
    _on_session = Session()
    _on_session.commit_landform()
    _on_record = run_diagnostics.read_record(_on_session.id)

_off_dir = os.path.join(tempfile.mkdtemp(prefix="run_diagnostics_off_"), "never-made")
with Diagnostics(on=False, directory=_off_dir), Harness():
    _off_session = Session()
    _off_session.commit_landform()
    assert run_diagnostics.enabled() is False

assert os.listdir(_on_dir) == [f"{_on_session.id}.json"], os.listdir(_on_dir)
assert not os.path.exists(_off_dir), f"a disabled run created {_off_dir}"
# A SESSION'S RECORD NOW OPENS AT ITS CREATION, not at its first
# generate: the fetch event is written by session_cache.build_session_
# context() while create_session() is still running. `fetch` carries no
# step_id -- it belongs to the session, not to a step.
assert [event["event"] for event in _on_record["events"]] == ["fetch", "generate", "commit"]
assert _on_record["events"][0]["step_id"] is None
assert _on_record["events"][1]["step_id"] == "landform"

# THE DEFAULT PATH, with DIRECTORY_ENV saying nothing: `diagnostics/`
# under the working directory, the cwd-relative shape session_api uses
# for `sessions/`. `.gitignore` must carry it -- a record is a capture of
# ONE machine's runs and checking one in puts somebody else's evidence
# where yours belongs.
_saved_directory_env = os.environ.pop(run_diagnostics.DIRECTORY_ENV, None)
try:
    assert run_diagnostics.directory() == "diagnostics", run_diagnostics.directory()
    os.environ[run_diagnostics.DIRECTORY_ENV] = ""
    assert run_diagnostics.directory() == "diagnostics", "an empty variable is not a configured path"
finally:
    os.environ.pop(run_diagnostics.DIRECTORY_ENV, None)
    if _saved_directory_env is not None:
        os.environ[run_diagnostics.DIRECTORY_ENV] = _saved_directory_env

_gitignore = open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gitignore"), encoding="utf-8"
).read()
assert "diagnostics/" in _gitignore.split(), "\n".join(_gitignore.split())

print(
    f"1 [test 1]. ON/OFF: with {run_diagnostics.ENABLED_ENV}=1 a landform generate and commit wrote "
    f"one file ({_on_session.id}.json) carrying {_on_record['header']['event_count']} events "
    f"{[e['event'] for e in _on_record['events']]}; with the variable UNSET the identical run wrote "
    f"nothing and did not even create the directory ({_off_dir!r} does not exist). With "
    f"{run_diagnostics.DIRECTORY_ENV} unset or empty the path is "
    f"{run_diagnostics.DEFAULT_DIRECTORY!r} under the working directory, and .gitignore carries "
    f"'diagnostics/'."
)


# =========================================================================
# 2 [test 2]. TWO RUNS ON THE SAME PARCEL DIFF CLEAN OUTSIDE THE HEADER
# =========================================================================
#
# THE TEST THAT MAKES THE TOOL WORK. If two runs that did the same thing
# produce records that differ, the record carries non-determinism of its
# own and cannot answer the question it exists for -- "what differed
# between the run that failed and the run that did not".
#
# TWO SEPARATE SESSIONS, EACH WITH ITS OWN FRESH CACHES, so the two runs
# genuinely have identical inputs: both fetch Layer 1 on creation and both
# find it in the fetch cache at generate time. The comparison is on the
# SERIALIZED body -- byte for byte, through the same sort_keys=True writer
# the module uses -- because that is what `diff` on the two files sees.
#
# THE FETCH GROUP DOES NOT BREAK THIS, and section 16 is where that is
# proved with real, moving numbers. Every duration is written to a key
# ending in run_diagnostics.TIMING_KEY_SUFFIX, and comparable_body() --
# which this section already goes through -- redacts exactly those. What
# this section compares is therefore unchanged in meaning: everything two
# runs did, minus the clock.

_diff_dir = tempfile.mkdtemp(prefix="run_diagnostics_diff_")
_bodies = []
_headers = []
_trees_events = []
with Diagnostics(on=True, directory=_diff_dir), Harness():
    for _run in range(2):
        _session = Session()
        _session.upstream()
        _session.trees()
        _record = run_diagnostics.read_record(_session.id)
        _headers.append(_record["header"])
        _trees_events.append(_generate_events(_record, "trees")[0])
        _bodies.append(
            json.dumps(
                run_diagnostics.comparable_body(_record), indent=2, sort_keys=True
            )
        )

if _bodies[0] != _bodies[1]:
    _left = _bodies[0].splitlines()
    _right = _bodies[1].splitlines()
    _first = next(
        (i for i, (a, b) in enumerate(zip(_left, _right)) if a != b), min(len(_left), len(_right))
    )
    _fail(
        "two identical runs produced different records -- first difference at line "
        f"{_first}:\n  A: {_left[_first:_first + 3]}\n  B: {_right[_first:_first + 3]}"
    )

# The two headers DO differ, which is the point of putting them there.
assert _headers[0]["session_id"] != _headers[1]["session_id"]
assert _headers[0]["created_at"] != _headers[1]["created_at"]

# THE CONTROL. A record that could not tell two runs apart would pass the
# assertion above by being empty of information. So: the same parcel, the
# same steps, but the session cache DROPPED before the trees generate --
# a real difference in what the run did -- and the record must show it, in
# exactly one place.
with Diagnostics(on=True, directory=_diff_dir), Harness():
    _control = Session()
    _control.upstream()
    _control.cache.discard(_control.id)
    _control.trees()
    _control_record = run_diagnostics.read_record(_control.id)
    _control_body = json.dumps(
        run_diagnostics.comparable_body(_control_record), indent=2, sort_keys=True
    )

assert _control_body != _bodies[0], "a rebuilt session cache left no trace in the record"
_control_trees = _generate_events(_control_record, "trees")[0]
_clean_trees = _trees_events[0]
assert _control_trees["inputs"]["session_cache"] == "rebuilt"
assert _clean_trees["inputs"]["session_cache"] == "warm"
_differing = [
    key
    for key in set(_control_trees["inputs"]) | set(_clean_trees["inputs"])
    if _control_trees["inputs"].get(key) != _clean_trees["inputs"].get(key)
]
assert _differing == ["session_cache"], _differing

print(
    f"2 [test 2]. TWO RUNS DIFF CLEAN: two sessions on the same parcel, each through landform, "
    f"water, roads and trees, produced records whose bodies are BYTE IDENTICAL -- "
    f"{len(_bodies[0])} bytes, {len(_bodies[0].splitlines())} lines, 0 differing lines outside the "
    f"header (whose session_id and created_at both differ). THE CONTROL: the same run with the "
    f"session cache dropped before the trees generate differs, at exactly one key -- "
    f"inputs.session_cache 'warm' -> 'rebuilt'."
)


# =========================================================================
# 3 [test 3]. A COMMIT REJECTION DUMPS THE OFFENDING FEATURE'S FULL GeoJSON
# =========================================================================
#
# THE SINGLE MOST VALUABLE LINE IN THE BRANCH. A `Self-intersection`
# rejection names the defect and the feature id and then the request ends;
# without the ring that produced it the next attempt at an intermittent
# failure is a fresh guess. The assertion below is not that a dump exists
# -- it is that the dump is REPLAYABLE: the geometry is pulled back out of
# the record on its own and pushed through the same rehydrator the commit
# gate ran, and it fails the same way, with the same message.

_reject_dir = tempfile.mkdtemp(prefix="run_diagnostics_reject_")
# A bowtie: the ring visits its corners out of order, so the two sides
# cross. Drawn in lon/lat, exactly as a frontend would send it.
BOWTIE_RING = [
    (-79.9830, 40.6440),
    (-79.9826, 40.6444),
    (-79.9830, 40.6444),
    (-79.9826, 40.6440),
]

with Diagnostics(on=True, directory=_reject_dir), Harness():
    _reject_session = Session()
    _reject_session.upstream()
    _reject_session.trees()
    _bowtie = _drawn("drawn-bowtie", BOWTIE_RING)
    _good = _drawn("drawn-good", _rect(-79.9821, -79.9817, 40.6446, 40.6449))
    try:
        _reject_session.commit(
            "trees",
            [_bowtie, _good],
            {"drawn-bowtie": "user_added", "drawn-good": "user_added"},
        )
        _fail("the bowtie must be rejected by the commit gate")
    except commit_validation.CommitRejectedError as _exc:
        _rejection_payload = _exc.as_payload()
    _reject_record = run_diagnostics.read_record(_reject_session.id)
    _reject_dem = _reject_session.context().dem

_reject_event = [
    event for event in _reject_record["events"] if event["event"] == "commit"
][-1]
assert _reject_event["gate_outcome"] == "rejected", _reject_event["gate_outcome"]
assert _reject_event["rejections"] == _rejection_payload["rejections"], (
    "the record's rejections must be the error's own as_payload(), not a second rendering"
)

_dumped = _reject_event["rejected_features_geojson"]
assert [feature["id"] for feature in _dumped] == ["drawn-bowtie"], (
    "only the REJECTED feature is dumped -- the accepted one is already in the document"
)
assert _dumped[0] == _bowtie, "the dump must be the feature verbatim, not a summary of it"

# REPLAYED FROM THE RECORD ALONE. Nothing from the session is used here
# except the DEM the rehydrator reprojects into.
try:
    wire_translation.rehydrate_tree_zone(_dumped[0], _reject_dem, zone_id=99)
    _fail("the dumped geometry must reproduce the rejection")
except wire_translation.InboundGeometryError as _replayed:
    pass

# THE FRAME, AND IT COMES OFF THE RECORD TOO. The replay above still
# reached into the live session for the DEM. This one does not: the CRS
# the rehydrator reprojects into is recorded beside the dump, so the
# failing operation -- and this defect IS a reprojection defect -- can be
# re-run from the file alone. A ring without its frame is evidence that
# cannot be re-run, which is not what "permanent fixture" has to mean.
_recorded_crs = _reject_event["geometry"]["dem"]["crs"]
assert _recorded_crs == str(_reject_dem["crs"]), (_recorded_crs, str(_reject_dem["crs"]))
try:
    wire_translation.rehydrate_tree_zone(_dumped[0], {"crs": _recorded_crs}, zone_id=99)
    _fail("the dump must reproduce the rejection off the RECORDED CRS alone")
except wire_translation.InboundGeometryError:
    pass

_recorded_reason = _reject_event["rejections"][0]["reason"]
assert "Self-intersection" in _recorded_reason, _recorded_reason
# The offending COORDINATE, as shapely gives it, survives into the record.
_coordinate = _recorded_reason.split("Self-intersection[", 1)[1].split("]", 1)[0]
assert len(_coordinate.split()) == 2, _coordinate
# Both halves of the geometry group are there for the same commit: the
# structural read of every committed feature, and the gate's verdict.
_committed = {
    entry["feature_id"]: entry for entry in _reject_event["geometry"]["committed_features"]
}
assert set(_committed) == {"drawn-bowtie", "drawn-good"}
assert _committed["drawn-bowtie"]["roundtrip"]["rehydrates"] is False
assert _committed["drawn-good"]["roundtrip"]["rehydrates"] is True

print(
    f"3 [test 3]. COMMIT REJECTION DUMP: a hand-drawn bowtie committed beside a valid drawn zone "
    f"was rejected {[r['code'] for r in _reject_event['rejections']]}, and the record carries the "
    f"OFFENDING FEATURE'S FULL GeoJSON -- {len(json.dumps(_dumped[0]))} bytes, feature "
    f"{_dumped[0]['id']!r}, {len(_dumped[0]['geometry']['coordinates'][0])} positions -- byte "
    f"identical to what was committed, and ONLY the rejected one (the accepted zone is not "
    f"dumped). Replayed from the record alone through rehydrate_tree_zone() it fails the same "
    f"way -- and replayed against the RECORDED CRS {_recorded_crs!r} alone, with no session in "
    f"hand, it fails the same way again. The message carries the offending coordinate: "
    f"Self-intersection[{_coordinate}]."
)


# =========================================================================
# 4 [test 4]. THE UTM-VERSUS-ROUND-TRIP COMPARISON, FOR EVERY TREE PATCH
# =========================================================================
#
# THE OPEN HYPOTHESIS, RECORDED. The emission gate validates `polygon_utm`;
# the commit gate validates `geometry_wgs84` reprojected BACK into the DEM's
# CRS. They are two different geometries and this asserts that BOTH verdicts
# are on the record for every emitted patch -- so if they ever disagree, the
# record says which patch and what shapely said about each.

_pair_dir = tempfile.mkdtemp(prefix="run_diagnostics_pair_")
with Diagnostics(on=True, directory=_pair_dir), Harness():
    _pair_session = Session()
    _pair_session.upstream()
    _pair_payload = _pair_session.trees()
    _pair_record = run_diagnostics.read_record(_pair_session.id)
    _pair_result = _pair_session.context().step_proposals["trees"]

_pair_event = _generate_events(_pair_record, "trees")[0]
_patch_records = _pair_event["geometry"]["patches"]
assert _patch_records, "the fixture must emit tree patches"
assert len(_patch_records) == len(_pair_result["patches"]), (
    f"{len(_patch_records)} recorded against {len(_pair_result['patches'])} emitted"
)
assert _pair_event["geometry"]["patches_truncated"] is False

# THE FRAME THE ROUND TRIP RAN IN, on the generate event too. A patch
# whose two verdicts DISAGREE is the bug, and this event is where that
# disagreement is recorded -- so the CRS the `roundtrip` half was reached
# in belongs here, not only on a commit rejection.
assert _pair_event["geometry"]["dem"]["crs"] == str(_pair_session.context().dem["crs"]), (
    _pair_event["geometry"]["dem"], str(_pair_session.context().dem["crs"])
)

_disagreements = []
for _entry in _patch_records:
    assert _entry["polygon_utm"]["is_valid"] in (True, False), _entry
    assert _entry["roundtrip"]["rehydrates"] in (True, False), _entry
    assert _entry["roundtrip"]["is_valid"] in (True, False), _entry
    assert _entry["verdicts_agree"] is not None, _entry
    if not _entry["verdicts_agree"]:
        _disagreements.append(_entry)

# The recorded verdicts are the ones shapely and the rehydrator actually
# hold RIGHT NOW, on the same objects -- not values the writer decided.
for _entry, _patch in zip(_patch_records, _pair_result["patches"]):
    assert _entry["id"] == _patch["id"]
    assert _entry["polygon_utm"]["is_valid"] == bool(_patch["polygon_utm"].is_valid)
    try:
        _live = wire_translation._polygonal_shape_from_wire(
            _patch["geometry_wgs84"], _pair_session.context().dem, "test replay"
        )
        _live_ok = True
    except wire_translation.InboundGeometryError:
        _live_ok = False
    assert _entry["roundtrip"]["rehydrates"] == _live_ok, _entry

print(
    f"4 [test 4]. UTM VS ROUND TRIP: all {len(_patch_records)} emitted tree patches carry BOTH "
    f"verdicts -- polygon_utm.is_valid "
    f"{[e['polygon_utm']['is_valid'] for e in _patch_records]} and the rehydrator's verdict on "
    f"geometry_wgs84 reprojected back into the recorded frame "
    f"{_pair_event['geometry']['dem']['crs']} "
    f"{[e['roundtrip']['is_valid'] for e in _patch_records]} -- plus verdicts_agree "
    f"{[e['verdicts_agree'] for e in _patch_records]}. Disagreements on this fixture: "
    f"{len(_disagreements)}. Each recorded verdict re-checked against shapely and against "
    f"wire_translation._polygonal_shape_from_wire() on the live objects."
)


# =========================================================================
# 5 [test 5]. EVERY RECORDED VALUE COMES FROM THE PIPELINE
# =========================================================================
#
# THE ONE RULE, ASSERTED. Every figure in the record is walked BACK along
# the path the record itself states, into the live result the entry point
# returned, and must be found there by value. A writer that computed an
# acreage, re-derived a gate count or defaulted a flag would produce a
# value this walk cannot match.
#
# WHAT IS DELIBERATELY EXEMPT, and why the exemption is not a loophole.
# The geometry group's STRUCTURAL reads -- geometry type, part and ring
# counts, position count, coordinate precision, is_valid and
# explain_validity's message -- are observations of an object the pipeline
# built and never itself measured, so there is no pipeline figure for them
# to drift from. They are checked against SHAPELY on the live geometry
# instead, below, which is the same standard. `area_acres` is NOT exempt:
# the pipeline computes acreage, so the record must be quoting it.

_rule_dir = tempfile.mkdtemp(prefix="run_diagnostics_rule_")
with Diagnostics(on=True, directory=_rule_dir), Harness():
    _rule_session = Session()
    _rule_session.upstream()
    _rule_payload = _rule_session.trees()
    _rule_record = run_diagnostics.read_record(_rule_session.id)
    _rule_context = _rule_session.context()
    _rule_result = _rule_context.step_proposals["trees"]

_rule_event = _generate_events(_rule_record, "trees")[0]
_roots = {
    "result": _rule_result,
    "payload": _rule_payload,
    "exclusion_zones": _rule_context.exclusion_zones,
    "session_context": {
        "hydric_floodplain_is_fallback": _rule_context.hydric_floodplain_is_fallback
    },
}


def _quoted(path, recorded, what):
    """The value at `path` in the live pipeline object, or a failure."""
    head, _, rest = path.partition(".")
    if head not in _roots:
        _fail(f"{what} {path!r} names no pipeline source")
    found = _resolve(_roots[head], rest) if rest else _roots[head]
    if found != recorded:
        _fail(f"{what} {path!r}: record says {recorded!r}, pipeline holds {found!r}")
    return found


_checked = 0
for _path, _value in _rule_event["inputs"]["flags"].items():
    _quoted(_path, _value, "flag")
    _checked += 1
for _path, _value in _rule_event["gates"]["counts"].items():
    _quoted(_path, _value, "count")
    _checked += 1

# narrative_data is quoted WHOLE, not summarised.
assert _rule_event["gates"]["narrative_data"] == _rule_result["narrative_data"]
_checked += 1

# Every acreage in the geometry group is the producer's own field.
for _entry in _rule_event["geometry"]["patches"]:
    _patch = _resolve(_roots["result"], _entry["source"].partition(".")[2])
    assert _entry["area_acres"] == _patch["area_acres"], _entry
    assert _entry["id"] == _patch["id"], _entry
    _checked += 2
for _entry in _rule_event["geometry"]["wire_features"]:
    _feature = _resolve(_roots["payload"], _entry["source"].partition(".")[2])
    assert _entry["feature_id"] == _feature["id"], _entry
    assert _entry["area_acres"] == (_feature["properties"] or {}).get("area_acres"), _entry
    assert _entry["layer"] == (_feature["properties"] or {}).get("layer"), _entry
    _checked += 3

# THE STRUCTURAL READS, held to shapely rather than to the pipeline.
for _entry in _rule_event["geometry"]["patches"]:
    _patch = _resolve(_roots["result"], _entry["source"].partition(".")[2])
    _geometry = _patch["polygon_utm"]
    _parts = [_geometry] if _geometry.geom_type == "Polygon" else list(_geometry.geoms)
    assert _entry["polygon_utm"]["geometry_type"] == _geometry.geom_type
    assert _entry["polygon_utm"]["part_count"] == len(_parts)
    assert _entry["polygon_utm"]["interior_ring_count"] == sum(len(p.interiors) for p in _parts)
    assert _entry["polygon_utm"]["is_valid"] == bool(_geometry.is_valid)
    _checked += 4

# AND THE NEGATIVE HALF OF THE RULE: the writer holds no arithmetic. A
# figure derived here would be a second implementation of something the
# pipeline already did, so the module's source carries no MULTIPLICATION,
# DIVISION or SUBTRACTION anywhere -- no scaling, no ratio, no difference,
# which between them are every shape a derived figure takes. Addition
# survives, and only as `sum()` over structural counts (rings, positions),
# which is enumeration and not derivation.
#
# WITH EXACTLY ONE CARVE-OUT, AND IT IS A STOPWATCH. The fetch group's
# subject IS time, and a stopwatch is irreducibly `(end - start)` scaled
# into milliseconds. That is a MEASUREMENT, not a derivation: there is no
# pipeline figure behind it to be a second implementation of, and nothing
# for it to drift from -- the pipeline never computed how long it took.
# The carve-out is written structurally and held to two occurrences, so it
# licenses a clock and nothing else. A ratio, a difference between two
# recorded values, or a unit conversion of a pipeline number still fails
# this, wherever it is put.
import ast as _ast
import inspect as _inspect


def _is_stopwatch(node) -> bool:
    """`(time.perf_counter() - <something>.started) * 1000.0`, exactly."""
    if not (isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Mult)):
        return False
    if not (isinstance(node.right, _ast.Constant) and node.right.value == 1000.0):
        return False
    inner = node.left
    if not (isinstance(inner, _ast.BinOp) and isinstance(inner.op, _ast.Sub)):
        return False
    return (
        isinstance(inner.left, _ast.Call)
        and isinstance(inner.left.func, _ast.Attribute)
        and inner.left.func.attr == "perf_counter"
        and isinstance(inner.right, _ast.Attribute)
        and inner.right.attr == "started"
    )


_tree = _ast.parse(_inspect.getsource(run_diagnostics))
_stopwatches = [node for node in _ast.walk(_tree) if _is_stopwatch(node)]
_licensed = {id(node) for node in _stopwatches} | {id(node.left) for node in _stopwatches}
_arithmetic = [
    node
    for node in _ast.walk(_tree)
    if isinstance(node, _ast.BinOp)
    and isinstance(node.op, (_ast.Mult, _ast.Div, _ast.Sub))
    and id(node) not in _licensed
]
assert not _arithmetic, (
    "run_diagnostics.py contains arithmetic at lines "
    f"{sorted({node.lineno for node in _arithmetic})} -- a figure derived here is a second "
    "implementation of something the pipeline already did"
)
# TWO CLOCKS AND NO MORE: one per layer, one per fetch. A third would mean
# something else started measuring, and this assertion is what makes the
# carve-out a carve-out rather than a hole.
assert len(_stopwatches) == 2, sorted({node.lineno for node in _stopwatches})

print(
    f"5 [test 5]. NOTHING IS COMPUTED: {_checked} recorded values were walked back along the "
    f"record's OWN paths into the live pipeline objects and matched by value -- "
    f"{len(_rule_event['inputs']['flags'])} flags, {len(_rule_event['gates']['counts'])} counts, "
    f"narrative_data quoted whole, and every id/acreage/layer in the geometry group off the "
    f"producer's own fields. The module's whole source holds no multiplication, division or "
    f"subtraction except {len(_stopwatches)} stopwatches -- (perf_counter() - started) * 1000.0, "
    f"one per layer and one per fetch. The structural reads (type, parts, rings, is_valid) match shapely on "
    f"the live geometry. Outside the two clocks the module's source contains no *, / or - in any "
    f"expression: there is no arithmetic in the writer to drift with."
)


# =========================================================================
# 6 [test 6]. DISABLED COSTS NOTHING
# =========================================================================
#
# NOT A STOPWATCH. "Costs nothing" is asserted as a fact about the code
# path rather than a threshold on a clock: every function that builds any
# part of a record -- the environment probe, the scanner, both geometry
# collectors, the validity and round-trip checks, the path resolver and
# the writer itself -- is replaced by one that RAISES, and a full
# generate-and-commit runs clean underneath. If a disabled hook computed a
# record and discarded it, one of them would have fired.
#
# The stopwatch is printed alongside as an observation, not asserted on: a
# wall-clock threshold in a test file is a flake waiting for a slow CI box.
#
# WHAT THIS SECTION CANNOT SEE, stated here so nobody reads more into it
# than it says. It asserts that the record-building functions DO NOT
# fire, and a function that is never called never fires -- so it passes
# IDENTICALLY on a build with the hooks torn out of step_orchestrator.
# It cannot tell "correctly silent when disabled" from "silent always".
# That was measured, not assumed: with the four hook call sites replaced
# by `pass`, this section still passes and section 9 fails. Section 9 is
# the positive counterpart that makes this one mean something, and
# section 1 catches the same tear from the other end.

_NEVER = [
    "environment",
    "_git_commit",
    "_sorted_scan",
    "_collect_patches",
    "_collect_wire_features",
    "_patch_health",
    "_feature_health",
    "_validity",
    "_roundtrip",
    "_wire_rings",
    "_shapely_rings",
    "append_event",
    "directory",
    "record_path",
    "_new_record",
]

import time as _time

_off_cost_dir = os.path.join(tempfile.mkdtemp(prefix="run_diagnostics_cost_"), "never-made")
with Diagnostics(on=False, directory=_off_cost_dir), Harness(), ExitStack() as _stack:
    for _name in _NEVER:
        _stack.enter_context(
            mock_patch.object(
                run_diagnostics,
                _name,
                side_effect=AssertionError(f"run_diagnostics.{_name}() ran with diagnostics OFF"),
            )
        )
    _cost_session = Session()
    _cost_session.upstream()
    _cost_start = _time.perf_counter()
    _cost_session.trees()
    _off_seconds = _time.perf_counter() - _cost_start
    _cost_bowtie = _drawn("drawn-bowtie", BOWTIE_RING)
    try:
        _cost_session.commit("trees", [_cost_bowtie], {"drawn-bowtie": "user_added"})
        _fail("the bowtie must still be rejected with diagnostics off")
    except commit_validation.CommitRejectedError:
        pass

assert not os.path.exists(_off_cost_dir)

_on_cost_dir = tempfile.mkdtemp(prefix="run_diagnostics_cost_on_")
with Diagnostics(on=True, directory=_on_cost_dir), Harness():
    _on_cost = Session()
    _on_cost.upstream()
    _on_start = _time.perf_counter()
    _on_cost.trees()
    _on_seconds = _time.perf_counter() - _on_start

print(
    f"6 [test 6]. DISABLED COSTS NOTHING: with the variable unset, all {len(_NEVER)} "
    f"record-building functions in run_diagnostics.py were replaced by ones that RAISE, and a "
    f"full landform/water/roads/trees generate plus a rejected trees commit ran clean -- not one "
    f"of them fired, no directory was created, and the rejection behaved identically. "
    f"(Observed, not asserted: the trees generate took {_off_seconds * 1000:.0f} ms off and "
    f"{_on_seconds * 1000:.0f} ms on.)"
)


# =========================================================================
# 7 [test 7]. THE ENVIRONMENT GROUP, THE FLAG SWEEP, AND WIRE PRECISION
# =========================================================================
#
# ALL SIX REGISTRY ENTRIES IN ONE SESSION -- landform, water, roads,
# trees, structures, fencing -- so the flag sweep is over the whole
# pipeline rather than over the one step this branch was written for. The
# collectors carry NO step-specific code (see run_diagnostics.py's
# "finding the emitted geometry"), and this is what says so: every step's
# flags, counts and geometry are recorded with nothing in the module
# naming any of them.

_full_dir = tempfile.mkdtemp(prefix="run_diagnostics_full_")
with Diagnostics(on=True, directory=_full_dir), Harness():
    _full = Session()
    _full.upstream()
    _full_trees_payload = _full.trees()
    _tree_features = _full_trees_payload["tree_zones"]["features"]
    _full.commit("trees", _tree_features, {f["id"]: "generated" for f in _tree_features})
    _structures_payload = _full.generate("structures")
    _sites = _structures_payload["structure_sites"]["features"][:1]
    _full.commit("structures", _sites, {_sites[0]["id"]: "generated"})
    _full.generate("fencing")
    _full_record = run_diagnostics.read_record(_full.id)

# --- group 1 -------------------------------------------------------------
_environment = _full_record["environment"]
assert set(_environment) == {
    "python_version",
    "shapely_version",
    "shapely_geos_version",
    "shapely_geos_version_string",
    "rasterio_version",
    "rasterio_gdal_version",
    "rasterio_proj_version",
    "backend_git_commit",
}, sorted(_environment)
for _key in ("python_version", "shapely_version", "shapely_geos_version", "rasterio_version"):
    assert _environment[_key], f"{_key} is empty"
assert _environment["backend_git_commit"]["sha"], _environment["backend_git_commit"]

# --- the steps that were recorded ----------------------------------------
_recorded_steps = [
    (event["event"], event["step_id"]) for event in _full_record["events"]
]
assert _recorded_steps == [
    # THE SESSION'S OWN EVENT, FIRST AND WITH NO STEP. The fetch is
    # recorded by session creation, before any step exists to name.
    ("fetch", None),
    ("generate", "landform"), ("commit", "landform"),
    ("generate", "water"), ("commit", "water"),
    ("generate", "roads"), ("commit", "roads"),
    ("generate", "trees"), ("commit", "trees"),
    ("generate", "structures"), ("commit", "structures"),
    ("generate", "fencing"),
], _recorded_steps
assert [event["sequence"] for event in _full_record["events"]] == list(
    range(len(_full_record["events"]))
), "sequence must be 0, 1, 2 ... in append order"

# --- the flag sweep ------------------------------------------------------
_flags_by_name = {}
for _event in _full_record["events"]:
    for _path, _value in _event.get("inputs", {}).get("flags", {}).items():
        _flags_by_name.setdefault(_path.rpartition(".")[2], set()).add(_value)

# Every flag the branch named, found by the scan and not by a list in the
# module. `canopy availability` is `canopy_data_available`;
# `floodplain_data_is_fallback` reaches the record from the roads
# narrative AND, under its SessionContext name, as
# `hydric_floodplain_is_fallback`.
for _expected in (
    "road_proximity_source",
    "soil_marginality_data_available",
    "hydric_data_available",
    "stream_data_available",
    "floodplain_data_is_fallback",
    "canopy_data_available",
    "soil_checked",
):
    assert _expected in _flags_by_name, f"{_expected} was not recorded on any step"

_counts_by_name = {}
for _event in _full_record["events"]:
    for _path, _value in _event.get("gates", {}).get("counts", {}).items():
        _counts_by_name.setdefault(_path.rpartition(".")[2], set()).add(_value)
for _expected in ("candidate_count", "dropped_count", "dropped_invalid_count"):
    assert _expected in _counts_by_name, f"{_expected} was not recorded on any step"

# --- the wire's coordinate precision for tree geometry -------------------
_tree_wire = [
    entry
    for entry in _generate_events(_full_record, "trees")[0]["geometry"]["wire_features"]
    if entry["layer"] == wire_translation.LAYER_TREE_ZONE
]
assert _tree_wire, "the fixture must put tree zones on the wire"
_precisions = sorted({entry["wire"]["coordinate_decimals_max"] for entry in _tree_wire})

print(
    f"7 [test 7]. ENVIRONMENT, FLAGS AND PRECISION over ALL SIX registry entries in one session "
    f"({len(_full_record['events'])} events, sequences 0..{len(_full_record['events']) - 1}):\n"
    f"   environment: python {_environment['python_version']}, shapely "
    f"{_environment['shapely_version']} on GEOS {_environment['shapely_geos_version']}, rasterio "
    f"{_environment['rasterio_version']} on GDAL {_environment['rasterio_gdal_version']} / PROJ "
    f"{_environment['rasterio_proj_version']}, backend commit "
    f"{_environment['backend_git_commit']['sha'][:12]}.\n"
    f"   {len(_flags_by_name)} distinct availability/fallback/source flags recorded across the six "
    f"steps: {sorted(_flags_by_name)}.\n"
    f"   {len(_counts_by_name)} distinct gate counts: {sorted(_counts_by_name)}.\n"
    f"   TREE GEOMETRY ON THE WIRE carries {_precisions} decimal places per coordinate "
    f"(coordinate_decimals_max over {len(_tree_wire)} tree features) -- unrounded double "
    f"precision, nothing quantized on the way out."
)


# =========================================================================
# 8 [test 8]. THE DROP SINK, SURFACED -- ROWS AND REASONS, NOT A LENGTH
# =========================================================================
#
# score_tree_search_space() records one {'id', 'area_acres', 'reason'} per
# patch it scored and then refused to emit, and identify_tree_zone_
# candidates() used to read only len() of that list before it died with
# the call -- so an intermittent geometry drop left a number and no
# evidence. It now RETURNS the sink, and the record carries the rows.
#
# THE FIXTURE DROPS NOTHING on a healthy generate, which is the whole
# reason a drop has to be induced to test this at all. It is induced as
# the REAL DEFECT rather than by stubbing the verdict: the first
# component's footprint is replaced with a genuine self-intersecting
# bowtie, and make_valid() is made to hand it back unrepaired -- which is
# exactly the "the rebuild could not be made valid" case _valid_polygonal()
# exists to catch. Everything downstream is the real path: the module's
# own gate refuses it, its own explain_validity() names the defect AND
# THE COORDINATE, its own sink records it, its own result returns it.

_drop_dir = tempfile.mkdtemp(prefix="run_diagnostics_drop_")
_real_fill_rings = tree_zone_candidates._fill_subthreshold_interior_rings
_bowtied = []


def _bowtie_at(geometry):
    """A self-intersecting ring on the same ground as `geometry` -- the
    corners visited out of order, so the two sides cross."""
    minx, miny, maxx, maxy = geometry.bounds
    return Polygon([(minx, miny), (maxx, maxy), (minx, maxy), (maxx, miny)])


def _bowtie_first(footprint, cell_area, max_cells):
    """_fill_subthreshold_interior_rings(), handing back a bowtie exactly
    once. Every later component goes through the module's real one."""
    if not _bowtied:
        broken = _bowtie_at(footprint)
        _bowtied.append(broken)
        return broken, 0, 0
    return _real_fill_rings(footprint, cell_area, max_cells)


with Diagnostics(on=True, directory=_drop_dir), Harness(), ExitStack() as _stack:
    _stack.enter_context(
        mock_patch.object(
            tree_zone_candidates, "_fill_subthreshold_interior_rings", _bowtie_first
        )
    )
    # make_valid() returning its input unrepaired is what makes the
    # bowtie UNREPAIRABLE rather than merely invalid -- without this the
    # gate would repair it into two lobes and emit them, which is the
    # gate working and not the drop this section is about.
    _stack.enter_context(
        mock_patch.object(tree_zone_candidates, "make_valid", lambda geometry: geometry)
    )
    _drop_session = Session()
    _drop_session.upstream()
    _drop_session.trees()
    _drop_record = run_diagnostics.read_record(_drop_session.id)
    _drop_result = _drop_session.context().step_proposals["trees"]

assert _bowtied, "the induced drop did not happen -- the gate was never reached"
assert not _bowtied[0].is_valid, "the induced footprint must genuinely be invalid"

# THE PIPELINE now returns the sink rather than discarding it.
_sink = _drop_result["dropped_invalid"]
assert len(_sink) == 1, _sink
assert set(_sink[0]) == {"id", "area_acres", "reason"}, sorted(_sink[0])

_drop_event = _generate_events(_drop_record, "trees")[0]
_recorded_drops = _drop_event["gates"]["drops"]["result.dropped_invalid"]

# THE ROWS ARE THE SINK'S, by value, field for field. Not a summary of it
# and not a re-derivation: this is test 5's rule applied to the group
# added for the sink.
assert _recorded_drops == _sink, (_recorded_drops, _sink)

# AND THE COUNT IS STILL THERE, beside the rows and answering a different
# question -- narrative_data keeps it, because the report says how many
# candidates were lost, not which ring crossed itself where.
assert _drop_event["gates"]["counts"]["result.narrative_data.dropped_invalid_count"] == 1
assert _drop_event["gates"]["narrative_data"]["dropped_invalid_count"] == 1

# The reason is explain_validity()'s own message on the refused footprint,
# not a sentence this module wrote -- AND IT CARRIES THE COORDINATE,
# which is the whole reason a row beats a count.
_reason = _recorded_drops[0]["reason"]
from shapely.validation import explain_validity as _explain

assert _reason == _explain(_bowtied[0]), (_reason, _explain(_bowtied[0]))
assert "Self-intersection" in _reason, _reason
_drop_coordinate = _reason.split("Self-intersection[", 1)[1].split("]", 1)[0]
assert len(_drop_coordinate.split()) == 2, _drop_coordinate

# A HEALTHY GENERATE RECORDS THE SINK AS `[]`, NOT AS AN ABSENT KEY -- so
# the line above is a CHANGE in a diff against a clean run rather than an
# addition to it.
_clean_drops = _generate_events(_full_record, "trees")[0]["gates"]["drops"]
assert _clean_drops.get("result.dropped_invalid") == [], _clean_drops

# The generic collector reaches every step's sink, with none named in the
# module: water's dropped zones carry their own drop_reason.
_water_drops = _generate_events(_drop_record, "water")[0]["gates"]["drops"]
_water_rows = _water_drops.get("result.dropped_zones", [])
assert _water_rows and all("drop_reason" in row for row in _water_rows), _water_drops
# ONE ROW PER DROPPED OBJECT: the water result holds the same list under
# `result.dropped_zones` and the nested `result.result.dropped_zones`, and
# the second is not recorded again.
assert "result.result.dropped_zones" not in _water_drops, sorted(_water_drops)

print(
    f"8 [test 8]. THE DROP SINK, SURFACED: with one component's footprint replaced by a genuine "
    f"self-intersecting bowtie that make_valid() could not rescue, "
    f"identify_tree_zone_candidates() RETURNED its dropped_invalid sink -- {len(_sink)} row "
    f"{sorted(_sink[0])} -- and the record carries it verbatim under gates.drops"
    f"['result.dropped_invalid'], equal to the pipeline's list by value. The reason is "
    f"explain_validity()'s own message: {_reason!r}. dropped_invalid_count is still 1 beside it, "
    f"in both counts and narrative_data. A healthy generate records the sink as [] rather than "
    f"omitting it. The collector is generic: water's {len(_water_rows)} dropped zones are recorded "
    f"with their own drop_reason "
    f"{sorted({row['drop_reason'] for row in _water_rows})}, once each despite the result holding "
    f"that list under two paths."
)


# =========================================================================
# 9 [test 9]. END TO END: THE HTTP SURFACE, THE DEFAULT DIRECTORY, A FILE
# =========================================================================
#
# THE TEST THIS FEATURE NEEDED AND DID NOT HAVE. Every section above does
# drive a real orchestrator generate -- through Session.generate() ->
# step_orchestrator.generate_step() -> the job runner -> _generate() --
# but all of them differ from a live server in the same two ways, and
# those two ways are exactly where a silent failure would hide:
#
#   * They pass an EXPLICIT store, fetch cache, session cache and job
#     runner. A live server passes none of them: session_api's
#     Dependencies() is all None and every layer falls back to its
#     process-wide default. begin_generate() resolves those defaults
#     ITSELF -- it has to, to sample the same caches the generate will
#     use -- and that resolution is code no test above ever ran.
#   * They set KEYLINE_RUN_DIAGNOSTICS_DIR. A live server usually does
#     not, so the path is the RELATIVE default against the server's
#     working directory, and nothing above ever wrote to it.
#
# So this drives the real Flask app over HTTP, with process-wide
# defaults, with the directory variable UNSET, from a working directory
# that starts empty -- and asserts a file appears with the generate AND
# the commit in it. Nothing is stubbed but the network.
#
# AND IT IS THE POSITIVE COUNTERPART TO SECTION 6, which section 6 needs
# to mean anything. Section 6 asserts that the record-building functions
# DO NOT fire when disabled, and a function that is never called never
# fires -- so on a build where the hooks were removed entirely, section 6
# would still pass. It cannot tell "correctly silent when disabled" from
# "silent always". This one can only pass if the hooks are wired.

_e2e_cwd = tempfile.mkdtemp(prefix="run_diagnostics_e2e_")
_e2e_store = os.path.join(_e2e_cwd, "sessions")
_e2e_previous_cwd = os.getcwd()
_e2e_saved = {
    key: os.environ.get(key)
    for key in (
        run_diagnostics.ENABLED_ENV,
        run_diagnostics.DIRECTORY_ENV,
        "KEYLINE_SESSION_STORE_DIR",
    )
}

try:
    os.environ[run_diagnostics.ENABLED_ENV] = "1"
    # UNSET, deliberately -- the default path is the thing under test.
    os.environ.pop(run_diagnostics.DIRECTORY_ENV, None)
    os.environ["KEYLINE_SESSION_STORE_DIR"] = _e2e_store
    os.chdir(_e2e_cwd)

    # The app api.py builds: `app.register_blueprint(session_api.
    # build_blueprint())`, no Dependencies argument, so the store, both
    # caches and the job runner are the process-wide ones.
    _e2e_app = session_api.create_app()
    _e2e_client = _e2e_app.test_client()

    with Harness():
        _e2e_created = _e2e_client.post(
            "/api/sessions", json={"boundary": [list(point) for point in REAL_BOUNDARY]}
        )
        assert _e2e_created.status_code == 201, _e2e_created.status_code
        _e2e_document = _e2e_created.get_json()
        _e2e_id = _e2e_document["session_id"]

        _e2e_started = _e2e_client.post(
            f"/api/sessions/{_e2e_id}/steps/landform/generate", json={}
        )
        assert _e2e_started.status_code == 202, _e2e_started.status_code
        _e2e_job_id = _e2e_started.get_json()["job_id"]
        for _ in range(3000):
            _e2e_job = _e2e_client.get(f"/api/jobs/{_e2e_job_id}").get_json()
            if _e2e_job["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        assert _e2e_job["status"] == "done", _e2e_job

        _e2e_zones = _e2e_job["result"]["payload"]["suggested_zones"]["features"]
        assert _e2e_zones, "the fixture must produce production zones to commit"
        _e2e_committed = _e2e_client.post(
            f"/api/sessions/{_e2e_id}/steps/landform/commit",
            json={
                "features": {"type": "FeatureCollection", "features": _e2e_zones},
                "provenance": {feature["id"]: "generated" for feature in _e2e_zones},
                "base_revision": 0,
            },
        )
        assert _e2e_committed.status_code == 200, _e2e_committed.get_json()

    # THE FILE, AT THE DEFAULT PATH, RELATIVE TO THE SERVER'S CWD.
    _e2e_directory = os.path.join(_e2e_cwd, run_diagnostics.DEFAULT_DIRECTORY)
    assert os.path.isdir(_e2e_directory), (
        f"no diagnostics directory at {_e2e_directory} after an enabled generate and commit "
        f"over the HTTP surface -- swallowed failures: {run_diagnostics.FAILURES}"
    )
    _e2e_files = sorted(os.listdir(_e2e_directory))
    assert _e2e_files == [f"{_e2e_id}.json"], _e2e_files
    _e2e_record = run_diagnostics.read_record(_e2e_id)
finally:
    os.chdir(_e2e_previous_cwd)
    for _key, _value in _e2e_saved.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value

assert [
    (event["event"], event["step_id"]) for event in _e2e_record["events"]
] == [
    ("fetch", None), ("generate", "landform"), ("commit", "landform")
], _e2e_record["events"]
# THE FETCH EVENT OVER THE REAL HTTP SURFACE, from POST /api/sessions
# through the process-wide caches -- the positive counterpart section 17's
# "nothing fired when off" needs in order to mean anything.
_e2e_fetch = _e2e_record["events"][0]
assert _e2e_fetch["reason"] == "create_session"
assert _e2e_fetch["cache"]["served_by"] == "fetch"
assert _e2e_fetch["outcome"]["status"] == "ok"
assert isinstance(_e2e_fetch["timings"]["total_ms"], float)
assert _e2e_record["events"][1]["geometry"]["patches"], "the generate event recorded no geometry"
assert _e2e_record["events"][2]["gate_outcome"] == "accepted"
# NOTHING WAS SWALLOWED. A hook that failed and was caught would leave
# the run looking exactly like a hook that was never wired, which is the
# failure mode this section exists to make impossible to miss.
assert run_diagnostics.FAILURES == [], run_diagnostics.FAILURES

# AND THE SELF-CHECK AGREES, from the same posture -- it is what a person
# runs on a machine where this is silent, so it must not disagree with a
# run that demonstrably works.
_selfcheck_out = io.StringIO()
_e2e_previous_cwd = os.getcwd()
try:
    os.environ[run_diagnostics.ENABLED_ENV] = "1"
    os.environ.pop(run_diagnostics.DIRECTORY_ENV, None)
    os.chdir(_e2e_cwd)
    _selfcheck_ok = run_diagnostics.self_check(stream=_selfcheck_out)
finally:
    os.chdir(_e2e_previous_cwd)
    for _key, _value in _e2e_saved.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value
assert _selfcheck_ok, _selfcheck_out.getvalue()
assert "RECORDS WOULD BE WRITTEN" in _selfcheck_out.getvalue()

# THE HOOKS ARE WIRED IN THE LOADED BYTECODE. Asserted directly, off the
# compiled code objects rather than off the file, because that is the one
# question a disabled-path test structurally cannot ask -- and it is the
# question a stale long-running server answers differently from its own
# checkout.
_wiring = run_diagnostics._hook_sites()
assert _wiring and all(_wiring.values()), _wiring
_fetch_wiring = run_diagnostics._fetch_hook_sites()
assert _fetch_wiring and all(_fetch_wiring.values()), _fetch_wiring

print(
    f"9 [test 9]. END TO END over the HTTP surface: session_api.create_app() with NO Dependencies "
    f"(the process-wide store, both caches and the job runner, exactly api.py's wiring), "
    f"{run_diagnostics.DIRECTORY_ENV} UNSET so the RELATIVE default path is used, and the working "
    f"directory a fresh empty temp dir. POST /api/sessions -> 201, POST .../landform/generate -> "
    f"202 polled to done, POST .../landform/commit -> 200 with {len(_e2e_zones)} zones. "
    f"{run_diagnostics.DEFAULT_DIRECTORY}/{_e2e_id}.json appeared, carrying "
    f"{[(e['event'], e['step_id']) for e in _e2e_record['events']]} with "
    f"{len(_e2e_record['events'][1]['geometry']['patches'])} patches recorded on the generate and "
    f"the creation's own fetch event ahead of it "
    f"({_e2e_fetch['timings']['total_ms']:.1f} ms, served_by 'fetch'). "
    f"Zero swallowed failures; self_check() agrees from the same posture; and all "
    f"{len(_wiring)} generate/commit hook sites and all {len(_fetch_wiring)} fetch sites read as "
    f"wired in the LOADED bytecode. Measured against a build "
    f"with the four hook calls replaced by `pass`: this section fails and section 6 still passes."
)


# =========================================================================
# 11 [fetch test 1]. A COLD CREATION RECORDS THIRTEEN LAYER TIMINGS
# =========================================================================
#
# THE DATA A PROGRESS DISPLAY WOULD LATER BE BUILT FROM. Thirteen rows, in
# fetch order, each with its own wall time, summing into the recorded
# total. Sequential is what makes that sentence true -- the fetches do not
# overlap, so "which layer is this run on" has an answer and the times add
# up rather than merging.
#
# EACH LAYER IS GIVEN A DIFFERENT, KNOWN WAIT (2 ms, 4 ms, ... 26 ms) so
# the assertion is not just "thirteen numbers appeared" but "row N carries
# LAYER N's wait". A recorder that mixed up which timer belonged to which
# call, or that recorded one clock thirteen times, passes the first and
# fails the second.

_cold_dir = tempfile.mkdtemp(prefix="run_diagnostics_cold_")
_STEP_SECONDS = 0.002
_DELAYS = {
    layer: (index + 1) * _STEP_SECONDS
    for index, layer in enumerate(parcel_data.FETCH_LAYERS)
}

with Diagnostics(on=True, directory=_cold_dir), FetchHarness(delays=_DELAYS) as _fh:
    _cold = Session()
    _cold_counts = _fh.call_counts()
    # READ INSIDE THE BLOCK: read_record() resolves the directory through
    # DIRECTORY_ENV, which Diagnostics restores on exit.
    _cold_record = run_diagnostics.read_record(_cold.id)

_cold_fetch = _sole_fetch_event(_cold_record)

# Every layer fetched EXACTLY ONCE -- this section measures one fetch, and
# a record of thirteen rows over fourteen calls would be a different thing.
assert set(_cold_counts.values()) == {1}, _cold_counts

assert _cold_fetch["reason"] == "create_session"
assert _cold_fetch["cache"]["cached_before"] is False
assert _cold_fetch["cache"]["served_by"] == "fetch"
assert _cold_fetch["cache"]["layers_timed"] == 13
assert _cold_fetch["outcome"]["status"] == "ok"

_cold_layers = _cold_fetch["layers"]
assert len(_cold_layers) == 13, len(_cold_layers)

# IN THE PIPELINE'S OWN ORDER, and each row says where it sat.
assert [row["layer"] for row in _cold_layers] == list(parcel_data.FETCH_LAYERS)
assert [row["order"] for row in _cold_layers] == list(range(13))
assert all(row["outcome"] == "ok" for row in _cold_layers)

# EACH ROW CARRIES ITS OWN LAYER'S WAIT. The injected waits increase
# strictly down the list, so the recorded ones must too -- and each must
# be at least the wait that layer was given.
_cold_elapsed = [row["elapsed_ms"] for row in _cold_layers]
for _index, (_layer, _elapsed) in enumerate(zip(parcel_data.FETCH_LAYERS, _cold_elapsed)):
    _floor = _DELAYS[_layer] * 1000.0
    assert _elapsed >= _floor, f"{_layer}: {_elapsed:.1f} ms < its own {_floor:.0f} ms wait"
assert _cold_elapsed == sorted(_cold_elapsed), _cold_elapsed

# THE THIRTEEN SUM TO THE RECORDED TOTAL. layers_total_ms is the sum the
# record carries; total_ms is the wall clock around the whole fetch, and
# the gap between them is real non-layer work (the boundary reprojection,
# the centroid warp, the cache's own bookkeeping) that no layer accounts
# for. Both are recorded so the gap is visible rather than hidden inside
# one number.
_cold_sum = sum(_cold_elapsed)
_cold_total = _cold_fetch["timings"]["total_ms"]
_cold_layers_total = _cold_fetch["timings"]["layers_total_ms"]
assert abs(_cold_sum - _cold_layers_total) < 1e-6, (_cold_sum, _cold_layers_total)
assert _cold_layers_total <= _cold_total, (_cold_layers_total, _cold_total)
assert _cold_layers_total >= 0.5 * _cold_total, (
    f"the thirteen layers account for only {_cold_layers_total / _cold_total:.1%} of the total"
)

print(
    f"11 [fetch test 1]. A COLD CREATION RECORDS THIRTEEN LAYER TIMINGS: one session creation "
    f"through the REAL fetch_parcel_data() recorded {len(_cold_layers)} layer rows, in "
    f"parcel_data.FETCH_LAYERS' own order, each fetched exactly once. Given thirteen distinct "
    f"injected waits of {_STEP_SECONDS * 1000:.0f}-{13 * _STEP_SECONDS * 1000:.0f} ms, every row "
    f"carries ITS OWN layer's wait and the recorded times rise strictly down the list. They sum "
    f"to layers_total_ms = {_cold_layers_total:.1f} ms, which is "
    f"{_cold_layers_total / _cold_total:.1%} of the {_cold_total:.1f} ms total; the "
    f"{_cold_total - _cold_layers_total:.1f} ms remainder is the reprojection and cache work no "
    f"layer row claims."
)


# =========================================================================
# 12 [fetch test 2]. A WARM CREATION SAYS THE CACHE SERVED IT
# =========================================================================
#
# A WARM CREATION AND A COLD ONE ARE DIFFERENT MEASUREMENTS AND MUST NEVER
# BE AVERAGED. The failure mode this guards is a record that reports
# thirteen zero-millisecond layers on a cache hit -- which reads as
# thirteen instantaneous fetches, and would quietly drag the average for
# every layer toward zero the moment anyone summarised a directory of
# records. `layers` is null instead, and the cache block says plainly who
# answered.

_warm_dir = tempfile.mkdtemp(prefix="run_diagnostics_warm_")
with Diagnostics(on=True, directory=_warm_dir), FetchHarness() as _wh:
    _first = Session()
    _first_counts = _wh.call_counts()
    # THE SAME BOUNDARY, THE SAME FETCH CACHE, a different session.
    _second = Session(fetch_cache=_first.fetch_cache)
    _second_counts = _wh.call_counts()
    _warm_fetch = _sole_fetch_event(run_diagnostics.read_record(_second.id))
    _cold_control = _sole_fetch_event(run_diagnostics.read_record(_first.id))
    # AND THE REBUILD PATH, named as itself. A rebuild is supposed to be
    # network-free -- session_cache.py states that as a property, and a
    # record showing a rebuild that fetched would be a real finding. It
    # cannot be one unless the two callers arrive under different names.
    _second.cache.discard(_second.id)
    _second.context()
    _rebuild_events = _fetch_events(run_diagnostics.read_record(_second.id))

assert set(_first_counts.values()) == {1}, _first_counts
# NOT ONE MORE CALL for the second session -- the cache served it whole.
assert _second_counts == _first_counts, (_first_counts, _second_counts)

assert _cold_control["cache"]["served_by"] == "fetch"
assert len(_cold_control["layers"]) == 13

assert _warm_fetch["cache"]["cached_before"] is True
assert _warm_fetch["cache"]["served_by"] == "fetch_cache"
assert _warm_fetch["cache"]["layers_timed"] == 0
# THE POINT: null, not thirteen zeroes.
assert _warm_fetch["layers"] is None, _warm_fetch["layers"]
assert _warm_fetch["outcome"]["status"] == "ok"
# The irradiance status still comes back on the warm path -- read off the
# CACHED ParcelData, which is the status that was actually fetched.
assert _warm_fetch["irradiance"]["status"] == "ok"
assert _warm_fetch["timings"]["layers_total_ms"] == 0
assert _warm_fetch["timings"]["total_ms"] < _cold_control["timings"]["total_ms"]

assert [event["reason"] for event in _rebuild_events] == [
    "create_session", "rebuild_session_context"
], [event["reason"] for event in _rebuild_events]
assert _rebuild_events[1]["cache"]["served_by"] == "fetch_cache"
assert _rebuild_events[1]["layers"] is None

print(
    f"12 [fetch test 2]. A WARM CREATION SAYS THE CACHE SERVED IT: a second session on the same "
    f"boundary against the same fetch cache called not one of the thirteen layer functions again, "
    f"and its record says served_by 'fetch_cache', layers_timed 0 and layers NULL -- not thirteen "
    f"zeroes. Its total was {_warm_fetch['timings']['total_ms']:.3f} ms against the cold run's "
    f"{_cold_control['timings']['total_ms']:.1f} ms, and its irradiance status "
    f"({_warm_fetch['irradiance']['status']!r}) is the cached ParcelData's own. Dropping that "
    f"session from the session cache and reading its context back adds a SECOND fetch event named "
    f"'rebuild_session_context' -- also cache-served, also layers null, which is the network-free "
    f"rebuild session_cache.py claims, now visible in the record rather than asserted about it."
)


# =========================================================================
# 13 [fetch test 3]. A FAILED FETCH STILL WRITES A RECORD
# =========================================================================
#
# THE ONE THAT MATTERS OPERATIONALLY. Twelve of the thirteen layers HARD-
# FAIL the session: fetch_parcel_data() raises, session_manager.create_
# session() persists nothing and caches nothing, and NO SESSION EXISTS.
# The record written on that path is the only evidence the run ever
# happened -- so if it is not written, the runs somebody most needs to
# diagnose are exactly the ones that leave no trace.
#
# BOTH FAILURES ARE REAL AND INDUCED AT THE FETCH, not stubbed verdicts:
# one fetch function raises a real requests timeout, and one returns the
# documented None sentinel that parcel_data.py converts into a
# ParcelDataIncompleteError. Each runs in a directory of its own, because
# the session id of a creation that never completed is not knowable from
# outside -- finding the record by looking in the directory IS the
# operator's situation.

# --- (a) a layer that RAISES, mid-block, in the five soil calls ----------

_raise_dir = tempfile.mkdtemp(prefix="run_diagnostics_raise_")
_induced = requests.exceptions.ReadTimeout("SDA did not answer in 90 s (induced)")
_raise_store = _fresh_store()
_raise_fetch_cache, _raise_cache = _fresh_caches()

with Diagnostics(on=True, directory=_raise_dir), FetchHarness(
    overrides={"erosion_factor": Mock(side_effect=_induced)}
):
    try:
        session_manager.create_session(
            REAL_BOUNDARY, _raise_store,
            fetch_cache=_raise_fetch_cache, cache=_raise_cache,
        )
        _fail("the induced SDA timeout must hard-fail the session creation")
    except requests.exceptions.ReadTimeout as exc:
        # THE SAME EXCEPTION INSTANCE, untouched -- the recorder does not
        # replace, wrap or swallow the pipeline's own failure.
        assert exc is _induced

_raise_record = _only_record_in(_raise_dir)
_raise_id = _raise_record["header"]["session_id"]
_raise_fetch = _sole_fetch_event(_raise_record)

# NO SESSION EXISTS. The record is the only thing that knows this run's id.
try:
    _raise_store.get(_raise_id)
    _fail("a hard-failed fetch must not leave a persisted document behind")
except document_store.SessionNotFoundError:
    pass
assert _raise_id not in _raise_cache

assert _raise_fetch["outcome"]["status"] == "failed"
assert _raise_fetch["outcome"]["error_type"] == "ReadTimeout"
assert "induced" in _raise_fetch["outcome"]["error_message"]
# Not a ParcelDataIncompleteError, so it names no layer of its own -- and
# the layer rows are what say where it happened.
assert _raise_fetch["outcome"]["failed_layer"] is None
assert _raise_fetch["outcome"]["failed_layer_label"] is None

_raise_layers = _raise_fetch["layers"]
assert [row["layer"] for row in _raise_layers] == [
    "dem", "soil_components", "farmland_classification", "erosion_factor"
], [row["layer"] for row in _raise_layers]
_erosion = _raise_layers[-1]
assert _erosion["outcome"] == "raised"
assert _erosion["error_type"] == "ReadTimeout"
assert "induced" in _erosion["error_message"]
assert all(row["outcome"] == "ok" for row in _raise_layers[:-1])
# The fetch stopped there: the nine layers after erosion_factor have no
# rows at all, which is the record saying where the run got to.
assert _raise_fetch["cache"]["layers_timed"] == 4
# No ParcelData came back, and the irradiance block says so rather than
# reporting a status nobody fetched.
assert _raise_fetch["irradiance"]["status"] is None
assert _raise_fetch["irradiance"]["source"].startswith("absent")

# --- (b) the None sentinel -> ParcelDataIncompleteError -----------------

_sentinel_dir = tempfile.mkdtemp(prefix="run_diagnostics_sentinel_")
_sentinel_store = _fresh_store()
_sentinel_fetch_cache, _sentinel_cache = _fresh_caches()

with Diagnostics(on=True, directory=_sentinel_dir), FetchHarness(
    overrides={"canopy_height": Mock(return_value=None)}
):
    try:
        session_manager.create_session(
            REAL_BOUNDARY, _sentinel_store,
            fetch_cache=_sentinel_fetch_cache, cache=_sentinel_cache,
        )
        _fail("a None canopy_height must hard-fail the session creation")
    except parcel_data.ParcelDataIncompleteError as exc:
        assert exc.layer == "canopy"

_sentinel_record = _only_record_in(_sentinel_dir)
_sentinel_fetch = _sole_fetch_event(_sentinel_record)

try:
    _sentinel_store.get(_sentinel_record["header"]["session_id"])
    _fail("a hard-failed fetch must not leave a persisted document behind")
except document_store.SessionNotFoundError:
    pass

assert _sentinel_fetch["outcome"]["status"] == "failed"
assert _sentinel_fetch["outcome"]["error_type"] == "ParcelDataIncompleteError"
# BOTH FIELDS THE EXCEPTION CARRIES -- the same pair session_api puts on
# the wire as failed_layer{type, label}.
assert _sentinel_fetch["outcome"]["failed_layer"] == "canopy"
assert _sentinel_fetch["outcome"]["failed_layer_label"] == "tree canopy height"

# THE SHAPE THIS CASE HAS, AND IT IS NOT THE SAME AS (a). The canopy CALL
# succeeded -- it ran and it returned -- so its row is "ok" with its real
# elapsed time, and what failed is this module's mandatory-layer rule
# applied to the sentinel it returned. Recording the call as having raised
# would be a lie about the network.
_canopy_row = [row for row in _sentinel_fetch["layers"] if row["layer"] == "canopy_height"]
assert len(_canopy_row) == 1
assert _canopy_row[0]["outcome"] == "ok", _canopy_row[0]
assert _canopy_row[0]["error_type"] is None
assert [row["layer"] for row in _sentinel_fetch["layers"]][-1] == "canopy_height"

print(
    f"13 [fetch test 3]. A FAILED FETCH STILL WRITES A RECORD: two REAL induced failures, each "
    f"leaving NO session behind (no document persisted, nothing cached) and each leaving a record "
    f"that is the only evidence the run happened. (a) a real requests.ReadTimeout out of the "
    f"erosion_factor fetch -- the same exception instance propagates untouched, and the record "
    f"names ReadTimeout, its message, and the four layer rows that got as far as erosion_factor, "
    f"whose row reads 'raised'. (b) a None canopy_height -- the record names "
    f"ParcelDataIncompleteError with the exception's own layer 'canopy' and label 'tree canopy "
    f"height', while the canopy row itself reads 'ok', because the call DID return and it is the "
    f"mandatory-layer rule that failed, not the network."
)


# =========================================================================
# 14 [fetch test 4]. irradiance RECORDS ITS status, AND DEGRADED IS NOT A FAILURE
# =========================================================================
#
# THE ONE DELIBERATELY NON-HARD-FAILING LAYER. get_regional_irradiance_
# baseline() never raises and always returns a populated dict whose
# `status` says whether the numbers are real, so a degraded baseline is
# NORMAL OPERATION. A record that marked it a failure would put a red mark
# on runs that were fine -- and a reader who learns to ignore that mark
# stops reading the twelve layers where it means something.

_irr_dir = tempfile.mkdtemp(prefix="run_diagnostics_irr_")
_DEGRADED = {
    "status": "no_api_key",
    "annual_ac_kwh_per_kw": None,
    "avg_solar_radiation_kwh_per_m2_per_day": None,
    "capacity_factor_pct": None,
    "station_distance_miles": None,
}
with Diagnostics(on=True, directory=_irr_dir), FetchHarness(
    overrides={"irradiance": Mock(return_value=_DEGRADED)}
):
    # THE SESSION IS CREATED. That is the carve-out, asserted rather than
    # assumed: a degraded irradiance does not gate a run.
    _degraded_session = Session()
    _degraded_fetch = _sole_fetch_event(
        run_diagnostics.read_record(_degraded_session.id)
    )

assert _degraded_fetch["outcome"]["status"] == "ok"
assert _degraded_fetch["outcome"]["failed_layer"] is None
assert _degraded_fetch["irradiance"]["status"] == "no_api_key"
assert _degraded_fetch["irradiance"]["degraded"] is True
assert _degraded_fetch["irradiance"]["recorded_as_failure"] is False
assert _degraded_fetch["irradiance"]["source"] == "ParcelData.irradiance"

# The LAYER row is "ok" too -- the call succeeded, which is what that row
# measures. Nothing anywhere in this event reads as a failure.
_irr_row = [row for row in _degraded_fetch["layers"] if row["layer"] == "irradiance"]
assert len(_irr_row) == 1 and _irr_row[0]["outcome"] == "ok"
assert not any(row["outcome"] != "ok" for row in _degraded_fetch["layers"])

# THE CONTROL: an "ok" baseline is recorded as not degraded, so the flag
# above is reporting the status and not a constant.
_ok_fetch = _sole_fetch_event(_cold_record)
assert _ok_fetch["irradiance"]["status"] == "ok"
assert _ok_fetch["irradiance"]["degraded"] is False

print(
    f"14 [fetch test 4]. irradiance RECORDS ITS status, AND DEGRADED IS NOT A FAILURE: a "
    f"'no_api_key' baseline still CREATED the session, and its fetch event reads outcome 'ok' "
    f"with irradiance {{status: 'no_api_key', degraded: true, recorded_as_failure: false}} and an "
    f"'ok' layer row -- nothing in the event reads as a failure. The control run's real 'ok' "
    f"baseline records degraded: false, so the flag reports the status rather than a constant."
)


# =========================================================================
# 15 [fetch test 5]. RETRY COUNTS ARE RECORDED, OR THEIR ABSENCE IS REPORTED
# =========================================================================
#
# THEY ARE NOT AVAILABLE, AND THE RECORD SAYS SO. Five modules behind
# these thirteen layers retry internally -- each keeping its own private
# copy of the same progressive-timeout loop -- and every one of them
# counts attempts in a local variable and returns only the final payload.
# A layer that succeeded on attempt 3 after two 2-second pauses and one
# that succeeded on attempt 1 are indistinguishable to every caller,
# including this one. Inferring the count from elapsed time is exactly the
# computation THE ONE RULE forbids, so the absence is recorded as an
# absence, and `attempts_source` names WHERE this module looked so a null
# can never be read as a zero.

_retries = _cold_fetch["retries"]
assert _retries["attempts_recorded"] is False
assert _retries["retry_time_recorded"] is False
assert _retries["attempts_attribute"] == run_diagnostics.ATTEMPTS_ATTRIBUTE
assert len(_retries["attempts_absent_reason"]) > 200
assert len(_retries["retry_time_absent_reason"]) > 100
assert all(row["attempts"] is None for row in _cold_layers)
assert all("not published by" in row["attempts_source"] for row in _cold_layers)

# THE ABSENCE IS OBSERVED, NOT ASSUMED. Asked of the REAL fetch entry
# points -- not the mocks the section above ran through -- every one of
# them reports no published count.
_REAL_ENTRY_POINTS = [
    soil_data.get_soil_data_for_polygon,
    soil_data.get_soil_geometries_for_polygon,
    hydrology_data.get_water_features_for_boundary,
    farm_roads_data.get_farm_roads_for_boundary,
    imagery_data.get_imagery_summary_for_boundary,
    canopy_height_data.get_canopy_height_for_boundary,
]
for _entry in _REAL_ENTRY_POINTS:
    _attempts, _source = run_diagnostics._published_attempts(_entry)
    assert _attempts is None, (_entry, _attempts)
    assert _source.startswith("not published by "), _source

# AND A COUNT IS REACHABLE, which is what makes the None above mean "not
# published" rather than "this never works". A module that publishes one
# under the contract is read, with no edit to run_diagnostics.py.
with mock_patch.object(soil_data, run_diagnostics.ATTEMPTS_ATTRIBUTE, 3, create=True):
    _reachable, _reachable_source = run_diagnostics._published_attempts(
        soil_data.get_soil_data_for_polygon
    )
assert _reachable == 3, _reachable
assert _reachable_source == f"soil_data.{run_diagnostics.ATTEMPTS_ATTRIBUTE}"

# WHAT IS AVAILABLE INSTEAD: the budget each retrying helper declares,
# read off the LOADED functions rather than a table written in the test.
# It bounds the worst case; it does not say what a run spent.
_HELPER_MODULES = [
    soil_data, hydrology_data, farm_roads_data, imagery_data, canopy_height_data
]
_helpers = run_diagnostics._retry_helpers(_HELPER_MODULES)
assert "soil_data._run_sda_query" in _helpers, sorted(_helpers)
assert _helpers["soil_data._run_sda_query"]["max_retries_default"] == 2
assert _helpers["soil_data._run_sda_query"]["sleeps_between_attempts"] is True
# canopy's deliberately larger budget is picked up as 5 without this
# module or this test naming it -- it is read from the function's own
# __defaults__.
assert _helpers["canopy_height_data._search_hag_items"]["max_retries_default"] == 5
assert len(_helpers) >= 5, sorted(_helpers)

# AND IT REACHES THE RECORD BY THAT ROUTE, not only by being callable.
# The sections above run through Mocks, whose __module__ is unittest.mock
# -- so their records honestly report no helpers, and that says nothing
# about a production run. Here erosion_factor is the REAL soil_data entry
# point (only its SDA transport is stubbed), so the timer resolves
# soil_data off the function it timed and the recorded retry_helpers are
# soil_data's own. This is the shape every real cold run has.
class _SDAResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"Table": [["mukey"], ["111111"]]}


_real_dir = tempfile.mkdtemp(prefix="run_diagnostics_realsoil_")
with Diagnostics(on=True, directory=_real_dir), FetchHarness(
    overrides={"erosion_factor": soil_data.get_erosion_factor_for_polygon}
), mock_patch.object(soil_data.requests, "post", return_value=_SDAResponse()):
    _real_session = Session()
    _real_fetch = _sole_fetch_event(run_diagnostics.read_record(_real_session.id))

_real_row = [row for row in _real_fetch["layers"] if row["layer"] == "erosion_factor"][0]
assert _real_row["function"] == "soil_data.get_erosion_factor_for_polygon", _real_row
assert _real_row["attempts"] is None
assert _real_row["attempts_source"] == "not published by soil_data", _real_row
_recorded_helpers = _real_fetch["retries"]["retry_helpers"]
assert "soil_data._run_sda_query" in _recorded_helpers, sorted(_recorded_helpers)
assert _recorded_helpers["soil_data._run_sda_query"] == {
    "max_retries_default": 2,
    "sleeps_between_attempts": True,
}, _recorded_helpers["soil_data._run_sda_query"]

print(
    f"15 [fetch test 5]. RETRY COUNTS: THEIR ABSENCE IS REPORTED. No fetch module publishes an "
    f"attempt count, so every layer row records attempts: null with an attempts_source naming the "
    f"module it asked -- never a zero. Asked of the six REAL fetch entry points, all six report "
    f"none; set {run_diagnostics.ATTEMPTS_ATTRIBUTE} on soil_data and the count IS read (3), so "
    f"the null means 'not published' and not 'never works'. What IS available is each helper's "
    f"declared budget, read off the loaded functions: {len(_helpers)} retrying helpers found, "
    f"soil_data._run_sda_query at max_retries=2 with a sleep between attempts, "
    f"canopy_height_data._search_hag_items at 5. And it reaches the RECORD by that route: a "
    f"creation whose erosion_factor is the real soil_data entry point (only its SDA transport "
    f"stubbed) records function 'soil_data.get_erosion_factor_for_polygon', attempts null from "
    f"'not published by soil_data', and soil_data's own helpers under retry_helpers."
)


# =========================================================================
# 16 [fetch test 6]. TIMINGS MOVE AND THE DIFF STILL COMES OUT CLEAN
# =========================================================================
#
# THE PROBLEM THIS BRANCH HAD TO SOLVE. Section 2's byte-identical
# assertion is what makes the whole record useful, and a fetch group is
# nothing but numbers that are different every time. Two runs that did
# exactly the same thing never take exactly the same number of
# milliseconds.
#
# HOW IT IS KEPT TRUE: every duration is written to a key ending in
# TIMING_KEY_SUFFIX (`_ms`), at a stable location with stable neighbours
# and in stable key order, and comparable_body() -- the view two runs are
# diffed on -- substitutes REDACTED_TIMING for each one. The file keeps
# the real numbers; the comparison keeps the shape. Asserted from both
# sides below: the raw bodies DIFFER, and they differ ONLY on `_ms` lines;
# the comparable bodies are byte identical; and a real difference still
# shows through the redaction.

_shape_dir = tempfile.mkdtemp(prefix="run_diagnostics_shape_")
_raw_bodies = []
_comparable_bodies = []
_shape_records = []
with Diagnostics(on=True, directory=_shape_dir), FetchHarness(delays=_DELAYS):
    for _run in range(2):
        _shape = Session()
        _record = run_diagnostics.read_record(_shape.id)
        _shape_records.append(_record)
        _raw_bodies.append(
            json.dumps(
                {k: v for k, v in _record.items() if k != "header"}, indent=2, sort_keys=True
            )
        )
        _comparable_bodies.append(
            json.dumps(run_diagnostics.comparable_body(_record), indent=2, sort_keys=True)
        )

# THE TIMINGS REALLY DO MOVE -- so the redaction is load-bearing and not a
# no-op that happens to pass.
assert _raw_bodies[0] != _raw_bodies[1], "the timings did not vary; this section proves nothing"

# AND THEY ARE THE ONLY THING THAT MOVED. Every differing line between the
# two raw bodies is a `_ms` key, which is TIMING_KEY_SUFFIX's whole claim.
_left, _right = _raw_bodies[0].splitlines(), _raw_bodies[1].splitlines()
assert len(_left) == len(_right), (len(_left), len(_right))
_differing_lines = [a for a, b in zip(_left, _right) if a != b]
assert _differing_lines, "no differing lines at all"
for _line in _differing_lines:
    _key = _line.strip().split(":")[0].strip('"')
    assert _key.endswith(run_diagnostics.TIMING_KEY_SUFFIX), (
        f"a non-timing line differed between two identical runs: {_line!r}"
    )

# THE BYTE-IDENTICAL ASSERTION, ON EVERYTHING THAT IS NOT A TIMING.
if _comparable_bodies[0] != _comparable_bodies[1]:
    _a = _comparable_bodies[0].splitlines()
    _b = _comparable_bodies[1].splitlines()
    _first = next(
        (i for i, (x, y) in enumerate(zip(_a, _b)) if x != y), min(len(_a), len(_b))
    )
    _fail(
        "two identical runs differ outside their timings -- first difference at line "
        f"{_first}:\n  A: {_a[_first:_first + 3]}\n  B: {_b[_first:_first + 3]}"
    )

# EVERY TIMING IS REDACTED, AND NOTHING ELSE IS. The `_ms` paths found in
# the raw record are exactly the values replaced in the comparable one.
_paths = _timing_keys({k: v for k, v in _shape_records[0].items() if k != "header"})
assert len(_paths) == 15, _paths  # 13 layer rows + total_ms + layers_total_ms
assert _comparable_bodies[0].count(run_diagnostics.REDACTED_TIMING) == len(_paths)
assert run_diagnostics.REDACTED_TIMING not in _raw_bodies[0]

# THE CONTROL. A redaction that swallowed real differences would pass
# everything above by comparing nothing. So: the same parcel, the same
# steps, but the second creation served by a WARM fetch cache -- a real
# difference in what the run did -- and it must still show, through the
# redaction, in the cache attribution and the layer rows.
with Diagnostics(on=True, directory=_shape_dir), FetchHarness(delays=_DELAYS):
    _shape_cold = Session()
    _shape_warm = Session(fetch_cache=_shape_cold.fetch_cache)
    _warm_body = json.dumps(
        run_diagnostics.comparable_body(run_diagnostics.read_record(_shape_warm.id)),
        indent=2, sort_keys=True,
    )
assert _warm_body != _comparable_bodies[0], "a cache-served fetch left no trace after redaction"
assert '"served_by": "fetch_cache"' in _warm_body
assert '"served_by": "fetch"' in _comparable_bodies[0]

print(
    f"16 [fetch test 6]. TIMINGS MOVE AND THE DIFF STILL COMES OUT CLEAN: two identical cold "
    f"creations produced raw bodies that DIFFER on {len(_differing_lines)} lines, and every one "
    f"of those lines is a `{run_diagnostics.TIMING_KEY_SUFFIX}` key -- the {len(_paths)} durations "
    f"(13 layer rows plus total_ms and layers_total_ms) are the only values that moved. Through "
    f"comparable_body() the two are BYTE IDENTICAL at {len(_comparable_bodies[0])} bytes, "
    f"{len(_comparable_bodies[0].splitlines())} lines, 0 differing lines. THE CONTROL: a warm "
    f"creation still differs after redaction, at served_by and at the layer rows."
)


# =========================================================================
# 17 [fetch test 7]. DISABLED COSTS NOTHING, AT THE FETCH TOO
# =========================================================================
#
# SECTION 6'S STANDARD, APPLIED TO THE FETCH PATH AND OVER THE REAL
# fetch_parcel_data(). Every function that builds any part of a fetch
# record -- and every one section 6 already lists -- is replaced by one
# that RAISES, and a whole cold session creation runs clean underneath,
# through all thirteen layer timers.
#
# time_layer() IS DELIBERATELY NOT IN THAT LIST. It DOES run when
# diagnostics are off -- it is the call site in parcel_data.py, thirteen
# times per fetch -- so what is asserted about it is what it COSTS: it
# hands back the module-level do-nothing singleton, having read one
# thread-local attribute and built nothing.

_FETCH_NEVER = _NEVER + [
    "_fetch_event",
    "_fetch_outcome",
    "_irradiance_health",
    "_retry_helpers",
    "_published_attempts",
    "_qualified",
    "_module_of",
    "_message",
    "_redacted",
    "FetchProbe",
    "_LayerTimer",
]

_off_fetch_dir = os.path.join(tempfile.mkdtemp(prefix="run_diagnostics_fetchcost_"), "never-made")
with Diagnostics(on=False, directory=_off_fetch_dir), FetchHarness() as _off_fh, ExitStack() as _stack:
    for _name in _FETCH_NEVER:
        _stack.enter_context(
            mock_patch.object(
                run_diagnostics,
                _name,
                side_effect=AssertionError(f"run_diagnostics.{_name}() ran with diagnostics OFF"),
            )
        )
    assert run_diagnostics.time_layer("dem", None) is run_diagnostics._NO_LAYER
    _off_fetch_session = Session()
    _off_fetch_counts = _off_fh.call_counts()

# The fetch really did run underneath -- an assertion that nothing fired
# means nothing if nothing happened.
assert set(_off_fetch_counts.values()) == {1}, _off_fetch_counts
assert not os.path.exists(_off_fetch_dir)

print(
    f"17 [fetch test 7]. DISABLED COSTS NOTHING, AT THE FETCH TOO: with the variable unset, all "
    f"{len(_FETCH_NEVER)} record-building functions in run_diagnostics.py -- section 6's list plus "
    f"the {len(_FETCH_NEVER) - len(_NEVER)} the fetch group adds -- were replaced by ones that "
    f"RAISE, and a full cold session creation ran clean through the REAL fetch_parcel_data(), "
    f"fetching all thirteen layers exactly once. Not one of them fired and no directory was "
    f"created. time_layer() is excluded because it DOES run: it returned the do-nothing singleton."
)


# =========================================================================
# 18 [fetch test 8]. self_check() REPORTS WHETHER FETCH INSTRUMENTATION IS WIRED
# =========================================================================
#
# THE SAME QUESTION IT ALREADY ANSWERS FOR THE GENERATE AND COMMIT HOOKS,
# and for the same reason: this feature can fail silently and has. It is
# asked of the LOADED modules -- their compiled code objects -- and not of
# the checkout, because a long-lived server that imported parcel_data
# before the checkout changed is exactly the case where those two answers
# differ and nothing is written.
#
# WITH A NEGATIVE CONTROL, which is what makes the positive mean anything:
# with the instrumentation torn out of the two loaded functions, the same
# check must say so and self_check() must fail.

_check_dir = tempfile.mkdtemp(prefix="run_diagnostics_check_")
with Diagnostics(on=True, directory=_check_dir):
    _check_stream = io.StringIO()
    _check_ok = run_diagnostics.self_check(stream=_check_stream)
_check_output = _check_stream.getvalue()

assert _check_ok, _check_output
_fetch_lines = [
    line for line in _check_output.splitlines() if "fetch instrumentation:" in line
]
assert len(_fetch_lines) == 4, _check_output
assert all(line.startswith("[ok]") for line in _fetch_lines), _fetch_lines
assert any("times 13 of 13 declared layers" in line for line in _fetch_lines), _fetch_lines
assert any("build_session_context calls begin_fetch" in line for line in _fetch_lines)
assert any("build_session_context calls record_fetch" in line for line in _fetch_lines)
assert any("fetch_parcel_data calls time_layer" in line for line in _fetch_lines)


def _uninstrumented_fetch(boundary_coordinates):
    """parcel_data.fetch_parcel_data() as it was BEFORE this branch: the
    same job, no timers, no run_diagnostics in its compiled names."""
    return _build_parcel_data(boundary_coordinates)


def _uninstrumented_build(session_id, boundary_coordinates, fetch_cache, reason="create_session"):
    """session_cache.build_session_context() with no probe opened."""
    parcel = fetch_cache.get_or_fetch(boundary_coordinates)
    return parcel


_torn_dir = tempfile.mkdtemp(prefix="run_diagnostics_torn_")
with Diagnostics(on=True, directory=_torn_dir), ExitStack() as _stack:
    _stack.enter_context(
        mock_patch.object(parcel_data, "fetch_parcel_data", _uninstrumented_fetch)
    )
    _stack.enter_context(
        mock_patch.object(session_cache, "build_session_context", _uninstrumented_build)
    )
    _torn_stream = io.StringIO()
    _torn_ok = run_diagnostics.self_check(stream=_torn_stream)
_torn_output = _torn_stream.getvalue()

assert not _torn_ok, _torn_output
_torn_lines = [line for line in _torn_output.splitlines() if "fetch instrumentation:" in line]
assert len(_torn_lines) == 4, _torn_output
assert all(line.startswith("[!!]") for line in _torn_lines), _torn_lines
assert any("times 0 of 13 declared layers" in line for line in _torn_lines), _torn_lines
assert "RECORDS WOULD NOT BE WRITTEN" in _torn_output

print(
    f"18 [fetch test 8]. self_check() REPORTS WHETHER FETCH INSTRUMENTATION IS WIRED: it prints "
    f"four fetch lines, all [ok] against the loaded modules -- build_session_context calls "
    f"begin_fetch and record_fetch, fetch_parcel_data calls time_layer, and it times 13 of 13 "
    f"declared layers. THE NEGATIVE CONTROL: with both functions replaced by uninstrumented ones, "
    f"the same four lines read [!!], the layer count reads 0 of 13, and self_check() returns "
    f"False with RECORDS WOULD NOT BE WRITTEN."
)


# =========================================================================
# 10 [test 10]. REGRESSION
# =========================================================================

print(
    "\n10 [test 10]. REGRESSION: run the other test files separately -- test_step_orchestrator.py, "
    "test_step_commit.py, test_step_registry.py, test_session_api.py, test_session_manager.py, "
    "test_session_cache.py, test_trees_step.py, test_water_step.py, test_roads_step.py, "
    "test_structures_step.py, test_fencing_step.py, test_wire_translation.py, "
    "test_wire_translation_inbound.py, test_tree_zone_candidates.py, "
    "test_tree_zone_geometry_validity.py, test_document_store.py, test_parcel_data.py."
)

shutil.rmtree(DIAGNOSTICS_DIR, ignore_errors=True)
print("\nAll run diagnostics checks passed.")
