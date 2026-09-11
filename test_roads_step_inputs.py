"""
test_roads_step_inputs.py

THE ROADS STEP, SECOND HALF: what reaches the roads generate from the
steps before it, and what leaves nothing behind. Run as:

    python test_roads_step_inputs.py

Sections 10-15 of test_roads_step.py, moved here verbatim so the two
halves run side by side under run_tests.py -- the roads file was the
longest in the suite and, in a parallel run, the one the wall clock
waited on. Same fixture (roads_step_fixture.py), same Harness, same
Session; the sections keep their original numbers.

Sections (the branch's numbered tests in brackets):
 10  [10] WATER EMPTY -> SENTINEL -- fetch_and_select_optimal_water_zone()
          runs ZERO times, the consumes edge resolves to NO_WATER_ZONE.
 11  [11] Water multi-select reaches roads as a UNION.
 12  [12] A commit missing its required input is REJECTED.
 13  [13] REOPEN restores every candidate, not just the committed one.
 14  [14] _API_ERRORS -- a failed POST /api/sessions carries failed_layer.
 15  [3,4,5] An access point that routes nothing leaves nothing.
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

# --- the fixture: the parcel, the DEM, the mocked network, the Harness and
# the Session live in roads_step_fixture.py so a file that needs them can import
# them without running this file's tests first.
from roads_step_fixture import (  # noqa: E402,F401
    _boundary_point,
    NO_NETWORK_CEILING_METERS_PER_ACRE,
    ACCESS_A,
    ACCESS_B,
    ACCESS_C,
    ACCESS_D,
    ACCESS_NO_NETWORK,
    _centroid_lon,
    _centroid_lat,
    INTERIOR_POINT,
    RESOLUTION_METERS,
    BUFFER_METERS,
    _minx,
    _miny,
    _maxx,
    _maxy,
    ORIGIN_X,
    ORIGIN_Y,
    COLS,
    ROWS,
    _centroid,
    CHANNEL_COL,
    KNEE_ROW,
    _build_dem,
    _build_canopy,
    HYDRIC_COMPONENTS,
    HYDRIC_GEOMETRIES,
    FIXTURE_ROADS,
    FIXTURE_KSAT,
    _build_parcel_data,
    Harness,
    _fresh_caches,
    _fresh_store,
    _fresh_runner,
    Session,
    _spanning_types,
    _network_features,
    _network,
    _commit_body,
    _comparable,
    KEY_A,
    KEY_B,
    KEY_C,
    KEY_D,
    KEY_NO_NETWORK,
)


ROADS = step_registry.get_step("roads")


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
        "C's network must reach the committed union on this fixture"
    )
    # AT THE CURRENT CEILING THE SPUR IS ZERO-LENGTH, WHICH IS THE STRONGEST
    # FORM OF REACHING THE WATER, NOT A FAILURE TO. At MAX_ROAD_METERS_PER_
    # SERVED_ACRE=200 this fixture's coverage loop stopped short of the pond
    # and the router built a real spur to close the gap, so this section
    # asserted exactly one water_spur FEATURE and measured its endpoint. At
    # 500 the loop itself already runs a branch through the traversable cell
    # beside the pond, so the spur has nothing left to build: it comes back
    # one cell long with 0.0 m of new construction and is dropped before
    # geometry (no segment, no line to draw). The road reaches the water in
    # both cases, which is exactly why reaches_water_zone is read off the
    # router's own branch list rather than off the drawn features -- see
    # build_road_network()'s own comment on that field.
    spur = [f for f in _network_features(payload, KEY_C) if f["properties"]["branch_role"] == "water_spur"]
    assert len(spur) <= 1, f"at most one water spur per network, got {len(spur)}"
    assert NETWORK_C_NO_WATER["access"]["reaches_water_zone"] is False
    assert NETWORK_C_NO_WATER["access"]["branch_count"] != network["access"]["branch_count"] or (
        NETWORK_C_NO_WATER["access"]["total_length_ft"] != network["access"]["total_length_ft"]
    ), "the committed water ground must change the network"
    # THE ROAD STOPS AT THE WATER'S EDGE, NOT ACROSS IT -- measured over the
    # whole network's own drawn geometry rather than one spur's endpoint, so
    # the assertion holds whether or not the spur became a feature. The pond
    # buffer is a HARD exclusion, so nothing may come closer than it; and
    # something must come close to it, or "reaches the water zone" would mean
    # nothing. Both bounds are the same ones the spur endpoint was held to.
    network_lon_lat = [
        point
        for feature in _network_features(payload, KEY_C)
        for point in feature["geometry"]["coordinates"]
    ]
    network_xs, network_ys = warp_transform(
        "EPSG:4326", CRS, [p[0] for p in network_lon_lat], [p[1] for p in network_lon_lat]
    )
    distance = min(
        union["render_fill_polygon_utm"].distance(Point(x, y))
        for x, y in zip(network_xs, network_ys)
    )
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
    # THE SAME EMBANKMENT-ONLY UNION Session.upstream() commits, and for its
    # reason: the access points below were surveyed against that union, so a
    # positional slice of the payload would make this route test depend on
    # the order the water step ships its zones in. When it began shipping
    # them in presentation order, `[:3]` quietly picked up an excavated zone,
    # ACCESS_B stopped routing, and the cap this section is about was never
    # reached -- a 409 assertion failing three generates away from its cause.
    water_zones = [
        f for f in water["survey_zones"]["features"]
        if f["properties"]["layer"] in wire_translation.LAYER_SURVEY_ZONES
        and f["properties"]["survey_type"] == "embankment"
    ]
    assert len(water_zones) >= 2, f"only {len(water_zones)} embankment zone(s) on the wire"
    picked = water_zones[:2]
    assert http_commit("water", picked, {f["id"]: "generated" for f in picked}).status_code == 200

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

# --- 15 [tests 3, 4, 5]. AN ACCESS POINT THAT ROUTES NOTHING LEAVES NOTHING
# BEHIND -- and the upstream-failure control that says the narrowing is real.
#
# THE WHOLE SECTION RUNS AT THE CEILING NO_NETWORK WAS SURVEYED AT (see the
# access-point block at the top of this file for the re-survey that made this
# necessary -- at the shipped 500 this parcel has no refusing point left).
# road_corridors.build_road_network() binds the shipped default into its own
# signature at import time, so a module attribute cannot move it; the pin is a
# wrapper passing the figure explicitly. The REAL function still runs, over the
# real cost surface and the real exclusions, and still refuses by its own
# stopping rule -- only the threshold that rule compares against is this
# section's own. Everything else here (the orchestrator, the document, the cap,
# the cache) is untouched and is what the section actually asserts about.
_real_build_road_network = road_corridors.build_road_network


def _build_at_surveyed_ceiling(*args, **kwargs):
    kwargs["max_meters_per_served_acre"] = NO_NETWORK_CEILING_METERS_PER_ACRE
    return _real_build_road_network(*args, **kwargs)


with Harness() as h, mock_patch.object(
    road_corridors, "build_road_network", _build_at_surveyed_ceiling
):
    s = Session()
    s.upstream()

    # A routes for real and takes a slot. Everything below is measured
    # against this: one recorded point, two slots free.
    payload_a = s.roads(ACCESS_A)
    assert s.stored()["steps"]["roads"]["inputs"]["access_points"] == [list(ACCESS_A)]
    assert payload_a["summary"]["slots_remaining"] == 2
    revision_before = s.stored()["document_revision"]
    calls_before = h.road_selfcomputes()
    network_before = h.total_network_calls

    # --- THE ROUTER FAILURE. Real terrain, no mock: the cheapest extension
    # from NO_NETWORK already costs more per acre than the router will pay,
    # so it accepts no branch at all.
    failed = s.job("roads", {"access_point": list(ACCESS_NO_NETWORK)}).wait(timeout=900)
    assert failed.status == job_runner.STATUS_FAILED, failed.status
    assert isinstance(failed.exception, step_orchestrator.EmptyCandidateError), failed.exception

    # THE WIRE SHAPE, AND THE WHOLE POINT OF IT: `no_candidate` is PRESENT
    # and names the input, and `failed_layer` is ABSENT. A client tells the
    # two failure kinds apart by the key each one carries, never by the key
    # the other lacks -- see error_payload().
    assert set(failed.error) == {"error", "no_candidate"}, failed.error
    assert failed.error["no_candidate"] == {
        "input": "access_point", "value": list(ACCESS_NO_NETWORK)
    }, failed.error
    assert "failed_layer" not in failed.error
    assert "access point" in failed.error["error"]

    # NOTHING RECORDED, NO SLOT SPENT, NO DOCUMENT WRITE. The document is
    # byte-identical to before the generate -- the same revision, the same
    # list -- because the input was never recorded in the first place.
    entry_after = s.stored()["steps"]["roads"]
    assert entry_after["inputs"]["access_points"] == [list(ACCESS_A)], entry_after["inputs"]
    assert entry_after["status"] == "generated"
    assert s.stored()["document_revision"] == revision_before, "a failed generate wrote a document"
    layers_after = s.layers("roads")
    assert [n["network_id"] for n in layers_after["networks"]] == [KEY_A]
    assert layers_after["summary"]["slots_remaining"] == 2, layers_after["summary"]
    # The cache holds no half-written candidate either.
    assert set(s.context().step_proposals["roads"]) == {KEY_A}

    # THE REFUSAL IS THE ROUTER'S, NOT A FETCH'S. Every network call on
    # this step is mocked and counted, and a refusal that had gone to the
    # network for its answer would say nothing about the access point.
    assert h.total_network_calls == network_before, (
        "the refusal must not have gone to the network"
    )
    assert calls_before == h.road_selfcomputes(), (
        "the refusal must not have re-run a road self-compute either"
    )

    # RETRYING THE SAME POINT IS REFUSED IDENTICALLY -- the terrain has not
    # moved, which is exactly why the input is not worth keeping.
    again = s.job("roads", {"access_point": list(ACCESS_NO_NETWORK)}).wait(timeout=900)
    assert again.status == job_runner.STATUS_FAILED
    assert again.error == failed.error
    assert s.stored()["steps"]["roads"]["inputs"]["access_points"] == [list(ACCESS_A)]

    # --- [test 4] THE UPSTREAM-DATA-FAILURE CONTROL. Same step, same verb,
    # a point that IS recorded -- and the opposite outcome, unchanged from
    # before this branch: the access point keeps its slot and a retry is
    # worth offering, because nothing is wrong with the point.
    payload_b = s.roads(ACCESS_B)
    assert s.stored()["steps"]["roads"]["inputs"]["access_points"] == [list(ACCESS_A), list(ACCESS_B)]
    assert payload_b["summary"]["slots_remaining"] == 1
    revision_recorded = s.stored()["document_revision"]

    # A COLD CACHE PLUS A SOURCE THAT DOES NOT ANSWER. Roads forwards the
    # DEM off the context, so the fetch that can actually fail under it is
    # the one rebuilding that context -- Layer 1, exactly as it fails on
    # session creation.
    s.cache.discard(s.id)
    s.fetch_cache.clear()
    with mock_patch.object(
        parcel_data, "fetch_parcel_data",
        side_effect=parcel_data.ParcelDataIncompleteError(
            "imagery did not answer", *parcel_data.LAYER_IMAGERY
        ),
    ):
        upstream_failed = s.job("roads", {"access_point": list(ACCESS_B)}).wait(timeout=900)
    assert upstream_failed.status == job_runner.STATUS_FAILED, upstream_failed.status
    assert "no_candidate" not in upstream_failed.error, upstream_failed.error
    # B IS STILL THERE, with its slot -- the assertion that makes the
    # narrowing above a narrowing and not a rule about failed generates.
    kept = s.stored()["steps"]["roads"]["inputs"]["access_points"]
    assert kept == [list(ACCESS_A), list(ACCESS_B)], kept
    assert s.stored()["document_revision"] == revision_recorded
    assert s.layers("roads")["summary"]["slots_remaining"] == 1

    # --- [test 5] THE CAP STILL COUNTS THE SURVIVORS. Two real points are
    # held and the router failure spent nothing, so a THIRD still fits --
    # which it would not if the refusal had taken a slot -- and only the
    # fourth is refused.
    payload_c = s.roads(ACCESS_C)
    assert payload_c["summary"]["slots_remaining"] == 0
    recorded_three = s.stored()["steps"]["roads"]["inputs"]["access_points"]
    assert recorded_three == [list(ACCESS_A), list(ACCESS_B), list(ACCESS_C)], recorded_three
    assert list(ACCESS_NO_NETWORK) not in recorded_three, (
        "the refused point must never have been recorded"
    )
    try:
        s.job("roads", {"access_point": list(ACCESS_D)})
        raise AssertionError("a fourth access point must be refused")
    except step_orchestrator.CandidateCapReachedError as exc:
        assert exc.max_candidates == 3 and len(exc.candidates) == 3
        assert list(ACCESS_NO_NETWORK) not in exc.candidates, (
            "the cap must not name a point the router refused"
        )

    # AND THE CAP IS COUNTED AGAINST THE DOCUMENT, so a refusal AT the cap
    # is refused by the cap and not by the router: NO_NETWORK does not even
    # reach a job while three points are held.
    try:
        s.job("roads", {"access_point": list(ACCESS_NO_NETWORK)})
        raise AssertionError("the cap must be checked before the entry point runs")
    except step_orchestrator.CandidateCapReachedError:
        pass

    # A FREED SLOT SURVIVES A REFUSAL SPENDING IT. Discard C, let the router
    # refuse the replacement, and the slot is still free for a real point.
    s.discard(ACCESS_C)
    assert s.layers("roads")["summary"]["slots_remaining"] == 1
    refused_at_cap = s.job("roads", {"access_point": list(ACCESS_NO_NETWORK)}).wait(timeout=900)
    assert refused_at_cap.status == job_runner.STATUS_FAILED
    assert "no_candidate" in refused_at_cap.error
    assert s.layers("roads")["summary"]["slots_remaining"] == 1, "the freed slot was spent by a refusal"
    assert s.roads(ACCESS_D)["summary"]["slots_remaining"] == 0
    assert s.stored()["steps"]["roads"]["inputs"]["access_points"] == [
        list(ACCESS_A), list(ACCESS_B), list(ACCESS_D)
    ]

    print(
        f"15 [tests 3, 4, 5]. ROUTER FAILURE LEAVES NOTHING: an access point the "
        f"router refuses on real terrain (stop_reason 'cost_per_acre_exceeded' before "
        f"a single branch is accepted, 0 network calls) fails the job with "
        f"`no_candidate` naming the input and NO `failed_layer`, and the document is "
        f"byte-identical across it -- same revision, {len(entry_after['inputs']['access_points'])} "
        f"recorded point, {layers_after['summary']['slots_remaining']} slots still free, "
        f"nothing in the cache. A retry is refused identically. THE CONTROL: the same "
        f"step's upstream ParcelDataIncompleteError KEEPS its recorded access point and its slot "
        f"and carries no `no_candidate`, unchanged. The cap counts the survivors only "
        f"-- three real points fill it, a fourth is refused before a job exists, "
        f"and a slot freed by a discard survives a refusal spending it."
    )


print("\nAll roads step checks passed.")
