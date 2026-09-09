"""
run_diagnostics.py

A PER-SESSION, PER-RUN DIAGNOSTIC RECORD, WRITTEN TO A FILE, OFF BY
DEFAULT.

WHY THIS EXISTS. Tree candidates sometimes fail commit with a
`Self-intersection` rejection and sometimes do not, on the same parcel,
and nothing in the pipeline records what differed between the two runs.
An intermittent bug with no record of its own inputs is not debuggable:
every attempt at it is a fresh guess. This module writes down what a run
actually did, so a failing run can be DIFFED against a passing one.

    THE FILE IS THE POINT. Not stdout, not a log line. The value is
    `diff record-A.json record-B.json` between two runs on the same
    parcel, which needs persistence and a stable, deterministic ordering
    that a log stream cannot give.

THE ONE RULE: IT RECORDS, IT DOES NOT COMPUTE.
==============================================
Every FIGURE in this record is read from something the pipeline already
produced -- acreages off the patch dict the producer built, gate counts
off the step's own `narrative_data`, availability flags off the result
and the session context. Nothing here re-derives a pipeline quantity.

A diagnostic that computes its own acreage is a second implementation of
the pipeline's, and it will drift: the record would then answer "what
does the diagnostic think happened" rather than "what happened", which
is the one question it exists for. If a wanted value is not currently
produced by the pipeline, this module REPORTS THAT IT IS ABSENT rather
than deriving it.

WHAT IS NOT A COMPUTATION, and why the line falls where it does. Reading
the STRUCTURE of a geometry object -- its type, its ring counts, whether
shapely calls it valid, what shapely's explain_validity() says when it
does not -- is an observation of an object the pipeline built, not a
re-derivation of a figure the pipeline calculated. There is nothing to
drift from: the pipeline never computed a ring count, so this cannot
disagree with it. `is_valid` is exactly the predicate the commit gate
runs, which is the whole subject of the investigation. Area is NOT in
that category -- the pipeline computes acreage -- so area is read off
`patch["area_acres"]` and never measured here.

FOUR CAPTURE GROUPS
===================
1. ENVIRONMENT, once per session. shapely's version and its GEOS
   version, the backend git commit, the Python version -- plus the
   rasterio/GDAL/PROJ versions, because the open hypothesis below runs
   through a PROJ round trip and a record that names GEOS but not PROJ
   would only answer half of it. The tree geometry bug reproduces on one
   machine and not another and nothing else in this system records which
   machine produced a run; this group may be the whole answer.

2. INPUT PROVENANCE, per generate. What VARIED between two runs of the
   same parcel: whether the session cache was WARM or REBUILT, whether
   ParcelData came from the fetch cache or was fetched fresh, and every
   availability/fallback/source flag the pipeline set on this run --
   collected by scanning the result and the session context for the
   flag-shaped key names the pipeline already uses (see _FLAG_KEY_
   SUFFIXES), path-qualified so two flags of the same name from
   different places stay distinguishable.

3. GATE COUNTS, per generate. The step's own `narrative_data` block,
   VERBATIM -- it is already the pipeline's pre-computed, JSON-
   serialisable account of what each stage considered and what survived
   (`candidate_count`, `dropped_count`, `dropped_invalid_count`, the
   search-space accounting, the selection rules). Copying it whole is
   the only capture that cannot drift from it. Alongside it, every
   count-shaped key found anywhere else in the result, path-qualified.

4. GEOMETRY HEALTH, per generate AND per commit. Per emitted feature:
   its id, geometry type, ring counts, the pipeline's own area, whether
   shapely calls it valid, and -- when it does not -- explain_validity()'s
   message, which carries the offending coordinate. ON A COMMIT
   REJECTION the offending feature's FULL GeoJSON is dumped into the
   record, which is what turns an intermittent bug into a permanent
   fixture. Beside it, THE DEM'S OWN CRS -- the frame that dump is
   replayed in. The defect is a reprojection defect, so a ring recorded
   without the CRS it failed to reproject into is evidence that cannot
   be re-run; see _dem_frame().

THE OPEN HYPOTHESIS, AND WHY EVERY PATCH IS CHECKED TWICE
=========================================================
The emission gate in tree_zone_candidates.score_tree_search_space()
validates `polygon_utm` -- the UTM footprint it just built. The commit
gate does NOT see that object: it sees `geometry_wgs84`, the WGS84
reprojection the producer stored, and wire_translation._polygonal_shape_
from_wire() reprojects it BACK into the DEM's CRS and validates THAT.
Those are two different geometries. At a pinch point, two near-
coincident vertices can reorder across the round trip, turning a ring
that merely touches into one that crosses -- which is a
`Self-intersection` at commit on geometry the generate called valid.

So this records BOTH verdicts for every emitted patch:

    polygon_utm_is_valid   shapely on the object the producer holds
    roundtrip              the verdict of wire_translation's OWN
                           reprojection-and-validate, called through
                           the same private helper the rehydrator calls

The round trip is deliberately NOT reimplemented here. It goes through
`wire_translation._polygonal_shape_from_wire`, which is the exact code
the commit gate runs, so the recorded verdict is the commit gate's
verdict and not a second opinion that could disagree with it for a
reason of its own.

If those two ever disagree, that is the bug, and the record says so.
THIS MODULE DOES NOT FIX IT. It captures it.

OFF BY DEFAULT, AND FREE WHEN OFF
=================================
`KEYLINE_RUN_DIAGNOSTICS` enables it (unset, empty, "0", "false", "no",
"off" -> disabled). A production session must not pay for this: it
writes on every generate and every commit.

When disabled, the hooks return on a cached boolean before touching
anything. `begin_generate()` returns None and `record_generate(None,
...)` returns immediately -- so no record is built and discarded, no
geometry is reprojected, no directory is created and no file is opened.
The cost of a disabled hook is a function call and a boolean test.

WHERE IT WRITES
===============
One JSON file per session, `<session_id>.json`, in
`$KEYLINE_RUN_DIAGNOSTICS_DIR`, defaulting to `diagnostics/` under the
working directory -- the same cwd-relative shape
`session_api.DEFAULT_STORE_DIRECTORY` uses for `sessions/`, so the two
sit side by side in a checkout and a deploy points each at its own disk.
`.gitignore` carries `diagnostics/`: these are captures of one machine's
runs, and the whole point of the record is to diff YOUR run against
someone else's, not to check either one in.

RESOLVED WITHOUT session_api, deliberately. Reading the default off
`session_api.store_directory()` would make the diagnostics directory
move whenever the document store moved -- which sounds tidy and is
wrong: it puts run captures inside the one directory a deploy is told to
back with a persistent disk, so the thing that must survive a restart
and the thing that must not would share a volume and a retention
policy. It also cost a function-local `import session_api` to dodge the
import cycle (session_api -> step_orchestrator -> this module), which is
now simply gone.

Written atomically (temp file + os.replace in the same directory) and
under a per-session lock, for document_store.py's reasons exactly: Flask
runs threaded, and every write here is a read-modify-write that appends
one event to a file another request may be appending to.

DETERMINISTIC, BECAUSE A NON-DETERMINISTIC RECORD ANSWERS NOTHING
=================================================================
Two runs on the same parcel with the same inputs must produce records
that diff clean outside the header. That means:

  * `sort_keys=True` on every write. Not the insertion order of a dict
    built by a comprehension over another dict.
  * Every collected map (flags, counts) is keyed by a stable path and
    emitted sorted.
  * Events carry a `sequence` (0, 1, 2 ...), never a timestamp.
  * NOTHING TIME-VARYING OR IDENTITY-VARYING IN THE COMPARED BODY. The
    timestamps and the session id live in `header`, which a diff is
    meant to ignore; `environment` is in the body deliberately, because
    "these two runs had different GEOS" is exactly a difference the diff
    should show.
"""

import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

# THE ENABLING VARIABLE and the directory variable, named for the two
# separate questions they answer: whether to record at all, and where.
ENABLED_ENV = "KEYLINE_RUN_DIAGNOSTICS"
DIRECTORY_ENV = "KEYLINE_RUN_DIAGNOSTICS_DIR"

# STRICT MODE: re-raise instead of swallowing. Off by default, because a
# diagnostic must never turn a working generate into a 500 -- but that
# swallow is also the one thing that can make this feature fail
# invisibly, so there has to be a way to turn it off while chasing
# exactly that. With it set, a hook's exception propagates out of the
# generate and lands in the job's error payload where a client sees it.
STRICT_ENV = "KEYLINE_RUN_DIAGNOSTICS_STRICT"

# Where records are written when DIRECTORY_ENV says nothing: a relative
# path under the working directory (/app in the Dockerfile), exactly like
# session_api.DEFAULT_STORE_DIRECTORY's "sessions". Named as a constant
# for the same reason that one is -- the thing a deploy may have to point
# somewhere else gets one obvious name. See the module docstring's WHERE
# IT WRITES.
DEFAULT_DIRECTORY = "diagnostics"

# Values of ENABLED_ENV that mean "no". Anything else -- including "1",
# "true", "yes" and a bare "x" -- enables. A variable that is SET is a
# deliberate act; only the conventional negatives undo it.
_DISABLED_VALUES = frozenset({"", "0", "false", "no", "off"})

# The schema version of the record itself. Bumped when the shape of a
# record changes, so a diff between two records written by different
# builds of this module is recognizable as such rather than read as a
# difference between the two runs.
RECORD_VERSION = 1

# Session ids come from secrets.token_urlsafe(); document_store.py
# refuses anything outside that alphabet before it can touch a path, and
# so does this -- a crafted session_id must not be able to name a file
# outside the diagnostics directory.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# --- what the scanner considers a flag, and what it considers a count -
#
# THE PIPELINE'S OWN NAMING, not a list of the flags that exist today. A
# step that grows a sixth `*_data_available` is recorded without this
# module being edited, which is the only way a scan like this stays
# honest as the pipeline moves.
_FLAG_KEY_SUFFIXES = (
    "_data_available",
    "_available",
    "_is_fallback",
    "_source",
    "_checked",
    "_excluded",
)
_FLAG_KEY_EXACT = frozenset({"data_available", "is_fallback"})

_COUNT_KEY_SUFFIXES = ("_count",)
_COUNT_KEY_PREFIXES = ("dropped",)

# A DROP SINK is a list of dicts under a `dropped*` key -- trees'
# `dropped_invalid`, water's `dropped_zones`. Each entry describes one
# thing the step scored and then refused, and `reason`/`drop_reason` on
# it is the only statement anywhere of WHY. Same naming-shape rule as
# the flags above, for the same reason: a seventh step's sink is
# recorded the day it lands, with nothing named here.
_DROP_KEY_PREFIXES = ("dropped",)

# Drop rows recorded per generate, across every sink. A bound rather
# than a budget -- a healthy generate drops nothing and a pathological
# one must not be able to write a record nobody will open.
_MAX_RECORDED_DROPS = 200

# How much of a STRING field on a drop row is kept. A drop row is
# recorded for its REASON, and a reason is a sentence: trees' is
# explain_validity()'s message with its coordinate, water's is a status
# constant. What overruns this is display prose -- water's dropped zones
# are whole zone dicts and carry the two-kilobyte `confidence_notes`
# essay the wire already delivers, which doubled the file to say nothing
# about the drop. Truncation is marked in the value, never silent.
_MAX_RECORDED_DROP_STRING = 400
_TRUNCATION_MARKER = "...[truncated by run_diagnostics]"

# Scan bounds. A result dict holds whole GeoJSON collections, and an
# unbounded walk over one is both slow and pointless -- a coordinate
# array carries no flags. The depth cap is the real guard; the node cap
# is the backstop, and when it trips the record SAYS SO (see _Scan) so a
# reader never mistakes a truncated scan for an absent flag.
_SCAN_MAX_DEPTH = 12
_SCAN_MAX_NODES = 200000

# Per-session locks around the read-modify-write, document_store.py's
# pattern and for its reason.
_SESSION_LOCKS = {}
_SESSION_LOCKS_GUARD = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _SESSION_LOCKS[session_id] = lock
        return lock


# ======================================================================
# Enabled, and where
# ======================================================================


def strict() -> bool:
    """
    Whether a hook re-raises instead of swallowing. See STRICT_ENV.

    Read from the environment on every call, enabled()'s reason exactly.
    """
    return os.environ.get(STRICT_ENV, "").strip().lower() not in _DISABLED_VALUES


def enabled() -> bool:
    """
    Whether run diagnostics are on, read from the environment EVERY TIME.

    NOT CACHED, deliberately. A cached-at-import boolean cannot be turned
    on by a test that sets the variable after importing this module --
    and a test doing exactly that is how the OFF path gets proved to cost
    nothing. `os.environ.get` is a dict lookup; the cost of asking is not
    the cost this module's "off by default" promise is about, which is
    the record it does not build.
    """
    return os.environ.get(ENABLED_ENV, "").strip().lower() not in _DISABLED_VALUES


def directory() -> str:
    """
    Where records are written. DIRECTORY_ENV first, DEFAULT_DIRECTORY
    otherwise -- session_api.store_directory()'s shape, and for its
    reasons, including the empty-string case: a variable set to "" is not
    a configured path and falls through to the default rather than
    naming the process's own working directory.
    """
    return os.environ.get(DIRECTORY_ENV) or DEFAULT_DIRECTORY


def record_path(session_id: str) -> str:
    """This session's record file. Raises on an unusable session_id."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"unusable session_id for a diagnostic record: {session_id!r}")
    return os.path.join(directory(), f"{session_id}.json")


# ======================================================================
# Group 1 -- the environment
# ======================================================================


def _git_commit() -> dict:
    """
    The backend git commit SHA, and -- when there is none -- WHY.

    `{"sha": None, "unavailable_reason": "..."}` rather than a bare null:
    a record from a container built without a .git directory and a record
    from a machine where git is not installed are different situations,
    and a reader comparing two records needs to be able to tell "no
    commit recorded" from "this build has no git".

    Run in the directory of THIS file, so it names the backend checkout
    rather than whatever the process happened to chdir into.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # git absent, or not executable
        return {"sha": None, "unavailable_reason": f"{type(exc).__name__}: {exc}"}
    if completed.returncode != 0:
        return {"sha": None, "unavailable_reason": completed.stderr.strip() or "git rev-parse HEAD failed"}
    return {"sha": completed.stdout.strip(), "unavailable_reason": None}


def environment() -> dict:
    """
    Group 1, built once per session -- and PART OF THE COMPARED BODY, not
    the header. "These two runs had different GEOS" is precisely a
    difference the diff exists to show.

    THE GEOMETRY STACK, BOTH HALVES. shapely/GEOS is what decides
    `is_valid` and writes the `Self-intersection` message; rasterio's
    GDAL/PROJ is what performs the WGS84 round trip the open hypothesis
    turns on. A record naming one and not the other answers half the
    question.

    Every value is read off a version attribute the library publishes.
    Nothing is derived.
    """
    import platform

    import rasterio
    import shapely

    return {
        "python_version": platform.python_version(),
        "shapely_version": getattr(shapely, "__version__", None),
        # A 3-tuple; joined so a diff shows "3.13.1" and not a JSON array
        # that reads differently from the version_string beside it.
        "shapely_geos_version": ".".join(str(part) for part in shapely.geos_version),
        "shapely_geos_version_string": shapely.geos_version_string,
        # getattr THROUGHOUT, and not because these are expected to be
        # missing. environment() runs inside _new_record(), which runs on
        # the FIRST event of every session -- so an AttributeError here
        # is not a missing line in one record, it is every record of
        # every session on that machine failing to be written, reported
        # only as a swallowed exception. A version this build's rasterio
        # does not publish is worth a null; it is not worth the feature.
        "rasterio_version": getattr(rasterio, "__version__", None),
        "rasterio_gdal_version": getattr(rasterio, "__gdal_version__", None),
        "rasterio_proj_version": getattr(rasterio, "__proj_version__", None),
        "backend_git_commit": _git_commit(),
    }


# ======================================================================
# The scanner -- how groups 2 and 3 are collected without recomputing
# ======================================================================


class _Scan:
    """
    A bounded, path-qualified walk over a pipeline value, collecting
    three things by the shape of the key they sit under: the SCALARS at
    flag-shaped keys, the INTEGERS at count-shaped keys, and the ROWS of
    every drop sink (see _record_drops()).

    PATH-QUALIFIED because the same flag name legitimately appears in
    several places on one result -- `hydric_data_available` rides every
    tree patch AND sits in `narrative_data.gates` -- and collapsing them
    into one key would silently pick a winner. `result.patches[0].
    hydric_data_available` and `result.narrative_data.gates.hydric_data_
    available` are two facts, recorded as two.

    LISTS ARE ENTERED ONLY AT THEIR DICT ELEMENTS. That single rule is
    what keeps this cheap and total: `narrative_data.zones` and a
    FeatureCollection's `features` are lists of dicts and are walked; a
    GeoJSON `coordinates` array is a list of lists of floats, holds no
    keys at all, and is skipped without a special case for it.

    NON-CONTAINERS THAT ARE NOT SCALARS ARE NOT ENTERED and not recorded
    as values -- a shapely Polygon or a numpy array at a flag-shaped key
    is recorded as its type name, so the key's presence is never lost.
    """

    def __init__(self):
        self.flags = {}
        self.counts = {}
        self.drops = {}
        self.truncated = False
        self._nodes = 0
        self._drop_rows = 0
        self._dropped_seen = set()

    def walk(self, value, path: str, depth: int = 0) -> None:
        if depth > _SCAN_MAX_DEPTH:
            self.truncated = True
            return
        self._nodes += 1
        if self._nodes > _SCAN_MAX_NODES:
            self.truncated = True
            return
        if isinstance(value, dict):
            for key in value:
                if not isinstance(key, str):
                    continue
                child = value[key]
                child_path = f"{path}.{key}" if path else key
                if _is_flag_key(key):
                    self.flags[child_path] = _scalar(child)
                elif (
                    _is_count_key(key)
                    and isinstance(child, int)
                    and not isinstance(child, bool)
                    # A GATE COUNT IS PER STAGE, NOT PER ITEM. "[" in the
                    # path means this count hangs off a list element -- a
                    # per-zone `cell_count`, a per-member tally -- and the
                    # water result alone carries sixty of them, which would
                    # bury the six counts that describe what the STEP's
                    # gates did. Nothing is lost: those per-item counts are
                    # already in `narrative_data`, recorded verbatim beside
                    # this map, wherever the pipeline publishes them.
                    and "[" not in child_path
                ):
                    self.counts[child_path] = child
                elif _is_drop_key(key) and isinstance(child, list):
                    self._record_drops(child_path, child)
                self.walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                # Only dict elements. See this class's docstring.
                if isinstance(child, dict):
                    self.walk(child, f"{path}[{index}]", depth + 1)

    def _record_drops(self, path: str, entries: list) -> None:
        """
        One drop sink, recorded ROW BY ROW rather than as its length.

        THE COUNT WAS NEVER THE ANSWER. `dropped_invalid_count` says a
        patch was lost; the row says which defect lost it and -- because
        `reason` is explain_validity()'s own message -- at which
        coordinate. That detail existed for exactly the length of the
        call that produced it and was thrown away at the one moment it
        was in hand, which is the position commit rejections were in
        before this module.

        EVERY FIELD OF EVERY ROW, through _scalar(), so a sink whose rows
        carry geometry (water's dropped zones are whole zone dicts) still
        records every scalar it carries and names the type of what it
        does not. Nothing is selected for; a row is copied.
        """
        rows = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # ONE ROW PER DROPPED OBJECT, WHICHEVER SINK REACHES IT
            # FIRST -- _collect_patches()'s rule. The water result holds
            # its dropped zones under both `dropped_zones` and the
            # nested `result.dropped_zones`, one list of the same dicts
            # reached two ways, and recording both said everything twice.
            if id(entry) in self._dropped_seen:
                continue
            if self._drop_rows >= _MAX_RECORDED_DROPS:
                self.truncated = True
                break
            self._dropped_seen.add(id(entry))
            self._drop_rows += 1
            rows.append(
                {key: _drop_field(entry[key]) for key in sorted(entry) if isinstance(key, str)}
            )
        # AN EMPTY SINK IS RECORDED AS `[]`, AND A FULLY-DEDUPED ONE IS
        # NOT RECORDED AT ALL. The two are different statements: `[]`
        # says this step ran its gate and dropped nothing -- a stable
        # line that a later non-empty run shows up against as a CHANGE
        # rather than as an addition, which is what a diff reads best --
        # while a sink whose every row was already recorded under
        # another path has nothing left to say and must not claim to be
        # empty.
        if rows or not entries:
            self.drops[path] = rows


def _is_flag_key(key: str) -> bool:
    return key in _FLAG_KEY_EXACT or key.endswith(_FLAG_KEY_SUFFIXES)


def _is_count_key(key: str) -> bool:
    return key.endswith(_COUNT_KEY_SUFFIXES) or key.startswith(_COUNT_KEY_PREFIXES)


def _is_drop_key(key: str) -> bool:
    return key.startswith(_DROP_KEY_PREFIXES)


def _drop_field(value):
    """
    One field of one drop row: _scalar(), with a long string cut to
    _MAX_RECORDED_DROP_STRING and MARKED as cut.

    The cut is on the record's own copy and nothing else -- the pipeline
    value is untouched and the marker says plainly that what is here is
    not all of what was there, so no reader can mistake a truncated
    essay for a short reason.
    """
    recorded = _scalar(value)
    if type(recorded) is str and len(recorded) > _MAX_RECORDED_DROP_STRING:
        return recorded[:_MAX_RECORDED_DROP_STRING] + _TRUNCATION_MARKER
    return recorded


_JSON_SCALARS = (bool, int, float, str)


def _scalar(value):
    """
    A value fit for the record: JSON scalars verbatim, a numpy scalar as
    the Python number it stands for, anything else as its type name in
    angle brackets.

    NEVER DROPPED SILENTLY. A flag holding a Polygon is a fact about the
    run ("this key was not the boolean a reader expects"), and a record
    that omitted the key would say the flag was absent.

    THE NUMPY CASE IS NOT A CONVERSION THIS MODULE CHOSE. A pipeline
    value read off a grid is an np.bool_ or an np.int64, which json
    cannot serialise and which `isinstance` does not agree about across
    platforms -- np.float64 IS a float, np.int64 is not an int. `.item()`
    is numpy's own name for the Python number the scalar already is, so
    this records the value rather than a `<bool_>` marker standing in for
    one. Guarded on a zero-dimensional shape so an ARRAY, which is not a
    scalar and has no business in a record, still reports as its type.

    EXACT TYPES, not isinstance, for the first test: a subclass of str or
    int (an enum, a numpy scalar) goes down the second path and is
    normalised there rather than being written out as whatever its
    repr happens to be.
    """
    if value is None or type(value) in _JSON_SCALARS:
        return value
    item = getattr(value, "item", None)
    if item is not None and getattr(value, "shape", None) == ():
        converted = item()
        if converted is None or type(converted) in _JSON_SCALARS:
            return converted
    return f"<{type(value).__name__}>"


def _sorted_scan(sources: dict) -> dict:
    """
    Scan several named sources into one flags/counts pair.

    `sources` is {prefix: value}; the prefix becomes the head of every
    path, so `exclusion_zones.layers.canopy.data_available` says which of
    the three places that flag came from. Iterated in SORTED prefix order
    and emitted through sorted dicts -- see the module docstring on
    determinism.
    """
    scan = _Scan()
    for prefix in sorted(sources):
        scan.walk(sources[prefix], prefix)
    return {
        "flags": {key: scan.flags[key] for key in sorted(scan.flags)},
        "counts": {key: scan.counts[key] for key in sorted(scan.counts)},
        "drops": {key: scan.drops[key] for key in sorted(scan.drops)},
        "scan_truncated": scan.truncated,
    }


# ======================================================================
# Group 4 -- geometry health
# ======================================================================
#
# STRUCTURE AND VALIDITY, NEVER A MEASUREMENT. Everything below reads the
# shape of a geometry object or the shape of a GeoJSON dict: its type,
# how many parts and rings it has, how many positions those rings carry,
# how many decimal places the wire spells them to, and what shapely says
# about its validity. The pipeline computes none of those, so none of
# them can drift from it. ACREAGE is the counter-example and is treated
# as such throughout: it is read off the producer's own `area_acres` and
# is never measured here.


def _shapely_rings(geometry) -> dict:
    """
    Part and ring counts for a shapely Polygon/MultiPolygon.

    A geometry of any other type (an empty geometry, or a
    GeometryCollection out of a repair) is reported as its type with null
    counts, rather than being coerced into a shape it does not have.
    """
    geom_type = geometry.geom_type
    if geom_type == "Polygon":
        parts = [geometry]
    elif geom_type == "MultiPolygon":
        parts = list(geometry.geoms)
    else:
        return {
            "geometry_type": geom_type,
            "part_count": None,
            "exterior_ring_count": None,
            "interior_ring_count": None,
            "position_count": None,
        }
    return {
        "geometry_type": geom_type,
        "part_count": len(parts),
        # One exterior ring per part, by definition of a polygon -- stated
        # as a count anyway so the record's four numbers can be read
        # against each other without knowing that.
        "exterior_ring_count": len(parts),
        "interior_ring_count": sum(len(part.interiors) for part in parts),
        "position_count": sum(
            len(part.exterior.coords) + sum(len(ring.coords) for ring in part.interiors)
            for part in parts
        ),
    }


def _wire_rings(geometry: dict) -> dict:
    """
    The same four counts plus the COORDINATE PRECISION, off a GeoJSON
    geometry dict -- what the wire actually carries.

    `coordinate_decimals_max` is the largest number of digits after the
    decimal point in any coordinate of this geometry, counted on
    `repr(float)`. That is the precision the wire carries in the sense
    that matters: how much of the producer's double survives into the
    JSON a client receives and sends back. A geometry whose coordinates
    round-trip at full double precision reads ~17 there; one that has
    been quantized to centimetres reads 7.
    """
    geometry_type = (geometry or {}).get("type")
    coordinates = (geometry or {}).get("coordinates")
    if geometry_type == "Polygon":
        parts = [coordinates]
    elif geometry_type == "MultiPolygon":
        parts = list(coordinates or [])
    else:
        return {
            "geometry_type": geometry_type,
            "part_count": None,
            "exterior_ring_count": None,
            "interior_ring_count": None,
            "position_count": None,
            "coordinate_decimals_max": None,
        }
    positions = 0
    decimals = 0
    interior = 0
    for part in parts:
        rings = list(part or [])
        # `rings[1:]` and not `len(rings) - 1`: every ring after the
        # exterior IS the interior set, said as a slice rather than as a
        # subtraction. There is no arithmetic anywhere in this module --
        # see test_run_diagnostics.py section 5, which asserts it.
        interior += len(rings[1:])
        for ring in rings:
            for position in ring or []:
                positions += 1
                for ordinate in position:
                    decimals = max(decimals, _decimals(ordinate))
    return {
        "geometry_type": geometry_type,
        "part_count": len(parts),
        "exterior_ring_count": len(parts),
        "interior_ring_count": interior,
        "position_count": positions,
        "coordinate_decimals_max": decimals,
    }


def _decimals(ordinate) -> int:
    """Digits after the decimal point in repr(ordinate). 0 for an int."""
    text = repr(float(ordinate))
    if "e" in text or "E" in text:
        # An exponent-form float carries its full significand; counting
        # the characters after the point would understate it, so say so
        # rather than report a number that means something else.
        return -1
    _, _, fraction = text.partition(".")
    return len(fraction.rstrip("0"))


def _validity(geometry) -> dict:
    """
    shapely's verdict on one geometry, with the message when it is
    negative.

    `explain_validity()` is what carries THE OFFENDING COORDINATE --
    "Self-intersection[512345.6 4478901.2]" -- and it is the same
    function tree_zone_candidates.py's own emission gate and
    wire_translation.py's inbound gate both call. Recorded verbatim; the
    coordinate is not parsed out of it, because the message is the
    pipeline's own statement of the defect and re-formatting it is how a
    record starts disagreeing with the error the user saw.
    """
    from shapely.validation import explain_validity

    is_valid = bool(geometry.is_valid)
    return {
        "is_valid": is_valid,
        # None when valid: explain_validity() returns "Valid Geometry" on
        # a valid one, which would put a constant string on every line of
        # every record for no information.
        "invalidity": None if is_valid else explain_validity(geometry),
    }


def _roundtrip(geometry_wgs84, dem) -> dict:
    """
    THE OPEN HYPOTHESIS, RECORDED. What the COMMIT GATE would say about
    this feature's wire geometry.

    Through wire_translation._polygonal_shape_from_wire -- the private
    helper every polygon rehydrator calls, and therefore the exact code
    the commit gate runs. NOT a reimplementation of the reprojection: a
    second `transform_geom` call written here could differ from the
    rehydrator's in a detail (the source CRS, the argument order, a
    later change to the helper) and would then record a verdict the
    commit gate does not hold, which is worse than recording nothing.

    Three outcomes, and they are three because they are different facts:

      rehydrates=True                   the gate accepts this geometry
      rehydrates=False, error="..."     the gate refuses it, with the
                                        rehydrator's own message -- which
                                        for a self-intersection carries
                                        explain_validity()'s coordinate
      rehydrates=None, error="..."      the check itself could not run

    `is_valid` beside them is shapely's verdict on the REPROJECTED
    geometry when there is one, so it lines up column-for-column with the
    `polygon_utm` validity recorded next to it. When they disagree, the
    two geometries the two gates validate are genuinely different
    geometries, which is the thing this branch exists to find out.
    """
    import wire_translation

    # NOT EVERY EMITTED FEATURE IS A ZONE. A road centreline, a fence
    # line and a placed structure point all reach this module through the
    # same payload walk, and _polygonal_shape_from_wire() is the POLYGON
    # rehydrators' gate: handed a LineString it correctly refuses it. That
    # refusal is not a fact about the run -- the roads commit never asks
    # this question of a LineString -- so it is reported as "not checked"
    # rather than as a failure a reader would chase.
    geometry_type = (geometry_wgs84 or {}).get("type") if isinstance(geometry_wgs84, dict) else None
    if geometry_type not in ("Polygon", "MultiPolygon"):
        return {
            "rehydrates": None,
            "error": f"not checked: geometry type {geometry_type!r} is not polygonal",
            "is_valid": None,
        }

    try:
        reprojected = wire_translation._polygonal_shape_from_wire(
            geometry_wgs84, dem, "run diagnostics round trip"
        )
    except wire_translation.InboundGeometryError as exc:
        return {"rehydrates": False, "error": str(exc), "is_valid": False}
    except Exception as exc:
        return {
            "rehydrates": None,
            "error": f"{type(exc).__name__}: {exc}",
            "is_valid": None,
        }
    return {
        "rehydrates": True,
        "error": None,
        "is_valid": bool(reprojected.is_valid),
    }


def _dem_frame(dem):
    """
    THE FRAME A DUMPED GEOMETRY IS REPLAYED IN: the DEM's own CRS.

    record_commit()'s `rejected_features_geojson` puts the offending ring
    into the record verbatim, which is what makes an intermittent
    rejection a permanent fixture. A RING ALONE IS NOT REPLAYABLE. The
    defect this module exists for is a REPROJECTION defect -- it appears
    when wire_translation._polygonal_shape_from_wire() takes that WGS84
    ring back into `dem['crs']` -- so a reader who cannot name that CRS
    can look at the coordinates the run failed on but cannot run the
    operation that failed on them. Recording the ring and withholding the
    frame captures the evidence and not the experiment.

    NOT INFERRED FROM THE COORDINATES. A lon/lat ring implies a UTM zone
    and deriving one here would be easy. It would also be this module
    DECIDING which frame the run used rather than recording it, and it
    would be silently wrong on any run whose DEM is not in the zone its
    own centre falls in. Read off the DEM the pipeline actually handed
    the rehydrator, or null.

    str(), NOT _scalar(). A rasterio CRS is not a JSON scalar, so
    _scalar() would correctly report it as `<CRS>` -- the type name in
    place of the one value this capture exists for. str() is the CRS
    object's own name for itself ("EPSG:32617"), and it is also the form
    transform_geom() accepts, so a replay can paste it straight back in.
    """
    if dem is None:
        return None
    if not isinstance(dem, dict):
        # _scalar()'s convention, for _scalar()'s reason: a DEM that is
        # not the dict the pipeline passes everywhere is a fact about the
        # run, and a null here would say there was no DEM at all.
        return {"crs": f"<{type(dem).__name__}>"}
    crs = dem.get("crs")
    return {"crs": None if crs is None else str(crs)}


def _patch_health(patch: dict, dem) -> dict:
    """
    One internal patch's geometry health -- BOTH VERDICTS SIDE BY SIDE.

    `area_acres` is the producer's own field, copied. It is not measured
    here and it is not defaulted: a patch that carries no `area_acres` is
    recorded as null, which says the producer did not publish one.

    `verdicts_agree` is the only derived value in this record, and it is
    an equality between the two booleans printed either side of it --
    not a third opinion about the geometry. It is here because the
    question this whole branch asks is "do these two ever disagree", and
    a reader should be able to grep one key for the answer rather than
    compare two columns by eye across a thousand lines.
    """
    polygon_utm = patch.get("polygon_utm")
    record = {
        "id": _scalar(patch.get("id")),
        "area_acres": _scalar(patch.get("area_acres")),
    }
    if polygon_utm is None:
        record["polygon_utm"] = None
        record["roundtrip"] = None
        record["verdicts_agree"] = None
        return record

    validity = _validity(polygon_utm)
    record["polygon_utm"] = {**_shapely_rings(polygon_utm), **validity}

    geometry_wgs84 = patch.get("geometry_wgs84")
    if geometry_wgs84 is None or dem is None:
        record["roundtrip"] = None
        record["verdicts_agree"] = None
        return record

    # THE WIRE FORM'S STRUCTURE AND THE GATE'S VERDICT, SEPARATELY. They
    # describe two different geometries -- `geometry_wgs84` as it sits on
    # the wire, and what the rehydrator makes of it -- and folding the
    # ring counts into the verdict block would read as counts OF the
    # reprojected geometry, which they are not. Same two keys, same
    # meaning, as _feature_health() below.
    roundtrip = _roundtrip(geometry_wgs84, dem)
    record["geometry_wgs84"] = _wire_rings(geometry_wgs84)
    record["roundtrip"] = roundtrip
    record["verdicts_agree"] = (
        None if roundtrip["is_valid"] is None else validity["is_valid"] == roundtrip["is_valid"]
    )
    return record


def _feature_health(feature: dict, dem) -> dict:
    """
    One WIRE feature's geometry health: what the client is handed, and
    what the commit gate would say if the client handed it straight back.

    The counterpart of _patch_health() for a step whose emitted geometry
    reaches this module as GeoJSON rather than as an internal patch. It
    has no `polygon_utm` to compare against -- the wire does not carry
    one -- so it records the round trip alone.
    """
    geometry = feature.get("geometry")
    properties = feature.get("properties") or {}
    record = {
        "feature_id": _scalar(feature.get("id")),
        "layer": _scalar(properties.get("layer")),
        # The producer's own acreage where the feature carries one; null
        # where it does not. Never measured off the geometry.
        "area_acres": _scalar(properties.get("area_acres")),
        "wire": _wire_rings(geometry),
    }
    record["roundtrip"] = None if dem is None else _roundtrip(geometry, dem)
    return record


# --- finding the emitted geometry, without knowing the step -----------
#
# NO STEP-SPECIFIC CODE. The two collectors below look for the two SHAPES
# every step already publishes -- a list of internal patch dicts carrying
# `polygon_utm`, and a GeoJSON FeatureCollection -- so a seventh registry
# entry is recorded the day it lands, with nothing added here. A
# per-step table would be a second registry, maintained by hand, and out
# of date exactly when a new step is the one being debugged.

_MAX_RECORDED_GEOMETRIES = 500


def _collect_patches(result, dem) -> tuple:
    """
    Every internal patch in `result`, path-qualified: (records, truncated).

    A "patch" is a dict carrying `polygon_utm` -- the field name every
    producer in this pipeline uses for the internal UTM footprint, and
    the field the commit gate's counterpart is derived from. Found
    wherever it sits: a list under `patches`, a bare dict, nested inside
    another block.
    """
    records = []
    truncated = [False]
    seen = set()

    def walk(value, path, depth):
        if depth > _SCAN_MAX_DEPTH or len(records) >= _MAX_RECORDED_GEOMETRIES:
            # EITHER BOUND SETS THE FLAG. A branch not walked and a
            # geometry not recorded are the same statement to a reader:
            # this list is not everything. Saying so is what keeps an
            # absent entry from reading as an absent geometry.
            truncated[0] = True
            return
        # ONE OBJECT, ONE RECORD, WHICHEVER PATH REACHES IT FIRST. A
        # step's result routinely holds the same patch under several keys
        # -- water's `zones`, `zones_by_type.embankment` and the whole
        # nested `result` block are three routes to one dict -- and
        # recording each route would triple the file and pay for a
        # reprojection per route to say the same thing three times. The
        # walk is depth-first over SORTED keys, so which path wins is
        # deterministic, which is what the diff needs. It also makes a
        # cycle in a result impossible to loop on.
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, dict):
            if "polygon_utm" in value:
                records.append({"source": path, **_patch_health(value, dem)})
                return
            for key in sorted(value):
                if isinstance(key, str):
                    walk(value[key], f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    walk(child, f"{path}[{index}]", depth + 1)

    walk(result, "result", 0)
    return records, truncated[0]


def _collect_wire_features(payload, dem) -> tuple:
    """
    Every feature of every FeatureCollection in `payload`, path-qualified:
    (records, truncated).

    The payload is what the client is handed, so this is the emitted
    geometry in the form the commit will hand back -- which is why each
    one carries the round-trip verdict. Collections are found by their
    own GeoJSON discriminator (`type == "FeatureCollection"`), not by the
    key they hang off, so a step that names its collection something new
    is still recorded.
    """
    records = []
    truncated = [False]
    seen = set()

    def walk(value, path, depth):
        if depth > _SCAN_MAX_DEPTH or len(records) >= _MAX_RECORDED_GEOMETRIES:
            truncated[0] = True
            return
        # _collect_patches()'s rule, for _collect_patches()'s reason: one
        # collection reachable by two payload keys is recorded once, at
        # the first path a sorted depth-first walk reaches it by.
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, dict):
            if value.get("type") == "FeatureCollection" and isinstance(value.get("features"), list):
                for index, feature in enumerate(value["features"]):
                    if len(records) >= _MAX_RECORDED_GEOMETRIES:
                        truncated[0] = True
                        return
                    if isinstance(feature, dict):
                        records.append(
                            {"source": f"{path}.features[{index}]", **_feature_health(feature, dem)}
                        )
                return
            for key in sorted(value):
                if isinstance(key, str):
                    walk(value[key], f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    walk(child, f"{path}[{index}]", depth + 1)

    walk(payload, "payload", 0)
    return records, truncated[0]


# ======================================================================
# The file
# ======================================================================


def _new_record(session_id: str) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        # THE HEADER IS WHAT A DIFF IGNORES. Everything that varies
        # between two runs for a reason other than the run itself lives
        # here and nowhere else: the clock and the session id.
        "header": {
            "record_version": RECORD_VERSION,
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
            "event_count": 0,
        },
        "environment": environment(),
        "events": [],
    }


def append_event(session_id: str, event: dict) -> None:
    """
    Append one event to this session's record, creating it (with group 1)
    on the first call.

    ATOMIC AND LOCKED, document_store.JSONFileStore.put()'s shape exactly:
    a temp file in the SAME directory then os.replace(), under a
    per-session lock, because this is a read-modify-write and Flask is
    threaded.

    `sequence` is assigned HERE, from the length of the list it is being
    appended to, so it is 0, 1, 2 ... in append order and carries no
    clock. It is what gives the record its stable ordering; the header's
    timestamps are what a diff skips.
    """
    target = record_path(session_id)
    with _session_lock(session_id):
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            with open(target, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except FileNotFoundError:
            record = _new_record(session_id)

        record["events"].append({"sequence": len(record["events"]), **event})

        record["header"]["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record["header"]["event_count"] = len(record["events"])

        # sort_keys=True IS THE DETERMINISM. Every dict in this record is
        # written in one order regardless of how it was built, so two
        # runs that did the same thing produce two files that diff clean.
        payload = json.dumps(record, indent=2, sort_keys=True, default=_scalar)
        fd, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(target) or ".", prefix=f".{session_id}.", suffix=".tmp"
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


def read_record(session_id: str) -> dict:
    """This session's record as written. Raises FileNotFoundError if none."""
    with open(record_path(session_id), "r", encoding="utf-8") as handle:
        return json.load(handle)


def comparable_body(record: dict) -> dict:
    """
    The record minus its header -- WHAT TWO RUNS ARE DIFFED ON.

    Named and provided here rather than left to each reader to strip,
    because "everything except the header" is the contract the header
    exists to serve, and a caller that strips a different set is
    comparing something else.
    """
    return {key: value for key, value in record.items() if key != "header"}


# ======================================================================
# The hooks -- two at generate, one at commit
# ======================================================================
#
# WHAT A DISABLED HOOK COSTS. `begin_generate()` and `record_commit()`
# test `enabled()` -- one os.environ lookup -- and return. `record_
# generate()` tests its probe for None and returns. No record is built,
# no geometry is reprojected, no directory is created, no file is
# opened, and `directory()` is never reached. The whole disabled path is
# a call and a boolean.
#
# A HOOK NEVER FAILS A RUN. Every one of them catches everything and
# prints; a diagnostic that turned a working generate into a 500 would
# be a worse bug than the one it was added to find. The print carries a
# fixed prefix so it is greppable, and the absence of the record is
# itself visible to anyone reading the file.

_FAILURE_PREFIX = "run_diagnostics: recording failed --"

# Every failure this module has swallowed in this process, newest last,
# as {"what", "error", "traceback"}. IN MEMORY AND NOT IN THE RECORD,
# necessarily: the failure being reported may BE the failure to write the
# record, so a sink that writes cannot report it. self_check() prints
# this, and a live process can be asked for it directly.
FAILURES = []
_MAX_REMEMBERED_FAILURES = 50


def _report_failure(what: str, exc: BaseException) -> None:
    """
    One swallowed hook failure: remembered, and printed TO STDERR with a
    traceback.

    STDERR AND NOT STDOUT, which is what it used to be and what made this
    module able to fail invisibly. A Flask or gunicorn process routinely
    has stdout buffered or redirected somewhere nobody reads, so a
    `print()` here could vanish completely -- and the symptom of a
    vanished report is EXACTLY the symptom of the feature not being
    wired at all: enabled, hooks in place, nothing on disk, nothing in
    the log. Server logs carry stderr.

    WITH THE TRACEBACK, for the same reason. "AttributeError: x" names
    the exception and not the line, and the line is the whole question
    when a hook fails on one machine and not another.

    Re-raised rather than reported when STRICT_ENV is set: see strict().
    """
    import traceback

    detail = traceback.format_exc()
    if len(FAILURES) >= _MAX_REMEMBERED_FAILURES:
        del FAILURES[0]
    FAILURES.append(
        {"what": what, "error": f"{type(exc).__name__}: {exc}", "traceback": detail}
    )
    print(f"{_FAILURE_PREFIX} {what}: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(detail, file=sys.stderr)
    sys.stderr.flush()


class GenerateProbe:
    """
    What must be sampled BEFORE the generate runs, carried to the hook
    that writes the record after it.

    THE CACHE STATES ARE ONLY ANSWERABLE BEFOREHAND. "Was the session
    cache warm" is a question about the moment before
    get_session_context() ran -- afterwards it is warm either way,
    because that call populates it. Same for the fetch cache. Asking
    after the fact would record a constant.
    """

    __slots__ = ("session_id", "step_id", "provenance")

    def __init__(self, session_id, step_id, provenance):
        self.session_id = session_id
        self.step_id = step_id
        self.provenance = provenance


def begin_generate(session_id, step_id, document, fetch_cache=None, cache=None):
    """
    Sample the two cache states. Returns a probe, or None when disabled.

    The membership questions are the caches' OWN predicates --
    `SessionCache.__contains__` and `FetchCache.contains` -- so this
    records what those caches say about themselves rather than a second
    opinion assembled from their internals.

    The `is None -> module default` resolution repeats
    session_manager.get_session_context()'s, deliberately and by value:
    this must ask the SAME cache that call is about to consult, and a
    probe that asked a different one would record a fact about the wrong
    object. `is None` and never `or`, for session_cache.py's documented
    reason -- both cache classes define __len__, so an empty caller-
    supplied cache is falsy.
    """
    if not enabled():
        return None
    try:
        import session_cache

        if fetch_cache is None:
            fetch_cache = session_cache.DEFAULT_FETCH_CACHE
        if cache is None:
            cache = session_cache.DEFAULT_SESSION_CACHE
        boundary = (document or {}).get("boundary")
        provenance = {
            "session_cache": "warm" if session_id in cache else "rebuilt",
            "parcel_data": "fetch_cache" if fetch_cache.contains(boundary) else "fetched",
        }
        return GenerateProbe(session_id, step_id, provenance)
    except Exception as exc:
        if strict():
            raise
        _report_failure(f"begin_generate({session_id!r}, {step_id!r})", exc)
        return None


def record_generate(probe, result, payload, context) -> None:
    """
    Groups 2, 3 and 4 for one generate, appended to the session's record.

    `result` is the step entry point's OWN result and `payload` is the
    wire payload built from it -- both already in hand at the call site,
    neither recomputed. `context` is the SessionContext, read for the
    DEM (the round trip needs the CRS it reprojects into) and for the two
    warm-up products that carry flags of their own.
    """
    if probe is None:
        return
    try:
        exclusion = getattr(context, "exclusion_zones", None)
        scanned = _sorted_scan(
            {
                "result": result,
                "exclusion_zones": exclusion,
                # The one SessionContext scalar that is a fallback flag.
                # Wrapped in a dict so it reaches the scanner under its
                # own key, as `session_context.hydric_floodplain_is_
                # fallback` -- the flag the trees, roads and solar steps
                # all read as `floodplain_data_is_fallback`.
                "session_context": {
                    "hydric_floodplain_is_fallback": getattr(
                        context, "hydric_floodplain_is_fallback", None
                    ),
                },
            }
        )
        dem = getattr(context, "dem", None)
        patches, patches_truncated = _collect_patches(result, dem)
        features, features_truncated = _collect_wire_features(payload, dem)
        append_event(
            probe.session_id,
            {
                "event": "generate",
                "step_id": probe.step_id,
                # --- group 2 -------------------------------------------
                "inputs": {
                    **probe.provenance,
                    "flags": scanned["flags"],
                    "scan_truncated": scanned["scan_truncated"],
                },
                # --- group 3 -------------------------------------------
                #
                # narrative_data VERBATIM. It is the pipeline's own
                # pre-computed, JSON-serialisable account of what each
                # stage considered and what survived; copying it whole is
                # the only capture of it that cannot drift. null -- not
                # {} -- for a step that publishes none, which says "this
                # step does not produce one" rather than "it produced an
                # empty one".
                "gates": {
                    "narrative_data": (
                        result.get("narrative_data") if isinstance(result, dict) else None
                    ),
                    "counts": scanned["counts"],
                    # EVERY DROP REASON, not just how many drops. See
                    # _Scan._record_drops(): the count is in `counts`
                    # beside this and answers a different question.
                    "drops": scanned["drops"],
                },
                # --- group 4 -------------------------------------------
                "geometry": {
                    # The CRS every `roundtrip` verdict below was reached
                    # in -- see _dem_frame(). Recorded on the generate as
                    # well as the commit because a patch whose verdicts
                    # DISAGREE is reproducible from this event alone.
                    "dem": _dem_frame(dem),
                    "patches": patches,
                    "patches_truncated": patches_truncated,
                    "wire_features": features,
                    "wire_features_truncated": features_truncated,
                },
            },
        )
    except Exception as exc:
        if strict():
            raise
        _report_failure(f"record_generate({probe.session_id!r}, {probe.step_id!r})", exc)


def record_commit(session_id, step_id, features, context, rejection=None) -> None:
    """
    Group 4 for one commit, and THE LINE THIS BRANCH EXISTS FOR: on a
    rejection, the offending feature's FULL GeoJSON, verbatim, in the
    record.

    `gate_outcome` is the GEOMETRY GATE's verdict and is named for that
    precisely -- "accepted" means commit_validation.check_commit()
    returned, not that the write landed. A commit can still fail after
    this point on a stale base_revision, which is a concurrency fact and
    not a geometry one; conflating the two under a key called "committed"
    would make the record answer a question it did not ask.

    THE FULL DUMP IS THE DELIVERABLE. A rejection message names the
    defect and the feature id; it does not carry the ring that produced
    it. Without the geometry, an intermittent `Self-intersection` is
    gone the moment the request ends and the next attempt is a fresh
    guess. With it, the case is a permanent fixture that can be replayed
    against a different GEOS, a different PROJ or a fixed rehydrator.
    ONLY the rejected features are dumped -- an accepted commit's
    geometry is already recoverable from the Design Document, and
    dumping every commit's rings would bury the one that matters.
    """
    if not enabled():
        return
    try:
        dem = getattr(context, "dem", None)
        feature_list = []
        if isinstance(features, dict) and isinstance(features.get("features"), list):
            feature_list = features["features"]

        rejections = []
        rejected_ids = set()
        if rejection is not None:
            # as_payload() is the error's OWN rendering, the same body
            # the 422 carries. Not re-derived from the rejection objects:
            # the record and the response then cannot disagree about
            # which feature was refused or why.
            rejections = rejection.as_payload().get("rejections", [])
            rejected_ids = {entry.get("feature_id") for entry in rejections}

        append_event(
            session_id,
            {
                "event": "commit",
                "step_id": step_id,
                "gate_outcome": "rejected" if rejection is not None else "accepted",
                "rejections": rejections,
                "geometry": {
                    # THE FRAME `rejected_features_geojson` BELOW IS
                    # REPLAYED IN. Without it the dump is coordinates
                    # nobody can re-run. See _dem_frame().
                    "dem": _dem_frame(dem),
                    "committed_features": [
                        {"source": f"features[{index}]", **_feature_health(feature, dem)}
                        for index, feature in enumerate(feature_list[:_MAX_RECORDED_GEOMETRIES])
                        if isinstance(feature, dict)
                    ],
                    "committed_features_truncated": len(feature_list) > _MAX_RECORDED_GEOMETRIES,
                },
                "rejected_features_geojson": [
                    feature
                    for feature in feature_list
                    if isinstance(feature, dict) and feature.get("id") in rejected_ids
                ],
            },
        )
    except Exception as exc:
        if strict():
            raise
        _report_failure(f"record_commit({session_id!r}, {step_id!r})", exc)


# ======================================================================
# self_check() -- one command that says why nothing is being written
# ======================================================================


def _hook_sites() -> dict:
    """
    Whether the hooks are wired into the step_orchestrator THIS
    INTERPRETER IS RUNNING -- read off each function's COMPILED CODE
    OBJECT.

    THAT DISTINCTION IS THE WHOLE POINT, and it is why this does not use
    inspect.getsource(). `grep step_orchestrator.py` answers "does the
    CHECKOUT have the hooks", which is a different question from "does
    the RUNNING PROCESS have them" the moment a long-lived server
    imported that module before the checkout changed -- and
    inspect.getsource() answers the checkout's question too, because it
    reads the file off disk through linecache rather than reading the
    loaded function. On a server running yesterday's code both would
    report the hooks present while none of them execute, which is
    exactly the silence this check exists to explain.

    `co_names` is the compiled tuple of global and attribute names the
    function body actually references. A wired _generate loads the global
    `run_diagnostics` and reads `begin_generate` off it, so both names
    are in there; an unwired one has neither, whatever the file says.
    """
    import step_orchestrator

    sites = {}
    for name, attribute in (
        ("_generate.begin_generate", "begin_generate"),
        ("_generate.record_generate", "record_generate"),
        ("_generate_accumulated.record_generate", "record_generate"),
        ("commit_step.record_commit", "record_commit"),
    ):
        function = getattr(step_orchestrator, name.split(".")[0], None)
        code = getattr(function, "__code__", None)
        if code is None:
            sites[name] = None
            continue
        sites[name] = "run_diagnostics" in code.co_names and attribute in code.co_names
    return sites


def self_check(stream=None) -> bool:
    """
    Print why this module is or is not writing, and return whether a
    record could be written right now. `python3 run_diagnostics.py`.

    WRITTEN BECAUSE THE FEATURE CAN FAIL SILENTLY AND DID. Every
    individual check a person would run by hand -- is the variable set,
    does enabled() say yes, is the directory writable, are the hooks in
    the file -- can pass while nothing is written, because none of them
    asks the two questions that actually decide it: what does the LOADED
    orchestrator call, and does an ACTUAL append_event() succeed from
    THIS process's working directory. Both are asked below, the second by
    really writing a record and deleting it.

    Everything is reported as it is found, including the raw environment
    strings, because "KEYLINE_RUN_DIAGNOSTICS=1 " with a trailing space
    and an unset variable are different problems with the same symptom.
    """
    out = stream if stream is not None else sys.stdout
    ok = True

    def say(label, value, good=None):
        nonlocal ok
        mark = "    " if good is None else ("[ok] " if good else "[!!] ")
        if good is False:
            ok = False
        print(f"{mark}{label}: {value}", file=out)

    print("run_diagnostics self-check", file=out)
    print(f"  module file: {os.path.abspath(__file__)}", file=out)

    raw_enabled = os.environ.get(ENABLED_ENV)
    say(f"{ENABLED_ENV} (raw)", repr(raw_enabled))
    say("enabled()", enabled(), enabled())
    say(f"{STRICT_ENV} (raw)", repr(os.environ.get(STRICT_ENV)))
    say("strict()", strict())

    raw_directory = os.environ.get(DIRECTORY_ENV)
    say(f"{DIRECTORY_ENV} (raw)", repr(raw_directory))
    say("directory()", repr(directory()))
    say("working directory", os.getcwd())
    say("resolved absolute path", os.path.abspath(directory()))

    # GUARDED, because importing step_orchestrator pulls in the whole
    # pipeline and a self-check that dies on an unrelated missing
    # dependency tells the person nothing about the question they came
    # with. A failure here is reported as the failure it is.
    try:
        sites = _hook_sites()
    except Exception as exc:
        say("hooks in loaded step_orchestrator", f"{type(exc).__name__}: {exc}", False)
        sites = {}
    for name, wired in sorted(sites.items()):
        say(f"hook wired in loaded step_orchestrator.{name}", wired, bool(wired))

    # THE REAL WRITE. Not os.access() -- a permission bit that says yes
    # and a write that fails are both things that happen, and only one of
    # them is what this module does.
    probe_id = "selfcheck0000000000000"
    try:
        append_event(probe_id, {"event": "self_check", "step_id": None})
        written = os.path.exists(record_path(probe_id))
        say("append_event() wrote a record", written, written)
        os.unlink(record_path(probe_id))
    except Exception as exc:
        say("append_event() wrote a record", f"{type(exc).__name__}: {exc}", False)

    if FAILURES:
        print(f"  {len(FAILURES)} swallowed failure(s) in this process:", file=out)
        for failure in FAILURES:
            print(f"    {failure['what']}: {failure['error']}", file=out)
    else:
        say("swallowed failures in this process", 0)

    print(f"\n  {'RECORDS WOULD BE WRITTEN' if ok else 'RECORDS WOULD NOT BE WRITTEN'}", file=out)
    return ok


if __name__ == "__main__":
    sys.exit(0 if self_check() else 1)
