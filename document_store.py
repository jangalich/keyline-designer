"""
document_store.py

Persistence for Design Documents. The interface (DocumentStore) is
deliberately three methods and nothing more, so the backing store can be
swapped (Postgres, Redis) without touching callers -- resist adding
query methods, filters, or convenience wrappers here.

JSONFileStore is the one implementation: one JSON file per session in a
directory, written atomically (temp file + os.replace in the same
directory, so a crash mid-write never leaves a partial document), with
per-session threading locks guarding read-modify-write -- Flask runs
threaded under gunicorn, so two requests against one session are
expected, not hypothetical.
"""

import abc
import json
import os
import re
import tempfile
import threading

from design_document import validate_document


class SessionNotFoundError(KeyError):
    """No stored document exists for the requested session_id."""


class DocumentStore(abc.ABC):
    """The minimal persistence contract. Three methods; keep it that way."""

    @abc.abstractmethod
    def get(self, session_id: str) -> dict:
        """Load a session's document. Raises SessionNotFoundError."""

    @abc.abstractmethod
    def put(self, document: dict) -> None:
        """Persist a document under its own session_id."""

    @abc.abstractmethod
    def list_sessions(self) -> list:
        """All stored session_ids."""


# session_ids come from secrets.token_urlsafe(); anything outside that
# alphabet is refused before it can touch a path (no traversal via a
# crafted "session_id").
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Per-session locks, keyed by session_id, guarded by their own lock.
# Module-level so every JSONFileStore pointed at the same directory
# within one process shares the same serialization.
_SESSION_LOCKS = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


class JSONFileStore(DocumentStore):
    def __init__(self, directory: str):
        self._directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, session_id: str) -> str:
        return os.path.join(self._directory, f"{session_id}.json")

    def get(self, session_id: str) -> dict:
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise SessionNotFoundError(session_id)
        with _session_lock(session_id):
            try:
                with open(self._path(session_id), "r", encoding="utf-8") as handle:
                    document = json.load(handle)
            except FileNotFoundError:
                raise SessionNotFoundError(session_id) from None
        # Validation happens on every load; an unrecognized
        # schema_version propagates as SchemaVersionError -- no
        # migration is attempted here.
        validate_document(document)
        return document

    def put(self, document: dict) -> None:
        # Validate before persisting: a malformed document must fail
        # loudly at the write, not poison every later get().
        validate_document(document)
        session_id = document["session_id"]
        if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
            raise ValueError(f"unusable session_id for storage: {session_id!r}")
        payload = json.dumps(document, indent=2)
        target = self._path(session_id)
        with _session_lock(session_id):
            # Temp file in the SAME directory so os.replace() is an
            # atomic rename, never a cross-filesystem copy.
            fd, temp_path = tempfile.mkstemp(
                dir=self._directory, prefix=f".{session_id}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, target)
            except BaseException:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                raise

    def list_sessions(self) -> list:
        sessions = []
        for name in os.listdir(self._directory):
            stem, ext = os.path.splitext(name)
            if ext == ".json" and _SESSION_ID_RE.match(stem):
                sessions.append(stem)
        return sorted(sessions)
