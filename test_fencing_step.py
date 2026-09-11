"""
test_fencing_step.py

THE FENCING STEP -- the registry's SIXTH and final entry, the first whose
TAB IS A FENCE TYPE rather than a feature, the first whose candidate set
varies in size with the upstream commits, and the first to consume every
one of the five steps before it. Run as:

    python test_fencing_step.py

REAL COORDINATES, REAL PIPELINE CODE, THE SAME FIXTURE AS THE PRIOR
BRANCHES. The boundary is the actual drawn property from generate_full_
report.py -- 5614 N Montour Rd, Gibsonia, PA (~13.23 acres, UTM 17N) -- and
the DEM is test_water_step.py's / test_roads_step.py's / test_trees_step.py's
/ test_structures_step.py's bench-and-drainage fixture with its flanking
levees, unchanged, so the production zones, water zones, road network, tree
zones and structure sites committed here are the ones those branches
asserted over. session_manager.create_session(), the terrain warm-up, the
five upstream generates and commits, every rehydrator, fencing.identify_
fencing() and the payload assembly all RUN, for real. What is mocked is the
NETWORK and only the network -- plus the three self-computes the fencing
entry point can fall into (fetch_and_select_optimal_water_zone, identify_
road_corridor_candidates, identify_tree_zone_candidates, each replaced by a
COUNTER on the fencing module), because a call is what the committed edges
exist to prevent and a real call would go to the network.

Sections (the branch's numbered tests in brackets):
  1  [1]  REGISTRY -- the fencing entry is complete; validate_registry()
          passes with SIX entries -- the full set.
  2  [2]  GENERATE with all five upstream steps committed: three candidate
          types, each with its summed length and loop count.
  3  [3]  WATER COMMITTED EMPTY -> NO water fencing candidate,
          distinguishable from a zero-length one.
  4  [4]  TREES COMMITTED EMPTY -> no tree fencing candidate.
  5  [5]  BOUNDARY FENCING EXISTS with everything else empty.
  6  [6]  THREE STRUCTURE SITES all subtract from the perimeter margin --
          the margin against one site and against three.
  7  [7]  ROAD AND STREAM FENCING produce NO candidates but STILL REACH the
          result.
  8  [8]  COMMITTING A TYPE COMMITS EVERY LOOP of that type -- and a partial
          type is refused server-side.
  9  [9]  ZERO NETWORK CALLS during generate -- exact count.
 10  [10] EACH empty_commit SENTINEL with its control.
 11  [11] Regression is the other test files, run separately.
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

# --- the fixture: the parcel, the DEM, the mocked network, the Harness and
# the Session live in fencing_step_fixture.py so a file that needs them can import
# them without running this file's tests first.
from fencing_step_fixture import (  # noqa: E402,F401
    _boundary_point,
    ACCESS_A,
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
    CANOPY_ROWS,
    CANOPY_COLS,
    _build_dem,
    _build_canopy,
    HYDRIC_COMPONENTS,
    HYDRIC_GEOMETRIES,
    FIXTURE_ROADS,
    FIXTURE_KSAT,
    FIXTURE_STREAM,
    FIXTURE_WATER_FEATURES,
    FIXTURE_FARMLAND,
    _build_parcel_data,
    Harness,
    _fresh_caches,
    _fresh_store,
    _fresh_runner,
    Session,
    _utm,
    _lon_lat,
    _blocks,
    _rings,
    _BOUNDARY_CALL_PARAMETERS,
    _boundary_call,
)


# --- 1 [test 1]. THE REGISTRY ENTRY ----------------------------------

step_registry.validate_registry()
assert step_registry.registered_steps() == ("landform", "water", "roads", "trees", "structures", "fencing")
assert step_registry.registered_steps() == design_document.STEP_ORDER, "the full set, in STEP_ORDER"
FENCING = step_registry.get_step("fencing")

assert FENCING.generate == "fencing.identify_fencing"
assert FENCING.payload == "step_orchestrator.build_fencing_payload"
assert FENCING.proposal_collection == "fence_lines"
assert FENCING.produces == ("fence_lines",)
assert FENCING.upstream_steps() == ("landform", "water", "roads", "trees", "structures"), FENCING.upstream_steps()
assert FENCING.user_inputs == () and FENCING.accumulate is None and FENCING.post_commit == ()
assert FENCING.placement is None and FENCING.failure_layers == ()

_consumed = {c.name: c for c in FENCING.consumes}
assert set(_consumed) == {
    "boundary_coordinates", "dem", "boundary_polygon_utm", "water_features",
    "production_zone_polygons_utm", "selected_water_zone", "selected_road_corridor",
    "road_corridor_cell_footprint_polygon_utm", "tree_zone_patches", "structure_sites",
}, sorted(_consumed)
for _name in ("valleys", "hydric_floodplain_union", "floodplain_data_is_fallback", "anchor_lon_lat",
              "canopy_height", "production_areas", "exclusion_zones"):
    assert _name not in _consumed, f"{_name} only feeds a self-compute the committed edges close, or nothing"
assert all(c.why for c in FENCING.consumes)

# THE FIVE COMMITTED STEPS, SIX COMMITTED EDGES -- roads read twice.
assert _consumed["production_zone_polygons_utm"].from_step == "landform"
assert _consumed["production_zone_polygons_utm"].combine == "wire_translation.production_zone_polygons"
assert _consumed["production_zone_polygons_utm"].empty_commit is None
_water_edge = _consumed["selected_water_zone"]
assert _water_edge.from_step == "water" and _water_edge.combine == "wire_translation.water_zone_union"
assert _water_edge.empty_commit == "water_suitability.NO_WATER_ZONE"
_road_edge = _consumed["selected_road_corridor"]
assert _road_edge.from_step == "roads" and _road_edge.combine == "wire_translation.selected_road_network"
assert _road_edge.empty_commit == "road_corridors.NO_ROAD_CORRIDOR"
_footprint_edge = _consumed["road_corridor_cell_footprint_polygon_utm"]
assert _footprint_edge.from_step == "roads" and _footprint_edge.empty_commit is None
assert _footprint_edge.combine == "wire_translation.selected_road_network_footprint"
assert _footprint_edge.rehydrate == _road_edge.rehydrate == "wire_translation.rehydrate_road_networks"
_tree_edge = _consumed["tree_zone_patches"]
assert _tree_edge.from_step == "trees" and _tree_edge.empty_commit is None
_site_edge = _consumed["structure_sites"]
assert _site_edge.from_step == "structures" and _site_edge.rehydrate == "wire_translation.rehydrate_structure_sites"
assert _site_edge.combine == "wire_translation.structure_site_polygons"
assert _site_edge.forward_as == "structure_site_polygons_utm" and _site_edge.empty_commit is None
# THE CACHE CLOSURE for the one fetch left.
assert _consumed["water_features"].cache_path == "parcel_data.water_features"
assert _consumed["water_features"].combine == "hydrology_data.water_features_to_geojson"
assert _consumed["water_features"].forward_as == "water_features_geojson"

# THE COMMIT CONTRACT: a TYPE is the unit; lines; select-only; no crossings.
_contract = FENCING.commit_contract
assert _contract.layers == (wire_translation.LAYER_PERIMETER_FENCING,) == ("perimeter_fencing",)
assert wire_translation.LAYER_EXCLUSION_FENCING not in _contract.layers, "stream exclusion fencing is never committable"
assert _contract.geometry_types == ("LineString", "MultiLineString"), "the first contract over lines with no width"
assert _contract.min_features == 0 and _contract.max_features is None
assert _contract.feature_group == "fence_type"
assert _contract.group_check == "wire_translation.check_fence_type_complete"
assert _contract.rehydrate == "wire_translation.rehydrate_fence_lines"
assert _contract.internal_id_parameter is None and _contract.internal_id_parser is None
assert _contract.requires_provenance is True and _contract.max_user_added is None
assert _contract.crossings is step_registry.CROSSINGS_NOT_RECORDED
assert step_registry.records_crossings(_contract) is False

# EVERY TARGET RESOLVES, and every forward_as is a real parameter.
_signature = inspect.signature(FENCING.resolve_generate())
for _c in FENCING.consumes:
    if _c.forward_as:
        assert _c.forward_as in _signature.parameters, _c.forward_as
    for _target in (_c.rehydrate, _c.combine, _c.empty_commit):
        if _target:
            step_registry.resolve(_target)
for _target in (FENCING.payload, _contract.rehydrate, _contract.group_check):
    assert callable(step_registry.resolve(_target)), _target
# THE PARAMETERS THE WATER BRANCH SAID DID NOT EXIST, verified present.
for _parameter in ("dem", "boundary_polygon_utm", "production_areas", "valleys", "selected_road_corridor",
                   "selected_water_zone", "tree_zone_patches", "canopy_height", "production_zone_polygons_utm",
                   "road_corridor_cell_footprint_polygon_utm", "structure_site_polygons_utm", "water_features_geojson"):
    assert _parameter in _signature.parameters, _parameter

# CONSTANTS AGREE with the modules that own them.
assert fencing.CANDIDATE_FENCE_TYPES == ("boundary", "water_zone_exclusion", "tree_zone_exclusion")
assert set(wire_translation._FENCE_LINE_ID_SPELLINGS) == set(fencing.CANDIDATE_FENCE_TYPES)

# THE EDGE HELPERS see the sixth entry.
assert step_registry.dependents_of("structures") == ("fencing",)
assert step_registry.dependents_of("landform") == ("water", "roads", "trees", "structures", "fencing")
assert step_registry.transitive_dependents("fencing") == ()
for _step in ("landform", "water", "roads", "trees", "structures"):
    assert "fencing" in step_registry.transitive_dependents(_step), _step

# THE DECLARATIONS ARE VALIDATED: a malformed copy of the entry is refused.


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


_contract_with = lambda **kw: dataclasses.replace(FENCING, commit_contract=dataclasses.replace(_contract, **kw))
_rejects(_contract_with(feature_group=None), "a group_check with no feature_group")
_rejects(_contract_with(layers="perimeter_fencing"), "layers as a bare string")
_rejects(_contract_with(geometry_types=()), "no geometry type")
_rejects(dataclasses.replace(FENCING, consumes=FENCING.consumes + (
    step_registry.Consumed(name="dem2", source="cache", cache_path="dem", forward_as="dem"),
)), "two values forwarded into one parameter")
_rejects(dataclasses.replace(FENCING, consumes=(
    step_registry.Consumed(name="x", source="committed", from_step="fencing",
                           rehydrate="wire_translation.rehydrate_fence_lines"),
)), "a step consuming its own commit")
step_registry.validate_registry()

print(
    f"1 [test 1]. REGISTRY: validate_registry() passes with SIX entries {step_registry.registered_steps()} "
    f"== STEP_ORDER -- the full set. The fencing entry consumes {len(FENCING.consumes)} values -- 4 off the "
    f"cache (three shared, plus the water_features closure), 6 off commits from all five upstream steps "
    f"with the roads commit read TWICE (network + footprint), empty_commit None (landform, trees, "
    f"structures: [] is explicit; the road footprint: None is that parameter's own 'none'), NO_WATER_ZONE "
    f"(water) and NO_ROAD_CORRIDOR (roads) -- declares a contract on layer {_contract.layers[0]!r} "
    f"accepting LineString/MultiLineString, min 0, no count ceiling, feature_group {_contract.feature_group!r} "
    f"with group_check {_contract.group_check}, select-only, crossings CROSSINGS_NOT_RECORDED, no failure "
    f"layers; 5 malformations are each refused. dependents_of('structures') == "
    f"{step_registry.dependents_of('structures')}."
)


# --- 2 [test 2]. GENERATE with all five upstream steps committed --------

with Harness() as h:
    s = Session()
    s.upstream()
    for step in ("landform", "water", "roads", "trees", "structures"):
        assert s.stored()["steps"][step]["status"] == design_document.STATUS_COMMITTED
    COMMITTED_TREES = s.committed("trees")
    COMMITTED_SITES = s.committed("structures")
    assert COMMITTED_TREES and len(COMMITTED_SITES) == 1

    network_before = h.total_network_calls
    selfcomputes_before = h.fencing_selfcomputes()
    payload = s.fencing()
    FENCING_NETWORK_CALLS = h.total_network_calls - network_before
    SELFCOMPUTES = {k: v - selfcomputes_before[k] for k, v in h.fencing_selfcomputes().items()}

    assert sorted(payload) == ["candidate_fence_types", "fence_lines", "fence_types", "summary"], sorted(payload)
    assert "crossing_grounds" not in payload
    assert h.identify_fencing.call_count == 1
    validate_feature_collection(payload["fence_lines"])
    FENCE_LINES = payload["fence_lines"]["features"]
    assert FENCE_LINES, "the fixture must produce fence lines, or every assertion below is vacuous"

    # WHAT THE ENTRY POINT RECEIVED: every committed edge, by shape, none None.
    call = h.identify_fencing.call_args
    assert len(call.kwargs["production_zone_polygons_utm"]) == len(s.stored()["steps"]["landform"]["features"]["features"])
    assert call.kwargs["selected_water_zone"] is not water_suitability.NO_WATER_ZONE
    assert "render_fill_polygon_utm" in call.kwargs["selected_water_zone"]
    assert call.kwargs["selected_road_corridor"] is not road_corridors.NO_ROAD_CORRIDOR
    assert call.kwargs["road_corridor_cell_footprint_polygon_utm"] is not None
    assert call.kwargs["road_corridor_cell_footprint_polygon_utm"].equals(
        call.kwargs["selected_road_corridor"]["cell_footprint_polygon_utm"]
    ), "the two reads of one roads commit agree"
    assert [p["id"] for p in call.kwargs["tree_zone_patches"]] == [p["id"] for p in COMMITTED_TREES]
    assert len(call.kwargs["structure_site_polygons_utm"]) == 1
    assert call.kwargs["structure_site_polygons_utm"][0].equals(COMMITTED_SITES[0]["polygon_utm"])
    assert call.kwargs["water_features_geojson"]["features"][0]["id"] == "nhd-streams-fixture-run-1"
    assert call.kwargs["dem"] is not None and call.kwargs["boundary_polygon_utm"] is not None
    for absent in ("valleys", "hydric_floodplain_union", "floodplain_data_is_fallback", "anchor_lon_lat",
                   "canopy_height", "production_areas", "structure_site_feature"):
        assert absent not in call.kwargs, absent

    # THREE CANDIDATE TYPES, each with its summed length and loop count.
    assert payload["candidate_fence_types"] == ["boundary", "water_zone_exclusion", "tree_zone_exclusion"]
    BLOCKS = _blocks(payload)
    assert [b["fence_type"] for b in payload["fence_types"]] == list(fencing.CANDIDATE_FENCE_TYPES)
    for fence_type, block in BLOCKS.items():
        assert block["generated"] is True and block["candidate"] is True, (fence_type, block)
        assert block["loop_count"] >= 1 and block["feature_count"] >= 1
        assert isinstance(block["total_length_ft"], float) and block["total_length_ft"] > 0
        assert block["reason"] is None
        assert block["label"] == fencing.FENCE_TYPE_LABELS[fence_type]
        # THE SUM: the type's length is the sum over its features' lengths,
        # and each feature's length is its rings' UTM length in feet.
        assert abs(sum(row["length_ft"] for row in block["features"]) - block["total_length_ft"]) < 0.1 * len(block["features"]) + 1e-6
        assert sum(row["loop_count"] for row in block["features"]) == block["loop_count"]
        assert block["feature_ids"] == [row["feature_id"] for row in block["features"]]
    by_id = {f["id"]: f for f in FENCE_LINES}
    for block in payload["fence_types"]:
        for row in block["features"]:
            feature = by_id[row["feature_id"]]
            line_utm = _utm(feature["geometry"])
            measured_ft = sum(r.length for r in _rings(line_utm)) / 0.3048
            assert abs(measured_ft - row["length_ft"]) < 0.5, (row, measured_ft)
            assert feature["properties"]["length_ft"] == row["length_ft"]
            assert feature["properties"]["loop_count"] == row["loop_count"] == len(_rings(line_utm))
    assert BLOCKS["tree_zone_exclusion"]["feature_count"] == len(COMMITTED_TREES), "one tree fence per committed zone"
    assert BLOCKS["water_zone_exclusion"]["feature_count"] == 1, "ONE water fence, around the union of the committed zones"
    assert BLOCKS["boundary"]["feature_count"] == payload["summary"]["segment_count"]

    # THE PER-FEATURE BLOCK: every feature stamped with its place in its type.
    for f in FENCE_LINES:
        p = f["properties"]
        assert p["layer"] == "perimeter_fencing" and p["fence_type"] in fencing.CANDIDATE_FENCE_TYPES
        assert f["geometry"]["type"] in ("LineString", "MultiLineString")
        assert wire_translation.internal_fence_line_identity(f["id"])[0] == p["fence_type"]
        assert 1 <= p["fence_index"] <= p["fence_count"]
        assert p["fence_count"] == BLOCKS[p["fence_type"]]["feature_count"]
        for ring in _rings(_utm(f["geometry"])):
            assert ring.coords[0] == ring.coords[-1], "every fence is a closed loop"
    for fence_type, block in BLOCKS.items():
        indexes = sorted(by_id[i]["properties"]["fence_index"] for i in block["feature_ids"])
        assert indexes == list(range(1, block["feature_count"] + 1)), (fence_type, indexes)

    # THE STEP-LEVEL BLOCK.
    summary = payload["summary"]
    assert set(summary) == {"narrative_only", "segment_count", "developed_site_count", "buffers_ft"}, sorted(summary)
    assert summary["developed_site_count"] == 1
    assert summary["buffers_ft"]["water_zone_ft"] == round(fencing.WATER_ZONE_FENCE_BUFFER_METERS / 0.3048, 1)
    json.dumps(payload)  # serialisable throughout (coordinates are tuples off mapping(), lists after a round trip)
    assert json.loads(json.dumps(payload["fence_types"])) == payload["fence_types"]
    assert json.loads(json.dumps(summary)) == summary, "the digest is JSON-native"

    # THE DOCUMENT: generated, no features. THE READ VERB returns the same
    # payload; a regenerate is idempotent, INCLUDING THE IDS.
    entry = s.stored()["steps"]["fencing"]
    assert entry["status"] == design_document.STATUS_GENERATED and not entry.get("features")
    assert s.layers("fencing") == payload
    again = s.fencing()
    assert [f["id"] for f in again["fence_lines"]["features"]] == [f["id"] for f in FENCE_LINES]

    GENERATE_SESSION = s
    GENERATE_PAYLOAD = payload
    RESULT = s.result()

LENGTHS = {t: (b["total_length_ft"], b["loop_count"], b["feature_count"]) for t, b in BLOCKS.items()}
print(
    f"2 [test 2]. GENERATE: with landform ({len(s.stored()['steps']['landform']['features']['features'])} "
    f"zones), water (3 zones, as a union), roads (one network), trees ({len(COMMITTED_TREES)} zones) and "
    f"structures (1 site) committed, the fencing generate produced THREE candidate types "
    f"{payload['candidate_fence_types']} -- (summed length ft, loop count, feature count) per type: "
    f"{LENGTHS} -- over {len(FENCE_LINES)} fence line feature(s), with {FENCING_NETWORK_CALLS} network "
    f"calls and {SELFCOMPUTES} self-computes. The entry point received all six committed values (none "
    f"None, the two roads reads agreeing), the cached NHD rows as GeoJSON, and no undeclared edge. Each "
    f"type's total is the sum of its features' lengths, each feature's length its UTM rings in feet at one "
    f"decimal; every feature carries fence_index/fence_count/loop_count/length_ft."
)


# --- 3 [test 3]. WATER COMMITTED EMPTY -> NO water fencing candidate --------

with Harness() as h:
    s = Session()
    s.upstream(water_zone_count=0)
    assert s.stored()["steps"]["water"]["features"]["features"] == []
    before = h.fencing_selfcomputes()
    payload = s.fencing()
    call = h.identify_fencing.call_args
    assert call.kwargs["selected_water_zone"] is water_suitability.NO_WATER_ZONE, "received by identity"
    assert h.fencing_water_selfcompute.call_count - before["fetch_and_select_optimal_water_zone"] == 0

    WATER_EMPTY = _blocks(payload)
    assert payload["candidate_fence_types"] == ["boundary", "tree_zone_exclusion"], payload["candidate_fence_types"]
    water_block = WATER_EMPTY["water_zone_exclusion"]
    # THE TYPE IS PRESENT, AND EXPLICITLY NOT GENERATED -- not a zero-length
    # candidate, not a missing key.
    assert water_block["generated"] is False and water_block["candidate"] is False
    assert water_block["loop_count"] == 0 and water_block["feature_count"] == 0
    assert water_block["total_length_ft"] is None, "None, not 0.0: nothing was measured"
    assert water_block["feature_ids"] == [] and water_block["features"] == []
    assert "no zone" in water_block["reason"]
    assert not [f for f in payload["fence_lines"]["features"] if f["properties"]["fence_type"] == "water_zone_exclusion"]
    # DISTINGUISHABLE FROM A ZERO-LENGTH ONE: a generated-but-empty type
    # reads generated=True, total_length_ft 0.0 -- built from the module's
    # own digest over an empty ring list.
    zero_length = fencing._fence_type_block("water_zone_exclusion", generated=True, lines_utm=[], features=[], reason="ran, nothing")
    assert zero_length["generated"] is True and zero_length["candidate"] is False
    assert zero_length["total_length_ft"] == 0.0 and zero_length["loop_count"] == 0
    assert (water_block["generated"], water_block["total_length_ft"]) != (zero_length["generated"], zero_length["total_length_ft"])
    # The boundary fence unioned no water ground: the sentinel reached
    # find_boundary_fencing() as None for the water polygon.
    assert _boundary_call(h)["water_zone_polygon_utm"] is None
    assert WATER_EMPTY["boundary"]["candidate"] and WATER_EMPTY["tree_zone_exclusion"]["candidate"]
    WATER_EMPTY_SESSION = s

print(
    f"3 [test 3]. WATER COMMITTED EMPTY: the sentinel reaches identify_fencing() by identity, the water "
    f"self-compute ran 0 times, and the payload lists {payload['candidate_fence_types']} as candidates -- "
    f"TWO tabs. The water type is IN fence_types with generated=False, candidate=False, loop_count 0, "
    f"total_length_ft None, no feature ids and reason {water_block['reason']!r}: distinguishable from a "
    f"zero-length candidate (generated=True, total_length_ft 0.0) and from a missing key. No water fence "
    f"line is on the wire and the boundary fence unioned no water ground."
)


# --- 4 [test 4]. TREES COMMITTED EMPTY -> no tree fencing candidate ---------

with Harness() as h:
    s = Session()
    s.upstream(tree_count=0)
    assert s.stored()["steps"]["trees"]["features"]["features"] == []
    payload = s.fencing()
    call = h.identify_fencing.call_args
    assert call.kwargs["tree_zone_patches"] == [] and call.kwargs["tree_zone_patches"] is not None
    assert h.fencing_tree_selfcompute.call_count == 0, "an empty trees commit must not regenerate tree zones"
    assert h.fencing_road_selfcompute.call_count == 0
    TREES_EMPTY = _blocks(payload)
    assert payload["candidate_fence_types"] == ["boundary", "water_zone_exclusion"]
    tree_block = TREES_EMPTY["tree_zone_exclusion"]
    assert tree_block["generated"] is False and tree_block["candidate"] is False
    assert tree_block["loop_count"] == 0 and tree_block["total_length_ft"] is None and tree_block["feature_ids"] == []
    assert "no zone" in tree_block["reason"]
    assert _boundary_call(h)["tree_zone_polygons_utm"] == []
    TREES_EMPTY_SESSION = s

print(
    f"4 [test 4]. TREES COMMITTED EMPTY: [] reaches identify_fencing() (never None), the tree and road "
    f"self-computes ran 0 times, candidates are {payload['candidate_fence_types']} -- TWO tabs -- and the "
    f"tree type is listed generated=False with reason {tree_block['reason']!r}. '[] means checked, nothing "
    f"to stay clear of' holds for fencing: nothing to fence, nothing in the boundary union."
)


# --- 5 [test 5]. BOUNDARY FENCING EXISTS with everything else empty --------

with Harness() as h:
    s = Session()
    s.upstream(landform=False, water_zone_count=0, access_point=None, tree_count=0, structure_count=0)
    for step in ("landform", "water", "roads", "trees", "structures"):
        assert s.stored()["steps"][step]["features"]["features"] == []
    payload = s.fencing()
    call = h.identify_fencing.call_args
    assert call.kwargs["production_zone_polygons_utm"] == []
    assert call.kwargs["selected_water_zone"] is water_suitability.NO_WATER_ZONE
    assert call.kwargs["selected_road_corridor"] is road_corridors.NO_ROAD_CORRIDOR
    assert call.kwargs["road_corridor_cell_footprint_polygon_utm"] is None
    assert call.kwargs["tree_zone_patches"] == [] and call.kwargs["structure_site_polygons_utm"] == []
    assert h.fencing_selfcomputes() == {
        "fetch_and_select_optimal_water_zone": 0,
        "identify_road_corridor_candidates": 0,
        "identify_tree_zone_candidates": 0,
    }, h.fencing_selfcomputes()

    ALL_EMPTY = _blocks(payload)
    assert payload["candidate_fence_types"] == ["boundary"], payload["candidate_fence_types"]
    boundary_block = ALL_EMPTY["boundary"]
    assert boundary_block["generated"] and boundary_block["candidate"]
    assert boundary_block["loop_count"] == 1 and boundary_block["feature_count"] == 1
    # THE DEGENERATE CASE IS THE PARCEL'S OWN RING: with nothing developed
    # the boundary fence collapses to the drawn boundary, unmodified.
    bare = [f for f in payload["fence_lines"]["features"] if f["properties"]["fence_type"] == "boundary"]
    assert len(bare) == 1
    bare_ring = _utm(bare[0]["geometry"])
    # Through one WGS84 round trip, so within a centimetre rather than equal.
    assert bare_ring.hausdorff_distance(LineString(BOUNDARY_POLYGON_UTM.exterior.coords)) < 0.05
    assert abs(boundary_block["total_length_ft"] - BOUNDARY_POLYGON_UTM.exterior.length / 0.3048) < 0.5
    assert ALL_EMPTY["water_zone_exclusion"]["generated"] is False
    assert ALL_EMPTY["tree_zone_exclusion"]["generated"] is False
    assert payload["summary"]["developed_site_count"] == 0
    # Committing the one type commits; committing nothing commits.
    document = s.commit("fencing", bare, {bare[0]["id"]: "generated"})
    assert document["steps"]["fencing"]["status"] == design_document.STATUS_COMMITTED
    s.reopen("fencing")
    document = s.commit("fencing", [], {})
    assert document["steps"]["fencing"]["status"] == design_document.STATUS_COMMITTED
    assert s.committed("fencing") == []

print(
    f"5 [test 5]. BOUNDARY FENCING EXISTS with landform, water, roads, trees and structures ALL committed "
    f"empty: ONE tab {payload['candidate_fence_types']}, the parcel's own ring ({boundary_block['total_length_ft']} ft, "
    f"parcel perimeter {BOUNDARY_POLYGON_UTM.exterior.length / 0.3048:.1f} ft), zero self-computes; water and "
    f"tree types listed generated=False. It commits, and committing nothing commits."
)


# --- 6 [test 6]. THREE STRUCTURE SITES all subtract from the perimeter margin
#
# THE MARGIN: the unfenced ground between the boundary fence and the parcel
# edge. A site pushes the fence outward, so each committed site SUBTRACTS
# from it. Measured on a parcel where the structure sites are the ONLY
# developed footprint (landform, water, roads and trees committed empty),
# because on the full fixture the five production zones, the water union,
# the road and the two tree zones already push the one-site fence most of
# the way to the parcel edge, leaving little margin for a site to move --
# section (c) below measures that case too, on the smaller margin it has.


def _fenced_polygon(payload):
    """The boundary fence's enclosed ground, every ring unioned."""
    return unary_union([
        Polygon(r) for f in payload["fence_lines"]["features"] if f["properties"]["fence_type"] == "boundary"
        for r in _rings(_utm(f["geometry"]))
    ])


def _margin_acres(payload) -> float:
    return BOUNDARY_POLYGON_UTM.difference(_fenced_polygon(payload)).area / SQUARE_METERS_PER_ACRE


with Harness() as h:
    margin = fencing.BOUNDARY_FENCE_MARGIN_METERS
    pad_half_side = (solar_suitability.MAX_STRUCTURE_FOOTPRINT_ACRES * SQUARE_METERS_PER_ACRE) ** 0.5 / 2

    # (a) ONE SITE, nothing else developed: the fence is that site's
    # margined pad, hulled and clipped, and the margin is nearly the parcel.
    one = Session()
    one.upstream(landform=False, water_zone_count=0, access_point=None, tree_count=0, structure_count=1)
    ONE_SITE = one.committed("structures")
    assert len(ONE_SITE) == 1
    one_payload = one.fencing()
    one_sites = _boundary_call(h)["structure_site_polygon_utm"]
    assert isinstance(one_sites, list) and len(one_sites) == 1, "the site list, not a pre-unioned polygon"
    one_fenced = _fenced_polygon(one_payload)
    one_margin_acres = _margin_acres(one_payload)
    assert one_fenced.buffer(0.05).contains(ONE_SITE[0]["polygon_utm"].buffer(margin).intersection(BOUNDARY_POLYGON_UTM))
    assert one_fenced.area < 0.25 * BOUNDARY_POLYGON_UTM.area, "one site alone fences a small fraction of the parcel"

    # (b) THREE SITES: the same generated site plus TWO PLACED sites, each
    # at a different parcel corner, each with its pad inside the parcel
    # and clear of the one-site fence, so each one has to move it.
    corners = []
    for vertex in BOUNDARY_POLYGON_UTM.exterior.coords[:-1]:
        toward_centre = LineString([Point(vertex), BOUNDARY_POLYGON_UTM.centroid])
        for distance in np.arange(20.0, toward_centre.length, 5.0):
            inward = toward_centre.interpolate(float(distance))
            pad = inward.buffer(pad_half_side * 1.5)
            if BOUNDARY_POLYGON_UTM.contains(pad) and not one_fenced.buffer(margin).intersects(pad):
                corners.append(inward)
                break
    assert len(corners) >= 2, "two parcel corners clear of the one-site fence are needed"
    PLACED = [_lon_lat(corners[0]), _lon_lat(corners[len(corners) // 2])]
    three = Session()
    three.upstream(landform=False, water_zone_count=0, access_point=None, tree_count=0,
                   structure_count=1, placed=PLACED)
    THREE_SITES = three.committed("structures")
    assert len(THREE_SITES) == 3
    assert [s_["site_origin"] for s_ in THREE_SITES] == ["generated", "user_placed", "user_placed"]
    assert THREE_SITES[0]["polygon_utm"].equals(ONE_SITE[0]["polygon_utm"]), "the same generated site in both"

    three_payload = three.fencing()
    three_sites = _boundary_call(h)["structure_site_polygon_utm"]
    assert isinstance(three_sites, list) and len(three_sites) == 3, "three committed sites are THREE developed parts"
    for site, part in zip(THREE_SITES, three_sites):
        assert part.equals(site["polygon_utm"])
    assert three_payload["summary"]["developed_site_count"] == 3
    three_fenced = _fenced_polygon(three_payload)
    three_margin_acres = _margin_acres(three_payload)

    # EVERY SITE, MARGINED, IS INSIDE THE THREE-SITE FENCE; each placed pad
    # lay OUTSIDE the one-site fence; the margin shrank by every one.
    SITE_SUBTRACTIONS = []
    for site in THREE_SITES:
        pad = site["polygon_utm"].buffer(margin).intersection(BOUNDARY_POLYGON_UTM)
        assert three_fenced.buffer(0.05).contains(pad), f"site {site['id']} is not enclosed with its margin"
        SITE_SUBTRACTIONS.append(round(pad.difference(one_fenced).area / SQUARE_METERS_PER_ACRE, 3))
    assert SITE_SUBTRACTIONS[0] == 0.0 and all(x > 0 for x in SITE_SUBTRACTIONS[1:]), SITE_SUBTRACTIONS
    assert three_margin_acres < one_margin_acres, (one_margin_acres, three_margin_acres)
    assert (one_margin_acres - three_margin_acres) >= sum(SITE_SUBTRACTIONS), "the hull takes at least every pad"
    assert three_fenced.area > one_fenced.area
    # And each site alone would be a smaller fence than the three: the
    # hull with two sites removed drops every pad but the remaining one.
    for keep in range(3):
        rings = fencing.find_boundary_fencing(BOUNDARY_POLYGON_UTM, [], [THREE_SITES[keep]["polygon_utm"]], None, None, [])
        alone = unary_union([Polygon(r) for r in rings])
        assert alone.area < three_fenced.area
        others = [s_ for i, s_ in enumerate(THREE_SITES) if i != keep]
        assert not all(alone.buffer(0.05).contains(o["polygon_utm"]) for o in others)

    # (c) THE FULL FIXTURE, three sites: the parts still arrive as three,
    # every margined pad is inside the fence, and the (already small)
    # margin shrinks.
    full = Session()
    full.commit_landform()
    full.commit_water(3)
    full.commit_roads(ACCESS_A)
    full.commit_trees()
    full.commit_structures(count=1, placed=PLACED)
    FULL_SITES = full.committed("structures")
    assert len(FULL_SITES) == 3
    full_payload = full.fencing()
    assert len(_boundary_call(h)["structure_site_polygon_utm"]) == 3
    full_fenced = _fenced_polygon(full_payload)
    for site in FULL_SITES:
        pad = site["polygon_utm"].buffer(margin).intersection(BOUNDARY_POLYGON_UTM)
        assert full_fenced.buffer(0.05).contains(pad), f"site {site['id']} is not enclosed with its margin"
    FULL_MARGIN = (_margin_acres(GENERATE_PAYLOAD), _margin_acres(full_payload))
    assert FULL_MARGIN[1] < FULL_MARGIN[0], FULL_MARGIN

    # (d) THE PURE CORE, DIRECTLY: three disjoint boxes as the site list; the
    # hull encloses all three where one box alone encloses one. A single
    # polygon (the batch caller's shape) still works.
    unit_boundary = box(0, 0, 100, 100)
    boxes = [box(10, 10, 20, 20), box(80, 10, 90, 20), box(45, 80, 55, 90)]
    ring_one = Polygon(fencing.find_boundary_fencing(unit_boundary, [], boxes[0], None, None, [])[0])
    ring_three = Polygon(fencing.find_boundary_fencing(unit_boundary, [], boxes, None, None, [])[0])
    assert all(ring_three.contains(b) for b in boxes) and not all(ring_one.contains(b) for b in boxes)
    assert ring_three.area > ring_one.area
    assert Polygon(fencing.find_boundary_fencing(unit_boundary, [], [boxes[0]], None, None, [])[0]).equals(ring_one)

print(
    f"6 [test 6]. THREE STRUCTURE SITES (sites the only developed footprint): ONE committed site leaves "
    f"{one_margin_acres:.3f} acres of unfenced margin between the boundary fence and the parcel edge "
    f"(the fence encloses {one_fenced.area / SQUARE_METERS_PER_ACRE:.3f} acres); the same site plus two "
    f"PLACED sites at parcel corners {PLACED} leave {three_margin_acres:.3f} acres (fence "
    f"{three_fenced.area / SQUARE_METERS_PER_ACRE:.3f} acres). Every margined pad is inside the three-site "
    f"fence; the pads lay {SITE_SUBTRACTIONS} acres outside the one-site fence (the shared site 0.0, the two "
    f"placed ones positive), and the margin shrank by {one_margin_acres - three_margin_acres:.3f} acres -- at "
    f"least every pad. find_boundary_fencing() received the sites as a LIST of 3 polygons (1 for the one-site "
    f"session); any single site alone fences less and leaves the other two outside. On the FULL fixture with "
    f"3 sites the three parts arrive and every pad is enclosed, and the margin there shrinks from "
    f"{FULL_MARGIN[0]:.3f} acres with one site to {FULL_MARGIN[1]:.3f} with three -- smaller to begin with, "
    f"because the production, water, road and tree ground already carry the fence most of the way to the "
    f"parcel edge. On three disjoint unit boxes the hull encloses all three "
    f"({ring_three.area:.0f} m^2 vs {ring_one.area:.0f} m^2 for one)."
)


# --- 7 [test 7]. ROAD AND STREAM FENCING: no candidate, still in the result ---

with Harness() as h:
    s = GENERATE_SESSION
    payload = s.fencing()
    result = s.result()
    assert h.find_stream_exclusion_fencing.call_count == 1, "find_stream_exclusion_fencing() still runs"
    stream_call = h.find_stream_exclusion_fencing.call_args
    assert [f["id"] for f in stream_call.args[0]] == ["nhd-streams-fixture-run-1"], "fed the cached NHD rows"
    # IN THE RESULT: the stream exclusion feature is on fencing_geojson.
    result_layers = sorted({f["properties"]["layer"] for f in result["fencing_geojson"]["features"]})
    assert result_layers == ["exclusion_fencing", "perimeter_fencing"], result_layers
    STREAM_FEATURES = [f for f in result["fencing_geojson"]["features"] if f["properties"]["layer"] == "exclusion_fencing"]
    assert len(STREAM_FEATURES) == 1 and STREAM_FEATURES[0]["id"] == "exclusion-fencing-stream-nhd-streams-fixture-run-1"
    validate_feature_collection(result["fencing_geojson"])
    # NOT A CANDIDATE: absent from fence_lines and from fence_types.
    assert not [f for f in payload["fence_lines"]["features"] if f["properties"]["layer"] == "exclusion_fencing"]
    assert "stream" not in " ".join(b["fence_type"] for b in payload["fence_types"])
    assert set(f["properties"]["fence_type"] for f in result["fencing_geojson"]["features"] if "fence_type" in f["properties"]) == set(fencing.CANDIDATE_FENCE_TYPES), "no road fence_type exists in the result at all"
    # IN THE SUMMARY: counted, with its own length, marked not a candidate.
    stream = payload["summary"]["narrative_only"]["stream_exclusion"]
    assert stream["generated"] is True and stream["candidate"] is False
    assert stream["feature_count"] == 1 and stream["loop_count"] >= 1 and stream["total_length_ft"] > 0
    road = payload["summary"]["narrative_only"]["road"]
    assert road["generated"] is False and road["candidate"] is False and "narrative" in road["reason"]
    # AND REFUSED BY NAME if a client sends it.
    try:
        s.commit("fencing", [STREAM_FEATURES[0]], {STREAM_FEATURES[0]["id"]: "generated"})
    except commit_validation.CommitRejectedError as exc:
        codes = {r.code for r in exc.rejections}
        assert commit_validation.REJECT_WRONG_LAYER in codes, codes
    else:
        raise AssertionError("a stream exclusion feature must be refused by layer")
    assert s.stored()["steps"]["fencing"]["status"] == design_document.STATUS_GENERATED

print(
    f"7 [test 7]. ROAD AND STREAM FENCING: find_stream_exclusion_fencing() ran once over the cached NHD rows "
    f"and its {len(STREAM_FEATURES)} feature is in the result's fencing_geojson (layers {result_layers}) and in "
    f"summary.narrative_only.stream_exclusion ({stream['total_length_ft']} ft, {stream['loop_count']} loop(s), "
    f"candidate=False) -- and NOT in fence_lines, NOT in fence_types, and refused by layer if committed. "
    f"Road fencing: no fence_type 'road' exists anywhere in the result; summary.narrative_only.road says "
    f"generated=False, {road['reason']!r}."
)


# --- 8 [test 8]. COMMITTING A TYPE COMMITS EVERY LOOP OF THAT TYPE ---------

with Harness() as h:
    s = GENERATE_SESSION
    payload = s.fencing()
    lines = payload["fence_lines"]["features"]
    by_type = {}
    for f in lines:
        by_type.setdefault(f["properties"]["fence_type"], []).append(f)
    multi = [t for t, fs in by_type.items() if len(fs) > 1]
    assert multi, "a type with more than one loop is needed to test partial commits; the fixture's tree zones give one"
    TYPE = "tree_zone_exclusion" if "tree_zone_exclusion" in multi else multi[0]
    whole = by_type[TYPE]

    # A PARTIAL TYPE IS REFUSED, naming every feature of the group.
    partial = whole[:-1]
    try:
        s.commit("fencing", partial, {f["id"]: "generated" for f in partial})
    except commit_validation.CommitRejectedError as exc:
        codes = [r.code for r in exc.rejections]
        assert codes == [commit_validation.REJECT_INCOHERENT_GROUP] * len(partial), codes
        assert all("committed whole" in r.reason for r in exc.rejections)
        PARTIAL_REJECTION = exc.as_payload()
    else:
        raise AssertionError("a fence type committed without all its loops must be refused")
    assert s.stored()["steps"]["fencing"]["status"] == design_document.STATUS_GENERATED

    # THE WHOLE TYPE COMMITS -- every loop; and two whole types commit.
    document = s.commit("fencing", whole, {f["id"]: "generated" for f in whole})
    assert document["steps"]["fencing"]["status"] == design_document.STATUS_COMMITTED
    stored = document["steps"]["fencing"]["features"]["features"]
    assert [f["id"] for f in stored] == [f["id"] for f in whole]
    for f in stored:
        assert "exclusion_crossings" not in f["properties"], "no crossings recorded for a line"
    committed = s.committed("fencing")
    assert [c["id"] for c in committed] == [f["id"] for f in whole]
    assert all(c["fence_type"] == TYPE for c in committed)
    assert sum(c["loop_count"] for c in committed) == _blocks(payload)[TYPE]["loop_count"]
    assert abs(sum(c["length_meters"] for c in committed) / 0.3048 - _blocks(payload)[TYPE]["total_length_ft"]) < 0.5
    for c in committed:
        assert c["polygon_utm"] is c["line_utm"], "the gate's polygon_utm is the line itself"
    # WARM AND COLD AGREE.
    s.cache.discard(s.id)
    cold = s.committed("fencing")
    assert [c["id"] for c in cold] == [c["id"] for c in committed]
    assert all(abs(a["length_meters"] - b["length_meters"]) < 1e-6 for a, b in zip(committed, cold))
    assert h.rehydrate_fences.call_count >= 1
    # REOPEN restores the selection by id.
    s.reopen("fencing")
    restored = s.context().step_restored["fencing"]
    assert restored["selected_feature_ids"] == [f["id"] for f in whole]
    assert restored["missing_feature_ids"] == [] and restored["user_added"]["features"] == []

    two_types = by_type["boundary"] + by_type["water_zone_exclusion"]
    document = s.commit("fencing", two_types, {f["id"]: "generated" for f in two_types})
    assert len(document["steps"]["fencing"]["features"]["features"]) == len(two_types)
    s.reopen("fencing")
    # ALL THREE TYPES commit, every loop of each.
    document = s.commit("fencing", lines, {f["id"]: "generated" for f in lines})
    assert len(document["steps"]["fencing"]["features"]["features"]) == len(lines)
    s.reopen("fencing")

    # A feature with an INVENTED id is refused (select-only); a feature with a
    # mismatched fence_index is refused; an open segment is refused.
    forged = copy.deepcopy(whole[0])
    forged["id"] = "perimeter-fencing-tree-zone-x"
    mismatched = copy.deepcopy(whole[0])
    mismatched["properties"]["fence_index"] = whole[0]["properties"]["fence_count"] + 5
    opened = copy.deepcopy(by_type["boundary"][0])
    opened["geometry"] = {"type": "LineString", "coordinates": opened["geometry"]["coordinates"][:-1]} if opened["geometry"]["type"] == "LineString" else opened["geometry"]
    for bad, why in ((forged, "an id this pipeline did not mint"), (mismatched, "a fence_index off its id"), (opened, "an open segment")):
        try:
            s.commit("fencing", [bad], {bad["id"]: "generated"})
        except commit_validation.CommitRejectedError as exc:
            assert {r.code for r in exc.rejections} & {
                commit_validation.REJECT_INVALID_GEOMETRY, commit_validation.REJECT_INCOHERENT_GROUP
            }, (why, [r.code for r in exc.rejections])
        else:
            raise AssertionError(f"{why} must be refused")

    # THE HTTP ROUTE: generate -> commit the whole type, through the app.
    deps = session_api.Dependencies(store=s.store, fetch_cache=s.fetch_cache, cache=s.cache, runner=s.runner)
    http = session_api.create_app(deps).test_client()
    layers = http.get(f"/api/sessions/{s.id}/steps/fencing/layers")
    assert layers.status_code == 200, layers.get_json()
    assert layers.get_json()["candidate_fence_types"] == payload["candidate_fence_types"]
    body = {
        "features": {"type": "FeatureCollection", "features": whole},
        "provenance": {f["id"]: "generated" for f in whole},
        "base_revision": s.revision("fencing"),
    }
    committed_http = http.post(f"/api/sessions/{s.id}/steps/fencing/commit", json=body)
    assert committed_http.status_code == 200, committed_http.get_json()
    assert committed_http.get_json()["steps"]["fencing"]["status"] == design_document.STATUS_COMMITTED
    s.reopen("fencing")

print(
    f"8 [test 8]. COMMITTING A TYPE: {TYPE!r} has {len(whole)} loops on this fixture; committing "
    f"{len(partial)} of them is rejected server-side with {len(PARTIAL_REJECTION['rejections'])} "
    f"{PARTIAL_REJECTION['rejections'][0]['code']!r} rejection(s) ({PARTIAL_REJECTION['rejections'][0]['reason']!r}) "
    f"and nothing written; committing all {len(whole)} commits, with no exclusion_crossings key on any "
    f"feature, the committed value carrying every loop at the type's summed length, warm and cold reads "
    f"agreeing, and a reopen restoring the selection. Two whole types commit; all three commit. An invented "
    f"id, a mismatched fence_index and an open segment are each refused. HTTP GET .../layers and POST "
    f".../commit route the same answers."
)


# --- 9 [test 9]. ZERO NETWORK CALLS during generate -- exact count ---------

import socket

_connection_attempts = []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _forbidden(address, *args, **kwargs):
    _connection_attempts.append(address)
    raise AssertionError(f"the fencing generate opened a network connection to {address!r}")


socket.socket.connect = lambda self, address: _forbidden(address)
socket.socket.connect_ex = lambda self, address: _forbidden(address)
socket.create_connection = _forbidden
try:
    with Harness() as h:
        s = GENERATE_SESSION
        before = h.total_network_calls
        selfcomputes_before = h.fencing_selfcomputes()
        _guarded = s.fencing()
        # A cold cache regenerates the run once, still off the network.
        s.cache.discard(s.id)
        _guarded_cold = s.layers("fencing")
        NETWORK_CALLS_GENERATE = h.total_network_calls - before
        SELFCOMPUTES_GENERATE = {k: v - selfcomputes_before[k] for k, v in h.fencing_selfcomputes().items()}
        assert NETWORK_CALLS_GENERATE == 0, NETWORK_CALLS_GENERATE
        assert h.fencing_nhd_fetch.call_count == 0 and h.fencing_water_selfcompute.call_count == 0
        assert SELFCOMPUTES_GENERATE == {
            "fetch_and_select_optimal_water_zone": 0,
            "identify_road_corridor_candidates": 0,
            "identify_tree_zone_candidates": 0,
        }, SELFCOMPUTES_GENERATE
        assert h.identify_fencing.call_count == 2
        assert [f["id"] for f in _guarded_cold["fence_lines"]["features"]] == [f["id"] for f in _guarded["fence_lines"]["features"]]
finally:
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    socket.create_connection = _real_create_connection

assert _connection_attempts == [], _connection_attempts

print(
    f"9 [test 9]. ZERO NETWORK: two fencing generates (warm, and a cold-cache regenerate through the read "
    f"verb) under a socket counter that raises -- {len(_connection_attempts)} connection attempts, "
    f"{NETWORK_CALLS_GENERATE} mocked fetch calls across all thirteen counted boundaries (the fencing NHD "
    f"fetch at 0), and {SELFCOMPUTES_GENERATE} self-computes. The ten forwarded edges are the whole of it."
)


# --- 10 [test 10]. EACH empty_commit SENTINEL WITH ITS CONTROL --------------
#
# The zero above is a measurement only if a nonzero count is reachable. For
# each consumed edge: the entry point called with the registry's own
# forwarded arguments, then again with None in the edge's place.

with Harness() as h:
    # WATER: NO_WATER_ZONE -> 0 water self-computes; None -> 1.
    s = WATER_EMPTY_SESSION
    control = s.forwarded()
    assert control["selected_water_zone"] is water_suitability.NO_WATER_ZONE
    fencing.identify_fencing(**control)
    assert h.fencing_water_selfcompute.call_count == 0
    control["selected_water_zone"] = None
    fencing.identify_fencing(**control)
    assert h.fencing_water_selfcompute.call_count == 1, "None must reach the water self-compute"
    WATER_CONTROL = (0, 1)

    # ROADS: NO_ROAD_CORRIDOR -> 0 road self-computes, even with trees
    # UNSUPPLIED (the path the sentinel guards); None with trees unsupplied
    # -> 1. With trees committed neither runs, which is the trees edge's
    # doing, not the sentinel's -- so the control removes trees too.
    s = Session()
    s.upstream(access_point=None)
    control = s.forwarded()
    assert control["selected_road_corridor"] is road_corridors.NO_ROAD_CORRIDOR
    assert control["road_corridor_cell_footprint_polygon_utm"] is None
    fencing.identify_fencing(**control)
    assert h.fencing_road_selfcompute.call_count == 0 and h.fencing_tree_selfcompute.call_count == 0
    control_no_trees = dict(control)
    control_no_trees["tree_zone_patches"] = None
    fencing.identify_fencing(**control_no_trees)
    assert h.fencing_road_selfcompute.call_count == 0, (
        "the sentinel must skip the nested road self-compute even when trees is unsupplied"
    )
    assert h.fencing_tree_selfcompute.call_count == 1
    assert h.fencing_tree_selfcompute.call_args.kwargs["selected_road_corridor"] is road_corridors.NO_ROAD_CORRIDOR, (
        "the nested tree call receives the sentinel, never a bare None"
    )
    control_no_trees["selected_road_corridor"] = None
    fencing.identify_fencing(**control_no_trees)
    assert h.fencing_road_selfcompute.call_count == 1, "None must reach the road self-compute"
    ROAD_CONTROL = (0, 1)
    # The footprint edge: None for an empty commit, off both reads.
    assert s.assembled()["road_corridor_cell_footprint_polygon_utm"] is None
    s.cache.discard(s.id)
    assert s.assembled()["road_corridor_cell_footprint_polygon_utm"] is None
    assert s.assembled()["selected_road_corridor"] is road_corridors.NO_ROAD_CORRIDOR

    # TREES: [] -> 0 tree self-computes; None -> 1.
    s = TREES_EMPTY_SESSION
    control = s.forwarded()
    assert control["tree_zone_patches"] == []
    before = h.fencing_tree_selfcompute.call_count
    fencing.identify_fencing(**control)
    assert h.fencing_tree_selfcompute.call_count == before
    control["tree_zone_patches"] = None
    fencing.identify_fencing(**control)
    assert h.fencing_tree_selfcompute.call_count == before + 1, "None must reach the tree self-compute"
    TREES_CONTROL = (0, 1)

    # STRUCTURES: [] and None are the SAME answer to this consumer, and
    # there is no self-compute to count -- fencing never imports solar.
    # The control is therefore geometric: the boundary fence with [] equals
    # the boundary fence with None, and both differ from one site.
    s = Session()
    s.upstream(structure_count=0)
    control = s.forwarded()
    assert control["structure_site_polygons_utm"] == []
    assert not hasattr(fencing, "identify_solar_candidate_zones")
    empty_result = fencing.identify_fencing(**control)
    control["structure_site_polygons_utm"] = None
    none_result = fencing.identify_fencing(**control)
    assert empty_result["fencing_geojson"] == none_result["fencing_geojson"]
    assert empty_result["narrative_data"]["developed_site_count"] == none_result["narrative_data"]["developed_site_count"] == 0
    with_site = GENERATE_SESSION.result()
    assert with_site["narrative_data"]["developed_site_count"] == 1
    empty_boundary = _blocks({"fence_types": empty_result["narrative_data"]["fence_types"]})["boundary"]
    site_boundary = _blocks({"fence_types": with_site["narrative_data"]["fence_types"]})["boundary"]
    STRUCTURES_EMPTY_VS_SITE = (empty_boundary["total_length_ft"], site_boundary["total_length_ft"])
    # LANDFORM: [] -> no production part; the fence is still built.
    s = Session()
    s.upstream(landform=False)
    control = s.forwarded()
    assert control["production_zone_polygons_utm"] == []
    fencing.identify_fencing(**control)
    assert _boundary_call(h)["production_zone_polygons_utm"] == []
    # THE NHD CLOSURE: the cached rows -> 0 fetches; None -> 1.
    before = h.fencing_nhd_fetch.call_count
    fencing.identify_fencing(**control)
    assert h.fencing_nhd_fetch.call_count == before
    control["water_features_geojson"] = None
    fencing.identify_fencing(**control)
    assert h.fencing_nhd_fetch.call_count == before + 1, "None must reach the NHD fetch"
    NHD_CONTROL = (0, 1)

print(
    f"10 [test 10]. SENTINELS WITH CONTROLS -- (with the edge's value, with None): water "
    f"NO_WATER_ZONE -> fetch_and_select_optimal_water_zone {WATER_CONTROL}; roads NO_ROAD_CORRIDOR -> "
    f"identify_road_corridor_candidates {ROAD_CONTROL} with trees unsupplied in both (with trees committed "
    f"the count is 0 either way, which is the trees edge's doing -- the sentinel's own guard is the one "
    f"measured here), the nested tree call receiving the sentinel; trees [] -> identify_tree_zone_candidates "
    f"{TREES_CONTROL}; the cached NHD rows -> get_water_features_geojson {NHD_CONTROL}. STRUCTURES committed "
    f"empty: [] and None are one answer to fencing ('no building to enclose') and there is no self-compute "
    f"to count -- the result with [] equals the result with None byte for byte, developed_site_count 0, "
    f"boundary fence {STRUCTURES_EMPTY_VS_SITE[0]} ft against {STRUCTURES_EMPTY_VS_SITE[1]} ft with one site."
)

print(
    "\n11 [test 11]. REGRESSION: run the other test files separately -- test_step_registry.py, "
    "test_fencing.py, test_wire_translation.py, test_wire_translation_inbound.py, test_step_orchestrator.py, "
    "test_step_commit.py, test_water_step.py, test_roads_step.py, test_trees_step.py, "
    "test_structures_step.py, test_session_api.py, test_render_layout_map.py, test_pipeline_context.py."
)
print("\nAll fencing step checks passed.")
