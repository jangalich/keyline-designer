"""
overview_derivations.py

SECTION I, SITE OVERVIEW: EVERY FIGURE, derived from the session's own
reads and the report data -- no fetch here.

    overview_inputs_from_context(context, document, report_data) -> OverviewInputs
    derive(inputs)                                               -> OverviewDerived

WHAT THE OVERVIEW STATES, AND WHERE EACH FIGURE COMES FROM:

    acreage, perimeter     the boundary polygon in the session's UTM zone --
                           the cover's acreage (landform_section.
                           TerrainInputs.parcel_acres) and the perimeter
                           Access measures frontage against (its ring
                           length), read the same way so the three agree.
    county and State       report data: census_geography, at the centroid.
    centroid               report data: the point every point-based layer
                           was fetched at.
    elevation range        the session's 5 m DEM over the parcel, by
                           report_map.parcel_contours -- Landform's own
                           lowest and highest figures, the same numbers.
    landscape position     the context DEM (the parcel's extent plus a
                           mile): where the parcel sits in the surrounding
                           relief, and how much ground drains into it.
    physiographic province bundled (physiography), at the centroid.
    buildings              report data: FEMA USA Structures footprints that
                           intersect the parcel.
    transmission line      report data: the nearest HIFLD line within five
                           miles, and the nearest with a known voltage.
    wildlife line          bundled (livestock_predators), for the State.

A FIGURE WHOSE SOURCE DEGRADED IS None, and `unavailable` names the
report-data layer so the page can say which source did not answer. The
bundled figures cannot degrade; a parcel outside what a bundle covers is
a real answer (no province mapped, State not surveyed separately).

LANDSCAPE POSITION -- THE RULE (branch 17). Weiss's slope position over a
topographic position index: for every cell of the context DEM, its
elevation minus the mean elevation within LANDSCAPE_TPI_RADIUS_M
(topographic_position.compute_tpi), standardised by the index's standard
deviation over the whole context window. The parcel's z is the mean index
over its cells divided by that deviation:

    ridge          z > 1
    upper slope    0.5 < z <= 1
    mid slope      |z| <= 0.5 and median slope > 5 degrees
    bench          |z| <= 0.5 and median slope <= 5 degrees
    valley floor   z < -0.5

(Weiss's "lower slope", -1 < z <= -0.5, is folded into valley floor, to
keep five classes a reader can picture.) THE RADIUS IS PRINTED ON THE
PAGE. Step 0 measured the reference parcel at z -0.02 at 300 m, -0.28 at
500 m and -0.55 at 800 m: mid slope at the first two, valley floor at the
third. A classification that changes with its window is a measurement
choice, and the page states the window so it does not read as a fact
about the land. 500 m because it is about the parcel's own width on
either side, and a quarter of the context map's extent.

Beside the class, two figures that do not depend on a radius: the
parcel's lowest and highest ground as percentiles of the context window's
elevations, and the ground outside the parcel whose D8 flow paths enter
it (valley_delineation's fill and flow direction over the context DEM) --
"how much drains toward it from above". When that ground reaches the edge
of the context window it is truncated, and `inflow_truncated` says the
figure is a lower bound.

THE CONTEXT MAP'S CONTOUR INTERVAL -- THE RULE. A topographic sheet's,
not a parcel map's: the smallest of CONTEXT_CONTOUR_INTERVALS_FT (10, 20,
40, 80 ft) that puts at most CONTEXT_CONTOUR_MAX_LINES levels across the
context window's relief, an index contour every CONTEXT_INDEX_EVERY
levels. For the reference parcel's 317 ft of relief that is 20 ft with
100 ft index lines -- the USGS 7.5-minute quadrangle convention for this
country, which is the look the map is after.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np
from rasterio.warp import transform_geom
from shapely.geometry import LineString, shape

import livestock_predators
import physiography
import report_map
import topographic_position
import valley_delineation
from contour_lines import compute_contour_lines
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres, cells_in_polygon
from terrain_metrics import compute_slope_and_aspect

METERS_PER_FOOT = 0.3048
METERS_PER_MILE = 1609.344

LANDSCAPE_TPI_RADIUS_M = 500.0
LANDSCAPE_BENCH_MAX_SLOPE_DEG = 5.0
LANDSCAPE_CLASSES = ("ridge", "upper slope", "mid slope", "bench", "valley floor")

CONTEXT_CONTOUR_INTERVALS_FT = (10, 20, 40, 80)
CONTEXT_CONTOUR_MAX_LINES = 20
CONTEXT_INDEX_EVERY = 5


@dataclass
class OverviewInputs:
    boundary: list                  # (lon, lat) ring
    boundary_polygon_utm: object
    dem: dict                       # the session's 5 m DEM
    parcel_acres: float
    retrieved_on: date              # Layer 1's: the document's created_at
    centroid: tuple                 # (lat, lon), the report data's point
    context_dem: Optional[dict] = None
    context_water: Optional[dict] = None
    context_roads: Optional[list] = None
    county_state: Optional[dict] = None
    structures: Optional[dict] = None
    transmission_lines: Optional[dict] = None
    report_retrieved_on: Optional[date] = None
    unavailable: dict = field(default_factory=dict)


@dataclass
class OverviewDerived:
    acres: float
    perimeter_m: float
    centroid: tuple
    county_state: Optional[dict]
    elevation: dict
    landscape: Optional[dict]
    context_contours: Optional[dict]
    physiography: Optional[dict]
    buildings: Optional[dict]
    transmission: Optional[dict]
    wildlife: Optional[dict]
    unavailable: dict


def _created_on(document: dict) -> date:
    stamp = document.get("created_at")
    if not stamp:
        return date.today()
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()


def overview_inputs_from_context(context, document: dict, report_data) -> OverviewInputs:
    boundary = context.boundary_polygon_utm
    return OverviewInputs(
        boundary=list(document["boundary"]) if isinstance(document.get("boundary"), list) else list(report_data.boundary),
        boundary_polygon_utm=boundary,
        dem=context.dem,
        parcel_acres=boundary.area / SQUARE_METERS_PER_ACRE,
        retrieved_on=_created_on(document),
        centroid=tuple(report_data.centroid),
        context_dem=report_data.context_dem,
        context_water=report_data.context_water,
        context_roads=report_data.context_roads,
        county_state=report_data.county_state,
        structures=report_data.structures,
        transmission_lines=report_data.transmission_lines,
        report_retrieved_on=report_data.retrieved_on,
        unavailable=dict(report_data.unavailable or {}),
    )


# ======================================================================
# The context DEM
# ======================================================================


def _parcel_mask(dem: dict, polygon) -> np.ndarray:
    mask = np.zeros(dem["array"].shape, dtype=bool)
    for r, c in cells_in_polygon(dem, polygon):
        mask[r, c] = True
    return mask


def landscape_class(z: float, median_slope_deg: float) -> str:
    if z > 1.0:
        return "ridge"
    if z > 0.5:
        return "upper slope"
    if z >= -0.5:
        return "mid slope" if median_slope_deg > LANDSCAPE_BENCH_MAX_SLOPE_DEG else "bench"
    return "valley floor"


def upslope_inflow(dem: dict, mask: np.ndarray) -> dict:
    """The cells outside `mask` whose D8 flow path enters it: their count
    and acreage, and whether any lies on the window's edge (in which case
    the true contributing ground may run past the window)."""
    filled = valley_delineation.fill_and_resolve(dem["array"])
    to_row, to_col = valley_delineation.compute_flow_direction(filled, dem["resolution_meters"])
    rows, cols = filled.shape
    reaches = mask.copy()
    # Ascending elevation: a cell's downstream neighbour is decided before it is.
    order = np.argsort(np.where(np.isnan(filled), np.inf, filled), axis=None)
    for flat in order:
        r, c = divmod(int(flat), cols)
        tr, tc = to_row[r, c], to_col[r, c]
        if tr >= 0 and reaches[tr, tc]:
            reaches[r, c] = True
    outside = reaches & ~mask
    edge = bool(outside[0].any() or outside[-1].any() or outside[:, 0].any() or outside[:, -1].any())
    cells = int(outside.sum())
    return {"cells": cells, "acres": cells * cell_area_acres(dem), "truncated": edge}


def derive_landscape(context_dem: dict, polygon) -> Optional[dict]:
    array = context_dem["array"]
    mask = _parcel_mask(context_dem, polygon)
    if not mask.any():
        return None
    tpi = topographic_position.compute_tpi(context_dem, LANDSCAPE_TPI_RADIUS_M)
    spread = float(np.nanstd(tpi))
    z = float(np.nanmean(tpi[mask]) / spread) if spread > 0 else 0.0
    slope_pct, _ = compute_slope_and_aspect(array, context_dem["resolution_meters"])
    median_slope_deg = math.degrees(math.atan(float(np.nanmedian(slope_pct[mask])) / 100.0))
    valid = array[~np.isnan(array)]
    parcel = array[mask]

    def percentile(value):
        return float((valid < value).mean() * 100.0)

    window_min, window_max = float(valid.min()), float(valid.max())
    return {
        "class": landscape_class(z, median_slope_deg),
        "z": z,
        "radius_m": LANDSCAPE_TPI_RADIUS_M,
        "tpi_sd_m": spread,
        "median_slope_deg": median_slope_deg,
        "percentile_low": percentile(float(parcel.min())),
        "percentile_high": percentile(float(parcel.max())),
        "percentile_mean": percentile(float(parcel.mean())),
        "window_min_ft": window_min / METERS_PER_FOOT,
        "window_max_ft": window_max / METERS_PER_FOOT,
        "window_relief_ft": (window_max - window_min) / METERS_PER_FOOT,
        "parcel_cells": int(mask.sum()),
        "inflow": upslope_inflow(context_dem, mask),
    }


def context_contour_interval_ft(relief_ft: float) -> int:
    for interval in CONTEXT_CONTOUR_INTERVALS_FT:
        if relief_ft / interval <= CONTEXT_CONTOUR_MAX_LINES:
            return interval
    return CONTEXT_CONTOUR_INTERVALS_FT[-1]


def elevation_bands(context_dem: dict, edges_ft: list) -> list:
    """THE CONTEXT MAP'S TINT: the ground between successive `edges_ft`
    (the index contours), as polygons in the DEM's UTM zone -- contourpy's
    filled contours over the same grid the lines come from, so a band's
    edge IS its index contour. [{'low_ft', 'high_ft', 'geometry'}], lowest
    first; the first band's low and the last's high are the window's own."""
    import contourpy
    from shapely.geometry import MultiPolygon, Polygon
    from shapely.ops import unary_union

    from contour_lines import _grid_axes

    array = context_dem["array"]
    low_ft, high_ft = float(np.nanmin(array)) / METERS_PER_FOOT, float(np.nanmax(array)) / METERS_PER_FOOT
    bounds = [low_ft - 1.0] + [e for e in edges_ft if low_ft < e < high_ft] + [high_ft + 1.0]
    x, y = _grid_axes(context_dem)
    generator = contourpy.contour_generator(x=x, y=y, z=array, fill_type=contourpy.FillType.OuterOffset)
    bands = []
    for lower, upper in zip(bounds[:-1], bounds[1:]):
        points, offsets = generator.filled(lower * METERS_PER_FOOT, upper * METERS_PER_FOOT)
        polygons = []
        for pts, offs in zip(points, offsets):
            rings = [pts[offs[i]:offs[i + 1]] for i in range(len(offs) - 1)]
            if rings and len(rings[0]) >= 4:
                polygons.append(Polygon(rings[0], [r for r in rings[1:] if len(r) >= 4]).buffer(0))
        geometry = unary_union(polygons) if polygons else MultiPolygon()
        bands.append({"low_ft": max(lower, low_ft), "high_ft": min(upper, high_ft), "geometry": geometry})
    return bands


def context_contours(context_dem: dict) -> dict:
    """The context map's contour levels over the whole window, in feet:
    {'interval_ft', 'index_every', 'relief_ft', 'levels': [{'elevation_ft',
    'index', 'geometry'}]}, geometry in the DEM's UTM zone."""
    array = context_dem["array"]
    low, high = float(np.nanmin(array)) / METERS_PER_FOOT, float(np.nanmax(array)) / METERS_PER_FOOT
    interval = context_contour_interval_ft(high - low)
    levels = []
    for contour in compute_contour_lines(context_dem, interval_meters=interval * METERS_PER_FOOT):
        elevation_ft = round(contour["elevation_m"] / METERS_PER_FOOT)
        levels.append({"elevation_ft": elevation_ft,
                       "index": elevation_ft % (CONTEXT_INDEX_EVERY * interval) == 0,
                       "geometry": contour["lines_utm"]})
    index_ft = [lv["elevation_ft"] for lv in levels if lv["index"]]
    return {"interval_ft": interval, "index_every": CONTEXT_INDEX_EVERY, "relief_ft": high - low,
            "min_ft": low, "max_ft": high, "levels": levels, "bands": elevation_bands(context_dem, index_ft)}


# ======================================================================
# Buildings and transmission
# ======================================================================


def _to_utm(geometry: dict, crs: str):
    return shape(transform_geom("EPSG:4326", crs, geometry))


def derive_buildings(structures: dict, polygon, crs: str) -> dict:
    """Footprints intersecting the parcel ('on'), and the nearest one that
    does not ('nearest_outside_m'), in the parcel's UTM zone."""
    on, outside = [], []
    for row in structures["structures"]:
        footprint = _to_utm(row["geometry"], crs)
        entry = {"build_id": row["build_id"], "occupancy": row["occupancy"], "sqft": row["sqft"],
                 "image_date": row["image_date"], "production_date": row["production_date"]}
        if footprint.intersects(polygon):
            entry["straddles"] = not polygon.contains(footprint)
            on.append(entry)
        else:
            outside.append((footprint.distance(polygon), entry))
    outside.sort(key=lambda pair: pair[0])
    return {
        "on_parcel": on,
        "count": len(on),
        "sqft": sum(e["sqft"] or 0 for e in on),
        "nearest_outside_m": outside[0][0] if outside else None,
        "nearest_outside": outside[0][1] if outside else None,
        "searched_m": structures.get("buffer_meters"),
        "image_dates": structures.get("image_dates") or [],
        "production_dates": structures.get("production_dates") or [],
        "min_sqft": 450,
    }


def derive_transmission(block: dict, polygon, crs: str) -> dict:
    """The nearest line within the search radius, and the nearest whose
    voltage the source knows (the same line when it knows the first)."""
    ranked = []
    for line in block["lines"]:
        distance = _to_utm(line["geometry"], crs).distance(polygon)
        ranked.append(dict({k: v for k, v in line.items() if k != "geometry"}, distance_m=distance))
    ranked.sort(key=lambda row: row["distance_m"])
    known = [row for row in ranked if row["voltage_kv"] is not None]
    return {
        "radius_m": block.get("radius_meters"),
        "count": len(ranked),
        "nearest": ranked[0] if ranked else None,
        "nearest_known_voltage": known[0] if known else None,
    }


# ======================================================================
# The whole overview
# ======================================================================


def derive(inputs: OverviewInputs) -> OverviewDerived:
    polygon = inputs.boundary_polygon_utm
    crs = inputs.dem["crs"]
    contours = report_map.parcel_contours(inputs.dem, polygon)
    elevation = {"min_ft": contours["min_ft"], "max_ft": contours["max_ft"], "relief_ft": contours["relief_ft"]}

    landscape = context = None
    if inputs.context_dem is not None:
        if inputs.context_dem["crs"] != crs:
            raise ValueError(f"context DEM in {inputs.context_dem['crs']}, the session's in {crs}")
        landscape = derive_landscape(inputs.context_dem, polygon)
        context = context_contours(inputs.context_dem)

    lat, lon = inputs.centroid
    code = livestock_predators.state_code((inputs.county_state or {}).get("state"))
    return OverviewDerived(
        acres=inputs.parcel_acres,
        perimeter_m=LineString(polygon.exterior.coords).length,
        centroid=(lat, lon),
        county_state=inputs.county_state,
        elevation=elevation,
        landscape=landscape,
        context_contours=context,
        physiography=physiography.province_at(lat, lon),
        buildings=derive_buildings(inputs.structures, polygon, crs) if inputs.structures is not None else None,
        transmission=(derive_transmission(inputs.transmission_lines, polygon, crs)
                      if inputs.transmission_lines is not None else None),
        wildlife=livestock_predators.predator_line(code) if code else None,
        unavailable={k: v for k, v in inputs.unavailable.items()
                     if k in ("context_dem", "context_water", "context_roads", "county_state", "structures",
                              "transmission_lines")},
    )
