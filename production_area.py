"""
production_area.py

Heuristic identification of candidate production/cultivation area(s) on a
property from DEM-derived slope — a structured stand-in for the same
judgment report_generator.py's Scale of Permanence prompt already makes in
prose (the "Land Shape" section: which parts of the property read as
strong, workable production land versus steep/awkward/marginal ground).

This is the FOUNDATION module of the consolidated production-zone
pipeline (this file / production_area_ceiling.py / production_suitability.py
together). It owns STEP 1 and STEP 3 of that pipeline, which every entry
point in the other two files reuses rather than recomputing:

    STEP 1 -- compute_step1_eligible_cells(): for every on-parcel DEM
        cell, TWO hard, cell-level gates -- slope (MAX_PRODUCTION_SLOPE_PCT)
        and hydric/disqualifying soil (soil_data.hydric_disqualifying_mukeys(),
        tested against each cell's own CENTER POINT, not a continuous-
        geometry difference) -- decide eligibility outright. Every eligible
        cell is then scored on slope+aspect only (size/compactness isn't a
        per-cell property; see production_suitability.py's STEP 4 for that).
        Computed ONCE per pipeline run and reused by every step after it --
        this is what replaces the old architecture's three independent mask
        rebuilds (identify_production_areas(), production_suitability.py's
        cell-recovery, production_area_ceiling.py's pool-building) and the
        old continuous-geometry soil-carving pass entirely. A cell is either
        in the eligible mask or it isn't -- no polygon differencing, so no
        spurious slivers can be created by construction.

    STEP 3 -- cluster_and_gate(): 8-connected-component labeling
        (raster_grid.connected_components()) of WHATEVER cell mask a caller
        passes in (STEP 1's raw eligible mask here, or
        production_area_ceiling.py's post-STEP-2-trim survivor mask), each
        cluster's REAL cell-union footprint (_cell_union_footprint(), not a
        convex hull), and a pure area survival gate
        (MIN_PRODUCTION_AREA_ACRES) -- nothing else gates survival here; no
        soil check, since hydric cells were already excluded at STEP 1.
        Shared by this module's own identify_production_areas() (no ceiling
        trim) and production_area_ceiling.py's identify_optimized_production_
        areas() (with the trim) -- one implementation, not two.

identify_production_areas() is STEP 1 (no trim) + STEP 3 chained together,
plus its own disqualifying-soil fetch (graceful-degrading, same
fetch-then-continue pattern every other optional network layer in this
pipeline uses) -- this is the "raw" production-area candidate list every
existing caller (water_candidate_zones.py, road_corridors.py,
solar_suitability.py) already consumes via this exact function/signature;
they get STEP 1's cell-level hydric exclusion automatically, with no
changes needed on their end. It does NOT apply production_area_ceiling.py's
80%-of-parcel ceiling trim (STEP 2) -- these three modules deliberately
still read the un-trimmed candidate list; see production_area_ceiling.py's
and tree_zone_candidates.py's own docstrings for why that's a separate,
already-tracked wiring item, not something this consolidation changes.

PARCEL CLIPPING: identify_production_areas() requires the real parcel
boundary (boundary_polygon_utm — dem_data.py deliberately fetches a DEM
buffered 100m past the drawn boundary) and clips every candidate down to
its on-parcel portion (STEP 1's own on-parcel cell test) before returning
it. A patch with zero on-parcel area, or whose clipped remainder falls
below MIN_PRODUCTION_AREA_ACRES, is dropped entirely.

FOOTPRINT GEOMETRY: polygon_utm/area_acres/geometry_wgs84 are built from
the REAL per-cell-square union footprint (_cell_union_footprint()), NOT a
convex hull of cell center points -- a convex hull fills in concave gaps
between actual qualifying cells with ground that was never actually
eligible, over-reporting the real footprint. display_polygon_utm is a
convex hull of the same cells -- ALWAYS a single Polygon -- exposed only
for rendering convenience; nothing here or downstream reads it for area or
spatial-relationship purposes. polygon_utm/geometry_wgs84 CAN legitimately
come back as a MultiPolygon: two cells whose real ground squares only
touch at a shared corner don't merge into one solid Polygon under
unary_union.
"""

import math

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import MultiPoint, Point, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_suitability import ASPECT_FACTOR_WEIGHT, SLOPE_FACTOR_WEIGHT
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres, connected_components, pixel_center_xy
from soil_data import (
    coordinates_to_wkt_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    hydric_disqualifying_mukeys,
)
from terrain_metrics import aspect_score, compute_slope_and_aspect

# Cells at or below this slope are considered plausibly workable
# production/cultivation ground for this heuristic. 15% is a common
# rule-of-thumb ceiling for mechanized row-crop work specifically, but
# ground up to ~20-25% is workable for pasture, hay, or contour-managed
# production — 20% still leaves meaningful separation from genuinely
# steep, erosion-prone ground. This is a HARD gate (cells above it are
# excluded outright, never merely scored low) — see STEP 1's own docstring
# for why a soft score here would let a worst-first trim end up keeping
# genuinely-too-steep cells just to hit a volume target. CONFIGURABLE.
MAX_PRODUCTION_SLOPE_PCT = 20.0

# Drop tiny, likely-noisy contiguous patches below this size. CONFIGURABLE.
MIN_PRODUCTION_AREA_ACRES = 0.5

# --- per-cell weighting (STEP 1's own scoring, used to order STEP 2's
# worst-first ceiling trim) ---
#
# production_suitability.py's zone-level composite score weights three
# factors: SLOPE_FACTOR_WEIGHT (0.55), SIZE_FACTOR_WEIGHT (0.30, acreage +
# Polsby-Popper shape compactness), ASPECT_FACTOR_WEIGHT (0.15). size_factor
# is a CLUSTER-level shape property -- "is this patch's footprint compact
# or a thin sliver" has no meaning for a single grid cell, so it's excluded
# from per-cell scoring entirely rather than forced onto cells it can't
# describe. The remaining two factors' EXISTING relative weight (0.55:0.15,
# i.e. 11:3) is preserved here, just renormalized to sum to 1.0 on its own
# -- not a new ratio invented for this pass.
_PER_CELL_WEIGHT_SUM = SLOPE_FACTOR_WEIGHT + ASPECT_FACTOR_WEIGHT
PER_CELL_SLOPE_WEIGHT = SLOPE_FACTOR_WEIGHT / _PER_CELL_WEIGHT_SUM
PER_CELL_ASPECT_WEIGHT = ASPECT_FACTOR_WEIGHT / _PER_CELL_WEIGHT_SUM

assert math.isclose(PER_CELL_SLOPE_WEIGHT + PER_CELL_ASPECT_WEIGHT, 1.0, abs_tol=1e-9), (
    "per-cell slope/aspect weights must sum to 1.0"
)

# Sentinel distinguishing "the disqualifying-soil fetch genuinely ran and
# found nothing" (a real value of None) from "never checked at all" (fetch
# failed, or check_soil=False) -- same reasoning as the rest of this
# pipeline's None-vs-"unavailable" conventions.
_SOIL_CHECK_UNCHECKED = object()

PRODUCTION_AREA_CONFIDENCE_NOTES = (
    "This is a slope + hydric-soil heuristic (contiguous ground at or below "
    f"{MAX_PRODUCTION_SLOPE_PCT}% grade, with hydric/wetland soil excluded "
    "cell-by-cell before clustering -- see soil_data.hydric_disqualifying_"
    "mukeys()), not a validated production-area determination. It doesn't "
    "account for soil quality beyond that hard hydric exclusion, drainage, "
    "existing land use, or the property owner's actual crop/grazing plans "
    "— see soil_data.py and the Land Shape section of the full Scale of "
    "Permanence report for those factors."
)


def compute_slope_percent(
    array: np.ndarray, resolution_meters: tuple[float, float]
) -> np.ndarray:
    """
    Local terrain steepness at every valid cell: the steepest elevation
    change (up or down, whichever neighbor differs most per unit ground
    distance) among its up-to-8 neighbors, as a percent grade
    (rise/run * 100). NaN at nodata cells.
    """
    rows, cols = array.shape
    valid = ~np.isnan(array)
    px, py = resolution_meters
    slope = np.full((rows, cols), np.nan, dtype=np.float32)

    offsets = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            steepest = 0.0
            for dr, dc in offsets:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    distance = math.hypot(dc * px, dr * py)
                    grade = abs(float(array[r, c]) - float(array[nr, nc])) / distance * 100.0
                    steepest = max(steepest, grade)
            slope[r, c] = steepest

    return slope


def _slope_factor(slope_values_pct: list[float], max_slope_pct: float) -> float:
    """1.0 at 0% grade, falling linearly to 0.0 at max_slope_pct -- the same
    slope ceiling that decides eligibility at all, so a cell/cluster sitting
    right at that ceiling (barely qualifying) scores near 0 rather than
    being treated the same as dead-flat ground."""
    if not slope_values_pct:
        return 0.0
    avg_slope = float(np.mean(slope_values_pct))
    return max(0.0, min(1.0, 1.0 - avg_slope / max_slope_pct))


def per_cell_score(slope_pct: float, aspect_deg: float, max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT) -> float:
    """
    0-1 per-cell quality score for one DEM cell's own real slope/aspect --
    reuses _slope_factor() (called on a length-1 list, the same formula
    production_suitability.py's STEP 4 composite uses at the cluster level)
    and terrain_metrics.aspect_score() (already a per-value function, so it
    needs no adaptation). See PER_CELL_SLOPE_WEIGHT/PER_CELL_ASPECT_WEIGHT
    above for the weighting and why size has no per-cell counterpart.
    """
    slope_factor = _slope_factor([slope_pct], max_slope_pct)
    aspect_factor = aspect_score(aspect_deg)
    return PER_CELL_SLOPE_WEIGHT * slope_factor + PER_CELL_ASPECT_WEIGHT * aspect_factor


def _fetch_disqualifying_soil_union(wkt_polygon: str, dem: dict):
    """
    Real SSURGO polygon geometry (not just component ratings) for every map
    unit whose SUMMED hydric component percentage meets
    soil_data.MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE, within wkt_polygon,
    unioned and reprojected into dem['crs']. Returns None if nothing
    disqualifying was found (the common, clean case -- not an error).

    soil_data.hydric_disqualifying_mukeys() decides which mukeys qualify --
    shared, not reimplemented here, with road_corridors.py's own hydric
    union fetch. Component-table rows (get_soil_data_for_polygon()) decide
    WHICH mukeys qualify; a second, geometry-specific query
    (get_soil_geometries_for_polygon()) fetches only those mukeys' actual
    polygons.

    Fetched ONCE per pipeline call, over the real parcel boundary (or
    whatever region a caller passes) -- STEP 1 tests each ELIGIBLE cell's
    own center point against this single union, so there is no need for a
    separate per-candidate-patch soil fetch the way the old, pre-
    consolidation architecture required (patches didn't exist yet when this
    runs).
    """
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    disqualifying_mukeys = hydric_disqualifying_mukeys(soil_components)
    if not disqualifying_mukeys:
        return None

    geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)
    pieces = [
        shape(transform_geom("EPSG:4326", dem["crs"], geometries_by_mukey[mukey]))
        for mukey in disqualifying_mukeys
        if mukey in geometries_by_mukey
    ]
    return unary_union(pieces) if pieces else None


def _cell_union_footprint(cells: list[tuple[int, int]], dem: dict):
    """
    The REAL footprint of a set of DEM cells: the union of each cell's own
    ground square at the DEM's resolution -- NOT a convex hull of cell
    CENTER points. A convex hull of centers fills in any concave gaps
    between the actual qualifying cells with ground that was never actually
    a surviving cell at all -- for a large, roughly-solid patch this means
    the hull reports MORE area than the true footprint; for a sparse,
    scattered fragment it can under-report instead (it misses each
    individual cell's own half-cell-width margin the real union always
    includes). Either way, the hull is only an approximation and this
    function is the accurate one every spatial consumer should use.
    """
    px, py = dem["resolution_meters"]
    squares = []
    for r, c in cells:
        x, y = pixel_center_xy(dem, r, c)
        squares.append(box(x - px / 2, y - py / 2, x + px / 2, y + py / 2))
    return unary_union(squares)


def compute_step1_eligible_cells(
    dem: dict,
    boundary_polygon_utm: Polygon,
    disqualifying_soil_union_utm=_SOIL_CHECK_UNCHECKED,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
) -> dict:
    """
    STEP 1 of the consolidated production-zone pipeline -- computed ONCE
    per pipeline run and reused by every step after it (see module
    docstring).

    disqualifying_soil_union_utm: a pre-fetched shapely Polygon/MultiPolygon
    (already reprojected into dem['crs']) of disqualifying (hydric) soil, a
    real None (soil WAS checked and genuinely came back clean), or the
    default sentinel meaning "not checked at all" (soil_data_available will
    be False and every cell that clears the slope gate is treated as
    eligible, same as if hydric soil didn't exist -- graceful degradation,
    same convention as every other optional network layer in this
    pipeline).

    Returns:
        {
            'eligible_mask': np.ndarray[bool],       # slope- AND soil-eligible, on-parcel
            'slope_only_mask': np.ndarray[bool],     # slope-eligible + on-parcel, BEFORE
                                                       # the hydric gate -- this is the
                                                       # connectivity source used for
                                                       # source_patch_id/soil_carved
                                                       # bookkeeping, not a second scoring pass
            'slope_source_labels': np.ndarray[int],  # 8-connected-component labels of
                                                       # slope_only_mask (-1 outside it)
            'slope_pct': np.ndarray[float],
            'aspect_deg': np.ndarray[float],
            'per_cell_slope_factor': np.ndarray[float],   # NaN outside eligible_mask
            'per_cell_aspect_factor': np.ndarray[float],  # NaN outside eligible_mask
            'per_cell_score': np.ndarray[float],          # combined; NaN outside eligible_mask;
                                                            # STEP 2's worst-first trim ordering
            'soil_carved_acres_by_cell': np.ndarray[float],  # NaN outside slope_only_mask;
                                                               # same value replicated across
                                                               # every cell of one contiguous
                                                               # slope-only source region --
                                                               # bookkeeping only, not per-cell
            'soil_carved_pct_by_cell': np.ndarray[float],
            'soil_data_available': bool,   # whether the hydric check actually ran
        }
    """
    array = dem["array"]
    resolution = dem["resolution_meters"]
    rows, cols = array.shape

    slope_pct = compute_slope_percent(array, resolution)
    _, aspect_deg = compute_slope_and_aspect(array, resolution)

    slope_ok = (~np.isnan(slope_pct)) & (slope_pct <= max_slope_pct)

    boundary_prepared = prep(boundary_polygon_utm)
    slope_only_mask = np.zeros((rows, cols), dtype=bool)
    for r, c in np.argwhere(slope_ok):
        r, c = int(r), int(c)
        if boundary_prepared.contains(Point(pixel_center_xy(dem, r, c))):
            slope_only_mask[r, c] = True

    soil_data_available = disqualifying_soil_union_utm is not _SOIL_CHECK_UNCHECKED
    soil_union = disqualifying_soil_union_utm if soil_data_available else None

    hydric_hit = np.zeros((rows, cols), dtype=bool)
    if soil_union is not None:
        soil_prepared = prep(soil_union)
        for r, c in np.argwhere(slope_only_mask):
            r, c = int(r), int(c)
            if soil_prepared.contains(Point(pixel_center_xy(dem, r, c))):
                hydric_hit[r, c] = True

    eligible_mask = slope_only_mask & (~hydric_hit)

    per_cell_slope_factor = np.full((rows, cols), np.nan, dtype=np.float32)
    per_cell_aspect_factor = np.full((rows, cols), np.nan, dtype=np.float32)
    per_cell_composite = np.full((rows, cols), np.nan, dtype=np.float32)
    for r, c in np.argwhere(eligible_mask):
        r, c = int(r), int(c)
        sf = _slope_factor([float(slope_pct[r, c])], max_slope_pct)
        af = aspect_score(float(aspect_deg[r, c]))
        per_cell_slope_factor[r, c] = sf
        per_cell_aspect_factor[r, c] = af
        per_cell_composite[r, c] = PER_CELL_SLOPE_WEIGHT * sf + PER_CELL_ASPECT_WEIGHT * af

    slope_source_labels, num_slope_sources = connected_components(slope_only_mask)

    area_per_cell = cell_area_acres(dem)
    soil_carved_acres_by_cell = np.full((rows, cols), np.nan, dtype=np.float32)
    soil_carved_pct_by_cell = np.full((rows, cols), np.nan, dtype=np.float32)
    for label in range(num_slope_sources):
        label_mask = slope_source_labels == label
        total_cells = int(label_mask.sum())
        if total_cells == 0:
            continue
        eligible_cells_in_label = int((label_mask & eligible_mask).sum())
        carved_cells = total_cells - eligible_cells_in_label
        carved_acres = round(carved_cells * area_per_cell, 2)
        total_acres = total_cells * area_per_cell
        carved_pct = round(carved_acres / total_acres * 100, 1) if total_acres > 0 else 0.0
        soil_carved_acres_by_cell[label_mask] = carved_acres
        soil_carved_pct_by_cell[label_mask] = carved_pct

    return {
        "eligible_mask": eligible_mask,
        "slope_only_mask": slope_only_mask,
        "slope_source_labels": slope_source_labels,
        "slope_pct": slope_pct,
        "aspect_deg": aspect_deg,
        "per_cell_slope_factor": per_cell_slope_factor,
        "per_cell_aspect_factor": per_cell_aspect_factor,
        "per_cell_score": per_cell_composite,
        "soil_carved_acres_by_cell": soil_carved_acres_by_cell,
        "soil_carved_pct_by_cell": soil_carved_pct_by_cell,
        "soil_data_available": soil_data_available,
    }


def cluster_and_gate(
    cell_mask: np.ndarray,
    dem: dict,
    boundary_polygon_utm: Polygon,
    step1: dict,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
) -> list[dict]:
    """
    STEP 3 of the consolidated production-zone pipeline: 8-connected-
    component labeling of `cell_mask` (STEP 1's raw eligible_mask here with
    no trim, or production_area_ceiling.py's post-STEP-2-trim survivor
    mask), each cluster's REAL cell-union footprint
    (_cell_union_footprint()), and a pure area survival gate -- a cluster
    survives if and only if its real footprint clears min_area_acres.
    Nothing else gates survival here: soil was already excluded at STEP 1,
    so there is nothing left to carve or reject at this stage.

    `step1` is compute_step1_eligible_cells()'s own return dict -- used
    here only to attach each surviving cluster's 'source_patch_id' (the
    connected-component label of STEP 1's pre-hydric slope_only_mask that
    the cluster's cells trace back to), which production_suitability.py's
    STEP 4 uses to look up that source region's soil_carved_acres/pct
    bookkeeping. A caller with no meaningful step1 in scope can still pass
    one built with disqualifying_soil_union_utm=None.

    Returns the same shape identify_production_areas() itself returns,
    PLUS 'cells' (this cluster's own constituent DEM cells) and
    'source_patch_id' -- consumed directly by production_suitability.py's
    score_production_areas(), so STEP 4 never has to recompute/recover
    cluster membership from a mask a second time.

    polygon_utm/geometry_wgs84 CAN legitimately come back as a
    MultiPolygon: connected_components() is 8-connected (diagonal
    neighbors count as one component), but two cells whose real ground
    squares only touch at a shared corner do not merge into one solid
    Polygon under unary_union -- their true combined footprint really is
    disconnected. feature_schema.py's GeoJSON schema already accepts
    MultiPolygon.
    """
    labels, num_components = connected_components(cell_mask)
    area_per_cell = cell_area_acres(dem)
    slope_source_labels = step1["slope_source_labels"]

    patches = []
    for component_id in range(num_components):
        cells = [(int(r), int(c)) for r, c in np.argwhere(labels == component_id)]
        if not cells:
            continue

        elevations = [float(dem["array"][r, c]) for r, c in cells]

        footprint = _cell_union_footprint(cells, dem)
        polygon_utm = footprint.intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
        if area_acres < min_area_acres:
            continue

        # Display-only convex hull -- NOT used for area_acres or any
        # spatial relationship; see module docstring.
        utm_points = [pixel_center_xy(dem, r, c) for r, c in cells]
        display_polygon_utm = MultiPoint(utm_points).convex_hull
        if display_polygon_utm.geom_type != "Polygon":
            px, py = dem["resolution_meters"]
            display_polygon_utm = display_polygon_utm.buffer(max(px, py) / 2)
        display_polygon_utm = display_polygon_utm.intersection(boundary_polygon_utm)

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

        first_r, first_c = cells[0]
        source_patch_id = int(slope_source_labels[first_r, first_c])

        patches.append(
            {
                "id": int(component_id),
                "area_acres": round(float(area_acres), 2),
                "representative_elevation_m": float(np.median(elevations)),
                "polygon_utm": polygon_utm,
                "display_polygon_utm": display_polygon_utm,
                "geometry_wgs84": geometry_wgs84,
                "cells": cells,
                "source_patch_id": source_patch_id,
            }
        )

    return patches


def identify_production_areas(
    dem: dict,
    boundary_polygon_utm: Polygon,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
    check_soil: bool = True,
) -> list[dict]:
    """
    Returns one entry per candidate production-area patch, clipped to the
    real parcel (see module docstring's PARCEL CLIPPING section):

        {
            'id': int,
            'area_acres': float,
            'representative_elevation_m': float,
            'polygon_utm': shapely Polygon/MultiPolygon,
            'display_polygon_utm': shapely Polygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'cells': list[(row, col)],
            'source_patch_id': int,
        }

    STEP 1 (compute_step1_eligible_cells(), no ceiling trim) + STEP 3
    (cluster_and_gate()) chained together -- see module docstring. When
    check_soil is True (the default), this fetches real disqualifying-soil
    geometry for the real parcel boundary ONCE and feeds it into STEP 1's
    cell-level hydric gate; a fetch failure degrades gracefully to
    slope-only eligibility (same fetch-then-continue pattern every other
    optional network layer in this pipeline already uses), not a crash --
    existing callers (water_candidate_zones.py, road_corridors.py,
    solar_suitability.py) that already call this exact function/signature
    get STEP 1's hydric exclusion automatically, with no changes needed on
    their end.
    """
    disqualifying_soil_union_utm = _SOIL_CHECK_UNCHECKED
    if check_soil:
        try:
            xs, ys = boundary_polygon_utm.exterior.coords.xy
            lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
            wkt_polygon = coordinates_to_wkt_polygon(list(zip(lons, lats)))
            disqualifying_soil_union_utm = _fetch_disqualifying_soil_union(wkt_polygon, dem)
        except Exception:
            disqualifying_soil_union_utm = _SOIL_CHECK_UNCHECKED

    step1 = compute_step1_eligible_cells(dem, boundary_polygon_utm, disqualifying_soil_union_utm, max_slope_pct)
    return cluster_and_gate(step1["eligible_mask"], dem, boundary_polygon_utm, step1, min_area_acres)


def production_areas_to_geojson(patches: list[dict]) -> dict:
    """Wraps identify_production_areas() output as a schema-conformant
    GeoJSON FeatureCollection (layer="production_area_candidate") — a
    diagnostic layer, useful for checking this heuristic's output directly
    against a known property, independent of the valley/zone logic built
    on top of it."""
    features = [
        make_feature(
            feature_id=f"production-area-{p['id']}",
            geometry=p["geometry_wgs84"],
            layer="production_area_candidate",
            label=f"Production area candidate {p['id']}",
            confidence=CONFIDENCE_LOW,
            confidence_notes=PRODUCTION_AREA_CONFIDENCE_NOTES,
            extra_properties={
                "area_acres": p["area_acres"],
                "representative_elevation_m": round(p["representative_elevation_m"], 1),
            },
        )
        for p in patches
    ]
    return make_feature_collection(features)


def summarize_production_areas(patches: list[dict]) -> str:
    if not patches:
        return "No production-area candidates identified on this property."

    lines = [f"Production-area candidates found: {len(patches)}"]
    for p in sorted(patches, key=lambda x: -x["area_acres"]):
        lines.append(
            f"  - Patch {p['id']}: {p['area_acres']} acres, "
            f"representative elevation {p['representative_elevation_m']:.1f}m"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline smoke test with a synthetic DEM: a flat low bench (workable
    # production land) bordered by a steep rise. See
    # test_production_area.py for the assertion-based version.
    size = 30
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        for col in range(size):
            if row < 15:
                array[row, col] = 100.0  # flat bench
            else:
                array[row, col] = 100.0 + (row - 14) * 5.0  # steep rise

    synthetic_dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    # A boundary matching the DEM's full extent — this smoke test isn't
    # about parcel clipping, just the slope heuristic itself.
    full_extent_boundary = box(500000.0, 4500000.0 - size * 5.0, 500000.0 + size * 5.0, 4500000.0)

    patches = identify_production_areas(synthetic_dem, full_extent_boundary, check_soil=False)
    print(summarize_production_areas(patches))
