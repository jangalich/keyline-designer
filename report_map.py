"""
report_map.py

THE REPORT MAP COMPONENT: one function that takes the parcel boundary and
an ordered list of styled layers and returns an SVG at a fixed frame size
for the report page. Every section with a map calls it with its own
layers -- Landform first, then Water, Access, Trees and Soils.

    render_map(boundary_polygon_utm, layers, tokens) -> {svg, ...measurements}
    contour_interval_ft(relief_ft)                    -> 2 | 5 | 10 | 20
    parcel_contours(dem, boundary_polygon_utm, ...)   -> the contour layers' input
    layer(...)                                        -> one styled layer

VECTOR, IN THE PIPELINE'S UTM ZONE. The SVG is drawn in the DEM's own
projected metres (dem['crs'], the UTM zone every KSOP module computes in),
scaled uniformly onto the frame, y flipped. A map drawn in raw longitude
and latitude at 40.6 N would be squashed about 24% horizontally -- the
error the contour asset and the access-point snapping each hit once -- so
nothing here ever sees a degree. Vector output stays crisp in print and
small in the file; WeasyPrint embeds inline SVG as paths, not pixels
(verified, site-data-report-proposal.md branch 6 step 0).

FIXED FURNITURE, EVERY MAP: the parcel boundary in ink, a north arrow, a
scale bar in feet at a round length chosen for the parcel's extent, an
extent fitted to the parcel with a consistent margin, and a legend BELOW
the frame -- the same strip, in the same place, on every section's map.
The legend is not drawn into the SVG: render_map() returns its entries
(a swatch drawn from the layer's own style, a label as summary-style
parts) and the map.html macro sets them under the frame in the report's
type, so a legend label obeys the page's data rule the way a table cell
does. A section adds layers; it does not restyle the frame.

LABELLED LINES. A line layer may carry `labels`, one per geometry (a
string or None): the label is set along the line at the midpoint of its
longest part, in the data face, and the line is BROKEN behind it -- a gap
the width of the label plus padding is cut from the geometry rather than
masked with a page-coloured box, so nothing opaque sits over the tints
beneath. The index contours use this for their elevations in whole feet.
A part too short to hold its label with clear line either side is left
unlabelled rather than crowded.

NO COLOUR LITERAL LIVES HERE. Every colour is a TOKEN NAME on a layer
("ink", "terrain", ...) resolved at render time against the `tokens` dict
the caller passes -- site_report.TOKENS, the one table the stylesheet
reads too. test_report_map.py greps this module for hex literals and
expects none. The rendered SVG carries hex values in its attributes,
exactly as the rendered stylesheet does, because that is what a renderer
emits; the rule is about where a value may be TYPED.

FONTS ARE PRESENTATION ATTRIBUTES, NOT CSS. WeasyPrint's SVG renderer
does not apply the document stylesheet to SVG text (step 0: a class-styled
<text> fell back to DejaVu; a font-family attribute rendered the embedded
face), so every <text> here carries font-family, font-size and fill. Names
and words are set in the prose face; a number and the unit attached to it
in the data face -- the report's own rule, carried into the map.

MEASUREMENTS COME BACK WITH THE SVG so a test can hold the geometry to
account: metres per user unit, the boundary's drawn bbox, the scale bar's
claimed feet and drawn length. One SVG user unit is one PDF point (the
root element declares width/height in pt and a matching viewBox).

CONTOURS. contour_lines.compute_contour_lines() is the geometry (contourpy
over the DEM, the same call the layout map makes); parcel_contours() picks
the interval from the parcel's relief -- 2, 5, 10 or 20 ft, aiming for
roughly 8-15 lines across the parcel -- clips each level to the boundary,
and marks every fifth level (an elevation that is a multiple of five
intervals) as an index contour to be drawn heavier. Relief and interval
are read off the DEM in metres and converted once; the levels themselves
are multiples of the foot interval (compute_contour_lines() places levels
at multiples of the interval it is given, measured from zero).
"""

import math
from typing import Optional
from xml.sax.saxutils import escape

from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Polygon,
)

from contour_lines import compute_contour_lines
from raster_grid import elevation_range_in_polygon

METERS_PER_FOOT = 0.3048

# The frame, in points: the Letter page's content width at the report's
# 0.85 in side margins (8.5 in - 1.7 in = 6.8 in = 489.6 pt) by a height
# that leaves the page room for a heading and a table above or below.
FRAME_WIDTH_PT = 489.6
FRAME_HEIGHT_PT = 340.0
FRAME = (FRAME_WIDTH_PT, FRAME_HEIGHT_PT)

# Inside the frame: the margin around the parcel extent, and the band along
# the bottom reserved for the scale bar so it is never drawn over the
# parcel. The legend lives below the frame, not in this band.
MARGIN_PT = 14.0
FURNITURE_BAND_PT = 22.0

# The legend strip below the frame: swatch geometry in points, shared by
# the map.html macro's layout (report.css sizes the strip to match).
LEGEND_SWATCH_W_PT = 14.0
LEGEND_SWATCH_H_PT = 8.0

# Line weights, in points.
BOUNDARY_STROKE_PT = 1.1
CONTOUR_STROKE_PT = 0.45
INDEX_CONTOUR_STROKE_PT = 0.95
FRAME_STROKE_PT = 0.5

# Type, in points. The prose face for names, the data face for figures.
FONT_PROSE = "Source Serif 4"
FONT_DATA = "IBM Plex Mono"
LABEL_SIZE_PT = 7.5
NORTH_SIZE_PT = 8.5
LINE_LABEL_SIZE_PT = 6.5
# IBM Plex Mono's advance is 0.6 em; the gap either side of a line label.
MONO_ADVANCE_EM = 0.6
LINE_LABEL_PAD_PT = 3.0
# A part must hold its label plus this much clear line on each side.
LINE_LABEL_MIN_CLEAR_PT = 18.0

# The contour interval rule: the intervals a reader expects on a US map,
# and the line-count band the choice aims for.
CONTOUR_INTERVALS_FT = (2, 5, 10, 20)
CONTOUR_TARGET_MAX_LINES = 15
INDEX_CONTOUR_EVERY = 5

# Round scale-bar lengths, in feet, and the largest fraction of the frame
# width the bar may take.
SCALE_BAR_CANDIDATES_FT = (20, 50, 100, 200, 250, 500, 1000, 2000, 5000)
SCALE_BAR_MAX_FRACTION = 0.30


# ======================================================================
# Layers
# ======================================================================


def layer(
    layer_id: str,
    geometries: list,
    *,
    kind: str,
    stroke: Optional[str] = None,
    stroke_width: float = 0.75,
    fill: Optional[str] = None,
    fill_opacity: float = 1.0,
    legend=None,
    dash: Optional[str] = None,
    labels: Optional[list] = None,
) -> dict:
    """
    One styled layer. `geometries` are shapely geometries in the DEM's UTM
    CRS; `kind` is "polygon", "line" or "point"; `stroke` and `fill` are
    TOKEN NAMES, never values; `legend` is this layer's legend entry -- a
    string (prose), or a list of summary-style parts (a string is prose, a
    {"value": ...} mapping is a measurement) -- or None for a layer that
    draws without an entry. `labels`, for a line layer, is one string or
    None per geometry, set along the line with the line broken behind it.
    """
    if kind not in ("polygon", "line", "point"):
        raise ValueError(f"layer kind must be polygon, line or point, got {kind!r}")
    if labels is not None:
        if kind != "line":
            raise ValueError("labels are drawn along lines only")
        if len(labels) != len(geometries):
            raise ValueError("labels must be one per geometry")
    return {
        "id": layer_id,
        "kind": kind,
        "geometries": list(geometries),
        "stroke": stroke,
        "stroke_width": float(stroke_width),
        "fill": fill,
        "fill_opacity": float(fill_opacity),
        "legend": legend,
        "dash": dash,
        "labels": list(labels) if labels is not None else None,
    }


# ======================================================================
# Contours
# ======================================================================


def contour_interval_ft(relief_ft: float) -> int:
    """
    The smallest of 2, 5, 10 and 20 ft that puts at most
    CONTOUR_TARGET_MAX_LINES lines across the parcel's relief; 20 ft when
    even that is more. A flat parcel therefore gets 2 ft (few lines, but
    the finest interval the DEM's ~0.8 m vertical accuracy can honestly
    carry -- contour_lines.py's own reasoning), a steep one 20 ft.
    """
    for interval in CONTOUR_INTERVALS_FT:
        if relief_ft / interval <= CONTOUR_TARGET_MAX_LINES:
            return interval
    return CONTOUR_INTERVALS_FT[-1]


def _is_index_level(elevation_ft: float, interval_ft: int) -> bool:
    return round(elevation_ft) % (INDEX_CONTOUR_EVERY * interval_ft) == 0


def parcel_contours(dem: dict, boundary_polygon_utm, interval_ft: Optional[int] = None) -> dict:
    """
    The parcel's contour lines, clipped to the boundary:

        {'relief_ft', 'min_ft', 'max_ft', 'interval_ft', 'line_count',
         'index_count',
         'levels': [{'elevation_ft', 'index': bool, 'geometry'}, ...]}

    `interval_ft` defaults to contour_interval_ft(relief). Levels with no
    line inside the parcel are dropped; `line_count` is the number of
    levels that survive, which is what "lines across the parcel" means.
    """
    span = elevation_range_in_polygon(dem, boundary_polygon_utm)
    if not span:
        return {"relief_ft": 0.0, "min_ft": None, "max_ft": None, "interval_ft": None,
                "line_count": 0, "index_count": 0, "levels": []}
    relief_ft = span["relief_meters"] / METERS_PER_FOOT
    if interval_ft is None:
        interval_ft = contour_interval_ft(relief_ft)
    levels = []
    for contour in compute_contour_lines(dem, interval_meters=interval_ft * METERS_PER_FOOT):
        clipped = contour["lines_utm"].intersection(boundary_polygon_utm)
        clipped = _linear_parts(clipped)
        if clipped is None:
            continue
        elevation_ft = contour["elevation_m"] / METERS_PER_FOOT
        levels.append(
            {"elevation_ft": elevation_ft, "index": _is_index_level(elevation_ft, interval_ft), "geometry": clipped}
        )
    return {
        "relief_ft": relief_ft,
        "min_ft": span["min_meters"] / METERS_PER_FOOT,
        "max_ft": span["max_meters"] / METERS_PER_FOOT,
        "interval_ft": interval_ft,
        "line_count": len(levels),
        "index_count": sum(1 for level in levels if level["index"]),
        "levels": levels,
    }


def contour_layers(contours: dict, legend: Optional[str] = None) -> list:
    """The two contour layers -- intermediate and index -- from
    parcel_contours()' result, both in the terrain token. `legend` names the
    one legend entry the pair shares, or None for no entry."""
    intermediate = [level["geometry"] for level in contours["levels"] if not level["index"]]
    index = [level for level in contours["levels"] if level["index"]]
    return [
        layer("contours", intermediate, kind="line", stroke="terrain", stroke_width=CONTOUR_STROKE_PT, legend=legend),
        layer(
            "index-contours",
            [level["geometry"] for level in index],
            kind="line",
            stroke="terrain",
            stroke_width=INDEX_CONTOUR_STROKE_PT,
            # The elevation in whole feet, set along the line.
            labels=[f"{round(level['elevation_ft']):,}" for level in index],
        ),
    ]


def _linear_parts(geometry):
    """The LineString/MultiLineString part of a clip result, or None when
    nothing linear survived (a contour touching the boundary at a point)."""
    if geometry.is_empty:
        return None
    if isinstance(geometry, (LineString, MultiLineString)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        lines = []
        for part in geometry.geoms:
            if isinstance(part, LineString):
                lines.append(part)
            elif isinstance(part, MultiLineString):
                lines.extend(part.geoms)
        if not lines:
            return None
        return lines[0] if len(lines) == 1 else MultiLineString(lines)
    return None


# ======================================================================
# Scale
# ======================================================================


def scale_bar_feet(meters_per_unit: float, frame_width_pt: float = FRAME_WIDTH_PT) -> int:
    """The largest round length whose bar fits within SCALE_BAR_MAX_FRACTION
    of the frame width; the smallest candidate when none does."""
    max_meters = frame_width_pt * SCALE_BAR_MAX_FRACTION * meters_per_unit
    chosen = SCALE_BAR_CANDIDATES_FT[0]
    for feet in SCALE_BAR_CANDIDATES_FT:
        if feet * METERS_PER_FOOT <= max_meters:
            chosen = feet
    return chosen


class _Projection:
    """UTM metres -> SVG user units (points), uniform scale, y down."""

    def __init__(self, bounds, frame, margin, band):
        minx, miny, maxx, maxy = bounds
        width, height = frame
        avail_w = width - 2 * margin
        avail_h = height - 2 * margin - band
        extent_w = max(maxx - minx, 1e-9)
        extent_h = max(maxy - miny, 1e-9)
        self.scale = min(avail_w / extent_w, avail_h / extent_h)  # units per metre
        drawn_w = extent_w * self.scale
        drawn_h = extent_h * self.scale
        # Centred in the available area, above the furniture band.
        self.offset_x = margin + (avail_w - drawn_w) / 2 - minx * self.scale
        self.offset_y = margin + (avail_h - drawn_h) / 2 + maxy * self.scale
        self.drawn_bbox = (
            margin + (avail_w - drawn_w) / 2,
            margin + (avail_h - drawn_h) / 2,
            margin + (avail_w - drawn_w) / 2 + drawn_w,
            margin + (avail_h - drawn_h) / 2 + drawn_h,
        )

    def xy(self, x, y):
        return (x * self.scale + self.offset_x, -y * self.scale + self.offset_y)

    @property
    def meters_per_unit(self):
        return 1.0 / self.scale


# ======================================================================
# SVG
# ======================================================================


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _ring_path(coords, projection) -> str:
    points = [projection.xy(x, y) for x, y in coords]
    if not points:
        return ""
    head = f"M{_fmt(points[0][0])} {_fmt(points[0][1])}"
    body = " ".join(f"L{_fmt(x)} {_fmt(y)}" for x, y in points[1:])
    return f"{head} {body} Z"


def _line_path(coords, projection) -> str:
    points = [projection.xy(x, y) for x, y in coords]
    if len(points) < 2:
        return ""
    head = f"M{_fmt(points[0][0])} {_fmt(points[0][1])}"
    body = " ".join(f"L{_fmt(x)} {_fmt(y)}" for x, y in points[1:])
    return f"{head} {body}"


def _units_path(points) -> str:
    """An open subpath from points already in SVG units."""
    if len(points) < 2:
        return ""
    head = f"M{_fmt(points[0][0])} {_fmt(points[0][1])}"
    body = " ".join(f"L{_fmt(x)} {_fmt(y)}" for x, y in points[1:])
    return f"{head} {body}"


def _linear_parts_list(geometry) -> list:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        parts = []
        for part in geometry.geoms:
            parts.extend(_linear_parts_list(part))
        return parts
    return []


def _label_width(text: str, size: float) -> float:
    return len(text) * size * MONO_ADVANCE_EM


def _labelled_line(geometry, label: str, projection, size: float) -> tuple:
    """
    The line's parts in SVG units with a gap cut for `label`, and the
    label's placement (x, y, angle_deg) -- or None for the placement when
    no part is long enough to carry it.

    The gap is cut from the LONGEST part at its midpoint: the label sits
    on the line, reads along it, and is flipped to stay upright.
    """
    from shapely.ops import substring

    parts = [
        LineString([projection.xy(x, y) for x, y in part.coords])
        for part in _linear_parts_list(geometry)
        if len(part.coords) >= 2
    ]
    if not parts:
        return [], None
    longest = max(parts, key=lambda p: p.length)
    gap = _label_width(label, size) + 2 * LINE_LABEL_PAD_PT
    if longest.length < gap + 2 * LINE_LABEL_MIN_CLEAR_PT:
        return [list(p.coords) for p in parts], None
    mid = longest.length / 2
    before = substring(longest, 0, mid - gap / 2)
    after = substring(longest, mid + gap / 2, longest.length)
    centre = longest.interpolate(mid)
    ahead = longest.interpolate(min(longest.length, mid + 1.0))
    behind = longest.interpolate(max(0.0, mid - 1.0))
    angle = math.degrees(math.atan2(ahead.y - behind.y, ahead.x - behind.x))
    if angle > 90 or angle <= -90:
        angle += 180.0 if angle <= -90 else -180.0
    drawn = [list(p.coords) for p in parts if p is not longest]
    drawn += [list(before.coords), list(after.coords)]
    return drawn, (centre.x, centre.y, angle)


def _geometry_path(geometry, projection) -> str:
    """One SVG path `d` for a shapely geometry: polygons as closed rings
    (holes included, evenodd), lines as open subpaths."""
    if isinstance(geometry, Polygon):
        parts = [_ring_path(geometry.exterior.coords, projection)]
        parts += [_ring_path(ring.coords, projection) for ring in geometry.interiors]
        return " ".join(p for p in parts if p)
    if isinstance(geometry, MultiPolygon):
        return " ".join(_geometry_path(p, projection) for p in geometry.geoms)
    if isinstance(geometry, LineString):
        return _line_path(geometry.coords, projection)
    if isinstance(geometry, MultiLineString):
        return " ".join(_line_path(line.coords, projection) for line in geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return " ".join(_geometry_path(g, projection) for g in geometry.geoms)
    raise TypeError(f"report_map cannot draw a {type(geometry).__name__} as a path")


def _asterisk(cx: float, cy: float, radius: float, stroke: str, width: float) -> str:
    """The keypoint convention: a plain asterisk, three strokes through
    the centre."""
    lines = []
    for angle in (90, 30, 150):
        dx = math.cos(math.radians(angle)) * radius
        dy = math.sin(math.radians(angle)) * radius
        lines.append(
            f'<line x1="{_fmt(cx - dx)}" y1="{_fmt(cy - dy)}" x2="{_fmt(cx + dx)}" y2="{_fmt(cy + dy)}" '
            f'stroke="{stroke}" stroke-width="{_fmt(width)}" stroke-linecap="round"/>'
        )
    return "".join(lines)


def _colour(tokens: dict, name: Optional[str]) -> str:
    if name is None:
        return "none"
    if name not in tokens:
        raise KeyError(f"report_map: no token named {name!r}; layers name tokens, never values")
    return tokens[name]


def _layer_svg(spec: dict, projection, tokens: dict) -> str:
    stroke = _colour(tokens, spec["stroke"])
    fill = _colour(tokens, spec["fill"])
    dash = f' stroke-dasharray="{spec["dash"]}"' if spec.get("dash") else ""
    pieces = [f'<g id="layer-{escape(spec["id"])}">']
    labels = spec.get("labels") or [None] * len(spec["geometries"])
    for geometry, label in zip(spec["geometries"], labels):
        if geometry is None or geometry.is_empty:
            continue
        if label and spec["kind"] == "line":
            parts, placement = _labelled_line(geometry, label, projection, LINE_LABEL_SIZE_PT)
            d = " ".join(p for p in (_units_path(pts) for pts in parts) if p)
            if d:
                pieces.append(
                    f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{_fmt(spec["stroke_width"])}" '
                    f'stroke-linejoin="round" stroke-linecap="round"{dash}/>'
                )
            if placement:
                x, y, angle = placement
                pieces.append(
                    _text(
                        x, y + LINE_LABEL_SIZE_PT * 0.35, label,
                        font=FONT_DATA, size=LINE_LABEL_SIZE_PT, fill=stroke, anchor="middle",
                        transform=f"rotate({_fmt(angle)} {_fmt(x)} {_fmt(y)})",
                    )
                )
            continue
        if spec["kind"] == "point":
            points = list(geometry.geoms) if isinstance(geometry, MultiPoint) else [geometry]
            for point in points:
                x, y = projection.xy(point.x, point.y)
                pieces.append(_asterisk(x, y, 3.2, stroke, spec["stroke_width"]))
            continue
        d = _geometry_path(geometry, projection)
        if not d:
            continue
        if spec["kind"] == "polygon":
            pieces.append(
                f'<path d="{d}" fill="{fill}" fill-opacity="{_fmt(spec["fill_opacity"])}" fill-rule="evenodd" '
                f'stroke="{stroke}" stroke-width="{_fmt(spec["stroke_width"])}" stroke-linejoin="round"{dash}/>'
            )
        else:
            pieces.append(
                f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{_fmt(spec["stroke_width"])}" '
                f'stroke-linejoin="round" stroke-linecap="round"{dash}/>'
            )
    pieces.append("</g>")
    return "".join(pieces)


def _text(x, y, content, *, font, size, fill, anchor="start", weight=None, transform=None) -> str:
    weight_attr = f' font-weight="{weight}"' if weight else ""
    transform_attr = f' transform="{transform}"' if transform else ""
    return (
        f'<text x="{_fmt(x)}" y="{_fmt(y)}" font-family="{escape(font)}" font-size="{_fmt(size)}" '
        f'fill="{fill}" text-anchor="{anchor}"{weight_attr}{transform_attr}>{escape(str(content))}</text>'
    )


def _north_arrow(frame, tokens) -> str:
    width, _ = frame
    ink = tokens["ink"]
    cx = width - MARGIN_PT - 8.0
    top = MARGIN_PT + 4.0
    length = 20.0
    return (
        f'<g id="north-arrow">'
        f'<line x1="{_fmt(cx)}" y1="{_fmt(top + length)}" x2="{_fmt(cx)}" y2="{_fmt(top + 6)}" '
        f'stroke="{ink}" stroke-width="0.8"/>'
        f'<path d="M{_fmt(cx)} {_fmt(top)} L{_fmt(cx - 3.2)} {_fmt(top + 7.5)} L{_fmt(cx + 3.2)} {_fmt(top + 7.5)} Z" fill="{ink}"/>'
        + _text(cx, top + length + NORTH_SIZE_PT + 1.5, "N", font=FONT_PROSE, size=NORTH_SIZE_PT, fill=ink, anchor="middle")
        + "</g>"
    )


def _scale_bar(frame, projection, tokens) -> tuple:
    """The scale bar SVG and its measurements {feet, units}."""
    width, height = frame
    ink = tokens["ink"]
    feet = scale_bar_feet(projection.meters_per_unit, width)
    units = feet * METERS_PER_FOOT / projection.meters_per_unit
    right = width - MARGIN_PT
    left = right - units
    y = height - MARGIN_PT - 9.0
    svg = (
        f'<g id="scale-bar">'
        f'<line x1="{_fmt(left)}" y1="{_fmt(y)}" x2="{_fmt(right)}" y2="{_fmt(y)}" stroke="{ink}" stroke-width="0.9"/>'
        f'<line x1="{_fmt(left)}" y1="{_fmt(y - 3.5)}" x2="{_fmt(left)}" y2="{_fmt(y + 3.5)}" stroke="{ink}" stroke-width="0.9"/>'
        f'<line x1="{_fmt(right)}" y1="{_fmt(y - 3.5)}" x2="{_fmt(right)}" y2="{_fmt(y + 3.5)}" stroke="{ink}" stroke-width="0.9"/>'
        + _text((left + right) / 2, y - 5.5, f"{feet:,} ft", font=FONT_DATA, size=LABEL_SIZE_PT, fill=ink, anchor="middle")
        + "</g>"
    )
    return svg, {"feet": feet, "units": units}


def _swatch(spec: dict, tokens: dict) -> str:
    """One legend swatch: a small standalone SVG drawn from the layer's own
    style -- a tinted square for a polygon layer, a stroke for a line, the
    asterisk for points."""
    w, h = LEGEND_SWATCH_W_PT, LEGEND_SWATCH_H_PT
    stroke = _colour(tokens, spec["stroke"])
    fill = _colour(tokens, spec["fill"])
    dash = f' stroke-dasharray="{spec["dash"]}"' if spec.get("dash") else ""
    if spec["kind"] == "polygon":
        body = (
            f'<rect x="0.25" y="0.25" width="{_fmt(w - 0.5)}" height="{_fmt(h - 0.5)}" fill="{fill}" '
            f'fill-opacity="{_fmt(spec["fill_opacity"])}" stroke="{stroke}" stroke-width="0.5"/>'
        )
    elif spec["kind"] == "line":
        body = (
            f'<line x1="0" y1="{_fmt(h / 2)}" x2="{_fmt(w)}" y2="{_fmt(h / 2)}" '
            f'stroke="{stroke}" stroke-width="{_fmt(spec["stroke_width"])}"{dash}/>'
        )
    else:
        body = _asterisk(w / 2, h / 2, 3.2, stroke, spec["stroke_width"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(w)}pt" height="{_fmt(h)}pt" '
        f'viewBox="0 0 {_fmt(w)} {_fmt(h)}" class="report-map__swatch">{body}</svg>'
    )


def legend_entries(layers: list, tokens: dict) -> list:
    """The legend, in layer order: [{'id', 'swatch': svg, 'parts': [...]}]
    for every layer that names an entry. A string legend becomes a
    one-part prose label."""
    entries = []
    for spec in layers:
        legend = spec.get("legend")
        if not legend:
            continue
        parts = [legend] if isinstance(legend, str) else list(legend)
        entries.append({"id": spec["id"], "swatch": _swatch(spec, tokens), "parts": parts})
    return entries


def render_map(boundary_polygon_utm, layers: list, tokens: dict, frame: tuple = FRAME) -> dict:
    """
    The map, and its measurements:

        {'svg': str,
         'frame': (w, h),                 # pt
         'meters_per_unit': float,        # ground metres per SVG user unit
         'extent_utm': (minx, miny, maxx, maxy),
         'drawn_bbox': (x0, y0, x1, y1),  # where the boundary's bbox landed
         'scale_bar': {'feet': int, 'units': float},
         'legend': legend_entries(layers, tokens)}

    Layers draw in the order given, under the boundary; the boundary, the
    north arrow and the scale bar draw last. The frame's outline is a
    hairline in the rule token. The legend is returned, not drawn: the
    map.html macro sets it below the frame.
    """
    width, height = frame
    projection = _Projection(boundary_polygon_utm.bounds, frame, MARGIN_PT, FURNITURE_BAND_PT)
    ink = tokens["ink"]
    rule = tokens["rule"]
    page = tokens["page"]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}pt" height="{_fmt(height)}pt" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" role="img" aria-label="Site map">',
        f'<rect x="0" y="0" width="{_fmt(width)}" height="{_fmt(height)}" fill="{page}" stroke="none"/>',
    ]
    for spec in layers:
        parts.append(_layer_svg(spec, projection, tokens))
    boundary_d = _geometry_path(boundary_polygon_utm, projection)
    parts.append(
        f'<path id="parcel-boundary" d="{boundary_d}" fill="none" stroke="{ink}" '
        f'stroke-width="{_fmt(BOUNDARY_STROKE_PT)}" stroke-linejoin="round"/>'
    )
    parts.append(_north_arrow(frame, tokens))
    scale_svg, scale_bar = _scale_bar(frame, projection, tokens)
    parts.append(scale_svg)
    parts.append(
        f'<rect x="{_fmt(FRAME_STROKE_PT / 2)}" y="{_fmt(FRAME_STROKE_PT / 2)}" '
        f'width="{_fmt(width - FRAME_STROKE_PT)}" height="{_fmt(height - FRAME_STROKE_PT)}" '
        f'fill="none" stroke="{rule}" stroke-width="{_fmt(FRAME_STROKE_PT)}"/>'
    )
    parts.append("</svg>")
    return {
        "svg": "".join(parts),
        "frame": frame,
        "meters_per_unit": projection.meters_per_unit,
        "extent_utm": boundary_polygon_utm.bounds,
        "drawn_bbox": projection.drawn_bbox,
        "scale_bar": scale_bar,
        "legend": legend_entries(layers, tokens),
    }
