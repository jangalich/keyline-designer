"""
test_design_document.py

Offline (no-network, no-geospatial) checks for design_document.py --
pure dict-in/dict-out functions, standard library only, same "no real
data fetch required" philosophy as the rest of this pipeline's tests.

Sections:
  1. CREATE -- all steps not_started, revision 0, unique session_ids.
  2. COMMIT -- landform commit: status/revision/document_revision.
  3. EMPTY COMMIT -- committed-with-zero-features is a real state,
     distinguishable from not_started by status alone.
  4. CASCADE -- reopen resets strictly-downstream steps outright,
     retains the reopened step's data; re-commit cascades identically.
  5. downstream_steps() agrees with what reopen_step() actually resets,
     for every step in STEP_ORDER.
  6. REVISION CONFLICT -- stale base_revision raises with both sides.
  7. PURITY -- every function leaves its input byte-identical.
  8. VALIDATION -- hard failures on every malformed shape the spec
     names; no coercion.
"""

import copy
import json

from design_document import (
    PROVENANCE_VALUES,
    STEP_ORDER,
    DocumentError,
    RevisionConflictError,
    SchemaVersionError,
    commit_step,
    create_document,
    downstream_steps,
    reopen_step,
    validate_document,
)

BOUNDARY = [(-84.5, 39.1), (-84.5, 39.2), (-84.4, 39.2), (-84.4, 39.1)]

EMPTY_FC = {"type": "FeatureCollection", "features": []}


def _fc(*feature_ids):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": fid,
                "geometry": {"type": "Point", "coordinates": [-84.45, 39.15]},
                "properties": {},
            }
            for fid in feature_ids
        ],
    }


def _snapshot(document):
    return json.dumps(document, sort_keys=True)


# --- 1. CREATE: all steps not_started (status only), revision 0,
#     boundary stored as JSON-native lists, session_ids unique ---

doc = create_document(BOUNDARY)
validate_document(doc)
assert doc["schema_version"] == 1
assert doc["document_revision"] == 0
assert doc["boundary"] == [[-84.5, 39.1], [-84.5, 39.2], [-84.4, 39.2], [-84.4, 39.1]]
assert set(doc["steps"]) == set(STEP_ORDER)
for step_id in STEP_ORDER:
    assert doc["steps"][step_id] == {"status": "not_started"}, (
        f"a fresh {step_id} step must carry ONLY its status -- no placeholders"
    )
assert doc["created_at"] == doc["updated_at"]

session_ids = {create_document(BOUNDARY)["session_id"] for _ in range(50)}
assert len(session_ids) == 50, "session_ids must be unique across calls"
assert doc["session_id"] not in session_ids

try:
    create_document([])
    raise AssertionError("empty boundary must hard-fail")
except DocumentError:
    pass

print("CREATE: fresh document shape, status-only steps, 51 unique session_ids.")


# --- 2. COMMIT landform: status/revision/document_revision all correct ---

landform_fc = _fc("ridge-1", "ridge-2")
provenance = {"ridge-1": "generated", "ridge-2": "user_added"}
doc1 = commit_step(doc, "landform", landform_fc, provenance, base_revision=0,
                   inputs={"contour_interval_m": 2.0})
validate_document(doc1)
entry = doc1["steps"]["landform"]
assert entry["status"] == "committed"
assert entry["revision"] == 1
assert entry["features"] == landform_fc
assert entry["provenance"] == provenance
assert entry["inputs"] == {"contour_interval_m": 2.0}
assert doc1["document_revision"] == 1
assert doc1["steps"]["water"] == {"status": "not_started"}

# inputs omitted when the step collected none -- no null placeholder.
doc_no_inputs = commit_step(doc, "landform", landform_fc, provenance, base_revision=0)
assert "inputs" not in doc_no_inputs["steps"]["landform"]

try:
    commit_step(doc, "boundary", EMPTY_FC, {}, base_revision=0)
    raise AssertionError("unknown step id must hard-fail")
except DocumentError:
    pass

try:
    commit_step(doc, "landform", {"type": "Point"}, {}, base_revision=0)
    raise AssertionError("non-FeatureCollection features must hard-fail")
except DocumentError:
    pass

try:
    commit_step(doc, "landform", EMPTY_FC, {"f1": "hand_drawn"}, base_revision=0)
    raise AssertionError("unknown provenance classification must hard-fail")
except DocumentError:
    pass

# "user_modified" IS NO LONGER ACCEPTED. It described a generated candidate
# whose vertices the user edited, and nothing in this system can produce one
# -- generated candidates are select-only at every step. A value nothing can
# emit reads to the next author as a supported case, so it was removed rather
# than left accepted-but-unreachable. See PROVENANCE_VALUES.
assert "user_modified" not in PROVENANCE_VALUES, (
    "user_modified must not be an accepted provenance: no path in this system "
    "can produce a modified generated candidate"
)
try:
    commit_step(doc, "landform", _fc("ridge-1"), {"ridge-1": "user_modified"},
                base_revision=0)
    raise AssertionError("'user_modified' must hard-fail like any other unknown value")
except DocumentError:
    pass

print("COMMIT: landform revision 1, document_revision 1, inputs only when given.")


# --- 3. EMPTY COMMIT: committed with zero features is a deliberate
#     decision, distinguishable from not_started by status alone ---

doc_empty = commit_step(doc, "landform", EMPTY_FC, {}, base_revision=0)
validate_document(doc_empty)
empty_entry = doc_empty["steps"]["landform"]
assert empty_entry["status"] == "committed"
assert empty_entry["features"] == {"type": "FeatureCollection", "features": []}
assert empty_entry["revision"] == 1
assert empty_entry["status"] != doc_empty["steps"]["water"]["status"], (
    "an empty commit and a not_started step must differ by status ALONE"
)

print("EMPTY COMMIT: status 'committed' with an empty FeatureCollection survives intact.")


# --- 4. CASCADE: commit landform, commit water, reopen landform ->
#     landform 'generated' with data retained; water reset outright ---

water_fc = _fc("dam-1")
doc2 = commit_step(doc1, "water", water_fc, {"dam-1": "user_added"}, base_revision=0)
validate_document(doc2)
assert doc2["document_revision"] == 2

doc3 = reopen_step(doc2, "landform")
validate_document(doc3)
reopened = doc3["steps"]["landform"]
assert reopened["status"] == "generated"
assert reopened["features"] == landform_fc, "reopen retains the last committed features"
assert reopened["provenance"] == provenance
assert reopened["inputs"] == {"contour_interval_m": 2.0}
assert reopened["revision"] == 1, "the revision chain survives a reopen"
assert doc3["steps"]["water"] == {"status": "not_started"}, (
    "downstream reset discards features/provenance/inputs/revision ENTIRELY"
)
assert doc3["document_revision"] == 3

try:
    reopen_step(doc3, "water")
    raise AssertionError("reopening a not_started step must hard-fail")
except DocumentError:
    pass

# Implicit cascade: RE-commit landform on doc2 instead of reopening ->
# water resets the same way, landform lands committed at revision 2.
doc4 = commit_step(doc2, "landform", _fc("ridge-3"), {"ridge-3": "user_added"},
                   base_revision=1)
validate_document(doc4)
assert doc4["steps"]["landform"]["status"] == "committed"
assert doc4["steps"]["landform"]["revision"] == 2
assert doc4["steps"]["water"] == {"status": "not_started"}, (
    "re-commit of a committed step applies the reopen cascade"
)
assert doc4["document_revision"] == 3

# After a reopen, committing the reopened step continues from its
# retained revision -- and the cascade already happened, so a first
# commit elsewhere still sees base_revision 0.
doc5 = commit_step(doc3, "landform", landform_fc, provenance, base_revision=1)
assert doc5["steps"]["landform"]["revision"] == 2
doc6 = commit_step(doc5, "water", water_fc, {"dam-1": "user_added"}, base_revision=0)
assert doc6["steps"]["water"]["revision"] == 1

print("CASCADE: reopen retains the step, resets downstream; re-commit cascades identically.")


# --- 5. downstream_steps() agrees with what reopen_step() actually
#     resets, for every step in STEP_ORDER ---

all_committed = doc
for step_id in STEP_ORDER:
    all_committed = commit_step(all_committed, step_id, _fc(f"{step_id}-f"),
                                {f"{step_id}-f": "generated"}, base_revision=0)
validate_document(all_committed)

for step_id in STEP_ORDER:
    after = reopen_step(all_committed, step_id)
    actually_reset = tuple(
        other for other in STEP_ORDER
        if after["steps"][other] == {"status": "not_started"}
    )
    assert actually_reset == downstream_steps(step_id), (
        f"reopen({step_id}) reset {actually_reset}, but downstream_steps "
        f"promised {downstream_steps(step_id)} -- the warning and the "
        f"cascade MUST agree"
    )
    untouched = [
        other for other in STEP_ORDER
        if other != step_id and other not in downstream_steps(step_id)
    ]
    for other in untouched:
        assert after["steps"][other] == all_committed["steps"][other], (
            f"reopen({step_id}) must not touch upstream step {other}"
        )
assert downstream_steps("fencing") == ()

try:
    downstream_steps("report")
    raise AssertionError("downstream_steps must reject unknown step ids")
except DocumentError:
    pass

print("DOWNSTREAM: downstream_steps() matches the actual reset set for all six steps.")


# --- 6. REVISION CONFLICT: stale base_revision raises, carrying both sides ---

try:
    commit_step(doc2, "landform", EMPTY_FC, {}, base_revision=0)
    raise AssertionError("stale base_revision must raise RevisionConflictError")
except RevisionConflictError as conflict:
    assert conflict.expected == 1
    assert conflict.received == 0
    assert conflict.step_id == "landform"
assert issubclass(RevisionConflictError, DocumentError)
assert issubclass(SchemaVersionError, DocumentError)

print("CONFLICT: RevisionConflictError carries expected=1 received=0.")


# --- 7. PURITY: every function leaves its input byte-identical ---

for label, call in [
    ("commit_step on fresh", lambda: commit_step(doc, "landform", landform_fc,
                                                 provenance, base_revision=0,
                                                 inputs={"k": 1})),
    ("commit_step re-commit", lambda: commit_step(doc2, "landform", _fc("x"),
                                                  {"x": "user_added"},
                                                  base_revision=1)),
    ("reopen_step", lambda: reopen_step(doc2, "landform")),
    ("downstream_steps", lambda: downstream_steps("water")),
    ("validate_document", lambda: validate_document(doc2)),
]:
    for target in (doc, doc2):
        before = _snapshot(target)
        call()
        assert _snapshot(target) == before, f"{label} mutated its input document"

# The returned document shares no structure with the input.
mutated_probe = commit_step(doc, "landform", landform_fc, provenance, base_revision=0)
mutated_probe["steps"]["landform"]["features"]["features"].append({"type": "Feature"})
assert len(landform_fc["features"]) == 2, "commit_step must deep-copy features in"

print("PURITY: inputs byte-identical after every call; outputs share no structure.")


# --- 8. VALIDATION: every malformed shape the spec names hard-fails ---

def _expect_invalid(mutate, message):
    broken = copy.deepcopy(doc2)
    mutate(broken)
    try:
        validate_document(broken)
        raise AssertionError(f"validate_document accepted: {message}")
    except DocumentError:
        pass


try:
    validate_document({**copy.deepcopy(doc2), "schema_version": 2})
    raise AssertionError("unknown schema_version must raise SchemaVersionError")
except SchemaVersionError:
    pass

_expect_invalid(lambda d: d["steps"]["water"].__setitem__("status", "done"),
                "unknown status value")
_expect_invalid(lambda d: d["steps"].__setitem__("report", {"status": "not_started"}),
                "unknown step id")
_expect_invalid(lambda d: d["steps"]["landform"].pop("features"),
                "committed step missing features")
_expect_invalid(lambda d: d["steps"]["landform"].pop("provenance"),
                "committed step missing provenance")
_expect_invalid(lambda d: d["steps"]["roads"].__setitem__("features", EMPTY_FC),
                "not_started step carrying extra keys")
_expect_invalid(lambda d: d.pop("boundary"), "missing top-level key")
_expect_invalid(lambda d: d["steps"].pop("fencing"), "missing step entry")

print("VALIDATION: all malformed shapes rejected, valid documents pass.")

print("\nAll design_document checks passed.")
