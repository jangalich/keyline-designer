"""
keypoint_detection.py

EXPERIMENTAL (branch: keypoint-sited-water-zones). Detection only -- no
siting, no zone growth, no rendering (that is water_candidate_zones.py's
flag-gated Part 2). Locates Yeomans' KEYPOINT in each primary valley's
long profile: the inflection where the steep upper slope breaks to the
flatter valley floor -- the lowest point of the steep slope, equivalently
the highest point of the flat floor below it. It is where a short dam wall
impounds the most water at the highest useful elevation, and the reference
line for keyline cultivation layout.

Pipeline (each step reuses valley_delineation.py rather than reimplementing
D8 hydrology):

    fill_depressions() -> compute_flow_direction() -> compute_flow_
    accumulation()                                   [valley_delineation]
        --> delineate_valleys() for primary-valley branches (head -> outlet)
        --> invert the flow-direction arrays into an UPSTREAM map (each cell
            -> the cells that drain directly into it)
        --> per branch, EXTEND the thalweg upstream from its head, always
            taking the highest-accumulation feeder, until the valley runs out
        --> sample RAW elevation + cumulative distance along the extended
            thalweg, smooth, take cell-to-cell slope percent, smooth again
        --> at each position with enough profile either side, compare mean
            slope above (steep) against mean slope below (flat); the keypoint
            is where that drop is largest
        --> hard gates + dedup -> one dict per surviving candidate

TWO FIXES CARRIED OVER FROM THE PROTOTYPE (both mandatory, both were
diagnosed from earlier failed attempts):

  1. Profile the RAW elevation array, never the FILLED one.
     delineate_valleys() builds branches_utm from filled[r, c], and
     fill_depressions() raises every pit to its spill elevation, so a marsh
     reads as a dead-flat plateau and any inflection detector fires on the
     steep-to-flat transition ENTERING the fill -- an artifact of the
     filling, not landform. Flow direction and accumulation still use the
     filled array (that is what makes them well-defined); ONLY the elevation
     profile uses raw dem['array']. The fill-artifact gate below
     (KEYPOINT_FILL_ARTIFACT_THRESHOLD_M) is a SECOND, independent defense:
     the inflection on a filled profile lands at the pit RIM, where fill
     depth is ~0, so the fill gate alone would not catch it -- raw profiling
     is what does. See test_keypoint_detection.py's raw-vs-filled contrast.

  2. Extend each thalweg upstream past the stream threshold.
     Branches begin only once contributing area reaches valley_delineation.
     MIN_STREAM_CONTRIBUTING_AREA_ACRES (0.5 ac), which on a small parcel is
     already BELOW where the keypoint sits -- so the traced profile may not
     contain the inflection at all. The upstream walk (highest-accumulation
     feeder each step) recovers it. On the reference property this added
     9-48 cells per branch, and many keypoints landed inside that extension.

CONSTANT TUNING CAVEAT (repeated in every constant's docstring): all of the
KEYPOINT_* thresholds below were tuned against TWO independently-drawn
boundaries of a SINGLE ~16-acre reference property in a prototype. They have
NOT been validated on any other property. Treat them as a first calibration,
not a settled default -- the reviewer will retune against real renders.

find_candidate detection is a PURE function over an already-fetched dem plus
already-computed production_areas/boundary (detect_keypoints()), so the
detection logic ("is the inflection math right") is unit-testable against a
small synthetic DEM independently of Stage 1 ("is the DEM/valley delineation
accurate") -- the same split every other terrain module in this pipeline
uses. identify_keypoints() is the network entry point that fetches/derives
the inputs when a caller does not already hold them.

This module deliberately does NOT touch pipeline_context.py: it has no
consumer until water_candidate_zones.py's KEYPOINT_SITED_ZONES flag is on,
and a context field nothing reads would violate the sizing principle.
"""

import logging
import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.prepared import prep

from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from raster_grid import cell_area_acres, pixel_center_xy
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    delineate_valleys,
    fill_depressions,
)

_LOGGER = logging.getLogger(__name__)


# Cells over which the raw elevation profile (and, separately, the derived
# slope profile) is smoothed with a centered moving average before the
# inflection search -- enough to damp single-cell DEM noise without erasing a
# real slope break. TUNED against two boundaries of one ~16-acre reference
# property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_PROFILE_SMOOTH_CELLS = 5

# Minimum run of cells the STEEP upper slope must occupy just above a
# candidate for it to be a real inflection rather than a one-cell blip.
# TUNED against two boundaries of one ~16-acre reference property; NOT
# validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_STEEP_RUN_CELLS = 6

# Minimum run of cells the FLAT valley floor must occupy just below a
# candidate -- the impoundable ground a dam at the keypoint would back water
# over. TUNED against two boundaries of one ~16-acre reference property; NOT
# validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_FLAT_RUN_CELLS = 6

# Minimum drop (mean steep-slope-percent minus mean flat-slope-percent) for a
# candidate to count as a real inflection at all -- below this the profile is
# effectively straight, not broken. TUNED against two boundaries of one
# ~16-acre reference property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_SLOPE_DROP_PCT = 4.0

# The marsh gate. A candidate whose fill depth (filled minus raw elevation at
# the keypoint cell) exceeds this is sitting on filled ground -- a pit/marsh
# the priority-flood raised to a flat plateau, whose steep-to-flat entrance is
# a filling artifact, not landform. Small and nonzero so genuine near-zero
# fill (a cell barely touched by the flood) still passes. TUNED against two
# boundaries of one ~16-acre reference property; NOT validated elsewhere.
# CONFIGURABLE.
KEYPOINT_FILL_ARTIFACT_THRESHOLD_M = 0.15

# Lower bound of the catchment window. Below this a keypoint impounds nothing
# worth a dam. This is the gate that did the real work in the prototype
# (cutting 29 raw candidates to 4), rejecting hillside convexities with no
# water above them. DEFINED SEPARATELY from water_candidate_zones.
# MAX_VALLEY_CONTRIBUTING_AREA_ACRES even where numerically equal, per this
# codebase's standing convention, so either can be retuned without silently
# moving the other. TUNED against two boundaries of one ~16-acre reference
# property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_MIN_CATCHMENT_ACRES = 5.0

# Upper bound of the catchment window. Above this a pond silts in and needs
# engineered spillway capacity (cf. water_candidate_zones.
# MAX_VALLEY_CONTRIBUTING_AREA_ACRES / NRCS CPS 378), so it is not a simple
# keypoint dam site. DEFINED SEPARATELY from that constant even though it
# starts numerically equal, per this codebase's standing convention, so
# either can be retuned independently. TUNED against two boundaries of one
# ~16-acre reference property; NOT validated elsewhere. CONFIGURABLE.
KEYPOINT_MAX_CATCHMENT_ACRES = 20.0

# Convergent branches (_trace_branches() emits one branch per head, and
# branches share cells near a confluence) report one real keypoint once per
# tributary passing through it. Within this radius, keep only the strongest
# slope drop. On the reference property this removed nothing -- the catchment
# window had already deduplicated implicitly (convergent branches carry
# different accumulation, so only one per convergence lands in [5, 20] ac) --
# but the step is kept for parcels with two genuine keypoints close together.
# TUNED against two boundaries of one ~16-acre reference property; NOT
# validated elsewhere. CONFIGURABLE.
KEYPOINT_DEDUP_RADIUS_METERS = 25.0


KEYPOINT_CONFIDENCE_NOTES = (
    "EXPERIMENTAL keypoint detection. A keypoint is the inflection in a "
    "primary valley's long profile -- the break from steep upper slope to "
    "flatter valley floor (Yeomans) -- located here from DEM-derived D8 flow "
    "direction/accumulation and a RAW-elevation long profile sampled along "
    "the valley thalweg (extended upstream past the stream-delineation "
    "threshold). It is NOT surveyed or field-verified, inherits every "
    "limitation of the DEM and the D8 valley delineation beneath it (see "
    "valley_delineation.py), and every detection threshold was tuned against "
    "two boundaries of a single ~16-acre reference property and has NOT been "
    "validated elsewhere. Treat a keypoint as a starting point to walk and "
    "ground-truth, and as a candidate dam/cultivation reference line, not a "
    "final siting."
)


def build_upstream_map(
    flow_to_row: np.ndarray, flow_to_col: np.ndarray
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """
    Inverts compute_flow_direction()'s (flow_to_row, flow_to_col) arrays into
    an upstream adjacency map: each key cell (r, c) maps to the list of cells
    that flow DIRECTLY into it (its immediate feeders). A cell with no
    upstream feeders (a ridge/source cell) simply never appears as a key.

    This is the exact inverse of the downstream flow field -- flow direction
    says where a cell drains TO; this says what drains INTO a cell -- and is
    what both the upstream thalweg extension (highest-accumulation feeder
    walk) and, in Part 2, the impoundment's upstream-contributing set are
    walked over. Cells with flow_to_row < 0 (grid-edge outlets / flat-plateau
    ties, compute_flow_direction()'s -1 sentinel) have no downstream target
    and contribute no upstream edge.
    """
    rows, cols = flow_to_row.shape
    upstream: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for r in range(rows):
        for c in range(cols):
            tr = int(flow_to_row[r, c])
            tc = int(flow_to_col[r, c])
            if tr < 0:
                continue
            upstream.setdefault((tr, tc), []).append((r, c))
    return upstream


def upstream_contributing_cells(
    seed: tuple[int, int],
    upstream_map: dict[tuple[int, int], list[tuple[int, int]]],
) -> set[tuple[int, int]]:
    """
    Every cell that drains THROUGH `seed` -- the seed itself plus, recursively,
    every cell that (transitively) flows into it -- collected by walking the
    upstream_map from the seed. A cell drains through itself, so the seed is
    always included. Because compute_flow_direction() only ever points a cell
    at a strictly lower (or off-grid) neighbor, the upstream map is a DAG and
    this BFS terminates; no downstream cell is ever reachable, which is
    exactly the property Part 2's impoundment relies on (a dam at the seed
    floods what is upstream of it, never downstream).
    """
    seen = {seed}
    stack = [seed]
    while stack:
        cell = stack.pop()
        for feeder in upstream_map.get(cell, ()):
            if feeder not in seen:
                seen.add(feeder)
                stack.append(feeder)
    return seen


def extend_thalweg_upstream(
    branch_rowcol: list[tuple[int, int]],
    upstream_map: dict[tuple[int, int], list[tuple[int, int]]],
    flow_accumulation: np.ndarray,
) -> list[tuple[int, int]]:
    """
    Extends one delineated branch (ordered head -> outlet) upstream past its
    head, returning the FULL thalweg ordered upstream -> downstream
    (topmost extension cell first, branch outlet last).

    Walks up from the branch head, at each step taking the highest-flow-
    accumulation feeder (the main upstream channel) among the cells that drain
    into the current cell, until the valley runs out (no feeder left). Cells
    already on the branch are excluded from the walk so it never doubles back.

    This is fix #2 from the module docstring: the traced branch begins only
    once contributing area reaches the stream threshold, which on a small
    parcel can be below the keypoint, so the raw branch may not contain the
    inflection; the extension recovers the profile above the threshold.
    """
    visited = set(branch_rowcol)
    extension: list[tuple[int, int]] = []
    current = branch_rowcol[0]  # the head (branch is head -> outlet)
    while True:
        feeders = [f for f in upstream_map.get(current, ()) if f not in visited]
        if not feeders:
            break
        best = max(feeders, key=lambda f: (float(flow_accumulation[f[0], f[1]]), -f[0], -f[1]))
        extension.append(best)
        visited.add(best)
        current = best

    # extension is ordered head-ward -> upstream; reverse it so the full
    # thalweg reads upstream (top) -> downstream (outlet).
    return list(reversed(extension)) + list(branch_rowcol)


def _centered_moving_average(values: np.ndarray, window_cells: int) -> np.ndarray:
    """
    Centered moving average with an edge-clamped (shrinking) window, so no
    wrap-around or zero-padding artifact is introduced at the profile ends.
    window_cells <= 1 returns the values unchanged. The half-width is
    window_cells // 2 on each side.
    """
    n = len(values)
    if window_cells <= 1 or n == 0:
        return np.asarray(values, dtype=float)
    half = window_cells // 2
    out = np.empty(n, dtype=float)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = float(np.mean(values[lo:hi]))
    return out


def _profile_along_thalweg(
    thalweg: list[tuple[int, int]],
    elevation_array: np.ndarray,
    dem: dict,
    smooth_cells: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Builds the (smoothed elevation, smoothed slope-percent) profile along the
    thalweg cells (ordered upstream -> downstream). Elevation is sampled from
    `elevation_array` at each cell (RAW dem['array'] on the real path -- see
    module docstring fix #1); cumulative distance is real ground distance
    between successive cell centers (pixel_center_xy(), so diagonal steps cost
    sqrt(2)x a cardinal one). Elevation is smoothed, cell-to-cell descent
    slope percent (rise/run*100, positive downhill) is taken, then the slope
    is smoothed again.

    Returns (smoothed_elevation[len n], smoothed_slope_pct[len n-1]); the
    slope at index i is the descent from thalweg cell i to cell i+1.
    """
    elevations = np.array([float(elevation_array[r, c]) for r, c in thalweg], dtype=float)
    xys = [pixel_center_xy(dem, r, c) for r, c in thalweg]

    smoothed_elev = _centered_moving_average(elevations, smooth_cells)

    slopes = np.empty(len(thalweg) - 1, dtype=float)
    for i in range(len(thalweg) - 1):
        (x0, y0), (x1, y1) = xys[i], xys[i + 1]
        run = math.hypot(x1 - x0, y1 - y0)
        drop = smoothed_elev[i] - smoothed_elev[i + 1]
        slopes[i] = (drop / run * 100.0) if run > 0 else 0.0

    smoothed_slope = _centered_moving_average(slopes, smooth_cells)
    return smoothed_elev, smoothed_slope


def _best_inflection(
    slope_pct: np.ndarray,
    steep_run: int,
    flat_run: int,
) -> Optional[tuple[int, float, float, float]]:
    """
    Finds the thalweg position with the largest steep->flat slope drop.

    slope_pct[i] is the descent between thalweg cell i and i+1. For a
    candidate keypoint at cell index k, the steep upper slope is the `steep_run`
    slopes just above it (indices k-steep_run .. k-1) and the flat floor is the
    `flat_run` slopes just below it (indices k .. k+flat_run-1). k therefore
    ranges over [steep_run, len(slope_pct) - flat_run]; cell k is the pivot --
    the lowest point of the steep slope and the highest point of the flat floor
    below it, which is exactly Yeomans' keypoint.

    Returns (k, slope_above_pct, slope_below_pct, slope_drop_pct) for the k
    with the maximum (slope_above - slope_below), or None if the profile is
    too short to hold both runs (the insufficient-run gate, in executable
    form).
    """
    n = len(slope_pct)
    best = None
    for k in range(steep_run, n - flat_run + 1):
        slope_above = float(np.mean(slope_pct[k - steep_run:k]))
        slope_below = float(np.mean(slope_pct[k:k + flat_run]))
        drop = slope_above - slope_below
        if best is None or drop > best[3]:
            best = (k, slope_above, slope_below, drop)
    return best


def _dedup_keypoints(
    survivors: list[dict], dedup_radius_meters: float
) -> tuple[list[dict], int]:
    """
    Keeps, within dedup_radius_meters, only the candidate with the strongest
    slope drop -- the convergence dedup described in the module docstring.
    Greedy: sort by slope drop descending, keep a candidate only if it is not
    within the radius of an already-kept one. Returns (kept, removed_count).
    """
    ordered = sorted(survivors, key=lambda c: -c["slope_drop_pct"])
    kept: list[dict] = []
    for cand in ordered:
        pt = cand["point_utm"]
        if any(pt.distance(k["point_utm"]) <= dedup_radius_meters for k in kept):
            continue
        kept.append(cand)
    return kept, len(survivors) - len(kept)


def detect_keypoints(
    dem: dict,
    boundary_polygon_utm,
    production_areas: list[dict],
    *,
    flow_to_row: Optional[np.ndarray] = None,
    flow_to_col: Optional[np.ndarray] = None,
    flow_accumulation: Optional[np.ndarray] = None,
    filled: Optional[np.ndarray] = None,
    valleys: Optional[list[dict]] = None,
    profile_smooth_cells: int = KEYPOINT_PROFILE_SMOOTH_CELLS,
    min_steep_run_cells: int = KEYPOINT_MIN_STEEP_RUN_CELLS,
    min_flat_run_cells: int = KEYPOINT_MIN_FLAT_RUN_CELLS,
    min_slope_drop_pct: float = KEYPOINT_MIN_SLOPE_DROP_PCT,
    fill_artifact_threshold_m: float = KEYPOINT_FILL_ARTIFACT_THRESHOLD_M,
    min_catchment_acres: float = KEYPOINT_MIN_CATCHMENT_ACRES,
    max_catchment_acres: float = KEYPOINT_MAX_CATCHMENT_ACRES,
    dedup_radius_meters: float = KEYPOINT_DEDUP_RADIUS_METERS,
    extend_upstream: bool = True,
    diagnostics: Optional[dict] = None,
    _profile_from_filled: bool = False,
) -> list[dict]:
    """
    Detects keypoints on `dem`, returning one dict per surviving candidate
    (or [] when nothing survives -- the honest empty answer, never a relaxed
    gate). Pure and network-free: dem, boundary_polygon_utm, and
    production_areas are the real inputs; flow_to_row/flow_to_col/
    flow_accumulation/filled/valleys are OPTIONAL overrides that each
    self-compute from `dem` (via valley_delineation.py) when not supplied,
    and every supplied override is forwarded into the internal computation
    rather than recomputed -- the same self-computing override pattern
    identify_water_system_candidate_zones() follows. A caller that already
    holds these (e.g. a shared pipeline pass) passes them straight through.

    production_areas is the list identify_production_areas() returns; only
    each patch's 'render_fill_polygon_utm' is read here (the production gate
    -- the drawn, opened geometry, the same field every other downstream KSOP
    step weighs against, NOT 'polygon_utm').

    Gates -- a candidate is rejected if ANY applies:
      * slope drop below min_slope_drop_pct (not a real inflection);
      * insufficient run either side (min_steep_run_cells above,
        min_flat_run_cells below -- enforced by _best_inflection() returning
        None for a too-short profile);
      * fill depth (filled - raw) above fill_artifact_threshold_m at the
        keypoint cell -- the marsh gate;
      * contributing catchment outside [min_catchment_acres,
        max_catchment_acres];
      * off-parcel -- the DEM is buffered, so the upstream walk routinely
        leaves the boundary; a dam you cannot build on is not a candidate;
      * inside any production area's render_fill_polygon_utm (the drawn,
        opened geometry) -- inside is rejected, outside kept regardless of
        distance.
    Then dedup: within dedup_radius_meters, keep the strongest slope drop.

    extend_upstream (default True) toggles fix #2 -- with it off the raw
    delineated branch is profiled directly, which is only exposed so a test
    can show the inflection above the stream threshold is missed without the
    extension. Leave it True on any real path.

    diagnostics, if a dict is passed, is populated in place with the raw
    candidate count and the per-gate rejection breakdown (including the
    catchment-low vs catchment-high split and the dedup removal count) -- a
    reporting hook only; it does not affect the return value. _profile_from_
    filled is a test-only hook that profiles the FILLED array instead of raw,
    to demonstrate fix #1's failure mode; never set it on a real path.

    Each returned dict (geometry-first, per this codebase's convention):
        {
            'id': int,
            'rowcol': (row, col),
            'point_utm': shapely Point,
            'geometry_wgs84': GeoJSON Point,
            'elevation_m': float,          # RAW elevation at the keypoint
            'contributing_acres': float,
            'slope_above_pct': float,
            'slope_below_pct': float,
            'slope_drop_pct': float,
            'valley_id': int,
            'confidence': str,
            'confidence_notes': str,
        }
    """
    if filled is None:
        filled = fill_depressions(dem["array"])
    if flow_to_row is None or flow_to_col is None:
        flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    if flow_accumulation is None:
        flow_accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)
    if valleys is None:
        valleys = delineate_valleys(dem)

    raw_array = dem["array"]
    profile_array = filled if _profile_from_filled else raw_array
    area_per_cell = cell_area_acres(dem)

    upstream_map = build_upstream_map(flow_to_row, flow_to_col)

    boundary_prepared = prep(boundary_polygon_utm)
    production_footprints = [
        patch["render_fill_polygon_utm"]
        for patch in production_areas
        if patch.get("render_fill_polygon_utm") is not None
    ]
    production_union = unary_union(production_footprints) if production_footprints else None
    production_prepared = prep(production_union) if production_union is not None else None

    stats = {
        "raw_candidates": 0,
        "rejected_insufficient_run": 0,
        "rejected_slope_drop": 0,
        "rejected_fill_artifact": 0,
        "rejected_catchment_low": 0,
        "rejected_catchment_high": 0,
        "rejected_off_parcel": 0,
        "rejected_production": 0,
        "dedup_removed": 0,
        "surviving": 0,
    }

    survivors: list[dict] = []
    for valley in valleys:
        for branch in valley["branches_rowcol"]:
            if extend_upstream:
                thalweg = extend_thalweg_upstream(branch, upstream_map, flow_accumulation)
            else:
                thalweg = list(branch)

            if len(thalweg) < min_steep_run_cells + min_flat_run_cells + 1:
                stats["rejected_insufficient_run"] += 1
                continue

            _, slope_pct = _profile_along_thalweg(
                thalweg, profile_array, dem, profile_smooth_cells
            )
            inflection = _best_inflection(slope_pct, min_steep_run_cells, min_flat_run_cells)
            if inflection is None:
                stats["rejected_insufficient_run"] += 1
                continue

            k, slope_above, slope_below, slope_drop = inflection
            stats["raw_candidates"] += 1

            r, c = thalweg[k]
            rejected = False

            if slope_drop < min_slope_drop_pct:
                stats["rejected_slope_drop"] += 1
                rejected = True

            fill_depth = float(filled[r, c]) - float(raw_array[r, c])
            if fill_depth > fill_artifact_threshold_m:
                stats["rejected_fill_artifact"] += 1
                rejected = True

            contributing_acres = float(flow_accumulation[r, c]) * area_per_cell
            if contributing_acres < min_catchment_acres:
                stats["rejected_catchment_low"] += 1
                rejected = True
            elif contributing_acres > max_catchment_acres:
                stats["rejected_catchment_high"] += 1
                rejected = True

            x, y = pixel_center_xy(dem, r, c)
            point = Point(x, y)
            if not boundary_prepared.contains(point):
                stats["rejected_off_parcel"] += 1
                rejected = True

            if production_prepared is not None and production_prepared.contains(point):
                stats["rejected_production"] += 1
                rejected = True

            if rejected:
                continue

            lon, lat = warp_transform(dem["crs"], "EPSG:4326", [x], [y])
            survivors.append(
                {
                    "rowcol": (r, c),
                    "point_utm": point,
                    "geometry_wgs84": {"type": "Point", "coordinates": (lon[0], lat[0])},
                    "elevation_m": round(float(raw_array[r, c]), 2),
                    "contributing_acres": round(contributing_acres, 2),
                    "slope_above_pct": round(slope_above, 2),
                    "slope_below_pct": round(slope_below, 2),
                    "slope_drop_pct": round(slope_drop, 2),
                    "valley_id": int(valley["id"]),
                    "confidence": CONFIDENCE_LOW,
                    "confidence_notes": KEYPOINT_CONFIDENCE_NOTES,
                }
            )

    kept, dedup_removed = _dedup_keypoints(survivors, dedup_radius_meters)
    stats["dedup_removed"] = dedup_removed
    stats["surviving"] = len(kept)

    kept.sort(key=lambda cand: -cand["slope_drop_pct"])
    for new_id, cand in enumerate(kept):
        cand["id"] = new_id

    if diagnostics is not None:
        diagnostics.update(stats)

    _LOGGER.info(
        "keypoint detection: raw=%d rejected(run=%d slope=%d fill=%d catch_low=%d "
        "catch_high=%d off_parcel=%d production=%d) dedup_removed=%d surviving=%d",
        stats["raw_candidates"],
        stats["rejected_insufficient_run"],
        stats["rejected_slope_drop"],
        stats["rejected_fill_artifact"],
        stats["rejected_catchment_low"],
        stats["rejected_catchment_high"],
        stats["rejected_off_parcel"],
        stats["rejected_production"],
        stats["dedup_removed"],
        stats["surviving"],
    )

    return kept


def keypoints_to_geojson(keypoints: list[dict]) -> dict:
    """
    Wraps detect_keypoints() output as a schema-conformant GeoJSON
    FeatureCollection (layer="keypoint"), following valleys_to_geojson()'s
    shape. Each feature carries the keypoint's real measured values
    (elevation, contributing catchment, the three slope figures) as
    properties -- geometry-first, no narrative.
    """
    features = [
        make_feature(
            feature_id=f"keypoint-{k['id']}",
            geometry=k["geometry_wgs84"],
            layer="keypoint",
            label=f"Keypoint {k['id']}",
            confidence=k["confidence"],
            confidence_notes=k["confidence_notes"],
            extra_properties={
                "elevation_m": k["elevation_m"],
                "contributing_acres": k["contributing_acres"],
                "slope_above_pct": k["slope_above_pct"],
                "slope_below_pct": k["slope_below_pct"],
                "slope_drop_pct": k["slope_drop_pct"],
                "valley_id": k["valley_id"],
            },
        )
        for k in keypoints
    ]
    return make_feature_collection(features)


def identify_keypoints(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    boundary_polygon_utm=None,
    valleys: Optional[list[dict]] = None,
    production_areas: Optional[list[dict]] = None,
    canopy_height: Optional[dict] = None,
    **detect_kwargs,
) -> dict:
    """
    Network entry point mirroring identify_water_system_candidate_zones():
    fetches/derives whatever the caller does not already hold, then runs
    detect_keypoints(). Every real input -- dem, boundary_polygon_utm,
    valleys, production_areas, canopy_height -- is an OPTIONAL override that
    self-computes when not supplied, independently of the others, and each is
    forwarded into every internal call (including production_areas' own canopy
    consumer) rather than re-fetched. Returns:

        {
            'keypoints_geojson': FeatureCollection,  # layer="keypoint"
            'valleys_geojson': FeatureCollection,    # layer="valley" (diagnostic)
        }

    This path needs the network (DEM + production-area canopy/soil/road
    fetches); the offline unit tests drive detect_keypoints() directly with
    synthetic inputs instead.
    """
    # Imported lazily so the offline detect_keypoints() path (and its tests)
    # never import the network layer just to reach the pure detector.
    from dem_data import get_dem_for_boundary
    from production_area import identify_production_areas
    from valley_delineation import valleys_to_geojson

    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    if boundary_polygon_utm is None:
        from shapely.geometry import Polygon

        boundary_xs, boundary_ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    if valleys is None:
        valleys = delineate_valleys(dem)

    if production_areas is None:
        production_areas = identify_production_areas(
            dem, boundary_polygon_utm, canopy_height=canopy_height
        )

    keypoints = detect_keypoints(
        dem,
        boundary_polygon_utm,
        production_areas,
        valleys=valleys,
        **detect_kwargs,
    )

    return {
        "keypoints_geojson": keypoints_to_geojson(keypoints),
        "valleys_geojson": valleys_to_geojson(valleys),
    }


def summarize_keypoints(keypoints: list[dict]) -> str:
    if not keypoints:
        return (
            "No keypoints detected on this property (no valley long profile "
            "held a qualifying steep-to-flat inflection inside the catchment "
            "window, on-parcel, clear of production) -- the honest empty "
            "answer, not a relaxed gate."
        )
    lines = [f"Keypoints detected: {len(keypoints)}"]
    for k in keypoints:
        lines.append(
            f"  - Keypoint {k['id']} (valley {k['valley_id']}): "
            f"{k['elevation_m']:.1f} m, {k['contributing_acres']:.1f} ac catchment, "
            f"slope drop {k['slope_drop_pct']:.1f}% "
            f"({k['slope_above_pct']:.1f}% -> {k['slope_below_pct']:.1f}%)"
        )
    return "\n".join(lines)
