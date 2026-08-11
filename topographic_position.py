"""
topographic_position.py

Continuous Topographic Position Index (TPI) grid from a DEM: for every
cell, that cell's own elevation minus the mean elevation of all valid
cells within a fixed real-world radius of it (Weiss, 2001, "Topographic
Position and Landforms Analysis" -- the standard formulation this is
modeled on: TPI = elevation(cell) - mean(elevation(neighborhood))).
Positive values are locally high ground relative to their surroundings
(a crest or spur), negative values are locally low (a hollow or draw),
and values near zero sit on a planar slope or flat area where the
neighborhood mean tracks the cell itself.

This is the first piece of a larger effort to replace road_corridors.py's
inverted-DEM ridge detection (flow-accumulation run on -elevation, which
finds ridge LINES the same way valley_delineation.py finds channels) with
a TPI-preference cost surface for road_cost_path.py -- roads generally
want to run along locally-high, well-drained ground rather than
crossing draws. This module is standalone and unconsumed for now: it
implements and tests the TPI grid itself, nothing wires it into
road_corridors.py or any pipeline/context object yet.

Same DEM dict shape and same numpy-only, no-rasterio, no-network import
discipline as valley_delineation.py and terrain_metrics.py, for the same
reason: this needs to be unit-testable against a small synthetic DEM
(see test_topographic_position.py) independent of whether a real DEM
fetch (dem_data.py) is working.

METHOD: a full disc neighborhood (every valid cell whose center falls
within radius_meters of the cell being scored, weighted equally), not an
annulus (excluding a smaller inner disc) -- the simpler of the two
standard TPI variants. Implemented as a set of (row, col) kernel offsets
built once from the DEM's own resolution_meters (which is NOT assumed
square: an offset (dr, dc) is inside the kernel iff
hypot(dc * pixel_size_x, dr * pixel_size_y) <= radius_meters, so a
non-square-pixel DEM gets a genuinely elliptical kernel in cell terms,
not a circular one squashed into a rectangle of cells). Each kernel
offset is applied to the whole grid at once via an array shift (same
shift-and-accumulate approach raster_grid.py's binary_erode()/
binary_dilate() use to stay scipy-free) rather than convolution, so this
has no scipy dependency either.

nodata is np.nan, matching valley_delineation.py's own _valid_mask()
convention. NaN cells are excluded from every neighborhood mean they'd
otherwise fall into (never treated as zero elevation), and a NaN cell's
own output is always NaN. A valid cell whose neighborhood contains fewer
than MIN_TPI_NEIGHBORHOOD_CELLS valid cells -- a grid corner, or ground
hugging a large nodata gap -- also gets NaN rather than a mean derived
from too small and unrepresentative a sample to mean anything.

Known limitations, stated plainly rather than glossed over:
  - Single fixed scale (radius_meters). Real terrain has landform
    structure at multiple scales at once (a small bench on the flank of
    a large ridge); this only ever answers "relative to ground roughly
    radius_meters away," not a multi-scale decomposition. Pick
    radius_meters for the landform scale that actually matters to the
    caller (see DEFAULT_TPI_RADIUS_METERS below) rather than expecting
    one run to serve every purpose.
  - Disc, not annulus. The Weiss (2001) formulation this module follows
    most closely for simplicity; some TPI variants instead use an annulus
    (excluding a smaller inner disc around the cell) to better separate
    "broad landform position" from "local micro-relief" at the same
    outer radius. Not implemented here -- a caller that needs that
    distinction needs a second run at a different radius, or a future
    annulus variant, not this function reinterpreted.
  - No slope term, so this alone can't distinguish a flat plateau summit
    from a knife-edge crest -- both can read as similarly positive TPI at
    a given radius. Weiss's own full slope-position classification
    combines TPI with a slope layer for exactly this reason; that
    combination is a job for a caller (e.g. a future road_corridors.py
    cost surface), not this module.
  - Returns RAW meters, deliberately NOT standardized, normalized, or
    clamped to any range. Same split as valley_delineation.py's
    get_flow_accumulation_for_dem(), which deliberately returns raw
    upstream cell counts and leaves the acres conversion to callers: a
    later branch's cost surface is expected to do its own normalization
    (e.g. against this property's own elevation range, or a fixed
    meters-per-cost-unit scale), and baking a normalization choice in
    here would make that caller's own choice invisible and hard to
    change independently.
"""

import math

import numpy as np

# Real-world radius (meters) of the disc neighborhood each cell's TPI is
# computed against. Sets the scale of landform this detects: roughly
# hillslope-width for a small farm parcel -- big enough to average out a
# single noisy DEM cell or a foot-wide rill, small enough to still react
# to a parcel-scale spur or draw rather than the whole property's overall
# relief. Too small a radius makes every minor micro-swale or hummock
# register as strongly positive/negative TPI (the neighborhood mean barely
# differs from the cell itself in any other case); too large a radius
# flattens exactly the spur-scale relief a road router cares about (ridge
# crests and draws average into the same broad regional mean and stop
# reading as distinct). CONFIGURABLE, deliberately UNVALIDATED starting
# value, same caveat as every other threshold in this pipeline: pending
# real tuning against a scored candidate road network on an actual
# property, not tuned against anything yet.
DEFAULT_TPI_RADIUS_METERS = 50.0

# A valid cell whose disc neighborhood contains fewer than this many valid
# cells gets NaN rather than a mean computed from too small a sample (a
# grid corner at a small radius, or ground pressed up against a large
# nodata gap). CONFIGURABLE, same unvalidated-starting-value caveat as
# above.
MIN_TPI_NEIGHBORHOOD_CELLS = 5


def _valid_mask(array: np.ndarray) -> np.ndarray:
    return ~np.isnan(array)


def _shift(array: np.ndarray, dr: int, dc: int, fill):
    """
    Returns a same-shape array where out[r, c] == array[r + dr, c + dc],
    treating anything outside array's own bounds as `fill` -- i.e. "shift
    the grid by (-dr, -dc)" with the vacated edge filled with `fill`,
    never wrapped. Local copy of the same shift building block
    raster_grid.py's binary_erode()/binary_dilate() use internally
    (raster_grid._shift is module-private there, boolean-only, and
    zero-fills; this version takes an explicit fill value so it works for
    both this module's float elevation sums and its boolean validity
    masks) -- same technique, not a shared dependency.
    """
    rows, cols = array.shape
    out = np.full_like(array, fill)

    r_src_start, r_src_end = max(0, dr), min(rows, rows + dr)
    c_src_start, c_src_end = max(0, dc), min(cols, cols + dc)
    r_dst_start = max(0, -dr)
    c_dst_start = max(0, -dc)
    r_dst_end = r_dst_start + (r_src_end - r_src_start)
    c_dst_end = c_dst_start + (c_src_end - c_src_start)

    out[r_dst_start:r_dst_end, c_dst_start:c_dst_end] = array[r_src_start:r_src_end, c_src_start:c_src_end]
    return out


def build_disc_kernel_offsets(
    resolution_meters: tuple[float, float], radius_meters: float
) -> list[tuple[int, int]]:
    """
    Every (dr, dc) cell offset whose ground-distance from the center cell
    -- hypot(dc * pixel_size_x, dr * pixel_size_y), using the DEM's own,
    not-necessarily-square resolution_meters -- falls within
    radius_meters. Includes (0, 0) (the center cell is always part of its
    own neighborhood mean). A non-square resolution_meters produces a
    genuinely elliptical set of offsets in cell terms, wider along the
    finer-resolution axis, not a circle.

    The search bounds (ceil(radius_meters / pixel_size)) are a safe outer
    bound, not a tight one -- correctness comes entirely from the hypot
    check on every candidate offset, not from the loop bounds themselves.
    """
    px, py = resolution_meters
    max_dr = math.ceil(radius_meters / py)
    max_dc = math.ceil(radius_meters / px)

    offsets = []
    for dr in range(-max_dr, max_dr + 1):
        for dc in range(-max_dc, max_dc + 1):
            if math.hypot(dc * px, dr * py) <= radius_meters:
                offsets.append((dr, dc))
    return offsets


def compute_tpi(dem: dict, radius_meters: float = DEFAULT_TPI_RADIUS_METERS) -> np.ndarray:
    """
    Continuous Topographic Position Index grid, same (rows, cols) shape as
    dem['array'] and the same row/col alignment as every other DEM-shaped
    array in this codebase. See module docstring for the method, its
    known limitations, and why this deliberately returns raw meters.

    Implementation: two accumulations over build_disc_kernel_offsets()'s
    offsets, each applied to the whole grid at once via _shift() rather
    than a per-cell Python loop -- a running sum of np.nan_to_num(array)
    (nodata treated as 0 contribution) and a running count of valid
    contributing cells, both masked by each shifted offset's own validity
    so a nodata neighbor contributes to neither. mean = sum / count;
    tpi = array - mean. Cells that are themselves NaN, or whose count of
    valid neighborhood cells falls below MIN_TPI_NEIGHBORHOOD_CELLS, are
    forced to NaN in the output rather than left as a mean/difference
    computed from an empty or too-small sample.
    """
    array = dem["array"]
    resolution_meters = dem["resolution_meters"]

    valid = _valid_mask(array)
    filled_zero = np.nan_to_num(array, nan=0.0).astype(np.float64)

    offsets = build_disc_kernel_offsets(resolution_meters, radius_meters)

    sum_elevation = np.zeros(array.shape, dtype=np.float64)
    count_valid = np.zeros(array.shape, dtype=np.int64)

    for dr, dc in offsets:
        # Both fills are 0/False: a neighbor offset that lands off the
        # grid edge contributes exactly like a neighbor offset that lands
        # on an in-grid nodata cell -- neither should add to the sum or
        # the count.
        shifted_values = _shift(filled_zero, dr, dc, 0.0)
        shifted_valid = _shift(valid, dr, dc, False)
        sum_elevation += np.where(shifted_valid, shifted_values, 0.0)
        count_valid += shifted_valid

    with np.errstate(invalid="ignore", divide="ignore"):
        mean_elevation = sum_elevation / count_valid

    tpi = array.astype(np.float64) - mean_elevation
    tpi[~valid] = np.nan
    tpi[count_valid < MIN_TPI_NEIGHBORHOOD_CELLS] = np.nan
    return tpi


if __name__ == "__main__":
    # Offline smoke test against valley_delineation.py's own synthetic
    # V-shaped valley DEM (see valley_delineation.py's __main__) -- see
    # test_topographic_position.py for the real (assertion-based) checks.
    size = 30
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        for col in range(size):
            distance_from_valley = abs((size - 1 - row) - col)
            array[row, col] = distance_from_valley * 2.0 + row * 0.5

    synthetic_dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    tpi = compute_tpi(synthetic_dem)
    print(f"TPI min={np.nanmin(tpi):.4f} max={np.nanmax(tpi):.4f} mean={np.nanmean(tpi):.4f} "
          f"nan_count={int(np.isnan(tpi).sum())}")
