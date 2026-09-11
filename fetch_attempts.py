"""
fetch_attempts.py

WHAT A RETRY LOOP COST, PUBLISHED. One convention, used by every fetch
module that retries, so a layer's cost in ATTEMPTS -- and in the seconds
it spent asleep between them -- is a fact the record can read instead of
a number somebody infers from elapsed time.

THE HOLE THIS FILLS. Five modules behind the twelve fetch layers retry
internally, each keeping its own private copy of the same progressive-
timeout loop:

    for attempt in range(max_retries + 1):
        timeout = 30 + (attempt * 30)
        try:
            ...
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise

`attempt` is a local. It dies with the frame. A layer that succeeded on
its third try after two two-second pauses returned exactly what a layer
that succeeded on its first try returned, and no caller -- including
run_diagnostics.py, which is in the business of saying what a creation
waited on -- could tell the two apart. A 34.9-second farm_roads fetch and
a 1.3-second one on the same parcel minutes apart were both consistent
with "one slow request" and with "three requests and two sleeps", and the
record could not say which.

THE READING SIDE ALREADY EXISTED AND WAS NEVER FED. run_diagnostics.py
publishes a contract -- a fetch module that wants its attempts recorded
sets them on itself under ATTEMPTS_ATTRIBUTE, and the layer timer reads
that attribute and nothing else. Nothing set it. This module is what
sets it, for all five modules at once.

ONE CONVENTION, NOT FIVE
========================
Three pieces, and a sixth module joins by using them:

  1. `for attempt in fetch_attempts.attempts(max_retries):` in place of
     `for attempt in range(max_retries + 1):`. Yields the same integers
     in the same order -- it IS a range when nobody is listening -- and
     counts each attempt as it starts.

  2. `fetch_attempts.sleep(seconds)` in place of `time.sleep(seconds)`
     between attempts. Sleeps exactly as before and records how long it
     actually slept.

  3. `@fetch_attempts.publishes` on the LAYER ENTRY POINT -- the public
     function parcel_data.py calls and run_diagnostics.py times. It opens
     the ledger the two above write into, and publishes that ledger's
     totals on the module when the call returns.

Plus one line of plumbing per module, which is what makes the published
values per-thread rather than process-global:

    def __getattr__(name):
        return fetch_attempts.published(__name__, name)

WHY THE ENTRY POINT AND NOT THE HELPER. A LAYER'S count must describe the
LAYER. farm_roads queries three road layers per fetch and imagery calls
its `_retry` three times (one search, two band reads); a count published
by the helper would describe whichever helper wrote last, and the record
would report the final band read as the whole imagery layer's cost. The
ledger is opened once per entry-point call and every helper underneath it
adds into that one ledger, so `attempts` is the layer's total. The
per-helper breakdown is kept beside it (DETAIL_ATTRIBUTE) rather than
instead of it, because "three layers queried once each" and "one layer
queried three times" are different problems that share a total of 3.

WHY A THREAD-LOCAL AND A MODULE __getattr__, NOT A PLAIN ATTRIBUTE.
The contract says the reader does `getattr(module, "LAST_FETCH_ATTEMPTS")`
and expects an int. A plain module attribute would make that int process-
global, and two sessions created concurrently on different boundaries
(job_runner runs them on a pool) would overwrite each other's counts
between the call returning and the timer reading -- the record would then
carry another parcel's number, silently. So nothing is ever set ON the
module. The ledger's totals live in a thread-local, and PEP 562's module-
level `__getattr__` -- which Python calls only when a normal attribute
lookup FAILS -- hands back the CALLING THREAD's value. The reader is
unchanged and does not know: it asks the module and gets an int.

STALENESS IS STRUCTURALLY UNREACHABLE, which the fetch cache makes worth
saying out loud. A warm creation is served by session_cache.FetchCache
without entering fetch_parcel_data() at all, so no layer timer fires and
no row exists to carry a stale count (the record's `layers` is null, not
twelve zeroes). Underneath that, the published values are CLEARED on
entry to a decorated call and set on exit, so within a thread the value
is always the most recently COMPLETED call's; and a thread that never
made the call has nothing published at all, so the reader's `getattr`
misses and the row honestly says "not published". A count can therefore
only ever be read by the timer that wrapped the call that produced it.

WHAT IT COSTS WHEN NOBODY IS LISTENING. This module does not ask whether
diagnostics are enabled, and deliberately: a publisher that depended on
its reader would invert the layering the contract is built on, and the
switch it would read is a per-process environment variable while the cost
it would save is nanoseconds. With no ledger open -- every call to these
helpers from outside a decorated entry point -- `attempts()` returns a
real `range` after one thread-local lookup and `sleep()` is `time.sleep`
after one more, which is what every non-fetch caller of these modules
pays. Inside a decorated entry point the ledger is a dict of small dicts
touched once per attempt plus one `sys._getframe` per helper CALL (not
per attempt), against a network request with a 30-second timeout. Test 7
of test_fetch_attempts.py measures it rather than asserting it.

NOTHING HERE RETRIES, SLEEPS LONGER, OR DECIDES ANYTHING. `attempts()`
yields `range(max_retries + 1)`'s integers and no others; `sleep()` sleeps
the seconds it is given; `publishes` returns what the function returned
and re-raises what it raised. The budgets, the backoff and the timeouts
are exactly where they were.
"""

import functools
import sys
import threading
import time

# THE THREE PUBLISHED NAMES. The first is run_diagnostics.ATTEMPTS_
# ATTRIBUTE's own value, spelled out here rather than imported: this
# module is the PUBLISHER and must not depend on the reader (see the
# module docstring). test_fetch_attempts.py asserts the two spellings
# agree, so the duplication cannot drift silently.
ATTEMPTS_ATTRIBUTE = "LAST_FETCH_ATTEMPTS"

# Milliseconds this layer spent inside sleep() between attempts, MEASURED
# by the loop that slept rather than derived from elapsed time. This is
# the figure run_diagnostics could not produce from outside: the retry
# loops sit inside the layer call, so a layer's elapsed_ms already
# contains every attempt plus every pause with no boundary an outside
# observer can see. The loop itself can see it.
SLEEP_ATTRIBUTE = "LAST_FETCH_RETRY_SLEEP_MS"

# The same call broken out per helper, plus how it ended. See _detail().
DETAIL_ATTRIBUTE = "LAST_FETCH_ATTEMPT_DETAIL"

PUBLISHED_ATTRIBUTES = (ATTEMPTS_ATTRIBUTE, SLEEP_ATTRIBUTE, DETAIL_ATTRIBUTE)

# THE PAUSE BETWEEN ATTEMPTS, in seconds. Every retry loop behind the
# fetch layers sleeps for this long between one failed attempt and the
# next -- the literal 2 the loops used to carry each in their own copy.
# One name so a test can shorten it (test_fetch_attempts.py measures the
# pause; it does not need the pause to be two seconds long to do so) and
# so an offline harness can zero it: an unreachable host costs three
# connection refusals and nothing else, instead of three refusals and four
# seconds asleep per fetch. The budget, the backoff and the progressive
# timeouts are unchanged; only the length of the pause is a name now.
RETRY_PAUSE_SECONDS = 2.0

# The calling thread's open ledger (`ledger`) and its published totals
# (`published`, a {module name: {attribute: value}}). Both thread-local
# for the module docstring's reason.
_LOCAL = threading.local()


class _Ledger:
    """
    One entry-point call's running total, written by the helpers under it.

    `parent` is the ledger this one displaced, if a decorated entry point
    was called from inside another decorated entry point. On close the
    child folds its totals into the parent, so an outer layer's count
    stays the layer's TOTAL rather than only the part its own frame made.
    """

    __slots__ = ("attempts", "sleep_ms", "helpers", "parent")

    def __init__(self, parent=None):
        self.attempts = 0
        self.sleep_ms = 0.0
        self.helpers = {}
        self.parent = parent

    def _row(self, helper) -> dict:
        row = self.helpers.get(helper)
        if row is None:
            row = {"calls": 0, "attempts": 0, "sleep_ms": 0.0}
            self.helpers[helper] = row
        return row

    def begin(self, helper) -> None:
        """One helper CALL started -- not one attempt. Three road layers
        queried once each and one queried three times both total 3
        attempts and are told apart here."""
        self._row(helper)["calls"] += 1

    def attempt(self, helper) -> None:
        self.attempts += 1
        self._row(helper)["attempts"] += 1

    def slept(self, helper, milliseconds) -> None:
        self.sleep_ms += milliseconds
        self._row(helper)["sleep_ms"] += milliseconds

    def fold_into_parent(self) -> None:
        parent = self.parent
        if parent is None:
            return
        parent.attempts += self.attempts
        parent.sleep_ms += self.sleep_ms
        for helper, row in self.helpers.items():
            into = parent._row(helper)
            into["calls"] += row["calls"]
            into["attempts"] += row["attempts"]
            into["sleep_ms"] += row["sleep_ms"]


def _helper_name(depth: int) -> str:
    """
    `farm_roads_data._query_road_layer` -- the function that is retrying,
    READ OFF ITS OWN FRAME rather than passed in as a string. A name
    passed in would be a second copy of the function's name, and the one
    thing a second copy does reliably is drift from the first.

    Resolved ONCE PER HELPER CALL, not once per attempt: `attempts()`
    asks for it before the loop starts and hands it to the ledger from
    there on.
    """
    frame = sys._getframe(depth)
    module = frame.f_globals.get("__name__", "?")
    return f"{module}.{frame.f_code.co_qualname}"


def attempts(max_retries: int, helper=None):
    """
    `range(max_retries + 1)`, counted.

        for attempt in fetch_attempts.attempts(max_retries):
            timeout = 30 + (attempt * 30)
            ...

    Yields the same integers in the same order, so the loop body, the
    progressive timeout, the `break` on success and the `raise` on the
    last attempt all keep working exactly as written. An attempt is
    counted when it STARTS, which is what makes the count right whether
    the loop breaks out, returns from inside, or falls off the end
    raising: a loop that broke on its first pass made one attempt.

    WITH NO LEDGER OPEN THIS IS A `range`. Helpers called outside a
    decorated entry point -- the geojson entry points, anything a test
    calls directly -- pay one thread-local lookup and get the builtin.

    `helper` names the retrying function; it is read off the caller's own
    frame when not given, which is how it is always used.
    """
    ledger = getattr(_LOCAL, "ledger", None)
    if ledger is None:
        return range(max_retries + 1)
    return _counted(ledger, max_retries, _helper_name(2) if helper is None else helper)


def _counted(ledger, max_retries: int, helper: str):
    ledger.begin(helper)
    for attempt in range(max_retries + 1):
        ledger.attempt(helper)
        yield attempt


def sleep(seconds: float, helper=None) -> None:
    """
    `time.sleep(seconds)`, timed.

    The pause between attempts is most of what a retry costs against a
    server that is merely slow, and it is the half run_diagnostics.py
    said it could not separate from success time: the loops are inside
    the layer call with no boundary an outside observer can see. THE LOOP
    CAN SEE IT. What is recorded is the MEASURED elapsed time of this
    sleep, not the seconds asked for -- `time.sleep` is a floor, and a
    loaded machine can overshoot it.

    Sleeps for exactly as long as before either way; the clock is the
    only addition.
    """
    ledger = getattr(_LOCAL, "ledger", None)
    if ledger is None:
        time.sleep(seconds)
        return
    name = _helper_name(2) if helper is None else helper
    started = time.perf_counter()
    try:
        time.sleep(seconds)
    finally:
        ledger.slept(name, (time.perf_counter() - started) * 1000.0)


def _detail(function, ledger, outcome, value) -> dict:
    """
    The layer's call, broken out.

    `returned_sentinel` is a SHAPE, not a judgement: True when the call
    returned None, which is the documented "nothing found" return of
    canopy_height_data.get_canopy_height_for_boundary() and imagery_data.
    get_imagery_summary_for_boundary() (parcel_data.py then hard-fails on
    it, outside the timer, and its own outcome block says so). It is null
    when the call raised, because a call that raised returned nothing to
    describe. Every other entry point here returns a list or a dict and
    this is simply False for them -- an empty list from a query that
    matched nothing is the source's answer, not a sentinel, and this
    module does not reinterpret it.
    """
    return {
        "entry_point": f"{function.__module__}.{function.__qualname__}",
        "attempts": ledger.attempts,
        "sleep_ms": ledger.sleep_ms,
        "outcome": outcome,
        "returned_sentinel": None if outcome != "ok" else value is None,
        # {helper: {calls, attempts, sleep_ms}} -- which helper under this
        # layer did the retrying, and whether it was one call retrying or
        # several calls each succeeding first time.
        "helpers": {helper: dict(row) for helper, row in ledger.helpers.items()},
    }


def _store(module_name: str, values) -> None:
    published = getattr(_LOCAL, "published", None)
    if published is None:
        published = _LOCAL.published = {}
    published[module_name] = values


def publishes(function):
    """
    Decorator for a LAYER ENTRY POINT: opens the ledger every retrying
    helper underneath this call writes into, and publishes its totals on
    the module when the call returns.

    PUBLISHED ON BOTH PATHS. The totals are written in a `finally`, so a
    layer that raised still reports the attempts it burned getting there
    -- which is the case the record exists for, since twelve of the
    twelve layers hard-fail the session and the record is then the only
    evidence the run happened.

    CLEARED ON ENTRY. The published values are set to None before the
    call and to the totals after it, so a read taken mid-call cannot see
    the previous call's number (the reader treats a non-int as "not
    published" and says so). Together with the ledger being thread-local
    this is what makes a stale count unreachable -- see the module
    docstring.

    Returns what the function returned and raises what it raised. It
    never swallows: a recorder that turned a working fetch into a failure
    would be a worse bug than any it was added to find.
    """
    module_name = function.__module__

    @functools.wraps(function)
    def publishing(*args, **kwargs):
        previous = getattr(_LOCAL, "ledger", None)
        ledger = _Ledger(previous)
        _LOCAL.ledger = ledger
        _store(module_name, dict.fromkeys(PUBLISHED_ATTRIBUTES))
        outcome = "raised"
        value = None
        try:
            value = function(*args, **kwargs)
            outcome = "ok"
            return value
        finally:
            _LOCAL.ledger = previous
            ledger.fold_into_parent()
            _store(
                module_name,
                {
                    ATTEMPTS_ATTRIBUTE: ledger.attempts,
                    SLEEP_ATTRIBUTE: ledger.sleep_ms,
                    DETAIL_ATTRIBUTE: _detail(function, ledger, outcome, value),
                },
            )

    return publishing


# The qualified name the decorator's wrapper compiles to. Named here so
# a check can ask a LOADED callable whether it is wrapped, rather than
# grepping a checkout that a long-running server may have imported an
# older copy of -- run_diagnostics.self_check()'s standard, and the
# silent-failure mode this convention has: an entry point that lost its
# decorator raises nothing and simply reports no count, forever.
PUBLISHING_WRAPPER = "publishes.<locals>.publishing"


def publishes_attempts(function) -> bool:
    """Whether this LOADED callable is wrapped by publishes() -- read off
    its own code object, not off the source. See PUBLISHING_WRAPPER."""
    code = getattr(function, "__code__", None)
    return getattr(code, "co_qualname", None) == PUBLISHING_WRAPPER


def published(module_name: str, attribute: str):
    """
    The CALLING THREAD's published value for one of the three names, for
    a publishing module's own `__getattr__` to hand back:

        def __getattr__(name):
            return fetch_attempts.published(__name__, name)

    PEP 562 calls a module's `__getattr__` only when a normal attribute
    lookup fails, so this runs exactly when somebody asks for a name the
    module does not otherwise define -- which is every read of the three
    published names, since nothing is ever set on the module.

    Raises AttributeError for anything else, and for a thread that has
    not completed one of this module's decorated calls. That is the right
    answer and not an evasion: `getattr(module, ..., None)` is what the
    reader does, and a miss there records "not published by <module>",
    which is exactly true of a thread that never made the call.
    """
    if attribute not in PUBLISHED_ATTRIBUTES:
        raise AttributeError(f"module {module_name!r} has no attribute {attribute!r}")
    published_values = getattr(_LOCAL, "published", None) or {}
    values = published_values.get(module_name)
    if values is None or attribute not in values:
        raise AttributeError(
            f"module {module_name!r} has not published {attribute!r} on this thread"
        )
    return values[attribute]


def clear(module_name=None) -> None:
    """
    Forget what this thread published -- all of it, or one module's.

    FOR TESTS AND FOR LONG-LIVED WORKER THREADS, not for the pipeline: a
    published value is already unreachable as a stale count (see the
    module docstring), so nothing on the fetch path needs to call this.
    What it buys a test is the ability to prove that -- a cached creation
    can be run against a thread with nothing published and shown to
    record no counts rather than an earlier fetch's.
    """
    published_values = getattr(_LOCAL, "published", None)
    if not published_values:
        return
    if module_name is None:
        published_values.clear()
    else:
        published_values.pop(module_name, None)
