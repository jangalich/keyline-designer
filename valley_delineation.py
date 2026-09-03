"""
valley_delineation.py

Delineates primary valleys (drainage/flow paths) from a DEM grid — the
terrain-analysis step this pipeline's README long flagged as a future
piece ("real LiDAR-based keypoint detection... running real terrain-
analysis tools to mathematically locate keypoints and candidate keylines").

Pipeline, standard D8 hydrology terrain analysis (Barnes-style priority-
flood fill, EPSILON variant, then D8 flow direction, then flow
accumulation):

    DEM grid --> fill depressions --> D8 flow direction --> flow
    accumulation --> threshold to valley cells --> group into connected
    drainage networks --> trace head-to-outlet branches --> line geometry

This module takes a plain DEM dict (see raster_grid.py's docstring for the
shape) and does pure numpy computation — no network calls, no rasterio
dependency beyond the one WGS84-reprojection step needed to produce
lon/lat output geometry. That split is deliberate: it's what makes this
logic unit-testable against a small synthetic DEM (see
test_valley_delineation.py) independently of whether the real DEM fetch
(dem_data.py) is working — the two failure modes ("is the terrain data
right" vs "is the valley-finding logic right") are easy to conflate
otherwise, per this feature's actual debugging history.

Known limitations, stated plainly rather than glossed over:
  - D8 (steepest single-neighbor descent) is the simplest standard flow
    model. It can't represent flow splitting/braiding: a cell gets one
    exit path even where real water would spread. Flat TIES are no longer
    part of that limitation — the fill is the epsilon variant
    (FILL_EPSILON_METERS), so a filled pit or plateau slopes gently
    toward its own outlet and every filled cell routes. The -1
    "no downhill neighbour" sentinel survives, and correctly, at cells
    the flood never reaches: the grid's own border seeds, and valid cells
    walled off from the border by nodata.
  - Depression filling assumes the fetched DEM's outer edge (the buffer
    around the property from dem_data.py) is a legitimate place for water
    to exit the grid. That's a reasonable assumption for a property-scale
    extract, not a full watershed delineation.
  - Resolution is whatever dem_data.py fetched (5m by default) — real
    micro-topography (a foot-wide rill) below that resolution won't show
    up. This is a first-pass, coarse valley network, not a survey.
"""

import heapq
import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform

from raster_grid import D8_OFFSETS, cell_area_acres, connected_components, pixel_center_xy

# Minimum upstream contributing area for a cell to count as "valley" at
# all (i.e. concentrated flow, not just diffuse sheet flow off a slope).
# CONFIGURABLE — tune against your own property: if the delineated network
# misses a drainage line you know is real on the ground, lower this; if it
# flags every minor swale as a "valley," raise it.
MIN_STREAM_CONTRIBUTING_AREA_ACRES = 0.5

# Minimum contributing area anywhere along a drainage network for it to
# count as a "primary" valley worth flagging as a water-system candidate,
# as opposed to a minor tributary. CONFIGURABLE, same tuning approach as
# above. Must be >= MIN_STREAM_CONTRIBUTING_AREA_ACRES.
MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES = 2.0

# The epsilon increment the depression fill adds per cell along a filled
# flat's own drainage path, so a filled cell never TIES with the
# neighbour it drains to.
#
# WHY IT EXISTS. fill_depressions() used to be the PLAIN priority-flood:
# it raised a pit to EXACTLY its spill elevation, so the filled cell and
# the neighbour it should drain to sat at the same elevation.
# compute_flow_direction() requires a STRICTLY positive slope, so every
# one of those tied cells got the -1 "no downhill neighbour" sentinel and
# was unroutable -- and every consumer that walks the flow field died
# there (truncated backwaters, wall walks ending flat_tie_sentinel,
# unreachable_stem_end cross-section stations, embankment seeds
# terminating flow_end with 0-2 stations measured). The epsilon variant
# (Barnes et al. 2014's own "Priority-Flood+epsilon") raises each cell to
# at least its predecessor's FILLED elevation plus this increment, so the
# filled surface slopes gently toward the outlet and every filled cell
# has a defined direction.
#
# WHY 0.001 m, stated as the two bounds it has to sit between:
#
#   UPPER BOUND -- it must be far too small to manufacture terrain
#   signal. USGS 3DEP's vertical accuracy is specified in the 0.1 m
#   neighbourhood (QL2 lidar's 10 cm RMSEz; the coarser 1/3 arc-second
#   product is worse), and this repo's own two independent "is this fill
#   real or noise" thresholds sit at 0.10 m
#   (water_survey_areas.DEPRESSION_NOISE_FLOOR_METERS) and 0.15 m
#   (keypoint_detection.KEYPOINT_FILL_ARTIFACT_THRESHOLD_M). At 0.001 m
#   a filled flat has to be 100 D8 HOPS across -- 500 m at this DEM's 5 m
#   resolution, in one dead-level piece -- before the accumulated rise
#   reaches even the lower of those two. (Hops, not cells: the flood
#   spreads outward from the spill, so a cell's increment count is its
#   Chebyshev distance from the spill point, not the flat's area. A
#   40x40 flat is 1600 cells and accumulates 40 increments, measured in
#   test_epsilon_fill.py.) That is two orders of magnitude below the
#   DEM's own ability to tell ground apart.
#
#   LOWER BOUND -- it must not vanish to floating-point rounding.
#   dem_data.get_dem_for_boundary() returns float32 (`src.read(1).
#   astype("float32")`), and fill_depressions() preserves its input's
#   dtype, so the increment is added at float32 precision. float32's ulp
#   at an elevation z is 2^(floor(log2 z) - 23): at the reference
#   property's ~346 m that is 3.05e-5 m, so 0.001 m is ~33 ulps and every
#   increment lands on a strictly greater float32. The margin holds up to
#   z = 0.001 * 2^23 = 8388 m, above every land elevation this tool can
#   be pointed at. (test_epsilon_fill.py asserts this against the real
#   float32 dtype via np.spacing() rather than taking it on faith.)
#
# CONFIGURABLE, but not a tuning knob in the ordinary sense: raising it
# buys nothing and starts eating into the noise floor above; lowering it
# eventually loses the strict inequality it exists to guarantee. v1
# prior -- if a filled flat on some property is ever long enough for the
# accumulated rise to approach DEPRESSION_NOISE_FLOOR_METERS, that is a
# finding to report, not a number to retune away.
FILL_EPSILON_METERS = 0.001

VALLEY_CONFIDENCE_NOTES = (
    "Valley lines are computed from an interpolated DEM (LiDAR-derived "
    "where flown, coarser 1/3 arc-second elsewhere — see dem_data.py) "
    "using standard D8 flow-direction/accumulation terrain analysis, not "
    "surveyed or field-verified. Flow is modeled as single-direction "
    "steepest descent between grid cells, which can't represent braided "
    "or split flow, and resolution-scale features (below the DEM's pixel "
    "size) won't appear. Treat this as a first-pass drainage network for "
    "design purposes, not a wetlands or hydrology survey. A valley line "
    "may legitimately extend past the drawn property boundary into the "
    "DEM's buffered surrounding area (see dem_data.py) — flow paths don't "
    "stop at a property line, so a branch's head or an inflection point "
    "sitting just off-parcel is real upstream/downstream context, not an "
    "error; this is intentionally NOT clipped to the boundary the way "
    "production-area and other candidate-zone layers are."
)


def _valid_mask(array: np.ndarray) -> np.ndarray:
    return ~np.isnan(array)


def fill_depressions(array: np.ndarray, epsilon_meters: float = FILL_EPSILON_METERS) -> np.ndarray:
    """
    Priority-flood depression filling, EPSILON variant (Barnes et al.
    2014's Priority-Flood+epsilon): raises every interior cell to at
    least its own flood predecessor's FILLED elevation plus
    epsilon_meters, so every valid cell has a STRICTLY DECREASING path to
    the grid's valid border. Without any fill, a local pit (real or a DEM
    interpolation artifact) traps flow accumulation and breaks valley
    tracing at that point.

    THE EPSILON IS THE POINT, not a detail. The plain variant this
    replaced raised a pit to EXACTLY its spill elevation, which left the
    filled cell TIED with the neighbour it should drain to;
    compute_flow_direction() requires a strictly positive slope, so every
    such cell got the -1 sentinel and was unroutable, and every consumer
    that walks the flow field stopped there. Raising by epsilon per cell
    gives the filled flat a defined flow direction toward its outlet. See
    FILL_EPSILON_METERS for the increment's two bounds (well under the
    DEM's vertical accuracy, well over float32's resolution) and for what
    the accumulated rise along a long flat costs.

    A cell already MORE than epsilon_meters above its predecessor is left
    alone — this raises only what it has to, so terrain outside the
    depressions and flats is bitwise unchanged. The RAW input array is
    never modified (a copy is filled and returned); the raw/filled
    division of labour — connectivity from the filled field, elevation
    truth from the raw — is a hard architectural boundary this function
    is on one side of.

    Passing epsilon_meters=0.0 reproduces the plain variant exactly, for
    before/after measurement only; nothing in the pipeline does that.

    nodata (np.nan) cells are excluded entirely — treated as barriers, not
    filled or flowed through.
    """
    rows, cols = array.shape
    valid = _valid_mask(array)
    filled = array.copy()
    closed = ~valid

    heap: list[tuple[float, int, int, int]] = []
    counter = 0

    def _seed(r: int, c: int) -> None:
        nonlocal counter
        if valid[r, c] and not closed[r, c]:
            heapq.heappush(heap, (float(filled[r, c]), counter, r, c))
            closed[r, c] = True
            counter += 1

    for c in range(cols):
        _seed(0, c)
        _seed(rows - 1, c)
    for r in range(rows):
        _seed(r, 0)
        _seed(r, cols - 1)

    while heap:
        elevation, _, r, c = heapq.heappop(heap)
        minimum_neighbor_elevation = elevation + epsilon_meters
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc] and not closed[nr, nc]:
                closed[nr, nc] = True
                if filled[nr, nc] < minimum_neighbor_elevation:
                    # The epsilon increment: this cell now sits strictly
                    # above the cell it drains to, so it routes.
                    filled[nr, nc] = minimum_neighbor_elevation
                heapq.heappush(heap, (float(filled[nr, nc]), counter, nr, nc))
                counter += 1

    return filled


def compute_flow_direction(
    filled: np.ndarray, resolution_meters: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray]:
    """
    D8 flow direction: each valid cell points to whichever of its up-to-8
    neighbors gives the steepest downhill slope (elevation drop / real
    ground distance, so diagonal neighbors are correctly penalized by
    sqrt(2)x the distance of cardinal ones).

    Returns (flow_to_row, flow_to_col), each shaped like `filled`, with -1
    at cells that have no downhill neighbor.

    WHERE THE -1 SENTINEL LEGITIMATELY REMAINS, now that fill_depressions()
    is the epsilon variant: a border cell that is the local minimum of its
    own neighbourhood (a grid-edge outlet — the flood SEEDS the border
    rather than raising it, so border cells are the one class it never
    gives a gradient to), and a valid cell the flood cannot reach at all
    because nodata walls it off from every border. Every cell the flood
    DOES reach was raised to at least its predecessor's filled elevation
    plus FILL_EPSILON_METERS, so it has a strictly lower neighbour by
    construction and gets a real direction. Interior flat ties — the old
    dominant source of this sentinel, and the defect the epsilon fill
    exists to remove — are gone. Downstream code still has to handle -1;
    it means "no direction here" and that is still a real answer at an
    outlet.
    """
    rows, cols = filled.shape
    valid = _valid_mask(filled)
    px, py = resolution_meters

    flow_to_row = np.full((rows, cols), -1, dtype=np.int32)
    flow_to_col = np.full((rows, cols), -1, dtype=np.int32)

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            best_slope = 0.0
            best = None
            for dr, dc in D8_OFFSETS:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    distance = math.hypot(dc * px, dr * py)
                    drop = filled[r, c] - filled[nr, nc]
                    slope = drop / distance
                    if slope > best_slope:
                        best_slope = slope
                        best = (nr, nc)
            if best is not None:
                flow_to_row[r, c], flow_to_col[r, c] = best

    return flow_to_row, flow_to_col


def compute_flow_accumulation(
    filled: np.ndarray, flow_to_row: np.ndarray, flow_to_col: np.ndarray
) -> np.ndarray:
    """
    Upstream contributing cell count per cell: each cell starts counting
    itself (1), then that total is added into whatever cell it flows into.
    Processing cells in descending filled-elevation order guarantees every
    cell has received all of its upstream contributions before it passes
    its own total downstream — flow direction only ever points to a
    strictly lower (or off-grid) cell, so there are no cycles to worry
    about.
    """
    rows, cols = filled.shape
    valid = _valid_mask(filled)
    accumulation = valid.astype(np.int64)

    sort_key = np.where(valid, filled, -np.inf)
    order = np.argsort(sort_key, axis=None)[::-1]  # descending elevation

    for flat_index in order:
        r, c = divmod(int(flat_index), cols)
        if not valid[r, c]:
            continue
        tr, tc = flow_to_row[r, c], flow_to_col[r, c]
        if tr >= 0:
            accumulation[tr, tc] += accumulation[r, c]

    return accumulation


def _trace_branches(
    component_cells: set[tuple[int, int]],
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
) -> list[list[tuple[int, int]]]:
    """
    Traces each "head" of a valley connected-component (a cell no other
    in-component cell flows into) downstream through the component to
    where it exits (an outlet — the next flow target isn't part of this
    component, i.e. drops below the stream threshold or leaves the grid).

    A component with tributaries produces multiple branches that share
    cells near their confluence — that's expected and matches how a real
    drainage network looks (tributaries merging into a main stem), not a
    bug.
    """
    in_degree = {cell: 0 for cell in component_cells}
    for r, c in component_cells:
        target = (int(flow_to_row[r, c]), int(flow_to_col[r, c]))
        if target in in_degree:
            in_degree[target] += 1

    heads = [cell for cell, degree in in_degree.items() if degree == 0]

    branches = []
    for head in heads:
        path = [head]
        visited = {head}
        r, c = head
        while True:
            target = (int(flow_to_row[r, c]), int(flow_to_col[r, c]))
            if target not in component_cells or target in visited:
                break
            path.append(target)
            visited.add(target)
            r, c = target
        branches.append(path)

    return branches


def _branch_to_wgs84_linestring(dem: dict, branch: list[tuple[int, int]]) -> dict:
    utm_points = [pixel_center_xy(dem, r, c) for r, c in branch]
    xs = [p[0] for p in utm_points]
    ys = [p[1] for p in utm_points]
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", xs, ys)
    return {"type": "LineString", "coordinates": list(zip(lons, lats))}


def get_flow_direction_for_dem(dem: dict) -> np.ndarray:
    """
    Runs the fill -> flow-direction prefix of the delineate_valleys()
    pipeline (see module docstring) and stops there -- one step before
    get_flow_accumulation_for_dem() itself, and well before the stream
    threshold/tracing steps that turn any of this into discrete valley
    branches.

    Returns a single (rows, cols, 2) int32 array, same rows/cols shape and
    alignment as dem['array'] and get_flow_accumulation_for_dem()'s own
    output (a cell at (row, col) means the same thing across all three).

    ENCODING: this is NOT the classic D8 8-direction bitmask/integer code
    (1, 2, 4, 8, 16, 32, 64, 128 for N/NE/E/SE/S/SW/W/NW) --
    compute_flow_direction() itself never computes that code, so this
    doesn't invent one just to expose it. Instead, the last axis holds
    compute_flow_direction()'s own (flow_to_row, flow_to_col) pair,
    stacked into one array:

        target_row, target_col = get_flow_direction_for_dem(dem)[row, col]

    i.e. the ABSOLUTE (row, col) of the single downhill neighbor that
    cell flows into (not a relative offset or direction code). (-1, -1)
    means no downhill neighbor at all (a grid-edge outlet, or a valid
    cell nodata walls off from the border -- see compute_flow_direction()
    for where the sentinel legitimately survives the epsilon fill) --
    matches compute_flow_direction()'s own -1 sentinel exactly.

    A caller walking the grid cell-to-cell reads target_row, target_col =
    get_flow_direction_for_dem(dem)[row, col]; if target_row < 0, stop
    (outlet); otherwise move to (target_row, target_col) and repeat.
    """
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    return np.stack([flow_to_row, flow_to_col], axis=-1)


def get_flow_accumulation_for_dem(dem: dict) -> np.ndarray:
    """
    Runs the fill -> flow-direction -> flow-accumulation prefix of the
    delineate_valleys() pipeline (see module docstring) and stops there,
    before the stream threshold/tracing steps that turn it into discrete
    valley branches.

    Returns the raw upstream contributing-cell-count grid (same shape as
    dem["array"], same row/col alignment as every other DEM-shaped array
    in this codebase — nodata cells count as 1, matching what
    compute_flow_accumulation always does; they're never valley cells
    themselves but flow accumulation doesn't zero them out). This is for
    a downstream module that wants to test individual DEM cells against
    the accumulation grid directly (e.g. "is this cell on high-enough
    contributing area to be a channel") rather than only consuming traced
    branch polylines. It intentionally does not multiply by
    cell_area_acres(dem) or apply either area threshold — callers that
    want acres or "is this a valley cell" should do that conversion
    themselves, same as delineate_valleys() does internally.
    """
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    return compute_flow_accumulation(filled, flow_to_row, flow_to_col)


def delineate_valleys(
    dem: dict,
    min_stream_area_acres: float = MIN_STREAM_CONTRIBUTING_AREA_ACRES,
    min_primary_valley_area_acres: float = MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES,
) -> list[dict]:
    """
    Runs the full fill -> flow direction -> flow accumulation -> threshold
    -> trace pipeline on `dem` (see raster_grid.py for the DEM dict shape)
    and returns one entry per primary valley:

        {
            'id': int,
            'max_contributing_area_acres': float,
            'branches_rowcol': [[(row, col), ...], ...],  # head -> outlet
            'branches_utm': [[(x, y, elevation_m), ...], ...],
            'geometry_wgs84': GeoJSON LineString or MultiLineString,
        }

    branches_utm is what water_candidate_zones.py actually reasons over
    (real-meter coordinates + elevation, in the DEM's own projected CRS,
    so gradient/distance math is exact); geometry_wgs84 is only for
    output/display.
    """
    array = dem["array"]
    filled = fill_depressions(array)
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)

    area_per_cell = cell_area_acres(dem)
    contributing_area_acres = accumulation * area_per_cell

    valley_mask = contributing_area_acres >= min_stream_area_acres
    labels, num_components = connected_components(valley_mask)

    valleys = []
    for component_id in range(num_components):
        rows_cols = np.argwhere(labels == component_id)
        component_cells = {(int(r), int(c)) for r, c in rows_cols}

        max_area = max(contributing_area_acres[r, c] for r, c in component_cells)
        if max_area < min_primary_valley_area_acres:
            continue  # a real drainage line, but too minor to call "primary"

        branches_rowcol = _trace_branches(component_cells, flow_to_row, flow_to_col)
        # A branch needs at least 2 cells to be a line at all; a component
        # that reduces to single isolated cells (a rare, degenerate case)
        # isn't a meaningful valley line and is dropped rather than
        # emitting invalid single-point GeoJSON LineString geometry.
        branches_rowcol = [b for b in branches_rowcol if len(b) >= 2]
        if not branches_rowcol:
            continue

        branches_utm = [
            [(*pixel_center_xy(dem, r, c), float(filled[r, c])) for r, c in branch]
            for branch in branches_rowcol
        ]

        line_geometries = [_branch_to_wgs84_linestring(dem, b) for b in branches_rowcol]
        geometry_wgs84 = (
            line_geometries[0]
            if len(line_geometries) == 1
            else {
                "type": "MultiLineString",
                "coordinates": [g["coordinates"] for g in line_geometries],
            }
        )

        valleys.append(
            {
                "id": component_id,
                "max_contributing_area_acres": round(float(max_area), 2),
                "branches_rowcol": branches_rowcol,
                "branches_utm": branches_utm,
                "geometry_wgs84": geometry_wgs84,
            }
        )

    return valleys


def valleys_to_geojson(valleys: list[dict]) -> dict:
    """
    Wraps delineate_valleys() output as a schema-conformant GeoJSON
    FeatureCollection (layer="valley") — the discrete-feature output Step 1
    calls for, separate from the raw DEM raster itself.

    CONSOLIDATED: the body now lives in wire_translation.py (the outbound
    half of the translation boundary), which is where every layer's
    internal-result-to-feature_schema conversion is kept so the inbound
    half added later has one place to be checked against. This name stays
    as the module's own established entry point; it is a thin forward, not
    a second implementation.
    """
    from wire_translation import valleys_to_feature_collection

    return valleys_to_feature_collection(valleys)


def summarize_valleys(valleys: list[dict]) -> str:
    if not valleys:
        return "No primary valleys identified on this property."

    lines = [f"Primary valleys found: {len(valleys)}"]
    for v in sorted(valleys, key=lambda x: -x["max_contributing_area_acres"]):
        lines.append(
            f"  - Valley {v['id']}: {len(v['branches_rowcol'])} branch(es), "
            f"up to {v['max_contributing_area_acres']} acres contributing area"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline smoke test with a synthetic V-shaped valley DEM — see
    # test_valley_delineation.py for the real (assertion-based) version of
    # this. This is just a quick eyeball check when iterating on the
    # algorithm directly.
    size = 30
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        for col in range(size):
            # Elevation rises with distance from the diagonal "valley
            # floor" and slopes down along it (south to north), so flow
            # should converge onto the diagonal and head toward row 0.
            distance_from_valley = abs((size - 1 - row) - col)
            array[row, col] = distance_from_valley * 2.0 + row * 0.5

    synthetic_dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    valleys = delineate_valleys(synthetic_dem)
    print(summarize_valleys(valleys))
