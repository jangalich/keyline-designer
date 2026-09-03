"""
design_document.py

The Design Document: the persistent record of an interactive design
session. It holds the user's decisions -- the parcel boundary and, per
step, the committed feature set with provenance -- and nothing derived:
generated proposals live in a session cache (a later branch), never
here. `status: "generated"` records only that proposals exist elsewhere.

Every function here is pure: it takes a document and returns a NEW
document, never mutating its input. No network, no geospatial
dependencies -- this module must stay importable and testable with the
standard library alone.

Error posture matches parcel_data.py: malformed input is a hard failure,
never silently coerced or defaulted.

The one deliberately load-bearing distinction: a step committed with an
EMPTY FeatureCollection is a real user decision ("nothing goes here")
and is carried as status "committed" with zero features -- it must never
be collapsed into "not_started".
"""

import copy
import secrets
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# The canonical ordered step list. The Step Registry (a later branch)
# keys off this constant rather than redefining the order. The boundary
# is a top-level document field, not a step; the report is out of scope
# for this phase.
STEP_ORDER = ("landform", "water", "roads", "trees", "structures", "fencing")

STATUS_NOT_STARTED = "not_started"
STATUS_GENERATED = "generated"
STATUS_COMMITTED = "committed"
VALID_STATUSES = (STATUS_NOT_STARTED, STATUS_GENERATED, STATUS_COMMITTED)

# WHAT A COMMITTED FEATURE CAN BE, and there are exactly TWO kinds:
# a generated candidate the user SELECTED, and a shape the user DREW.
#
# "user_modified" WAS HERE AND IS GONE. It described a third kind -- a
# generated candidate whose vertices the user edited -- and nothing in this
# system can produce one: generated candidates are SELECT-ONLY at every step
# (the shipped frontend offers a checkbox and a delete, never a vertex
# handle), and the commit path rejects the value outright. A provenance value
# nothing can emit is worse than absent: it reads to the next person as a
# supported case, and the first consumer written to branch on it gets a
# branch that never runs and never gets tested. If vertex editing is ever
# built, this is the line to add it back on -- deliberately, with the
# rehydration and round-trip questions it raises answered at the same time.
PROVENANCE_VALUES = ("generated", "user_added")

# Keys a step entry may carry, by status. not_started is exactly its
# status -- no empty features, no null placeholders. A generated step
# may retain the last commit's data (the reopen_step contract) or carry
# only its status (proposals exist in the session cache only).
_COMMITTED_REQUIRED_KEYS = frozenset({"status", "revision", "features", "provenance"})
_STEP_OPTIONAL_KEYS = frozenset({"inputs"})
_GENERATED_ALLOWED_KEYS = _COMMITTED_REQUIRED_KEYS | _STEP_OPTIONAL_KEYS
_DOCUMENT_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "document_revision",
        "created_at",
        "updated_at",
        "boundary",
        "steps",
    }
)


class DocumentError(Exception):
    """Base for all Design Document failures."""


class SchemaVersionError(DocumentError):
    """The document's schema_version is not one this code understands."""


class RevisionConflictError(DocumentError):
    """
    A commit was based on a stale step revision -- someone else committed
    in between. Carries both sides so the caller can report or retry.

    AND THE CURRENT DOCUMENT, which is what makes the retry possible rather
    than merely describable. A client holding a stale base_revision has to
    refetch before it can rebase, and the thing it needs is in this
    function's hand at the moment it raises -- so handing it over costs
    nothing and saves the caller a round trip that could itself lose another
    race. `document` is the CURRENT persisted state, deep-copied so a caller
    reading it out of the exception cannot mutate the document the raiser is
    still holding. None only when a caller constructs this error without one.
    """

    def __init__(self, step_id: str, expected: int, received: int, document: dict = None):
        self.step_id = step_id
        self.expected = expected
        self.received = received
        self.document = copy.deepcopy(document) if document is not None else None
        super().__init__(
            f"revision conflict on step '{step_id}': "
            f"expected base_revision {expected}, received {received}"
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_known_step(step_id: str) -> None:
    if step_id not in STEP_ORDER:
        raise DocumentError(
            f"unknown step id '{step_id}'; valid steps are {STEP_ORDER}"
        )


def create_document(boundary: list) -> dict:
    """
    A fresh document: unique session_id, revision 0, every step
    not_started. `boundary` is a sequence of (lon, lat) pairs; it is
    stored as plain lists of floats so the document is JSON-native from
    birth (a round-trip through the store must not change it).
    """
    if not boundary:
        raise DocumentError("boundary must be a non-empty sequence of (lon, lat) pairs")
    stored_boundary = []
    for point in boundary:
        if len(point) != 2:
            raise DocumentError(f"boundary point {point!r} is not a (lon, lat) pair")
        stored_boundary.append([float(point[0]), float(point[1])])
    now = _utc_now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": secrets.token_urlsafe(16),
        "document_revision": 0,
        "created_at": now,
        "updated_at": now,
        "boundary": stored_boundary,
        "steps": {step_id: {"status": STATUS_NOT_STARTED} for step_id in STEP_ORDER},
    }


def document_body(document: dict) -> dict:
    """
    A Design Document ON THE WIRE: the stored document plus `step_order`.

    THE ONE OUTBOUND SHAPE. Every surface that hands a client a document --
    the session routes in session_api.py and a generate job's `done` result
    (step_orchestrator.run_generate_job) -- passes it through here, so a
    document is the same document whichever route produced it. A document
    served by one path and not another is exactly the drift this function
    exists to prevent, which is why it lives beside STEP_ORDER rather than
    in a transport that only some of those callers go through.

    WHY A FIELD AND NOT JUST THE ORDER OF `steps`. create_document() builds
    its `steps` map in STEP_ORDER and Python preserves that insertion order --
    but NOTHING BETWEEN HERE AND THE CLIENT DOES. RFC 8259 says a JSON object
    is an unordered collection of members, and Flask's DefaultJSONProvider
    takes it at its word: `sort_keys` defaults to True, so jsonify() emits the
    steps ALPHABETICALLY -- fencing, landform, roads, structures, trees,
    water. Measured against flask 3.1.3, not assumed.

    That is the worst available failure mode for a client reading the order
    off the keys, because the wrong answer is PLAUSIBLE: six real step ids in
    a stable order that simply is not the pipeline's. A reopen confirmation
    built on it would name the wrong steps as the ones about to be reset, and
    nothing anywhere would raise.

    So the order travels as DATA. The frontend needs it to show what a reopen
    will discard before the click, and the only alternative is a second copy
    of STEP_ORDER hardcoded over there -- a second source of truth for the one
    constant downstream_steps(), the commit cascade and the step registry all
    key off. A derived field on the way out is much the cheaper side of that.

    DERIVED HERE, NOT STORED. This module stays the single owner of the
    constant; a document neither gains this key by being persisted nor keeps
    it by being read back, and every document any surface serves carries it
    -- including ones written by a build that predates this function.
    """
    return {**document, "step_order": list(STEP_ORDER)}


def downstream_steps(step_id: str) -> tuple:
    """
    The steps a reopen (or re-commit) of step_id resets, in order. The
    frontend warns from this BEFORE the click; the cascade in
    reopen_step/commit_step iterates this same function, so the warning
    and the reset can never disagree.
    """
    _require_known_step(step_id)
    return STEP_ORDER[STEP_ORDER.index(step_id) + 1 :]


def _reset_downstream(steps: dict, step_id: str) -> None:
    # Discard features, provenance, inputs, AND revision entirely: a
    # reset step reads as never committed (base_revision 0 next time).
    for downstream_id in downstream_steps(step_id):
        steps[downstream_id] = {"status": STATUS_NOT_STARTED}


def commit_step(
    document: dict,
    step_id: str,
    features: dict,
    provenance: dict,
    base_revision: int,
    inputs: dict = None,
) -> dict:
    """
    Commit a feature set to a step. `base_revision` is the step revision
    the caller last saw (0 for a step never committed); a mismatch means
    a concurrent commit landed first and raises RevisionConflictError
    rather than silently overwriting it.

    Re-committing an already committed step invalidates everything built
    on top of it, so it applies the same downstream cascade reopen_step
    applies.
    """
    _require_known_step(step_id)
    if not isinstance(features, dict) or features.get("type") != "FeatureCollection":
        raise DocumentError(
            f"features for step '{step_id}' must be a GeoJSON FeatureCollection"
        )
    if not isinstance(provenance, dict):
        raise DocumentError(f"provenance for step '{step_id}' must be a dict")
    for feature_id, classification in provenance.items():
        if classification not in PROVENANCE_VALUES:
            raise DocumentError(
                f"provenance for feature '{feature_id}' is "
                f"'{classification}'; must be one of {PROVENANCE_VALUES}"
            )
    if inputs is not None and not isinstance(inputs, dict):
        raise DocumentError(f"inputs for step '{step_id}' must be a dict or None")

    current = document["steps"][step_id]
    expected = current.get("revision", 0)
    if base_revision != expected:
        raise RevisionConflictError(
            step_id, expected=expected, received=base_revision, document=document
        )

    new_document = copy.deepcopy(document)
    if current["status"] == STATUS_COMMITTED:
        _reset_downstream(new_document["steps"], step_id)

    entry = {
        "status": STATUS_COMMITTED,
        "revision": expected + 1,
        "features": copy.deepcopy(features),
        "provenance": copy.deepcopy(provenance),
    }
    if inputs is not None:
        entry["inputs"] = copy.deepcopy(inputs)
    new_document["steps"][step_id] = entry
    new_document["document_revision"] += 1
    new_document["updated_at"] = _utc_now_iso()
    return new_document


def mark_step_generated(document: dict, step_id: str) -> dict:
    """
    Record that a step's proposals now EXIST -- and nothing else. The
    proposals themselves live in the session cache; this document holds
    decisions, never derived bulk data, so "generated" is a status and no
    features are written (see this module's docstring).

    RETURNS THE DOCUMENT UNCHANGED -- the same object, not a copy -- when the
    step is already generated. Regenerating is idempotent and repeatable by
    contract (interactive-design-architecture-proposal.md section 3.1): a
    user who deletes everything and asks for a fresh set of proposals has
    changed no decision, so bumping document_revision for it would churn the
    optimistic-concurrency chain every commit is checked against and make a
    regenerate look, to another tab, like someone else's edit. Callers
    compare identity to decide whether to persist.

    A COMMITTED step is NOT downgraded here. Moving a committed step back to
    "generated" is reopen_step()'s job and carries a downstream cascade with
    it; doing it silently from a generate would discard that cascade and
    leave later steps built on a decision the user is now re-editing. Raises
    instead.

    A step already carrying a reopened commit's features keeps them: this
    sets `status` and touches no other key, so the editable starting point
    reopen_step() left behind survives a regenerate.
    """
    _require_known_step(step_id)
    current = document["steps"][step_id]
    if current["status"] == STATUS_COMMITTED:
        raise DocumentError(
            f"cannot mark committed step '{step_id}' as generated; reopening "
            f"a committed step is reopen_step()'s job and resets the steps "
            f"built on it"
        )
    if current["status"] == STATUS_GENERATED:
        return document

    new_document = copy.deepcopy(document)
    new_document["steps"][step_id]["status"] = STATUS_GENERATED
    new_document["document_revision"] += 1
    new_document["updated_at"] = _utc_now_iso()
    return new_document


def record_step_inputs(document: dict, step_id: str, inputs: dict) -> dict:
    """
    Write a GENERATED step's `inputs` -- and nothing else. Returns the
    document unchanged (the same object) when the entry already holds
    exactly these inputs, so callers compare identity to decide whether to
    persist, as they do with mark_step_generated().

    WHY A GENERATE WRITES AN INPUT TO THE DOCUMENT AT ALL, when a generate
    otherwise writes only a status. The document holds DECISIONS, never
    derived data, and an access point IS a decision: the user placed it,
    and the alternatives they tried are part of their work. The roads step
    accumulates one candidate network per access point, up to a cap, and
    three things depend on the tried set being in the document rather than
    only in the session cache -- a reopen restores every candidate, a cold
    cache rebuilds every candidate, and the cap is enforced against the
    same list in every process. What is NOT written is anything the
    generate computed from those inputs; that stays derived and disposable.

    GENERATED ONLY. A not_started step has no inputs to hold (it must carry
    only its status, per validate_document), and a committed step's inputs
    are the commit's own, written by commit_step and changed only by
    re-committing. Raises DocumentError for either.
    """
    _require_known_step(step_id)
    if not isinstance(inputs, dict):
        raise DocumentError(f"inputs for step '{step_id}' must be a dict")
    current = document["steps"][step_id]
    if current["status"] != STATUS_GENERATED:
        raise DocumentError(
            f"cannot record inputs on step '{step_id}' with status "
            f"'{current['status']}'; only a generated step collects inputs "
            f"outside a commit"
        )
    if current.get("inputs") == inputs:
        return document
    new_document = copy.deepcopy(document)
    new_document["steps"][step_id]["inputs"] = copy.deepcopy(inputs)
    new_document["document_revision"] += 1
    new_document["updated_at"] = _utc_now_iso()
    return new_document


def reopen_step(document: dict, step_id: str) -> dict:
    """
    Reopen a committed step for editing: status goes to "generated" with
    the last committed features/provenance/inputs retained as the
    editable starting point (revision retained too, so the eventual
    re-commit carries the optimistic-concurrency chain forward). Every
    step after it in STEP_ORDER is reset to not_started outright --
    their decisions were built on the reopened step's output.
    """
    _require_known_step(step_id)
    current = document["steps"][step_id]
    if current["status"] != STATUS_COMMITTED:
        raise DocumentError(
            f"cannot reopen step '{step_id}' with status "
            f"'{current['status']}'; only a committed step reopens"
        )
    new_document = copy.deepcopy(document)
    new_document["steps"][step_id]["status"] = STATUS_GENERATED
    _reset_downstream(new_document["steps"], step_id)
    new_document["document_revision"] += 1
    new_document["updated_at"] = _utc_now_iso()
    return new_document


def validate_document(document: dict) -> None:
    """
    Hard structural validation. Raises SchemaVersionError for a
    schema_version this code doesn't understand, DocumentError for
    everything else malformed. Never repairs, migrates, or fills
    defaults.
    """
    if not isinstance(document, dict):
        raise DocumentError(f"document must be a dict, got {type(document).__name__}")
    missing = _DOCUMENT_REQUIRED_KEYS - document.keys()
    if missing:
        raise DocumentError(f"document missing required keys: {sorted(missing)}")
    if document["schema_version"] != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"unknown schema_version {document['schema_version']!r}; "
            f"this code understands {SCHEMA_VERSION}"
        )
    steps = document["steps"]
    if not isinstance(steps, dict):
        raise DocumentError("document 'steps' must be a dict")
    unknown_steps = set(steps) - set(STEP_ORDER)
    if unknown_steps:
        raise DocumentError(f"unknown step ids: {sorted(unknown_steps)}")
    missing_steps = set(STEP_ORDER) - set(steps)
    if missing_steps:
        raise DocumentError(f"document missing steps: {sorted(missing_steps)}")

    for step_id, entry in steps.items():
        if not isinstance(entry, dict) or "status" not in entry:
            raise DocumentError(f"step '{step_id}' entry must be a dict with a status")
        status = entry["status"]
        if status not in VALID_STATUSES:
            raise DocumentError(
                f"step '{step_id}' has unknown status {status!r}; "
                f"must be one of {VALID_STATUSES}"
            )
        if status == STATUS_NOT_STARTED:
            extra = set(entry) - {"status"}
            if extra:
                raise DocumentError(
                    f"not_started step '{step_id}' carries extra keys "
                    f"{sorted(extra)}; it must carry only its status"
                )
        elif status == STATUS_COMMITTED:
            missing_keys = _COMMITTED_REQUIRED_KEYS - entry.keys()
            if missing_keys:
                raise DocumentError(
                    f"committed step '{step_id}' missing {sorted(missing_keys)}"
                )
            extra = set(entry) - _COMMITTED_REQUIRED_KEYS - _STEP_OPTIONAL_KEYS
            if extra:
                raise DocumentError(
                    f"committed step '{step_id}' carries unknown keys {sorted(extra)}"
                )
        else:  # generated: bare, or carrying a reopened commit's data
            extra = set(entry) - _GENERATED_ALLOWED_KEYS
            if extra:
                raise DocumentError(
                    f"generated step '{step_id}' carries unknown keys {sorted(extra)}"
                )
