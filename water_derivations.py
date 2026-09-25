"""
water_derivations.py

THE WATER & HYDROLOGY SECTION'S DERIVATIONS (section IV, branch 9 phase 1),
computed on the report path from the session's reads and the report
layer's Water blocks. Everything here is in the DEM's metres, centimetres
and cell counts; water_section (phase 2) converts and formats. No colour,
no page text, no network, no siting.

    water_inputs_from_context(context, document, report_data) -> WaterInputs
    derive(inputs, flow=None)                                 -> WaterDerived

WHAT IS READ AND WHAT IS DERIVED. Off the session: the DEM, the boundary,
the exclusion result's slope grid (production_area.compute_slope_percent's
own, the grid the water step's TWI reads too), the warm-up's valleys, and
ParcelData's NHD rows, SSURGO component rows and map-unit polygons. Off
the report layer: springs, stream order, wetlands, flood zones, land
cover, the seasonal water table. Derived here: everything the section
prints.

ONE FLOW PASS PER REPORT, SHARED. The filled surface, the flow field and
the accumulation are the landform derivations' own (landform_derivations.
TerrainDerived, ~70 ms once per report); derive() takes that object and
runs the pass itself only when handed none. From it: depression depth
(filled minus raw, the water step's noise floor), raw TWI (the water
step's own function over the same accumulation and slope), and the
parcel's outlet watersheds. test_water_derivations.py counts the calls
and checks the arrays against the water step's own screens.

THE WETNESS THRESHOLD IS THE PIPELINE'S. "Wet ground by terrain" is the
on-parcel cells at or above the water step's window-referenced
full-credit breakpoint (water_survey_areas.twi_window_breakpoints over
twi_reference_window -- the 90th percentile of the DEM window's raw TWI).
Reusing the step's own curve keeps the section and the step from
drifting; the block carries the TWI value so the page prints it.

WET GROUND THREE WAYS, ON CELLS. Hydric soil (SSURGO), mapped wetland
(NWI) and terrain wetness (TWI) are each a mask over the on-parcel cells
by pixel-centre containment -- the pipeline's rasterisation convention --
so their agreement is a cell count and every acreage partition sums to
the parcel. Hydric is the pipeline's own rule: a map unit whose hydric
components sum to MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE or more is
PREDOMINANTLY hydric; one with any hydric share below it is PARTIALLY
hydric and is tabled but not counted as hydric ground in the comparison.

THE SEASONAL WATER TABLE, WEIGHTED. Per map unit and month, over its
MAJOR components weighted by comppct: the mean depth of those with a Wet
layer, the share of the unit's major-component percentage that has one,
and, for the rest, the shallowest described profile depth (the depth the
water table is deeper than -- a component's deepest described layer
bottom in any month, because a month without a water table often carries
no moisture layer at all). The parcel row weights each unit by its cells
on the parcel. A unit whose components carry no month rows has no data
and says so.

CATCHMENT, TWO NUMBERS FOR TWO QUESTIONS. The stream's catchment is
NHDPlus HR's total drainage area at the reach beside the parcel -- whole-
network, not truncated by anything the report chose. What the terrain
analysis sees is the union of the parcel outlets' watersheds within the
DEM window, split on- and off-parcel, with the count of watershed cells
on the window's rim: any rim cell means the window truncates it. Land
cover of that window-derived catchment comes from the NLCD grid, which is
cell-aligned with the DEM.

FLOOD. Each flood zone polygon becomes a cell mask; the on-parcel cells
partition into the zones present plus "not in any zone" -- which reads
as "no digital flood map" when the NFHL has no study here and as
"unmapped within the study" when it does. Adjacent zones are measured by
polygon area inside the fetch window.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely import contains_xy
from shapely.geometry import LineString, MultiLineString, box, shape
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

import hydrology_data
import landform_derivations
import nlcd_landcover_data
import water_survey_areas
from raster_grid import SQUARE_METERS_PER_ACRE, cells_in_polygon
from soil_data import MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE, is_hydric
from water_suitability import _stream_permanence_label

ADJACENCY_BUFFER_METERS = 150.0
HYDRIC_PREDOMINANT = "predominantly"
HYDRIC_PARTIAL = "partially"
HYDRIC_NONE = "none"
HYDRIC_NO_POLYGON = "no survey polygon"
HYDRIC_CLASSES = (HYDRIC_PREDOMINANT, HYDRIC_PARTIAL, HYDRIC_NONE, HYDRIC_NO_POLYGON)

FLOOD_NOT_IN_ANY_ZONE = "not in any zone"
FLOOD_NO_DIGITAL_MAP = "no digital flood map"

WT_WET = "wet"
WT_DEEPER = "deeper"
WT_NO_DATA = "no data"


@dataclass
class WaterInputs:
    dem: dict
    boundary_polygon_utm: object
    slope_pct: np.ndarray
    valleys: list
    water_features: dict            # ParcelData's NHD rows: {'streams': [...], 'water_bodies': [...]}
    soil_components: list           # ParcelData's SSURGO component rows
    soil_geometries: dict           # ParcelData's {mukey: GeoJSON geometry (WGS84)}
    parcel_acres: float
    retrieved_on: date              # Layer 1's retrieval date (the document's created_at)
    # The report layer's Water blocks, each None when absent.
    nhd_points: Optional[list]
    nhdplus_hr: Optional[dict]
    nwi: Optional[dict]
    fema_nfhl: Optional[dict]
    nlcd_landcover: Optional[dict]
    soil_water_table: Optional[dict]
    unavailable: dict = field(default_factory=dict)


@dataclass
class WaterDerived:
    cells: dict                     # the grid bookkeeping: counts, cell_acres, masks
    surface_water: dict
    hydric: dict
    wetlands: dict
    wetness: dict
    comparison: dict
    water_table: dict
    drainage: dict
    catchment: dict
    land_cover: dict
    flood: dict
    flow: landform_derivations.TerrainDerived = None


# ======================================================================
# Inputs
# ======================================================================


def _created_on(document: dict) -> date:
    stamp = (document or {}).get("created_at")
    if not stamp:
        return date.today()
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()


def water_inputs_from_context(context, document: dict, report_data) -> WaterInputs:
    """Every read off the session and the report data. Raises when the
    exclusion result carries no slope grid, as the Landform reads do."""
    exclusion = context.exclusion_zones or {}
    slope = exclusion.get("slope_pct")
    if slope is None:
        raise ValueError("the session's exclusion result carries no slope grid")
    parcel = context.parcel_data
    boundary = context.boundary_polygon_utm
    return WaterInputs(
        dem=context.dem,
        boundary_polygon_utm=boundary,
        slope_pct=slope,
        valleys=list(context.valleys or []),
        water_features=getattr(parcel, "water_features", None) or {"streams": [], "water_bodies": []},
        soil_components=list(getattr(parcel, "soil_components", None) or []),
        soil_geometries=dict(getattr(parcel, "soil_geometries", None) or {}),
        parcel_acres=boundary.area / SQUARE_METERS_PER_ACRE,
        retrieved_on=_created_on(document),
        nhd_points=getattr(report_data, "nhd_points", None),
        nhdplus_hr=getattr(report_data, "nhdplus_hr", None),
        nwi=getattr(report_data, "nwi", None),
        fema_nfhl=getattr(report_data, "fema_nfhl", None),
        nlcd_landcover=getattr(report_data, "nlcd_landcover", None),
        soil_water_table=getattr(report_data, "soil_water_table", None),
        unavailable=dict(getattr(report_data, "unavailable", None) or {}),
    )


# ======================================================================
# Grid bookkeeping
# ======================================================================


def _cell_centres(dem: dict) -> tuple:
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    xs = dem["origin_x"] + (np.arange(cols) + 0.5) * px
    ys = dem["origin_y"] - (np.arange(rows) + 0.5) * py
    return np.meshgrid(xs, ys)


def _polygon_mask(geometry, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Cells whose centre lies inside `geometry` (a shapely polygon or
    multipolygon in the DEM's CRS)."""
    if geometry is None or geometry.is_empty:
        return np.zeros(xs.shape, dtype=bool)
    return contains_xy(geometry, xs, ys)


def grid_bookkeeping(inputs: WaterInputs) -> dict:
    dem = inputs.dem
    px, py = dem["resolution_meters"]
    rows, cols = dem["array"].shape
    on_parcel = np.zeros((rows, cols), dtype=bool)
    for r, c in cells_in_polygon(dem, inputs.boundary_polygon_utm):
        on_parcel[r, c] = True
    xs, ys = _cell_centres(dem)
    rim = np.zeros((rows, cols), dtype=bool)
    rim[0, :] = rim[-1, :] = rim[:, 0] = rim[:, -1] = True
    return {
        "on_parcel": on_parcel,
        "on_parcel_count": int(on_parcel.sum()),
        "cell_acres": px * py / SQUARE_METERS_PER_ACRE,
        "xs": xs,
        "ys": ys,
        "rim": rim,
        "window_polygon": box(dem["origin_x"], dem["origin_y"] - rows * py, dem["origin_x"] + cols * px, dem["origin_y"]),
    }


# ======================================================================
# Geometry helpers
# ======================================================================


def _to_crs(geometry_geojson, crs: str):
    """A GeoJSON geometry in WGS84 -> a shapely geometry in `crs`."""
    geometry = shape(geometry_geojson)

    def _project(xs, ys, zs=None):
        out_x, out_y = warp_transform("EPSG:4326", crs, list(xs), list(ys))
        return (out_x, out_y)

    return shapely_transform(_project, geometry)


def _linear(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, (LineString, MultiLineString)):
        return geometry
    parts = [g for g in getattr(geometry, "geoms", []) if isinstance(g, LineString) and not g.is_empty]
    return None if not parts else (parts[0] if len(parts) == 1 else MultiLineString(parts))


def _polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        return geometry
    parts = [g for g in getattr(geometry, "geoms", []) if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
    return unary_union(parts) if parts else None


def adjacency_window(boundary_polygon_utm, buffer_meters: float = ADJACENCY_BUFFER_METERS):
    """THE GROUND WITHIN buffer_meters OF THE BOUNDARY -- a true distance,
    the boundary buffered, not the fetch box. The fetches ask the services
    for the parcel's bounding box plus 150 m, which on a triangular parcel
    reaches ground 266 m from the boundary at a corner; "within 500 ft"
    on the page has to mean 500 ft, so adjacency is measured here."""
    return boundary_polygon_utm.buffer(buffer_meters)


# ======================================================================
# Surface water
# ======================================================================


def derive_surface_water(inputs: WaterInputs) -> dict:
    """
    NHD on and adjacent to the parcel:

        {'streams': [{'name', 'fcode', 'permanence', 'permanent_identifier',
                      'stream_order', 'total_drainage_acres', 'geometry_utm',
                      'on_parcel', 'length_on_parcel_m', 'length_in_window_m',
                      'distance_m'}],
         'waterbodies': [{'name', 'fcode', 'geometry_utm', 'area_on_parcel_m2',
                          'area_in_window_m2', 'distance_m'}],
         'springs': [{'name', 'point_utm', 'on_parcel', 'distance_m'}] | None,
         'springs_fetched': bool,
         'nearest_stream_distance_m': float | None,
         'length_on_parcel_by_permanence_m': {label: m},
         'streams_in_window_by_permanence': {label: n},
         'order_available': bool}
    """
    crs = inputs.dem["crs"]
    parcel = inputs.boundary_polygon_utm
    window = adjacency_window(parcel)
    joined = inputs.nhdplus_hr or {}
    streams = []
    for row in inputs.water_features.get("streams") or []:
        if not row.get("geometry"):
            continue
        line = _to_crs(row["geometry"], crs)
        pid = row.get("permanent_identifier")
        extra = joined.get(str(pid)) if pid is not None else None
        on_parcel = _linear(line.intersection(parcel))
        in_window = _linear(line.intersection(window))
        streams.append({
            "name": row.get("name"),
            "fcode": row.get("feature_code"),
            "permanence": _stream_permanence_label(row.get("feature_code")),
            "permanent_identifier": pid,
            "stream_order": extra["stream_order"] if extra else None,
            "total_drainage_acres": extra["total_drainage_acres"] if extra else None,
            "geometry_utm": line,
            "on_parcel": on_parcel,
            "length_on_parcel_m": float(on_parcel.length) if on_parcel is not None else 0.0,
            "length_in_window_m": float(in_window.length) if in_window is not None else 0.0,
            "distance_m": float(parcel.distance(line)),
        })
    waterbodies = []
    for row in inputs.water_features.get("water_bodies") or []:
        if not row.get("geometry"):
            continue
        polygon = _polygonal(_to_crs(row["geometry"], crs).buffer(0))
        if polygon is None:
            continue
        waterbodies.append({
            "name": row.get("name"),
            "fcode": row.get("feature_code"),
            "geometry_utm": polygon,
            "area_on_parcel_m2": float(polygon.intersection(parcel).area),
            "area_in_window_m2": float(polygon.intersection(window).area),
            "distance_m": float(parcel.distance(polygon)),
        })
    springs = None
    if inputs.nhd_points is not None:
        springs = []
        for row in hydrology_data.springs_and_seeps(inputs.nhd_points):
            if not row.get("geometry"):
                continue
            point = _to_crs(row["geometry"], crs)
            springs.append({
                "name": row.get("name"),
                "point_utm": point,
                "on_parcel": bool(parcel.contains(point)),
                "distance_m": float(parcel.distance(point)),
            })
    by_permanence = {}
    for stream in streams:
        by_permanence[stream["permanence"]] = by_permanence.get(stream["permanence"], 0.0) + stream["length_on_parcel_m"]
    in_window = {}
    for stream in streams:
        if stream["length_in_window_m"] > 0:
            in_window[stream["permanence"]] = in_window.get(stream["permanence"], 0) + 1
    return {
        "streams": streams,
        "waterbodies": waterbodies,
        "springs": springs,
        "springs_fetched": inputs.nhd_points is not None,
        "nearest_stream_distance_m": min((s["distance_m"] for s in streams), default=None),
        "length_on_parcel_by_permanence_m": by_permanence,
        "streams_in_window_by_permanence": in_window,
        "order_available": inputs.nhdplus_hr is not None,
    }


# ======================================================================
# Wet ground, three ways
# ======================================================================


def hydric_share_by_mukey(soil_components: list) -> dict:
    """{mukey: summed comppct of hydric components} -- soil_data's own
    is_hydric per component, summed the way hydric_disqualifying_mukeys
    sums it."""
    shares = {}
    names = {}
    for row in soil_components:
        mukey = str(row.get("mukey"))
        names.setdefault(mukey, row.get("muname"))
        shares.setdefault(mukey, 0.0)
        if is_hydric(row.get("hydricrating")):
            try:
                shares[mukey] += float(row.get("comppct_r") or 0.0)
            except (TypeError, ValueError):
                pass
    return {mukey: {"hydric_pct": pct, "muname": names.get(mukey)} for mukey, pct in shares.items()}


def hydric_class(hydric_pct: float, threshold: float = MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE) -> str:
    if hydric_pct >= threshold:
        return HYDRIC_PREDOMINANT
    if hydric_pct > 0:
        return HYDRIC_PARTIAL
    return HYDRIC_NONE


def derive_hydric(inputs: WaterInputs, cells: dict) -> dict:
    """
    {'map_units': {mukey: {'muname', 'hydric_pct', 'class', 'cells',
                           'geometry_utm'}},
     'counts': {class: cells}   -- a partition of the on-parcel cells,
     'mask_predominant', 'mask_any', 'mukey_grid': int32 index grid (-1 none),
     'mukeys': [mukey per index]}
    """
    crs = inputs.dem["crs"]
    shares = hydric_share_by_mukey(inputs.soil_components)
    on_parcel = cells["on_parcel"]
    mukey_grid = np.full(on_parcel.shape, -1, dtype=np.int32)
    mukeys = []
    map_units = {}
    for mukey, geometry in inputs.soil_geometries.items():
        mukey = str(mukey)
        polygon = _polygonal(_to_crs(geometry, crs).buffer(0))
        mask = _polygon_mask(polygon, cells["xs"], cells["ys"]) & on_parcel & (mukey_grid < 0)
        index = len(mukeys)
        mukeys.append(mukey)
        mukey_grid[mask] = index
        pct = shares.get(mukey, {}).get("hydric_pct", 0.0)
        map_units[mukey] = {
            "muname": shares.get(mukey, {}).get("muname"),
            "hydric_pct": pct,
            "class": hydric_class(pct),
            "cells": int(mask.sum()),
            "geometry_utm": polygon,
        }
    counts = {name: 0 for name in HYDRIC_CLASSES}
    mask_predominant = np.zeros(on_parcel.shape, dtype=bool)
    mask_any = np.zeros(on_parcel.shape, dtype=bool)
    for index, mukey in enumerate(mukeys):
        unit = map_units[mukey]
        counts[unit["class"]] += unit["cells"]
        if unit["class"] == HYDRIC_PREDOMINANT:
            mask_predominant |= (mukey_grid == index)
        if unit["class"] != HYDRIC_NONE:
            mask_any |= (mukey_grid == index)
    counts[HYDRIC_NO_POLYGON] = int((on_parcel & (mukey_grid < 0)).sum())
    return {
        "map_units": map_units,
        "counts": counts,
        "mask_predominant": mask_predominant & on_parcel,
        "mask_any": mask_any & on_parcel,
        "mukey_grid": mukey_grid,
        "mukeys": mukeys,
        "threshold_pct": MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE,
    }


def derive_wetlands(inputs: WaterInputs, cells: dict) -> dict:
    """
    NWI on the parcel (cells) and within the fetch window (polygon area):

        {'fetched': bool,
         'features': [{... nwi_data's feature, plus 'distance_m',
                       'area_in_window_m2', 'cells_on_parcel'}],
         'counts_by_type': {wetland_type: cells}, 'mask', 'on_parcel_cells',
         'window_area_by_type_m2': {wetland_type: m2},
         'project': nwi_data's project dict | None}
    """
    on_parcel = cells["on_parcel"]
    mask = np.zeros(on_parcel.shape, dtype=bool)
    if inputs.nwi is None:
        return {"fetched": False, "features": [], "counts_by_type": {}, "mask": mask, "on_parcel_cells": 0,
                "window_area_by_type_m2": {}, "project": None}
    parcel = inputs.boundary_polygon_utm
    window = adjacency_window(parcel)
    features = []
    counts = {}
    window_area = {}
    for feature in inputs.nwi["features"]:
        geometry = feature.get("geometry_utm")
        row = dict(feature)
        row["distance_m"] = float(parcel.distance(geometry)) if geometry is not None else None
        row["area_in_window_m2"] = float(geometry.intersection(window).area) if geometry is not None else 0.0
        feature_mask = _polygon_mask(geometry, cells["xs"], cells["ys"]) & on_parcel
        row["cells_on_parcel"] = int(feature_mask.sum())
        kind = feature.get("wetland_type") or "Unclassified"
        counts[kind] = counts.get(kind, 0) + int((feature_mask & ~mask).sum())
        mask |= feature_mask
        window_area[kind] = window_area.get(kind, 0.0) + row["area_in_window_m2"]
        features.append(row)
    return {
        "fetched": True,
        "features": features,
        "counts_by_type": {k: v for k, v in counts.items() if v},
        "mask": mask,
        "on_parcel_cells": int(mask.sum()),
        "window_area_by_type_m2": window_area,
        "project": inputs.nwi.get("project"),
    }


def derive_wetness(inputs: WaterInputs, cells: dict, flow: landform_derivations.TerrainDerived) -> dict:
    """
    Terrain wetness from the shared flow pass:

        {'twi_raw', 'breakpoints' (twi_window_breakpoints' dict),
         'threshold': the full-credit breakpoint, 'mask', 'wet_cells',
         'percentiles_on_parcel': {'p10', 'p25', 'p50', 'p75', 'p90', 'max'},
         'depression_depth', 'depression_cells', 'depression_max_m',
         'reference': twi_reference_window's dict}
    """
    dem = inputs.dem
    on_parcel = cells["on_parcel"]
    twi_raw = water_survey_areas.compute_topographic_wetness_index(dem, flow.accumulation, inputs.slope_pct)
    reference = water_survey_areas.twi_reference_window(dem)
    breakpoints = water_survey_areas.twi_window_breakpoints(twi_raw, reference["mask"])
    threshold = breakpoints["full_credit"]
    with np.errstate(invalid="ignore"):
        mask = on_parcel & (threshold is not None) & (twi_raw >= (threshold if threshold is not None else np.inf))
    depression = water_survey_areas.compute_depression_depth(dem["array"], flow.filled)
    with np.errstate(invalid="ignore"):
        depression_mask = on_parcel & (depression > 0)
    values = twi_raw[on_parcel & ~np.isnan(twi_raw)]
    percentiles = {}
    if values.size:
        for label, q in (("p10", 10), ("p25", 25), ("p50", 50), ("p75", 75), ("p90", 90)):
            percentiles[label] = float(np.percentile(values, q))
        percentiles["max"] = float(values.max())
        percentiles["min"] = float(values.min())
    return {
        "twi_raw": twi_raw,
        "breakpoints": breakpoints,
        "threshold": threshold,
        "mask": mask,
        "wet_cells": int(mask.sum()),
        "percentiles_on_parcel": percentiles,
        "measured_cells": int(values.size),
        "depression_depth": depression,
        "depression_mask": depression_mask,
        "depression_cells": int(depression_mask.sum()),
        "depression_max_m": float(np.nanmax(depression[on_parcel])) if on_parcel.any() else 0.0,
        "reference": reference,
    }


def derive_comparison(cells: dict, hydric: dict, wetlands: dict, wetness: dict) -> dict:
    """The three indicators against each other, in cells: each alone,
    every pair, all three, any, none; the wettest terrain cell's hydric
    class -- the water standards finding, measured."""
    h, w, t = hydric["mask_predominant"], wetlands["mask"], wetness["mask"]
    on_parcel = cells["on_parcel"]
    out = {
        "hydric": int(h.sum()), "wetland": int(w.sum()), "terrain": int(t.sum()),
        "hydric_and_wetland": int((h & w).sum()), "hydric_and_terrain": int((h & t).sum()),
        "wetland_and_terrain": int((w & t).sum()), "all_three": int((h & w & t).sum()),
        "any": int((h | w | t).sum()), "none": int((on_parcel & ~(h | w | t)).sum()),
        "hydric_only": int((h & ~w & ~t).sum()), "wetland_only": int((w & ~h & ~t).sum()),
        "terrain_only": int((t & ~h & ~w).sum()),
        "terrain_on_partially_hydric": int((t & hydric["mask_any"] & ~h).sum()),
        "wetlands_fetched": wetlands["fetched"],
    }
    twi = wetness["twi_raw"]
    masked = np.where(on_parcel & ~np.isnan(twi), twi, -np.inf)
    if np.isfinite(masked).any():
        r, c = np.unravel_index(int(np.argmax(masked)), masked.shape)
        index = int(hydric["mukey_grid"][r, c])
        mukey = hydric["mukeys"][index] if index >= 0 else None
        out["wettest_cell"] = {
            "rowcol": (int(r), int(c)), "twi": float(masked[r, c]), "mukey": mukey,
            "hydric_class": hydric["map_units"][mukey]["class"] if mukey else HYDRIC_NO_POLYGON,
            "muname": hydric["map_units"][mukey]["muname"] if mukey else None,
            "in_wetland": bool(w[r, c]),
        }
    else:
        out["wettest_cell"] = None
    return out


# ======================================================================
# The seasonal water table
# ======================================================================


def derive_water_table(inputs: WaterInputs, hydric: dict) -> dict:
    """
    Per map unit and month, then the parcel:

        {'fetched': bool,
         'map_units': {mukey: {'muname', 'cells', 'has_data', 'major_pct',
                               'months': {m: {'state', 'depth_cm', 'wet_share',
                                              'deeper_than_cm'}}}},
         'parcel': {m: {'depth_cm', 'wet_share', 'data_share',
                        'deeper_than_cm', 'flood': {class: share}, 'pond': {class: share}}},
         'weighted_cells': int, 'survey_areas': [...]}

    A unit's month is WET (a comppct-weighted depth over the major
    components with a Wet layer, and the share of major-component
    percentage that has one), DEEPER (rows exist, no major component is
    Wet: deeper than the shallowest described bottom) or NO DATA (no
    rows). The parcel row weights units by their cells on the parcel;
    its depth is over the wet share only, so 'wet_share' must be read
    beside it.
    """
    block = inputs.soil_water_table
    if block is None:
        return {"fetched": False, "map_units": {}, "parcel": {}, "weighted_cells": 0, "survey_areas": []}
    map_units = {}
    parcel = {m: {"wet_weight": 0.0, "depth_weight": 0.0, "data_weight": 0.0, "deeper": [],
                  "flood": {}, "pond": {}, "class_weight": 0.0} for m in range(1, 13)}
    total_cells = 0
    for mukey, unit in hydric["map_units"].items():
        source = block["map_units"].get(mukey)
        cells = unit["cells"]
        if source is None or cells == 0:
            map_units[mukey] = {"muname": unit["muname"], "cells": cells, "has_data": False, "major_pct": 0.0, "months": {}}
            continue
        components = [block["components"][cokey] for cokey in source["components"]]
        majors = [c for c in components if c["major"] and c["has_rows"]]
        major_pct = sum(c["comppct"] for c in majors)
        if not majors or major_pct <= 0:
            # No major component carries a month row: NO DATA, in those
            # words, never twelve empty months and never "deep".
            map_units[mukey] = {"muname": unit["muname"], "cells": cells, "has_data": False, "major_pct": major_pct, "months": {}}
            continue
        # THE DESCRIBED PROFILE DEPTH is a property of the component, not
        # of the month: the deepest soil-moisture layer bottom it carries
        # in any month. A month with no Wet layer -- including the common
        # case of a month with no moisture layer described at all (Ernest
        # May to November carries comonth rows and zero cosoilmoist rows)
        # -- reads as deeper than that depth. None only when the component
        # describes no layer in any month.
        profile_depth = {
            c["compname"]: max((row["described_bottom_cm"] for row in c["months"].values() if row["described_bottom_cm"] is not None), default=None)
            for c in majors
        }
        months = {}
        for m in range(1, 13):
            wet = [(c["comppct"], c["months"][m]["wet_top_cm"]) for c in majors
                   if m in c["months"] and c["months"][m]["wet_top_cm"] is not None]
            dry = [profile_depth[c["compname"]] for c in majors
                   if m not in c["months"] or c["months"][m]["wet_top_cm"] is None]
            wet_pct = sum(p for p, _ in wet)
            if wet:
                depth = sum(p * d for p, d in wet) / wet_pct
                months[m] = {"state": WT_WET, "depth_cm": depth, "wet_share": wet_pct / major_pct,
                             "deeper_than_cm": min((d for d in dry if d is not None), default=None)}
            else:
                months[m] = {"state": WT_DEEPER, "depth_cm": None, "wet_share": 0.0,
                             "deeper_than_cm": min((d for d in dry if d is not None), default=None)}
        map_units[mukey] = {"muname": unit["muname"], "cells": cells, "has_data": True, "major_pct": major_pct, "months": months}
        total_cells += cells
        # Flooding and ponding: every component with rows, weighted by
        # comppct, a NULL class as "not stated".
        for m in range(1, 13):
            row = months[m]
            parcel[m]["data_weight"] += cells
            if row["state"] == WT_WET:
                parcel[m]["wet_weight"] += cells * row["wet_share"]
                parcel[m]["depth_weight"] += cells * row["wet_share"] * row["depth_cm"]
            if row["deeper_than_cm"] is not None:
                parcel[m]["deeper"].append(row["deeper_than_cm"])
            for c in components:
                if not c["has_rows"] or m not in c["months"]:
                    continue
                weight = cells * c["comppct"] / 100.0
                parcel[m]["class_weight"] += weight
                for key in ("flood", "pond"):
                    label = c["months"][m][key] or "not stated"
                    parcel[m][key][label] = parcel[m][key].get(label, 0.0) + weight
    summary = {}
    for m, row in parcel.items():
        data = row["data_weight"]
        summary[m] = {
            "depth_cm": (row["depth_weight"] / row["wet_weight"]) if row["wet_weight"] > 0 else None,
            "wet_share": (row["wet_weight"] / data) if data > 0 else 0.0,
            "data_share": (data / hydric_total(hydric)) if hydric_total(hydric) > 0 else 0.0,
            "deeper_than_cm": min(row["deeper"]) if row["deeper"] else None,
            "flood": {k: v / row["class_weight"] for k, v in row["flood"].items()} if row["class_weight"] > 0 else {},
            "pond": {k: v / row["class_weight"] for k, v in row["pond"].items()} if row["class_weight"] > 0 else {},
        }
    return {"fetched": True, "map_units": map_units, "parcel": summary, "weighted_cells": total_cells,
            "survey_areas": list(block.get("survey_areas") or [])}


def hydric_total(hydric: dict) -> int:
    return sum(unit["cells"] for unit in hydric["map_units"].values())


# ======================================================================
# Drainage, catchment, land cover
# ======================================================================


def derive_drainage(inputs: WaterInputs) -> dict:
    """The warm-up's valley branches clipped to the parcel: the flow
    paths the map draws as context. A read, not a recomputation."""
    parcel = inputs.boundary_polygon_utm
    paths = []
    total = 0.0
    for valley in inputs.valleys:
        for branch in valley.get("branches_utm") or []:
            coords = [(float(x), float(y)) for x, y, *_ in branch]
            if len(coords) < 2:
                continue
            clipped = _linear(LineString(coords).intersection(parcel))
            if clipped is not None:
                paths.append({"valley_id": int(valley["id"]), "geometry": clipped})
                total += float(clipped.length)
    return {"flow_paths": paths, "length_on_parcel_m": total, "valleys": len(inputs.valleys)}


def parcel_outlets(cells: dict, flow: landform_derivations.TerrainDerived) -> list:
    """On-parcel cells whose flow leaves the parcel (or the window), with
    their accumulation, largest first."""
    on_parcel = cells["on_parcel"]
    outlets = []
    for r, c in zip(*np.nonzero(on_parcel)):
        tr, tc = int(flow.flow_to_row[r, c]), int(flow.flow_to_col[r, c])
        if tr < 0 or not on_parcel[tr, tc]:
            outlets.append(((int(r), int(c)), int(flow.accumulation[r, c])))
    outlets.sort(key=lambda o: -o[1])
    return outlets


def derive_catchment(inputs: WaterInputs, cells: dict, flow: landform_derivations.TerrainDerived, surface: dict) -> dict:
    """
    {'reaches': [{'name', 'stream_order', 'total_drainage_acres', 'in_window'}],
     'stream_catchment_acres': the largest reach figure in the window | None,
     'outlets': n, 'largest_outlet': {'rowcol', 'cells', 'rim_cells'},
     'watershed_cells', 'on_parcel_cells', 'off_parcel_cells', 'rim_cells',
     'truncated': bool, 'mask': the union watershed}
    """
    import keypoint_detection

    upstream = keypoint_detection.build_upstream_map(flow.flow_to_row, flow.flow_to_col)
    on_parcel = cells["on_parcel"]
    union = np.zeros(on_parcel.shape, dtype=bool)
    outlets = parcel_outlets(cells, flow)
    largest = None
    for rowcol, accumulation in outlets:
        watershed = water_survey_areas.watershed_cells(rowcol, upstream)
        mask = np.zeros(on_parcel.shape, dtype=bool)
        for r, c in watershed:
            mask[r, c] = True
        if largest is None:
            largest = {"rowcol": rowcol, "cells": int(mask.sum()), "rim_cells": int((mask & cells["rim"]).sum()),
                       "on_parcel_cells": int((mask & on_parcel).sum())}
        union |= mask
    reaches = []
    for stream in surface["streams"]:
        if stream["total_drainage_acres"] is None:
            continue
        reaches.append({"name": stream["name"], "stream_order": stream["stream_order"],
                        "total_drainage_acres": stream["total_drainage_acres"],
                        "in_window": stream["length_in_window_m"] > 0, "on_parcel": stream["length_on_parcel_m"] > 0})
    in_window = [r["total_drainage_acres"] for r in reaches if r["in_window"]]
    rim_cells = int((union & cells["rim"]).sum())
    return {
        "reaches": reaches,
        "stream_catchment_acres": max(in_window) if in_window else None,
        "outlets": len(outlets),
        "largest_outlet": largest,
        "watershed_cells": int(union.sum()),
        "on_parcel_cells": int((union & on_parcel).sum()),
        "off_parcel_cells": int((union & ~on_parcel).sum()),
        "rim_cells": rim_cells,
        "truncated": rim_cells > 0,
        "mask": union,
        "window_cells": int(union.size),
    }


def derive_land_cover(inputs: WaterInputs, cells: dict, catchment: dict) -> dict:
    """NLCD group counts over the window-derived catchment and over the
    parcel, nodata counted, the year carried."""
    block = inputs.nlcd_landcover
    if block is None:
        return {"fetched": False, "year": None, "catchment": {}, "parcel": {}, "catchment_nodata": 0, "parcel_nodata": 0}
    array = block["array"]
    if array.shape != cells["on_parcel"].shape:
        raise ValueError(f"the NLCD grid {array.shape} is not the DEM grid {cells['on_parcel'].shape}")

    def _over(mask):
        counts = nlcd_landcover_data.class_counts(array, mask)
        return {"classes": counts, "groups": nlcd_landcover_data.group_counts(counts),
                "nodata": int(np.count_nonzero(np.isnan(array[mask])))}

    return {
        "fetched": True, "year": block["year"], "collection": block["collection"],
        "catchment": _over(catchment["mask"]), "parcel": _over(cells["on_parcel"]),
        "native_resolution_m": block["native_resolution_m"],
    }


# ======================================================================
# Flood
# ======================================================================


def flood_zone_label(zone: dict) -> str:
    """'Zone X, minimal flood hazard' / 'Zone A' / 'Zone AE, floodway'."""
    name = zone.get("zone") or "?"
    subtype = (zone.get("subtype") or "").strip()
    if subtype:
        return f"Zone {name}, {subtype.lower()}"
    return f"Zone {name}"


def derive_flood(inputs: WaterInputs, cells: dict) -> dict:
    """
    {'fetched', 'available', 'study_ids', 'panel': {...} | None,
     'zones': [{... nfhl_data's zone, 'label', 'cells_on_parcel', 'area_in_window_m2'}],
     'counts': {label: cells}  -- a partition of the on-parcel cells,
     'unmapped_label': FLOOD_NOT_IN_ANY_ZONE | FLOOD_NO_DIGITAL_MAP,
     'sfha_cells', 'mask_sfha', 'masks': {label: mask}}
    """
    on_parcel = cells["on_parcel"]
    block = inputs.fema_nfhl
    if block is None:
        return {"fetched": False, "available": None, "study_ids": [], "panel": None, "zones": [], "counts": {},
                "unmapped_label": None, "sfha_cells": 0, "mask_sfha": np.zeros(on_parcel.shape, dtype=bool), "masks": {}}
    parcel = inputs.boundary_polygon_utm
    window = adjacency_window(parcel)
    covered = np.zeros(on_parcel.shape, dtype=bool)
    sfha = np.zeros(on_parcel.shape, dtype=bool)
    counts, masks, zones = {}, {}, []
    order = sorted(block["zones"], key=lambda z: (not z["sfha"], flood_zone_label(z)))
    for zone in order:
        label = flood_zone_label(zone)
        geometry = zone.get("geometry_utm")
        mask = _polygon_mask(geometry, cells["xs"], cells["ys"]) & on_parcel & ~covered
        covered |= mask
        if zone["sfha"]:
            sfha |= mask
        counts[label] = counts.get(label, 0) + int(mask.sum())
        masks[label] = masks.get(label, np.zeros(on_parcel.shape, dtype=bool)) | mask
        row = dict(zone)
        row["label"] = label
        row["cells_on_parcel"] = int(mask.sum())
        row["area_in_window_m2"] = float(geometry.intersection(window).area) if geometry is not None else 0.0
        zones.append(row)
    unmapped_label = FLOOD_NOT_IN_ANY_ZONE if block["available"] else FLOOD_NO_DIGITAL_MAP
    counts = {k: v for k, v in counts.items() if v}
    counts[unmapped_label] = int((on_parcel & ~covered).sum())
    panel = None
    for candidate in block["panels"]:
        if candidate.get("dfirm_id") in block["study_ids"]:
            panel = candidate
            break
    if panel is None and block["panels"]:
        panel = block["panels"][0]
    return {
        "fetched": True, "available": block["available"], "study_ids": list(block["study_ids"]), "panel": panel,
        "zones": zones, "counts": counts, "unmapped_label": unmapped_label,
        "sfha_cells": int(sfha.sum()), "mask_sfha": sfha, "masks": masks,
    }


# ======================================================================
# derive()
# ======================================================================


def derive(inputs: WaterInputs, flow: Optional[landform_derivations.TerrainDerived] = None) -> WaterDerived:
    """Every block. `flow` is Landform's derived object (the shared flow
    pass); when None, the pass runs here once."""
    if flow is None:
        filled, flow_to_row, flow_to_col, accumulation = landform_derivations.flow_pass(inputs.dem)
        flow = landform_derivations.TerrainDerived(
            filled=filled, flow_to_row=flow_to_row, flow_to_col=flow_to_col, accumulation=accumulation,
            labels=None, catchments={}, ridges=[], stems={}, keylines=[], primary_valley_id=None, profile=None, valley_rows=[],
        )
    # COMPUTED AGAIN HERE, KNOWINGLY -- NOT A BUG, AND NOT FREE TO REMOVE.
    # The map-unit cell grid (grid_bookkeeping + derive_hydric) runs once in
    # each section that reads it: Water, Access, Trees and Soils, four times
    # per report, over the same Layer 1 rows, to the same answer (test_soils_
    # derivations.py holds the partitions equal). Measured at about 0.05-0.1 s
    # a time and no network (diagnose_report_generation_time.py), so the repeat
    # costs under half a second and cannot fail on a flaky source. Sharing one
    # result means threading it through every section's inputs; worth doing
    # only if the report's compute ever matters beside its fetches.
    cells = grid_bookkeeping(inputs)
    surface = derive_surface_water(inputs)
    hydric = derive_hydric(inputs, cells)
    wetlands = derive_wetlands(inputs, cells)
    wetness = derive_wetness(inputs, cells, flow)
    comparison = derive_comparison(cells, hydric, wetlands, wetness)
    water_table = derive_water_table(inputs, hydric)
    drainage = derive_drainage(inputs)
    catchment = derive_catchment(inputs, cells, flow, surface)
    land_cover = derive_land_cover(inputs, cells, catchment)
    flood = derive_flood(inputs, cells)
    return WaterDerived(
        cells=cells, surface_water=surface, hydric=hydric, wetlands=wetlands, wetness=wetness, comparison=comparison,
        water_table=water_table, drainage=drainage, catchment=catchment, land_cover=land_cover, flood=flood, flow=flow,
    )
