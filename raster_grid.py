"""
raster_grid.py

Tiny, dependency-free helpers for working with the plain DEM grid dict
dem_data.get_dem_for_boundary() returns:

    {
        'array': np.ndarray (rows, cols), meters, np.nan = nodata,
        'resolution_meters': (pixel_size_x, pixel_size_y),
        'origin_x': x of the upper-left pixel's upper-left corner,
        'origin_y': y of the same corner,
        'crs': 'EPSG:<utm zone>',
    }

The only real dependencies here are numpy and shapely (both already
required project-wide, no network involved) — this deliberately has no
rasterio or requests import, so every terrain-analysis module downstream
of the DEM fetch (valley_delineation.py, production_area.py,
water_candidate_zones.py) can depend on this and stay unit-testable
against a synthetic DEM dict without hitting the network. dem_data.py is
the only module in this pipeline that talks to rasterio/the network
directly.
"""

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

SQUARE_METERS_PER_ACRE = 4046.8564224


def pixel_center_xy(dem: dict, row: int, col: int) -> tuple[float, float]:
    """Real-world (x, y) in dem['crs'] meters for the center of grid cell (row, col)."""
    px, py = dem["resolution_meters"]
    x = dem["origin_x"] + (col + 0.5) * px
    y = dem["origin_y"] - (row + 0.5) * py
    return x, y


def cell_area_acres(dem: dict) -> float:
    """Ground area one grid cell covers, in acres."""
    px, py = dem["resolution_meters"]
    return (px * py) / SQUARE_METERS_PER_ACRE


D8_OFFSETS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _shift(arr: np.ndarray, dr: int, dc: int) -> np.ndarray:
    """Returns a same-shape array where out[r, c] == arr[r + dr, c + dc],
    treating anything outside arr's own bounds as False -- i.e. "shift the
    grid by (-dr, -dc)" with the vacated edge filled with background,
    never wrapped. Shared building block for binary_erode()'s per-
    neighbor AND."""
    rows, cols = arr.shape
    out = np.zeros_like(arr)

    r_src_start, r_src_end = max(0, dr), min(rows, rows + dr)
    c_src_start, c_src_end = max(0, dc), min(cols, cols + dc)
    r_dst_start = max(0, -dr)
    c_dst_start = max(0, -dc)
    r_dst_end = r_dst_start + (r_src_end - r_src_start)
    c_dst_end = c_dst_start + (c_src_end - c_src_start)

    out[r_dst_start:r_dst_end, c_dst_start:c_dst_end] = arr[r_src_start:r_src_end, c_src_start:c_src_end]
    return out


def binary_erode(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """
    8-connected (Chebyshev/square) binary erosion by radius_cells, treating
    everything outside `mask`'s own bounds as background -- same D8
    adjacency connected_components() already uses, so a shape that eroded
    down to 2+ separate components is genuinely pinched by this grid's own
    connectivity rule, not by an inconsistent one.

    Implemented as radius_cells repeated single-ring (3x3) erosions rather
    than one direct radius-N structuring element -- applying the 3x3
    erosion r times is mathematically equivalent to eroding once with a
    single (2r+1)-square structuring element, and this keeps the whole
    operation plain numpy (shift-and-AND over the 8 neighbor offsets), no
    scipy dependency. Deliberately implemented here rather than adding
    scipy: this module's own docstring commits to staying dependency-free
    (numpy only) so every terrain-analysis module downstream can unit-test
    against a synthetic DEM without a heavier, unvetted new requirement for
    what is otherwise a single, simple, self-contained operation.

    radius_cells <= 0 returns a copy of `mask` unchanged (no erosion).
    """
    if radius_cells <= 0:
        return mask.copy()

    eroded = mask.copy()
    for _ in range(radius_cells):
        shrunk = eroded.copy()
        for dr, dc in D8_OFFSETS:
            shrunk &= _shift(eroded, dr, dc)
        eroded = shrunk
    return eroded


def binary_dilate(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """
    8-connected (Chebyshev/square) binary dilation by radius_cells -- the
    exact dual of binary_erode() above (shift-and-OR over the 8 neighbor
    offsets instead of shift-and-AND), grown outward radius_cells times
    rather than dilated once with a single (2r+1)-square structuring
    element, for the same reason binary_erode() repeats a 3x3 ring instead
    of building one large kernel: it stays plain numpy, no scipy
    dependency. _shift() treats anything outside `mask`'s own bounds as
    background (False), so dilation never grows in from beyond the grid's
    own edges -- consistent with binary_erode()'s own edge convention.

    radius_cells <= 0 returns a copy of `mask` unchanged (no dilation).
    """
    if radius_cells <= 0:
        return mask.copy()

    dilated = mask.copy()
    for _ in range(radius_cells):
        grown = dilated.copy()
        for dr, dc in D8_OFFSETS:
            grown |= _shift(dilated, dr, dc)
        dilated = grown
    return dilated


def cell_union_footprint(dem: dict, cell_mask: np.ndarray):
    """
    The REAL footprint of every True cell in `cell_mask`: the union of
    each cell's own ground square at the DEM's resolution — NOT a convex
    hull of cell CENTER points, and not a smoothed continuous-geometry
    buffer. A hull of centers fills in concave gaps between actual cells
    with ground that was never really eligible; a buffer rounds away real
    corners. This is the accurate footprint every spatial consumer that
    clusters DEM cells (production_area.py, water_candidate_zones.py,
    ...) should build from a boolean cell mask.

    GRID-SEAM FIX: each square's corners are computed directly from its
    own row/col boundary via `origin +/- N * resolution` — NOT via
    pixel_center_xy() (a cell's CENTER) offset by +/- half a cell width.
    The center-then-half-width approach computes each shared edge via two
    DIFFERENT floating-point expressions depending on which neighboring
    cell is doing the computing (e.g. cell c's right edge as
    `(origin + (c+0.5)*px) + px/2` vs cell c+1's left edge as
    `(origin + (c+1.5)*px) - px/2`) — mathematically identical, but not
    bit-for-bit identical once origin_x/origin_y are realistic
    large-magnitude UTM values (confirmed live: this left visible
    razor-thin sliver gaps in rendered output, unary_union() failing to
    fully dissolve adjacent squares' shared edges). Computing both cell
    c's right edge and cell c+1's left edge from the exact same
    expression (`origin + (c+1) * resolution`) makes every shared edge
    bit-for-bit identical by construction, regardless of origin's
    magnitude. buffer(0) afterward is cheap, defensive cleanup against any
    remaining near-zero-area topology noise from unary_union'ing many
    touching squares — the corner-snapping above should already make it a
    no-op in practice.

    Returns an empty Polygon if `cell_mask` has no True cells at all.
    """
    px, py = dem["resolution_meters"]
    origin_x = dem["origin_x"]
    origin_y = dem["origin_y"]

    squares = []
    for r, c in np.argwhere(cell_mask):
        r, c = int(r), int(c)
        x0 = origin_x + c * px
        x1 = origin_x + (c + 1) * px
        y1 = origin_y - r * py
        y0 = origin_y - (r + 1) * py
        squares.append(box(x0, y0, x1, y1))

    if not squares:
        return Polygon()

    return unary_union(squares).buffer(0)


def connected_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """
    8-connected component labeling of a 2D boolean grid, via iterative
    BFS. Returns (labels, num_components): labels is a same-shape int
    array of component indices (-1 where mask is False).

    Shared by valley_delineation.py (grouping thresholded flow-
    accumulation cells into drainage networks) and production_area.py
    (grouping low-slope cells into candidate production patches) — the
    same generic grouping operation, just applied to a different boolean
    mask in each case.
    """
    rows, cols = mask.shape
    labels = np.full((rows, cols), -1, dtype=np.int32)
    next_label = 0

    for r in range(rows):
        for c in range(cols):
            if mask[r, c] and labels[r, c] == -1:
                labels[r, c] = next_label
                stack = [(r, c)]
                while stack:
                    cr, cc = stack.pop()
                    for dr, dc in D8_OFFSETS:
                        nr, nc = cr + dr, cc + dc
                        if (
                            0 <= nr < rows
                            and 0 <= nc < cols
                            and mask[nr, nc]
                            and labels[nr, nc] == -1
                        ):
                            labels[nr, nc] = next_label
                            stack.append((nr, nc))
                next_label += 1

    return labels, next_label
