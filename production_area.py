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
"""

import math

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import MultiPoint, mapping

from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from raster_grid import cell_area_acres, connected_components, pixel_center_xy

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
    max_slope_pct: float = MAX_PRODUCTION_SLOPE_PCT,
    min_area_acres: float = MIN_PRODUCTION_AREA_ACRES,
) -> list[dict]:
    """
    Returns one entry per candidate production-area patch:

        {
            'id': int,
            'area_acres': float,
            'representative_elevation_m': float,  # median elevation of the patch
            'polygon_utm': shapely Polygon,        # convex hull of cell centers
            'geometry_wgs84': GeoJSON geometry dict,
        }

    polygon_utm is what water_candidate_zones.py does distance math
    against (real meters, DEM's own projected CRS); geometry_wgs84 is only
    for output/display. The convex hull of cell centers is a deliberately
    simple footprint for a heuristic patch — adequate for that proximity
    math, not a precise cadastral-grade boundary.
    """
    array = dem["array"]
    slope = compute_slope_percent(array, dem["resolution_meters"])
    candidate_mask = (~np.isnan(slope)) & (slope <= max_slope_pct)

    labels, num_components = connected_components(candidate_mask)
    area_per_cell = cell_area_acres(dem)

    patches = []
    for component_id in range(num_components):
        cells = np.argwhere(labels == component_id)
        area_acres = len(cells) * area_per_cell
        if area_acres < min_area_acres:
            continue

        elevations = [float(array[r, c]) for r, c in cells]
        representative_elevation_m = float(np.median(elevations))

        utm_points = [pixel_center_xy(dem, int(r), int(c)) for r, c in cells]
        polygon_utm = MultiPoint(utm_points).convex_hull
        if polygon_utm.geom_type != "Polygon":
            # Degenerate hull (near-collinear patch) — buffer to a real
            # polygon rather than passing a Point/LineString downstream.
            px, py = dem["resolution_meters"]
            polygon_utm = polygon_utm.buffer(max(px, py) / 2)

        xs, ys = zip(*polygon_utm.exterior.coords)
        lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
        geometry_wgs84 = {"type": "Polygon", "coordinates": [list(zip(lons, lats))]}

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

    patches = identify_production_areas(synthetic_dem)
    print(summarize_production_areas(patches))
