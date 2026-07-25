"""
contour_lines.py

Elevation contour lines over a DEM's full extent -- pure geometry
computation (no plotting), following this codebase's usual split between
computation modules (raster_grid.py, valley_delineation.py) and the one
module that actually touches matplotlib for drawing (render_layout_map.py).

Built for render_layout_map.py's production-zone rendering style: instead
of a filled/shaded shape, production zones are drawn as contour-line
texture, clipped per zone at render time (real geometry_wgs84/polygon_utm
intersection, not a pre-clipped raster) -- see that module's own
docstring for the full rendering picture. compute_contour_lines() itself
knows nothing about zones; it always computes over the DEM's own full
extent, same "compute once, let callers clip/reuse" pattern
production_area.py's compute_step1_eligible_cells() already established.

Output shape mirrors valley_delineation.delineate_valleys(): one entry
per contour LEVEL, carrying both real dem['crs']-meter geometry (for
math/clipping -- render_layout_map.py intersects this directly against
each zone's own polygon_utm, same CRS, no reprojection needed first) and
a WGS84 GeoJSON counterpart (display/API parity with every other layer
module in this pipeline, even though the current sole consumer clips the
UTM version directly).
"""

import math

import contourpy
import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString, MultiLineString

from raster_grid import pixel_center_xy

# Vertical spacing between adjacent contour lines. 0.6m (~2 ft) is chosen
# specifically because it's appropriate for the NATIONWIDE-GUARANTEED
# USGS 3DEP 1/3 arc-second DEM baseline (~0.82m RMSE vertical accuracy
# everywhere in the US, per USGS's own current published figures) -- NOT
# the finer resolution some properties may happen to have via 1m lidar
# coverage. Drawing contour detail finer than the DEM's own real vertical
# accuracy would draw precision the data can't actually support
# nationwide. Like every other threshold in this pipeline, this is a
# documented STARTING value, UNVALIDATED against real ground-truth and
# open to revisiting -- not a derived or measured figure. CONFIGURABLE.
CONTOUR_INTERVAL_METERS = 0.6  # ~2 ft


def _grid_axes(dem: dict) -> tuple[np.ndarray, np.ndarray]:
    """
    Cell-center x (ascending, real dem['crs'] meters) / y (DESCENDING,
    matching this DEM dict's own row-increases-southward convention --
    see raster_grid.pixel_center_xy()) coordinate arrays. contourpy
    accepts a descending y axis correctly (its contour_generator() only
    needs x/y to be monotonic, either direction, matched consistently to
    the z array's own row/col order) -- there's no need to flip the
    elevation array to force y ascending.
    """
    rows, cols = dem["array"].shape
    x = np.array([pixel_center_xy(dem, 0, c)[0] for c in range(cols)])
    y = np.array([pixel_center_xy(dem, r, 0)[1] for r in range(rows)])
    return x, y


def _segment_to_wgs84_linestring(dem: dict, vertices_utm: np.ndarray) -> dict:
    xs, ys = vertices_utm[:, 0], vertices_utm[:, 1]
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
    return {"type": "LineString", "coordinates": list(zip(lons, lats))}


def compute_contour_lines(dem: dict, interval_meters: float = CONTOUR_INTERVAL_METERS) -> list[dict]:
    """
    Runs contourpy (matplotlib's own contour-COMPUTATION engine -- an
    unavoidable transitive dependency of matplotlib>=3.8, the exact
    matplotlib version already required by this pipeline and already
    used by render_layout_map.py, not a new third-party choice) directly
    against the DEM's own elevation array, over its FULL extent (no
    zone/boundary clipping here -- that happens per zone, at render
    time). contourpy.contour_generator().lines(level) is real, PUBLIC,
    documented API for exactly this: pure line-vertex geometry, no
    Figure/Axes/plotting involved at all -- unlike matplotlib's own
    ContourSet internals (.allsegs / .get_paths()), which have changed
    shape across matplotlib versions. This codebase already avoids
    relying on that kind of fragile private/internal API elsewhere (see
    render_layout_map.py's own _extent_based_auto_zoom() docstring,
    reimplementing contextily's zoom calculation directly rather than
    importing its private underscore-prefixed function, for the same
    reasoning) -- contourpy's public API is the equivalent right call
    here.

    NaN (nodata) cells are correctly excluded: contourpy stops/truncates
    contour lines at the boundary of valid data rather than interpolating
    across a nodata gap (confirmed against its real behavior, not just
    assumed).

    Returns one entry per contour level (an elevation value spaced
    interval_meters apart, covering the DEM's real min-max elevation
    range) that has at least one real line segment anywhere in the DEM:

        {
            'elevation_m': float,
            'lines_utm': shapely LineString/MultiLineString,  # real dem['crs'] meters, FULL DEM extent
            'geometry_wgs84': GeoJSON LineString or MultiLineString,
        }

    A DEM with no valid (non-NaN) cells at all returns [].
    """
    array = dem["array"]
    valid_values = array[~np.isnan(array)]
    if valid_values.size == 0:
        return []

    x, y = _grid_axes(dem)
    min_elevation = float(np.min(valid_values))
    max_elevation = float(np.max(valid_values))

    first_level = math.ceil(min_elevation / interval_meters) * interval_meters
    levels = np.arange(first_level, max_elevation, interval_meters)

    generator = contourpy.contour_generator(x=x, y=y, z=array, line_type=contourpy.LineType.Separate)

    contours = []
    for level in levels:
        segments = [seg for seg in generator.lines(float(level)) if len(seg) >= 2]
        if not segments:
            continue

        lines_utm = (
            LineString(segments[0])
            if len(segments) == 1
            else MultiLineString([LineString(seg) for seg in segments])
        )

        line_geometries = [_segment_to_wgs84_linestring(dem, seg) for seg in segments]
        geometry_wgs84 = (
            line_geometries[0]
            if len(line_geometries) == 1
            else {
                "type": "MultiLineString",
                "coordinates": [g["coordinates"] for g in line_geometries],
            }
        )

        contours.append(
            {
                "elevation_m": round(float(level), 3),
                "lines_utm": lines_utm,
                "geometry_wgs84": geometry_wgs84,
            }
        )

    return contours


def summarize_contour_lines(contours: list[dict]) -> str:
    if not contours:
        return "No contour lines computed (empty or fully nodata DEM)."

    lines = [f"Contour lines: {len(contours)} level(s)"]
    for c in sorted(contours, key=lambda x: x["elevation_m"]):
        segment_count = (
            1 if c["lines_utm"].geom_type == "LineString" else len(c["lines_utm"].geoms)
        )
        lines.append(f"  - {c['elevation_m']}m: {segment_count} segment(s)")
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline smoke test: a simple linear ramp, same "quick eyeball check"
    # philosophy as valley_delineation.py's own __main__ block -- see
    # test_contour_lines.py for the real (assertion-based) version.
    size = 40
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        array[row, :] = 100.0 + row * 0.5  # 0.5m rise per 5m row -- 10% grade

    synthetic_dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    contours = compute_contour_lines(synthetic_dem)
    print(summarize_contour_lines(contours))
