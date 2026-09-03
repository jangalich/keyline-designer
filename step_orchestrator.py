"""
step_orchestrator.py

THE GENERIC GENERATE PATH -- one function that can generate ANY step,
because it reads step_registry.py rather than knowing anything about the
steps (interactive-design-architecture-proposal.md sections 2.3 and 3.1).

    generate_step(session_id, step_id, store, params=None) -> Job

WHAT IT DOES, in order:

  1. Resolve the step's registry entry. An unregistered step fails here,
     before any work.
  2. Validate `params` against the entry's declared `user_inputs`.
  3. Submit a job (job_runner.py -- generate is asynchronous per section
     3.1). Everything below runs on the job's thread.
  4. Get the session context -- a cache hit, or a rebuild from the
     authoritative Design Document if the entry was evicted. Transparent
     either way; that is session_manager.get_session_context()'s contract.
  5. ASSEMBLE the entry's `consumes` set: each declaration resolved through
     the resolver for its source.
  6. Call the declared entry point with the consumed values in their
     declared OVERRIDE parameters.
  7. Cache the proposals on the context (SessionContext.step_proposals --
     the slot session_cache.py left for exactly this).
  8. Translate outbound: the step's declared payload builder turns the
     internal result into the wire shape.
  9. Set the step's document status to "generated" and persist.
 10. Return BOTH to the job: {"payload": ..., "document": ...}. The status
     in step 9 is a fact this process now holds and the client does not;
     making it fetch the document to learn it would be a round trip for
     something already in hand. See run_generate_job().

NOTHING IN THAT LIST NAMES A STEP. There is no `if step_id == "landform"`
anywhere in this file, and adding water must not add one -- it is one entry
in step_registry.py plus one payload builder. The two step-shaped things
here are build_landform_payload() (an outbound translator, which is
per-layer by nature -- see wire_translation.py's own "one function per
layer" rule) and nothing else.

IDEMPOTENT AND REPEATABLE (section 3.1). Regenerating recomputes ONLY this
step. Every upstream input comes from the session cache or from a commit, so
delineate_valleys(), fetch_parcel_data() and identify_exclusion_zones() do
not run again -- asserted at exact counts in test_step_orchestrator.py,
which is the same discipline test_pipeline_context.py and
test_session_manager.py apply. Two generates of one step produce equivalent
payloads and leave the document at the same revision.

PROPOSALS ARE NOT WRITTEN TO THE DOCUMENT. The document holds DECISIONS,
never derived bulk data (section 2.1); `status: "generated"` records only
that proposals exist elsewhere, and design_document.py's own docstring says
so. They live on the session cache, which is disposable precisely because
they are regenerable. A generate therefore writes exactly one thing to the
document -- a status -- and writes nothing at all if the status is already
right.

COMMIT AND REOPEN LIVE HERE TOO, and complete the per-step verb set:

    generate_step(session_id, step_id, store, params=None)         -> Job
    commit_step(session_id, step_id, features, provenance,
                base_revision, store, inputs=None)                 -> document
    reopen_step(session_id, step_id, store)                        -> document
    step_payload(session_id, step_id, store)                       -> payload
    discard_candidate(session_id, step_id, store, params)          -> document

ACCUMULATING STEPS (the roads entry's step_registry.Accumulation). Every
verb above still reads the registry rather than a step id, but a step that
declares `accumulate` takes a different path through generate, payload,
restore and discard: its proposals are a KEYED STORE of candidate sets, one
per distinct value of the declared user input, and a generate REPLACES
exactly the set for its own input while leaving every other set untouched.
The tried inputs are recorded on the document (design_document.record_
step_inputs) so a cold cache and a reopen rebuild every set, and the cap is
enforced against that record. See _ensure_accumulated_proposals().

step_payload() is the READ verb over what generate produced -- a resume or a
reload wants the proposals back without paying for them again, and every
other verb here writes something. It is the newest of the five and the only
one B6 (the HTTP surface) needed that was not already here; see its
docstring for why the rebuild-on-eviction path is the tier-2 contract rather
than a fallback.

They obey the same rule as generate: NOTHING BELOW NAMES A STEP. A commit
reads its gates off the entry's CommitContract, its post-commit hooks off
the entry's `post_commit`, and its cascade off the `consumes` edges; a
reopen restores editable state by re-running the entry's own generate. The
JUDGING half of a commit -- validity, containment, exclusion crossings --
is commit_validation.py's, so this module stays sequencing.

OUT OF SCOPE, DELIBERATELY: HTTP. This module stops at functions callable
from Python, the same place session_manager.py stops. session_api.py is the
transport over these five verbs and adds no behaviour of its own -- if a
route ever seems to need a rule that is not here, the rule belongs here.
"""

import math
import operator
from typing import Optional

import commit_validation
import design_document
import job_runner
import production_zone_payload
import session_cache
import session_manager
import step_registry
from design_document import mark_step_generated
# The two ENVELOPE layer names, from the module that mints them -- never
# re-typed here (build_water_payload()'s feature_id lookup filters on them,
# and "starts with survey_zone_" is true of the member layers too).
from wire_translation import LAYER_SURVEY_ZONES


class StepOrchestrationError(Exception):
    """A generate that cannot even be attempted -- a bad step id, bad params."""


# ======================================================================
# Assembling a step's `consumes` set
# ======================================================================
#
# THE EXTENSION POINT B5b USES. One resolver per step_registry source. The
# assembly below is a dispatch through this table -- it holds no branch on
# source, so wiring committed values in is REGISTERING A RESOLVER, not
# rewriting the assembler, which is the structural requirement section 3.2
# puts on this function ("production areas from the committed landform step
# (rehydrated), not from the optimizer").
#
# A resolver takes (definition, consumed, context, document) and returns the
# value. The consuming step's own definition is passed because a resolver may
# have to REFUSE and say which step it was refusing for -- see
# UpstreamNotCommittedError -- and a message naming only the upstream step
# leaves the reader to work out who was asking.


def _resolve_from_cache(definition, consumed, context, document):
    """
    A SessionContext attribute path. `dem` and `boundary_polygon_utm` are
    properties over ParcelData; `parcel_data.canopy_height` walks one level
    in. operator.attrgetter handles both, so the registry can name any of
    them without this function enumerating them.

    Then the edge's declared `combine`, when it has one -- see _combined().
    Water's `soil_inputs` reaches its override that way: the cache holds the
    three soil layers on ParcelData and the entry point takes one dict
    assembled from them.
    """
    try:
        value = operator.attrgetter(consumed.cache_path)(context)
    except AttributeError as exc:
        raise StepOrchestrationError(
            f"consumed '{consumed.name}' declares cache_path "
            f"'{consumed.cache_path}', which the session context does not "
            f"have: {exc}"
        ) from None
    return _combined(consumed, value)


def _combined(consumed, value):
    """
    The edge's declared `combine` applied to a resolved value, or the value
    unchanged when it declares none.

    ONE FUNCTION FOR BOTH SOURCES because it is one statement about the edge
    -- "the shape the override takes is this function of the shape the source
    holds" -- and it is true of a cache read and of a rehydrated commit alike.
    See step_registry.Consumed's ONE SOURCE, ANOTHER SHAPE note for what may
    and may not be declared here.

    APPLIED ON EVERY READ, INCLUDING A CACHE HIT, and that is deliberate: the
    committed cache holds the REHYDRATION (what the commit gate already paid
    for), not the combination, so a hit and a miss go through the same
    reduction and cannot answer differently. The reductions are a shapely
    union and a dict literal; caching either would be caching something
    cheaper to recompute than to key correctly.
    """
    if not consumed.combine:
        return value
    return step_registry.resolve(consumed.combine)(value)


class UpstreamNotCommittedError(StepOrchestrationError):
    """
    A generate that consumes an upstream step's COMMIT, asked for before that
    step was committed.

    REFUSED, NEVER SELF-COMPUTED, and this is the decision the whole
    committed-source path turns on. Every KSOP entry point in this pipeline
    treats a missing override as "compute it yourself" -- that is the trap
    pipeline_context.py documents at every forward it makes -- so generating
    the water step without the landform commit would not fail, it would
    quietly re-run the production optimiser and route water to zones the user
    never selected. The result would look right and be wrong, which is the
    worst available outcome.

    Refusing is also the answer the document already gives. A step's status
    says whether it is reachable; the frontend reads it to decide what is
    clickable, and a generate that arrives anyway is asking for something the
    document says does not exist yet. Carries the upstream step and its
    actual status so the caller can say which step to go back to.
    """

    def __init__(self, step_id: str, upstream_step: str, upstream_status: str, name: str):
        self.step_id = step_id
        self.upstream_step = upstream_step
        self.upstream_status = upstream_status
        self.consumed_name = name
        super().__init__(
            f"step '{step_id}' consumes '{name}' from the committed "
            f"'{upstream_step}' step, which has status "
            f"'{upstream_status}'. Commit '{upstream_step}' first: this step "
            f"is not computable without that decision, and computing one for "
            f"the user instead of asking for it is how a generated answer "
            f"gets mistaken for a chosen one."
        )


def committed_entry_or_refuse(definition, consumed, document):
    """
    The document entry behind one committed consumes edge, or
    UpstreamNotCommittedError.

    A DOCUMENT READ AND NOTHING ELSE -- no context, no rehydration, no
    network. Split out because the question "is the upstream commit there"
    has TWO askers now and they must not answer differently: the consumes
    resolver below asks it on its way to a value, and generate_step() asks it
    BEFORE it creates a job, so a client gets a 409 naming the upstream step
    instead of a failed job carrying this step's generic error. One
    implementation, so the pre-check cannot drift from the resolver it is
    pre-checking.
    """
    entry = document["steps"][consumed.from_step]
    if entry["status"] != design_document.STATUS_COMMITTED:
        raise UpstreamNotCommittedError(
            step_id=definition.step_id,
            upstream_step=consumed.from_step,
            upstream_status=entry["status"],
            name=consumed.name,
        )
    return entry


def check_upstream_commits(definition, document) -> None:
    """
    Every committed consumes edge this step declares, checked against the
    document in declaration order. Raises UpstreamNotCommittedError on the
    first one that is not committed; returns None when they all are.

    THE REGISTRY'S OWN EDGES, WALKED, not a list of upstream steps written
    down somewhere a route can read. There is exactly one place that says
    what water needs from landform and it is the water entry's `consumes`;
    anything else is a second opinion that can go stale the day an edge is
    added.
    """
    for consumed in definition.consumes:
        if consumed.source == step_registry.SOURCE_COMMITTED:
            committed_entry_or_refuse(definition, consumed, document)


def _resolve_from_committed(definition, consumed, context, document):
    """
    A committed upstream step's features, read off the document and put
    through the declared inbound rehydrator (proposal section 2.4) so a
    user-authored feature travels down the same override parameter a
    computer-authored one does.

    THREE CASES, AND ALL THREE ARE DIFFERENT:

      NOT COMMITTED -> UpstreamNotCommittedError. See that class.

      COMMITTED WITH FEATURES -> the rehydrated internal shape, served from
        SessionContext.step_committed when its cached revision matches the
        document's and rehydrated (and cached) when it does not. The revision
        key is the correctness guarantee; the cache is the speed.

      COMMITTED WITH ZERO FEATURES -> the declared `empty_commit` sentinel if
        the consumer has one, and otherwise the rehydrator's own empty answer.
        DECIDED BEFORE THE CACHE IS CONSULTED -- see committed_value().
        NEVER None, under any circumstance. An empty commit is a DECISION
        ("nothing goes here"), None is every consumer's "not supplied, go
        compute it", and collapsing the first into the second is what turns
        one user decision into five water-suitability runs producing a zone
        that was explicitly rejected. See step_registry.Consumed's EMPTY IS
        AN ANSWER note for the full argument and for how a step declares its
        sentinel.
    """
    committed_entry_or_refuse(definition, consumed, document)
    return committed_value(consumed, context, document)


def committed_value(consumed, context, document):
    """
    The rehydrated value behind one committed consumes edge. Split out of the
    resolver so the post-commit hooks can ask the same question of the same
    cache without going through consumes-edge dispatch.

    The caller has already established that `consumed.from_step` is
    committed.
    """
    step_id = consumed.from_step
    entry = document["steps"][step_id]
    revision = entry.get("revision", 0)
    features = entry["features"]
    provenance = entry.get("provenance", {})
    contract = step_registry.get_step(step_id).commit_contract

    if not features.get("features"):
        # THE SENTINEL, or the rehydrator's own explicit empty. Not cached:
        # it is a constant-time answer, and caching a sentinel object under a
        # revision key invites someone to mutate it.
        #
        # AHEAD OF THE CACHE READ, AND THAT ORDERING IS THE WHOLE OF WHETHER
        # THE SENTINEL WORKS. commit_step() writes the gate's rehydrated list
        # into step_committed under the new revision, and for a commit of
        # ZERO features that list is []. Consulting the cache first would
        # therefore serve [] for every read until the context happened to be
        # evicted -- the sentinel would fire only on a cold cache, which is
        # the worst possible shape for a value whose entire job is to stop
        # five consumers re-running the water pipeline. The comment above
        # already said this answer is not cached; this is where that stops
        # being true only by accident.
        if consumed.empty_commit:
            return step_registry.resolve(consumed.empty_commit)
        return _combined(
            consumed, step_registry.resolve(contract.rehydrate)(features, context.dem)
        )

    cached = context.step_committed.get(step_id)
    if cached is not None and cached["revision"] == revision:
        return _combined(consumed, cached["value"])

    value = _rehydrate_committed(features, provenance, contract, context.dem)
    context.step_committed[step_id] = {"revision": revision, "value": value}
    return _combined(consumed, value)


def _rehydrate_committed(features, provenance, contract, dem):
    """
    A stored committed FeatureCollection -> the internal shape, through the
    step's own declared translator.

    THE INTERNAL IDS ARE RECOMPUTED, NOT STORED. commit_validation.
    internal_ids_for() is deterministic in the committed collection and its
    provenance, and both are exactly what the document holds -- so this call
    and the one the commit path made produce the same ids without the
    document having to carry a derived field it would then have to keep
    correct.
    """
    kwargs = {}
    if contract.internal_id_parameter:
        kwargs[contract.internal_id_parameter] = commit_validation.internal_ids_for(
            features.get("features") or [], provenance
        )
    return step_registry.resolve(contract.rehydrate)(features, dem, **kwargs)


_CONSUMES_RESOLVERS = {
    step_registry.SOURCE_CACHE: _resolve_from_cache,
    step_registry.SOURCE_COMMITTED: _resolve_from_committed,
}


def assemble_consumes(definition, context, document) -> dict:
    """
    {consumed name -> value} for one step, in declaration order.

    KEYED BY THE CONSUMED NAME, not by the parameter it forwards into. The
    two differ (landform's `exclusion_zones` forwards as `exclusion_result`)
    and the consumed name is the one that means something outside the call:
    it is the edge label the cascade reports and the key the payload builder
    reads. forwarded_arguments() below does the rename, once, at the call.
    """
    assembled = {}
    for consumed in definition.consumes:
        resolver = _CONSUMES_RESOLVERS.get(consumed.source)
        if resolver is None:
            raise StepOrchestrationError(
                f"consumed '{consumed.name}' has source {consumed.source!r}, "
                f"which no resolver handles"
            )
        assembled[consumed.name] = resolver(definition, consumed, context, document)
    return assembled


def forwarded_arguments(definition, assembled: dict, params: dict) -> dict:
    """
    The keyword arguments the entry point is actually called with: every
    consumed value that declares a `forward_as`, under that name, plus the
    step's user inputs under their own.

    A consumed value with forward_as=None is assembled and NOT passed -- see
    step_registry.Consumed's note on why that is a real case and not a gap.
    """
    arguments = {
        consumed.forward_as: assembled[consumed.name]
        for consumed in definition.consumes
        if consumed.forward_as
    }
    # USER INPUTS UNDER THEIR OWN forward_as. The client sends `access_point`
    # and the entry point takes `anchor_lon_lat`; the UserInput declaration
    # carries the rename, exactly as Consumed.forward_as does for the cache
    # edges. `params` is validate_params()'s output, keyed by input NAME.
    for user_input in definition.user_inputs:
        if user_input.name in params:
            arguments[user_input.parameter] = params[user_input.name]
    return arguments


def validate_params(definition, params: Optional[dict]) -> dict:
    """
    `params` against the step's declared `user_inputs`. Fail-loud, the
    posture design_document.py and parcel_data.py take: an unknown parameter
    is rejected rather than dropped, because a client sending `acess_point`
    must be told, not quietly given a default-routed answer.

    Landform declares no user inputs, so ANY params supplied to it is an
    error -- which is a meaningful assertion rather than a vacuous one: it
    is what stops a caller "passing through" a value the step will never
    read and believing it took effect.
    """
    params = dict(params or {})
    declared = definition.user_input_names()
    unknown = sorted(set(params) - set(declared))
    if unknown:
        raise StepOrchestrationError(
            f"step '{definition.step_id}' accepts user inputs "
            f"{declared or '()'}; got unknown {unknown}"
        )
    missing = sorted(set(declared) - set(params))
    if missing:
        raise StepOrchestrationError(
            f"step '{definition.step_id}' requires user input(s) {missing}"
        )
    # SHAPE-CHECKED AND NORMALISED, per the declared UserInput.shape. What
    # comes back is what the entry point is called with -- a JSON array has
    # become a tuple of floats -- so a client sending [lat, lon] as a string,
    # or a three-element array, is told here at 400 rather than at a failed
    # job deep inside a routing pass.
    return {
        user_input.name: _INPUT_SHAPE_CHECKS[user_input.shape](
            params[user_input.name],
            f"step '{definition.step_id}' user input '{user_input.name}'",
        )
        for user_input in definition.user_inputs
    }


def _check_lon_lat(value, where: str) -> tuple:
    """A [lon, lat] pair of finite numbers in range -> (lon, lat) floats."""
    ok = (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(
            isinstance(part, (int, float)) and not isinstance(part, bool)
            for part in value
        )
    )
    if ok:
        lon, lat = float(value[0]), float(value[1])
        if math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            return (lon, lat)
    raise StepOrchestrationError(
        f"{where} must be a [lon, lat] pair of numbers (lon in [-180, 180], "
        f"lat in [-90, 90]); got {value!r}"
    )


# ONE CHECK PER step_registry.VALID_INPUT_SHAPES ENTRY. The registry names
# the shape; this is where it is enforced. A shape added there without a
# check here fails validate_params() with a KeyError rather than passing
# unchecked, which is the right failure.
_INPUT_SHAPE_CHECKS = {
    step_registry.INPUT_SHAPE_LON_LAT: _check_lon_lat,
}


def validate_inputs_on_boundary(definition, params: dict, boundary) -> None:
    """
    Every declared UserInput with a `validate` target, run against the
    session's own boundary. A ValueError from the validator is the
    rejection, re-raised as a StepOrchestrationError so a transport maps it
    to 400 like every other bad-params case.

    THE VALIDATOR IS THE PIPELINE'S OWN. road_corridors.validate_access_
    point_on_boundary() is the one implementation of "a real access point
    is where the parcel meets a road along its perimeter", and it needs no
    DEM -- it derives a lightweight UTM CRS from the boundary -- which is
    what lets this run synchronously, before a job exists.
    """
    for user_input in definition.user_inputs:
        if not user_input.validate or user_input.name not in params:
            continue
        try:
            step_registry.resolve(user_input.validate)(boundary, params[user_input.name])
        except ValueError as exc:
            raise StepOrchestrationError(
                f"step '{definition.step_id}' user input '{user_input.name}' "
                f"was rejected: {exc}"
            ) from exc


# ======================================================================
# Accumulating steps
# ======================================================================
#
# THE ONE PLACE THE REPLACE/ACCUMULATE DIFFERENCE LIVES. Everything below
# reads step_registry.Accumulation and nothing below names a step.


class CandidateCapReachedError(StepOrchestrationError):
    """
    A generate for an accumulating step whose document already records the
    cap of candidate sets, for an input that is not one of them.

    ENFORCED AGAINST THE DOCUMENT, not the cache: the tried inputs are
    recorded there precisely so the cap holds in every process and across
    an eviction, and so the frontend's own limit is a convenience rather
    than the rule. A regenerate for an input ALREADY recorded is not
    refused -- it replaces that set and holds no new slot.
    """

    def __init__(self, step_id: str, max_candidates: int, candidates: list):
        self.step_id = step_id
        self.max_candidates = max_candidates
        self.candidates = candidates
        super().__init__(
            f"step '{step_id}' already holds its maximum of {max_candidates} "
            f"candidate set(s) ({candidates}); discard one before generating "
            f"for a new input."
        )


class CandidateNotFoundError(StepOrchestrationError):
    """A discard naming an input the step's document does not record."""

    def __init__(self, step_id: str, candidate):
        self.step_id = step_id
        self.candidate = candidate
        super().__init__(
            f"step '{step_id}' holds no candidate set for {candidate!r}; "
            f"nothing to discard."
        )


class CommitInputError(StepOrchestrationError):
    """
    A commit to a step that declares a required user input, whose body does
    not carry it.

    REFUSED, NEVER READ AS EMPTY. buildCommitBody on the client sends
    `inputs` only when non-empty, so a lost input leaves the key OFF the
    body rather than erroring -- and a server that accepted a roads commit
    with no access points would be recording a decision the user never made
    about where the road starts. That is the same silent-empty-commit class
    that has already produced three separate bugs, and this is the server
    end of the fix; the client end is a separate branch.
    """


def accumulation_key(definition, params: dict) -> str:
    """The candidate-set key of validated `params`, through the declared
    Accumulation.key function."""
    accumulation = definition.accumulate
    return step_registry.resolve(accumulation.key)(params[accumulation.keyed_by])


def recorded_candidate_inputs(definition, entry: dict) -> list:
    """
    The values of the accumulating input this step's document entry records,
    normalised through the input's shape check, in the order they were tried.
    [] for an entry recording none.
    """
    accumulation = definition.accumulate
    user_input = definition.user_input(accumulation.keyed_by)
    raw = (entry.get("inputs") or {}).get(accumulation.inputs_list) or []
    check = _INPUT_SHAPE_CHECKS[user_input.shape]
    return [
        check(value, f"step '{definition.step_id}' recorded input '{accumulation.keyed_by}'")
        for value in raw
    ]


def _inputs_document(definition, values: list) -> dict:
    """The document `inputs` for an accumulating step: the list key holding
    every tried value, JSON-native (lists of floats)."""
    return {definition.accumulate.inputs_list: [list(value) for value in values]}


def check_candidate_cap(definition, document: dict, key: str) -> None:
    """Refuse a generate that would exceed Accumulation.max_candidates. A
    key already recorded holds no new slot and passes."""
    accumulation = definition.accumulate
    recorded = recorded_candidate_inputs(definition, document["steps"][definition.step_id])
    key_of = step_registry.resolve(accumulation.key)
    if key in {key_of(value) for value in recorded}:
        return
    if len(recorded) >= accumulation.max_candidates:
        raise CandidateCapReachedError(
            definition.step_id,
            accumulation.max_candidates,
            [list(value) for value in recorded],
        )


def _run_entry_point(definition, assembled: dict, params: dict):
    return definition.resolve_generate()(**forwarded_arguments(definition, assembled, params))


def _ensure_accumulated_proposals(definition, context, document, assembled) -> dict:
    """
    The keyed candidate-set store for an accumulating step, brought into
    agreement with the DOCUMENT's recorded inputs: every recorded input has
    a result (regenerated if the cache lost it), and nothing the document
    does not record survives (a discard in another process). Returns the
    store, which is SessionContext.step_proposals[step_id].

    HOW ACCUMULATED PROPOSALS ARE CACHED, AND WHY: several results keyed by
    the candidate set's key, in the document's order -- not one merged
    collection. Three requirements decided it. Regenerating for A must not
    disturb B, and a keyed store makes that structural rather than careful:
    the write for A touches A's key. A discard frees one slot, which is one
    pop. And a cold cache must rebuild every candidate the user tried, which
    is a walk over the document's list regenerating whatever is missing --
    the same walk this function is. The merge into one collection happens
    at payload time (build_roads_payload), where the one-collection wire
    contract lives, and nowhere earlier.

    ORDER-INDEPENDENT BY CONSTRUCTION: each set is computed from the same
    `assembled` inputs plus its own user input, never from another set's
    result, so the store's contents do not depend on the order the sets
    were generated in. test_roads_step.py section 5 asserts that rather
    than trusting this sentence.
    """
    accumulation = definition.accumulate
    key_of = step_registry.resolve(accumulation.key)
    recorded = recorded_candidate_inputs(definition, document["steps"][definition.step_id])

    existing = context.step_proposals.get(definition.step_id)
    if not isinstance(existing, dict) or any(
        not isinstance(entry, dict) or "result" not in entry for entry in existing.values()
    ):
        existing = {}

    rebuilt = {}
    for value in recorded:
        key = key_of(value)
        if key in existing:
            rebuilt[key] = existing[key]
            continue
        params = {accumulation.keyed_by: value}
        rebuilt[key] = {"inputs": params, "result": _run_entry_point(definition, assembled, params)}
    context.step_proposals[definition.step_id] = rebuilt
    return rebuilt


# ======================================================================
# Failure -> the client-facing error payload
# ======================================================================


def error_payload(definition, exc: BaseException) -> dict:
    """
    One exception -> the error shape the job carries, read off the step's own
    `failure_layers` declaration.

    THE SHAPE IS /api/production-zones', EXACTLY:

        {"error": prose, "failed_layer": {"type": ..., "label": ...}}

    and, for a failure that is not one named layer going down, that
    endpoint's other branch: an error string and NO failed_layer. The panel
    branches on `failed_layer.type` and prints `failed_layer.label` in prose,
    and renders "The data sources did not respond." when there is no layer to
    name -- so both halves of this are a working frontend's contract, not a
    convention. Never a traceback: the exception stays on the Job for
    server-side logging (job_runner.py).
    """
    for failure in definition.failure_layers:
        try:
            exception_class = step_registry.resolve(failure.exception)
        except step_registry.RegistryError:
            # A declared exception whose module is unimportable must not
            # swallow the real failure it was there to classify.
            continue
        if not isinstance(exc, exception_class):
            continue
        if failure.self_describing:
            layer = getattr(exc, "layer", None)
            label = getattr(exc, "label", None)
        else:
            layer, label = failure.layer, failure.label
        if not layer:
            break
        return {
            "error": f"The {label} could not be retrieved.",
            "failed_layer": {"type": layer, "label": label},
        }
    return {"error": definition.generic_error}


# ======================================================================
# generate
# ======================================================================


def generate_step(
    session_id: str,
    step_id: str,
    store,
    params: Optional[dict] = None,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
    runner: Optional[job_runner.JobRunner] = None,
) -> job_runner.Job:
    """
    Generate one step's proposals. Returns the JOB immediately (section 3.1:
    generate is asynchronous, 202 + polling recommended first); the payload
    AND the updated document arrive as that job's `result` -- see
    run_generate_job() for the shape and for why both.

    The two failure classes are deliberately different:

      * A step that cannot be generated AT ALL -- unregistered, params that
        do not match its declared user_inputs, or an UPSTREAM COMMIT THAT IS
        NOT THERE -- raises RIGHT HERE, from this call, before a job exists.
        There is nothing to poll for; the request was wrong, and an HTTP
        layer turns this into a 400/404/409.
      * Anything that goes wrong DURING the generate becomes the job's
        `failed` state with the error payload above. The caller already holds
        a job id and finds out by asking.

    WHY THE UPSTREAM CHECK MOVED IN FRONT OF THE JOB. assemble_consumes()
    raises UpstreamNotCommittedError, and it runs on the job's thread -- so a
    water generate asked for before landform was committed used to become a
    FAILED JOB carrying the water step's generic "Water survey areas could
    not be generated." The client would be told the parcel's data failed,
    when in fact the request was answerable and the answer was "go commit
    landform first" -- the one thing UpstreamNotCommittedError carries and
    the generic error throws away. Nothing hit it while landform was the only
    entry, because landform consumes no commit; water is the first step that
    does.

    So the committed edges are resolved SYNCHRONOUSLY, here, through the
    registry's own walk (check_upstream_commits) rather than a second opinion
    assembled in a route. It costs one document read and no network -- an
    upstream step's status is a field on a document this process is about to
    load anyway -- and a 409 before a job id is issued is the honest answer:
    no work was accepted, so none should be reported as accepted.

    TWO THINGS IT DELIBERATELY DOES NOT DO. It does not read the document at
    all for a step with no committed edges (landform), because there would be
    nothing to ask of it. And it lets the store's own failures -- an unknown
    session, an unreadable file -- fall through to the job exactly as before,
    rather than converting them here: those are not statements about upstream
    steps, test_step_orchestrator.py asserts that an unknown session_id fails
    INSIDE the job, and the HTTP layer already asks store.get() for itself so
    a stale bookmark still gets its 404 (session_api.generate_step_endpoint).

    A caller that wants the result synchronously calls
    `generate_step(...).wait()` and reads `.result["payload"]`. That is what
    the tests do; a transport must not (see job_runner.py).
    """
    definition = step_registry.get_step(step_id)
    validated_params = validate_params(definition, params)

    # THREE SYNCHRONOUS CHECKS NEED THE DOCUMENT: the upstream commits, a
    # user input's validator (the access point against the boundary), and
    # an accumulating step's cap. A step with none of them (landform) reads
    # no document here, exactly as before.
    if definition.upstream_steps() or definition.user_inputs or definition.accumulate:
        try:
            document = store.get(session_id)
        except Exception:
            # See the docstring's TWO THINGS: the store's failures stay the
            # job's. Swallowed only for the purpose of skipping a check that
            # has no document to run against -- the same store call is made
            # again on the job's thread and raises there.
            document = None
        if document is not None:
            check_upstream_commits(definition, document)
            validate_inputs_on_boundary(definition, validated_params, document["boundary"])
            if definition.accumulate:
                check_candidate_cap(
                    definition, document, accumulation_key(definition, validated_params)
                )

    # `is None`, never `or`: JobRunner defines __len__, so a caller-supplied
    # runner holding no jobs is FALSY and `or` would silently swap in the
    # process-wide default on exactly the first submission of a fresh
    # runner's life -- the same trap session_cache.py documents for its two
    # cache classes. The caller would then be handed a Job the runner it
    # passed has never heard of, and every get_job() against it would raise
    # JobNotFoundError.
    if runner is None:
        runner = job_runner.DEFAULT_JOB_RUNNER

    def work():
        return run_generate_job(
            session_id,
            definition,
            store,
            validated_params,
            fetch_cache=fetch_cache,
            cache=cache,
        )

    return runner.submit(work, on_error=lambda exc: error_payload(definition, exc))


def run_generate(
    session_id: str,
    definition,
    store,
    params: dict,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    The generate itself, synchronously -- THE PAYLOAD.

    Separate and public so the compute path can be exercised, profiled and
    reasoned about without a thread in the way; generate_step() is the
    supported entry point and this is what it does.

    The payload-only view of _generate(), for the two callers that want
    exactly that and nothing else: step_payload() on a cache miss, whose
    whole contract is to return the payload GET .../layers serves, and
    restore_step_state(), which reads proposal ids out of it. Neither is
    handing a document to a client, so neither should be made to unwrap one.
    """
    return _generate(
        session_id, definition, store, params,
        fetch_cache=fetch_cache, cache=cache,
    )[0]


def run_generate_job(
    session_id: str,
    definition,
    store,
    params: dict,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    THE GENERATE JOB'S `done` RESULT: {"payload": ..., "document": ...}.

    WHY THE DOCUMENT RIDES ALONG. A generate does two things -- it produces
    proposals, and it moves the step's status to "generated". Returning only
    the first told the client that a transition had happened without telling
    it what the transition was, so the only honest way to learn the new
    status was a GET /api/sessions/<id> immediately after: a round trip for a
    fact this process had in hand a millisecond earlier, and had already
    persisted.

    That round trip is worse than its cost. The frontend's rule is that its
    mirror of the document is only ever written by hydrating a server
    response, and a required-but-uninteresting fetch after every generate is
    exactly the pressure that makes patching `status: "generated"` in
    client-side look reasonable. It is not: the client would then be
    asserting a transition rather than observing one, and a generate the
    server rejected or coalesced would leave the mirror lying. Sending the
    document removes the temptation by removing the round trip.

    TWO KEYS, NEITHER NESTED IN THE OTHER. `payload` is the step's wire
    payload, byte-for-byte what GET .../layers serves; `document` is the
    Design Document, byte-for-byte what GET /api/sessions/<id> serves. They
    answer different questions and are consumed by different parts of the
    client -- the layers and the wizard's state -- so folding one inside the
    other, or replacing the payload with a document carrying it, would make
    every reader of either learn the shape of the other.

    THROUGH design_document.document_body(), the same call the session routes
    make. A document that reached a client without `step_order` because it
    came out of a job rather than a route would be the drift that function
    exists to prevent, and the client reading order off `steps` instead would
    get six real step ids alphabetically -- a plausible wrong answer that
    raises nowhere.

    A FAILED GENERATE CARRIES NO DOCUMENT and is not touched here: it never
    reaches this return. The job's error payload is error_payload()'s, and
    the reason there is no document in it is not economy -- it is that the
    step's status DID NOT CHANGE. Attaching one would be sending a document
    to say nothing happened, and a client hydrating on it would rewrite its
    mirror on the strength of a failure.
    """
    payload, document = _generate(
        session_id, definition, store, params,
        fetch_cache=fetch_cache, cache=cache,
    )
    return {"payload": payload, "document": design_document.document_body(document)}


def _generate(
    session_id: str,
    definition,
    store,
    params: dict,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> tuple:
    """
    The generate, once: (payload, document).

    ONE COMPUTE PATH WITH TWO VIEWS OVER IT, rather than a second function
    that re-reads the document the first one just wrote. The document
    returned is the one this call put (or the untouched one it did not need
    to -- see mark_step_generated), so it is the state at the end of THIS
    generate by construction. Re-reading the store instead would be a second
    read that answers a slightly different question -- "what does the store
    hold now", which another writer can have changed -- to arrive at the same
    answer more slowly and less certainly.
    """
    # The document FIRST. It is the authority (section 2.1), an unknown
    # session_id raises from the store, and step 5's committed values will
    # read from it.
    document = store.get(session_id)

    # A cache hit, or a rebuild from that document. Either way the terrain
    # warm-up's products are in hand and NOTHING upstream recomputes on a
    # hit -- which is what makes regenerating cheap and what
    # test_step_orchestrator.py asserts at exact call counts.
    context = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=cache
    )

    validate_inputs_on_boundary(definition, params, document["boundary"])
    assembled = assemble_consumes(definition, context, document)

    if definition.accumulate:
        return _generate_accumulated(definition, store, context, document, assembled, params)

    result = _run_entry_point(definition, assembled, params)

    # The proposals, cached where session_cache.py reserved room for them:
    # heavy, native, and regenerable from the document, so they belong in
    # tier 2 and not in the document. Overwritten on a regenerate rather than
    # appended -- there is one current proposal per step, and a superseded
    # one is not a version to keep.
    context.step_proposals[definition.step_id] = result

    payload = definition.resolve_payload()(result, assembled)

    # The ONLY document write a generate makes. mark_step_generated() is a
    # no-op on a step already generated, so a regenerate does not bump
    # document_revision -- see its docstring for why that matters here.
    #
    # `updated` is returned either way, and on the no-op branch it IS
    # `document`: a regenerate legitimately reports the unchanged document,
    # because the unchanged document is the truth about a step that was
    # already generated. A client hydrating it sees the same revision and
    # the same status, which is what happened.
    updated = mark_step_generated(document, definition.step_id)
    if updated is not document:
        store.put(updated)

    return payload, updated


def _generate_accumulated(definition, store, context, document, assembled, params) -> tuple:
    """
    The accumulate branch of _generate(): (payload, document).

    ONE SET REPLACED, THE REST KEPT. The store is first brought into
    agreement with the document (every recorded input has a result), then
    the set for THIS input is computed and written under its key --
    replacing a previous result for the same input, appending for a new one
    -- and the payload is built over the whole store.

    TWO DOCUMENT WRITES, COLLAPSED INTO ONE PUT. The status (mark_step_
    generated, a no-op after the first) and the recorded inputs (record_
    step_inputs, a no-op for an input already recorded). A regenerate for a
    recorded input therefore bumps nothing, which is the same repeatability
    contract the replace path keeps.
    """
    accumulation = definition.accumulate
    key = accumulation_key(definition, params)
    check_candidate_cap(definition, document, key)

    proposals = _ensure_accumulated_proposals(definition, context, document, assembled)
    proposals[key] = {"inputs": params, "result": _run_entry_point(definition, assembled, params)}

    payload = definition.resolve_payload()(proposals, assembled)

    updated = mark_step_generated(document, definition.step_id)
    recorded = recorded_candidate_inputs(definition, updated["steps"][definition.step_id])
    key_of = step_registry.resolve(accumulation.key)
    if key not in {key_of(value) for value in recorded}:
        recorded.append(params[accumulation.keyed_by])
    updated = design_document.record_step_inputs(
        updated, definition.step_id, _inputs_document(definition, recorded)
    )
    if updated is not document:
        store.put(updated)
    return payload, updated


def accumulated_payload(definition, context, document) -> dict:
    """
    An accumulating step's payload over EVERY recorded candidate set --
    the read verb's and the restore's shared path. Regenerates whatever the
    cache lost, writes nothing to the document.
    """
    assembled = assemble_consumes(definition, context, document)
    proposals = _ensure_accumulated_proposals(definition, context, document, assembled)
    return definition.resolve_payload()(proposals, assembled)


def discard_candidate(
    session_id: str,
    step_id: str,
    store,
    params: Optional[dict] = None,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    Discard ONE candidate set of an accumulating step, freeing its slot.
    Returns the NEW Design Document.

    `params` names the set exactly as the generate that made it did -- the
    same user input, validated the same way -- so a client discards what it
    generated with the value it generated it from. The document's recorded
    list loses the value (design_document.record_step_inputs) and the cache
    loses the result; a subsequent generate for the same input starts a
    fresh set in a fresh slot.

    SYNCHRONOUS AND CHEAP: one document write and one dict pop. The cache is
    consulted without rebuilding it -- a discard on an evicted session has
    nothing to pop, and rebuilding a context to remove one key from it would
    be the opposite of cheap.

    Refuses (StepOrchestrationError -> 400) a step that does not accumulate:
    its generate replaces, so there is no slot to free. Refuses
    (DocumentError -> 409) a step that is not generated: a committed step's
    inputs are the commit's own, and a not_started step records none.
    Refuses (CandidateNotFoundError -> 404) an input the step does not hold.
    """
    definition = step_registry.get_step(step_id)
    if not definition.accumulate:
        raise StepOrchestrationError(
            f"step '{step_id}' does not accumulate candidate sets; its "
            f"generate replaces its proposals, so there is nothing to discard"
        )
    validated = validate_params(definition, params)
    key = accumulation_key(definition, validated)
    accumulation = definition.accumulate
    key_of = step_registry.resolve(accumulation.key)

    document = store.get(session_id)
    entry = document["steps"][step_id]
    if entry["status"] != design_document.STATUS_GENERATED:
        raise design_document.DocumentError(
            f"cannot discard a candidate from step '{step_id}' with status "
            f"'{entry['status']}'; only a generated step holds candidate sets"
        )
    recorded = recorded_candidate_inputs(definition, entry)
    remaining = [value for value in recorded if key_of(value) != key]
    if len(remaining) == len(recorded):
        raise CandidateNotFoundError(step_id, list(validated[accumulation.keyed_by]))

    updated = design_document.record_step_inputs(
        document, step_id, _inputs_document(definition, remaining)
    )
    store.put(updated)

    if cache is None:
        cache = session_cache.DEFAULT_SESSION_CACHE
    context = cache.get(session_id)
    if context is not None:
        proposals = context.step_proposals.get(step_id)
        if isinstance(proposals, dict):
            proposals.pop(key, None)
        context.step_restored.pop(step_id, None)
    return updated


# ======================================================================
# Reading a generated step back
# ======================================================================


class StepNotGeneratedError(StepOrchestrationError):
    """
    A step's payload was asked for while the step has no proposals.

    SAID EXPLICITLY RATHER THAN ANSWERED WITH AN EMPTY PAYLOAD, and the
    distinction is the whole reason this class exists. A not_started step and
    a step whose generate produced nothing are different answers -- the first
    means "ask for a generate", the second means "this parcel has no
    production ground" -- and a reader handed `{"suggested_zones":
    {"features": []}}` for both cannot tell them apart. That is the same
    null-versus-absent line design_document.py draws around an EMPTY COMMIT.

    A COMMITTED step lands here too, carrying status "committed", and that is
    correct rather than an oversight. Its proposals are no longer the current
    state of the step: what it holds is a decision, and the document already
    carries that decision in full. Getting the candidate set back is what
    reopen_step() is for, and it has a downstream cascade attached -- so
    handing the proposals over from a read verb would be offering the
    editable state without the reset that makes editing it safe.

    Carries the step's ACTUAL status so a caller can say which of those two
    things happened.
    """

    def __init__(self, step_id: str, status: str):
        self.step_id = step_id
        self.status = status
        super().__init__(
            f"step '{step_id}' has status '{status}'; its layers exist only "
            f"while it is '{design_document.STATUS_GENERATED}'. Generate it "
            f"first"
            + (
                ", or reopen it to edit the committed decision"
                if status == design_document.STATUS_COMMITTED
                else ""
            )
            + "."
        )


def step_payload(
    session_id: str,
    step_id: str,
    store,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    The wire payload of an already-generated step -- the READ verb over what
    generate produced, so a resume or a page reload does not have to
    regenerate.

    THE SAME PAYLOAD, BY CONSTRUCTION, NOT BY A SECOND COPY OF IT. The heavy
    half of a generate is the entry point's compute pass, and its result is
    already on the context (SessionContext.step_proposals, written by
    run_generate). What is NOT cached is the wire shape, and rebuilding that
    is the registry's own payload builder over that same result -- microseconds
    over geometry in hand. So this returns what the job returned because it is
    assembled from the identical object, not because a copy was filed away
    somewhere that could drift from it.

    ON A CACHE MISS IT REGENERATES, transparently, and that is the tier-2
    contract rather than a fallback bolted on here: SessionContext is
    non-authoritative and evictable by design, and generate is idempotent and
    network-free, so a rebuilt context re-running the compute pass is slower
    and otherwise indistinguishable -- the same promise
    session_manager.get_session_context() makes one level down. It uses the
    step's OWN committed user inputs, for restore_step_state()'s reason:
    regenerating from different inputs would return a candidate set the user
    never saw.

    Raises StepNotGeneratedError when the step has no current proposals --
    never an empty payload. See that class.
    """
    definition = step_registry.get_step(step_id)
    document = store.get(session_id)
    entry = document["steps"][step_id]
    if entry["status"] != design_document.STATUS_GENERATED:
        raise StepNotGeneratedError(step_id, entry["status"])

    context = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=cache
    )
    # AN ACCUMULATING STEP'S PAYLOAD IS EVERY RECORDED CANDIDATE SET, hit or
    # miss: the store is reconciled with the document's list and whatever
    # the cache lost is regenerated. A partial miss (three recorded, one
    # evicted) is the same walk.
    if definition.accumulate:
        return accumulated_payload(definition, context, document)

    result = context.step_proposals.get(step_id)
    if result is None:
        return run_generate(
            session_id,
            definition,
            store,
            validate_params(definition, entry.get("inputs")),
            fetch_cache=fetch_cache,
            cache=cache,
        )

    # assemble_consumes() again rather than a cached `assembled`: it is
    # attribute reads off the context plus, for a committed source, a
    # rehydration the commit already cached. Caching it here would be a
    # second copy of values the context is already the home of.
    assembled = assemble_consumes(definition, context, document)
    return definition.resolve_payload()(result, assembled)


# ======================================================================
# Outbound translation: the landform payload
# ======================================================================


def build_landform_payload(result: dict, assembled: dict) -> dict:
    """
    The landform step's wire payload -- and it is /api/production-zones'
    response, not a new shape that resembles it.

    THERE IS A WORKING FRONTEND CONSUMING THIS TODAY. App.jsx,
    ProductionZonePanel.jsx and ProductionZoneLayers.jsx read
    `data.zones`, `data.suggested_zones.features`, `data.eligible_union`,
    `data.exclusion_layers`, `data.summary` and `data.scales`, and the
    session path exists to slot in underneath them unchanged. So this
    function does not assemble a payload; it calls the endpoint's OWN
    assembler -- production_zone_payload.assemble_production_zone_payload()
    -- with the two results that endpoint fetches for itself. One
    implementation, two ways in.

    THE TWO REPRESENTATIONS ARE BOTH KEPT, and must stay both. `zones` is
    tabular (rank, score, slope range, aspect) for the panel's list;
    `suggested_zones.features` is GeoJSON with properties.area_acres for the
    map. They are the same zones seen by two consumers with different needs,
    not a duplication to collapse -- collapsing them would push either
    geometry into a table or table formatting into feature properties.

    WHAT THIS FUNCTION SUPPLIES THAT THE ENDPOINT FETCHES. The exclusion
    result. The endpoint calls identify_exclusion_zones() itself; here it is
    already in hand -- the terrain warm-up computed it at session creation
    and the registry declared it as a consumed value, forwarded into
    identify_optimized_production_areas() as `exclusion_result`. That single
    forward is what keeps this whole path network-free, and reading the
    payload's exclusion half off the same object is what keeps the two
    halves consistent: the eligible union the map highlights is the exact
    union the production gates ran against.

    `assembled` is the orchestrator's consumes dict, keyed by the registry's
    own consumed names. Read rather than re-derived, so a payload builder can
    never disagree with what the generate was actually computed from.
    """
    return production_zone_payload.assemble_production_zone_payload(
        assembled["exclusion_zones"], result
    )


def build_water_payload(result: dict, assembled: dict) -> dict:
    """
    The water step's wire payload: the survey zones the user selects from,
    plus the step-level block the panel reads.

    TWO SOURCES, WHICH IS production_zone_payload.assemble_production_zone_
    payload()'S OWN SPLIT AND NOT A NEW ONE. That assembler -- the established
    precedent for this shape -- takes its per-feature values off the feature
    properties the pipeline already built, and its step-level values
    (`scales`, the parcel aggregates in `summary`) off build_narrative_data().
    Water's two halves are the same two:

      PER-FEATURE -> water_survey_areas._zone_feature_properties(), described
        by its own docstring as the full measurement contract every feature
        carries, and already on every feature of the result's zones_geojson.
      STEP-LEVEL -> water_survey_areas.build_narrative_data(), already
        computed by the entry point and carried on the result -- including
        the per-zone `panel` rows and the step's `scales` block, both built
        there beside the values they read (see build_zone_panel() /
        build_scales()). NEITHER IS ASSEMBLED HERE. A panel row list built
        in a payload function would be a second editorial decision about the
        same measurements, in a file that cannot see them.

    WHERE IT DIFFERS FROM THE PRODUCTION ASSEMBLER, and why the difference is
    the pipeline's rather than this function's: production's map geometry is
    the render-fill OPENING of its wire geometry, so that assembler has to
    swap the geometry of every feature and drop the zones whose opening came
    back empty. A survey zone's envelope and its render fill are ONE OBJECT
    (water_survey_areas.build_survey_zones()), so there is nothing to swap
    and nothing that can vanish. This function therefore carries the
    collection through unchanged rather than rebuilding it -- the difference
    is that water has no second geometry, not that water is being assembled
    more loosely.

    NOTHING IS RECOMPUTED, COERCED OR DEFAULTED HERE, and for this step that
    is a hard requirement rather than tidiness:

      * THE OVERLAP SENTINELS. canopy_overlap_pct, road_overlap_pct and
        production_overlap_pct each use None for "never checked" and 0.0 for
        "checked and genuinely none". The frontend renders an em-dash for
        null precisely so the second never prints as the first. A `or 0.0`
        anywhere on this path would erase a measurement's absence.
      * cross_type_overlaps. The agreement report between the two survey
        instruments, computed at GENERATE time against the SURVIVING zones.
        It is a finding about the ground, not about the selection, so it is
        not recomputed against a commit set and does not change when one
        changes.
      * MEMBERS RIDE AS THEIR OWN FEATURES on survey_zone_member_<type>,
        beside the zone envelopes on survey_zone_<type>, exactly as the
        entry point built them. The frontend styles on survey_type, which is
        carried both as a property and as the layer name.

    THE ONE THING THIS FUNCTION ADDS: `feature_id` ON EVERY TABULAR ROW,
    CARRIED FROM THE FEATURE AND NEVER REBUILT. This is production_zone_
    payload.assemble_production_zone_payload()'s documented precedent,
    applied to the step with the same split. `zones` (the narrative digest)
    is keyed by the INTERNAL zone id; the map, the tabs and the commit body
    all key on the WIRE feature id that water_survey_zones_to_feature_
    collection() minted. A panel row that reconstructed that id with a
    format string -- `f"water-survey-zone-{row['id']}"` -- would be one
    identity with two sources of truth joined by a template nothing checks:
    rename the prefix and selection stops matching, silently, with no error
    anywhere. So the id is LOOKED UP off the features themselves, and the
    bare `id` stays alongside it because the digest's own consumers key on
    it. The lookup reads only the two ENVELOPE layers -- a member feature
    carries no zone_id of its own kind and a dropped zone is not in this
    collection at all.

    `assembled` is the orchestrator's consumes dict. Unread here -- every
    value this payload needs is on `result` -- and taken anyway because the
    payload signature is the registry's, not this step's. The landform
    builder reads its exclusion half from it; water's entry point already
    folded its inputs into the result.
    """
    narrative = result["narrative_data"]

    # THE WIRE FEATURE ID, CARRIED RATHER THAN REBUILT -- see this
    # function's own note. Keyed by the internal zone_id the feature
    # properties carry, which is the id the narrative digest's rows use.
    feature_id_by_zone_id = {
        feature["properties"]["zone_id"]: feature["id"]
        for feature in result["zones_geojson"]["features"]
        if feature["properties"]["layer"] in LAYER_SURVEY_ZONES
    }

    return {
        # The proposals. Named by the water entry's proposal_collection, which
        # is what the reopen restore matches committed ids against.
        "survey_zones": result["zones_geojson"],
        # The tabular half, as `zones` is for landform: build_narrative_data()
        # has already reduced every surviving zone to the imperial,
        # JSON-native block the report reads (dual acreage, the criterion
        # means, the three overlaps with their sentinels intact, the gravity
        # block, the cross-type finding) AND the curated `panel` rows the
        # map's zone tab renders -- a subset and a reordering of the same
        # block, never a second source. Only `feature_id` is added here.
        "zones": [
            {**row, "feature_id": feature_id_by_zone_id[row["id"]]}
            for row in narrative["zones"]
        ],
        # THE STEP-LEVEL BLOCK, whole. Counts per type, the dropped count, the
        # gate accounting, the threshold and grouping distance the zones were
        # produced under, the parcel-relative TWI caveat, and soil_checked.
        # Passed as one object rather than spread into the payload's top level
        # so the panel reads the same block the report does.
        "summary": {
            key: value for key, value in narrative.items() if key not in ("zones", "scales")
        },
        # HOW TO READ EVERY SCORED VALUE IN A PANEL ROW, at the payload's
        # top level exactly where the production payload puts its own --
        # a scale describes the instrument, not one step's summary of it,
        # and a panel that renders a number without its scale is showing a
        # figure nobody can act on.
        "scales": narrative["scales"],
        # NO SEPARATE gate_mask_stats KEY. The result carries one
        # (compute_water_survey_areas()'s own), and it is numpy and shapely
        # -- not JSON-serializable, by that function's own statement. What
        # CAN go on the wire is build_narrative_data()'s digest of it, which
        # is already here as summary["gates"]. A second key holding that same
        # digest under the internal name would look like the native object
        # and be a copy of the digest.
    }


def build_roads_payload(proposals: dict, assembled: dict) -> dict:
    """
    The roads step's wire payload: EVERY candidate network, merged into one
    proposal collection, beside a per-network block.

    `proposals` is the accumulating step's KEYED STORE (see _ensure_
    accumulated_proposals) -- candidate-set key -> {"inputs", "result"} --
    not one entry point result, which is the shape difference between this
    builder and the two before it. Each result is identify_road_corridor_
    candidates()'s own return for ONE access point.

        {
          "road_corridors": FeatureCollection,   # one LineString per branch,
                                                 #   every network, ids carrying
                                                 #   the network id
          "networks": [                          # in the order the access
            {                                    #   points were tried
              "network_id", "access_point", "feature_ids",
              ...build_narrative_data()'s block, whole: network_found,
                 stop_reason, determination, access, branches
            }, ...
          ],
          "summary": {"network_count", "max_networks", "slots_remaining"},
        }

    THE COLLECTION IS REBUILT HERE WITH THE NETWORK ID, not carried from the
    result's own zones_geojson: the entry point minted "road-corridor-<n>"
    knowing nothing of other networks, and two networks' trunks would share
    an id. wire_translation.road_network_to_feature_collection() takes the
    network id and the access point and stamps both; nothing else about the
    features changes. floodplain_data_is_fallback is read off the result's
    OWN narrative block -- the value the run actually applied.

    EACH NETWORK'S NARRATIVE BLOCK IS ITS OWN. build_narrative_data() ran
    once per generate, so `access.reaches_water_zone` -- a boolean that was
    ambiguous with one network and a union of zones -- is a per-network fact
    here: this network's spur reached the selected water ground, or did
    not. Nothing had to change for that to be true; the per-network
    candidate is what made the boolean unambiguous.
    """
    from feature_schema import make_feature_collection
    from wire_translation import road_network_to_feature_collection

    features = []
    networks = []
    for key, entry in proposals.items():
        result = entry["result"]
        access_point = entry["inputs"]["access_point"]
        narrative = result["narrative_data"]
        collection = road_network_to_feature_collection(
            result["road_network"],
            floodplain_data_is_fallback=bool(
                narrative["determination"]["floodplain_data_is_fallback"]
            ),
            network_id=key,
            access_point=access_point,
        )
        features.extend(collection["features"])
        networks.append(
            {
                "network_id": key,
                "access_point": [float(access_point[0]), float(access_point[1])],
                "feature_ids": [feature["id"] for feature in collection["features"]],
                **narrative,
            }
        )
    max_networks = step_registry.get_step("roads").accumulate.max_candidates
    return {
        "road_corridors": make_feature_collection(features),
        "networks": networks,
        "summary": {
            "network_count": len(networks),
            "max_networks": max_networks,
            "slots_remaining": max(max_networks - len(networks), 0),
        },
    }


# ======================================================================
# Post-commit hooks
# ======================================================================


def run_post_commit_hooks(definition, context, document) -> tuple:
    """
    Every hook the step DECLARES, in declaration order. Returns the targets
    that ran, for the caller to report.

    NO BRANCH ON step_id, here or anywhere below it. The registry entry says
    what must re-run after a commit to its step; this resolves and calls it.
    Adding the water step's own hook is a line in that entry.

    A HOOK MAY NOT FAIL SILENTLY. Nothing is caught here: a hook that raises
    takes the commit call down with it, AFTER the document write, which is
    the honest ordering -- the decision is the user's and it is recorded; a
    derived context layer that could not be recomputed is a server problem
    and must be reported as one rather than swallowed into a successful
    return. The context it failed to update is disposable and will be rebuilt.
    """
    ran = []
    for hook in definition.post_commit:
        step_registry.resolve(hook.target)(context, document)
        ran.append(hook.target)
    return tuple(ran)


def attach_keypoint_relationships(context, document) -> None:
    """
    THE KEYPOINT RELATIONSHIP LAYER, recomputed against what is committed
    NOW. Declared by the landform entry's post_commit, and DELIBERATELY NOT
    by the water entry's -- see THE WATER HALF below, which is a reported gap
    rather than a step that does not exist yet.

    WHAT IT IS. Every keypoint gains a `feature_relationships` dict: distance
    and signed elevation differential to the nearest production area and to
    the selected water zone, each carrying a status of "computed" or
    "no_feature". pipeline_context._attach_keypoint_feature_relationships()
    is the implementation and this does not reimplement it -- the batch
    pipeline and the interactive session must produce the same relationship
    data or the report says something the panel does not.

    WHY IT IS A HOOK AND NOT A STEP. Keypoints are NOT interactive. They are
    never generated, never committed, never in the Design Document at all --
    they are a read-only context layer the terrain warm-up computes and the
    report reads. But their relationship data is DERIVED FROM COMMITS, so the
    only moment it can be brought up to date is just after one lands. That is
    exactly what a post-commit hook is for, and it is why this cannot be a
    registry `produces` field or a consumes edge: there is no step here to
    hang either on.

    IDEMPOTENT. The implementation OVERWRITES kp["feature_relationships"]
    rather than appending to it, so running after the landform commit and
    again after a re-commit converges on the same answer each time. That is
    what would make it safe to declare on a second step.

    THE WATER HALF IS STILL "no_feature", AND THAT IS NOW A KNOWN GAP RATHER
    THAN A STEP THAT DOES NOT EXIST. The water entry is written and the water
    step commits a SELECTION of survey zones, which reaches consumers as ONE
    value whose geometry is the UNION of them
    (wire_translation.water_zone_union()). But the implementation below reads
    TWO fields off the selected water zone -- render_fill_polygon_utm for the
    distance, and representative_elevation_m for the differential -- and a
    union of several zones has no single representative elevation. The honest
    answer is per-keypoint, against the NEAREST selected zone, which changes
    _attach_keypoint_feature_relationships()'s signature and changes what the
    BATCH pipeline means by the keypoint water relationship; the union
    deliberately carries no elevation rather than inventing one.

    So the hook stays declared on landform alone. Nothing regressed: the
    water half reads "no_feature" exactly as it did before the water entry
    existed. What is NOT true any more is that "no_feature" is a truthful
    answer -- after a real water commit it is a STALE one, and declaring the
    hook on the water entry without fixing the elevation question would only
    make it stale faster. Fixing it is the branch that gives the keypoint
    layer a per-zone answer.

    Resolved late (step_registry.resolve) rather than imported at module
    scope: pipeline_context.py pulls in the whole batch pipeline, and this
    module's import surface is deliberately small.
    """
    attach = step_registry.resolve(
        "pipeline_context._attach_keypoint_feature_relationships"
    )
    attach(
        context.keypoints,
        committed_internal_value(context, document, "landform") or [],
        # THE ONE SELECTION THIS HOOK STILL CANNOT READ, and the reason is
        # no longer "the water entry does not exist". It exists, and it says
        # how its commit reaches a consumer: as the UNION of the selected
        # zones. What the union cannot supply is the second field the
        # implementation reads off it, representative_elevation_m -- a union
        # of three zones has no representative elevation, and putting a
        # number there would be inventing a measurement of an object no
        # suitability surface nominated. See this function's docstring.
        None,
    )


def committed_internal_value(context, document, step_id):
    """
    A committed step's rehydrated internal value, or None when that step is
    not committed -- the question a post-commit hook asks, as opposed to the
    question a consumes edge asks.

    THE DIFFERENCE FROM _resolve_from_committed() IS THE ANSWER TO "NOT
    COMMITTED". A consumes edge REFUSES, because a generate that proceeds
    without an input it declared would silently self-compute it. A hook
    recomputing a derived context layer gets None and carries on, because
    "nothing is committed here yet" is a real state that layer has to be able
    to represent -- the keypoint layer's own "no_feature".

    Returns None for a step with no registry entry too. That is the same
    answer for a different reason and the caller cannot tell them apart,
    which is fine for a hook: either way there is no commit to read.
    """
    entry = document["steps"].get(step_id)
    if entry is None or entry["status"] != design_document.STATUS_COMMITTED:
        return None
    try:
        definition = step_registry.get_step(step_id)
    except step_registry.RegistryError:
        return None
    return committed_value(
        step_registry.Consumed(
            name=step_id,
            source=step_registry.SOURCE_COMMITTED,
            from_step=step_id,
            rehydrate=definition.commit_contract.rehydrate,
        ),
        context,
        document,
    )


# ======================================================================
# commit
# ======================================================================


def commit_step(
    session_id: str,
    step_id: str,
    features: dict,
    provenance: dict,
    base_revision: int,
    store,
    inputs: Optional[dict] = None,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    Commit a feature set to one step. Returns the NEW Design Document.

    SYNCHRONOUS, unlike generate. A commit rehydrates the committed features
    and runs the step's post-commit hooks -- bounded work over geometry
    already in memory, with no fetch anywhere in it -- and it is the moment a
    user's decision is recorded, so a client that gets a document back knows
    the decision landed. A job id would say only that it might have.

    WHAT IT DOES, in order, and the order is the contract:

      1. VALIDATE against the step's CommitContract
         (commit_validation.check_commit). Boundary containment and geometric
         validity are hard gates and reject the commit, naming the offending
         features; exclusion crossings are not gates at all. Rehydration
         happens HERE, as part of the gate, because it IS the validity check.
      2. COMPUTE EXCLUSION CROSSINGS per feature and annotate the collection
         with them.
      3. WRITE, through design_document.commit_step -- B1's pure function,
         which owns the revision check and the downstream cascade. Not
         reimplemented, not partially repeated: the cascade that resets later
         steps on a re-commit is that function's and stays there.
      4. RUN THE POST-COMMIT HOOKS declared by the registry entry.
      5. UPDATE THE SESSION CACHE so a downstream generate reads the
         committed value without rehydrating it again.

    NOTHING IS WRITTEN IF STEP 1 REJECTS. The gate runs to completion before
    the document is touched, so a rejected commit leaves the step exactly as
    it was and can be retried with the same base_revision.

    VALIDATION BEFORE THE REVISION CHECK, deliberately. A stale base_revision
    is a retry-after-refetch; a self-intersecting ring is a drawing to fix.
    Both can be true at once, and the second is the one the user has to act
    on either way -- it will still be there after the refetch. So the gate
    reports the geometry first, and RevisionConflictError (carrying the
    current document, so the caller can rebase without a round trip) comes
    from the write.

    `inputs` is the step's collected user inputs. Landform and water collect
    none and store whatever arrives verbatim (None). A step that DECLARES
    user inputs has them checked FIRST (validate_commit_inputs): the body
    must carry them, in shape and on the boundary, or the commit is refused
    before any geometry is looked at -- and for an accumulating step every
    committed feature must come from a candidate set whose input is in the
    declared list, checked after the gate as a per-feature rejection.
    """
    definition = step_registry.get_step(step_id)
    document = store.get(session_id)
    inputs = validate_commit_inputs(definition, inputs, document["boundary"])
    context = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=cache
    )

    # 1. THE GATE. Raises CommitRejectedError carrying every problem.
    check = commit_validation.check_commit(
        definition,
        features,
        provenance,
        context.dem,
        context.boundary_polygon_utm,
    )
    check_features_against_inputs(definition, features, inputs)

    # 2. Crossings, recorded alongside each feature. Measured against the
    # session's own exclusion result -- the same gates the proposals were
    # computed against, so a zone's record cannot describe a different
    # parcel's masks than the ones the user was shown.
    annotated = commit_validation.annotate_crossings(
        features["features"], check.rehydrated, context.exclusion_zones
    )

    # 3. THE WRITE.
    updated = design_document.commit_step(
        document,
        step_id,
        annotated,
        provenance,
        base_revision,
        inputs=inputs,
    )
    store.put(updated)

    # 5, before 4: the cache first, so a hook that reads a committed value
    # through committed_internal_value() gets the rehydration this commit
    # already paid for rather than doing it a second time. Keyed by the
    # revision the write just produced.
    #
    # WARM AND COLD MUST AGREE. For an ungrouped contract the gate's
    # per-feature rehydration IS the collection's (one patch per feature),
    # so it is cached as paid for. For a GROUPED contract it is not: the
    # gate rehydrated each road branch alone, producing one-branch networks,
    # where the collection rehydrator assembles the branches into their
    # network. Caching the gate's list would serve a different shape on a
    # warm read than a cold read rebuilds -- so a grouped commit is put
    # through the same _rehydrate_committed() the cold path uses. One more
    # pass over geometry in hand; identical answers by construction.
    if definition.commit_contract.feature_group:
        value = _rehydrate_committed(
            updated["steps"][step_id]["features"], provenance,
            definition.commit_contract, context.dem,
        )
    else:
        value = check.rehydrated
    context.step_committed[step_id] = {
        "revision": updated["steps"][step_id]["revision"],
        "value": value,
    }
    # A COMMIT INVALIDATES THE SAME THINGS A REOPEN DOES. design_document.
    # commit_step() resets every later step in the document when it
    # re-commits an already-committed step; the derived values cached for
    # those steps are stale in exactly the same way. Run unconditionally --
    # a first commit has nothing downstream to drop, so this is a no-op
    # there, and making it conditional would mean tracking the same "was it
    # already committed" state the document already answers.
    invalidate_downstream(context, updated, step_id)

    # 4. The hooks.
    run_post_commit_hooks(definition, context, updated)

    return updated


def validate_commit_inputs(definition, inputs, boundary):
    """
    A commit body's `inputs` against the step's declared UserInputs.
    Returns the normalised inputs to store, or `inputs` untouched for a
    step that declares none.

    THE SERVER END OF A KNOWN CLIENT GAP. See CommitInputError: a body
    missing a declared input is refused at 400, never accepted as a commit
    with no input. For an accumulating step the input travels as the LIST
    of every value tried (Accumulation.inputs_list), and every element is
    shape-checked and boundary-validated exactly as a generate's param is.
    An EMPTY list is legal only alongside an EMPTY commit -- "no road, and no
    access point was ever placed" -- which check_features_against_inputs()
    enforces once the features are known.
    """
    if not definition.user_inputs:
        return inputs
    if not isinstance(inputs, dict):
        raise CommitInputError(
            f"step '{definition.step_id}' declares required user input(s) "
            f"{definition.user_input_names()}; the commit body carries no "
            f"'inputs'. An absent input is not a decision."
        )
    accumulation = definition.accumulate
    normalised = {}
    singular = list(definition.user_inputs)
    if accumulation is not None:
        user_input = definition.user_input(accumulation.keyed_by)
        singular = [ui for ui in singular if ui.name != accumulation.keyed_by]
        values = inputs.get(accumulation.inputs_list)
        if not isinstance(values, list):
            raise CommitInputError(
                f"step '{definition.step_id}' records every "
                f"'{accumulation.keyed_by}' tried under "
                f"inputs['{accumulation.inputs_list}'] (a list); the commit "
                f"body carries {values!r}."
            )
        checked = []
        for index, value in enumerate(values):
            where = (
                f"step '{definition.step_id}' inputs['{accumulation.inputs_list}'][{index}]"
            )
            normal = _INPUT_SHAPE_CHECKS[user_input.shape](value, where)
            if user_input.validate:
                try:
                    step_registry.resolve(user_input.validate)(boundary, normal)
                except ValueError as exc:
                    raise StepOrchestrationError(f"{where} was rejected: {exc}") from exc
            checked.append(list(normal))
        normalised[accumulation.inputs_list] = checked
    for user_input in singular:
        if user_input.name not in inputs:
            raise CommitInputError(
                f"step '{definition.step_id}' declares required user input "
                f"'{user_input.name}'; the commit body's inputs carry "
                f"{sorted(inputs)}."
            )
        where = f"step '{definition.step_id}' inputs['{user_input.name}']"
        normal = _INPUT_SHAPE_CHECKS[user_input.shape](inputs[user_input.name], where)
        if user_input.validate:
            try:
                step_registry.resolve(user_input.validate)(boundary, normal)
            except ValueError as exc:
                raise StepOrchestrationError(f"{where} was rejected: {exc}") from exc
        normalised[user_input.name] = list(normal)
    unknown = sorted(set(inputs) - set(normalised))
    if unknown:
        raise CommitInputError(
            f"step '{definition.step_id}' accepts inputs {sorted(normalised)}; "
            f"got unknown {unknown}"
        )
    return normalised


def check_features_against_inputs(definition, features: dict, inputs) -> None:
    """
    For an accumulating step: every committed feature must carry the key of
    a candidate set whose input is in the declared list. Raises
    CommitRejectedError naming each feature that does not.

    WHY THIS IS A GATE. The declared list is what a reopen regenerates from
    and what the document says the user tried. A committed network whose
    access point is not in that list would be restored as nothing -- its
    ids matching no regenerated proposal -- and the user's selection would
    quietly vanish on reopen. The mismatch is a client that lost an input,
    and it is told so at commit rather than at reopen.
    """
    accumulation = definition.accumulate
    if accumulation is None or not definition.user_inputs:
        return
    key_of = step_registry.resolve(accumulation.key)
    declared = {key_of(value) for value in inputs.get(accumulation.inputs_list, [])}
    rejections = []
    for feature in (features or {}).get("features") or []:
        key = (feature.get("properties") or {}).get(accumulation.feature_key_property)
        if key not in declared:
            rejections.append(
                commit_validation.FeatureRejection(
                    feature.get("id"),
                    commit_validation.REJECT_INPUT_NOT_DECLARED,
                    f"This feature came from the candidate set "
                    f"{accumulation.feature_key_property}={key!r}, whose "
                    f"'{accumulation.keyed_by}' is not among the "
                    f"{len(declared)} declared in "
                    f"inputs['{accumulation.inputs_list}']. A reopen could "
                    f"not restore it.",
                )
            )
    if rejections:
        raise commit_validation.CommitRejectedError(definition.step_id, rejections)


# ======================================================================
# reopen
# ======================================================================


def invalidate_downstream(context, document, step_id) -> tuple:
    """
    Drop the cached DERIVED values that a change to `step_id` made stale, and
    return what was dropped.

    TWO DIFFERENT INVALIDATIONS, WALKED ALONG TWO DIFFERENT EDGE SETS,
    because they answer two different questions:

      PROPOSALS AND RESTORED STATE follow the REGISTRY'S consumes edges,
      transitively (step_registry.transitive_dependents). A step's proposals
      are stale exactly when they were computed from something that changed
      -- which is what a consumes edge means and the only reason those edges
      are written down. A step that comes LATER in KSOP order but consumes
      nothing from `step_id` keeps its proposals, and that is the precision
      design_document.downstream_steps() deliberately does not have (it
      resets everything after a step because the document cannot know what
      depends on what -- the conservative answer, correct for decisions).

      COMMITTED VALUES follow the DOCUMENT. `step_committed` caches the
      rehydration of a committed FeatureCollection, so it is stale the moment
      the document says that step is no longer committed -- which the
      document has already decided by the time this runs. Reading the
      document rather than the registry here is not a shortcut: a step the
      document reset is not committed, full stop, and keeping a rehydrated
      "committed value" for it would be a cache holding a decision that was
      withdrawn.

    Cheap and total: nothing here is expensive to rebuild, which is the whole
    premise of tier 2, so this errs toward dropping.
    """
    dropped = []
    for dependent in step_registry.transitive_dependents(step_id):
        if context.step_proposals.pop(dependent, None) is not None:
            dropped.append(f"{dependent}.proposals")
        if context.step_restored.pop(dependent, None) is not None:
            dropped.append(f"{dependent}.restored")
    for other, entry in document["steps"].items():
        if other == step_id:
            continue
        if entry["status"] != design_document.STATUS_COMMITTED:
            if context.step_committed.pop(other, None) is not None:
                dropped.append(f"{other}.committed")
    return tuple(dropped)


def restore_step_state(
    session_id: str,
    step_id: str,
    store,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    The editable state of a reopened step: its proposals, regenerated, with
    the user's prior selection re-applied off the document.

        {"payload": <the step's wire payload>,
         "selected_feature_ids": [...],   # generated proposals they had picked
         "user_added": <FeatureCollection>,  # the shapes they drew
         "provenance": {...},             # as committed
         "missing_feature_ids": [...]}    # selected ids no longer proposed

    BY RE-RUNNING GENERATE, NOT BY CACHING PROPOSALS SEPARATELY. A reopened
    step needs the candidate set back, and there are two ways to have it:
    keep a copy from before the commit, or recompute it. Recomputing wins on
    the only ground that matters -- there is ONE source of truth for what the
    proposals are. A stored copy is a second one, and the day it disagrees
    with what a regenerate produces (a fixture changed, a threshold moved,
    the cache was evicted and rebuilt) the user is editing a candidate set
    the server would no longer propose. generate is idempotent and
    network-free by contract, so the recompute is cheap and the answer is
    current.

    THIS DEPENDS ON PROPOSAL FEATURE IDS BEING STABLE ACROSS REGENERATES,
    and that is not an assumption -- test_step_commit.py generates twice on
    one session and asserts the id sets are identical. If they ever stop
    being stable this function silently restores an empty selection, so the
    assertion is the thing standing between a user and losing their
    selections on reopen.

    DRAWN ZONES NEED NO ID MATCHING. They carry their own geometry in the
    document and come back from it directly -- they were never proposals and
    a regenerate has nothing to say about them.

    `missing_feature_ids` is REPORTED RATHER THAN SWALLOWED. It should always
    be empty given stable ids; if it is not, the user selected something the
    server no longer offers, and that is a thing to surface, not to quietly
    drop from the restored selection.
    """
    definition = step_registry.get_step(step_id)
    document = store.get(session_id)
    entry = document["steps"][step_id]

    # THE STEP'S OWN COMMITTED USER INPUTS, not an empty dict. design_document
    # .reopen_step() retains `inputs` on the reopened entry precisely so the
    # editable starting point is the one the user was editing -- regenerating
    # the roads step from a different access point than the one they chose
    # would restore a candidate set they never saw. Landform collects none,
    # so this is {} there and the validation is the meaningful part.
    if definition.accumulate:
        # EVERY CANDIDATE, NOT JUST THE COMMITTED ONE. The reopened entry's
        # `inputs` list carries every access point the user tried (the
        # commit gate required it), and the restore regenerates a network
        # for each -- the alternatives are part of their work.
        context = session_manager.get_session_context(
            session_id, store, fetch_cache=fetch_cache, cache=cache
        )
        payload = accumulated_payload(definition, context, document)
    else:
        payload = run_generate(
            session_id,
            definition,
            store,
            validate_params(definition, entry.get("inputs")),
            fetch_cache=fetch_cache,
            cache=cache,
        )

    features = (entry.get("features") or {}).get("features") or []
    provenance = entry.get("provenance") or {}

    # THE REGISTRY NAMES THE KEY, this function does not know it. See
    # StepDefinition.proposal_collection.
    collection = payload.get(definition.proposal_collection) or {}
    proposed_ids = {feature["id"] for feature in collection.get("features", [])}
    selected, user_added, missing = [], [], []
    for feature in features:
        feature_id = feature.get("id")
        if provenance.get(feature_id) == "user_added":
            user_added.append(feature)
        elif feature_id in proposed_ids:
            selected.append(feature_id)
        else:
            missing.append(feature_id)

    return {
        "payload": payload,
        "selected_feature_ids": selected,
        "user_added": {"type": "FeatureCollection", "features": user_added},
        "provenance": provenance,
        "missing_feature_ids": missing,
    }


def reopen_step(
    session_id: str,
    step_id: str,
    store,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    Reopen a committed step for editing. Returns the NEW Design Document.

    WHAT IT DOES, in order:

      1. design_document.reopen_step -- B1's pure function. It moves the step
         back to "generated" keeping its committed features as the editable
         starting point, and resets every later step to not_started. That
         cascade is the DOCUMENT's and is not reimplemented here.
      2. INVALIDATE the downstream cached values (invalidate_downstream --
         the registry's consumes edges for proposals, the document for
         committed values).
      3. RESTORE this step's editable state by re-running its generate and
         re-applying the selection off the document (restore_step_state),
         leaving the result on SessionContext.step_restored.

    THE DOCUMENT IS WRITTEN BEFORE THE RESTORE, and that ordering is
    load-bearing: the restore RE-READS the document through run_generate(),
    which needs to see the step as "generated" rather than "committed" --
    mark_step_generated() refuses to downgrade a committed step, correctly,
    because doing so silently would discard exactly the cascade step 1 just
    applied.

    Raises design_document.DocumentError for a step that is not committed;
    reopening a step nobody committed is not a no-op to absorb, it is a
    caller that thinks the session is in a state it is not.
    """
    # Registered? Fail here, before the document is touched -- an
    # unregistered step has no generate to restore from, so reopening it
    # would leave a step in "generated" with nothing able to generate it.
    step_registry.get_step(step_id)
    document = store.get(session_id)

    updated = design_document.reopen_step(document, step_id)
    store.put(updated)

    context = session_manager.get_session_context(
        session_id, store, fetch_cache=fetch_cache, cache=cache
    )
    invalidate_downstream(context, updated, step_id)
    # The reopened step's OWN cached committed value goes too -- it is no
    # longer committed, and invalidate_downstream() deliberately skips the
    # step it is called about (for a commit, that step's value is the one
    # thing that is fresh).
    context.step_committed.pop(step_id, None)

    context.step_restored[step_id] = restore_step_state(
        session_id, step_id, store, fetch_cache=fetch_cache, cache=cache
    )
    return updated
