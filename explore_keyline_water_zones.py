"""
explore_keyline_water_zones.py

STANDALONE DIAGNOSTIC EXPLORATION -- the keyline reading of water design,
generated in full so the keep/throw rules can be designed from evidence.
Same family as diagnose_water_zone_mask.py: a permanent, read-only script
that reports what the terrain says. IT IS NOT THE PIPELINE, IT IS NOT THE
FINAL DESIGN, AND NOTHING IMPORTS IT. water_candidate_zones.py,
water_suitability.py, pipeline_context.py, render_layout_map.py and every
batch consumer are untouched by this file's existence.

THE GOVERNING RULE OF THIS PASS IS: FLAGS, NOT FILTERS. Every rule that
DROPS a candidate in the production path is a REPORTED ATTRIBUTE here.
There is no area floor, no seed separation, no contributing-area ceiling
acting as a gate, no on-parcel requirement, and no overlap trim. A pool of
0.004 acres is emitted with its exact acreage; a pool wholly off-parcel is
emitted with an on-parcel fraction of 0.0; a wall walk that dies in a
filled flat is emitted as a point with its walk_end_reason. ZERO CANDIDATES
MAY BE SILENTLY ABSENT: every crossing of every qualifying keyline appears
in the output with an outcome. That is the entire point -- a filtered
picture cannot tell you what the filter was throwing away, and the rules
this pass exists to inform are exactly rules about what to throw away.

WHAT IT DOES, in the five steps the design names:

  STEP 1  QUALIFYING KEYPOINTS. Each production area's REPRESENTATIVE
          ELEVATION is read straight off production_area.identify_
          production_areas()' own patch dicts -- the SAME
          'representative_elevation_m' (the median RAW elevation over the
          patch's cells) that water_candidate_zones._zone_production_area_
          relationships() differences to decide whether a zone sits above a
          production area. No second definition of "how high is this
          production area" is invented here. A keypoint QUALIFIES if its
          elevation exceeds AT LEAST ONE production area's representative
          elevation, and the ids it clears are recorded. Non-qualifying
          keypoints are printed with their elevations and the margin they
          missed by -- excluded from tracing, visible in the output.

          ON-PARCEL STATUS DOES NOT AFFECT QUALIFICATION. This is a stated
          requirement of the design, not an oversight: an off-parcel
          keypoint's keyline is traced wherever it runs, because the DEM
          covers dem_data.py's buffered extent and a contour that enters
          from next door and crosses this parcel's stems is a real line
          over real ground on this parcel.

  STEP 2  TRACE EACH QUALIFYING KEYLINE. One contour per qualifying
          keypoint, at that keypoint's own elevation, over the FULL DEM
          (keyline_analysis.extract_level_contour()). Polylines that
          intersect the parcel are kept AND SO ARE THEIR OFF-PARCEL
          CONTINUATIONS -- nothing is clipped. The boundary is drawn on the
          map; it is never applied to the data. Keyline pairs within
          KEYLINE_ELEVATION_SIMILARITY_METERS of each other are NOTED
          (Yeomans' observation that a property's keypoints often sit at
          similar elevations, which is what makes one contour serve
          several valleys) but every keyline is still traced independently.

  STEP 3  CROSSINGS. keyline_analysis.find_stem_crossings() against EVERY
          traced branch of every valley. Every crossing is a candidate
          anchor seed -- on-parcel or not, ceiling-clearing or not.

  STEP 4  WALL + FILL PER CROSSING. From each crossing's own traced channel
          cell, water_candidate_zones._find_wall_site() walks downstream to
          the first cell a full POOL_REFERENCE_HEIGHT_METERS below it (the
          production wall walk, reused verbatim -- same constant, same
          walk_end_reason vocabulary). At the wall anchor,
          valley_level_pool.delineate_level_pool() delineates the backwater.

          THE ZONE FOR THIS PASS IS THE POOL ONLY. The dam-axis band and
          the cross-section stations are switched OFF at the call
          (abutment_search_half_width_meters=0.0, station_count=0), because
          keyline_analysis.decompose_pool_perimeter() is this pass's
          measurement layer and it replaces both: it classifies every
          exterior edge of the pool as held by GROUND or held only by a
          PROPOSED WALL, and returns the wall lines themselves. NO BEARING
          IS COMPUTED OR ASSUMED ANYWHERE IN THIS SCRIPT.

          BY CONSTRUCTION the pool's waterline sits at (approximately) the
          keyline elevation -- the wall stands a full reference height
          below the crossing, so anchor + height lands back at it. That
          near-equality is ASSERTED per candidate as an exact arithmetic
          identity, and its two real sources of inexactness are reported
          separately: CELL QUANTIZATION (the crossing's channel cell has
          the DEM's elevation, not the contour's) and WALL OVERSHOOT (the
          walk gives up the height in whole cells, so the last step can
          drop past it).

  STEP 5  MEASURE, FLAG, GROUP, EMIT. Overlap GROUPS -- adjacent crossings
          whose pools share cells are ONE POND IN REALITY -- are found by
          union-find over the pool cell sets and reported as a group id on
          every member. NOTHING IS TRIMMED. The production path would
          delete the later member; here both are drawn, because "these two
          crossings are the same pond" is precisely the evidence a
          separation rule has to be designed against.

OUTPUT. keyline_water_exploration.geojson, feature_schema-compliant, six
layers (keyline, keyline_crossing, keyline_wall_anchor, keyline_pool,
keyline_wall_segment, keyline_keypoint) plus a keyline_run_summary feature
carrying the run-level context including every production area's
representative elevation. Plus the terminal table below. THAT TABLE AND
THAT FILE OVER IMAGERY ARE THE DELIVERABLE.

THE GEOMETRY CONTRACT. Every polyline, point and polygon in this run
stores its WGS84 form BESIDE its UTM form at BUILD time.
build_exploration_geojson() READS those stored wire forms and REPROJECTS
NOTHING -- test_keyline_analysis.py asserts that by inspecting this
function's own source for reprojection calls. The two reprojection calls in
this file are both at FETCH/FIXTURE time (the drawn boundary into the DEM's
CRS, and pool footprints out of it), which is exactly where the contract
puts them.

MODULE PRIVATES ARE IMPORTED, DELIBERATELY. water_candidate_zones.
_find_wall_site() and ._overlap_fraction_pct() are underscore-prefixed and
are used here anyway. A diagnostic exists to report on the REAL machinery;
reimplementing the wall walk publicly-but-separately would give this script
its own quietly-drifting second definition of where a wall stands, which is
the failure mode the whole file is built to avoid. This is a stated,
accepted coupling of a diagnostic to the module it diagnoses -- if either
function's signature changes, this script must be updated with it.

OUT OF SCOPE, FLAGGED AND NOT FIXED HERE:
  * The production pipeline in its entirety. Nothing in generation,
    scoring, rendering or the batch consumers is changed.
  * The pending `valleys` NameError bugfix (its own branch). This script
    never calls the affected entry point -- it drives delineate_valleys(),
    detect_keypoints(), _find_wall_site() and delineate_level_pool()
    directly -- so it runs regardless of whether that branch has landed.
  * The epsilon fill. valley_delineation.py's plain priority-flood plus
    strictly-positive-slope D8 leaves a filled flat unroutable, which
    surfaces here as 'flat_tie_sentinel' wall walks. Those are REPORTED as
    failed walks with their reason, never worked around.
  * The queued export/rendering fixes from the visual review (band
    rendering, family-2 survivor anchors, sliver retention). This pass
    supersedes none of them; its findings feed the redesign that will.

Requires network access for the real run (dem_data.py's USGS fetch and
production_area.py's own SSURGO/canopy/road fetches). --synthetic runs the
identical exploration over a hand-built two-valley fixture with no network
at all, which is how the whole path is verified offline.
"""

import argparse
import json

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

from feature_schema import (
    CONFIDENCE_LOW,
    make_feature,
    make_feature_collection,
    validate_feature_collection,
)
from keyline_analysis import (
    decompose_pool_perimeter,
    extract_level_contour,
    find_stem_crossings,
)
from keypoint_detection import build_upstream_map, detect_keypoints
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    cell_area_acres,
    cell_union_footprint,
    pixel_center_xy,
)
from valley_delineation import delineate_valleys
from valley_level_pool import POOL_REFERENCE_HEIGHT_METERS, delineate_level_pool
from water_candidate_zones import (
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    MAX_WALL_SEARCH_DOWNSTREAM_METERS,
    MIN_WATER_ZONE_AREA_ACRES,
    WATER_ZONE_CANOPY_BUFFER_METERS,
    _CANOPY_CHECK_UNCHECKED,
    _ROAD_CHECK_UNCHECKED,
    _find_wall_site,
    _flow_arrays_for_dem,
    _overlap_fraction_pct,
)

# The user's real, drawn property boundary -- the same one
# diagnose_water_zone_mask.py and every module's own __main__ block use.
PROPERTY_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

DEFAULT_OUTPUT_PATH = "keyline_water_exploration.geojson"

# Two keylines whose elevations sit within this of each other are NOTED as
# a similar-elevation pair. Yeomans observed that a property's keypoints
# often fall at close to the same elevation, which is what lets one
# contour do the work of several -- so a run where three keypoints sit
# within a metre is telling you something about the landform, and a run
# where they are spread over twenty is telling you something else.
#
# THIS IS A REPORTING THRESHOLD ONLY. It merges nothing, drops nothing and
# gates nothing: every qualifying keyline is traced independently at its
# own elevation regardless of what this says. 1.0 m is roughly one
# contour interval at contour_lines.CONTOUR_INTERVAL_METERS (0.6 m) plus
# the USGS 3DEP vertical RMSE (~0.82 m) -- i.e. about the point below
# which two elevations are not reliably distinguishable on this data
# anyway. NOT validated beyond the reference property. CONFIGURABLE.
KEYLINE_ELEVATION_SIMILARITY_METERS = 1.0

# Tolerance for the per-candidate waterline identity assertion. This is
# NOT a measurement tolerance -- the identity
#     waterline - keyline_elevation == quantization - overshoot
# is exact algebra over the same floats, so anything above float noise
# means the wall walk and the pool delineation have stopped agreeing about
# which cell the anchor is, which is a BUG and must fail loudly rather
# than be reported as a residual.
WATERLINE_IDENTITY_TOLERANCE_M = 1e-6

KEYLINE_EXPLORATION_CONFIDENCE_NOTES = (
    "DIAGNOSTIC EXPLORATION OUTPUT, NOT A DESIGN AND NOT PIPELINE OUTPUT. "
    "Every valley crossing of every qualifying keyline is drawn here with "
    "NO floor, ceiling, separation or on-parcel filter applied -- pools far "
    "too small to build, pools wholly off-parcel, pools that are really one "
    "pond counted twice, and walls that could not be found at all are all "
    "present ON PURPOSE, each carrying the flags that say so. Elevations "
    "come from an interpolated public DEM (LiDAR-derived where flown, "
    "coarser 1/3 arc-second elsewhere -- see dem_data.py), not from a "
    "survey; keypoints come from a two-segment fit over a modelled D8 long "
    "profile (see keypoint_detection.py's own caveats); pools are "
    "delineated at valley_level_pool.POOL_REFERENCE_HEIGHT_METERS, which is "
    "a fixed MEASURING STICK and never a proposed dam height. The 'proposed "
    "wall' lines are where the ground fails to reach the waterline -- a "
    "statement about terrain, carrying no height, no section, no spillway "
    "and no cost. Nothing here has been field-verified, and no feature in "
    "this file should be built from without a survey."
)


# --------------------------------------------------------------------------
# STEP 1 -- qualifying keypoints
# --------------------------------------------------------------------------


def qualify_keypoints(keypoints: list[dict], production_areas: list[dict]) -> list[dict]:
    """
    Tags every keypoint with whether it clears at least one production
    area's representative elevation, and which ones.

    The elevation compared against is production_area.py's OWN
    'representative_elevation_m' -- the median RAW elevation over the
    patch's cells, the same field water_candidate_zones.
    _zone_production_area_relationships() differences for its gravity
    relationships. Read, never recomputed: a second definition of "how high
    is this production area" is exactly the kind of quiet drift this
    codebase keeps deleting.

    ON-PARCEL STATUS IS NOT CONSULTED. See the module docstring.

    Returns one dict per keypoint, IN INPUT ORDER, qualifying or not:

        {
            'keypoint': dict,               # detect_keypoints()' own dict
            'qualifies': bool,
            'clears_production_area_ids': [int, ...],
            'margin_above_lowest_m': float, # kp elevation - the LOWEST
                                            #   representative elevation;
                                            #   negative = the margin missed
            'lowest_production_elevation_m': float or None,
        }

    With no production areas at all, nothing qualifies and every entry says
    so with lowest_production_elevation_m None -- the honest answer, not a
    default that lets everything through.
    """
    elevations = [(int(p["id"]), float(p["representative_elevation_m"])) for p in production_areas]
    lowest = min((e for _id, e in elevations), default=None)

    tagged = []
    for keypoint in keypoints:
        keypoint_elevation = float(keypoint["elevation_m"])
        clears = [patch_id for patch_id, e in elevations if keypoint_elevation > e]
        tagged.append(
            {
                "keypoint": keypoint,
                "qualifies": bool(clears),
                "clears_production_area_ids": clears,
                "margin_above_lowest_m": (
                    round(keypoint_elevation - lowest, 3) if lowest is not None else None
                ),
                "lowest_production_elevation_m": round(lowest, 3) if lowest is not None else None,
            }
        )
    return tagged


# --------------------------------------------------------------------------
# STEP 2 -- trace the keylines
# --------------------------------------------------------------------------


def trace_keylines(dem: dict, qualifying: list[dict], boundary_polygon_utm: Polygon) -> list[dict]:
    """
    One keyline per qualifying keypoint: the level contour of the RAW DEM
    at that keypoint's own elevation, over the DEM's FULL extent.

    NOTHING IS CLIPPED. Polylines are partitioned into those that intersect
    the parcel and those that do not, and BOTH counts are reported, but the
    kept polylines keep their off-parcel continuations intact -- a keyline
    that enters from next door and runs back out is one line, and cutting
    it at the property line would draw a fact about the deed rather than
    about the ground.

    Each entry:

        {
            'keypoint_id': int,
            'valley_id': int,
            'elevation_m': float,
            'keypoint_on_parcel': bool,
            'polylines': [extract_level_contour() dicts that touch the parcel],
            'polyline_count_total': int,
            'polyline_count_off_parcel_only': int,
            'on_parcel_length_m': float,      # measured, not clipped from
            'total_length_m': float,          #   the kept geometry
        }
    """
    keylines = []
    for entry in qualifying:
        keypoint = entry["keypoint"]
        elevation = float(keypoint["elevation_m"])
        polylines = extract_level_contour(dem, elevation)

        kept = [p for p in polylines if p["line_utm"].intersects(boundary_polygon_utm)]
        on_parcel_length = sum(
            float(p["line_utm"].intersection(boundary_polygon_utm).length) for p in kept
        )
        keylines.append(
            {
                "keypoint_id": int(keypoint["id"]),
                "valley_id": int(keypoint["valley_id"]),
                "elevation_m": round(elevation, 3),
                "keypoint_on_parcel": bool(keypoint["on_parcel"]),
                "polylines": kept,
                "polyline_count_total": len(polylines),
                "polyline_count_off_parcel_only": len(polylines) - len(kept),
                "on_parcel_length_m": round(on_parcel_length, 3),
                "total_length_m": round(sum(p["length_m"] for p in kept), 3),
            }
        )
    return keylines


def similar_elevation_pairs(
    keylines: list[dict], tolerance_m: float = KEYLINE_ELEVATION_SIMILARITY_METERS
) -> list[dict]:
    """
    Keyline pairs whose elevations sit within `tolerance_m`. REPORTED ONLY
    -- see KEYLINE_ELEVATION_SIMILARITY_METERS: nothing is merged, dropped
    or gated, and every keyline is traced independently regardless.
    """
    pairs = []
    for i in range(len(keylines)):
        for j in range(i + 1, len(keylines)):
            difference = abs(keylines[i]["elevation_m"] - keylines[j]["elevation_m"])
            if difference <= tolerance_m:
                pairs.append(
                    {
                        "keypoint_ids": (keylines[i]["keypoint_id"], keylines[j]["keypoint_id"]),
                        "elevations_m": (keylines[i]["elevation_m"], keylines[j]["elevation_m"]),
                        "difference_m": round(difference, 3),
                    }
                )
    return pairs


# --------------------------------------------------------------------------
# STEPS 3-5 -- crossings, wall + fill, measurement and flags
# --------------------------------------------------------------------------

FLAG_BELOW_PRODUCTION_MIN_AREA = "below_production_min_area"
FLAG_ANCHOR_OFF_PARCEL = "anchor_off_parcel"
FLAG_POOL_PARTLY_OFF_PARCEL = "pool_partly_off_parcel"
FLAG_POOL_ENTIRELY_OFF_PARCEL = "pool_entirely_off_parcel"
FLAG_CROSSING_EXCEEDS_CEILING = "crossing_exceeds_contributing_area_ceiling"
FLAG_WALL_EXCEEDS_CEILING = "wall_exceeds_contributing_area_ceiling"
FLAG_WALL_WALK_FAILED = "wall_walk_failed"
FLAG_WATERLINE_ABOVE_KEYLINE = "waterline_above_keyline"
FLAG_BACKWATER_DISTANCE_LIMITED = "backwater_distance_limited"
FLAG_POOL_OVERLAPS_ANOTHER = "pool_overlaps_another_pool"
FLAG_CROSSING_CLUSTER_COLLAPSED = "crossing_cluster_collapsed"
FLAG_COLLINEAR_OVERLAP = "keyline_runs_along_channel"

OUTCOME_POOL_DELINEATED = "pool_delineated"
OUTCOME_WALL_WALK_FAILED = "wall_walk_failed"


def _pool_footprints(dem: dict, pool_cells, boundary_polygon_utm: Polygon):
    """
    (mask, full footprint, on-parcel footprint) for a pool's cells.

    The full footprint is raster_grid.cell_union_footprint()'s real
    cell-union -- UNCLIPPED, because this pass reports on-parcel fraction
    rather than enforcing it. The clip is computed alongside so both
    acreages are available; NEITHER replaces the other.
    """
    mask = np.zeros(dem["array"].shape, dtype=bool)
    for r, c in pool_cells:
        mask[r, c] = True
    footprint = cell_union_footprint(dem, mask)
    return mask, footprint, footprint.intersection(boundary_polygon_utm)


def _overlap_groups(candidates: list[dict]) -> list[dict]:
    """
    Union-find over pools that SHARE CELLS: adjacent crossings whose
    backwaters merge are ONE POND IN REALITY, and this reports which.

    NOTHING IS TRIMMED OR DROPPED. The production path deletes the later
    member of an overlap (water_candidate_zones' overlap trim); here every
    member keeps its full geometry and carries the group id, because "these
    three crossings are the same pond" is exactly the evidence the eventual
    separation rule has to be designed against, and a trimmed output cannot
    show it.

    Returns one dict per group of 2 or more, and assigns 'overlap_group_id'
    on every candidate (None for a pool that shares cells with no other).
    """
    parent = list(range(len(candidates)))

    def _find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def _union(i, j):
        ri, rj = _find(i), _find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    cell_owner: dict = {}
    for index, candidate in enumerate(candidates):
        for cell in candidate.get("pool_cells", ()):
            if cell in cell_owner:
                _union(index, cell_owner[cell])
            else:
                cell_owner[cell] = index

    members: dict = {}
    for index in range(len(candidates)):
        members.setdefault(_find(index), []).append(index)

    groups = []
    for candidate in candidates:
        candidate["overlap_group_id"] = None
    for root, group_members in sorted(members.items()):
        if len(group_members) < 2:
            continue
        group_id = len(groups)
        shared = set(candidates[group_members[0]].get("pool_cells", ()))
        union_cells: set = set()
        for index in group_members:
            cells = set(candidates[index].get("pool_cells", ()))
            shared &= cells
            union_cells |= cells
            candidates[index]["overlap_group_id"] = group_id
            candidates[index]["flags"].append(FLAG_POOL_OVERLAPS_ANOTHER)
        groups.append(
            {
                "group_id": group_id,
                "candidate_indices": list(group_members),
                "member_count": len(group_members),
                "shared_cell_count": len(shared),
                "union_cell_count": len(union_cells),
            }
        )
    return groups


def explore(
    dem: dict,
    boundary_polygon_utm: Polygon,
    boundary_geometry_wgs84: dict,
    production_areas: list[dict],
    *,
    canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    max_valley_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    reference_height_meters: float = POOL_REFERENCE_HEIGHT_METERS,
    max_wall_search_distance_meters: float = MAX_WALL_SEARCH_DOWNSTREAM_METERS,
    keypoints=None,
    valleys=None,
    filled=None,
    flow_to_row=None,
    flow_to_col=None,
    flow_accumulation=None,
) -> dict:
    """
    The whole exploration, as a PURE function over an already-fetched dem
    plus already-computed production areas and boundary -- no network, no
    fetch. That is what lets --synthetic drive the identical code path over
    a hand-built fixture, which is how this script is verified offline.

    keypoints/valleys/filled/flow_* are optional overrides in the same
    self-computing, forwarded family the rest of this pipeline uses (see
    water_candidate_zones._flow_arrays_for_dem()); nothing here re-derives
    an argument it was handed.

    Returns one result dict (see the layer builders below for what is read
    out of it). Every geometry inside it already carries its WGS84 form.
    """
    filled, flow_to_row, flow_to_col, flow_accumulation = _flow_arrays_for_dem(
        dem, filled, flow_to_row, flow_to_col, flow_accumulation
    )
    if valleys is None:
        valleys = delineate_valleys(dem)
    if keypoints is None:
        keypoints = detect_keypoints(
            dem,
            boundary_polygon_utm,
            flow_to_row=flow_to_row,
            flow_to_col=flow_to_col,
            flow_accumulation=flow_accumulation,
            filled=filled,
            valleys=valleys,
        )
    upstream_map = build_upstream_map(flow_to_row, flow_to_col)

    raw = dem["array"]
    area_per_cell = cell_area_acres(dem)
    max_contributing_cells = max_valley_contributing_area_acres / area_per_cell
    boundary_prepared = prep(boundary_polygon_utm)

    # Production render fill as ONE prepared union -- the informational
    # overlap percentage, computed the same way water_candidate_zones.py
    # computes its canopy/road percentages (its own _overlap_fraction_pct,
    # imported rather than re-derived).
    render_fills = [
        p["render_fill_polygon_utm"]
        for p in production_areas
        if p.get("render_fill_polygon_utm") is not None and not p["render_fill_polygon_utm"].is_empty
    ]
    production_union = unary_union(render_fills) if render_fills else None
    production_prepared = prep(production_union) if production_union is not None else None

    canopy_checked = canopy_root_zone_mask_utm is not _CANOPY_CHECK_UNCHECKED
    canopy_mask = np.asarray(canopy_root_zone_mask_utm, dtype=bool) if canopy_checked else None
    road_checked = road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED
    road_union = (
        road_exclusion_union_utm
        if road_checked and road_exclusion_union_utm is not None
        else None
    )
    road_prepared = prep(road_union) if road_union is not None else None

    # --- STEP 1 --------------------------------------------------------
    tagged = qualify_keypoints(keypoints, production_areas)
    qualifying = [t for t in tagged if t["qualifies"]]

    # --- STEP 2 --------------------------------------------------------
    keylines = trace_keylines(dem, qualifying, boundary_polygon_utm)
    pairs = similar_elevation_pairs(keylines)

    # --- STEPS 3-5 -----------------------------------------------------
    candidates: list[dict] = []
    for keyline in keylines:
        crossings = find_stem_crossings(keyline["polylines"], valleys, dem)
        keyline["crossing_count"] = len(crossings)

        for crossing in crossings:
            flags: list[str] = []
            if crossing["cluster_size"] > 1:
                flags.append(FLAG_CROSSING_CLUSTER_COLLAPSED)
            if crossing["collinear_overlap"]:
                flags.append(FLAG_COLLINEAR_OVERLAP)

            crossing_cell = tuple(crossing["branch_rowcol"])
            crossing_contributing_acres = (
                float(flow_accumulation[crossing_cell[0], crossing_cell[1]]) * area_per_cell
            )
            if crossing_contributing_acres > max_valley_contributing_area_acres:
                flags.append(FLAG_CROSSING_EXCEEDS_CEILING)

            wall = _find_wall_site(
                dem,
                crossing_cell,
                flow_to_row,
                flow_to_col,
                flow_accumulation,
                reference_height_meters,
                max_wall_search_distance_meters,
                max_contributing_cells,
            )

            candidate = {
                "index": len(candidates),
                "keypoint_id": keyline["keypoint_id"],
                "keyline_elevation_m": keyline["elevation_m"],
                "keyline_valley_id": keyline["valley_id"],
                "crossing": crossing,
                "crossing_contributing_acres": round(crossing_contributing_acres, 3),
                "wall": wall,
                "wall_walk_end_reason": wall["walk_end_reason"],
                "flags": flags,
                "pool_cells": [],
                "overlap_group_id": None,
            }

            if not wall["found"]:
                # A FAILED WALK IS STILL A CANDIDATE. It is emitted with its
                # reason and the cell it died on; nothing is skipped.
                flags.append(FLAG_WALL_WALK_FAILED)
                candidate.update(
                    {
                        "outcome": OUTCOME_WALL_WALK_FAILED,
                        "anchor_rowcol": None,
                        "pool": None,
                        "perimeter": None,
                    }
                )
                candidates.append(candidate)
                continue

            anchor = tuple(wall["anchor"])
            wall_contributing_acres = float(wall["contributing_cells_at_anchor"]) * area_per_cell
            if wall["exceeds_ceiling"]:
                flags.append(FLAG_WALL_EXCEEDS_CEILING)

            # THE POOL ONLY. Band and stations off -- decompose_pool_
            # perimeter() is this pass's measurement layer (module docstring).
            pool = delineate_level_pool(
                dem,
                filled,
                flow_to_row,
                flow_to_col,
                flow_accumulation,
                upstream_map,
                anchor,
                reference_height_meters=reference_height_meters,
                abutment_search_half_width_meters=0.0,
                station_count=0,
                max_contributing_cells=max_contributing_cells,
            )
            if pool["backwater_distance_limited"]:
                flags.append(FLAG_BACKWATER_DISTANCE_LIMITED)

            # THE WATERLINE IDENTITY. Recomputed from the RAW array rather
            # than from the rounded reported fields, so the assertion tests
            # the machinery and not the rounding.
            anchor_elevation = float(raw[anchor[0], anchor[1]])
            waterline = anchor_elevation + float(reference_height_meters)
            crossing_cell_raw = float(raw[crossing_cell[0], crossing_cell[1]])
            residual = waterline - keyline["elevation_m"]
            quantization = crossing_cell_raw - keyline["elevation_m"]
            overshoot = (crossing_cell_raw - anchor_elevation) - float(reference_height_meters)
            assert abs(residual - (quantization - overshoot)) < WATERLINE_IDENTITY_TOLERANCE_M, (
                f"waterline identity broken at candidate {candidate['index']}: "
                f"residual={residual} quantization={quantization} overshoot={overshoot} "
                "-- the wall walk and the pool delineation disagree about the anchor cell"
            )
            if residual > 0:
                flags.append(FLAG_WATERLINE_ABOVE_KEYLINE)

            pool_cells = [tuple(cell) for cell in pool["pool_cells"]]
            mask, footprint, on_parcel_footprint = _pool_footprints(
                dem, pool_cells, boundary_polygon_utm
            )
            perimeter = decompose_pool_perimeter(mask, dem, waterline)

            total_acres = footprint.area / SQUARE_METERS_PER_ACRE
            on_parcel_acres = on_parcel_footprint.area / SQUARE_METERS_PER_ACRE
            if total_acres < MIN_WATER_ZONE_AREA_ACRES:
                flags.append(FLAG_BELOW_PRODUCTION_MIN_AREA)
            if on_parcel_acres <= 0:
                flags.append(FLAG_POOL_ENTIRELY_OFF_PARCEL)
            elif on_parcel_acres < total_acres - 1e-9:
                flags.append(FLAG_POOL_PARTLY_OFF_PARCEL)

            anchor_point = Point(*_cell_center(dem, anchor))
            anchor_off_parcel = not (
                boundary_prepared.contains(anchor_point) or boundary_polygon_utm.touches(anchor_point)
            )
            if anchor_off_parcel:
                flags.append(FLAG_ANCHOR_OFF_PARCEL)

            candidate.update(
                {
                    "outcome": OUTCOME_POOL_DELINEATED,
                    "anchor_rowcol": anchor,
                    "anchor_elevation_m": round(anchor_elevation, 3),
                    "anchor_off_parcel": anchor_off_parcel,
                    "anchor_distance_outside_boundary_m": (
                        0.0
                        if not anchor_off_parcel
                        else round(float(anchor_point.distance(boundary_polygon_utm)), 2)
                    ),
                    "wall_offset_downstream_m": wall["offset_downstream_m"],
                    "wall_drop_m": wall["drop_m"],
                    "wall_contributing_acres": round(wall_contributing_acres, 3),
                    "waterline_elevation_m": round(waterline, 3),
                    "waterline_residual_m": round(residual, 4),
                    "waterline_residual_cell_quantization_m": round(quantization, 4),
                    "waterline_residual_wall_overshoot_m": round(overshoot, 4),
                    "pool": pool,
                    "pool_cells": pool_cells,
                    "pool_mask": mask,
                    "perimeter": perimeter,
                    "area_acres": round(total_acres, 4),
                    "on_parcel_area_acres": round(on_parcel_acres, 4),
                    "on_parcel_fraction": (
                        round(on_parcel_acres / total_acres, 4) if total_acres > 0 else 0.0
                    ),
                    "geometry_wgs84": transform_geom(
                        dem["crs"], "EPSG:4326", mapping(footprint)
                    ),
                    "production_overlap_pct": _overlap_fraction_pct(
                        pool_cells, dem, production_prepared is not None,
                        prepared_union=production_prepared,
                    ),
                    "canopy_overlap_pct": _overlap_fraction_pct(
                        pool_cells, dem, canopy_checked, mask_utm=canopy_mask
                    ),
                    "road_overlap_pct": _overlap_fraction_pct(
                        pool_cells, dem, road_checked, prepared_union=road_prepared
                    ),
                }
            )
            candidates.append(candidate)

    groups = _overlap_groups(candidates)
    # The wall anchor is the one geometry whose UTM form is derived from a
    # CELL rather than returned as a polyline, so its WGS84 twin is built
    # here -- inside explore(), in one batched call -- rather than left for
    # the export. That keeps the geometry contract total: no candidate can
    # reach build_exploration_geojson() without its stored wire form.
    _attach_wall_anchor_geometry(dem, candidates)

    return {
        "dem_shape": tuple(int(v) for v in dem["array"].shape),
        "dem_resolution_meters": tuple(float(v) for v in dem["resolution_meters"]),
        "dem_crs": dem["crs"],
        "boundary_geometry_wgs84": boundary_geometry_wgs84,
        "boundary_acres": round(boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE, 3),
        "production_areas": [
            {
                "id": int(p["id"]),
                "area_acres": float(p["area_acres"]),
                "representative_elevation_m": round(float(p["representative_elevation_m"]), 3),
            }
            for p in production_areas
        ],
        "valley_count": len(valleys),
        "branch_count": sum(len(v.get("branches_utm") or []) for v in valleys),
        "tagged_keypoints": tagged,
        "keylines": keylines,
        "similar_elevation_pairs": pairs,
        "candidates": candidates,
        "overlap_groups": groups,
        "reference_height_meters": float(reference_height_meters),
        "max_valley_contributing_area_acres": float(max_valley_contributing_area_acres),
        "min_water_zone_area_acres": float(MIN_WATER_ZONE_AREA_ACRES),
        "canopy_checked": canopy_checked,
        "road_checked": road_checked,
    }


def _cell_center(dem: dict, cell) -> tuple[float, float]:
    """raster_grid.pixel_center_xy() taking a (row, col) TUPLE, which is the
    shape every cell in this script is carried as."""
    return pixel_center_xy(dem, int(cell[0]), int(cell[1]))


# --------------------------------------------------------------------------
# OUTPUT -- GeoJSON
# --------------------------------------------------------------------------


def build_exploration_geojson(result: dict) -> dict:
    """
    The six layers plus the run summary, as ONE schema-conformant
    FeatureCollection.

    READS STORED WIRE FORMS AND REPROJECTS NOTHING. Every geometry it
    emits was built in WGS84 alongside its UTM twin at the moment the
    geometry was created -- keyline_analysis._polyline()/._point_pair() for
    the lines and points, explore()'s own transform_geom() call for the
    pool footprints. test_keyline_analysis.py inspects THIS FUNCTION's
    source and asserts no reprojection call appears in it; that assertion
    is the contract, so do not add one here -- build the WGS84 form where
    the geometry is born instead.
    """
    features: list[dict] = []

    def _feature(feature_id, geometry, layer, label, extra):
        return make_feature(
            feature_id=feature_id,
            geometry=geometry,
            layer=layer,
            label=label,
            confidence=CONFIDENCE_LOW,
            confidence_notes=KEYLINE_EXPLORATION_CONFIDENCE_NOTES,
            extra_properties=extra,
        )

    # --- run summary: the parcel, carrying the run-level context --------
    features.append(
        _feature(
            "keyline-run-summary",
            result["boundary_geometry_wgs84"],
            "keyline_run_summary",
            "Keyline water exploration -- run summary",
            {
                "dem_rows": result["dem_shape"][0],
                "dem_cols": result["dem_shape"][1],
                "dem_resolution_meters": list(result["dem_resolution_meters"]),
                "dem_crs": result["dem_crs"],
                "boundary_acres": result["boundary_acres"],
                "valley_count": result["valley_count"],
                "branch_count": result["branch_count"],
                "keypoint_count": len(result["tagged_keypoints"]),
                "qualifying_keypoint_count": len(result["keylines"]),
                "keyline_count": len(result["keylines"]),
                "crossing_count": len(result["candidates"]),
                "pool_count": sum(
                    1 for c in result["candidates"] if c["outcome"] == OUTCOME_POOL_DELINEATED
                ),
                "failed_wall_walk_count": sum(
                    1 for c in result["candidates"] if c["outcome"] == OUTCOME_WALL_WALK_FAILED
                ),
                "overlap_group_count": len(result["overlap_groups"]),
                "overlap_groups": result["overlap_groups"],
                "production_areas": result["production_areas"],
                "production_representative_elevations_m": {
                    str(p["id"]): p["representative_elevation_m"] for p in result["production_areas"]
                },
                "similar_elevation_pairs": [
                    {
                        "keypoint_ids": list(p["keypoint_ids"]),
                        "elevations_m": list(p["elevations_m"]),
                        "difference_m": p["difference_m"],
                    }
                    for p in result["similar_elevation_pairs"]
                ],
                "reference_height_meters": result["reference_height_meters"],
                "max_valley_contributing_area_acres": result["max_valley_contributing_area_acres"],
                "min_water_zone_area_acres_NOT_APPLIED": result["min_water_zone_area_acres"],
                "canopy_checked": result["canopy_checked"],
                "road_checked": result["road_checked"],
                "filters_applied": "none -- every crossing is emitted with an outcome",
            },
        )
    )

    # --- keypoints, qualifying AND not ----------------------------------
    for tag in result["tagged_keypoints"]:
        keypoint = tag["keypoint"]
        features.append(
            _feature(
                f"keyline-keypoint-{keypoint['id']}",
                keypoint["geometry_wgs84"],
                "keyline_keypoint",
                f"Keypoint {keypoint['id']}"
                + (" (qualifying)" if tag["qualifies"] else " (does NOT qualify)"),
                {
                    "keypoint_id": int(keypoint["id"]),
                    "valley_id": int(keypoint["valley_id"]),
                    "elevation_m": float(keypoint["elevation_m"]),
                    "contributing_acres": float(keypoint["contributing_acres"]),
                    "on_parcel": bool(keypoint["on_parcel"]),
                    "distance_outside_boundary_m": float(keypoint["distance_outside_boundary_m"]),
                    "qualifies": tag["qualifies"],
                    "clears_production_area_ids": tag["clears_production_area_ids"],
                    "margin_above_lowest_m": tag["margin_above_lowest_m"],
                    "lowest_production_elevation_m": tag["lowest_production_elevation_m"],
                },
            )
        )

    # --- keylines --------------------------------------------------------
    for keyline in result["keylines"]:
        for i, polyline in enumerate(keyline["polylines"]):
            features.append(
                _feature(
                    f"keyline-{keyline['keypoint_id']}-{i}",
                    polyline["geometry_wgs84"],
                    "keyline",
                    f"Keyline at {keyline['elevation_m']}m (keypoint {keyline['keypoint_id']})",
                    {
                        "keypoint_id": keyline["keypoint_id"],
                        "keyline_elevation_m": keyline["elevation_m"],
                        "valley_id": keyline["valley_id"],
                        "keypoint_on_parcel": keyline["keypoint_on_parcel"],
                        "polyline_index": i,
                        "length_m": polyline["length_m"],
                        "closed": polyline["closed"],
                        "polyline_count_total": keyline["polyline_count_total"],
                        "polyline_count_off_parcel_only": keyline["polyline_count_off_parcel_only"],
                        "on_parcel_length_m": keyline["on_parcel_length_m"],
                        "crossing_count": keyline.get("crossing_count", 0),
                    },
                )
            )

    # --- per candidate: crossing, wall anchor, pool, wall segments -------
    for candidate in result["candidates"]:
        crossing = candidate["crossing"]
        common = {
            "candidate_index": candidate["index"],
            "keypoint_id": candidate["keypoint_id"],
            "keyline_elevation_m": candidate["keyline_elevation_m"],
            "valley_id": crossing["valley_id"],
            "branch_index": crossing["branch_index"],
            "outcome": candidate["outcome"],
            "flags": candidate["flags"],
            "overlap_group_id": candidate["overlap_group_id"],
        }

        features.append(
            _feature(
                f"keyline-crossing-{candidate['index']}",
                crossing["geometry_wgs84"],
                "keyline_crossing",
                f"Crossing {candidate['index']} -- valley {crossing['valley_id']} "
                f"branch {crossing['branch_index']}",
                {
                    **common,
                    "channel_elevation_m": crossing["channel_elevation_m"],
                    "channel_elevation_raw_m": crossing["channel_elevation_raw_m"],
                    "fill_depth_at_crossing_m": crossing["fill_depth_at_crossing_m"],
                    "level_residual_m": crossing["level_residual_m"],
                    "along_branch_m": crossing["along_branch_m"],
                    "cluster_size": crossing["cluster_size"],
                    "cluster_along_branch_m": crossing["cluster_along_branch_m"],
                    "collinear_overlap": crossing["collinear_overlap"],
                    "rowcol": list(crossing["rowcol"]),
                    "branch_rowcol": list(crossing["branch_rowcol"]),
                    "crossing_contributing_acres": candidate["crossing_contributing_acres"],
                },
            )
        )

        wall = candidate["wall"]
        anchor_geometry = (
            crossing["geometry_wgs84"]
            if candidate["outcome"] == OUTCOME_WALL_WALK_FAILED
            else candidate["wall_anchor_geometry_wgs84"]
        )
        features.append(
            _feature(
                f"keyline-wall-anchor-{candidate['index']}",
                anchor_geometry,
                "keyline_wall_anchor",
                f"Wall anchor {candidate['index']} ({wall['walk_end_reason']})",
                {
                    **common,
                    "walk_end_reason": wall["walk_end_reason"],
                    "walk_end_rowcol": list(wall["walk_end_rowcol"]),
                    "wall_found": bool(wall["found"]),
                    "keypoint_elevation_m": wall["keypoint_elevation_m"],
                    "anchor_elevation_m": wall["anchor_elevation_m"],
                    "offset_downstream_m": wall["offset_downstream_m"],
                    "drop_m": wall["drop_m"],
                    "required_drop_m": result["reference_height_meters"],
                    "exceeds_ceiling": bool(wall["exceeds_ceiling"]),
                    "anchor_rowcol": (
                        list(candidate["anchor_rowcol"]) if candidate["anchor_rowcol"] else None
                    ),
                    "geometry_is_crossing_fallback": candidate["outcome"] == OUTCOME_WALL_WALK_FAILED,
                },
            )
        )

        if candidate["outcome"] != OUTCOME_POOL_DELINEATED:
            continue

        perimeter = candidate["perimeter"]
        features.append(
            _feature(
                f"keyline-pool-{candidate['index']}",
                candidate["geometry_wgs84"],
                "keyline_pool",
                f"Pool {candidate['index']} -- {candidate['area_acres']} ac",
                {
                    **common,
                    "area_acres": candidate["area_acres"],
                    "on_parcel_area_acres": candidate["on_parcel_area_acres"],
                    "on_parcel_fraction": candidate["on_parcel_fraction"],
                    "pool_cell_count": perimeter["pool_cell_count"],
                    "waterline_elevation_m": candidate["waterline_elevation_m"],
                    "waterline_residual_m": candidate["waterline_residual_m"],
                    "waterline_residual_cell_quantization_m": candidate[
                        "waterline_residual_cell_quantization_m"
                    ],
                    "waterline_residual_wall_overshoot_m": candidate[
                        "waterline_residual_wall_overshoot_m"
                    ],
                    "enclosure_fraction": perimeter["enclosure_fraction"],
                    "open_length_m": perimeter["open_length_m"],
                    "ground_closed_length_m": perimeter["ground_closed_length_m"],
                    "undetermined_length_m": perimeter["undetermined_length_m"],
                    "total_perimeter_m": perimeter["total_perimeter_m"],
                    "open_segment_count": perimeter["open_segment_count"],
                    "pool_area_per_open_meter_m2": perimeter["pool_area_per_open_meter_m2"],
                    "edge_counts": perimeter["edge_counts"],
                    "anchor_rowcol": list(candidate["anchor_rowcol"]),
                    "anchor_off_parcel": candidate["anchor_off_parcel"],
                    "anchor_distance_outside_boundary_m": candidate[
                        "anchor_distance_outside_boundary_m"
                    ],
                    "wall_offset_downstream_m": candidate["wall_offset_downstream_m"],
                    "wall_drop_m": candidate["wall_drop_m"],
                    "wall_contributing_acres": candidate["wall_contributing_acres"],
                    "crossing_contributing_acres": candidate["crossing_contributing_acres"],
                    "production_overlap_pct": candidate["production_overlap_pct"],
                    "canopy_overlap_pct": candidate["canopy_overlap_pct"],
                    "road_overlap_pct": candidate["road_overlap_pct"],
                    "backwater_distance_limited": candidate["pool"]["backwater_distance_limited"],
                },
            )
        )

        for j, segment in enumerate(perimeter["open_segments"]):
            features.append(
                _feature(
                    f"keyline-wall-segment-{candidate['index']}-{j}",
                    segment["geometry_wgs84"],
                    "keyline_wall_segment",
                    f"Proposed wall {candidate['index']}.{j} -- {segment['length_m']}m",
                    {
                        **common,
                        "segment_index": j,
                        "length_m": segment["length_m"],
                        "closed": segment["closed"],
                        "waterline_elevation_m": candidate["waterline_elevation_m"],
                        "pool_area_acres": candidate["area_acres"],
                        "pool_open_length_m": perimeter["open_length_m"],
                        "pool_enclosure_fraction": perimeter["enclosure_fraction"],
                        "pool_area_per_open_meter_m2": perimeter["pool_area_per_open_meter_m2"],
                    },
                )
            )

    return make_feature_collection(features)


# --------------------------------------------------------------------------
# OUTPUT -- terminal table
# --------------------------------------------------------------------------


def _pct(value) -> str:
    """None means THE CHECK NEVER RAN; 0.0 means it ran and found nothing.
    water_candidate_zones._overlap_fraction_pct() keeps that distinction
    load-bearing, so the table must not print the two alike."""
    return "unchecked" if value is None else f"{value}%"


def summarize(result: dict) -> str:
    """The design-evidence table: per keyline, per pool, overlap groups,
    totals. Read alongside the GeoJSON over imagery."""
    lines: list[str] = []
    add = lines.append

    add("=" * 100)
    add("KEYLINE WATER ZONES -- EXPLORATION PASS (diagnostic; FLAGS, NOT FILTERS)")
    add("=" * 100)
    add(
        f"DEM {result['dem_shape'][0]}x{result['dem_shape'][1]} @ "
        f"{result['dem_resolution_meters'][0]}m, crs={result['dem_crs']}; "
        f"parcel {result['boundary_acres']} ac; "
        f"{result['valley_count']} valley(s), {result['branch_count']} branch(es)"
    )
    add(
        f"reference height {result['reference_height_meters']}m (a MEASURING STICK, never a dam "
        f"height); contributing-area ceiling {result['max_valley_contributing_area_acres']} ac and "
        f"area floor {result['min_water_zone_area_acres']} ac are REPORTED, NOT APPLIED"
    )
    add("")

    add("--- STEP 1: production representative elevations (production_area.py's own field) ---")
    if not result["production_areas"]:
        add("  NONE -- no production area was found, so NO keypoint can qualify. Honest empty run.")
    for patch in result["production_areas"]:
        add(
            f"  production area {patch['id']}: {patch['area_acres']} ac, representative elevation "
            f"{patch['representative_elevation_m']}m"
        )
    add("")

    add("--- STEP 1: keypoint qualification (on-parcel status is NOT consulted) ---")
    for tag in result["tagged_keypoints"]:
        keypoint = tag["keypoint"]
        location = (
            "on parcel"
            if keypoint["on_parcel"]
            else f"{keypoint['distance_outside_boundary_m']}m off parcel"
        )
        if tag["qualifies"]:
            add(
                f"  QUALIFIES  keypoint {keypoint['id']} (valley {keypoint['valley_id']}): "
                f"{keypoint['elevation_m']}m, {location}, clears production area(s) "
                f"{tag['clears_production_area_ids']} -- {tag['margin_above_lowest_m']}m above the "
                f"lowest ({tag['lowest_production_elevation_m']}m)"
            )
        else:
            missed = (
                f"missed by {abs(tag['margin_above_lowest_m'])}m"
                if tag["margin_above_lowest_m"] is not None
                else "no production area to clear"
            )
            add(
                f"  excluded   keypoint {keypoint['id']} (valley {keypoint['valley_id']}): "
                f"{keypoint['elevation_m']}m, {location}, clears NOTHING -- {missed} "
                f"(lowest production representative elevation "
                f"{tag['lowest_production_elevation_m']}m)"
            )
    add("")

    if result["similar_elevation_pairs"]:
        add(
            f"--- Similar-elevation keyline pairs (within "
            f"{KEYLINE_ELEVATION_SIMILARITY_METERS}m -- NOTED, still traced independently) ---"
        )
        for pair in result["similar_elevation_pairs"]:
            add(
                f"  keypoints {pair['keypoint_ids'][0]} & {pair['keypoint_ids'][1]}: "
                f"{pair['elevations_m'][0]}m vs {pair['elevations_m'][1]}m "
                f"({pair['difference_m']}m apart)"
            )
        add("")

    add("--- STEPS 2-3: keylines traced and crossings found ---")
    if not result["keylines"]:
        add("  NONE -- no keypoint qualified, so no keyline was traced. Honest empty run.")
    for keyline in result["keylines"]:
        by_keyline = [c for c in result["candidates"] if c["keypoint_id"] == keyline["keypoint_id"]]
        pools = sum(1 for c in by_keyline if c["outcome"] == OUTCOME_POOL_DELINEATED)
        failed = sum(1 for c in by_keyline if c["outcome"] == OUTCOME_WALL_WALK_FAILED)
        add(
            f"  keyline @ {keyline['elevation_m']}m (keypoint {keyline['keypoint_id']}, valley "
            f"{keyline['valley_id']}, keypoint "
            f"{'on' if keyline['keypoint_on_parcel'] else 'OFF'} parcel): "
            f"{len(keyline['polylines'])} polyline(s) touching the parcel of "
            f"{keyline['polyline_count_total']} traced "
            f"({keyline['polyline_count_off_parcel_only']} entirely off-parcel), "
            f"{keyline['total_length_m']}m total / {keyline['on_parcel_length_m']}m on-parcel; "
            f"{len(by_keyline)} crossing(s) -> {pools} pool(s), {failed} failed wall walk(s)"
        )
    add("")

    add("--- STEP 4-5: every candidate, one line each (nothing omitted) ---")
    add(
        "  idx  kp  vly/br   acres    on-parcel   encl   wall_m   ac/wall_m   grp   outcome / flags"
    )
    for candidate in result["candidates"]:
        crossing = candidate["crossing"]
        location = f"{crossing['valley_id']}/{crossing['branch_index']}"
        group = "-" if candidate["overlap_group_id"] is None else str(candidate["overlap_group_id"])
        if candidate["outcome"] == OUTCOME_WALL_WALK_FAILED:
            add(
                f"  {candidate['index']:>3}  {candidate['keypoint_id']:>2}  {location:<7} "
                f"{'--':>7}  {'--':>10}  {'--':>5}  {'--':>7}  {'--':>10}   {group:<3}   "
                f"WALL WALK FAILED ({candidate['wall_walk_end_reason']}; found "
                f"{candidate['wall']['drop_m']}m of {result['reference_height_meters']}m) "
                f"{candidate['flags']}"
            )
            continue
        perimeter = candidate["perimeter"]
        per_meter = perimeter["pool_area_per_open_meter_m2"]
        add(
            f"  {candidate['index']:>3}  {candidate['keypoint_id']:>2}  {location:<7} "
            f"{candidate['area_acres']:>7.4f}  {candidate['on_parcel_fraction']:>9.2%}  "
            f"{perimeter['enclosure_fraction']:>5.2f}  {perimeter['open_length_m']:>7.1f}  "
            f"{('n/a' if per_meter is None else f'{per_meter:.1f}'):>10}   {group:<3}   "
            f"pool @ {candidate['waterline_elevation_m']}m "
            f"(residual {candidate['waterline_residual_m']:+.3f}m) "
            f"{candidate['flags']}"
        )
    add("")

    add("--- Per-pool detail ---")
    for candidate in result["candidates"]:
        if candidate["outcome"] != OUTCOME_POOL_DELINEATED:
            continue
        perimeter = candidate["perimeter"]
        add(
            f"  pool {candidate['index']} (keypoint {candidate['keypoint_id']} @ "
            f"{candidate['keyline_elevation_m']}m, valley {candidate['crossing']['valley_id']} "
            f"branch {candidate['crossing']['branch_index']}):"
        )
        add(
            f"      crossing at cell {tuple(candidate['crossing']['branch_rowcol'])}, channel "
            f"{candidate['crossing']['channel_elevation_m']}m filled / "
            f"{candidate['crossing']['channel_elevation_raw_m']}m raw "
            f"(fill depth {candidate['crossing']['fill_depth_at_crossing_m']}m), "
            f"{candidate['crossing_contributing_acres']} ac contributing"
            + (
                f"  [CLUSTER of {candidate['crossing']['cluster_size']} collapsed]"
                if candidate["crossing"]["cluster_size"] > 1
                else ""
            )
        )
        add(
            f"      wall {candidate['wall_offset_downstream_m']}m downstream at cell "
            f"{candidate['anchor_rowcol']}, {candidate['wall_drop_m']}m drop, "
            f"{candidate['wall_contributing_acres']} ac contributing"
            + (" [ANCHOR OFF PARCEL]" if candidate["anchor_off_parcel"] else "")
        )
        add(
            f"      waterline {candidate['waterline_elevation_m']}m vs keyline "
            f"{candidate['keyline_elevation_m']}m -> residual "
            f"{candidate['waterline_residual_m']:+.4f}m "
            f"(cell quantization {candidate['waterline_residual_cell_quantization_m']:+.4f}m, "
            f"wall overshoot {candidate['waterline_residual_wall_overshoot_m']:+.4f}m)"
        )
        add(
            f"      pool {candidate['area_acres']} ac total / "
            f"{candidate['on_parcel_area_acres']} ac on-parcel "
            f"({candidate['on_parcel_fraction']:.1%}), {perimeter['pool_cell_count']} cell(s)"
        )
        add(
            f"      perimeter {perimeter['total_perimeter_m']}m = "
            f"{perimeter['ground_closed_length_m']}m ground-closed + "
            f"{perimeter['open_length_m']}m OPEN (the wall) + "
            f"{perimeter['undetermined_length_m']}m undetermined; enclosure "
            f"{perimeter['enclosure_fraction']}; {perimeter['open_segment_count']} wall segment(s)"
            + (
                ""
                if perimeter["pool_area_per_open_meter_m2"] is None
                else f"; {perimeter['pool_area_per_open_meter_m2']} m2 of pool per meter of wall"
            )
        )
        add(
            f"      overlap: production {_pct(candidate['production_overlap_pct'])}, canopy "
            f"{_pct(candidate['canopy_overlap_pct'])}, road {_pct(candidate['road_overlap_pct'])}"
        )
        if candidate["flags"]:
            add(f"      flags: {candidate['flags']}")
    add("")

    add("--- Overlap groups (adjacent crossings whose pools merge are ONE POND; nothing trimmed) ---")
    if not result["overlap_groups"]:
        add("  none -- every pool is cell-disjoint from every other")
    for group in result["overlap_groups"]:
        add(
            f"  group {group['group_id']}: candidates {group['candidate_indices']} "
            f"({group['member_count']} pools) share {group['shared_cell_count']} cell(s); "
            f"their union is {group['union_cell_count']} cell(s)"
        )
    add("")

    pools = [c for c in result["candidates"] if c["outcome"] == OUTCOME_POOL_DELINEATED]
    failed = [c for c in result["candidates"] if c["outcome"] == OUTCOME_WALL_WALK_FAILED]
    below_floor = [c for c in pools if FLAG_BELOW_PRODUCTION_MIN_AREA in c["flags"]]
    off_parcel = [c for c in pools if FLAG_POOL_ENTIRELY_OFF_PARCEL in c["flags"]]
    over_ceiling = [
        c
        for c in result["candidates"]
        if FLAG_CROSSING_EXCEEDS_CEILING in c["flags"] or FLAG_WALL_EXCEEDS_CEILING in c["flags"]
    ]
    add("--- TOTALS ---")
    add(f"  keypoints detected:            {len(result['tagged_keypoints'])}")
    add(f"  keypoints qualifying:          {len(result['keylines'])}")
    add(f"  crossings found:               {len(result['candidates'])}")
    add(f"  pools delineated:              {len(pools)}")
    add(f"  wall walks failed:             {len(failed)}")
    add(
        f"  pools under the {result['min_water_zone_area_acres']} ac floor: "
        f"{len(below_floor)}  (the production path would DROP these)"
    )
    add(f"  pools entirely off-parcel:     {len(off_parcel)}  (the production path would DROP these)")
    add(
        f"  candidates over the {result['max_valley_contributing_area_acres']} ac ceiling: "
        f"{len(over_ceiling)}  (the production path would DROP these)"
    )
    add(f"  overlap groups:                {len(result['overlap_groups'])}")
    if pools:
        add(f"  total pool acreage:            {sum(c['area_acres'] for c in pools):.4f} ac")
        add(
            f"  total on-parcel pool acreage:  "
            f"{sum(c['on_parcel_area_acres'] for c in pools):.4f} ac"
        )
        add(
            f"  total proposed wall length:    "
            f"{sum(c['perimeter']['open_length_m'] for c in pools):.1f} m"
        )
    add("")
    add(
        "This table plus the GeoJSON over imagery IS the deliverable: the evidence from which the "
        "keep/throw rules get designed. Nothing here is a recommendation."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# ENTRY POINTS
# --------------------------------------------------------------------------


def _attach_wall_anchor_geometry(dem: dict, candidates: list[dict]) -> None:
    """
    Builds each successful candidate's wall-anchor point in BOTH wire forms,
    in ONE batched reprojection, at build time -- the geometry contract
    again (build_exploration_geojson() reprojects nothing).

    A failed wall walk gets no anchor point of its own: there is no anchor,
    only a cell the walk died on, and the export draws that candidate's
    anchor feature at the CROSSING instead, flagged
    geometry_is_crossing_fallback so nobody reads it as a found wall site.
    """
    pending = [c for c in candidates if c["outcome"] == OUTCOME_POOL_DELINEATED]
    if not pending:
        return
    centers = [_cell_center(dem, c["anchor_rowcol"]) for c in pending]
    lons, lats = warp_transform(
        dem["crs"], "EPSG:4326", [p[0] for p in centers], [p[1] for p in centers]
    )
    for candidate, x, y, lon, lat in zip(pending, [p[0] for p in centers], [p[1] for p in centers], lons, lats):
        candidate["wall_anchor_point_utm"] = (float(x), float(y))
        candidate["wall_anchor_point_wgs84"] = (float(lon), float(lat))
        candidate["wall_anchor_geometry_wgs84"] = {
            "type": "Point",
            "coordinates": (float(lon), float(lat)),
        }


def _boundary_polygon(dem: dict, boundary_coordinates) -> Polygon:
    xs, ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    return Polygon(zip(xs, ys))


def synthetic_fixture() -> dict:
    """
    A hand-built TWO-VALLEY fixture -- DEM, boundary and production areas --
    for the offline end-to-end run. No network, no fetch, nothing mocked:
    explore() runs its real code path over real (synthetic) terrain.

    The surface is a plane rising to the south (0.5 m per 5 m row, a 10%
    grade) with two V-notches cut into it at columns 8 and 24, each notch
    2 m deep per column of lateral offset. Flow therefore converges into
    both notches and runs north to row 0, giving two independent primary
    valleys with real branches, real keypoints and real level pools.

    The keypoints come out of the real detector, so their elevations are
    not hand-chosen; the two production areas are placed LOW, on the notch
    floors near row 0 at the bottom of the slope, at representative
    elevations comfortably below any keypoint -- which is what makes both
    keypoints qualify and the keyline pass have something to explore. The
    two patches sit at slightly different rows so their representative
    elevations DIFFER, rather than the perfectly symmetric fixture
    reporting one number twice and hiding a bug that read only one of them.

    ONE PROPERTY OF THIS FIXTURE IS DELIBERATE AND IS ITSELF THE POINT:
    the two valleys are mirror images, so the detector puts both keypoints
    at the SAME elevation, so the two keylines are the same contour. Every
    crossing is therefore found twice and every pool is delineated twice on
    exactly the same cells -- which exercises the similar-elevation pair
    report and the overlap grouping on ground where the right answer
    ("these are one pond, counted twice") is known by construction.
    """
    rows, cols = 46, 34
    resolution = 5.0
    array = np.zeros((rows, cols), dtype=np.float64)
    for r in range(rows):
        for c in range(cols):
            notch = min(abs(c - 8), abs(c - 24))
            # The slope term also curves: a steep upper reach over a gentler
            # lower one, so the two-segment keypoint fit has a real
            # inflection to find rather than a straight line.
            along = 0.35 * r + 0.012 * r * r
            array[r, c] = 100.0 + along + 2.0 * min(notch, 6)

    dem = {
        "array": array,
        "resolution_meters": (resolution, resolution),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    # A boundary covering most of the grid but not all of it, so on/off
    # parcel is a real distinction in the fixture rather than vacuous.
    x0 = dem["origin_x"] + 2 * resolution
    x1 = dem["origin_x"] + (cols - 2) * resolution
    y1 = dem["origin_y"] - 3 * resolution
    y0 = dem["origin_y"] - (rows - 6) * resolution
    boundary_polygon_utm = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    boundary_geometry_wgs84 = transform_geom(
        dem["crs"], "EPSG:4326", mapping(boundary_polygon_utm)
    )

    production_areas = []
    for patch_id, center_col, first_row in ((0, 8, 1), (1, 24, 2)):
        cells = [
            (r, c)
            for r in range(first_row, first_row + 4)
            for c in range(center_col - 1, center_col + 2)
        ]
        xs = [pixel_center_xy(dem, r, c)[0] for r, c in cells]
        ys = [pixel_center_xy(dem, r, c)[1] for r, c in cells]
        polygon = Polygon(
            [
                (min(xs) - resolution / 2, min(ys) - resolution / 2),
                (max(xs) + resolution / 2, min(ys) - resolution / 2),
                (max(xs) + resolution / 2, max(ys) + resolution / 2),
                (min(xs) - resolution / 2, max(ys) + resolution / 2),
            ]
        )
        production_areas.append(
            {
                "id": patch_id,
                "area_acres": round(polygon.area / SQUARE_METERS_PER_ACRE, 2),
                "representative_elevation_m": float(
                    np.median([array[r, c] for r, c in cells])
                ),
                "polygon_utm": polygon,
                "render_fill_polygon_utm": polygon,
                "cells": cells,
            }
        )

    return {
        "dem": dem,
        "boundary_polygon_utm": boundary_polygon_utm,
        "boundary_geometry_wgs84": boundary_geometry_wgs84,
        "production_areas": production_areas,
    }


def run(
    *,
    synthetic: bool,
    output_path: str,
    max_contributing_acres: float,
) -> dict:
    """Fetches (or builds) the inputs, explores, writes the GeoJSON, prints
    the table. Returns the result dict so a caller/test can assert on it."""
    if synthetic:
        print(
            "explore_keyline_water_zones.py --synthetic -- hand-built two-valley fixture, "
            "NO NETWORK. Identical exploration code path to the real run.\n"
        )
        fixture = synthetic_fixture()
        dem = fixture["dem"]
        boundary_polygon_utm = fixture["boundary_polygon_utm"]
        boundary_geometry_wgs84 = fixture["boundary_geometry_wgs84"]
        production_areas = fixture["production_areas"]
        canopy = _CANOPY_CHECK_UNCHECKED
        roads = _ROAD_CHECK_UNCHECKED
    else:
        print("explore_keyline_water_zones.py -- keyline water zones, EXPLORATION PASS\n")
        print(f"Property: real reference boundary, {len(PROPERTY_BOUNDARY)} vertices\n")

        from dem_data import get_dem_for_boundary
        from production_area import (
            _fetch_road_exclusion_union_utm,
            get_required_tree_root_zone_mask_utm,
            identify_production_areas,
        )
        from farm_roads_data import ROAD_EXCLUSION_BUFFER_METERS

        dem = get_dem_for_boundary(PROPERTY_BOUNDARY)
        print(
            f"DEM fetched: {dem['array'].shape[0]}x{dem['array'].shape[1]} cells, "
            f"{dem['resolution_meters'][0]}m resolution, crs={dem['crs']}\n"
        )
        boundary_polygon_utm = _boundary_polygon(dem, PROPERTY_BOUNDARY)
        boundary_geometry_wgs84 = {
            "type": "Polygon",
            "coordinates": [[(float(lon), float(lat)) for lon, lat in PROPERTY_BOUNDARY]],
        }

        production_areas = identify_production_areas(dem, boundary_polygon_utm)
        print(f"Production areas found: {len(production_areas)}\n")

        # Both degrade GRACEFULLY here, the same choice
        # diagnose_water_zone_mask.py makes: a diagnostic that cannot run at
        # all when one informational fetch fails is less useful than one
        # that reports what it can and says plainly what it could not check.
        canopy = _CANOPY_CHECK_UNCHECKED
        try:
            canopy = get_required_tree_root_zone_mask_utm(
                boundary_polygon_utm, dem, buffer_meters=WATER_ZONE_CANOPY_BUFFER_METERS
            )
            print(f"Canopy root-zone mask fetched: {int(np.asarray(canopy).sum())} cell(s)\n")
        except Exception as e:
            print(f"Canopy fetch failed ({e}) -- canopy overlap will read UNCHECKED (None).\n")

        roads = _ROAD_CHECK_UNCHECKED
        try:
            roads = _fetch_road_exclusion_union_utm(
                PROPERTY_BOUNDARY, dem, buffer_meters=ROAD_EXCLUSION_BUFFER_METERS
            )
            print(
                "Road exclusion fetched: "
                + ("no mapped road nearby (a real 0, not unchecked).\n" if roads is None else "union built.\n")
            )
        except Exception as e:
            print(f"Road fetch failed ({e}) -- road overlap will read UNCHECKED (None).\n")

    result = explore(
        dem,
        boundary_polygon_utm,
        boundary_geometry_wgs84,
        production_areas,
        canopy_root_zone_mask_utm=canopy,
        road_exclusion_union_utm=roads,
        max_valley_contributing_area_acres=max_contributing_acres,
    )
    collection = build_exploration_geojson(result)
    validate_feature_collection(collection)
    with open(output_path, "w") as handle:
        json.dump(collection, handle, indent=2)

    print(summarize(result))
    print(
        f"\nWrote {len(collection['features'])} feature(s) to {output_path} "
        "(feature_schema-validated)."
    )
    layer_counts: dict = {}
    for feature in collection["features"]:
        layer = feature["properties"]["layer"]
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
    for layer, count in sorted(layer_counts.items()):
        print(f"  {layer}: {count}")

    result["geojson"] = collection
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnostic exploration of keyline-based water zones: a pool at EVERY valley "
            "crossing of EVERY qualifying keyline, with every production filter reported "
            "rather than applied."
        )
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Run offline against a hand-built two-valley fixture instead of fetching real data.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_PATH,
        help=f"Where to write the GeoJSON (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--max-contributing-acres",
        type=float,
        default=MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
        help=(
            "The contributing-area ceiling to REPORT against for this run (default: the module "
            f"constant, {MAX_VALLEY_CONTRIBUTING_AREA_ACRES}). It is never applied as a filter "
            "here -- it only decides which candidates carry the over-ceiling flags."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        run(
            synthetic=args.synthetic,
            output_path=args.output,
            max_contributing_acres=args.max_contributing_acres,
        )
    except Exception as e:
        print(f"Request failed: {e}")
        if not args.synthetic:
            print(
                "\nNote: the real run requires internet access (USGS National Map for the DEM, "
                "plus production_area.py's own SSURGO/canopy/road sources). Run with --synthetic "
                "to exercise the identical exploration path offline."
            )
        raise
