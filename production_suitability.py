"""
production_suitability.py

Adds a suitability RANKING to production-zone candidates that
production_area.py has already identified — it does not change which
ground counts as a candidate or how its boundary is drawn (that stays
entirely production_area.py's job, untouched here). Given
identify_production_areas()'s patches, this scores each one against three
independently-computed, positively-weighted factors, plus a soil-carving
step that runs BEFORE scoring:

    slope_factor    -- flatter (within the low-slope mask that already
                        defined the patch) scores higher, from the same
                        DEM already fetched (production_area.py's own
                        compute_slope_percent()).
    size_factor      -- larger AND more compact/contiguous scores higher;
                        a large irregular sliver is penalized relative to
                        a compact block of the same acreage (see
                        _compactness_score()).
    aspect_factor    -- a minor factor (small weight, see
                        ASPECT_FACTOR_WEIGHT below): south-facing scores
                        higher, from the same DEM (terrain_metrics.py's
                        Horn-method aspect, already built for
                        solar_suitability.py). Aspect matters far less for
                        general production than it did for solar siting,
                        so it's included but deliberately not
                        heavily weighted.

    soil carving     -- NOT a weighted score, and NOT a whole-patch
                        pass/fail either (an earlier version of this
                        module rejected an entire patch if ANY soil
                        component intersecting its footprint was
                        disqualifying — tested live, this excluded BOTH
                        real surviving candidates on the reference
                        property even though most of each patch's soil
                        was fine; a real but partial wet inclusion
                        shouldn't veto ground that's mostly workable).
                        Real SSURGO polygon geometry for map units meeting
                        is_disqualifying_soil_condition() (hydric/wetland,
                        or permanently saturated drainage — soil_data.py,
                        reused as-is, not redefined) is subtracted OUT of
                        each patch's own footprint before anything is
                        scored. The remainder — one piece, or several if
                        the subtraction splits the patch into disconnected
                        remainders — is what actually gets scored. See
                        _carve_soil_from_patch().

Soil quality is still deliberately NOT a positively-weighted scoring
factor, unlike slope/size/aspect. Per Scale of Permanence sequencing,
soil is step 8 — the LAST step, and treated as the most IMPROVABLE factor
of the sequence (amendable, buildable, the one thing a grower can
actually change about a given piece of ground). Scoring zones higher for
currently-better soil would pull that step-8 judgment forward into what
should be a step-2 (Land Shape) decision about where production zones
sit at all. So SSURGO is used here the same way road_corridors.py already
uses it for floodplain/hydric and erosion-prone soil — as real geometry to
route AROUND, not a quality signal to rank by — just applied as a
subtraction on an already-identified zone's own footprint instead of a
routing constraint.

Each of the three scored factors, plus the carving outcome
(soil_carved_acres/soil_carved_pct/source_patch_id), are stored
independently in the output properties (not just folded into one opaque
number) alongside the weighted composite suitability_score, so report
narrative and any future scenario-selection logic can explain WHY a zone
scored the way it did and whether/how much of it was soil-adjusted ("good
slope and shape; 0.8 of this candidate's original 2.0 acres was hydric
and carved out before scoring"), not just the final number.

    DEM (dem_data.py, already fetched for production_area.py)
        --> identify_production_areas(dem, boundary_polygon_utm) (production_area.py,
            UNCHANGED logic -- already clips each patch to the real parcel)
        --> [this module] per-patch soil carving (real SSURGO geometry,
            subtracted -- may split one patch into several sub-patches)
        --> per-(sub-)patch slope/size/aspect scoring (against the SAME
            on-parcel, post-carve cells)
        --> enriched production_area_candidate features (same layer,
            same or split zones -- with suitability_score/*_factor/
            soil_carved_* properties added)

score_production_areas() is the pure scoring core: it takes an
already-computed DEM and patches list, plus optionally pre-fetched
disqualifying-soil-geometry unions per patch, and does no network I/O
itself -- same pure-core-vs-network-fetch split as water_candidate_zones.py
and solar_suitability.py, so the scoring/carving math is unit-testable
against a synthetic DEM independent of whether SSURGO is reachable.
identify_production_area_suitability() is the fetch-and-score entry point.

This is a self-contained, standalone pass: it is NOT wired into
generate_full_report.py or report_generator.py's prompt in this pass (see
the module docstring conventions elsewhere in this codebase for that
"later pass" framing) -- validate the ranking on its own first, the same
way production_area.py's own diagnostic layer was validated before
water_candidate_zones.py or solar_suitability.py were built on top of it.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import (
    MAX_PRODUCTION_SLOPE_PCT,
    MIN_PRODUCTION_AREA_ACRES,
    compute_slope_percent,
    identify_production_areas,
)
from raster_grid import SQUARE_METERS_PER_ACRE, connected_components, pixel_center_xy
from soil_data import (
    coordinates_to_wkt_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    is_disqualifying_soil_condition,
)
from terrain_metrics import aspect_score, compute_slope_and_aspect

# --- composite weights (must sum to 1.0). CONFIGURABLE -- tune against a
# real property once ground-truthed; see README.md's roadmap note. Soil is
# deliberately NOT one of these -- see module docstring for why it's a
# carving step instead. Aspect is deliberately the smallest weight --
# general production suitability cares far less about compass orientation
# than solar siting did.
SLOPE_FACTOR_WEIGHT = 0.55
SIZE_FACTOR_WEIGHT = 0.30
ASPECT_FACTOR_WEIGHT = 0.15

_WEIGHT_SUM = SLOPE_FACTOR_WEIGHT + SIZE_FACTOR_WEIGHT + ASPECT_FACTOR_WEIGHT
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

# suitability_score is reported on a 0-100 scale (composite of the 0-1
# factors below, rounded to 1 decimal) -- same convention
# solar_suitability.py already uses for its own suitability_score, so a
# reader comparing the two layers doesn't have to remember two different
# scales. The three individual *_factor properties stay on their native
# 0-1 scale (documented per-property below), since those are meant to be
# read alongside each other and against the composite, not multiplied by
# 100 individually.
SUITABILITY_SCORE_SCALE = 100

# size_factor sub-weights (must sum to 1.0). CONFIGURABLE.
SIZE_AREA_SUBWEIGHT = 0.5
SIZE_SHAPE_SUBWEIGHT = 0.5

# Acreage at/above which the area component of size_factor maxes out at
# 1.0 -- bigger than this doesn't add further score, it's already a large,
# workable block. CONFIGURABLE -- tune to your own property's scale
# (production_area.py's own MIN_PRODUCTION_AREA_ACRES is 0.5; this is
# meant to sit well above that, not right at the noise floor).
REFERENCE_MAX_AREA_ACRES = 10.0

# Polsby-Popper compactness (4*pi*area/perimeter^2) of a perfect square is
# exactly pi/4 (~0.785), not 1.0 -- and for an axis-aligned raster
# footprint (this module's patch shapes, built from square DEM cells) a
# solid square block of cells is the most compact shape achievable, same
# as a real geometric square. Dividing by this ceiling rescales so that
# best-case compact block reads as 1.0 rather than topping out at ~0.785,
# while fragmented/elongated real shapes still fall well below it.
_SQUARE_COMPACTNESS = math.pi / 4

# Sentinel distinguishing "the disqualifying-soil fetch for this patch
# genuinely returned no disqualifying geometry" (a real dict value of
# None) from "this patch was never checked at all" (fetch failed, or
# check_soil=False) -- same reasoning as solar_suitability.py's
# None-vs-[] convention for road_geometries_utm, just via an explicit
# sentinel here since None is itself a meaningful, valid result.
_SOIL_CHECK_UNAVAILABLE = object()

PRODUCTION_SUITABILITY_CONFIDENCE_NOTES_TEMPLATE = (
    "This ADDS a suitability ranking to production-zone candidates that were already "
    "identified by production_area.py's slope-only heuristic -- it does not change which "
    "ground counts as a candidate or its boundary (see that layer's own confidence_notes "
    "for the underlying detection caveats). suitability_score (0-100) is a weighted composite "
    "of THREE independently-stored 0-1 factors: slope_factor (weight {slope_weight}, real DEM "
    "slope), size_factor (weight {size_weight}, real geometry: acreage + Polsby-Popper "
    "compactness of the candidate's own cell footprint -- a large irregular sliver scores lower "
    "than a compact block of the same acreage), and aspect_factor (weight {aspect_weight}, "
    "deliberately the smallest weight -- general production suitability cares far less about "
    "compass orientation than solar siting did -- {aspect_availability}). Soil quality is "
    "DELIBERATELY NOT one of these weighted factors -- per Scale of Permanence sequencing, "
    "soil is step 8, the last and most improvable step, so it shouldn't gate/rank where "
    "production zones go the way slope/size/aspect (Land Shape, step 2) do. Instead, real "
    "SSURGO polygon geometry for disqualifying soil (hydric/wetland, or permanently saturated "
    "drainage) is CARVED OUT of each candidate's own footprint before scoring, rather than "
    "rejecting or down-scoring the whole candidate for a partial wet inclusion. {soil_note}"
)

_SOIL_UNCARVED_NOTE = (
    "Checked against real SSURGO polygon geometry: no disqualifying soil was found in this "
    "candidate's footprint, so it's reported unmodified (soil_carved_acres=0)."
)
_SOIL_CARVED_NOTE = (
    "Checked against real SSURGO polygon geometry: {carved_acres} acres ({carved_pct}%) of the "
    "original candidate's footprint (id {source_id}) was disqualifying soil and was carved out "
    "before scoring -- this candidate's geometry/score reflect only the remaining, non-"
    "disqualifying ground."
)
_SOIL_UNAVAILABLE_NOTE = (
    "No SSURGO geometry could be fetched for this candidate's footprint (fetch failed, or the "
    "soil check was skipped), so soil carving could NOT be verified here -- this candidate's "
    "geometry may still include disqualifying ground that wasn't caught. ESTIMATE, not a "
    "measurement."
)
_ASPECT_AVAILABLE_NOTE = "computed from real DEM-derived aspect (Horn's method) averaged across the candidate"
_ASPECT_OMITTED_NOTE = (
    "this candidate's ground was too flat for a well-defined downhill direction, so aspect_factor "
    "was defaulted to a neutral 1.0 (flat ground has no unfavorable orientation) -- OMITTED, not measured"
)


def _slope_factor(slope_values_pct: list[float], max_slope_pct: float) -> float:
    """1.0 at 0% grade, falling linearly to 0.0 at max_slope_pct -- the
    same slope ceiling production_area.py already used to decide this is
    a candidate at all, so a patch sitting right at that ceiling (barely
    qualifying) scores near 0 rather than being treated the same as
    dead-flat ground."""
    if not slope_values_pct:
        return 0.0
    avg_slope = float(np.mean(slope_values_pct))
    return max(0.0, min(1.0, 1.0 - avg_slope / max_slope_pct))


def _circular_mean_aspect_deg(aspect_values_deg: list[float]) -> Optional[float]:
    """Mean compass bearing via vector averaging (a plain arithmetic mean
    of e.g. 350 deg and 10 deg would wrongly give 180 instead of 0).
    Returns None if every input is undefined (an all-flat patch)."""
    valid = [a for a in aspect_values_deg if not math.isnan(a)]
    if not valid:
        return None
    sin_sum = sum(math.sin(math.radians(a)) for a in valid)
    cos_sum = sum(math.cos(math.radians(a)) for a in valid)
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def _compactness_score(cells: list[tuple[int, int]], dem: dict) -> float:
    """0-1 shape-compactness score for a (sub-)patch's own constituent DEM
    cells (NOT its convex-hull or carved-polygon footprint -- neither is
    guaranteed to reflect real fragmentation the way the actual
    constituent cells do). Builds the real footprint as a union of
    per-cell squares (each cell's own ground square, sized by the DEM's
    resolution) and scores it via Polsby-Popper (4*pi*area/perimeter^2),
    normalized against the most compact shape an axis-aligned raster
    footprint can achieve (a solid square block, see _SQUARE_COMPACTNESS)
    so a compact block reads ~1.0 and a fragmented/elongated sliver of
    the same area reads well below it."""
    if not cells:
        return 0.0
    px, py = dem["resolution_meters"]
    squares = []
    for r, c in cells:
        x, y = pixel_center_xy(dem, r, c)
        squares.append(box(x - px / 2, y - py / 2, x + px / 2, y + py / 2))
    footprint = unary_union(squares)
    area = footprint.area
    perimeter = footprint.length
    if perimeter <= 0:
        return 0.0
    polsby_popper = 4 * math.pi * area / (perimeter**2)
    return max(0.0, min(1.0, polsby_popper / _SQUARE_COMPACTNESS))


def _size_factor(area_acres: float, cells: list[tuple[int, int]], dem: dict, reference_max_area_acres: float) -> tuple[float, float, float]:
    """Returns (size_factor, area_score, compactness_score) -- the
    sub-scores are returned too so callers/tests can inspect why a
    (sub-)patch scored the way it did, not just the blended result."""
    area_score = max(0.0, min(1.0, area_acres / reference_max_area_acres))
    compactness = _compactness_score(cells, dem)
    size_factor = SIZE_AREA_SUBWEIGHT * area_score + SIZE_SHAPE_SUBWEIGHT * compactness
    return size_factor, area_score, compactness


def _fetch_disqualifying_soil_union(wkt_polygon: str, dem: dict) -> Optional[object]:
    """
    Real SSURGO polygon geometry (not just component ratings) for every
    map unit with at least one disqualifying component within
    wkt_polygon, unioned and reprojected into dem['crs']. Returns None if
    nothing disqualifying was found (the common, clean case -- not an
    error).

    is_disqualifying_soil_condition() (soil_data.py) decides what counts
    as disqualifying -- reused as-is, not redefined here.

    Same fetch-then-filter-then-fetch-geometry pattern as
    road_corridors.py's _fetch_erosion_prone_union()/
    _fetch_floodplain_hydric_union(): component-table rows
    (get_soil_data_for_polygon()) decide WHICH mukeys qualify; a second,
    geometry-specific query (soil_data.get_soil_geometries_for_polygon())
    fetches only those mukeys' actual polygons -- reused directly rather
    than reimplementing hydric-geometry fetching from scratch.
    """
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    disqualifying_mukeys = {
        row["mukey"]
        for row in soil_components
        if is_disqualifying_soil_condition(row.get("hydricrating"), row.get("drainagecl"))
    }
    if not disqualifying_mukeys:
        return None

    geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)
    pieces = [
        shape(transform_geom("EPSG:4326", dem["crs"], geometries_by_mukey[mukey]))
        for mukey in disqualifying_mukeys
        if mukey in geometries_by_mukey
    ]
    return unary_union(pieces) if pieces else None


def _polygon_pieces(geom) -> list:
    """Flattens a geometry down to its Polygon parts. A .difference()
    between two real-world polygons can, in edge cases (a subtracted
    piece touching the outer boundary only along a shared edge or at a
    single vertex), return a GeometryCollection mixing stray points/lines
    in with the real remaining area -- same reasoning as soil_data.py's
    own _polygonal_parts(). Only Polygon/MultiPolygon parts represent
    real ground."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        pieces = []
        for sub_geom in geom.geoms:
            pieces.extend(_polygon_pieces(sub_geom))
        return pieces
    return []


def _carve_soil_from_patch(
    patch: dict,
    cells: list[tuple[int, int]],
    dem: dict,
    disqualifying_union_utm,
    soil_data_available: bool,
    min_area_acres: float,
) -> list[dict]:
    """
    Splits `patch` around any disqualifying soil geometry found within its
    footprint, instead of rejecting the whole patch (see module
    docstring). Returns a list of "sub-patch" dicts, each carrying
    everything the scoring loop in score_production_areas() needs to
    score it independently:
        {
            'id': int | str,            # patch['id'] unchanged if carving didn't
                                          # split the patch; f"{id}a"/f"{id}b"/...
                                          # (a new string id) per disconnected
                                          # remainder if it did
            'source_patch_id': int,      # original pre-carve patch id, always --
                                          # for traceability even when id is unchanged
            'polygon_utm': Polygon,
            'geometry_wgs84': dict,
            'area_acres': float,         # of THIS piece's own geometry
            'representative_elevation_m': float,  # median elevation of THIS piece's cells
            'cells': list[(row, col)],   # this piece's own on-parcel/low-slope cells
            'soil_carved_acres': float,  # acreage of disqualifying soil found in the
                                          # ORIGINAL (pre-carve) patch -- same value on
                                          # every piece split from one original patch
            'soil_carved_pct': float,    # soil_carved_acres as a % of the ORIGINAL
                                          # patch's own area_acres
            'soil_data_available': bool, # whether the disqualifying-soil check itself
                                          # could be run for this patch at all
        }

    If disqualifying_union_utm is None (soil data was available and
    genuinely clean, OR unavailable -- either way there's nothing to
    subtract), or the original patch's polygon doesn't actually intersect
    it, returns [patch] essentially unchanged (soil_carved_acres=0.0) --
    no geometry churn or re-scoring for the common case.

    A patch whose ENTIRE footprint turns out to be disqualifying soil
    returns [] (dropped entirely, same as today's existing size-filtering
    behavior for a patch with no real remainder).
    """
    original_polygon = patch["polygon_utm"]

    if disqualifying_union_utm is None or not original_polygon.intersects(disqualifying_union_utm):
        return [
            {
                **patch,
                "source_patch_id": patch["id"],
                "cells": cells,
                "soil_carved_acres": 0.0,
                "soil_carved_pct": 0.0,
                "soil_data_available": soil_data_available,
            }
        ]

    original_area_acres = patch["area_acres"]
    soil_carved_acres = original_polygon.intersection(disqualifying_union_utm).area / SQUARE_METERS_PER_ACRE
    soil_carved_pct = round(soil_carved_acres / original_area_acres * 100, 1) if original_area_acres > 0 else 0.0

    carved_geometry = original_polygon.difference(disqualifying_union_utm)
    pieces = _polygon_pieces(carved_geometry)

    if not pieces:
        return []  # this patch's entire footprint was disqualifying soil

    split = len(pieces) > 1  # only assign lettered sub-ids for a REAL disconnected split
    suffix_letters = "abcdefghijklmnopqrstuvwxyz"

    sub_patches = []
    for index, piece in enumerate(pieces):
        piece_area_acres = piece.area / SQUARE_METERS_PER_ACRE
        if piece_area_acres < min_area_acres:
            continue

        piece_prepared = prep(piece)
        piece_cells = [(r, c) for r, c in cells if piece_prepared.contains(Point(pixel_center_xy(dem, r, c)))]
        if not piece_cells:
            # patch['polygon_utm'] is a convex hull (production_area.py's
            # deliberately simple footprint, not a precise cell-by-cell
            # outline), so a thin sliver piece can clear the area
            # threshold on continuous geometry alone without any actual
            # cell center landing inside it. Fall back to the single
            # nearest recovered cell to the piece's centroid rather than
            # dropping a genuinely-qualifying piece or crashing on an
            # empty elevation/slope sample.
            nearest = min(cells, key=lambda rc: piece.centroid.distance(Point(pixel_center_xy(dem, *rc))))
            piece_cells = [nearest]

        elevations = [float(dem["array"][r, c]) for r, c in piece_cells]
        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(piece))

        sub_patches.append(
            {
                "id": f"{patch['id']}{suffix_letters[index]}" if split else patch["id"],
                "source_patch_id": patch["id"],
                "polygon_utm": piece,
                "geometry_wgs84": geometry_wgs84,
                "area_acres": round(piece_area_acres, 2),
                "representative_elevation_m": float(np.median(elevations)),
                "cells": piece_cells,
                "soil_carved_acres": round(soil_carved_acres, 2),
                "soil_carved_pct": soil_carved_pct,
                "soil_data_available": soil_data_available,
            }
        )

    return sub_patches


def score_production_areas(
    patches: list[dict],
    dem: dict,
    boundary_polygon_utm: Polygon,
    disqualifying_soil_by_patch_id: Optional[dict[int, Optional[object]]] = None,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    reference_max_area_acres: float = REFERENCE_MAX_AREA_ACRES,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
) -> list[dict]:
    """
    Pure carving + scoring logic -- see module docstring for why this
    takes already-computed inputs rather than fetching anything.

    patches is identify_production_areas()'s own output (production_area.py,
    UNCHANGED -- this function does not alter membership, geometry, or
    which original patches exist; it may SPLIT a patch into several
    scored sub-patches during soil carving, or drop one entirely if its
    whole footprint is disqualifying soil).

    boundary_polygon_utm MUST be the same real parcel boundary passed to
    identify_production_areas() to produce `patches` (see cell-recovery
    note below for why).

    disqualifying_soil_by_patch_id maps patch['id'] to that patch's own
    pre-fetched disqualifying-soil geometry union (a shapely Polygon/
    MultiPolygon, or None if the fetch ran and genuinely found nothing) --
    see _fetch_disqualifying_soil_union(). A patch['id'] simply ABSENT
    from this dict (or the whole argument omitted) is treated as
    "never checked" (soil_data_available=False), distinct from a real
    None result (checked, clean) -- same reasoning as solar_suitability.py's
    None-vs-[] convention for road_geometries_utm.

    max_slope_pct MUST match whatever max_slope_pct produced `patches` (the
    default already matches identify_production_areas()'s own default) --
    see the cell-recovery note below for why a mismatch would silently
    recompute the wrong component labeling.

    Recovers each original patch's own constituent DEM cells (not just its
    convex-hull footprint) by recomputing the exact same low-slope mask +
    8-connected-component labeling production_area.py's own
    identify_production_areas() used internally (same compute_slope_percent()
    and MAX_PRODUCTION_SLOPE_PCT it already exposes, same
    raster_grid.connected_components()) -- deterministic, so patch['id']
    lines up with the recomputed component label exactly. This is reusing
    production_area.py's own building blocks, not reimplementing or
    changing its detection logic. Recovered cells are filtered to the
    on-parcel subset, then (via _carve_soil_from_patch()) to whichever
    post-soil-carve piece they fall inside, before any factor is computed
    from them.

    Returns a flat list of scored sub-patch dicts (see
    _carve_soil_from_patch()'s docstring for the base shape), each with
    these scoring fields added:
        {
            'suitability_score': float,   # 0-100, slope/size/aspect only
            'slope_factor': float,        # 0-1
            'size_factor': float,         # 0-1
            'aspect_factor': float,       # 0-1
            'avg_slope_pct': float,
            'aspect_deg': Optional[float],
            'area_score': float,          # 0-1, size_factor's area sub-score
            'compactness_score': float,   # 0-1, size_factor's shape sub-score
            'aspect_available': bool,
            'rank': int,                  # 1 = highest suitability_score, over
                                            # ALL resulting sub-patches -- soil
                                            # no longer gates ranking at all,
                                            # only shrinks the scored footprint
        }
    """
    disqualifying_soil_by_patch_id = disqualifying_soil_by_patch_id or {}

    array = dem["array"]
    resolution = dem["resolution_meters"]

    slope = compute_slope_percent(array, resolution)
    _, aspect_deg = compute_slope_and_aspect(array, resolution)

    candidate_mask = (~np.isnan(slope)) & (slope <= max_slope_pct)
    labels, _ = connected_components(candidate_mask)
    boundary_prepared = prep(boundary_polygon_utm)

    all_sub_patches: list[dict] = []

    for patch in patches:
        raw_cells = [(int(r), int(c)) for r, c in np.argwhere(labels == patch["id"])]
        cells = [(r, c) for r, c in raw_cells if boundary_prepared.contains(Point(pixel_center_xy(dem, r, c)))]
        if not cells:
            # Shouldn't happen -- identify_production_areas() already
            # dropped any patch with no on-parcel cells at all -- but
            # fall back to the raw (unclipped) cells rather than
            # producing an empty candidate if it's ever called with a
            # mismatched boundary/patches pair.
            cells = raw_cells

        entry = disqualifying_soil_by_patch_id.get(patch["id"], _SOIL_CHECK_UNAVAILABLE)
        soil_data_available = entry is not _SOIL_CHECK_UNAVAILABLE
        disqualifying_union_utm = entry if soil_data_available else None

        all_sub_patches.extend(
            _carve_soil_from_patch(patch, cells, dem, disqualifying_union_utm, soil_data_available, min_area_acres)
        )

    for sub_patch in all_sub_patches:
        sub_cells = sub_patch["cells"]

        slope_values = [float(slope[r, c]) for r, c in sub_cells]
        slope_factor = _slope_factor(slope_values, max_slope_pct)

        aspect_values = [float(aspect_deg[r, c]) for r, c in sub_cells if not math.isnan(aspect_deg[r, c])]
        mean_aspect = _circular_mean_aspect_deg(aspect_values)
        aspect_available = mean_aspect is not None
        aspect_factor = aspect_score(mean_aspect) if aspect_available else 1.0

        size_factor, area_score, compactness_score = _size_factor(
            sub_patch["area_acres"], sub_cells, dem, reference_max_area_acres
        )

        composite = (
            SLOPE_FACTOR_WEIGHT * slope_factor
            + SIZE_FACTOR_WEIGHT * size_factor
            + ASPECT_FACTOR_WEIGHT * aspect_factor
        )

        sub_patch.update(
            {
                "suitability_score": round(composite * SUITABILITY_SCORE_SCALE, 1),
                "slope_factor": round(slope_factor, 3),
                "size_factor": round(size_factor, 3),
                "aspect_factor": round(aspect_factor, 3),
                "avg_slope_pct": round(float(np.mean(slope_values)), 1) if slope_values else None,
                "aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
                "area_score": round(area_score, 3),
                "compactness_score": round(compactness_score, 3),
                "aspect_available": aspect_available,
            }
        )

    all_sub_patches.sort(key=lambda p: -p["suitability_score"])
    for rank, sub_patch in enumerate(all_sub_patches, start=1):
        sub_patch["rank"] = rank

    return all_sub_patches


def production_suitability_to_geojson(scored_patches: list[dict]) -> dict:
    """Wraps score_production_areas() output as a schema-conformant
    GeoJSON FeatureCollection on the SAME layer production_area.py's own
    production_areas_to_geojson() uses ("production_area_candidate") --
    these are the same zones (or their soil-carved remainders), just
    enriched with suitability_score, its component factors, and the soil
    carving outcome -- not a new/different layer."""
    features = []
    for patch in scored_patches:
        if patch["soil_carved_acres"] > 0:
            soil_note = _SOIL_CARVED_NOTE.format(
                carved_acres=patch["soil_carved_acres"],
                carved_pct=patch["soil_carved_pct"],
                source_id=patch["source_patch_id"],
            )
        elif patch["soil_data_available"]:
            soil_note = _SOIL_UNCARVED_NOTE
        else:
            soil_note = _SOIL_UNAVAILABLE_NOTE

        confidence_notes = PRODUCTION_SUITABILITY_CONFIDENCE_NOTES_TEMPLATE.format(
            slope_weight=SLOPE_FACTOR_WEIGHT,
            size_weight=SIZE_FACTOR_WEIGHT,
            aspect_weight=ASPECT_FACTOR_WEIGHT,
            aspect_availability=_ASPECT_AVAILABLE_NOTE if patch["aspect_available"] else _ASPECT_OMITTED_NOTE,
            soil_note=soil_note,
        )

        label = f"Production area candidate {patch['id']} (suitability rank {patch['rank']})"

        features.append(
            make_feature(
                feature_id=f"production-area-{patch['id']}",
                geometry=patch["geometry_wgs84"],
                layer="production_area_candidate",
                label=label,
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties={
                    "area_acres": patch["area_acres"],
                    "representative_elevation_m": round(patch["representative_elevation_m"], 1),
                    "rank": patch["rank"],
                    "suitability_score": patch["suitability_score"],
                    "slope_factor": patch["slope_factor"],
                    "size_factor": patch["size_factor"],
                    "aspect_factor": patch["aspect_factor"],
                    "avg_slope_pct": patch["avg_slope_pct"],
                    "aspect_deg": patch["aspect_deg"],
                    "soil_carved_acres": patch["soil_carved_acres"],
                    "soil_carved_pct": patch["soil_carved_pct"],
                    "soil_data_available": patch["soil_data_available"],
                    "source_patch_id": patch["source_patch_id"],
                },
            )
        )
    return make_feature_collection(features)


def summarize_production_area_suitability(scored_patches: list[dict]) -> str:
    if not scored_patches:
        return "No production-area candidates to score."

    lines = [f"Production-area suitability ranking ({len(scored_patches)} candidate(s)):"]
    for patch in sorted(scored_patches, key=lambda p: p["rank"]):
        carved_note = f", {patch['soil_carved_acres']} ac soil-carved from patch {patch['source_patch_id']}" if patch["soil_carved_acres"] > 0 else ""
        lines.append(
            f"  - Rank {patch['rank']}: patch {patch['id']}, score {patch['suitability_score']}/100 "
            f"(slope={patch['slope_factor']}, size={patch['size_factor']}, aspect={patch['aspect_factor']}), "
            f"{patch['area_acres']} acres{carved_note}"
        )
    return "\n".join(lines)


def identify_production_area_suitability(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_soil: bool = True,
    **score_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    identifies production-area candidates (production_area.py, unchanged),
    fetches real disqualifying-soil geometry per candidate footprint,
    carves it out and (re)scores the result, and returns the enriched
    "production_area_candidate" GeoJSON FeatureCollection.

    Each candidate's own SSURGO fetch degrades independently and
    gracefully (a USDA SDA outage for one candidate's footprint shouldn't
    block scoring for the others, or block the whole pass) -- same
    reasoning solar_suitability.py's prime-farmland check and every other
    optional network layer in this pipeline already uses.

    Computes boundary_polygon_utm the same way road_corridors.py/
    solar_suitability.py already do (reproject the drawn boundary into
    the DEM's own CRS) and passes it to BOTH identify_production_areas()
    (required there, for on-parcel clipping) and score_production_areas()
    (so its own recovered cells stay on-parcel too).
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    patches = identify_production_areas(dem, boundary_polygon_utm)

    disqualifying_soil_by_patch_id: dict[int, Optional[object]] = {}

    if check_soil:
        for patch in patches:
            wkt_polygon = coordinates_to_wkt_polygon(patch["geometry_wgs84"]["coordinates"][0])
            try:
                disqualifying_soil_by_patch_id[patch["id"]] = _fetch_disqualifying_soil_union(wkt_polygon, dem)
            except Exception:
                pass  # left absent from the dict -- score_production_areas() treats that as "never checked"

    scored = score_production_areas(patches, dem, boundary_polygon_utm, disqualifying_soil_by_patch_id, **score_kwargs)

    return {"zones_geojson": production_suitability_to_geojson(scored), "scored_patches": scored}


if __name__ == "__main__":
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Scoring production-area candidates for property boundary...\n")

    try:
        result = identify_production_area_suitability(property_boundary)
        print(summarize_production_area_suitability(result["scored_patches"]))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
