"""
session_design.py

THE COMMITTED DESIGN, READ BACK OUT OF A SESSION -- so the report and the
layout map narrate and draw WHAT THE USER CHOSE rather than what a fresh
pipeline walk would have chosen for them.

    build_session_design(session_id, store) -> SessionDesign
    layout_layers(design)                   -> the fetch_layout_layers() dict

--- NO PRODUCTION CONSUMER NOW -------------------------------------------

The narrated report, its PDF assembly and Claude call, and the matplotlib
layout map this module fed were retired when the report job switched to
the site data report (site_report.py), whose Design section reads the
committed steps off the Design Document directly (design_record.py). What is still read
from here is SessionWorkingDataExpiredError, which session_report.
error_payload() maps. This module is kept, unchanged in behaviour, and is
retired in a branch of its own -- deleting it in the same change that
removed its only reader is how a live dependency is found in production
rather than in review. What follows describes it as it was built.

WHY THIS EXISTS. The narrated report's generator and the layout map's
fetch_layout_layers() both ran pipeline_context.build_pipeline_context():
every KSOP step recomputed from the boundary, every winner picked by the
pipeline's own ranking. On the BATCH path that is exactly right -- there is
no user and no decision, only the boundary. On the SESSION path it is
silently wrong: the user has committed a design, and a report built off a
fresh walk describes a different one while looking completely normal. This
module is the other source -- the session's committed decisions -- in the
shapes those two consumers already read.

build_pipeline_context() IS NOT TOUCHED and is not deleted. It is still the
batch path's walk and the only known-correct full-chain reference.

--- A REPORT IS GENERATED IN THE SESSION THAT MADE THE DESIGN ------------

That is the product rule this module is built on, and it is what makes the
whole thing a wiring pass rather than an architectural one. Reports are
paid, one-off artifacts: commit a design, generate, pay, get a link. The
report does not have to be reconstructible from a cold session.

So this module READS. It fetches nothing, and it re-runs no KSOP
computation. Its two sources are both already in hand:

  THE DESIGN DOCUMENT (authoritative, durable) -- every committed
  FeatureCollection, its provenance and the step's recorded user inputs.
  Rehydrated through each step's own declared inbound translator, via
  step_orchestrator.committed_internal_value(), which serves the
  rehydration the commit already paid for.

  THE SESSION CACHE (tier 2, evictable) -- ParcelData, the terrain warm-up,
  and each step's GENERATE RESULT, which is where the per-module
  `narrative_data` block the report formats every data section from
  actually lives.

--- AND AN EVICTED SESSION FAILS, LOUDLY AND SAYABLY ---------------------

session_cache.rebuild_session_context() can reconstruct ParcelData and the
terrain warm-up from the document. It CANNOT reconstruct a committed step's
generate result -- proposals are deliberately not written to the document
(design_document.py's own rule: the document holds decisions, never derived
bulk data), and step_orchestrator refuses to serve a committed step's
payload at all (StepNotGeneratedError).

A report assembled from a partly-populated context would be a plausible
document describing nothing, which is the worst available outcome. So a
committed step with no generate result in the cache raises
SessionWorkingDataExpiredError, carrying WORKING_DATA_EXPIRED -- one
sentence a user can act on -- rather than a block that quietly reads as "no
data available".

--- THE FOUR SHAPE DIVERGENCES, AND HOW EACH RESOLVES -------------------

A session's committed shape differs from a PipelineContext in four places.
SessionDesign carries the session's shape as the primary field and the
PipelineContext-shaped singleton beside it, so neither consumer has to
guess:

  STRUCTURE SITES are a LIST (up to three generated plus two placed);
  PipelineContext holds one `selected_structure_site`. The report's solar
  block narrates ONE site by construction (solar_suitability.build_
  narrative_data() emits `selected_site`, not a list), and it narrates the
  best COMMITTED one here. THE MAP DRAWS EVERY COMMITTED SITE -- a design
  with two placed sites draws two pins. See `structure_sites`.

  WATER commits SEVERAL zones; PipelineContext holds one
  `selected_water_zone`. The narrative block lists every committed zone;
  the map draws every committed zone's ripple texture; and
  `selected_water_zone` is wire_translation.water_zone_union() of them --
  the same union every downstream consumes edge already takes -- for a
  reader that wants the PipelineContext shape.

  ROADS commits ONE network out of up to three candidates, which maps
  cleanly: the commit contract caps it at one (max_features=1 counted in
  networks), so `selected_road_corridor` is wire_translation.selected_road_
  network() of the commit and the narrative block is that network's OWN
  block -- build_narrative_data() already ran once per access point, so
  reading the committed network's block is a lookup, not a filter.

  FENCING has no PipelineContext field at all; the layout map's
  fetch_layout_layers() computed it after the context was built, through its
  own identify_fencing() call. In a session fencing IS a step, so the
  design reads its COMMITTED fence lines and the map draws those. A session
  whose fencing step is not committed draws no fence -- that is the honest
  reading of "the user made no fencing decision", not a gap to fill with a
  computed one.

--- HOW EACH NARRATIVE BLOCK IS MADE COMMITTED-ONLY ---------------------

The narrated report formatted each data section from the producing module's
`narrative_data` block. Those blocks narrate a CANDIDATE SET -- every
surviving zone, every scored patch -- so forwarding one whole would put a
candidate the user DECLINED into the report. Each step therefore takes the
one route its own shapes allow, and the route is a property of that
module's contracts, not a preference:

  landform  FILTER. block['patches'] is kept for the committed ids and
            parcel.selected_acres / selected_pct_of_parcel are recomputed
            over what is left. NOT re-narrated: production_area_ceiling.
            build_narrative_data() reads `aspect_available` and
            `soil_available` off every patch, and those two are
            deliberately not on the wire (wire_translation.
            _ADVISORY_FIELDS_NOT_ON_THE_WIRE), so a rehydrated patch cannot
            supply them.

  water     FILTER. block['zones'] is kept for the committed ids, the three
            counts are recomputed, `selection` is water_survey_areas.
            select_survey_zone()'s own answer over the COMMITTED zones, and
            `presentation` is dropped -- a presented set computed over the
            candidate list does not describe the committed one, and the
            report's formatter already has an honest no-presentation path.
            NOT re-narrated: rehydrate_water_survey_zone() carries identity
            and geometry only, by design, so the measurements the block is
            built from are not on a committed zone.

  roads     READ. The committed network's own block, looked up by the
            network_id its committed features carry.

  trees     RE-NARRATE, through tree_zone_candidates.build_narrative_data()
            over the committed patches. Its rehydration is complete (every
            scored field survives the round trip) and its block carries no
            per-zone id to filter on, so the owning module narrating the
            committed set is both possible and the more faithful answer:
            candidate_count and claimed acreage come out describing the
            decision rather than the candidate list.

  structures RE-NARRATE, through solar_suitability.build_narrative_data()
            over the committed sites. Its block has no candidate list at
            all -- only `selected_site`, select_optimal_structure_site()'s
            pick over whatever list it was handed -- so filtering cannot
            reach it, and handing the module the committed sites is what
            makes `selected_site` the user's best site instead of the
            run's. Its rehydration is complete (a superset of a scored
            candidate), and the run-level flags it also needs are on the
            step result's own `run_flags`.

EVERY BLOCK CARRIES A `commitment` SUB-BLOCK on top (COMMITMENT_KEY),
purely additive, under the narrative_data convention's own additive rule:
which step it came from, how many candidates the run produced, how many the
user committed, and whether the commit was EMPTY. The narrated report read
it to lead each section with the decision -- and an empty commit is the
case that needs it most, because every formatter's own "nothing here" text
describes a gap in the DATA, which is exactly the wrong thing to say about
a user who decided there would be no pond.

--- THE ACCESS POINT ----------------------------------------------------

The narrated report's generator took `anchor_lon_lat` and validated it
against the boundary, because on the batch path there was nothing else to
get it from.
In a session it is already committed and already validated: it is the roads
step's own user input, checked at generate (Consumed/UserInput.validate)
and again at commit (step_orchestrator.validate_commit_inputs). So the
design READS it off the roads commit -- the declared access point whose
wire_translation.access_point_key() matches the committed features'
network_id -- and nothing here takes it as an argument or re-validates it.
An EMPTY roads commit has no access point and `anchor_lon_lat` is None,
which is a real answer: no road was placed.
"""

from dataclasses import dataclass, field
from typing import Optional

import design_document
import session_cache
import session_manager
import solar_suitability
import step_orchestrator
import step_registry
import tree_zone_candidates
import water_survey_areas
import wire_translation

# --- the evicted-session failure -------------------------------------

# THE ONE SENTENCE. Kept as a constant because it is the whole point of the
# failure: a user who sees it has to know what to do next, and a test has to
# be able to assert that they were told.
WORKING_DATA_EXPIRED = (
    "this session's working data has expired; reopen and recommit to generate a report"
)


class SessionWorkingDataExpiredError(Exception):
    """
    A committed step's generate result is gone from the session cache.

    THE COMMITTED DESIGN IS INTACT -- it is in the Design Document, which is
    durable and authoritative. What expired is tier 2: the step's generate
    result, which is where its narrative block lives and which
    session_cache.rebuild_session_context() deliberately cannot rebuild (a
    proposal is regenerable, so it is never written to the document; and
    step_orchestrator refuses to serve a committed step's payload at all --
    see StepNotGeneratedError).

    RAISED RATHER THAN DEGRADED, and that is the entire reason this class
    exists. A report assembled from a half-populated context reads as a
    finished document about a property nobody analysed. Failing here, with
    a sentence that names the remedy, is the only honest answer.
    """

    def __init__(self, session_id: str, step_id: str):
        self.session_id = session_id
        self.step_id = step_id
        super().__init__(
            f"session '{session_id}': step '{step_id}' is committed, but the "
            f"generated run its narrative is read from is no longer cached -- "
            f"{WORKING_DATA_EXPIRED}. The committed design itself is intact in "
            f"the Design Document; only this session's working data is gone."
        )


# --- the commitment marker -------------------------------------------

# The key every session-derived narrative block carries on top of the
# producing module's own block. PURELY ADDITIVE -- no module's block loses
# or renames anything -- which is the narrative_data convention's own rule
# for adding to one of these.
#
# DECLARED HERE SINCE THE NARRATED REPORT WAS RETIRED. It used to be
# imported from the narrated report, its only reader, so producer and
# consumer could not drift apart. That reader is gone -- the site data
# report's Design section reads committed steps off the Design Document
# (design_record.py), not this module -- so the marker is written and
# currently read by nothing. It stays, unchanged, until this module is
# retired in its own branch, where what is actually left can be seen.
COMMITMENT_KEY = "commitment"


def _commitment(step_id: str, candidate_count: int, committed_count: int) -> dict:
    """
    What the report leads a section with: this is a DECISION, taken from
    this many candidates, and here is whether it was an empty one.

    `declined_count` is reported rather than left to subtraction because an
    empty commit makes it equal to candidate_count, and that is the number
    the sentence "you declined all N" is written from.
    """
    return {
        "source": "committed_design",
        "step_id": step_id,
        "candidate_count": int(candidate_count),
        "committed_count": int(committed_count),
        "declined_count": max(int(candidate_count) - int(committed_count), 0),
        "empty": committed_count == 0,
    }


# --- the design ------------------------------------------------------


@dataclass
class SessionDesign:
    """
    One session's COMMITTED design, in the shapes the report and the layout
    map already read.

    EVERY FIELD IS A READ. Nothing here was fetched and nothing was
    recomputed by a KSOP module: the committed halves come from the Design
    Document through each step's own rehydrator, the terrain halves off the
    session cache's warm-up, and the narrative blocks off the steps' own
    generate results (see the module docstring for the per-step route).

    THE LIST AND THE SINGLETON ARE BOTH HERE ON PURPOSE. `water_zones` and
    `structure_sites` are what the user committed and what the map draws;
    `selected_water_zone` and `selected_structure_site` are the
    PipelineContext-shaped singletons beside them, for a reader that wants
    the batch path's field. Neither is derived from the other at read time
    -- both are built once, here, so the two can never disagree.
    """

    session_id: str
    boundary: list
    parcel_data: object

    # Terrain, off the warm-up -- no user decision is involved in any of
    # these, which is why session creation can compute them before the
    # first step runs.
    valleys: list
    keypoints: list
    exclusion_zones: dict

    # The committed decisions, per step. An EMPTY list is a real decision
    # ("nothing goes here"), never "not decided" -- `committed_steps` below
    # is what says which steps were decided at all.
    production_areas: list
    water_zones: list
    road_networks: list
    tree_zone_patches: list
    structure_sites: list
    fence_features: list

    # The PipelineContext-shaped singletons. None where the batch path's
    # own field would be None.
    selected_water_zone: Optional[dict]
    selected_road_corridor: Optional[dict]
    selected_structure_site: Optional[dict]

    # The roads step's own committed user input -- see the module docstring.
    anchor_lon_lat: Optional[tuple]

    # Keyed exactly like pipeline_context.PipelineContext.narrative_data, so
    # the retired narrated report's generator took it unchanged.
    narrative_data: dict

    # Which steps the document says are committed. A step that is not here
    # contributed no decision and no narrative block; its report section
    # falls back to that formatter's own honest no-data text.
    committed_steps: tuple = ()

    # The committed FeatureCollections, verbatim off the document, for the
    # layers the map draws in WIRE form (roads, structures, fencing). Read
    # rather than rebuilt: these are the exact bytes the user committed.
    committed_features: dict = field(default_factory=dict)

    @property
    def dem(self) -> dict:
        return self.parcel_data.dem

    @property
    def boundary_polygon_utm(self):
        return self.parcel_data.boundary_polygon_utm

    @property
    def parcel_acres(self) -> float:
        from raster_grid import SQUARE_METERS_PER_ACRE

        return self.parcel_data.boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE


# --- reading one committed step --------------------------------------


def _is_committed(document: dict, step_id: str) -> bool:
    entry = document["steps"].get(step_id)
    return entry is not None and entry["status"] == design_document.STATUS_COMMITTED


def _committed_features(document: dict, step_id: str) -> list:
    entry = document["steps"].get(step_id) or {}
    return list((entry.get("features") or {}).get("features") or [])


def _run_result(context, document, session_id: str, step_id: str):
    """
    The step's own generate result, or SessionWorkingDataExpiredError.

    THE EVICTION GATE, and the only place it is checked. Called for every
    COMMITTED step whose narrative block this module needs -- so a session
    that lost its working data fails on the first such step rather than
    assembling four good blocks and one empty one.
    """
    result = context.step_proposals.get(step_id)
    if result is None:
        raise SessionWorkingDataExpiredError(session_id, step_id)
    return result


# --- the five narrative blocks ---------------------------------------


def _landform_block(result: dict, committed: list, step_id: str = "landform") -> dict:
    """
    FILTERED. production_area_ceiling.build_narrative_data()'s own block with
    `patches` kept for the committed ids only.

    TWO PARCEL FIGURES ARE RECOMPUTED and they are the only values this
    function touches: `selected_acres` (the sum of the committed blocks'
    own area_acres -- the same sum the run made over its own set) and
    `selected_pct_of_parcel` (that sum over parcel.total_acres). Every other
    number in the block is a measurement of the parcel or of a gate, not of
    the selected set, and is passed through exactly as the run made it.

    WHY NOT RE-NARRATE, when trees and structures do: build_narrative_data()
    reads `aspect_available` and `soil_available` off every patch and
    neither survives the wire (wire_translation.
    _ADVISORY_FIELDS_NOT_ON_THE_WIRE names them and says why). Handing it a
    rehydrated patch would either raise or require inventing two advisory
    flags, and an invented availability flag is exactly the silent falsehood
    those flags exist to prevent.
    """
    block = result["narrative_data"]
    committed_ids = {int(patch["id"]) for patch in committed}
    patches = [p for p in block["patches"] if int(p["id"]) in committed_ids]

    parcel = dict(block["parcel"])
    selected_acres = round(sum(float(p["area_acres"]) for p in committed), 1)
    total_acres = float(parcel.get("total_acres") or 0.0)
    parcel["selected_acres"] = selected_acres
    parcel["selected_pct_of_parcel"] = (
        round(100.0 * selected_acres / total_acres, 1) if total_acres else 0.0
    )

    return {
        **block,
        "parcel": parcel,
        "patches": patches,
        COMMITMENT_KEY: _commitment(step_id, len(block["patches"]), len(patches)),
    }


def _water_block(result: dict, committed: list, step_id: str = "water") -> dict:
    """
    FILTERED. water_survey_areas.build_narrative_data()'s own block with
    `zones` kept for the committed ids only.

    WHAT IS RECOMPUTED, and nothing else is: the three counts that describe
    the list (`zone_count` and the per-type pair), and `selection` --
    water_survey_areas.select_survey_zone()'s OWN answer over the committed
    zones, so "the selected zone" is the best zone the USER kept rather than
    the best the run found. `zone_found` follows the committed list, because
    the report's water section leads off it.

    `presentation` IS DROPPED TO None. It describes a reading order computed
    over the candidate set -- which two zones of each type to lead with, out
    of thirteen and three -- and that is not a statement about the committed
    set. The report's formatter has an honest path for a block with no
    presentation block ("all of them are listed"), which is exactly true
    here. `dropped_count` is passed through UNCHANGED and is still true: the
    floor pruned those before the user ever saw them, which is a different
    fact from declining one.

    WHY NOT RE-NARRATE: rehydrate_water_survey_zone() carries identity,
    acreage and geometry and says, at length, why it carries so little. The
    per-criterion means, the seed blend score and the drainage score the
    block is built from do not survive a commit, so the run's own zone dicts
    -- matched by the committed id -- are the only place they exist.
    """
    block = result["narrative_data"]
    committed_ids = {int(zone["id"]) for zone in committed}

    zones = [z for z in block["zones"] if int(z["id"]) in committed_ids]
    # The run's OWN zone dicts for the committed ids -- select_survey_zone()
    # ranks on measurements that are on those and not on the block.
    run_zones = [z for z in result["zones"] if int(z["id"]) in committed_ids]
    selected = water_survey_areas.select_survey_zone(run_zones)

    by_type = {}
    for zone in zones:
        by_type[zone["survey_type"]] = by_type.get(zone["survey_type"], 0) + 1

    return {
        **block,
        "zone_found": bool(zones),
        "zone_count": len(zones),
        "embankment_zone_count": by_type.get(water_survey_areas.SURVEY_TYPE_EMBANKMENT, 0),
        "excavated_zone_count": by_type.get(water_survey_areas.SURVEY_TYPE_EXCAVATED, 0),
        "presentation": None,
        "selection": (
            None
            if selected is None
            else {
                **(block.get("selection") or {}),
                "selected_zone_id": selected["id"],
                "selected_survey_type": selected["survey_type"],
            }
        ),
        "zones": zones,
        COMMITMENT_KEY: _commitment(step_id, len(block["zones"]), len(zones)),
    }


def _roads_block(proposals: dict, committed_features: list, step_id: str = "roads") -> dict:
    """
    READ. The committed network's own narrative block.

    NOTHING IS FILTERED OR REBUILT because nothing has to be: the roads step
    accumulates one candidate set per access point and build_narrative_data()
    ran once per generate, so every candidate network already has its own
    whole block. Picking the committed one by the network_id its features
    carry IS the decision, and a network the user generated and declined is
    simply another key in the store that nothing here looks at.

    An EMPTY roads commit has no network_id to look up and gets a block
    marked empty, with no network described -- see _commitment().
    """
    keys = {
        (feature.get("properties") or {}).get("network_id")
        for feature in committed_features
    }
    keys.discard(None)
    candidate_count = len(proposals)
    if not keys:
        return {"network_found": False, COMMITMENT_KEY: _commitment(step_id, candidate_count, 0)}
    # The commit contract caps roads at ONE network (max_features=1 counted
    # in networks), and check_features_against_inputs() already refused a
    # commit whose features came from a set the document does not declare --
    # so more than one key here is a contract that stopped holding, not a
    # case to pick a winner from.
    if len(keys) != 1:
        raise ValueError(
            f"the roads commit carries features from {len(keys)} candidate networks; "
            f"the commit contract allows exactly one"
        )
    key = keys.pop()
    entry = proposals.get(key)
    if entry is None:
        raise ValueError(
            f"the committed road network '{key}' is not among this session's "
            f"{candidate_count} generated candidate set(s)"
        )
    return {
        **entry["result"]["narrative_data"],
        COMMITMENT_KEY: _commitment(step_id, candidate_count, 1),
    }


def _trees_block(result: dict, committed: list, step_id: str = "trees") -> dict:
    """
    RE-NARRATED, by tree_zone_candidates.build_narrative_data() itself, over
    the COMMITTED patches.

    THE OWNING MODULE NARRATES THE DECISION. Every argument below is a read:
    the committed patches (whose rehydration is complete -- every scored
    field survives the round trip), the run's own boundary polygon and the
    three availability flags off its `run_inputs`, and the thresholds off
    the block the same run already produced. Nothing is recomputed by this
    module and no tree-zone scoring runs again.

    WHY NOT FILTER: the block's zone entries carry rank and no id, so there
    is nothing to match a committed patch against. Re-narrating is also the
    better answer here -- candidate_count and the claimed acreage come out
    describing the committed set instead of the candidate list.
    """
    block = result["narrative_data"]
    run_inputs = result["run_inputs"]
    selection = block["selection"]
    rebuilt = tree_zone_candidates.build_narrative_data(
        committed,
        run_inputs["boundary_polygon_utm"],
        result["boundary_acres"],
        round(sum(float(p["area_acres"]) for p in committed), 2),
        result["search_space_acres"],
        soil_marginality_data_available=run_inputs["prime_farmland_data_available"],
        hydric_data_available=run_inputs["hydric_data_available"],
        stream_data_available=run_inputs["stream_data_available"],
        existing_canopy_excluded=selection["existing_canopy_excluded"],
        min_score=selection["min_suitability_score"],
        min_area_acres=selection["min_zone_acres"],
        dropped_invalid_count=block["dropped_invalid_count"],
    )
    return {
        **rebuilt,
        COMMITMENT_KEY: _commitment(step_id, block["candidate_count"], len(committed)),
    }


def _structures_block(result: dict, committed: list, step_id: str = "structures") -> dict:
    """
    RE-NARRATED, by solar_suitability.build_narrative_data() itself, over the
    COMMITTED sites.

    THIS IS THE STEP FILTERING CANNOT REACH. The block carries no candidate
    list -- only `selected_site`, which is select_optimal_structure_site()'s
    pick over whatever list the module was handed. Forward the run's block
    and the report describes the run's best site, which may be a site the
    user never committed and would look entirely normal.

    Every argument is a read: the committed sites (rehydrated into a
    SUPERSET of a scored candidate -- id, site_origin and the satisfied
    constraints on top), the run's own boundary polygon off `run_inputs`,
    and the run-level flags off `run_flags`, which the structures registry
    branch put there for exactly this kind of reuse. A site the user PLACED
    is narrated on the same footing as a generated one, because it was
    scored through the same measurement set.

    WITH ONE COMMITTED SITE THE REPORT NARRATES THAT SITE. With several it
    narrates the best of them -- the block's shape holds one, and widening
    it is a change to a KSOP module's return contract rather than to this
    wiring. THE MAP DRAWS ALL OF THEM; see layout_layers().
    """
    block = result["narrative_data"]
    run_inputs = result["run_inputs"]
    run_flags = result["run_flags"]
    gates = block["gates"]
    rebuilt = solar_suitability.build_narrative_data(
        committed,
        run_inputs["boundary_polygon_utm"],
        run_flags["road_proximity_source"],
        run_flags["tree_zone_exclusion_available"],
        gates["water_zone_excluded"],
        gates["existing_canopy_excluded"],
        drainage_gates_checked=tuple(run_flags["drainage_gates_checked"]),
    )
    return {
        **rebuilt,
        COMMITMENT_KEY: _commitment(step_id, block["candidate_count"], len(committed)),
    }


# --- the access point ------------------------------------------------


def _committed_access_point(document: dict, committed_features: list) -> Optional[tuple]:
    """
    The roads step's committed access point -- the one the committed network
    was routed from.

    NOT AN ARGUMENT AND NOT RE-VALIDATED. It is a user input the roads step
    already checked twice: at generate, through the Consumed/UserInput
    validator, and again at commit, through validate_commit_inputs(), which
    puts every declared access point back on the boundary before the
    features are looked at. Asking the caller for it again would invite a
    third, different answer.

    The document records EVERY access point tried (Accumulation.inputs_list)
    because a reopen has to rebuild every candidate set. Exactly one of them
    produced the committed network, and wire_translation.access_point_key()
    -- the same function the accumulation keys on -- is what says which.

    None for an EMPTY roads commit: no network, no access point, and the
    document's declared list is empty too (check_features_against_inputs()
    enforces that pairing).
    """
    entry = document["steps"].get("roads") or {}
    declared = (entry.get("inputs") or {}).get("access_points") or []
    keys = {
        (feature.get("properties") or {}).get("network_id")
        for feature in committed_features
    }
    keys.discard(None)
    if not keys:
        return None
    key = next(iter(keys))
    for value in declared:
        point = (float(value[0]), float(value[1]))
        if wire_translation.access_point_key(point) == key:
            return point
    return None


# --- the entry point -------------------------------------------------


def build_session_design(
    session_id: str,
    store,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> SessionDesign:
    """
    This session's committed design, assembled from the Design Document and
    the session cache.

    A PURE READ. No network, no KSOP recompute: the committed halves are
    rehydrations the commit already paid for (served out of
    SessionContext.step_committed under the document's own revision), the
    terrain halves are the warm-up's, and the narrative halves are the
    steps' own generate results. The one thing that is not a lookup is the
    narrative reduction, which removes declined members and recomputes the
    counts that describe what is left -- see the module docstring for the
    per-step route.

    Raises SessionWorkingDataExpiredError if any committed step's generate
    result has been evicted. NEVER returns a partial design.

    A step the document does not report as committed contributes nothing:
    no decision, no narrative block. Its report section falls back to that
    formatter's own no-data text and the map draws no layer for it. That is
    "not decided", which is a different thing from an EMPTY COMMIT -- and
    the two must read differently, because one of them is the user saying
    no.
    """
    context = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=cache
    )
    document = store.get(session_id)

    committed_steps = tuple(
        step_id
        for step_id in design_document.STEP_ORDER
        if _is_committed(document, step_id)
    )

    def committed_value(step_id):
        if step_id not in committed_steps:
            return []
        value = step_orchestrator.committed_internal_value(context, document, step_id)
        return list(value or [])

    production_areas = committed_value("landform")
    water_zones = committed_value("water")
    road_networks = committed_value("roads")
    tree_zone_patches = committed_value("trees")
    structure_sites = committed_value("structures")

    committed_features = {
        step_id: _committed_features(document, step_id) for step_id in committed_steps
    }

    narrative_data = {}
    if "landform" in committed_steps:
        narrative_data["production_area_ceiling"] = _landform_block(
            _run_result(context, document, session_id, "landform"), production_areas
        )
    if "water" in committed_steps:
        narrative_data["water_survey_areas"] = _water_block(
            _run_result(context, document, session_id, "water"), water_zones
        )
    if "roads" in committed_steps:
        narrative_data["road_corridors"] = _roads_block(
            _run_result(context, document, session_id, "roads"),
            committed_features.get("roads", []),
        )
    if "trees" in committed_steps:
        narrative_data["tree_zone_candidates"] = _trees_block(
            _run_result(context, document, session_id, "trees"), tree_zone_patches
        )
    if "structures" in committed_steps:
        narrative_data["solar_suitability"] = _structures_block(
            _run_result(context, document, session_id, "structures"), structure_sites
        )
    # The warm-up's own block -- no user decision is involved, so it is the
    # session's exclusion result verbatim, exactly as build_pipeline_context()
    # carries it.
    narrative_data["exclusion_zones"] = (context.exclusion_zones or {}).get("narrative_data")

    return SessionDesign(
        session_id=session_id,
        boundary=list(document["boundary"]),
        parcel_data=context.parcel_data,
        valleys=context.valleys,
        keypoints=context.keypoints,
        exclusion_zones=context.exclusion_zones,
        production_areas=production_areas,
        water_zones=water_zones,
        road_networks=road_networks,
        tree_zone_patches=tree_zone_patches,
        structure_sites=structure_sites,
        fence_features=committed_features.get("fencing", []),
        selected_water_zone=(
            wire_translation.water_zone_union(water_zones) if water_zones else None
        ),
        selected_road_corridor=(
            wire_translation.selected_road_network(road_networks) if road_networks else None
        ),
        selected_structure_site=(
            solar_suitability.select_optimal_structure_site(structure_sites)
            if structure_sites
            else None
        ),
        anchor_lon_lat=_committed_access_point(
            document, committed_features.get("roads", [])
        ),
        narrative_data=narrative_data,
        committed_steps=committed_steps,
        committed_features=committed_features,
    )


# --- the map -----------------------------------------------------------


def layout_layers(design: SessionDesign) -> dict:
    """
    The committed design in the retired layout map's fetch_layout_layers()
    return shape -- so that map's renderer (boundary, path, layers=...) drew
    THE USER'S DESIGN and nothing else changed about how it drew.

    NO KSOP CALL AND NO FETCH. fetch_layout_layers() runs
    build_pipeline_context() and then makes three more calls of its own
    (roads, solar, fencing); this makes none. The only computation here is
    contour_lines.compute_contour_lines() over the DEM already in hand,
    which is the same pure-numpy pass that function does on the batch path
    and reaches no network. (The basemap tiles the retired layout map fetched
    were unchanged -- that is imagery for the map, and the only network this
    whole path touches.)

    TWO NEW PLURAL KEYS, and they are the map half of two of the four shape
    divergences (see the module docstring):

      `water_zones`     every committed survey zone, each drawn with its own
                        ripple texture. `water_zone` stays beside it -- the
                        union -- so a consumer built before this branch
                        still reads one zone.
      `structure_sites` every committed site, each drawn with its own pin. A
                        design with two placed sites draws two.

    THE WIRE LAYERS ARE THE DOCUMENT'S OWN COMMITTED FEATURES. Roads,
    structures and fencing are drawn from GeoJSON, and the exact GeoJSON the
    user committed is in the document -- so those three are read straight
    out of it rather than rebuilt from a rehydration and re-serialised.
    Production, water and tree zones are drawn from UTM shapely geometry, so
    those come from the rehydrated committed values.

    A STEP THE USER NEVER COMMITTED DRAWS NOTHING. That is not a hole to
    fill: the map is the committed design, and a computed layer standing in
    for a decision nobody made is the same falsehood this whole module
    exists to remove.
    """
    from contour_lines import compute_contour_lines

    return {
        "dem": design.dem,
        "exclusion_zones": design.exclusion_zones,
        "production_areas": design.production_areas,
        # The union, for a reader that wants one zone; the list is what the
        # map actually iterates.
        "water_zone": design.selected_water_zone,
        "water_zones": design.water_zones,
        "road_corridor": design.committed_features.get("roads", []),
        "tree_zone_result": {"patches": design.tree_zone_patches},
        "structure_site": (
            design.committed_features.get("structures", [None])[0]
            if design.committed_features.get("structures")
            else None
        ),
        "structure_sites": design.committed_features.get("structures", []),
        "keypoints": design.keypoints,
        "water_features": design.parcel_data.water_features,
        "contour_lines": compute_contour_lines(design.dem),
        "fencing_result": {
            "fencing_geojson": {
                "type": "FeatureCollection",
                "features": list(design.fence_features),
            },
            "segment_count": len(design.fence_features),
        },
    }
