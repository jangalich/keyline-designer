"""
test_roads_step.py

THE ROADS STEP -- the registry's third entry, and the first that breaks the
model the first two share. Run as:

    python test_roads_step.py

REAL COORDINATES, REAL PIPELINE CODE, THE SAME FIXTURE AS THE PRIOR
BRANCHES. The boundary is the actual drawn property from generate_full_
report.py -- 5614 N Montour Rd, Gibsonia, PA (~13.23 acres, UTM 17N) -- and
the DEM is test_water_step.py's bench-and-drainage fixture with its flanking
levees, unchanged, so the water zones committed here are the zones that
branch asserted over. session_manager.create_session(), the terrain warm-up,
the landform and water generates, the commit gate, the rehydrators,
road_corridors.identify_road_corridor_candidates() and the whole payload
assembly all RUN, for real. What is mocked is the NETWORK and only the
network -- plus ONE pipeline function, fetch_and_select_optimal_water_zone,
replaced by a counter because section 10 exists to prove it is never
reached, and a real call would itself go to the network.

HOW ROADS DIFFERS, and why this file's sections look nothing like water's.
identify_road_corridor_candidates() returns ONE network per call; the
branches inside it are a tree, not alternatives. So the candidates are
NETWORKS, one per ACCESS POINT, and the user generates them by trying
different access points. Results accumulate (up to three), any may be
discarded, and exactly one -- or none -- is committed.

Sections (the branch's numbered tests in brackets):
  1  [1]  REGISTRY -- the roads entry's shape, validate_registry() with
          every entry (four, since the trees branch), constants agree with
          the modules that own them.
  2  [2]  GENERATE with an access point -> one network. Zero network calls.
  3  [3]  ACCUMULATION -- A then B; both exist; A is byte-identical.
  4  [4]  ID STABILITY across accumulated generates and a cache eviction.
  5  [5]  INDEPENDENCE -- A twice is identical; B-then-A == A-then-B.
          THE ONE THAT DECIDES WHETHER THE INTERACTION IS SOUND.
  6  [6]  CAP -- a fourth generate is refused server-side, before a job.
  7  [7]  DISCARD frees a slot.
  8  [8]  max_features 1 -- a two-network commit is rejected; so is half a
          network.
  9  [9]  EMPTY COMMIT is legal and records no road.
 --- sections 10-15 live in test_roads_step_inputs.py, same fixture ---
 10  [10] WATER EMPTY -> SENTINEL -- fetch_and_select_optimal_water_zone()
          did NOT run. Counted, with a control. THE ONE THAT FAILS SILENTLY.
 11  [11] Water multi-select reaches roads as a UNION.
 12  [12] A commit missing its required input is REJECTED.
 13  [13] REOPEN restores every candidate, not just the committed one.
 14  [14] _API_ERRORS -- a failed POST /api/sessions carries failed_layer.
 15  [15] Regression is the other test files, run separately.
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


# --- 1 [test 1]. THE REGISTRY ENTRY ----------------------------------

step_registry.validate_registry()
assert step_registry.registered_steps() == ("landform", "water", "roads", "trees", "structures", "fencing"), step_registry.registered_steps()
ROADS = step_registry.get_step("roads")

assert ROADS.generate == "road_corridors.identify_road_corridor_candidates"
assert ROADS.payload == "step_orchestrator.build_roads_payload"
assert ROADS.proposal_collection == "road_corridors"
assert ROADS.produces == ("selected_road_corridor",)
assert ROADS.upstream_steps() == ("landform", "water"), ROADS.upstream_steps()

_consumed = {c.name: c for c in ROADS.consumes}
assert set(_consumed) == {
    "boundary_coordinates", "dem", "boundary_polygon_utm", "valleys", "canopy_height",
    "hydric_floodplain_union", "floodplain_data_is_fallback", "production_areas",
    "selected_water_zone",
}, sorted(_consumed)
_water_edge = _consumed["selected_water_zone"]
assert _water_edge.source == step_registry.SOURCE_COMMITTED and _water_edge.from_step == "water"
assert _water_edge.combine == "wire_translation.water_zone_union"
assert _water_edge.empty_commit == "water_suitability.NO_WATER_ZONE", (
    "THE line section 10 exists for: without it an empty water commit reaches "
    "the entry point as None and the water self-compute runs"
)
assert step_registry.resolve(_water_edge.empty_commit) is water_suitability.NO_WATER_ZONE

# THE ACCESS POINT, DECLARED. The first real use of user_inputs.
assert len(ROADS.user_inputs) == 1
_access = ROADS.user_inputs[0]
assert isinstance(_access, step_registry.UserInput)
assert _access.name == "access_point" and _access.parameter == "anchor_lon_lat"
assert _access.shape == step_registry.INPUT_SHAPE_LON_LAT
assert _access.validate == "road_corridors.validate_access_point_on_boundary"
assert step_registry.resolve(_access.validate) is road_corridors.validate_access_point_on_boundary, (
    "the validator is the pipeline's own, not a second one"
)

# ACCUMULATION, DECLARED.
assert ROADS.accumulate is not None
assert ROADS.accumulate.keyed_by == "access_point"
assert ROADS.accumulate.inputs_list == "access_points"
assert ROADS.accumulate.feature_key_property == "network_id"
assert ROADS.accumulate.max_candidates == 3
assert step_registry.resolve(ROADS.accumulate.key) is wire_translation.access_point_key

# AN INPUT THAT PRODUCES NO CANDIDATE, DECLARED. The orchestrator cannot read
# a road network's shape, so the test for "the router grew nothing" is the
# roads module's own function and the registry names it. Landform and water
# leave it None, which is what keeps their generates untouched.
assert ROADS.accumulate.empty_result == "road_corridors.road_network_is_empty"
assert step_registry.resolve(ROADS.accumulate.empty_result) is road_corridors.road_network_is_empty
assert ROADS.accumulate.empty_error and "access point" in ROADS.accumulate.empty_error, (
    "the empty-result prose must be about the access point; generic_error "
    "describes a generate that broke and this one did not"
)
assert step_registry.get_step("landform").accumulate is None
assert step_registry.get_step("water").accumulate is None

# THE TWO ROUTING CONSTANTS THIS BRANCH MOVED, asserted against the module
# that owns them rather than restated here.
assert road_network_router.MAX_ROAD_METERS_PER_SERVED_ACRE == 250.0
assert road_network_router.PRODUCTION_SERVICE_RADIUS_METERS == 25.0
assert not hasattr(road_corridors, "MIN_CORRIDOR_LENGTH_METERS"), (
    "the network-length floor is deleted, not merely unused: a constant left "
    "behind is one a later caller can pass again"
)


# THE COMMIT CONTRACT: one network or none, counted in networks.
_contract = ROADS.commit_contract
assert _contract.layers == (wire_translation.LAYER_ROAD_CORRIDOR,)
assert _contract.geometry_types == ("LineString",)
assert _contract.min_features == 0 and _contract.max_features == 1
assert _contract.feature_group == "network_id"
assert _contract.group_check == "wire_translation.check_road_network_complete"
assert _contract.rehydrate == "wire_translation.rehydrate_road_networks"
assert _contract.internal_id_parameter is None and _contract.requires_provenance
assert callable(step_registry.resolve(_contract.rehydrate))
assert callable(step_registry.resolve(_contract.group_check))
assert callable(ROADS.resolve_generate()) and callable(ROADS.resolve_payload())

# EVERY FORWARDED NAME IS A REAL PARAMETER of the real entry point.
import inspect as _inspect  # noqa: E402

_signature = _inspect.signature(road_corridors.identify_road_corridor_candidates).parameters
assert "min_corridor_length_meters" not in _inspect.signature(
    road_corridors.build_road_network
).parameters, "the deleted floor must not survive as a parameter callers can still pass"
for _c in ROADS.consumes:
    if _c.forward_as:
        assert _c.forward_as in _signature, f"{_c.name} forwards as {_c.forward_as!r}, not a parameter"
assert _access.parameter in _signature

# THE CASCADE EDGES, read off the declarations.
assert step_registry.dependents_of("water") == ("roads", "trees", "structures", "fencing")
assert step_registry.dependents_of("landform") == ("water", "roads", "trees", "structures", "fencing")
assert step_registry.transitive_dependents("landform") == ("water", "roads", "trees", "structures", "fencing")
assert step_registry.transitive_dependents("roads") == ("trees", "structures", "fencing"), (
    "trees consumes the roads commit as of the trees branch"
)

# CONSTANTS AGREE with the modules that own them.
assert parcel_data.LAYER_CANOPY == production_zone_payload.LAYER_CANOPY

# THE BARE-STRING SHAPE IS REJECTED BY NAME.
import dataclasses as _dc  # noqa: E402

try:
    with mock_patch.dict(step_registry.STEP_REGISTRY, {"roads": _dc.replace(ROADS, user_inputs=("access_point",))}):
        step_registry.validate_registry()
except step_registry.RegistryError as exc:
    assert "not a UserInput" in str(exc), exc
else:
    raise AssertionError("a bare user input name must be rejected")
try:
    with mock_patch.dict(
        step_registry.STEP_REGISTRY,
        {"roads": _dc.replace(ROADS, accumulate=_dc.replace(ROADS.accumulate, keyed_by="anchor"))},
    ):
        step_registry.validate_registry()
except step_registry.RegistryError as exc:
    assert "does not declare" in str(exc), exc
else:
    raise AssertionError("accumulating by an undeclared input must be rejected")

print(
    f"1 [test 1]. REGISTRY: validate_registry() passes with every entry "
    f"{step_registry.registered_steps()}. The roads entry consumes {len(ROADS.consumes)} "
    f"edges (7 cache, 2 committed: landform's production areas, water's zones as a union "
    f"with empty_commit=NO_WATER_ZONE), declares the access point as a UserInput "
    f"(shape {_access.shape!r}, forward_as {_access.parameter!r}, validated by "
    f"road_corridors' own validator), accumulates by it with a cap of "
    f"{ROADS.accumulate.max_candidates}, and commits at most {_contract.max_features} "
    f"{_contract.feature_group} group. dependents_of('water') == "
    f"{step_registry.dependents_of('water')}."
)


# --- 2 [test 2]. GENERATE WITH AN ACCESS POINT -> ONE NETWORK ----------

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=2)

    # THE ACCESS POINT IS NEVER AUTO-ARMED: a roads generate with no params
    # is refused, synchronously, before a job exists.
    try:
        s.job("roads")
    except step_orchestrator.StepOrchestrationError as exc:
        assert "requires user input(s) ['access_point']" in str(exc), exc
    else:
        raise AssertionError("a roads generate must require its access point")

    # SHAPE: a string, a three-element array, a [lat, lon] out of range.
    for bad in ("here", [1, 2, 3], [40.64, -79.98, 0][:2][::-1], [200.0, 40.6], [True, 1.0]):
        try:
            s.job("roads", {"access_point": bad})
        except step_orchestrator.StepOrchestrationError as exc:
            assert "[lon, lat]" in str(exc) or "rejected" in str(exc), (bad, exc)
        else:
            raise AssertionError(f"{bad!r} must be refused as an access point")

    # ON THE BOUNDARY: the parcel centroid is not where the parcel meets a
    # road. road_corridors' own validator, through the registry.
    try:
        s.job("roads", {"access_point": list(INTERIOR_POINT)})
    except step_orchestrator.StepOrchestrationError as exc:
        assert "from the property boundary edge" in str(exc), exc
    else:
        raise AssertionError("an interior point must be refused as an access point")
    assert h.identify_roads.call_count == 0, "no job was created for any refusal above"

    network_before = h.total_network_calls
    selfcomputes_before = h.road_selfcomputes()
    payload_a = s.roads(ACCESS_A)
    roads_network_calls = h.total_network_calls - network_before
    selfcomputes = {k: v - selfcomputes_before[k] for k, v in h.road_selfcomputes().items()}

    assert sorted(payload_a) == ["networks", "road_corridors", "summary"], sorted(payload_a)
    assert h.identify_roads.call_count == 1
    assert len(payload_a["networks"]) == 1
    NETWORK_A = _network(payload_a, KEY_A)
    assert NETWORK_A["network_found"], (
        f"the fixture must produce a network from A, or every assertion below is "
        f"vacuous: stop_reason {NETWORK_A['stop_reason']!r}"
    )
    FEATURES_A = _network_features(payload_a, KEY_A)
    assert FEATURES_A and len(FEATURES_A) == NETWORK_A["access"]["branch_count"]
    assert len(payload_a["road_corridors"]["features"]) == len(FEATURES_A)
    assert NETWORK_A["feature_ids"] == [f["id"] for f in FEATURES_A]
    assert NETWORK_A["access_point"] == [ACCESS_A[0], ACCESS_A[1]]

    # THE BRANCHES ARE A TREE, not alternatives: exactly one trunk, every
    # other branch joins one that is in the network.
    roles = [f["properties"]["branch_role"] for f in FEATURES_A]
    indexes = {f["properties"]["branch_index"] for f in FEATURES_A}
    assert roles.count("trunk") == 1 and roles[0] == "trunk"
    for f in FEATURES_A[1:]:
        assert f["properties"]["joins_branch_index"] in indexes
    for f in FEATURES_A:
        assert f["properties"]["layer"] == wire_translation.LAYER_ROAD_CORRIDOR
        assert f["geometry"]["type"] == "LineString"
        assert f["properties"]["network_id"] == KEY_A
        assert f["properties"]["access_point"] == [ACCESS_A[0], ACCESS_A[1]]
        assert f["id"].startswith(f"road-corridor-{KEY_A}-"), f["id"]
        assert "unserved_acres" in f["properties"]

    # THE PER-NETWORK NARRATIVE BLOCK, whole, on the network record.
    for key in ("network_found", "stop_reason", "determination", "access", "branches"):
        assert key in NETWORK_A, sorted(NETWORK_A)
    assert NETWORK_A["determination"]["water_zone_excluded"] is True, (
        "water zones were committed; the union must have been hard-excluded"
    )
    assert isinstance(NETWORK_A["access"]["reaches_water_zone"], bool)
    assert NETWORK_A["determination"]["floodplain_data_available"] is True, (
        "the warm-up's floodplain union (hydric fixture polygon) must have reached routing"
    )
    assert NETWORK_A["determination"]["floodplain_data_is_fallback"] is False
    assert NETWORK_A["determination"]["canopy_data_available"] is True
    assert payload_a["summary"] == {"network_count": 1, "max_networks": 3, "slots_remaining": 2}

    # ZERO NETWORK, ZERO SELF-COMPUTE. Every one of the nine edges did its job.
    assert roads_network_calls == 0, roads_network_calls
    assert selfcomputes == {
        "delineate_valleys": 0,
        "identify_optimized_production_areas": 0,
        "_fetch_floodplain_hydric_union": 0,
        "fetch_and_select_optimal_water_zone": 0,
    }, selfcomputes
    assert h.road_canopy_mask.call_count == 1 and h.canopy_refetch.call_count == 0

    # THE DOCUMENT: generated, no features, and the access point RECORDED.
    entry = s.stored()["steps"]["roads"]
    assert entry["status"] == design_document.STATUS_GENERATED
    assert not entry.get("features")
    assert entry["inputs"] == {"access_points": [[ACCESS_A[0], ACCESS_A[1]]]}, entry.get("inputs")

    # THE READ VERB returns the same payload from the cache.
    assert s.layers("roads") == payload_a

print(
    f"2 [test 2]. GENERATE: with access point A, ONE network -- "
    f"{NETWORK_A['access']['branch_count']} branch(es) "
    f"({', '.join(roles)}), {NETWORK_A['access']['total_length_ft']} ft, "
    f"{NETWORK_A['access']['served_acres']} acres served of "
    f"{NETWORK_A['access']['served_acres'] + NETWORK_A['access']['unserved_acres']:.1f}, "
    f"stop_reason {NETWORK_A['stop_reason']!r}, reaches_water_zone="
    f"{NETWORK_A['access']['reaches_water_zone']}. {roads_network_calls} network calls "
    f"and every road self-compute at zero. A missing, malformed, or interior access "
    f"point is refused before a job exists. The document records the access point."
)


# --- 3-7. ACCUMULATION, ID STABILITY, INDEPENDENCE, CAP, DISCARD -------
#
# One session, because these are five statements about ONE store of
# candidate sets as it grows, is evicted, is regenerated into, hits its cap
# and is discarded from. A fresh session per section would test five
# stores that never held more than one thing.

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=2)

    payload_a = s.roads(ACCESS_A)
    FEATURES_A = copy.deepcopy(_network_features(payload_a, KEY_A))
    NETWORK_A = copy.deepcopy(_network(payload_a, KEY_A))
    IDS_A = [f["id"] for f in FEATURES_A]
    revision_after_a = s.stored()["document_revision"]

    # --- 3 [test 3]. ACCUMULATION: A, then B. Both exist; A is untouched.
    payload_ab = s.roads(ACCESS_B)
    assert [n["network_id"] for n in payload_ab["networks"]] == [KEY_A, KEY_B], (
        "generating for B must ADD a candidate, in the order tried, not replace A"
    )
    assert _network_features(payload_ab, KEY_A) == FEATURES_A, (
        "A's features must be byte-identical after B's generate"
    )
    assert _network(payload_ab, KEY_A) == NETWORK_A, "A's narrative block must be untouched"
    NETWORK_B = _network(payload_ab, KEY_B)
    FEATURES_B = _network_features(payload_ab, KEY_B)
    assert NETWORK_B["network_found"] and FEATURES_B, NETWORK_B["stop_reason"]
    assert len(payload_ab["road_corridors"]["features"]) == len(FEATURES_A) + len(FEATURES_B)
    assert h.identify_roads.call_count == 2, (
        f"B's generate must not recompute A: {h.identify_roads.call_count} entry-point calls"
    )
    assert s.stored()["steps"]["roads"]["inputs"] == {
        "access_points": [list(ACCESS_A), list(ACCESS_B)]
    }
    assert payload_ab["summary"] == {"network_count": 2, "max_networks": 3, "slots_remaining": 1}

    # THE CACHE SHAPE: keyed, in order, one result per access point.
    store = s.context().step_proposals["roads"]
    assert list(store) == [KEY_A, KEY_B]
    assert store[KEY_A]["inputs"] == {"access_point": ACCESS_A}
    assert "road_network" in store[KEY_A]["result"]

    print(
        f"3 [test 3]. ACCUMULATION: A then B -> two candidates in the order tried "
        f"([{KEY_A}, {KEY_B}]); A's {len(FEATURES_A)} features and narrative block are "
        f"byte-identical after B's generate; B has {len(FEATURES_B)} branches "
        f"({NETWORK_B['access']['served_acres']} acres served). The entry point ran "
        f"{h.identify_roads.call_count} times -- once per access point, never again for A. "
        f"The document records both access points; the cache holds a keyed store."
    )

    # --- 4 [test 4]. ID STABILITY across accumulated generates and an
    # eviction. The ids carry the access point's identity, so B's
    # numbering cannot renumber A's.
    assert [f["id"] for f in _network_features(payload_ab, KEY_A)] == IDS_A
    IDS_B = [f["id"] for f in FEATURES_B]
    assert not set(IDS_A) & set(IDS_B), "two networks must never share a feature id"
    assert all(i.startswith(f"road-corridor-{KEY_B}-") for i in IDS_B)
    # A's branch ordinals and B's are both 1..n -- the collision the key
    # exists to prevent, shown rather than assumed.
    ordinals_a = [wire_translation.internal_road_branch_identity(i)[1] for i in IDS_A]
    ordinals_b = [wire_translation.internal_road_branch_identity(i)[1] for i in IDS_B]
    assert ordinals_a[0] == ordinals_b[0] == 0, "both trunks are branch 0 -- only the key tells them apart"

    calls_before_eviction = h.identify_roads.call_count
    assert s.cache.discard(s.id), "the session should have been cached"
    assert s.id not in s.cache
    rebuilt = s.layers("roads")
    assert h.identify_roads.call_count == calls_before_eviction + 2, (
        "a cold read regenerates EVERY recorded access point -- both of them"
    )
    assert _comparable(rebuilt) == _comparable(payload_ab), "the rebuild must reproduce both networks"
    assert [n["network_id"] for n in rebuilt["networks"]] == [KEY_A, KEY_B], "in the document's order"
    assert [f["id"] for f in _network_features(rebuilt, KEY_A)] == IDS_A
    assert [f["id"] for f in _network_features(rebuilt, KEY_B)] == IDS_B

    print(
        f"4 [test 4]. ID STABILITY: A's ids {IDS_A} are unchanged by B's generate, "
        f"disjoint from B's {IDS_B} though both trunks are branch 0, and both id sets "
        f"survive a cache eviction and a rebuild from the document's recorded access "
        f"points ({h.identify_roads.call_count - calls_before_eviction} regenerates)."
    )

    # --- 5 [test 5]. INDEPENDENCE. THE ONE THAT DECIDES WHETHER THE
    # INTERACTION IS SOUND. If a second generate saw the first network as
    # existing infrastructure, the alternatives would not be comparable.
    #
    # (a) The same access point twice is identical -- and replaces, holding
    #     no new slot and writing nothing to the document.
    revision_before = s.stored()["document_revision"]
    payload_aab = s.roads(ACCESS_A)
    assert _network_features(payload_aab, KEY_A) == FEATURES_A
    assert _network(payload_aab, KEY_A) == NETWORK_A
    assert _network_features(payload_aab, KEY_B) == FEATURES_B, "B untouched by A's regenerate"
    assert [n["network_id"] for n in payload_aab["networks"]] == [KEY_A, KEY_B]
    assert s.stored()["document_revision"] == revision_before, (
        "a regenerate for a recorded access point writes nothing"
    )

    # (b) B then A, in a fresh session, equals A then B -- feature for
    #     feature: geometry, length, grade, served acres, ids.
    s2 = Session()
    s2.upstream(water_zone_count=2)
    payload_b_first = s2.roads(ACCESS_B)
    payload_ba = s2.roads(ACCESS_A)
    assert [n["network_id"] for n in payload_ba["networks"]] == [KEY_B, KEY_A], "order tried is kept"
    assert _comparable(payload_ba) == _comparable(payload_ab), (
        "B-then-A must equal A-then-B. It does not: the networks depend on the "
        "order they were generated in, so the alternatives are not comparable "
        "and the accumulate interaction is unsound. STOP."
    )
    # And B generated FIRST (with no A in the session) equals B generated
    # second (with A already there): A was never existing infrastructure.
    assert _network_features(payload_b_first, KEY_B) == FEATURES_B
    assert _network(payload_b_first, KEY_B) == NETWORK_B

    # (c) The geometry is genuinely different between A and B -- so (b) is
    #     not two copies of one network agreeing with themselves.
    assert {f["geometry"]["coordinates"][0] for f in FEATURES_A} != {
        f["geometry"]["coordinates"][0] for f in FEATURES_B
    }, "A and B must start from different cells"
    assert NETWORK_A["access"]["total_length_ft"] != NETWORK_B["access"]["total_length_ft"] or (
        NETWORK_A["branches"] != NETWORK_B["branches"]
    )

    print(
        f"5 [test 5]. INDEPENDENCE: A generated twice is identical and writes "
        f"nothing; B-then-A (fresh session) equals A-then-B feature for feature -- "
        f"same {len(FEATURES_A) + len(FEATURES_B)} features, same geometry, lengths "
        f"({NETWORK_A['access']['total_length_ft']} ft / "
        f"{NETWORK_B['access']['total_length_ft']} ft), served acres "
        f"({NETWORK_A['access']['served_acres']} / {NETWORK_B['access']['served_acres']}) "
        f"and ids; and B generated alone equals B generated after A. Each network "
        f"routes on its own cost surface: an earlier network is never existing "
        f"infrastructure to a later one. ORDER-INDEPENDENT."
    )

    # --- 6 [test 6]. THE CAP, server-side, before a job.
    payload_abc = s.roads(ACCESS_C)
    assert [n["network_id"] for n in payload_abc["networks"]] == [KEY_A, KEY_B, KEY_C]
    NETWORK_C = _network(payload_abc, KEY_C)
    assert NETWORK_C["network_found"], NETWORK_C["stop_reason"]
    assert payload_abc["summary"]["slots_remaining"] == 0
    calls_at_cap = h.identify_roads.call_count
    try:
        s.job("roads", {"access_point": list(ACCESS_D)})
    except step_orchestrator.CandidateCapReachedError as exc:
        assert exc.step_id == "roads" and exc.max_candidates == 3
        assert exc.candidates == [list(ACCESS_A), list(ACCESS_B), list(ACCESS_C)]
    else:
        raise AssertionError("a fourth access point must be refused")
    assert h.identify_roads.call_count == calls_at_cap, "refused BEFORE a job -- nothing ran"
    assert s.stored()["steps"]["roads"]["inputs"]["access_points"] == [
        list(ACCESS_A), list(ACCESS_B), list(ACCESS_C)
    ], "nothing was recorded for D"
    # A regenerate for a RECORDED access point is not a fourth candidate.
    payload_at_cap = s.roads(ACCESS_B)
    assert [n["network_id"] for n in payload_at_cap["networks"]] == [KEY_A, KEY_B, KEY_C]
    # THE CAP IS THE DOCUMENT'S, not the cache's: evict, and it still holds.
    s.cache.discard(s.id)
    try:
        s.job("roads", {"access_point": list(ACCESS_D)})
    except step_orchestrator.CandidateCapReachedError:
        pass
    else:
        raise AssertionError("the cap must hold across an eviction")

    print(
        f"6 [test 6]. CAP: with A, B and C held, a fourth access point (D) is refused "
        f"synchronously with CandidateCapReachedError naming the three candidates -- "
        f"no job, no entry-point call, nothing recorded -- while a regenerate for B "
        f"still replaces B's set. The cap holds against the document, so it survives "
        f"an eviction."
    )

    # --- 7 [test 7]. DISCARD frees a slot.
    discarded = s.discard(ACCESS_B)
    assert discarded["steps"]["roads"]["status"] == design_document.STATUS_GENERATED
    assert discarded["steps"]["roads"]["inputs"] == {"access_points": [list(ACCESS_A), list(ACCESS_C)]}
    payload_ac = s.layers("roads")
    assert [n["network_id"] for n in payload_ac["networks"]] == [KEY_A, KEY_C]
    assert _network_features(payload_ac, KEY_A) == FEATURES_A
    assert _network(payload_ac, KEY_C) == NETWORK_C
    assert payload_ac["summary"]["slots_remaining"] == 1
    assert KEY_B not in s.context().step_proposals["roads"]
    try:
        s.discard(ACCESS_B)
    except step_orchestrator.CandidateNotFoundError:
        pass
    else:
        raise AssertionError("discarding a candidate twice must be refused")
    # A step that does not accumulate has nothing to discard.
    try:
        step_orchestrator.discard_candidate(s.id, "water", s.store, params={}, fetch_cache=s.fetch_cache, cache=s.cache)
    except step_orchestrator.StepOrchestrationError as exc:
        assert "does not accumulate" in str(exc)
    else:
        raise AssertionError("water does not accumulate")
    # The freed slot takes D.
    payload_acd = s.roads(ACCESS_D)
    assert [n["network_id"] for n in payload_acd["networks"]] == [KEY_A, KEY_C, KEY_D]
    assert _network(payload_acd, KEY_D)["network_found"]

    print(
        f"7 [test 7]. DISCARD: discarding B leaves [A, C] in the document and the "
        f"cache (A and C byte-identical), a second discard of B is CandidateNotFoundError, "
        f"water has nothing to discard, and the freed slot takes D -> [A, C, D]."
    )


# --- 8, 12, 13, 9. THE COMMIT: ONE NETWORK OR NONE, ITS INPUT REQUIRED,
# EVERY CANDIDATE RESTORED, EMPTY LEGAL -------------------------------

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=2)
    payload = s.roads(ACCESS_A)
    payload = s.roads(ACCESS_B)
    payload = s.roads(ACCESS_C)
    features_a, provenance_a = _commit_body(payload, KEY_A)
    features_b, provenance_b = _commit_body(payload, KEY_B)
    ALL_INPUTS = {"access_points": [list(ACCESS_A), list(ACCESS_B), list(ACCESS_C)]}
    assert len(features_a) >= 2 and len(features_b) >= 2, "both networks need a spur for section 8"

    # --- 8 [test 8]. max_features 1, COUNTED IN NETWORKS.
    try:
        s.commit("roads", features_a + features_b, {**provenance_a, **provenance_b},
                 base_revision=0, inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        codes = [r.code for r in exc.rejections]
        assert commit_validation.REJECT_TOO_MANY in codes, codes
        reason = next(r.reason for r in exc.rejections if r.code == commit_validation.REJECT_TOO_MANY)
        assert "at most 1 network_id group" in reason and "2 were committed" in reason, reason
    else:
        raise AssertionError("a two-network commit must be rejected")
    # ...and NOT merely "too many features": one whole network of 5
    # branches is fine, because the unit is the network.
    committed_b = s.commit("roads", features_b, provenance_b, base_revision=0, inputs=ALL_INPUTS)
    assert committed_b["steps"]["roads"]["status"] == design_document.STATUS_COMMITTED
    assert len(committed_b["steps"]["roads"]["features"]["features"]) == len(features_b)
    s.reopen("roads")
    # HALF A NETWORK IS INCOHERENT: a spur without its trunk.
    spur_only = [f for f in features_a if f["properties"]["branch_role"] != "trunk"]
    try:
        s.commit("roads", spur_only, {f["id"]: "generated" for f in spur_only},
                 base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        codes = {r.code for r in exc.rejections}
        assert codes == {commit_validation.REJECT_INCOHERENT_GROUP}, codes
        assert "trunk (branch 0) is not in the commit" in exc.rejections[0].reason
    else:
        raise AssertionError("a spur without its trunk must be rejected")
    # A feature with no network id cannot be counted as anything.
    stripped = copy.deepcopy(features_a)
    del stripped[0]["properties"]["network_id"]
    try:
        s.commit("roads", stripped, provenance_a, base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        assert commit_validation.REJECT_MISSING_GROUP in {r.code for r in exc.rejections}
    else:
        raise AssertionError("a feature with no network_id must be rejected")
    # A foreign id is refused by the rehydrator, not invented for.
    foreign = copy.deepcopy(features_a)
    foreign[0]["id"] = "road-corridor-drawn-by-hand"
    try:
        s.commit("roads", foreign, {**provenance_a, "road-corridor-drawn-by-hand": "user_added"},
                 base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    except commit_validation.CommitRejectedError as exc:
        codes = {r.code for r in exc.rejections}
        assert codes & {commit_validation.REJECT_INCOHERENT_GROUP, commit_validation.REJECT_INVALID_GEOMETRY}, codes
    else:
        raise AssertionError("an id this pipeline did not mint must be refused")
    print(
        f"8 [test 8]. max_features 1: a two-network commit ({len(features_a)} + "
        f"{len(features_b)} branches) is rejected as too_many_features counted in "
        f"network_id groups; one whole {len(features_b)}-branch network commits; a spur "
        f"without its trunk is incoherent_feature_group; a branch with no network_id is "
        f"missing_feature_group; a hand-made id is refused."
    )

    # --- 12 [test 12]. A COMMIT MISSING ITS REQUIRED INPUT IS REJECTED.
    for missing in (None, {}, {"access_point": list(ACCESS_A)}, {"access_points": None}):
        try:
            s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"), inputs=missing)
        except step_orchestrator.CommitInputError as exc:
            assert "access_point" in str(exc), exc
        else:
            raise AssertionError(f"inputs={missing!r} must be refused: an absent input is not a decision")
    assert s.stored()["steps"]["roads"]["status"] == design_document.STATUS_GENERATED, "nothing written"
    # Declared, but not the access point this network came from.
    try:
        s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"),
                 inputs={"access_points": [list(ACCESS_B)]})
    except commit_validation.CommitRejectedError as exc:
        codes = {r.code for r in exc.rejections}
        assert codes == {commit_validation.REJECT_INPUT_NOT_DECLARED}, codes
        assert {r.feature_id for r in exc.rejections} == {f["id"] for f in features_a}
    else:
        raise AssertionError("a network whose access point is not declared must be rejected")
    # Declared, but off the boundary.
    try:
        s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"),
                 inputs={"access_points": [list(ACCESS_A), list(INTERIOR_POINT)]})
    except step_orchestrator.StepOrchestrationError as exc:
        assert "from the property boundary edge" in str(exc)
    else:
        raise AssertionError("an interior access point in a commit must be refused")
    # Declared, wrong shape.
    try:
        s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"),
                 inputs={"access_points": [[ACCESS_A[0]]]})
    except step_orchestrator.StepOrchestrationError as exc:
        assert "[lon, lat]" in str(exc)
    else:
        raise AssertionError("a malformed access point in a commit must be refused")
    print(
        f"12 [test 12]. REQUIRED INPUT: a roads commit with inputs absent, empty, "
        f"singular, or null is CommitInputError (400) with nothing written; one "
        f"declaring only B for A's network is rejected per feature as "
        f"input_not_declared; an interior or malformed access point in the list is "
        f"refused."
    )

    # THE REAL COMMIT: network A, every access point declared.
    committed = s.commit("roads", features_a, provenance_a, base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    entry = committed["steps"]["roads"]
    assert entry["status"] == design_document.STATUS_COMMITTED
    assert entry["inputs"] == ALL_INPUTS, "the document carries EVERY access point tried"
    assert [f["id"] for f in entry["features"]["features"]] == [f["id"] for f in features_a]
    for f in entry["features"]["features"]:
        assert "exclusion_crossings" in f["properties"], "crossings are recorded, as for every step"
    # THE REHYDRATED COMMITTED VALUE: one network, the shape consumers read.
    cached = s.context().step_committed["roads"]["value"]
    assert len(cached) == 1 and cached[0]["network_id"] == KEY_A
    network = cached[0]
    assert len(network["branches"]) == len(features_a)
    assert network["cells"] and not network["cell_footprint_polygon_utm"].is_empty
    assert network["branches"][0]["branch_role"] == "trunk"
    assert abs(network["total_length_meters"] - _network(payload, KEY_A)["access"]["total_length_ft"] * 0.3048) < 0.1
    # ...and it round-trips: the rehydrated cells are the cells the router
    # walked, so a consumer excluding the road excludes the real footprint.
    original = s.context().step_proposals["roads"][KEY_A]["result"]["road_network"]
    assert network["cells"] == original["cells"], "the rehydrated cells must be the routed cells, exactly"
    assert network["cell_footprint_polygon_utm"].equals(original["cell_footprint_polygon_utm"])
    assert step_orchestrator.committed_internal_value(s.context(), s.stored(), "roads")[0]["network_id"] == KEY_A
    # A cold read agrees with the warm one.
    s.cache.discard(s.id)
    cold = step_orchestrator.committed_internal_value(s.context(), s.stored(), "roads")
    assert cold[0]["cells"] == original["cells"]

    # --- 13 [test 13]. REOPEN RESTORES EVERY CANDIDATE.
    s.cache.discard(s.id)  # a reopen after an eviction is the harder case
    calls_before = h.identify_roads.call_count
    reopened = s.reopen("roads")
    assert reopened["steps"]["roads"]["status"] == design_document.STATUS_GENERATED
    assert reopened["steps"]["roads"]["inputs"] == ALL_INPUTS
    restored = s.context().step_restored["roads"]
    assert [n["network_id"] for n in restored["payload"]["networks"]] == [KEY_A, KEY_B, KEY_C], (
        "the restore must bring back ALL THREE candidates, not just the committed one"
    )
    assert h.identify_roads.call_count == calls_before + 3
    assert restored["selected_feature_ids"] == [f["id"] for f in features_a], restored["selected_feature_ids"]
    assert restored["missing_feature_ids"] == [], restored["missing_feature_ids"]
    assert restored["user_added"]["features"] == []
    assert _network_features(restored["payload"], KEY_A) == [
        {**f, "properties": {k: v for k, v in f["properties"].items() if k != "exclusion_crossings"}}
        for f in features_a
    ] or _network_features(restored["payload"], KEY_A) == features_a
    assert _network_features(restored["payload"], KEY_B) == features_b
    # The read verb after a reopen serves the same three.
    assert [n["network_id"] for n in s.layers("roads")["networks"]] == [KEY_A, KEY_B, KEY_C]
    print(
        f"13 [test 13]. REOPEN: after committing A (with A, B and C declared) and "
        f"evicting the cache, a reopen restores ALL THREE candidates "
        f"({h.identify_roads.call_count - calls_before} regenerates), re-selects "
        f"A's {len(restored['selected_feature_ids'])} features with nothing missing, and "
        f"the layers read serves the same three."
    )

    # --- 9 [test 9]. AN EMPTY COMMIT IS LEGAL AND RECORDS NO ROAD.
    empty = s.commit("roads", [], {}, base_revision=s.revision("roads"), inputs=ALL_INPUTS)
    entry = empty["steps"]["roads"]
    assert entry["status"] == design_document.STATUS_COMMITTED
    assert entry["features"]["features"] == []
    assert entry["inputs"] == ALL_INPUTS, "the tried access points are still the user's work"
    assert s.context().step_committed["roads"]["value"] == []
    assert step_orchestrator.committed_internal_value(s.context(), s.stored(), "roads") == []
    # And an empty commit with NO access point ever tried -- "no road" with
    # nothing placed -- is legal too: an empty list, not an absent key.
    s3 = Session()
    s3.upstream(water_zone_count=2)
    no_road = s3.commit("roads", [], {}, base_revision=0, inputs={"access_points": []})
    assert no_road["steps"]["roads"]["status"] == design_document.STATUS_COMMITTED
    assert no_road["steps"]["roads"]["inputs"] == {"access_points": []}
    try:
        s3.commit("roads", [], {}, base_revision=1, inputs=None)
    except step_orchestrator.CommitInputError:
        pass
    else:
        raise AssertionError("even an empty roads commit must carry its inputs key")
    print(
        f"9 [test 9]. EMPTY COMMIT: roads committed with zero features is status "
        f"'committed', carries every tried access point, and rehydrates to [] -- no "
        f"road. An empty commit on a never-generated step with access_points=[] is "
        f"legal; one with inputs absent is not."
    )

print("\nAll roads step checks passed (sections 1-9; sections 10-15 are test_roads_step_inputs.py).")
