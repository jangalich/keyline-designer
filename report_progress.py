"""
report_progress.py

HOW FAR THROUGH A REPORT THE JOB IS -- completed work over total work, and
what is being worked on now.

    plan_session_report(...)     -> the units a run WILL do, fixed up front
    ReportProgress(units)        -> the counter one report job ticks
    active(progress)             -> binds it to the running job's context
    stage(key) / unit(key)       -> context managers the pipeline wraps
    tick(layer) / settle(stage)  -> completion, from inside the pipeline
    drawing                      -> decorator for an SVG renderer

THE BAR MUST NEVER LIE. Progress is the summed weight of COMPLETED units over
the summed weight of the PLAN. It is never elapsed time over an estimate: a
fetch that hangs for forty seconds holds the fraction exactly where it was
for forty seconds, and the label keeps naming what is outstanding. That
stillness is the honest report, and nothing here smooths it.

--- THE PLAN IS FIXED BEFORE ANY WORK STARTS ---------------------------

plan_session_report() is called once, before the first fetch, with what the
three caches hold at that moment:

  the report-layer cache   holds this boundary -> no report fetches planned
  the session cache        holds this session  -> no rebuild planned
  the Layer 1 fetch cache  holds this boundary -> a rebuild is warm-up only

So a warm run's total never includes a rebuild it will not do (the bar
would otherwise sit short of 100 and jump at the end), and a cold run's
total includes it from the first poll (the bar would otherwise have to
grow its denominator mid-run, which is a bar going BACKWARDS). The total
is set once; set_plan() refuses a second call.

A CACHE CAN CHANGE BETWEEN THE PLAN AND THE CALL -- another request fills
the report cache, the idle timer evicts the session. Neither can move the
bar backwards: a tick for a unit the plan does not hold is ignored, and a
stage that finishes with planned units never ticked (because someone else
did the work) SETTLES them, since the stage's work is then genuinely done.

--- COUNTED, NOT SEQUENCED ---------------------------------------------

Every unit has a key, and completion is membership in a set. "Seven of the
twenty report fetches have completed" survives any order the fetches run
in; "now on layer 7" would not survive them running at once. The
report-layer fetch IS concurrent since branch 31 (report_data.fetch_
report_data runs its layers on worker threads), and this module was
shaped so that branch changed nothing here: tick() and the in-flight
table are under one lock, and the label is read off whatever is still
outstanding rather than off a position. The one thing the branch did
touch is abandon(): it now remembers every unit an exception left, in
order, so fail() can name the required layer that stopped the run even
when degradable layers raised (and then completed, degraded) around it.

THE LABEL NAMES THE LONGEST-OUTSTANDING UNIT. Sequentially that was just
the one running; concurrently it is the one everything else is waiting
behind -- the stall a person is looking at the bar to understand.

--- A CONTEXTVAR, NOT A THREAD-LOCAL, AND THAT IS DELIBERATE ------------

A threading.local would have looked perfectly adequate when the report
job ran its fetches on one pool thread, and it would have been wrong the
day they went concurrent: a worker thread started by a ThreadPoolExecutor
sees an EMPTY thread-local, so every concurrent fetch would tick nothing
and the bar would sit still through the whole fetch stage and then jump.
A ContextVar is carried into those workers with contextvars.copy_
context().run(...), which is the one line report_data added. run_
diagnostics' fetch probe made the same move for the same reason, so the
per-layer timing rows ride the same copied context.

--- WIRE SHAPE ---------------------------------------------------------

snapshot() is what GET /api/jobs/<id> carries under "progress", in EVERY
state -- running, done and failed. A failed job keeps the bar where the
run stopped and the label on what it was doing, rather than resetting or
completing:

    {"fraction": 0.0..1.0, "percent": 0..100, "completed": n, "total": n,
     "fetches": {"completed": n, "total": n},
     "stage": "records" | "rebuild" | "terrain" | "maps" | "pages" | None,
     "detail": <kind of data> | None,
     "failed": bool}

`stage` and `detail` are KEYS, not prose. The words the user reads are the
frontend's (ReportProgress.jsx), as every other sentence in that UI is; a
layer's module name (nhdplus_hr, soil_woodland) never reaches the wire.

`percent` is FLOORED, so it reads 100 only when every unit is done -- a
bar showing 100 over a job still laying out pages is the one lie a
rounding step could tell.
"""

import contextlib
import contextvars
import functools
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

# ---------------------------------------------------------------------
# Stage keys. The frontend owns the words; these are the contract.
# ---------------------------------------------------------------------

STAGE_RECORDS = "records"   # the 20 report-layer fetches
STAGE_REBUILD = "rebuild"   # a cold session context: Layer 1 + warm-up
STAGE_TERRAIN = "terrain"   # section inputs and the eight section builders
STAGE_MAPS = "maps"         # an SVG map or chart is being drawn (a label only)
STAGE_PAGES = "pages"       # HTML, WeasyPrint layout, PDF write
STAGES = (STAGE_RECORDS, STAGE_REBUILD, STAGE_TERRAIN, STAGE_MAPS, STAGE_PAGES)

# ---------------------------------------------------------------------
# What each report-layer fetch is, as a KIND OF DATA -- the sub-label the
# fetch stage shows. Named for what the data is about, never the service
# or the module: several layers share a kind on purpose (five SSURGO
# queries are all "the soil survey" to the person waiting). test_report_
# progress.py asserts this covers report_data.REPORT_FETCH_LAYERS exactly.
# ---------------------------------------------------------------------

KIND_CLIMATE = "climate"
KIND_STORMS = "storms"
KIND_STREAMS = "streams"
KIND_WETLANDS = "wetlands"
KIND_FLOOD = "flood"
KIND_LAND_COVER = "land_cover"
KIND_SOIL = "soil"
KIND_FOREST = "forest"
KIND_GEOLOGY = "geology"
KIND_AERIAL = "aerial"
KIND_SURROUNDINGS = "surroundings"
KIND_COUNTY = "county"
KIND_BUILDINGS = "buildings"

REPORT_LAYER_KINDS = {
    "daymet_daily": KIND_CLIMATE,
    "atlas14": KIND_STORMS,
    "power_wind": KIND_CLIMATE,
    "nhdplus_hr": KIND_STREAMS,
    "nwi": KIND_WETLANDS,
    "fema_nfhl": KIND_FLOOD,
    "nlcd_landcover": KIND_LAND_COVER,
    "soil_water_table": KIND_SOIL,
    "soil_road_ratings": KIND_SOIL,
    "forest_type_group": KIND_FOREST,
    "soil_woodland": KIND_SOIL,
    "soil_survey": KIND_SOIL,
    "bedrock_geology": KIND_GEOLOGY,
    "naip_imagery": KIND_AERIAL,
    "context_dem": KIND_SURROUNDINGS,
    # The context map's water is streams, whatever map it is drawn on.
    "context_water": KIND_STREAMS,
    "context_roads": KIND_SURROUNDINGS,
    "county_state": KIND_COUNTY,
    "structures": KIND_BUILDINGS,
    "transmission_lines": KIND_BUILDINGS,
}

# ---------------------------------------------------------------------
# THE WEIGHTS, from diagnose_report_generation_time.py's measured shares
# on a warm run: the report-layer fetches ~80%, the section derivations
# with their SVGs ~10%, WeasyPrint ~10% -- which diagnose_report_progress.py
# reproduced live (80 / 9 / 11 of a 50 s warm run). A cold run's rebuild
# measured 8.3 s of a 52.6 s run (16%), nearly all of it the Layer 1
# refetch and under a second the warm-up; the audit's figure was ~13 s.
# 20 on this scale is 17% of the cold total, 17 of it Layer 1.
#
# WEIGHTS SIZE THE STEPS; THEY DO NOT MOVE THE BAR. Only a completion
# moves it. A wrong weight makes one step larger or smaller than the time
# it took, and never makes the bar move while nothing completes.
# ---------------------------------------------------------------------

WEIGHT_RECORDS = 80.0
WEIGHT_TERRAIN = 10.0
WEIGHT_PAGES_HTML = 1.0
WEIGHT_PAGES_LAYOUT = 7.0
WEIGHT_PAGES_WRITE = 2.0
WEIGHT_REBUILD_LAYER1 = 17.0
WEIGHT_REBUILD_WARM_UP = 3.0

WARM_UP_UNIT = "terrain_warm_up"

# The section inputs and builders a session report runs, in the order
# site_report runs them. Design only when every step is committed.
INPUT_UNITS = ("terrain", "water", "access", "trees", "soils", "overview")
SECTION_UNITS = ("overview", "climate", "landform", "water", "access", "trees", "soils")
PAGES_UNITS = (("html", WEIGHT_PAGES_HTML), ("layout", WEIGHT_PAGES_LAYOUT), ("write", WEIGHT_PAGES_WRITE))


@dataclass(frozen=True)
class Unit:
    stage: str
    name: str
    weight: float
    kind: Optional[str] = None

    @property
    def key(self) -> str:
        return unit_key(self.stage, self.name)


def unit_key(stage: str, name: str) -> str:
    return f"{stage}:{name}"


def plan_session_report(
    report_layers: Iterable[str],
    layer1_layers: Iterable[str],
    report_cached: bool,
    context_cached: bool,
    layer1_cached: bool,
    design: bool,
) -> list:
    """
    Every unit one session report will do, given what the caches hold now.

    `report_layers` is report_data.REPORT_FETCH_LAYERS' keys and
    `layer1_layers` parcel_data.FETCH_LAYERS -- passed rather than imported
    so this module stays importable without the fetch graph (and so a
    test can plan without it).
    """
    units = []
    if not report_cached:
        layers = list(report_layers)
        for layer in layers:
            units.append(Unit(STAGE_RECORDS, layer, WEIGHT_RECORDS / len(layers), REPORT_LAYER_KINDS.get(layer)))
    if not context_cached:
        if not layer1_cached:
            layers = list(layer1_layers)
            for layer in layers:
                units.append(Unit(STAGE_REBUILD, layer, WEIGHT_REBUILD_LAYER1 / len(layers)))
        units.append(Unit(STAGE_REBUILD, WARM_UP_UNIT, WEIGHT_REBUILD_WARM_UP))
    inputs = list(INPUT_UNITS) + (["design"] if design else [])
    sections = list(SECTION_UNITS) + (["design"] if design else [])
    terrain = [f"inputs.{name}" for name in inputs] + [f"section.{name}" for name in sections]
    for name in terrain:
        units.append(Unit(STAGE_TERRAIN, name, WEIGHT_TERRAIN / len(terrain)))
    for name, weight in PAGES_UNITS:
        units.append(Unit(STAGE_PAGES, name, weight))
    return units


class ReportProgress:
    """
    One report job's progress. Thread-safe: every mutation and every read
    is under one lock, so a poll on the request thread reads a consistent
    snapshot while the job thread -- or, later, several fetch workers --
    tick it.

    Created UNPLANNED when the job is submitted (0 of 0, no stage), so the
    first poll has something to read; the job plans it before its first
    unit of work.
    """

    def __init__(self, units: Optional[list] = None, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._units = {}
        self._order = []
        self._total = 0.0
        self._planned = False
        self._completed = set()
        self._done_weight = 0.0
        self._inflight = {}          # key -> start time
        self._drawing = 0            # SVG renders in flight
        self._stage = None
        self._failed = False
        self._frozen = None          # (stage, detail) at failure
        self._abandoned = []         # units an exception left, in order, never completed since
        if units is not None:
            self.set_plan(units)

    # --- the plan ---

    def set_plan(self, units: list) -> None:
        with self._lock:
            if self._planned:
                raise RuntimeError("a report's plan is fixed once; set_plan() was called twice")
            for unit in units:
                if unit.key in self._units:
                    raise ValueError(f"duplicate unit {unit.key!r} in the plan")
                if unit.weight <= 0:
                    raise ValueError(f"unit {unit.key!r} has weight {unit.weight}")
                self._units[unit.key] = unit
                self._order.append(unit.key)
            self._total = sum(unit.weight for unit in units)
            self._planned = True

    @property
    def planned(self) -> bool:
        with self._lock:
            return self._planned

    # --- where the work is ---

    def enter_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def begin(self, key: str) -> None:
        with self._lock:
            if key in self._units and key not in self._completed:
                self._inflight.setdefault(key, self._clock())

    def complete(self, key: str) -> None:
        """
        Mark one unit done. A key the plan does not hold is IGNORED (a
        cache that changed after the plan was made), and a unit completed
        twice counts once -- both are how the fraction stays monotonic.
        """
        with self._lock:
            self._inflight.pop(key, None)
            if key in self._units and key not in self._completed:
                self._completed.add(key)
                self._done_weight += self._units[key].weight

    def abandon(self, key: str) -> None:
        """The unit's block raised. It stops being in flight; it is NOT
        complete. A degradable fetch is completed by its caller right
        after (report_data's _degrade); a fatal one leaves the bar where
        it was and is remembered for the failure label.

        REMEMBERED IN ORDER, ALL OF THEM, not only the last: with the
        report fetches running concurrently, a degradable layer can raise
        (and then complete, degraded) AFTER the required layer raised,
        and "the unit an exception last left" would then name a layer the
        bar had already counted as done. fail() names the first abandoned
        unit that never completed -- the one that actually stopped the
        run -- whatever raised around it."""
        with self._lock:
            self._inflight.pop(key, None)
            if key in self._units and key not in self._abandoned:
                self._abandoned.append(key)

    def settle(self, stage: str) -> None:
        """
        A stage's work is DONE: every planned unit of it is complete, ticked
        or not. Called only after the stage's function has RETURNED -- a
        report fetch served by a cache someone else filled after the plan,
        a Layer 1 layer whose own fallback caught its exception. Never on a
        failure, so a failed run cannot settle its way to a full bar.
        """
        with self._lock:
            for key in self._order:
                if self._units[key].stage == stage and key not in self._completed:
                    self._completed.add(key)
                    self._done_weight += self._units[key].weight
                    self._inflight.pop(key, None)

    def finish(self) -> None:
        """The job returned its result: every unit is done."""
        for stage in STAGES:
            self.settle(stage)
        with self._lock:
            self._stage = None
            self._inflight.clear()

    def fail(self) -> None:
        """
        The job raised. The fraction STAYS where it is, and the label is
        frozen on what was being worked on when it stopped -- the unit
        that raised, if one did, else the stage the run was in.
        """
        with self._lock:
            if self._failed:
                return
            self._failed = True
            key = next((k for k in self._abandoned if k not in self._completed), None)
            if key is not None:
                unit = self._units[key]
                self._frozen = (unit.stage, unit.kind)
            else:
                self._frozen = self._label_locked()

    def drawing_started(self) -> None:
        with self._lock:
            self._drawing += 1

    def drawing_finished(self) -> None:
        with self._lock:
            self._drawing = max(0, self._drawing - 1)

    # --- reading it ---

    def _label_locked(self) -> tuple:
        if self._drawing and self._stage == STAGE_TERRAIN:
            return STAGE_MAPS, None
        outstanding = [
            (started, key) for key, started in self._inflight.items()
            if self._units[key].stage == self._stage
        ]
        if outstanding:
            _, key = min(outstanding)
            return self._stage, self._units[key].kind
        return self._stage, None

    def snapshot(self) -> dict:
        with self._lock:
            fraction = min(1.0, self._done_weight / self._total) if self._total > 0 else 0.0
            if self._planned and len(self._completed) == len(self._units):
                fraction = 1.0
            stage, detail = self._frozen if self._failed else self._label_locked()
            fetches = [key for key in self._order if self._units[key].stage == STAGE_RECORDS]
            return {
                "fraction": round(fraction, 4),
                "percent": int(fraction * 100 + 1e-9) if fraction < 1.0 else 100,
                "completed": len(self._completed),
                "total": len(self._units),
                "fetches": {
                    "completed": sum(1 for key in fetches if key in self._completed),
                    "total": len(fetches),
                },
                "stage": stage,
                "detail": detail,
                "failed": self._failed,
            }


# ---------------------------------------------------------------------
# The context binding and the hooks the pipeline calls. Every one of
# them is a no-op when no report job is running -- the batch paths, the
# diagnostics, a session creation's own Layer 1 fetch.
# ---------------------------------------------------------------------

_CURRENT: contextvars.ContextVar = contextvars.ContextVar("report_progress", default=None)


def current() -> Optional[ReportProgress]:
    return _CURRENT.get()


@contextlib.contextmanager
def active(progress: Optional[ReportProgress]):
    """Bind `progress` for the duration of one report job."""
    token = _CURRENT.set(progress)
    try:
        yield progress
    finally:
        _CURRENT.reset(token)


@contextlib.contextmanager
def stage(key: str):
    """The run is now in `key`. The label follows; nothing completes."""
    progress = _CURRENT.get()
    if progress is not None:
        progress.enter_stage(key)
    yield


@contextlib.contextmanager
def unit(key_stage: str, name: str):
    """
    One planned unit of work. Completed when the block returns; ABANDONED,
    not completed, when it raises -- see ReportProgress.abandon().
    """
    progress = _CURRENT.get()
    if progress is None:
        yield
        return
    key = unit_key(key_stage, name)
    progress.begin(key)
    try:
        yield
    except BaseException:
        progress.abandon(key)
        raise
    progress.complete(key)


def layer(name: str):
    """
    A fetch layer, keyed under whatever stage the run is in -- the report
    fetch stage or a rebuild's Layer 1. run_diagnostics.time_layer() calls
    this, which is the one seam every layer of both fetches passes through.
    """
    progress = _CURRENT.get()
    if progress is None:
        return contextlib.nullcontext()
    with progress._lock:
        current_stage = progress._stage
    if current_stage not in (STAGE_RECORDS, STAGE_REBUILD):
        return contextlib.nullcontext()
    return unit(current_stage, name)


def tick(key_stage: str, name: str) -> None:
    """Complete one unit from outside its block -- a degraded fetch."""
    progress = _CURRENT.get()
    if progress is not None:
        progress.complete(unit_key(key_stage, name))


def settle(key_stage: str) -> None:
    progress = _CURRENT.get()
    if progress is not None:
        progress.settle(key_stage)


def drawing(function):
    """
    Decorates an SVG renderer: while it runs, the label reads "drawing the
    maps". A LABEL AND NOT A UNIT -- the maps are ~1% of the run and their
    count depends on the data, so they are weighed inside their sections,
    and this only says truthfully what the section is doing right now.
    """

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        progress = _CURRENT.get()
        if progress is None:
            return function(*args, **kwargs)
        progress.drawing_started()
        try:
            return function(*args, **kwargs)
        finally:
            progress.drawing_finished()

    return wrapper
