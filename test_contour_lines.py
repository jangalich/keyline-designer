"""
test_contour_lines.py

Offline (no-network) checks for contour_lines.py -- pure geometry
computation against small synthetic DEMs, same "no real data fetch
required" philosophy as the rest of this pipeline's tests.
"""

import math

import numpy as np

from contour_lines import CONTOUR_INTERVAL_METERS, compute_contour_lines
from raster_grid import pixel_center_xy

RESOLUTION = (5.0, 5.0)


def _dem(array: np.ndarray) -> dict:
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


# --- Linear ramp: a known, simple synthetic slope must produce a predictable,
#     countable number of contour lines, correctly spaced by interval_meters ---

ROWS, COLS = 50, 10
RISE_PER_ROW = 0.5  # meters -- 0.5m rise every 5m row = 10% grade
BASE_ELEVATION = 100.0

ramp_array = np.zeros((ROWS, COLS), dtype=np.float32)
for row in range(ROWS):
    ramp_array[row, :] = BASE_ELEVATION + row * RISE_PER_ROW
ramp_dem = _dem(ramp_array)

TOTAL_RISE = (ROWS - 1) * RISE_PER_ROW  # elevation at the last row minus the first
INTERVAL = 0.6  # deliberately explicit, independent of whatever CONTOUR_INTERVAL_METERS currently is

contours = compute_contour_lines(ramp_dem, interval_meters=INTERVAL)

# The ramp spans [100.0, 100.0 + TOTAL_RISE] -- the number of interval-spaced
# levels strictly inside that range (contourpy only draws a level if it's
# actually crossed, so a level exactly at the max isn't drawn -- np.arange's
# own exclusive stop already matches that) is floor(TOTAL_RISE / INTERVAL),
# plus 1 if the very first level (ceil(min/interval)*interval) sits exactly
# at the ramp's own minimum (a real, drawable crossing right at row 0).
first_level = math.ceil(BASE_ELEVATION / INTERVAL) * INTERVAL
expected_count = len(np.arange(first_level, BASE_ELEVATION + TOTAL_RISE, INTERVAL))

assert len(contours) == expected_count, (
    f"a linear ramp with a known {TOTAL_RISE}m total rise at a {INTERVAL}m interval should produce exactly "
    f"{expected_count} contour line(s), got {len(contours)}"
)
print(
    f"Linear ramp: a {TOTAL_RISE}m total-rise slope at a {INTERVAL}m contour interval produces exactly the "
    f"predicted {expected_count} contour line(s)."
)

# Spacing between consecutive levels must be EXACTLY interval_meters (a
# linear ramp with a single, monotonic gradient has no reason to skip or
# double up a level).
sorted_elevations = sorted(c["elevation_m"] for c in contours)
spacings = [round(b - a, 6) for a, b in zip(sorted_elevations, sorted_elevations[1:])]
assert all(abs(s - INTERVAL) < 1e-6 for s in spacings), (
    f"consecutive contour levels on a simple linear ramp must be spaced exactly {INTERVAL}m apart, "
    f"got spacings {spacings}"
)
print(f"Linear ramp: consecutive contour levels are spaced exactly {INTERVAL}m apart, as expected.")

# Each level on this simple ramp should be a single straight line spanning
# the full column width (the ramp only varies by row) -- one connected
# LineString per level, not a fragmented MultiLineString.
for c in contours:
    assert c["lines_utm"].geom_type == "LineString", (
        f"a single, monotonic linear ramp should produce ONE connected line per level, got "
        f"{c['lines_utm'].geom_type} at {c['elevation_m']}m"
    )
    assert c["geometry_wgs84"]["type"] == "LineString"
print("Linear ramp: every contour level is a single connected LineString (no spurious fragmentation).")


# --- Default CONTOUR_INTERVAL_METERS: sanity check it's the documented ~2 ft (0.6m) starting value ---

assert abs(CONTOUR_INTERVAL_METERS - 0.6) < 1e-9, (
    f"CONTOUR_INTERVAL_METERS should be the documented 0.6m (~2 ft) starting value, got {CONTOUR_INTERVAL_METERS}"
)
default_contours = compute_contour_lines(ramp_dem)
assert len(default_contours) > 0, "the default interval must produce real contour lines on a sloped DEM"
print(f"Default CONTOUR_INTERVAL_METERS={CONTOUR_INTERVAL_METERS}m produces {len(default_contours)} real contour line(s) on the ramp.")


# --- NaN (nodata) handling: contour lines must stop at the edge of valid data, not
#     interpolate across a nodata gap ---

nodata_array = ramp_array.copy()
nodata_array[10:40, 6:10] = np.nan  # cut a nodata block out of the east side, spanning several rows

nodata_dem = _dem(nodata_array)
nodata_contours = compute_contour_lines(nodata_dem, interval_meters=INTERVAL)

# Pick a level that falls within the nodata block's row range (rows 10-39 ->
# elevations 105.0 to 119.5) -- its line must not extend past column 6's
# real-world x coordinate (the edge of the nodata block), since nothing
# beyond that is valid data.
nodata_edge_x = pixel_center_xy(nodata_dem, 20, 6)[0] - RESOLUTION[0] / 2  # left edge of the nodata block
probe_level = 110.0
probe_contour = next((c for c in nodata_contours if abs(c["elevation_m"] - probe_level) < INTERVAL), None)
assert probe_contour is not None, "expected a contour line near 110.0m given the ramp's own elevation range"
probe_geom = probe_contour["lines_utm"]
segments = [probe_geom] if probe_geom.geom_type == "LineString" else list(probe_geom.geoms)
max_x_reached = max(coord[0] for seg in segments for coord in seg.coords)
assert max_x_reached <= nodata_edge_x + 1e-6, (
    f"a contour line crossing the nodata block's row range must stop at the nodata boundary "
    f"(x <= {nodata_edge_x}), but reached x={max_x_reached} -- it must not interpolate across the gap"
)
print(
    f"NaN handling: a contour line crossing rows with a nodata block correctly stops at the real data "
    f"boundary (x <= {round(nodata_edge_x, 2)}) rather than interpolating across the gap."
)


# --- Degenerate cases: fully flat terrain produces zero contour lines; a fully-NaN DEM returns [] ---

flat_array = np.full((10, 10), 100.0, dtype=np.float32)
flat_contours = compute_contour_lines(_dem(flat_array), interval_meters=INTERVAL)
assert flat_contours == [], f"perfectly flat terrain must produce zero contour lines, got {len(flat_contours)}"
print("Degenerate case: perfectly flat terrain produces zero contour lines.")

all_nan_array = np.full((10, 10), np.nan, dtype=np.float32)
all_nan_contours = compute_contour_lines(_dem(all_nan_array), interval_meters=INTERVAL)
assert all_nan_contours == [], f"a fully-nodata DEM must return [], got {len(all_nan_contours)}"
print("Degenerate case: a fully-nodata DEM returns [].")


print("\nAll contour_lines checks passed.")
