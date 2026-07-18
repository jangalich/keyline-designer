"""
terrain_metrics.py

DEM-derived terrain metrics for solar suitability scoring
(solar_suitability.py): slope, aspect, and a horizon-based shading proxy.
Same DEM dict shape as raster_grid.py/valley_delineation.py/
production_area.py, same reasoning for keeping this numpy-only with no
network or rasterio dependency of its own — testable against a synthetic
DEM (see test_terrain_metrics.py) independent of a real DEM fetch.

This is a separate module from production_area.py's compute_slope_percent
deliberately: that function answers a different question ("what's the
steepest grade to any neighbor," a flat/steep binary classifier input) and
production_area.py is explicitly untouched in this pass (it's part of the
merged, working water-system candidate zone feature). Slope here uses the
standard Horn (1981) 3x3-window method, and aspect (which
production_area.py doesn't compute at all) uses the matching standard
formula — both are what solar siting actually needs: true rise/run slope
and compass-bearing aspect, not just "is there a steep neighbor."
"""

import math

import numpy as np

# --- slope + aspect (Horn's method) -----------------------------------

# Horn (1981) 3x3 kernel weights for a well-conditioned dz/dx, dz/dy
# estimate — standard in GIS terrain analysis (ArcGIS, GDAL, QGIS all use
# this), better conditioned against single noisy DEM cells than a naive
# 2-cell finite difference.
_HORN_KERNEL_OFFSETS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def compute_slope_and_aspect(
    array: np.ndarray, resolution_meters: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray]:
    """
    Returns (slope_pct, aspect_deg), same shape as `array`:
      - slope_pct: rise/run as a percent grade (0 = flat).
      - aspect_deg: compass bearing (0/360=N, 90=E, 180=S, 270=W) of the
        downhill direction. NaN where slope is ~0 (aspect is undefined on
        flat ground) or where a full 3x3 neighborhood isn't available
        (nodata neighbor or grid edge).

    Uses Horn's method: needs all 8 neighbors valid, so cells at the grid
    edge or adjacent to a nodata gap get NaN rather than a value computed
    from a partial, lower-confidence neighborhood — for solar siting,
    dropping a thin edge fringe is preferable to silently guessing there.
    """
    rows, cols = array.shape
    px, py = resolution_meters
    valid = ~np.isnan(array)

    slope_pct = np.full((rows, cols), np.nan, dtype=np.float32)
    aspect_deg = np.full((rows, cols), np.nan, dtype=np.float32)

    for r in range(1, rows - 1):
        for c in range(1, cols - 1):
            if not valid[r, c]:
                continue
            neighborhood = {}
            complete = True
            for dr, dc in _HORN_KERNEL_OFFSETS:
                nr, nc = r + dr, c + dc
                if not valid[nr, nc]:
                    complete = False
                    break
                neighborhood[(dr, dc)] = float(array[nr, nc])
            if not complete:
                continue

            a, b, c_ = neighborhood[(-1, -1)], neighborhood[(-1, 0)], neighborhood[(-1, 1)]
            d, f = neighborhood[(0, -1)], neighborhood[(0, 1)]
            g, h, i = neighborhood[(1, -1)], neighborhood[(1, 0)], neighborhood[(1, 1)]

            dzdx = ((c_ + 2 * f + i) - (a + 2 * d + g)) / (8 * px)
            dzdy = ((g + 2 * h + i) - (a + 2 * b + c_)) / (8 * py)

            rise_run = math.hypot(dzdx, dzdy)
            slope_pct[r, c] = rise_run * 100.0

            if rise_run < 1e-6:
                continue  # flat: aspect undefined, leave as NaN

            aspect = math.degrees(math.atan2(dzdy, -dzdx))
            if aspect < 0:
                aspect = 90.0 - aspect
            elif aspect > 90.0:
                aspect = 360.0 - aspect + 90.0
            else:
                aspect = 90.0 - aspect
            aspect_deg[r, c] = aspect

    return slope_pct, aspect_deg


def aspect_to_compass_label(aspect_deg: float) -> str:
    """16-point compass label for a bearing in degrees (0-360)."""
    labels = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
    ]
    index = round((aspect_deg % 360) / 22.5) % 16
    return labels[index]


def aspect_score(aspect_deg: float) -> float:
    """
    0-1 solar suitability score for a compass bearing, peaking at 1.0 due
    south (180 deg, the northern-hemisphere solar optimum) and falling to
    0.0 due north (0/360 deg). Flat ground (aspect undefined, passed as
    NaN) scores 1.0 — flat ground has no unfavorable orientation, so it
    isn't penalized the way a north-facing slope would be.
    """
    if math.isnan(aspect_deg):
        return 1.0
    return (1.0 + math.cos(math.radians(aspect_deg - 180.0))) / 2.0


# --- shading proxy: DEM-only horizon analysis --------------------------

# Which compass directions matter for northern-hemisphere solar shading:
# the sun's arc runs through the southern sky, so terrain rising to the
# south/southeast/southwest is what actually blocks useful sun hours;
# terrain to the north is irrelevant (the sun is never in that direction).
# CONFIGURABLE.
SHADING_AZIMUTH_RANGE_DEG = (100.0, 260.0)  # ESE through S through WSW

# How far out (in cells) to search for terrain that could cast a shadow.
# CONFIGURABLE — larger catches distant ridgelines but costs more compute;
# a nearby hill matters far more than a similar rise 500m away.
SHADING_SEARCH_RADIUS_CELLS = 10

# Horizon (elevation) angle, in degrees, at or above which nearby terrain
# is treated as a significant shading concern (a fully shut-out score of
# 0). Real winter-solstice sun angles at mid-latitude US locations bottom
# out somewhere around 20-30 deg at solar noon — a horizon obstruction at
# or above that starts cutting meaningfully into the shoulder seasons, not
# just a couple of midwinter hours. CONFIGURABLE.
MAX_CONCERNING_HORIZON_ANGLE_DEG = 20.0


def _direction_within_shading_arc(dr: int, dc: int, azimuth_range: tuple[float, float]) -> bool:
    """dr/dc are grid offsets (row increases south, col increases east).
    Converts to a compass bearing and checks it against the shading arc."""
    bearing = math.degrees(math.atan2(dc, -dr)) % 360  # 0=N, 90=E, 180=S, 270=W
    low, high = azimuth_range
    return low <= bearing <= high


def compute_shading_score(
    array: np.ndarray,
    resolution_meters: tuple[float, float],
    search_radius_cells: int = SHADING_SEARCH_RADIUS_CELLS,
    max_concerning_angle_deg: float = MAX_CONCERNING_HORIZON_ANGLE_DEG,
    azimuth_range: tuple[float, float] = SHADING_AZIMUTH_RANGE_DEG,
) -> np.ndarray:
    """
    A coarse horizon-based shading proxy, not a real multi-azimuth
    viewshed/skyview calculation: for each valid cell, looks at every
    other valid cell within search_radius_cells whose direction falls in
    the "sun-relevant" southern arc, and finds the steepest horizon angle
    (how far above this cell's own elevation, over that ground distance) —
    the single most obstructive point in that arc, since one tall
    obstruction is enough to cast a shadow regardless of what's around it.

    Returns a 0-1 score per cell (same shape as `array`): 1.0 = open
    southern horizon (no shading concern), 0.0 = a horizon angle at or
    above max_concerning_angle_deg somewhere in the arc. NaN where the
    source cell itself is nodata.

    This captures TERRAIN shading (a ridge or hillside blocking low sun) —
    it has no way to see vegetation/canopy, which is a separate, real
    shading mechanism this proxy simply doesn't cover. See
    solar_suitability.py's confidence_notes for how that limitation is
    surfaced on the actual output feature.
    """
    rows, cols = array.shape
    px, py = resolution_meters
    valid = ~np.isnan(array)

    offsets = []
    for dr in range(-search_radius_cells, search_radius_cells + 1):
        for dc in range(-search_radius_cells, search_radius_cells + 1):
            if dr == 0 and dc == 0:
                continue
            if dr * dr + dc * dc > search_radius_cells * search_radius_cells:
                continue
            if _direction_within_shading_arc(dr, dc, azimuth_range):
                offsets.append((dr, dc))

    scores = np.full((rows, cols), np.nan, dtype=np.float32)

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            max_horizon_angle = 0.0
            this_elevation = float(array[r, c])
            for dr, dc in offsets:
                nr, nc = r + dr, c + dc
                if not (0 <= nr < rows and 0 <= nc < cols) or not valid[nr, nc]:
                    continue
                rise = float(array[nr, nc]) - this_elevation
                if rise <= 0:
                    continue  # lower or equal terrain doesn't shade
                run = math.hypot(dc * px, dr * py)
                horizon_angle = math.degrees(math.atan2(rise, run))
                if horizon_angle > max_horizon_angle:
                    max_horizon_angle = horizon_angle
            scores[r, c] = 1.0 - min(max_horizon_angle / max_concerning_angle_deg, 1.0)

    return scores
