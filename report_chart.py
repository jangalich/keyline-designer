"""
report_chart.py

THE REPORT CHART COMPONENT: SVG charts for the report page, on the same
principles as report_map.py -- vector output at a fixed frame width,
every colour a TOKEN NAME resolved against the tokens dict the caller
passes, fonts as presentation attributes, the legend returned rather
than drawn so the chart.html macro sets it under the frame in the
report's type. Climate is the first section to draw one; later sections
draw theirs with the same helpers.

    render_water_balance(months, tokens)  -> {svg, legend, ...measurements}
    render_wind_roses(seasons, tokens)    -> {svg, ...measurements}
    render_valley_profile(profile, tokens) -> {svg, legend, ...measurements}

THE WATER BALANCE DIAGRAM is the classic hydrology-bulletin form: monthly
precipitation and monthly potential evapotranspiration as two lines
across the year, the area between them shaded WATER where precipitation
is the higher (surplus) and OCHRE where evaporation is (deficit). The
two lines cross between months; the crossing is found by linear
interpolation so a shaded band ends exactly where the lines meet, and
consecutive months of one sign are merged into one polygon so no seam
runs through a band. Values arrive ALREADY IN THE DISPLAY UNIT (inches):
the chart scales and draws, it converts nothing -- climate_section's
rule. The y axis runs from zero to the next whole tick above the
larger series; month initials sit under their points in the prose
face; tick labels are numbers and take the data face, and the unit
rides beside the top value ("6 in") rather than above it.

THE WIND ROSES are two small roses side by side, winter and summer,
eight sectors, in ink: each sector's wedge reaches a radius in
proportion to the share of days the wind came FROM that sector, against
rings at whole tens of percent; the PREVAILING sector -- the modal one,
the longest wedge, by construction: the renderer picks the largest
share itself and refuses a caller whose `prevailing` names another --
is filled solid, the rest tinted. The compass letters sit outside the rings; each rose is
titled with its season and months and carries the words "wind from",
so a reader cannot take a wedge for a heading. The mean speed is set
under the rose in the data face, already formatted by the section.

THE VALLEY PROFILE is a long section down one valley's main stem: ground
distance along the stem across, elevation up, both in feet, the line in
the water token (the valley's colour on the map). The vertical scale is
exaggerated, as every long profile is, and the exaggeration is a WHOLE
NUMBER chosen by the renderer -- the largest that keeps the relief inside
the plot -- and returned so the caption can state it. The keypoint is a
filled dot on the line with its elevation set beside it and the grades
above and below it set on their reaches; a tick on the distance axis
marks each place the stem crosses the parcel boundary, so the reader can
see how much of the profile is this property. A profile without a
keypoint draws the line alone, and the section says why in words.

NO COLOUR LITERAL LIVES HERE (test_site_report.py greps this module
too). The legend is a list of report_map.layer() specs run through
report_map.legend_entries(), so a chart's swatches are drawn by the
same code as a map's and a chart legend label obeys the page's data
rule the way a map legend does.

MEASUREMENTS COME BACK WITH THE SVG -- the plot rectangle, the y scale,
the crossings, each rose's centre, radius and wedge lengths -- so a test
can hold the geometry to account without parsing SVG.
"""

import math
from typing import Optional

from report_map import FONT_DATA, FONT_PROSE, _colour, _fmt, _text, layer, legend_entries

# The frame width is the page's content width, as the map's is.
FRAME_WIDTH_PT = 489.6
BALANCE_FRAME = (FRAME_WIDTH_PT, 212.0)
ROSES_FRAME = (FRAME_WIDTH_PT, 188.0)

# Water balance geometry, in points.
BALANCE_MARGIN_LEFT_PT = 34.0
BALANCE_MARGIN_RIGHT_PT = 10.0
BALANCE_MARGIN_TOP_PT = 12.0
BALANCE_MARGIN_BOTTOM_PT = 18.0
BALANCE_LINE_PT = 1.1
BALANCE_FILL_OPACITY = 0.38
GRID_STROKE_PT = 0.4
AXIS_STROKE_PT = 0.6
TICK_LABEL_SIZE_PT = 6.5
MONTH_LABEL_SIZE_PT = 7.5

# Wind rose geometry.
ROSE_RADIUS_PT = 57.0
ROSE_RING_STEP = 0.10          # rings every 10 % of days
ROSE_WEDGE_OPACITY = 0.28
ROSE_STROKE_PT = 0.55
ROSE_TITLE_SIZE_PT = 8.5
ROSE_SUBTITLE_SIZE_PT = 7.0
ROSE_COMPASS_SIZE_PT = 7.0
ROSE_RING_LABEL_SIZE_PT = 5.5
ROSE_SPEED_SIZE_PT = 7.0
SECTOR_COUNT = 8

# Valley profile geometry.
PROFILE_FRAME = (FRAME_WIDTH_PT, 170.0)
PROFILE_MARGIN_LEFT_PT = 40.0
PROFILE_MARGIN_RIGHT_PT = 12.0
PROFILE_MARGIN_TOP_PT = 16.0
PROFILE_MARGIN_BOTTOM_PT = 26.0
PROFILE_LINE_PT = 1.1
PROFILE_KEYPOINT_RADIUS_PT = 2.2
PROFILE_TICK_PT = 5.0
PROFILE_ANNOTATION_SIZE_PT = 6.5
PROFILE_MAX_EXAGGERATION = 10


# ======================================================================
# Shared
# ======================================================================


def _svg_open(width: float, height: float, label: str, tokens: dict) -> str:
    page = _colour(tokens, "page")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}pt" height="{_fmt(height)}pt" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" role="img" aria-label="{label}">'
        f'<rect x="0" y="0" width="{_fmt(width)}" height="{_fmt(height)}" fill="{page}" stroke="none"/>'
    )


def _polygon(points, fill: str, opacity: float) -> str:
    d = " ".join(f"{'M' if i == 0 else 'L'}{_fmt(x)} {_fmt(y)}" for i, (x, y) in enumerate(points)) + " Z"
    return f'<path d="{d}" fill="{fill}" fill-opacity="{_fmt(opacity)}" stroke="none"/>'


def _polyline(points, stroke: str, width: float) -> str:
    d = " ".join(f"{'M' if i == 0 else 'L'}{_fmt(x)} {_fmt(y)}" for i, (x, y) in enumerate(points))
    return f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{_fmt(width)}" stroke-linejoin="round" stroke-linecap="round"/>'


def _tick_step(maximum: float) -> float:
    """1, 2 or 5 display units per tick, so the axis holds four to eight."""
    for step in (0.5, 1.0, 2.0, 5.0, 10.0, 20.0):
        if maximum / step <= 8:
            return step
    return 50.0


# ======================================================================
# The water balance
# ======================================================================


def balance_bands(precipitation, evaporation) -> list:
    """
    The shaded bands between the two series, in DATA coordinates:
    [{'sign': 'surplus'|'deficit', 'upper': [(x, y), ...], 'lower': [...]}]
    with x the month index 0..11 (a crossing at a fractional x), the
    upper edge the higher series and the lower the other. Consecutive
    months of one sign are one band; a crossing between months splits
    the segment at the interpolated point.
    """
    n = len(precipitation)
    bands = []
    current = None

    def _start(sign, x, p, e):
        return {"sign": sign, "upper": [(x, max(p, e))], "lower": [(x, min(p, e))]}

    def _sign(d):
        return "surplus" if d >= 0 else "deficit"

    p0, e0 = precipitation[0], evaporation[0]
    current = _start(_sign(p0 - e0), 0.0, p0, e0)
    for i in range(1, n):
        p1, e1 = precipitation[i], evaporation[i]
        d0, d1 = p0 - e0, p1 - e1
        if d0 * d1 < 0:
            t = d0 / (d0 - d1)
            xc = (i - 1) + t
            yc = p0 + t * (p1 - p0)
            current["upper"].append((xc, yc))
            current["lower"].append((xc, yc))
            bands.append(current)
            current = _start(_sign(d1), xc, yc, yc)
        current["upper"].append((float(i), max(p1, e1)))
        current["lower"].append((float(i), min(p1, e1)))
        p0, e0 = p1, e1
    bands.append(current)
    return bands


def render_water_balance(
    month_labels, precipitation, evaporation, unit: str, tokens: dict, frame: tuple = BALANCE_FRAME
) -> dict:
    """
    The diagram and its measurements:

        {'svg': str, 'frame': (w, h), 'plot': (x0, y0, x1, y1),
         'y_max': float, 'tick_step': float, 'ticks': [...],
         'points_per_month': float, 'crossings': [x, ...],   # fractional month index
         'bands': balance_bands()'s list,
         'legend': [...]}                                    # legend_entries() shape

    `precipitation` and `evaporation` are twelve values in the display
    unit; `unit` is its label ("in"). Colours: precipitation and surplus
    in the water token, evaporation and deficit in ochre, axes and
    labels in ink and rule.
    """
    if len(month_labels) != 12 or len(precipitation) != 12 or len(evaporation) != 12:
        raise ValueError("render_water_balance: twelve months, twelve values each")
    width, height = frame
    x0, y1 = BALANCE_MARGIN_LEFT_PT, height - BALANCE_MARGIN_BOTTOM_PT
    x1, y0 = width - BALANCE_MARGIN_RIGHT_PT, BALANCE_MARGIN_TOP_PT
    top = max(max(precipitation), max(evaporation), 0.0)
    step = _tick_step(top)
    y_max = math.ceil(top / step) * step if top > 0 else step
    ticks = [round(k * step, 6) for k in range(int(round(y_max / step)) + 1)]
    per_month = (x1 - x0) / 11.0
    scale = (y1 - y0) / y_max

    def xy(month_x, value):
        return (x0 + month_x * per_month, y1 - value * scale)

    water, ochre = _colour(tokens, "water"), _colour(tokens, "ochre")
    ink, muted, rule = _colour(tokens, "ink"), _colour(tokens, "ink-muted"), _colour(tokens, "rule")

    parts = [_svg_open(width, height, "Monthly water balance", tokens)]
    # Grid and ticks.
    for tick in ticks:
        gx0, gy = xy(0.0, tick)
        gx1, _ = xy(11.0, tick)
        stroke = ink if tick == 0 else rule
        stroke_w = AXIS_STROKE_PT if tick == 0 else GRID_STROKE_PT
        parts.append(f'<line x1="{_fmt(gx0)}" y1="{_fmt(gy)}" x2="{_fmt(gx1)}" y2="{_fmt(gy)}" stroke="{stroke}" stroke-width="{_fmt(stroke_w)}"/>')
        # The unit rides beside the top value ("6 in"), never stacked above it.
        label = f"{tick:g} {unit}" if tick == ticks[-1] else f"{tick:g}"
        parts.append(_text(x0 - 4.0, gy + TICK_LABEL_SIZE_PT * 0.35, label, font=FONT_DATA, size=TICK_LABEL_SIZE_PT, fill=muted, anchor="end"))
    # Bands.
    bands = balance_bands(precipitation, evaporation)
    for band in bands:
        points = [xy(x, y) for x, y in band["upper"]] + [xy(x, y) for x, y in reversed(band["lower"])]
        parts.append(_polygon(points, water if band["sign"] == "surplus" else ochre, BALANCE_FILL_OPACITY))
    # Lines.
    parts.append(_polyline([xy(float(i), v) for i, v in enumerate(evaporation)], ochre, BALANCE_LINE_PT))
    parts.append(_polyline([xy(float(i), v) for i, v in enumerate(precipitation)], water, BALANCE_LINE_PT))
    # Month labels.
    for i, label in enumerate(month_labels):
        mx, _ = xy(float(i), 0.0)
        parts.append(_text(mx, y1 + MONTH_LABEL_SIZE_PT + 3.5, label, font=FONT_PROSE, size=MONTH_LABEL_SIZE_PT, fill=ink, anchor="middle"))
    parts.append("</svg>")

    legend_layers = [
        layer("precipitation", [], kind="line", stroke="water", stroke_width=BALANCE_LINE_PT, legend="precipitation"),
        layer("evaporation", [], kind="line", stroke="ochre", stroke_width=BALANCE_LINE_PT, legend="potential evaporation"),
        layer("surplus", [], kind="polygon", stroke="water", stroke_width=0.5, fill="water", fill_opacity=BALANCE_FILL_OPACITY, legend="surplus"),
        layer("deficit", [], kind="polygon", stroke="ochre", stroke_width=0.5, fill="ochre", fill_opacity=BALANCE_FILL_OPACITY, legend="deficit"),
    ]
    crossings = [band["upper"][0][0] for band in bands[1:]]
    return {
        "svg": "".join(parts),
        "frame": frame,
        "plot": (x0, y0, x1, y1),
        "y_max": y_max,
        "tick_step": step,
        "ticks": ticks,
        "points_per_month": per_month,
        "crossings": crossings,
        "bands": bands,
        "legend": legend_entries(legend_layers, tokens),
    }


# ======================================================================
# The valley profile
# ======================================================================


def profile_exaggeration(plot_width: float, plot_height: float, run: float, relief: float) -> int:
    """The largest whole-number vertical exaggeration at which `relief`
    fits `plot_height` when `run` fills `plot_width`; at least 1, at most
    PROFILE_MAX_EXAGGERATION."""
    if run <= 0 or relief <= 0:
        return 1
    horizontal = plot_width / run
    fits = int(math.floor(plot_height / (relief * horizontal)))
    return max(1, min(PROFILE_MAX_EXAGGERATION, fits))


def render_valley_profile(profile: dict, tokens: dict, frame: tuple = PROFILE_FRAME) -> dict:
    """
    The profile and its measurements:

        {'svg': str, 'frame': (w, h), 'plot': (x0, y0, x1, y1),
         'exaggeration': int, 'x_per_ft': float, 'y_per_ft': float,
         'y_range': (low, high), 'x_ticks': [...], 'y_ticks': [...],
         'keypoint_xy': (x, y) | None, 'boundary_ticks_x': [...],
         'legend': [...]}

    `profile` carries, ALREADY IN FEET: 'distance' and 'elevation' (equal-
    length lists, upstream first), 'crossings' (distances along the stem
    where it crosses the parcel boundary), and 'keypoint' -- None, or
    {'distance', 'elevation', 'grade_above_pct', 'grade_below_pct',
    'label'} with the label already formatted by the section.
    """
    distance, elevation = list(profile["distance"]), list(profile["elevation"])
    if len(distance) != len(elevation) or len(distance) < 2:
        raise ValueError("render_valley_profile: distance and elevation are equal lists of at least two")
    width, height = frame
    x0, y1 = PROFILE_MARGIN_LEFT_PT, height - PROFILE_MARGIN_BOTTOM_PT
    x1, y0 = width - PROFILE_MARGIN_RIGHT_PT, PROFILE_MARGIN_TOP_PT
    run = distance[-1] - distance[0]
    low, high = min(elevation), max(elevation)
    y_step = _tick_step(high - low) if high > low else 1.0
    y_low = math.floor(low / y_step) * y_step
    y_high = math.ceil(high / y_step) * y_step
    if y_high == y_low:
        y_high = y_low + y_step
    exaggeration = profile_exaggeration(x1 - x0, y1 - y0, run, y_high - y_low)
    x_per_ft = (x1 - x0) / run if run > 0 else 1.0
    y_per_ft = x_per_ft * exaggeration
    # The plot's vertical extent at the chosen exaggeration; the axis sits at the bottom.
    y_base = y1

    def xy(d, z):
        return (x0 + (d - distance[0]) * x_per_ft, y_base - (z - y_low) * y_per_ft)

    ink, muted, rule = _colour(tokens, "ink"), _colour(tokens, "ink-muted"), _colour(tokens, "rule")
    water = _colour(tokens, "water")
    parts = [_svg_open(width, height, "Valley profile", tokens)]
    # Elevation grid and tick labels, the unit beside the top tick.
    y_ticks = []
    tick = y_low
    while tick <= y_high + 1e-9:
        y_ticks.append(round(tick, 6))
        tick += y_step
    for tick in y_ticks:
        gx0, gy = xy(distance[0], tick)
        gx1, _ = xy(distance[-1], tick)
        parts.append(f'<line x1="{_fmt(gx0)}" y1="{_fmt(gy)}" x2="{_fmt(gx1)}" y2="{_fmt(gy)}" stroke="{rule}" stroke-width="{_fmt(GRID_STROKE_PT)}"/>')
        label = f"{tick:,.0f} ft" if tick == y_ticks[-1] else f"{tick:,.0f}"
        parts.append(_text(x0 - 4.0, gy + TICK_LABEL_SIZE_PT * 0.35, label, font=FONT_DATA, size=TICK_LABEL_SIZE_PT, fill=muted, anchor="end"))
    # The distance axis with ticks at round distances.
    parts.append(f'<line x1="{_fmt(x0)}" y1="{_fmt(y_base)}" x2="{_fmt(x1)}" y2="{_fmt(y_base)}" stroke="{ink}" stroke-width="{_fmt(AXIS_STROKE_PT)}"/>')
    x_step = _tick_step(run / 100.0) * 100.0
    x_ticks = []
    tick = 0.0
    while tick <= run + 1e-9:
        x_ticks.append(round(tick, 6))
        tick += x_step
    for tick in x_ticks:
        tx, _ = xy(distance[0] + tick, y_low)
        parts.append(f'<line x1="{_fmt(tx)}" y1="{_fmt(y_base)}" x2="{_fmt(tx)}" y2="{_fmt(y_base + 3.0)}" stroke="{ink}" stroke-width="{_fmt(AXIS_STROKE_PT)}"/>')
        label = f"{tick:,.0f} ft" if tick == x_ticks[-1] else f"{tick:,.0f}"
        parts.append(_text(tx, y_base + 3.0 + TICK_LABEL_SIZE_PT + 1.5, label, font=FONT_DATA, size=TICK_LABEL_SIZE_PT, fill=muted, anchor="middle"))
    # Boundary ticks: a longer tick through the axis, labelled once.
    boundary_x = []
    for crossing in profile.get("crossings") or []:
        bx, _ = xy(crossing, y_low)
        boundary_x.append(bx)
        parts.append(f'<line x1="{_fmt(bx)}" y1="{_fmt(y_base - PROFILE_TICK_PT)}" x2="{_fmt(bx)}" y2="{_fmt(y_base + PROFILE_TICK_PT)}" stroke="{ink}" stroke-width="{_fmt(AXIS_STROKE_PT * 1.5)}"/>')
    if boundary_x:
        label_x = sum(boundary_x) / len(boundary_x)
        parts.append(_text(label_x, y_base - PROFILE_TICK_PT - 2.5, "parcel boundary", font=FONT_PROSE, size=PROFILE_ANNOTATION_SIZE_PT, fill=ink, anchor="middle"))
    # The profile line.
    parts.append(_polyline([xy(d, z) for d, z in zip(distance, elevation)], water, PROFILE_LINE_PT))
    # The keypoint and its grades.
    keypoint = profile.get("keypoint")
    keypoint_xy = None
    if keypoint:
        kx, ky = xy(keypoint["distance"], keypoint["elevation"])
        keypoint_xy = (kx, ky)
        parts.append(f'<circle cx="{_fmt(kx)}" cy="{_fmt(ky)}" r="{_fmt(PROFILE_KEYPOINT_RADIUS_PT)}" fill="{ink}" stroke="none"/>')
        parts.append(_text(kx + 5.0, ky - 4.0, keypoint["label"], font=FONT_DATA, size=PROFILE_ANNOTATION_SIZE_PT, fill=ink, anchor="start"))
        # Grades on their reaches: above sits up-profile of the keypoint, below down-profile.
        above_x = (x0 + kx) / 2
        below_x = (kx + x1) / 2
        above_z = max(z for d, z in zip(distance, elevation) if d <= keypoint["distance"])
        below_z = max(z for d, z in zip(distance, elevation) if d >= keypoint["distance"])
        _, above_y = xy(keypoint["distance"], above_z)
        _, below_y = xy(keypoint["distance"], below_z)
        parts.append(_text(above_x, above_y - 5.0, f"{keypoint['grade_above_pct']:.1f}% above", font=FONT_DATA, size=PROFILE_ANNOTATION_SIZE_PT, fill=ink, anchor="middle"))
        parts.append(_text(below_x, below_y - 5.0, f"{keypoint['grade_below_pct']:.1f}% below", font=FONT_DATA, size=PROFILE_ANNOTATION_SIZE_PT, fill=ink, anchor="middle"))
    parts.append("</svg>")
    legend_layers = [
        layer("profile", [], kind="line", stroke="water", stroke_width=PROFILE_LINE_PT, legend="valley floor"),
    ]
    if keypoint:
        legend_layers.append(layer("keypoint", [], kind="point", stroke="ink", marker="dot", legend="keypoint"))
    return {
        "svg": "".join(parts),
        "frame": frame,
        "plot": (x0, y0, x1, y1),
        "exaggeration": exaggeration,
        "x_per_ft": x_per_ft,
        "y_per_ft": y_per_ft,
        "y_range": (y_low, y_high),
        "x_ticks": x_ticks,
        "y_ticks": y_ticks,
        "keypoint_xy": keypoint_xy,
        "boundary_ticks_x": boundary_x,
        "legend": legend_entries(legend_layers, tokens),
    }


# ======================================================================
# The wind roses
# ======================================================================


def _wedge(cx, cy, radius, centre_deg, half_width_deg) -> str:
    a0 = math.radians(centre_deg - half_width_deg)
    a1 = math.radians(centre_deg + half_width_deg)
    x_a, y_a = cx + radius * math.sin(a0), cy - radius * math.cos(a0)
    x_b, y_b = cx + radius * math.sin(a1), cy - radius * math.cos(a1)
    return f"M{_fmt(cx)} {_fmt(cy)} L{_fmt(x_a)} {_fmt(y_a)} A{_fmt(radius)} {_fmt(radius)} 0 0 1 {_fmt(x_b)} {_fmt(y_b)} Z"


def render_wind_roses(seasons: list, tokens: dict, frame: tuple = ROSES_FRAME) -> dict:
    """
    Two (or more) roses side by side and their measurements:

        {'svg': str, 'frame': (w, h), 'ring_max': float, 'rings': [...],
         'roses': [{'title', 'centre': (cx, cy), 'radius': r,
                    'sectors': [...], 'wedge_radii': {sector: r_pt},
                    'prevailing': sector | None}, ...]}

    Each season is {'title': 'Winter', 'months': 'Dec–Feb',
    'sectors': [8 names clockwise from N], 'frequency': {name: share},
    'prevailing': name | None, 'speed_label': '6.1 mph' | None}. The
    solid wedge is the LONGEST one -- the sector with the largest share
    -- and a `prevailing` that names a different sector raises: the
    picture and the word must agree. All roses share one ring scale --
    the smallest multiple of ten percent above the largest share in any
    season -- so the two can be read against each other.
    """
    if not seasons:
        raise ValueError("render_wind_roses: at least one season")
    width, height = frame
    ink, muted, rule = _colour(tokens, "ink"), _colour(tokens, "ink-muted"), _colour(tokens, "rule")
    largest = max(max(s["frequency"].values()) for s in seasons)
    ring_max = round(max(ROSE_RING_STEP, math.ceil(largest / ROSE_RING_STEP - 1e-9) * ROSE_RING_STEP), 6)
    rings = [round(k * ROSE_RING_STEP, 6) for k in range(1, int(round(ring_max / ROSE_RING_STEP)) + 1)]
    slot = width / len(seasons)
    cy = ROSE_TITLE_SIZE_PT + ROSE_SUBTITLE_SIZE_PT + 12.0 + ROSE_RADIUS_PT + 6.0
    half = 360.0 / SECTOR_COUNT / 2.0

    parts = [_svg_open(width, height, "Seasonal wind roses, direction the wind comes from", tokens)]
    roses = []
    for index, season in enumerate(seasons):
        sectors = list(season["sectors"])
        if len(sectors) != SECTOR_COUNT:
            raise ValueError(f"render_wind_roses: {SECTOR_COUNT} sectors, got {len(sectors)}")
        cx = slot * index + slot / 2
        parts.append(_text(cx, ROSE_TITLE_SIZE_PT + 2.0, f"{season['title']} ({season['months']})", font=FONT_PROSE, size=ROSE_TITLE_SIZE_PT, fill=ink, anchor="middle", weight="600"))
        parts.append(_text(cx, ROSE_TITLE_SIZE_PT + ROSE_SUBTITLE_SIZE_PT + 5.0, "wind from", font=FONT_PROSE, size=ROSE_SUBTITLE_SIZE_PT, fill=muted, anchor="middle"))
        # Ring labels read outward along the north-east diagonal, clear of
        # the cardinal letters on the axes.
        diagonal = math.radians(45.0)
        for ring in rings:
            r = ROSE_RADIUS_PT * ring / ring_max
            parts.append(f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r)}" fill="none" stroke="{rule}" stroke-width="{_fmt(GRID_STROKE_PT)}"/>')
            parts.append(_text(cx + (r + 1.0) * math.sin(diagonal) + 1.0, cy - (r + 1.0) * math.cos(diagonal) - 0.5,
                               f"{round(ring * 100):d}%", font=FONT_DATA, size=ROSE_RING_LABEL_SIZE_PT, fill=muted, anchor="start"))
        wedge_radii = {}
        longest = max(sectors, key=lambda sec: (season["frequency"].get(sec, 0.0), -sectors.index(sec)))
        if season["frequency"].get(longest, 0.0) <= 0:
            longest = None
        if season.get("prevailing") is not None and season["prevailing"] != longest:
            raise ValueError(
                f"render_wind_roses: {season['title']} names {season['prevailing']!r} as prevailing but the "
                f"largest share is {longest!r}; prevailing is the modal sector"
            )
        for i, sector in enumerate(sectors):
            share = season["frequency"].get(sector, 0.0)
            r = ROSE_RADIUS_PT * share / ring_max
            wedge_radii[sector] = r
            if r <= 0:
                continue
            opacity = 1.0 if sector == longest else ROSE_WEDGE_OPACITY
            parts.append(f'<path d="{_wedge(cx, cy, r, i * 360.0 / SECTOR_COUNT, half)}" fill="{ink}" fill-opacity="{_fmt(opacity)}" stroke="{ink}" stroke-width="{_fmt(ROSE_STROKE_PT)}" stroke-linejoin="round"/>')
        for i, sector in enumerate(sectors):
            if i % 2:
                continue    # the cardinal points only: N, E, S, W
            angle = math.radians(i * 360.0 / SECTOR_COUNT)
            lx = cx + (ROSE_RADIUS_PT + 7.5) * math.sin(angle)
            ly = cy - (ROSE_RADIUS_PT + 7.5) * math.cos(angle) + ROSE_COMPASS_SIZE_PT * 0.35
            parts.append(_text(lx, ly, sector, font=FONT_PROSE, size=ROSE_COMPASS_SIZE_PT, fill=ink, anchor="middle"))
        if season.get("speed_label"):
            parts.append(_text(cx, cy + ROSE_RADIUS_PT + 20.0, "mean", font=FONT_PROSE, size=ROSE_SPEED_SIZE_PT, fill=muted, anchor="end"))
            parts.append(_text(cx + 2.5, cy + ROSE_RADIUS_PT + 20.0, season["speed_label"], font=FONT_DATA, size=ROSE_SPEED_SIZE_PT, fill=ink, anchor="start"))
        roses.append(
            {
                "title": season["title"],
                "centre": (cx, cy),
                "radius": ROSE_RADIUS_PT,
                "sectors": sectors,
                "wedge_radii": wedge_radii,
                "prevailing": longest,
            }
        )
    parts.append("</svg>")
    return {"svg": "".join(parts), "frame": frame, "ring_max": ring_max, "rings": rings, "roses": roses}
