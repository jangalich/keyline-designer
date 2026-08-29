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

OUT OF SCOPE, DELIBERATELY: commit, reopen, the cascade, commit validation,
exclusion-crossing recording, the keypoint relationship post-commit hook,
and HTTP. This module stops at functions callable from Python, the same
place session_manager.py stops.
"""

import operator
from typing import Optional

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
# A resolver takes (consumed, context, document) and returns the value.


def _resolve_from_cache(consumed, context, document):
    """
    A SessionContext attribute path. `dem` and `boundary_polygon_utm` are
    properties over ParcelData; `parcel_data.canopy_height` walks one level
    in. operator.attrgetter handles both, so the registry can name any of
    them without this function enumerating them.
    """
    try:
        return operator.attrgetter(consumed.cache_path)(context)
    except AttributeError as exc:
        raise StepOrchestrationError(
            f"consumed '{consumed.name}' declares cache_path "
            f"'{consumed.cache_path}', which the session context does not "
            f"have: {exc}"
        ) from None


def _resolve_from_committed(consumed, context, document):
    """
    B5b. A committed upstream step's features, read off the document and put
    through the declared inbound rehydrator (proposal section 2.4) so a
    user-authored feature travels down the same override parameter a
    computer-authored one does.

    NOT WRITTEN HERE, and the failure is loud rather than silent, because
    every piece it needs is a commit-path decision this branch does not get
    to make: what an uncommitted upstream step means for a generate (block?
    generate anyway?), and how a commit's `inputs` reach the rehydrator.
    Guessing at those now would be writing untested code against an
    unwritten contract. No registry entry reaches this today -- landform is
    the first step and consumes only derived values -- so the raise is
    unreachable rather than latent.
    """
    raise StepOrchestrationError(
        f"consumed '{consumed.name}' is sourced from the committed "
        f"'{consumed.from_step}' step; reading committed steps is B5b's "
        f"(register a {step_registry.SOURCE_COMMITTED!r} resolver in "
        f"_CONSUMES_RESOLVERS). No registered step needs one yet."
    )


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
        assembled[consumed.name] = resolver(consumed, context, document)
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
    arrives as that job's `result`.

    The two failure classes are deliberately different:

      * A step that cannot be generated AT ALL -- unregistered, or params
        that do not match its declared user_inputs -- raises RIGHT HERE, from
        this call, before a job exists. There is nothing to poll for; the
        request was wrong, and an HTTP layer turns this into a 400/404.
      * Anything that goes wrong DURING the generate becomes the job's
        `failed` state with the error payload above. The caller already holds
        a job id and finds out by asking.

    A caller that wants the payload synchronously calls
    `generate_step(...).wait()` and reads `.result`. That is what the tests
    do; a transport must not (see job_runner.py).
    """
    definition = step_registry.get_step(step_id)
    validated_params = validate_params(definition, params)
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
        return run_generate(
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
    The generate itself, synchronously -- what generate_step()'s job runs.

    Separate and public so the compute path can be exercised, profiled and
    reasoned about without a thread in the way; generate_step() is the
    supported entry point and this is what it does.
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
    updated = mark_step_generated(document, definition.step_id)
    if updated is not document:
        store.put(updated)

    return payload


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
