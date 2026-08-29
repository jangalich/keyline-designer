"""
test_job_runner.py

The job resource's own contract, run as:

    python test_job_runner.py

NO PIPELINE HERE. This is the transport-agnostic job resource in isolation --
the state machine, the error payload, thread safety, and the bounds. The job
lifecycle AS A GENERATE uses it is section 8 of test_step_orchestrator.py;
this file asserts the properties that must hold for any work at all, which is
what "transport-agnostic" and "thread-safe" actually mean.

Sections:
  1. LIFECYCLE -- running -> done; running -> failed; the snapshot shape.
  2. THE ERROR IS A PAYLOAD -- on_error builds it; the exception stays off
     the wire; a broken on_error still fails the job.
  3. THREAD SAFETY -- many concurrent jobs, each landing on its own answer;
     readers polling throughout see only valid states.
  4. BOUNDS -- the finished-job cap evicts oldest-first and never evicts a
     running job.
  5. UNKNOWN IDS -- raise rather than reporting a synthetic failure.
  6. THE FALSY-RUNNER TRAP -- an EMPTY runner is falsy (JobRunner defines
     __len__), so every call site must test `is None`.
"""

import threading
import time

import job_runner

# --- 1. LIFECYCLE -----------------------------------------------------

runner = job_runner.JobRunner(max_workers=4, max_jobs=64)

started = threading.Event()
release = threading.Event()


def _blocking_work():
    started.set()
    release.wait(30)
    return {"answer": 42}


job = runner.submit(_blocking_work)
assert started.wait(10), "the executor must accept work immediately"
running = runner.get_job(job.id)
assert running["status"] == job_runner.STATUS_RUNNING, running
assert "result" not in running and "error" not in running, (
    f"a running job carries neither half: {sorted(running)}"
)

release.set()
job.wait(10)
done = runner.get_job(job.id)
assert done["status"] == job_runner.STATUS_DONE, done
assert done["result"] == {"answer": 42}
assert "error" not in done, "a done job carries no error key"
assert done["job_id"] == job.id

failing = runner.submit(
    lambda: (_ for _ in ()).throw(ValueError("upstream is down")),
    on_error=lambda exc: {"error": "It did not work.", "failed_layer": None},
).wait(10)
failed = runner.get_job(failing.id)
assert failed["status"] == job_runner.STATUS_FAILED, failed
assert failed["error"] == {"error": "It did not work.", "failed_layer": None}
assert "result" not in failed, "a failed job carries no result key"

assert set(job_runner.VALID_STATUSES) == {"running", "done", "failed"}, (
    "three statuses; there is no 'queued' a caller could act on"
)

print(
    f"1. LIFECYCLE: a job observed RUNNING mid-work (carrying neither result "
    f"nor error), then DONE carrying only its result; a raising job went "
    f"FAILED carrying only its error. Statuses are exactly "
    f"{job_runner.VALID_STATUSES}."
)


# --- 2. THE ERROR IS A PAYLOAD ----------------------------------------

boom = ValueError("a rasterio traceback nobody can act on")
classified = runner.submit(
    lambda: (_ for _ in ()).throw(boom),
    on_error=lambda exc: {"error": "The elevation data could not be retrieved."},
).wait(10)

assert classified.error == {"error": "The elevation data could not be retrieved."}
assert str(boom) not in str(classified.error), (
    "the raw exception text must not reach the wire"
)
assert classified.exception is boom, (
    "the original exception stays ON THE JOB for server-side logging"
)
assert "exception" not in runner.get_job(classified.id), (
    "and is never part of the snapshot"
)

# NO on_error AT ALL: the job still fails, with a generic payload rather than
# leaking anything.
bare = runner.submit(lambda: (_ for _ in ()).throw(RuntimeError("x"))).wait(10)
assert bare.status == job_runner.STATUS_FAILED
assert bare.error == {"error": "This job failed."}

# A BROKEN on_error must not lose the failure it was there to describe --
# that would turn a failed job into one that never resolves.
broken = runner.submit(
    lambda: (_ for _ in ()).throw(RuntimeError("real failure")),
    on_error=lambda exc: (_ for _ in ()).throw(KeyError("on_error is broken")),
).wait(10)
assert broken.status == job_runner.STATUS_FAILED, broken.snapshot()
assert broken.error == {"error": "This job failed."}
assert isinstance(broken.exception, RuntimeError), (
    "the ORIGINAL exception is kept, not on_error's own"
)

# An on_error returning a non-dict is a bug in the caller, not a reason to
# put a non-serialisable value on the wire.
wrong_shape = runner.submit(
    lambda: (_ for _ in ()).throw(RuntimeError("x")),
    on_error=lambda exc: "just a string",
).wait(10)
assert wrong_shape.error == {"error": "This job failed."}

print(
    f"2. THE ERROR IS A PAYLOAD: on_error's dict is what the job carries; the "
    f"exception text never reaches it and the exception itself stays off the "
    f"snapshot. A missing, raising, or wrong-shaped on_error still leaves a "
    f"FAILED job with a serialisable error."
)


# --- 3. THREAD SAFETY -------------------------------------------------
#
# Many jobs at once, each with its own answer, while readers poll throughout.
# The failure this catches is shared mutable state in the runner: a reader
# seeing another job's result, or a status outside the three.

JOB_COUNT = 40
concurrent_runner = job_runner.JobRunner(max_workers=8, max_jobs=256)
gate = threading.Barrier(2)
observed_statuses = set()
poll_errors = []
stop_polling = threading.Event()


def _work_for(index):
    def work():
        # A little real contention rather than an instant return.
        time.sleep(0.01)
        return {"index": index}
    return work


jobs = [concurrent_runner.submit(_work_for(i)) for i in range(JOB_COUNT)]
expected_index = {j.id: i for i, j in enumerate(jobs)}


def _poller():
    gate.wait(30)
    while not stop_polling.is_set():
        for j in jobs:
            try:
                snapshot = concurrent_runner.get_job(j.id)
            except Exception as exc:  # noqa: BLE001
                poll_errors.append(exc)
                return
            observed_statuses.add(snapshot["status"])
            # THE READ THAT MATTERS: a job id must resolve to ITS OWN result,
            # mid-flight, while 39 other jobs are finishing around it. Shared
            # mutable state in the runner shows up here as one job's id
            # returning another's answer.
            if snapshot["status"] == job_runner.STATUS_DONE:
                if snapshot["result"]["index"] != expected_index[j.id]:
                    poll_errors.append(
                        AssertionError(
                            f"job {j.id} returned index "
                            f"{snapshot['result']['index']}, expected "
                            f"{expected_index[j.id]}"
                        )
                    )
                    return


poller = threading.Thread(target=_poller, daemon=True)
poller.start()
gate.wait(30)
for j in jobs:
    j.wait(60)
stop_polling.set()
poller.join(30)

assert not poll_errors, f"a concurrent reader saw an error: {poll_errors[:3]}"
assert observed_statuses <= set(job_runner.VALID_STATUSES), (
    f"a reader saw a status outside the contract: {observed_statuses}"
)
for index, j in enumerate(jobs):
    snapshot = concurrent_runner.get_job(j.id)
    assert snapshot["status"] == job_runner.STATUS_DONE, snapshot
    assert snapshot["result"] == {"index": index}, (
        f"job {index} came back with another job's answer: {snapshot['result']}"
    )
assert len(concurrent_runner) == JOB_COUNT

print(
    f"3. THREAD SAFETY: {JOB_COUNT} jobs run concurrently on 8 workers while a "
    f"reader polls every one of them throughout; every job returned its OWN "
    f"answer, every observed status was in {job_runner.VALID_STATUSES} "
    f"(saw {sorted(observed_statuses)}), and no read raised."
)


# --- 4. BOUNDS --------------------------------------------------------

bounded = job_runner.JobRunner(max_workers=2, max_jobs=3)
finished = [bounded.submit(lambda: "ok").wait(10) for _ in range(5)]
assert len(bounded) == 3, f"the finished-job cap must hold: {len(bounded)}"
# Oldest first: the two earliest are gone, the three most recent remain.
for evicted in finished[:2]:
    try:
        bounded.get_job(evicted.id)
    except job_runner.JobNotFoundError:
        pass
    else:
        raise AssertionError("the oldest finished jobs must be evicted first")
for kept in finished[2:]:
    assert bounded.get_job(kept.id)["status"] == job_runner.STATUS_DONE

# A RUNNING job is never evicted, however old -- a client polling an id that
# will never resolve is worse than holding one more entry.
holding = job_runner.JobRunner(max_workers=4, max_jobs=2)
hold_release = threading.Event()
hold_started = threading.Event()


def _held():
    hold_started.set()
    hold_release.wait(30)
    return "held"


long_running = holding.submit(_held)
assert hold_started.wait(10)
for _ in range(6):
    holding.submit(lambda: "quick").wait(10)
assert holding.get_job(long_running.id)["status"] == job_runner.STATUS_RUNNING, (
    "a running job must survive the cap"
)
hold_release.set()
long_running.wait(10)

print(
    f"4. BOUNDS: a runner capped at 3 finished jobs holds {len(bounded)} after "
    f"5, evicting the two oldest; a still-RUNNING job survived 6 later "
    f"submissions against a cap of 2."
)


# --- 5. UNKNOWN IDS ---------------------------------------------------

for unknown in ("never-submitted", "", finished[0].id):
    try:
        bounded.get_job(unknown)
    except job_runner.JobNotFoundError:
        continue
    raise AssertionError(f"an unknown job id must raise, got a result for {unknown!r}")

print(
    "5. UNKNOWN IDS: a never-submitted id, an empty id and an evicted id each "
    "raise JobNotFoundError rather than reporting a synthetic 'failed' -- a "
    "job that never existed and a job that failed are different answers."
)


# --- 6. THE FALSY-RUNNER TRAP -----------------------------------------
#
# JobRunner defines __len__, so an EMPTY runner is FALSY. Any call site
# written `runner or DEFAULT_JOB_RUNNER` silently reads the process-wide
# default on exactly the first use of a fresh runner -- the same trap
# session_cache.py documents for its two cache classes, and one this branch
# hit for real. Asserted here as the property, and at the call sites through
# test_step_orchestrator.py section 8, which asks a fresh runner for the id it
# was just handed.

empty = job_runner.JobRunner()
assert len(empty) == 0 and not empty, (
    "an empty JobRunner is falsy; every call site must test `is None`"
)
first = empty.submit(lambda: "first").wait(10)
assert empty.get_job(first.id)["result"] == "first", (
    "the first job submitted to a fresh runner must be held BY THAT RUNNER"
)
try:
    job_runner.DEFAULT_JOB_RUNNER.get_job(first.id)
except job_runner.JobNotFoundError:
    pass
else:
    raise AssertionError(
        "a job submitted to an explicit runner must not land in the default one"
    )
assert job_runner.get_job(first.id, empty)["result"] == "first", (
    "the module-level get_job() must honour an explicit empty runner too"
)

print(
    "6. THE FALSY-RUNNER TRAP: an empty JobRunner is falsy; a job submitted to "
    "a fresh runner is held by it, is absent from DEFAULT_JOB_RUNNER, and is "
    "reachable through the module-level get_job() with that runner passed."
)

runner.shutdown()
concurrent_runner.shutdown()
bounded.shutdown()
holding.shutdown()
empty.shutdown()

print("\nAll job_runner checks passed.")
