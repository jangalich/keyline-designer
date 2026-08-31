"""
test_water_step.py

THE WATER STEP -- stage 3's first step, and the first test of whether the
Step Registry's schema, built around ONE entry, generalises to a second.
Run as:

    python test_water_step.py

REAL COORDINATES, REAL PIPELINE CODE, THE SAME FIXTURE AS THE PRIOR
BRANCHES. The boundary is the actual drawn property from generate_full_
report.py -- 5614 N Montour Rd, Gibsonia, PA (~13.23 acres, UTM 17N) -- and
the DEM is test_step_orchestrator.py's own bench-and-drainage fixture,
unchanged. That fixture turns out to feed BOTH survey instruments without
being reshaped for them: the incised drainage gives the embankment surface
real catchment on a moderate grade, and the bench's flatter ground gives the
excavated surface wet, low-slope cells. Six survey zones come back, three of
each type, which is what makes a multi-select test over both types possible
against terrain that was not built to produce it.

water_survey_areas.identify_water_survey_areas(), session_manager.create_
session(), the terrain warm-up, the commit gate, the rehydrators and the
whole payload assembly all RUN, for real. What is mocked is the NETWORK and
only the network.

READ THIS BEFORE EDITING: water_candidate_zones.py is NOT this step.
Its own docstring says DEMOTED, NOT DELETED. Nothing here touches it.

Sections (the branch's numbered tests in brackets):
  1  [2]  GENERATE -- landform committed, then water generates. Both survey
          types, members, cross_type_overlaps and the step-level block.
  2  [3]  UPSTREAM NOT COMMITTED -- 409 BEFORE a job is created, naming
          landform and its status; NO job id issued.
  3  [4]  MULTI-SELECT COMMIT -- three zones spanning both types.
  4  [5]  THE UNION -- those three reach tree_zone_candidates.identify_tree_
          zone_candidates() as ONE geometry. A real consumer, not a mock.
  5  [6]  EMPTY COMMIT -> NO_WATER_ZONE -- the sentinel reaches the consumer
          and the self-compute fallback did NOT run. THE ONE THAT MATTERS.
  6  [7]  ID STABILITY -- two generates, a cache eviction, a third.
  7  [8]  ID UNIQUENESS across both survey types.
  8  [9]  SENTINELS -- None survives the payload, distinct from 0.0.
  9  [10] cross_type_overlaps is unchanged by which zones are committed.
"""

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
import session_cache
import session_manager
import step_orchestrator
import step_registry
import tree_zone_candidates
import valley_delineation
import water_suitability
import water_survey_areas
import wire_translation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- the real property, verbatim from B2, B4, B5a and B5b -------------

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

# --- the DEM fixture, verbatim from test_step_orchestrator.py ---------

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
    """A 4% bench with one incised drainage down CHANNEL_COL -- B5a's
    fixture, not a water-specific one. The drainage carries the flow
    accumulation the embankment surface wants; the bench carries the flat,
    wet ground the excavated surface wants."""
    rows = np.arange(ROWS)[:, None].astype(np.float32)
    cols = np.arange(COLS)[None, :].astype(np.float32)
    array = 300.0 + 0.20 * rows + 0.05 * cols
    array -= 9.0 * np.exp(-((cols - CHANNEL_COL) ** 2) / (2 * 3.0 ** 2))
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
FIXTURE_KSAT = [{"mukey": "111111", "ksat_r": "9.0"}]


def _build_parcel_data(_boundary=None) -> ParcelData:
    dem = _build_dem()
    return ParcelData(
        dem=dem,
        boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
        soil_components=HYDRIC_COMPONENTS,
        farmland_classification=[],
        erosion_factor=[],
        # A REAL ksat row rather than B5a's empty list: the water step's soil
        # criterion is assembled from all three soil pieces or none, and an
        # empty ksat list would still assemble (it is not None) but would
        # score every map unit as unknown -- the criterion would be
        # technically "checked" and say nothing. One row makes soil_checked
        # true AND meaningful.
        saturated_hydraulic_conductivity=FIXTURE_KSAT,
        soil_geometries=HYDRIC_GEOMETRIES,
        water_features={"features": []},
        farm_roads=FIXTURE_ROADS,
        climate_summary={},
        elevation_grid=[],
        canopy_height=_build_canopy(dem),
        imagery_summary={},
        irradiance={"status": "ok"},
    )


class Harness:
    """
    B5a/B5b's harness with the water step's own boundaries added. Every
    network call is mocked; every real computation is wrapped (wraps=) so it
    RUNS and is COUNTED. An assertion that a count is zero only means
    something if a nonzero count was reachable.
    """

    def __enter__(self):
        self._stack = ExitStack()
        patch = self._stack.enter_context

        self.fetch_parcel_data = patch(
            mock_patch.object(
                parcel_data, "fetch_parcel_data", side_effect=_build_parcel_data
            )
        )
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
                farm_roads_data, "get_farm_roads_for_boundary",
                return_value=FIXTURE_ROADS,
            )
        )
        self.roads_helper_refetch = patch(
            mock_patch.object(
                production_area, "_fetch_road_exclusion_union_utm",
                wraps=production_area._fetch_road_exclusion_union_utm,
            )
        )
        # THE WATER STEP'S OWN STANDALONE SOIL FETCH -- three SDA calls it
        # makes when soil_inputs= is not forwarded. Counted at zero, which is
        # what the registry's `combine` edge exists to close.
        self.water_soil_fetch = patch(
            mock_patch.object(
                water_survey_areas, "_fetch_soil_inputs",
                side_effect=AssertionError(
                    "_fetch_soil_inputs() must not run: the registry forwards "
                    "soil_inputs off ParcelData"
                ),
            )
        )
        self.water_road_fetch = patch(
            mock_patch.object(
                water_survey_areas, "_fetch_road_exclusion_union_utm",
                side_effect=AssertionError(
                    "the water step must not fetch roads: the registry "
                    "forwards the warm-up's own union"
                ),
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
        self.identify_production = patch(
            mock_patch.object(
                production_area_ceiling, "identify_optimized_production_areas",
                wraps=production_area_ceiling.identify_optimized_production_areas,
            )
        )
        # THE STEP'S OWN GENERATE, patched on ITS module (what
        # step_registry.resolve() looks up at call time).
        self.identify_water = patch(
            mock_patch.object(
                water_survey_areas, "identify_water_survey_areas",
                wraps=water_survey_areas.identify_water_survey_areas,
            )
        )
        # THE MEASUREMENT SECTION 5 TURNS ON. Every downstream consumer of a
        # selected_water_zone override reacts to None by re-running the WHOLE
        # water-suitability pipeline. This binding is tree_zone_candidates'
        # own; counted at zero it says the sentinel was believed.
        self.tree_water_selfcompute = patch(
            mock_patch.object(
                tree_zone_candidates, "identify_water_suitability",
                wraps=tree_zone_candidates.identify_water_suitability,
            )
        )
        self.rehydrate_water = patch(
            mock_patch.object(
                wire_translation, "rehydrate_water_survey_zones",
                wraps=wire_translation.rehydrate_water_survey_zones,
            )
        )
        self.water_union = patch(
            mock_patch.object(
                wire_translation, "water_zone_union",
                wraps=wire_translation.water_zone_union,
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
            + self.roads_helper_refetch.call_count
        )


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="water_step_test_"))


def _fresh_runner():
    return job_runner.JobRunner(max_workers=2, max_jobs=32)


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

    def job(self, step_id):
        return step_orchestrator.generate_step(
            self.id, step_id, self.store, fetch_cache=self.fetch_cache,
            cache=self.cache, runner=self.runner,
        )

    def generate(self, step_id):
        job = self.job(step_id).wait(timeout=900)
        if job.status != job_runner.STATUS_DONE:
            raise AssertionError(f"{step_id} generate failed: {job.error} ({job.exception!r})")
        return job.result["payload"]

    def commit(self, step_id, features, provenance, base_revision):
        return step_orchestrator.commit_step(
            self.id, step_id, {"type": "FeatureCollection", "features": list(features)},
            provenance, base_revision, self.store,
            fetch_cache=self.fetch_cache, cache=self.cache,
        )

    def context(self):
        return session_manager.get_session_context(
            self.id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def stored(self):
        return self.store.get(self.id)

    def commit_landform(self):
        """Generate landform and commit every proposal -- the upstream state
        the water step requires before it can be generated at all."""
        payload = self.generate("landform")
        features = payload["suggested_zones"]["features"]
        assert features, "the fixture must produce production zones to commit"
        return self.commit(
            "landform", features, {f["id"]: "generated" for f in features},
            base_revision=self.stored()["steps"]["landform"].get("revision", 0),
        )


def _zone_features(payload) -> list:
    """The committable survey-zone features off a water payload -- the zone
    envelopes only. Member footprints ride the same collection on their own
    layers and are NOT selectable."""
    return [
        feature
        for feature in payload["survey_zones"]["features"]
        if feature["properties"]["layer"] in wire_translation.LAYER_SURVEY_ZONES
    ]


def _member_features(payload) -> list:
    return [
        feature
        for feature in payload["survey_zones"]["features"]
        if feature["properties"]["layer"].startswith("survey_zone_member_")
    ]


print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} "
    f"vertices, {PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at "
    f"{RESOLUTION_METERS:.0f} m. Same boundary and same DEM fixture as B5a/B5b.\n"
)


# --- 1 [test 2]. GENERATE, LANDFORM COMMITTED -------------------------

with Harness() as h:
    s = Session()
    s.commit_landform()
    # The counts are taken AROUND the water generate: session creation
    # legitimately fetches ParcelData once, and a zero that included it would
    # be measuring the wrong window.
    network_before = h.total_network_calls
    water_payload = s.generate("water")
    water_generate_network = h.total_network_calls - network_before

    assert sorted(water_payload) == ["gate_mask_stats", "summary", "survey_zones", "zones"], (
        f"the water payload's keys: {sorted(water_payload)}"
    )
    collection = water_payload["survey_zones"]
    assert collection["type"] == "FeatureCollection"

    ZONES = _zone_features(water_payload)
    MEMBERS = _member_features(water_payload)
    assert ZONES, (
        "the fixture terrain produced no survey zones; every assertion below "
        "would be vacuously true"
    )
    assert MEMBERS, "members must ride the wire as their own features"
    assert len(collection["features"]) == len(ZONES) + len(MEMBERS), (
        "the collection is zone envelopes plus member footprints and nothing "
        "else -- a dropped zone is not in the pipeline output"
    )

    # BOTH SURVEY TYPES, from ONE generate call. This is the claim the
    # registry entry makes by declaring one generate target: SURVEY_TYPES
    # drives the per-type logic inside the module, and the two surfaces come
    # back together with survey_type on each zone.
    by_type = {}
    for feature in ZONES:
        by_type.setdefault(feature["properties"]["survey_type"], []).append(feature)
    assert set(by_type) == set(water_survey_areas.SURVEY_TYPES), (
        f"one generate must return BOTH survey types: {sorted(by_type)}"
    )
    assert h.identify_water.call_count == 1, (
        f"ONE entry point, ONE call -- not one per survey type: "
        f"{h.identify_water.call_count}"
    )

    # The layer carries the type, which is what the frontend styles on.
    for survey_type, features in by_type.items():
        for feature in features:
            assert feature["properties"]["layer"] == f"survey_zone_{survey_type}"
    for feature in MEMBERS:
        assert feature["properties"]["layer"] == (
            f"survey_zone_member_{feature['properties']['survey_type']}"
        )
        assert feature["properties"]["zone_id"] is not None, (
            "a member links back to its parent zone both ways"
        )

    # THE PER-FEATURE HALF -- water_survey_areas._zone_feature_properties()'s
    # full measurement contract, named here so a payload that drops one fails
    # with the name of what was lost.
    ZONE_PROPERTIES = (
        "zone_id", "survey_type", "status", "drop_reason", "rank",
        "sparse_anchor", "cross_type_overlaps", "member_ids", "member_count",
        "member_acres", "zone_acres", "mean_suitability", "max_suitability",
        "criterion_contributions", "twi_percentile_mean",
        "depression_depth_max_m", "slope_median_pct",
        "boundary_adjacency_fraction", "canopy_overlap_pct", "road_overlap_pct",
        "production_overlap_pct", "primary_production_area_relationship",
        "has_service_relationship", "served_production_area_ids",
        "soil_coverage_fraction", "criteria_complete", "flags",
        "representative_elevation_m",
    )
    for feature in ZONES:
        missing = [key for key in ZONE_PROPERTIES if key not in feature["properties"]]
        assert not missing, f"zone {feature['id']} is missing {missing}"
        assert feature["properties"]["status"] == water_survey_areas.ZONE_STATUS_NOMINATED

    # cross_type_overlaps -- the agreement report between the two survey
    # instruments, on the wire and intact.
    assert any(feature["properties"]["cross_type_overlaps"] for feature in ZONES), (
        "the two instruments must find SOME common ground on this fixture, or "
        "section 9's invariance assertion is vacuous"
    )
    for feature in ZONES:
        for entry in feature["properties"]["cross_type_overlaps"]:
            assert set(entry) == {"zone_id", "fraction"}, entry

    # THE STEP-LEVEL BLOCK -- build_narrative_data()'s own, whole.
    summary = water_payload["summary"]
    for key in (
        "zone_found", "zone_count", "dropped_count", "member_region_count",
        "embankment_zone_count", "excavated_zone_count", "suitability_threshold",
        "grouping_distance_meters", "twi_is_parcel_relative", "twi_note",
        "gates", "soil_checked", "selection",
    ):
        assert key in summary, f"the step-level block is missing {key}: {sorted(summary)}"
    assert "zones" not in summary, (
        "the tabular rows live at payload['zones']; carrying them twice would "
        "be two copies of the same reduction"
    )
    assert summary["zone_count"] == len(ZONES)
    assert summary["soil_checked"] is True, (
        "the registry's soil_inputs edge must reach the scorer -- soil_checked "
        "False here means the combine forwarded nothing"
    )
    assert len(water_payload["zones"]) == len(ZONES), (
        "the tabular half and the map half describe the SAME zones"
    )

    # ZERO NETWORK. The point of the six forwarded edges.
    assert water_generate_network == 0, (
        f"the water generate must touch no network: {water_generate_network} "
        f"call(s). The AssertionError side effects on the water module's own "
        f"soil/road/DEM fetches would have fired first if an override were "
        f"missing."
    )

    # The document moved, and carries NO features.
    entry = s.stored()["steps"]["water"]
    assert entry["status"] == design_document.STATUS_GENERATED, entry["status"]
    assert not entry.get("features", {}).get("features"), (
        "a generate records proposals in the session cache, never in the document"
    )

print(
    f"1 [test 2]. GENERATE: landform committed, then water generated in ONE "
    f"call ({h.identify_water.call_count}) returning {len(ZONES)} survey zones "
    f"across BOTH types ("
    f"{', '.join(f'{t}: {len(v)}' for t, v in sorted(by_type.items()))}) plus "
    f"{len(MEMBERS)} member features on their own layers, every zone carrying "
    f"the full {len(ZONE_PROPERTIES)}-field measurement contract and its "
    f"cross_type_overlaps, beside the step-level block "
    f"({len(summary)} keys, soil_checked={summary['soil_checked']}, "
    f"threshold {summary['suitability_threshold']}). "
    f"{water_generate_network} network calls during the generate; the "
    f"document says 'generated' and holds no features."
)


# --- 2 [test 3]. UPSTREAM NOT COMMITTED -------------------------------
#
# THE FIX THIS BRANCH OWES B7. UpstreamNotCommittedError is raised inside
# assemble_consumes(), which runs on the JOB'S THREAD -- so a water generate
# asked for before landform was committed became a FAILED JOB carrying
# "Water survey areas could not be generated", which says the parcel's data
# failed when the truth was "commit landform first". Nothing hit it while
# landform was the only entry, because landform consumes no commit.
#
# generate_step() now resolves the committed edges SYNCHRONOUSLY, through the
# registry's own walk, before it submits. The assertion that matters is that
# NO JOB ID WAS ISSUED: a 202 against work that was never accepted is the
# thing being fixed, and an exception that arrived after a job existed would
# leave the client polling a job for an answer the server already had.

with Harness() as h:
    s = Session()
    runner_jobs_before = len(s.runner)

    # NOT COMMITTED AT ALL -- landform is not_started.
    try:
        s.job("water")
        raise AssertionError("a water generate with landform uncommitted must refuse")
    except step_orchestrator.UpstreamNotCommittedError as exc:
        not_started_refusal = exc
    assert not_started_refusal.step_id == "water"
    assert not_started_refusal.upstream_step == "landform"
    assert not_started_refusal.upstream_status == design_document.STATUS_NOT_STARTED, (
        not_started_refusal.upstream_status
    )
    assert not_started_refusal.consumed_name == "production_areas"
    assert "landform" in str(not_started_refusal)

    # GENERATED BUT NOT COMMITTED -- the more interesting case, because the
    # step has proposals and looks ready. A generated landform is still an
    # undecided one, and water is not computable from a proposal.
    s.generate("landform")
    try:
        s.job("water")
        raise AssertionError("a generated-but-uncommitted upstream must still refuse")
    except step_orchestrator.UpstreamNotCommittedError as exc:
        generated_refusal = exc
    assert generated_refusal.upstream_status == design_document.STATUS_GENERATED, (
        generated_refusal.upstream_status
    )

    # NO JOB WAS CREATED by either refusal.
    assert len(s.runner) == runner_jobs_before + 1, (
        f"exactly one job (the landform generate) may exist here: the two "
        f"refused water generates must not have created any. "
        f"{len(s.runner) - runner_jobs_before} were created."
    )

    # AND THE STEP'S STATUS DID NOT MOVE.
    assert s.stored()["steps"]["water"]["status"] == design_document.STATUS_NOT_STARTED

    # THE HTTP MAPPING IS ALREADY DECLARED: 409, carrying the upstream step
    # and its status so the client's next action is nameable.
    import session_api
    status_codes = {
        exception: code for exception, code, _ in session_api._API_ERRORS
    }
    assert status_codes[step_orchestrator.UpstreamNotCommittedError] == 409, (
        "an upstream commit that is not there is a conflict with the "
        "session's state, not a bad request"
    )

    # ONCE LANDFORM IS COMMITTED it resolves, with no change to anything else.
    s.commit_landform()
    accepted = s.job("water")
    assert accepted.id, "a job is issued once the upstream commit is there"
    assert accepted.wait(timeout=900).status == job_runner.STATUS_DONE

print(
    f"2 [test 3]. UPSTREAM NOT COMMITTED: a water generate with landform "
    f"'{not_started_refusal.upstream_status}' and again with landform "
    f"'{generated_refusal.upstream_status}' each raised "
    f"UpstreamNotCommittedError naming step 'landform', the edge "
    f"'{not_started_refusal.consumed_name}' and the status -- synchronously, "
    f"with NO job id issued (0 jobs created by the two refusals) and water "
    f"still not_started. session_api maps it to "
    f"{status_codes[step_orchestrator.UpstreamNotCommittedError]}. With "
    f"landform committed the same call was accepted and ran to done."
)


# --- 3 [test 4]. MULTI-SELECT COMMIT ----------------------------------

with Harness() as h:
    s = Session()
    s.commit_landform()
    payload = s.generate("water")
    zones = _zone_features(payload)

    # THREE ZONES SPANNING BOTH TYPES -- the product decision this step
    # exists to serve. Picked as the first embankment plus the first two
    # excavated (or whatever the fixture offers), never a same-type triple.
    embankment = [f for f in zones if f["properties"]["survey_type"] == "embankment"]
    excavated = [f for f in zones if f["properties"]["survey_type"] == "excavated"]
    assert len(embankment) >= 1 and len(excavated) >= 2, (
        f"the fixture must offer a cross-type triple: {len(embankment)} "
        f"embankment, {len(excavated)} excavated"
    )
    SELECTED = [embankment[0], excavated[0], excavated[1]]
    SELECTED_IDS = [f["id"] for f in SELECTED]

    document = s.commit(
        "water", SELECTED, {f["id"]: "generated" for f in SELECTED},
        base_revision=s.stored()["steps"]["water"].get("revision", 0),
    )

    entry = document["steps"]["water"]
    assert entry["status"] == design_document.STATUS_COMMITTED
    assert [f["id"] for f in entry["features"]["features"]] == SELECTED_IDS, (
        f"all three committed zones must be in the document: "
        f"{[f['id'] for f in entry['features']['features']]}"
    )
    assert {f["properties"]["survey_type"] for f in entry["features"]["features"]} == {
        "embankment", "excavated"
    }, "the selection spans both survey types"

    # A MEMBER FOOTPRINT IS NOT SELECTABLE, and neither is a feature with no
    # pipeline id. Water is select-only: there is nothing to allocate an id
    # for, so the rehydrator refuses rather than inventing one.
    member = _member_features(payload)[0]
    try:
        s.commit(
            "water", [member], {member["id"]: "generated"},
            base_revision=s.stored()["steps"]["water"].get("revision", 0),
        )
        raise AssertionError("a member footprint must not be committable")
    except Exception as exc:
        member_rejection = exc
    assert type(member_rejection).__name__ == "CommitRejectedError", member_rejection
    assert any(
        r.code == "wrong_layer" for r in member_rejection.rejections
    ), [r.code for r in member_rejection.rejections]

print(
    f"3 [test 4]. MULTI-SELECT COMMIT: committed {len(SELECTED_IDS)} zones "
    f"spanning both survey types ({SELECTED_IDS}) at once. The document "
    f"carries all three, with min_features=0 and no ceiling in the contract. "
    f"A member footprint offered for commit was rejected by layer -- members "
    f"ride the wire but are not selectable."
)


# --- 4 [test 5]. THE UNION, AGAINST A REAL CONSUMER -------------------
#
# A SYNTHETIC TREES ENTRY, for test_step_commit.py section 12's reason: the
# union and the empty-commit sentinel are properties of the edge INTO water's
# consumers, and the roads/trees entries are a later branch's. So the edge is
# declared here, over the REAL rehydrator, the REAL combine and the REAL
# generate target -- which is also the assertion that a consumer of the water
# commit is a ROW IN THE TABLE and nothing else.

SYNTHETIC_TREES = step_registry.StepDefinition(
    step_id="trees",
    consumes=(
        step_registry.Consumed(
            name="selected_water_zone",
            source=step_registry.SOURCE_COMMITTED,
            from_step="water",
            rehydrate="wire_translation.rehydrate_water_survey_zones",
            # THE UNION RULE, DECLARED AS DATA.
            combine="wire_translation.water_zone_union",
            # THE SENTINEL. Section 5 is what this line is for.
            empty_commit="water_suitability.NO_WATER_ZONE",
            forward_as="selected_water_zone",
            why="Selected water ground is claimed ground: trees do not go on it.",
        ),
        step_registry.Consumed(
            name="production_areas",
            source=step_registry.SOURCE_COMMITTED,
            from_step="landform",
            rehydrate="wire_translation.rehydrate_production_zones",
            forward_as="production_areas",
            why="Committed production ground is claimed ground.",
        ),
        step_registry.Consumed(
            name="dem", source=step_registry.SOURCE_CACHE,
            cache_path="dem", forward_as="dem", why="ParcelData's own grid.",
        ),
        step_registry.Consumed(
            name="boundary_coordinates", source=step_registry.SOURCE_CACHE,
            cache_path="boundary", forward_as="boundary_coordinates",
            why="The parcel ring.",
        ),
        step_registry.Consumed(
            name="boundary_polygon_utm", source=step_registry.SOURCE_CACHE,
            cache_path="boundary_polygon_utm", forward_as="boundary_polygon_utm",
            why="The parcel ring in UTM.",
        ),
        step_registry.Consumed(
            name="canopy_height", source=step_registry.SOURCE_CACHE,
            cache_path="parcel_data.canopy_height", forward_as="canopy_height",
            why="ParcelData's HAG layer.",
        ),
    ),
    generate="tree_zone_candidates.identify_tree_zone_candidates",
    payload="step_orchestrator.build_water_payload",
    proposal_collection="tree_zones",
    produces=("tree_zones",),
    commit_contract=step_registry.CommitContract(
        layers=("tree_zone_candidate",),
        geometry_types=("Polygon", "MultiPolygon"),
        min_features=0,
        max_features=None,
        rehydrate="wire_translation.rehydrate_production_zones",
    ),
)

with Harness() as h, mock_patch.dict(
    step_registry.STEP_REGISTRY, {"trees": SYNTHETIC_TREES}
):
    assert step_registry.dependents_of("water") == ("trees",), (
        "the consumes edge IS the invalidation edge"
    )
    assert step_registry.transitive_dependents("landform") == ("water", "trees"), (
        "staleness is transitive: reopening landform makes the tree proposals "
        "stale too, because they were computed from a water answer that was "
        "itself computed from the landform commit"
    )

    s = Session()
    s.commit_landform()
    payload = s.generate("water")
    zones = _zone_features(payload)
    embankment = [f for f in zones if f["properties"]["survey_type"] == "embankment"]
    excavated = [f for f in zones if f["properties"]["survey_type"] == "excavated"]
    SELECTED = [embankment[0], excavated[0], excavated[1]]
    s.commit(
        "water", SELECTED, {f["id"]: "generated" for f in SELECTED},
        base_revision=s.stored()["steps"]["water"].get("revision", 0),
    )

    assembled = step_orchestrator.assemble_consumes(
        SYNTHETIC_TREES, s.context(), s.stored()
    )
    resolved = assembled["selected_water_zone"]

    # ONE ZONE-SHAPED VALUE, not a list -- which is the whole of what the
    # consumer signature requires and the whole of what `combine` does.
    assert isinstance(resolved, dict), type(resolved)
    assert resolved["zone_ids"] == [
        wire_translation.internal_water_survey_zone_id(f["id"]) for f in SELECTED
    ], resolved["zone_ids"]
    assert set(resolved["survey_types"]) == {"embankment", "excavated"}

    # THE GEOMETRY IS THE UNION OF ALL THREE, measured rather than asserted:
    # every committed zone's own envelope lies inside it, and its area is the
    # union's (the three do not overlap on this fixture, so it is their sum).
    from shapely.ops import unary_union as _unary_union
    per_zone = wire_translation.rehydrate_water_survey_zones(
        {"type": "FeatureCollection", "features": SELECTED}, s.context().dem
    )
    expected = _unary_union([z["render_fill_polygon_utm"] for z in per_zone])
    union = resolved["render_fill_polygon_utm"]
    assert union.equals(expected), "the combine must union every selected zone"
    for zone in per_zone:
        assert union.contains(zone["render_fill_polygon_utm"].buffer(-0.01)), (
            f"zone {zone['id']}'s envelope is not inside the union"
        )
    assert union.area > max(z["render_fill_polygon_utm"].area for z in per_zone), (
        "a union of three disjoint zones must exceed the largest of them -- "
        "otherwise `combine` silently picked one"
    )

    # WHAT THE UNION DELIBERATELY DOES NOT CARRY. A union is not a zone, and
    # a fabricated id/acreage/elevation would read as measured.
    for invented in ("id", "survey_type", "rank", "zone_acres",
                     "mean_suitability", "representative_elevation_m"):
        assert invented not in resolved, (
            f"the union must not carry {invented!r}: no suitability surface "
            f"nominated this object, so any value for it would be invented. "
            f"A consumer reaching for it must get a KeyError, loudly."
        )

    # AND IT REACHES A REAL CONSUMER, RUN FOR REAL. Not a mock: identify_tree_
    # zone_candidates() is called through the registry's own forwarded
    # arguments and its search space actually excludes the unioned ground.
    tree_result = SYNTHETIC_TREES.resolve_generate()(
        **step_orchestrator.forwarded_arguments(SYNTHETIC_TREES, assembled, {})
    )
    assert h.tree_water_selfcompute.call_count == 0, (
        f"the consumer received a real water answer and must NOT have re-run "
        f"the water pipeline: {h.tree_water_selfcompute.call_count} run(s)"
    )
    # The union is claimed ground: no tree zone may sit on it. Measured
    # against the render fill, which is the geometry the consumer subtracts.
    assert tree_result["patches"], (
        "the consumer must actually site tree zones, or the overlap "
        "assertion below is vacuous"
    )
    for patch_zone in tree_result["patches"]:
        overlap = patch_zone["render_fill_polygon_utm"].intersection(union).area
        assert overlap < 1.0, (
            f"tree zone {patch_zone['id']} overlaps the committed water union "
            f"by {overlap:.2f} m^2 -- the union did not reach the consumer as "
            f"claimed ground"
        )

print(
    f"4 [test 5]. UNION: three committed zones across both types resolved to "
    f"ONE zone-shaped value (zone_ids {resolved['zone_ids']}, survey_types "
    f"{resolved['survey_types']}) whose render_fill_polygon_utm equals the "
    f"union of all three ({union.area:.0f} m^2 vs "
    f"{max(z['render_fill_polygon_utm'].area for z in per_zone):.0f} m^2 for "
    f"the largest alone) and which carries NO invented id, acreage, rank or "
    f"elevation. tree_zone_candidates.identify_tree_zone_candidates() -- the "
    f"REAL consumer, run for real -- took it without re-running the water "
    f"pipeline (0 runs) and sited {len(tree_result['patches'])} tree "
    f"zone(s), none of them on the unioned ground."
)


# --- 5 [test 6]. EMPTY COMMIT -> NO_WATER_ZONE ------------------------
#
# THE ONE THAT MATTERS MOST, and the first time the sentinel path has ever
# run for real. A user who commits the water step with nothing selected has
# DECIDED there is no water system on this parcel. Every downstream consumer
# reads None as "not supplied, go compute it" and reacts by re-running the
# whole water pipeline -- measured at five water-suitability runs across one
# build_pipeline_context() -- and handing back a zone the user rejected. The
# result looks right and is wrong, which is the worst available outcome.
#
# TWO READS, AND THE FIRST ONE IS THE TRAP. commit_step() writes the gate's
# rehydrated list into SessionContext.step_committed under the new revision,
# and for a zero-feature commit that list is []. A resolver that consulted
# the cache before checking the feature count would serve [] on the WARM read
# and the sentinel only after an eviction -- the sentinel would fire on a
# cold cache and nowhere else. So both reads are asserted, in that order.

with Harness() as h, mock_patch.dict(
    step_registry.STEP_REGISTRY, {"trees": SYNTHETIC_TREES}
):
    s = Session()
    s.commit_landform()
    s.generate("water")

    empty_document = s.commit(
        "water", [], {},
        base_revision=s.stored()["steps"]["water"].get("revision", 0),
    )

    # COMMITTED, NOT not_started. An empty commit is a decision the document
    # records, and design_document.py's governing distinction is exactly this.
    entry = empty_document["steps"]["water"]
    assert entry["status"] == design_document.STATUS_COMMITTED, entry["status"]
    assert entry["features"]["features"] == [], entry["features"]

    # READ 1 -- WARM. The context is the one the commit just wrote to.
    warm_context = s.context()
    assert "water" in warm_context.step_committed, (
        "the commit must have cached something for water, or this read is not "
        "the trap it is meant to be"
    )
    assert warm_context.step_committed["water"]["value"] == [], (
        "the cached rehydration of a zero-feature commit is [] -- which is "
        "precisely the value that must NOT reach a consumer"
    )
    warm = step_orchestrator.assemble_consumes(
        SYNTHETIC_TREES, warm_context, empty_document
    )["selected_water_zone"]
    assert warm is water_suitability.NO_WATER_ZONE, (
        f"a WARM read of an empty water commit must be the sentinel, not "
        f"{warm!r}. The commit cached [] under this revision; serving that "
        f"would make the sentinel fire only on a cold cache."
    )

    # READ 2 -- COLD. Evict the session and rebuild it from the document.
    assert s.cache.discard(s.id), "the session should have been cached"
    assert s.id not in s.cache
    cold_context = s.context()
    assert "water" not in cold_context.step_committed, "the eviction must be real"
    cold = step_orchestrator.assemble_consumes(
        SYNTHETIC_TREES, cold_context, s.stored()
    )["selected_water_zone"]
    assert cold is water_suitability.NO_WATER_ZONE, f"cold read: {cold!r}"

    # NEVER None, AND NOT MERELY FALSY. `is` identity, because the whole
    # mechanism is identity comparison at the consumer.
    assert warm is not None and cold is not None
    assert warm is cold

    # THE UNION IS NOT CONSULTED at all on this path -- the sentinel is
    # returned before any rehydration or combination happens.
    assert h.water_union.call_count == 0, (
        f"an empty commit must short-circuit ahead of the combine: "
        f"{h.water_union.call_count} union(s) built"
    )

    # AND THE CONSUMER BELIEVES IT. identify_tree_zone_candidates() run for
    # real with the sentinel forwarded: the water self-compute fallback must
    # not fire. THIS IS THE MEASUREMENT -- a None slipping through would run
    # the whole water-suitability pipeline here and produce a plausible wrong
    # answer with nothing raising.
    selfcompute_before = h.tree_water_selfcompute.call_count
    assembled = step_orchestrator.assemble_consumes(
        SYNTHETIC_TREES, s.context(), s.stored()
    )
    assert assembled["selected_water_zone"] is water_suitability.NO_WATER_ZONE
    empty_tree_result = SYNTHETIC_TREES.resolve_generate()(
        **step_orchestrator.forwarded_arguments(SYNTHETIC_TREES, assembled, {})
    )
    selfcompute_runs = h.tree_water_selfcompute.call_count - selfcompute_before
    assert selfcompute_runs == 0, (
        f"the consumer re-ran the water pipeline {selfcompute_runs} time(s). "
        f"The sentinel was not believed, and the tree zones just computed were "
        f"sited around a water zone the user explicitly rejected."
    )

    # AND THE ANSWER DIFFERS from the three-zone case in the right direction:
    # with no water ground claimed, the search space is larger.
    assert empty_tree_result["claimed_acres"] < tree_result["claimed_acres"], (
        f"an empty water commit claims LESS ground than a three-zone one: "
        f"{empty_tree_result['claimed_acres']} vs {tree_result['claimed_acres']}"
    )

    # THE CONTROL. A None forwarded in the sentinel's place DOES re-run the
    # pipeline -- which is what makes the zero above a measurement rather
    # than a path that never runs.
    control_before = h.tree_water_selfcompute.call_count
    control_arguments = dict(
        step_orchestrator.forwarded_arguments(SYNTHETIC_TREES, assembled, {})
    )
    control_arguments["selected_water_zone"] = None
    SYNTHETIC_TREES.resolve_generate()(**control_arguments)
    control_runs = h.tree_water_selfcompute.call_count - control_before
    assert control_runs > 0, (
        "forwarding None must reach the self-compute fallback -- if it does "
        "not, the zero above proves nothing"
    )

print(
    f"5 [test 6]. EMPTY COMMIT -> SENTINEL: the water step committed with "
    f"ZERO features is 'committed' (not not_started), and the commit cached "
    f"[] under the new revision. Both the WARM read (through that cache) and "
    f"the COLD read (after eviction) returned "
    f"water_suitability.NO_WATER_ZONE by identity -- never None, never [] -- "
    f"with the union never built ({h.water_union.call_count} calls). "
    f"tree_zone_candidates.identify_tree_zone_candidates(), run for real on "
    f"the sentinel, re-ran the water pipeline {selfcompute_runs} times and "
    f"claimed {empty_tree_result['claimed_acres']} ac against "
    f"{tree_result['claimed_acres']} ac for the three-zone commit. THE "
    f"CONTROL: forwarding None in its place re-ran it {control_runs} time(s), "
    f"so the zero is a measurement and not an unreachable path."
)


# --- 6 [test 7]. ID STABILITY -----------------------------------------
#
# AN ID-BASED SELECTION BREAKS SILENTLY IF A REBUILD RENUMBERS. The commit
# carries geometry, so a renumber would not corrupt the committed ground --
# but the REOPEN RESTORE matches committed feature ids against a fresh
# generate's proposals, and a shifted id set would silently restore the wrong
# selection. Three generates: two warm, then one after the session cache is
# evicted and the whole context rebuilt from the document.

with Harness() as h:
    s = Session()
    s.commit_landform()

    first = [f["id"] for f in _zone_features(s.generate("water"))]
    second = [f["id"] for f in _zone_features(s.generate("water"))]

    assert s.cache.discard(s.id), "the session should have been cached"
    assert s.id not in s.cache
    evicted_context_rebuilt = "water" not in s.context().step_proposals
    third_payload = s.generate("water")
    third = [f["id"] for f in _zone_features(third_payload)]

    assert first == second == third, (
        f"the survey-zone id set must be identical across regenerates and "
        f"across a cache eviction:\n  1: {first}\n  2: {second}\n  3: {third}"
    )
    assert len(set(first)) == len(first), f"ids must be unique: {first}"
    assert evicted_context_rebuilt, (
        "the eviction must have dropped the cached proposals, or the third "
        "generate did not actually re-run"
    )
    # THE TYPE ASSIGNMENT IS STABLE TOO -- an id that keeps its number but
    # changes instrument would be the same silent failure.
    types_by_id = {
        f["id"]: f["properties"]["survey_type"] for f in _zone_features(third_payload)
    }

print(
    f"6 [test 7]. ID STABILITY: three generates -- two warm, one after the "
    f"session cache was evicted and the context rebuilt from the document -- "
    f"produced the IDENTICAL id set {first}, each id keeping its survey type "
    f"({types_by_id})."
)


# --- 7 [test 8]. ID UNIQUENESS ACROSS BOTH SURVEY TYPES ---------------
#
# RANKING IS PER TYPE and both types hold a rank 1, so an id numbered per
# type would be ambiguous in a commit that spans them. Ids are assigned over
# the FULL cross-type list (compute_water_survey_areas() enumerates every
# zone, dropped ones included, before the floor filters), which is what makes
# "water-survey-zone-4" name exactly one zone.

with Harness() as h:
    s = Session()
    s.commit_landform()
    payload = s.generate("water")
    zones = _zone_features(payload)

    wire_ids = [f["id"] for f in zones]
    internal_ids = [f["properties"]["zone_id"] for f in zones]
    assert len(set(wire_ids)) == len(wire_ids), f"duplicate wire ids: {wire_ids}"
    assert len(set(internal_ids)) == len(internal_ids), internal_ids
    assert internal_ids == [
        wire_translation.internal_water_survey_zone_id(i) for i in wire_ids
    ], "the wire id and properties.zone_id must be the same identity"

    # BOTH TYPES HOLD A RANK 1 -- the condition that makes per-type numbering
    # ambiguous, asserted so the uniqueness claim is not vacuous.
    ranks_by_type = {}
    for feature in zones:
        ranks_by_type.setdefault(feature["properties"]["survey_type"], []).append(
            feature["properties"]["rank"]
        )
    assert all(1 in ranks for ranks in ranks_by_type.values()), ranks_by_type
    assert len(ranks_by_type) == 2

    # AND ACROSS TYPES the id sets are disjoint.
    ids_by_type = {}
    for feature in zones:
        ids_by_type.setdefault(feature["properties"]["survey_type"], set()).add(
            feature["properties"]["zone_id"]
        )
    embankment_ids, excavated_ids = (
        ids_by_type["embankment"], ids_by_type["excavated"]
    )
    assert not (embankment_ids & excavated_ids), (
        f"the two survey types must not share an id: "
        f"{sorted(embankment_ids & excavated_ids)}"
    )

    # A MEMBER ID DOES NOT PARSE AS A ZONE ID, even though the wire ids share
    # a prefix -- which is what stops a member footprint being rehydrated as
    # the zone whose number it happens to carry.
    for member in _member_features(payload):
        assert wire_translation.internal_water_survey_zone_id(member["id"]) is None, (
            f"{member['id']} must not parse as a survey-zone id"
        )

print(
    f"7 [test 8]. ID UNIQUENESS: {len(wire_ids)} zones numbered over the FULL "
    f"cross-type list -- embankment {sorted(embankment_ids)}, excavated "
    f"{sorted(excavated_ids)}, disjoint -- while BOTH types hold a rank 1 "
    f"({ {t: sorted(r) for t, r in ranks_by_type.items()} }), which is what "
    f"would make per-type numbering ambiguous. Member wire ids share the "
    f"prefix and deliberately do not parse as zone ids."
)


# --- 8 [test 9]. THE OVERLAP SENTINELS SURVIVE THE WIRE ---------------
#
# canopy_overlap_pct, road_overlap_pct and production_overlap_pct each use
# None for "never checked" and 0.0 for "checked and genuinely none". The
# frontend renders an em-dash for null precisely so the second never prints
# as the first. Two runs, because a sentinel assertion needs both values to
# be reachable: the session path (every layer supplied -> real numbers,
# including genuine zeros) and a run with the road check genuinely unmade.

with Harness() as h:
    s = Session()
    s.commit_landform()
    checked_payload = s.generate("water")
    checked_zones = _zone_features(checked_payload)

    OVERLAPS = ("canopy_overlap_pct", "road_overlap_pct", "production_overlap_pct")
    for feature in checked_zones:
        for key in OVERLAPS:
            assert feature["properties"][key] is not None, (
                f"{feature['id']}.{key} is None on a run where every layer was "
                f"supplied -- 'never checked' would be a lie here"
            )
            assert isinstance(feature["properties"][key], float), (
                f"{feature['id']}.{key}: {feature['properties'][key]!r}"
            )
    genuine_zeros = {
        key: sum(1 for f in checked_zones if f["properties"][key] == 0.0)
        for key in OVERLAPS
    }
    assert sum(genuine_zeros.values()) > 0, (
        "the fixture must produce at least one GENUINE 0.0 overlap, or the "
        "distinction from None below is untested"
    )

    # THE UNCHECKED RUN. The road union is not merely absent (a real None
    # means 'checked, genuinely no mapped road') -- it is the module's own
    # never-checked sentinel, which is the state a road-service outage
    # produces. Run through compute + the SAME payload builder the registry
    # names, so the assertion is about what reaches the WIRE.
    context = s.context()
    unchecked = water_survey_areas.compute_water_survey_areas(
        context.dem,
        context.boundary_polygon_utm,
        production_areas=None,
        soil_inputs=water_survey_areas.soil_inputs_for_parcel_data(context.parcel_data),
    )
    unchecked_payload = step_orchestrator.build_water_payload(
        {
            "zones_geojson": water_survey_areas.survey_areas_to_geojson(
                unchecked["zones"]
            ),
            "narrative_data": water_survey_areas.build_narrative_data(unchecked),
        },
        {},
    )
    unchecked_zones = _zone_features(unchecked_payload)
    assert unchecked_zones, "the unchecked run must still produce zones"

    for feature in unchecked_zones:
        for key in ("canopy_overlap_pct", "road_overlap_pct", "production_overlap_pct"):
            assert feature["properties"][key] is None, (
                f"{feature['id']}.{key} came back "
                f"{feature['properties'][key]!r} on a run where the layer was "
                f"never checked. A coerced 0.0 would print as a measured "
                f"'no overlap' for ground nobody looked at."
            )
    # ... and in the step-level block too, which is a second serialisation of
    # the same three numbers.
    for block in unchecked_payload["zones"]:
        assert block["overlaps"] == {
            "canopy_pct": None, "road_pct": None, "production_pct": None
        }, block["overlaps"]
    for block in checked_payload["zones"]:
        assert all(value is not None for value in block["overlaps"].values()), (
            block["overlaps"]
        )

    # THE JSON ROUND TRIP. None must survive as null and 0.0 as 0.0 -- they
    # are different values on the wire, not two spellings of "nothing".
    import json
    round_tripped = json.loads(json.dumps(unchecked_payload["zones"][0]["overlaps"]))
    assert round_tripped == {"canopy_pct": None, "road_pct": None, "production_pct": None}
    assert json.dumps({"a": None, "b": 0.0}) == '{"a": null, "b": 0.0}'

print(
    f"8 [test 9]. SENTINELS: on the fully-supplied session run every zone "
    f"carries a float for all three overlaps, including "
    f"{sum(genuine_zeros.values())} genuine 0.0(s) ({genuine_zeros}); on a run "
    f"with canopy, roads and production never checked every zone carries None "
    f"for all three -- on the feature properties AND in the step-level block "
    f"-- and None survives json.dumps() as null, distinct from 0.0."
)


# --- 9 [test 10]. cross_type_overlaps IS A FINDING, NOT A SELECTION ----
#
# It is the agreement report between the two survey instruments -- what
# fraction of each zone's envelope the OTHER type's surviving zones also
# claim -- and it is computed at GENERATE time against the surviving set. It
# is a finding about the GROUND. Committing a selection does not change the
# ground, so it must not change this.

with Harness() as h:
    s = Session()
    s.commit_landform()
    before_payload = s.generate("water")
    before = {
        f["id"]: f["properties"]["cross_type_overlaps"]
        for f in _zone_features(before_payload)
    }
    assert any(before.values()), (
        "the fixture must produce at least one cross-type overlap, or this "
        "assertion is vacuous"
    )

    zones = _zone_features(before_payload)
    embankment = [f for f in zones if f["properties"]["survey_type"] == "embankment"]
    excavated = [f for f in zones if f["properties"]["survey_type"] == "excavated"]

    for label, selection in (
        ("one embankment zone", [embankment[0]]),
        ("two excavated zones", excavated[:2]),
        ("a cross-type triple", [embankment[0], excavated[0], excavated[1]]),
        ("nothing at all", []),
    ):
        s.commit(
            "water", selection, {f["id"]: "generated" for f in selection},
            base_revision=s.stored()["steps"]["water"].get("revision", 0),
        )
        # RE-DERIVED, not re-read from a cache. A committed step has no
        # layers to serve (step_payload() says so on purpose), so the
        # comparison runs through a REOPEN -- which re-runs the generate for
        # real and restores the selection against the fresh proposals. That
        # makes this the strongest form of the claim: not "the stored payload
        # was not edited", but "computing the report again after the commit
        # gives the same answer".
        s.document = step_orchestrator.reopen_step(
            s.id, "water", s.store, fetch_cache=s.fetch_cache, cache=s.cache
        )
        restored = s.context().step_restored["water"]
        after_payload = restored["payload"]
        assert set(restored["selected_feature_ids"]) == {
            f["id"] for f in selection
        }, (
            f"the reopen must restore exactly the {len(selection)} zone(s) "
            f"committed as {label}: {restored['selected_feature_ids']}"
        )
        after = {
            f["id"]: f["properties"]["cross_type_overlaps"]
            for f in _zone_features(after_payload)
        }
        assert after == before, (
            f"committing {label} changed cross_type_overlaps. It is a finding "
            f"about the ground, computed against the SURVIVING zones at "
            f"generate time, not a report on the selection.\n"
            f"  before: {before}\n  after:  {after}"
        )
        # Nor may the committed selection prune the candidate set: every
        # surviving zone is still offered, whatever was committed.
        assert set(after) == set(before)

print(
    f"9 [test 10]. cross_type_overlaps: unchanged across four different "
    f"commits of the same generate (one embankment zone, two excavated, a "
    f"cross-type triple, and nothing at all), each re-derived through a "
    f"REOPEN that re-ran the generate and restored the selection. "
    f"{sum(1 for v in before.values() if v)} of {len(before)} zones carry an "
    f"agreement entry, and the same {len(before)} candidates stay on offer "
    f"after every commit -- it is a finding about the ground, not a report on "
    f"the selection."
)


print("\nAll water step checks passed.")
