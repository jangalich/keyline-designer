"""
valley_delineation.py

Delineates primary valleys (drainage/flow paths) from a DEM grid — the
terrain-analysis step this pipeline's README long flagged as a future
piece ("real LiDAR-based keypoint detection... running real terrain-
analysis tools to mathematically locate keypoints and candidate keylines").

Pipeline, standard D8 hydrology terrain analysis (Barnes-style priority-
flood fill, then D8 flow direction, then flow accumulation):

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
    model. It can't represent flow splitting/braiding, and ties on a
    perfectly flat filled cell (e.g. a real pond/wetland) aren't resolved
    beyond one exit path — a small, standard limitation of this approach,
    not a bug.
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

from feature_schema import CONFIDENCE_MEDIUM, make_feature, make_feature_collection
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


def fill_depressions(array: np.ndarray) -> np.ndarray:
    """
    Priority-flood depression filling (Barnes et al. 2014, plain variant):
    raises every interior cell to at least the elevation of the lowest
    path connecting it to the grid's valid border, so every valid cell has
    a monotonically non-increasing path to an edge. Without this, any
    local pit (real or a DEM interpolation artifact) would trap flow
    accumulation and break valley tracing at that point.

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
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc] and not closed[nr, nc]:
                closed[nr, nc] = True
                if filled[nr, nc] < elevation:
                    filled[nr, nc] = elevation
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
    at cells that have no downhill neighbor (a grid-edge outlet, or a flat
    plateau tie — see module docstring).
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
    """
    features = [
        make_feature(
            feature_id=f"valley-{v['id']}",
            geometry=v["geometry_wgs84"],
            layer="valley",
            label=f"Valley {v['id']}",
            confidence=CONFIDENCE_MEDIUM,
            confidence_notes=VALLEY_CONFIDENCE_NOTES,
            extra_properties={
                "max_contributing_area_acres": v["max_contributing_area_acres"],
                "branch_count": len(v["branches_rowcol"]),
            },
        )
        for v in valleys
    ]
    return make_feature_collection(features)


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
