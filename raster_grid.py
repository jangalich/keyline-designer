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

Not everything here takes a `dem`: the RING SMOOTHING section at the
bottom is pure shapely, no grid dict involved. It lives here because a
cell-union footprint's 5 m staircase is the only thing anyone in this
pipeline ever smooths, so the smoothers belong beside the code that
builds that staircase -- and because both a Layer 2 computation module
(exclusion_zones.py) and the Layer 3 renderer need them, which rules out
either one owning them.
"""

import math
from collections import deque

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

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
D4_OFFSETS = [(-1, 0), (0, -1), (0, 1), (1, 0)]


def build_disc_kernel_offsets(
    resolution_meters: tuple[float, float], radius_meters: float
) -> list[tuple[int, int]]:
    """
    Every (dr, dc) cell offset whose real-ground displacement from a
    center cell -- hypot(dc * px, dr * py), using a (px, py) resolution
    tuple that is NOT assumed square -- falls within radius_meters.
    Always includes (0, 0) (the center cell is always within
    radius_meters of itself). Whenever px != py, the result is a
    genuinely elliptical set of offsets in cell terms (wider along
    whichever axis has the finer resolution), not a circle.

    Shared raster/grid geometry, not tied to any one caller's use of it --
    the same offset list works as a disc-neighborhood kernel for a
    neighborhood-average computation, or as the structuring element for a
    disc-radius coverage/dilation test ("is this cell within
    radius_meters of any cell in a given mask"). Lives here alongside
    D8_OFFSETS for that reason: it's shared grid geometry, not specific
    to any one terrain-analysis module.

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


def _disc_offsets(radius_cells: int) -> list[tuple[int, int]]:
    """Every (dr, dc) whose Euclidean distance from the center is within
    radius_cells (dr*dr + dc*dc <= radius_cells*radius_cells), including
    (0, 0) -- the DISC structuring element, symmetric, for a single direct
    radius-r pass. Contrast the SQUARE element binary_erode()/binary_dilate()
    build by repeating a 3x3 (D8) ring r times: repeating a 3x3 ring is
    equivalent to a (2r+1)-square element ONLY, so it can never produce a
    disc -- a disc needs the radius-r offsets enumerated directly, as here."""
    r2 = radius_cells * radius_cells
    return [
        (dr, dc)
        for dr in range(-radius_cells, radius_cells + 1)
        for dc in range(-radius_cells, radius_cells + 1)
        if dr * dr + dc * dc <= r2
    ]


def binary_erode(mask: np.ndarray, radius_cells: int, element: str = "square") -> np.ndarray:
    """
    Binary erosion by radius_cells, treating everything outside `mask`'s own
    bounds as background. `element` selects the structuring element:

      "square" (the DEFAULT, unchanged): 8-connected (Chebyshev) erosion --
        radius_cells repeated single-ring (3x3, D8_OFFSETS) erosions, which
        is mathematically equivalent to one (2r+1)-square structuring
        element. The default is deliberately square: it matches
        connected_components()'s own D8 adjacency, so "eroded into 2+
        components" means pinched under the same connectivity rule the rest
        of the module uses. attempt_waist_split() (and, through it,
        water_candidate_zones.py) depends on that -- do NOT change the
        default.

      "disc": Euclidean erosion by a single radius-r disc pass (include a
        neighbour offset only when dr*dr + dc*dc <= r*r). Corners round
        instead of developing the flat 45-degree facets a square element
        leaves at larger radii. Used only by production_area.py's render
        opening.

    Either way this stays plain numpy (shift-and-AND over the element's
    offsets), no scipy dependency -- this module commits to numpy-only so
    every downstream terrain module can unit-test against a synthetic DEM.

    radius_cells <= 0 returns a copy of `mask` unchanged (no erosion).
    Raises ValueError for any element other than "square" or "disc".
    """
    if element not in ("square", "disc"):
        raise ValueError(f"binary_erode(): element must be 'square' or 'disc', got {element!r}")
    if radius_cells <= 0:
        return mask.copy()

    if element == "disc":
        eroded = mask.copy()
        for dr, dc in _disc_offsets(radius_cells):
            eroded &= _shift(mask, dr, dc)
        return eroded

    eroded = mask.copy()
    for _ in range(radius_cells):
        shrunk = eroded.copy()
        for dr, dc in D8_OFFSETS:
            shrunk &= _shift(eroded, dr, dc)
        eroded = shrunk
    return eroded


def binary_dilate(mask: np.ndarray, radius_cells: int, element: str = "square") -> np.ndarray:
    """
    Binary dilation by radius_cells -- the exact dual of binary_erode()
    (shift-and-OR instead of shift-and-AND). `element` selects the
    structuring element, mirroring binary_erode():

      "square" (the DEFAULT, unchanged): 8-connected (Chebyshev) dilation,
        radius_cells repeated 3x3 (D8_OFFSETS) rings. Every existing caller
        keeps this with no edit.

      "disc": Euclidean dilation by a single radius-r disc pass (offset
        included only when dr*dr + dc*dc <= r*r), so grown corners round
        rather than square off. Used only by production_area.py's render
        opening, paired with a disc erosion to form a disc opening.

    _shift() treats anything outside `mask`'s own bounds as background, so
    dilation never grows in from beyond the grid's edges -- consistent with
    binary_erode()'s edge convention. Stays plain numpy, no scipy.

    radius_cells <= 0 returns a copy of `mask` unchanged (no dilation).
    Raises ValueError for any element other than "square" or "disc".
    """
    if element not in ("square", "disc"):
        raise ValueError(f"binary_dilate(): element must be 'square' or 'disc', got {element!r}")
    if radius_cells <= 0:
        return mask.copy()

    if element == "disc":
        dilated = mask.copy()
        for dr, dc in _disc_offsets(radius_cells):
            dilated |= _shift(mask, dr, dc)
        return dilated

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


def cells_in_polygon(dem: dict, polygon) -> list[tuple[int, int]]:
    """
    THE INVERSE of cell_union_footprint(): every grid cell whose CENTER falls
    inside `polygon` (in dem['crs'] meters), as a sorted (row, col) list.

    PIXEL-CENTER CONTAINMENT IS THIS PIPELINE'S RASTERIZATION CONVENTION, not
    a choice made here. It is the test production_area.compute_step1_eligible_
    cells() applies for the parcel boundary, the hydric union and the road
    union; the test canopy_height_data.tree_root_zone_mask() applies; and the
    test exclusion_zones.py applies for its per-gate footprints -- all of them
    spelled the same way inline:

        prep(polygon).contains(Point(pixel_center_xy(dem, r, c)))

    ...which is why a cell either belongs to a zone whole or not at all, and
    why cell_union_footprint() can rebuild a footprint from cells exactly.
    Anything else (area-weighted coverage, corner containment, a rasterio
    burn) would disagree with STEP 1 about which cells a geometry covers, and
    a zone whose cells disagree with STEP 1's is a zone whose acreage,
    representative elevation and render opening are all computed over the
    wrong ground.

    THIS FUNCTION IS NEW; THE CONVENTION IS NOT. The inline copies above are
    left exactly as they are -- rewiring STEP 1 to call this would be a change
    to a KSOP module's own computation, which is not something a helper's
    introduction gets to do quietly. The agreement between this function and
    STEP 1 is therefore asserted rather than assumed:
    test_wire_translation_inbound.py round-trips a real generated patch through
    the translation boundary and asserts the rehydrated 'cells' are EQUAL to
    the ones cluster_and_gate() produced. If the convention here ever drifts from STEP 1's, that test is
    what says so.

    Returns [] for an empty polygon, and for a polygon so small or so placed
    that no cell center falls inside it -- a real outcome (a sub-cell sliver
    between four centers), not an error. The caller decides what that means.
    """
    if polygon is None or polygon.is_empty:
        return []

    rows, cols = dem["array"].shape

    # Bounding-box prefilter, so a small zone on a large grid does not pay for
    # a containment test per cell of the whole DEM. Row/col ranges are derived
    # from the same origin +/- N * resolution expression cell_union_footprint()
    # uses, then widened by one cell on each side and clamped -- the widening
    # is deliberate slack so a cell center exactly on the box edge is tested
    # rather than filtered out by float comparison before prep() ever sees it.
    px, py = dem["resolution_meters"]
    minx, miny, maxx, maxy = polygon.bounds
    col_lo = max(0, int(math.floor((minx - dem["origin_x"]) / px)) - 1)
    col_hi = min(cols - 1, int(math.ceil((maxx - dem["origin_x"]) / px)) + 1)
    row_lo = max(0, int(math.floor((dem["origin_y"] - maxy) / py)) - 1)
    row_hi = min(rows - 1, int(math.ceil((dem["origin_y"] - miny) / py)) + 1)
    if col_lo > col_hi or row_lo > row_hi:
        return []

    prepared = prep(polygon)
    cells = []
    for r in range(row_lo, row_hi + 1):
        for c in range(col_lo, col_hi + 1):
            if prepared.contains(Point(pixel_center_xy(dem, r, c))):
                cells.append((r, c))
    return cells


def waist_erosion_radius_cells(dem: dict, min_waist_meters: float) -> int:
    """
    Converts a real-world minimum waist width into a cell-count erosion
    radius using the DEM's own resolution_meters -- shared by every
    cluster-splitting caller (production_area.py's production-zone waist
    detection, water_candidate_zones.py's post-dilation water-zone waist
    detection: same "a single connected cluster can pinch down to
    something too narrow to sensibly treat as one zone" logic, just
    applied to a different per-cell eligibility mask in each caller).
    Eroding a mask by radius r cells strips away anything narrower than
    roughly (2r) cells wide, so the radius is half the minimum waist
    width, rounded UP (via ceil) so a real waist genuinely narrower than
    min_waist_meters is reliably eroded away rather than surviving due to
    a too-small radius. Always at least 1 cell, so a nonzero
    min_waist_meters always does *something*.
    """
    px, py = dem["resolution_meters"]
    cell_size = (px + py) / 2.0
    return max(1, math.ceil(min_waist_meters / cell_size / 2.0))


def closing_radius_cells(dem: dict, radius_meters: float) -> int:
    """
    Metres -> cell radius for a disc closing, using the DEM's own
    resolution_meters. cell_size is the mean of the DEM's two pixel
    dimensions (they are usually equal but computed independently
    upstream, see dem_data.get_dem_for_boundary()).

    Deliberately NOT waist_erosion_radius_cells() directly above: that
    one converts a minimum waist WIDTH and halves it, and rounds UP so a
    real waist is reliably eroded away. A closing radius is ALREADY a
    radius, so this is a plain round() with no halving. round() is used,
    not ceil(), so a positive radius smaller than half a cell honestly
    reports "0 cells, no-op" (surfaced by effective_radius_meters()
    below) instead of being inflated to a full cell -- e.g. 2 m at the
    pipeline's 5 m DEM resolution is 0 cells, not 1.

    SHARED, one definition: diagnose_exclusion_footprints.py measured the
    per-gate exclusion footprints with this conversion and
    exclusion_zones.py applies it in production, so the module that
    MEASURED the radii and the module that APPLIES them cannot drift
    apart. Same "shared helper, one definition" reasoning
    cell_union_footprint()/waist_erosion_radius_cells() above already
    established.
    """
    px, py = dem["resolution_meters"]
    cell_size = (px + py) / 2.0
    return max(0, int(round(radius_meters / cell_size)))


def effective_radius_meters(dem: dict, radius_cells: int) -> float:
    """The ground radius an integer cell radius really corresponds to --
    the honest read-back of closing_radius_cells()' quantization, so a
    caller can report what was ACTUALLY applied rather than what was
    asked for."""
    px, py = dem["resolution_meters"]
    return radius_cells * (px + py) / 2.0


def disc_closing(mask: np.ndarray, radius_cells: int) -> np.ndarray:
    """
    Morphological closing (dilate THEN erode) with the disc structuring
    element, at radius_cells.

    DISC, not square: a square element produces 45-degree facets at
    radius 2 and above, and on an exclusion layer those facets are edges
    a user eventually draws production ground against. Same element=
    "disc" choice production_area.py's own render opening makes.

    The mask is PADDED by radius_cells + 1 cells of background before the
    dilation and cropped back afterwards. _shift() treats everything
    beyond the array bounds as background, so without the pad the erosion
    half would chew into any region touching the grid edge and the
    operation would NOT be extensive. With the pad, closed >= mask holds
    for every input (asserted in both callers' fixtures).

    EXTENSIVE, unlike every other morphology in this module: a closing
    only ever ADDS cells. binary_erode()/eroded_cell_mask()/
    attempt_waist_split() are all anti-extensive (they remove). A caller
    that reasons about a closed mask the way it reasons about an opened
    one has the inequality backwards -- see exclusion_zones.py's own
    render_fill_polygon_utm notes.

    radius_cells <= 0 returns a copy unchanged -- the raw footprint, no
    closing at all, which is a real configuration (a gate whose measured
    closing radius is 0.0 m), not a degenerate one.
    """
    if radius_cells <= 0:
        return mask.copy()

    pad = radius_cells + 1
    padded = np.pad(mask, pad, mode="constant", constant_values=False)
    closed = binary_erode(
        binary_dilate(padded, radius_cells, element="disc"), radius_cells, element="disc"
    )
    return closed[pad:-pad, pad:-pad]


def reclaim_stripped_cells(
    cluster_cells: set[tuple[int, int]],
    seed_labels: dict[tuple[int, int], int],
) -> dict[tuple[int, int], int]:
    """
    Recovers the cells erosion stripped away -- erosion only exists to
    DECIDE whether a cluster splits, never to permanently remove real,
    eligible ground. Multi-source 8-connected BFS, confined to
    `cluster_cells` (the ORIGINAL, pre-erosion cluster footprint): every
    stripped cell (a cell in cluster_cells not already in seed_labels) is
    assigned to whichever eroded sub-component's frontier reaches it
    first, expanding one ring at a time from every surviving sub-component
    simultaneously. BFS ring distance under 8-connected (D8_OFFSETS)
    adjacency is exactly Chebyshev pixel distance -- the same adjacency
    connected_components() already uses -- so this is a simple per-cell
    nearest-surviving-component assignment by pixel distance, staying
    entirely in cell-space: not a hull, not a buffer.
    """
    assignment = dict(seed_labels)
    queue = deque(seed_labels.items())
    while queue:
        (r, c), label = queue.popleft()
        for dr, dc in D8_OFFSETS:
            neighbor = (r + dr, c + dc)
            if neighbor in cluster_cells and neighbor not in assignment:
                assignment[neighbor] = label
                queue.append((neighbor, label))
    return assignment


def eroded_cell_mask(
    cells: list[tuple[int, int]],
    grid_shape: tuple[int, int],
    dem: dict,
    radius_meters: float,
    element: str = "square",
    extra_erode_cells: int = 0,
) -> np.ndarray:
    """
    The boolean survivor mask left after eroding the cell set `cells` by
    radius_meters: build the cluster's own cell mask on `grid_shape`, convert
    radius_meters to a cell radius via waist_erosion_radius_cells(), and
    binary_erode() by it. The True cells of the returned mask are exactly the
    PRE-reclaim erosion survivors -- np.argwhere() them to recover the survivor
    cell list. May be all-False if the cluster is thinner than the erosion
    radius throughout.

    `radius_meters` (renamed from min_waist_meters -- one caller now passes a
    RENDER_OPENING_RADIUS_METERS, another a MIN_ZONE_WAIST_METERS, so the old
    "waist" name no longer describes both) is the base radius. `element`
    ("square" default, or "disc") is passed straight through to binary_erode().
    `extra_erode_cells` is ADDED to the computed cell radius before eroding --
    folding it in here is what makes it COMPOSE into a single erosion of
    (radius + extra) rather than a separate second pass (consecutive erosions
    compose; erode(a) then erode(b) == erode(a+b) for the same element). This
    is the "lead erode" the render opening uses to sever more aggressively.

    Defaults ("square", 0) reproduce the original behavior exactly, so
    attempt_waist_split()'s own call is unchanged.

    Originally extracted verbatim from attempt_waist_split()'s own body so a
    caller that wants the SAME erosion for render-only geometry
    (production_area.cluster_and_gate()'s render opening) reuses this identical
    computation rather than a second, independently-maintained copy of it.
    """
    rows, cols = grid_shape
    cell_mask = np.zeros((rows, cols), dtype=bool)
    for r, c in cells:
        cell_mask[r, c] = True

    radius_cells = waist_erosion_radius_cells(dem, radius_meters) + extra_erode_cells
    return binary_erode(cell_mask, radius_cells, element=element)


def attempt_waist_split(
    cells: list[tuple[int, int]],
    grid_shape: tuple[int, int],
    dem: dict,
    min_area_acres: float,
    min_waist_meters: float,
) -> list[dict]:
    """
    Waist detection and splitting for ONE cluster's own cell mask -- a
    raster morphological operation on the cell mask itself (via
    binary_erode()), NOT a continuous-geometry operation on any polygon.
    Shared by production_area.py's production-zone clustering
    (originally built there; extracted here so water_candidate_zones.py's
    water-zone clustering can reuse the exact same detection/splitting
    logic against its own post-dilation eligibility mask, rather than a
    second, independently-maintained copy).

    Erodes the cluster by min_waist_meters (converted to a cell radius via
    waist_erosion_radius_cells()) and re-labels the result. If erosion
    doesn't produce 2+ components, there's no real waist here -- returns
    [{"cells": cells, "render_cells": cells}] completely unchanged (this
    function is idempotent and side-effect-free for clusters with no real
    waist, e.g. a normal, roughly-convex field/drainage band).

    If erosion DOES produce 2+ components, reclaims every stripped cell
    back onto its nearest surviving sub-component (reclaim_stripped_cells())
    and checks each reclaimed sub-cluster's own REAL cell-union footprint
    (cell_union_footprint(), not a cell count) against min_area_acres. The
    split is committed -- returning one dict per sub-cluster, each with
    "cells" (the full, POST-reclaim cell set -- every stripped cell
    reassigned back to its nearest surviving piece, used for everything a
    caller reports: area_acres, polygon_utm, geometry_wgs84, scoring) and
    "render_cells" (the narrower PRE-reclaim cell set -- exactly the cells
    that survived erosion and landed on this sub-component, before any
    stripped cell was reassigned anywhere; a render-only pre-reclaim cell
    partition kept so a dilation/reclaim step doesn't silently erase the
    real visual gap between two newly-split pieces) -- only if EVERY
    sub-cluster clears min_area_acres on its own POST-reclaim footprint;
    otherwise this returns [{"cells": cells, "render_cells": cells}]
    unchanged (a technically-2+-component erosion result that can't
    actually support 2+ real zones isn't a split).

    Enforces a cell-count invariant right after reclaim_stripped_cells()
    runs: every one of the `cells` passed in must come back out across the
    sub-groups (nothing is ever supposed to vanish during a split -- only
    find_candidate_zones()'s/cluster_and_gate()'s own SEPARATE, later
    min_area_acres/boundary-clipping rejection of an entire small
    sub-cluster is allowed to shrink the real final zone/patch count).
    Raises RuntimeError, loudly, with the exact stripped/reclaimed/
    unreachable cell counts, if this doesn't hold -- see the check's own
    inline comment for why it should be mathematically impossible given a
    correctly-constructed single connected component, and therefore always
    points at an upstream caller bug rather than something to silently
    paper over.
    """
    if len(cells) <= 1:
        return [{"cells": cells, "render_cells": cells}]

    rows, cols = grid_shape
    eroded_mask = eroded_cell_mask(cells, grid_shape, dem, min_waist_meters)

    eroded_labels, num_eroded = connected_components(eroded_mask)
    if num_eroded < 2:
        return [{"cells": cells, "render_cells": cells}]

    cluster_cells = set(cells)
    seed_labels = {
        (int(r), int(c)): int(eroded_labels[r, c]) for r, c in np.argwhere(eroded_mask)
    }
    assignment = reclaim_stripped_cells(cluster_cells, seed_labels)

    # Cell-count invariant: reclaim_stripped_cells() is a multi-source BFS
    # confined to cluster_cells, seeded from every surviving eroded cell --
    # since cluster_cells is (by every caller's own construction, via
    # connected_components()'s own labeling) always ONE single 8-connected
    # component under this exact D8_OFFSETS adjacency, and seed_labels is a
    # nonempty subset of it (num_eroded >= 2 guarantees eroded_mask has at
    # least 2 True cells), every cell in cluster_cells is reachable from
    # SOME seed and must end up in `assignment`. A mismatch here means
    # cluster_cells was NOT actually one connected component -- e.g. a
    # stripped cell sits in a pocket whose own eroded survivors all
    # vanished (fully eroded away) while a DIFFERENT, spatially
    # disconnected pocket kept its own -- a caller bug upstream (the input
    # `cells` list wasn't really one connected blob), not something to
    # silently paper over by dropping ground that was real, eligible
    # cluster membership going in.
    if len(assignment) != len(cluster_cells):
        unreachable_cells = cluster_cells - set(assignment)
        num_stripped = len(cluster_cells) - len(seed_labels)
        num_reclaimed = len(assignment) - len(seed_labels)
        raise RuntimeError(
            f"attempt_waist_split(): cell-count invariant violated -- {len(cluster_cells)} cell(s) went "
            f"in, only {len(assignment)} came back out of reclaim_stripped_cells(). "
            f"{len(seed_labels)} cell(s) survived erosion (never stripped); of the {num_stripped} "
            f"stripped cell(s), {num_reclaimed} were successfully reclaimed onto a surviving "
            f"sub-component, but {len(unreachable_cells)} had NO reachable surviving sub-component at "
            f"all (example cell(s): {sorted(unreachable_cells)[:5]}). This means the `cells` argument "
            "was not actually one single 8-connected component -- check the caller's own clustering "
            "(e.g. connected_components()'s labeling) rather than treating this as expected loss."
        )

    sub_groups: dict[int, list[tuple[int, int]]] = {}
    for cell, label in assignment.items():
        sub_groups.setdefault(label, []).append(cell)

    if len(sub_groups) < 2:
        return [{"cells": cells, "render_cells": cells}]

    for group_cells in sub_groups.values():
        group_mask = np.zeros((rows, cols), dtype=bool)
        for r, c in group_cells:
            group_mask[r, c] = True
        footprint = cell_union_footprint(dem, group_mask)
        area_acres = footprint.area / SQUARE_METERS_PER_ACRE
        if area_acres < min_area_acres:
            return [{"cells": cells, "render_cells": cells}]

    pre_reclaim_groups: dict[int, list[tuple[int, int]]] = {}
    for cell, label in seed_labels.items():
        pre_reclaim_groups.setdefault(label, []).append(cell)

    return [
        {"cells": group_cells, "render_cells": pre_reclaim_groups[label]}
        for label, group_cells in sub_groups.items()
    ]


def connected_components(mask: np.ndarray, connectivity: int = 8) -> tuple[np.ndarray, int]:
    """
    Connected-component labeling of a 2D boolean grid, via iterative BFS.
    Returns (labels, num_components): labels is a same-shape int array of
    component indices (-1 where mask is False).

    `connectivity` selects the neighbor adjacency: 8 (the default --
    diagonal neighbors count, D8_OFFSETS) or 4 (edge-only neighbors,
    D4_OFFSETS). The default is 8 and is DELIBERATELY unchanged: every
    existing caller — valley_delineation.py, water_candidate_zones.py,
    tree_zone_candidates.py, this module's own attempt_waist_split(), and
    two of production_area.py's three call sites (its STEP 1 source-region
    labeling and its hole-component labeling) — relies on 8-connectivity.
    The only caller that passes connectivity=4 is
    production_area.cluster_and_gate()'s own cluster labeling, so a
    cluster's cells are edge-connected and its real ground footprint is a
    single Polygon rather than a corner-touch MultiPolygon (see that
    function's docstring for the full rationale).

    Shared by valley_delineation.py (grouping thresholded flow-
    accumulation cells into drainage networks) and production_area.py
    (grouping low-slope cells into candidate production patches) — the
    same generic grouping operation, just applied to a different boolean
    mask in each case.
    """
    if connectivity == 8:
        offsets = D8_OFFSETS
    elif connectivity == 4:
        offsets = D4_OFFSETS
    else:
        raise ValueError(
            f"connected_components(): connectivity must be 4 or 8, got {connectivity!r}"
        )

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
                    for dr, dc in offsets:
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


# ===========================================================================
# RING SMOOTHING -- angular simplify + Chaikin corner-cutting
#
# These four ring-level helpers and the polygon-level wrapper below lived in
# render_layout_map.py until this branch, where they were private to the
# renderer. They are not renderer-specific: they are the same kind of
# dependency-free geometry building block as binary_erode()/binary_dilate()/
# cell_union_footprint()/disc_closing() above, and exclusion_zones.py (a
# Layer 2 computation module) now needs them too. A Layer 2 module importing
# from Layer 3's renderer would be a wrong-direction dependency, so they live
# here instead and render_layout_map.py imports them from this module. The
# move is behaviour-neutral: the bodies are unchanged, only the leading
# underscore (module-private) is dropped now that they are a shared API.
#
# WHY BOTH OPERATIONS, IN THIS ORDER. Every mask this pipeline turns into a
# polygon (cell_union_footprint()) arrives as a literal per-cell right-angle
# staircase. simplify() collapses that staircase's collinear runs down to the
# shape's real turns; the Chaikin pass then has actual corners to round rather
# than hundreds of individual cell steps. Reversing the order would round each
# 5 m step individually and leave the staircase intact, just fuzzier.
#
# DIRECTION OF ERROR. Chaikin's corner cut pushes the ring OUTWARD at reflex
# vertices and INWARD at convex ones. Which of those is the dangerous
# direction depends entirely on which side of the ring the caller's interior
# sits, so no clipping is applied here -- each caller clips (or asserts) in
# whichever direction is safe for its own geometry. See render_layout_map.py's
# production-fill call site (clips back to polygon_utm) and exclusion_zones.py's
# own EXCLUSION SMOOTHING docstring section (over-exclusion is the safe
# direction there, so it does not clip back).
# ===========================================================================


def chaikin_smooth_coords(coords: list[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    """
    Chaikin's corner-cutting subdivision, run `iterations` times, over an
    OPEN polyline (a road corridor, not a closed ring) -- simple enough
    to not need a new spline/smoothing dependency for what's purely a
    cosmetic rendering touch-up (see render_layout_map._smooth_line_for_
    render(), its only caller).

    The first and last coordinates are always kept EXACTLY as given, so a
    smoothed branch still starts/ends at the same real endpoint -- the
    anchor for a trunk, or the join-on-the-parent-branch cell for a spur
    (build_road_network()'s own "cells" ordering puts a spur's join point
    first) -- only the interior gets rounded. This is exactly why
    _smooth_line_for_render() must be called once PER BRANCH rather than
    on some pre-merged whole-network line: smoothing each branch
    independently is what keeps a spur's own join point exact, so the
    rendered network still reads as connected. Each iteration replaces
    every edge
    (Pi, Pi+1) with two points at 1/4 and 3/4 along it (the standard
    Chaikin construction), which is what visually rounds a sharp corner:
    each cut moves the curve a little further off the original corner and
    a little closer to a smooth arc through it.

    A no-op below 3 points, or once an iteration's own input drops below
    3 points -- there's no interior corner left to cut on a 2-point
    (straight) line.
    """
    for _ in range(iterations):
        if len(coords) < 3:
            break
        smoothed = [coords[0]]
        for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
            smoothed.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            smoothed.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        smoothed.append(coords[-1])
        coords = smoothed
    return coords


def angular_simplify_closed_ring(ring: LineString, tolerance: float) -> LineString:
    """
    shapely .simplify(tolerance, preserve_topology=True) on a CLOSED ring
    only -- no Chaikin call, zero smoothing iterations. Callers that want
    corner-rounding on top chain through angular_then_smooth_closed_ring()
    below instead; callers that want a purely angular result (the water-
    zone-exclusion and tree-zone-exclusion fence loops, and the boundary
    fence) use this one directly.

    simplify() can drop the duplicate closing coordinate (coords[0] ==
    coords[-1]) that every closed ring arrives with, so the closure is
    re-applied here if it did.
    """
    simplified = ring.simplify(tolerance, preserve_topology=True)
    coords = list(simplified.coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return LineString(coords)


def chaikin_smooth_closed_ring(coords: list[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    """
    Same Chaikin corner-cutting construction as chaikin_smooth_coords()
    above, but CYCLIC -- for a closed ring (a fence loop, a polygon's
    exterior or interior ring), not an open route. A closed ring has no
    meaningful start/end point to pin the way the open-line version pins
    its first/last coordinate, so pinning one here would leave a real,
    visible unsmoothed sharp seam at whatever index the coordinate array
    happens to start at -- a correctness issue, not just cosmetic, since
    that seam's location is an arbitrary artifact of how the ring's
    coordinates happen to be ordered, not a real corner of the geometry.

    Concretely: the duplicated closing coordinate (coords[0] == coords[-1],
    every ring passed here is already closed) is dropped before
    smoothing, every edge is cut INCLUDING the wraparound edge from the
    last point back to the first (`zip(coords, coords[1:] + coords[:1])`,
    not `zip(coords, coords[1:])`), and the result is re-closed by
    appending its own first point back onto the end.

    A no-op below 3 points -- there's no real ring geometry to smooth
    below a triangle.
    """
    if coords[0] == coords[-1]:
        coords = coords[:-1]

    for _ in range(iterations):
        if len(coords) < 3:
            break
        smoothed = []
        for (x0, y0), (x1, y1) in zip(coords, coords[1:] + coords[:1]):
            smoothed.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            smoothed.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        coords = smoothed

    return coords + [coords[0]]


def angular_then_smooth_closed_ring(ring: LineString, simplify_tolerance: float, chaikin_iterations: int) -> LineString:
    """
    The full two-step treatment for ONE closed ring: run it through
    angular_simplify_closed_ring() first, then feed the result through
    chaikin_smooth_closed_ring() (cyclic, closed-ring-aware -- see that
    function's own docstring) for chaikin_iterations passes. Plumbing that
    chains the two helpers above, not smoothing math of its own.

    Meant to soften the angular corners simplify() leaves behind, not to
    re-curve the line into something that no longer tracks the real shape
    -- which is why every caller's iteration count starts at 1.
    """
    angular_ring = angular_simplify_closed_ring(ring, simplify_tolerance)
    smoothed_coords = chaikin_smooth_closed_ring(list(angular_ring.coords), chaikin_iterations)
    return LineString(smoothed_coords)


def angular_smooth_polygon(geometry, simplify_tolerance: float, chaikin_iterations: int):
    """
    angular_then_smooth_closed_ring() lifted from a single ring to a whole
    POLYGON: smooths the exterior ring AND every interior ring of every part
    and reassembles into a Polygon/MultiPolygon. The ring helpers above all
    take one closed ring (a fence loop); every real caller here hands in a
    cell-union footprint, which is routinely a MultiPolygon and routinely
    carries interior rings, so the polygon-level wrapper is needed either way.

    Non-polygonal input (a GeometryCollection, a LineString) and EMPTY input are
    both returned UNCHANGED rather than raising -- same degradation contract as
    the invalid case below. The empty check is explicit and comes first: an
    empty Polygon still reports geom_type 'Polygon' but has a zero-length
    exterior coordinate sequence, which indexes out of range rather than raising
    anything the except clause below would catch.

    DEGRADES, NEVER RAISES: if smoothing yields empty or invalid geometry (a
    Chaikin pass self-intersecting a very thin sliver, a ring left with too few
    coordinates after simplify) this returns the INPUT geometry unchanged. Both
    callers use the result as display-and-clamp geometry, where a bad smooth
    must fall back to the exact unsmoothed shape rather than fail a render or a
    pipeline run. A minor ring self-touch from corner-cutting is healed with
    buffer(0) first; only a result still empty/invalid after that falls back.
    """
    def _smooth_ring_coords(ring):
        return list(
            angular_then_smooth_closed_ring(
                LineString(ring.coords), simplify_tolerance, chaikin_iterations
            ).coords
        )

    def _smooth_polygon(poly):
        exterior = _smooth_ring_coords(poly.exterior)
        interiors = [_smooth_ring_coords(interior) for interior in poly.interiors]
        return Polygon(exterior, interiors)

    if geometry.is_empty:
        return geometry

    try:
        if geometry.geom_type == "MultiPolygon":
            smoothed = MultiPolygon([_smooth_polygon(part) for part in geometry.geoms])
        elif geometry.geom_type == "Polygon":
            smoothed = _smooth_polygon(geometry)
        else:
            return geometry
        if not smoothed.is_valid:
            smoothed = smoothed.buffer(0)  # heal minor ring self-touch from corner-cutting
        if smoothed.is_empty or not smoothed.is_valid:
            return geometry
        return smoothed
    except (IndexError, ValueError, TypeError):
        # A degenerate ring (too few coordinates after simplify, an empty ring
        # inside an otherwise non-empty MultiPolygon) can make Polygon()/
        # MultiPolygon() raise or index out of range -- degrade to the
        # unsmoothed input either way, per this function's own contract.
        return geometry
