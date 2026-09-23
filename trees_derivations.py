"""
trees_derivations.py

THE TREES & FORESTRY SECTION'S DERIVATIONS (section VI, branch 11 phase
1), computed on the report path from the session's reads and the report
layer's forest type and woodland blocks. Everything here is in the DEM's
metres and cell counts; trees_section (phase 2) converts and formats. No
colour, no page text, no network, no planting: the section is an
INVENTORY of the canopy that stands, the forest the model calls, and the
capacity the soil survey rates.

    trees_inputs_from_context(context, document, report_data) -> TreesInputs
    derive(inputs)                                            -> TreesDerived

THE CANOPY IS THE ONE THE DESIGN CONSUMED. `canopy` is ParcelData's own
canopy_height dict, read off the session context -- the object the trees
step's registry edge (parcel_data.canopy_height) forwards to its canopy
gate and the exclusion warm-up handed to identify_exclusion_zones(). It
is NOT the exclusion result's canopy layer, which is the root zone
dilated by the design buffer and restricted to the slope-and-setback
universe. test_trees_derivations.py holds the two to account: the canopy
cells this module reads, dilated by the shared buffer over that universe,
are the exclusion result's canopy mask byte for byte.

THE SOURCE IS STATED, AND CHECKED. canopy_source() reads the dict's own
`source`; the exclusion result recorded the same read as
layers['canopy']['data_source'] when the warm-up ran. TreesInputs carries
both, derive() requires them to agree, and the section prints the one
source. A height-derived canopy and a cover-derived one are different
measurements at different resolutions:

    lidar HAG      per cell, metres of first return above ground, from a
                   2 m product resampled bilinearly onto the 5 m grid;
                   canopy is a cell at or above CANOPY_HEIGHT_THRESHOLD_
                   METERS (4.5 m, the design's own 15 ft rule). Heights
                   are classified; closure is measured at a 30 m grain.
    NLCD TCC       per 30 m pixel, percent cover, sampled nearest-
                   neighbour onto the grid; canopy is any nonzero cover
                   (the design's rule). There are NO HEIGHTS: the height
                   distribution is a stated absence, and cover classes
                   stand where closure would.

HEIGHT CLASSES (HAG only), in feet because the page is imperial, with the
first break the design threshold ITSELF (4.5 m, printed as 15 ft) so the
classed cells are exactly the canopy cells: under 15 ft is not canopy
(open ground, crops, scrub and anything below the design's rule); 15-30
ft scrub and young regrowth; 30-50 ft a young stand; 50-80 ft a maturing
stand; 80 ft and over mature canopy. The top break is the survey's own
site index for the oaks and yellow-poplar on this ground (70-95 ft at 50
years), so it reads as roughly a 50-year stand here rather than as a
round number.

CLOSURE (HAG only) answers scattered-trees-or-woodland: the 5 m grid is
cut into 30 m blocks (CLOSURE_BLOCK_METERS, the grain of the fallback
product) from the grid origin; in every block that holds at least one
canopy cell, closure is the canopy cells over the block's valid on-parcel
cells. The figure is the cell-weighted mean over those blocks, with the
blocks' ground partitioned by closure class. It is DERIVED from a height
threshold and is not the fallback's percent cover, which is a model of
cover within a pixel; the page labels it so.

THE FOREST TYPE GROUP is counted over the on-parcel cells of the nearest-
neighbour sample of a 30 m modelled product, non-forest a class of its
own. It is reported beside the canopy, never reconciled with it.

THE SOIL IS RATED WHOLE, on the Water section's map-unit cell grid
(water_derivations.derive_hydric over the same reads), so every acreage
column partitions the parcel and the same cell falls in the same unit in
every section. Limitations take NRCS's dominant condition per unit
(soil_woodland.unit_condition); productivity condenses the survey's
species rows by plant symbol, sharing a unit's ground among its major
components by their percentages (soil_woodland.unit_species), the site
index acre-weighted and ranged, the base curves carried.
"""

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import numpy as np

import forest_type_data
import soil_woodland as sw
import water_derivations as wd
from canopy_cover_data import canopy_cover_cell_mask
from canopy_height_data import (
    CANOPY_HEIGHT_THRESHOLD_METERS,
    CANOPY_SOURCE_LIDAR_HAG,
    CANOPY_SOURCE_NLCD_TCC,
    TREE_ROOT_ZONE_BUFFER_METERS,
    canopy_source,
)
from raster_grid import SQUARE_METERS_PER_ACRE

METERS_PER_FOOT = 0.3048

# The height classes, (id, lower bound m, upper bound m or None). The
# first break is the design's canopy threshold; the rest are 30, 50 and
# 80 ft in metres. Lower bound inclusive.
HEIGHT_CLASSES = (
    ("under", 0.0, CANOPY_HEIGHT_THRESHOLD_METERS),
    ("15-30", CANOPY_HEIGHT_THRESHOLD_METERS, 30 * METERS_PER_FOOT),
    ("30-50", 30 * METERS_PER_FOOT, 50 * METERS_PER_FOOT),
    ("50-80", 50 * METERS_PER_FOOT, 80 * METERS_PER_FOOT),
    ("80+", 80 * METERS_PER_FOOT, None),
)
HEIGHT_CLASS_LABELS = {
    "under": "Under 15 ft", "15-30": "15–30 ft", "30-50": "30–50 ft", "50-80": "50–80 ft", "80+": "80 ft and over",
}
CANOPY_CLASSES = ("15-30", "30-50", "50-80", "80+")

CLOSURE_BLOCK_METERS = 30.0
# Closure and cover classes, (id, low % exclusive of 0, high % inclusive).
DENSITY_CLASSES = (("1-25", 0.0, 25.0), ("26-50", 25.0, 50.0), ("51-75", 50.0, 75.0), ("76-100", 75.0, 100.0))

CANOPY = "canopy"
OPEN = "open"
NO_DATA = "no data"

# The limitation partition's states beyond an interpretation's classes.
SOIL_NO_DATA = "no data"


@dataclass
class TreesInputs:
    dem: dict
    boundary_polygon_utm: object
    canopy: dict                        # ParcelData.canopy_height, the design's input
    canopy_source_recorded: Optional[str]   # exclusion_zones['layers']['canopy']['data_source']
    soil_components: list
    soil_geometries: dict
    parcel_acres: float
    retrieved_on: date
    forest_type_group: Optional[dict]   # the report layer's block, None when absent
    soil_woodland: Optional[dict]       # the report layer's block, None when absent
    wind: Optional[dict] = None         # the report data's wind block, for the caption
    unavailable: dict = field(default_factory=dict)


@dataclass
class TreesDerived:
    cells: dict
    canopy: dict
    heights: Optional[dict]     # HAG only
    closure: Optional[dict]     # HAG only
    cover: Optional[dict]       # TCC only
    forest_type: dict
    hydric: dict                # the map-unit grid
    productivity: dict
    limitations: dict


# ======================================================================
# Inputs
# ======================================================================


def _created_on(document: dict) -> date:
    stamp = (document or {}).get("created_at")
    if not stamp:
        return date.today()
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()


def trees_inputs_from_context(context, document: dict, report_data) -> TreesInputs:
    """Every read off the session and the report data. Raises when the
    session carries no canopy dict or the exclusion result no canopy
    layer: a report cannot describe a canopy the design did not have."""
    parcel = context.parcel_data
    canopy = getattr(parcel, "canopy_height", None)
    if not canopy:
        raise ValueError("the session's ParcelData carries no canopy dict")
    exclusion = context.exclusion_zones or {}
    layer = (exclusion.get("layers") or {}).get("canopy")
    if layer is None:
        raise ValueError("the session's exclusion result carries no canopy layer")
    boundary = context.boundary_polygon_utm
    return TreesInputs(
        dem=context.dem,
        boundary_polygon_utm=boundary,
        canopy=canopy,
        canopy_source_recorded=layer.get("data_source"),
        soil_components=list(getattr(parcel, "soil_components", None) or []),
        soil_geometries=dict(getattr(parcel, "soil_geometries", None) or {}),
        parcel_acres=boundary.area / SQUARE_METERS_PER_ACRE,
        retrieved_on=_created_on(document),
        forest_type_group=getattr(report_data, "forest_type_group", None),
        soil_woodland=getattr(report_data, "soil_woodland", None),
        wind=getattr(report_data, "wind", None),
        unavailable=dict(getattr(report_data, "unavailable", None) or {}),
    )


# ======================================================================
# Canopy
# ======================================================================


def canopy_grid_mask(canopy: dict) -> np.ndarray:
    """The canopy cells over the WHOLE grid, by the design's own rule for
    the dict's source: HAG at or above the threshold, TCC any nonzero
    cover. NaN is never canopy. This is the un-dilated first step of
    canopy_height_data.root_zone_mask_from_canopy()."""
    source = canopy_source(canopy)
    array = canopy["array"]
    if source == CANOPY_SOURCE_LIDAR_HAG:
        return (~np.isnan(array)) & (array >= CANOPY_HEIGHT_THRESHOLD_METERS)
    if source == CANOPY_SOURCE_NLCD_TCC:
        return canopy_cover_cell_mask(array)
    raise ValueError(f"unrecognised canopy source {source!r}")


def root_zone_radius_cells(dem: dict, buffer_meters: float = TREE_ROOT_ZONE_BUFFER_METERS) -> int:
    """The design's own metres-to-cells rounding (tree_root_zone_mask)."""
    px, py = dem["resolution_meters"]
    return max(1, math.ceil(buffer_meters / ((px + py) / 2.0)))


def derive_canopy(inputs: TreesInputs, cells: dict) -> dict:
    """
    {'source', 'units', 'source_item_id', 'year', 'product_version',
     'threshold_m', 'native_resolution_m',
     'grid_mask': canopy cells over the whole grid,
     'mask': canopy cells on the parcel,
     'counts': {CANOPY, OPEN, NO_DATA}  -- a partition of the on-parcel cells}
    """
    canopy = inputs.canopy
    source = canopy_source(canopy)
    if source != inputs.canopy_source_recorded:
        raise ValueError(
            f"the canopy dict says {source!r} but the exclusion result recorded {inputs.canopy_source_recorded!r}: "
            "the report would describe a source the design did not run on"
        )
    array = canopy["array"]
    on_parcel = cells["on_parcel"]
    if array.shape != on_parcel.shape:
        raise ValueError(f"the canopy grid {array.shape} is not the DEM grid {on_parcel.shape}")
    grid_mask = canopy_grid_mask(canopy)
    no_data = np.isnan(array) & on_parcel
    mask = grid_mask & on_parcel
    counts = {
        CANOPY: int(mask.sum()),
        OPEN: int((on_parcel & ~grid_mask & ~no_data).sum()),
        NO_DATA: int(no_data.sum()),
    }
    return {
        "source": source,
        "units": canopy.get("units", "meters_above_ground"),
        "source_item_id": canopy.get("source_item_id"),
        "year": canopy.get("year"),
        "product_version": canopy.get("product_version"),
        "threshold_m": CANOPY_HEIGHT_THRESHOLD_METERS if source == CANOPY_SOURCE_LIDAR_HAG else None,
        "native_resolution_m": 2.0 if source == CANOPY_SOURCE_LIDAR_HAG else canopy.get("native_resolution_meters"),
        "grid_mask": grid_mask,
        "mask": mask,
        "counts": counts,
    }


def height_class(height_m: float) -> Optional[str]:
    if height_m is None or math.isnan(height_m):
        return None
    for name, low, high in HEIGHT_CLASSES:
        if height_m >= low and (high is None or height_m < high):
            return name
    return HEIGHT_CLASSES[0][0] if height_m < 0 else None


def derive_heights(inputs: TreesInputs, cells: dict, canopy: dict) -> Optional[dict]:
    """HAG only: {'counts': {class: cells} over HEIGHT_CLASSES plus NO_DATA
    -- a partition of the on-parcel cells, 'max_m', 'canopy_mean_m',
    'canopy_median_m'}. None on the fallback path: there are no heights."""
    if canopy["source"] != CANOPY_SOURCE_LIDAR_HAG:
        return None
    array = inputs.canopy["array"]
    on_parcel = cells["on_parcel"]
    values = array[on_parcel]
    counts = {name: 0 for name, _, _ in HEIGHT_CLASSES}
    counts[NO_DATA] = 0
    for value in values:
        name = height_class(float(value))
        counts[name if name is not None else NO_DATA] += 1
    heights = values[canopy["mask"][on_parcel]]
    return {
        "counts": counts,
        "max_m": float(np.nanmax(values)) if values.size else float("nan"),
        "canopy_mean_m": float(np.mean(heights)) if heights.size else float("nan"),
        "canopy_median_m": float(np.median(heights)) if heights.size else float("nan"),
    }


def density_class(percent: float) -> Optional[str]:
    for name, low, high in DENSITY_CLASSES:
        if percent > low and percent <= high:
            return name
    return None


def derive_closure(inputs: TreesInputs, cells: dict, canopy: dict) -> Optional[dict]:
    """
    HAG only: canopy closure at a 30 m grain.

        {'block_m', 'block_cells', 'blocks': n holding canopy,
         'canopy_cells', 'valid_cells': the on-parcel valid cells in those blocks,
         'mean_pct': canopy_cells / valid_cells,
         'classes': {class: {'blocks', 'cells', 'canopy_cells'}},
         'grid': per-block closure % over the block grid (NaN where no canopy)}
    """
    if canopy["source"] != CANOPY_SOURCE_LIDAR_HAG:
        return None
    dem = inputs.dem
    px, py = dem["resolution_meters"]
    block = max(1, int(round(CLOSURE_BLOCK_METERS / ((px + py) / 2.0))))
    on_parcel = cells["on_parcel"]
    valid = on_parcel & ~np.isnan(inputs.canopy["array"])
    rows, cols = on_parcel.shape
    grid_rows, grid_cols = math.ceil(rows / block), math.ceil(cols / block)
    grid = np.full((grid_rows, grid_cols), np.nan, dtype=float)
    classes = {name: {"blocks": 0, "cells": 0, "canopy_cells": 0} for name, _, _ in DENSITY_CLASSES}
    blocks = canopy_cells = valid_cells = 0
    for r in range(grid_rows):
        for c in range(grid_cols):
            sl = (slice(r * block, (r + 1) * block), slice(c * block, (c + 1) * block))
            n_canopy = int(canopy["mask"][sl].sum())
            if n_canopy == 0:
                continue
            n_valid = int(valid[sl].sum())
            closure = 100.0 * n_canopy / n_valid
            grid[r, c] = closure
            blocks += 1
            canopy_cells += n_canopy
            valid_cells += n_valid
            entry = classes[density_class(closure)]
            entry["blocks"] += 1
            entry["cells"] += n_valid
            entry["canopy_cells"] += n_canopy
    return {
        "block_m": block * (px + py) / 2.0,
        "block_cells": block,
        "blocks": blocks,
        "canopy_cells": canopy_cells,
        "valid_cells": valid_cells,
        "mean_pct": 100.0 * canopy_cells / valid_cells if valid_cells else float("nan"),
        "classes": classes,
        "grid": grid,
    }


def derive_cover(inputs: TreesInputs, cells: dict, canopy: dict) -> Optional[dict]:
    """TCC only: {'classes': {class: cells} over DENSITY_CLASSES -- a
    partition of the canopy cells by their pixel's percent cover,
    'mean_pct': the mean cover over canopy cells, 'max_pct'}. None on the
    lidar path."""
    if canopy["source"] != CANOPY_SOURCE_NLCD_TCC:
        return None
    values = inputs.canopy["array"][canopy["mask"]]
    classes = {name: 0 for name, _, _ in DENSITY_CLASSES}
    for value in values:
        classes[density_class(float(value))] += 1
    return {
        "classes": classes,
        "mean_pct": float(np.mean(values)) if values.size else float("nan"),
        "max_pct": float(np.max(values)) if values.size else float("nan"),
    }


# ======================================================================
# Forest type group
# ======================================================================


def derive_forest_type(inputs: TreesInputs, cells: dict) -> dict:
    """{'fetched', 'counts': {code: cells} over the parcel, 'nodata': cells,
    'forest_cells', 'groups': [codes with any cell, forest first by cells],
    'single': the one forest group's code when exactly one, else None,
    'vintage', 'product', 'native_resolution_m'}. Counts plus nodata
    partition the on-parcel cells."""
    block = inputs.forest_type_group
    if block is None:
        return {"fetched": False, "counts": {}, "nodata": 0, "forest_cells": 0, "groups": [], "single": None}
    array = block["array"]
    on_parcel = cells["on_parcel"]
    if array.shape != on_parcel.shape:
        raise ValueError(f"the forest type grid {array.shape} is not the DEM grid {on_parcel.shape}")
    counts = forest_type_data.class_counts(array, on_parcel)
    forest = {code: n for code, n in counts.items() if code != forest_type_data.NON_FOREST}
    ordered = sorted(forest, key=lambda code: -forest[code])
    return {
        "fetched": True,
        "counts": counts,
        "nodata": int(np.count_nonzero(np.isnan(array[on_parcel]))),
        "forest_cells": sum(forest.values()),
        "groups": ordered + ([forest_type_data.NON_FOREST] if forest_type_data.NON_FOREST in counts else []),
        "single": ordered[0] if len(ordered) == 1 else None,
        "vintage": block["vintage"],
        "product": block["product"],
        "native_resolution_m": block["native_resolution_m"],
    }


# ======================================================================
# Soil: productivity and limitations
# ======================================================================


def derive_productivity(inputs: TreesInputs, hydric: dict) -> dict:
    """
    {'fetched': bool,
     'species': [{'symbol', 'common', 'scientific', 'cells', 'units': [mukey],
                  'site_index_mean', 'site_index_min', 'site_index_max',
                  'bases': [curve names], 'volume_mean' (cu ft/ac/yr) | None}]
                  -- most ground first,
     'units': {mukey: {'muname', 'cells', 'major_pct', 'rated_major', 'unrated_major',
                       'species': {symbol: share}}},
     'rated_cells': ground with at least one rated species,
     'no_data_cells': ground in a unit the layer does not carry or whose
                      major components carry no rows,
     'survey_areas': [...]}

    A species' cells are the sum over units of the unit's cells times the
    share of its major components that rate the species; its site index
    is the mean over those components weighted by that ground.
    """
    block = inputs.soil_woodland
    units, species = {}, {}
    rated_cells = no_data_cells = 0
    for mukey, unit in hydric["map_units"].items():
        unit_cells = unit["cells"]
        if block is None or mukey not in block["map_units"]:
            units[mukey] = {"muname": unit["muname"], "cells": unit_cells, "major_pct": 0.0, "rated_major": [],
                            "unrated_major": [], "species": {}}
            no_data_cells += unit_cells
            continue
        rated = sw.unit_species(block, mukey)
        units[mukey] = {"muname": unit["muname"], "cells": unit_cells, "major_pct": rated["major_pct"],
                        "rated_major": rated["rated_major"], "unrated_major": rated["unrated_major"],
                        "species": {symbol: record["share"] for symbol, record in rated["species"].items()}}
        if not rated["species"]:
            no_data_cells += unit_cells
            continue
        rated_cells += unit_cells
        for symbol, record in rated["species"].items():
            entry = species.setdefault(symbol, {
                "symbol": symbol, "common": record["common"], "scientific": record["scientific"],
                "cells": 0.0, "units": [], "_si": [], "_vol": [], "bases": [],
            })
            entry["cells"] += unit_cells * record["share"]
            entry["units"].append(mukey)
            for value, comppct, base in record["site_index"]:
                weight = unit_cells * comppct / rated["major_pct"] if rated["major_pct"] else 0.0
                entry["_si"].append((value, weight))
                if base and base not in entry["bases"]:
                    entry["bases"].append(base)
            for value, comppct in record["volume"]:
                weight = unit_cells * comppct / rated["major_pct"] if rated["major_pct"] else 0.0
                entry["_vol"].append((value, weight))
    rows = []
    for entry in species.values():
        si_weight = sum(w for _, w in entry["_si"])
        vol_weight = sum(w for _, w in entry["_vol"])
        rows.append({
            "symbol": entry["symbol"], "common": entry["common"], "scientific": entry["scientific"],
            "cells": entry["cells"], "units": entry["units"],
            "site_index_mean": sum(v * w for v, w in entry["_si"]) / si_weight if si_weight else None,
            "site_index_min": min(v for v, _ in entry["_si"]),
            "site_index_max": max(v for v, _ in entry["_si"]),
            "bases": entry["bases"],
            "volume_mean": sum(v * w for v, w in entry["_vol"]) / vol_weight if vol_weight else None,
        })
    rows.sort(key=lambda r: (-r["cells"], r["common"] or ""))
    return {
        "fetched": block is not None,
        "species": rows,
        "units": units,
        "rated_cells": rated_cells,
        "no_data_cells": no_data_cells,
        "survey_areas": list((block or {}).get("survey_areas") or []),
    }


def derive_limitations(inputs: TreesInputs, hydric: dict) -> dict:
    """
    {'fetched': bool,
     'interpretations': {name: {'counts': {class: cells} -- a partition of the
                                on-parcel cells over the interpretation's
                                classes, NOT_RATED, SOIL_NO_DATA and the
                                no-polygon state,
                                'features': {class: {feature: cells}},
                                'units': {mukey: {'class', 'features'}}}},
     'survey_areas': [...]}
    """
    block = inputs.soil_woodland
    interpretations = {}
    for name in sw.INTERPRETATIONS:
        classes = sw.INTERPRETATION_CLASSES[name]
        counts = {c: 0 for c in classes + (sw.NOT_RATED, SOIL_NO_DATA, wd.HYDRIC_NO_POLYGON)}
        counts[wd.HYDRIC_NO_POLYGON] = hydric["counts"][wd.HYDRIC_NO_POLYGON]
        features = {c: {} for c in classes}
        units = {}
        for mukey, unit in hydric["map_units"].items():
            unit_cells = unit["cells"]
            if block is None or mukey not in block["map_units"]:
                units[mukey] = {"class": None, "features": {}}
                counts[SOIL_NO_DATA] += unit_cells
                continue
            condition = sw.unit_condition(block, mukey, name)
            cls = condition["class"]
            key = cls if cls in counts else (SOIL_NO_DATA if cls is None else sw.NOT_RATED)
            counts[key] += unit_cells
            units[mukey] = {"class": cls, "features": condition["features"]}
            if key in features:
                for feature, share in condition["features"].items():
                    features[key][feature] = features[key].get(feature, 0.0) + share * unit_cells
        for cls in features:
            features[cls] = dict(sorted(features[cls].items(), key=lambda kv: -kv[1]))
        interpretations[name] = {"counts": counts, "features": features, "units": units}
    return {
        "fetched": block is not None,
        "interpretations": interpretations,
        "survey_areas": list((block or {}).get("survey_areas") or []),
    }


# ======================================================================
# derive()
# ======================================================================


def derive(inputs: TreesInputs) -> TreesDerived:
    cells = wd.grid_bookkeeping(inputs)
    canopy = derive_canopy(inputs, cells)
    hydric = wd.derive_hydric(inputs, cells)
    return TreesDerived(
        cells=cells,
        canopy=canopy,
        heights=derive_heights(inputs, cells, canopy),
        closure=derive_closure(inputs, cells, canopy),
        cover=derive_cover(inputs, cells, canopy),
        forest_type=derive_forest_type(inputs, cells),
        hydric=hydric,
        productivity=derive_productivity(inputs, hydric),
        limitations=derive_limitations(inputs, hydric),
    )
