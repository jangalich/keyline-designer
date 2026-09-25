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

THE TERRAIN MAP adds two kinds of layer to the fixed frame, drawn in this
order: slope-class tints (a graduated ramp of ONE hue, the terrain token,
light for flat ground to dark for steep -- only the classes present), then
the contours with their index labels. Valleys, ridges, keypoints and
keylines are the KEYLINE-STRUCTURE MAP's, a page later at the same extent
(build_structure_layers). THE DARKEST TINT IS CAPPED at SLOPE_TINT_OPACITY['F'] so a
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

THREE PAGES, BY RULE. The terrain: summary, the terrain map and, under
it, the slope table -- the map's legend in numbers. The keyline structure:
the structure map, its caption, the primary valley's profile. The
numbers: the nine key figures, the aspect and valley tables, the footer.
landform.html marks the split with .section__figures / .section__structure
/ .section__detail.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import contourpy
import numpy as np
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import substring, unary_union

import landform_derivations
import report_chart
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

# THE KEYLINE-STRUCTURE MAP'S HIERARCHY, explicit and in this order:
# keylines heaviest (ink, solid), contours next (the terrain map's own
# weights, index labels left to the terrain map a page earlier at the same
# extent), valley STEMS lighter (water, dashed -- the tributaries are not
# drawn: on a keyline map only the primary stems matter), ridges lightest
# (terrain, a long dash so they neither shimmer nor compete with the
# contours in the same hue). Keypoints are small filled dots with a halo,
# in ink on the parcel and in the muted ink just outside it; a keyline too
# short to carry its elevation in a gap sets it beside the dot instead.
KEYLINE_STROKE_PT = 1.3
VALLEY_STEM_STROKE_PT = 0.55
VALLEY_STEM_DASH = "4 2.5"
RIDGE_STROKE_PT = 0.4
RIDGE_DASH = "6 3"

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
    return [line for _, line in valley_lines_by_valley(terrain)]


def valley_lines_by_valley(terrain: TerrainInputs) -> list:
    """[(valley id, the valley's branches on the parcel as one geometry)],
    for the valleys that have any."""
    result = []
    for valley in terrain.valleys:
        lines = []
        for branch in valley.get("branches_utm") or []:
            coords = [(float(x), float(y)) for x, y, *_ in branch]
            if len(coords) < 2:
                continue
            clipped = report_map._linear_parts(LineString(coords).intersection(terrain.boundary_polygon_utm))
            if clipped is not None:
                lines.append(clipped)
        if lines:
            merged = report_map._linear_parts(unary_union(lines))
            if merged is not None:
                result.append((int(valley["id"]), merged))
    return result


def parcel_keypoints(terrain: TerrainInputs) -> list:
    """The keypoints on the parcel, as points."""
    return [kp["point_utm"] for kp in terrain.keypoints if kp.get("on_parcel") and kp.get("point_utm") is not None]


def outside_keypoints(terrain: TerrainInputs) -> list:
    """The keypoints the detector kept just outside the boundary, as points."""
    return [kp["point_utm"] for kp in terrain.keypoints if not kp.get("on_parcel") and kp.get("point_utm") is not None]


# ======================================================================
# Formatting
# ======================================================================


def _feet(meters_or_feet: float) -> str:
    return f"{round(meters_or_feet):,}"


def _one_decimal(value: float) -> str:
    return f"{value:,.1f}"


# A TRUE zero in a one-decimal column is set as a dash: it reads as "none",
# not as a measurement, and keeps the decimal line down the column. A value
# that is not zero but rounds below the displayed precision is NOT none and
# reads "<0.1", so a sector with a few cells can never show a dash beside a
# nonzero share. `count` is the cell count behind the allocated value when
# the caller has one; without it the value itself decides.
ZERO_DASH = "–"
BELOW_PRECISION = "<0.1"


def _one_decimal_or_dash(value: float, count: Optional[int] = None) -> str:
    nonzero = (count > 0) if count is not None else (value != 0)
    if not nonzero:
        return ZERO_DASH
    if round(value, 1) == 0:
        return BELOW_PRECISION
    return _one_decimal(value)


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
            "cells": [slope_range_label(name), _one_decimal_or_dash(acre, counts[name]), _one_decimal_or_dash(share, counts[name])],
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
            {"label": "Acres", "cells": [_one_decimal_or_dash(a, counts.get(name, 0)) for name, a in zip(columns, acres)]},
            {"label": "% of parcel", "cells": [_one_decimal_or_dash(v, counts.get(name, 0)) for name, v in zip(columns, shares)]},
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


def build_key_figures(contours: dict, classified: dict, derived: landform_derivations.TerrainDerived) -> list:
    """Nine figures. The keypoint count is the DETECTOR'S -- every keypoint
    it kept, on the parcel or just outside it within its margin -- because
    that is what the keyline-structure map draws (the outside one in the
    muted ink) and the count must agree with the map; the split is stated
    in words beside the figures (build_keypoint_statement()). The ridges
    are the divides with any length on the parcel; the keyline length is
    the sum of the keylines' lengths within the boundary."""
    sector = dominant_aspect(classified["aspect_counts"])
    counts = derived.keypoint_counts
    keyline_ft = sum(k["length_on_parcel_m"] for k in derived.keylines) / METERS_PER_FOOT
    return [
        {"value": f"{_feet(contours['min_ft'])} ft", "label": "lowest elevation"},
        {"value": f"{_feet(contours['max_ft'])} ft", "label": "highest elevation"},
        {"value": f"{_feet(contours['relief_ft'])} ft", "label": "relief"},
        {"value": f"{_one_decimal(classified['mean_slope_pct'])}%", "label": "mean slope"},
        {"value": ASPECT_WORDS[sector].capitalize() if sector else FLAT_LABEL, "label": "dominant aspect", "word": True},
        {"value": str(counts["detected"]), "label": keypoint_figure_label(counts)},
        {"value": str(counts["valleys_on_parcel"]), "label": "valleys on the parcel"},
        {"value": str(counts["ridges_on_parcel"]), "label": "ridges on the parcel"},
        {"value": f"{_feet(keyline_ft)} ft", "label": "keyline length on the parcel"},
    ]


# ======================================================================
# Keypoints, keylines, the profile and the valley table
# ======================================================================


def keypoint_figure_label(counts: dict) -> str:
    """The figure most likely to be misread carries its qualifier: how
    many of the detected keypoints sit just outside the boundary."""
    outside = counts["outside"]
    if outside == 0:
        return "keypoints detected"
    return f"keypoints detected, {outside} just outside the boundary"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def build_keypoint_statement(derived: landform_derivations.TerrainDerived, keypoints: list) -> list:
    """The count, in words, at the point of use: how many keypoints, how
    many on the property, how many just outside and how far. No keypoint
    means no keyline, said plainly."""
    counts = derived.keypoint_counts
    if counts["detected"] == 0:
        return ["No keypoint was found on this property, so there is no keyline to draw."]
    parts = [{"value": str(counts["detected"])}, " keypoint" + ("s" if counts["detected"] != 1 else "")]
    if counts["outside"] == 0:
        parts += [", all on the property."]
    else:
        outside = [kp for kp in keypoints if not kp.get("on_parcel")]
        farthest_ft = max(float(kp.get("distance_outside_boundary_m") or 0.0) for kp in outside) / METERS_PER_FOOT
        parts += [
            ": ", {"value": str(counts["on_parcel"])}, " on the property and ",
            {"value": str(counts["outside"])}, " just outside the boundary, within ",
            {"value": f"{_feet(farthest_ft)} ft"}, " of it",
        ]
        crossing = sum(1 for k in derived.keylines if not k["keypoint_on_parcel"] and k["on_parcel"] is not None)
        if crossing:
            parts += ["; its keyline still crosses the parcel and is drawn." if crossing == 1
                      else "; their keylines still cross the parcel and are drawn."]
        else:
            parts += ["."]
    missing = [k for k in derived.keylines if k["geometry"] is None]
    if missing:
        parts += [f" {_plural(len(missing), 'keypoint')} could not be given a keyline: {missing[0]['reason']}."]
    return parts


def build_valley_table(derived: landform_derivations.TerrainDerived) -> Optional[dict]:
    """One row per valley whose main stem crosses the parcel, the profile's
    valley first. Lengths and elevations in whole feet, grades to one
    decimal; a dash where a valley has no keypoint."""
    rows = []
    for row in derived.valley_rows:
        kp = row["keypoint"]
        cells = [
            _feet(row["length_m"] / METERS_PER_FOOT),
            _feet(row["fall_m"] / METERS_PER_FOOT),
            _one_decimal(row["grade_pct"]),
        ]
        if kp is None:
            cells += [ZERO_DASH, ZERO_DASH, ZERO_DASH]
        else:
            cells += [_feet(kp["elevation_m"] / METERS_PER_FOOT), _one_decimal(kp["grade_above_pct"]), _one_decimal(kp["grade_below_pct"])]
        rows.append({"label": ["Valley ", {"value": str(row["number"])}], "cells": cells})
    if not rows:
        return None
    return {
        "corner": "Valley",
        "columns": ["Stem, ft", "Fall, ft", "Grade, %", "Keypoint, ft", "Above, %", "Below, %"],
        "rows": rows,
    }


def build_valley_table_caption(derived: landform_derivations.TerrainDerived) -> list:
    parts = ["The stem is the valley's main line traced from its outlet up to the ridge, measured on the parcel; the "
             "fall is its drop between entering and leaving; above and below are the grades either side of the keypoint."]
    for row in derived.valley_rows:
        kp = row["keypoint"]
        if kp is None:
            parts.append(f" Valley {row['number']} has no keypoint, so it has no keyline.")
        elif not kp["on_parcel"]:
            parts += [f" Valley {row['number']}'s keypoint lies ", {"value": f"{_feet(kp['distance_outside_boundary_m'] / METERS_PER_FOOT)} ft"},
                      " outside the boundary."]
    return parts


def build_profile(derived: landform_derivations.TerrainDerived, tokens: dict) -> dict:
    """The primary valley's long profile as a chart, or the statement that
    stands in its place, with its caption."""
    profile = derived.profile
    if profile is None:
        return {"chart": None, "caption": [], "valley_number": None,
                "unavailable": ["No valley line crosses this property, so there is no profile to draw."]}
    number = next(r["number"] for r in derived.valley_rows if r["valley_id"] == profile["valley_id"])
    keypoint = profile["keypoint"]
    chart_input = {
        "distance": [d / METERS_PER_FOOT for d in profile["distance_m"]],
        "elevation": [z / METERS_PER_FOOT for z in profile["elevation_m"]],
        "crossings": [c["distance_m"] / METERS_PER_FOOT for c in profile["crossings"]],
        "keypoint": None if keypoint is None else {
            "distance": keypoint["distance_m"] / METERS_PER_FOOT,
            "elevation": keypoint["elevation_m"] / METERS_PER_FOOT,
            "grade_above_pct": keypoint["grade_above_pct"],
            "grade_below_pct": keypoint["grade_below_pct"],
            "label": f"{_feet(keypoint['elevation_m'] / METERS_PER_FOOT)} ft",
        },
    }
    chart = report_chart.render_valley_profile(chart_input, tokens)
    caption = [f"Valley {number}, the full stem from its head to where it leaves the elevation model; vertical exaggeration ",
               {"value": f"{chart['exaggeration']}×"}, "."]
    crossings = profile["crossings"]
    if len(crossings) >= 2:
        reach_ft = sum(b["distance_m"] - a["distance_m"] for a, b in zip(crossings, crossings[1:]) if a["entering"]) / METERS_PER_FOOT
        caption += [" The parcel is the short reach between the ticks, ", {"value": f"{_feet(reach_ft)} ft"},
                    " of it." if reach_ft / (profile["distance_m"][-1] / METERS_PER_FOOT) < 0.25 else " of it."]
        if reach_ft / (profile["distance_m"][-1] / METERS_PER_FOOT) >= 0.25:
            caption[-3] = " The parcel is the reach between the ticks, "
    elif len(crossings) == 1:
        caption.append(" The tick is where the stem enters the parcel." if crossings[0]["entering"]
                       else " The tick is where the stem leaves the parcel.")
    elif all(profile["on_parcel"]):
        caption.append(" The whole stem lies on the parcel.")
    if keypoint is None:
        caption.append(" No keypoint was found on this valley, so no grades are marked and it has no keyline.")
    elif not keypoint["on_parcel"]:
        caption += [" The keypoint lies ", {"value": f"{_feet(keypoint['distance_outside_boundary_m'] / METERS_PER_FOOT)} ft"},
                    " outside the boundary."]
    return {"chart": chart, "caption": caption, "valley_number": number, "unavailable": None}


def _split_at(geometry, point) -> object:
    """The keyline as two parts either side of its keypoint, so the
    elevation label -- set at the midpoint of the longest part -- lands
    on the longer reach and never on the keypoint's dot. A keyline whose
    keypoint is not on it (outside the parcel) is returned whole."""
    parts = [geometry] if isinstance(geometry, LineString) else list(geometry.geoms)
    split = []
    for part in parts:
        if part.distance(point) < 1.0:
            at = part.project(point)
            for piece in (substring(part, 0.0, at), substring(part, at, part.length)):
                if isinstance(piece, LineString) and piece.length > 0:
                    split.append(piece)
        else:
            split.append(part)
    return split[0] if len(split) == 1 else MultiLineString(split)


def _reach_to_keypoint(keyline: dict, keypoint: dict, boundary_polygon_utm):
    """For a keypoint just outside the boundary: the piece of its keyline
    from the boundary out to the keypoint, drawn in the muted ink so the
    dot is tied to its line. None when the line does not leave the
    parcel toward the keypoint."""
    line = keyline["geometry"]
    point = keypoint["point_utm"]
    if line is None or line.distance(point) > 1.0:
        return None
    outside = line.difference(boundary_polygon_utm)
    parts = [outside] if isinstance(outside, LineString) else [g for g in getattr(outside, "geoms", []) if isinstance(g, LineString)]
    touching = [p for p in parts if p.distance(point) < 1.0 and p.distance(boundary_polygon_utm) < 1e-6]
    if not touching:
        return None
    part = touching[0]
    at = part.project(point)
    start_on_boundary = boundary_polygon_utm.exterior.distance(Point(part.coords[0])) < 1e-6
    reach = substring(part, 0.0, at) if start_on_boundary else substring(part, at, part.length)
    return reach if isinstance(reach, LineString) and reach.length > 0 else None


def build_structure_layers(terrain: TerrainInputs, contours: dict, derived: landform_derivations.TerrainDerived) -> list:
    """The keyline-structure map's layers in drawing order (lightest
    context first): contours, ridges, valley stems, keylines with their
    elevation labels, the outside keypoint's reach, keypoints on the
    parcel, keypoints just outside it. See the hierarchy note above."""
    boundary = terrain.boundary_polygon_utm
    layers = []
    for spec in report_map.contour_layers(contours, legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}]):
        spec["labels"] = None      # elevations are on the terrain map, same extent, a page earlier
        layers.append(spec)
    ridges = [r["on_parcel"] for r in derived.ridges if r["on_parcel"] is not None]
    if ridges:
        layers.append(report_map.layer(
            "ridges", ridges, kind="line", stroke="terrain", stroke_width=RIDGE_STROKE_PT, dash=RIDGE_DASH, legend="Ridges",
        ))
    stems = []
    for row in derived.valley_rows:
        line = landform_derivations.stem_line(derived.stems[row["valley_id"]], terrain.dem)
        clipped = report_map._linear_parts(line.intersection(boundary)) if line is not None else None
        if clipped is not None:
            stems.append((row["number"], clipped))
    if stems:
        layers.append(report_map.layer(
            "valley-stems", [line for _, line in stems], kind="line", stroke="water", stroke_width=VALLEY_STEM_STROKE_PT,
            dash=VALLEY_STEM_DASH, legend="Valley stems, numbered as in the table",
            labels=[str(number) for number, _ in stems],
        ))
    by_id = {kp["id"]: kp for kp in terrain.keypoints}
    keylines = [k for k in derived.keylines if k["on_parcel"] is not None]
    dot_labels = {}
    if keylines:
        keyline_layer = report_map.layer(
            "keylines", [_split_at(k["on_parcel"], by_id[k["keypoint_id"]]["point_utm"]) for k in keylines],
            kind="line", stroke="ink", stroke_width=KEYLINE_STROKE_PT,
            legend="Keylines, elevation in ft", labels=[_feet(k["elevation_m"] / METERS_PER_FOOT) for k in keylines],
        )
        layers.append(keyline_layer)
        # A keyline too short to carry its label sets the elevation beside its keypoint's dot.
        for keyline, placed in zip(keylines, report_map.label_placements(boundary, keyline_layer)):
            if not placed:
                dot_labels[keyline["keypoint_id"]] = _feet(keyline["elevation_m"] / METERS_PER_FOOT)
    reaches = [r for r in (_reach_to_keypoint(k, by_id[k["keypoint_id"]], boundary) for k in keylines
                           if not k["keypoint_on_parcel"]) if r is not None]
    if reaches:
        layers.append(report_map.layer(
            "keylines-outside", reaches, kind="line", stroke="ink-muted", stroke_width=KEYLINE_STROKE_PT,
        ))
    on_parcel = [kp for kp in terrain.keypoints if kp.get("on_parcel") and kp.get("point_utm") is not None]
    if on_parcel:
        layers.append(report_map.layer(
            "keypoints", [kp["point_utm"] for kp in on_parcel], kind="point", stroke="ink", stroke_width=KEYPOINT_STROKE_PT,
            marker="dot", legend="Keypoints", labels=[dot_labels.get(kp["id"]) for kp in on_parcel],
        ))
    outside = [kp for kp in terrain.keypoints if not kp.get("on_parcel") and kp.get("point_utm") is not None]
    if outside:
        layers.append(report_map.layer(
            "keypoints-outside", [kp["point_utm"] for kp in outside], kind="point", stroke="ink-muted",
            stroke_width=KEYPOINT_STROKE_PT, marker="dot", legend="Keypoint just outside the boundary",
            labels=[dot_labels.get(kp["id"]) for kp in outside],
        ))
    return layers


def build_structure_caption(derived: landform_derivations.TerrainDerived, keypoints: list) -> list:
    """Under the keyline-structure map: what a keyline is, then the
    keypoint count as a rule."""
    parts = ["A keyline is the contour through its keypoint, drawn out to the ridges either side of its valley: "
             "the reference line from which cultivation is laid out. "]
    return parts + build_keypoint_statement(derived, keypoints)


# ======================================================================
# Sources and methods
# ======================================================================

CITATION_3DEP = ("U.S. Geological Survey, 3D Elevation Program (3DEP) seamless 1/3 arc-second digital elevation model, "
                 "served by The National Map elevation service and resampled to 5 m in the parcel's UTM zone.")


def build_sources(terrain: TerrainInputs) -> list:
    """One line per source: Landform has one."""
    return [[f"USGS 3DEP elevation, 1/3 arc-second, resampled to 5 m, retrieved {format_retrieved_on(terrain.retrieved_on)}."]]


def build_methods(terrain: TerrainInputs, derived: landform_derivations.TerrainDerived) -> list:
    """The methods note's inputs -- the full citation and the method behind
    each figure -- for the note the Site overview branch builds. Not
    rendered by this section."""
    from valley_delineation import MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES, MIN_STREAM_CONTRIBUTING_AREA_ACRES

    resolution = max(terrain.dem["resolution_meters"])
    return [
        {
            "source": "USGS 3DEP",
            "identifier": f"3DEP 1/3 arc-second DEM, resampled to {resolution:.0f} m, {terrain.dem['crs']}",
            "period": format_retrieved_on(terrain.retrieved_on),
            "citation": CITATION_3DEP,
            "method": "Contours by contourpy over the raw grid at 2, 5, 10 or 20 ft, the interval chosen for 8–15 lines "
                      "across the parcel; slope by finite differences on the 5 m grid, classed "
                      "by the SSURGO slope phases; aspect by Horn's method in eight sectors, Flat under 2%; acreages "
                      "from cell counts allocated exactly to the parcel's polygon area.",
            # WHAT THE SECTION PRINTS, NOT HOW THE DESIGN USES IT (branch 17
            # review). Valleys, keypoints, ridges, keylines and the profile
            # are figures on this section's pages, so each is defined here;
            # how the keypoint detector finds its break and how the design
            # steps build on them belongs to the design methods document, not
            # to the site data report's note.
            "notes": [
                f"Valleys: depressions filled with an epsilon gradient, D8 flow direction and accumulation; a stream where "
                f"at least {MIN_STREAM_CONTRIBUTING_AREA_ACRES} acres drain to a cell, a primary valley where the largest "
                f"contributing area reaches {MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES} acres. The map draws each valley's "
                "main stem, traced from its outlet up to the ridge by the highest-accumulation feeder at each step.",
                "Keypoints: where a valley's long profile changes from steeper to gentler ground; one per valley at most, "
                "none where the profile holds no such break.",
                "Ridges: the divides between the valleys' drainage areas.",
                "Keylines: the contour through a keypoint, followed across its valley's drainage area to the divides "
                "and clipped to the parcel.",
                "The profile is the primary valley's stem -- the largest contributing area among the valleys whose stem "
                "crosses the parcel -- with raw elevations against ground distance.",
                f"The {resolution:.0f} m grid smooths features narrower than about {2 * resolution:.0f} m; 3DEP's vertical "
                "accuracy is about 0.8 m, so elevations are set in whole feet and the finest contour interval is 2 ft.",
            ],
        },
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
    """The terrain map: slope-class tints under the contours, nothing
    else. Valleys and keypoints belong to the keyline-structure map a
    page later; drawn here too they were a second, partial copy (the
    on-parcel keypoints only) that disagreed with the figures."""
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
    # COMPUTED AGAIN HERE, KNOWINGLY -- NOT A BUG, AND NOT FREE TO REMOVE.
    # parcel_contours() runs once per section map: Site overview, Landform,
    # Water, Access, Trees, Soils and Design, seven times per report over the
    # same DEM and interval. About 0.06 s a time and no network
    # (diagnose_report_generation_time.py), so the repeat costs under half a
    # second and cannot fail on a flaky source. Sharing one result means
    # threading it through every section's builder; worth doing only if the
    # report's compute ever matters beside its fetches.
    contours = report_map.parcel_contours(terrain.dem, terrain.boundary_polygon_utm)
    classified = classify_cells(terrain)
    class_geometries = slope_class_geometries(terrain, classified["slope_counts"])
    derived = landform_derivations.derive(terrain)
    layers = build_map_layers(terrain, contours, class_geometries)
    rendered = report_map.render_map(terrain.boundary_polygon_utm, layers, tokens)
    structure = report_map.render_map(terrain.boundary_polygon_utm, build_structure_layers(terrain, contours, derived), tokens)
    slope_table = build_slope_table(classified["slope_counts"], terrain.parcel_acres)
    aspect_table = build_aspect_table(classified["aspect_counts"], terrain.parcel_acres)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(contours["relief_ft"], classified["slope_counts"], classified["aspect_counts"]),
        "map": rendered,
        "structure_map": structure,
        "structure_caption": build_structure_caption(derived, terrain.keypoints),
        "key_figures": build_key_figures(contours, classified, derived),
        "keypoint_statement": build_keypoint_statement(derived, terrain.keypoints),
        "profile": build_profile(derived, tokens),
        "valley_table": build_valley_table(derived),
        "valley_table_caption": build_valley_table_caption(derived),
        "valley_table_unavailable": ["No valley stem crosses this property, so there is no valley table."],
        "slope_table": slope_table,
        "aspect_table": aspect_table,
        "footer": build_footer(terrain.retrieved_on),
        "sources": build_sources(terrain),
        "methods": build_methods(terrain, derived),
        "derived": derived,
        # For tests and diagnostics, not the template.
        "contours": {k: v for k, v in contours.items() if k != "levels"},
        "classified": {k: v for k, v in classified.items() if k != "cells"},
        "classes_present": list(class_geometries),
        "aspect_source": terrain.aspect_source,
    }
