"""
landform_derivations.py

THE LANDFORM SECTION'S OWN DERIVATIONS -- ridges, keylines, the valley
profile and the valley rows -- computed on the report path from the
session's terrain reads (landform_section.TerrainInputs).

    derive(terrain) -> TerrainDerived

WHY HERE AND NOT IN THE WARM-UP. The step registry's consumes edges name
what each KSOP step reads: roads consumes valleys, and no step consumes
keypoints, ridges or keylines. A ridge or a keyline is read by the report
alone, so it is derived by the report -- publishing it from the terrain
warm-up would be a Layer 2 change for a report-only need. The valleys,
keypoints and slope grid stay READS off the session (test_landform_
section.py's call counts hold that); what this module adds is derived
from those reads plus the DEM.

ONE RECOMPUTATION, STATED. The session context keeps valleys and
keypoints but not the filled surface or the flow arrays they came from,
and both the ridges and the valley stem need those. They are recomputed
here ONCE per report (fill_and_resolve(), compute_flow_direction(),
compute_flow_accumulation(): ~70 ms on the reference grid) and every
derivation below reads that one pass. test_landform_derivations.py counts
the calls.

RIDGES ARE THE DIVIDES BETWEEN THE VALLEYS' DRAINAGE AREAS. Every cell is
walked down the flow field to the first cell that belongs to a delineated
valley and takes that valley's id; a cell that leaves the DEM window
before reaching one is labelled OFF_WINDOW -- it drains to something the
window does not hold. The labelled areas are polygonised (rasterio's
shapes over the cell grid) and a ridge is the boundary two of them share,
including the boundary a valley's area shares with the off-window
drainage: that divide is a real ridge, and it is drawn the same. Chosen
over flow accumulation on the inverted surface, which on the reference
parcel drew 1,919 m of branching spurs down every nose against these
735 m of divides; the divides are consistent with the valleys by
construction and give the keylines their extent. The cell-edge staircase
is rounded once for the page (Chaikin, RIDGE_SMOOTH_ITERATIONS) and the
rounded line is what the map draws and the parcel clips; it stays within
half a cell of the cell-edge divide.

A KEYLINE IS THE CONTOUR THROUGH THE KEYPOINT, out to the ridges either
side of its valley. Per keypoint: the contour at the keypoint's elevation
is taken from the raw DEM (contourpy, the same generator as the contour
lines), the piece passing through the keypoint is kept, it is clipped to
the keypoint valley's drainage area (so it ends at the divides -- the
ridges -- and nowhere else) and then to the parcel for the page. NO
KEYPOINT, NO KEYLINE: a valley without a keypoint has no keyline, and the
section says so in those words rather than drawing a contour in its
place. A keypoint just outside the drawn boundary (within the detector's
margin) still has its keyline drawn where it crosses the parcel; the
keypoint itself is drawn and counted as outside.

THE PROFILE is the primary valley's main stem -- the valley with the
largest contributing area among those whose stem crosses the parcel --
traced from the outlet up to the ridge exactly as the keypoint detector
traces it (keypoint_detection.trace_stem_from_outlet()), with the RAW
elevation at each cell against the ground distance along the stem, the
keypoint marked where the detector put it, and a tick where the stem
crosses the parcel boundary. The grades either side of the keypoint are
the detector's own (slope_above_pct / slope_below_pct), never refitted.

THE VALLEY ROWS: one per valley whose main stem crosses the parcel, in
descending order of contributing area (the profile's valley first). The
length is the stem's length ON THE PARCEL; the fall is the elevation
difference between where the stem enters and leaves; the average grade
is fall over length. Keypoint columns read a dash for a valley without
one. A valley whose stem never touches the parcel gets no row.

Everything here is in the DEM's metres; landform_section converts and
formats. No colour, no text for the page, no network.
"""

import itertools
import math
from dataclasses import dataclass, field
from typing import Optional

import contourpy
import numpy as np
from rasterio import features
from rasterio.transform import from_origin
from shapely.geometry import LineString, MultiLineString, Point, Polygon, shape
from shapely.ops import linemerge, unary_union

import keypoint_detection
import valley_delineation
from contour_lines import _grid_axes
from raster_grid import chaikin_smooth_coords, pixel_center_xy

OFF_WINDOW = -1
RIDGE_SMOOTH_ITERATIONS = 2
# A keyline's contour must pass within this many cells of the keypoint's
# cell centre; the contour is taken at the keypoint's own (rounded) raw
# elevation, so the miss is the rounding plus float32, ~0.1 m in practice.
KEYLINE_CONTOUR_TOLERANCE_CELLS = 0.5
# The drainage-area clip is padded by this so the keyline reaches the
# divide rather than stopping a hair short of the cell edge.
KEYLINE_CLIP_PAD_M = 0.5


@dataclass
class TerrainDerived:
    filled: np.ndarray
    flow_to_row: np.ndarray
    flow_to_col: np.ndarray
    accumulation: np.ndarray
    labels: np.ndarray                  # catchment label per cell: a valley id or OFF_WINDOW
    catchments: dict                    # label -> Polygon/MultiPolygon in the DEM's CRS
    ridges: list                        # ridge_divides()' dicts
    stems: dict                         # valley id -> [(row, col), ...] upstream -> downstream
    keylines: list                      # keyline_for()'s dicts, one per keypoint
    primary_valley_id: Optional[int]
    profile: Optional[dict]             # stem_profile()'s dict for the primary valley
    valley_rows: list                   # valley_rows()' dicts
    keypoint_counts: dict = field(default_factory=dict)


# ======================================================================
# The one flow pass
# ======================================================================


def flow_pass(dem: dict) -> tuple:
    return valley_delineation.flow_pass(dem)


# ======================================================================
# Catchments and ridges
# ======================================================================


def catchment_labels(valleys: list, flow_to_row: np.ndarray, flow_to_col: np.ndarray) -> np.ndarray:
    """Each cell's label: the id of the delineated valley its flow first
    reaches, or OFF_WINDOW when the flow leaves the grid first."""
    rows, cols = flow_to_row.shape
    labels = np.full((rows, cols), -2, dtype=np.int32)
    for valley in valleys:
        for branch in valley["branches_rowcol"]:
            for r, c in branch:
                labels[r, c] = int(valley["id"])
    for r0 in range(rows):
        for c0 in range(cols):
            if labels[r0, c0] != -2:
                continue
            path = []
            r, c = r0, c0
            while labels[r, c] == -2:
                path.append((r, c))
                labels[r, c] = -3          # on the current path: a cycle guard
                tr, tc = int(flow_to_row[r, c]), int(flow_to_col[r, c])
                if tr < 0:
                    label = OFF_WINDOW
                    break
                r, c = tr, tc
            else:
                label = OFF_WINDOW if labels[r, c] == -3 else int(labels[r, c])
            for pr, pc in path:
                labels[pr, pc] = label
    return labels


def catchment_polygons(labels: np.ndarray, dem: dict) -> dict:
    """One (multi)polygon per label, over the cell grid, in the DEM's CRS."""
    resx, resy = dem["resolution_meters"]
    transform = from_origin(dem["origin_x"], dem["origin_y"], resx, resy)
    parts = {}
    for geometry, value in features.shapes(labels.astype(np.int32), transform=transform):
        parts.setdefault(int(value), []).append(shape(geometry))
    return {label: unary_union(polys) for label, polys in parts.items()}


def _linear(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, LineString):
        return geometry
    if isinstance(geometry, MultiLineString):
        return geometry
    lines = []
    for part in getattr(geometry, "geoms", []):
        if isinstance(part, LineString) and not part.is_empty:
            lines.append(part)
        elif isinstance(part, MultiLineString):
            lines.extend(p for p in part.geoms if not p.is_empty)
    if not lines:
        return None
    return lines[0] if len(lines) == 1 else MultiLineString(lines)


def _smooth(geometry, iterations: int):
    parts = [geometry] if isinstance(geometry, LineString) else list(geometry.geoms)
    smoothed = [LineString(chaikin_smooth_coords(list(p.coords), iterations)) for p in parts if len(p.coords) >= 2]
    return smoothed[0] if len(smoothed) == 1 else MultiLineString(smoothed)


def ridge_divides(catchments: dict, boundary_polygon_utm) -> list:
    """Every boundary two drainage areas share, merged, rounded and
    clipped to the parcel:

        {'between': (label_a, label_b), 'geometry': window-wide line,
         'on_parcel': the parcel's part or None, 'length_on_parcel_m'}

    One entry per pair of areas, in label order; the off-window drainage
    takes part like any valley."""
    ridges = []
    for a, b in itertools.combinations(sorted(catchments), 2):
        shared = _linear(catchments[a].boundary.intersection(catchments[b].boundary))
        if shared is None:
            continue
        merged = _linear(linemerge(shared) if isinstance(shared, MultiLineString) else shared)
        smoothed = _smooth(merged, RIDGE_SMOOTH_ITERATIONS)
        on_parcel = _linear(smoothed.intersection(boundary_polygon_utm))
        ridges.append({
            "between": (a, b),
            "geometry": smoothed,
            "on_parcel": on_parcel,
            "length_on_parcel_m": float(on_parcel.length) if on_parcel is not None else 0.0,
        })
    return ridges


# ======================================================================
# Stems and keylines
# ======================================================================


def stem_line(stem: list, dem: dict) -> Optional[LineString]:
    if len(stem) < 2:
        return None
    return LineString([pixel_center_xy(dem, r, c) for r, c in stem])


def contour_generator(dem: dict):
    x, y = _grid_axes(dem)
    return contourpy.contour_generator(x=x, y=y, z=dem["array"], line_type=contourpy.LineType.Separate)


def _nearest_part(geometry, point: Point):
    parts = [geometry] if isinstance(geometry, LineString) else list(geometry.geoms)
    return min(parts, key=lambda p: p.distance(point))


def keyline_for(keypoint: dict, dem: dict, generator, catchment, boundary_polygon_utm) -> dict:
    """The keyline of one keypoint, or the reason there is none:

        {'keypoint_id', 'valley_id', 'elevation_m', 'keypoint_on_parcel',
         'geometry': the line across the valley's drainage area (window-wide),
         'on_parcel': the parcel's part or None, 'length_on_parcel_m',
         'contour_miss_m': how far the contour passes from the keypoint,
         'ends': [{'point', 'to_divide_m', 'at_window_edge'}, ...],
         'reason': None | why no line}"""
    point = keypoint["point_utm"]
    elevation = float(keypoint["elevation_m"])
    result = {
        "keypoint_id": keypoint["id"], "valley_id": keypoint["valley_id"], "elevation_m": elevation,
        "keypoint_on_parcel": bool(keypoint.get("on_parcel")),
        "geometry": None, "on_parcel": None, "length_on_parcel_m": 0.0,
        "contour_miss_m": None, "ends": [], "reason": None,
    }
    pieces = [LineString(seg) for seg in generator.lines(elevation) if len(seg) >= 2]
    if not pieces:
        result["reason"] = "no contour at the keypoint's elevation"
        return result
    tolerance = KEYLINE_CONTOUR_TOLERANCE_CELLS * max(dem["resolution_meters"])
    nearest = min(pieces, key=lambda p: p.distance(point))
    result["contour_miss_m"] = float(nearest.distance(point))
    if result["contour_miss_m"] > tolerance:
        result["reason"] = "the contour at the keypoint's elevation does not pass through the keypoint"
        return result
    if catchment is None:
        result["reason"] = "the keypoint's valley has no drainage area"
        return result
    within = _linear(nearest.intersection(catchment.buffer(KEYLINE_CLIP_PAD_M)))
    if within is None:
        result["reason"] = "the contour leaves the valley's drainage area at once"
        return result
    line = _nearest_part(within, point)
    result["geometry"] = line
    resx, resy = dem["resolution_meters"]
    rows, cols = dem["array"].shape
    x_edge = (dem["origin_x"] + resx / 2, dem["origin_x"] + (cols - 0.5) * resx)
    y_edge = (dem["origin_y"] - (rows - 0.5) * resy, dem["origin_y"] - resy / 2)
    for end in (Point(line.coords[0]), Point(line.coords[-1])):
        at_edge = (min(abs(end.x - x_edge[0]), abs(end.x - x_edge[1])) < 1e-6
                   or min(abs(end.y - y_edge[0]), abs(end.y - y_edge[1])) < 1e-6)
        result["ends"].append({
            "point": end,
            "to_divide_m": float(catchment.boundary.distance(end)),
            "at_window_edge": bool(at_edge),
        })
    on_parcel = _linear(line.intersection(boundary_polygon_utm))
    result["on_parcel"] = on_parcel
    result["length_on_parcel_m"] = float(on_parcel.length) if on_parcel is not None else 0.0
    return result


# ======================================================================
# The profile and the rows
# ======================================================================


def _crossings(stem_xy: list, cumulative: np.ndarray, boundary_polygon_utm) -> list:
    """Where the stem crosses the parcel boundary, as distances along the
    stem with the direction of travel."""
    ring = boundary_polygon_utm.exterior
    crossings = []
    for i in range(len(stem_xy) - 1):
        segment = LineString([stem_xy[i], stem_xy[i + 1]])
        hit = segment.intersection(ring)
        if hit.is_empty:
            continue
        points = [hit] if isinstance(hit, Point) else [g for g in getattr(hit, "geoms", []) if isinstance(g, Point)]
        for p in points:
            along = segment.project(p)
            entering = boundary_polygon_utm.contains(Point(stem_xy[i + 1])) and not boundary_polygon_utm.contains(Point(stem_xy[i]))
            crossings.append({"distance_m": float(cumulative[i] + along), "entering": bool(entering)})
    crossings.sort(key=lambda c: c["distance_m"])
    return crossings


def stem_profile(valley: dict, stem: list, dem: dict, boundary_polygon_utm, keypoint: Optional[dict]) -> dict:
    """The long profile of one valley's main stem, upstream first:

        {'valley_id', 'distance_m': [...], 'elevation_m': [...] (raw),
         'on_parcel': [bool per cell], 'crossings': [{distance_m, entering}],
         'keypoint': None | {'index', 'distance_m', 'elevation_m',
                             'grade_above_pct', 'grade_below_pct', 'on_parcel'}}"""
    xy = [pixel_center_xy(dem, r, c) for r, c in stem]
    elevation = [float(dem["array"][r, c]) for r, c in stem]
    cumulative = np.zeros(len(stem))
    for i in range(1, len(stem)):
        cumulative[i] = cumulative[i - 1] + math.dist(xy[i - 1], xy[i])
    on_parcel = [bool(boundary_polygon_utm.contains(Point(p))) for p in xy]
    marked = None
    if keypoint is not None:
        k = int(keypoint["position_along_stem"])
        if 0 <= k < len(stem) and tuple(stem[k]) == tuple(keypoint["rowcol"]):
            marked = {
                "index": k, "distance_m": float(cumulative[k]), "elevation_m": float(keypoint["elevation_m"]),
                "grade_above_pct": float(keypoint["slope_above_pct"]),
                "grade_below_pct": float(keypoint["slope_below_pct"]),
                "on_parcel": bool(keypoint.get("on_parcel")),
                "distance_outside_boundary_m": float(keypoint.get("distance_outside_boundary_m") or 0.0),
            }
    return {
        "valley_id": int(valley["id"]),
        "distance_m": [float(d) for d in cumulative],
        "elevation_m": elevation,
        "on_parcel": on_parcel,
        "crossings": _crossings(xy, cumulative, boundary_polygon_utm),
        "keypoint": marked,
    }


def stem_on_parcel(stem: list, dem: dict, boundary_polygon_utm) -> dict:
    """The stem's parts on the parcel: length and fall (entry elevation
    minus exit elevation, summed over the parts, raw elevation interpolated
    along the stem)."""
    line = stem_line(stem, dem)
    if line is None:
        return {"length_m": 0.0, "fall_m": 0.0, "parts": 0}
    clipped = _linear(line.intersection(boundary_polygon_utm))
    if clipped is None:
        return {"length_m": 0.0, "fall_m": 0.0, "parts": 0}
    elevation = np.array([float(dem["array"][r, c]) for r, c in stem])
    along = np.array([line.project(Point(p)) for p in line.coords])
    parts = [clipped] if isinstance(clipped, LineString) else list(clipped.geoms)
    fall = 0.0
    for part in parts:
        start, end = Point(part.coords[0]), Point(part.coords[-1])
        z0 = float(np.interp(line.project(start), along, elevation))
        z1 = float(np.interp(line.project(end), along, elevation))
        fall += z0 - z1
    return {"length_m": float(clipped.length), "fall_m": float(fall), "parts": len(parts)}


def valley_rows(valleys: list, stems: dict, keypoints: list, dem: dict, boundary_polygon_utm) -> list:
    by_valley = {int(kp["valley_id"]): kp for kp in keypoints}
    rows = []
    for valley in valleys:
        vid = int(valley["id"])
        measure = stem_on_parcel(stems.get(vid, []), dem, boundary_polygon_utm)
        if measure["length_m"] <= 0.0:
            continue
        kp = by_valley.get(vid)
        rows.append({
            "valley_id": vid,
            "contributing_acres": float(valley["max_contributing_area_acres"]),
            "length_m": measure["length_m"],
            "fall_m": measure["fall_m"],
            "grade_pct": measure["fall_m"] / measure["length_m"] * 100.0,
            "keypoint": None if kp is None else {
                "id": kp["id"], "elevation_m": float(kp["elevation_m"]),
                "grade_above_pct": float(kp["slope_above_pct"]), "grade_below_pct": float(kp["slope_below_pct"]),
                "on_parcel": bool(kp.get("on_parcel")),
                "distance_outside_boundary_m": float(kp.get("distance_outside_boundary_m") or 0.0),
            },
        })
    rows.sort(key=lambda r: (-r["contributing_acres"], r["valley_id"]))
    for number, row in enumerate(rows, start=1):
        row["number"] = number
    return rows


# ======================================================================
# derive()
# ======================================================================


def derive(terrain) -> TerrainDerived:
    dem = terrain.dem
    boundary = terrain.boundary_polygon_utm
    valleys = list(terrain.valleys)
    keypoints = list(terrain.keypoints)

    filled, flow_to_row, flow_to_col, accumulation = flow_pass(dem)
    labels = catchment_labels(valleys, flow_to_row, flow_to_col)
    catchments = catchment_polygons(labels, dem)
    ridges = ridge_divides(catchments, boundary)

    upstream = keypoint_detection.build_upstream_map(flow_to_row, flow_to_col)
    stems = {int(v["id"]): keypoint_detection.trace_stem_from_outlet(v, upstream, accumulation) for v in valleys}

    generator = contour_generator(dem)
    keylines = [keyline_for(kp, dem, generator, catchments.get(int(kp["valley_id"])), boundary) for kp in keypoints]

    rows = valley_rows(valleys, stems, keypoints, dem, boundary)
    primary = rows[0]["valley_id"] if rows else None
    profile = None
    if primary is not None:
        valley = next(v for v in valleys if int(v["id"]) == primary)
        keypoint = next((kp for kp in keypoints if int(kp["valley_id"]) == primary), None)
        profile = stem_profile(valley, stems[primary], dem, boundary, keypoint)

    outside = sum(1 for kp in keypoints if not kp.get("on_parcel"))
    counts = {"detected": len(keypoints), "on_parcel": len(keypoints) - outside, "outside": outside,
              "keylines_on_parcel": sum(1 for k in keylines if k["on_parcel"] is not None),
              "valleys_on_parcel": len(rows),
              "ridges_on_parcel": sum(1 for r in ridges if r["on_parcel"] is not None)}
    return TerrainDerived(
        filled=filled, flow_to_row=flow_to_row, flow_to_col=flow_to_col, accumulation=accumulation,
        labels=labels, catchments=catchments, ridges=ridges, stems=stems, keylines=keylines,
        primary_valley_id=primary, profile=profile, valley_rows=rows, keypoint_counts=counts,
    )
