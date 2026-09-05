"""
test_trees_step.py

THE TREES STEP -- the registry's fourth entry, the second with DRAWING, and
the first that consumes every step before it. Run as:

    python test_trees_step.py

REAL COORDINATES, REAL PIPELINE CODE, THE SAME FIXTURE AS THE PRIOR
BRANCHES. The boundary is the actual drawn property from generate_full_
report.py -- 5614 N Montour Rd, Gibsonia, PA (~13.23 acres, UTM 17N) -- and
the DEM is test_water_step.py's / test_roads_step.py's bench-and-drainage
fixture with its flanking levees, unchanged, so the production zones, water
zones and road network committed here are the ones those branches asserted
over. session_manager.create_session(), the terrain warm-up, the landform,
water and roads generates, the commit gate, every rehydrator, tree_zone_
candidates.identify_tree_zone_candidates() and the payload assembly all
RUN, for real. What is mocked is the NETWORK and only the network -- plus
the three SELF-COMPUTES the trees entry point can fall into
(identify_road_corridor_candidates, identify_water_suitability,
identify_optimized_production_areas, each replaced by a COUNTER on the trees
module so a call is counted and never performed), because sections 3 and 4
exist to prove they are never reached and a real call would go to the
network.

WHAT TREES IS, so the assertions read correctly. A tree zone is a MARGINAL-
LAND CROP: the scorer inverts slope, weights hydric overlap heaviest and
rewards soil marginality. A high score is ground production does not want.
That is why section 8 asserts that a drawn zone on hydric, steep ground
records NO caution for either -- it is the step working, not a defect.

Sections (the branch's numbered tests in brackets):
  1  [1]  REGISTRY -- the trees entry's shape, validate_registry() with
          four entries, constants agree with the modules that own them,
          the two new CommitContract declarations are validated.
  2  [2]  GENERATE with production, water and roads committed. Zero
          network calls, zero self-computes, the four factor weights and
          the three availability flags on the wire.
  3  [3]  ROADS EMPTY -> SENTINEL, with a control: the road self-compute
          ran 0 times with road_corridors.NO_ROAD_CORRIDOR and 1 time with
          None in its place. THE ONE THAT FAILS SILENTLY. Plus LANDFORM
          EMPTY -> [] and nothing self-computes.
  4  [4]  WATER EMPTY -> water_suitability.NO_WATER_ZONE, still, with the
          same control.
  5  [5]  ROUND-TRIP IDENTITY: a generated patch out through
          tree_zones_to_feature_collection() and back through
          rehydrate_tree_zone() is field-identical. One tolerance,
          justified in place.
  6  [6]  A DRAWN ZONE rehydrates with every consumer-read field present
          and correctly typed, scoring fields ABSENT; the commit path
          allocates its id above every generated one; a reopen restores
          it as user_added.
  7  [7]  DOWNSTREAM ACCEPTANCE: a rehydrated drawn zone passed as
          tree_zone_patches= into fencing.identify_fencing() and as the
          tree-zone exclusion into solar_suitability.find_candidate_solar_
          zones() runs and produces sane output. THE PROOF THE OVERRIDE
          SEAM WORKS FOR A SECOND DRAWN LAYER.
  8  [8]  CROSSINGS recorded for all four grounds; hydric and steep ground
          record NOTHING, with a control showing the exclusion gates WOULD
          have.
  9  [9]  The three availability flags survive to the wire, and a False
          flag beside a neutral 0.5 comes back distinguishable.
 10  [10] NO NETWORK during rehydration -- a socket counter that raises.
 11  [11] Regression is the other test files, run separately.
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


def _box_around(geometry_utm, half_meters: float) -> list:
    """A square of side 2*half_meters centred on a point INSIDE the
    geometry, as a lon/lat ring -- a drawn zone that certainly overlaps it."""
    point = geometry_utm.representative_point()
    return _ring_wgs84(box(point.x - half_meters, point.y - half_meters, point.x + half_meters, point.y + half_meters))


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


# --- 1 [test 1]. THE REGISTRY ENTRY ----------------------------------

step_registry.validate_registry()
assert step_registry.registered_steps() == ("landform", "water", "roads", "trees"), (
    step_registry.registered_steps()
)
TREES = step_registry.get_step("trees")

assert TREES.generate == "tree_zone_candidates.identify_tree_zone_candidates"
assert TREES.payload == "step_orchestrator.build_trees_payload"
assert TREES.proposal_collection == "tree_zones"
assert TREES.produces == ("tree_zone_patches",)
assert TREES.upstream_steps() == ("landform", "water", "roads"), TREES.upstream_steps()
assert TREES.user_inputs == () and TREES.accumulate is None and TREES.post_commit == ()

_consumed = {c.name: c for c in TREES.consumes}
assert set(_consumed) == {
    "boundary_coordinates", "dem", "boundary_polygon_utm", "canopy_height", "scoring_inputs",
    "production_areas", "selected_water_zone", "selected_road_corridor",
}, sorted(_consumed)
for _name in ("valleys", "hydric_floodplain_union", "floodplain_data_is_fallback", "anchor_lon_lat"):
    assert _name not in _consumed, f"{_name} only feeds a self-compute the committed edges close"

# THE THREE COMMITTED EDGES, one per upstream step.
_landform_edge = _consumed["production_areas"]
assert _landform_edge.source == step_registry.SOURCE_COMMITTED and _landform_edge.from_step == "landform"
assert _landform_edge.empty_commit is None, "[] is the explicit empty answer for a list override"
_water_edge = _consumed["selected_water_zone"]
assert _water_edge.from_step == "water" and _water_edge.combine == "wire_translation.water_zone_union"
assert _water_edge.empty_commit == "water_suitability.NO_WATER_ZONE"
assert step_registry.resolve(_water_edge.empty_commit) is water_suitability.NO_WATER_ZONE
_road_edge = _consumed["selected_road_corridor"]
assert _road_edge.from_step == "roads" and _road_edge.rehydrate == "wire_translation.rehydrate_road_networks"
assert _road_edge.combine == "wire_translation.selected_road_network"
assert _road_edge.empty_commit == "road_corridors.NO_ROAD_CORRIDOR", (
    "THE line section 3 exists for: without it an empty roads commit reaches the "
    "entry point as None and the road self-compute routes a network"
)
assert step_registry.resolve(_road_edge.empty_commit) is road_corridors.NO_ROAD_CORRIDOR
assert road_corridors.NO_ROAD_CORRIDOR is not water_suitability.NO_WATER_ZONE
assert bool(road_corridors.NO_ROAD_CORRIDOR) is True, "an opaque object(), NO_WATER_ZONE's own shape"

# THE SCORING-INPUTS EDGE: the cache's ParcelData through a combine.
_scoring_edge = _consumed["scoring_inputs"]
assert _scoring_edge.source == step_registry.SOURCE_CACHE and _scoring_edge.cache_path == "parcel_data"
assert _scoring_edge.combine == "tree_zone_candidates.scoring_inputs_for_parcel_data"
assert _scoring_edge.forward_as == "scoring_inputs"

# THE COMMIT CONTRACT: landform's shape (select-only plus drawing).
_contract = TREES.commit_contract
assert _contract.layers == (wire_translation.LAYER_TREE_ZONE,) == ("tree_zone_candidate",)
assert _contract.geometry_types == ("Polygon", "MultiPolygon")
assert _contract.min_features == 0 and _contract.max_features is None
assert _contract.rehydrate == "wire_translation.rehydrate_tree_zones"
assert _contract.internal_id_parameter == "zone_ids"
assert _contract.internal_id_parser == "wire_translation.internal_tree_zone_id"
assert _contract.requires_provenance is True
assert _contract.feature_group is None and _contract.group_check is None
assert step_registry.get_step("landform").commit_contract.internal_id_parser == "wire_translation.internal_zone_id"

# THE FOUR CROSSING GROUNDS, exactly, and not the exclusion gates.
assert _contract.crossings is not None and len(_contract.crossings) == 4
_grounds = {g.type: g for g in _contract.crossings}
assert set(_grounds) == {"production", "water", "road", "canopy"}, sorted(_grounds)
assert _grounds["production"].consumed == "production_areas"
assert _grounds["water"].consumed == "selected_water_zone"
assert _grounds["road"].consumed == "selected_road_corridor"
assert _grounds["canopy"].exclusion_layer == "canopy" and _grounds["canopy"].consumed is None
for _t in ("production", "water", "road"):
    assert _grounds[_t].footprint and callable(step_registry.resolve(_grounds[_t].footprint))
    assert _grounds[_t].label
for _other in step_registry.STEP_REGISTRY.values():
    if _other.step_id != "trees":
        assert _other.commit_contract.crossings is None, (
            f"{_other.step_id} keeps the exclusion gates as its grounds"
        )
assert "hydric" not in _grounds and "slope" not in _grounds

# EVERY TARGET RESOLVES, and every forward_as is a real parameter.
import inspect

_signature = inspect.signature(TREES.resolve_generate())
for _c in TREES.consumes:
    if _c.forward_as:
        assert _c.forward_as in _signature.parameters, _c.forward_as
    for _target in (_c.rehydrate, _c.combine, _c.empty_commit):
        if _target:
            step_registry.resolve(_target)
step_registry.resolve(TREES.payload)
step_registry.resolve(_contract.rehydrate)
step_registry.resolve(_contract.internal_id_parser)

# FAILURE LAYERS: canopy, mandatory, at production_zone_payload's own pair.
assert len(TREES.failure_layers) == 1
assert TREES.failure_layers[0].exception == "canopy_height_data.CanopyCoverageIncompleteError"
assert (TREES.failure_layers[0].layer, TREES.failure_layers[0].label) == production_zone_payload.LAYER_CANOPY

# THE EDGE HELPERS see the new entry.
assert step_registry.dependents_of("roads") == ("trees",)
assert step_registry.dependents_of("water") == ("roads", "trees")
assert step_registry.transitive_dependents("landform") == ("water", "roads", "trees")
assert step_registry.transitive_dependents("trees") == ()

# THE TWO NEW DECLARATIONS ARE VALIDATED. A copy of the trees entry with
# each malformation must be refused.
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


_G = step_registry.CrossingGround
_contract_with = lambda **kw: dataclasses.replace(TREES, commit_contract=dataclasses.replace(_contract, **kw))
_rejects(_contract_with(internal_id_parser=None), "an allocated id with no parser")
_rejects(_contract_with(internal_id_parameter=None), "a parser with nothing to allocate")
_rejects(_contract_with(crossings=(_G(type="x", consumed="dem", footprint="f", label="l"),)), "a ground on a cache edge")
_rejects(_contract_with(crossings=(_G(type="x", consumed="nope", footprint="f", label="l"),)), "a ground on an undeclared edge")
_rejects(_contract_with(crossings=(_G(type="x", consumed="production_areas", label="l"),)), "a committed ground with no footprint")
_rejects(_contract_with(crossings=(_G(type="x", consumed="production_areas", footprint="f"),)), "a committed ground with no label")
_rejects(_contract_with(crossings=(_G(type="x", exclusion_layer="canopy", footprint="f"),)), "a gate ground with a footprint")
_rejects(_contract_with(crossings=(_G(type="x"),)), "a ground that is neither")
_rejects(_contract_with(crossings=(_G(type="x", exclusion_layer="canopy"), _G(type="x", exclusion_layer="slope"))), "two grounds of one type")
_rejects(_contract_with(crossings=_G(type="x", exclusion_layer="canopy")), "a bare CrossingGround instead of a tuple")
step_registry.validate_registry()

print(
    f"1 [test 1]. REGISTRY: validate_registry() passes with four entries "
    f"{step_registry.registered_steps()}. The trees entry consumes {len(TREES.consumes)} "
    f"values -- 5 off the cache (scoring_inputs through a combine), 3 off commits, one per "
    f"upstream step, with empty_commit None (landform, [] is explicit), NO_WATER_ZONE (water) "
    f"and NO_ROAD_CORRIDOR (roads) -- declares landform's contract shape (zone_ids allocated, "
    f"parsed by internal_tree_zone_id), FOUR crossing grounds "
    f"{sorted(_grounds)} and no hydric or slope, one canopy failure layer at "
    f"production_zone_payload.LAYER_CANOPY, and 10 malformations of the two new "
    f"CommitContract declarations are each refused. dependents_of('roads') == "
    f"{step_registry.dependents_of('roads')}."
)


# --- 2 [test 2]. GENERATE with production, water and roads committed ----

with Harness() as h:
    s = Session()
    s.upstream()
    for step in ("landform", "water", "roads"):
        assert s.stored()["steps"][step]["status"] == design_document.STATUS_COMMITTED

    network_before = h.total_network_calls
    selfcomputes_before = h.tree_selfcomputes()
    canopy_masks_before = h.tree_canopy_mask.call_count
    payload = s.trees()
    trees_network_calls = h.total_network_calls - network_before
    selfcomputes = {k: v - selfcomputes_before[k] for k, v in h.tree_selfcomputes().items()}

    assert sorted(payload) == ["search_space", "summary", "tree_zones", "zones"], sorted(payload)
    assert h.identify_trees.call_count == 1
    validate_feature_collection(payload["tree_zones"])
    CANDIDATES = payload["tree_zones"]["features"]
    assert CANDIDATES, "the fixture must produce tree zone candidates, or every assertion below is vacuous"
    assert len(payload["zones"]) == len(CANDIDATES) == payload["summary"]["candidate_count"]

    # WHAT THE ENTRY POINT RECEIVED: every committed edge, by identity /
    # shape, and NOT None for any of them.
    call = h.identify_trees.call_args
    assert call.kwargs["production_areas"] is not None and len(call.kwargs["production_areas"]) == len(
        s.stored()["steps"]["landform"]["features"]["features"]
    )
    assert call.kwargs["selected_water_zone"] is not water_suitability.NO_WATER_ZONE
    assert "render_fill_polygon_utm" in call.kwargs["selected_water_zone"]
    assert call.kwargs["selected_road_corridor"] is not road_corridors.NO_ROAD_CORRIDOR
    assert call.kwargs["selected_road_corridor"]["branches"] and call.kwargs["selected_road_corridor"]["cells"]
    assert call.kwargs["scoring_inputs"] is not None and set(call.kwargs["scoring_inputs"]) == {
        "farmland_classification", "soil_components", "soil_geometries", "water_features",
    }
    assert call.kwargs["canopy_height"] is not None and call.kwargs["dem"] is not None
    assert h.water_union.call_count >= 1 and h.road_selection.call_count >= 1
    for absent in ("valleys", "hydric_floodplain_union", "floodplain_data_is_fallback", "anchor_lon_lat"):
        assert absent not in call.kwargs, absent

    # ZERO NETWORK, ZERO SELF-COMPUTE. Every one of the eight edges did its job.
    assert trees_network_calls == 0, trees_network_calls
    assert selfcomputes == {
        "identify_road_corridor_candidates": 0,
        "identify_water_suitability": 0,
        "identify_optimized_production_areas": 0,
    }, selfcomputes
    assert h.tree_canopy_mask.call_count == canopy_masks_before + 1 and h.canopy_refetch.call_count == 0

    # THE PER-FEATURE BLOCK: the score, the four factors, the slope, the
    # rank, and THE THREE AVAILABILITY FLAGS -- True, because the cache's
    # rows were used and ParcelData is hard-fail.
    for f in CANDIDATES:
        p = f["properties"]
        assert p["layer"] == wire_translation.LAYER_TREE_ZONE
        assert f["geometry"]["type"] in ("Polygon", "MultiPolygon")
        assert f["id"].startswith("tree-zone-candidate-")
        for key in (
            "area_acres", "tree_suitability_score", "soil_marginality_factor", "slope_factor",
            "hydric_overlap_factor", "stream_proximity_factor", "avg_slope_pct", "rank",
        ):
            assert isinstance(p[key], (int, float)), (key, p[key])
        assert p["tree_suitability_score"] >= tree_zone_candidates.MIN_TREE_SUITABILITY_SCORE
        assert p["area_acres"] >= tree_zone_candidates.MIN_TREE_ZONE_ACRES
        for flag in ("soil_marginality_data_available", "hydric_data_available", "stream_data_available"):
            assert p[flag] is True, (flag, p[flag])
        # No prime farmland on the fixture, so marginality is measured at
        # 1.0 -- a real measurement, not the neutral 0.5.
        assert p["soil_marginality_factor"] == 1.0
    ranks = sorted(f["properties"]["rank"] for f in CANDIDATES)
    assert ranks == list(range(1, len(CANDIDATES) + 1))

    # THE TABULAR ROWS carry the wire feature id, looked up off the features.
    by_id = {f["id"]: f for f in CANDIDATES}
    for row in payload["zones"]:
        assert row["feature_id"] in by_id and by_id[row["feature_id"]]["properties"]["rank"] == row["rank"]
        assert set(row["factors"]) == {"hydric_overlap", "slope", "soil_marginality", "stream_proximity"}

    # THE STEP-LEVEL BLOCK: the narrative whole, and the FOUR FACTOR WEIGHTS
    # on the wire -- what lets a panel explain a score.
    summary = payload["summary"]
    assert set(summary) == {"candidate_count", "search_space", "selection", "gates"}, sorted(summary)
    WEIGHTS = summary["selection"]["factor_weights_pct"]
    assert WEIGHTS == {
        "hydric_overlap": round(tree_zone_candidates.HYDRIC_OVERLAP_FACTOR_WEIGHT * 100, 1),
        "slope": round(tree_zone_candidates.SLOPE_FACTOR_WEIGHT * 100, 1),
        "soil_marginality": round(tree_zone_candidates.SOIL_MARGINALITY_FACTOR_WEIGHT * 100, 1),
        "stream_proximity": round(tree_zone_candidates.STREAM_PROXIMITY_FACTOR_WEIGHT * 100, 1),
    }, WEIGHTS
    assert abs(sum(WEIGHTS.values()) - 100.0) < 1e-9
    assert summary["gates"] == {
        "soil_marginality_data_available": True, "hydric_data_available": True, "stream_data_available": True,
    }
    assert summary["selection"]["existing_canopy_excluded"] is True
    assert summary["search_space"]["claimed_acres"] > 0 and summary["search_space"]["search_space_acres"] > 0
    assert summary["search_space"]["parcel_acres"] == round(PARCEL_ACRES, 1)
    # The scores decompose as the weights say they do.
    for f in CANDIDATES:
        p = f["properties"]
        composite = (
            tree_zone_candidates.HYDRIC_OVERLAP_FACTOR_WEIGHT * p["hydric_overlap_factor"]
            + tree_zone_candidates.SLOPE_FACTOR_WEIGHT * p["slope_factor"]
            + tree_zone_candidates.SOIL_MARGINALITY_FACTOR_WEIGHT * p["soil_marginality_factor"]
            + tree_zone_candidates.STREAM_PROXIMITY_FACTOR_WEIGHT * p["stream_proximity_factor"]
        ) * tree_zone_candidates.SUITABILITY_SCORE_SCALE
        assert abs(composite - p["tree_suitability_score"]) < 0.2, (composite, p["tree_suitability_score"])

    validate_feature_collection(payload["search_space"])

    # THE DOCUMENT: generated, no features.
    entry = s.stored()["steps"]["trees"]
    assert entry["status"] == design_document.STATUS_GENERATED and not entry.get("features")
    # THE READ VERB returns the same payload from the cache; a regenerate
    # is idempotent, INCLUDING THE IDS the reopen restore matches on.
    assert s.layers("trees") == payload
    again = s.trees()
    assert [f["id"] for f in again["tree_zones"]["features"]] == [f["id"] for f in CANDIDATES]
    assert again["summary"] == payload["summary"]

    GENERATE_SESSION = s
    GENERATE_PAYLOAD = payload
    GENERATED_PATCHES = s.context().step_proposals["trees"]["patches"]

print(
    f"2 [test 2]. GENERATE: with landform ({len(s.stored()['steps']['landform']['features']['features'])} "
    f"zones), water (3 zones, as a union) and roads (one network) committed, the trees generate "
    f"produced {len(CANDIDATES)} candidate(s) -- ranks {ranks}, scores "
    f"{[f['properties']['tree_suitability_score'] for f in CANDIDATES]}, avg slope "
    f"{[f['properties']['avg_slope_pct'] for f in CANDIDATES]}% -- over a "
    f"{summary['search_space']['search_space_acres']}-acre search space "
    f"({summary['search_space']['claimed_acres']} acres claimed of "
    f"{summary['search_space']['parcel_acres']}), with {trees_network_calls} network calls and "
    f"{selfcomputes} self-computes. The entry point received all three committed values (none "
    f"None), scoring_inputs off the cache, and no undeclared edge. Factor weights on the wire: "
    f"{WEIGHTS}; all three availability flags True on every feature and in summary.gates."
)


# --- 3 [test 3]. ROADS EMPTY -> SENTINEL. THE ONE THAT FAILS SILENTLY.
#
# A user who commits the roads step with nothing has DECIDED there is no
# road. identify_tree_zone_candidates():
#
#     if selected_road_corridor is NO_ROAD_CORRIDOR: selected_road_corridor = None
#     elif selected_road_corridor is None:
#         road_result = identify_road_corridor_candidates(...)   # a FULL routing pass
#         selected_road_corridor = road_result["selected_road_corridor"]
#
# Forward None and the second branch routes a network with no anchor, and
# claims whatever it grew as ground trees may not use. Nothing raises. The
# registry's empty_commit declaration is the only thing between the two.

with Harness() as h:
    s = Session()
    s.upstream(access_point=None)
    assert s.stored()["steps"]["roads"]["status"] == design_document.STATUS_COMMITTED
    assert s.stored()["steps"]["roads"]["features"]["features"] == []

    # READ 1 -- WARM (the cache holds [] for roads' rehydration: the trap).
    warm_context = s.context()
    assert warm_context.step_committed["roads"]["value"] == []
    warm = step_orchestrator.assemble_consumes(TREES, warm_context, s.stored())["selected_road_corridor"]
    assert warm is road_corridors.NO_ROAD_CORRIDOR, f"warm read: {warm!r}"
    # READ 2 -- COLD.
    s.cache.discard(s.id)
    cold = step_orchestrator.assemble_consumes(TREES, s.context(), s.stored())["selected_road_corridor"]
    assert cold is road_corridors.NO_ROAD_CORRIDOR, f"cold read: {cold!r}"
    assert h.road_selection.call_count == 0, "no selection is made from an empty commit"

    # THE MEASUREMENT: a real trees generate with roads committed empty.
    assert h.tree_road_selfcompute.call_count == 0
    payload = s.trees()
    assert h.tree_road_selfcompute.call_count == 0, (
        f"identify_road_corridor_candidates() ran {h.tree_road_selfcompute.call_count} time(s) "
        f"from inside the trees generate. The sentinel was not believed: a road the user "
        f"explicitly decided against was routed and claimed."
    )
    call = h.identify_trees.call_args
    assert call.kwargs["selected_road_corridor"] is road_corridors.NO_ROAD_CORRIDOR, (
        "the entry point receives the sentinel BY IDENTITY"
    )
    # No road was claimed: the claimed acreage is production + water only,
    # and it is LESS than section 2's, which claimed a network too.
    assert payload["summary"]["search_space"]["claimed_acres"] < GENERATE_PAYLOAD["summary"]["search_space"]["claimed_acres"]
    assert payload["tree_zones"]["features"], "the fixture still yields candidates"
    NO_ROAD_PAYLOAD = payload

    # THE CONTROL: forward None in the sentinel's place and the self-compute
    # DOES run -- which is what makes the zero above a measurement.
    assembled = s.assembled()
    control = dict(step_orchestrator.forwarded_arguments(TREES, assembled, {}))
    control["selected_road_corridor"] = None
    tree_zone_candidates.identify_tree_zone_candidates(**control)
    assert h.tree_road_selfcompute.call_count == 1, (
        "forwarding None must reach the self-compute -- if it does not, the zero above proves nothing"
    )
    # ...and the sentinel, forwarded directly, is normalized rather than
    # subscripted: the entry point ran, routed nothing, and treated it as
    # "no road" (the falsy branch at the exclusion polygon).
    control["selected_road_corridor"] = road_corridors.NO_ROAD_CORRIDOR
    direct = tree_zone_candidates.identify_tree_zone_candidates(**control)
    assert h.tree_road_selfcompute.call_count == 1
    assert direct["narrative_data"]["search_space"]["claimed_acres"] == NO_ROAD_PAYLOAD["summary"]["search_space"]["claimed_acres"]
    # The water self-compute never ran either way: water was committed.
    assert h.tree_water_selfcompute.call_count == 0

    # The two direct calls above went through the same wrapper as the one
    # real generate: three entry-point calls, ONE of them the orchestrator's.
    assert h.identify_trees.call_count == 3

print(
    f"3 [test 3]. ROADS EMPTY -> SENTINEL: with roads committed EMPTY, the trees consumes edge "
    f"resolves to road_corridors.NO_ROAD_CORRIDOR by identity on a warm read and a cold read, "
    f"the entry point receives it by identity, and identify_road_corridor_candidates() ran "
    f"0 times inside the generate (claimed acres "
    f"{NO_ROAD_PAYLOAD['summary']['search_space']['claimed_acres']} vs "
    f"{GENERATE_PAYLOAD['summary']['search_space']['claimed_acres']} with a road). THE CONTROL: "
    f"the same forwarded arguments with None in the sentinel's place ran it exactly 1 time -- "
    f"the zero is a measurement, not an unreachable path. Forwarded directly, the sentinel is "
    f"normalized (not subscripted) and claims no road."
)

# --- 3b. LANDFORM EMPTY -> [] and nothing self-computes -----------------
#
# The contract allows an empty landform commit. Water and roads both handle
# [] as "no production ground"; roads then has no demand and routes nothing,
# so it is committed empty too. Trees receives [] for production_areas --
# the rehydrator's own explicit empty -- and the production optimiser is
# never run in its place.

with Harness() as h:
    s = Session()
    s.commit_landform(whole=False)
    assert s.stored()["steps"]["landform"]["features"]["features"] == []
    s.commit_water(3)
    s.commit_roads(None)

    payload = s.trees()
    call = h.identify_trees.call_args
    assert call.kwargs["production_areas"] == [], call.kwargs["production_areas"]
    assert call.kwargs["production_areas"] is not None
    assert h.tree_production_selfcompute.call_count == 0, (
        "an empty landform commit must not re-run the production optimiser"
    )
    assert h.tree_road_selfcompute.call_count == 0 and h.tree_water_selfcompute.call_count == 0
    # With no production claimed, the search space is the largest of the
    # three sessions so far.
    assert payload["summary"]["search_space"]["search_space_acres"] > NO_ROAD_PAYLOAD["summary"]["search_space"]["search_space_acres"]
    LANDFORM_EMPTY_PAYLOAD = payload

    # THE CONTROL, once more: None DOES reach the optimiser.
    control = dict(step_orchestrator.forwarded_arguments(TREES, s.assembled(), {}))
    control["production_areas"] = None
    tree_zone_candidates.identify_tree_zone_candidates(**control)
    assert h.tree_production_selfcompute.call_count == 1

print(
    f"3b. LANDFORM EMPTY: trees receives production_areas == [] (the rehydrator's own explicit "
    f"empty, no sentinel needed), identify_optimized_production_areas() ran 0 times (1 with None "
    f"in its place), and the search space grew to "
    f"{LANDFORM_EMPTY_PAYLOAD['summary']['search_space']['search_space_acres']} acres with "
    f"nothing claimed by production."
)


# --- 4 [test 4]. WATER EMPTY -> NO_WATER_ZONE, still -------------------

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=0)
    assert s.stored()["steps"]["water"]["features"]["features"] == []

    warm = step_orchestrator.assemble_consumes(TREES, s.context(), s.stored())["selected_water_zone"]
    assert warm is water_suitability.NO_WATER_ZONE, f"warm read: {warm!r}"
    s.cache.discard(s.id)
    cold = step_orchestrator.assemble_consumes(TREES, s.context(), s.stored())["selected_water_zone"]
    assert cold is water_suitability.NO_WATER_ZONE, f"cold read: {cold!r}"
    assert h.water_union.call_count == 0

    payload = s.trees()
    assert h.tree_water_selfcompute.call_count == 0, (
        f"identify_water_suitability() ran {h.tree_water_selfcompute.call_count} time(s): the "
        f"retired water pipeline claimed a zone the user rejected"
    )
    assert h.identify_trees.call_args.kwargs["selected_water_zone"] is water_suitability.NO_WATER_ZONE
    assert h.tree_road_selfcompute.call_count == 0
    WATER_EMPTY_PAYLOAD = payload

    control = dict(step_orchestrator.forwarded_arguments(TREES, s.assembled(), {}))
    control["selected_water_zone"] = None
    tree_zone_candidates.identify_tree_zone_candidates(**control)
    assert h.tree_water_selfcompute.call_count == 1, "None must reach the self-compute"

print(
    f"4 [test 4]. WATER EMPTY -> NO_WATER_ZONE: the sentinel arrives by identity on warm and "
    f"cold reads, identify_water_suitability() ran 0 times in the generate and 1 time with None "
    f"in its place. Claimed acres "
    f"{WATER_EMPTY_PAYLOAD['summary']['search_space']['claimed_acres']} (production + road only)."
)


# --- 5 [test 5]. ROUND-TRIP IDENTITY -----------------------------------
#
# A GENERATED patch pushed OUT through tree_zones_to_feature_collection()
# and back IN through rehydrate_tree_zone() must come back as the same
# internal dict, field by field.
#
# THE ONE TOLERANCE, on polygon_utm (and therefore render_fill_polygon_utm,
# which is the same object). The wire carries polygon_utm as its stored
# WGS84 reprojection; coming back, rehydration reprojects that ring into
# the DEM's CRS, and PROJ is not exactly idempotent across the pair -- each
# vertex lands within a fraction of a nanometre of where it started. Measured
# as symmetric-difference area RELATIVE to the original's area, the
# strictest available form (an inward drift on one edge cannot cancel an
# outward drift on another). 1e-9 is the production branch's own ceiling,
# taken unchanged: on a half-acre patch it is about two square micrometres,
# nine orders of magnitude below one 25 m^2 DEM cell, and a real geometric
# difference of even one cell is instantly fatal against it. Nothing else is
# toleranced: id, area_acres and every advisory field are asserted EXACTLY.

REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE = 1e-9
PRODUCER_FIELDS = {
    "id", "polygon_utm", "render_fill_polygon_utm", "geometry_wgs84", "area_acres",
    "tree_suitability_score", "soil_marginality_factor", "slope_factor", "hydric_overlap_factor",
    "stream_proximity_factor", "avg_slope_pct", "soil_marginality_data_available",
    "hydric_data_available", "stream_data_available", "rank",
}
ADVISORY_ON_THE_WIRE = wire_translation._TREE_ADVISORY_WIRE_FIELDS

DEM = GENERATE_SESSION.context().dem
assert GENERATED_PATCHES, "section 2 must have produced patches"
for original in GENERATED_PATCHES:
    assert set(original) == PRODUCER_FIELDS, (
        f"the producer's patch literal has drifted from what this test enumerates: "
        f"{sorted(set(original) ^ PRODUCER_FIELDS)}"
    )
    assert original["render_fill_polygon_utm"] is original["polygon_utm"], "the producer's own identity"

OUTBOUND = wire_translation.tree_zones_to_feature_collection(GENERATED_PATCHES)
validate_feature_collection(OUTBOUND)
assert OUTBOUND == GENERATE_PAYLOAD["tree_zones"], "the payload carries the outbound collection unchanged"

worst_relative_symmetric_difference = 0.0
for feature, original in zip(OUTBOUND["features"], GENERATED_PATCHES):
    rehydrated = wire_translation.rehydrate_tree_zone(feature, DEM)
    assert set(rehydrated) == set(original), (
        f"patch {original['id']}: field set differs -- {sorted(set(rehydrated) ^ set(original))}"
    )
    # The id came home off the feature's own id string, with no help.
    assert rehydrated["id"] == original["id"]
    # EXACT: the acreage, at the producer's own rounding.
    assert rehydrated["area_acres"] == original["area_acres"], (rehydrated["area_acres"], original["area_acres"])
    # THE IDENTITY SURVIVES: render fill IS the polygon, as the producer has it.
    assert rehydrated["render_fill_polygon_utm"] is rehydrated["polygon_utm"]
    assert rehydrated["polygon_utm"].geom_type == original["polygon_utm"].geom_type
    # TOLERANCED, and only here.
    relative = rehydrated["polygon_utm"].symmetric_difference(original["polygon_utm"]).area / original["polygon_utm"].area
    worst_relative_symmetric_difference = max(worst_relative_symmetric_difference, relative)
    assert relative < REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE, (
        f"patch {original['id']}: relative symmetric difference {relative:.3e} exceeds "
        f"{REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE:.0e} -- a real geometric difference"
    )
    assert rehydrated["geometry_wgs84"]["type"] == original["geometry_wgs84"]["type"]
    # The advisory block, INCLUDING the three flags, came home verbatim.
    for field in ADVISORY_ON_THE_WIRE:
        assert rehydrated[field] == original[field], (field, rehydrated[field], original[field])
    assert "confidence_notes" not in rehydrated, "the producer never held one; inheriting it would add a field"

# THE WHOLE COLLECTION, through the list rehydrator, ids parsed not given.
whole = wire_translation.rehydrate_tree_zones(OUTBOUND, DEM)
assert [p["id"] for p in whole] == [p["id"] for p in GENERATED_PATCHES]

print(
    f"5 [test 5]. ROUND TRIP: all {len(GENERATED_PATCHES)} generated patches return field-identical "
    f"({len(PRODUCER_FIELDS)} fields, read off the producer's literal). id, area_acres and all "
    f"{len(ADVISORY_ON_THE_WIRE)} advisory fields EXACT; render_fill_polygon_utm IS polygon_utm on "
    f"both sides; worst relative symmetric difference on polygon_utm "
    f"{worst_relative_symmetric_difference:.3e} (tolerance "
    f"{REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE:.0e}, PROJ round-trip noise). confidence_notes "
    f"is not inherited: the producer never held it."
)


# --- 6 [test 6]. A DRAWN ZONE ------------------------------------------

CONSUMER_READ_FIELDS = ("id", "polygon_utm", "render_fill_polygon_utm", "geometry_wgs84", "area_acres")
SCORING_FIELDS = tuple(f for f in PRODUCER_FIELDS if f not in CONSUMER_READ_FIELDS)

with Harness() as h:
    s = GENERATE_SESSION
    context = s.context()
    committed_production = s.committed("landform")
    committed_water = s.assembled()["selected_water_zone"]
    committed_road = s.assembled()["selected_road_corridor"]

    # A ring no pipeline produced: a box over the hydric footprint, the
    # ground the step exists to find. Directly, first.
    drawn_feature = _drawn("drawn-hydric", HYDRIC_ZONE_RING)
    drawn = wire_translation.rehydrate_tree_zone(drawn_feature, context.dem, zone_id=41)
    assert set(drawn) == set(CONSUMER_READ_FIELDS), sorted(drawn)
    assert drawn["id"] == 41
    assert drawn["polygon_utm"].geom_type == "Polygon" and drawn["polygon_utm"].is_valid
    assert drawn["render_fill_polygon_utm"] is drawn["polygon_utm"]
    assert drawn["geometry_wgs84"]["type"] == "Polygon"
    assert isinstance(drawn["area_acres"], float) and drawn["area_acres"] > 0
    assert abs(drawn["area_acres"] - _utm(drawn_feature).area / SQUARE_METERS_PER_ACRE) < 0.01
    for field in SCORING_FIELDS:
        assert field not in drawn, f"{field} must be ABSENT on a drawn zone, not zeroed"
    # ...and without an allocated id it is refused, never invented.
    try:
        wire_translation.rehydrate_tree_zone(drawn_feature, context.dem)
    except wire_translation.InboundGeometryError as exc:
        assert "zone_id=" in str(exc)
    else:
        raise AssertionError("a drawn zone with no pipeline id must be refused without zone_id=")

    # THE COMMIT PATH allocates the id ABOVE every generated one, and the
    # tree parser is what keeps the generated ones. A drawn zone WEARING a
    # generated id but declared user_added is allocated a fresh one.
    generated_ids = [p["id"] for p in GENERATED_PATCHES]
    disguised = _drawn(f"tree-zone-candidate-{generated_ids[0]}", _box_around(BOUNDARY_POLYGON_UTM, 8))
    commit_features = list(CANDIDATES) + [drawn_feature]
    provenance = {f["id"]: "generated" for f in CANDIDATES}
    provenance["drawn-hydric"] = "user_added"
    ids = commit_validation.internal_ids_for(
        commit_features, provenance, wire_translation.internal_tree_zone_id
    )
    assert ids == generated_ids + [max(generated_ids) + 1], ids
    assert commit_validation.internal_ids_for(commit_features, provenance) != ids, (
        "the production parser would renumber every tree zone -- which is why the contract declares its own"
    )
    ids_disguised = commit_validation.internal_ids_for(
        commit_features + [disguised], {**provenance, disguised["id"]: "user_added"},
        wire_translation.internal_tree_zone_id,
    )
    assert ids_disguised[-1] not in generated_ids and len(set(ids_disguised)) == len(ids_disguised)

    document = s.commit("trees", commit_features, provenance)
    assert document["steps"]["trees"]["status"] == design_document.STATUS_COMMITTED
    stored = document["steps"]["trees"]["features"]["features"]
    assert [f["id"] for f in stored] == [f["id"] for f in commit_features]
    assert all("exclusion_crossings" in f["properties"] for f in stored)
    # The drawn feature is stored AS DRAWN: no scoring field was invented.
    stored_drawn = next(f for f in stored if f["id"] == "drawn-hydric")
    for field in SCORING_FIELDS:
        assert field not in stored_drawn["properties"], field

    # WARM AND COLD AGREE: the committed value (the gate's own rehydration)
    # and a cold rebuild carry the same patches with the same ids.
    warm_value = s.context().step_committed["trees"]["value"]
    assert [p["id"] for p in warm_value] == ids
    assert set(warm_value[-1]) == set(CONSUMER_READ_FIELDS)
    for patch, original in zip(warm_value, GENERATED_PATCHES):
        assert set(patch) == PRODUCER_FIELDS and patch["rank"] == original["rank"]
    s.cache.discard(s.id)
    cold_value = s.committed("trees")
    assert [p["id"] for p in cold_value] == ids
    assert set(cold_value[-1]) == set(CONSUMER_READ_FIELDS)
    assert h.rehydrate_trees.call_count >= 1
    DRAWN_COMMITTED = cold_value[-1]

    # REOPEN restores the drawn zone as user_added and the selection by id.
    s.reopen("trees")
    restored = s.context().step_restored["trees"]
    assert restored["selected_feature_ids"] == [f["id"] for f in CANDIDATES]
    assert [f["id"] for f in restored["user_added"]["features"]] == ["drawn-hydric"]
    assert restored["missing_feature_ids"] == []
    assert restored["provenance"] == provenance
    assert s.stored()["steps"]["trees"]["status"] == design_document.STATUS_GENERATED
    # Committed again, unchanged, for section 7 and 8 to read.
    s.commit("trees", commit_features, provenance)

print(
    f"6 [test 6]. DRAWN ZONE: a hand-drawn ring over the hydric footprint rehydrates with exactly "
    f"the {len(CONSUMER_READ_FIELDS)} consumer-read fields {CONSUMER_READ_FIELDS} present and typed "
    f"({drawn['area_acres']} acres, Polygon, render fill IS polygon), and all "
    f"{len(SCORING_FIELDS)} scoring fields ABSENT. The commit path allocated it id "
    f"{ids[-1]} above the generated {generated_ids} through the tree id parser (the production "
    f"parser would have renumbered everything), warm and cold reads agree, and a reopen restored "
    f"it as user_added beside the {len(CANDIDATES)} selected candidates."
)


# --- 7 [test 7]. DOWNSTREAM ACCEPTANCE. THE PROOF THE SEAM WORKS. -------
#
# A rehydrated drawn zone, in the SAME list as the generated ones, passed
# as tree_zone_patches= into fencing.identify_fencing() -- the one real
# consumer that takes the list by that name -- and as the tree-zone
# exclusion into solar_suitability.find_candidate_solar_zones(), built the
# way identify_solar_candidate_zones() builds it. Both run and both produce
# output that says what it should about the drawn zone.

with Harness() as h:
    s = GENERATE_SESSION
    context = s.context()
    tree_patches = s.committed("trees")
    assert tree_patches[-1]["id"] == DRAWN_COMMITTED["id"] and "rank" not in tree_patches[-1]
    production_patches = s.committed("landform")
    water_union = s.assembled()["selected_water_zone"]
    road_network = s.assembled()["selected_road_corridor"]

    network_before = h.total_network_calls
    fence_result = fencing.identify_fencing(
        REAL_BOUNDARY,
        water_features_geojson={"type": "FeatureCollection", "features": []},
        dem=context.dem,
        boundary_polygon_utm=context.boundary_polygon_utm,
        production_areas=production_patches,
        selected_water_zone=water_union,
        selected_road_corridor=road_network,
        tree_zone_patches=tree_patches,
        canopy_height=context.parcel_data.canopy_height,
        production_zone_polygons_utm=[p["render_fill_polygon_utm"] for p in production_patches],
        road_corridor_cell_footprint_polygon_utm=road_network["cell_footprint_polygon_utm"],
    )
    assert h.total_network_calls == network_before
    assert h.tree_road_selfcompute.call_count == 0 and h.identify_trees.call_count == 0, (
        "supplying tree_zone_patches must skip every self-compute in identify_fencing()"
    )
    validate_feature_collection(fence_result["fencing_geojson"])
    tree_fences = [
        f for f in fence_result["fencing_geojson"]["features"]
        if f["properties"].get("fence_type") == "tree_zone_exclusion"
    ]
    assert len(tree_fences) == len(tree_patches), (len(tree_fences), len(tree_patches))
    # The DRAWN zone's loop is the last (input order), a closed ring OUTSIDE
    # the drawn polygon by the fence buffer, enclosing it entirely.
    drawn_fence = tree_fences[-1]
    assert drawn_fence["properties"]["candidate_rank"] == len(tree_patches)
    fence_utm = shape(transform_geom("EPSG:4326", CRS, drawn_fence["geometry"]))
    assert fence_utm.geom_type in ("LineString", "MultiLineString")
    assert fence_utm.is_ring if fence_utm.geom_type == "LineString" else True
    enclosed = Polygon(fence_utm.coords) if fence_utm.geom_type == "LineString" else unary_union(
        [Polygon(part.coords) for part in fence_utm.geoms]
    )
    assert enclosed.contains(DRAWN_COMMITTED["polygon_utm"]), "the fence loop must enclose the drawn zone"
    assert not fence_utm.intersects(DRAWN_COMMITTED["polygon_utm"]), "and sit outside its edge"
    assert enclosed.area > DRAWN_COMMITTED["polygon_utm"].area
    assert fence_result["segment_count"] >= 1

    # SOLAR: the drawn zone is hard-excluded from structure siting, exactly
    # as identify_solar_candidate_zones() builds the exclusion.
    tree_exclusion = unary_union([p["render_fill_polygon_utm"] for p in tree_patches]).buffer(
        solar_suitability.TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS
    )
    assert tree_exclusion.intersects(DRAWN_COMMITTED["polygon_utm"])
    candidates = solar_suitability.find_candidate_solar_zones(
        context.dem,
        production_patches,
        [water_union],
        [road_network["cell_footprint_polygon_utm"]],
        context.boundary_polygon_utm,
        tree_zone_exclusion_polygon_utm=tree_exclusion,
        road_proximity_buffer_meters=solar_suitability.ROAD_CORRIDOR_PROXIMITY_METERS,
    )
    assert isinstance(candidates, list)
    for candidate in candidates:
        footprint = candidate.get("footprint_polygon_utm") or candidate.get("render_fill_polygon_utm") or candidate.get("polygon_utm")
        if footprint is not None:
            assert not footprint.intersects(tree_exclusion), "a structure candidate sits on a drawn tree zone"
    without_exclusion = solar_suitability.find_candidate_solar_zones(
        context.dem, production_patches, [water_union], [road_network["cell_footprint_polygon_utm"]],
        context.boundary_polygon_utm, tree_zone_exclusion_polygon_utm=None,
        road_proximity_buffer_meters=solar_suitability.ROAD_CORRIDOR_PROXIMITY_METERS,
    )
    assert len(without_exclusion) >= len(candidates)

print(
    f"7 [test 7]. DOWNSTREAM ACCEPTANCE: {len(tree_patches)} committed tree patches -- "
    f"{len(tree_patches) - 1} generated plus the drawn one, unscored -- passed as tree_zone_patches= "
    f"into fencing.identify_fencing() produced {len(tree_fences)} tree_zone_exclusion fence loops "
    f"with 0 network calls and 0 self-computes; the drawn zone's loop encloses it "
    f"({enclosed.area:.0f} m^2 around {DRAWN_COMMITTED['polygon_utm'].area:.0f} m^2) and sits outside "
    f"its edge. solar_suitability.find_candidate_solar_zones() ran with the same patches as its "
    f"tree-zone exclusion: {len(candidates)} structure candidate(s), none on a tree zone "
    f"({len(without_exclusion)} without the exclusion)."
)


# --- 8 [test 8]. CROSSINGS: four grounds, and NOT hydric or slope -------

with Harness() as h:
    s = Session()
    s.upstream()
    s.trees()
    context = s.context()
    production_patches = s.committed("landform")
    water_union = s.assembled()["selected_water_zone"]
    road_network = s.assembled()["selected_road_corridor"]
    exclusion = context.exclusion_zones

    # The resolved grounds are the four, in declaration order, and the
    # canopy ground is the exclusion result's own gate.
    grounds = step_orchestrator.crossing_grounds(TREES, context, s.stored())
    assert [g["type"] for g in grounds] == ["production", "water", "road", "canopy"], [g["type"] for g in grounds]
    canopy_gate = next(w for w in exclusion["wire"]["layers"] if w["type"] == "canopy")
    assert canopy_gate["data_available"] is True, "canopy is mandatory: the ground is always present"
    assert grounds[3]["label"] == canopy_gate["label"]
    assert grounds[3]["polygon_utm"].equals(exclusion["layers"]["canopy"]["polygon_utm"])
    assert grounds[0]["polygon_utm"].equals(unary_union([p["render_fill_polygon_utm"] for p in production_patches]))
    assert grounds[1]["polygon_utm"].equals(water_union["render_fill_polygon_utm"])
    assert grounds[2]["polygon_utm"].equals(road_network["cell_footprint_polygon_utm"])

    # FOUR DRAWN ZONES, one over each ground, built off the committed
    # geometry itself so they cross by construction.
    canopy_box = box(
        *pixel_center_xy(context.dem, CANOPY_ROWS[1] - 1, CANOPY_COLS[0]),
        *pixel_center_xy(context.dem, CANOPY_ROWS[0], CANOPY_COLS[1] - 1),
    )
    assert exclusion["layers"]["canopy"]["polygon_utm"].intersects(canopy_box)
    road_strip = road_network["cell_footprint_polygon_utm"].buffer(6.0).intersection(
        box(
            road_network["cell_footprint_polygon_utm"].representative_point().x - 45,
            road_network["cell_footprint_polygon_utm"].representative_point().y - 45,
            road_network["cell_footprint_polygon_utm"].representative_point().x + 45,
            road_network["cell_footprint_polygon_utm"].representative_point().y + 45,
        )
    ).intersection(context.boundary_polygon_utm.buffer(-2.0))
    road_strip = _largest(road_strip)
    zones = {
        "drawn-production": _drawn("drawn-production", _box_around(grounds[0]["polygon_utm"], 15)),
        "drawn-water": _drawn("drawn-water", _box_around(grounds[1]["polygon_utm"], 15)),
        "drawn-road": _drawn("drawn-road", _ring_wgs84(road_strip.simplify(0.5))),
        "drawn-canopy": _drawn("drawn-canopy", _ring_wgs84(_largest(
            canopy_box.buffer(12).intersection(context.boundary_polygon_utm.buffer(-2.0))
        ))),
        # Hydric: a box INSIDE the hydric gate's own footprint, clipped to the
        # parcel -- 0.2 ac of ground the exclusion gate rejects outright.
        "drawn-hydric": _drawn("drawn-hydric", _ring_wgs84(_largest(
            box(
                exclusion["layers"]["hydric"]["polygon_utm"].representative_point().x - 15,
                exclusion["layers"]["hydric"]["polygon_utm"].representative_point().y - 15,
                exclusion["layers"]["hydric"]["polygon_utm"].representative_point().x + 15,
                exclusion["layers"]["hydric"]["polygon_utm"].representative_point().y + 15,
            ).intersection(context.boundary_polygon_utm.buffer(-2.0))
        ))),
    }
    # And a fifth, STEEP: the top-ranked generated candidate's own outline,
    # redrawn by hand -- ground the slope gate rejected.
    steep_source = s.context().step_proposals["trees"]["patches"][0]
    steep_polygon = steep_source["polygon_utm"] if steep_source["polygon_utm"].geom_type == "Polygon" else max(
        steep_source["polygon_utm"].geoms, key=lambda g: g.area
    )
    zones["drawn-steep"] = _drawn("drawn-steep", _ring_wgs84(steep_polygon.buffer(-0.5).simplify(0.5)))

    provenance = {feature_id: "user_added" for feature_id in zones}
    document = s.commit("trees", list(zones.values()), provenance)
    recorded = {
        f["id"]: f["properties"]["exclusion_crossings"]
        for f in document["steps"]["trees"]["features"]["features"]
    }
    types = {feature_id: [c["type"] for c in crossings] for feature_id, crossings in recorded.items()}

    # EACH GROUND IS RECORDED where it is crossed, with the declared label
    # and an acreage at or above the floor.
    for ground_type, feature_id in (
        ("production", "drawn-production"), ("water", "drawn-water"),
        ("road", "drawn-road"), ("canopy", "drawn-canopy"),
    ):
        assert ground_type in types[feature_id], (feature_id, recorded[feature_id])
        record = next(c for c in recorded[feature_id] if c["type"] == ground_type)
        assert record["acres"] >= commit_validation.CROSSING_MIN_ACRES
        assert record["label"] == {g.type: g.label for g in TREES.commit_contract.crossings}.get(ground_type) or (
            ground_type == "canopy" and record["label"] == canopy_gate["label"]
        )
    # ONLY THE FOUR TYPES EVER APPEAR.
    for feature_id, crossed in types.items():
        assert set(crossed) <= {"production", "water", "road", "canopy"}, (feature_id, crossed)
        assert "hydric" not in crossed and "slope" not in crossed and "setback" not in crossed and "roads" not in crossed

    # THE CONTROL: against the EXCLUSION gates the hydric and steep zones
    # WOULD have recorded hydric and slope -- so their absence above is a
    # declaration working, not ground that happened to be clear.
    hydric_patch = wire_translation.rehydrate_tree_zone(zones["drawn-hydric"], context.dem, zone_id=90)
    steep_patch = wire_translation.rehydrate_tree_zone(zones["drawn-steep"], context.dem, zone_id=91)
    gate_hydric = [c["type"] for c in commit_validation.exclusion_crossings(hydric_patch["polygon_utm"], exclusion)]
    gate_steep = [c["type"] for c in commit_validation.exclusion_crossings(steep_patch["polygon_utm"], exclusion)]
    assert "hydric" in gate_hydric, f"the hydric ring must cross the hydric gate for the control to mean anything: {gate_hydric}"
    assert "slope" in gate_steep, f"the steep ring must cross the slope gate for the control to mean anything: {gate_steep}"

    # The other three steps STILL record the exclusion gates: the same
    # hydric ring committed as a PRODUCTION zone records hydric.
    s2 = Session()
    s2.generate("landform")
    hydric_as_production = copy.deepcopy(zones["drawn-hydric"])
    hydric_as_production["properties"]["layer"] = wire_translation.LAYER_PRODUCTION_AREA
    landform_document = s2.commit("landform", [hydric_as_production], {"drawn-hydric": "user_added"})
    landform_types = [
        c["type"] for c in landform_document["steps"]["landform"]["features"]["features"][0]["properties"]["exclusion_crossings"]
    ]
    assert "hydric" in landform_types, landform_types

print(
    f"8 [test 8]. CROSSINGS: six drawn zones committed to trees. Recorded: "
    f"{ {k: v for k, v in types.items()} }. All four grounds recorded where crossed, at or above "
    f"{commit_validation.CROSSING_MIN_ACRES} ac; the hydric and steep zones record NOTHING for "
    f"hydric or slope, while the exclusion gates (the control) would have recorded "
    f"{gate_hydric} and {gate_steep} -- and the same hydric ring committed as a PRODUCTION zone "
    f"records {landform_types}. The canopy ground is the exclusion result's own gate, "
    f"data_available True."
)


# --- 9 [test 9]. THE THREE AVAILABILITY FLAGS SURVIVE TO THE WIRE ------
#
# On the interactive path every flag is True (section 2 asserted it on
# every feature and in summary.gates). The case the flags exist for is the
# other one: a factor that DEFAULTED to _NEUTRAL_FACTOR_VALUE because the
# data was unavailable, indistinguishable from a measured 0.5 without the
# flag. Built synthetically, since ParcelData's hard-fail contract means the
# session path cannot produce it.

_neutral = tree_zone_candidates._NEUTRAL_FACTOR_VALUE
_defaulted = {
    **GENERATED_PATCHES[0],
    "id": 77,
    "soil_marginality_factor": _neutral,
    "soil_marginality_data_available": False,
    "hydric_overlap_factor": _neutral,
    "hydric_data_available": False,
    "stream_proximity_factor": _neutral,
    "stream_data_available": False,
}
_measured = {**_defaulted, "id": 78, "soil_marginality_data_available": True, "hydric_data_available": True, "stream_data_available": True}
_wire = wire_translation.tree_zones_to_feature_collection([_defaulted, _measured])
_back = wire_translation.rehydrate_tree_zones(_wire, DEM)
assert _back[0]["soil_marginality_factor"] == _back[1]["soil_marginality_factor"] == _neutral
for flag in ("soil_marginality_data_available", "hydric_data_available", "stream_data_available"):
    assert _wire["features"][0]["properties"][flag] is False and _wire["features"][1]["properties"][flag] is True
    assert _back[0][flag] is False and _back[1][flag] is True, flag
assert "not available" in _wire["features"][0]["properties"]["confidence_notes"]
assert "not available" not in _wire["features"][1]["properties"]["confidence_notes"]
# And a build_trees_payload over such a result carries the gates block.
_payload = step_orchestrator.build_trees_payload(
    {
        "zones_geojson": _wire,
        "search_space_geojson": {"type": "FeatureCollection", "features": []},
        "narrative_data": tree_zone_candidates.build_narrative_data(
            [_defaulted, _measured], BOUNDARY_POLYGON_UTM, PARCEL_ACRES, 1.0, 2.0,
            soil_marginality_data_available=False, hydric_data_available=False,
            stream_data_available=False, existing_canopy_excluded=True,
        ),
    },
    {},
)
assert _payload["summary"]["gates"] == {
    "soil_marginality_data_available": False, "hydric_data_available": False, "stream_data_available": False,
}
assert _payload["summary"]["selection"]["factor_weights_pct"] == WEIGHTS

print(
    f"9 [test 9]. FLAGS: a patch whose three factors defaulted to the neutral {_neutral} with all "
    f"three flags False, and one measured at {_neutral} with all three True, go out and come back "
    f"with identical factors and DIFFERENT flags -- the two are distinguishable on the wire and "
    f"after rehydration only because the flags travel. The confidence note names the outage; "
    f"summary.gates carries the step-level flags; the factor weights ride beside them."
)


# --- 10 [test 10]. NO NETWORK during rehydration ----------------------
# A counter that also RAISES, not a stopwatch.

_connection_attempts = []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _forbidden(address, *args, **kwargs):
    _connection_attempts.append(address)
    raise AssertionError(f"rehydration opened a network connection to {address!r}")


socket.socket.connect = lambda self, address: _forbidden(address)
socket.socket.connect_ex = lambda self, address: _forbidden(address)
socket.create_connection = _forbidden
try:
    _guarded = wire_translation.rehydrate_tree_zones(OUTBOUND, DEM)
    _guarded.append(wire_translation.rehydrate_tree_zone(_drawn("g-1", HYDRIC_ZONE_RING), DEM, zone_id=1))
    _guarded.append(wire_translation.rehydrate_tree_zone(zones["drawn-road"], DEM, zone_id=2))
    _guarded.extend(
        wire_translation.rehydrate_tree_zones(
            {"type": "FeatureCollection", "features": list(zones.values())}, DEM, zone_ids=list(range(10, 16))
        )
    )
    _guarded.append(wire_translation.selected_road_network([road_network]))
    _guarded.append(wire_translation.production_zones_footprint(production_patches))
    _guarded.append(wire_translation.water_zone_footprint(water_union))
    _guarded.append(wire_translation.road_network_footprint(road_network))
    _guarded.append(wire_translation.water_zone_footprint(water_suitability.NO_WATER_ZONE))
    _guarded.append(wire_translation.road_network_footprint(road_corridors.NO_ROAD_CORRIDOR))
finally:
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    socket.create_connection = _real_create_connection

assert _connection_attempts == [], _connection_attempts
assert _guarded[-2] is None and _guarded[-1] is None, "the sentinels are 'no ground'"

print(
    f"10 [test 10]. NO NETWORK: {len(_guarded)} rehydrations and edge reductions (generated, drawn, "
    f"multi-zone, the road selection, the four footprints and both sentinels) under a socket "
    f"counter that raises -- {len(_connection_attempts)} connection attempts."
)

print(
    "\n11 [test 11]. REGRESSION: run the other test files separately -- test_step_registry.py, "
    "test_wire_translation.py, test_wire_translation_inbound.py, test_step_orchestrator.py, "
    "test_step_commit.py, test_water_step.py, test_roads_step.py, test_tree_zone_candidates.py, "
    "test_session_api.py, test_solar_suitability.py, test_fencing.py, test_render_layout_map.py."
)
print("\nAll trees step checks passed.")
