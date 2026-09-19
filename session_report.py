"""
session_report.py

THE REPORT, AS A THING THE OWNER OF A SESSION CAN ASK FOR.

    check_ready(document)                       -> None, or ReportNotReadyError
    submit_report(session_id, store, ...)       -> a job_runner.Job
    run_report_job(...)                         -> the job's `done` result
    error_payload(exc)                          -> the job's `failed` payload
    ReportStore                                 -> the produced PDFs, by id

generate_full_report.generate_session_report() already builds the narrative
from the session's committed design, and generate_pdf_report.
generate_session_report_pdf() already assembles the PDF around it. Both are
Python-callable only. This module is what makes them reachable over HTTP --
the preconditions, the job, the failure shapes and the place the finished
file lives -- and session_api.py wires it to two routes.

--- THE REPORT IS NOT A SEVENTH STEP ------------------------------------

It has no candidates, nothing to select and nothing to commit, so it is not
in design_document.STEP_ORDER, it has no step_registry entry, and there is
no `report` status on any document. It is a TERMINAL ACTION over a session
whose six steps are already decided -- which is why it lives in its own
module rather than as another entry in the registry's table.

That is a real distinction and not a filing preference. A step is a thing
the cascade can reset: committing landform again resets everything
downstream of it (design_document._reset_downstream). A report is not
state, so there is nothing for the cascade to reset -- what reopening a
step does to a report is make a NEW one refusable, and that falls out of
check_ready() reading the document, with nothing tracking it.

--- EVERY STEP MUST BE COMMITTED, AND THE REFUSAL IS SYNCHRONOUS ---------

A partial design is not a design. The report reads whatever the document
currently says is committed (session_design.build_session_design(): "a step
the document does not report as committed contributes nothing"), so a
report of a session with an uncommitted water step is a finished-looking
document with a silent hole in it -- the same class of failure the whole
session_design branch exists to remove.

So check_ready() refuses, and it refuses BEFORE a job exists. The shape of
that refusal is step_orchestrator.UpstreamNotCommittedError's, deliberately:
409, naming what is not committed and what state it is in, so the client's
next action ("go back to step X") is in the response. A precondition the
client has to POLL to discover is the wrong story -- it says the work was
accepted when it never could be, and it arrives as a failure that reads
like the report broke.

--- THE TWO FAILURES ARE DIFFERENT IN KIND ------------------------------

EXPIRED WORKING DATA is ACTIONABLE. session_design.
SessionWorkingDataExpiredError means the committed design is intact in the
Design Document and this session's tier-2 cache entry is gone; the user
reopens the steps and recommits, and the report works. Its payload carries
`session_expired`, and the message is the module's own sentence verbatim
(session_design.WORKING_DATA_EXPIRED) -- it was written to be shown.

EVERYTHING ELSE IS NOT. The Claude call failing, a basemap tile server not
answering, weasyprint falling over: none of it is something the user can
fix by retrying differently, and none of it means their design is wrong.
Its payload carries `report_failed`, and the prose says what happened and
that the design is unharmed. It does NOT invite a different attempt,
because there is no different attempt to make.

TOLD APART BY A KEY EACH CARRIES, never by one they lack -- step_
orchestrator.error_payload()'s rule, for its reason: a client reading "no
session_expired" as "must be the other kind" is one new failure mode away
from telling a user to reopen and recommit against something a recommit
cannot touch.

--- SERVING THE FILE ----------------------------------------------------

The job's result carries a URL; the client fetches it and gets the bytes.

NOT THE BYTES IN THE JOB RESULT. GET /api/jobs/<id> is JSON and a PDF is
megabytes of binary -- base64 in a polled envelope would be paid on every
poll after the first, and the poll is the one request that must stay cheap.

NOT A FILESYSTEM PATH ON THE WIRE. A path is a server detail, and a client
that holds one has been handed something only a second endpoint can use
anyway.

So: an unguessable report id, and GET /api/reports/<report_id>. That is the
same capability-URL posture the session ids already have (see session_api.
py's own note): v1 has no accounts to attach ownership to, an unguessable
id is a real barrier against enumeration, and it is not a barrier against
disclosure of a specific URL. Written down here as a decision on the
record, not smuggled in.

NO EXPIRY, NO PAYMENT, NO CREDITS. This branch is the plain version: press
a button, wait, download a PDF. The expiring link arrives with the payment
work and belongs on the ReportStore, which is where a TTL would go.

--- THE FILESYSTEM IS EPHEMERAL, AND THAT IS ACCEPTED HERE --------------

The PDF is written to disk under a temp directory and the ReportStore is
in-memory, so a restart loses both -- the same posture job_runner.py takes
for jobs and for the same reason: a lost report is a regenerate. On Render
and Railway the container filesystem does not survive a deploy at all
(session_api.py's PERSISTENCE note), so a download link that is hours old
can 404 even without a crash.

THAT IS FINE FOR A DOWNLOAD TAKEN IMMEDIATELY, which is the only thing
this branch offers: the client polls the job it just submitted and fetches
the URL the moment it resolves. It is NOT fine for a link mailed to
someone, and that is exactly the feature this branch does not have. When
the expiring link lands, the file moves to object storage and the
ReportStore becomes a row; nothing else in this module changes.
"""

import os
import secrets
import shutil
import tempfile
import threading
from collections import OrderedDict
from typing import Optional

import design_document
import job_runner
import session_design

# The download route this module's result points at. ONE SPELLING, imported
# by session_api.py to build its own route rule -- a URL invented here and
# re-spelled over there is a 404 the day either one is renamed.
REPORT_URL_PREFIX = "/api/reports"

# What the browser saves it as. Not the report id: a user with two reports
# in their downloads folder wants two files they can tell apart by property
# and date, and neither is in a token.
DOWNLOAD_FILENAME = "scale-of-permanence-report.pdf"

PDF_MIME_TYPE = "application/pdf"

# THE PROSE FOR A FAILURE THE USER CANNOT ACT ON. A constant because it is
# the whole of what they will see, and because a test has to be able to
# assert that it does not tell them to try again differently -- there is
# nothing different to try.
GENERATION_FAILED = (
    "The report could not be generated. Something outside your design did not "
    "respond -- the narrative service or the map imagery. Your committed design "
    "is unharmed and nothing about it needs to change."
)


class ReportError(Exception):
    """Base for this module's own refusals."""


class ReportNotReadyError(ReportError):
    """
    A report asked for over a session that is not fully committed.

    RAISED BEFORE A JOB EXISTS. See the module docstring: this is a 409 with
    no job id issued, because the request could never have been attempted
    and a client polling to discover its own missing commit would be told
    the report broke.

    Carries EVERY uncommitted step, not just the first. The client's next
    action is "go back and finish", and a user three steps short who is sent
    back one at a time is being made to rediscover the same refusal twice.
    That is the one way this differs from UpstreamNotCommittedError, which
    names a single upstream edge because a single edge is what it resolved.
    """

    def __init__(self, session_id: str, missing: list):
        self.session_id = session_id
        # [{"step_id": ..., "status": ...}], in STEP_ORDER -- so a client
        # rendering the list renders it in the order the rail shows.
        self.missing = list(missing)
        names = ", ".join(f"'{entry['step_id']}' ({entry['status']})" for entry in self.missing)
        super().__init__(
            f"session '{session_id}' cannot be reported on: the report is built "
            f"from the committed design, and these steps are not committed: "
            f"{names}. Commit every step first -- a report of a partial design "
            f"is a finished-looking document with a hole in it."
        )


class ReportNotFoundError(KeyError):
    """
    No report with that id: never produced, evicted, or lost with the
    process. See the module docstring's ephemerality note -- these three are
    one answer to a client (ask for the report again), and separating them
    would be publishing which of them happened, which it cannot act on.
    """


# ======================================================================
# The preconditions
# ======================================================================


def uncommitted_steps(document: dict) -> list:
    """
    Every step of design_document.STEP_ORDER this document does not report
    as committed, in order, as [{"step_id", "status"}].

    THE DOCUMENT IS THE ONLY SOURCE. Not the session cache, not the
    orchestrator, not a registry walk: "committed" is a fact the Design
    Document holds (section 2.1, it is the authority), and a second way of
    deciding it is a second answer waiting to disagree with the rail the
    user is looking at.
    """
    steps = document.get("steps", {})
    return [
        {"step_id": step_id, "status": steps.get(step_id, {}).get("status", design_document.STATUS_NOT_STARTED)}
        for step_id in design_document.STEP_ORDER
        if steps.get(step_id, {}).get("status") != design_document.STATUS_COMMITTED
    ]


def check_ready(document: dict) -> None:
    """
    Raises ReportNotReadyError unless every step is committed; returns None
    when they all are.

    THE WHOLE PRECONDITION, IN ONE FUNCTION, called synchronously from the
    route. It reads a document and nothing else, so it is the same question
    the frontend's rail answers for itself when it decides to offer the
    button at all -- and the server asks it again because the client's
    answer is not a permission.
    """
    missing = uncommitted_steps(document)
    if missing:
        raise ReportNotReadyError(document.get("session_id", "?"), missing)


# ======================================================================
# Where a produced PDF lives
# ======================================================================


class ReportStore:
    """
    The finished PDFs, by unguessable id. In-memory registry, files under
    one temp directory.

    THE SAME SHAPE AND THE SAME LIFETIME AS job_runner.JobRunner's registry,
    deliberately: a cap, oldest-first eviction, a lock, and nothing that
    survives a restart. A report whose job is still pollable and whose file
    is gone would be the one combination a client cannot make sense of, so
    the two are sized alike and evict alike.

    EVICTION DELETES THE FILE. A registry that forgot an entry while leaving
    its megabytes on a container's disk is a leak that only shows up as a
    full volume, and the ephemeral filesystem this runs on is small.
    """

    def __init__(self, max_reports: int = 64, directory: Optional[str] = None):
        if max_reports < 1:
            raise ValueError(f"max_reports must be >= 1, got {max_reports}")
        self._max_reports = max_reports
        self._directory = directory
        self._reports = OrderedDict()  # report_id -> absolute path
        self._lock = threading.Lock()

    def directory(self) -> str:
        """
        The temp directory this store's PDFs are written into, made on first
        use.

        LAZY, for session_api.default_store()'s reason: importing this module
        must not create a directory as a side effect, because a read-only
        filesystem would then make the module unimportable rather than the
        first report unservable, and the first is a much harder failure to
        read.
        """
        with self._lock:
            if self._directory is None:
                self._directory = tempfile.mkdtemp(prefix="keyline-reports-")
            return self._directory

    def new_path(self) -> tuple:
        """
        (report_id, path) for a report about to be written. The id exists
        before the file does, so the job has one name for the thing from the
        moment it starts.
        """
        report_id = secrets.token_urlsafe(16)
        return report_id, os.path.join(self.directory(), f"{report_id}.pdf")

    def register(self, report_id: str, path: str) -> str:
        """Record a written PDF and return its download URL."""
        with self._lock:
            self._reports[report_id] = os.path.abspath(path)
            self._evict_locked()
        return download_url(report_id)

    def path(self, report_id: str) -> str:
        """
        The file for one report id. Raises ReportNotFoundError for an id
        this store does not hold OR whose file is gone from disk -- the
        second is the ephemeral-filesystem case, and serving a 500 for it
        would tell a client the server broke when the honest answer is that
        the report is no longer there.
        """
        with self._lock:
            path = self._reports.get(report_id)
        if path is None or not os.path.exists(path):
            raise ReportNotFoundError(report_id)
        return path

    def _evict_locked(self) -> None:
        while len(self._reports) > self._max_reports:
            _, path = self._reports.popitem(last=False)
            try:
                os.remove(path)
            except OSError:
                pass

    def discard_all(self) -> None:
        """Drop every report and its directory. For tests and shutdown."""
        with self._lock:
            self._reports.clear()
            directory, self._directory = self._directory, None
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    def __len__(self) -> int:
        with self._lock:
            return len(self._reports)


# The process-wide default, in the same shape job_runner.DEFAULT_JOB_RUNNER
# and session_cache.py's defaults take: a caller with its own store (a test)
# passes it, everyone else gets this one.
DEFAULT_REPORT_STORE = ReportStore()


def download_url(report_id: str) -> str:
    """The URL a client fetches for one report. One spelling, here."""
    return f"{REPORT_URL_PREFIX}/{report_id}"


# ======================================================================
# The job
# ======================================================================


def run_report_job(
    session_id: str,
    store,
    fetch_cache=None,
    cache=None,
    reports: Optional[ReportStore] = None,
    property_label: str = "Property Design Report",
) -> dict:
    """
    THE REPORT JOB'S `done` RESULT:

        {"report_id": ..., "download_url": "/api/reports/<id>",
         "filename": "scale-of-permanence-report.pdf", "size_bytes": N}

    A URL AND NOT THE BYTES -- see the module docstring. `filename` is what
    the download route sets as the attachment name and is here so a client
    can label the link before it fetches; `size_bytes` is what it takes to
    show a real "12.4 MB" beside it rather than a hopeful one.

    WEASYPRINT IS IMPORTED INSIDE THIS FUNCTION, api.py's reason verbatim:
    generate_pdf_report imports weasyprint, which links libpango at import
    time, so at module scope one missing system library would make this
    module unimportable and take every session route down with it. Paid on
    the first report instead, where the capability is used.

    RAISES rather than returning a failure. This runs on a job thread and
    job_runner.submit() turns whatever comes out of it into the job's
    `failed` state through error_payload() below -- which is where the two
    kinds of failure are told apart.
    """
    from generate_pdf_report import generate_session_report_pdf

    if reports is None:
        reports = DEFAULT_REPORT_STORE

    report_id, pdf_path = reports.new_path()
    generate_session_report_pdf(
        session_id,
        store,
        pdf_path,
        fetch_cache=fetch_cache,
        cache=cache,
        property_label=property_label,
    )
    return {
        "report_id": report_id,
        "download_url": reports.register(report_id, pdf_path),
        "filename": DOWNLOAD_FILENAME,
        "size_bytes": os.path.getsize(pdf_path),
    }


def error_payload(exc: BaseException) -> dict:
    """
    One exception -> the payload a failed report job carries.

    TWO SHAPES, EACH NAMING ITSELF (the module docstring's argument):

        {"error": <session_design's own sentence>,
         "session_expired": {"session_id", "step_id", "remedy": "reopen_and_recommit"}}

        {"error": GENERATION_FAILED, "report_failed": {"actionable": false}}

    THE EXPIRED MESSAGE IS THE RAISER'S, VERBATIM -- str(exc), which carries
    session_design.WORKING_DATA_EXPIRED inside it. That sentence was written
    to be shown to the person who has to act on it ("reopen and recommit"),
    and a copy of it here would be a second wording to drift.

    `remedy` IS A KEY, NOT A SENTENCE. The client renders its own copy for
    this case (it owns the wording of its own UI) and needs to branch, not
    to parse prose.

    NEVER A TRACEBACK, for job_runner.py's reason: the exception stays on the
    Job for server-side logging, and a weasyprint stack in a user-facing
    panel tells the reader nothing they can act on.
    """
    if isinstance(exc, session_design.SessionWorkingDataExpiredError):
        return {
            "error": str(exc),
            "session_expired": {
                "session_id": exc.session_id,
                "step_id": exc.step_id,
                "remedy": "reopen_and_recommit",
            },
        }
    return {
        "error": GENERATION_FAILED,
        # `actionable: false` is said out loud rather than left to the
        # absence of `remedy`. It is the one thing this payload is FOR -- a
        # client must not offer "try again with something different", and a
        # contract that relies on a missing key to say so is a contract the
        # next failure shape can break by accident.
        "report_failed": {"actionable": False},
    }


def submit_report(
    session_id: str,
    store,
    fetch_cache=None,
    cache=None,
    runner: Optional[job_runner.JobRunner] = None,
    reports: Optional[ReportStore] = None,
    property_label: str = "Property Design Report",
):
    """
    Check the preconditions, then submit the report job. Returns the Job.

    THE ORDER IS THE POINT. store.get() raises SessionNotFoundError for an
    id that does not exist and check_ready() raises ReportNotReadyError for
    a session that is not finished -- both BEFORE submit(), so both reach
    the client as a status code with no job id issued. Everything after the
    submit is a failure the client polls for, and the line between the two
    is exactly "could this have been attempted at all".

    `runner is None`, never `or`: JobRunner defines __len__, so a runner
    holding no jobs is falsy -- job_runner.get_job()'s own trap, and the
    same one session_cache.py documents for its caches.
    """
    check_ready(store.get(session_id))

    if runner is None:
        runner = job_runner.DEFAULT_JOB_RUNNER

    return runner.submit(
        lambda: run_report_job(
            session_id,
            store,
            fetch_cache=fetch_cache,
            cache=cache,
            reports=reports,
            property_label=property_label,
        ),
        on_error=error_payload,
    )
