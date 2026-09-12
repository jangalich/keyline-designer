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
 12        THE DATA-PANEL BRANCH, on the reference parcel (the pure-core
          halves of these are in test_solar_suitability.py):
     12a [1] FT TO ROAD, the BEFORE and the AFTER: every shipped
             candidate's PAD intersects the committed corridor, so the
             pad-based figure was 0.0 on all of them; the point-based
             figure is real.
     12b [3] THE THREE-ROW ROAD TABLE -- BEFORE (pad, 15 m) / PART 1 ONLY
             (point, 15 m) / BOTH (point, 30 m): candidate count, scores
             and ft-to-road for each.
     12c [5] A PLACED SITE NAMES ITS OWN GATE: hydric alone, floodplain
             alone, both, and clear of both -- every one SCORED, never
             refused.
     12d [4] NO GENERATED CANDIDATE lands on either ground, asserted
             against the gate geometry itself, with the two gates shown
             INDEPENDENT.
     12e [6] A FULLY GATED PARCEL reports that state -- zero candidates
             with the blocking gates named, on the result AND the payload.
     12f [7,8,9] The panel's rows and notices: the solar value
             DISTRIBUTION over the whole parcel and the words it produces;
             elevation_position against production's IMPORTED bands;
             road_proximity_source as a step-level value.
 13  [12] Regression is the other test files, run separately.
"""

import copy
import socket
import tempfile
from unittest.mock import MagicMock
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
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
from document_store import JSONFileStore
from feature_schema import validate_feature_collection
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- the real property, verbatim from B2, B4, B5a, B5b, water, roads, trees

# The parcel, its UTM CRS, the projected polygon and its acreage -- one
# definition shared by every step test (see reference_fixture.py).
from reference_fixture import BOUNDARY_POLYGON_UTM, CRS, PARCEL_ACRES, REAL_BOUNDARY  # noqa: E402

# --- the fixture: the parcel, the DEM, the mocked network, the Harness and
# the Session live in structures_step_fixture.py so a file that needs them
# can import them without running this file's tests first (the same
# extraction trees_step_fixture.py and fencing_step_fixture.py already
# made).
from structures_step_fixture import (  # noqa: E402,F401
    _boundary_point,
    ACCESS_A,
    RESOLUTION_METERS,
    BUFFER_METERS,
    ORIGIN_X,
    ORIGIN_Y,
    COLS,
    ROWS,
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
    _patch_set,
    _candidate_signature,
)

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
    "farm_roads", "farmland_classifications", "hydric_union", "floodplain_union",
    "production_areas", "selected_water_zone", "selected_road_corridor", "tree_zone_patches",
}, sorted(_consumed)
for _name in ("valleys", "hydric_floodplain_union", "floodplain_data_is_fallback", "anchor_lon_lat", "exclusion_zones"):
    assert _name not in _consumed, f"{_name} only feeds a self-compute the committed edges close, or nothing"
# THE TWO DRAINAGE GATES, each its OWN edge off the cache -- because they
# are two different problems (drainage under a foundation, flood risk
# around a building), a site can break either or both, and constraints_
# violated has to name which. The COMBINED union roads reads stays
# undeclared above: it reaches the entry point only for the nested
# self-computes the committed edges close.
assert _consumed["hydric_union"].cache_path == "hydric_union"
assert _consumed["hydric_union"].forward_as == "hydric_union"
assert _consumed["floodplain_union"].cache_path == "floodplain_union"
assert _consumed["floodplain_union"].forward_as == "floodplain_union"
assert _consumed["hydric_union"].source == _consumed["floodplain_union"].source == step_registry.SOURCE_CACHE

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


# --- 5 [test 5]. THE RUN-LEVEL FLAGS ARE ON THE RESULT -------------------

# FIVE NOW: drainage_gates_checked joined the four with the two hard
# drainage gates, and it is the one that also changes a per-feature
# property rather than only the shared confidence notes -- a generated
# candidate's constraints_satisfied GUARANTEE is reconstructed on the
# wire, so the wire has to be told which drainage gates the run applied
# or it would have to claim "clear" for a check that never ran.
assert set(RESULT["run_flags"]) == {
    "shading_is_rough_proxy", "road_proximity_source", "tree_zone_exclusion_available",
    "drainage_gates_checked", "spacing_meters", "max_structure_footprint_acres",
}, sorted(RESULT["run_flags"])
FLAGS = RESULT["run_flags"]
assert FLAGS["shading_is_rough_proxy"] is True
assert FLAGS["road_proximity_source"] == ROAD_SOURCE
assert FLAGS["tree_zone_exclusion_available"] is True
# THE FIXTURE'S SSURGO ROWS CARRY ONE 85%-HYDRIC MAP UNIT and no NHD
# streams, so the terrain warm-up derives a real hydric union and NO
# floodplain union -- the hydric gate ran, the floodplain gate did not,
# and the flag says exactly that rather than reporting both clear.
assert FLAGS["drainage_gates_checked"] == ["outside_hydric_soil"], FLAGS["drainage_gates_checked"]
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
            hydric_union_utm=inputs["hydric_union_utm"],
            floodplain_union_utm=inputs["floodplain_union_utm"],
            **inputs["thresholds"],
        )
        # AND ITS PAD MUST BE UNCLIPPED -- the full cap, not a boundary
        # sliver. The assertions below read the pad as "the same fixed
        # footprint a generated candidate gets", which is only a statement
        # about a spot far enough inside the parcel to keep all of it.
        if (
            measured is not None
            and all(measured["constraints"].values())
            and abs(
                measured["footprint_area_acres"] - solar_suitability.MAX_STRUCTURE_FOOTPRINT_ACRES
            ) < 1e-3
        ):
            nudged, NUDGE = probe, (dx, dy)
            break
    assert nudged is not None, (
        "no gate-clearing spot with an unclipped pad within 40 m of the rank-1 pad on this fixture"
    )
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

# =========================================================================
# --- 12. THE MEASUREMENT, THE GATES AND THE PANEL, ON THE REFERENCE PARCEL
# =========================================================================
# The branch's own numbered tests 1, 3, 5, 7 and 9, measured through the
# real step on the real committed upstream rather than on a synthetic core
# fixture (test_solar_suitability.py holds the pure-core halves).

with Harness() as h:
    s = GENERATE_SESSION
    result = s.result()
    inputs = result["run_inputs"]
    GATE_UNIONS = (inputs["hydric_union_utm"], inputs["floodplain_union_utm"])

    # --- 12a [test 1]. FT TO ROAD: the before AND the after -----------
    # The fix is only visible as a fix if the old value is shown too, and
    # the old value was 0.0 on nine of the eleven clearing footprints.
    road_union = unary_union(inputs["road_geometries_utm"])
    pad_distances_m = []
    point_distances_m = []
    for candidate in GENERATED:
        pad_distances_m.append(float(candidate["polygon_utm"].distance(road_union)))
        point_distances_m.append(float(candidate["distance_to_road_m"]))
    assert all(d == 0.0 for d in pad_distances_m), (
        "THE BEFORE: every shipped candidate's PAD intersects the committed corridor, so the "
        f"pad-based reading was 0.0 ft on all of them -- got {pad_distances_m}"
    )
    assert all(d > 0.0 for d in point_distances_m), (
        f"THE AFTER: every candidate must now report a real distance from its own point -- got "
        f"{point_distances_m}"
    )
    for candidate in GENERATED:
        expected = round(float(Point(candidate["polygon_utm"].centroid).distance(road_union)), 1)
        assert abs(candidate["distance_to_road_m"] - expected) <= 0.2, (
            "the reported distance must be the SITE POINT's distance to the corridor"
        )
    ROAD_FT_BEFORE = [round(d / 0.3048, 1) for d in pad_distances_m]
    ROAD_FT_AFTER = [round(d / 0.3048, 1) for d in point_distances_m]
    print(
        f"12a [test 1]. FT TO ROAD on the reference parcel -- BEFORE (pad): {ROAD_FT_BEFORE}, "
        f"AFTER (point): {ROAD_FT_AFTER}. The pad of every shipped candidate INTERSECTS the "
        "committed corridor, which is what the road-proximity constraint tunes for, so shapely's "
        ".distance() returned 0.0 and the panel's headline figure read zero on every one of them."
    )

    # --- 12b [test 3]. THE THREE-ROW ROAD TABLE -----------------------
    # BEFORE / PART 1 ONLY / BOTH, re-run through the real scoring core on
    # the committed inputs. The middle row is the one that says what the
    # constant actually governs once the measurement is honest.
    def _road_row(buffer_meters, from_point):
        """The clearing set under one (gate geometry, buffer) combination.
        The pad-based gate is reconstructed here because the shipped code
        no longer has one -- which is the point: the 'before' column has
        to be a measurement, not a memory."""
        run = solar_suitability._prepare_scoring_run(
            inputs["dem"], inputs["production_areas"], inputs["water_zones"],
            inputs["road_geometries_utm"], inputs["boundary_polygon_utm"],
            canopy_mask_utm=inputs["canopy_mask_utm"],
            tree_zone_exclusion_polygon_utm=inputs["tree_zone_exclusion_polygon_utm"],
            hydric_union_utm=inputs["hydric_union_utm"],
            floodplain_union_utm=inputs["floodplain_union_utm"],
            **{**inputs["thresholds"], "road_proximity_buffer_meters": buffer_meters},
        )
        side = run["footprint_side_m"]
        region = inputs["boundary_polygon_utm"].intersection(
            run["road_union"].buffer(buffer_meters + side)
        )
        if region.is_empty:
            region = inputs["boundary_polygon_utm"]
        kept = []
        for x, y in solar_suitability._generate_candidate_points(
            region, solar_suitability.CANDIDATE_POINT_SPACING_METERS
        ):
            measured = solar_suitability._measure_footprint(x, y, run)
            if measured is None:
                continue
            outcomes = measured.pop("constraints")
            if from_point:
                distance_m = Point(x, y).distance(run["road_union"])
            else:
                half = side / 2
                pad = shape({
                    "type": "Polygon",
                    "coordinates": [[
                        [x - half, y - half], [x + half, y - half], [x + half, y + half],
                        [x - half, y + half], [x - half, y - half],
                    ]],
                }).intersection(inputs["boundary_polygon_utm"])
                distance_m = pad.distance(run["road_union"])
            outcomes["within_road_proximity_buffer"] = distance_m <= buffer_meters
            if not all(outcomes.values()):
                continue
            measured["_road_ft"] = round(distance_m / 0.3048, 1)
            kept.append(measured)
        kept.sort(key=lambda c: -c["suitability_score"])
        return kept

    OLD_BUFFER_M = 15.0  # ROAD_CORRIDOR_PROXIMITY_METERS as it shipped
    NEW_BUFFER_M = solar_suitability.ROAD_CORRIDOR_PROXIMITY_METERS
    assert NEW_BUFFER_M > OLD_BUFFER_M, "this branch RAISED the Tier 1 constant"
    ROW_BEFORE = _road_row(OLD_BUFFER_M, from_point=False)
    ROW_PART1 = _road_row(OLD_BUFFER_M, from_point=True)
    ROW_BOTH = _road_row(NEW_BUFFER_M, from_point=True)

    # The shipped run IS the bottom row -- the table is not a side model.
    assert [c["suitability_score"] for c in ROW_BOTH[:len(GENERATED)]] == [
        c["suitability_score"] for c in GENERATED
    ], "the 'both' row must reproduce the shipped generate, or it is measuring something else"
    # THE BEFORE COLUMN IS ZEROS. That is the branch's reason to exist.
    assert all(c["_road_ft"] == 0.0 for c in ROW_BEFORE[:3]), (
        f"the pad-based reading must be 0.0 on the top three: {[c['_road_ft'] for c in ROW_BEFORE[:3]]}"
    )
    assert all(c["_road_ft"] > 0.0 for c in ROW_PART1[:3]) and all(c["_road_ft"] > 0.0 for c in ROW_BOTH[:3])

    def _row_text(label, row):
        return (
            f"     {label:<34s} candidates {len(row):>3d}   top-3 scores "
            f"{[c['suitability_score'] for c in row[:3]]}   top-3 ft to road "
            f"{[c['_road_ft'] for c in row[:3]]}   all ft to road "
            f"{sorted(c['_road_ft'] for c in row)}"
        )

    print(
        f"12b [test 3]. THE THREE-ROW ROAD TABLE, reference parcel (Tier 1, the committed "
        f"corridor; ROAD_CORRIDOR_PROXIMITY_METERS {OLD_BUFFER_M:.0f} m -> {NEW_BUFFER_M:.0f} m):\n"
        + _row_text(f"BEFORE (pad, {OLD_BUFFER_M:.0f} m)", ROW_BEFORE) + "\n"
        + _row_text(f"PART 1 ONLY (point, {OLD_BUFFER_M:.0f} m)", ROW_PART1) + "\n"
        + _row_text(f"BOTH (point, {NEW_BUFFER_M:.0f} m)", ROW_BOTH) + "\n"
        "     THE MIDDLE ROW is what the constant actually governs once the measurement is honest: "
        f"a pad-based 15 m gate admitted a point up to 15 + {solar_suitability._footprint_side_meters(solar_suitability.MAX_STRUCTURE_FOOTPRINT_ACRES) / 2:.1f} m "
        "from the corridor, so moving to the point TIGHTENED the real requirement by a half-pad "
        "even with the constant unchanged. Raising it to 30 m restores that reach and adds to it."
    )

    # --- 12c [test 5]. A PLACED SITE NAMES ITS OWN GATE ---------------
    # Hydric and floodplain are two gates, not one, and constraints_
    # violated must name WHICH. The HYDRIC union here is the fixture's own
    # committed one (its 85%-hydric map unit, through the terrain warm-up);
    # the FLOODPLAIN half is synthesised from real on-parcel ground,
    # because the fixture's NHD rows are empty -- which is itself the
    # "gate not applied" case section 5's flags assert.
    HYDRIC_UNION = inputs["hydric_union_utm"]
    assert HYDRIC_UNION is not None and not HYDRIC_UNION.is_empty, (
        "the fixture's 85%-hydric map unit must produce a real union, or these gates are untested"
    )
    assert HYDRIC_UNION.intersects(BOUNDARY_POLYGON_UTM), "the hydric union must reach on-parcel ground"
    HYDRIC_POINT = HYDRIC_UNION.intersection(BOUNDARY_POLYGON_UTM).representative_point()

    # A floodplain band over a GENERATED candidate's own ground -- so the
    # gate bites a real candidate in 12d rather than empty ground, and so
    # the floodplain-only probe sits on a spot the step actually offered.
    FLOOD_TARGET = GENERATED[-1]["polygon_utm"].centroid
    FLOOD_BAND = FLOOD_TARGET.buffer(30.0)
    assert not FLOOD_BAND.intersects(HYDRIC_UNION), (
        "the two fixture grounds must be DISJOINT, or 'hydric only' and 'floodplain only' are not "
        "separate cases"
    )
    # And a band covering BOTH probes, for the site that breaks both gates.
    BOTH_BAND = unary_union([FLOOD_BAND, HYDRIC_POINT.buffer(30.0)])

    def _placed_gates(lon_lat, hydric, floodplain):
        """One placed site, scored against the committed run with the two
        drainage gates set as given -- the real score_placed_structure_
        site() path, through a result whose run_inputs carry those gates."""
        patched = copy.copy(result)
        patched["run_inputs"] = {**inputs, "hydric_union_utm": hydric, "floodplain_union_utm": floodplain}
        site = solar_suitability.score_placed_structure_site(lon_lat, patched)
        feature = wire_translation.placed_structure_site_to_feature(
            site,
            {**result, "run_flags": {
                **result["run_flags"],
                "drainage_gates_checked": [
                    name for name, union in (("outside_hydric_soil", hydric),
                                             ("outside_floodplain", floodplain))
                    if union is not None
                ],
            }},
        )
        return site, feature["properties"]

    # HYDRIC ONLY: names its own gate, and not the other's.
    _, hydric_props = _placed_gates(_lon_lat(HYDRIC_POINT), HYDRIC_UNION, FLOOD_BAND)
    assert "outside_hydric_soil" in hydric_props["constraints_violated"], hydric_props["constraints_violated"]
    assert "outside_floodplain" not in hydric_props["constraints_violated"]
    assert "outside_floodplain" in hydric_props["constraints_satisfied"]
    assert isinstance(hydric_props["suitability_score"], float), "SCORED, not refused -- landform's rule"

    # FLOODPLAIN ONLY: the other gate, and not hydric's.
    _, flood_props = _placed_gates(_lon_lat(FLOOD_TARGET), HYDRIC_UNION, FLOOD_BAND)
    assert "outside_floodplain" in flood_props["constraints_violated"], flood_props["constraints_violated"]
    assert "outside_hydric_soil" not in flood_props["constraints_violated"]
    assert "outside_hydric_soil" in flood_props["constraints_satisfied"]
    assert isinstance(flood_props["suitability_score"], float)

    # BOTH: names BOTH. This is the case a single combined union can never
    # report, and the reason the two were split apart upstream.
    _, both_props = _placed_gates(_lon_lat(HYDRIC_POINT), HYDRIC_UNION, BOTH_BAND)
    assert {"outside_hydric_soil", "outside_floodplain"} <= set(both_props["constraints_violated"]), (
        both_props["constraints_violated"]
    )
    assert isinstance(both_props["suitability_score"], float)
    # The two gates are never merged on the wire: a site clear of both
    # lists both as satisfied and neither as violated.
    _, clear_props = _placed_gates(PLACED_A, HYDRIC_UNION, FLOOD_BAND)
    assert {"outside_hydric_soil", "outside_floodplain"} <= set(clear_props["constraints_satisfied"])
    assert not ({"outside_hydric_soil", "outside_floodplain"} & set(clear_props["constraints_violated"]))
    def _drainage(props):
        return sorted(
            gate for gate in props["constraints_violated"]
            if gate in ("outside_hydric_soil", "outside_floodplain")
        )

    print(
        f"12c [test 5]. A PLACED SITE NAMES ITS OWN GATE. Drainage gates violated: a site on "
        f"hydric ground alone -> {_drainage(hydric_props)}, scored "
        f"{hydric_props['suitability_score']}/100; a site in the floodplain alone -> "
        f"{_drainage(flood_props)}, scored {flood_props['suitability_score']}/100; a site on BOTH "
        f"-> {_drainage(both_props)}, scored {both_props['suitability_score']}/100; a site clear "
        f"of both -> {_drainage(clear_props)}, with both listed as SATISFIED. (The hydric probe "
        f"also breaks the water and road gates -- it sits on the committed pond ground, far from "
        f"the corridor -- and its full list is {sorted(hydric_props['constraints_violated'])}; "
        "that is the point of naming gates individually.) Every one is SCORED and committable -- "
        "landform's rule exactly: a generated candidate is gated and can never land there, a site "
        "the user places commits with the crossing recorded -- and a single COMBINED union could "
        "not have named which gate was broken."
    )

    # --- 12d [test 4]. NO GENERATED CANDIDATE LANDS ON EITHER GROUND ---
    def _generate_with(hydric, floodplain):
        return solar_suitability.find_candidate_solar_zones(
            inputs["dem"], inputs["production_areas"], inputs["water_zones"],
            inputs["road_geometries_utm"], inputs["boundary_polygon_utm"],
            canopy_mask_utm=inputs["canopy_mask_utm"],
            tree_zone_exclusion_polygon_utm=inputs["tree_zone_exclusion_polygon_utm"],
            hydric_union_utm=hydric,
            floodplain_union_utm=floodplain,
            max_candidates=10 ** 6,
            **inputs["thresholds"],
        )

    ungated = _generate_with(None, None)

    # THE FIXTURE'S OWN COMMITTED HYDRIC UNION FIRST, because it is the
    # real gated geometry and the honest finding about it has to be
    # stated: it sits away from the committed road corridor, so it
    # intersects NO clearing pad on this parcel and excludes nothing here.
    # That makes it a true assertion and a VACUOUS one, which is exactly
    # why the bands below exist.
    real_hydric_run = _generate_with(HYDRIC_UNION, None)
    assert not any(c["polygon_utm"].intersects(HYDRIC_UNION) for c in real_hydric_run)
    REAL_HYDRIC_BITES = len(real_hydric_run) < len(ungated)

    # SO THE GATES ARE PUT OVER GROUND THE STEP ACTUALLY OFFERED: a band
    # on rank 1's own pad and a band on rank 3's, disjoint, so each gate's
    # effect is separately visible and neither assertion can pass vacuously.
    HYDRIC_BAND = unary_union([HYDRIC_UNION, GENERATED[0]["polygon_utm"].centroid.buffer(22.0)])
    assert not HYDRIC_BAND.intersects(FLOOD_BAND), (
        "the two gate grounds must be DISJOINT, or 'each gate excludes its own ground' cannot be "
        "told from 'one gate excludes both'"
    )

    both_gates = _generate_with(HYDRIC_BAND, FLOOD_BAND)
    hydric_only = _generate_with(HYDRIC_BAND, None)
    flood_only = _generate_with(None, FLOOD_BAND)

    # THE GATE, ASSERTED AGAINST THE GEOMETRY ITSELF, not against a flag.
    for candidate in both_gates:
        assert not candidate["polygon_utm"].intersects(HYDRIC_BAND), "a GENERATED candidate on hydric ground"
        assert not candidate["polygon_utm"].intersects(FLOOD_BAND), "a GENERATED candidate in the floodplain"
    assert len(both_gates) < len(ungated), "the two gates must exclude real ground on this parcel"

    # EACH GATE IS INDEPENDENT. A combined union cannot make this
    # statement, which is the whole reason the two were split upstream.
    assert not any(c["polygon_utm"].intersects(HYDRIC_BAND) for c in hydric_only)
    assert any(c["polygon_utm"].intersects(FLOOD_BAND) for c in hydric_only), (
        "the hydric gate alone must NOT exclude floodplain ground -- they are two gates, not one"
    )
    assert not any(c["polygon_utm"].intersects(FLOOD_BAND) for c in flood_only)
    assert any(c["polygon_utm"].intersects(HYDRIC_BAND) for c in flood_only), (
        "the floodplain gate alone must NOT exclude hydric ground"
    )
    dropped_hydric = [c for c in ungated if c["polygon_utm"].intersects(HYDRIC_BAND)]
    dropped_flood = [c for c in ungated if c["polygon_utm"].intersects(FLOOD_BAND)]
    assert dropped_hydric and dropped_flood, "both bands must cover ground the ungated run offered"
    print(
        f"12d [test 4]. GENERATED CANDIDATES NEVER LAND ON EITHER GROUND. The fixture's REAL "
        f"committed hydric union (its 85%-hydric map unit, through the terrain warm-up) is never "
        f"intersected by a generated candidate -- and on this parcel it sits away from the "
        f"committed road corridor and so excludes "
        f"{'ground of its own' if REAL_HYDRIC_BITES else 'NOTHING (a true but vacuous pass)'}, "
        f"which is why the gates are also put over ground the step actually offered: "
        f"{len(ungated)} pads clear the stack ungated, {len(hydric_only)} with the hydric gate "
        f"alone, {len(flood_only)} with the floodplain gate alone, {len(both_gates)} with both -- "
        f"and not one of those {len(both_gates)} intersects either ground. The two are "
        f"INDEPENDENT: hydric alone still leaves candidates on floodplain ground and floodplain "
        f"alone still leaves candidates on hydric ground ({len(dropped_hydric)} and "
        f"{len(dropped_flood)} pads respectively). One combined union could not tell those two "
        "runs apart."
    )

    # --- 12e [test 6]. A FULLY GATED PARCEL SAYS SO -------------------
    # Trees deliberately targets hydric ground, so on a wet parcel trees
    # and structures compete for opposite qualities -- and hydric plus
    # floodplain plus canopy, road proximity and the score floor can leave
    # nothing. That is an answer, and it must read as one.
    wall_to_wall = BOUNDARY_POLYGON_UTM.buffer(50.0)
    gated_result = solar_suitability.identify_solar_candidate_zones(
        REAL_BOUNDARY,
        dem=inputs["dem"],
        boundary_polygon_utm=inputs["boundary_polygon_utm"],
        production_areas=inputs["production_areas"],
        selected_water_zone=water_suitability.NO_WATER_ZONE,
        selected_road_corridor=road_corridors.NO_ROAD_CORRIDOR,
        tree_zone_patches=[],
        canopy_height=_build_canopy(inputs["dem"]),
        farm_roads=FIXTURE_ROADS,
        farmland_classifications=FIXTURE_FARMLAND,
        hydric_union=wall_to_wall,
        floodplain_union=None,
    )
    assert gated_result["all_scored_candidates"] == []
    assert gated_result["selected_structure_site"] is None
    assert gated_result["zones_geojson"]["features"] == []
    gated_narrative = gated_result["narrative_data"]
    assert gated_narrative["site_found"] is False and gated_narrative["candidate_count"] == 0
    assert gated_narrative["selected_site"] is None
    reasons = gated_narrative["no_candidates"]
    assert reasons is not None, "an empty run must SAY WHY rather than come back blank"
    assert reasons["reason"] == "every_pad_failed_a_gate"
    assert reasons["blocking_gates"][0]["gate"] == "outside_hydric_soil"
    assert reasons["blocking_gates"][0]["pads_rejected"] == reasons["pads_measurable"]
    assert gated_narrative["gates"]["hydric_gate_checked"] is True
    assert gated_narrative["gates"]["floodplain_gate_checked"] is False, (
        "the floodplain union was None on this run, and NOT CHECKED must not read as clear"
    )
    gated_payload = step_orchestrator.build_structures_payload(gated_result, {})
    assert gated_payload["sites"] == [] and gated_payload["structure_sites"]["features"] == []
    assert gated_payload["summary"]["no_candidates"] == reasons, (
        "the panel reads the reason off summary, or it renders an empty list that looks broken"
    )
    print(
        f"12e [test 6]. A FULLY GATED PARCEL REPORTS THAT STATE: with hydric soil over the whole "
        f"parcel the step returns ZERO candidates and narrative_data says reason="
        f"{reasons['reason']!r}, {reasons['pads_sampled']} pad(s) sampled, "
        f"{reasons['pads_measurable']} measurable, blocking gate "
        f"{reasons['blocking_gates'][0]['gate']!r} rejecting all "
        f"{reasons['blocking_gates'][0]['pads_rejected']} of them -- with floodplain reported NOT "
        "CHECKED rather than clear. The payload carries it under summary.no_candidates, so the "
        "panel says so instead of rendering an empty list."
    )

    # --- 12f [tests 7, 8, 9]. THE PANEL'S OWN ROWS AND NOTICES --------
    payload = GENERATE_PAYLOAD
    summary = payload["summary"]
    features = payload["structure_sites"]["features"]

    # [test 9] road_proximity_source is a STEP-LEVEL value, promoted
    # beside candidate_count because ft-to-road's MEANING depends on it.
    assert summary["road_proximity_source"] == ROAD_SOURCE == summary["gates"]["road_proximity_source"]
    assert summary["run_flags"]["road_proximity_source"] == ROAD_SOURCE
    assert all(f["properties"]["road_proximity_source"] == ROAD_SOURCE for f in features)
    assert all(row["road_proximity_source"] == ROAD_SOURCE for row in payload["sites"])

    # [test 7] The bands are on the wire; the frontend holds no threshold.
    assert summary["scales"]["solar_rating"]["bands"] == solar_suitability.SOLAR_RATING_BANDS
    assert summary["scales"]["solar_rating"]["not_a"] == "rank_among_candidates"
    assert summary["scales"]["elevation_position"]["bands"] is production_area_ceiling.ELEVATION_POSITION_BANDS
    assert "signed_distance_to_production_ft" in summary["scales"]
    for feature in features:
        props = feature["properties"]
        assert props["solar_rating"] == solar_suitability._solar_rating(props["solar_value"])
        low, high = solar_suitability.SOLAR_RATING_BANDS[props["solar_rating"]]
        assert low <= props["solar_value"] <= high
    # [test 8] elevation_position against the IMPORTED constant, not a copy.
    for feature in features:
        props = feature["properties"]
        assert props["elevation_position"] == production_area_ceiling._elevation_position(
            props["elevation_percentile_of_parcel"]
        )
        low, high = production_area_ceiling.ELEVATION_POSITION_BANDS[props["elevation_position"]]
        assert low <= props["elevation_percentile_of_parcel"] <= high
    # The panel's four rows reach the `sites` projection too.
    for row in payload["sites"]:
        assert set(("solar_value", "solar_rating", "elevation_percentile_of_parcel",
                    "elevation_position", "signed_distance_to_production_ft")) <= set(row)
        assert row["solar_rating"] is not None and row["elevation_position"] is not None

    # THE SOLAR DISTRIBUTION, REPORTED -- the cuts were chosen against it.
    all_pads = []
    dist_run = solar_suitability._prepare_scoring_run(
        inputs["dem"], inputs["production_areas"], inputs["water_zones"],
        inputs["road_geometries_utm"], inputs["boundary_polygon_utm"],
        canopy_mask_utm=inputs["canopy_mask_utm"],
        tree_zone_exclusion_polygon_utm=inputs["tree_zone_exclusion_polygon_utm"],
        **inputs["thresholds"],
    )
    for x, y in solar_suitability._generate_candidate_points(BOUNDARY_POLYGON_UTM, 10.0):
        measured = solar_suitability._measure_footprint(x, y, dist_run)
        if measured is not None:
            all_pads.append(measured["solar_value"])
    all_pads.sort()
    words = {}
    for value in all_pads:
        word = solar_suitability._solar_rating(value)
        words[word] = words.get(word, 0) + 1
    quantile = lambda q: all_pads[min(len(all_pads) - 1, int(q * len(all_pads)))]  # noqa: E731
    print(
        f"12f [tests 7, 8, 9]. THE PANEL'S ROWS AND NOTICES.\n"
        f"     SOLAR VALUE DISTRIBUTION, {len(all_pads)} measurable pads at 10 m spacing over the "
        f"whole reference parcel: min {all_pads[0]}, p25 {quantile(0.25)}, median "
        f"{quantile(0.50)}, p75 {quantile(0.75)}, max {all_pads[-1]} -- a TIGHT CLUSTER (this "
        f"fixture's bench DEM faces north, so aspect_score sits near 0 everywhere). Words over "
        f"that distribution: {dict(sorted(words.items(), key=lambda kv: solar_suitability.SOLAR_RATING_BANDS[kv[0]][0]))}. "
        f"The three shipped candidates read "
        f"{[(f['properties']['solar_rating'], f['properties']['solar_value']) for f in features]}.\n"
        f"     elevation_position on the shipped candidates: "
        f"{[(f['properties']['elevation_position'], f['properties']['elevation_percentile_of_parcel']) for f in features]}, "
        "classified by PRODUCTION's own ELEVATION_POSITION_BANDS -- the imported object, asserted "
        "by identity, never a copy of the cuts.\n"
        f"     road_proximity_source {ROAD_SOURCE!r} reaches summary (step-level), summary.gates, "
        "summary.run_flags, every feature and every `sites` row; both band sets and the signed "
        "production distance's rule ship in summary.scales, so the frontend holds no threshold."
    )


print(
    "\n13 [test 12]. REGRESSION: run the other test files separately -- test_step_registry.py, "
    "test_wire_translation.py, test_wire_translation_inbound.py, test_step_orchestrator.py, "
    "test_step_commit.py, test_water_step.py, test_roads_step.py, test_trees_step.py, "
    "test_solar_suitability.py, test_solar_suitability_pipeline.py, test_solar_road_fallback.py, "
    "test_tree_zone_candidates.py, test_session_api.py, test_fencing.py, test_render_layout_map.py, "
    "test_pipeline_context.py."
)
print("\nAll structures step checks passed.")
