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
    arguments.update(params)
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
    declared = set(definition.user_inputs)
    unknown = sorted(set(params) - declared)
    if unknown:
        raise StepOrchestrationError(
            f"step '{definition.step_id}' accepts user inputs "
            f"{definition.user_inputs or '()'}; got unknown {unknown}"
        )
    missing = sorted(declared - set(params))
    if missing:
        raise StepOrchestrationError(
            f"step '{definition.step_id}' requires user input(s) {missing}"
        )
    return params


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

    if definition.upstream_steps():
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

    assembled = assemble_consumes(definition, context, document)
    arguments = forwarded_arguments(definition, assembled, params)

    result = definition.resolve_generate()(**arguments)

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
        computed by the entry point and carried on the result.

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

    `assembled` is the orchestrator's consumes dict. Unread here -- every
    value this payload needs is on `result` -- and taken anyway because the
    payload signature is the registry's, not this step's. The landform
    builder reads its exclusion half from it; water's entry point already
    folded its inputs into the result.
    """
    narrative = result["narrative_data"]
    return {
        # The proposals. Named by the water entry's proposal_collection, which
        # is what the reopen restore matches committed ids against.
        "survey_zones": result["zones_geojson"],
        # The tabular half, as `zones` is for landform: build_narrative_data()
        # has already reduced every surviving zone to the imperial,
        # JSON-native block a panel row needs (dual acreage, the criterion
        # means, the three overlaps with their sentinels intact, the gravity
        # block, the cross-type finding).
        "zones": narrative["zones"],
        # THE STEP-LEVEL BLOCK, whole. Counts per type, the dropped count, the
        # gate accounting, the threshold and grouping distance the zones were
        # produced under, the parcel-relative TWI caveat, and soil_checked.
        # Passed as one object rather than spread into the payload's top level
        # so the panel reads the same block the report does.
        "summary": {
            key: value for key, value in narrative.items() if key != "zones"
        },
        # NO SEPARATE gate_mask_stats KEY. The result carries one
        # (compute_water_survey_areas()'s own), and it is numpy and shapely
        # -- not JSON-serializable, by that function's own statement. What
        # CAN go on the wire is build_narrative_data()'s digest of it, which
        # is already here as summary["gates"]. A second key holding that same
        # digest under the internal name would look like the native object
        # and be a copy of the digest.
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

    `inputs` is the step's collected user inputs, stored verbatim on the
    entry. Landform collects none.
    """
    definition = step_registry.get_step(step_id)
    document = store.get(session_id)
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
    context.step_committed[step_id] = {
        "revision": updated["steps"][step_id]["revision"],
        "value": check.rehydrated,
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
