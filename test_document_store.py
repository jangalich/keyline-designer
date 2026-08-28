"""
test_document_store.py

Offline checks for document_store.py -- JSONFileStore against a temp
directory, standard library only.

Sections:
  1. ROUND-TRIP -- put then get returns an equal document; list_sessions.
  2. SCHEMA VERSION -- an unrecognized schema_version on load raises
     SchemaVersionError (no migration attempted).
  3. NOT FOUND / HYGIENE -- missing session raises SessionNotFoundError;
     a path-shaped "session_id" never touches the filesystem.
  4. ATOMICITY -- a put over an existing document leaves no temp litter
     and the target is always complete.
  5. CONCURRENCY -- two threads hammering put() on ONE session both
     complete and leave a valid, parseable document.
"""

import json
import os
import tempfile
import threading

from design_document import (
    SchemaVersionError,
    commit_step,
    create_document,
    validate_document,
)
from document_store import DocumentStore, JSONFileStore, SessionNotFoundError

BOUNDARY = [(-84.5, 39.1), (-84.5, 39.2), (-84.4, 39.2)]
EMPTY_FC = {"type": "FeatureCollection", "features": []}

workdir = tempfile.mkdtemp(prefix="document_store_test_")
store = JSONFileStore(workdir)
assert isinstance(store, DocumentStore)


# --- 1. ROUND-TRIP: put then get returns an equal document ---

doc = create_document(BOUNDARY)
doc = commit_step(doc, "landform", EMPTY_FC, {}, base_revision=0,
                  inputs={"interval": 2.0})
store.put(doc)
loaded = store.get(doc["session_id"])
assert loaded == doc, "round-trip through the store must be lossless"
assert loaded is not doc

second = create_document(BOUNDARY)
store.put(second)
assert store.list_sessions() == sorted([doc["session_id"], second["session_id"]])

# Overwrite (the read-modify-write cycle): the newer revision wins.
doc_v2 = commit_step(doc, "water", EMPTY_FC, {}, base_revision=0)
store.put(doc_v2)
assert store.get(doc["session_id"])["document_revision"] == doc_v2["document_revision"]

print("ROUND-TRIP: lossless put/get, list_sessions, overwrite.")


# --- 2. SCHEMA VERSION: unknown version on load raises, no migration ---

future_doc = dict(doc, schema_version=99, session_id="futuresession0000")
with open(os.path.join(workdir, "futuresession0000.json"), "w") as handle:
    json.dump(future_doc, handle)
try:
    store.get("futuresession0000")
    raise AssertionError("unknown schema_version must raise SchemaVersionError")
except SchemaVersionError:
    pass

print("SCHEMA VERSION: schema_version 99 on disk -> SchemaVersionError on get().")


# --- 3. NOT FOUND / HYGIENE ---

try:
    store.get("nosuchsession")
    raise AssertionError("missing session must raise SessionNotFoundError")
except SessionNotFoundError:
    pass

# A traversal-shaped id is refused as not-found, not resolved as a path.
os.makedirs(os.path.join(workdir, "sub"), exist_ok=True)
try:
    store.get("../" + doc["session_id"])
    raise AssertionError("path-shaped session_id must be refused")
except SessionNotFoundError:
    pass

# A malformed session_id in a document is refused at put().
try:
    store.put(dict(doc, session_id="../escape"))
    raise AssertionError("put must refuse a path-shaped session_id")
except ValueError:
    pass

# put() validates: a malformed document never reaches disk.
broken = json.loads(json.dumps(doc))
broken["steps"]["roads"] = {"status": "not_started", "features": EMPTY_FC}
try:
    store.put(broken)
    raise AssertionError("put must refuse an invalid document")
except Exception as error:
    assert not isinstance(error, AssertionError)

print("HYGIENE: missing -> SessionNotFoundError; traversal ids and invalid docs refused.")


# --- 4. ATOMICITY: no temp litter, target always complete ---

for _ in range(20):
    store.put(doc_v2)
leftovers = [name for name in os.listdir(workdir)
             if name.endswith(".tmp") or name.startswith(".")]
assert leftovers == [], f"temp files must never survive a put: {leftovers}"

print("ATOMICITY: 20 consecutive overwrites, zero temp-file litter.")


# --- 5. CONCURRENCY: two threads, one session, both complete, the file
#     is valid and parseable afterwards ---

contended = create_document(BOUNDARY)
variant_a = commit_step(contended, "landform", EMPTY_FC, {}, base_revision=0)
variant_b = commit_step(contended, "water", EMPTY_FC, {}, base_revision=0)

ITERATIONS = 100
start_gate = threading.Barrier(2)
failures = []


def _hammer(variant):
    try:
        start_gate.wait()
        for _ in range(ITERATIONS):
            store.put(variant)
            validate_document(store.get(contended["session_id"]))
    except Exception as error:  # surfaced below -- a thread must not die silently
        failures.append(error)


threads = [threading.Thread(target=_hammer, args=(v,)) for v in (variant_a, variant_b)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=60)
    assert not thread.is_alive(), "concurrent puts deadlocked"
assert failures == [], f"concurrent put/get raised: {failures}"

with open(os.path.join(workdir, f"{contended['session_id']}.json")) as handle:
    final = json.load(handle)  # must parse -- never a torn write
validate_document(final)
assert final in (variant_a, variant_b), "the final document is one whole variant, not a blend"

print(f"CONCURRENCY: 2 threads x {ITERATIONS} put+get on one session, "
      "final file whole and valid.")

print("\nAll document_store checks passed.")
