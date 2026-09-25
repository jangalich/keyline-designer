"""
test_session_report_route.py

THE SITE DATA REPORT, ASKED FOR OVER HTTP, FOR A SESSION THE OWNER COMMITTED.

Offline, through the real routes on the real parcel. A session is created,
every step generated and committed exactly as a client would, and then
POST /api/sessions/<id>/report is called on Flask's own test client and
polled to a downloadable PDF.

THE JOB PRODUCES THE SITE DATA REPORT (site_report.generate_session_site_
report_pdf) -- the narrated generator it replaced, and its Claude call, are
retired. WHAT IS MOCKED IS THE NETWORK, AND NOTHING ELSE: offline_harness.
install() refuses every outbound request, fencing_step_fixture.Harness
patches the Layer 1 fetch boundaries the session path reaches, and the
REPORT LAYER is served through the route's own report_fetch_cache seam
(session_api.Dependencies) from the reference fixtures
(overview_reference_fixture.report_data()) -- counted, and made to fail on
demand. Everything else runs: the real session context, eight real
sections, the real WeasyPrint pass.

THE SEVEN CASES:

  1. A FULLY COMMITTED SESSION reports: 202 with a job id, polled to a
     result carrying a download URL, fetched to a PDF; the report layer is
     fetched ONCE. With no property label the cover carries the parcel's
     coordinates -- never an invented placeholder title.
  2. A SESSION WITH ANY STEP UNCOMMITTED IS REFUSED SYNCHRONOUSLY -- 409
     naming what is missing, and NO JOB IS CREATED.
  3. AN EVICTED SESSION REPORTS. The session cache's entry dropped, and
     then the Layer 1 fetch cache too -- the user who comes back the next
     day -- and the report still completes: the context is rebuilt from the
     Design Document, and nothing answers `session_expired`.
  4. A REQUIRED REPORT-LAYER FAILURE (Daymet) polls to a failed job
     carrying `failed_layer`; ANY OTHER FAILURE polls to GENERATION_FAILED.
     Neither suggests retrying differently, neither leaks the raiser's text,
     and neither names a narrative service that no longer exists.
  5. THE PDF IS A REAL PDF -- the %PDF- header, the %%EOF trailer, the
     page count, and the Content-Type and Content-Disposition it was
     served under.
  6. THE NARRATED REPORT IS GONE: its modules are not importable and the
     job names only the site report.
  7. THE REPORT IS NOT A SEVENTH STEP: STEP_ORDER is untouched, the
     registry has no report entry, and no document grows a report status.
"""

import importlib.util
import inspect
import json
import re
import time
import zlib
from unittest.mock import patch as mock_patch

import offline_harness

offline_harness.install()

import design_document  # noqa: E402
import job_runner  # noqa: E402
import overview_reference_fixture  # noqa: E402
import report_data  # noqa: E402
import session_api  # noqa: E402
import session_cache  # noqa: E402
import session_report  # noqa: E402
import site_report  # noqa: E402
import step_registry  # noqa: E402
from fencing_step_fixture import Harness, Session  # noqa: E402

POLL_INTERVAL_SECONDS = 0.05
POLL_TIMEOUT_SECONDS = 900.0

# THE REPORT LAYER, from the reference fixtures -- built once: every report
# in this file is on the same boundary, and a ReportData is a value.
#
# THREE LAYERS DEGRADED, because this session is not the reference session.
# The commits here run the real orchestrator on fencing_step_fixture's
# SYNTHETIC bench-and-drainage DEM (108x98 cells) and its synthetic SSURGO
# rows; the report fixtures were captured on the real DEM (108x96) and the
# real survey. NLCD land cover and the forest type group are rasters on the
# DEM grid and cannot be laid over a different one, and the survey's
# components are keyed to map units this session does not have. Each is
# passed as absent -- the degraded path every section already renders --
# so this file tests the route and the job; the whole report's content on
# the reference session is test_whole_report.py's subject.
FIXTURE_REPORT_DATA = overview_reference_fixture.report_data(
    nlcd_landcover=None, forest_type_group=None, soil_survey_rows=None,
)


class ReportLayer:
    """
    The report layer behind the route's report_fetch_cache seam: a real
    session_cache.FetchCache over a fetch function that serves the fixture
    ReportData, COUNTS every fetch, and raises `raises` when set. A real
    cache, so "fetched once per boundary" is the cache's own behaviour and
    a failure is not cached -- exactly as in production.
    """

    def __init__(self, raises=None):
        self.raises = raises
        self.calls = 0
        self.cache = session_cache.FetchCache(fetch_function=self._fetch)

    def _fetch(self, boundary):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return FIXTURE_REPORT_DATA


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


class Http:
    """
    Flask's test client over session_api's own blueprint, bound to a
    Session's OWN store, caches and runner.

    THE SAME DEPENDENCIES THE COMMITS WENT THROUGH. A route reading a
    different store than the fixture committed into would make every
    assertion below about an empty session, and the failure would look like
    a precondition bug.
    """

    def __init__(self, session, report_layer=None):
        self.session = session
        self.reports = session_report.ReportStore(max_reports=8)
        self.report_layer = report_layer or ReportLayer()
        self.deps = session_api.Dependencies(
            store=session.store,
            fetch_cache=session.fetch_cache,
            cache=session.cache,
            runner=session.runner,
            reports=self.reports,
            report_fetch_cache=self.report_layer.cache,
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
print("test_session_report_route.py -- the site data report, over HTTP, for a committed session")
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
    assert HTTP.report_layer.calls == 0, "a refused report fetches no report layer"
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
    print("\n[test 1]. A FULLY COMMITTED SESSION: 202, polled to the site data report")

    LABELS = []
    _real_cover_label = site_report.cover_label

    def _recording_cover_label(data, label):
        LABELS.append(_real_cover_label(data, label))
        return LABELS[-1]

    with mock_patch.object(site_report, "cover_label", _recording_cover_label):
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
    assert RESULT["filename"] == "site-data-report.pdf", RESULT
    assert HTTP.report_layer.calls == 1, f"the report layer is fetched exactly once, not {HTTP.report_layer.calls}"
    assert LABELS == ["5614 N Montour Rd, Gibsonia, PA 15044"], LABELS
    print(f"   202 -> {POLLS} polls over {REPORT_SECONDS:.1f}s -> {RESULT['download_url']} "
          f"({RESULT['size_bytes'] / 1024:.0f} KB); report layer fetched {HTTP.report_layer.calls}x")
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
    # REAL PAGES: the cover and eight sections run to twenty-odd pages; a
    # render that lost a section, or the narrated document's handful, would
    # not reach this.
    PDF_OBJECTS = pdf_objects(PDF_BYTES)
    PAGE_COUNT = len(re.findall(rb"/Type\s*/Page[^s]", PDF_OBJECTS))
    assert PAGE_COUNT >= 20, f"the site data report is twenty-odd pages; found {PAGE_COUNT}"
    # SERVED AS A DOWNLOAD, with the name a browser saves.
    assert DOWNLOAD.mimetype == "application/pdf", DOWNLOAD.mimetype
    DISPOSITION = DOWNLOAD.headers.get("Content-Disposition", "")
    assert "attachment" in DISPOSITION and "site-data-report.pdf" in DISPOSITION, DISPOSITION
    print(f"   {len(PDF_BYTES)} bytes, %PDF- header, %%EOF trailer, {PAGE_COUNT} pages")
    print(f"   Content-Type {DOWNLOAD.mimetype}; Content-Disposition {DISPOSITION}")
    print("   PASS")

    # The same URL fetched twice serves the same file -- a reload of the
    # download, or a second tab, is not a 404 the first fetch caused.
    AGAIN = HTTP.download(RESULT["download_url"])
    assert AGAIN.status_code == 200 and AGAIN.data == PDF_BYTES, "a second fetch serves the same PDF"
    # An id this process does not hold is a 404, never a 500.
    assert HTTP.download("/api/reports/not-a-real-report-id").status_code == 404
    print("   the link is re-fetchable; an unknown report id is a 404")

    # NO LABEL: THE COVER CARRIES THE COORDINATES. The frontend sends none,
    # and the route used to substitute "Property Design Report" -- a
    # placeholder title that would print on a site data report's cover. A
    # second report on the same boundary is also the report layer's cache
    # hit: no second fetch.
    print("\n[test 1b]. NO PROPERTY LABEL: the cover prints the parcel's coordinates")
    LABELS.clear()
    with mock_patch.object(site_report, "cover_label", _recording_cover_label):
        accepted = HTTP.report()
        assert accepted.status_code == 202, accepted.get_json()
        UNLABELLED, _ = HTTP.poll(accepted.get_json()["job_id"])
    assert UNLABELLED["status"] == job_runner.STATUS_DONE, UNLABELLED
    assert len(LABELS) == 1 and re.fullmatch(r"\d+\.\d{4}° N, \d+\.\d{4}° W", LABELS[0]), LABELS
    assert "Report" not in LABELS[0]
    assert HTTP.report_layer.calls == 1, "the second report on the same land is a report-layer cache hit"
    print(f"   cover label: {LABELS[0]}; report layer still fetched {HTTP.report_layer.calls}x")
    print("   PASS")

    # ==================================================================
    # 4. A REQUIRED-LAYER FAILURE, AND ANY OTHER FAILURE
    # ==================================================================
    print("\n[test 4]. A REQUIRED REPORT-LAYER FAILURE carries failed_layer; ANY OTHER failure is "
          "GENERATION_FAILED; neither suggests retrying differently")

    FAILURE_BODIES = {}

    # (a) DAYMET, the one REQUIRED report layer, did not answer. A new
    # report-layer cache (a failure is never cached; a fresh one also means
    # no earlier success can be served).
    DAYMET_DOWN = report_data.ReportDataIncompleteError(
        "report layer 'daymet_daily' (climate records) failed: 503 from daymet.ornl.gov",
        *report_data.LAYER_CLIMATE,
        report_data.ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE,
    )
    FAILING_HTTP = Http(SESSION, report_layer=ReportLayer(raises=DAYMET_DOWN))
    accepted = FAILING_HTTP.report()
    assert accepted.status_code == 202, accepted.get_json()
    FAILURE_BODIES["daymet"], _ = FAILING_HTTP.poll(accepted.get_json()["job_id"])

    # (b) EVERYTHING ELSE: the renderer itself going down.
    with mock_patch.object(site_report, "generate_site_report_pdf",
                           side_effect=OSError("weasyprint: libpango went away")):
        accepted = HTTP.report()
        assert accepted.status_code == 202, accepted.get_json()
        FAILURE_BODIES["renderer"], _ = HTTP.poll(accepted.get_json()["job_id"])

    for kind, body in FAILURE_BODIES.items():
        assert body["status"] == job_runner.STATUS_FAILED, (kind, body)
        error = body["error"]
        assert "session_expired" not in error, (kind, error)
        assert "report_failed" in error, (kind, error)
        prose = error["error"].lower()
        # IT DOES NOT INVITE A DIFFERENT ATTEMPT, AND DOES NOT BLAME THE DESIGN.
        for forbidden in ("try again", "reopen", "recommit", "different", "check your"):
            assert forbidden not in prose, f"{kind}: the message must not say {forbidden!r} -- {prose}"
        assert "unharmed" in prose, (kind, prose)
        # NOR DOES IT NAME WHAT IS NOT THERE. The narrated report's message
        # blamed "the narrative service or the map imagery"; neither exists.
        assert "narrative" not in prose and "imagery" not in prose, (kind, prose)
        # NEVER A TRACEBACK: the raiser's own text stays server-side.
        assert "503" not in json.dumps(error) and "libpango" not in json.dumps(error), (kind, error)

    DAYMET_ERROR = FAILURE_BODIES["daymet"]["error"]
    assert DAYMET_ERROR["failed_layer"] == {
        "type": "climate", "label": "climate records",
        "reason": report_data.ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE,
    }, DAYMET_ERROR
    assert DAYMET_ERROR["report_failed"] == {"actionable": True}, DAYMET_ERROR
    RENDERER_ERROR = FAILURE_BODIES["renderer"]["error"]
    assert RENDERER_ERROR == {"error": session_report.GENERATION_FAILED, "report_failed": {"actionable": False}}
    print(f"   daymet:   {DAYMET_ERROR['error']}")
    print(f"             failed_layer {DAYMET_ERROR['failed_layer']}")
    print(f"   renderer: {RENDERER_ERROR['error']}")
    print("   PASS")

    # ==================================================================
    # 3. AN EVICTED SESSION REPORTS
    # ==================================================================
    print("\n[test 3]. AN EVICTED SESSION -- session cache and Layer 1 fetch cache both dropped -- "
          "still reports")

    # THE EVICTION, exactly as the LRU/idle-timeout would do it: the tier-2
    # entry is dropped and the Design Document is untouched. And the Layer 1
    # fetch cache too, so the rebuild refetches Layer 1 -- the next-day case.
    assert SESSION.cache.discard(SESSION.id) is True
    SESSION.fetch_cache.clear()
    assert SESSION.stored()["steps"]["water"]["status"] == design_document.STATUS_COMMITTED
    REBUILDS = []
    _real_rebuild = session_cache.rebuild_session_context

    def _recording_rebuild(document, fetch_cache):
        REBUILDS.append(document["session_id"])
        return _real_rebuild(document, fetch_cache)

    with mock_patch.object(session_cache, "rebuild_session_context", _recording_rebuild):
        accepted = HTTP.report()
        assert accepted.status_code == 202, accepted.get_json()
        EVICTED, _ = HTTP.poll(accepted.get_json()["job_id"])

    assert EVICTED["status"] == job_runner.STATUS_DONE, EVICTED
    assert REBUILDS == [SESSION.id], f"the context must be rebuilt from the document, once: {REBUILDS}"
    assert SESSION.id in SESSION.cache, "the rebuilt context is back in the session cache"
    EVICTED_PDF = HTTP.download(EVICTED["result"]["download_url"]).data
    assert EVICTED_PDF[:5] == b"%PDF-" and len(re.findall(rb"/Type\s*/Page[^s]", pdf_objects(EVICTED_PDF))) >= 20
    print(f"   rebuilt once from the Design Document; {len(EVICTED_PDF)} bytes; no session_expired")
    print("   PASS")


# ======================================================================
# 6. THE NARRATED REPORT IS GONE
# ======================================================================
print("\n[test 6]. THE NARRATED REPORT IS GONE: not importable, and the job names only the site report")

for retired in ("generate_full_report", "generate_pdf_report", "report_generator", "render_layout_map"):
    assert importlib.util.find_spec(retired) is None, f"{retired} is still importable"
JOB_SOURCE = inspect.getsource(session_report.run_report_job)
assert "site_report.generate_session_site_report_pdf(" in JOB_SOURCE, JOB_SOURCE
print("   four modules retired; run_report_job calls site_report.generate_session_site_report_pdf")
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
    "FULLY COMMITTED session producing the SITE DATA REPORT; an uncommitted step is a\n"
    "synchronous 409 with no job; an evicted session rebuilds and reports; a required-\n"
    "layer failure carries failed_layer; and the download is a real PDF. Not a step."
)
print("=" * 72)
