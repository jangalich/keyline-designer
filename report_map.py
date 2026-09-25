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

SCREENED TINTS (branch 11). A `screen` layer is a polygon drawn as a
dot screen -- rows of round dots at SCREEN_SPACING_PT, the mid-century
convention for woodland -- rather than a flat fill, so contours and
linework beneath and above it read through the gaps. The dots are
GEOMETRY, not an SVG pattern: rows are cut across the polygon in the
DEM's metres at the spacing the frame's scale gives, clipped to it, and
each row is one stroked path with a zero-length dash and round caps,
its dash offset set so the dots fall on one grid across every row and
every polygon. The screen's weight is the dot's diameter
(`screen_dot_pt`): a graduated ramp is several screen layers of one
token at growing dot sizes, the way Landform's slope ramp is one token at
growing opacities, and the darkest is capped by the caller so a contour
stays legible over it. A screen's legend swatch is drawn with the same
dots.

LABELLED LINES. A line layer may carry `labels`, one per geometry (a
string or None): the label is set along the line at the midpoint of its
longest part, in the data face, and the line is BROKEN behind it -- a gap
the width of the label plus padding is cut from the geometry rather than
masked with a page-coloured box, so nothing opaque sits over the tints
beneath. The index contours use this for their elevations in whole feet.
A part too short to hold its label with clear line either side is left
unlabelled rather than crowded.

LABELLED POLYGONS (branch 12). A polygon layer may carry `labels` the
same way, and the label is set INSIDE the polygon at its POLE OF
INACCESSIBILITY -- the centre of the largest circle the polygon
contains, which is the point furthest from any edge. Not the centroid: a
centroid falls outside a crescent and hugs the notch of an L, and the
published soil surveys this is built for set a map unit's symbol where
the unit is widest. polygon_pole() finds it by a coarse-to-fine grid
search in the geometry's own metres, with no dependency beyond shapely.

A POLYGON LABEL IS KNOCKED OUT of what it sits on -- a stroked copy in
the page colour under the glyphs -- because unlike a line label it has
nothing to break behind it: it lands on a tint, on contours, and on
whatever those contours' own labels have already put there.

A POLYGON TOO SMALL TO HOLD ITS LABEL IS LEFT UNLABELLED, the same rule
the lines follow and for the same reason: crowding a 0.06-acre sliver
with three characters costs more than the sliver's symbol is worth, and
the section's own table carries every symbol anyway. The alternative --
a leader line to a label set outside the polygon, which is what the
published sheets do -- would put collision handling into a shared
component for the smallest shapes on the page. The label fits when the
pole's radius covers half the diagonal of the label's box; render_map()
reports what was placed and what was dropped in `labels_placed`, exactly
as it does for lines, so a section can state the count in its caption.

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
    Point,
    Polygon,
)
from shapely.ops import unary_union

from contour_lines import compute_contour_lines
from raster_grid import elevation_range_in_polygon

METERS_PER_FOOT = 0.3048

# The frame, in points: the Letter page's content width at the report's
# 0.85 in side margins (8.5 in - 1.7 in = 6.8 in = 489.6 pt) by a height
# that leaves the page room for a heading and a table above or below.
FRAME_WIDTH_PT = 489.6
FRAME_HEIGHT_PT = 340.0
FRAME = (FRAME_WIDTH_PT, FRAME_HEIGHT_PT)

# THE LAYOUT MAP'S FRAME (branch 13), and it is NOT ON THE SHARED SCALE.
# Every inventory section draws the parcel in FRAME, fitted, at one scale,
# so a reader can lay Landform over Water over Soils. The layout map is the
# deliverable -- the design, on its land, to take to the field -- and it
# takes the full content width and most of the page's height instead,
# drawn at whatever scale that gives, which its own scale bar states. This
# is deliberate: do not "fix" it into line with FRAME. It is drawn with
# fit=False, so the width is never cut either.
LAYOUT_FRAME = (FRAME_WIDTH_PT, 520.0)

# Inside the frame: the margin around the parcel extent, and the band along
# the bottom reserved for the scale bar so it is never drawn over the
# parcel. The legend lives below the frame, not in this band.
MARGIN_PT = 14.0
FURNITURE_BAND_PT = 22.0
# THE FRAME IS NARROWED TO THE PARCEL. A tall parcel in the wide frame
# left 126 m of empty ground either side of the reference boundary --
# room a section with context beyond the parcel would fill, and a blank
# that a section without it read as a failure. The frame's width is cut
# so that no more than this much ground shows beyond the parcel's bbox on
# either side; the height, and so the scale, never change, and every
# section's map of one parcel gets the same narrowed frame (fitted_frame).
MAX_CONTEXT_MARGIN_M = 50.0

# The legend strip below the frame: swatch geometry in points, shared by
# the map.html macro's layout (report.css sizes the strip to match).
LEGEND_SWATCH_W_PT = 14.0
LEGEND_SWATCH_H_PT = 8.0

# Line weights, in points.
BOUNDARY_STROKE_PT = 1.1
CONTOUR_STROKE_PT = 0.45
INDEX_CONTOUR_STROKE_PT = 0.95
FRAME_STROKE_PT = 0.5
FURNITURE_CASING_PT = 1.2  # the page-coloured casing each side of the boundary and furniture, when halo=True
ASTERISK_RADIUS_PT = 3.2
DOT_RADIUS_PT = 2.4
DOT_HALO_PT = 1.1          # a page-coloured ring so a dot on a line reads as a point, not a thickening
POINT_LABEL_GAP_PT = 3.0
GLYPH_HALF_PT = 3.4        # the building glyph's half-width (branch 13's layout map)

# Type, in points. The prose face for names, the data face for figures.
FONT_PROSE = "Source Serif 4"
FONT_DATA = "IBM Plex Mono"
LABEL_SIZE_PT = 7.5
NORTH_SIZE_PT = 8.5
LINE_LABEL_SIZE_PT = 6.5
# A polygon label's knock-out, as a fraction of its size: wide enough to
# clear a contour and its own label, narrow enough not to eat the tint.
HALO_WIDTH_EM = 0.38
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

# The dot screen: rows this far apart, in points, alternate rows offset by
# half a spacing so the dots sit on a staggered grid. A dot's diameter is
# the layer's own `screen_dot_pt`; coverage is pi r^2 / spacing^2, so a
# 1.7 pt dot on a 3 pt grid covers about a quarter of the ground.
SCREEN_SPACING_PT = 3.0

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
    marker: str = "asterisk",
    stroke_opacity: float = 1.0,
    screen_dot_pt: float = 1.2,
    casing_pt: float = 0.0,
    hatch_spacing_pt: float = 3.2,
    hatch_angle_deg: float = 45.0,
    label_halo: bool = False,
    casing_opacity: float = 1.0,
    pattern: Optional[dict] = None,
    pattern_opacity: float = 1.0,
    marker_size_pt: Optional[float] = None,
    marker_halo_pt: Optional[float] = None,
    casing_rim: bool = False,
    label_face: str = "data",
) -> dict:
    """
    One styled layer. `geometries` are shapely geometries in the DEM's UTM
    CRS; `kind` is "polygon", "line" or "point"; `stroke` and `fill` are
    TOKEN NAMES, never values; `legend` is this layer's legend entry -- a
    string (prose), or a list of summary-style parts (a string is prose, a
    {"value": ...} mapping is a measurement) -- or None for a layer that
    draws without an entry. `labels`, for a line layer, is one string or
    None per geometry, set along the line with the line broken behind it;
    for a polygon layer, one per polygon, set inside it at its pole of
    inaccessibility and dropped when the polygon cannot hold it; for a
    point layer, one string or None per point, set beside the marker.
    `marker`, for a point layer, is "asterisk" (the layout map's keypoint
    convention) or "dot" (a small filled circle in the stroke token, for a
    point that sits where two lines meet). `stroke_opacity` below 1 sets
    a line back, for linework that is context rather than subject. A
    "screen" layer is a polygon drawn as a dot screen in the `fill`
    token, dots `screen_dot_pt` across on the SCREEN_SPACING_PT grid (see
    the module docstring); `stroke` and `fill_opacity` do not apply to it.

    THE LAYOUT MAP'S OPTIONS (branch 13), each off by default so every
    other section's SVG is byte for byte what it was. `casing_pt` draws
    every mark of the layer first in the page colour, that many points
    wider on each side -- a halo casing, which is what keeps a mark legible
    over photography, where no single ink wins against every patch of
    ground. A "hatch" layer is a polygon filled with parallel lines in the
    `fill` token, `hatch_spacing_pt` apart at `hatch_angle_deg`, each
    `stroke_width` wide, with no outline unless `stroke` names one; like a
    screen, the lines are GEOMETRY clipped to the polygon, not an SVG
    pattern. `marker="glyph"` is a filled building glyph in the stroke
    token. `label_halo` knocks a point layer's labels out of what they sit
    on, the way a polygon label always is. `casing_opacity` below 1 sets a
    casing back, for linework that is context rather than design. A hatch
    or a screen may carry
    `labels` like a polygon layer: set inside each shape at its pole, in
    the layer's `fill` token, knocked out, dropped when the shape cannot
    hold it.

    THE LAYOUT MAP'S TILED MARKS (branch 16), off by default like the rest.
    A "pattern" layer is a polygon filled with an SVG <pattern> in
    userSpaceOnUse -- a tile FIXED ON THE PAGE, not on the ground, which is
    how the interactive map's paint servers are fixed on the screen. The
    tile is `pattern`, one of

        {"type": "hatch", "tile_pt", "weight_pt", "rise": "up" | "down",
         "screen": {"token", "opacity"} | None}
        {"type": "dots", "tile_pt", "grid", "radius_pt",
         "screen": {"token", "opacity"} | None}

    drawn in the `fill` token, the screen (a full-tile rect under the marks)
    in its own token. `pattern_opacity` scales the screen and the marks
    together, as the interactive map's fill-opacity does. `stroke`, when
    named, is the polygon's edge at `stroke_width` and `stroke_opacity`; a
    hatch names none, because its edge is where the hatching stops.
    `marker="pin"` is the interactive map's teardrop, `marker_size_pt` tall,
    its tip on the point, on a page-coloured halo; `marker="disc"` is a
    filled circle `marker_size_pt` across, ringed `marker_halo_pt` wide in
    the page colour inside that size.

    `label_face` (branch 17) sets a line layer's labels in the "data" face
    (the default: an elevation is a measurement) or the "prose" face (a
    road's name is a word -- the site overview's context map).
    `casing_rim` draws a line's casing as a RIM: the ring between the
    line's own outline and the casing's, filled in the page colour at
    `casing_opacity`, with nothing under the line itself. A stroked casing
    lies under the whole width of the line, so a translucent line over it
    reads as a washed-out line with a faded core; a rim leaves the core over the
    ground, as an uncased line is, and separates only its edges.
    """
    if kind not in ("polygon", "line", "point", "screen", "hatch", "pattern"):
        raise ValueError(f"layer kind must be polygon, line, point, screen, hatch or pattern, got {kind!r}")
    if kind == "pattern" and (fill is None or not pattern or pattern.get("type") not in ("hatch", "dots")):
        raise ValueError("a pattern layer names its token and a hatch or dots tile")
    if kind == "hatch" and fill is None:
        raise ValueError("a hatch layer names the token its lines are drawn in")
    if kind == "screen" and fill is None:
        raise ValueError("a screen layer names the token its dots are drawn in")
    if label_face not in ("data", "prose"):
        raise ValueError(f"label_face must be data or prose, got {label_face!r}")
    if marker not in ("asterisk", "dot", "glyph", "pin", "disc"):
        raise ValueError(f"marker must be asterisk, dot, glyph, pin or disc, got {marker!r}")
    if labels is not None:
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
        "marker": marker,
        "stroke_opacity": float(stroke_opacity),
        "screen_dot_pt": float(screen_dot_pt),
        "casing_pt": float(casing_pt),
        "hatch_spacing_pt": float(hatch_spacing_pt),
        "hatch_angle_deg": float(hatch_angle_deg),
        "label_halo": bool(label_halo),
        "casing_opacity": float(casing_opacity),
        "pattern": dict(pattern) if pattern else None,
        "pattern_opacity": float(pattern_opacity),
        "marker_size_pt": marker_size_pt,
        "marker_halo_pt": marker_halo_pt,
        "casing_rim": bool(casing_rim),
        "label_face": label_face,
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


# The pole search: the widest part is found on a coarse grid over the
# bounding box and refined by halving the step around the best point so
# far, which needs no dependency beyond shapely. Twelve halvings take the
# step below a millimetre on any parcel-sized polygon.
POLE_GRID_STEPS = 16
POLE_REFINEMENTS = 12
# A label fits when the pole's inscribed radius covers half the diagonal
# of its box, plus this much clear ground all round.
POLYGON_LABEL_PAD_PT = 1.5


def _largest_part(geometry):
    """The biggest polygon of a MultiPolygon, or the polygon itself. A map
    unit clipped to a boundary can arrive as several disjoint fragments;
    its symbol goes in the one with room for it."""
    if isinstance(geometry, Polygon):
        return geometry if not geometry.is_empty else None
    if isinstance(geometry, MultiPolygon):
        parts = [g for g in geometry.geoms if not g.is_empty]
        return max(parts, key=lambda g: g.area) if parts else None
    if isinstance(geometry, GeometryCollection):
        parts = [g for g in geometry.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        return _largest_part(unary_union(parts)) if parts else None
    return None


def polygon_pole(geometry):
    """
    (x, y, radius) of the polygon's POLE OF INACCESSIBILITY in the
    geometry's own units -- the centre of the largest circle the polygon
    contains, and that circle's radius -- or None for an empty geometry.

    The point furthest from any edge, which is where a label has the most
    room and where a published soil survey sets a map unit's symbol. A
    centroid will not do: it falls outside a crescent entirely, and on an
    L-shaped unit it lands in the notch hard against an edge. Holes count
    as edges, because `boundary` carries the interior rings too.
    """
    polygon = _largest_part(geometry)
    if polygon is None or polygon.area <= 0:
        return None
    minx, miny, maxx, maxy = polygon.bounds
    step = max(maxx - minx, maxy - miny) / POLE_GRID_STEPS
    if step <= 0:
        return None
    seed = polygon.representative_point()
    best = (seed.x, seed.y)
    best_distance = polygon.boundary.distance(seed)
    candidates = []
    x = minx + step / 2
    while x < maxx:
        y = miny + step / 2
        while y < maxy:
            candidates.append((x, y))
            y += step
        x += step
    for _ in range(POLE_REFINEMENTS):
        for cx, cy in candidates:
            point = Point(cx, cy)
            if not polygon.contains(point):
                continue
            distance = polygon.boundary.distance(point)
            if distance > best_distance:
                best_distance, best = distance, (cx, cy)
        step /= 2.0
        candidates = [
            (best[0] + dx * step, best[1] + dy * step)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)
        ]
    return (best[0], best[1], best_distance)


def polygon_label_radius_pt(label: str, size: float = LINE_LABEL_SIZE_PT) -> float:
    """The inscribed radius, in points, a polygon needs to hold `label`:
    half the diagonal of the label's box plus clear ground."""
    return math.hypot(_label_width(label, size) / 2, size / 2) + POLYGON_LABEL_PAD_PT


def _labelled_polygon(geometry, label: str, projection, size: float):
    """(x, y) in SVG units for `label` inside `geometry`, or None when the
    polygon cannot hold it clear of its own edges."""
    pole = polygon_pole(geometry)
    if pole is None:
        return None
    x, y, radius_m = pole
    if radius_m / projection.meters_per_unit < polygon_label_radius_pt(label, size):
        return None
    return projection.xy(x, y)


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


def _dot(cx: float, cy: float, radius: float, fill: str, halo: str) -> str:
    """The keyline-structure convention: a small filled circle with a
    page-coloured halo, so it reads as a point sitting on a line."""
    return (
        f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(radius + DOT_HALO_PT)}" fill="{halo}" stroke="none"/>'
        f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(radius)}" fill="{fill}" stroke="none"/>'
    )


def _glyph_path(cx: float, cy: float, half: float) -> str:
    """A building: a square body under a pitched roof, `half` the body's
    half-width, centred on the point."""
    top = cy - half * 0.35
    return (f"M{_fmt(cx - half)} {_fmt(cy + half)} L{_fmt(cx + half)} {_fmt(cy + half)} "
            f"L{_fmt(cx + half)} {_fmt(top)} L{_fmt(cx)} {_fmt(top - half * 0.95)} "
            f"L{_fmt(cx - half)} {_fmt(top)} Z")


def _glyph(cx: float, cy: float, fill: str, halo: str) -> str:
    """The structure convention on the layout map: a filled building glyph
    with a page-coloured halo, so it holds over photography."""
    d = _glyph_path(cx, cy, GLYPH_HALF_PT)
    return (f'<path d="{d}" fill="{halo}" stroke="{halo}" stroke-width="{_fmt(2 * DOT_HALO_PT)}" stroke-linejoin="round"/>'
            f'<path d="{d}" fill="{fill}" stroke="none"/>')


# THE PIN SILHOUETTE (branch 16): assets/icons/farm_location_pin.svg's
# teardrop in its 24-unit viewBox, the path the interactive map draws
# verbatim (ProductionHatchPattern.PIN_GLYPH_PATH). The tip is at (12, 22)
# and is set on the point, so the pin points at the site.
PIN_PATH = "M12 2C8.13401 2 5 5.13401 5 9C5 14.25 12 22 12 22C12 22 19 14.25 19 9C19 5.13401 15.866 2 12 2Z"
PIN_VIEWBOX = 24.0
PIN_TIP = (12.0, 22.0)
PIN_HALO_UNITS = 4.0      # the halo's stroke in viewBox units, half of it outside the body (SITE_PIN_HALO_WIDTH)
PIN_HEAD_CENTRE_Y = 9.0   # the head's centre in viewBox units, where a label is placed around
PIN_HEAD_HALF_W = 7.0     # the head's half-width in viewBox units


def _pin(x: float, y: float, size: float, fill: str, halo: str) -> str:
    """The teardrop, `size` tall, its tip on (x, y), on a halo stroke."""
    k = size / PIN_VIEWBOX
    transform = f"translate({_fmt(x - PIN_TIP[0] * k)} {_fmt(y - PIN_TIP[1] * k)}) scale({k:.4f})"
    return (f'<g transform="{transform}">'
            f'<path d="{PIN_PATH}" fill="none" stroke="{halo}" stroke-width="{_fmt(PIN_HALO_UNITS)}" stroke-linejoin="round"/>'
            f'<path d="{PIN_PATH}" fill="{fill}" stroke="none"/></g>')


def _disc(x: float, y: float, size: float, ring: float, fill: str, halo: str) -> str:
    """A filled circle `size` across overall, its outer `ring` the halo --
    a CSS border-box circle, which is how the access point's 18 px is
    measured on screen."""
    outer = size / 2
    return (f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(outer)}" fill="{halo}" stroke="none"/>'
            f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{_fmt(outer - ring)}" fill="{fill}" stroke="none"/>')


def _marker_anchor(spec: dict, x: float, y: float) -> tuple:
    """(x, y, r) of the box a point's label is placed around: the marker
    itself, or for a pin its head, which sits above the point."""
    if spec.get("marker") == "pin":
        k = spec["marker_size_pt"] / PIN_VIEWBOX
        return x, y - (PIN_TIP[1] - PIN_HEAD_CENTRE_Y) * k, (PIN_HEAD_HALF_W + PIN_HALO_UNITS / 2) * k
    return x, y, _marker_radius(spec)


def _marker(spec: dict, x: float, y: float, stroke: str, tokens: dict) -> str:
    if spec.get("marker") == "pin":
        return _pin(x, y, spec["marker_size_pt"], stroke, _colour(tokens, "halo" if "halo" in tokens else "page"))
    if spec.get("marker") == "disc":
        return _disc(x, y, spec["marker_size_pt"], spec["marker_halo_pt"] or 0.0, stroke,
                     _colour(tokens, "halo" if "halo" in tokens else "page"))
    if spec.get("marker") == "dot":
        return _dot(x, y, DOT_RADIUS_PT, stroke, _colour(tokens, "page"))
    if spec.get("marker") == "glyph":
        return _glyph(x, y, stroke, _colour(tokens, "page"))
    return _asterisk(x, y, ASTERISK_RADIUS_PT, stroke, spec["stroke_width"])


def _marker_radius(spec: dict) -> float:
    if spec.get("marker") == "disc":
        return spec["marker_size_pt"] / 2
    if spec.get("marker") == "pin":
        return spec["marker_size_pt"] / 2
    if spec.get("marker") == "glyph":
        return GLYPH_HALF_PT + DOT_HALO_PT
    return DOT_RADIUS_PT + DOT_HALO_PT if spec.get("marker") == "dot" else ASTERISK_RADIUS_PT


def _colour(tokens: dict, name: Optional[str]) -> str:
    if name is None:
        return "none"
    if name not in tokens:
        raise KeyError(f"report_map: no token named {name!r}; layers name tokens, never values")
    return tokens[name]


def _screen_rows(geometry, projection) -> list:
    """The dot rows of a screen over one polygon: [(units path, dash
    offset), ...], rows SCREEN_SPACING_PT apart in the frame, alternate
    rows staggered by half a spacing, each clipped to the polygon and its
    dash offset set so the dots land on the frame-wide grid."""
    spacing_m = SCREEN_SPACING_PT * projection.meters_per_unit
    minx, miny, maxx, maxy = geometry.bounds
    rows = []
    # Row positions on a frame-wide grid, so two polygons' screens align.
    first = math.floor(miny / spacing_m)
    last = math.ceil(maxy / spacing_m)
    for index in range(first, last + 1):
        y = index * spacing_m
        if y < miny or y > maxy:
            continue
        clipped = geometry.intersection(LineString([(minx - spacing_m, y), (maxx + spacing_m, y)]))
        for part in _linear_parts_list(clipped):
            coords = list(part.coords)
            if len(coords) < 2:
                continue
            (x0, y0), (x1, y1) = coords[0], coords[-1]
            if x1 < x0:
                x0, x1 = x1, x0
            start = projection.xy(x0, y0)
            end = projection.xy(x1, y1)
            stagger = SCREEN_SPACING_PT / 2 if index % 2 else 0.0
            offset = (start[0] - stagger) % SCREEN_SPACING_PT
            rows.append((f"M {_fmt(start[0])} {_fmt(start[1])} L {_fmt(end[0])} {_fmt(end[1])}", offset))
    return rows


def _screen_svg(spec: dict, projection, fill: str) -> list:
    pieces = []
    dot = spec.get("screen_dot_pt", 1.2)
    for geometry in spec["geometries"]:
        if geometry is None or geometry.is_empty:
            continue
        polygons = [geometry] if isinstance(geometry, Polygon) else [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon)]
        for polygon in polygons:
            for d, offset in _screen_rows(polygon, projection):
                pieces.append(
                    f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{_fmt(dot)}" stroke-linecap="round" '
                    f'stroke-dasharray="0 {_fmt(SCREEN_SPACING_PT)}" stroke-dashoffset="{_fmt(offset)}"/>'
                )
    return pieces


def _hatch_lines(geometry, projection, spacing_pt: float, angle_deg: float) -> list:
    """The hatch lines over one polygon, in the geometry's metres: parallel
    lines `spacing_pt` apart on the page at `angle_deg` from the x axis,
    on a grid anchored at the CRS origin so neighbouring polygons' hatches
    line up, each clipped to the polygon."""
    spacing_m = spacing_pt * projection.meters_per_unit
    theta = math.radians(angle_deg)
    # Unit normal to the lines; line k is every point p with p . n = k * spacing.
    nx, ny = -math.sin(theta), math.cos(theta)
    dx, dy = math.cos(theta), math.sin(theta)
    minx, miny, maxx, maxy = geometry.bounds
    along = [x * nx + y * ny for x, y in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy))]
    reach = math.hypot(maxx - minx, maxy - miny) + spacing_m
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    lines = []
    for k in range(math.floor(min(along) / spacing_m), math.ceil(max(along) / spacing_m) + 1):
        offset = k * spacing_m - (cx * nx + cy * ny)
        px, py = cx + nx * offset, cy + ny * offset
        segment = LineString([(px - dx * reach, py - dy * reach), (px + dx * reach, py + dy * reach)])
        lines.extend(_linear_parts_list(geometry.intersection(segment)))
    return lines


def _hatch_svg(spec: dict, projection, tokens: dict, sink: Optional[list] = None) -> list:
    fill = _colour(tokens, spec["fill"])
    paths = []
    for geometry in spec["geometries"]:
        if geometry is None or geometry.is_empty:
            continue
        lines = _hatch_lines(geometry, projection, spec["hatch_spacing_pt"], spec["hatch_angle_deg"])
        d = " ".join(p for p in (_line_path(line.coords, projection) for line in lines) if p)
        if d:
            paths.append(d)
    pieces = []
    width = spec["stroke_width"]
    if spec.get("casing_pt"):
        page = _colour(tokens, "page")
        pieces += [f'<path d="{d}" fill="none" stroke="{page}" stroke-width="{_fmt(width + 2 * spec["casing_pt"])}" '
                   f'stroke-linecap="butt"/>' for d in paths]
    pieces += [f'<path d="{d}" fill="none" stroke="{fill}" stroke-width="{_fmt(width)}" stroke-linecap="butt"/>' for d in paths]
    return pieces + _tint_labels(spec, projection, tokens, sink)


def _pattern_tile(spec: dict, tokens: dict) -> str:
    """The <pattern> a pattern layer fills with, in userSpaceOnUse: fixed on
    the page, so every shape on the map shares one grid, as every zone on
    the interactive map shares the screen's. The screen first, so the marks
    sit on it.

    THE HATCH TILE is the interactive map's rulingPath(): one diagonal
    corner to corner and a stub past each of the two corners it misses, so
    the rules join across tile edges into continuous lines. "up" rises to
    the right ("/"), "down" falls ("\\"), the mirror in y the whole of the
    difference. THE DOT TILE is stippleTile(): a grid x grid lattice, one
    dot at the centre of each cell."""
    tile = spec["pattern"]
    size = tile["tile_pt"]
    colour = _colour(tokens, spec["fill"])
    body = []
    screen = tile.get("screen")
    if screen:
        body.append(f'<rect x="0" y="0" width="{_fmt(size)}" height="{_fmt(size)}" '
                    f'fill="{_colour(tokens, screen["token"])}" fill-opacity="{_fmt(screen["opacity"])}"/>')
    if tile["type"] == "hatch":
        reach = tile["weight_pt"]

        def y(value):
            return size - value if tile.get("rise", "up") == "down" else value

        d = (f"M0 {_fmt(y(size))} L{_fmt(size)} {_fmt(y(0))} "
             f"M{_fmt(-reach)} {_fmt(y(reach))} L{_fmt(reach)} {_fmt(y(-reach))} "
             f"M{_fmt(size - reach)} {_fmt(y(size + reach))} L{_fmt(size + reach)} {_fmt(y(size - reach))}")
        body.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="{_fmt(tile["weight_pt"])}" '
                    f'stroke-linecap="square"/>')
    else:
        cell = size / tile["grid"]
        radius = _fmt(tile["radius_pt"])
        body += [f'<circle cx="{_fmt((col + 0.5) * cell)}" cy="{_fmt((row + 0.5) * cell)}" r="{radius}" fill="{colour}"/>'
                 for row in range(tile["grid"]) for col in range(tile["grid"])]
    return (f'<pattern id="pattern-{escape(spec["id"])}" patternUnits="userSpaceOnUse" x="0" y="0" '
            f'width="{_fmt(size)}" height="{_fmt(size)}">' + "".join(body) + "</pattern>")


def _pattern_svg(spec: dict, projection, tokens: dict, sink: Optional[list] = None) -> list:
    """A pattern layer: its tile in a <defs>, each shape filled with it at
    `pattern_opacity`, and the shape's edge when the layer names one."""
    pieces = ["<defs>" + _pattern_tile(spec, tokens) + "</defs>"]
    edge = spec["stroke"]
    for geometry in spec["geometries"]:
        if geometry is None or geometry.is_empty:
            continue
        d = _geometry_path(geometry, projection)
        if not d:
            continue
        pieces.append(f'<path d="{d}" fill="url(#pattern-{escape(spec["id"])})" '
                      f'fill-opacity="{_fmt(spec["pattern_opacity"])}" fill-rule="evenodd" stroke="none"/>')
        if edge:
            faint = spec.get("stroke_opacity", 1.0)
            faint = f' stroke-opacity="{_fmt(faint)}"' if faint < 1.0 else ""
            pieces.append(f'<path d="{d}" fill="none" stroke="{_colour(tokens, edge)}" '
                          f'stroke-width="{_fmt(spec["stroke_width"])}" stroke-linejoin="round"{faint}/>')
    return pieces + _tint_labels(spec, projection, tokens, sink)


def _defer_area_label(sink: list, geometry, placement, label: str, fill: str, layer_id: str, projection) -> None:
    """A shape's label for _place_labels(): inside at its pole when it
    fits there, otherwise BESIDE the pole like a point's -- on a map whose
    job is to name what it draws, a narrow shape is named beside itself
    rather than left anonymous."""
    if placement:
        sink.append({"kind": "area", "x": placement[0], "y": placement[1], "text": label, "fill": fill, "layer": layer_id})
        return
    pole = polygon_pole(geometry)
    if pole is None:
        return
    x, y = projection.xy(pole[0], pole[1])
    sink.append({"kind": "point", "x": x, "y": y, "r": 0.0, "text": label, "fill": fill, "layer": layer_id, "beside": True})


def _tint_labels(spec: dict, projection, tokens: dict, sink: Optional[list] = None) -> list:
    """A hatch's or a screen's labels, inside each shape as a polygon's
    are, knocked out, in the tint's own token."""
    fill = _colour(tokens, spec["fill"])
    pieces = []
    for geometry, label in zip(spec["geometries"], spec.get("labels") or []):
        if not label or geometry is None or geometry.is_empty:
            continue
        placement = _labelled_polygon(geometry, label, projection, LINE_LABEL_SIZE_PT)
        if sink is not None:
            _defer_area_label(sink, geometry, placement, label, fill, spec["id"], projection)
            continue
        if placement:
            lx, ly = placement
            pieces.append(_text(lx, ly + LINE_LABEL_SIZE_PT * 0.35, label, font=FONT_DATA, size=LINE_LABEL_SIZE_PT,
                                fill=fill, anchor="middle", halo=_colour(tokens, "page")))
    return pieces


def _casing_svg(spec: dict, projection, tokens: dict) -> str:
    """A layer's halo casing: its strokes (or, for a screen, its dots) in
    the page colour, `casing_pt` wider on every side, drawn beneath it. A
    hatch cases its own lines; a point marker carries its own halo."""
    casing = spec.get("casing_pt") or 0.0
    if casing <= 0 or spec["kind"] in ("hatch", "point"):
        return ""
    page = _colour(tokens, "page")
    pieces = []
    if spec["kind"] == "screen":
        dot = spec.get("screen_dot_pt", 1.2) + 2 * casing
        for geometry in spec["geometries"]:
            if geometry is None or geometry.is_empty:
                continue
            polygons = [geometry] if isinstance(geometry, Polygon) else [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon)]
            for polygon in polygons:
                for d, offset in _screen_rows(polygon, projection):
                    pieces.append(
                        f'<path d="{d}" fill="none" stroke="{page}" stroke-width="{_fmt(dot)}" stroke-linecap="round" '
                        f'stroke-dasharray="0 {_fmt(SCREEN_SPACING_PT)}" stroke-dashoffset="{_fmt(offset)}"/>'
                    )
        return "".join(pieces)
    if spec.get("casing_rim"):
        return _casing_rim(spec, projection, page)
    width = spec["stroke_width"] + 2 * casing
    faint = spec.get("casing_opacity", 1.0)
    faint = f' stroke-opacity="{_fmt(faint)}"' if faint < 1.0 else ""
    # A DASHED LINE'S CASING IS DASHED ON THE LINE'S OWN ARRAY (branch 16),
    # as the interactive map's LineLayer draws it: a solid casing under a
    # dashed line reads as a page-coloured line with beads of ink on it.
    if spec.get("dash"):
        faint += f' stroke-dasharray="{spec["dash"]}"'

    for geometry in spec["geometries"]:
        if geometry is None or geometry.is_empty:
            continue
        d = _geometry_path(geometry, projection)
        if d:
            pieces.append(f'<path d="{d}" fill="none" stroke="{page}" stroke-width="{_fmt(width)}" '
                          f'stroke-linejoin="round" stroke-linecap="round"{faint}/>')
    return "".join(pieces)


def _casing_rim(spec: dict, projection, page: str) -> str:
    """A line layer's casing as a rim (see layer()): each line buffered to
    the casing's outer edge, minus its buffer to the line's own, in the
    ground's metres at the map's scale, round-capped and round-joined as
    the line is drawn."""
    half = spec["stroke_width"] / 2 * projection.meters_per_unit
    outer = (spec["stroke_width"] / 2 + spec["casing_pt"]) * projection.meters_per_unit
    lines = [g for g in spec["geometries"] if g is not None and not g.is_empty]
    if not lines:
        return ""
    whole = unary_union(lines)
    rim = whole.buffer(outer, join_style="round", cap_style="round").difference(
        whole.buffer(half, join_style="round", cap_style="round"))
    if rim.is_empty:
        return ""
    faint = spec.get("casing_opacity", 1.0)
    faint = f' fill-opacity="{_fmt(faint)}"' if faint < 1.0 else ""
    return f'<path d="{_geometry_path(rim, projection)}" fill="{page}"{faint} fill-rule="evenodd" stroke="none"/>'


def _layer_svg(spec: dict, projection, tokens: dict, sink: Optional[list] = None) -> str:
    """One layer's <g>. With `sink` a list, the layer's polygon, tint and
    point labels are appended to it instead of drawn (render_map's
    labels_on_top), so every label can be set last and clear of the rest."""
    stroke = _colour(tokens, spec["stroke"])
    fill = _colour(tokens, spec["fill"])
    if spec["kind"] == "hatch":
        return f'<g id="layer-{escape(spec["id"])}">' + "".join(_hatch_svg(spec, projection, tokens, sink)) + "</g>"
    if spec["kind"] == "pattern":
        return f'<g id="layer-{escape(spec["id"])}">' + "".join(_pattern_svg(spec, projection, tokens, sink)) + "</g>"
    if spec["kind"] == "screen":
        return (f'<g id="layer-{escape(spec["id"])}">' + _casing_svg(spec, projection, tokens)
                + "".join(_screen_svg(spec, projection, fill)) + "".join(_tint_labels(spec, projection, tokens, sink)) + "</g>")
    dash = f' stroke-dasharray="{spec["dash"]}"' if spec.get("dash") else ""
    opacity = spec.get("stroke_opacity", 1.0)
    if opacity < 1.0:
        dash += f' stroke-opacity="{_fmt(opacity)}"'
    pieces = [f'<g id="layer-{escape(spec["id"])}">']
    if spec.get("casing_pt"):
        pieces.append(_casing_svg(spec, projection, tokens))
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
                        font=FONT_PROSE if spec.get("label_face") == "prose" else FONT_DATA, size=LINE_LABEL_SIZE_PT,
                        fill=stroke, anchor="middle",
                        transform=f"rotate({_fmt(angle)} {_fmt(x)} {_fmt(y)})",
                    )
                )
            continue
        if spec["kind"] == "point":
            points = list(geometry.geoms) if isinstance(geometry, MultiPoint) else [geometry]
            colour = stroke
            for point in points:
                x, y = projection.xy(point.x, point.y)
                pieces.append(_marker(spec, x, y, colour, tokens))
                if label and sink is not None:
                    ax, ay, ar = _marker_anchor(spec, x, y)
                    sink.append({"kind": "point", "x": ax, "y": ay, "r": ar, "text": label, "fill": colour,
                                 "layer": spec["id"]})
                elif label:
                    # Beside and a little above the marker, so the text clears a line running through it.
                    pieces.append(_text(
                        x + _marker_radius(spec) + POINT_LABEL_GAP_PT, y - _marker_radius(spec) * 0.6, label,
                        font=FONT_DATA, size=LINE_LABEL_SIZE_PT, fill=colour, anchor="start",
                        halo=_colour(tokens, "page") if spec.get("label_halo") else None,
                    ))
            continue
        d = _geometry_path(geometry, projection)
        if not d:
            continue
        if spec["kind"] == "polygon":
            pieces.append(
                f'<path d="{d}" fill="{fill}" fill-opacity="{_fmt(spec["fill_opacity"])}" fill-rule="evenodd" '
                f'stroke="{stroke}" stroke-width="{_fmt(spec["stroke_width"])}" stroke-linejoin="round"{dash}/>'
            )
            if label:
                placement = _labelled_polygon(geometry, label, projection, LINE_LABEL_SIZE_PT)
                if sink is not None:
                    _defer_area_label(sink, geometry, placement, label, stroke, spec["id"], projection)
                elif placement:
                    lx, ly = placement
                    pieces.append(_text(
                        lx, ly + LINE_LABEL_SIZE_PT * 0.35, label,
                        font=FONT_DATA, size=LINE_LABEL_SIZE_PT, fill=stroke, anchor="middle",
                        halo=_colour(tokens, "page"),
                    ))
        else:
            pieces.append(
                f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{_fmt(spec["stroke_width"])}" '
                f'stroke-linejoin="round" stroke-linecap="round"{dash}/>'
            )
    pieces.append("</g>")
    return "".join(pieces)


def _label_box(x: float, y: float, text: str, anchor: str) -> tuple:
    """The box a label at (x, y) -- its baseline-centre reference, as
    _place_labels() sets it -- covers, in SVG units."""
    width = _label_width(text, LINE_LABEL_SIZE_PT)
    left = x - width / 2 if anchor == "middle" else (x - width if anchor == "end" else x)
    return (left - 1.0, y - LINE_LABEL_SIZE_PT * 0.5 - 1.0, left + width + 1.0, y + LINE_LABEL_SIZE_PT * 0.5 + 1.0)


def _overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _place_labels(pending: list, halo: str) -> tuple:
    """Every deferred label, set last: area labels at their poles, as
    placed; then each point label at the first of four positions -- right,
    left, below, above its marker -- whose box clears every label already
    set and every other marker. A point label with no clear position is
    set to the right regardless: a name beside its mark, crowded, is
    better than a mark with no name. Returns (svg, [(text, box), ...])."""
    markers = [(p["x"] - p["r"], p["y"] - p["r"], p["x"] + p["r"], p["y"] + p["r"]) for p in pending if p["kind"] == "point"]
    placed = []
    pieces = []
    for item in [p for p in pending if p["kind"] == "area"]:
        box_ = _label_box(item["x"], item["y"], item["text"], "middle")
        placed.append((item["text"], box_))
        pieces.append(_text(item["x"], item["y"] + LINE_LABEL_SIZE_PT * 0.35, item["text"], font=FONT_DATA,
                            size=LINE_LABEL_SIZE_PT, fill=item["fill"], anchor="middle", halo=halo))
    for item in [p for p in pending if p["kind"] == "point"]:
        gap = item["r"] + POINT_LABEL_GAP_PT
        x, y = item["x"], item["y"]
        own = (x - item["r"], y - item["r"], x + item["r"], y + item["r"])
        candidates = [
            (x + gap, y, "start"), (x - gap, y, "end"),
            (x, y + gap + LINE_LABEL_SIZE_PT * 0.5, "middle"), (x, y - gap - LINE_LABEL_SIZE_PT * 0.5, "middle"),
        ]
        chosen = candidates[0]
        for cx, cy, anchor in candidates:
            box_ = _label_box(cx, cy, item["text"], anchor)
            if not any(_overlaps(box_, other) for _, other in placed) and \
                    not any(_overlaps(box_, m) for m in markers if m != own):
                chosen = (cx, cy, anchor)
                break
        cx, cy, anchor = chosen
        placed.append((item["text"], _label_box(cx, cy, item["text"], anchor)))
        pieces.append(_text(cx, cy + LINE_LABEL_SIZE_PT * 0.35, item["text"], font=FONT_DATA, size=LINE_LABEL_SIZE_PT,
                            fill=item["fill"], anchor=anchor, halo=halo))
    return '<g id="labels">' + "".join(pieces) + "</g>", placed


def _text(x, y, content, *, font, size, fill, anchor="start", weight=None, transform=None, halo=None) -> str:
    """One <text>, or TWO when `halo` is a colour: a stroked copy in that
    colour first and the filled glyphs over it, which knocks the label out
    of whatever it sits on. Two elements rather than SVG's `paint-order`,
    which WeasyPrint does not implement -- a single stroked-and-filled
    text there draws the stroke OVER the glyphs and thickens them.

    A line label does not need this and does not get it: the line is
    broken behind it instead, which leaves no ink to knock out. A polygon
    label has no such option -- it sits over a tint, over contours, and
    over whatever those contours' own labels put there."""
    weight_attr = f' font-weight="{weight}"' if weight else ""
    transform_attr = f' transform="{transform}"' if transform else ""
    glyphs = escape(str(content))

    def element(paint):
        # THE ATTRIBUTE ORDER IS PART OF THE OUTPUT. report_chart.py draws
        # through this function too, and its test reads the compass
        # letters back with a regex over the whole tag, so a text with no
        # halo is byte for byte what it was before haloes existed.
        return (f'<text x="{_fmt(x)}" y="{_fmt(y)}" font-family="{escape(font)}" font-size="{_fmt(size)}" '
                f'{paint} text-anchor="{anchor}"{weight_attr}{transform_attr}>{glyphs}</text>')

    parts = []
    if halo:
        parts.append(element(f'fill="none" stroke="{halo}" stroke-width="{_fmt(size * HALO_WIDTH_EM)}" '
                             f'stroke-linejoin="round"'))
    parts.append(element(f'fill="{fill}"'))
    return "".join(parts)


def _north_arrow(frame, tokens, halo: bool = False) -> str:
    width, _ = frame
    ink = tokens["ink"]
    cx = width - MARGIN_PT - 8.0
    top = MARGIN_PT + 4.0
    length = 20.0
    casing = ""
    if halo:
        page = tokens["page"]
        casing = (
            f'<line x1="{_fmt(cx)}" y1="{_fmt(top + length)}" x2="{_fmt(cx)}" y2="{_fmt(top + 6)}" '
            f'stroke="{page}" stroke-width="{_fmt(0.8 + 2 * FURNITURE_CASING_PT)}" stroke-linecap="round"/>'
            f'<path d="M{_fmt(cx)} {_fmt(top)} L{_fmt(cx - 3.2)} {_fmt(top + 7.5)} L{_fmt(cx + 3.2)} {_fmt(top + 7.5)} Z" '
            f'fill="{page}" stroke="{page}" stroke-width="{_fmt(2 * FURNITURE_CASING_PT)}" stroke-linejoin="round"/>'
        )
    return (
        f'<g id="north-arrow">' + casing +
        f'<line x1="{_fmt(cx)}" y1="{_fmt(top + length)}" x2="{_fmt(cx)}" y2="{_fmt(top + 6)}" '
        f'stroke="{ink}" stroke-width="0.8"/>'
        f'<path d="M{_fmt(cx)} {_fmt(top)} L{_fmt(cx - 3.2)} {_fmt(top + 7.5)} L{_fmt(cx + 3.2)} {_fmt(top + 7.5)} Z" fill="{ink}"/>'
        + _text(cx, top + length + NORTH_SIZE_PT + 1.5, "N", font=FONT_PROSE, size=NORTH_SIZE_PT, fill=ink, anchor="middle",
                halo=tokens["page"] if halo else None)
        + "</g>"
    )


def _scale_bar(frame, projection, tokens, halo: bool = False) -> tuple:
    """The scale bar SVG and its measurements {feet, units}."""
    width, height = frame
    ink = tokens["ink"]
    feet = scale_bar_feet(projection.meters_per_unit, width)
    units = feet * METERS_PER_FOOT / projection.meters_per_unit
    right = width - MARGIN_PT
    left = right - units
    y = height - MARGIN_PT - 9.0
    casing = ""
    if halo:
        page, wide = tokens["page"], _fmt(0.9 + 2 * FURNITURE_CASING_PT)
        casing = (
            f'<line x1="{_fmt(left)}" y1="{_fmt(y)}" x2="{_fmt(right)}" y2="{_fmt(y)}" stroke="{page}" stroke-width="{wide}" stroke-linecap="square"/>'
            f'<line x1="{_fmt(left)}" y1="{_fmt(y - 3.5)}" x2="{_fmt(left)}" y2="{_fmt(y + 3.5)}" stroke="{page}" stroke-width="{wide}" stroke-linecap="square"/>'
            f'<line x1="{_fmt(right)}" y1="{_fmt(y - 3.5)}" x2="{_fmt(right)}" y2="{_fmt(y + 3.5)}" stroke="{page}" stroke-width="{wide}" stroke-linecap="square"/>'
        )
    svg = (
        f'<g id="scale-bar">' + casing +
        f'<line x1="{_fmt(left)}" y1="{_fmt(y)}" x2="{_fmt(right)}" y2="{_fmt(y)}" stroke="{ink}" stroke-width="0.9"/>'
        f'<line x1="{_fmt(left)}" y1="{_fmt(y - 3.5)}" x2="{_fmt(left)}" y2="{_fmt(y + 3.5)}" stroke="{ink}" stroke-width="0.9"/>'
        f'<line x1="{_fmt(right)}" y1="{_fmt(y - 3.5)}" x2="{_fmt(right)}" y2="{_fmt(y + 3.5)}" stroke="{ink}" stroke-width="0.9"/>'
        + _text((left + right) / 2, y - 5.5, f"{feet:,} ft", font=FONT_DATA, size=LABEL_SIZE_PT, fill=ink, anchor="middle",
                halo=tokens["page"] if halo else None)
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
    elif spec["kind"] == "screen":
        # The same dots, three staggered rows across the swatch, inside a hairline of the token.
        dot = spec.get("screen_dot_pt", 1.2)
        rows = []
        for index, y in enumerate((1.5, 4.0, 6.5)):
            offset = _fmt((SCREEN_SPACING_PT / 2) if index % 2 else 0.0)
            rows.append(
                f'<path d="M 1.5 {_fmt(y)} L {_fmt(w - 1.5)} {_fmt(y)}" fill="none" stroke="{fill}" stroke-width="{_fmt(dot)}" '
                f'stroke-linecap="round" stroke-dasharray="0 {_fmt(SCREEN_SPACING_PT)}" stroke-dashoffset="{offset}"/>'
            )
        body = (
            f'<rect x="0.25" y="0.25" width="{_fmt(w - 0.5)}" height="{_fmt(h - 0.5)}" fill="none" stroke="{fill}" '
            f'stroke-width="0.4"/>' + "".join(rows)
        )
    elif spec["kind"] == "hatch":
        # The same lines at the same spacing and weight, across a swatch with no outline.
        spacing = spec["hatch_spacing_pt"]
        lines = []
        k = -h
        while k < w + h:
            lines.append(f'<line x1="{_fmt(k)}" y1="{_fmt(h)}" x2="{_fmt(k + h)}" y2="0" stroke="{fill}" '
                         f'stroke-width="{_fmt(spec["stroke_width"])}"/>')
            k += spacing * math.sqrt(2)
        body = (f'<clipPath id="swatch-clip-{escape(spec["id"])}"><rect x="0" y="0" width="{_fmt(w)}" height="{_fmt(h)}"/></clipPath>'
                f'<g clip-path="url(#swatch-clip-{escape(spec["id"])})">' + "".join(lines) + "</g>")
    elif spec["kind"] == "pattern":
        # The same tile, the same opacity, the same edge, across the swatch.
        tile = _pattern_tile(spec, tokens).replace('id="pattern-', 'id="swatch-pattern-', 1)
        edge = ""
        if spec["stroke"]:
            faint = spec.get("stroke_opacity", 1.0)
            faint = f' stroke-opacity="{_fmt(faint)}"' if faint < 1.0 else ""
            edge = (f' stroke="{stroke}" stroke-width="{_fmt(min(spec["stroke_width"], 1.0))}"{faint}')
        body = (f"<defs>{tile}</defs>"
                f'<rect x="0.5" y="0.5" width="{_fmt(w - 1)}" height="{_fmt(h - 1)}" '
                f'fill="url(#swatch-pattern-{escape(spec["id"])})" fill-opacity="{_fmt(spec["pattern_opacity"])}"'
                + (edge or ' stroke="none"') + "/>")
    elif spec["kind"] == "point" and spec.get("marker") in ("pin", "disc"):
        # The marker at the swatch's height, not its map size.
        scaled = dict(spec, marker_size_pt=h, marker_halo_pt=(spec["marker_halo_pt"] or 0.0) * h / spec["marker_size_pt"])
        tip = h / 2 + (PIN_TIP[1] - PIN_VIEWBOX / 2) * h / PIN_VIEWBOX if spec["marker"] == "pin" else h / 2
        body = _marker(scaled, w / 2, tip, stroke, tokens)
    elif spec["kind"] == "line":
        opacity = spec.get("stroke_opacity", 1.0)
        faint = f' stroke-opacity="{_fmt(opacity)}"' if opacity < 1.0 else ""
        body = (
            f'<line x1="0" y1="{_fmt(h / 2)}" x2="{_fmt(w)}" y2="{_fmt(h / 2)}" '
            f'stroke="{stroke}" stroke-width="{_fmt(spec["stroke_width"])}"{dash}{faint}/>'
        )
    else:
        body = _marker(spec, w / 2, h / 2, stroke, tokens)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(w)}pt" height="{_fmt(h)}pt" '
        f'viewBox="0 0 {_fmt(w)} {_fmt(h)}" class="report-map__swatch">{body}</svg>'
    )


def fitted_frame(boundary_polygon_utm, frame: tuple = FRAME) -> tuple:
    """The frame a parcel is drawn in: `frame`, its width cut so that at
    most MAX_CONTEXT_MARGIN_M of ground shows beyond the parcel's bbox on
    either side. The height is the page's and is never changed, so the
    scale is the full frame's; the cut only removes empty width."""
    projection = _Projection(boundary_polygon_utm.bounds, frame, MARGIN_PT, FURNITURE_BAND_PT)
    width, height = frame
    x0, _, x1, _ = projection.drawn_bbox
    allowed = MAX_CONTEXT_MARGIN_M * projection.scale
    if x0 - MARGIN_PT > allowed + 1e-6:
        width = (x1 - x0) + 2 * (MARGIN_PT + allowed)
    return (width, height)


def visible_extent_utm(boundary_polygon_utm, frame: tuple = FRAME, fit: bool = True) -> tuple:
    """The ground rectangle the (fitted) frame shows, in the DEM's CRS,
    above the furniture band: (minx, miny, maxx, maxy). A section drawing
    context beyond the parcel -- a stream next door, a flood zone along
    it -- clips to this so nothing is drawn under the scale bar or outside
    the frame, and the extent and scale stay the parcel's."""
    if fit:
        frame = fitted_frame(boundary_polygon_utm, frame)
    projection = _Projection(boundary_polygon_utm.bounds, frame, MARGIN_PT, FURNITURE_BAND_PT)
    width, height = frame
    minx = (0 - projection.offset_x) / projection.scale
    maxx = (width - projection.offset_x) / projection.scale
    maxy = projection.offset_y / projection.scale
    miny = (projection.offset_y - (height - FURNITURE_BAND_PT)) / projection.scale
    return (minx, miny, maxx, maxy)


def frame_extent_utm(boundary_polygon_utm, frame: tuple = FRAME, fit: bool = True) -> tuple:
    """The ground the WHOLE frame covers, furniture band included, in the
    DEM's CRS: what an underlay must cover to fill the frame."""
    if fit:
        frame = fitted_frame(boundary_polygon_utm, frame)
    projection = _Projection(boundary_polygon_utm.bounds, frame, MARGIN_PT, FURNITURE_BAND_PT)
    width, height = frame
    return ((0 - projection.offset_x) / projection.scale, (projection.offset_y - height) / projection.scale,
            (width - projection.offset_x) / projection.scale, projection.offset_y / projection.scale)


def label_placements(boundary_polygon_utm, spec: dict, frame: tuple = FRAME, fit: bool = True) -> list:
    """For a labelled line or polygon layer, whether each geometry's label
    will be SET (True) or DROPPED (False) -- for want of a part long
    enough to carry it clear of the line either side, or of room inside
    the polygon for its box. The same rules _labelled_line and
    _labelled_polygon apply at render time, so a caller can state the
    count or put the label elsewhere."""
    projection = _Projection(boundary_polygon_utm.bounds, fitted_frame(boundary_polygon_utm, frame) if fit else frame,
                             MARGIN_PT, FURNITURE_BAND_PT)
    placed = []
    for geometry, label in zip(spec["geometries"], spec.get("labels") or []):
        if not label or geometry is None or geometry.is_empty:
            placed.append(False)
        elif spec["kind"] == "line":
            _, placement = _labelled_line(geometry, label, projection, LINE_LABEL_SIZE_PT)
            placed.append(placement is not None)
        elif spec["kind"] in ("polygon", "hatch", "screen", "pattern"):
            placed.append(_labelled_polygon(geometry, label, projection, LINE_LABEL_SIZE_PT) is not None)
        else:
            placed.append(False)
    return placed


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


def _graded_edge(boundary_polygon_utm, edge: dict, projection, tokens: dict) -> str:
    """render_map's `edge`: disjoint rings outside the parcel, each band's
    width in points converted to the ground's metres at the map's scale."""
    colour = _colour(tokens, edge["token"])
    pieces = ['<g id="parcel-edge">']
    inner, reach = boundary_polygon_utm, 0.0
    for width_pt, opacity in edge["steps"]:
        reach += width_pt
        outer = boundary_polygon_utm.buffer(reach * projection.meters_per_unit, join_style="round")
        ring = outer.difference(inner)
        if not ring.is_empty:
            pieces.append(f'<path d="{_geometry_path(ring, projection)}" fill="{colour}" fill-opacity="{_fmt(opacity)}" '
                          f'fill-rule="evenodd" stroke="none"/>')
        inner = outer
    return "".join(pieces) + "</g>"


def render_map(boundary_polygon_utm, layers: list, tokens: dict, frame: tuple = FRAME, note: Optional[dict] = None,
               *, fit: bool = True, underlay: Optional[dict] = None, wash: Optional[dict] = None,
               halo: bool = False, labels_on_top: bool = False, boundary_style: Optional[dict] = None,
               edge: Optional[dict] = None) -> dict:
    """
    The map, and its measurements:

        {'svg': str,
         'frame': (w, h),                 # pt, the FITTED frame (fitted_frame)
         'meters_per_unit': float,        # ground metres per SVG user unit
         'extent_utm': (minx, miny, maxx, maxy),
         'drawn_bbox': (x0, y0, x1, y1),  # where the boundary's bbox landed
         'scale_bar': {'feet': int, 'units': float},
         'legend': legend_entries(layers, tokens),
         'labels_placed': {layer id: [bool per geometry]}}   # labelled line and polygon layers

    Layers draw in the order given, under the boundary; the boundary, the
    north arrow and the scale bar draw last. The frame's outline is a
    hairline in the rule token. The legend is returned, not drawn: the
    map.html macro sets it below the frame.

    `note` is a quiet statement set ON the map -- {'lines': [str, ...],
    'point': a shapely Point in the DEM's CRS} -- in the prose face and
    the muted ink, centred on the point, between the layers and the
    boundary: what a section says where a parcel has nothing to draw, so
    an empty shape reads as a finding rather than a failure.

    THE LAYOUT MAP'S ARGUMENTS (branch 13), every one off by default:

      fit       False draws in `frame` exactly as given, instead of the
                width cut fitted_frame() makes -- the layout map takes
                the full page and is NOT on the sections' shared scale.
      underlay  {'href': a data: URI, 'extent_utm': (minx, miny, maxx,
                maxy)} -- a raster set as an SVG <image> over the page
                fill and under every layer, stretched to the extent it
                was warped to. WeasyPrint embeds it as one image object
                and keeps every path and glyph above it vector.
      wash      {'token': name, 'opacity': float} -- the ground OUTSIDE
                the parcel, over the underlay, so the parcel reads
                distinctly against the neighbours' land.
      halo      True cases the boundary, the north arrow and the scale
                bar in the page colour, for a map drawn over photography.
      labels_on_top
                True sets every polygon, tint and point label after all
                the layers and the boundary, point labels moved clear of
                the others (_place_labels) -- so no label is drawn under a
                later layer. The placed boxes come back as 'label_boxes'.

    AND BRANCH 16'S, off by default too:

      boundary_style
                {'stroke': token or None, 'width': pt, 'casing': bool} --
                the boundary drawn in that token and weight, cased in the
                page colour only if asked; a None stroke draws no boundary
                line at all. `halo` then cases the furniture alone.
      edge      {'token': name, 'steps': [(width_pt, opacity), ...]} -- a
                GRADED EDGE outside the parcel, over the wash: nested bands,
                the first `width_pt` wide against the boundary, each next
                one reaching `width_pt` further out, each at its own
                opacity. Disjoint rings (buffer minus the buffer inside
                it), so each band prints at exactly its stated opacity and
                nothing compounds. Built with shapely, not an SVG filter:
                WeasyPrint ignores feGaussianBlur and feDropShadow.
    """
    if fit:
        frame = fitted_frame(boundary_polygon_utm, frame)
    width, height = frame
    projection = _Projection(boundary_polygon_utm.bounds, frame, MARGIN_PT, FURNITURE_BAND_PT)
    rule = tokens["rule"]
    page = tokens["page"]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}pt" height="{_fmt(height)}pt" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" role="img" aria-label="Site map">',
        f'<rect x="0" y="0" width="{_fmt(width)}" height="{_fmt(height)}" fill="{page}" stroke="none"/>',
    ]
    if underlay:
        ux0, uy0, ux1, uy1 = underlay["extent_utm"]
        left, top = projection.xy(ux0, uy1)
        right, bottom = projection.xy(ux1, uy0)
        parts.append(
            f'<image id="underlay" x="{_fmt(left)}" y="{_fmt(top)}" width="{_fmt(right - left)}" '
            f'height="{_fmt(bottom - top)}" preserveAspectRatio="none" href="{underlay["href"]}"/>'
        )
    if wash:
        ux0, uy0, ux1, uy1 = underlay["extent_utm"] if underlay else visible_extent_utm(boundary_polygon_utm, frame, fit=False)
        outside = Polygon([(ux0, uy0), (ux1, uy0), (ux1, uy1), (ux0, uy1)]).difference(boundary_polygon_utm)
        if not outside.is_empty:
            parts.append(
                f'<path id="off-parcel-wash" d="{_geometry_path(outside, projection)}" fill="{_colour(tokens, wash["token"])}" '
                f'fill-opacity="{_fmt(wash["opacity"])}" fill-rule="evenodd" stroke="none"/>'
            )
    if edge:
        parts.append(_graded_edge(boundary_polygon_utm, edge, projection, tokens))
    pending = [] if labels_on_top else None
    for spec in layers:
        parts.append(_layer_svg(spec, projection, tokens, pending))
    if note and note.get("lines"):
        x, y = projection.xy(note["point"].x, note["point"].y)
        lines = list(note["lines"])
        leading = LABEL_SIZE_PT * 1.35
        top = y - leading * (len(lines) - 1) / 2 + LABEL_SIZE_PT * 0.35
        parts.append('<g id="map-note">' + "".join(
            _text(x, top + i * leading, line, font=FONT_PROSE, size=LABEL_SIZE_PT, fill=tokens["ink-muted"], anchor="middle")
            for i, line in enumerate(lines)
        ) + "</g>")
    boundary_d = _geometry_path(boundary_polygon_utm, projection)
    if boundary_style is None:
        boundary_style = {"stroke": "ink", "width": BOUNDARY_STROKE_PT, "casing": halo}
    boundary_width = boundary_style.get("width", BOUNDARY_STROKE_PT)
    if boundary_style.get("stroke") and boundary_style.get("casing"):
        parts.append(
            f'<path id="parcel-boundary-casing" d="{boundary_d}" fill="none" stroke="{page}" '
            f'stroke-width="{_fmt(boundary_width + 2 * FURNITURE_CASING_PT)}" stroke-linejoin="round"/>'
        )
    if boundary_style.get("stroke"):
        parts.append(
            f'<path id="parcel-boundary" d="{boundary_d}" fill="none" stroke="{_colour(tokens, boundary_style["stroke"])}" '
            f'stroke-width="{_fmt(boundary_width)}" stroke-linejoin="round"/>'
        )
    label_boxes = []
    if pending is not None:
        labels_svg, label_boxes = _place_labels(pending, page)
        parts.append(labels_svg)
    parts.append(_north_arrow(frame, tokens, halo=halo))
    scale_svg, scale_bar = _scale_bar(frame, projection, tokens, halo=halo)
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
        "label_boxes": label_boxes,
        "labels_placed": {
            spec["id"]: label_placements(boundary_polygon_utm, spec, frame, fit=False)
            for spec in layers if spec["kind"] in ("line", "polygon", "hatch", "screen", "pattern") and spec.get("labels")
        },
    }
