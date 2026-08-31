"""
session_api.py

THE HTTP SURFACE over the session orchestrator
(interactive-design-architecture-proposal.md section 3.1).

    GET    /api/steps                                -> 200 {step_order}
    POST   /api/sessions                             -> 201 document
    GET    /api/sessions/<sid>                       -> 200 document
    POST   /api/sessions/<sid>/steps/<step>/generate -> 202 {job_id, status}
    POST   /api/sessions/<sid>/steps/<step>/commit   -> 200 document
    POST   /api/sessions/<sid>/steps/<step>/reopen   -> 200 document
    GET    /api/sessions/<sid>/steps/<step>/layers   -> 200 step payload
    GET    /api/jobs/<jid>                           -> 200 {status, result|error}

WIRING, AND NOTHING BUT. Every behaviour these routes expose already exists
in session_manager.py, step_orchestrator.py, commit_validation.py,
design_document.py and job_runner.py, and is tested there. A route in this
file does three things: parse the request body, call one function, and map
the exception it may raise to a status code. There is no rule here that is
not already a rule down there -- and if a route ever appears to need one,
the rule belongs in the orchestrator, not in a handler. See _API_ERRORS.

NO LOCKING HERE. commit and reopen carry `base_revision` through to
design_document.commit_step(), which owns the optimistic-concurrency check,
and document_store.JSONFileStore already holds a per-session lock around
every read-modify-write. A second lock at this layer would be a different
lock protecting the same thing, which is worse than no lock at all: two
locks over one invariant is how a deadlock or a false sense of safety gets
introduced.

WHY A BLUEPRINT AND NOT A SECOND APP. api.py's existing endpoints --
/api/production-zones above all -- are what the shipped frontend spike calls
today, and they keep working unchanged; this surface mounts alongside them
on the same app, same CORS policy, same JSON conventions. The frontend
migrates onto the session path when it is ready, not because this branch
moved its endpoints.

THE JOB ENDPOINT ALWAYS ANSWERS 200 FOR A JOB IT HOLDS, running, done and
FAILED alike. A polling client is asking "did the job finish", and a
finished-with-failure job is a successful answer to that question -- the
failure is in the body, as {"status": "failed", "error": {...}} carrying the
step's own failed_layer. A 5xx there would say the poll itself did not work
and invite a retry of the poll, which would return the same failure forever.
Only an id this process does not hold is a 404.


KNOWN LIMITATION -- SESSION IDS ARE THE ONLY CREDENTIAL.
========================================================
A session id is `secrets.token_urlsafe(16)` (design_document.create_document)
-- 128 bits, unguessable -- and there is NO authentication on any route in
this file. Anyone holding the URL has full read and write on that session,
and what sits behind it is the polygon of someone's land plus every design
decision they have made on it. A URL in a shared browser history, a pasted
link, a referer header on an outbound click all hand that over completely,
and nothing here can tell the owner from the recipient.

THAT IS AN ACCEPTED v1 POSTURE, NOT AN OVERSIGHT. It is written down here so
it is a decision on the record: capability-URL access is the whole security
model, chosen because v1 has no user accounts to attach ownership to and an
unguessable id is a real barrier against enumeration (which is the attack
that scales). What it is not is a barrier against disclosure of a specific
URL. Adding auth means adding an owner to the Design Document and a check to
every route below; it is deliberately not smuggled in as part of the
transport layer, because a half-built one would be worse than a documented
absent one.

There is also no rate limiting. A generate is a DEM-wide compute pass and
job_runner.py caps concurrency at DEFAULT_MAX_WORKERS, so the bound today is
the thread pool rather than a policy -- an unauthenticated caller can still
queue work. Same posture, same reason, same place to fix it.


PERSISTENCE. The store's directory comes from KEYLINE_SESSION_STORE_DIR
(DEFAULT_STORE_DIRECTORY otherwise). On Render and Railway the container
filesystem is EPHEMERAL unless a persistent disk/volume is attached, so
without one every deploy silently discards every in-flight session. See
default_store() and README.md's "Deploying" section.
"""

import os
from dataclasses import dataclass
from typing import Optional

from flask import Blueprint, current_app, jsonify, request

import commit_validation
import design_document
import document_store
import job_runner
import session_cache
import session_manager
import step_orchestrator
import step_registry

# Where JSONFileStore writes, when nothing says otherwise. A relative path
# under the working directory (/app in the Dockerfile), which is exactly the
# ephemeral location the module docstring warns about -- named as a constant
# so the thing a deploy has to point at a real disk has one obvious name.
DEFAULT_STORE_DIRECTORY = "sessions"
STORE_DIRECTORY_ENV = "KEYLINE_SESSION_STORE_DIR"


def store_directory() -> str:
    """The configured Design Document directory. Environment first."""
    return os.environ.get(STORE_DIRECTORY_ENV) or DEFAULT_STORE_DIRECTORY


def default_store() -> document_store.DocumentStore:
    """
    The process-wide store, built lazily on first use.

    LAZY BECAUSE JSONFileStore's CONSTRUCTOR MAKES THE DIRECTORY. Doing that
    at import time would have `import session_api` -- which a test, a linter
    or a doc build may do for reasons unrelated to serving -- create a
    directory as a side effect, and on a read-only filesystem it would make
    the module unimportable rather than the first request unservable. The
    first is a much harder failure to read.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = document_store.JSONFileStore(store_directory())
    return _DEFAULT_STORE


_DEFAULT_STORE = None


@dataclass
class Dependencies:
    """
    What the routes need that is not in the request: the store, the two
    caches and the job runner.

    INJECTED RATHER THAN IMPORTED AT CALL SITES so a test drives the real
    routes over a temp-directory store and its own caches, which is the only
    way an HTTP test can assert anything about eviction or job state without
    reaching into process-wide singletons it shares with every other test in
    the file.

    Every field is Optional and every consumer passes it straight through:
    None means "the process-wide default", which is the convention
    session_manager.py, session_cache.py and job_runner.py already use, and
    the `is None` checks that make it safe live in those modules rather than
    being repeated here. The store is the one exception -- it has no
    module-level default of its own, so this resolves it.
    """

    store: Optional[document_store.DocumentStore] = None
    fetch_cache: Optional[session_cache.FetchCache] = None
    cache: Optional[session_cache.SessionCache] = None
    runner: Optional[job_runner.JobRunner] = None

    def resolved_store(self) -> document_store.DocumentStore:
        return self.store if self.store is not None else default_store()


# ======================================================================
# Errors -> status codes
# ======================================================================
#
# ONE TABLE, READ TOP TO BOTTOM, first match wins -- so a subclass is listed
# above its base (RevisionConflictError and SchemaVersionError are both
# design_document.DocumentError). Everything in it is an exception type that
# ALREADY EXISTS and is already tested where it is raised; nothing here
# re-derives the condition that produces one. That is the point of a table:
# a route cannot quietly grow a second opinion about when a commit conflicts.


def _rejection_payload(exc: commit_validation.CommitRejectedError) -> dict:
    """
    422 carries the OFFENDING FEATURE IDS with a reason each -- never a
    collapsed message.

    THE FRONTEND RENDERS REJECTIONS PER FEATURE, against its own list, which
    is the only presentation that tells a user WHICH of the four zones they
    selected is the problem. A single banner saying "this commit could not be
    saved" makes them delete zones one at a time to find out. The shape is
    CommitRejectedError.as_payload()'s, verbatim -- {"error", "rejections":
    [{feature_id, code, reason}]} -- because that error was built to be
    rendered and this route has nothing to add to it.
    """
    return exc.as_payload()


def _conflict_payload(exc: design_document.RevisionConflictError) -> dict:
    """
    409 carries the CURRENT DOCUMENT, and without it this status code is
    useless.

    Section 2.6: on a conflict the client hydrates the document it is given,
    keeps the draft wherever its base step survived, and re-prompts. It
    cannot do any of that from a status code alone -- it would have to GET
    the session, which is a second round trip that can itself lose another
    race. The document is in the raiser's hand at the moment it raises (see
    RevisionConflictError's docstring, which deep-copies it for exactly this
    handover), so putting it in the body costs nothing.

    `document` is None only when something constructs this error without one;
    the key is then omitted rather than sent as null, job_runner.snapshot()'s
    convention for a half that does not exist.
    """
    payload = {
        "error": str(exc),
        "step_id": exc.step_id,
        "expected_base_revision": exc.expected,
        "received_base_revision": exc.received,
    }
    if exc.document is not None:
        # THE SAME SHAPE AS A 200 BODY, step_order included. The client
        # hydrates this through the identical path it hydrates a successful
        # commit's document through -- that is the whole point of section
        # 2.6's single reconciliation path -- so a document here missing a
        # field a 200 carries would quietly make that one path into two.
        payload["document"] = _document_body(exc.document)
    return payload


def _upstream_payload(exc: step_orchestrator.UpstreamNotCommittedError) -> dict:
    """
    409 NAMING THE UPSTREAM STEP AND ITS STATUS. The client's next action is
    "go back to step X", and that is only actionable if the response says
    which X and what state it is in -- both of which the exception already
    carries, for that reason.
    """
    return {
        "error": str(exc),
        "step_id": exc.step_id,
        "upstream_step": exc.upstream_step,
        "upstream_status": exc.upstream_status,
    }


def _not_generated_payload(exc: step_orchestrator.StepNotGeneratedError) -> dict:
    """
    409 SAYING WHICH STATUS THE STEP IS ACTUALLY IN -- the "say so explicitly
    rather than return empty" half of the layers contract. An empty payload
    would be indistinguishable from a parcel with no production ground.
    """
    return {"error": str(exc), "step_id": exc.step_id, "status": exc.status}


# (exception type, status, payload builder). A payload builder of None means
# the generic {"error": str(exc)} shape the endpoints in api.py already send.
_API_ERRORS = (
    # --- 404: the thing addressed does not exist -----------------------
    (document_store.SessionNotFoundError, 404, None),
    (job_runner.JobNotFoundError, 404, None),
    # A step id that is not in STEP_ORDER, or a real step with no registry
    # entry yet: both are "this URL names no resource", and get_step()'s
    # message already tells the two apart in prose.
    (step_registry.RegistryError, 404, None),
    # --- 422: the body is well-formed but cannot be accepted ------------
    # THE ONLY 422 ON THIS SURFACE, deliberately, so a client can treat
    # "422" as "walk `rejections`" with no second shape to sniff for. An
    # InboundGeometryError -- a self-intersecting ring, a coordinate array
    # that is not a ring -- arrives here rather than as a 500 because
    # commit_validation.check_commit() already catches it and turns it into
    # a per-feature `invalid_geometry` rejection; there is no separate
    # handler for it, and there must not be one.
    (commit_validation.CommitRejectedError, 422, _rejection_payload),
    # --- 409: the request is fine, the session's state is not -----------
    (design_document.RevisionConflictError, 409, _conflict_payload),
    # REACHES A CLIENT FROM BOTH VERBS NOW. step_payload() is synchronous, so
    # GET .../layers has always answered 409 naming the upstream step. POST
    # .../generate used to NOT: assemble_consumes() raises this on the job's
    # thread, so a generate whose upstream commit was missing became a failed
    # job carrying the step's generic error -- "Water survey areas could not
    # be generated", which says the parcel's data failed when the truth was
    # "commit landform first". step_orchestrator.generate_step() now resolves
    # the committed edges BEFORE it submits, through the registry's own walk
    # (check_upstream_commits), so the 409 arrives with no job id issued. The
    # fix is the orchestrator's and not this table's, for the reason it always
    # was: a route pre-checking it here would be re-deriving which upstream
    # commits a step needs, a second opinion about the consumes edges kept
    # where the registry's cascade cannot see it.
    (step_orchestrator.UpstreamNotCommittedError, 409, _upstream_payload),
    (step_orchestrator.StepNotGeneratedError, 409, _not_generated_payload),
    # SchemaVersionError -> 409. THE CHOICE, and the reasoning:
    #
    # It is raised by design_document.validate_document() on a document
    # LOADED FROM THE STORE, so nothing about the request that triggered it
    # is wrong. 422 means "your entity is well-formed but I cannot process
    # it", which points the client at its own body -- and no correction to
    # that body will ever help, because the stored document is the thing
    # this code cannot read. Sending 422 would invite an edit-and-retry loop
    # that cannot terminate.
    #
    # 409 says "conflict with the current state of the target resource",
    # which is literally the case: this session was written by a build whose
    # schema this one does not understand, and the resolution is at the
    # session level (abandon it, or migrate it) rather than in the payload.
    # It also sits with the other two things on this surface that mean "your
    # request was fine, refetch and reconcile", which is the same client
    # branch.
    #
    # AND IT KEEPS 422 MEANING EXACTLY ONE THING. A second 422 shape, with
    # no `rejections` key, would break the per-feature rendering rule above
    # for every client that took it at its word.
    (design_document.SchemaVersionError, 409, None),
    # Plain DocumentError, last of its family: reopening a step that is not
    # committed, or a generate against a committed step. Both are "the
    # session is not in the state this verb needs", which is 409's
    # definition. The malformed-input half of DocumentError (features that
    # are not a FeatureCollection, an unknown provenance value) is
    # unreachable from these routes -- commit_validation.check_commit() runs
    # first and rejects all of it at 422, per feature.
    (design_document.DocumentError, 409, None),
    # --- 400: the request itself is malformed --------------------------
    (session_manager.BoundaryValidationError, 400, None),
    # Unknown or missing `params` against the step's declared user_inputs.
    # Raised by generate_step() BEFORE a job exists, which is what makes it
    # a 400 rather than a failed job -- there is nothing to poll for.
    (step_orchestrator.StepOrchestrationError, 400, None),
)


def _map_error(exc: BaseException):
    """
    (payload, status) for an exception the table knows, or None.

    Returns None rather than raising or defaulting, so a caller can let an
    unrecognised exception propagate to Flask's 500 handler with its
    traceback intact. Swallowing the unknown into a tidy 500 here is how a
    real bug becomes a log line nobody reads.
    """
    for exception_class, status, builder in _API_ERRORS:
        if isinstance(exc, exception_class):
            payload = builder(exc) if builder is not None else {"error": str(exc)}
            return payload, status
    return None


def _handled(function):
    """
    Run a route body, mapping any known exception through _API_ERRORS.

    A DECORATOR RATHER THAN A try/except IN EVERY HANDLER: the mapping must
    be identical on all seven routes, and seven copies of it is seven chances
    for one of them to drift. Flask's own errorhandler() would apply to the
    whole app, which would silently change how api.py's existing endpoints
    report failures -- explicitly out of scope for this branch.
    """

    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 -- re-raised if unmapped
            mapped = _map_error(exc)
            if mapped is None:
                raise
            payload, status = mapped
            current_app.logger.info(
                "session-api: %s -> %s", type(exc).__name__, status
            )
            return jsonify(payload), status

    wrapper.__name__ = function.__name__
    wrapper.__doc__ = function.__doc__
    return wrapper


def _document_body(document: dict) -> dict:
    """
    A Design Document on the wire -- design_document.document_body().

    DELEGATED, NOT DUPLICATED. A generate job's `done` result now carries the
    updated document too (step_orchestrator.run_generate_job), and that result
    does not pass through a route here on its way out -- GET /api/jobs hands
    back job_runner.snapshot() whole and reads nothing inside it. Two places
    shaping a document meant one of them could quietly stop adding
    `step_order`, and the client reading the order off `steps` would get six
    real step ids in the wrong order rather than an error. So the shape lives
    beside STEP_ORDER, where every producer of a document can reach it, and
    this name stays as the local spelling of it.
    """
    return design_document.document_body(document)


def _json_body() -> dict:
    """
    The request body as a dict, or {} -- `silent=True`, api.py's convention
    on every endpoint it has. A missing body then fails on the field that is
    actually missing ("boundary is required") rather than on the parse, which
    is the more useful message.
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ======================================================================
# The blueprint
# ======================================================================


def build_blueprint(deps: Optional[Dependencies] = None, name: str = "sessions"):
    """
    The eight routes, bound to `deps`. `name` is Flask's blueprint name and
    only has to be unique per app -- a test registering a second blueprint on
    a fresh app passes its own.

    SEVEN OF THEM TOUCH A SESSION AND ONE DOES NOT. GET /api/steps takes no
    `deps` at all -- it serves a constant -- and it is registered here anyway
    rather than on its own blueprint, because it is the same surface, under
    the same prefix, with the same CORS policy and the same JSON conventions.
    A second blueprint for one constant would be a second thing to mount.
    """
    if deps is None:
        deps = Dependencies()
    blueprint = Blueprint(name, __name__)

    @blueprint.route("/api/steps", methods=["GET"])
    @_handled
    def get_steps_endpoint():
        """
        THE STEP ORDER, WITH NO SESSION IN IT. design_document.STEP_ORDER,
        served as `step_order` -- the same array under the same name that
        document_body() puts on every document, from the same constant.

        WHY A ROUTE FOR A CONSTANT. The frontend's step rail enumerates the
        pipeline, and before POST /api/sessions there is no document, so
        there is no `step_order`, so it had nothing to enumerate and showed
        the boundary alone. The alternative to this route is a hardcoded
        copy of the six ids over there -- which is precisely the drift
        document_body()'s docstring exists to argue against. That argument
        does not stop applying because a session has not started yet.

        SAME NAME AS THE DOCUMENT'S FIELD, DELIBERATELY. A client reading
        the order pre-session and post-session reads the same key holding
        the same list, so the fallback is a second SOURCE and never a second
        SHAPE -- there is no translation step between the two to get wrong.

        IDS ONLY, NO TITLES. The frontend owns display titles already: they
        are the `title` field of its own step definitions, which is where a
        step says what its panel header reads, and the rail renders that.
        Adding titles here would put "Landform" in two repositories and make
        the copy that shows a coin toss. What the backend owns is the ORDER
        -- the thing that is a fact about the pipeline rather than about the
        chrome -- and that is exactly what this serves. A step id with no
        definition on the client is named by its id, which is what the rail
        already does for the five steps this build has no chrome for.

        NO SESSION, NO AUTH, NO PARAMETERS, and nothing in `deps` is touched.
        Every other route in this file resolves a store; this one cannot
        fail in any way a store could, so there is no 404 and no 409 it can
        reach. It is @_handled anyway for the same reason the others are:
        the wrapper is what makes an unexpected exception a 500 with this
        surface's error shape rather than Flask's HTML default.
        """
        return jsonify({"step_order": list(design_document.STEP_ORDER)})

    @blueprint.route("/api/sessions", methods=["POST"])
    @_handled
    def create_session_endpoint():
        """
        Expects {"boundary": [[lon, lat], ...]} and returns the new Design
        Document, 201 with a Location header.

        201, not 200: this creates a server-side resource with its own URL,
        and the id in that URL is the only handle the client will ever have
        on it (see the module docstring's session-id note). The other POSTs
        here return 200 -- they act on a resource that already exists.

        THE DOCUMENT, NOT JUST THE ID. session_manager.create_session() has
        already fetched Layer 1 and run the terrain warm-up by the time it
        returns, so the document is in hand; making the client immediately
        GET what this call just built would be a round trip for nothing.
        """
        boundary = _json_body().get("boundary")
        document = session_manager.create_session(
            boundary,
            deps.resolved_store(),
            fetch_cache=deps.fetch_cache,
            cache=deps.cache,
        )
        response = jsonify(_document_body(document))
        response.status_code = 201
        response.headers["Location"] = f"/api/sessions/{document['session_id']}"
        return response

    @blueprint.route("/api/sessions/<session_id>", methods=["GET"])
    @_handled
    def get_session_endpoint(session_id):
        """
        The Design Document -- THE RESUME ENDPOINT.

        Served from the store, never from the session cache, because the
        document is the authority (section 2.1) and the cache is disposable.
        It carries every step's status and revision, which is the whole of
        what a returning client needs to know where the wizard is and what
        base_revision its next commit carries.
        """
        return jsonify(_document_body(deps.resolved_store().get(session_id)))

    @blueprint.route(
        "/api/sessions/<session_id>/steps/<step_id>/generate", methods=["POST"]
    )
    @_handled
    def generate_step_endpoint(session_id, step_id):
        """
        Start a generate. 202 + {"job_id", "status"}; the result arrives via
        GET /api/jobs/<job_id> as {"payload", "document"} -- the step's wire
        payload and the document the generate just moved to "generated", so a
        client does not have to GET this session back to learn a status this
        process already knew. See step_orchestrator.run_generate_job().

        202 ACCEPTED IS THE HONEST CODE: the work has been accepted and has
        not been done. Section 3.1 calls for 202 + polling, and the reason is
        in the work itself -- a generate is a DEM-wide compute pass, well past
        what a request should hold a connection open for.

        A request that CANNOT BE ATTEMPTED still fails synchronously, with no
        job: an unregistered step (404) or params that do not match the step's
        declared user_inputs (400). generate_step() raises those before
        submitting, and turning them into a job the client must poll to
        discover its own typo would be worse on every axis.

        The optional {"params": {...}} is the step's user inputs. Landform
        declares none, so any params to it is a 400 -- see validate_params().

        THE ONE PLACE A ROUTE READS THE STORE FOR ITSELF, and it reads it to
        answer a question that is the transport's rather than the
        orchestrator's: does the resource this URL names exist?

        step_orchestrator.generate_step() deliberately does NOT ask. It
        resolves the step and validates the params before submitting, then
        gets the document on the job's thread -- so an unknown session_id
        becomes a FAILED JOB, which is a defensible position from Python
        (test_step_orchestrator.py asserts it: "the step and params were
        fine, so a job legitimately exists to carry the answer") and the
        wrong answer over HTTP. 202 Accepted against a session that does not
        exist tells a client its work was accepted, and the failure it then
        polls for is the step's generic "Production zones could not be
        generated" -- which says the parcel's data failed, not "this session
        is gone". A user on a stale bookmark would be told the wrong thing.

        So this asks, and lets SessionNotFoundError map to 404 like it does
        on every other route here. It adds no rule -- store.get() is the
        existing question and the existing answer -- and the orchestrator's
        own posture is left exactly as it was rather than being quietly
        changed underneath its test.
        """
        deps.resolved_store().get(session_id)
        job = step_orchestrator.generate_step(
            session_id,
            step_id,
            deps.resolved_store(),
            params=_json_body().get("params"),
            fetch_cache=deps.fetch_cache,
            cache=deps.cache,
            runner=deps.runner,
        )
        return jsonify({"job_id": job.id, "status": job.status}), 202

    @blueprint.route(
        "/api/sessions/<session_id>/steps/<step_id>/commit", methods=["POST"]
    )
    @_handled
    def commit_step_endpoint(session_id, step_id):
        """
        Commit a feature set. Expects

            {"features": <FeatureCollection>,
             "provenance": {feature_id: "generated" | "user_added"},
             "base_revision": <int>,
             "inputs": {...}}          # optional, the step's user inputs

        and returns the NEW Design Document, 200.

        SYNCHRONOUS, unlike generate, because the work is: validation and
        rehydration over geometry already in memory, with no fetch in it. A
        job id here would tell the user their decision MIGHT have been
        recorded, which is the one thing a commit must not be vague about.

        `base_revision` is required and is not defaulted. Defaulting it to 0
        would make every commit from a client that forgot the field look like
        a first commit, which is precisely the conflict this field exists to
        detect -- so a missing one is a 400 from the check below rather than
        an optimistic-concurrency chain that silently does nothing.
        """
        body = _json_body()
        if "base_revision" not in body:
            return (
                jsonify(
                    {
                        "error": "Request must include 'base_revision': the step "
                        "revision this commit is based on (0 for a step never "
                        "committed). It is what detects a concurrent commit."
                    }
                ),
                400,
            )
        base_revision = body["base_revision"]
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            return (
                jsonify({"error": "'base_revision' must be an integer."}),
                400,
            )
        document = step_orchestrator.commit_step(
            session_id,
            step_id,
            body.get("features"),
            # `.get(key, {})`, never `.get(key) or {}`: a provenance that
            # arrived as a list or a null is WRONG, and coercing it to an
            # empty map here would replace commit_validation's own "must be a
            # {feature id -> classification} map, got list" with a pile of
            # missing_provenance rejections against features that were fine.
            # An absent key is the only thing that legitimately means "none".
            body.get("provenance", {}),
            base_revision,
            deps.resolved_store(),
            inputs=body.get("inputs"),
            fetch_cache=deps.fetch_cache,
            cache=deps.cache,
        )
        return jsonify(_document_body(document))

    @blueprint.route(
        "/api/sessions/<session_id>/steps/<step_id>/reopen", methods=["POST"]
    )
    @_handled
    def reopen_step_endpoint(session_id, step_id):
        """
        Reopen a committed step for editing. Returns the NEW Design Document,
        200.

        NO BODY. There is nothing for a caller to say -- which step is in the
        URL, and the editable starting point comes off the document. It takes
        no base_revision either: reopen_step() keeps the step's revision, so
        the chain continues rather than being rebased, and there is no
        lost-update to guard against because the reopen writes no features.

        THE DOCUMENT, NOT THE RESTORED STATE. The restore (proposals back,
        prior selection re-applied) lands on the session cache; the client
        reads it through GET .../layers, which is the same endpoint it uses
        on a plain resume. One way to ask for a step's layers, not two.

        Reopening a step that is not committed is a 409, not a no-op: a
        client that thinks the session is in a state it is not should be told.
        """
        document = step_orchestrator.reopen_step(
            session_id,
            step_id,
            deps.resolved_store(),
            fetch_cache=deps.fetch_cache,
            cache=deps.cache,
        )
        return jsonify(_document_body(document))

    @blueprint.route(
        "/api/sessions/<session_id>/steps/<step_id>/layers", methods=["GET"]
    )
    @_handled
    def step_layers_endpoint(session_id, step_id):
        """
        The step's payload -- the same object the generate job returned.

        THIS ENDPOINT EXISTS SO A RESUME OR A RELOAD DOES NOT REGENERATE.
        Without it, a user who refreshes the page has to sit through the
        compute pass again to see the zones they were already looking at.

        A step with no current proposals is a 409 NAMING ITS ACTUAL STATUS,
        never a 200 with an empty collection -- "you have not generated this
        yet" and "this parcel has no production ground" are different answers
        and a client acts differently on them. See StepNotGeneratedError.
        """
        return jsonify(
            step_orchestrator.step_payload(
                session_id,
                step_id,
                deps.resolved_store(),
                fetch_cache=deps.fetch_cache,
                cache=deps.cache,
            )
        )

    @blueprint.route("/api/jobs/<job_id>", methods=["GET"])
    @_handled
    def get_job_endpoint(job_id):
        """
        {"job_id", "status", "result" | "error"} -- job_runner.Job.snapshot()
        verbatim.

        200 FOR A FAILED JOB. The question this endpoint answers is "what is
        the state of this job", and "it failed, here is the step's
        failed_layer" is a complete and successful answer to it. Mapping a
        failed job onto a 5xx would conflate the job's outcome with the
        poll's, and a client that retried the poll on 5xx would then retry
        forever against a terminal state.

        404 ONLY for an id this process does not hold -- never submitted, or
        evicted after finishing (job_runner.py is in-memory and capped). That
        is a genuinely different answer from "failed", and job_runner.get_job()
        already keeps the two apart.
        """
        return jsonify(job_runner.get_job(job_id, runner=deps.runner))

    return blueprint


def create_app(deps: Optional[Dependencies] = None):
    """
    A bare Flask app carrying ONLY this blueprint.

    For tests and for anyone who wants the session surface without api.py's
    report and production-zone endpoints. The deployed app is api.py's, which
    registers the same blueprint -- so this builds nothing the served surface
    does not also have.
    """
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(build_blueprint(deps))
    return app
