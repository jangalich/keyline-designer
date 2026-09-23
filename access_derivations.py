"""
access_derivations.py

THE ACCESS SECTION'S DERIVATIONS (section V, branch 10 phase 1), computed
on the report path from the session's reads and the report layer's soil
road ratings. Everything here is in the DEM's metres and cell counts;
access_section (phase 2) converts and formats. No colour, no page text,
no network, no proposed road: the section is an INVENTORY of the access
that exists -- frontage, tracks and what the ground allows.

    access_inputs_from_context(context, document, report_data) -> AccessInputs
    derive(inputs)                                            -> AccessDerived

WHAT IS READ AND WHAT IS DERIVED. Off the session: the DEM, the boundary,
the exclusion result's slope grid, ParcelData's road rows (Layer 1's
farm_roads: name, geometry and, since this branch, the service's own
attributes), its NHD rows, its SSURGO rows and map-unit polygons. Off
the report layer: the soil road-construction ratings. Derived here:
everything the section prints.

THE EXISTING TRACKS ARE THE FEATURES THE DESIGN STEP EXCLUDES. The rows
read here are the rows the terrain warm-up buffers by farm_roads_data.
ROAD_EXCLUSION_BUFFER_METERS into the session's existing-roads union,
the union the exclusion result's roads layer is rasterised from. This
module rebuilds that union from the same rows (exclusion_union) so
test_access_derivations.py can hold the two to be equal -- and the
roads layer mask to be the union rasterised over the same universe --
rather than assume it.

FRONTAGE is the length of the boundary ring within FRONTAGE_TOLERANCE_
METERS of a mapped road centreline, merged by road name. A centreline is
not a right-of-way edge: the tolerance is a judgment (step 0), 15 m,
just above TIGER's stated 7.6 m positional accuracy and below a
township road's half right-of-way plus that error, and the page states
it. Whether a road touches is decided by the same rule: a road with no
ring within the tolerance is not frontage, and the section reports the
distance and bearing to the nearest one instead. With NO mapped road in
the fetch box the parcel reads as having no mapped public road within
the fetch distance, stated as the finding it is.

TRACKS are the parts of the mapped segments that lie ON the parcel: an
internal lane the source carries as a road. Their grade is read off the
DEM at TRACK_SAMPLE_STEP_M stations along the line -- the mean and
steepest step, and end to end -- and the slope of the ground they enter
across is the slope grid where they cross the boundary.

THE BOUNDARY'S DRIVABILITY is the exclusion result's slope grid sampled
BOUNDARY_INSET_M inside the ring at BOUNDARY_SAMPLE_STEP_M stations; a
station is undrivable at or above UNDRIVABLE_SLOPE_PCT, which is
Landform's own class D break (15%) rather than a threshold invented
here. Runs of stations become ring pieces for the map; their lengths
partition the perimeter exactly, unknown ground (a station whose cell
carries no slope) counted as its own piece.

STREAM CROSSINGS: the parcel split by its on-parcel NHD flowlines; a
piece that touches no frontage is reachable only across a stream.

THE SOIL PARTITION reuses the Water section's map-unit cell grid
(water_derivations.derive_hydric over the same reads) so the same cell
falls in the same map unit in both sections by construction, and every
acreage column sums to the parcel. A map unit's class is NRCS's
dominant condition (soil_road_ratings.dominant_condition); its limiting
features are those of the components in that class, weighted by their
share of it, then by the unit's cells on the parcel.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import nearest_points, substring, unary_union

import soil_road_ratings as srr
import water_derivations as wd
from farm_roads_data import ROAD_EXCLUSION_BUFFER_METERS
from landform_section import ASPECT_SECTORS, SLOPE_CLASSES
from raster_grid import SQUARE_METERS_PER_ACRE

# The frontage tolerance: a judgment, stated on the page (see the module docstring).
FRONTAGE_TOLERANCE_METERS = 15.0
# Landform's class D lower bound, reused rather than restated.
UNDRIVABLE_SLOPE_PCT = next(low for name, low, _ in SLOPE_CLASSES if name == "D")
BOUNDARY_SAMPLE_STEP_M = 5.0
BOUNDARY_INSET_M = 5.0
TRACK_SAMPLE_STEP_M = 5.0
# A parcel piece is reachable from frontage when it comes within this of it.
FRONTAGE_TOUCH_M = 1.0

DRIVABLE = "drivable"
UNDRIVABLE = "undrivable"
UNKNOWN = "unknown"

# The soil partition's classes, in table order, then the two states that
# are not ratings.
SOIL_NO_DATA = "no data"
SOIL_CLASSES = (srr.NOT_LIMITED, srr.SOMEWHAT_LIMITED, srr.VERY_LIMITED, srr.NOT_RATED, SOIL_NO_DATA, wd.HYDRIC_NO_POLYGON)


@dataclass
class AccessInputs:
    dem: dict
    boundary_polygon_utm: object
    slope_pct: np.ndarray
    farm_roads: list                 # ParcelData's road rows: [{'name', 'geometry', 'properties'?}]
    water_features: dict             # ParcelData's NHD rows
    soil_components: list            # ParcelData's SSURGO component rows
    soil_geometries: dict            # ParcelData's {mukey: GeoJSON geometry (WGS84)}
    parcel_acres: float
    retrieved_on: date
    soil_road_ratings: Optional[dict]   # the report layer's block, None when absent
    unavailable: dict = field(default_factory=dict)


@dataclass
class AccessDerived:
    cells: dict
    roads: list          # one dict per mapped segment
    frontage: dict
    tracks: dict
    boundary: dict
    crossings: dict
    soil: dict
    exclusion_union: object   # the rows buffered at ROAD_EXCLUSION_BUFFER_METERS, or None


# ======================================================================
# Inputs
# ======================================================================


def _created_on(document: dict) -> date:
    stamp = (document or {}).get("created_at")
    if not stamp:
        return date.today()
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()


def access_inputs_from_context(context, document: dict, report_data) -> AccessInputs:
    """Every read off the session and the report data. Raises when the
    exclusion result carries no slope grid, as the Landform and Water
    reads do."""
    exclusion = context.exclusion_zones or {}
    slope = exclusion.get("slope_pct")
    if slope is None:
        raise ValueError("the session's exclusion result carries no slope grid")
    parcel = context.parcel_data
    boundary = context.boundary_polygon_utm
    return AccessInputs(
        dem=context.dem,
        boundary_polygon_utm=boundary,
        slope_pct=slope,
        farm_roads=list(getattr(parcel, "farm_roads", None) or []),
        water_features=getattr(parcel, "water_features", None) or {"streams": [], "water_bodies": []},
        soil_components=list(getattr(parcel, "soil_components", None) or []),
        soil_geometries=dict(getattr(parcel, "soil_geometries", None) or {}),
        parcel_acres=boundary.area / SQUARE_METERS_PER_ACRE,
        retrieved_on=_created_on(document),
        soil_road_ratings=getattr(report_data, "soil_road_ratings", None),
        unavailable=dict(getattr(report_data, "unavailable", None) or {}),
    )


# ======================================================================
# Grid helpers
# ======================================================================


def _cell_of(dem: dict, x: float, y: float) -> Optional[tuple]:
    px, py = dem["resolution_meters"]
    c = int(math.floor((x - dem["origin_x"]) / px))
    r = int(math.floor((dem["origin_y"] - y) / py))
    rows, cols = dem["array"].shape
    if 0 <= r < rows and 0 <= c < cols:
        return (r, c)
    return None


def _grid_value(grid: np.ndarray, dem: dict, x: float, y: float) -> Optional[float]:
    cell = _cell_of(dem, x, y)
    if cell is None:
        return None
    value = float(grid[cell])
    return None if math.isnan(value) else value


def _bearing_deg(from_xy: tuple, to_xy: tuple) -> float:
    """Compass bearing, 0 = north, clockwise, in the DEM's projected metres."""
    dx, dy = to_xy[0] - from_xy[0], to_xy[1] - from_xy[1]
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def compass_sector(bearing_deg: float) -> str:
    return ASPECT_SECTORS[int(round((bearing_deg % 360.0) / 45.0)) % 8]


def _road_properties(row: dict) -> dict:
    return dict(row.get("properties") or {})


def road_route(properties: dict) -> Optional[str]:
    """The first route designation the row carries, if any."""
    for key in ("interstate", "us_route", "state_route", "county_route", "federal_lands_route"):
        value = properties.get(key)
        if value not in (None, ""):
            return str(value)
    return None


# ======================================================================
# Roads, frontage, tracks
# ======================================================================


def derive_roads(inputs: AccessInputs, tolerance_m: float = FRONTAGE_TOLERANCE_METERS) -> list:
    """
    One dict per mapped segment:

        {'name', 'properties', 'class', 'route', 'geometry_utm', 'length_m',
         'distance_m', 'on_parcel' (geometry | None), 'length_on_parcel_m',
         'frontage' (ring pieces | None), 'frontage_m', 'length_in_window_m'}
    """
    crs = inputs.dem["crs"]
    parcel = inputs.boundary_polygon_utm
    ring = parcel.exterior
    window = wd.adjacency_window(parcel)
    roads = []
    for row in inputs.farm_roads:
        if not row.get("geometry"):
            continue
        line = wd._to_crs(row["geometry"], crs)
        properties = _road_properties(row)
        on_parcel = wd._linear(line.intersection(parcel))
        frontage = wd._linear(ring.intersection(line.buffer(tolerance_m)))
        in_window = wd._linear(line.intersection(window))
        roads.append({
            "name": row.get("name") or "Unnamed road",
            "properties": properties,
            "class": properties.get("layer_name"),
            "route": road_route(properties),
            "geometry_utm": line,
            "length_m": float(line.length),
            "distance_m": float(parcel.distance(line)),
            "on_parcel": on_parcel,
            "length_on_parcel_m": float(on_parcel.length) if on_parcel is not None else 0.0,
            "frontage": frontage,
            "frontage_m": float(frontage.length) if frontage is not None else 0.0,
            "length_in_window_m": float(in_window.length) if in_window is not None else 0.0,
        })
    return roads


def ring_intervals(ring, geometry) -> list:
    """The stretches of `ring` (a LineString of the boundary's coordinates)
    that `geometry` (ring pieces) covers, as merged [start, end] distances
    along the ring. Intervals, not geometry, are what frontage adds up
    in: two roads whose tolerance bands overlap cover the SAME stretch of
    boundary once, and a union of their pieces as linework can keep both
    when floating point leaves them a hair off collinear."""
    if geometry is None or geometry.is_empty:
        return []
    parts = [geometry] if isinstance(geometry, LineString) else [g for g in getattr(geometry, "geoms", []) if isinstance(g, LineString)]
    length = float(ring.length)
    raw = []
    for part in parts:
        if part.length <= 0:
            continue
        stations = [ring.project(Point(c)) for c in part.coords]
        # Consecutive coordinates, so a piece that runs through the ring's
        # origin splits there rather than spanning the whole ring.
        for s0, s1 in zip(stations, stations[1:]):
            low, high = min(s0, s1), max(s0, s1)
            if high - low <= length / 2:
                raw.append((low, high))
            else:
                raw.append((high, length))
                raw.append((0.0, low))
    return merge_intervals(raw)


def merge_intervals(intervals: list) -> list:
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def intervals_overlap_m(a: list, b: list) -> float:
    """The length two interval lists share along the ring."""
    total = 0.0
    for a0, a1 in a:
        for b0, b1 in b:
            total += max(0.0, min(a1, b1) - max(a0, b0))
    return total


def intervals_geometry(ring, intervals: list):
    pieces = [substring(ring, a, b) for a, b in intervals if b > a]
    pieces = [p for p in pieces if isinstance(p, LineString) and p.length > 0]
    return None if not pieces else (pieces[0] if len(pieces) == 1 else MultiLineString(pieces))


def derive_frontage(inputs: AccessInputs, roads: list, tolerance_m: float = FRONTAGE_TOLERANCE_METERS) -> dict:
    """
    Frontage merged by road name, and the nearest road when there is none:

        {'tolerance_m', 'perimeter_m', 'roads': [{'name', 'class', 'route',
          'segments', 'length_m', 'intervals', 'geometry'}] (longest first),
         'total_m', 'intervals', 'geometry' (the boundary every road covers,
          counted ONCE where bands overlap), 'sum_of_roads_m',
         'nearest': None | {'name', 'class', 'distance_m', 'bearing_deg',
                            'sector', 'from_xy', 'to_xy'},
         'mapped_roads': n}

    Lengths are stretches of the boundary ring, as intervals along it:
    a road's frontage is the union of its segments' stretches, and the
    total is the union over every road, so it can never exceed the
    perimeter; `sum_of_roads_m` is the per-road sum, for the record.
    """
    parcel = inputs.boundary_polygon_utm
    ring = LineString(parcel.exterior.coords)
    by_name = {}
    for road in roads:
        if road["frontage"] is None:
            continue
        entry = by_name.setdefault(road["name"], {"name": road["name"], "class": road["class"], "route": road["route"],
                                                  "segments": 0, "intervals": []})
        entry["segments"] += 1
        entry["intervals"] += ring_intervals(ring, road["frontage"])
        if entry["class"] is None:
            entry["class"] = road["class"]
    merged = []
    for entry in by_name.values():
        intervals = merge_intervals(entry["intervals"])
        merged.append({"name": entry["name"], "class": entry["class"], "route": entry["route"], "segments": entry["segments"],
                       "length_m": sum(b - a for a, b in intervals), "intervals": intervals,
                       "geometry": intervals_geometry(ring, intervals)})
    merged.sort(key=lambda r: -r["length_m"])
    all_intervals = merge_intervals([i for r in merged for i in r["intervals"]])
    union = intervals_geometry(ring, all_intervals) if merged else None
    total = sum(b - a for a, b in all_intervals)
    nearest = None
    if not merged and roads:
        closest = min(roads, key=lambda r: r["distance_m"])
        on_ring, on_road = nearest_points(ring, closest["geometry_utm"])
        bearing = _bearing_deg((on_ring.x, on_ring.y), (on_road.x, on_road.y))
        nearest = {"name": closest["name"], "class": closest["class"], "distance_m": closest["distance_m"],
                   "bearing_deg": bearing, "sector": compass_sector(bearing),
                   "from_xy": (on_ring.x, on_ring.y), "to_xy": (on_road.x, on_road.y)}
    return {"tolerance_m": tolerance_m, "perimeter_m": float(ring.length), "roads": merged, "total_m": total,
            "intervals": all_intervals, "geometry": union, "sum_of_roads_m": sum(r["length_m"] for r in merged),
            "nearest": nearest, "mapped_roads": len(roads)}


def _line_profile(line, dem: dict, step_m: float) -> dict:
    """Elevations at `step_m` stations along a line (the last station at
    its end), the grade of each step and the end-to-end grade."""
    if line is None or line.length <= 0:
        return {"stations_m": [], "elevations_m": [], "grades_pct": [], "mean_grade_pct": None,
                "max_grade_pct": None, "end_to_end_grade_pct": None}
    distances = list(np.arange(0.0, float(line.length), step_m)) + [float(line.length)]
    # A final step shorter than a full station is a fraction of a cell,
    # not a grade: fold it into the step before it rather than divide a
    # cell's rise by a metre or two.
    if len(distances) >= 3 and distances[-1] - distances[-2] < step_m:
        del distances[-2]
    stations, elevations = [], []
    for d in distances:
        point = line.interpolate(d)
        z = _grid_value(dem["array"], dem, point.x, point.y)
        if z is None:
            continue
        stations.append(float(d))
        elevations.append(z)
    grades = []
    for (d0, z0), (d1, z1) in zip(zip(stations, elevations), zip(stations[1:], elevations[1:])):
        if d1 - d0 > 0:
            grades.append(abs(z1 - z0) / (d1 - d0) * 100.0)
    end_to_end = None
    if len(stations) >= 2 and stations[-1] - stations[0] > 0:
        end_to_end = abs(elevations[-1] - elevations[0]) / (stations[-1] - stations[0]) * 100.0
    return {"stations_m": stations, "elevations_m": elevations, "grades_pct": grades,
            "mean_grade_pct": float(np.mean(grades)) if grades else None,
            "max_grade_pct": float(max(grades)) if grades else None,
            "end_to_end_grade_pct": end_to_end}


def _entry_slopes(line, inputs: AccessInputs) -> list:
    """The slope grid BOUNDARY_INSET_M inside the boundary where the line
    crosses it, one value per crossing (None where the cell has none)."""
    parcel = inputs.boundary_polygon_utm
    crossings = line.intersection(parcel.exterior)
    points = [crossings] if isinstance(crossings, Point) else [g for g in getattr(crossings, "geoms", []) if isinstance(g, Point)]
    slopes = []
    for point in points:
        # A point on the line just inside the parcel, BOUNDARY_INSET_M along it.
        at = line.project(point)
        candidates = [line.interpolate(min(line.length, at + BOUNDARY_INSET_M)), line.interpolate(max(0.0, at - BOUNDARY_INSET_M))]
        inside = [p for p in candidates if parcel.contains(p)]
        probe = inside[0] if inside else point
        slopes.append(_grid_value(inputs.slope_pct, inputs.dem, probe.x, probe.y))
    return slopes


def derive_tracks(inputs: AccessInputs, roads: list) -> dict:
    """
    The on-parcel parts of the mapped segments:

        {'tracks': [{'name', 'class', 'geometry', 'length_m', 'profile',
                     'entry_slopes_pct'}], 'total_m', 'count'}
    """
    tracks = []
    for road in roads:
        if road["on_parcel"] is None:
            continue
        parts = [road["on_parcel"]] if isinstance(road["on_parcel"], LineString) else list(road["on_parcel"].geoms)
        for part in parts:
            tracks.append({
                "name": road["name"],
                "class": road["class"],
                "geometry": part,
                "length_m": float(part.length),
                "profile": _line_profile(part, inputs.dem, TRACK_SAMPLE_STEP_M),
                "entry_slopes_pct": _entry_slopes(part, inputs),
            })
    tracks.sort(key=lambda t: -t["length_m"])
    return {"tracks": tracks, "total_m": sum(t["length_m"] for t in tracks), "count": len(tracks)}


# ======================================================================
# The boundary's drivability
# ======================================================================


def _inward_probe(ring, parcel, distance: float, inset_m: float) -> Point:
    """The point `inset_m` inside the parcel from the ring at `distance`
    along it, found by offsetting along the local normal on whichever
    side the parcel lies."""
    ahead = ring.interpolate(min(ring.length, distance + 0.5))
    behind = ring.interpolate(max(0.0, distance - 0.5))
    here = ring.interpolate(distance)
    dx, dy = ahead.x - behind.x, ahead.y - behind.y
    norm = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / norm, dx / norm
    left = Point(here.x + nx * inset_m, here.y + ny * inset_m)
    right = Point(here.x - nx * inset_m, here.y - ny * inset_m)
    if parcel.contains(left):
        return left
    if parcel.contains(right):
        return right
    return here


def derive_boundary(inputs: AccessInputs, frontage: dict, step_m: float = BOUNDARY_SAMPLE_STEP_M,
                    inset_m: float = BOUNDARY_INSET_M, threshold_pct: float = UNDRIVABLE_SLOPE_PCT) -> dict:
    """
    {'threshold_pct', 'step_m', 'inset_m', 'perimeter_m',
     'stations': [{'distance_m', 'slope_pct' | None, 'state'}],
     'runs': [{'state', 'start_m', 'end_m', 'length_m', 'geometry'}],
     'lengths_m': {state: m}  -- a partition of the perimeter,
     'frontage_lengths_m': {state: m}  -- of the frontage ring pieces,
     'edges': [{'index', 'length_m', 'mean_slope_pct', 'max_slope_pct',
                'undrivable_share', 'frontage_m'}]}
    """
    parcel = inputs.boundary_polygon_utm
    ring = LineString(parcel.exterior.coords)
    length = float(ring.length)
    count = max(1, int(math.ceil(length / step_m)))
    stations = []
    for k in range(count):
        d = min(length, k * step_m)
        probe = _inward_probe(ring, parcel, d, inset_m)
        slope = _grid_value(inputs.slope_pct, inputs.dem, probe.x, probe.y)
        state = UNKNOWN if slope is None else (UNDRIVABLE if slope >= threshold_pct else DRIVABLE)
        stations.append({"distance_m": d, "slope_pct": slope, "state": state})
    # Runs of one state; station k stands for the ring between (k-0.5) and (k+0.5) steps.
    runs = []
    for index, station in enumerate(stations):
        if runs and runs[-1]["state"] == station["state"]:
            runs[-1]["last"] = index
        else:
            runs.append({"state": station["state"], "first": index, "last": index})
    pieces = []
    for run in runs:
        start = max(0.0, (run["first"] - 0.5) * step_m)
        end = min(length, (run["last"] + 0.5) * step_m)
        if run is runs[-1]:
            end = length
        if run is runs[0]:
            start = 0.0
        geometry = substring(ring, start, end)
        pieces.append({"state": run["state"], "start_m": start, "end_m": end, "length_m": end - start,
                       "geometry": geometry if isinstance(geometry, LineString) and geometry.length > 0 else None})
    lengths = {DRIVABLE: 0.0, UNDRIVABLE: 0.0, UNKNOWN: 0.0}
    for piece in pieces:
        lengths[piece["state"]] += piece["length_m"]
    # The frontage's drivable and undrivable lengths, as intervals along the ring: exact, and a partition of the frontage.
    frontage_lengths = {DRIVABLE: 0.0, UNDRIVABLE: 0.0, UNKNOWN: 0.0}
    frontage_intervals = frontage.get("intervals") or []
    for piece in pieces:
        frontage_lengths[piece["state"]] += intervals_overlap_m([(piece["start_m"], piece["end_m"])], frontage_intervals)
    # Per drawn edge: the stations whose distance falls on it.
    edges = []
    coords = list(parcel.exterior.coords)
    cursor = 0.0
    for index in range(len(coords) - 1):
        edge = LineString([coords[index], coords[index + 1]])
        if edge.length < 1e-6:
            continue
        start, end = cursor, cursor + float(edge.length)
        cursor = end
        on_edge = [s for s in stations if start <= s["distance_m"] < end and s["slope_pct"] is not None]
        values = [s["slope_pct"] for s in on_edge]
        frontage_m = intervals_overlap_m([(start, end)], frontage_intervals)
        edges.append({
            "index": index, "length_m": float(edge.length),
            "mean_slope_pct": float(np.mean(values)) if values else None,
            "max_slope_pct": float(max(values)) if values else None,
            "undrivable_share": (sum(1 for s in on_edge if s["state"] == UNDRIVABLE) / len(on_edge)) if on_edge else None,
            "frontage_m": frontage_m,
            "midpoint_bearing_deg": _bearing_deg((parcel.centroid.x, parcel.centroid.y), (edge.centroid.x, edge.centroid.y)),
        })
    return {"threshold_pct": threshold_pct, "step_m": step_m, "inset_m": inset_m, "perimeter_m": length,
            "stations": stations, "runs": pieces, "lengths_m": lengths, "frontage_lengths_m": frontage_lengths, "edges": edges}


# ======================================================================
# Stream crossings
# ======================================================================


def derive_crossings(inputs: AccessInputs, frontage: dict) -> dict:
    """
    {'streams_on_parcel': [{'name', 'geometry', 'length_m'}],
     'pieces': [{'area_m2', 'reachable': bool}], 'crossings_needed': n,
     'beyond_crossing_m2': m2}
    """
    crs = inputs.dem["crs"]
    parcel = inputs.boundary_polygon_utm
    streams = []
    for row in inputs.water_features.get("streams") or []:
        if not row.get("geometry"):
            continue
        line = wd._to_crs(row["geometry"], crs)
        on_parcel = wd._linear(line.intersection(parcel))
        if on_parcel is not None and on_parcel.length > 0:
            streams.append({"name": row.get("name"), "geometry": on_parcel, "length_m": float(on_parcel.length)})
    if not streams:
        return {"streams_on_parcel": [], "pieces": [], "crossings_needed": 0, "beyond_crossing_m2": 0.0}
    cut = unary_union([s["geometry"] for s in streams]).buffer(0.01)
    remainder = parcel.difference(cut)
    polygons = [remainder] if remainder.geom_type == "Polygon" else [g for g in getattr(remainder, "geoms", []) if g.geom_type == "Polygon"]
    pieces = []
    frontage_geometry = frontage.get("geometry")
    for polygon in polygons:
        reachable = frontage_geometry is not None and polygon.distance(frontage_geometry) <= FRONTAGE_TOUCH_M
        pieces.append({"area_m2": float(polygon.area), "reachable": reachable, "geometry": polygon})
    beyond = [p for p in pieces if not p["reachable"]]
    return {"streams_on_parcel": streams, "pieces": pieces, "crossings_needed": len(beyond),
            "beyond_crossing_m2": sum(p["area_m2"] for p in beyond)}


# ======================================================================
# The soil partition
# ======================================================================


def derive_soil(inputs: AccessInputs, cells: dict, hydric: dict) -> dict:
    """
    {'fetched': bool,
     'map_units': {mukey: {'muname', 'cells', 'paved', 'unpaved', 'roadfill',
                           'features': {name: share}}},
     'counts': {class: cells}   -- a partition of the on-parcel cells over
                                   SOIL_CLASSES,
     'features': {class: {name: cells}}   -- ground in the class affected
                                            by each feature,
     'roadfill_counts': {class: cells}, 'unpaved_differs': [mukey, ...],
     'survey_areas': [...]}

    `hydric` is water_derivations.derive_hydric()'s block over the same
    reads: its map_units carry each unit's cells on the parcel and its
    counts the cells no survey polygon covers.
    """
    block = inputs.soil_road_ratings
    counts = {name: 0 for name in SOIL_CLASSES}
    counts[wd.HYDRIC_NO_POLYGON] = hydric["counts"][wd.HYDRIC_NO_POLYGON]
    map_units = {}
    features = {name: {} for name in srr.LIMITATION_CLASSES}
    roadfill_counts = {name: 0 for name in srr.ROADFILL_CLASSES + (srr.NOT_RATED, SOIL_NO_DATA, wd.HYDRIC_NO_POLYGON)}
    roadfill_counts[wd.HYDRIC_NO_POLYGON] = counts[wd.HYDRIC_NO_POLYGON]
    unpaved_differs = []
    for mukey, unit in hydric["map_units"].items():
        unit_cells = unit["cells"]
        source = (block or {}).get("map_units", {}).get(mukey)
        if block is None or source is None:
            map_units[mukey] = {"muname": unit["muname"], "cells": unit_cells, "paved": None, "unpaved": None,
                                "roadfill": None, "features": {}}
            counts[SOIL_NO_DATA] += unit_cells
            roadfill_counts[SOIL_NO_DATA] += unit_cells
            continue
        components = [block["components"][cokey] for cokey in source["components"]]
        paved = srr.dominant_condition(components, srr.LOCAL_ROADS)
        unpaved = srr.dominant_condition(components, srr.UNPAVED_LOCAL_ROADS)
        roadfill = srr.dominant_condition(components, srr.ROADFILL, srr.ROADFILL_CLASSES)
        unit_features = srr.class_features(paved["components"], srr.LOCAL_ROADS) if paved["class"] in srr.LIMITATION_CLASSES else {}
        map_units[mukey] = {"muname": unit["muname"], "cells": unit_cells, "paved": paved, "unpaved": unpaved,
                            "roadfill": roadfill, "features": unit_features}
        paved_class = paved["class"] if paved["class"] in counts else (SOIL_NO_DATA if paved["class"] is None else srr.NOT_RATED)
        counts[paved_class] += unit_cells
        if paved_class in features:
            for name, share in unit_features.items():
                features[paved_class][name] = features[paved_class].get(name, 0.0) + share * unit_cells
        fill_class = roadfill["class"] if roadfill["class"] in roadfill_counts else (SOIL_NO_DATA if roadfill["class"] is None else srr.NOT_RATED)
        roadfill_counts[fill_class] += unit_cells
        if unpaved["class"] != paved["class"]:
            unpaved_differs.append(mukey)
    for name in features:
        features[name] = dict(sorted(features[name].items(), key=lambda kv: -kv[1]))
    return {
        "fetched": block is not None,
        "map_units": map_units,
        "counts": counts,
        "features": features,
        "roadfill_counts": roadfill_counts,
        "unpaved_differs": unpaved_differs,
        "survey_areas": list((block or {}).get("survey_areas") or []),
    }


# ======================================================================
# The exclusion union, rebuilt from the same rows
# ======================================================================


def exclusion_union(inputs: AccessInputs, roads: list):
    """The rows buffered by ROAD_EXCLUSION_BUFFER_METERS and unioned -- the
    session's existing-roads geometry rebuilt from the section's own
    reading of the rows, for the agreement test. None with no road."""
    if not roads:
        return None
    union = unary_union([road["geometry_utm"].buffer(ROAD_EXCLUSION_BUFFER_METERS) for road in roads])
    return None if union.is_empty else union


# ======================================================================
# derive()
# ======================================================================


def derive(inputs: AccessInputs) -> AccessDerived:
    cells = wd.grid_bookkeeping(inputs)
    hydric = wd.derive_hydric(inputs, cells)
    roads = derive_roads(inputs)
    frontage = derive_frontage(inputs, roads)
    tracks = derive_tracks(inputs, roads)
    boundary = derive_boundary(inputs, frontage)
    crossings = derive_crossings(inputs, frontage)
    soil = derive_soil(inputs, cells, hydric)
    return AccessDerived(
        cells=cells, roads=roads, frontage=frontage, tracks=tracks, boundary=boundary, crossings=crossings, soil=soil,
        exclusion_union=exclusion_union(inputs, roads),
    )
