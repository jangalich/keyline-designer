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

PROVENANCE_VALUES = ("generated", "user_modified", "user_added")

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
    """

    def __init__(self, step_id: str, expected: int, received: int):
        self.step_id = step_id
        self.expected = expected
        self.received = received
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
        raise RevisionConflictError(step_id, expected=expected, received=base_revision)

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
