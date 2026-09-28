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

import canopy_cover_data
import forest_type_data as ftd
import report_map
import report_text as rt
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

# The canopy's figures, from the constants that make them.
CANOPY_HEIGHT = rt.feet(td.CANOPY_HEIGHT_THRESHOLD_METERS)
HEIGHT_BREAKS = [rt.feet(low) for _, low, _ in td.HEIGHT_CLASSES[2:]]
TCC_PIXEL = rt.meters(canopy_cover_data.TCC_NATIVE_RESOLUTION_METERS)
# How closed the canopy is where there are trees, in words, by the closure
# classes the extent and cover tables use: the class the mean falls in.
CLOSURE_WORDS = {"1-25": "sparse, a few trees", "26-50": "scattered trees rather than closed woodland",
                 "51-75": "fairly dense, with gaps", "76-100": "closed woodland"}
# The closure class a block must reach to count as nearly solid canopy.
SOLID_CLASS = td.DENSITY_CLASSES[-1][0]


# ======================================================================
# Formatting
# ======================================================================


def _closure_word(mean_pct: float) -> str:
    for name, low, high in td.DENSITY_CLASSES:
        if mean_pct <= high:
            return CLOSURE_WORDS[name]
    return CLOSURE_WORDS[td.DENSITY_CLASSES[-1][0]]


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
    return rt.lower_first(ftd.FOREST_TYPE_GROUPS[code]).replace(" / ", "/")


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


WINDTHROW_GLOSS = "windthrow hazard — how likely trees are to blow over —"


def _canopy_sentence(derived: td.TreesDerived) -> list:
    canopy = derived.canopy
    counts = canopy["counts"]
    acres, shares = _acre_values(derived, [counts[td.CANOPY], counts[td.OPEN], counts[td.NO_DATA]])
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        year = hag_acquisition_year(canopy["source_item_id"])
        flown = rt.clause(" flown in ", rt.year(year)) if year else [" from the air, its year not stated by the source record"]
        lead = rt.clause("Lidar, a laser survey", flown, ", finds trees ", CANOPY_HEIGHT, " or taller")
        if counts[td.CANOPY] == 0:
            return rt.clause(lead, " nowhere on the parcel.")
        return rt.clause(lead, " on ", rt.acres(acres[0]), ", ", rt.percent(shares[0]), " of the parcel; the tallest reach ",
                         rt.feet(derived.heights["max_m"]), ".")
    lead = rt.clause("NLCD Tree Canopy Cover, a national satellite map of ", rt.year(canopy["year"]), ", puts trees on ",
                     rt.acres(acres[0]), ", ", rt.percent(shares[0]), " of the parcel")
    return rt.clause(lead, "; no height survey covers this parcel.")


def _forest_sentence(derived: td.TreesDerived) -> list:
    forest = derived.forest_type
    if not forest["fetched"]:
        return None
    if forest["forest_cells"] == 0:
        return ["A national forest map calls none of the parcel woodland."]
    f_acres = allocate_exactly([forest["forest_cells"], derived.cells["on_parcel_count"] - forest["forest_cells"]],
                               derived.cells["on_parcel_count"] * derived.cells["cell_acres"], 1)
    names = rt.series_text([group_name(code) for code in forest["groups"] if code != ftd.NON_FOREST])
    return rt.clause("A national forest map calls ", rt.acres(f_acres[0]), f" of it woodland, {names}.")


def _windthrow_sentence(derived: td.TreesDerived) -> list:
    lim = derived.limitations
    if not lim["fetched"]:
        return None
    wind = lim["interpretations"][sw.WINDTHROW]["counts"]
    w_acres, _ = _acre_values(derived, list(wind.values()))
    moderate = w_acres[list(wind).index("Moderate")]
    severe = w_acres[list(wind).index("Severe")]
    if not (wind["Moderate"] or wind["Severe"]):
        return [f"The soil survey rates {WINDTHROW_GLOSS} slight on the whole parcel."]
    return rt.clause(f"The soil survey rates {WINDTHROW_GLOSS} moderate on ", rt.acres(moderate),
                     rt.clause(" and severe on ", rt.acres(severe)) if wind["Severe"] else None, ".")


def build_summary(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    return rt.sentences(_canopy_sentence(derived), _forest_sentence(derived), _windthrow_sentence(derived))


def build_map_caption(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    """The one caveat that changes how the map is read: what counts as a
    tree. Resolution and resampling are in the methods note."""
    if derived.canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        return rt.sentences(rt.clause("Shaded wherever the lidar finds something ", CANOPY_HEIGHT,
                                      " or taller, darker as it gets taller. A roof or a barn counts too, so check buildings "
                                      "against the aerial photograph."),
                            ["Contours as on the Landform map."])
    return rt.sentences(rt.clause("Shaded wherever the satellite map finds any tree cover. It works in squares about ", TCC_PIXEL,
                                  " across, so edges are blocky and a thin hedgerow may be missed."),
                        ["There is no height survey here, so all trees share one shade. Contours as on the Landform map."])


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


def _closure_clause(derived: td.TreesDerived) -> list:
    canopy = derived.canopy
    if canopy["counts"][td.CANOPY] == 0:
        return None
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        closure = derived.closure
        solid = closure["classes"][SOLID_CLASS]["blocks"]
        return rt.clause("Where there are trees, they cover ", rt.percent(closure["mean_pct"]), " of the ground — ",
                         _closure_word(closure["mean_pct"]), " — and ", rt.number(solid), " of the ",
                         rt.count(closure["blocks"], "wooded patch", "wooded patches"), " are nearly solid canopy.")
    mean = derived.cover["mean_pct"]
    return rt.clause("Where there are trees, the satellite map puts their cover at ", rt.percent(mean), " — ", _closure_word(mean), ".")


def _no_value_clause(derived: td.TreesDerived) -> list:
    if not derived.canopy["counts"][td.NO_DATA]:
        return None
    return ["The row with no value is a thin strip along the boundary where the survey stops, not a gap in the trees."]


def build_extent_caption(derived: td.TreesDerived) -> list:
    return rt.sentences(_closure_clause(derived), _no_value_clause(derived))


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
    lead = rt.clause("Trees by height, from ", CANOPY_HEIGHT, " up")
    if heights is None or derived.canopy["counts"][td.CANOPY] == 0:
        return rt.clause(lead, ".")
    return rt.clause(lead, "; half the canopy stands taller than ", rt.feet(heights["canopy_median_m"]),
                     ", and the tallest reaches ", rt.feet(heights["max_m"]), ".")


def build_height_unavailable(derived: td.TreesDerived) -> list:
    return rt.clause("No tree heights here: the only tree map that covers this parcel is NLCD Tree Canopy Cover of ",
                     rt.year(derived.canopy["year"]), ", a satellite estimate of cover in squares about ", TCC_PIXEL,
                     " across, which does not measure height.")


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
    return ["How thick the tree cover is, by the satellite map's own measure; it is not comparable with the cover a lidar "
            "survey gives on other parcels."]


def build_forest_type(derived: td.TreesDerived) -> Optional[list]:
    forest = derived.forest_type
    if not forest["fetched"]:
        return None
    total = derived.cells["on_parcel_count"]
    if forest["forest_cells"] == 0:
        return ["A national forest map, FIA BIGMAP, calls none of the parcel woodland."]
    acres = allocate_exactly([forest["forest_cells"], total - forest["forest_cells"]], total * derived.cells["cell_acres"], 1)
    shares = allocate_exactly([forest["forest_cells"], total - forest["forest_cells"]], 100.0, 1)
    lead = rt.clause("A national forest map, FIA BIGMAP, calls ", rt.acres(acres[0]), " of the parcel woodland, ", rt.percent(shares[0]))
    if forest["single"] is not None:
        return rt.clause(lead, ", all ", rt.text(group_name(forest["single"])), ".")
    groups = [code for code in forest["groups"] if code != ftd.NON_FOREST]
    g_acres = allocate_exactly([forest["counts"][c] for c in groups], acres[0], 1)
    return rt.clause(lead, ": ", rt.series([[rt.text(group_name(code)), " on ", rt.acres(a)] for code, a in zip(groups, g_acres)]), ".")


def _canopy_against_forest(derived: td.TreesDerived) -> list:
    forest = derived.forest_type
    canopy = derived.canopy
    if forest["forest_cells"] == canopy["counts"][td.CANOPY] or canopy["counts"][td.CANOPY] == 0:
        return None
    total = derived.cells["on_parcel_count"] * derived.cells["cell_acres"]
    c_acres = allocate_exactly([canopy["counts"][td.CANOPY], derived.cells["on_parcel_count"] - canopy["counts"][td.CANOPY]], total, 1)[0]
    f_acres = allocate_exactly([forest["forest_cells"], derived.cells["on_parcel_count"] - forest["forest_cells"]], total, 1)[0]
    source = "the lidar" if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG else "the satellite map"
    if forest["forest_cells"] < canopy["counts"][td.CANOPY]:
        why = "trees in groups too small for it to call a stand"
    else:
        why = "it maps whole stands, gaps included"
    return rt.clause("It counts ", rt.acres(f_acres), f" as woodland where {source} finds ", rt.acres(c_acres), f" of trees: {why}.")


def build_forest_type_caption(derived: td.TreesDerived) -> list:
    vintage = derived.forest_type.get("vintage")
    plots = rt.clause(" of ", rt.text(vintage)) if vintage else []
    return rt.sentences(rt.clause("The map spreads the Forest Service's field plots", plots, " across the country, so it names the kind "
                                  "of forest in the area, not what stands on any one acre."),
                        _canopy_against_forest(derived))


def build_forest_type_unavailable(inputs: td.TreesInputs) -> list:
    reason = inputs.unavailable.get("forest_type_group", {}).get("reason")
    if reason == "no_data_for_parcel":
        return ["The national forest map has nothing for this parcel."]
    return ["The national forest map did not answer when this report was generated, so the kind of forest is not reported."]


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


def _species_list_clause(derived: td.TreesDerived) -> list:
    return rt.clause("The soil survey's ", rt.number(len(select_species(derived))), " trees rated on the most ground, of the ",
                     rt.number(len(derived.productivity["species"])), " it lists for these soils: the survey's ratings, not a "
                     "recommendation. Site index is the height in feet a tree reaches by a set age on this soil.")


def _unrated_clause(derived: td.TreesDerived, unit: dict) -> list:
    from water_section import map_unit_short_name

    names = rt.series_text([u["compname"] for u in unit["unrated_major"]])
    rated = rt.series_text([u["compname"] for u in unit["rated_major"]]) or "no other major soil"
    return rt.clause(f"The survey rates no trees for {names}, ", rt.percent(unit["unrated_major"][0]["comppct"]),
                     f" of the {map_unit_short_name(unit['muname'])} soil (", rt.acres(unit["cells"] * derived.cells["cell_acres"]),
                     f"), so those acres are rated on {rated} alone.")


def build_species_caption(derived: td.TreesDerived) -> list:
    units = derived.productivity["units"].values()
    return rt.sentences(_species_list_clause(derived), *[_unrated_clause(derived, u) for u in units if u["unrated_major"]])


def build_species_unavailable(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    if derived.productivity["fetched"]:
        return ["The soil survey rates no trees for this parcel's soils."]
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
                text = rt.series_text([_feature_name(f) for f in features]) if features else ZERO_DASH
            elif cls in classes:
                text = ZERO_DASH
            else:
                text = {sw.NOT_RATED: "not rated by the survey", td.SOIL_NO_DATA: "no rating returned",
                        wd.HYDRIC_NO_POLYGON: "no survey polygon"}.get(cls, cls)
            label = f"{sw.INTERPRETATION_LABELS[name]}, {rt.lower_first(cls)}" if cls in classes else \
                f"{sw.INTERPRETATION_LABELS[name]}, " + {wd.HYDRIC_NO_POLYGON: "not surveyed", td.SOIL_NO_DATA: "no data"}.get(cls, rt.lower_first(cls))
            rows.append({"label": label, "cells": [_one_decimal_or_dash(a, counts[cls]), _one_decimal_or_dash(s, counts[cls]),
                                                   {"value": text, "kind": "text"}]})
    return {"corner": "Woodland limitation", "columns": ["Acres", "% of parcel", "Limiting features"], "rows": rows,
            "text_columns": ["Limiting features"], "groups": groups}


def _feature_name(name: str) -> str:
    return {"Surface kw times slope times R index": "erodibility, slope and rainfall"}.get(name, rt.lower_first(name))


def _windthrow_clause(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    wind = derived.limitations["interpretations"][sw.WINDTHROW]
    counts = wind["counts"]
    limited = [c for c in ("Moderate", "Severe") if counts[c]]
    if not limited:
        return None
    cell_acres = derived.cells["cell_acres"]
    total = derived.cells["on_parcel_count"] * cell_acres
    present = [c for c, n in counts.items() if n]
    acres = dict(zip(present, allocate_exactly([counts[c] for c in present], total, 1)))
    parts = rt.clause(rt.upper_first(WINDTHROW_GLOSS), " is ", rt.series([[f"{rt.lower_first(c)} on ", rt.acres(acres[c])] for c in limited]))
    water_table = sum(wind["features"][c].get("Water table depth", 0.0) for c in limited)
    if water_table > 0:
        wt_acres = allocate_exactly([water_table, derived.cells["on_parcel_count"] - water_table], total, 1)[0]
        parts = rt.clause(parts, ", on ", rt.acres(wt_acres), " of it because the water table rises near the surface in wet "
                          "seasons, as the Water section maps")
    winter = ((inputs.wind or {}).get("seasons") or {}).get("winter") or {}
    word = compass_word(winter.get("prevailing_sector"))
    return rt.clause(parts, f"; winter wind here comes mostly from the {word}." if word else ".")


def build_limitations_caption(inputs: td.TreesInputs, derived: td.TreesDerived) -> list:
    return rt.sentences(["These ratings describe the soil, so they hold whether the ground is in trees or in field."],
                        _windthrow_clause(inputs, derived))


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
    grid_m = max(inputs.dem["resolution_meters"])
    no_data = canopy["counts"][td.NO_DATA]
    no_value_note = (f"{no_data:,} on-parcel {rt.plural(no_data, 'cell')} carry no value -- the edge of the resampling along the "
                     "boundary, not a gap in coverage -- the extent table's no-value row." if no_data else None)
    if canopy["source"] == CANOPY_SOURCE_LIDAR_HAG:
        canopy_method = {
            "source": "USGS 3DEP lidar height above ground",
            "identifier": f"Planetary Computer collection 3dep-lidar-hag, item {canopy['source_item_id']}",
            "period": retrieved,
            "citation": "U.S. Geological Survey 3D Elevation Program lidar point clouds, height above ground derived by Microsoft Planetary "
                        "Computer (PDAL smrf ground classification and hag_nn), 2 m.",
            "terms": "USGS 3DEP data are U.S. federal works in the public domain; Microsoft Planetary Computer's hosting terms for "
                     "the derived collection are not yet confirmed.",
            "method": f"First-return height above bare earth at {HAG_NATIVE_M:g} m, warped bilinearly onto the session's "
                      f"{grid_m:.0f} m DEM grid. A cell is canopy at or above {td.CANOPY_HEIGHT_THRESHOLD_METERS:g} m "
                      f"({CANOPY_HEIGHT['value']}), the design's threshold. Height classes break at that threshold and at "
                      f"{rt.series_text([b['value'] for b in HEIGHT_BREAKS])}, by each cell's tallest return. Closure: the grid cut "
                      f"into {td.CLOSURE_BLOCK_METERS:g} m blocks from its origin -- the wooded patches of the extent caption; in each "
                      "block holding a canopy cell, canopy cells over valid on-parcel cells, the figure the cell-weighted mean, the "
                      "summary's word for it the closure class the mean falls in; a patch is nearly solid in the top class. It is "
                      "derived from the height threshold, not NLCD's percent cover, and the two are not comparable. A roof is a first "
                      "return and counts as canopy.",
            "notes": [no_value_note] if no_value_note else [],
        }
    else:
        canopy_method = {
            "source": "USDA Forest Service NLCD Tree Canopy Cover",
            "identifier": f"IIPP USFS_EDW_NLCD_TCC_CONUS, {canopy.get('product_version')}, year {canopy['year']}",
            "period": retrieved,
            "citation": "USDA Forest Service, NLCD Tree Canopy Cover, conterminous United States, 30 m, produced by RedCastle Resources "
                        "under contract to the Forest Service Field Services and Innovation Center Geospatial Office.",
            "terms": "U.S. federal work; public domain.",
            "method": f"Percent cover per {canopy_cover_data.TCC_NATIVE_RESOLUTION_METERS:g} m pixel, exported nearest-neighbour onto "
                      f"the session's {grid_m:.0f} m DEM grid with the year pinned by a mosaic rule; any nonzero pixel is canopy, the "
                      "design's own rule, so edges are pixel steps and a strip narrower than a pixel may be missed. Heights are not "
                      "measured by this product.",
            "notes": [no_value_note] if no_value_note else [],
        }
    methods = [canopy_method]
    if inputs.forest_type_group is not None:
        methods.append({
            "source": "USDA Forest Service FIA BIGMAP forest type group",
            "identifier": "IIPP USFS_FIA_BIGMAP_CONUS_ForestTypeGroup_2018, exportImage on the DEM window",
            "period": retrieved,
            "citation": ftd.FOREST_TYPE_CITATION,
            "terms": ftd.FOREST_TYPE_TERMS,
            "method": f"Forest type group codes sampled nearest-neighbour onto the {grid_m:.0f} m grid and counted over the parcel's cells; "
                      f"a share is a share of {ftd.FOREST_TYPE_NATIVE_RESOLUTION_METERS:g} m pixels. Non-forest is a class. The model imputes inventory plots to pixels and says what type of "
                      "forest occupies an area, not what stands on any acre; it is reported beside the canopy, never reconciled with it. "
                      f"Where the two disagree, the canopy is height returns on the {grid_m:.0f} m grid and the model a classification "
                      f"of stands in {ftd.FOREST_TYPE_NATIVE_RESOLUTION_METERS:g} m pixels.",
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
    # COMPUTED AGAIN HERE, KNOWINGLY -- NOT A BUG, AND NOT FREE TO REMOVE.
    # parcel_contours() runs once per section map: Site overview, Landform,
    # Water, Access, Trees, Soils and Design, seven times per report over the
    # same DEM and interval. About 0.06 s a time and no network
    # (diagnose_report_generation_time.py), so the repeat costs under half a
    # second and cannot fail on a flaky source. Sharing one result means
    # threading it through every section's builder; worth doing only if the
    # report's compute ever matters beside its fetches.
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
