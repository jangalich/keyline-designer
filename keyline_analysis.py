"""
keyline_analysis.py

Three pure, network-free measurement primitives for keyline-based water
design. Written for explore_keyline_water_zones.py -- the diagnostic
exploration pass that generates a water zone at EVERY valley crossing of
EVERY qualifying keyline, with no floors, drops or separation rules -- so
that the keep/throw rules of the eventual redesign get designed from
evidence rather than from assumption.

NOTHING HERE IS WIRED INTO THE PIPELINE. water_candidate_zones.py,
water_suitability.py, pipeline_context.py and every batch consumer are
untouched by this module's existence. The third function below
(decompose_pool_perimeter) is the measurement layer the redesign is
expected to adopt; this is its first real exercise, on real ground, before
anything depends on it.

    1. extract_level_contour(dem, elevation)
           the level set of the RAW DEM at ONE elevation, as polylines.
           A keyline is exactly this: the contour through a keypoint.

    2. find_stem_crossings(contour_lines_utm, valleys, dem)
           where that level set crosses each traced valley BRANCH -- every
           branch, not one per valley. Each crossing is a candidate anchor
           seed for the wall walk.

    3. decompose_pool_perimeter(pool_mask, dem, waterline)
           the 2D LEVEL-SET measurement that replaces the perpendicular
           transect: classify every exterior edge of a delineated pool as
           held by GROUND or held only by a PROPOSED WALL, and return the
           wall lines themselves.

WHY (3) REPLACES THE TRANSECT, stated plainly because it is the point of
this module. valley_level_pool.py measures a pool by shooting a
perpendicular across the channel at the anchor and at stations upstream,
and every one of those measurements needs a DIRECTION -- which is why that
module carries a local-stem-secant, a window constant, a degenerate-window
flag, and a documented history of a straight-line axis fit that could
settle 90 degrees off on subtle ground. A pool's perimeter needs no
direction at all. The waterline is a level set; the ground either reaches
it or it does not; the edges where it does not ARE the wall. Every number
below falls out of a 4-neighbour scan over the cell mask. NO BEARING IS
COMPUTED, ASSUMED OR REQUIRED ANYWHERE IN THIS MODULE.

THE GEOMETRY CONTRACT. Every polyline this module returns carries its
WGS84 form BESIDE its UTM form, both built in the same call that created
the geometry. Nothing downstream reprojects at serialization time: an
exporter READS points_wgs84/geometry_wgs84 and writes them. That is why
each of the three functions takes `dem` (it holds the CRS and the grid
origin) even where the arithmetic itself is CRS-free.

FILLED vs RAW, the division this codebase keeps hammering and this module
does not break:
  * extract_level_contour reads the RAW dem['array']. A keyline is a line
    on the ground as measured, not on a depression-filled surrogate; a
    contour of the filled DEM would run around the rim of every pit
    instead of through it.
  * find_stem_crossings compares against the branch's own channel
    elevation, which valley_delineation.delineate_valleys() builds from
    the FILLED array (branches_utm carries filled[r, c]). The two
    therefore disagree by exactly the fill depth wherever the channel runs
    through a filled flat -- which is the mechanism that produces the
    CLUSTERS this function collapses, and it is reported rather than
    smoothed over: every crossing carries both the interpolated filled
    channel elevation and the RAW elevation at its own cell.
  * decompose_pool_perimeter reads the RAW dem['array'] for the "does this
    ground reach the waterline" test, the same choice
    valley_level_pool.delineate_level_pool() makes for the same reason.

CONTOURPY, NOT scikit-image. The exploration design named skimage's
measure.find_contours(). scikit-image is NOT a dependency of this
pipeline -- it is absent from requirements.txt and not installed -- and
adding one to a diagnostic branch would be a real cost for no gain, since
this repository ALREADY computes contours, in contour_lines.py, using
contourpy (matplotlib's own contour-computation engine, an unavoidable
transitive dependency of the matplotlib this pipeline already requires).
See contour_lines.compute_contour_lines()'s docstring for the full
argument, which applies here unchanged. The one behavioural difference
worth naming: find_contours() works in GRID index space and would need a
grid->UTM conversion afterwards, while contourpy is handed the real
cell-center axes and returns real dem['crs'] meters directly -- one fewer
place for a coordinate convention to drift.

PURE AND NETWORK-FREE. Every input is an array, a plain dict or a shapely
geometry. test_keyline_analysis.py drives all three against synthetic
fixtures whose expected numbers are hand-derived in comments before the
code runs.
"""

import math

import contourpy
import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString, Point

from raster_grid import SQUARE_METERS_PER_ACRE, pixel_center_xy

# Edge classifications returned by decompose_pool_perimeter(). Part of the
# measurement contract, in the same spirit as valley_level_pool.py's
# STATION_MEASURED / STATION_UNREACHABLE_STEM_END: the three are different
# FACTS about an edge and collapsing any two would misreport the pool.
#
#   GROUND_CLOSED -- the cell across this edge is on the grid, has a real
#                    elevation, and that elevation is AT OR ABOVE the
#                    waterline. The ground itself holds the water here.
#   OPEN          -- the cell across this edge is on the grid, has a real
#                    elevation, and it is BELOW the waterline. Water would
#                    run out here; only a wall holds it. THESE EDGES ARE
#                    THE PROPOSED WALL.
#   UNDETERMINED  -- there is no cell across this edge to ask (the DEM ends)
#                    or it is nodata. This is NOT open: proposing a wall on
#                    ground we cannot see would be inventing the one number
#                    a wall line is supposed to state. It is reported as its
#                    own length and excluded from the wall lines.
EDGE_GROUND_CLOSED = "ground_closed"
EDGE_OPEN = "open"
EDGE_UNDETERMINED = "undetermined"


def _polyline(dem: dict, points_utm) -> dict:
    """
    ONE polyline in both wire forms, built together, in the call that
    created the geometry -- the geometry contract in a single place so no
    caller can satisfy half of it.

    Returns
        {
            'points_utm':  [(x, y), ...],          # real dem['crs'] meters
            'points_wgs84': [(lon, lat), ...],     # built HERE, not later
            'line_utm': shapely LineString,        # for math/intersection
            'geometry_wgs84': GeoJSON LineString,  # for the wire
            'length_m': float,                     # measured in UTM meters
            'closed': bool,                        # first point == last
        }

    The single warp_transform() call per polyline (not per vertex) is
    deliberate -- it is the same batched form every other layer module in
    this pipeline uses.
    """
    points_utm = [(float(x), float(y)) for x, y in points_utm]
    xs = [p[0] for p in points_utm]
    ys = [p[1] for p in points_utm]
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", xs, ys)
    points_wgs84 = [(float(lon), float(lat)) for lon, lat in zip(lons, lats)]
    line_utm = LineString(points_utm)
    return {
        "points_utm": points_utm,
        "points_wgs84": points_wgs84,
        "line_utm": line_utm,
        "geometry_wgs84": {"type": "LineString", "coordinates": points_wgs84},
        "length_m": round(float(line_utm.length), 3),
        "closed": points_utm[0] == points_utm[-1],
    }


def _point_pair(dem: dict, x: float, y: float) -> dict:
    """A single point in both wire forms, same contract as _polyline()."""
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", [float(x)], [float(y)])
    return {
        "point_utm": (float(x), float(y)),
        "point_wgs84": (float(lons[0]), float(lats[0])),
        "geometry_wgs84": {"type": "Point", "coordinates": (float(lons[0]), float(lats[0]))},
    }


def _grid_axes(dem: dict) -> tuple[np.ndarray, np.ndarray]:
    """
    Cell-center x (ascending) / y (DESCENDING, matching this pipeline's
    row-increases-southward DEM convention) axes for contourpy.

    Duplicated from contour_lines._grid_axes() rather than imported, the
    same call valley_level_pool.py makes for its own _cell_center_distance:
    that module computes contours at a fixed INTERVAL across the whole DEM
    and this one computes a SINGLE named level, so the two have no shared
    public entry point to hang a helper on, and reaching into another
    module's private for three lines of arithmetic buys less than it costs.
    contourpy accepts a descending y axis directly -- see that module's own
    docstring for the confirmation.
    """
    rows, cols = dem["array"].shape
    x = np.array([pixel_center_xy(dem, 0, c)[0] for c in range(cols)])
    y = np.array([pixel_center_xy(dem, r, 0)[1] for r in range(rows)])
    return x, y


def extract_level_contour(dem: dict, elevation: float) -> list[dict]:
    """
    The level set of the RAW DEM at exactly `elevation`, as polylines --
    i.e. THE KEYLINE, when `elevation` is a keypoint's own elevation.

    Runs over the DEM's FULL extent and CLIPS NOTHING. A keyline that
    enters from off-parcel and runs back out is a real line on real ground;
    the property boundary is something to DRAW on the map, never something
    to apply to the data. (valley_delineation.py states the same policy for
    valley branches, and for the same reason: terrain does not stop at a
    property line.)

    Returns one entry per polyline, each carrying both wire forms per the
    module docstring's geometry contract:

        {
            'elevation_m': float,       # the level, echoed on every polyline
            'points_utm': [(x, y), ...],
            'points_wgs84': [(lon, lat), ...],
            'line_utm': shapely LineString,
            'geometry_wgs84': GeoJSON LineString,
            'length_m': float,
            'closed': bool,
        }

    Polylines are returned LONGEST FIRST, so a caller printing "the
    keyline" prints the dominant run rather than whichever fragment
    contourpy emitted first.

    NaN (nodata) cells truncate a contour at the edge of valid data rather
    than being interpolated across -- contourpy's own confirmed behaviour,
    relied on in contour_lines.py already.

    An `elevation` outside the DEM's range, or a fully-nodata DEM, returns
    [] -- the honest empty answer. A polyline of fewer than 2 vertices is
    dropped for the same reason valley_delineation.py drops single-cell
    branches: it is not a line, and emitting it would produce invalid
    GeoJSON.
    """
    array = dem["array"]
    valid = array[~np.isnan(array)]
    if valid.size == 0:
        return []

    x, y = _grid_axes(dem)
    generator = contourpy.contour_generator(
        x=x, y=y, z=array, line_type=contourpy.LineType.Separate
    )
    segments = [seg for seg in generator.lines(float(elevation)) if len(seg) >= 2]

    polylines = []
    for segment in segments:
        polyline = _polyline(dem, [(float(px), float(py)) for px, py in segment])
        polyline["elevation_m"] = round(float(elevation), 3)
        polylines.append(polyline)

    polylines.sort(key=lambda p: -p["length_m"])
    return polylines


def _branch_measure(branch_utm: list) -> tuple[LineString, list[float], list[float]]:
    """
    (line, cumulative_distance_m, channel_elevation_m) for one
    delineate_valleys() branch -- branch_utm is its [(x, y, elevation), ...]
    vertex list, whose elevations come from the FILLED array (see the module
    docstring). Returns the 2D line for intersection, the cumulative
    along-line distance at each vertex, and the vertex elevations, so a
    crossing's channel elevation can be interpolated at its own projected
    position rather than snapped to a vertex.
    """
    points = [(float(v[0]), float(v[1])) for v in branch_utm]
    elevations = [float(v[2]) for v in branch_utm]
    cumulative = [0.0]
    for i in range(1, len(points)):
        cumulative.append(
            cumulative[-1] + math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1])
        )
    return LineString(points), cumulative, elevations


def _interpolate_along(cumulative: list[float], values: list[float], distance: float) -> tuple[float, int]:
    """
    (value at `distance` along the polyline, index of the NEAREST vertex).

    Linear interpolation between the two vertices bracketing `distance`;
    the nearest-vertex index is returned alongside because that vertex is a
    REAL traced channel cell, which is what a downstream wall walk must be
    seeded from (an interpolated point can land in a cell the branch never
    visited, on a diagonal step).
    """
    if distance <= cumulative[0]:
        return values[0], 0
    if distance >= cumulative[-1]:
        return values[-1], len(values) - 1
    for i in range(1, len(cumulative)):
        if distance <= cumulative[i]:
            span = cumulative[i] - cumulative[i - 1]
            t = 0.0 if span <= 0 else (distance - cumulative[i - 1]) / span
            value = values[i - 1] + t * (values[i] - values[i - 1])
            nearest = i - 1 if t < 0.5 else i
            return value, nearest
    return values[-1], len(values) - 1


def _intersection_points(line_a: LineString, line_b: LineString) -> tuple[list[Point], bool]:
    """
    (points where the two lines meet, collinear_overlap).

    A contour and a channel normally meet at isolated POINTS. They can also
    share a RUN of line -- a contour tracing along a channel that is level
    over several cells -- and shapely returns that as a LineString rather
    than a Point. That is a real configuration (a level reach), not a
    degenerate one to discard, so it is represented by the run's midpoint
    and reported with collinear_overlap=True rather than being silently
    dropped or silently counted as an ordinary crossing.
    """
    result = line_a.intersection(line_b)
    if result.is_empty:
        return [], False

    points: list[Point] = []
    collinear = False
    geometries = list(result.geoms) if hasattr(result, "geoms") else [result]
    for geometry in geometries:
        if geometry.geom_type == "Point":
            points.append(geometry)
        elif geometry.geom_type in ("LineString", "LinearRing"):
            collinear = True
            points.append(geometry.interpolate(0.5, normalized=True))
        elif geometry.geom_type == "MultiPoint":
            points.extend(list(geometry.geoms))
    return points, collinear


def find_stem_crossings(contour_lines_utm, valleys: list[dict], dem: dict) -> list[dict]:
    """
    Where one keyline crosses each traced valley BRANCH -- EVERY branch of
    every valley, not one representative branch per valley.

    `contour_lines_utm` is the keyline: either extract_level_contour()'s own
    output dicts or a bare list of shapely LineStrings (both accepted, since
    a caller holding lines from somewhere else should not have to fake the
    dict). `valleys` is valley_delineation.delineate_valleys()' output --
    branches_utm supplies the geometry and the channel elevation,
    branches_rowcol supplies the real channel cells. `dem` is needed for the
    CRS (the geometry contract: WGS84 is stored, never derived later) and
    for nothing else.

    AT MOST ONE CROSSING PER BRANCH IS RETURNED, and that is a statement
    about the terrain rather than a filter. Channel elevation is strictly
    decreasing along a branch -- valley_delineation.compute_flow_direction()
    requires a strictly positive slope on the FILLED array, so a branch
    cannot revisit a level -- therefore a branch has exactly one true
    crossing of any given level, or none.

    A CLUSTER of two or more intersections on one branch is therefore
    numerical, and its mechanism is known: the contour is a level set of the
    RAW DEM while the branch's elevations are FILLED, so wherever the
    channel runs through a filled flat the two surfaces disagree by the fill
    depth and the raw contour can weave back and forth across the channel
    line. The cluster is collapsed to the member whose interpolated channel
    elevation is CLOSEST TO THE LEVEL ITSELF -- the branch's own elevation
    match -- and `cluster_size` records how many there were, so a cluster is
    visible in the output rather than being quietly averaged away.

    Returns one dict per surviving crossing:

        {
            'valley_id': int,
            'branch_index': int,               # index into branches_utm
            'level_m': float,                  # the keyline's elevation
            'point_utm': (x, y),
            'point_wgs84': (lon, lat),
            'geometry_wgs84': GeoJSON Point,
            'channel_elevation_m': float,      # FILLED, interpolated at the
                                               #   crossing's own position
            'channel_elevation_raw_m': float,  # RAW, at branch_rowcol
            'fill_depth_at_crossing_m': float, # filled - raw; > 0 means the
                                               #   crossing sits on ground the
                                               #   priority-flood raised
            'level_residual_m': float,         # channel_elevation_m - level_m
            'rowcol': (row, col),              # the cell CONTAINING the point
            'branch_rowcol': (row, col),       # the nearest TRACED CHANNEL cell
                                               #   -- seed a flow walk from THIS
            'along_branch_m': float,           # distance from the branch head
            'cluster_size': int,               # 1 when there was no cluster
            'cluster_along_branch_m': [float, ...],   # every member's position
            'collinear_overlap': bool,         # the contour ran ALONG the channel
        }

    ordered by (valley_id, branch_index) so a run is diffable.
    """
    lines: list[LineString] = []
    for entry in contour_lines_utm:
        if isinstance(entry, dict):
            lines.append(entry["line_utm"])
        else:
            lines.append(entry)

    px, py = dem["resolution_meters"]
    rows, cols = dem["array"].shape
    raw = dem["array"]

    # The level is the contour's own elevation, and every polyline in one
    # keyline shares it, so it is read ONCE here rather than per branch. A
    # caller that passed bare LineStrings has not told us the level; rather
    # than invent one, such a call falls back per branch to the median of
    # that branch's own measured crossing elevations (see below), which is
    # the measured value instead of a fabricated one.
    declared_level = None
    for entry in contour_lines_utm:
        if isinstance(entry, dict) and "elevation_m" in entry:
            declared_level = float(entry["elevation_m"])
            break

    crossings: list[dict] = []
    for valley in valleys:
        branches_utm = valley.get("branches_utm") or []
        branches_rowcol = valley.get("branches_rowcol") or []
        for branch_index, branch_utm in enumerate(branches_utm):
            if len(branch_utm) < 2:
                continue
            branch_line, cumulative, elevations = _branch_measure(branch_utm)
            branch_cells = branches_rowcol[branch_index] if branch_index < len(branches_rowcol) else []

            members: list[dict] = []
            collinear_any = False
            for line in lines:
                points, collinear = _intersection_points(line, branch_line)
                collinear_any = collinear_any or collinear
                for point in points:
                    distance = float(branch_line.project(point))
                    channel_elevation, nearest_vertex = _interpolate_along(cumulative, elevations, distance)
                    members.append(
                        {
                            "point": point,
                            "along_branch_m": distance,
                            "channel_elevation_m": channel_elevation,
                            "nearest_vertex": nearest_vertex,
                        }
                    )

            if not members:
                continue

            level = (
                declared_level
                if declared_level is not None
                else float(np.median([m["channel_elevation_m"] for m in members]))
            )

            best = min(members, key=lambda m: (abs(m["channel_elevation_m"] - level), m["along_branch_m"]))
            point = best["point"]

            # The cell CONTAINING the crossing -- the exact inverse of
            # pixel_center_xy(), the same arithmetic
            # valley_level_pool.rowcol_for_xy() performs (duplicated rather
            # than imported so this module keeps no dependency on the
            # production delineator it is being designed to inform).
            col = int(math.floor((point.x - dem["origin_x"]) / px))
            row = int(math.floor((dem["origin_y"] - point.y) / py))
            row = min(max(row, 0), rows - 1)
            col = min(max(col, 0), cols - 1)

            branch_rowcol = (
                tuple(int(v) for v in branch_cells[min(best["nearest_vertex"], len(branch_cells) - 1)])
                if branch_cells
                else (row, col)
            )
            raw_elevation = float(raw[branch_rowcol[0], branch_rowcol[1]])

            pair = _point_pair(dem, point.x, point.y)
            crossings.append(
                {
                    "valley_id": int(valley["id"]),
                    "branch_index": int(branch_index),
                    "level_m": round(level, 3),
                    "point_utm": pair["point_utm"],
                    "point_wgs84": pair["point_wgs84"],
                    "geometry_wgs84": pair["geometry_wgs84"],
                    "channel_elevation_m": round(float(best["channel_elevation_m"]), 3),
                    "channel_elevation_raw_m": round(raw_elevation, 3)
                    if math.isfinite(raw_elevation)
                    else None,
                    "fill_depth_at_crossing_m": round(float(best["channel_elevation_m"]) - raw_elevation, 3)
                    if math.isfinite(raw_elevation)
                    else None,
                    "level_residual_m": round(float(best["channel_elevation_m"]) - level, 3),
                    "rowcol": (row, col),
                    "branch_rowcol": branch_rowcol,
                    "along_branch_m": round(float(best["along_branch_m"]), 3),
                    "cluster_size": len(members),
                    "cluster_along_branch_m": sorted(round(float(m["along_branch_m"]), 3) for m in members),
                    "collinear_overlap": bool(collinear_any),
                }
            )

    crossings.sort(key=lambda c: (c["valley_id"], c["branch_index"]))
    return crossings


def _cell_edges(dem: dict, row: int, col: int) -> dict:
    """
    The four boundary segments of one cell's ground square, keyed by the
    4-neighbour they face, as ((x0, y0), (x1, y1)) endpoint pairs.

    Corners come from `origin +/- N * resolution` DIRECTLY, never from
    pixel_center_xy() offset by half a cell. This is
    raster_grid.cell_union_footprint()'s own GRID-SEAM FIX, and it is
    load-bearing here for a second reason on top of that function's: the
    open edges are CHAINED into polylines by matching endpoints, and two
    adjacent edges only match if their shared corner is bit-for-bit
    identical. Computing a corner two different ways at realistic
    large-magnitude UTM values does not produce the same float, and the
    chain would silently break into single-edge fragments.
    """
    px, py = dem["resolution_meters"]
    x0 = dem["origin_x"] + col * px
    x1 = dem["origin_x"] + (col + 1) * px
    y1 = dem["origin_y"] - row * py
    y0 = dem["origin_y"] - (row + 1) * py
    return {
        (-1, 0): ((x0, y1), (x1, y1)),   # north edge
        (1, 0): ((x0, y0), (x1, y0)),    # south edge
        (0, -1): ((x0, y0), (x0, y1)),   # west edge
        (0, 1): ((x1, y0), (x1, y1)),    # east edge
    }


def _chain_edges(edges: list[tuple]) -> list[list[tuple[float, float]]]:
    """
    Chains a set of ((x0, y0), (x1, y1)) segments into ORDERED polylines by
    matching endpoints exactly (see _cell_edges() for why exact matching is
    sound here).

    Walks from every odd-degree endpoint first, so open runs come out as
    single polylines rather than being cut in half by an arbitrary start;
    whatever remains after that is a closed ring and is walked from any of
    its vertices. Each segment is consumed exactly once, so the returned
    polylines partition the input and total length is conserved -- which is
    what lets the caller report one open length and a per-segment breakdown
    that agree by construction.
    """
    adjacency: dict = {}
    remaining = set()
    for index, (a, b) in enumerate(edges):
        remaining.add(index)
        adjacency.setdefault(a, []).append((index, b))
        adjacency.setdefault(b, []).append((index, a))

    def _walk(start):
        path = [start]
        current = start
        while True:
            nxt = None
            for index, other in adjacency.get(current, ()):
                if index in remaining:
                    nxt = (index, other)
                    break
            if nxt is None:
                break
            remaining.discard(nxt[0])
            path.append(nxt[1])
            current = nxt[1]
        return path

    polylines = []
    odd_starts = sorted(
        (node for node, links in adjacency.items() if len(links) % 2 == 1),
        key=lambda node: (node[0], node[1]),
    )
    for node in odd_starts:
        while any(index in remaining for index, _ in adjacency.get(node, ())):
            path = _walk(node)
            if len(path) >= 2:
                polylines.append(path)
    # Anything left is a closed ring (every vertex even-degree).
    while remaining:
        index = min(remaining)
        start = edges[index][0]
        path = _walk(start)
        if len(path) >= 2:
            polylines.append(path)
        else:  # defensive: a walk that consumed nothing would not terminate
            remaining.discard(index)
    return polylines


def decompose_pool_perimeter(pool_mask: np.ndarray, dem: dict, waterline: float) -> dict:
    """
    Classifies every EXTERIOR edge of a delineated pool's cell mask against
    the waterline, and returns the OPEN ones as ordered polylines -- the
    proposed wall lines.

    This is the 2D level-set measurement that replaces the perpendicular
    transect. NO DIRECTION IS COMPUTED OR ASSUMED ANYWHERE: an exterior
    edge is one whose 4-neighbour is outside the pool, and it is
    GROUND-CLOSED, OPEN or UNDETERMINED purely by what the RAW DEM says
    that neighbour's elevation is (see this module's EDGE_* constants for
    the three facts and why none may be collapsed into another).

    4-NEIGHBOURS, NOT 8. A pool's perimeter is made of cell EDGES, and a
    cell shares an edge with exactly four neighbours; a diagonal neighbour
    shares only a corner, which has no length and cannot hold or release
    water. (This is the same 4-vs-8 distinction production_area.py already
    draws for its own footprints; connectivity of the pool ITSELF is
    whatever the caller's delineation used and is not re-decided here.)

    UNDETERMINED IS NOT OPEN. Where the DEM ends or reads nodata there is
    no elevation to compare, so such an edge is reported as its own length
    and is EXCLUDED from the wall lines. Drawing a proposed wall across
    ground we cannot see would be fabricating exactly the number the wall
    line exists to state. A pool with a large undetermined length is a pool
    whose measurement is incomplete, and the caller can see that.

    NO VOLUME, NO HEIGHT, NO COST. Consistent with
    valley_level_pool.py's own refusal: this returns lengths, an area, and
    a ratio between them. A wall's height, its section, and what it would
    cost are design and survey numbers this has no basis to quote, and no
    key below names one.

    Returns

        {
          'waterline_elevation_m': float,
          'open_segments': [ {points_utm, points_wgs84, line_utm,
                              geometry_wgs84, length_m, closed}, ... ],
                                             # THE PROPOSED WALL LINES,
                                             #   longest first
          'open_segment_count': int,
          'open_length_m': float,
          'ground_closed_length_m': float,
          'undetermined_length_m': float,
          'total_perimeter_m': float,        # the three lengths, summed
          'enclosure_fraction': float,       # ground_closed / total, 0..1
          'pool_cell_count': int,
          'pool_area_m2': float,
          'pool_area_acres': float,
          'pool_area_per_open_meter_m2': float or None,
                                             # pool area per METER OF WALL --
                                             #   None, never infinity, when
                                             #   there is no open perimeter
          'edge_counts': {'ground_closed': int, 'open': int, 'undetermined': int},
        }

    An empty mask returns zeros with enclosure_fraction 0.0 and
    pool_area_per_open_meter_m2 None -- the honest empty answer, not a
    division by zero.
    """
    mask = np.asarray(pool_mask, dtype=bool)
    rows, cols = mask.shape
    raw = dem["array"]
    px, py = dem["resolution_meters"]
    waterline = float(waterline)

    lengths = {EDGE_GROUND_CLOSED: 0.0, EDGE_OPEN: 0.0, EDGE_UNDETERMINED: 0.0}
    counts = {EDGE_GROUND_CLOSED: 0, EDGE_OPEN: 0, EDGE_UNDETERMINED: 0}
    open_edges: list[tuple] = []

    for r, c in np.argwhere(mask):
        r, c = int(r), int(c)
        edges = _cell_edges(dem, r, c)
        for (dr, dc), (start, end) in edges.items():
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and mask[nr, nc]:
                continue  # interior edge -- shared with another pool cell
            # A north/south edge runs east-west and is one cell WIDE (px);
            # an east/west edge runs north-south and is one cell TALL (py).
            edge_length = float(px) if dr != 0 else float(py)

            if not (0 <= nr < rows and 0 <= nc < cols):
                classification = EDGE_UNDETERMINED
            else:
                elevation = float(raw[nr, nc])
                if not math.isfinite(elevation):
                    classification = EDGE_UNDETERMINED
                elif elevation >= waterline:
                    classification = EDGE_GROUND_CLOSED
                else:
                    classification = EDGE_OPEN

            lengths[classification] += edge_length
            counts[classification] += 1
            if classification == EDGE_OPEN:
                open_edges.append((start, end))

    total = lengths[EDGE_GROUND_CLOSED] + lengths[EDGE_OPEN] + lengths[EDGE_UNDETERMINED]
    cell_count = int(mask.sum())
    area_m2 = cell_count * float(px) * float(py)

    segments = []
    for path in _chain_edges(open_edges):
        segments.append(_polyline(dem, path))
    segments.sort(key=lambda s: -s["length_m"])

    return {
        "waterline_elevation_m": round(waterline, 3),
        "open_segments": segments,
        "open_segment_count": len(segments),
        "open_length_m": round(lengths[EDGE_OPEN], 3),
        "ground_closed_length_m": round(lengths[EDGE_GROUND_CLOSED], 3),
        "undetermined_length_m": round(lengths[EDGE_UNDETERMINED], 3),
        "total_perimeter_m": round(total, 3),
        "enclosure_fraction": round(lengths[EDGE_GROUND_CLOSED] / total, 4) if total > 0 else 0.0,
        "pool_cell_count": cell_count,
        "pool_area_m2": round(area_m2, 3),
        "pool_area_acres": round(area_m2 / SQUARE_METERS_PER_ACRE, 4),
        "pool_area_per_open_meter_m2": (
            round(area_m2 / lengths[EDGE_OPEN], 3) if lengths[EDGE_OPEN] > 0 else None
        ),
        "edge_counts": {
            EDGE_GROUND_CLOSED: counts[EDGE_GROUND_CLOSED],
            EDGE_OPEN: counts[EDGE_OPEN],
            EDGE_UNDETERMINED: counts[EDGE_UNDETERMINED],
        },
    }
