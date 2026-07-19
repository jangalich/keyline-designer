"""
production_area.py

Heuristic identification of candidate production/cultivation area(s) on a
property from DEM-derived slope — a structured stand-in for the same
judgment report_generator.py's Scale of Permanence prompt already makes in
prose (the "Land Shape" section: which parts of the property read as
strong, workable production land versus steep/awkward/marginal ground).

That existing step is LLM narrative reasoning over a coarse elevation
grid, not a reusable structured value — there's no dedicated "production
area" data this module could just import. water_candidate_zones.py (Step
3 of the water-system candidate-zone logic) needs an actual elevation
number to compare valleys against, so this module derives one directly
from the same DEM already fetched for valley delineation, using a simple,
explainable slope threshold: contiguous, gently-sloped ground is treated
as workable production land; steep ground isn't.

This does NOT replace or modify the Land Shape narrative step in
report_generator.py — it's a separate, purpose-built heuristic feeding
only this module and water_candidate_zones.py's zone-filtering logic.
Deliberately as simple as imagery_data.py's fixed NDVI threshold
classifier — explainable and easy to sanity-check against a real
property, not a claim of agronomic precision.

PARCEL CLIPPING: dem_data.py deliberately fetches a DEM buffered 100m past
the drawn boundary (real, legitimate reasons — see its own docstring), so
a slope-qualifying patch can span both on-parcel and buffered off-parcel
ground. identify_production_areas() requires the real parcel boundary
(boundary_polygon_utm) and CLIPS each candidate down to its on-parcel
portion before returning it — a patch that's 34% on-parcel is returned as
that smaller, real 34%-sized candidate, not the full (partly off-parcel)
footprint. A patch with zero on-parcel area, or whose clipped remainder
falls below MIN_PRODUCTION_AREA_ACRES, is dropped entirely rather than
returned as a technically-nonempty but meaningless sliver. This was a
real bug (confirmed live: candidates describing land that isn't part of
the property), not a hypothetical — see test_production_area.py's
regression test for the exact scenario.
"""

import math

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import MultiPoint, Point, Polygon, box, mapping
from shapely.prepared import prep

from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres, connected_components, pixel_center_xy

# Cells at or below this slope are considered plausibly workable
# production/cultivation ground for this heuristic. CONFIGURABLE — tune
# against your own property: if ground you'd actually farm reads as too
# steep here (or vice versa), adjust this threshold rather than the
# valley/gradient logic downstream of it.
MAX_PRODUCTION_SLOPE_PCT = 15.0

# Drop tiny, likely-noisy contiguous patches below this size.
# CONFIGURABLE.
MIN_PRODUCTION_AREA_ACRES = 0.5

PRODUCTION_AREA_CONFIDENCE_NOTES = (
    "This is a slope-only heuristic (contiguous ground at or below "
    f"{MAX_PRODUCTION_SLOPE_PCT}% grade, computed from an interpolated "
    "DEM), not a validated production-area determination. It doesn't "
    "account for soil quality, drainage, aspect, existing land use, or "
    "the property owner's actual crop/grazing plans — see soil_data.py "
    "and the Land Shape section of the full Scale of Permanence report "
    "for those factors. Used here only as an elevation reference point "
    "for the water-system candidate-zone logic (water_candidate_zones.py), "
    "not as a production-land recommendation in its own right."
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


def identify_production_areas(
    dem: dict,
    boundary_polygon_utm: Polygon,
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
) -> list[dict]:
    """
    Returns one entry per candidate production-area patch, clipped to the
    real parcel (boundary_polygon_utm — see module docstring's PARCEL
    CLIPPING section for why this is required, not optional):

        {
            'id': int,
            'area_acres': float,                  # of the ON-PARCEL (clipped) footprint
            'representative_elevation_m': float,  # median elevation of the ON-PARCEL cells
            'polygon_utm': shapely Polygon/MultiPolygon,  # clipped to boundary_polygon_utm
            'geometry_wgs84': GeoJSON geometry dict,
        }

    polygon_utm is what water_candidate_zones.py does distance math
    against (real meters, DEM's own projected CRS); geometry_wgs84 is only
    for output/display. The convex hull of cell centers is a deliberately
    simple footprint for a heuristic patch — adequate for that proximity
    math, not a precise cadastral-grade boundary.

    A candidate slope-qualifying patch can span the DEM's buffered area
    past the drawn boundary (dem_data.py fetches ~100m past the parcel on
    purpose — see its docstring). This function filters each patch's
    cells down to the ones actually on the parcel BEFORE computing area/
    elevation/geometry, so every number reported describes real, on-parcel
    ground — not a mix of real and off-parcel land. A patch with no
    on-parcel cells at all is skipped entirely; one whose on-parcel
    remainder is real but tiny is dropped by the same min_area_acres
    filter already applied to the unclipped case. The final polygon is
    additionally intersected with boundary_polygon_utm as a defensive
    safety net — the convex hull of on-parcel cell centers can still bulge
    slightly past a concave boundary edge even when every contributing
    cell is itself on-parcel.
    """
    array = dem["array"]
    slope = compute_slope_percent(array, dem["resolution_meters"])
    candidate_mask = (~np.isnan(slope)) & (slope <= max_slope_pct)

    labels, num_components = connected_components(candidate_mask)
    area_per_cell = cell_area_acres(dem)
    boundary_prepared = prep(boundary_polygon_utm)

    patches = []
    for component_id in range(num_components):
        cells = np.argwhere(labels == component_id)
        on_parcel_cells = [
            (int(r), int(c))
            for r, c in cells
            if boundary_prepared.contains(Point(pixel_center_xy(dem, int(r), int(c))))
        ]
        if not on_parcel_cells:
            continue  # entire patch sits outside the drawn boundary -- not a real candidate

        area_acres = len(on_parcel_cells) * area_per_cell
        if area_acres < min_area_acres:
            continue

        elevations = [float(array[r, c]) for r, c in on_parcel_cells]
        representative_elevation_m = float(np.median(elevations))

        utm_points = [pixel_center_xy(dem, r, c) for r, c in on_parcel_cells]
        polygon_utm = MultiPoint(utm_points).convex_hull
        if polygon_utm.geom_type != "Polygon":
            # Degenerate hull (near-collinear patch) — buffer to a real
            # polygon rather than passing a Point/LineString downstream.
            px, py = dem["resolution_meters"]
            polygon_utm = polygon_utm.buffer(max(px, py) / 2)

        polygon_utm = polygon_utm.intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        # The clip above can, in principle, trim more than the cell-count
        # estimate above did (hull bulge past a concave boundary edge) —
        # recompute area from the actual returned geometry so area_acres
        # always matches polygon_utm exactly, not the pre-clip estimate.
        area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
        if area_acres < min_area_acres:
            continue

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

        patches.append(
            {
                "id": int(component_id),
                "area_acres": round(float(area_acres), 2),
                "representative_elevation_m": representative_elevation_m,
                "polygon_utm": polygon_utm,
                "geometry_wgs84": geometry_wgs84,
            }
        )

    return patches


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

    patches = identify_production_areas(synthetic_dem, full_extent_boundary)
    print(summarize_production_areas(patches))
