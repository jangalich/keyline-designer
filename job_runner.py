"""
job_runner.py

The JOB RESOURCE behind every asynchronous step
(interactive-design-architecture-proposal.md section 3.1). `generate` and
`report` carry DEM-wide computation -- scoring passes, Dijkstra runs, a
Claude call -- so they do not run inside a request; they become a job, and
the client comes back for the answer.

TRANSPORT-AGNOSTIC, WHICH IS THE ONE STRUCTURAL REQUIREMENT. Section 3.1
names two transports for the same resource -- 202 + polling first, SSE or
websockets as a later upgrade -- and states that the upgrade "should change
only the client". Nothing in this module knows which one is in use: there is
no long-poll, no timeout-until-the-client-asks-again, no queue of undelivered
events, no notion of a subscriber at all. A job is a value that changes state
and can be read; polling reads it repeatedly and SSE would push on change,
and neither of those is this module's business. A `wait()` exists for tests
and for a synchronous caller, and is NOT how a transport should consume this.

STATUS IS THREE VALUES. running | done | failed. There is no "queued": the
executor accepts work immediately and a caller cannot act differently on the
distinction, so publishing it would be publishing an implementation detail as
a contract. A job is running from the moment submit() returns.

THE ERROR IS A PAYLOAD, NOT A TRACEBACK. `error` is a JSON-serialisable dict
the client renders -- for a generate that is /api/production-zones' own shape,
{"error": prose, "failed_layer": {"type", "label"} | None}. The exception
itself is kept on the job object as `exception` for server-side logging and
is never part of get_job()'s result. Same reasoning as production_zone_
payload.LayerFetchError's: a rasterio traceback in a user-facing panel tells
the reader nothing they can act on, and the layer identity is the only part
of the failure they can respond to.

IN-PROCESS, DELIBERATELY. One Flask process today (see api.py). A
ThreadPoolExecutor, an in-memory registry, and a lock. Nothing here survives
a restart and nothing here needs to -- a lost job is a regenerate, and
generate is idempotent by contract, which is exactly what makes losing one
safe. The moment this runs under more than one process the registry moves to
Redis and this module's interface does not change; that is what the three
methods are sized for.

BOUNDED, BOTH WAYS. Concurrency is capped (a generate is CPU-heavy and
unbounded threads would thrash), and finished jobs are evicted oldest-first
past a cap, so a long-lived process does not accumulate every payload it ever
produced. A job evicted before it was read reports as unknown, which a client
handles the same way it handles an unknown id.
"""

import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
VALID_STATUSES = (STATUS_RUNNING, STATUS_DONE, STATUS_FAILED)

# A generate is a DEM-wide compute pass, not an IO wait. More threads than
# this on one Flask process makes every job slower without making any of them
# finish sooner.
DEFAULT_MAX_WORKERS = 4

# How many finished jobs are retained. Generous -- a payload is tens of
# kilobytes -- but finite, so a process that has served ten thousand
# generates is not still holding the first one.
DEFAULT_MAX_JOBS = 256


class JobNotFoundError(KeyError):
    """No job with that id: never submitted, or evicted after finishing."""


@dataclass
class Job:
    """
    One unit of asynchronous work.

    The lock is the job's OWN, not the runner's: a reader asking for one
    job's state must not wait behind an unrelated job's completion. `event`
    is what wait() blocks on, set exactly once when the job leaves running.

    result and error are mutually exclusive by construction -- _finish() sets
    one and only one -- so a client never has to decide which to believe.
    """

    id: str
    status: str = STATUS_RUNNING
    result: Any = None
    error: Optional[dict] = None
    # The raw exception, for server-side logging ONLY. Never serialised, never
    # returned by get_job(). See the module docstring.
    exception: Optional[BaseException] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def snapshot(self) -> dict:
        """
        THE WIRE SHAPE: {status, result | error}, per section 3.1's
        `GET /api/jobs/{job_id}`.

        The absent half is OMITTED rather than sent as null. A running job
        has no result yet and a failed one has no result at all; shipping
        "result": null for both would make them indistinguishable from a
        step that legitimately produced nothing, which is the same
        null-versus-absent distinction design_document.py's status field
        exists to keep straight.
        """
        with self._lock:
            snap = {"job_id": self.id, "status": self.status}
            if self.status == STATUS_DONE:
                snap["result"] = self.result
            elif self.status == STATUS_FAILED:
                snap["error"] = self.error
            return snap

    def wait(self, timeout: Optional[float] = None) -> "Job":
        """
        Block until this job leaves `running`. FOR TESTS AND SYNCHRONOUS
        CALLERS -- a transport must not use this (see the module docstring):
        a polling endpoint that blocks here is no longer polling, and an SSE
        one would hold a thread per subscriber.
        """
        self._event.wait(timeout)
        return self

    # --- internal state transitions ---

    def _finish(self, status: str, result=None, error=None, exception=None) -> None:
        with self._lock:
            self.status = status
            self.result = result
            self.error = error
            self.exception = exception
            self.finished_at = time.time()
        # Set OUTSIDE the lock: a waiter wakes and immediately calls
        # snapshot(), which takes the same lock.
        self._event.set()


class JobRunner:
    """
    Submits work, keeps the jobs, hands back their state. Three methods.

    submit() NEVER RAISES FOR A FAILING JOB. An exception inside the work
    function becomes that job's `failed` state, which is the entire point of
    a job resource -- a caller that has already been handed a job id must be
    able to find out what happened by asking, not by having had the
    submission itself blow up in a different request.
    """

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_jobs: int = DEFAULT_MAX_JOBS,
    ):
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        if max_jobs < 1:
            raise ValueError(f"max_jobs must be >= 1, got {max_jobs}")
        self._max_jobs = max_jobs
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="keyline-job"
        )
        self._jobs = OrderedDict()  # job_id -> Job, insertion order
        self._lock = threading.Lock()

    def submit(
        self,
        work: Callable[[], Any],
        on_error: Optional[Callable[[BaseException], dict]] = None,
    ) -> Job:
        """
        Run `work()` on the pool and return its Job, already `running`.

        `on_error` turns a raised exception into the CLIENT-FACING error
        payload. It is supplied by the caller rather than fixed here because
        this module does not know what a failure means -- step_orchestrator.py
        does, and it reads the answer off the step's own registry entry. An
        on_error that is itself broken must not lose the original failure, so
        its own exception is caught and the job still fails, with a generic
        payload.
        """
        job = Job(id=secrets.token_urlsafe(12))
        self._register(job)

        def run():
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 -- a job never leaks
                self._fail(job, exc, on_error)
            else:
                job._finish(STATUS_DONE, result=result)

        self._executor.submit(run)
        return job

    @staticmethod
    def _fail(job: Job, exc: BaseException, on_error) -> None:
        payload = None
        if on_error is not None:
            try:
                payload = on_error(exc)
            except BaseException:  # noqa: BLE001
                payload = None
        if not isinstance(payload, dict):
            payload = {"error": "This job failed."}
        job._finish(STATUS_FAILED, error=payload, exception=exc)

    def _register(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._evict_locked()

    def _evict_locked(self) -> None:
        # Oldest FINISHED job first. A running job is never evicted, however
        # old: dropping it would leave a client polling an id that will never
        # resolve, which is worse than holding one more entry.
        while len(self._jobs) > self._max_jobs:
            evictable = next(
                (
                    job_id
                    for job_id, job in self._jobs.items()
                    if job.status != STATUS_RUNNING
                ),
                None,
            )
            if evictable is None:
                return
            del self._jobs[evictable]

    def job(self, job_id: str) -> Job:
        """The Job object. Raises JobNotFoundError."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job

    def get_job(self, job_id: str) -> dict:
        """
        {status, result | error} for one job -- section 3.1's job endpoint,
        as a plain dict. Raises JobNotFoundError for an id this runner does
        not hold, rather than returning a synthetic "failed": a job that never
        existed and a job that failed are different answers and a client acts
        differently on them (retry the poll versus show the error).
        """
        return self.job(job_id).snapshot()

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)


# The process-wide default, in the same shape session_cache.py's two default
# caches take: callers that have their own runner (a test, a future
# multi-process arrangement) pass it; everyone else gets this one.
DEFAULT_JOB_RUNNER = JobRunner()


def get_job(job_id: str, runner: Optional[JobRunner] = None) -> dict:
    """
    Module-level convenience over DEFAULT_JOB_RUNNER.get_job().

    `is None`, never `or`: JobRunner defines __len__, so a runner holding no
    jobs is falsy and `or` would read the DEFAULT runner instead of the one
    the caller passed -- see step_orchestrator.generate_step()'s note and
    session_cache.py's, which documents the same trap for its cache classes.
    """
    if runner is None:
        runner = DEFAULT_JOB_RUNNER
    return runner.get_job(job_id)
