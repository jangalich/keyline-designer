"""
test_session_report_route.py

THE REPORT, ASKED FOR OVER HTTP, FOR A SESSION THE OWNER COMMITTED.

Offline, through the real routes on the real parcel. A session is created,
every step generated and committed exactly as a client would, and then
POST /api/sessions/<id>/report is called on Flask's own test client and
polled to a downloadable PDF.

WHAT IS MOCKED IS THE CLAUDE CALL AND THE NETWORK, AND NOTHING ELSE.
offline_harness.install() refuses every outbound request;
fencing_step_fixture.Harness patches the fetch boundaries the session path
reaches; and generate_full_report.generate_scale_of_permanence_report is
replaced by a function returning fixed markdown -- the narrative's CONTENT
is test_session_report.py's subject, and what this file is about is the
route, the job, the failures and the bytes. Everything else runs: the real
session_design read, the real layout map render, the real weasyprint pass.

THE SEVEN CASES:

  1. A FULLY COMMITTED SESSION reports: 202 with a job id, polled to a
     result carrying a download URL, fetched to a PDF.
  2. A SESSION WITH ANY STEP UNCOMMITTED IS REFUSED SYNCHRONOUSLY -- 409
     naming what is missing, and NO JOB IS CREATED. The job runner holds
     exactly as many jobs after the refusal as before it, which is the
     assertion that makes "synchronously" mean something.
  3. AN EXPIRED SESSION fails with session_design.WORKING_DATA_EXPIRED
     VERBATIM on the wire, carrying `session_expired`, and is
     DISTINGUISHABLE from a source failure by a key rather than by prose.
  4. A CLAUDE FAILURE AND AN IMAGERY FAILURE both poll to a failed job
     whose message does not suggest retrying differently.
  5. THE PDF IS A REAL PDF -- the %PDF- header, the %%EOF trailer, the
     page count, and the Content-Type and Content-Disposition it was
     served under.
  6. generate_full_report() AND build_pipeline_context() ARE UNCHANGED,
     and the batch path's own PDF entry point still assembles from them.
  7. THE REPORT IS NOT A SEVENTH STEP: STEP_ORDER is untouched, the
     registry has no report entry, and no document grows a report status.
"""

import json
import os
import re
import tempfile
import time
import zlib

import offline_harness

offline_harness.install()

import design_document  # noqa: E402
import generate_full_report  # noqa: E402
import job_runner  # noqa: E402
import session_api  # noqa: E402
import session_cache  # noqa: E402
import session_design  # noqa: E402
import session_report  # noqa: E402
import step_registry  # noqa: E402
from fencing_step_fixture import Harness, Session  # noqa: E402
from unittest.mock import patch as mock_patch  # noqa: E402

POLL_INTERVAL_SECONDS = 0.05
POLL_TIMEOUT_SECONDS = 900.0

# The narrative the mocked Claude call returns. REAL MARKDOWN with real
# headings, because it goes through markdown -> HTML -> weasyprint for
# real and a one-line string would make "the PDF has several pages" a
# claim about the fixture rather than about the assembly.
STUB_NARRATIVE = "\n\n".join(
    ["# Scale of Permanence Report"]
    + [
        f"## {index}. {heading}\n\n" + ("This section's narrative body. " * 40)
        for index, heading in enumerate(
            [
                "Climate", "Landform", "Water", "Access", "Vegetation",
                "Structures", "Subdivision", "Soil",
            ],
            start=1,
        )
    ]
)


def pdf_objects(pdf_bytes: bytes) -> bytes:
    """
    Every object definition in a PDF, with its compressed object streams
    expanded -- so a structural assertion can be made over the real objects.

    WEASYPRINT WRITES PDF 1.7 WITH OBJECT STREAMS, which means `/Type/Page`
    does not appear in the file's bytes at all: the page objects live inside
    flate-compressed /ObjStm streams. Grepping the raw file for them finds
    zero and would make "the PDF has pages" an assertion that silently
    cannot fail in the other direction either. There is no PDF library in
    requirements.txt -- weasyprint writes through pydyf, which does not read
    -- so this inflates every stream it can and concatenates the result with
    the raw file. Streams it cannot inflate (an image's DCT data) are
    skipped, which is right: nothing asserted here lives inside one.
    """
    parts = [pdf_bytes]
    for match in re.finditer(rb"stream\r?\n", pdf_bytes):
        start = match.end()
        end = pdf_bytes.find(b"endstream", start)
        if end < 0:
            continue
        try:
            parts.append(zlib.decompress(pdf_bytes[start:end]))
        except zlib.error:
            continue
    return b"".join(parts)


class ClaudeStub:
    """
    generate_full_report's own reference to generate_scale_of_permanence_
    report, replaced. PATCHED ON generate_full_report, NOT ON report_
    generator: that module is imported `from report_generator import ...`,
    so the name the session path actually calls is the one bound over there.
    """

    def __init__(self, raises=None):
        self.raises = raises
        self.calls = 0

    def __enter__(self):
        def call(*args, **kwargs):
            self.calls += 1
            if self.raises is not None:
                raise self.raises
            return STUB_NARRATIVE

        self._patch = mock_patch.object(
            generate_full_report, "generate_scale_of_permanence_report", call
        )
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False


class Http:
    """
    Flask's test client over session_api's own blueprint, bound to a
    Session's OWN store, caches and runner.

    THE SAME DEPENDENCIES THE COMMITS WENT THROUGH. A route reading a
    different store than the fixture committed into would make every
    assertion below about an empty session, and the failure would look like
    a precondition bug.
    """

    def __init__(self, session):
        self.session = session
        self.reports = session_report.ReportStore(max_reports=8)
        self.deps = session_api.Dependencies(
            store=session.store,
            fetch_cache=session.fetch_cache,
            cache=session.cache,
            runner=session.runner,
            reports=self.reports,
        )
        self.client = session_api.create_app(self.deps).test_client()

    def report(self, session_id=None, **body):
        return self.client.post(
            f"/api/sessions/{session_id or self.session.id}/report", json=body
        )

    def job(self, job_id):
        return self.client.get(f"/api/jobs/{job_id}")

    def download(self, url):
        return self.client.get(url)

    def poll(self, job_id):
        """
        GET the job until it leaves `running`. A LOOP OF REAL REQUESTS,
        never job.wait(): the polling contract is half of what this file is
        testing, and every poll of a held job must answer 200 whatever the
        job's outcome.
        """
        deadline = time.time() + POLL_TIMEOUT_SECONDS
        polls = 0
        while True:
            response = self.job(job_id)
            polls += 1
            assert response.status_code == 200, (
                f"every poll of a held job is a 200 -- got {response.status_code}"
            )
            body = response.get_json()
            if body["status"] != job_runner.STATUS_RUNNING:
                return body, polls
            assert time.time() < deadline, f"job {job_id} never finished"
            time.sleep(POLL_INTERVAL_SECONDS)


def commit_fencing(session):
    """The sixth step, which the fixture's own upstream() stops short of."""
    payload = session.generate("fencing")
    features = [
        f
        for f in payload["fence_lines"]["features"]
        if f["properties"]["fence_type"] == "boundary"
    ]
    assert features, "the fixture must produce boundary fencing to commit"
    return session.commit("fencing", features, {f["id"]: "generated" for f in features})


def commit_everything(session):
    """
    Every step of STEP_ORDER committed, through the real orchestrator.

    ONLY THE UNCOMMITTED ONES, which is what makes this callable twice.
    After a reopen the cascade leaves the reopened step and everything
    downstream of it needing a commit while the steps ABOVE it are still
    committed -- and a generate against a committed step is a DocumentError
    by design (reopening one is reopen_step()'s job). So this asks the
    document which steps it owes, exactly as the route's own precondition
    does, and commits those.
    """
    commit = {
        "landform": lambda: session.commit_landform(whole=True),
        "water": lambda: session.commit_water(2),
        "roads": lambda: session.commit_roads(),
        "trees": lambda: session.commit_trees(1),
        "structures": lambda: session.commit_structures(1),
        "fencing": lambda: commit_fencing(session),
    }
    document = session.stored()
    for entry in session_report.uncommitted_steps(document):
        document = commit[entry["step_id"]]()
    assert not session_report.uncommitted_steps(document), session_report.uncommitted_steps(document)
    return document


print("=" * 72)
print("test_session_report_route.py -- the report, over HTTP, for a committed session")
print("=" * 72)


with Harness() as HARNESS:
    SESSION = Session()
    DOCUMENT = commit_everything(SESSION)
    HTTP = Http(SESSION)
    print(
        f"\nsession {SESSION.id}: every step of {list(design_document.STEP_ORDER)} committed."
    )

    # ==================================================================
    # 2. AN UNCOMMITTED STEP IS REFUSED SYNCHRONOUSLY, WITH NO JOB
    # ==================================================================
    #
    # RUN FIRST, on a reopen of THIS session, so the refusal is asserted
    # against a session that is otherwise completely finished -- the exact
    # state a real user reaches by reopening one step of a done design, and
    # the one where a report "helpfully" proceeding would produce a
    # finished-looking document with a hole in it.
    print("\n[test 2]. AN UNCOMMITTED STEP is refused SYNCHRONOUSLY, naming it, with NO job created")

    SESSION.reopen("water")
    reopened = SESSION.stored()
    # The cascade: reopening water leaves everything downstream not_started.
    MISSING = session_report.uncommitted_steps(reopened)
    assert [entry["step_id"] for entry in MISSING] == [
        "water", "roads", "trees", "structures", "fencing",
    ], MISSING

    JOBS_BEFORE = len(SESSION.runner)
    REFUSAL = HTTP.report()
    JOBS_AFTER = len(SESSION.runner)

    assert REFUSAL.status_code == 409, (REFUSAL.status_code, REFUSAL.get_json())
    REFUSAL_BODY = REFUSAL.get_json()
    # NO JOB ID, and no job. Both halves: a body carrying one would have the
    # client poll, and a runner holding one would mean the work was started
    # and the 409 was decoration.
    assert "job_id" not in REFUSAL_BODY, REFUSAL_BODY
    assert JOBS_AFTER == JOBS_BEFORE, (
        f"a refused report must create NO job -- runner went from {JOBS_BEFORE} to {JOBS_AFTER}"
    )
    # IT NAMES WHAT IS MISSING, every one of them, with its actual status.
    assert [entry["step_id"] for entry in REFUSAL_BODY["uncommitted_steps"]] == [
        "water", "roads", "trees", "structures", "fencing",
    ], REFUSAL_BODY
    assert REFUSAL_BODY["uncommitted_steps"][0]["status"] == design_document.STATUS_GENERATED, (
        REFUSAL_BODY["uncommitted_steps"][0]
    )
    assert all(
        entry["status"] == design_document.STATUS_NOT_STARTED
        for entry in REFUSAL_BODY["uncommitted_steps"][1:]
    ), REFUSAL_BODY["uncommitted_steps"]
    assert "'water'" in REFUSAL_BODY["error"] and "not committed" in REFUSAL_BODY["error"], REFUSAL_BODY["error"]
    assert REFUSAL_BODY["session_id"] == SESSION.id
    print(f"   409, no job (runner still holds {JOBS_AFTER}); "
          f"names {[e['step_id'] for e in REFUSAL_BODY['uncommitted_steps']]}")
    print(f"   message: {REFUSAL_BODY['error'][:110]}...")
    print("   PASS")

    # And a session that was NEVER finished is refused the same way -- the
    # reopen above is one route into the state, a fresh session is the other.
    FRESH = Session()
    FRESH_HTTP = Http(FRESH)
    FRESH_REFUSAL = FRESH_HTTP.report()
    assert FRESH_REFUSAL.status_code == 409, FRESH_REFUSAL.get_json()
    assert [e["step_id"] for e in FRESH_REFUSAL.get_json()["uncommitted_steps"]] == list(
        design_document.STEP_ORDER
    )
    assert len(FRESH.runner) == 0, "a never-started session's report creates no job either"
    print(f"   a fresh session: 409 naming all {len(design_document.STEP_ORDER)} steps, no job")

    # An unknown session is a 404 and not a 409 -- the store read happens
    # first, and "this session does not exist" is a different answer from
    # "this session is not finished".
    UNKNOWN = HTTP.report(session_id="no-such-session-id")
    assert UNKNOWN.status_code == 404, (UNKNOWN.status_code, UNKNOWN.get_json())
    print("   an unknown session id: 404, not 409")

    # Recommit everything so the rest of the file has a reportable session.
    DOCUMENT = commit_everything(SESSION)

    # ==================================================================
    # 1 + 5. THE HAPPY PATH, AND THE BYTES
    # ==================================================================
    print("\n[test 1]. A FULLY COMMITTED SESSION: 202, polled to a PDF")

    with ClaudeStub() as CLAUDE:
        ACCEPTED = HTTP.report(property_label="5614 N Montour Rd, Gibsonia, PA 15044")
        assert ACCEPTED.status_code == 202, (ACCEPTED.status_code, ACCEPTED.get_json())
        ACCEPTED_BODY = ACCEPTED.get_json()
        assert sorted(ACCEPTED_BODY) == ["job_id", "status"], ACCEPTED_BODY
        assert ACCEPTED_BODY["status"] == job_runner.STATUS_RUNNING, ACCEPTED_BODY

        STARTED = time.time()
        DONE, POLLS = HTTP.poll(ACCEPTED_BODY["job_id"])
        REPORT_SECONDS = time.time() - STARTED

    assert DONE["status"] == job_runner.STATUS_DONE, DONE
    RESULT = DONE["result"]
    assert sorted(RESULT) == ["download_url", "filename", "report_id", "size_bytes"], RESULT
    assert RESULT["download_url"] == f"/api/reports/{RESULT['report_id']}", RESULT
    assert RESULT["filename"] == "scale-of-permanence-report.pdf", RESULT
    assert CLAUDE.calls == 1, f"the narrative is generated exactly once, not {CLAUDE.calls}"
    print(f"   202 -> {POLLS} polls over {REPORT_SECONDS:.1f}s -> {RESULT['download_url']} "
          f"({RESULT['size_bytes'] / 1024:.0f} KB)")
    print("   PASS")

    print("\n[test 5]. THE PDF IS A REAL PDF -- the bytes, not just a 200")

    DOWNLOAD = HTTP.download(RESULT["download_url"])
    assert DOWNLOAD.status_code == 200, DOWNLOAD.status_code
    PDF_BYTES = DOWNLOAD.data

    # THE HEADER AND THE TRAILER. A 200 carrying an HTML error page, a
    # truncated write or an empty file all pass "status_code == 200"; none
    # of them passes both of these.
    assert PDF_BYTES[:5] == b"%PDF-", PDF_BYTES[:32]
    assert b"%%EOF" in PDF_BYTES[-2048:], PDF_BYTES[-64:]
    assert len(PDF_BYTES) == RESULT["size_bytes"], (len(PDF_BYTES), RESULT["size_bytes"])
    # REAL PAGES: the cover, the narrative, and the full-bleed map page.
    PDF_OBJECTS = pdf_objects(PDF_BYTES)
    PAGE_COUNT = len(re.findall(rb"/Type\s*/Page[^s]", PDF_OBJECTS))
    assert PAGE_COUNT >= 3, f"a report is a cover, narrative pages and a map page; found {PAGE_COUNT}"
    # AND THE MAP PAGE IS A REAL IMAGE: an embedded image XObject, which is
    # the rendered layout map and nothing else on this document carries one.
    assert re.search(rb"/Subtype\s*/Image", PDF_OBJECTS), (
        "the final page must carry the rendered layout map as an embedded image"
    )
    # SERVED AS A DOWNLOAD, with the name a browser saves.
    assert DOWNLOAD.mimetype == "application/pdf", DOWNLOAD.mimetype
    DISPOSITION = DOWNLOAD.headers.get("Content-Disposition", "")
    assert "attachment" in DISPOSITION and "scale-of-permanence-report.pdf" in DISPOSITION, DISPOSITION
    print(f"   {len(PDF_BYTES)} bytes, %PDF- header, %%EOF trailer, {PAGE_COUNT} pages, an embedded map image")
    print(f"   Content-Type {DOWNLOAD.mimetype}; Content-Disposition {DISPOSITION}")
    print("   PASS")

    # The same URL fetched twice serves the same file -- a reload of the
    # download, or a second tab, is not a 404 the first fetch caused.
    AGAIN = HTTP.download(RESULT["download_url"])
    assert AGAIN.status_code == 200 and AGAIN.data == PDF_BYTES, "a second fetch serves the same PDF"
    # An id this process does not hold is a 404, never a 500.
    assert HTTP.download("/api/reports/not-a-real-report-id").status_code == 404
    print("   the link is re-fetchable; an unknown report id is a 404")

    # ==================================================================
    # 4. A CLAUDE FAILURE AND AN IMAGERY FAILURE
    # ==================================================================
    print("\n[test 4]. A CLAUDE FAILURE and an IMAGERY FAILURE poll to a failed job that does not "
          "suggest retrying differently")

    FAILURE_BODIES = {}

    # (a) THE CLAUDE CALL. A RuntimeError is what report_generator raises
    # for a missing key and what the SDK surfaces for an API failure.
    with ClaudeStub(raises=RuntimeError("Claude API request failed: 529 overloaded")):
        accepted = HTTP.report()
        assert accepted.status_code == 202, accepted.get_json()
        FAILURE_BODIES["claude"], _ = HTTP.poll(accepted.get_json()["job_id"])

    # (b) THE IMAGERY. render_layout_map() swallows a basemap TILE outage by
    # design (it falls back to a neutral fill), so the failure modelled here
    # is the render itself going down -- the case that actually reaches the
    # job.
    with ClaudeStub(), mock_patch.object(
        __import__("generate_pdf_report"),
        "render_layout_map",
        side_effect=OSError("imagery pipeline unavailable"),
    ):
        accepted = HTTP.report()
        assert accepted.status_code == 202, accepted.get_json()
        FAILURE_BODIES["imagery"], _ = HTTP.poll(accepted.get_json()["job_id"])

    for kind, body in FAILURE_BODIES.items():
        assert body["status"] == job_runner.STATUS_FAILED, (kind, body)
        error = body["error"]
        # IT NAMES ITSELF as the kind the user cannot act on.
        assert error["report_failed"] == {"actionable": False}, (kind, error)
        assert "session_expired" not in error, (kind, error)
        assert error["error"] == session_report.GENERATION_FAILED, (kind, error)
        # IT DOES NOT INVITE A DIFFERENT ATTEMPT. There is nothing different
        # to do, and copy that implies otherwise sends the user back through
        # their design looking for a mistake they did not make.
        prose = error["error"].lower()
        for forbidden in ("try again", "retry", "reopen", "recommit", "different", "check your"):
            assert forbidden not in prose, f"{kind}: the message must not say {forbidden!r} -- {prose}"
        # AND IT DOES NOT BLAME THE DESIGN.
        assert "unharmed" in prose, (kind, prose)
        # NEVER A TRACEBACK: the raiser's own text stays server-side.
        assert "529" not in json.dumps(error) and "imagery pipeline" not in json.dumps(error), error
    print(f"   claude:  {FAILURE_BODIES['claude']['error']['error']}")
    print(f"   imagery: same prose, same report_failed {FAILURE_BODIES['imagery']['error']['report_failed']}")
    print("   PASS")

    # ==================================================================
    # 3. AN EXPIRED SESSION
    # ==================================================================
    print("\n[test 3]. AN EXPIRED SESSION fails with WORKING_DATA_EXPIRED verbatim, distinguishable "
          "from a source failure")

    # THE EVICTION, exactly as the LRU/idle-timeout would do it: the tier-2
    # entry is dropped and the Design Document is untouched, so the session
    # still reads as fully committed and the report is still accepted.
    assert SESSION.cache.discard(SESSION.id) is True
    assert SESSION.stored()["steps"]["water"]["status"] == design_document.STATUS_COMMITTED

    with ClaudeStub() as EXPIRED_CLAUDE:
        accepted = HTTP.report()
        # STILL A 202. The precondition is the document's, and the document
        # is intact -- the eviction is only discoverable by reading tier 2,
        # which is the job's work.
        assert accepted.status_code == 202, accepted.get_json()
        EXPIRED_BODY, _ = HTTP.poll(accepted.get_json()["job_id"])

    assert EXPIRED_BODY["status"] == job_runner.STATUS_FAILED, EXPIRED_BODY
    EXPIRED_ERROR = EXPIRED_BODY["error"]
    # THE SENTENCE, VERBATIM, ON THE WIRE. Not paraphrased by the route and
    # not re-worded here: it is session_design's own constant, written to be
    # shown to the person who has to act on it.
    assert session_design.WORKING_DATA_EXPIRED in EXPIRED_ERROR["error"], EXPIRED_ERROR
    assert (
        session_design.WORKING_DATA_EXPIRED
        == "this session's working data has expired; reopen and recommit to generate a report"
    )
    # IT NAMES ITSELF, and the remedy is a KEY rather than prose to parse.
    assert EXPIRED_ERROR["session_expired"]["remedy"] == "reopen_and_recommit", EXPIRED_ERROR
    assert EXPIRED_ERROR["session_expired"]["session_id"] == SESSION.id, EXPIRED_ERROR
    assert EXPIRED_ERROR["session_expired"]["step_id"] in design_document.STEP_ORDER, EXPIRED_ERROR
    assert "report_failed" not in EXPIRED_ERROR, EXPIRED_ERROR
    # THE CLAUDE CALL NEVER RAN. The design build raises before it -- so an
    # expired session costs nothing and, more importantly, the failure is
    # the eviction rather than whatever the narrative would have done.
    assert EXPIRED_CLAUDE.calls == 0, "an expired session must fail BEFORE the Claude call"

    # DISTINGUISHABLE FROM A SOURCE FAILURE BY A KEY, not by prose -- the
    # whole point, and the assertion that proves a client can branch.
    assert set(EXPIRED_ERROR) & {"session_expired", "report_failed"} == {"session_expired"}
    for kind, body in FAILURE_BODIES.items():
        assert set(body["error"]) & {"session_expired", "report_failed"} == {"report_failed"}, kind
        assert body["error"]["error"] != EXPIRED_ERROR["error"], kind
    print(f"   message: {EXPIRED_ERROR['error'][:130]}...")
    print(f"   session_expired {EXPIRED_ERROR['session_expired']}; the Claude call never ran")
    print("   PASS")


# ======================================================================
# 6. THE BATCH PATH IS UNTOUCHED
# ======================================================================
print("\n[test 6]. generate_full_report() and build_pipeline_context() are UNCHANGED; the batch path still works")

import inspect  # noqa: E402

import generate_pdf_report  # noqa: E402
import pipeline_context  # noqa: E402

assert generate_full_report.build_pipeline_context is pipeline_context.build_pipeline_context
assert list(inspect.signature(generate_full_report.generate_full_report).parameters) == [
    "boundary_coordinates", "anchor_lon_lat",
]
assert list(inspect.signature(pipeline_context.build_pipeline_context).parameters)[:2] == [
    "boundary_coordinates", "anchor_lon_lat",
]
assert "context.narrative_data" in inspect.getsource(generate_full_report.generate_full_report)
# The session entry point kept its own signature too -- report_from_design()
# is its BODY, split at one seam, not a second entry point that replaced it.
assert list(inspect.signature(generate_full_report.generate_session_report).parameters) == [
    "session_id", "store", "fetch_cache", "cache",
]
assert "report_from_design" in inspect.getsource(generate_full_report.generate_session_report)

# THE BATCH PDF ENTRY POINT: same name, same signature, and it still calls
# the batch narrative and the batch layer fetch -- neither of which the
# session path touches.
assert list(inspect.signature(generate_pdf_report.generate_full_report_pdf).parameters) == [
    "boundary_coordinates", "output_path", "anchor_lon_lat", "property_label", "map_image_path",
]
BATCH_SOURCE = inspect.getsource(generate_pdf_report.generate_full_report_pdf)
assert "generate_full_report(boundary_coordinates, anchor_lon_lat)" in BATCH_SOURCE, BATCH_SOURCE
assert "fetch_layout_layers(boundary_coordinates" in BATCH_SOURCE, BATCH_SOURCE
assert "session" not in BATCH_SOURCE.lower(), "the batch path did not grow a session mode"

# AND THE SHARED HALF ACTUALLY ASSEMBLES. _write_pdf() is what both entry
# points end on, and it is exercised here against the same stub narrative
# and a real PNG -- so "the batch path still works" is a PDF on disk rather
# than a signature check.
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

with tempfile.TemporaryDirectory() as tmpdir:
    map_png = os.path.join(tmpdir, "map.png")
    figure = plt.figure(figsize=(8.5, 11))
    figure.savefig(map_png, dpi=60)
    plt.close(figure)
    batch_pdf = generate_pdf_report._write_pdf(
        STUB_NARRATIVE, map_png, os.path.join(tmpdir, "batch.pdf"), "A Batch Property"
    )
    batch_bytes = open(batch_pdf, "rb").read()
    assert batch_bytes[:5] == b"%PDF-" and b"%%EOF" in batch_bytes[-2048:]
    assert len(re.findall(rb"/Type\s*/Page[^s]", pdf_objects(batch_bytes))) >= 3, "cover, narrative, map"
    print(f"   the shared assembly still writes a real PDF ({len(batch_bytes)} bytes) for the batch path")
print("   PASS")


# ======================================================================
# 7. THE REPORT IS NOT A SEVENTH STEP
# ======================================================================
print("\n[test 7]. THE REPORT IS NOT A STEP -- no STEP_ORDER entry, no registry entry, no document status")

assert design_document.STEP_ORDER == (
    "landform", "water", "roads", "trees", "structures", "fencing",
), design_document.STEP_ORDER
assert "report" not in design_document.STEP_ORDER
assert "report" not in design_document.VALID_STATUSES
try:
    step_registry.get_step("report")
    raise AssertionError("the registry must have no 'report' step")
except step_registry.RegistryError:
    pass
FRESH_DOCUMENT = design_document.create_document([[0.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
assert sorted(FRESH_DOCUMENT["steps"]) == sorted(design_document.STEP_ORDER)
assert design_document.document_body(FRESH_DOCUMENT)["step_order"] == list(design_document.STEP_ORDER)
print(f"   STEP_ORDER is still {len(design_document.STEP_ORDER)} steps; no registry entry; "
      f"no status named 'report'")
print("   PASS")


print("\n" + "=" * 72)
print(
    "test_session_report_route.py: POST /api/sessions/<id>/report is a JOB over a\n"
    "FULLY COMMITTED session; an uncommitted step is a synchronous 409 with no job;\n"
    "an expired session and a source failure are told apart by the key each carries;\n"
    "and the thing the client downloads is a real PDF. The report is not a step."
)
print("=" * 72)
