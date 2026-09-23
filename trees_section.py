"""
trees_section.py

THE TREES & FORESTRY SECTION'S CONTENT (section VI), formatted for the
page -- the one place trees_derivations' metres and cell counts become
the imperial, rounded, worded values the template sets, plus the
section's map.

    build_trees_section(inputs, tokens) -> the section dict

TWO PAGES, AN INVENTORY. The canopy that stands, the forest the model
calls, and the capacity the soil survey rates: extent, height, density,
forest type, woodland productivity and management limitations. Where
trees should go is the trees step's job and appears on the layout map and
in the design record, never here; test_trees_section.py greps the
section's words for that language. Nothing about what the trees ARE:
species composition, identification, vigour, invasive or noxious plants
and anything cultivable are out of the section's scope, and the pages do
not say so -- a note about a gap only draws attention to it.

THE PAGES, top to bottom: the summary line; the canopy map AT LANDFORM'S
EXTENT AND SCALE, the canopy as a screened tint of the field green
graduated by height class, light to dark, the contours reading through
it; its legend and caption; the canopy extent table with its caption.
Then, on the continuation page: the height table (or, on the fallback
path, the stated absence and the product's own cover classes), the forest
type figure, the productivity table, the limitations table, the sources.

THE MAP'S PLATE. Trees are green in the plate system: `field`, the
frontend's map-geometry token, MAP-ONLY here as there. A screened tint
rather than a flat fill -- the mid-century convention for woodland --
lets the contours read through; the height ramp is three screens of the
one token at growing dot sizes (SCREEN_DOT_PT), the darkest capped at
SCREEN_DOT_CAP_PT (about a quarter of the ground inked) so a contour
stays legible over it, on the same principle as Landform's slope tints.
The fallback path draws one screen at the middle weight: there are no
heights to grade. Contours at Landform's interval, full strength, above
the screens.

TWO MEASUREMENTS, BOTH REPORTED. The lidar's canopy and the forest type
model's forest differ (on the reference parcel 1.6 against 0.6 acres),
because one measures height returns at 5 m and the other classifies
forest stands at 30 m. The section reports both, each labelled by what
it measures, and the forest type caption says plainly why they differ --
the same posture as Water's three wet-ground indicators.
"""

import re
from datetime import date
from typing import Optional

import forest_type_data as ftd
import report_map
import soil_woodland as sw
import trees_derivations as td
import water_derivations as wd
from canopy_height_data import CANOPY_SOURCE_LIDAR_HAG, CANOPY_SOURCE_NLCD_TCC
from landform_section import ZERO_DASH, _one_decimal, _one_decimal_or_dash, allocate_exactly, format_retrieved_on
from raster_grid import cell_union_footprint
from report_outline import section_number

SECTION_NAME = "Trees & forestry"
SECTION_TEMPLATE = "trees.html"

METERS_PER_FOOT = report_map.METERS_PER_FOOT

# --- the canopy map's plate ---------------------------------------------
# LANDFORM'S FRAME, EXTENT AND SCALE: report_map.render_map() fits the
# same frame to the same boundary. test_trees_section.py measures the
# scale, the drawn bbox and the frame against Landform's.
SCREEN_TOKEN = "field"
# The height ramp: dot diameter per class, light to dark, on the shared
# 3 pt grid (report_map.SCREEN_SPACING_PT). Coverage pi r^2 / 9: about
# 7%, 15% and 25% of the ground inked. The darkest is CAPPED: a contour
# at 0.45 pt in the terrain token still reads over a quarter-inked screen.
SCREEN_DOT_PT = {"15-30": 0.9, "30-50": 1.3, "50+": 1.7}
SCREEN_DOT_CAP_PT = 1.7
# The fallback's single tint: the middle weight.
SCREEN_DOT_SINGLE_PT = 1.3
# The species table: the survey's list, cut where the ground rated stops
# being meaningful -- the six species on the most ground, and none rated
# on less than this share of the parcel.
SPECIES_ROWS_MAX = 6
SPECIES_MIN_SHARE = 0.05
# A limiting feature is named when it affects at least this share of its
# class's ground; the rest are in the methods note.
FEATURE_NAME_SHARE = 0.25
FEATURE_NAME_MAX = 4

HAG_NATIVE_M = 2.0
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

FOREST_TYPE_CAVEAT = "a model imputing inventory plots to 30 m pixels, the type of forest occupying an area, not what stands on any acre"
CLOSURE_LABEL = "closure at a 30 m grain, derived from the height threshold"


# ======================================================================
# Formatting
# ======================================================================


def _ft(meters: float) -> str:
    return f"{round(meters / METERS_PER_FOOT):,}"


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def _acres_word(value: float) -> str:
    return f"{value:,.1f} ac"


def _lower(name: str) -> str:
    return name[:1].lower() + name[1:] if name else name


def _list(words: list) -> str:
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _word(text: str) -> dict:
    return {"value": text, "kind": "word"}


def hag_acquisition_year(source_item_id) -> Optional[int]:
    """The acquisition year the STAC item id encodes (the USGS project
    name, e.g. PA_WesternPA_2_2019-hag-2m-5-4), parsed DEFENSIVELY: the
    first four-digit year between 1990 and this year, else None -- an
    unexpected id format produces a stated absence, never a wrong year."""
    if not isinstance(source_item_id, str):
        return None
    for match in YEAR_RE.finditer(source_item_id):
        year = int(match.group(1))
        if 1990 <= year <= date.today().year:
            return year
    return None


def group_name(code: int) -> str:
    """The service's label in running text: 'Oak / hickory' -> 'oak/hickory'."""
    return _lower(ftd.FOREST_TYPE_GROUPS[code]).replace(" / ", "/")


def compass_word(sector: Optional[str]) -> Optional[str]:
    return {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east", "S": "south", "SW": "south-west",
            "W": "west", "NW": "north-west"}.get(sector or "")


# ======================================================================
# The map
# ======================================================================


def _cells_polygon(dem: dict, mask, parcel):
    if not mask.any():
        return None
    footprint = cell_union_footprint(dem, mask)
    clipped = wd._polygonal(footprint.intersection(parcel))
    return clipped


def height_class_masks(inputs: td.TreesInputs, derived: td.TreesDerived) -> dict:
    """{class: on-parcel cell mask} for the canopy classes present."""
    array = inputs.canopy["array"]
    masks = {}
    for name, low, high in td.HEIGHT_CLASSES:
        if name not in td.CANOPY_CLASSES:
            continue
        mask = derived.canopy["mask"] & (array >= low)
        if high is not None:
            mask &= array < high
        if mask.any():
            masks[name] = mask
    return masks


def build_map_layers(inputs: td.TreesInputs, derived: td.TreesDerived, contours: dict) -> list:
    """Drawing order: the canopy screens (light to dark), then the
    contours at full strength, so the linework reads through the dots."""
    parcel = inputs.boundary_polygon_utm
    layers = []
    if derived.canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        for name, mask in height_class_masks(inputs, derived).items():
            polygon = _cells_polygon(inputs.dem, mask, parcel)
            if polygon is None:
                continue
            label = td.HEIGHT_CLASS_LABELS[name]
            legend = ["Canopy ", {"value": label}] if name == td.CANOPY_CLASSES[0] else [{"value": label}]
            layers.append(report_map.layer(
                f"canopy-{name}", [polygon], kind="screen", fill=SCREEN_TOKEN,
                screen_dot_pt=min(SCREEN_DOT_PT[name], SCREEN_DOT_CAP_PT), legend=legend,
            ))
    else:
        polygon = _cells_polygon(inputs.dem, derived.canopy["mask"], parcel)
        if polygon is not None:
            layers.append(report_map.layer(
                "canopy", [polygon], kind="screen", fill=SCREEN_TOKEN, screen_dot_pt=SCREEN_DOT_SINGLE_PT,
                legend=["Canopy, NLCD cover ", {"value": str(derived.canopy["year"])}],
            ))
    layers += report_map.contour_layers(contours, legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}])
    return layers


def empty_map_note(derived: td.TreesDerived, parcel) -> Optional[dict]:
    if derived.canopy["counts"][td.CANOPY] > 0:
        return None
    if derived.canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        lines = ["No canopy at 15 ft", "on this parcel"]
    else:
        lines = ["No canopy cover", "on this parcel"]
    return {"lines": lines, "point": parcel.centroid}


# ======================================================================
# The words
# ======================================================================


def _acre_values(derived: td.TreesDerived, counts: list) -> tuple:
    total = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    return allocate_exactly(counts, total, 1), allocate_exactly(counts, 100.0, 1)


def build_summary(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    canopy = derived.canopy
    counts = canopy["counts"]
    acres, shares = _acre_values(derived, [counts[td.CANOPY], counts[td.OPEN], counts[td.NO_DATA]])
    parts = []
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        year = hag_acquisition_year(canopy["source_item_id"])
        parts += ["Lidar" + (f" of {year}" if year else "") + " measures ", {"value": _acres_word(acres[0])}, " of canopy at 15 ft and over, ",
                  {"value": _pct(shares[0])}, " of the parcel"]
        if counts[td.CANOPY] > 0:
            parts += [", ", {"value": _pct(derived.closure["mean_pct"])}, " closed at a 30 m grain; the tallest cell is ",
                      {"value": f"{_ft(derived.heights['max_m'])} ft"}, ". "]
        else:
            parts += [". "]
    else:
        parts += ["NLCD Tree Canopy Cover of ", {"value": str(canopy["year"])}, " marks ", {"value": _acres_word(acres[0])}, " of canopy, ",
                  {"value": _pct(shares[0])}, " of the parcel"]
        if counts[td.CANOPY] > 0:
            parts += [", ", {"value": _pct(derived.cover["mean_pct"])}, " mean cover within it; no lidar height is available here. "]
        else:
            parts += ["; no lidar height is available here. "]
    forest = derived.forest_type
    if forest["fetched"]:
        if forest["forest_cells"] == 0:
            parts += ["The forest type model calls none of the parcel forest. "]
        else:
            f_acres = allocate_exactly([forest["forest_cells"], derived.cells["on_parcel_count"] - forest["forest_cells"]],
                                       derived.cells["on_parcel_count"] * derived.cells["cell_acres"], 1)
            names = _list([group_name(code) for code in forest["groups"] if code != ftd.NON_FOREST])
            parts += ["The forest type model calls ", {"value": _acres_word(f_acres[0])}, f" of it forest, {names}. "]
    lim = derived.limitations
    if lim["fetched"]:
        wind = lim["interpretations"][sw.WINDTHROW]["counts"]
        w_acres, _ = _acre_values(derived, list(wind.values()))
        moderate = w_acres[list(wind).index("Moderate")]
        severe = w_acres[list(wind).index("Severe")]
        if wind["Moderate"] or wind["Severe"]:
            parts += ["The soil survey rates windthrow hazard moderate on ", {"value": _acres_word(moderate)}]
            if wind["Severe"]:
                parts += [" and severe on ", {"value": _acres_word(severe)}]
            parts += ["."]
        else:
            parts += ["The soil survey rates windthrow hazard slight on the whole parcel."]
    return parts


def build_map_caption(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    canopy = derived.canopy
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        year = hag_acquisition_year(canopy["source_item_id"])
        when = f"acquired {year}" if year else "acquisition year not stated by the source record"
        return ["Lidar first-return height above ground, ", {"value": f"{HAG_NATIVE_M:.0f} m"}, f", {when}, resampled to the ",
                {"value": "5 m"}, " grid; canopy is a cell at or above the design's ", {"value": "15 ft"},
                " threshold, so a roof reads as canopy too. Contours at Landform's interval."]
    return ["NLCD Tree Canopy Cover ", {"value": str(canopy["year"])}, ", percent cover per ", {"value": "30 m"},
            " pixel sampled onto the 5 m grid, any nonzero pixel counted, so edges are 30 m steps and thin strips may be missed; "
            "no lidar height exists here, so the canopy is one tint. Contours at Landform's interval."]


def build_extent_table(derived: td.TreesDerived) -> dict:
    counts = derived.canopy["counts"]
    names = [td.CANOPY, td.OPEN] + ([td.NO_DATA] if counts[td.NO_DATA] else [])
    acres, shares = _acre_values(derived, [counts[n] for n in names])
    labels = {td.CANOPY: "Canopy" if derived.canopy["source"] == CANOPY_SOURCE_NLCD_TCC else "Canopy, 15 ft and over",
              td.OPEN: "Open, no canopy" if derived.canopy["source"] == CANOPY_SOURCE_NLCD_TCC else "Open, under 15 ft",
              td.NO_DATA: "No value"}
    rows = [{"label": labels[n], "cells": [_one_decimal_or_dash(a, counts[n]), _one_decimal_or_dash(s, counts[n])]}
            for n, a, s in zip(names, acres, shares)]
    total = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    rows.append({"label": "Total", "cells": [_one_decimal(round(total, 1)), _one_decimal(100.0)]})
    return {"corner": "Canopy extent", "columns": ["Acres", "% of parcel"], "rows": rows, "compact": True,
            "acres": acres, "shares": shares}


def build_extent_caption(derived: td.TreesDerived) -> list:
    canopy = derived.canopy
    counts = canopy["counts"]
    parts = []
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG and counts[td.CANOPY] > 0:
        closure = derived.closure
        blocks = closure["classes"]
        parts += ["Within the ", {"value": "30 m"}, " blocks that hold canopy, ", {"value": _pct(closure["mean_pct"])},
                  " of the ground is canopy: " + CLOSURE_LABEL + ", not the fallback product's percent cover; ",
                  {"value": str(blocks["76-100"]["blocks"])}, f" of {closure['blocks']} blocks are over three-quarters closed. "]
    elif canopy["source"] == CANOPY_SOURCE_NLCD_TCC and counts[td.CANOPY] > 0:
        parts += ["Mean cover within the canopy pixels is ", {"value": _pct(derived.cover["mean_pct"])}, ", the product's own percent cover. "]
    if counts[td.NO_DATA]:
        parts += ["The ", {"value": str(counts[td.NO_DATA])}, " cells with no value are the edge of the resampling, not a gap in coverage."]
    return parts


def build_height_table(derived: td.TreesDerived) -> Optional[dict]:
    heights = derived.heights
    if heights is None:
        return None
    counts = heights["counts"]
    names = [name for name, _, _ in td.HEIGHT_CLASSES] + ([td.NO_DATA] if counts[td.NO_DATA] else [])
    acres, shares = _acre_values(derived, [counts[n] for n in names])
    labels = dict(td.HEIGHT_CLASS_LABELS, **{"under": "Under 15 ft, not canopy", td.NO_DATA: "No value"})
    rows = [{"label": labels[n], "cells": [_one_decimal_or_dash(a, counts[n]), _one_decimal_or_dash(s, counts[n])]}
            for n, a, s in zip(names, acres, shares)]
    total = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    rows.append({"label": "Total", "cells": [_one_decimal(round(total, 1)), _one_decimal(100.0)]})
    return {"corner": "Canopy height", "columns": ["Acres", "% of parcel"], "rows": rows, "compact": True, "acres": acres, "shares": shares}


def build_height_caption(derived: td.TreesDerived) -> list:
    heights = derived.heights
    if heights is None or derived.canopy["counts"][td.CANOPY] == 0:
        return ["Lidar height above ground classed at the design's ", {"value": "15 ft"}, " threshold and at ", {"value": "30"}, " and ",
                {"value": "50 ft"}, "."]
    return ["Classed at the design's ", {"value": "15 ft"}, " threshold and at ", {"value": "30"}, " and ", {"value": "50 ft"},
            ", the height of a cell's tallest return; the tallest cell is ", {"value": f"{_ft(heights['max_m'])} ft"}, ", the canopy's median ",
            {"value": f"{_ft(heights['canopy_median_m'])} ft"}, "."]


def build_height_unavailable(derived: td.TreesDerived) -> list:
    return ["No canopy height is reported: this parcel's canopy is NLCD Tree Canopy Cover ", {"value": str(derived.canopy["year"])},
            ", percent cover per ", {"value": "30 m"}, " pixel; no lidar height product covers it, so height classes are not available."]


def build_cover_table(derived: td.TreesDerived) -> Optional[dict]:
    cover = derived.cover
    if cover is None:
        return None
    names = [name for name, _, _ in td.DENSITY_CLASSES]
    counts = [cover["classes"][n] for n in names]
    total_canopy = sum(counts)
    if total_canopy == 0:
        return None
    acres = allocate_exactly(counts, total_canopy * derived.cells["cell_acres"], 1)
    shares = allocate_exactly(counts, 100.0, 1)
    labels = {"1-25": "1–25% cover", "26-50": "26–50% cover", "51-75": "51–75% cover", "76-100": "76–100% cover"}
    rows = [{"label": labels[n], "cells": [_one_decimal_or_dash(a, c), _one_decimal_or_dash(s, c)]} for n, a, s, c in zip(names, acres, shares, counts)]
    rows.append({"label": "All canopy", "cells": [_one_decimal(round(total_canopy * derived.cells["cell_acres"], 1)), _one_decimal(100.0)]})
    return {"corner": "Canopy cover", "columns": ["Acres", "% of canopy"], "rows": rows, "compact": True, "acres": acres, "shares": shares}


def build_cover_caption(derived: td.TreesDerived) -> list:
    return ["The product's own percent cover within the canopy pixels, not comparable with a lidar parcel's closure."]


def build_forest_type(derived: td.TreesDerived) -> Optional[list]:
    forest = derived.forest_type
    if not forest["fetched"]:
        return None
    total = derived.cells["on_parcel_count"]
    if forest["forest_cells"] == 0:
        return ["The forest type group model calls none of the parcel forest: every ", {"value": "30 m"}, " pixel is non-forest."]
    acres = allocate_exactly([forest["forest_cells"], total - forest["forest_cells"]], total * derived.cells["cell_acres"], 1)
    shares = allocate_exactly([forest["forest_cells"], total - forest["forest_cells"]], 100.0, 1)
    if forest["single"] is not None:
        return ["The forest type model calls ", {"value": _acres_word(acres[0])}, " of the parcel forest, ", {"value": _pct(shares[0])},
                ", all ", {"value": group_name(forest["single"])}, "."]
    parts = ["The forest type model calls ", {"value": _acres_word(acres[0])}, " of the parcel forest, ", {"value": _pct(shares[0])}, ": "]
    groups = [code for code in forest["groups"] if code != ftd.NON_FOREST]
    g_acres = allocate_exactly([forest["counts"][c] for c in groups], acres[0], 1)
    pieces = []
    for code, a in zip(groups, g_acres):
        pieces.append([{"value": group_name(code)}, " on ", {"value": _acres_word(a)}])
    for index, piece in enumerate(pieces):
        if index:
            parts.append(", " if index < len(pieces) - 1 else " and ")
        parts += piece
    parts.append(".")
    return parts


def build_forest_type_caption(derived: td.TreesDerived) -> list:
    forest = derived.forest_type
    canopy = derived.canopy
    parts = ["FIA BIGMAP, plots of ", {"value": forest.get("vintage") or ""}, ", " + FOREST_TYPE_CAVEAT + ". "]
    if forest["forest_cells"] != canopy["counts"][td.CANOPY] and canopy["counts"][td.CANOPY] > 0:
        total = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
        c_acres = allocate_exactly([canopy["counts"][td.CANOPY], derived.cells["on_parcel_count"] - canopy["counts"][td.CANOPY]], total, 1)[0]
        f_acres = allocate_exactly([forest["forest_cells"], derived.cells["on_parcel_count"] - forest["forest_cells"]], total, 1)[0]
        source = "The lidar" if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG else "The cover product"
        what = "height returns" if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG else "percent cover"
        parts += [f"{source} sees ", {"value": _acres_word(c_acres)}, " of canopy where the model calls ", {"value": _acres_word(f_acres)},
                  f" forest: {what} at 5 m against a classification of stands at 30 m."]
    return parts


def build_forest_type_unavailable(inputs: td.TreesInputs) -> list:
    reason = inputs.unavailable.get("forest_type_group", {}).get("reason")
    if reason == "no_data_for_parcel":
        return ["The forest type group model carries no pixel for this parcel."]
    return ["The forest type group service did not answer when this report was generated; the modelled forest type is not reported."]


def select_species(derived: td.TreesDerived) -> list:
    on_parcel = derived.cells["on_parcel_count"]
    rows = [s for s in derived.productivity["species"] if on_parcel and s["cells"] / on_parcel >= SPECIES_MIN_SHARE]
    return rows[:SPECIES_ROWS_MAX]


def build_species_table(derived: td.TreesDerived) -> Optional[dict]:
    prod = derived.productivity
    if not prod["fetched"] or not prod["species"]:
        return None
    cell_acres = derived.cells["cell_acres"]
    rows = []
    for species in select_species(derived):
        acres = species["cells"] * cell_acres
        si = species["site_index_mean"]
        low, high = species["site_index_min"], species["site_index_max"]
        si_text = f"{si:.0f}" if low == high else f"{si:.0f} ({low:.0f}–{high:.0f})"
        volume = f"{species['volume_mean']:.0f}" if species["volume_mean"] is not None else ZERO_DASH
        rows.append({"label": species["common"] or species["scientific"] or species["symbol"],
                     "cells": [_one_decimal_or_dash(acres, 1), si_text, volume]})
    return {"corner": "Site index by species", "columns": ["Acres rated", "Site index, ft", "Growth, cu ft/ac/yr"], "rows": rows,
            "compact": True, "species": [s["symbol"] for s in select_species(derived)]}


def build_species_caption(derived: td.TreesDerived) -> list:
    prod = derived.productivity
    shown = len(select_species(derived))
    parts = [f"The survey's list for these map units, the {shown} species rated on the most ground of {len(prod['species'])}, not a "
             "recommendation; site index is height in feet at the base age of the survey's curve. "]
    unrated = [(mukey, unit) for mukey, unit in prod["units"].items() if unit["unrated_major"]]
    for mukey, unit in unrated:
        from water_section import map_unit_short_name

        names = _list([u["compname"] for u in unit["unrated_major"]])
        pct = unit["unrated_major"][0]["comppct"]
        acres = unit["cells"] * derived.cells["cell_acres"]
        rated = _list([u["compname"] for u in unit["rated_major"]]) or "no major component"
        parts += [f"{names}, ", {"value": f"{pct:.0f}%"}, f" of the {map_unit_short_name(unit['muname'])} unit (",
                  {"value": _acres_word(acres)}, f"), carries no rows, so that unit rests on {rated}. "]
    return parts


def build_species_unavailable(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    if derived.productivity["fetched"]:
        return ["The survey rates no species for this parcel's map units."]
    return ["SSURGO's woodland ratings did not answer when this report was generated; the productivity and limitation tables are "
            "not reported. Look the map units up in Web Soil Survey, Forestland Productivity."]


def named_features(features: dict, class_cells: int, share: float = FEATURE_NAME_SHARE, limit: int = FEATURE_NAME_MAX) -> list:
    if class_cells <= 0:
        return []
    picked = [name for name, cells in features.items() if cells / class_cells >= share]
    return picked[:limit]


def build_limitations_table(derived: td.TreesDerived) -> Optional[dict]:
    lim = derived.limitations
    if not lim["fetched"]:
        return None
    cell_acres = derived.cells["cell_acres"]
    total = derived.cells["on_parcel_count"] * cell_acres
    rows, groups = [], []
    for name in sw.INTERPRETATIONS:
        block = lim["interpretations"][name]
        counts = block["counts"]
        present = [cls for cls, n in counts.items() if n]
        acres = allocate_exactly([counts[c] for c in present], total, 1)
        shares = allocate_exactly([counts[c] for c in present], 100.0, 1)
        classes = sw.INTERPRETATION_CLASSES[name]
        groups.append({"interpretation": name, "classes": present, "acres": acres, "shares": shares})
        for cls, a, s in zip(present, acres, shares):
            if cls in classes and cls != classes[0]:
                features = named_features(block["features"][cls], counts[cls])
                text = _list([_feature_name(f) for f in features]) if features else ZERO_DASH
            elif cls in classes:
                text = ZERO_DASH
            else:
                text = {sw.NOT_RATED: "not rated by the survey", td.SOIL_NO_DATA: "no rating returned",
                        wd.HYDRIC_NO_POLYGON: "no survey polygon"}.get(cls, cls)
            label = f"{sw.INTERPRETATION_LABELS[name]}, {_lower(cls)}" if cls in classes else \
                f"{sw.INTERPRETATION_LABELS[name]}, " + {wd.HYDRIC_NO_POLYGON: "not surveyed", td.SOIL_NO_DATA: "no data"}.get(cls, _lower(cls))
            rows.append({"label": label, "cells": [_one_decimal_or_dash(a, counts[cls]), _one_decimal_or_dash(s, counts[cls]),
                                                   {"value": text, "kind": "text"}]})
    return {"corner": "Woodland limitation", "columns": ["Acres", "% of parcel", "Limiting features"], "rows": rows,
            "text_columns": ["Limiting features"], "groups": groups}


def _feature_name(name: str) -> str:
    return {"Surface kw times slope times R index": "erodibility, slope and rainfall"}.get(name, _lower(name))


def build_limitations_caption(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    lim = derived.limitations
    parts = ["NRCS's rating by dominant components, each interpretation partitioning the parcel: the soil's capacity, the same under a "
             "field as under a stand. "]
    wind = lim["interpretations"][sw.WINDTHROW]
    counts = wind["counts"]
    limited = [c for c in ("Moderate", "Severe") if counts[c]]
    if limited:
        cell_acres = derived.cells["cell_acres"]
        total = derived.cells["on_parcel_count"] * cell_acres
        present = [c for c, n in counts.items() if n]
        acres = dict(zip(present, allocate_exactly([counts[c] for c in present], total, 1)))
        pieces = []
        for cls in limited:
            pieces += [f"{_lower(cls)} on ", {"value": _acres_word(acres[cls])}]
            if cls != limited[-1]:
                pieces.append(" and ")
        parts += ["Windthrow hazard is "] + pieces
        water_table = sum(wind["features"][c].get("Water table depth", 0.0) for c in limited)
        if water_table > 0:
            wt_acres = allocate_exactly([water_table, derived.cells["on_parcel_count"] - water_table], total, 1)[0]
            parts += [", water table depth the feature on ", {"value": _acres_word(wt_acres)},
                      " of them, the shallow seasonal water table the Water section maps"]
        winter = ((inputs.wind or {}).get("seasons") or {}).get("winter") or {}
        word = compass_word(winter.get("prevailing_sector"))
        if word:
            parts += [f"; Climate's winter wind prevails from the {word}."]
        else:
            parts += ["."]
    return parts


# ======================================================================
# Sources and methods
# ======================================================================


def _survey_line(derived: td.TreesDerived) -> str:
    surveys = derived.productivity["survey_areas"] or derived.limitations["survey_areas"]
    interps = "the four woodland interpretations tabled"
    if surveys:
        version = (surveys[0].get("saverest") or "").split(" ")[0]
        return f"USDA NRCS SSURGO, soil survey {surveys[0]['areasymbol']}, version of {version}: coforprod; cointerp, {interps}."
    return f"USDA NRCS SSURGO: coforprod; cointerp, {interps}."


def canopy_source_line(inputs: td.TreesInputs, derived: td.TreesDerived) -> str:
    canopy = derived.canopy
    retrieved = format_retrieved_on(inputs.retrieved_on)
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        year = hag_acquisition_year(canopy["source_item_id"])
        when = f", {year}" if year else ", acquisition year not stated"
        return (f"USGS 3DEP lidar height above ground, {canopy['source_item_id']}{when}, {HAG_NATIVE_M:.0f} m resampled to 5 m, "
                f"via Microsoft Planetary Computer, retrieved {retrieved}.")
    version = canopy.get("product_version") or ""
    return f"USDA Forest Service NLCD Tree Canopy Cover {version}, {canopy['year']}, 30 m, via the IIPP image service, retrieved {retrieved}."


def build_sources(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    lines = [[canopy_source_line(inputs, derived)]]
    if inputs.forest_type_group is not None:
        lines.append([f"USDA Forest Service FIA BIGMAP forest type group 2018, plots {derived.forest_type['vintage']}, 30 m."])
    if inputs.soil_woodland is not None:
        lines.append([_survey_line(derived)])
    lines.append([f"USGS 3DEP elevation, 1/3 arc-second, resampled to 5 m, retrieved {retrieved}."])
    return lines


def build_methods(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    canopy = derived.canopy
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        canopy_method = {
            "source": "USGS 3DEP lidar height above ground",
            "identifier": f"Planetary Computer collection 3dep-lidar-hag, item {canopy['source_item_id']}",
            "period": retrieved,
            "citation": "U.S. Geological Survey 3D Elevation Program lidar point clouds, height above ground derived by Microsoft Planetary "
                        "Computer (PDAL smrf ground classification and hag_nn), 2 m.",
            "terms": "USGS 3DEP data are U.S. federal works in the public domain; the derived collection is served under Planetary "
                     "Computer's terms.",
            "method": "First-return height above bare earth, warped bilinearly onto the session's 5 m DEM grid (the canopy dict the design "
                      f"consumed, read off the session). A cell is canopy at or above {td.CANOPY_HEIGHT_THRESHOLD_METERS} m (15 ft), the "
                      "design's own threshold, so the section's canopy is the design's. Height classes break at that threshold and at "
                      "30 and 50 ft. Closure: the grid cut into 30 m blocks from its origin; in each block holding a canopy cell, canopy "
                      "cells over valid on-parcel cells, the figure the cell-weighted mean. A roof is a first return and counts as canopy.",
        }
    else:
        canopy_method = {
            "source": "USDA Forest Service NLCD Tree Canopy Cover",
            "identifier": f"IIPP USFS_EDW_NLCD_TCC_CONUS, {canopy.get('product_version')}, year {canopy['year']}",
            "period": retrieved,
            "citation": "USDA Forest Service, NLCD Tree Canopy Cover, conterminous United States, 30 m, produced by RedCastle Resources "
                        "under contract to the Forest Service Field Services and Innovation Center Geospatial Office.",
            "terms": "U.S. federal work; public domain.",
            "method": "Percent cover per 30 m pixel, exported nearest-neighbour onto the session's 5 m DEM grid with the year pinned "
                      "by a mosaic rule; any nonzero pixel is canopy, the design's own rule. Heights are not measured by this product.",
        }
    methods = [canopy_method]
    if inputs.forest_type_group is not None:
        methods.append({
            "source": "USDA Forest Service FIA BIGMAP forest type group",
            "identifier": "IIPP USFS_FIA_BIGMAP_CONUS_ForestTypeGroup_2018, exportImage on the DEM window",
            "period": retrieved,
            "citation": ftd.FOREST_TYPE_CITATION,
            "terms": ftd.FOREST_TYPE_TERMS,
            "method": "Forest type group codes sampled nearest-neighbour onto the 5 m grid and counted over the parcel's cells; a share is "
                      "a share of 30 m pixels. Non-forest is a class. The model imputes inventory plots to pixels and says what type of "
                      "forest occupies an area, not what stands on any acre; it is reported beside the canopy, never reconciled with it.",
        })
    if inputs.soil_woodland is not None:
        bases = sorted({b for s in derived.productivity["species"] for b in s["bases"]})
        methods.append({
            "source": "USDA NRCS SSURGO",
            "identifier": "coforprod and cointerp joined to component over the parcel's map units; " + ", ".join(sw.INTERPRETATIONS),
            "period": retrieved,
            "citation": sw.SSURGO_CITATION,
            "terms": "U.S. federal work; public domain.",
            "method": "Productivity: a species is rated for a map unit when a major component carries a representative site index for it; "
                      "the unit's ground is shared among its major components by their percentages, species keyed by plant symbol, the "
                      "site index acre-weighted and ranged across units, volume growth the culmination of mean annual increment in cubic "
                      "feet per acre per year. A major component with no rows takes no part and is named in the caption. Limitations: "
                      "NRCS's dominant condition per map unit, a tie to the more limiting class; features are the depth-1 rules of the "
                      "winning class's components weighted by their share and the unit's cells; the table names those affecting at "
                      f"least {FEATURE_NAME_SHARE:.0%} of the class's ground. Acres by the same map-unit cell grid the Water section uses, "
                      "allocated exactly to the parcel's area.",
            "notes": ["Site index base curves: " + "; ".join(bases) + "." if bases else "No species rated."],
        })
    methods.append({
        "source": "USGS 3DEP", "identifier": f"3DEP 1/3 arc-second DEM, resampled to {max(inputs.dem['resolution_meters']):.0f} m, {inputs.dem['crs']}",
        "period": retrieved, "citation": "U.S. Geological Survey, 3D Elevation Program seamless DEM, served by The National Map elevation service.",
        "terms": "U.S. federal work; public domain.", "method": "Contours at Landform's interval.",
    })
    return methods


# ======================================================================
# The section
# ======================================================================


def build_trees_section(inputs: td.TreesInputs, tokens: Optional[dict] = None) -> dict:
    """The section dict trees.html sets. `tokens` defaults to site_report.TOKENS."""
    if tokens is None:
        import site_report

        tokens = site_report.TOKENS
    derived = td.derive(inputs)
    parcel = inputs.boundary_polygon_utm
    contours = report_map.parcel_contours(inputs.dem, parcel)
    rendered = report_map.render_map(parcel, build_map_layers(inputs, derived, contours), tokens, note=empty_map_note(derived, parcel))
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(inputs, derived),
        "map": rendered,
        "map_caption": build_map_caption(inputs, derived),
        "extent_table": build_extent_table(derived),
        "extent_caption": build_extent_caption(derived),
        "height_table": build_height_table(derived),
        "height_caption": build_height_caption(derived),
        "height_unavailable": build_height_unavailable(derived),
        "cover_table": build_cover_table(derived),
        "cover_caption": build_cover_caption(derived),
        "forest_type": build_forest_type(derived),
        "forest_type_caption": build_forest_type_caption(derived),
        "forest_type_unavailable": build_forest_type_unavailable(inputs),
        "species_table": build_species_table(derived),
        "species_caption": build_species_caption(derived),
        "species_unavailable": build_species_unavailable(inputs, derived),
        "limitations_table": build_limitations_table(derived),
        "limitations_caption": build_limitations_caption(inputs, derived),
        "sources": build_sources(inputs, derived),
        "methods": build_methods(inputs, derived),
        "derived": derived,
        "contours": {k: v for k, v in contours.items() if k != "levels"},
    }
