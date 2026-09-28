"""
soils_section.py

THE SOILS & GEOLOGY SECTION'S CONTENT (section VII), formatted for the
page -- the one place soils_derivations' centimetres and cell counts
become the imperial, rounded, worded values the template sets, plus the
section's map.

    build_soils_section(inputs, tokens) -> the section dict

THREE PAGES, AN INVENTORY. What the survey recorded and where, and
nothing about what to do with it: no amendment, no crop, no management
prescription. test_soils_section.py greps the section's words for that
language, the reverse of Landform's grep.

THE PAGES, top to bottom.

  ONE, THE SOIL MAP. The summary line, each map unit polygon outlined and
  labelled with its symbol over the parcel, and then the two
  CLASSIFICATION tables -- land capability and farmland, three or four
  rows of acres each.

  TWO, THE MAP UNIT TABLE AND THE PHYSICAL PROPERTIES. What each symbol
  on the map means and how much ground it covers, then the surface
  horizon by map unit: the particle-size split, water capacity, organic
  matter, reaction, and the depth at which something stops a root. Then
  the profile's own water figure, on its own depth basis.

  THREE, EROSION AND GEOLOGY. The K and T factors, the formation as a
  line, the cross-reference and the sources.

THE MAP UNIT TABLE IS NOT ON THE MAP'S PAGE, though the two were
specified as a pair and that pairing is what makes either usable: without
the map a reader knows what soils are present but not where. It does not
fit. The map draws at 453 pt of an 878 pt page at the extent and scale
every section's map shares, and seven map units are 261 pt of table
against the 182 pt left under the caption -- measured, with three units
the most that ever fit there. Rather than leave a half-page of air
defending an ordering that does not survive a parcel with a normal number
of soils, the page takes the two CLASSIFICATION tables, the shortest and
most scannable in the section, and the map unit table gets the room it
needs overleaf beside the properties it belongs with. The map caption
says where it went, and the section reads in one direction either way.

TEXTURE IS A SENTENCE, NOT A COLUMN (the author's decision, phase 2
review). Every one of the reference parcel's seven map units is a silt
loam, and a column reading "Silt loam" seven times is a table doing a
sentence's job. The named class leads the SUMMARY LINE -- "silt loam
throughout" is the phrase a grower uses -- and the table carries the
sand, silt and clay split, which is where the units actually differ
(15 to 30 percent sand across them). A unit whose surface carries rock
fragments enough to change the survey's own phrase for it -- the
reference parcel's channery silt loam, on a tenth of an acre -- is the
caption's business: it is an exception to the sentence, not a column.
Where the units do NOT share one class the sentence names the classes
covering the most ground and the caption says how many are left.

pH LEADS BESIDE IT. Of everything on these pages it is the figure most
likely to prompt action, and a column is the wrong place for it. The
summary line carries the range and says plainly what it means -- acid,
across the whole parcel, on this ground.

THE RESTRICTION THAT IS NOT BEDROCK SITS AGAINST THE CELL IT CORRECTS.
The reference parcel's Ernest component honestly reports no bedrock
within the depth the survey described, so its column reads "deeper than
72 in"; there is a fragipan at 28 in. A reader scanning that column
concludes six feet of rootable profile where there is about two. The
note is set INSIDE the cell (the data table's `note`), not in a caption
under seven rows, because a caption there is read after the conclusion
it exists to prevent.

THE MAP'S PLATE. Each map unit is outlined in ink over a light neutral
tint, its symbol set in the data face at the polygon's widest point
(report_map's polygon labels). THE TINT IS NOT A VALUE: it cycles
through SOIL_TINTS so that neighbouring units read as separate shapes,
and the caption says so -- a graduated ramp would imply an order the map
units do not have. Contours read through beneath, set back, as they do
on the Access map: this map is about boundaries, not terrain. AT
LANDFORM'S EXTENT AND SCALE, so a reader can compare Landform's
contours, Water's streams, Access's roads, Trees' canopy and this map as
pictures of the same land. The contours here carry no elevations: a
symbol set inside a polygon lands in the same gap an index contour's
label wants, and two labels in one face fighting over it reads as
neither. The interval is in the legend and Landform is where a height is
read.

A POLYGON TOO SMALL FOR ITS SYMBOL IS LEFT UNLABELLED AND THE CAPTION
SAYS HOW MANY -- report_map's rule, the same one the index contours
follow. The table beneath the map carries every symbol, so nothing is
lost; crowding a sliver would cost more than the symbol is worth.

THE SURVEY IS NOT A SOIL TEST, and the section says so where organic
matter and pH are read, not in the methods. A map unit's values are
representative of that unit across a whole county, not measured on this
ground, and those two move most between one field and the next.

EVERY ACREAGE TABLE SUMS TO THE COVER'S ACREAGE (landform_section.
allocate_exactly). A dash is a true zero; a nonzero value below display
precision reads "<0.1". Mono only for measurements. One caveat per
figure, at the point of use; one source line per source; full citations
and methods under `methods` for the note Site overview will render.
"""

from typing import Optional

import bedrock_geology as bg
import report_map
import report_text as rt
import soil_survey as ss
from soil_data import SSURGO_COMPILATION_SCALE
import soils_derivations as sd
from landform_section import (
    ZERO_DASH,
    _one_decimal,
    _one_decimal_or_dash,
    allocate_exactly,
    format_retrieved_on,
)
from report_outline import section_number

SECTION_NAME = "Soils & geology"
SECTION_TEMPLATE = "soils.html"

CM_PER_INCH = 2.54

# --- the soil map's plate -----------------------------------------------
# LANDFORM'S FRAME, EXTENT AND SCALE: report_map.render_map() fits the
# same frame to the same boundary, so the map is the other sections' map
# of this parcel with different layers on it.
UNIT_STROKE_PT = 0.7
CONTOUR_OPACITY = 0.45
# THE TINT SEPARATES, IT DOES NOT RANK. Five light values of the ink
# token, assigned so that NO TWO UNITS THAT TOUCH ON THE GROUND SHARE ONE
# and touching units are as far apart in the ramp as the five allow (see
# assign_tints). Light enough that a contour and a knocked-out mono symbol
# both read through: the darkest is a fifth of the ink. Ordering them by
# area instead -- the obvious thing -- puts near-equal tints against each
# other wherever the map unit order happens not to match the ground, which
# is most of the time.
SOIL_TINTS = (0.06, 0.15, 0.09, 0.20, 0.12)
# Two clipped map unit polygons are neighbours when they come this close.
# The service clips them from one coverage, so touching polygons share a
# boundary to within rounding.
NEIGHBOUR_TOLERANCE_M = 1.0

# THE USDA SOIL REACTION CLASSES, in the Soil Survey Manual's own breaks
# and its own words (SSURGO gives 1:1 water pH). Only the class is stated
# -- the section says what the number IS, never what to do about it. A
# range spanning several classes is named by its ends, so 5.0 to 5.9 reads
# "very strongly to moderately acid" rather than a list.
PH_CLASSES = (
    (0.0, 3.5, "ultra acid"),
    (3.5, 4.5, "extremely acid"),
    (4.5, 5.1, "very strongly acid"),
    (5.1, 5.6, "strongly acid"),
    (5.6, 6.1, "moderately acid"),
    (6.1, 6.6, "slightly acid"),
    (6.6, 7.4, "neutral"),
    (7.4, 7.9, "slightly alkaline"),
    (7.9, 8.5, "moderately alkaline"),
    (8.5, 9.1, "strongly alkaline"),
    (9.1, 14.0, "very strongly alkaline"),
)

# Below this share of the parcel a map unit's texture is an exception the
# caption names rather than a class the summary line lists.
TEXTURE_MINOR_SHARE = 0.05
# A pH range narrower than this is one figure.
PH_SAME = 0.05



# ======================================================================
# Formatting
# ======================================================================


def _inches(centimeters: float) -> str:
    return f"{round(centimeters / CM_PER_INCH):,.0f}"


def _normalised(name: Optional[str]) -> str:
    """A survey name folded to lower case, to compare two of them or set one
    mid-sentence ('Silt loam' -> 'silt loam')."""
    return (name or "").strip().lower()


_word = rt.word
# The map unit glossed at first use on each page it is read on.
MAP_UNIT_GLOSS = rt.MAP_UNITS
MAIN_SOIL = "each unit's main soil"


def ph_class(ph: float) -> str:
    for low, high, name in PH_CLASSES:
        if low <= ph < high:
            return name
    return PH_CLASSES[-1][2]


def ph_class_span(low: float, high: float) -> str:
    """The reaction across a range, named by its ends: one class where
    both fall in it, "very strongly to moderately acid" where they do
    not. The shared word is not repeated -- "strongly to moderately acid",
    not "strongly acid to moderately acid"."""
    first, last = ph_class(low), ph_class(high)
    if first == last:
        return first
    head, tail = first.split(), last.split()
    if head[-1] == tail[-1]:
        return f"{' '.join(head[:-1])} to {last}"
    return f"{first} to {last}"


def capability_phrase(label: str) -> list:
    """The row label for a capability class: the code in the data face,
    and the subclass's own word after it. SSURGO's subclass letters are
    not self-explaining, and the letter alone would be a code the reader
    has to look up."""
    if label == sd.NO_DATA:
        return ["No class assigned"]
    digits = "".join(c for c in label if c.isdigit())
    subclass = label[len(digits):]
    word = ss.CAPABILITY_SUBCLASS_LABELS.get(subclass)
    parts = ["Class ", {"value": label}]
    if word:
        parts.append(f", limited by {word}")
    return parts


# ======================================================================
# The map
# ======================================================================


def neighbours(derived: sd.SoilsDerived, tolerance_m: float = NEIGHBOUR_TOLERANCE_M) -> dict:
    """{mukey: {mukey, ...}} -- which map units touch on the ground."""
    adjacency = {mukey: set() for mukey in derived.order}
    for i, a in enumerate(derived.order):
        geometry_a = derived.map_units[a]["geometry_utm"]
        if geometry_a is None or geometry_a.is_empty:
            continue
        grown = geometry_a.buffer(tolerance_m)
        for b in derived.order[i + 1:]:
            geometry_b = derived.map_units[b]["geometry_utm"]
            if geometry_b is None or geometry_b.is_empty:
                continue
            if grown.intersects(geometry_b):
                adjacency[a].add(b)
                adjacency[b].add(a)
    return adjacency


def assign_tints(derived: sd.SoilsDerived, tints: tuple = SOIL_TINTS) -> dict:
    """
    {mukey: opacity} -- a greedy map colouring over SOIL_TINTS, largest
    unit first.

    A unit takes a tint NO NEIGHBOUR ALREADY HAS, preferring one nothing
    on the map has yet -- so the ramp is used as widely as the parcel
    allows -- and, among those, the one furthest from its neighbours', so
    a shared boundary is always a visible step. Where every tint is
    already on a neighbour (a unit hemmed in by more units than there are
    tints) it takes the most distant rather than failing: the outline
    carries the boundary in that case and the symbol carries the identity.
    Deterministic for a given parcel: the map unit order decides, and ties
    go to the earliest tint in the ramp.
    """
    adjacency = neighbours(derived)
    assigned = {}
    for mukey in derived.order:
        taken = {assigned[n] for n in adjacency[mukey] if n in assigned}
        used = set(assigned.values())
        free = [t for t in tints if t not in taken] or list(tints)

        def rank(tint, taken=taken, used=used):
            distance = min((abs(tint - other) for other in taken), default=max(tints))
            return (tint not in used, distance, -tints.index(tint))

        assigned[mukey] = max(free, key=rank)
    return assigned


def build_map_layers(derived: sd.SoilsDerived, contours: dict) -> list:
    """Contours set back beneath, then one layer per map unit -- its
    polygon outlined in ink over its tint, labelled with its symbol. One
    layer each rather than one layer of many geometries, because the tint
    is per unit and a layer carries one style."""
    layers = list(report_map.contour_layers(contours, legend=None))
    for spec in layers:
        spec["stroke_opacity"] = CONTOUR_OPACITY
        # THE CONTOURS CARRY NO ELEVATIONS ON THIS MAP. Every other
        # section's index contour is labelled with its height; here the
        # symbol inside each polygon is the map's subject and lands in the
        # same space, and two labels in the same face fighting over one
        # gap reads as neither. The interval is in the legend, and Landform
        # is where a reader goes for a height.
        spec["labels"] = None
    if layers:
        layers[0]["legend"] = [f"Contours, {contours['interval_ft']} ft"]
    tints = assign_tints(derived)
    for mukey in derived.order:
        unit = derived.map_units[mukey]
        symbol = unit["musym"]
        layers.append(report_map.layer(
            f"unit-{symbol or mukey}", [unit["geometry_utm"]], kind="polygon",
            stroke="ink", stroke_width=UNIT_STROKE_PT, fill="ink", fill_opacity=tints[mukey],
            labels=[symbol] if symbol else None,
        ))
    return layers


def unlabelled_units(rendered: dict, derived: sd.SoilsDerived) -> list:
    """The map units whose polygon could not hold its symbol, in the map
    unit table's order -- report_map decided it, this only reads the
    record back."""
    missed = []
    for mukey in derived.order:
        symbol = derived.map_units[mukey]["musym"]
        placed = rendered["labels_placed"].get(f"unit-{symbol or mukey}")
        if symbol and placed is not None and not placed[0]:
            missed.append(mukey)
    return missed


# ======================================================================
# Page one: the summary line, the map and the map unit table
# ======================================================================


def texture_classes(derived: sd.SoilsDerived) -> list:
    """[{'texture', 'cells', 'share'}] over the parcel, largest first --
    the base class, so the channery variant of a silt loam is a silt
    loam here and an exception in the caption."""
    totals = {}
    for mukey, block in derived.properties.items():
        name = block["texture_class"]
        if name:
            totals[name] = totals.get(name, 0) + derived.map_units[mukey]["cells"]
    on_parcel = derived.cells["on_parcel_count"] or 1
    return sorted(
        ({"texture": n, "cells": c, "share": c / on_parcel} for n, c in totals.items()),
        key=lambda t: -t["cells"],
    )


def ph_range(derived: sd.SoilsDerived) -> Optional[tuple]:
    values = [b["ph"] for b in derived.properties.values() if b["ph"] is not None]
    return (min(values), max(values)) if values else None


def _count_sentence(derived: sd.SoilsDerived) -> list:
    count = len(derived.order)
    return rt.clause("The survey maps ", rt.count(count, "soil map unit"), f" here, {MAP_UNIT_GLOSS}.")


def _texture_and_reaction(derived: sd.SoilsDerived) -> list:
    textures = texture_classes(derived)
    major = [t for t in textures if t["share"] >= TEXTURE_MINOR_SHARE]
    parts = []
    if len(textures) == 1:
        parts += ["Every one is a ", rt.text(_normalised(textures[0]["texture"])), " at the surface"]
    elif major:
        parts += ["Their surfaces are ", rt.series_text([_normalised(t["texture"]) for t in major]), ", together ",
                  rt.share(sum(t["share"] for t in major)), " of the parcel"]
    ph = ph_range(derived)
    if ph:
        low, high = ph
        parts.append(", at " if textures else "The surface horizon reads ")
        parts += ["pH ", rt.number(low, 1)] if abs(high - low) < PH_SAME else ["pH ", rt.number(low, 1), "–", rt.number(high, 1)]
        parts.append(f" — {ph_class_span(low, high)}.")
    elif textures:
        parts.append(".")
    return rt.clause(*parts)


def build_summary(derived: sd.SoilsDerived) -> list:
    """The section's two sentences: how many units, then the texture class
    the parcel is and the reaction it is at (see the module docstring)."""
    return rt.sentences(_count_sentence(derived), _texture_and_reaction(derived))


def _unlabelled_clause(derived: sd.SoilsDerived, missed: list) -> list:
    if not missed:
        return None
    symbols = [derived.map_units[m]["musym"] for m in missed]
    acres = allocate_exactly([derived.map_units[m]["cells"] for m in derived.order],
                             derived.cells["on_parcel_count"] * derived.cells["cell_acres"], 1)
    missed_acres = sum(a for m, a in zip(derived.order, acres) if m in missed)
    return rt.clause(rt.series_text(symbols), " — ", rt.acres(missed_acres), " — ", rt.agree(len(missed), "is", "are"),
                     " too small to label; the table overleaf names every symbol.")


def build_map_caption(derived: sd.SoilsDerived, missed: list) -> list:
    """The one caveat at the point of use: the lines are approximate and
    the shades carry no value; then the symbols the map could not carry."""
    return rt.sentences(rt.clause("Lines are the survey's own, drawn at ", rt.scale(SSURGO_COMPILATION_SCALE),
                                  " and approximate; shades only tell neighbours apart."),
                        _unlabelled_clause(derived, missed) or ["The table overleaf names every symbol."])


def build_map_unit_table(derived: sd.SoilsDerived, with_drainage: bool = False) -> dict:
    """Symbol and name, acres, share, hydrologic group, Ksat, capability
    and farmland classification -- one row per map unit, plus a total that
    IS the cover's acreage.

    THE DRAINAGE CLASS COLUMN IS CUT, and `with_drainage=True` puts it
    back. Nine columns beside a map unit's full name -- "Gilpin, Weikert,
    Culleoka channery silt loams and 25 to 80 percent slopes" is 72
    characters -- ran 279 pt past the 489.6 pt measure and spilled the
    table onto a second page (measured, phase 2). Drainage class is the
    column to lose: the hydrologic group beside it describes the same
    property more precisely and in one or two characters, and the
    drainage class is carried in full on the physical-properties page's
    own terms. test_soils_section.py renders both and holds the
    difference to account."""
    parcel_acres = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    counts = [derived.map_units[m]["cells"] for m in derived.order]
    acres = allocate_exactly(counts, parcel_acres, 1)
    shares = allocate_exactly(counts, 100.0, 1)
    columns = ["Acres", "% of parcel"]
    if with_drainage:
        columns.append("Drainage")
    columns += ["Group", "Ksat", "Class", "Farmland"]
    rows = []
    for mukey, acre, share, count in zip(derived.order, acres, shares, counts):
        unit = derived.map_units[mukey]
        cells = [_one_decimal_or_dash(acre, count), _one_decimal_or_dash(share, count)]
        if with_drainage:
            cells.append({"value": unit["drainage_class"] or "no data", "kind": "text"})
        cells += [
            unit["hydrologic_group"] or _word("no data"),
            f"{unit['ksat_um_s']:.2f}" if unit["ksat_um_s"] is not None else _word("no data"),
            unit["capability_class"] and sd.capability_label(unit["capability_class"], unit["capability_subclass"])
            or _word("no data"),
            {"value": unit["farmland"] or "not classified", "kind": "text"},
        ]
        label = [{"value": unit["musym"]}, " "] if unit["musym"] else []
        rows.append({"label": label + [unit["muname"] or "unnamed"], "cells": cells})
    total = [_one_decimal(sum(acres)), _one_decimal(sum(shares))]
    total += [""] * (len(columns) - 2)
    rows.append({"label": "Total", "cells": total})
    text_columns = ["Farmland"] + (["Drainage"] if with_drainage else [])
    return {"corner": "Map unit", "columns": columns, "rows": rows, "text_columns": text_columns,
            "variant": "units"}


def build_map_unit_caption(derived: sd.SoilsDerived) -> list:
    """The column headers stay the survey's; the caption says what they
    mean. Whose values they are and how acres are counted are in the
    methods note."""
    return [f"Each row is a map unit, {rt.MAP_UNIT}, and its figures are its main soil's. Group is the "
            "hydrologic soil group, how readily the soil takes in rain, from A (freely) to D (hardly); conductivity is how "
            "fast water moves through the top layer, in micrometres per second; Class is land capability, the survey's "
            "grade of the soil's limits, higher meaning more limited, with the main limit named."]


# ======================================================================
# Page two: physical properties
# ======================================================================


def bedrock_cell(block: dict, note: Optional[list]) -> dict:
    """The depth to bedrock, or the depth the survey looked to when it
    described none -- a BOUND, set muted so it cannot be read across a
    column of depths -- carrying its restriction note where there is
    one."""
    if block["bedrock_cm"] is None:
        return _word("no data")
    value = _inches(block["bedrock_cm"])
    cell = {"value": f"> {value}" if block["bedrock_is_bound"] else value,
            "kind": "bound" if block["bedrock_is_bound"] else None}
    if note:
        cell["note"] = note
    return cell


def _root_note(note: dict) -> list:
    """'fragipan at 28 in stops roots above the rock': set in the cell it corrects."""
    return rt.clause(f"{_normalised(note['kind'])} at ", rt.inches_of(note["depth_cm"] / CM_PER_INCH, places=0),
                     " stops roots above the rock")


def build_properties_table(derived: sd.SoilsDerived) -> Optional[dict]:
    """The dominant major component's surface horizon by map unit: the
    horizon's own depth, the particle-size split, water capacity, organic
    matter, reaction and the depth to bedrock. No texture column: the
    class is the summary line's (see the module docstring)."""
    if not derived.properties:
        return None
    notes = {n["mukey"]: n for n in sd.restriction_notes(derived.properties, derived.map_units)}
    rows = []
    for mukey in derived.order:
        block = derived.properties.get(mukey)
        if block is None:
            continue
        unit = derived.map_units[mukey]
        label = ([{"value": unit["musym"]}, " "] if unit["musym"] else []) + [block["compname"] or "unnamed"]
        if block["top_cm"] is None:
            rows.append({"label": label, "cells": [_word("no horizon described")] + [ZERO_DASH] * 7})
            continue
        note = notes.get(mukey)
        note_parts = _root_note(note) if note else None
        rows.append({"label": label, "cells": [
            f"{_inches(block['top_cm'])}–{_inches(block['bottom_cm'])}",
            f"{block['sand_pct']:.0f}" if block["sand_pct"] is not None else ZERO_DASH,
            f"{block['silt_pct']:.0f}" if block["silt_pct"] is not None else ZERO_DASH,
            f"{block['clay_pct']:.0f}" if block["clay_pct"] is not None else ZERO_DASH,
            f"{block['awc']:.2f}" if block["awc"] is not None else _word("no data"),
            f"{block['om_pct']:.1f}" if block["om_pct"] is not None else _word("no data"),
            f"{block['ph']:.1f}" if block["ph"] is not None else _word("no data"),
            bedrock_cell(block, note_parts),
        ]})
    return {"corner": "Surface horizon", "rows": rows, "variant": "horizon",
            "columns": ["Depth in", "Sand %", "Silt %", "Clay %", "AWC in/in", "Organic %", "pH", "To bedrock in"],
            "text_columns": []}


def build_properties_caption(derived: sd.SoilsDerived) -> list:
    """Two facts at the point of use: whose values these are, and the
    texture exception the summary line's sentence does not cover."""
    parts = ["Each row is a unit's main soil at its top layer below any leaf litter; that layer starts deeper under some units "
             "than others, so its depth is given. "]
    variants = []
    for mukey in derived.order:
        block = derived.properties.get(mukey)
        if not block or not block["texture"] or not block["texture_class"]:
            continue
        if _normalised(block["texture"]) != _normalised(block["texture_class"]):
            variants.append((derived.map_units[mukey]["musym"], block["texture"], derived.map_units[mukey]["cells"]))
    if variants:
        parcel_acres = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
        acres = allocate_exactly([derived.map_units[m]["cells"] for m in derived.order], parcel_acres, 1)
        by_mukey = dict(zip([derived.map_units[m]["musym"] for m in derived.order], acres))
        named = [f"{symbol} is a {_normalised(texture)}" for symbol, texture, _ in variants]
        total = sum(by_mukey.get(symbol, 0.0) for symbol, _, _ in variants)
        parts += rt.clause("The survey's own name differs on ", rt.count(len(variants), "unit"), " — ", rt.series_text(named),
                           ", for the stones in it — on ", rt.acres(total), ". ")
    parts.append("Sand, silt and clay are typical percentages, so they need not add up exactly.")
    return rt.sentences(parts)


def build_soil_test_statement(derived: sd.SoilsDerived) -> list:
    """THE ONE SENTENCE THIS SECTION OWES A READER, at the point of use
    rather than in the methods: a map unit's values describe that unit
    wherever it is mapped, not this ground, and organic matter and pH are
    where that is weakest."""
    return ["These are representative values for each map unit across the whole survey area, not measurements taken "
            "on this property. Organic matter and pH vary field to field within one unit and shift with what has been "
            "done to the ground; a soil test is the only way to know either on this parcel."]


def build_profile_water(derived: sd.SoilsDerived) -> Optional[list]:
    """The whole profile's available water, acre-weighted, as one figure
    with ITS OWN DEPTH BASIS STATED -- which is why it is not a column in
    the table above, whose every figure is the surface horizon's."""
    values, counts = [], []
    for mukey in derived.order:
        block = derived.properties.get(mukey)
        if block and block["aws0150_cm"] is not None:
            values.append(block["aws0150_cm"])
            counts.append(derived.map_units[mukey]["cells"])
    if not values:
        return None
    total = sum(counts) or 1
    weighted = sum(v * c for v, c in zip(values, counts)) / total
    low, high = min(values), max(values)
    depth = ss.AWS_DEPTH_CM
    return rt.clause("Down through the whole soil, to ", rt.centimeters(depth), " (about ",
                     rt.inches_of(depth / CM_PER_INCH, places=0), "), the survey credits the parcel with ",
                     rt.inches_of(weighted / CM_PER_INCH), " of available water — what roots can draw — from ",
                     rt.number(low / CM_PER_INCH, 1), " to ", rt.inches_of(high / CM_PER_INCH), " by unit: a deeper measure "
                     "than the top-layer figures above, so it stands apart from them.")


# ======================================================================
# Page three: classification, erosion and geology
# ======================================================================


def build_capability_table(derived: sd.SoilsDerived) -> Optional[dict]:
    labels = derived.capability["labels"]
    if not labels:
        return None
    parcel_acres = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    counts = [derived.capability["counts"][l] for l in labels]
    acres = allocate_exactly(counts, parcel_acres, 1)
    shares = allocate_exactly(counts, 100.0, 1)
    rows = [{"label": capability_phrase(label),
             "cells": [_one_decimal_or_dash(a, c), _one_decimal_or_dash(s, c)]}
            for label, a, s, c in zip(labels, acres, shares, counts)]
    rows.append({"label": "Total", "cells": [_one_decimal(sum(acres)), _one_decimal(sum(shares))]})
    return {"corner": "Land capability", "columns": ["Acres", "% of parcel"], "rows": rows, "compact": True}


def build_capability_caption(derived: sd.SoilsDerived) -> list:
    # The absence of an irrigated class is not a caveat on this figure; it
    # is a fact about the survey area, and it is stated in the methods.
    return [f"Land capability without irrigation, the survey's grade of the soil's limits, higher classes more limited, of {MAIN_SOIL}."]


def build_farmland_table(derived: sd.SoilsDerived) -> Optional[dict]:
    values = derived.farmland["values"]
    if not values:
        return None
    parcel_acres = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    counts = [derived.farmland["counts"][v] for v in values]
    acres = allocate_exactly(counts, parcel_acres, 1)
    shares = allocate_exactly(counts, 100.0, 1)
    rows = [{"label": value, "cells": [_one_decimal_or_dash(a, c), _one_decimal_or_dash(s, c)]}
            for value, a, s, c in zip(values, acres, shares, counts)]
    rows.append({"label": "Total", "cells": [_one_decimal(sum(acres)), _one_decimal(sum(shares))]})
    return {"corner": "Farmland classification", "columns": ["Acres", "% of parcel"], "rows": rows, "compact": True}


def build_farmland_caption(derived: sd.SoilsDerived) -> list:
    # "By the ground each unit covers" is the caption above this one, two
    # inches away; what this figure needs said is the conditional grade.
    return ["“If drained” or “if irrigated” is the survey's own: the class holds only once that work is done."]


def build_erosion_table(derived: sd.SoilsDerived) -> Optional[dict]:
    rows = []
    for mukey in derived.order:
        unit, erosion = derived.map_units[mukey], derived.erosion[mukey]
        if erosion["kwfact"] is None and erosion["tfact"] is None:
            continue
        rows.append({"label": ([{"value": unit["musym"]}, " "] if unit["musym"] else []) + [unit["muname"] or "unnamed"],
                     "cells": [f"{erosion['kwfact']:.2f}" if erosion["kwfact"] is not None else _word("no data"),
                               f"{erosion['tfact']:.0f}" if erosion["tfact"] is not None else _word("no data")]})
    if not rows:
        return None
    return {"corner": "Erosion factors", "columns": ["K factor", "T tons/acre/yr"], "rows": rows}


def build_erosion_caption(derived: sd.SoilsDerived) -> list:
    return ["K is how easily the top layer washes away, higher meaning more easily; T is the soil the survey allows to be "
            "lost each year without harming what it can grow. Both describe the soil itself, not how steep this parcel is."]


def build_geology(derived: sd.SoilsDerived) -> Optional[list]:
    """The formation as a value: a line, not a map and not a table."""
    geology = derived.geology
    if not geology or not geology["units"]:
        return None
    units = geology["units"]
    lead = units[0]
    parts = ["The rock beneath is the ", rt.text(lead["name"])]
    if lead["age"]:
        parts.append(f" of {lead['age']} age")
    if lead["age_max_ma"] and lead["age_min_ma"]:
        parts += [" (", rt.number(lead["age_max_ma"]), " to ", rt.number(lead["age_min_ma"]), " million years)"]
    if lead["description"]:
        # The compilation's own words for the rock. They already name the
        # lithologies, so the record's separate lithology list -- whose
        # top-level terms are "clastic" and "sedimentary" -- would only
        # repeat them less precisely.
        parts.append(f": {lead['description'][0].lower()}{lead['description'][1:]}")
    also = None
    if geology["straddles"]:
        under = f", with the {lead['name']} under its centre" if lead["at_centroid"] else ""
        also = [f"The national map also shows the {rt.series_text([u['name'] for u in units[1:]])} across the parcel{under}."]
    province = None
    if lead["province"]:
        province = [f"Both sit in the {lead['province']} province." if geology["straddles"] else f"It sits in the {lead['province']} province."]
    return rt.sentences(rt.clause(*parts, "."), also, province)


def build_geology_caption(derived: sd.SoilsDerived) -> list:
    return rt.clause("The national geologic map is drawn for use at about ", rt.scale(bg.SGMC_INTENDED_SCALE),
                     ", too coarse to place a rock boundary within a parcel: it names the rock, not where one gives way "
                     "to the next.")


def build_cross_reference(inputs: sd.SoilsInputs) -> list:
    """The sentence that stops a reader hunting here for figures that
    live elsewhere: the same survey runs through the whole report."""
    return ["The same soil survey supplies the seasonal water table, flooding and ponding in Water & hydrology, the "
            "road-construction ratings in Access, and the woodland productivity and management limitations in Trees "
            "& forestry; those figures are reported there rather than repeated here."]


# ======================================================================
# Unavailable statements
# ======================================================================


def _unavailable_reason(inputs: sd.SoilsInputs, layer: str) -> str:
    entry = (inputs.unavailable or {}).get(layer) or {}
    if entry.get("reason") == "no_data_for_parcel":
        return "does not cover this parcel"
    return "did not answer"


def build_survey_unavailable(inputs: sd.SoilsInputs) -> list:
    """What stands where the properties table would be. TWO CASES, and the
    statement says which: the survey layer did not answer (or has no data
    here), or it ANSWERED and described no dominant major component for any
    map unit on this parcel -- the survey rows then belong to other units,
    and there is no row to print. The second used to reach the template with
    no statement at all, and the render raised."""
    if inputs.soil_survey is None:
        reason = _unavailable_reason(inputs, "soil_survey")
    else:
        reason = "answered, but described no major component for the map units on this parcel"
    return ["The soil survey's detailed properties are not shown: the Soil Data Access service ",
            reason,
            ". The map unit polygons, their drainage class, hydrologic group, saturated conductivity, farmland "
            "classification and K factor above come from the session's own soil readings and are unaffected; the "
            "map unit symbols, the surface horizon's properties, the land capability class and the T factor are not "
            "available for this report."]


def build_geology_unavailable(inputs: sd.SoilsInputs) -> list:
    """The survey's two cases, for the same reason (build_survey_unavailable):
    no answer, or an answer that maps no unit here."""
    if inputs.bedrock_geology is None:
        reason = _unavailable_reason(inputs, "bedrock_geology")
    else:
        reason = "answered, but maps no geologic unit under this parcel"
    return ["The bedrock geology is not shown: the USGS State Geologic Map Compilation ", reason, "."]


# ======================================================================
# Sources and methods
# ======================================================================


def _survey_line(derived: sd.SoilsDerived) -> str:
    tables = ("mapunit, muaggatt, component, chorizon, chtexturegrp, chtexture and corestrictions")
    if derived.survey_areas:
        survey = derived.survey_areas[0]
        version = (survey.get("saverest") or "").split(" ")[0]
        return f"USDA NRCS SSURGO, soil survey {survey['areasymbol']}, version of {version}: {tables}."
    return f"USDA NRCS SSURGO: {tables}."


def build_sources(inputs: sd.SoilsInputs, derived: sd.SoilsDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    lines = [[f"USDA NRCS SSURGO map unit polygons and components, retrieved {retrieved}."]]
    if inputs.soil_survey is not None:
        lines.append([_survey_line(derived)])
    if inputs.bedrock_geology is not None:
        lines.append([bg.SGMC_CITATION])
    lines.append([f"USGS 3DEP elevation, 1/3 arc-second, resampled to 5 m, retrieved {retrieved}."])
    return lines


def build_methods(inputs: sd.SoilsInputs, derived: sd.SoilsDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    methods = [{
        "source": "USDA NRCS SSURGO",
        "identifier": "mupolygon, mapunit, component, chorizon and corestrictions over the parcel's map units, "
                      "served by Soil Data Access",
        "period": retrieved,
        "citation": ss.SSURGO_CITATION,
        "terms": "U.S. federal work; public domain.",
        "method": "Map unit polygons are clipped to the boundary on the service and rasterised onto the session's 5 m "
                  "grid, the same map-unit cell grid the Water, Access and Trees sections partition; acres are "
                  "allocated exactly to the parcel's polygon area. Each unit's row is its DOMINANT MAJOR COMPONENT "
                  "throughout, because a named texture class cannot be averaged and a row of weighted means carrying "
                  "one component's texture would describe no soil that exists. The surface horizon is the shallowest "
                  "whose name does not begin with O — the same rule the K factor and the saturated conductivity were "
                  "read at, so one row describes one horizon — and its depth is stated per unit. Depth to bedrock is "
                  "the shallowest lithic, paralithic or densic restriction; where none is described the figure is the "
                  "depth the survey described to, set as a bound. A restriction that is not bedrock is reported "
                  "against the depth it qualifies. Land capability is the non-irrigated class and subclass; no "
                  "irrigated class is assigned in this survey area. Values are representative of a map unit across "
                  "the survey area, not measured on this property. Group is the NRCS hydrologic soil group, the runoff class "
                  "assigned from drainage and permeability; conductivity is saturated hydraulic conductivity; K is the "
                  "whole-soil erodibility factor Kw and T the tolerable annual soil loss.",
    }]
    if inputs.bedrock_geology is not None:
        methods.append({
            "source": "USGS State Geologic Map Compilation (SGMC)",
            "identifier": "WFS layer ms:Lithology over the parcel's envelope, attributes only; the unit records from "
                          "the Mineral Resources unit service",
            "period": retrieved,
            "citation": bg.SGMC_CITATION,
            "terms": f"Access constraints: {bg.SGMC_ACCESS_CONSTRAINTS} Use constraints: {bg.SGMC_USE_CONSTRAINTS}. "
                     "U.S. federal work; public domain.",
            "method": "The geologic units the compilation maps across the parcel's bounding box, named from the "
                      "compilation's own unit records. Where more than one unit is returned a second query at the "
                      "parcel's centroid says which lies under its centre, and that one leads. The compilation is "
                      "assembled from state geologic maps ranging from 1:50,000 to 1:1,000,000 and its metadata "
                      f"record states it is intended for use at about 1:{bg.SGMC_INTENDED_SCALE:,} or smaller; this "
                      "state's polygons derive from the Pennsylvania Geological Survey's own 1:250,000 mapping. No "
                      "contact is placed to parcel precision.",
        })
    methods.append({
        "source": "USGS 3DEP",
        "identifier": f"3DEP 1/3 arc-second DEM, resampled to {max(inputs.dem['resolution_meters']):.0f} m, {inputs.dem['crs']}",
        "period": retrieved,
        "citation": "U.S. Geological Survey, 3D Elevation Program seamless DEM, served by The National Map elevation service.",
        "terms": "U.S. federal work; public domain.",
        "method": "Contours at Landform's interval; the map unit cell grid.",
    })
    return methods


# ======================================================================
# The section
# ======================================================================


def build_soils_section(inputs: sd.SoilsInputs, tokens: Optional[dict] = None) -> dict:
    """The section dict soils.html sets. `tokens` defaults to
    site_report.TOKENS."""
    if tokens is None:
        import site_report

        tokens = site_report.TOKENS
    derived = sd.derive(inputs)
    parcel = inputs.boundary_polygon_utm
    # COMPUTED AGAIN HERE, KNOWINGLY -- NOT A BUG, AND NOT FREE TO REMOVE.
    # parcel_contours() runs once per section map: Site overview, Landform,
    # Water, Access, Trees, Soils and Design, seven times per report over the
    # same DEM and interval. About 0.06 s a time and no network
    # (diagnose_report_generation_time.py), so the repeat costs under half a
    # second and cannot fail on a flaky source. Sharing one result means
    # threading it through every section's builder; worth doing only if the
    # report's compute ever matters beside its fetches.
    contours = report_map.parcel_contours(inputs.dem, parcel)
    rendered = report_map.render_map(parcel, build_map_layers(derived, contours), tokens)
    missed = unlabelled_units(rendered, derived)
    # THE STATEMENT FOLLOWS THE TABLE, NOT THE LAYER. The template prints the
    # unavailable statement whenever the table (or the geology line) is
    # absent, so each is built exactly then -- a layer that answered with
    # nothing usable for this parcel leaves the table empty just as an absent
    # layer does, and must not reach the template with no statement.
    properties_table = build_properties_table(derived)
    geology = build_geology(derived)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(derived),
        "map": rendered,
        "map_caption": build_map_caption(derived, missed),
        "map_unit_table": build_map_unit_table(derived),
        "map_unit_caption": build_map_unit_caption(derived),
        "properties_table": properties_table,
        "properties_caption": build_properties_caption(derived),
        "soil_test": build_soil_test_statement(derived),
        "profile_water": build_profile_water(derived),
        "survey_unavailable": None if properties_table else build_survey_unavailable(inputs),
        "capability_table": build_capability_table(derived),
        "capability_caption": build_capability_caption(derived),
        "farmland_table": build_farmland_table(derived),
        "farmland_caption": build_farmland_caption(derived),
        "erosion_table": build_erosion_table(derived),
        "erosion_caption": build_erosion_caption(derived),
        "geology": geology,
        "geology_caption": build_geology_caption(derived) if derived.geology and derived.geology["units"] else [],
        "geology_unavailable": None if geology else build_geology_unavailable(inputs),
        "cross_reference": build_cross_reference(inputs),
        "sources": build_sources(inputs, derived),
        "methods": build_methods(inputs, derived),
        "derived": derived,
        "unlabelled": missed,
        "contours": {k: v for k, v in contours.items() if k != "levels"},
    }
