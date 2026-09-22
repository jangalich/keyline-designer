"""
landform_section.py

THE LANDFORM SECTION'S CONTENT (section III), formatted for the page -- the
one place the session's terrain products become the imperial, rounded,
worded values the template sets, plus the section's map.

    terrain_inputs_from_context(context, document) -> TerrainInputs
    build_landform_section(terrain)                 -> the section dict

EVERY INPUT IS A READ OFF THE SESSION. Nothing here fetches and nothing
here re-runs a KSOP module:

  dem, boundary      ParcelData's own (Layer 1, fetched once per boundary).
  slope grid         exclusion_zones.identify_exclusion_zones()'s own
                     `slope_pct` -- production_area.compute_slope_percent()'s
                     grid, computed once in the terrain warm-up and
                     published on the exclusion result for exactly this
                     kind of reuse. The slope classes, the flat threshold
                     and the mean slope all read this one grid.
  aspect grid        the landform step's own STEP 1 `aspect_deg` when the
                     session has generated that step (terrain_metrics.
                     compute_slope_and_aspect(), Horn's method, already
                     paid for); otherwise ONE Horn pass here, and the
                     section says which (TerrainInputs.aspect_source).
  valleys, keypoints the warm-up's own lists, verbatim.
  parcel acreage     the boundary polygon's area in the DEM's UTM zone.
  retrieval date     the Design Document's created_at: Layer 1 is fetched
                     when the session is created, so that is the day the
                     elevation model was retrieved.

test_landform_section.py holds this to account with call counts: building
the section calls compute_slope_percent(), delineate_valleys() and
detect_keypoints() zero times.

SLOPE CLASSES are the SSURGO slope phases -- A 0-3, B 3-8, C 8-15, D 15-25,
E 25-35, F 35%+ -- with no production ceiling in sight: this is the land,
not what may be farmed. A cell belongs to the class its slope falls in
(lower bound inclusive). ASPECT is eight compass sectors of 45 degrees
centred on the cardinal and intercardinal points, plus Flat for ground
under FLAT_SLOPE_PCT, where a downhill direction means nothing.

EVERY ACREAGE TABLE SUMS EXACTLY TO THE PARCEL ACREAGE ON THE COVER. Shares
are computed from on-parcel cell counts; acres are share x the parcel
polygon's acreage, rounded by largest remainder (allocate_exactly) so the
rounded column adds up to the rounded total, never to a tenth either side
of it. Percent columns are allocated the same way to 100.0.

THE MAP adds four kinds of layer to the fixed frame, drawn in this order:
slope-class tints (a graduated ramp of ONE hue, the terrain token, light
for flat ground to dark for steep -- only the classes present), the
contours with their index labels, valleys as dashed lines, keypoints as
asterisks. THE DARKEST TINT IS CAPPED at SLOPE_TINT_OPACITY['F'] so a
full-strength terrain contour still reads over class F ground, and the
LIGHTEST IS LIFTED so the class covering most of a parcel never reads as
blank paper; the ramp is a table of opacities on the one token, never a
second colour.

COMPUTE ON CELLS, DRAW WHAT READS CORRECTLY -- the production zones' own
split. The tables count cells (pixel-centre containment, the pipeline's
rasterization convention). The tints are FILLED CONTOURS of the slope
grid at the class breaks -- 3, 8, 15, 25, 35% -- drawn by contourpy the
way the elevation contours are drawn from the DEM, so their edges are the
smooth iso-lines of the same surface the linework comes from, and they
clip cleanly to the boundary with no cell-edge slivers. The acreages do
not move: nothing in a table reads a fill polygon.

TWO PAGES, BY RULE. A section with a map is two pages: the picture and
its takeaways (summary, map, legend, key figures) on the first, every
number (the tables, the footer) on the second. landform.html marks the
split with .section__figures / .section__detail; every later section
with a map takes the same shape.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import contourpy
import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union

import report_map
from contour_lines import _grid_axes
from raster_grid import SQUARE_METERS_PER_ACRE, cells_in_polygon
from report_outline import section_number

SECTION_NAME = "Landform"
SECTION_TEMPLATE = "landform.html"

METERS_PER_FOOT = report_map.METERS_PER_FOOT

# SSURGO slope phases: (class, lower bound %, upper bound % or None).
SLOPE_CLASSES = (
    ("A", 0.0, 3.0),
    ("B", 3.0, 8.0),
    ("C", 8.0, 15.0),
    ("D", 15.0, 25.0),
    ("E", 25.0, 35.0),
    ("F", 35.0, None),
)

# The slope ramp: fill-opacity of the terrain token per class, light to
# dark. Six steps of 0.07-0.09; the darkest is CAPPED at 0.55 so the
# contours, drawn in the token at full strength, stay legible over class
# F, and the lightest starts at 0.14 so the lightest class present is
# clearly toned rather than paper.
SLOPE_TINT_OPACITY = {"A": 0.14, "B": 0.22, "C": 0.31, "D": 0.40, "E": 0.48, "F": 0.55}
SLOPE_TINT_CAP = 0.55

# Ground under this slope has no meaningful downhill direction.
FLAT_SLOPE_PCT = 2.0

ASPECT_SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
ASPECT_WORDS = {
    "N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
    "S": "south", "SW": "southwest", "W": "west", "NW": "northwest",
}
FLAT_LABEL = "Flat"

VALLEY_STROKE_PT = 0.9
VALLEY_DASH = "3 2"
KEYPOINT_STROKE_PT = 0.9

SOURCE_LINE = "Source: USGS 3DEP elevation, resampled to 5 m · retrieved "
CAVEAT_LINE = (
    "A 5 m elevation model smooths out features narrower than about 10 m — "
    "terraces, swales, and ditches may not appear."
)


# ======================================================================
# Inputs
# ======================================================================


@dataclass
class TerrainInputs:
    """The session reads the section is built from. See the module
    docstring for where each comes from."""

    dem: dict
    boundary_polygon_utm: object
    slope_pct: np.ndarray
    aspect_deg: np.ndarray
    valleys: list
    keypoints: list
    parcel_acres: float
    retrieved_on: date
    aspect_source: str  # "landform step" | "one Horn pass"


def _created_on(document: dict) -> date:
    stamp = document.get("created_at")
    if not stamp:
        return date.today()
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()


def _step1_aspect(context) -> Optional[np.ndarray]:
    """The landform step's own STEP 1 aspect grid, when the session has
    generated that step and the proposal is still in the cache."""
    proposal = (getattr(context, "step_proposals", None) or {}).get("landform")
    if not isinstance(proposal, dict):
        return None
    # identify_optimized_production_areas() carries STEP 1 under run_inputs
    # (the grids the rescoring path reads); the older direct key is kept
    # for a result that still carries it there.
    step1 = (proposal.get("run_inputs") or {}).get("step1") or proposal.get("step1")
    if not isinstance(step1, dict):
        return None
    aspect = step1.get("aspect_deg")
    return aspect if isinstance(aspect, np.ndarray) else None


def terrain_inputs_from_context(context, document: dict) -> TerrainInputs:
    """Every read, and the one conditional computation (aspect, when the
    landform step has not been generated)."""
    dem = context.dem
    boundary = context.boundary_polygon_utm
    exclusion = context.exclusion_zones or {}
    slope = exclusion.get("slope_pct")
    if slope is None:
        raise ValueError("the session's exclusion result carries no slope grid")
    aspect = _step1_aspect(context)
    if aspect is not None:
        source = "landform step"
    else:
        from terrain_metrics import compute_slope_and_aspect

        _, aspect = compute_slope_and_aspect(dem["array"], dem["resolution_meters"])
        source = "one Horn pass"
    return TerrainInputs(
        dem=dem,
        boundary_polygon_utm=boundary,
        slope_pct=slope,
        aspect_deg=aspect,
        valleys=list(context.valleys or []),
        keypoints=list(context.keypoints or []),
        parcel_acres=boundary.area / SQUARE_METERS_PER_ACRE,
        retrieved_on=_created_on(document),
        aspect_source=source,
    )


# ======================================================================
# Exact allocation
# ======================================================================


def allocate_exactly(counts: list, total: float, decimals: int) -> list:
    """
    `total` split in proportion to `counts`, each part rounded to
    `decimals`, the parts summing EXACTLY to round(total, decimals) --
    largest-remainder rounding. A zero count gets 0.0; an all-zero list
    gets all zeros.
    """
    scale = 10 ** decimals
    target = int(round(total * scale))
    weight = float(sum(counts))
    if weight <= 0 or target <= 0:
        return [0.0 for _ in counts]
    raw = [count / weight * target for count in counts]
    floors = [int(math.floor(value)) for value in raw]
    short = target - sum(floors)
    order = sorted(range(len(counts)), key=lambda i: (raw[i] - floors[i], counts[i]), reverse=True)
    for i in order[:short]:
        floors[i] += 1
    return [value / scale for value in floors]


# ======================================================================
# Classification
# ======================================================================


def slope_class(slope_pct: float) -> Optional[str]:
    if slope_pct is None or math.isnan(slope_pct):
        return None
    for name, low, high in SLOPE_CLASSES:
        if slope_pct >= low and (high is None or slope_pct < high):
            return name
    return None


def slope_range_label(name: str) -> str:
    for cls, low, high in SLOPE_CLASSES:
        if cls == name:
            return f"{low:g}–{high:g}%" if high is not None else f"{low:g}%+"
    raise KeyError(name)


def aspect_sector(aspect_deg: float, slope_pct: float) -> Optional[str]:
    """One of the eight sectors, FLAT_LABEL under the flat threshold, None
    where neither is known (an edge cell Horn's window could not fill)."""
    if slope_pct is None or math.isnan(slope_pct):
        return None
    if slope_pct < FLAT_SLOPE_PCT:
        return FLAT_LABEL
    if aspect_deg is None or math.isnan(aspect_deg):
        return None
    return ASPECT_SECTORS[int(round((aspect_deg % 360.0) / 45.0)) % 8]


def classify_cells(terrain: TerrainInputs) -> dict:
    """
    Per-cell classes over the on-parcel cells (raster_grid.cells_in_polygon,
    the pipeline's own rasterization convention):

        {'cells', 'slope_counts': {class: n}, 'aspect_counts': {sector: n},
         'unclassified_aspect': n, 'mean_slope_pct', 'valid_cells'}
    """
    cells = cells_in_polygon(terrain.dem, terrain.boundary_polygon_utm)
    rows = np.array([r for r, _ in cells], dtype=int)
    cols = np.array([c for _, c in cells], dtype=int)
    slope_values = terrain.slope_pct[rows, cols] if len(cells) else np.array([])
    aspect_values = terrain.aspect_deg[rows, cols] if len(cells) else np.array([])

    slope_counts = {name: 0 for name, _, _ in SLOPE_CLASSES}
    aspect_counts = {sector: 0 for sector in ASPECT_SECTORS + (FLAT_LABEL,)}
    unclassified_aspect = 0
    valid = []
    for (r, c), slope, aspect in zip(cells, slope_values, aspect_values):
        cls = slope_class(float(slope))
        if cls is None:
            continue
        valid.append(float(slope))
        slope_counts[cls] += 1
        sector = aspect_sector(float(aspect), float(slope))
        if sector is None:
            unclassified_aspect += 1
        else:
            aspect_counts[sector] += 1
    return {
        "cells": cells,
        "slope_counts": slope_counts,
        "aspect_counts": aspect_counts,
        "unclassified_aspect": unclassified_aspect,
        "mean_slope_pct": float(np.mean(valid)) if valid else float("nan"),
        "valid_cells": len(valid),
    }


# ======================================================================
# Geometry for the map
# ======================================================================


def _filled_polygons(points_list, offsets_list) -> list:
    """contourpy FillType.OuterOffset -> shapely polygons: each entry is
    one outer ring followed by its holes, delimited by offsets."""
    polygons = []
    for points, offsets in zip(points_list, offsets_list):
        rings = [points[offsets[i]:offsets[i + 1]] for i in range(len(offsets) - 1)]
        rings = [ring for ring in rings if len(ring) >= 4]
        if not rings:
            continue
        polygon = Polygon(rings[0], rings[1:]).buffer(0)
        if not polygon.is_empty:
            polygons.append(polygon)
    return polygons


def slope_class_geometries(terrain: TerrainInputs, slope_counts: dict) -> dict:
    """
    {class: geometry} -- FILLED CONTOURS of the slope grid between the
    class breaks, clipped to the boundary, for the classes that have any
    on-parcel cell. The grid's NaN cells are masked out, exactly as the
    elevation contours exclude nodata. Display only: the acreage tables
    never read these.
    """
    slope = np.ma.masked_invalid(terrain.slope_pct.astype(float))
    if slope.count() == 0:
        return {}
    x, y = _grid_axes(terrain.dem)
    generator = contourpy.contour_generator(x=x, y=y, z=slope, fill_type=contourpy.FillType.OuterOffset)
    top = float(slope.max()) + 1.0
    out = {}
    for name, low, high in SLOPE_CLASSES:
        if slope_counts.get(name, 0) <= 0:
            continue
        upper = top if high is None else float(high)
        if upper <= low:
            continue
        polygons = _filled_polygons(*generator.filled(float(low) if low > 0 else -1.0, upper))
        if not polygons:
            continue
        clipped = unary_union(polygons).intersection(terrain.boundary_polygon_utm)
        clipped = _polygonal(clipped)
        if clipped is not None:
            out[name] = clipped
    return out


def _polygonal(geometry):
    """The polygonal part of a clip result, or None."""
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    parts = [g for g in getattr(geometry, "geoms", []) if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
    if not parts:
        return None
    return unary_union(parts)


def valley_lines(terrain: TerrainInputs) -> list:
    """Every valley branch as a line clipped to the boundary."""
    lines = []
    for valley in terrain.valleys:
        for branch in valley.get("branches_utm") or []:
            coords = [(float(x), float(y)) for x, y, *_ in branch]
            if len(coords) < 2:
                continue
            clipped = report_map._linear_parts(LineString(coords).intersection(terrain.boundary_polygon_utm))
            if clipped is not None:
                lines.append(clipped)
    return lines


def parcel_keypoints(terrain: TerrainInputs) -> list:
    """The keypoints on the parcel, as points."""
    return [kp["point_utm"] for kp in terrain.keypoints if kp.get("on_parcel") and kp.get("point_utm") is not None]


# ======================================================================
# Formatting
# ======================================================================


def _feet(meters_or_feet: float) -> str:
    return f"{round(meters_or_feet):,}"


def _one_decimal(value: float) -> str:
    return f"{value:,.1f}"


# A true zero in a one-decimal column is set as a dash: it reads as "none",
# not as a measurement, and keeps the decimal line down the column.
ZERO_DASH = "–"


def _one_decimal_or_dash(value: float) -> str:
    return ZERO_DASH if value == 0 else _one_decimal(value)


def format_retrieved_on(when: date) -> str:
    from climate_section import format_generated_on

    return format_generated_on(when)


def build_slope_table(counts: dict, parcel_acres: float) -> dict:
    """Class, range, acres, % of parcel for every class present; a total
    row that IS the parcel acreage."""
    present = [name for name, _, _ in SLOPE_CLASSES if counts.get(name, 0) > 0]
    acres = allocate_exactly([counts[name] for name in present], parcel_acres, 1)
    shares = allocate_exactly([counts[name] for name in present], 100.0, 1)
    rows = []
    for name, acre, share in zip(present, acres, shares):
        rows.append({
            "label": ["Class ", {"value": name}],
            "cells": [slope_range_label(name), _one_decimal_or_dash(acre), _one_decimal_or_dash(share)],
        })
    rows.append({"label": "Total", "cells": ["", _one_decimal(round(parcel_acres, 1)), _one_decimal(100.0)]})
    return {"corner": "Slope class", "columns": ["Range", "Acres", "% of parcel"], "rows": rows,
            "compact": True, "acres": acres, "shares": shares, "classes": present}


def build_aspect_table(counts: dict, parcel_acres: float) -> dict:
    """Transposed: the nine sectors across, acres and percent down."""
    columns = list(ASPECT_SECTORS) + [FLAT_LABEL]
    acres = allocate_exactly([counts.get(name, 0) for name in columns], parcel_acres, 1)
    shares = allocate_exactly([counts.get(name, 0) for name in columns], 100.0, 1)
    return {
        "corner": "Aspect",
        "columns": columns,
        "rows": [
            {"label": "Acres", "cells": [_one_decimal_or_dash(a) for a in acres]},
            {"label": "% of parcel", "cells": [_one_decimal_or_dash(s) for s in shares]},
        ],
        "acres": acres,
        "shares": shares,
    }


def dominant_aspect(counts: dict) -> Optional[str]:
    """The most-populated compass sector, ignoring Flat; None when no cell
    has a direction."""
    best = max(ASPECT_SECTORS, key=lambda s: counts.get(s, 0))
    return best if counts.get(best, 0) > 0 else None


def dominant_slope_class(counts: dict) -> str:
    return max((name for name, _, _ in SLOPE_CLASSES), key=lambda n: counts.get(n, 0))


def build_summary(relief_ft: float, slope_counts: dict, aspect_counts: dict) -> list:
    cls = dominant_slope_class(slope_counts)
    parts = [
        "The land rises ", {"value": _feet(relief_ft)}, " ft across the parcel. Most of it is in slope class ",
        {"value": cls}, ", ", {"value": slope_range_label(cls)},
    ]
    sector = dominant_aspect(aspect_counts)
    if sector is None:
        parts.append(", and it is essentially flat.")
    else:
        parts.append(f", and it falls toward the {ASPECT_WORDS[sector]}.")
    return parts


def build_key_figures(contours: dict, classified: dict, keypoint_count: int) -> list:
    sector = dominant_aspect(classified["aspect_counts"])
    return [
        {"value": f"{_feet(contours['min_ft'])} ft", "label": "lowest elevation"},
        {"value": f"{_feet(contours['max_ft'])} ft", "label": "highest elevation"},
        {"value": f"{_feet(contours['relief_ft'])} ft", "label": "relief"},
        {"value": f"{_one_decimal(classified['mean_slope_pct'])}%", "label": "mean slope"},
        {"value": ASPECT_WORDS[sector].capitalize() if sector else FLAT_LABEL, "label": "dominant aspect", "word": True},
        {"value": f"{keypoint_count:,}", "label": "keypoints detected"},
    ]


def build_footer(retrieved_on: date) -> dict:
    return {
        "caveat": [CAVEAT_LINE],
        "citation": [SOURCE_LINE + format_retrieved_on(retrieved_on)],
    }


# ======================================================================
# The map
# ======================================================================


def build_map_layers(terrain: TerrainInputs, contours: dict, class_geometries: dict) -> list:
    layers = []
    for name, _, _ in SLOPE_CLASSES:
        geometry = class_geometries.get(name)
        if geometry is None:
            continue
        layers.append(report_map.layer(
            f"slope-{name}", [geometry], kind="polygon", fill="terrain",
            fill_opacity=SLOPE_TINT_OPACITY[name], stroke=None,
            legend=[{"value": name}, " ", {"value": slope_range_label(name)}],
        ))
    layers += report_map.contour_layers(contours, legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}])
    valleys = valley_lines(terrain)
    if valleys:
        layers.append(report_map.layer(
            "valleys", valleys, kind="line", stroke="ink-muted", stroke_width=VALLEY_STROKE_PT,
            dash=VALLEY_DASH, legend="Valleys",
        ))
    points = parcel_keypoints(terrain)
    if points:
        layers.append(report_map.layer(
            "keypoints", points, kind="point", stroke="ink", stroke_width=KEYPOINT_STROKE_PT, legend="Keypoints",
        ))
    return layers


# ======================================================================
# The section
# ======================================================================


def build_landform_section(terrain: TerrainInputs, tokens: Optional[dict] = None) -> dict:
    """The section dict the landform.html template sets. `tokens` defaults
    to site_report.TOKENS; passed in so the renderer never imports the
    page module."""
    if tokens is None:
        import site_report

        tokens = site_report.TOKENS
    contours = report_map.parcel_contours(terrain.dem, terrain.boundary_polygon_utm)
    classified = classify_cells(terrain)
    class_geometries = slope_class_geometries(terrain, classified["slope_counts"])
    keypoints = parcel_keypoints(terrain)
    layers = build_map_layers(terrain, contours, class_geometries)
    rendered = report_map.render_map(terrain.boundary_polygon_utm, layers, tokens)
    slope_table = build_slope_table(classified["slope_counts"], terrain.parcel_acres)
    aspect_table = build_aspect_table(classified["aspect_counts"], terrain.parcel_acres)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(contours["relief_ft"], classified["slope_counts"], classified["aspect_counts"]),
        "map": rendered,
        "key_figures": build_key_figures(contours, classified, len(keypoints)),
        "slope_table": slope_table,
        "aspect_table": aspect_table,
        "footer": build_footer(terrain.retrieved_on),
        # For tests and diagnostics, not the template.
        "contours": {k: v for k, v in contours.items() if k != "levels"},
        "classified": {k: v for k, v in classified.items() if k != "cells"},
        "classes_present": list(class_geometries),
        "aspect_source": terrain.aspect_source,
    }
