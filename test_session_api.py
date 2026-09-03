"""
test_session_api.py

THE HTTP SURFACE, end to end, over Flask's test client. Run as:

    python3 test_session_api.py

REAL COORDINATES, REAL PIPELINE CODE, REAL ROUTES. The boundary is the
actual drawn property from generate_full_report.py -- 5614 N Montour Rd,
Gibsonia, PA (~13.23 acres, UTM 17N / EPSG:32617) -- the SAME six lon/lat
pairs test_session_manager.py (B2), test_wire_translation_inbound.py (B4),
test_step_orchestrator.py (B5a) and test_step_commit.py (B5b) use, over
B5a's DEM fixture, so every figure printed below is comparable with theirs.
Nothing is stubbed between the request and the orchestrator: session_api's
own blueprint is registered on a Flask app and every assertion below is
about a real response object.

What is mocked is the NETWORK and only the network -- B5b's harness,
verbatim. An assertion that a status code is 200 means something only if the
work behind it actually ran.

THE THING THIS FILE EXISTS TO PROVE is that three contracts survive the
transport, because each of them is one careless `jsonify({"error": ...})`
away from being lost:

  * 409 CARRIES THE CURRENT DOCUMENT (section 2). Without it the client
    cannot hydrate and rebase, and the status code is decoration.
  * 422 CARRIES OFFENDING FEATURE IDS WITH A REASON EACH (section 3). The
    frontend renders rejections per feature; a collapsed banner makes the
    user delete zones one at a time to find the bad one.
  * AN EXCLUSION CROSSING STILL COMMITS (section 5). The exclusion gates are
    advisory and recorded -- the settled contract, and the one an HTTP layer
    would be most likely to "helpfully" turn into a rejection.

Sections:
  1.  HAPPY PATH -- create, generate, poll, layers, commit, get session.
  2.  409 CONFLICT -- carrying the current document.
  3.  422 REJECTION -- per-feature ids and reasons.
  4.  422 NOT 500 -- a self-intersecting ring.
  5.  EXCLUSION CROSSING COMMITS -- 200, with the crossing recorded.
  6.  JOB FAILURE -- a 200 poll whose BODY carries failed + failed_layer.
  7.  RESUME -- a fresh client, and the document says where the wizard is.
  8.  LAYERS AFTER EVICTION -- rebuilt, byte-identical.
  9.  404s -- unknown session, unknown step, unknown job.
  10. /api/production-zones UNCHANGED -- the frontend spike still works.
  11. GET /api/steps -- the step order, with no session in existence.
"""

import json
import tempfile
import time
from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

import canopy_height_data
import dem_data
import design_document
import exclusion_zones
import farm_roads_data
import job_runner
import keypoint_detection
import parcel_data
import production_area
import production_area_ceiling
import production_zone_payload
import session_api
import session_cache
import valley_delineation
import wire_translation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- the real property, verbatim from B2, B4, B5a and B5b ------------

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

# --- the DEM fixture, B5a's verbatim ---------------------------------

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
    """A 4% bench with one incised drainage down CHANNEL_COL -- B5a's fixture,
    so the zones committed over HTTP here are the zones those branches
    asserted over."""
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


def _build_parcel_data(_boundary=None) -> ParcelData:
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


# --- the harness, B5b's -----------------------------------------------


class Harness:
    """
    Every network boundary mocked, every real computation wrapped and
    counted. B5b's harness with the two /api/production-zones fetch points
    added -- section 10 drives that endpoint through its OWN code path, so
    only its network calls are closed, not the function itself.
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
        self.dem_refetch = patch(
            mock_patch.object(
                production_area_ceiling, "get_dem_for_boundary",
                side_effect=AssertionError("get_dem_for_boundary() must not run"),
            )
        )
        # The two layers /api/production-zones fetches for ITSELF -- the
        # session path never reaches these (its DEM and HAG come off the
        # warmed cache), so they are the only difference between the two
        # harnesses and section 10 is the only section that touches them.
        self.spike_dem_fetch = patch(
            mock_patch.object(
                dem_data, "get_dem_for_boundary", side_effect=lambda *a, **k: _build_dem()
            )
        )
        self.spike_canopy_fetch = patch(
            mock_patch.object(
                production_zone_payload, "get_canopy_height_for_boundary",
                side_effect=lambda boundary, dem=None, **k: _build_canopy(
                    dem or _build_dem()
                ),
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
        self.rehydrate = patch(
            mock_patch.object(
                wire_translation, "rehydrate_production_zones",
                wraps=wire_translation.rehydrate_production_zones,
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


# --- the client -------------------------------------------------------

POLL_INTERVAL_SECONDS = 0.05
POLL_TIMEOUT_SECONDS = 600.0


class Client:
    """
    One Flask test client over session_api's own blueprint, with a
    temp-directory store, its own caches and its own job runner.

    THE REAL ROUTES, not a re-registration of them: create_app() builds the
    same blueprint api.py registers, from the same factory, so a rule that
    holds here holds on the deployed app. Section 10 asserts the deployed app
    actually carries them.
    """

    def __init__(self, deps=None):
        if deps is None:
            deps = session_api.Dependencies(
                store=JSONFileStore(tempfile.mkdtemp(prefix="session_api_test_")),
                fetch_cache=session_cache.FetchCache(max_entries=8),
                cache=session_cache.SessionCache(
                    max_sessions=8, idle_timeout_seconds=1800.0
                ),
                runner=job_runner.JobRunner(max_workers=2, max_jobs=32),
            )
        self.deps = deps
        self.http = session_api.create_app(deps).test_client()

    # --- verbs, each one request ---

    def create(self, boundary=REAL_BOUNDARY):
        return self.http.post(
            "/api/sessions", json={"boundary": [list(p) for p in boundary]}
        )

    def get_session(self, session_id):
        return self.http.get(f"/api/sessions/{session_id}")

    def generate(self, session_id, step_id="landform", **body):
        return self.http.post(
            f"/api/sessions/{session_id}/steps/{step_id}/generate", json=body
        )

    def layers(self, session_id, step_id="landform"):
        return self.http.get(f"/api/sessions/{session_id}/steps/{step_id}/layers")

    def commit(self, session_id, features, provenance, base_revision, step_id="landform"):
        return self.http.post(
            f"/api/sessions/{session_id}/steps/{step_id}/commit",
            json={
                "features": features,
                "provenance": provenance,
                "base_revision": base_revision,
            },
        )

    def reopen(self, session_id, step_id="landform"):
        return self.http.post(f"/api/sessions/{session_id}/steps/{step_id}/reopen")

    def job(self, job_id):
        return self.http.get(f"/api/jobs/{job_id}")

    def steps(self):
        return self.http.get("/api/steps")

    # --- polling, the way a real client does it ---

    def poll(self, job_id):
        """
        GET the job until it leaves `running`. Returns (final response, polls).

        A LOOP OF REAL REQUESTS, never job.wait(). The polling contract is
        what this file is testing: that every poll of a job this process
        holds answers 200, and that the terminal state is in the BODY.
        """
        deadline = time.time() + POLL_TIMEOUT_SECONDS
        polls = 0
        while True:
            response = self.job(job_id)
            polls += 1
            assert response.status_code == 200, (
                f"every poll of a held job is a 200 -- got "
                f"{response.status_code}: {response.get_json()}"
            )
            if response.get_json()["status"] != job_runner.STATUS_RUNNING:
                return response, polls
            if time.time() > deadline:
                raise AssertionError(f"job {job_id} never finished")
            time.sleep(POLL_INTERVAL_SECONDS)


# --- building a commit, B5b's shapes ----------------------------------

LAYER = wire_translation.LAYER_PRODUCTION_AREA


def _collection(features):
    return {"type": "FeatureCollection", "features": list(features)}


def _drawn(feature_id: str, ring_lon_lat, label="Drawn zone"):
    ring = [list(point) for point in ring_lon_lat]
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


# B5b's three rings, verbatim: over the hydric mask (accepted, recorded),
# straddling the property line (rejected), and self-intersecting (rejected
# per feature rather than escaping as a 500).
HYDRIC_ZONE_RING = _rect(-79.98303, -79.98291, 40.64342, 40.64390)
OFF_PARCEL_RING = _rect(-79.9845, -79.9834, 40.6448, 40.6452)
BOWTIE_RING = [
    (-79.9826, 40.6448),
    (-79.9820, 40.6452),
    (-79.9826, 40.6452),
    (-79.9820, 40.6448),
]


print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} "
    f"vertices, {PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at "
    f"{RESOLUTION_METERS:.0f} m. Same boundary as B2, B4, B5a and B5b; every "
    f"assertion below is over a real Flask response.\n"
)


# --- 1. HAPPY PATH ----------------------------------------------------

with Harness() as h:
    c = Client()

    created = c.create()
    assert created.status_code == 201, (created.status_code, created.get_json())
    document = created.get_json()
    session_id = document["session_id"]
    assert created.headers["Location"] == f"/api/sessions/{session_id}"
    design_document.validate_document(document)
    assert document["document_revision"] == 0
    assert all(
        entry["status"] == design_document.STATUS_NOT_STARTED
        for entry in document["steps"].values()
    ), document["steps"]

    accepted = c.generate(session_id)
    assert accepted.status_code == 202, (accepted.status_code, accepted.get_json())
    submitted = accepted.get_json()
    assert set(submitted) == {"job_id", "status"}, submitted
    assert submitted["status"] in (
        job_runner.STATUS_RUNNING, job_runner.STATUS_DONE
    ), submitted

    finished, poll_count = c.poll(submitted["job_id"])
    body = finished.get_json()
    assert body["status"] == job_runner.STATUS_DONE, body
    assert "result" in body and "error" not in body, sorted(body)

    # BOTH HALVES, SIDE BY SIDE. `payload` is the step's layers; `document` is
    # the document this generate just moved to "generated". Two sibling keys:
    # neither nested in the other, and the payload not replaced by a document
    # carrying it, so a reader of either never has to know the other's shape.
    assert set(body["result"]) == {"payload", "document"}, (
        f"a done generate carries its payload AND the updated document: "
        f"{sorted(body['result'])}"
    )
    job_payload = body["result"]["payload"]
    job_document = body["result"]["document"]

    # THE STATUS THE CLIENT CAME FOR, without asking again. The generate moved
    # landform to "generated" and the job says so; the round trip that used to
    # be the only way to learn it is what this key removes.
    assert job_document["steps"]["landform"]["status"] == (
        design_document.STATUS_GENERATED
    ), job_document["steps"]["landform"]

    # AND IT IS A DOCUMENT LIKE EVERY OTHER DOCUMENT. step_order rides along
    # because the job's document goes out through the same
    # design_document.document_body() the session routes use -- a document
    # that lost it by coming from a job would leave the client reading order
    # off `steps`, which Flask serves alphabetically.
    assert job_document["step_order"] == list(design_document.STEP_ORDER), (
        f"the carried document must carry step_order like every other "
        f"document served here: {job_document.get('step_order')}"
    )
    assert job_document["session_id"] == session_id, job_document["session_id"]

    # The document says "generated" now, and says it WITHOUT the proposals in
    # it -- the document holds decisions, the payload lives elsewhere.
    after_generate = c.get_session(session_id).get_json()

    # BYTE-IDENTICAL TO GET /api/sessions/<id>. Not "equivalent", not "carries
    # the same status" -- the same bytes, because they are the same shape from
    # the same function. This is the assertion that makes the extra GET
    # genuinely redundant rather than merely usually redundant: a client that
    # hydrates from the job result is in exactly the state it would have been
    # in had it fetched.
    assert json.dumps(job_document, sort_keys=True) == json.dumps(
        after_generate, sort_keys=True
    ), (
        f"the job's document and GET /api/sessions/<id> must be the same "
        f"document:\n  job: {json.dumps(job_document, sort_keys=True)[:400]}\n"
        f"  get: {json.dumps(after_generate, sort_keys=True)[:400]}"
    )
    assert after_generate["steps"]["landform"]["status"] == (
        design_document.STATUS_GENERATED
    ), after_generate["steps"]["landform"]
    assert set(after_generate["steps"]["landform"]) == {"status"}, (
        f"a generated step carries its status and nothing else: "
        f"{after_generate['steps']['landform']}"
    )

    layers = c.layers(session_id)
    assert layers.status_code == 200, (layers.status_code, layers.get_json())
    assert layers.get_json() == job_payload, (
        "GET .../layers must return the SAME payload the generate job "
        "produced -- that is the whole reason a resume does not regenerate"
    )

    proposals = job_payload["suggested_zones"]["features"]
    assert len(proposals) >= 2, len(proposals)
    selected = proposals[:2]
    selected_ids = [feature["id"] for feature in selected]

    committed = c.commit(
        session_id,
        _collection(selected),
        {feature_id: "generated" for feature_id in selected_ids},
        base_revision=0,
    )
    assert committed.status_code == 200, (committed.status_code, committed.get_json())
    committed_document = committed.get_json()
    entry = committed_document["steps"]["landform"]
    assert entry["status"] == design_document.STATUS_COMMITTED, entry["status"]
    assert entry["revision"] == 1, entry["revision"]
    assert [f["id"] for f in entry["features"]["features"]] == selected_ids

    resumed = c.get_session(session_id)
    assert resumed.status_code == 200
    assert resumed.get_json() == committed_document, (
        "GET /api/sessions/<id> must return exactly what the commit returned "
        "-- one document, served from the store"
    )
    design_document.validate_document(resumed.get_json())

print(
    f"1. HAPPY PATH: POST /api/sessions -> 201 (Location: /api/sessions/"
    f"{session_id[:8]}..., document_revision 0, six not_started steps); POST "
    f".../generate -> 202 job {submitted['job_id'][:8]}...; {poll_count} poll(s) "
    f"of GET /api/jobs -> 200 status 'done' whose result carries BOTH "
    f"{sorted(body['result'])} -- the document at landform "
    f"'{job_document['steps']['landform']['status']}' with step_order "
    f"{job_document['step_order']}, byte-identical to GET /api/sessions/<id> "
    f"immediately after, so no client needs that fetch; GET .../layers -> 200 carrying the "
    f"identical payload ({len(proposals)} proposals); POST .../commit -> 200 "
    f"status 'committed' at step revision {entry['revision']}; GET /api/sessions "
    f"-> 200, byte-identical to the commit's response."
)


# --- 2. 409 CONFLICT, CARRYING THE CURRENT DOCUMENT -------------------
#
# The one that makes the status code useful. Section 2.6: the client
# hydrates the document it is handed, keeps the draft where its base step
# survived, and re-prompts. A bare 409 forces a GET it can lose another race
# on.

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]
    payload = c.poll(c.generate(session_id).get_json()["job_id"])[0].get_json()[
        "result"
    ]["payload"]
    proposals = payload["suggested_zones"]["features"]

    first = c.commit(
        session_id,
        _collection(proposals[:1]),
        {proposals[0]["id"]: "generated"},
        base_revision=0,
    )
    assert first.status_code == 200, first.get_json()

    # The SAME base_revision again -- what a second tab holding a stale
    # document sends.
    second = c.commit(
        session_id,
        _collection(proposals[1:2]),
        {proposals[1]["id"]: "generated"},
        base_revision=0,
    )
    assert second.status_code == 409, (second.status_code, second.get_json())
    conflict = second.get_json()

    assert "document" in conflict, (
        f"THE 409 MUST CARRY THE CURRENT DOCUMENT. Without it the client "
        f"cannot reconcile -- it has a stale base_revision and no way to "
        f"rebase but a second round trip. Got keys {sorted(conflict)}"
    )
    current = conflict["document"]
    design_document.validate_document(current)
    assert current == c.get_session(session_id).get_json(), (
        "the document in the 409 body must BE the current one, not a stale "
        "copy the raiser happened to be holding"
    )
    assert current["steps"]["landform"]["revision"] == 1, current["steps"]["landform"]
    assert conflict["expected_base_revision"] == 1, conflict
    assert conflict["received_base_revision"] == 0, conflict
    assert conflict["step_id"] == "landform", conflict

    # AND THE FIRST COMMIT SURVIVED. A conflict rejects the second write; it
    # does not disturb the one that won.
    assert [
        f["id"] for f in current["steps"]["landform"]["features"]["features"]
    ] == [proposals[0]["id"]], current["steps"]["landform"]["features"]

    # The client can now do exactly what 2.6 describes, over HTTP.
    rebased = c.commit(
        session_id,
        _collection(proposals[1:2]),
        {proposals[1]["id"]: "generated"},
        base_revision=current["steps"]["landform"]["revision"],
    )
    assert rebased.status_code == 200, rebased.get_json()
    assert rebased.get_json()["steps"]["landform"]["revision"] == 2

print(
    f"2. 409 CONFLICT: a second commit at base_revision 0 against a step at "
    f"revision 1 returned 409 -- and its BODY carries the CURRENT DOCUMENT "
    f"(document_revision {current['document_revision']}, landform at revision "
    f"{current['steps']['landform']['revision']} holding the winning commit), "
    f"byte-identical to GET /api/sessions, plus expected/received "
    f"{conflict['expected_base_revision']}/{conflict['received_base_revision']}. "
    f"Rebasing on it committed at revision "
    f"{rebased.get_json()['steps']['landform']['revision']} -- the reconcile "
    f"section 2.6 describes, with no extra round trip."
)


# --- 3. 422 REJECTION, PER FEATURE ------------------------------------

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]
    payload = c.poll(c.generate(session_id).get_json()["job_id"])[0].get_json()[
        "result"
    ]["payload"]
    proposals = payload["suggested_zones"]["features"]

    good = proposals[0]
    bad = _drawn("drawn-off", OFF_PARCEL_RING, label="Half off the property")
    rejected = c.commit(
        session_id,
        _collection([good, bad]),
        {good["id"]: "generated", "drawn-off": "user_added"},
        base_revision=0,
    )
    assert rejected.status_code == 422, (rejected.status_code, rejected.get_json())
    problem = rejected.get_json()

    assert "rejections" in problem, (
        f"422 MUST CARRY PER-FEATURE REJECTIONS, not a collapsed message -- "
        f"the frontend renders them against its own feature list. Got keys "
        f"{sorted(problem)}"
    )
    rejections = problem["rejections"]
    assert isinstance(rejections, list) and rejections, problem
    by_id = {r["feature_id"]: r for r in rejections}
    assert "drawn-off" in by_id, (
        f"the OFFENDING FEATURE ID must be named: {rejections}"
    )
    offender = by_id["drawn-off"]
    assert offender["code"] == "outside_boundary", offender
    assert offender["reason"] and len(offender["reason"]) > 20, (
        f"every rejection carries its OWN reason, not just a code: {offender}"
    )
    # THE INNOCENT FEATURE IS NOT BLAMED. A per-feature contract that
    # rejected the whole collection by id would pass every check above.
    assert good["id"] not in by_id, (
        f"the valid feature in the same commit must not be named: {rejections}"
    )

    # NOTHING WAS WRITTEN. A rejected commit is retryable with the same
    # base_revision, which is only true if the gate wrote nothing.
    after = c.get_session(session_id).get_json()
    assert after["steps"]["landform"]["status"] == design_document.STATUS_GENERATED
    retry = c.commit(
        session_id, _collection([good]), {good["id"]: "generated"}, base_revision=0
    )
    assert retry.status_code == 200, retry.get_json()

print(
    f"3. 422 REJECTION: an off-parcel drawn zone committed alongside a valid "
    f"proposal returned 422 carrying {len(rejections)} per-feature "
    f"rejection(s) -- feature_id 'drawn-off', code '{offender['code']}', its "
    f"own reason ({len(offender['reason'])} chars: "
    f"\"{offender['reason'][:64]}...\"). The valid feature "
    f"'{good['id']}' is NOT named. Nothing was written: the step was still "
    f"'{after['steps']['landform']['status']}' and the same base_revision 0 "
    f"then committed at 200."
)


# --- 4. 422 NOT 500 ---------------------------------------------------
#
# A self-intersecting ring is a valid GeoJSON coordinate array and not a
# valid polygon. It reaches the rehydrator, which raises InboundGeometryError
# -- and that must arrive as a per-feature 422, never as a traceback and a
# 500 saying the server broke on the user's drawing.

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]
    c.poll(c.generate(session_id).get_json()["job_id"])

    bowtie = _drawn("drawn-bowtie", BOWTIE_RING, label="A bowtie")
    response = c.commit(
        session_id,
        _collection([bowtie]),
        {"drawn-bowtie": "user_added"},
        base_revision=0,
    )
    assert response.status_code == 422, (
        f"a self-intersecting ring is the USER'S drawing being wrong, not the "
        f"server failing -- got {response.status_code}: {response.get_data(as_text=True)[:400]}"
    )
    bowtie_rejections = response.get_json()["rejections"]
    bowtie_rejection = next(
        r for r in bowtie_rejections if r["feature_id"] == "drawn-bowtie"
    )
    assert bowtie_rejection["code"] == "invalid_geometry", bowtie_rejection
    assert "Traceback" not in json.dumps(response.get_json())

print(
    f"4. 422 NOT 500: a self-intersecting ring returned 422 with code "
    f"'{bowtie_rejection['code']}' on feature 'drawn-bowtie' -- "
    f"InboundGeometryError surfaced through commit validation as a "
    f"per-feature rejection, never as a 500. Reason: "
    f"\"{bowtie_rejection['reason'][:80]}...\""
)


# --- 5. EXCLUSION CROSSING COMMITS ------------------------------------
#
# THE ADVISORY CONTRACT, ACROSS THE TRANSPORT. The parcel boundary is the
# only hard spatial gate; every exclusion gate is advisory and RECORDED. An
# HTTP layer that "helpfully" turned a crossing into a 422 would silently
# reverse the settled decision -- so this asserts the 200 and the record.

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]
    payload = c.poll(c.generate(session_id).get_json()["job_id"])[0].get_json()[
        "result"
    ]["payload"]
    proposals = payload["suggested_zones"]["features"]

    clean = proposals[0]
    wet = _drawn("drawn-wet", HYDRIC_ZONE_RING, label="Over the wet ground")
    response = c.commit(
        session_id,
        _collection([clean, wet]),
        {clean["id"]: "generated", "drawn-wet": "user_added"},
        base_revision=0,
    )
    assert response.status_code == 200, (
        f"A ZONE OVER AN EXCLUSION MASK COMMITS. The exclusion gates are "
        f"advisory; only the parcel boundary rejects. Got "
        f"{response.status_code}: {response.get_json()}"
    )
    stored_features = response.get_json()["steps"]["landform"]["features"]["features"]
    wet_stored = next(f for f in stored_features if f["id"] == "drawn-wet")
    crossings = wet_stored["properties"]["exclusion_crossings"]
    assert crossings, (
        "the crossing must be RECORDED alongside the feature -- a 200 with no "
        "record would lose the advisory half of the contract"
    )
    crossed_types = [crossing["type"] for crossing in crossings]
    assert "hydric" in crossed_types, crossings
    hydric_crossing = next(c_ for c_ in crossings if c_["type"] == "hydric")
    assert hydric_crossing["acres"] > 0, hydric_crossing
    assert hydric_crossing["label"], hydric_crossing

    clean_stored = next(f for f in stored_features if f["id"] == clean["id"])
    assert clean_stored["properties"]["exclusion_crossings"] == [], clean_stored
    assert "exclusion_crossings" in clean_stored["properties"], (
        "[] says 'checked, crosses nothing'; an absent key would say 'this "
        "commit predates crossings being recorded'"
    )

print(
    f"5. EXCLUSION CROSSING COMMITS: a drawn zone sitting on the hydric mask "
    f"returned 200 -- status "
    f"'{response.get_json()['steps']['landform']['status']}' at revision "
    f"{response.get_json()['steps']['landform']['revision']} -- with the "
    f"crossing recorded on the feature: "
    + ", ".join(f"{c_['type']} {c_['acres']} ac ({c_['label']})" for c_ in crossings)
    + f". A selected proposal in the same commit records []. The advisory "
    f"contract survives the HTTP layer intact."
)


# --- 6. JOB FAILURE IS A 200 POLL -------------------------------------

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]

    with mock_patch.object(
        production_area_ceiling,
        "identify_optimized_production_areas",
        side_effect=canopy_height_data.CanopyCoverageIncompleteError(
            "fixture: HAG coverage too sparse"
        ),
    ):
        accepted = c.generate(session_id)
        assert accepted.status_code == 202, (
            f"the SUBMISSION still succeeds -- the failure is the job's, and "
            f"the client already holds an id to ask with: {accepted.get_json()}"
        )
        failed_response, failure_polls = c.poll(accepted.get_json()["job_id"])

    assert failed_response.status_code == 200, (
        f"A FINISHED-WITH-FAILURE JOB IS A SUCCESSFUL POLL. The question is "
        f"'did the job finish', and it did. Got {failed_response.status_code}"
    )
    failed_body = failed_response.get_json()
    assert failed_body["status"] == job_runner.STATUS_FAILED, failed_body
    assert "error" in failed_body and "result" not in failed_body, sorted(failed_body)
    assert failed_body["error"]["failed_layer"] == {
        "type": "canopy", "label": "tree canopy height"
    }, (
        f"the body must carry failed_layer {{type, label}} -- the shape the "
        f"panel branches on: {failed_body['error']}"
    )
    # NO DOCUMENT ON A FAILURE, and not as an oversight. The error body is
    # error_payload()'s and nothing was added to it, because the step's status
    # DID NOT CHANGE -- there is no transition to report. A document here
    # would invite a client to hydrate its mirror on the strength of a
    # generate that failed.
    assert "document" not in failed_body["error"], (
        f"a failed generate reports failed_layer and no document; the step's "
        f"status did not move: {sorted(failed_body['error'])}"
    )
    assert "document" not in failed_body, sorted(failed_body)

    assert "Traceback" not in json.dumps(failed_body)
    assert "fixture: HAG coverage too sparse" not in json.dumps(failed_body), (
        "the raw exception text must not cross the wire"
    )

    # AND THE DOCUMENT DID NOT MOVE. A failed generate is not a generate.
    assert c.get_session(session_id).get_json()["steps"]["landform"]["status"] == (
        design_document.STATUS_NOT_STARTED
    )
    # ... so layers has nothing to serve, and SAYS SO rather than returning
    # an empty payload.
    empty = c.layers(session_id)
    assert empty.status_code == 409, (empty.status_code, empty.get_json())
    assert empty.get_json()["status"] == design_document.STATUS_NOT_STARTED, empty.get_json()

print(
    f"6. JOB FAILURE: a generate whose entry point raised "
    f"CanopyCoverageIncompleteError was ACCEPTED at 202 and polled "
    f"({failure_polls} poll(s)) to an HTTP 200 whose BODY carries status "
    f"'{failed_body['status']}' and failed_layer "
    f"{failed_body['error']['failed_layer']}. No traceback, no raw exception "
    f"text. The document stayed 'not_started', and GET .../layers said so "
    f"explicitly (409, status "
    f"'{empty.get_json()['status']}') rather than returning an empty payload."
)


# --- 7. RESUME --------------------------------------------------------
#
# A FRESH CLIENT over the same store: a new browser session, a new tab, a
# process that has never seen this session. The document alone must say
# where the wizard is.

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]
    payload = c.poll(c.generate(session_id).get_json()["job_id"])[0].get_json()[
        "result"
    ]["payload"]
    proposals = payload["suggested_zones"]["features"]
    c.commit(
        session_id,
        _collection(proposals[:1]),
        {proposals[0]["id"]: "generated"},
        base_revision=0,
    )

    # A DIFFERENT Flask app and a DIFFERENT test client, sharing only the
    # store and the caches -- the second is what a second gunicorn worker
    # would share nothing of, which is why the DOCUMENT has to carry the
    # answer.
    fresh = Client(deps=c.deps)
    assert fresh.http is not c.http

    resumed = fresh.get_session(session_id)
    assert resumed.status_code == 200, resumed.get_json()
    resumed_document = resumed.get_json()
    design_document.validate_document(resumed_document)

    landform = resumed_document["steps"]["landform"]
    assert landform["status"] == design_document.STATUS_COMMITTED, landform
    assert landform["revision"] == 1, landform
    assert [f["id"] for f in landform["features"]["features"]] == [
        proposals[0]["id"]
    ], landform
    # WHERE THE WIZARD IS: landform decided, everything after it untouched.
    remaining = [
        step_id
        for step_id in design_document.STEP_ORDER
        if resumed_document["steps"][step_id]["status"]
        == design_document.STATUS_NOT_STARTED
    ]
    assert remaining == list(design_document.STEP_ORDER[1:]), remaining
    assert resumed_document["boundary"] == [list(p) for p in REAL_BOUNDARY], (
        "the parcel the user drew comes back with the session -- there is "
        "nowhere else for a resuming client to get it"
    )

    # THE STEP ORDER IS ON THE WIRE, AS AN ARRAY. The frontend computes the
    # reopen cascade off this to warn what a reopen discards, and the only
    # alternative is a second hardcoded copy of STEP_ORDER over there.
    assert resumed_document["step_order"] == list(design_document.STEP_ORDER), (
        f"the document must carry the canonical step order: got "
        f"{resumed_document.get('step_order')!r}"
    )
    # AND THE KEYS OF `steps` ARE NOT IT, which is the whole reason the field
    # exists. Flask's DefaultJSONProvider sets sort_keys = True, so the map the
    # document builds in pipeline order serialises ALPHABETICALLY. A client
    # reading the order off these keys gets six real step ids in a stable order
    # that is not the pipeline's, and nothing anywhere raises.
    assert list(resumed_document["steps"]) == sorted(design_document.STEP_ORDER), (
        f"if this ever stops being alphabetical the serializer changed, and "
        f"_document_body()'s reasoning should be re-read: "
        f"{list(resumed_document['steps'])}"
    )
    assert list(resumed_document["steps"]) != list(design_document.STEP_ORDER), (
        "the two orders must actually differ, or this test proves nothing"
    )
    # And the next commit's base_revision is readable straight off it.
    assert landform["revision"] == 1

    # REOPEN, THEN LAYERS -- the claim reopen_step_endpoint()'s docstring
    # makes, asserted rather than asserted-in-prose: a reopen returns the
    # DOCUMENT, and the client gets the editable candidate set back through
    # the same layers endpoint a plain resume uses. One way to ask.
    reopened = fresh.reopen(session_id)
    assert reopened.status_code == 200, (reopened.status_code, reopened.get_json())
    reopened_landform = reopened.get_json()["steps"]["landform"]
    assert reopened_landform["status"] == design_document.STATUS_GENERATED
    assert reopened_landform["revision"] == 1, (
        "the revision is RETAINED, so the eventual re-commit carries the "
        "optimistic-concurrency chain forward"
    )
    restored = fresh.layers(session_id)
    assert restored.status_code == 200, restored.get_json()
    restored_ids = [
        f["id"] for f in restored.get_json()["suggested_zones"]["features"]
    ]
    assert restored_ids == [f["id"] for f in proposals], (
        f"the candidate set comes back identical: {restored_ids}"
    )

print(
    f"7. RESUME: a fresh Flask app and test client, sharing only the store, "
    f"GET /api/sessions/<id> -> 200. The document says where the wizard is: "
    f"landform '{landform['status']}' at revision {landform['revision']} "
    f"holding {len(landform['features']['features'])} committed feature(s), "
    f"{len(remaining)} steps still not_started ({', '.join(remaining)}), and "
    f"the {len(resumed_document['boundary'])}-vertex boundary the user drew. "
    f"The next commit's base_revision reads straight off it. Reopening over "
    f"HTTP returned the document ('{reopened_landform['status']}', revision "
    f"{reopened_landform['revision']} retained) and GET .../layers then "
    f"served the restored candidate set back -- {len(restored_ids)} "
    f"proposals, the same ids."
)


# --- 8. LAYERS AFTER EVICTION -----------------------------------------

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]
    generated_payload = c.poll(
        c.generate(session_id).get_json()["job_id"]
    )[0].get_json()["result"]["payload"]

    warm = c.layers(session_id)
    assert warm.status_code == 200
    warm_payload = warm.get_json()
    assert warm_payload == generated_payload

    # EVICT. The session cache is tier 2 and disposable by design; a real
    # eviction is an idle timeout or a max_sessions overflow, and this is the
    # same thing arriving sooner.
    assert c.deps.cache.discard(session_id) is True
    assert session_id not in c.deps.cache
    production_runs_before = h.identify_production.call_count

    cold = c.layers(session_id)
    assert cold.status_code == 200, (cold.status_code, cold.get_json())
    cold_payload = cold.get_json()
    assert cold_payload == warm_payload, (
        "an evicted context must REBUILD to the identical payload -- a "
        "rebuild that returns something else means a resume shows the user a "
        "different candidate set than the one they were looking at"
    )
    rebuild_runs = h.identify_production.call_count - production_runs_before
    assert rebuild_runs == 1, (
        f"the rebuild re-ran the generate exactly once: {rebuild_runs}"
    )
    # THE REBUILD COST NO NETWORK. The fetch cache still holds Layer 1, so a
    # cold context is slower arithmetic, not a refetch of someone's parcel.
    network_before = h.total_network_calls
    assert c.deps.cache.discard(session_id) is True
    c.layers(session_id)
    assert h.total_network_calls == network_before, (
        f"a layers rebuild must not touch the network: "
        f"{h.total_network_calls - network_before} call(s)"
    )
    # And it did not churn the document: a regenerate changes no decision.
    assert c.get_session(session_id).get_json()["document_revision"] == 1

print(
    f"8. LAYERS AFTER EVICTION: the session context was discarded from the "
    f"cache, then GET .../layers returned 200 with a payload byte-identical "
    f"to the warm one ({len(cold_payload['suggested_zones']['features'])} "
    f"proposals, keys {sorted(cold_payload)}). The rebuild re-ran the generate "
    f"exactly {rebuild_runs} time and made 0 network calls, and left "
    f"document_revision at 1 -- a regenerate changes no decision."
)


# --- 9. 404s ----------------------------------------------------------

with Harness() as h:
    c = Client()
    session_id = c.create().get_json()["session_id"]

    unknown_session = c.get_session("definitely-not-a-session")
    assert unknown_session.status_code == 404, unknown_session.status_code
    assert "error" in unknown_session.get_json()

    # Every verb, not just the GET -- a 404 on read and a 500 on commit for
    # the same missing session would be two answers to one question.
    assert c.generate("definitely-not-a-session").status_code == 404
    assert c.layers("definitely-not-a-session").status_code == 404
    assert c.commit(
        "definitely-not-a-session", _collection([]), {}, base_revision=0
    ).status_code == 404
    assert c.reopen("definitely-not-a-session").status_code == 404

    # A step id that is not a step at all.
    nonsense = c.generate(session_id, step_id="orchard")
    assert nonsense.status_code == 404, (nonsense.status_code, nonsense.get_json())
    assert "orchard" in nonsense.get_json()["error"], nonsense.get_json()

    # A REAL step whose registry entry is not written yet. Same status --
    # this URL names no resource either -- but the message tells them apart,
    # which is get_step()'s own contract. "trees", not "water" or "roads":
    # both HAVE entries as of their branches, and ask different questions of
    # this surface -- see the 409 below.
    unregistered = c.generate(session_id, step_id="trees")
    assert unregistered.status_code == 404, unregistered.get_json()
    assert "no registry entry yet" in unregistered.get_json()["error"], (
        unregistered.get_json()
    )

    # A REGISTERED step whose UPSTREAM COMMIT IS NOT THERE: 409, and the
    # request never became a job.
    #
    # THE HTTP HALF OF B7'S FIX, asserted here because here is where it was
    # visible as wrong. UpstreamNotCommittedError is raised inside
    # assemble_consumes(), which runs on the JOB'S THREAD -- so this used to
    # come back 202 with a job id, and the failure the client then polled for
    # was the water step's generic "Water survey areas could not be
    # generated": the parcel's data failed, when the truth was "commit
    # landform first". step_orchestrator.generate_step() now resolves the
    # committed edges synchronously before submitting, so the answer arrives
    # as the status code with the upstream step named in the body.
    #
    # landform is COMMITTED by this point in the section, so water is asked
    # for on a fresh session where it is not.
    fresh = c.create()
    fresh_id = fresh.get_json()["session_id"]
    uncommitted_upstream = c.generate(fresh_id, step_id="water")
    assert uncommitted_upstream.status_code == 409, (
        uncommitted_upstream.status_code, uncommitted_upstream.get_json()
    )
    upstream_body = uncommitted_upstream.get_json()
    assert upstream_body["step_id"] == "water"
    assert upstream_body["upstream_step"] == "landform"
    assert upstream_body["upstream_status"] == "not_started", upstream_body
    assert "job_id" not in upstream_body, (
        "a 409 must not carry a job id -- no work was accepted, so nothing "
        "should be pollable"
    )

    unknown_job = c.job("not-a-job-id")
    assert unknown_job.status_code == 404, unknown_job.status_code

    # Params the step does not declare: a 400 from the SUBMISSION, with no
    # job -- there is nothing to poll for when the request itself is wrong.
    bad_params = c.generate(session_id, params={"access_point": [0, 0]})
    assert bad_params.status_code == 400, (bad_params.status_code, bad_params.get_json())
    assert "job_id" not in bad_params.get_json()

    # A boundary that encloses no ground.
    collinear = c.create(boundary=[(-79.98, 40.64), (-79.97, 40.64), (-79.96, 40.64)])
    assert collinear.status_code == 400, collinear.get_json()

    # A commit with no base_revision. Not defaulted to 0 -- that would make
    # every forgetful client's commit look like a first commit, which is the
    # exact race base_revision exists to catch.
    no_base = c.http.post(
        f"/api/sessions/{session_id}/steps/landform/commit",
        json={"features": _collection([]), "provenance": {}},
    )
    assert no_base.status_code == 400, no_base.get_json()

    # A reopen of a step nobody committed: a state conflict, not a no-op.
    not_committed = c.reopen(session_id)
    assert not_committed.status_code == 409, (
        not_committed.status_code, not_committed.get_json()
    )

    # A COMMIT BODY MISSING `features` ENTIRELY, and one whose features are
    # not a FeatureCollection. Both must land on the same 422 the per-feature
    # gate produces -- with a null feature_id, which FeatureRejection uses for
    # a defect of the COLLECTION rather than of a feature in it -- rather than
    # on a 500 from a route that assumed the key was there.
    for label, body in (
        ("missing", {"provenance": {}, "base_revision": 0}),
        (
            "not a collection",
            {"features": [1, 2, 3], "provenance": {}, "base_revision": 0},
        ),
    ):
        malformed = c.http.post(
            f"/api/sessions/{session_id}/steps/landform/commit", json=body
        )
        assert malformed.status_code == 422, (
            f"features {label}: expected 422, got {malformed.status_code} -- "
            f"{malformed.get_data(as_text=True)[:200]}"
        )
        collection_rejection = malformed.get_json()["rejections"][0]
        assert collection_rejection["feature_id"] is None, collection_rejection
        assert collection_rejection["code"] == "not_a_feature_collection", (
            collection_rejection
        )

    # A WRONG-TYPED PROVENANCE reaches commit_validation's own check rather
    # than being coerced to {} by the route -- which would have turned "your
    # provenance is a list" into a pile of missing_provenance rejections
    # against features that were fine.
    wrong_provenance = c.http.post(
        f"/api/sessions/{session_id}/steps/landform/commit",
        json={
            "features": _collection([]),
            "provenance": ["generated"],
            "base_revision": 0,
        },
    )
    assert wrong_provenance.status_code == 422, wrong_provenance.get_json()
    provenance_rejection = wrong_provenance.get_json()["rejections"][0]
    assert provenance_rejection["code"] == "missing_provenance", provenance_rejection
    assert "list" in provenance_rejection["reason"], provenance_rejection

print(
    f"9. 404s AND REJECTED INPUT: an unknown session is 404 on all five verbs; "
    f"an unknown step id ('orchard') is 404 and a REGISTERED-BUT-UNWRITTEN one "
    f"('trees') is 404 with a different message; an unknown job id is 404. "
    f"A REGISTERED step whose upstream commit is missing (water, before "
    f"landform) is {uncommitted_upstream.status_code} naming "
    f"'{upstream_body['upstream_step']}' at status "
    f"'{upstream_body['upstream_status']}' -- with NO job id, because "
    f"generate_step() resolves the committed edges before it submits rather "
    f"than on the job's thread. "
    f"Undeclared params are a 400 with NO job id, a collinear boundary is 400, "
    f"a commit missing base_revision is 400, and reopening an uncommitted step "
    f"is 409. A commit body with no 'features' at all, and one whose features "
    f"are not a FeatureCollection, both land on the SAME 422 with a "
    f"null-feature_id '{collection_rejection['code']}' rejection -- never a 500."
)


# --- 10. /api/production-zones UNCHANGED ------------------------------
#
# The frontend spike calls this endpoint today and must keep working until it
# migrates. This section drives it through the deployed app -- api.app, the
# one gunicorn serves -- with only its network boundaries closed.

import api  # noqa: E402 -- imported here so sections 1-9 do not pay for it

with Harness() as h:
    spike = api.app.test_client()

    response = spike.post(
        "/api/production-zones",
        json={"boundary": [list(point) for point in REAL_BOUNDARY]},
    )
    assert response.status_code == 200, (
        response.status_code, response.get_data(as_text=True)[:400]
    )
    spike_payload = response.get_json()
    for key in (
        "zones", "suggested_zones", "eligible_union", "exclusion_layers",
        "summary", "scales",
    ):
        assert key in spike_payload, (
            f"App.jsx / ProductionZonePanel.jsx / ProductionZoneLayers.jsx "
            f"read data.{key}; it must still be there. Keys: "
            f"{sorted(spike_payload)}"
        )
    assert spike_payload["suggested_zones"]["features"], spike_payload["summary"]

    # Its own 400 branch, untouched.
    assert spike.post("/api/production-zones", json={}).status_code == 400
    assert spike.get("/api/health").status_code == 200

    # AND THE SESSION SURFACE IS ON THAT SAME APP. Sections 1-9 run over
    # create_app(); this is what proves the deployed app carries the same
    # rules from the same factory.
    served = {
        rule.rule
        for rule in api.app.url_map.iter_rules()
        if rule.rule.startswith("/api/sessions")
        or rule.rule.startswith("/api/jobs")
        or rule.rule == "/api/steps"
    }
    assert served == {
        "/api/steps",
        "/api/sessions",
        "/api/sessions/<session_id>",
        "/api/sessions/<session_id>/steps/<step_id>/generate",
        "/api/sessions/<session_id>/steps/<step_id>/commit",
        "/api/sessions/<session_id>/steps/<step_id>/reopen",
        "/api/sessions/<session_id>/steps/<step_id>/layers",
        # The accumulating step's own write verb (the roads entry): free
        # one candidate set's slot.
        "/api/sessions/<session_id>/steps/<step_id>/discard",
        "/api/jobs/<job_id>",
    }, sorted(served)

print(
    f"10. /api/production-zones UNCHANGED: driven through api.app itself, it "
    f"returned 200 with all six keys the shipped frontend reads "
    f"({len(spike_payload['suggested_zones']['features'])} suggested zones, "
    f"{spike_payload['summary']['selected_acres']} selected acres), its "
    f"missing-boundary branch still 400s, and /api/health still 200s. The "
    f"deployed app carries all {len(served)} session/job routes from the same "
    f"blueprint factory sections 1-9 ran over."
)


# --- 11. GET /api/steps, WITH NO SESSION ------------------------------
#
# The step rail enumerates the pipeline, and before POST /api/sessions there
# is no document to enumerate it from. This route is where that list comes
# from, and the only thing that makes it worth having instead of six ids
# hardcoded in the frontend is that it and `step_order` cannot disagree.
# So the section asserts the agreement, not just the contents.

# A CLIENT THAT HAS NEVER CREATED A SESSION, over its own empty temp store --
# and asserted empty, so "no session in existence" is a checked precondition
# rather than an assumption about what ran above.
steps_client = Client()
assert steps_client.deps.store.list_sessions() == [], (
    f"this section is about the no-session case, so the store must be empty "
    f"before the call: {steps_client.deps.store.list_sessions()}"
)

no_session = steps_client.steps()
assert no_session.status_code == 200, (
    no_session.status_code, no_session.get_data(as_text=True)[:400]
)
steps_body = no_session.get_json()

# THE SIX IDS, IN STEP_ORDER'S ORDER. A list comparison, not a set one: the
# order IS the payload, and `sorted(...) == sorted(...)` would pass for the
# alphabetical serialisation this route exists to avoid.
assert steps_body == {"step_order": list(design_document.STEP_ORDER)}, steps_body
assert len(steps_body["step_order"]) == 6, steps_body
assert steps_body["step_order"] != sorted(steps_body["step_order"]), (
    "STEP_ORDER is not alphabetical, so a payload that IS alphabetical means "
    "something sorted it on the way out -- which is the exact failure this "
    f"route exists to prevent: {steps_body['step_order']}"
)

# IDS ONLY, NO TITLES. The frontend owns display titles in its own step
# definitions; a title here would be a second copy of one. Asserted, so the
# division is a contract rather than an omission someone later "fixes".
assert set(steps_body) == {"step_order"}, (
    f"/api/steps serves the order and nothing else -- ids, no titles. Extra "
    f"keys: {sorted(set(steps_body) - {'step_order'})}"
)
assert all(isinstance(step_id, str) for step_id in steps_body["step_order"]), steps_body

# AND IT IS THE SAME ARRAY A DOCUMENT CARRIES. Same client, so the only
# variable between the two calls is whether a session exists.
with Harness():
    steps_created = steps_client.create()
assert steps_created.status_code == 201, steps_created.get_json()
steps_document = steps_created.get_json()

assert steps_document["step_order"] == steps_body["step_order"], (
    f"pre-session and post-session must hand the client the SAME list under "
    f"the SAME key, or the rail's fallback is a second source of truth: "
    f"{steps_body['step_order']} vs {steps_document['step_order']}"
)

# The route is a constant and says so: creating a session did not move it.
assert steps_client.steps().get_json() == steps_body, steps_client.steps().get_json()

# And the alphabetical order really is different, which is why any of this
# is load-bearing.
assert list(steps_document["steps"]) == sorted(design_document.STEP_ORDER), (
    f"Flask sorts the steps object; if it ever stops, the reason this route "
    f"exists changes: {list(steps_document['steps'])}"
)

print(
    f"11. GET /api/steps: with an empty store and no session in existence it "
    f"returned 200 with {steps_body['step_order']} -- the six ids in "
    f"STEP_ORDER's order, ids only and no titles (the frontend owns those). "
    f"The same client's new document carries an IDENTICAL `step_order`, under "
    f"the same key, while its `steps` object serialises as "
    f"{list(steps_document['steps'])} -- the alphabetical order the array "
    f"exists to override."
)


# --- the store's directory, reported not asserted ---------------------

print(
    f"\nPERSISTENCE: the store's directory is session_api.store_directory() "
    f"-- ${session_api.STORE_DIRECTORY_ENV} if set, else "
    f"'{session_api.DEFAULT_STORE_DIRECTORY}' relative to the working "
    f"directory (/app in the Dockerfile). On Render and Railway that path is "
    f"EPHEMERAL unless a disk/volume is mounted at it: every deploy discards "
    f"every in-flight session. See README.md's Deploying section."
)

print("\nAll session_api checks passed.")
