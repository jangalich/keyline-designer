"""
test_structures_step.py

THE STRUCTURES STEP -- the registry's fifth entry, the first POINT layer,
the first whose user-authored feature is SCORED, and the first to consume
every one of the four steps before it. Run as:

    python test_structures_step.py

REAL COORDINATES, REAL PIPELINE CODE, THE SAME FIXTURE AS THE PRIOR
BRANCHES. The boundary is the actual drawn property from generate_full_
report.py -- 5614 N Montour Rd, Gibsonia, PA (~13.23 acres, UTM 17N) -- and
the DEM is test_water_step.py's / test_roads_step.py's / test_trees_step.py's
bench-and-drainage fixture with its flanking levees, unchanged, so the
production zones, water zones, road network and tree zones committed here
are the ones those branches asserted over. session_manager.create_session(),
the terrain warm-up, the landform, water, roads and trees generates, the
commit gate, every rehydrator, solar_suitability.identify_solar_candidate_
zones(), the placed-site scorer and the payload assembly all RUN, for real.
What is mocked is the NETWORK and only the network -- plus the three self-
computes the solar entry point can fall into (identify_road_corridor_
candidates, identify_water_suitability, identify_optimized_production_areas,
each replaced by a COUNTER on the solar module), because a call is what the
committed edges exist to prevent and a real call would go to the network.
The NESTED TREE GENERATE is NOT a counter: it is wrapped and real, because
section 4 has to run it to compare what it regenerates against what was
committed.

Sections (the branch's numbered tests in brackets):
  1  [1]  REGISTRY -- the structures entry is complete; validate_registry()
          passes with FIVE entries; the three new declarations (Placement,
          max_user_added, CROSSINGS_NOT_RECORDED) are validated.
  2  [2]  GENERATE with all four upstream steps committed: zero network,
          zero self-computes. MAX_CANDIDATES is 3; the dropped ranks 4 and
          5 and their scores relative to the surviving three, reported.
  3  [3]  TREE OVERRIDE: the nested tree generator runs ZERO times with the
          committed patches supplied. CONTROL: once with None in its place.
  4  [4]  THE TREE-SET COMPARISON: regenerated versus committed -- count,
          ids, total area -- with a WHOLE commit and with a PARTIAL one,
          and whether solar's candidates change when the override is
          supplied.
  5  [5]  The four run-level flags are on the result and reproduce the
          wire form byte for byte; fetch_layout_layers() could now read
          from context.
  6  [6]  NO_ROAD_CORRIDOR still normalizes (the road self-compute at zero,
          the sentinel received by identity); road_proximity_source reaches
          the payload in three places.
  7  [7]  ROADS COMMITTED EMPTY -> Tier 2 (real mapped roads, off the cache,
          zero fetches), and the payload says so. Control: None fetches.
  8  [8]  A PLACED SITE carries the full measurement set including the
          composite score, is ranked against the generated three, and
          rehydrates with the set intact; a site on bad ground is scored
          and told which gates it failed; a site off the parcel is refused.
  9  [9]  Placed-site scoring makes NO network calls -- a socket counter
          that raises, around the whole verb.
 10  [10] The placed cap of 2 is enforced server-side: a third placed site
          is a rejection naming the rule, and the cap is a registry
          declaration the UI cannot loosen.
 11  [11] Committing several sites (selected and placed) succeeds with NO
          exclusion_crossings key on any feature; committing none succeeds;
          warm and cold reads agree; a reopen restores the placed sites as
          user_added. The HTTP route maps the verb's answers.
 12  [12] Regression is the other test files, run separately.
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
        elevation_grid=[],
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


print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} vertices, "
    f"{PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at {RESOLUTION_METERS:.0f} m. "
    f"Same boundary and same DEM fixture as the water, roads and trees steps; access point A "
    f"{ACCESS_A} (west edge) is the roads branch's own.\n"
)


# --- 1 [test 1]. THE REGISTRY ENTRY ----------------------------------

step_registry.validate_registry()
assert step_registry.registered_steps() == ("landform", "water", "roads", "trees", "structures", "fencing"), (
    step_registry.registered_steps()
)
STRUCTURES = step_registry.get_step("structures")

assert STRUCTURES.generate == "solar_suitability.identify_solar_candidate_zones"
assert STRUCTURES.payload == "step_orchestrator.build_structures_payload"
assert STRUCTURES.proposal_collection == "structure_sites"
assert STRUCTURES.produces == ("structure_sites",)
assert STRUCTURES.upstream_steps() == ("landform", "water", "roads", "trees"), STRUCTURES.upstream_steps()
assert STRUCTURES.user_inputs == () and STRUCTURES.accumulate is None and STRUCTURES.post_commit == ()

_consumed = {c.name: c for c in STRUCTURES.consumes}
assert set(_consumed) == {
    "boundary_coordinates", "dem", "boundary_polygon_utm", "canopy_height",
    "farm_roads", "farmland_classifications",
    "production_areas", "selected_water_zone", "selected_road_corridor", "tree_zone_patches",
}, sorted(_consumed)
for _name in ("valleys", "hydric_floodplain_union", "floodplain_data_is_fallback", "anchor_lon_lat", "exclusion_zones"):
    assert _name not in _consumed, f"{_name} only feeds a self-compute the committed edges close, or nothing"

# THE FOUR COMMITTED EDGES, one per upstream step -- the first entry to
# consume every step before it.
assert _consumed["production_areas"].from_step == "landform" and _consumed["production_areas"].empty_commit is None
_water_edge = _consumed["selected_water_zone"]
assert _water_edge.from_step == "water" and _water_edge.combine == "wire_translation.water_zone_union"
assert _water_edge.empty_commit == "water_suitability.NO_WATER_ZONE"
_road_edge = _consumed["selected_road_corridor"]
assert _road_edge.from_step == "roads" and _road_edge.combine == "wire_translation.selected_road_network"
assert _road_edge.empty_commit == "road_corridors.NO_ROAD_CORRIDOR"
_tree_edge = _consumed["tree_zone_patches"]
assert _tree_edge.from_step == "trees" and _tree_edge.rehydrate == "wire_translation.rehydrate_tree_zones"
assert _tree_edge.empty_commit is None, "[] is the explicit empty answer for a list override -- what trees committed empty means"
assert _tree_edge.forward_as == "tree_zone_patches"
# THE TWO CACHE CLOSURES this branch added beyond its two named solar changes.
assert _consumed["farm_roads"].cache_path == "parcel_data.farm_roads"
assert _consumed["farmland_classifications"].cache_path == "parcel_data.farmland_classification"

# THE COMMIT CONTRACT: select-only PLUS placing, any number committed,
# two placed at most, NO crossings.
_contract = STRUCTURES.commit_contract
assert _contract.layers == (wire_translation.LAYER_SOLAR,) == ("solar_infrastructure",)
assert _contract.geometry_types == ("Polygon", "MultiPolygon", "Point"), "the first contract to accept a Point"
assert _contract.min_features == 0 and _contract.max_features is None
assert _contract.max_user_added == 2
assert _contract.rehydrate == "wire_translation.rehydrate_structure_sites"
assert _contract.internal_id_parameter == "site_ids"
assert _contract.internal_id_parser == "wire_translation.internal_structure_site_id"
assert _contract.requires_provenance is True
assert _contract.feature_group is None and _contract.group_check is None
assert _contract.crossings is step_registry.CROSSINGS_NOT_RECORDED
assert step_registry.records_crossings(_contract) is False
for _other in step_registry.STEP_REGISTRY.values():
    # fencing declares the same sentinel for its own reason (a fence line
    # has no acres to overlap -- see its entry); every other entry records.
    if _other.step_id not in ("structures", "fencing"):
        assert step_registry.records_crossings(_other.commit_contract) is True
    if _other.step_id != "structures":
        assert _other.commit_contract.max_user_added is None

# THE PLACEMENT DECLARATION.
_placement = STRUCTURES.placement
assert isinstance(_placement, step_registry.Placement)
assert _placement.input == "site" and _placement.shape == step_registry.INPUT_SHAPE_LON_LAT
assert _placement.score == "solar_suitability.score_placed_structure_site"
assert _placement.feature == "wire_translation.placed_structure_site_to_feature"
for _other in step_registry.STEP_REGISTRY.values():
    if _other.step_id != "structures":
        assert _other.placement is None

# CONSTANTS AGREE with the modules that own them.
assert solar_suitability.MAX_CANDIDATES == 3

# EVERY TARGET RESOLVES, and every forward_as is a real parameter.
import inspect

_signature = inspect.signature(STRUCTURES.resolve_generate())
for _c in STRUCTURES.consumes:
    if _c.forward_as:
        assert _c.forward_as in _signature.parameters, _c.forward_as
    for _target in (_c.rehydrate, _c.combine, _c.empty_commit):
        if _target:
            step_registry.resolve(_target)
for _target in (STRUCTURES.payload, _contract.rehydrate, _contract.internal_id_parser, _placement.score, _placement.feature):
    assert callable(step_registry.resolve(_target)), _target

# FAILURE LAYERS: canopy, mandatory, at production_zone_payload's own pair.
assert len(STRUCTURES.failure_layers) == 1
assert STRUCTURES.failure_layers[0].exception == "canopy_height_data.CanopyCoverageIncompleteError"
assert (STRUCTURES.failure_layers[0].layer, STRUCTURES.failure_layers[0].label) == production_zone_payload.LAYER_CANOPY

# THE EDGE HELPERS see the fifth entry.
assert step_registry.dependents_of("trees") == ("structures", "fencing")
assert step_registry.dependents_of("roads") == ("trees", "structures", "fencing")
assert step_registry.transitive_dependents("landform") == ("water", "roads", "trees", "structures", "fencing")
assert step_registry.transitive_dependents("structures") == ("fencing",)

# THE THREE NEW DECLARATIONS ARE VALIDATED.
import dataclasses


def _rejects(broken, why):
    original = step_registry.STEP_REGISTRY
    step_registry.STEP_REGISTRY = {broken.step_id: broken}
    try:
        step_registry.validate_registry()
    except step_registry.RegistryError:
        return
    finally:
        step_registry.STEP_REGISTRY = original
    raise AssertionError(f"validate_registry() accepted a malformed entry: {why}")


_contract_with = lambda **kw: dataclasses.replace(STRUCTURES, commit_contract=dataclasses.replace(_contract, **kw))
_placement_with = lambda **kw: dataclasses.replace(STRUCTURES, placement=dataclasses.replace(_placement, **kw))
_rejects(_contract_with(max_user_added=-1), "a negative placed cap")
_rejects(_contract_with(max_user_added=True), "a boolean placed cap")
_rejects(_contract_with(max_user_added=3, max_features=2), "a placed cap the commit ceiling makes unreachable")
_rejects(_placement_with(input=""), "a placement with no input name")
_rejects(_placement_with(shape="polygon"), "a placement input of an unknown shape")
_rejects(_placement_with(score=""), "a placement with no scorer")
_rejects(_placement_with(feature=""), "a placement with no wire builder")
_rejects(dataclasses.replace(STRUCTURES, placement="solar_suitability.score_placed_structure_site"), "a bare path instead of a Placement")
_rejects(
    dataclasses.replace(
        STRUCTURES,
        user_inputs=(step_registry.UserInput(name="site"),),
    ),
    "a placement input colliding with a user input",
)
step_registry.validate_registry()

print(
    f"1 [test 1]. REGISTRY: validate_registry() passes with FIVE entries "
    f"{step_registry.registered_steps()}. The structures entry consumes {len(STRUCTURES.consumes)} "
    f"values -- 6 off the cache (four shared, plus the farm_roads and farmland_classifications "
    f"closures), 4 off commits, one per upstream step, with empty_commit None (landform, trees: "
    f"[] is explicit), NO_WATER_ZONE (water) and NO_ROAD_CORRIDOR (roads) -- declares a contract "
    f"accepting Polygon/MultiPolygon/Point on layer {_contract.layers[0]!r}, min 0, no max, "
    f"max_user_added {_contract.max_user_added}, site_ids allocated through "
    f"internal_structure_site_id, crossings CROSSINGS_NOT_RECORDED, a Placement "
    f"(input {_placement.input!r}, {_placement.shape}) scoring through {_placement.score}, one canopy "
    f"failure layer, and 9 malformations of the three new declarations are each refused. "
    f"dependents_of('trees') == {step_registry.dependents_of('trees')}."
)


# --- 2 [test 2]. GENERATE, and MAX_CANDIDATES 5 -> 3 --------------------

with Harness() as h:
    s = Session()
    s.upstream()
    for step in ("landform", "water", "roads", "trees"):
        assert s.stored()["steps"][step]["status"] == design_document.STATUS_COMMITTED
    COMMITTED_TREES = s.committed("trees")
    assert COMMITTED_TREES, "the fixture must produce tree zones to commit"

    network_before = h.total_network_calls
    selfcomputes_before = h.solar_selfcomputes()
    payload = s.structures()
    structures_network_calls = h.total_network_calls - network_before
    selfcomputes = {k: v - selfcomputes_before[k] for k, v in h.solar_selfcomputes().items()}

    assert sorted(payload) == ["placement", "sites", "structure_sites", "summary"], sorted(payload)
    assert "crossing_grounds" not in payload
    assert h.identify_solar.call_count == 1
    validate_feature_collection(payload["structure_sites"])
    CANDIDATES = payload["structure_sites"]["features"]
    assert CANDIDATES, "the fixture must produce structure candidates, or every assertion below is vacuous"
    assert len(CANDIDATES) <= solar_suitability.MAX_CANDIDATES == 3

    # WHAT THE ENTRY POINT RECEIVED: every committed edge, by identity /
    # shape, and NOT None for any of them.
    call = h.identify_solar.call_args
    assert len(call.kwargs["production_areas"]) == len(s.stored()["steps"]["landform"]["features"]["features"])
    assert call.kwargs["selected_water_zone"] is not water_suitability.NO_WATER_ZONE
    assert "render_fill_polygon_utm" in call.kwargs["selected_water_zone"]
    assert call.kwargs["selected_road_corridor"] is not road_corridors.NO_ROAD_CORRIDOR
    assert call.kwargs["selected_road_corridor"]["cell_footprint_polygon_utm"] is not None
    assert call.kwargs["tree_zone_patches"] is not None
    assert [p["id"] for p in call.kwargs["tree_zone_patches"]] == [p["id"] for p in COMMITTED_TREES]
    assert call.kwargs["farm_roads"] == FIXTURE_ROADS and call.kwargs["farmland_classifications"] == FIXTURE_FARMLAND
    assert call.kwargs["canopy_height"] is not None and call.kwargs["dem"] is not None
    for absent in ("valleys", "hydric_floodplain_union", "floodplain_data_is_fallback", "anchor_lon_lat"):
        assert absent not in call.kwargs, absent

    # ZERO NETWORK, ZERO SELF-COMPUTE. Every one of the ten edges did its job.
    assert structures_network_calls == 0, structures_network_calls
    assert selfcomputes == {
        "identify_optimized_production_areas": 0,
        "identify_water_suitability": 0,
        "identify_road_corridor_candidates": 0,
        "identify_tree_zone_candidates": 0,
    }, selfcomputes
    assert h.solar_canopy_mask.call_count == 1 and h.canopy_refetch.call_count == 0

    # THE PER-FEATURE BLOCK: the measurement set, the four factors ON THE
    # WIRE, the road tier, and site_origin.
    for f in CANDIDATES:
        p = f["properties"]
        assert p["layer"] == wire_translation.LAYER_SOLAR
        assert f["geometry"]["type"] in ("Polygon", "MultiPolygon")
        assert f["id"].startswith("solar-candidate-")
        for key in (
            "rank", "suitability_score", "slope_score", "aspect_score", "shading_score",
            "production_proximity_score", "avg_slope_pct", "footprint_area_acres",
        ):
            assert isinstance(p[key], (int, float)), (key, p[key])
        assert p["suitability_score"] >= solar_suitability.MIN_SUITABILITY_SCORE * 100
        assert p["site_origin"] == "generated"
        assert p["road_proximity_source"] in ("selected_road_corridor", "real_mapped_road", "unavailable")
        assert "constraints_violated" not in p, "a generated candidate has no gate it failed"
        assert "prime_farmland_conflict" in p, "the farmland rows off the cache flagged every candidate"
        composite = 100.0 * (
            solar_suitability.SLOPE_SCORE_WEIGHT * p["slope_score"]
            + solar_suitability.ASPECT_SCORE_WEIGHT * p["aspect_score"]
            + solar_suitability.SHADING_SCORE_WEIGHT * p["shading_score"]
            + solar_suitability.PRODUCTION_PROXIMITY_SCORE_WEIGHT * p["production_proximity_score"]
        )
        assert abs(composite - p["suitability_score"]) < 0.3, (composite, p["suitability_score"])
    ranks = sorted(f["properties"]["rank"] for f in CANDIDATES)
    assert ranks == list(range(1, len(CANDIDATES) + 1))
    ROAD_SOURCE = CANDIDATES[0]["properties"]["road_proximity_source"]

    # THE TABULAR ROWS are a projection of the features, keyed by wire id.
    by_id = {f["id"]: f for f in CANDIDATES}
    assert len(payload["sites"]) == len(CANDIDATES)
    for row in payload["sites"]:
        assert row["feature_id"] in by_id
        for key in ("rank", "suitability_score", "slope_score", "road_proximity_source", "site_origin"):
            assert row[key] == by_id[row["feature_id"]]["properties"][key]

    # THE STEP-LEVEL BLOCK: narrative whole + the two caps + the flags +
    # the weights; the placement handshake.
    summary = payload["summary"]
    assert summary["max_candidates"] == 3 and summary["max_placed"] == 2
    assert summary["candidate_count"] == len(CANDIDATES) and summary["site_found"] is True
    assert summary["gates"]["road_proximity_source"] == ROAD_SOURCE
    assert summary["gates"]["tree_zone_exclusion_checked"] is True
    assert summary["run_flags"]["road_proximity_source"] == ROAD_SOURCE
    assert abs(sum(summary["factor_weights_pct"].values()) - 100.0) < 1e-9
    assert payload["placement"] == {"input": "site", "shape": "lon_lat", "max_placed": 2}

    # THE DOCUMENT: generated, no features. THE READ VERB returns the same
    # payload; a regenerate is idempotent, INCLUDING THE IDS.
    entry = s.stored()["steps"]["structures"]
    assert entry["status"] == design_document.STATUS_GENERATED and not entry.get("features")
    assert s.layers("structures") == payload
    again = s.structures()
    assert [f["id"] for f in again["structure_sites"]["features"]] == [f["id"] for f in CANDIDATES]

    GENERATE_SESSION = s
    GENERATE_PAYLOAD = payload
    RESULT = s.result()
    GENERATED = RESULT["all_scored_candidates"]

    # MAX_CANDIDATES 5 -> 3: the SAME run, cut at five, and what ranks 4
    # and 5 would have been.
    inputs = RESULT["run_inputs"]
    five = solar_suitability.find_candidate_solar_zones(
        inputs["dem"], inputs["production_areas"], inputs["water_zones"], inputs["road_geometries_utm"],
        inputs["boundary_polygon_utm"], canopy_mask_utm=inputs["canopy_mask_utm"],
        tree_zone_exclusion_polygon_utm=inputs["tree_zone_exclusion_polygon_utm"],
        max_candidates=5, **inputs["thresholds"],
    )
    everything = solar_suitability.find_candidate_solar_zones(
        inputs["dem"], inputs["production_areas"], inputs["water_zones"], inputs["road_geometries_utm"],
        inputs["boundary_polygon_utm"], canopy_mask_utm=inputs["canopy_mask_utm"],
        tree_zone_exclusion_polygon_utm=inputs["tree_zone_exclusion_polygon_utm"],
        max_candidates=10_000, **inputs["thresholds"],
    )
    assert _candidate_signature(five[:3]) == _candidate_signature(GENERATED), (
        "the surviving three are the SAME three, in the same order -- the ranking is unchanged"
    )
    DROPPED = five[3:]
    assert len(DROPPED) == min(2, max(len(everything) - 3, 0))
    survivor_scores = [c["suitability_score"] for c in GENERATED]
    dropped_scores = [c["suitability_score"] for c in DROPPED]
    assert all(d <= min(survivor_scores) for d in dropped_scores)

print(
    f"2 [test 2]. GENERATE: with landform ({len(s.stored()['steps']['landform']['features']['features'])} "
    f"zones), water (3 zones, as a union), roads (one network) and trees "
    f"({len(COMMITTED_TREES)} zones) committed, the structures generate produced "
    f"{len(CANDIDATES)} candidate(s) -- ranks {ranks}, scores {survivor_scores}, road tier "
    f"{ROAD_SOURCE!r} -- with {structures_network_calls} network calls and {selfcomputes} self-computes. "
    f"The entry point received all four committed values (none None), the two cache closures, and no "
    f"undeclared edge. The four factor scores, site_origin and road_proximity_source are on every "
    f"feature; summary carries the caps (3 generated, 2 placed), gates and weights.\n"
    f"   MAX_CANDIDATES 5 -> 3: the run clears {len(everything)} footprint(s) in all; at five it "
    f"would return {len(five)}, the first three IDENTICAL to the shipped three. DROPPED: "
    f"{[(c['rank'], c['suitability_score']) for c in DROPPED]} (rank, score) against the "
    f"surviving {survivor_scores} -- the dropped scores sit "
    f"{[round(min(survivor_scores) - d, 1) for d in dropped_scores]} point(s) below the third."
)


# --- 3 [test 3]. TREE OVERRIDE: the nested generator runs ZERO times ----
#
# identify_solar_candidate_zones():
#
#     if tree_zone_patches is None:
#         tree_zone_result = identify_tree_zone_candidates(...)   # regenerate
#         tree_zone_patches = tree_zone_result["patches"]
#
# Forward None and the nested generate re-ranks tree candidates the user
# never committed, excludes ground they never chose -- and fetches its
# Step-2 rows over the network on the way, because the nested call forwards
# no scoring_inputs. The registry's tree_zone_patches edge is the only thing
# between the two.

with Harness() as h:
    s = GENERATE_SESSION
    assert h.solar_tree_nested.call_count == 0
    payload = s.structures()
    assert h.solar_tree_nested.call_count == 0, (
        f"identify_tree_zone_candidates() ran {h.solar_tree_nested.call_count} time(s) from inside the "
        f"structures generate. The override was not believed: tree zones the user never committed were "
        f"regenerated and excluded."
    )
    assert h.tree_soil_fetch.call_count == 0 and h.tree_nhd_fetch.call_count == 0
    call = h.identify_solar.call_args
    assert [p["id"] for p in call.kwargs["tree_zone_patches"]] == [p["id"] for p in s.committed("trees")]

    # THE CONTROL: forward None in the override's place and the nested
    # generate DOES run -- once -- which is what makes the zero above a
    # measurement. It runs the REAL tree generator (wrapped, not a counter)
    # so section 4 can compare what it produced.
    assembled = s.assembled()
    control = dict(step_orchestrator.forwarded_arguments(STRUCTURES, assembled, {}))
    control["tree_zone_patches"] = None
    CONTROL_RESULT = solar_suitability.identify_solar_candidate_zones(**control)
    assert h.solar_tree_nested.call_count == 1, (
        "forwarding None must reach the nested tree generate -- if it does not, the zero above proves nothing"
    )
    # The nested call passes boundary_coordinates positionally; keep both halves.
    NESTED_CALL = (tuple(h.solar_tree_nested.call_args.args), dict(h.solar_tree_nested.call_args.kwargs))
    CONTROL_TREE_FETCHES = (
        h.tree_farmland_fetch.call_count + h.tree_soil_fetch.call_count
        + h.tree_soil_geometry_fetch.call_count + h.tree_nhd_fetch.call_count
    )
    assert CONTROL_TREE_FETCHES > 0, "the nested generate's Step-2 rows are fetched -- the override closes network calls too"
    assert CONTROL_RESULT["run_flags"]["tree_zone_exclusion_available"] is True
    # The other three self-computes stayed at zero on both paths.
    assert h.solar_production_selfcompute.call_count == 0
    assert h.solar_water_selfcompute.call_count == 0
    assert h.solar_road_selfcompute.call_count == 0

print(
    f"3 [test 3]. TREE OVERRIDE: with the committed tree zones supplied, identify_tree_zone_"
    f"candidates() ran 0 times inside the structures generate and the tree module fetched nothing. "
    f"THE CONTROL: the same forwarded arguments with None in the override's place ran it exactly "
    f"1 time -- and that nested run made {CONTROL_TREE_FETCHES} tree-module fetch call(s) for its "
    f"soil and stream rows (the nested call forwards no scoring_inputs). The zero is a measurement, "
    f"not an unreachable path."
)


# --- 4 [test 4]. THE TREE-SET COMPARISON --------------------------------
#
# Regenerated versus committed, on the reference parcel: count, ids, total
# area -- and whether solar's candidates change when the override is
# supplied. Two cases: the user committed EVERY generated tree zone, and
# the user committed a SUBSET.

with Harness() as h:
    # (a) WHOLE COMMIT -- section 2's session. The regenerated set is the
    # nested call's own answer, re-run here with exactly the kwargs solar
    # forwarded (so the production, water and road it looked at are the
    # committed ones).
    s = GENERATE_SESSION
    regenerated = tree_zone_candidates.identify_tree_zone_candidates(*NESTED_CALL[0], **NESTED_CALL[1])["patches"]
    committed = s.committed("trees")
    REGEN_WHOLE, COMMITTED_WHOLE = _patch_set(regenerated), _patch_set(committed)
    # Solar's candidates with the override (section 2's run) versus without
    # (section 3's control).
    WITH_OVERRIDE = _candidate_signature(RESULT["all_scored_candidates"])
    WITHOUT_OVERRIDE = _candidate_signature(CONTROL_RESULT["all_scored_candidates"])
    WHOLE_CANDIDATES_CHANGED = WITH_OVERRIDE != WITHOUT_OVERRIDE
    # The exclusion polygons themselves.
    whole_exclusion_with = RESULT["run_inputs"]["tree_zone_exclusion_polygon_utm"]
    whole_exclusion_without = CONTROL_RESULT["run_inputs"]["tree_zone_exclusion_polygon_utm"]
    WHOLE_EXCLUSION_SYMDIFF_ACRES = round(
        (whole_exclusion_with.symmetric_difference(whole_exclusion_without).area / SQUARE_METERS_PER_ACRE)
        if whole_exclusion_with is not None and whole_exclusion_without is not None
        else float("nan"),
        4,
    )

    # (b) PARTIAL COMMIT -- a fresh session committing only the FIRST
    # generated tree zone, so the committed set is a strict subset of what
    # a regenerate produces.
    s2 = Session()
    s2.upstream(tree_count=1)
    committed_partial = s2.committed("trees")
    assert len(committed_partial) == 1
    partial_payload = s2.structures()
    PARTIAL_RESULT = s2.result()
    assert h.solar_tree_nested.call_count == 0
    control2 = dict(step_orchestrator.forwarded_arguments(STRUCTURES, s2.assembled(), {}))
    control2["tree_zone_patches"] = None
    PARTIAL_CONTROL = solar_suitability.identify_solar_candidate_zones(**control2)
    assert h.solar_tree_nested.call_count == 1
    regenerated_partial = tree_zone_candidates.identify_tree_zone_candidates(
        *h.solar_tree_nested.call_args.args, **h.solar_tree_nested.call_args.kwargs
    )["patches"]
    REGEN_PARTIAL, COMMITTED_PARTIAL = _patch_set(regenerated_partial), _patch_set(committed_partial)
    assert REGEN_PARTIAL[0] > COMMITTED_PARTIAL[0], "a partial commit is a strict subset of the regenerate"
    PARTIAL_WITH = _candidate_signature(PARTIAL_RESULT["all_scored_candidates"])
    PARTIAL_WITHOUT = _candidate_signature(PARTIAL_CONTROL["all_scored_candidates"])
    PARTIAL_CANDIDATES_CHANGED = PARTIAL_WITH != PARTIAL_WITHOUT
    # The regenerate's exclusion strictly contains the committed one.
    ex_with = PARTIAL_RESULT["run_inputs"]["tree_zone_exclusion_polygon_utm"]
    ex_without = PARTIAL_CONTROL["run_inputs"]["tree_zone_exclusion_polygon_utm"]
    assert ex_with is not None and ex_without is not None
    assert ex_without.area > ex_with.area
    PARTIAL_EXTRA_EXCLUSION_ACRES = round((ex_without.area - ex_with.area) / SQUARE_METERS_PER_ACRE, 3)
    # Direct measurement of the behavioural difference, independent of
    # which footprints the road tier happened to sample: how many of the
    # run's ALL-clearing footprints under the committed exclusion are
    # inside the regenerated exclusion.
    inputs = PARTIAL_RESULT["run_inputs"]
    clearing = solar_suitability.find_candidate_solar_zones(
        inputs["dem"], inputs["production_areas"], inputs["water_zones"], inputs["road_geometries_utm"],
        inputs["boundary_polygon_utm"], canopy_mask_utm=inputs["canopy_mask_utm"],
        tree_zone_exclusion_polygon_utm=ex_with, max_candidates=10_000, **inputs["thresholds"],
    )
    PARTIAL_FOOTPRINTS_LOST = sum(1 for c in clearing if c["polygon_utm"].intersects(ex_without))

    # (c) TREES COMMITTED EMPTY -> [] -> "checked, nothing to stay clear of".
    s3 = Session()
    s3.upstream(tree_count=0)
    assert s3.stored()["steps"]["trees"]["features"]["features"] == []
    empty_payload = s3.structures()
    call = h.identify_solar.call_args
    assert call.kwargs["tree_zone_patches"] == [] and call.kwargs["tree_zone_patches"] is not None
    assert h.solar_tree_nested.call_count == 1, "an empty trees commit must not regenerate tree zones"
    EMPTY_RESULT = s3.result()
    assert EMPTY_RESULT["run_inputs"]["tree_zone_exclusion_polygon_utm"] is None
    assert EMPTY_RESULT["run_flags"]["tree_zone_exclusion_available"] is True, (
        "trees committed EMPTY is 'checked, nothing to exclude', never 'unavailable'"
    )
    assert empty_payload["summary"]["gates"]["tree_zone_exclusion_checked"] is True
    for f in empty_payload["structure_sites"]["features"]:
        assert "outside_tree_zone_candidate_buffer" in f["properties"]["constraints_satisfied"]

print(
    f"4 [test 4]. THE TREE-SET COMPARISON, reference parcel.\n"
    f"   WHOLE COMMIT: regenerated (count, ids, acres) = {REGEN_WHOLE}; committed = {COMMITTED_WHOLE}; "
    f"{'IDENTICAL' if REGEN_WHOLE == COMMITTED_WHOLE else 'DIFFERENT'}. Solar's candidates with the "
    f"override {WITH_OVERRIDE} vs without {WITHOUT_OVERRIDE}: "
    f"{'CHANGED' if WHOLE_CANDIDATES_CHANGED else 'unchanged'} (exclusion polygons differ by "
    f"{WHOLE_EXCLUSION_SYMDIFF_ACRES} acres).\n"
    f"   PARTIAL COMMIT (1 of {REGEN_PARTIAL[0]} zones): regenerated = {REGEN_PARTIAL}; committed = "
    f"{COMMITTED_PARTIAL}. The regenerate excludes {PARTIAL_EXTRA_EXCLUSION_ACRES} more acres than the "
    f"commit; {PARTIAL_FOOTPRINTS_LOST} of the {len(clearing)} footprint(s) clearing every gate under "
    f"the committed exclusion sit on ground the regenerate would have excluded. Solar's candidates: "
    f"with {PARTIAL_WITH} vs without {PARTIAL_WITHOUT}: "
    f"{'CHANGED -- a real behavioural change' if PARTIAL_CANDIDATES_CHANGED else 'unchanged on this fixture'}.\n"
    f"   TREES EMPTY: [] reaches the entry point, no exclusion polygon, tree_zone_exclusion_available "
    f"True, the nested generate ran 0 times, {len(empty_payload['structure_sites']['features'])} "
    f"candidate(s)."
)


# --- 5 [test 5]. THE FOUR RUN-LEVEL FLAGS ARE ON THE RESULT --------------

assert set(RESULT["run_flags"]) == {
    "shading_is_rough_proxy", "road_proximity_source", "tree_zone_exclusion_available",
    "spacing_meters", "max_structure_footprint_acres",
}, sorted(RESULT["run_flags"])
FLAGS = RESULT["run_flags"]
assert FLAGS["shading_is_rough_proxy"] is True
assert FLAGS["road_proximity_source"] == ROAD_SOURCE
assert FLAGS["tree_zone_exclusion_available"] is True
assert FLAGS["spacing_meters"] == solar_suitability.CANDIDATE_POINT_SPACING_METERS
assert FLAGS["max_structure_footprint_acres"] == solar_suitability.MAX_STRUCTURE_FOOTPRINT_ACRES
import json

assert json.loads(json.dumps(FLAGS)) == FLAGS

# THE WIRE FORM IS REPRODUCIBLE FROM THE RESULT, byte for byte -- which is
# exactly what render_layout_map.fetch_layout_layers() could not do from
# context.selected_structure_site before.
assert solar_suitability.candidates_to_geojson(GENERATED, **FLAGS) == RESULT["zones_geojson"]
rebuilt = wire_translation.selected_structure_site_to_feature_collection(
    RESULT["selected_structure_site"], **FLAGS
)["features"][0]
assert rebuilt == RESULT["zones_geojson"]["features"][0], (
    "the rank-1 Feature rebuilt from the selected site under the run's flags must be the run's own"
)
# ...and WITHOUT the flags it is not: the default notes describe a
# different run whenever the tier is not 'unavailable'.
default_rebuilt = wire_translation.selected_structure_site_to_feature_collection(RESULT["selected_structure_site"])
if ROAD_SOURCE != "unavailable":
    assert default_rebuilt["features"][0] != RESULT["zones_geojson"]["features"][0]
    assert default_rebuilt["features"][0]["properties"]["road_proximity_source"] == "unavailable"

print(
    f"5 [test 5]. THE FOUR RUN-LEVEL FLAGS on the result: {FLAGS}. candidates_to_geojson("
    f"all_scored_candidates, **run_flags) reproduces zones_geojson byte for byte, and the rank-1 Feature "
    f"rebuilt from selected_structure_site under the flags equals feature 0 -- so "
    f"fetch_layout_layers() could now read structure_site from context.selected_structure_site plus "
    f"solar's run_flags instead of from zones_geojson (not changed on this branch). Rebuilt WITHOUT "
    f"the flags it carries road_proximity_source "
    f"{default_rebuilt['features'][0]['properties']['road_proximity_source']!r}: the wrong run."
)


# --- 6 [test 6]. NO_ROAD_CORRIDOR STILL NORMALIZES; the source on the payload
# --- 7 [test 7]. ROADS COMMITTED EMPTY -> TIER 2, off the cache -----------

with Harness() as h:
    s = Session()
    s.upstream(access_point=None)
    assert s.stored()["steps"]["roads"]["features"]["features"] == []

    warm = s.assembled()["selected_road_corridor"]
    assert warm is road_corridors.NO_ROAD_CORRIDOR, f"warm read: {warm!r}"
    s.cache.discard(s.id)
    cold = s.assembled()["selected_road_corridor"]
    assert cold is road_corridors.NO_ROAD_CORRIDOR, f"cold read: {cold!r}"

    payload = s.structures()
    call = h.identify_solar.call_args
    assert call.kwargs["selected_road_corridor"] is road_corridors.NO_ROAD_CORRIDOR, "received by identity"
    assert h.solar_road_selfcompute.call_count == 0, (
        f"identify_road_corridor_candidates() ran {h.solar_road_selfcompute.call_count} time(s) inside "
        f"the structures generate: the sentinel was not normalized and a road the user decided against "
        f"was routed."
    )
    # TIER 2 answered, off the cache: zero road fetches, the payload says so
    # in THREE places.
    TIER2_RESULT = s.result()
    assert TIER2_RESULT["run_flags"]["road_proximity_source"] == "real_mapped_road"
    assert h.solar_roads_fetch.call_count == 0, "farm_roads off the cache must close Tier 2's fetch"
    assert call.kwargs["farm_roads"] == FIXTURE_ROADS
    assert payload["summary"]["gates"]["road_proximity_source"] == "real_mapped_road"
    assert payload["summary"]["run_flags"]["road_proximity_source"] == "real_mapped_road"
    TIER2_FEATURES = payload["structure_sites"]["features"]
    assert TIER2_FEATURES, "Tier 2 must yield candidates on this fixture"
    for f in TIER2_FEATURES:
        assert f["properties"]["road_proximity_source"] == "real_mapped_road"
        assert f["properties"]["distance_to_road_ft"] is not None
        assert f["properties"]["distance_to_road_ft"] <= solar_suitability.ROAD_PROXIMITY_BUFFER_METERS / 0.3048 + 0.1
        assert "within_road_proximity_buffer" in f["properties"]["constraints_satisfied"]
        assert "real mapped road data" in f["properties"]["confidence_notes"]
    # The network-free claim holds on the Tier 2 path too.
    assert h.solar_farmland_fetch.call_count == 0

    # THE CONTROL for the sentinel: None in its place routes a network.
    control = dict(step_orchestrator.forwarded_arguments(STRUCTURES, s.assembled(), {}))
    control["selected_road_corridor"] = None
    solar_suitability.identify_solar_candidate_zones(**control)
    assert h.solar_road_selfcompute.call_count == 1, "None must reach the road self-compute"
    # THE CONTROL for the closure: None farm_roads fetches.
    control = dict(step_orchestrator.forwarded_arguments(STRUCTURES, s.assembled(), {}))
    control["farm_roads"] = None
    solar_suitability.identify_solar_candidate_zones(**control)
    assert h.solar_roads_fetch.call_count == 1, "None must reach Tier 2's fetch"
    control["farm_roads"] = FIXTURE_ROADS
    control["farmland_classifications"] = None
    solar_suitability.identify_solar_candidate_zones(**control)
    assert h.solar_farmland_fetch.call_count == 1, "None must reach the SSURGO farmland fetch"

    # WATER EMPTY -> NO_WATER_ZONE, the sentinel's third use, in passing.
    s4 = Session()
    s4.upstream(water_zone_count=0)
    s4.structures()
    assert h.identify_solar.call_args.kwargs["selected_water_zone"] is water_suitability.NO_WATER_ZONE
    assert h.solar_water_selfcompute.call_count == 0
    assert s4.result()["run_inputs"]["water_zones"] == []
    assert s4.layers("structures")["summary"]["gates"]["water_zone_excluded"] is False

print(
    f"6 [test 6]. NO_ROAD_CORRIDOR: an empty roads commit resolves to the sentinel on a warm and a "
    f"cold read, reaches identify_solar_candidate_zones() by identity, and its normalization at the "
    f"entry point (the trees branch's line) still holds -- the road self-compute ran 0 times (1 with "
    f"None in its place). road_proximity_source reaches the payload on every feature, in "
    f"summary.gates and in summary.run_flags.\n"
    f"7 [test 7]. ROADS EMPTY -> TIER 2: {len(TIER2_FEATURES)} candidate(s) against real mapped roads "
    f"(road_proximity_source='real_mapped_road', within {solar_suitability.ROAD_PROXIMITY_BUFFER_METERS:.0f} m, "
    f"distances {[f['properties']['distance_to_road_ft'] for f in TIER2_FEATURES]} ft) with the road rows "
    f"OFF THE CACHE: 0 farm-road fetches (1 with farm_roads=None), 0 SSURGO farmland fetches (1 with "
    f"farmland_classifications=None). Water committed empty reaches solar as NO_WATER_ZONE, 0 water "
    f"self-computes, water_zone_excluded False."
)


# --- 8 [test 8]. A PLACED SITE carries the full measurement set ---------

GENERATED_WIRE_KEYS = set(GENERATE_PAYLOAD["structure_sites"]["features"][0]["properties"])
PRODUCER_FIELDS = set(GENERATED[0])
CONSUMER_READ_FIELDS = {"id", "polygon_utm", "geometry_wgs84", "footprint_area_acres", "site_origin"}

with Harness() as h:
    s = GENERATE_SESSION
    context = s.context()
    result = s.result()

    # A point the user might well choose: near the rank-1 candidate's own
    # pad, off the 25 m grid, on ground that clears every gate -- FOUND BY
    # MEASUREMENT (the scorer's own, direct) rather than by a fixed nudge,
    # because on this fixture the pads line up beside the water buffer and
    # a blind 12 m step east lands in it. The offsets are not multiples of
    # the grid spacing, so no sample fell on the spot.
    top = result["selected_structure_site"]
    inputs = result["run_inputs"]
    nudged = None
    for dx, dy in ((12.0, 0.0), (-12.0, 0.0), (0.0, 12.0), (0.0, -12.0), (7.0, 7.0), (-7.0, -7.0),
                   (18.0, 4.0), (-18.0, 4.0), (4.0, 18.0), (4.0, -18.0), (0.0, 37.0), (0.0, -37.0)):
        probe = Point(top["polygon_utm"].centroid.x + dx, top["polygon_utm"].centroid.y + dy)
        if not BOUNDARY_POLYGON_UTM.contains(probe):
            continue
        measured = solar_suitability.measure_structure_site(
            probe.x, probe.y, inputs["dem"], inputs["production_areas"], inputs["water_zones"],
            inputs["road_geometries_utm"], inputs["boundary_polygon_utm"],
            canopy_mask_utm=inputs["canopy_mask_utm"],
            tree_zone_exclusion_polygon_utm=inputs["tree_zone_exclusion_polygon_utm"],
            **inputs["thresholds"],
        )
        if measured is not None and all(measured["constraints"].values()):
            nudged, NUDGE = probe, (dx, dy)
            break
    assert nudged is not None, "no gate-clearing spot within 40 m of the rank-1 pad on this fixture"
    PLACED_A = _lon_lat(nudged)
    network_before = h.total_network_calls
    feature = s.score(PLACED_A)
    assert h.total_network_calls == network_before
    assert h.identify_solar.call_count == 0, "scoring reads the cached run; it does not regenerate"

    # THE FEATURE: a Point, a placed id, the generated property block PLUS
    # the placed extras, nothing missing.
    assert feature["type"] == "Feature" and feature["geometry"]["type"] == "Point"
    assert feature["id"].startswith("structure-site-placed-")
    assert wire_translation.internal_structure_site_id(feature["id"]) is None, "a placed id does not parse as a rank"
    p = feature["properties"]
    assert p["layer"] == wire_translation.LAYER_SOLAR
    assert GENERATED_WIRE_KEYS <= set(p), sorted(GENERATED_WIRE_KEYS - set(p))
    assert set(p) - GENERATED_WIRE_KEYS == {"constraints_violated", "placed_lon_lat", "footprint_wgs84"}, (
        sorted(set(p) - GENERATED_WIRE_KEYS)
    )
    assert p["site_origin"] == "user_placed"
    assert p["placed_lon_lat"] == [PLACED_A[0], PLACED_A[1]]
    assert feature["geometry"]["coordinates"] == [PLACED_A[0], PLACED_A[1]]
    assert isinstance(p["suitability_score"], float) and 0 <= p["suitability_score"] <= 100
    for key in ("slope_score", "aspect_score", "shading_score", "production_proximity_score"):
        assert 0.0 <= p[key] <= 1.0, (key, p[key])
    composite = 100.0 * (
        solar_suitability.SLOPE_SCORE_WEIGHT * p["slope_score"]
        + solar_suitability.ASPECT_SCORE_WEIGHT * p["aspect_score"]
        + solar_suitability.SHADING_SCORE_WEIGHT * p["shading_score"]
        + solar_suitability.PRODUCTION_PROXIMITY_SCORE_WEIGHT * p["production_proximity_score"]
    )
    assert abs(composite - p["suitability_score"]) < 0.3
    assert p["road_proximity_source"] == ROAD_SOURCE
    assert p["prime_farmland_conflict"] == GENERATED[0]["prime_farmland_conflict"], "parcel-level, inherited from the run"
    assert "THIS SITE WAS PLACED BY THE USER" in p["confidence_notes"]
    assert p["confidence_notes"].startswith(GENERATE_PAYLOAD["structure_sites"]["features"][0]["properties"]["confidence_notes"]), (
        "the run's own notes, then the placed-site sentence"
    )
    # The pad: the same fixed footprint, clipped, measured -- and on the
    # wire as a property beside the point.
    pad = _utm(p["footprint_wgs84"])
    assert pad.geom_type == "Polygon" and pad.contains(nudged)
    assert abs(pad.area / SQUARE_METERS_PER_ACRE - p["footprint_area_acres"]) < 1e-3
    assert abs(p["footprint_area_acres"] - solar_suitability.MAX_STRUCTURE_FOOTPRINT_ACRES) < 1e-3, "an interior pad is the full cap"
    # A good spot beside the winner clears every gate...
    assert p["constraints_violated"] == [], p["constraints_violated"]
    assert set(p["constraints_satisfied"]) == set(GENERATED_WIRE := GENERATE_PAYLOAD["structure_sites"]["features"][0]["properties"]["constraints_satisfied"]), (
        p["constraints_satisfied"], GENERATED_WIRE,
    )
    # ...and is RANKED against the generated three: 1 + the number scoring
    # strictly higher.
    expected_rank = 1 + sum(1 for c in GENERATED if c["suitability_score"] > p["suitability_score"])
    assert p["rank"] == expected_rank, (p["rank"], expected_rank)
    PLACED_A_FEATURE = feature

    # THE INTERNAL DICT the scorer built: every producer field, plus the
    # placed extras -- "the same fields a generated candidate does".
    site = solar_suitability.score_placed_structure_site(PLACED_A, result)
    assert PRODUCER_FIELDS <= set(site), sorted(PRODUCER_FIELDS - set(site))
    assert set(site) - PRODUCER_FIELDS == {"constraints", "site_origin", "placed_lon_lat", "point_utm"}, (
        sorted(set(site) - PRODUCER_FIELDS)
    )
    assert site["rank"] == expected_rank and site["site_origin"] == "user_placed"

    # A SITE ON BAD GROUND IS SCORED AND TOLD WHICH GATES IT FAILED -- the
    # divergence from trees, measured. The fixture's canopy block.
    from raster_grid import pixel_center_xy

    canopy_xy = pixel_center_xy(context.dem, (CANOPY_ROWS[0] + CANOPY_ROWS[1]) // 2, (CANOPY_COLS[0] + CANOPY_COLS[1]) // 2)
    canopy_point = Point(canopy_xy)
    assert BOUNDARY_POLYGON_UTM.contains(canopy_point), "the fixture's canopy block is on-parcel"
    PLACED_CANOPY = _lon_lat(canopy_point)
    bad = s.score(PLACED_CANOPY)
    bp = bad["properties"]
    assert "outside_existing_canopy" in bp["constraints_violated"], bp["constraints_violated"]
    assert isinstance(bp["suitability_score"], float), "scored, not refused"
    assert set(bp["constraints_satisfied"]) | set(bp["constraints_violated"]) == set(p["constraints_satisfied"]), (
        "the same gates, every one an outcome"
    )
    assert not set(bp["constraints_satisfied"]) & set(bp["constraints_violated"])
    BAD_VIOLATED = bp["constraints_violated"]

    # ...and a site on the committed water ground, or inside the committed
    # tree zones, likewise.
    water_union = s.assembled()["selected_water_zone"]["render_fill_polygon_utm"]
    water_point = water_union.representative_point()
    wet = s.score(_lon_lat(water_point))
    assert "outside_water_candidate_zone" in wet["properties"]["constraints_violated"]
    tree_point = s.committed("trees")[0]["render_fill_polygon_utm"].representative_point()
    treed = s.score(_lon_lat(tree_point))
    assert "outside_tree_zone_candidate_buffer" in treed["properties"]["constraints_violated"]

    # OFF THE PARCEL IS THE ONE HARD GATE.
    outside = Point(BOUNDARY_POLYGON_UTM.bounds[0] - 50.0, BOUNDARY_POLYGON_UTM.bounds[1] - 50.0)
    try:
        s.score(_lon_lat(outside))
    except step_orchestrator.StepOrchestrationError as exc:
        assert "outside the parcel boundary" in str(exc), str(exc)
    else:
        raise AssertionError("a point off the parcel must be refused")
    # A bad shape is refused before anything is measured; an unknown key too.
    for bad_params in ({"site": [40.6, -79.9, 0.0]}, {"site": "here"}, {"point": list(PLACED_A)}, {}):
        try:
            step_orchestrator.score_placed_feature(
                s.id, "structures", s.store, params=bad_params, fetch_cache=s.fetch_cache, cache=s.cache
            )
        except step_orchestrator.StepOrchestrationError:
            pass
        else:
            raise AssertionError(f"params {bad_params!r} must be refused")
    # A step with no placement is refused by declaration.
    try:
        step_orchestrator.score_placed_feature(
            s.id, "trees", s.store, params={"site": list(PLACED_A)}, fetch_cache=s.fetch_cache, cache=s.cache
        )
    except step_orchestrator.StepOrchestrationError as exc:
        assert "declares no placement" in str(exc)
    else:
        raise AssertionError("trees declares no placement and must refuse")

    # ROUND TRIP through the rehydrator: the placed Feature comes home as a
    # site dict carrying the measurement set -- the divergence from trees'
    # unscored drawn zone -- with the pad derived from the point.
    home = wire_translation.rehydrate_structure_site(PLACED_A_FEATURE, context.dem, site_id=77)
    assert home["id"] == 77 and home["site_origin"] == "user_placed"
    assert home["polygon_utm"].geom_type == "Polygon" and home["polygon_utm"].contains(home["point_utm"])
    assert abs(home["point_utm"].x - nudged.x) < 1e-6 and abs(home["point_utm"].y - nudged.y) < 1e-6
    assert home["placed_lon_lat"] == [PLACED_A[0], PLACED_A[1]]
    for field in ("suitability_score", "slope_score", "aspect_score", "shading_score", "production_proximity_score",
                  "avg_slope_pct", "aspect_deg", "aspect_label", "production_zone_relationship", "rank"):
        assert home[field] == site[field], (field, home[field], site[field])
    # The three WIRE-level fields -- the run's tier and the two constraint
    # lists -- are inherited from the Feature; the internal dict carries the
    # outcomes as `constraints` and the tier lives on the run.
    assert home["road_proximity_source"] == ROAD_SOURCE
    assert home["constraints_satisfied"] == p["constraints_satisfied"]
    assert home["constraints_violated"] == p["constraints_violated"] == []
    assert set(home["constraints_satisfied"]) == {name for name, ok in site["constraints"].items() if ok}
    for field in ("distance_to_road_m", "distance_to_production_zone_m", "distance_to_water_zone_m"):
        if site[field] is None:
            assert home[field] is None
        else:
            assert abs(home[field] - site[field]) <= 0.05, (field, home[field], site[field])
    # An interior pad rehydrates to the same area it was scored over.
    assert abs(home["footprint_area_acres"] - site["footprint_area_acres"]) < 1e-3
    # A placed feature without an allocated id is refused, never invented.
    try:
        wire_translation.rehydrate_structure_site(PLACED_A_FEATURE, context.dem)
    except wire_translation.InboundGeometryError as exc:
        assert "site_id=" in str(exc)
    else:
        raise AssertionError("a placed site with no pipeline id must be refused without site_id=")

    # A GENERATED candidate round-trips too: the same tolerance the tree
    # and production branches justified (1e-9 relative symmetric
    # difference on the reprojected polygon), everything else exact except
    # the three distances through feet.
    for original, wire in zip(GENERATED, GENERATE_PAYLOAD["structure_sites"]["features"]):
        back = wire_translation.rehydrate_structure_site(wire, context.dem)
        assert back["id"] == original["rank"] and back["site_origin"] == "generated"
        assert back["polygon_utm"].symmetric_difference(original["polygon_utm"]).area / original["polygon_utm"].area < 1e-9
        for field in ("rank", "suitability_score", "slope_score", "aspect_score", "shading_score",
                      "production_proximity_score", "avg_slope_pct", "aspect_deg", "aspect_label",
                      "production_zone_relationship", "prime_farmland_conflict", "prime_farmland_note"):
            assert back[field] == original[field], (field, back[field], original[field])
        for field in ("distance_to_road_m", "distance_to_production_zone_m", "distance_to_water_zone_m"):
            if original[field] is None:
                assert back[field] is None
            else:
                assert abs(back[field] - original[field]) <= 0.05, (field, back[field], original[field])
        assert abs(back["footprint_area_acres"] - original["footprint_area_acres"]) < 1e-3
        assert "point_utm" not in back and "constraints_violated" not in back

    # WHICH MEASUREMENTS ARE COMPUTABLE AT A POINT, AND WHICH ARE AREAL.
    # Every one of them is taken over the PAD, not the point -- slope,
    # aspect and shading are means over the pad's DEM cells, the three
    # distances are from the pad's polygon, footprint_area_acres IS the
    # pad -- so all are computable at a placed point because the point
    # DEFINES a pad. The two that are not measurements of the spot at all:
    # `rank` (a comparison against the run's shortlist) and the
    # prime-farmland flag (parcel-level SSURGO, inherited). Asserted by
    # what the scorer needs: a pad with cells.
    unmeasurable = solar_suitability.measure_structure_site(
        BOUNDARY_POLYGON_UTM.bounds[0] - 500.0, BOUNDARY_POLYGON_UTM.bounds[1] - 500.0,
        context.dem, [], [], None, BOUNDARY_POLYGON_UTM,
    )
    assert unmeasurable is None, "no pad on the parcel -> no measurement, not a zeroed one"

print(
    f"8 [test 8]. PLACED SITE at {PLACED_A} ({NUDGE[0]:+.0f} m E, {NUDGE[1]:+.0f} m N of the rank-1 pad): scored "
    f"{p['suitability_score']}/100 (factors slope {p['slope_score']}, aspect {p['aspect_score']}, "
    f"shading {p['shading_score']}, production proximity {p['production_proximity_score']}), "
    f"{p['avg_slope_pct']}% slope, {p['aspect']}-facing, {p['footprint_area_acres']} ac pad, "
    f"{p['distance_to_road_ft']} ft to road ({p['road_proximity_source']}), "
    f"{p['production_zone_relationship']} production, would rank {p['rank']} against the generated "
    f"{[c['suitability_score'] for c in GENERATED]}; every generated wire property present plus "
    f"constraints_violated (empty), placed_lon_lat and footprint_wgs84. The full producer field set "
    f"{len(PRODUCER_FIELDS)} fields is on the internal dict. A site on the canopy block is SCORED "
    f"({bp['suitability_score']}/100) and told it violates {BAD_VIOLATED}; sites on the committed water "
    f"ground and inside a committed tree zone likewise. Off the parcel is refused. Rehydration brings "
    f"the placed Feature home with the measurement set intact (distances within 0.05 m through feet), "
    f"and every generated candidate round-trips at 1e-9."
)


# --- 9 [test 9]. NO NETWORK during placed-site scoring --------------------
# A counter that also RAISES, not a stopwatch -- around the WHOLE verb.

_connection_attempts = []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _forbidden(address, *args, **kwargs):
    _connection_attempts.append(address)
    raise AssertionError(f"placed-site scoring opened a network connection to {address!r}")


socket.socket.connect = lambda self, address: _forbidden(address)
socket.socket.connect_ex = lambda self, address: _forbidden(address)
socket.create_connection = _forbidden
try:
    with Harness() as h:
        s = GENERATE_SESSION
        before = h.total_network_calls
        _guarded = [s.score(PLACED_A), s.score(PLACED_CANOPY)]
        _guarded.append(solar_suitability.score_placed_structure_site(PLACED_A, s.result()))
        _guarded.append(wire_translation.rehydrate_structure_site(_guarded[0], s.context().dem, site_id=9))
        _guarded.extend(wire_translation.rehydrate_structure_sites(GENERATE_PAYLOAD["structure_sites"], s.context().dem))
        assert h.total_network_calls == before
finally:
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    socket.create_connection = _real_create_connection

assert _connection_attempts == [], _connection_attempts

print(
    f"9 [test 9]. NO NETWORK: {len(_guarded)} placed-site scorings and rehydrations (through the verb, "
    f"the scorer directly, a placed feature home, and every generated feature home) under a socket "
    f"counter that raises -- {len(_connection_attempts)} connection attempts, 0 mocked fetches."
)


# --- 10 [test 10]. THE PLACED CAP OF 2 IS ENFORCED SERVER-SIDE -----------

with Harness() as h:
    s = GENERATE_SESSION
    result = s.result()
    top = result["selected_structure_site"]
    # Three distinct placed sites, all on good ground.
    placed = []
    for dx, dy in ((12.0, 0.0), (-12.0, 30.0), (30.0, -30.0)):
        point = Point(top["polygon_utm"].centroid.x + dx, top["polygon_utm"].centroid.y + dy)
        if not BOUNDARY_POLYGON_UTM.contains(point):
            point = BOUNDARY_POLYGON_UTM.representative_point()
        placed.append(s.score(_lon_lat(point)))
    assert len({f["id"] for f in placed}) == 3
    selected = list(CANDIDATES)

    three = selected + placed
    provenance = {f["id"]: "generated" for f in selected}
    provenance.update({f["id"]: "user_added" for f in placed})
    try:
        s.commit("structures", three, provenance)
    except commit_validation.CommitRejectedError as exc:
        codes = [r.code for r in exc.rejections]
        assert codes == [commit_validation.REJECT_TOO_MANY_USER_ADDED], codes
        assert exc.rejections[0].feature_id is None
        assert "at most 2 user-added" in exc.rejections[0].reason
        CAP_REJECTION = exc.as_payload()
    else:
        raise AssertionError("a third placed site must be refused")
    assert s.stored()["steps"]["structures"]["status"] == design_document.STATUS_GENERATED, "nothing was written"

    # The cap counts PLACED sites, not the commit: three generated plus
    # two placed is fine, and so is one placed wearing a generated id but
    # declared user_added -- it still counts as placed.
    two = selected + placed[:2]
    provenance = {f["id"]: "generated" for f in selected}
    provenance.update({f["id"]: "user_added" for f in placed[:2]})
    document = s.commit("structures", two, provenance)
    assert document["steps"]["structures"]["status"] == design_document.STATUS_COMMITTED
    assert len(document["steps"]["structures"]["features"]["features"]) == len(two)
    s.reopen("structures")
    # The cap is the registry's declaration, read by the gate -- loosen the
    # declaration and the same commit passes; the UI has no say.
    loosened = dataclasses.replace(STRUCTURES, commit_contract=dataclasses.replace(_contract, max_user_added=3))
    commit_validation.check_commit(
        loosened, {"type": "FeatureCollection", "features": three},
        {**{f["id"]: "generated" for f in selected}, **{f["id"]: "user_added" for f in placed}},
        s.context().dem, s.context().boundary_polygon_utm,
    )
    PLACED_THREE = placed

print(
    f"10 [test 10]. THE PLACED CAP: committing {len(selected)} selected + 3 placed sites is rejected "
    f"server-side with one collection-level {CAP_REJECTION['rejections'][0]['code']!r} rejection "
    f"({CAP_REJECTION['rejections'][0]['reason']!r}) and nothing written; {len(selected)} selected + 2 "
    f"placed commits. With the contract's max_user_added loosened to 3 the same three pass the gate: "
    f"the cap is the declaration, not the UI."
)


# --- 11 [test 11]. SEVERAL SITES COMMIT; NONE COMMITS; no crossings ------

with Harness() as h:
    s = GENERATE_SESSION
    assert s.stored()["steps"]["structures"]["status"] == design_document.STATUS_GENERATED
    selected = list(CANDIDATES)
    placed = PLACED_THREE[:2]
    features = selected + placed
    provenance = {f["id"]: "generated" for f in selected}
    provenance.update({f["id"]: "user_added" for f in placed})

    # THE INTERNAL IDS: a selected candidate keeps its rank; a placed site
    # is allocated above every rank in the commit.
    ids = commit_validation.internal_ids_for(features, provenance, wire_translation.internal_structure_site_id)
    ranks = [f["properties"]["rank"] for f in selected]
    assert ids == ranks + [max(ranks) + 1, max(ranks) + 2], ids

    document = s.commit("structures", features, provenance)
    stored = document["steps"]["structures"]["features"]["features"]
    assert [f["id"] for f in stored] == [f["id"] for f in features]
    # NO CROSSINGS, DECLARED ABSENT: not an empty list, no key at all.
    for f in stored:
        assert "exclusion_crossings" not in f["properties"], f["id"]
    # Every other committed step in this session carries the key.
    for step in ("landform", "water", "roads", "trees"):
        for f in s.stored()["steps"][step]["features"]["features"]:
            assert "exclusion_crossings" in f["properties"], (step, f["id"])
    # The placed features are stored AS SCORED: the measurement set is on
    # the document, which is the divergence from trees made durable.
    stored_placed = [f for f in stored if f["id"] in {p["id"] for p in placed}]
    for f in stored_placed:
        assert f["geometry"]["type"] == "Point"
        assert isinstance(f["properties"]["suitability_score"], float)
        assert f["properties"]["site_origin"] == "user_placed"

    # WARM AND COLD AGREE, and the placed sites come home scored.
    warm_value = s.context().step_committed["structures"]["value"]
    assert [site["id"] for site in warm_value] == ids
    assert set(warm_value[-1]) >= CONSUMER_READ_FIELDS | {"suitability_score", "rank", "point_utm"}
    s.cache.discard(s.id)
    cold_value = s.committed("structures")
    assert [site["id"] for site in cold_value] == ids
    for warm, cold in zip(warm_value, cold_value):
        assert warm["suitability_score"] == cold["suitability_score"]
        assert warm["polygon_utm"].symmetric_difference(cold["polygon_utm"]).area < 1e-6
    assert h.rehydrate_sites.call_count >= 1
    # The committed value is what a downstream consumer would take: a list
    # of sites each with a polygon pad -- including the placed ones.
    for site in cold_value:
        assert site["polygon_utm"].geom_type in ("Polygon", "MultiPolygon") and site["polygon_utm"].area > 0

    # REOPEN restores the placed sites as user_added and the selection by id.
    s.reopen("structures")
    restored = s.context().step_restored["structures"]
    assert restored["selected_feature_ids"] == [f["id"] for f in selected]
    assert [f["id"] for f in restored["user_added"]["features"]] == [f["id"] for f in placed]
    assert restored["missing_feature_ids"] == []
    assert s.stored()["steps"]["structures"]["status"] == design_document.STATUS_GENERATED

    # COMMITTING NONE succeeds: a decision, status committed, [] downstream.
    document = s.commit("structures", [], {})
    assert document["steps"]["structures"]["status"] == design_document.STATUS_COMMITTED
    assert document["steps"]["structures"]["features"]["features"] == []
    assert s.committed("structures") == []
    s.reopen("structures")

    # ONE placed site alone commits too, and a generated candidate alone.
    document = s.commit("structures", [placed[0]], {placed[0]["id"]: "user_added"})
    assert len(document["steps"]["structures"]["features"]["features"]) == 1
    s.reopen("structures")
    document = s.commit("structures", [selected[0]], {selected[0]["id"]: "generated"})
    assert len(document["steps"]["structures"]["features"]["features"]) == 1
    s.reopen("structures")

    # THE HTTP ROUTE maps the verb: 200 with the feature; 400 for a bad
    # point; 409 for a step with no run; 400 for a step with no placement.
    deps = session_api.Dependencies(store=s.store, fetch_cache=s.fetch_cache, cache=s.cache, runner=s.runner)
    http = session_api.create_app(deps).test_client()
    ok = http.post(f"/api/sessions/{s.id}/steps/structures/score", json={"params": {"site": list(PLACED_A)}})
    assert ok.status_code == 200, ok.get_json()
    assert ok.get_json()["feature"]["id"] == PLACED_A_FEATURE["id"]
    assert ok.get_json()["feature"]["properties"]["suitability_score"] == PLACED_A_FEATURE["properties"]["suitability_score"]
    off = http.post(f"/api/sessions/{s.id}/steps/structures/score", json={"params": {"site": [-79.99, 40.60]}})
    assert off.status_code == 400 and "outside the parcel" in off.get_json()["error"], off.get_json()
    no_placement = http.post(f"/api/sessions/{s.id}/steps/trees/score", json={"params": {"site": list(PLACED_A)}})
    assert no_placement.status_code == 400 and "declares no placement" in no_placement.get_json()["error"]
    s.commit("structures", [], {})
    committed = http.post(f"/api/sessions/{s.id}/steps/structures/score", json={"params": {"site": list(PLACED_A)}})
    assert committed.status_code == 409, committed.get_json()
    assert committed.get_json()["status"] == design_document.STATUS_COMMITTED
    # A cache miss regenerates transparently: evict and score again.
    s.reopen("structures")
    s.cache.discard(s.id)
    calls_before = h.identify_solar.call_count
    again = s.score(PLACED_A)
    assert again["properties"]["suitability_score"] == PLACED_A_FEATURE["properties"]["suitability_score"]
    assert h.identify_solar.call_count == calls_before + 1, "a cold cache regenerates the run once"

print(
    f"11 [test 11]. COMMITS: {len(selected)} selected + {len(placed)} placed sites committed with internal "
    f"ids {ids} (ranks kept, placed allocated above), NO exclusion_crossings key on any structure "
    f"feature (every other step's features carry one), the placed features stored as scored Points; "
    f"warm and cold reads agree; a reopen restored {len(placed)} user_added beside {len(selected)} "
    f"selected; committing NONE, ONE placed alone and ONE selected alone each succeed. HTTP: "
    f"POST .../steps/structures/score answers 200 with the feature, 400 off-parcel, 400 for trees "
    f"(no placement), 409 on a committed step; a cold cache regenerates the run once."
)

print(
    "\n12 [test 12]. REGRESSION: run the other test files separately -- test_step_registry.py, "
    "test_wire_translation.py, test_wire_translation_inbound.py, test_step_orchestrator.py, "
    "test_step_commit.py, test_water_step.py, test_roads_step.py, test_trees_step.py, "
    "test_solar_suitability.py, test_solar_suitability_pipeline.py, test_solar_road_fallback.py, "
    "test_tree_zone_candidates.py, test_session_api.py, test_fencing.py, test_render_layout_map.py, "
    "test_pipeline_context.py."
)
print("\nAll structures step checks passed.")
