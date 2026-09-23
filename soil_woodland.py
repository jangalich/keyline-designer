"""
soil_woodland.py

SSURGO'S WOODLAND PRODUCTIVITY AND MANAGEMENT LIMITATIONS for the site
data report's Trees & forestry section: the site index and volume growth
the survey rates by species for each soil component, and how limited the
soil is for harvest equipment, for seedling survival, for windthrow and
for erosion off-road -- the soil's CAPACITY for woodland, whether or not
any woodland is standing on it.

    get_woodland_for_polygon(wkt)        -> the raw rows (a fetch, two queries)
    parse_woodland(raw)                  -> the parsed block
    condense_species(block, ...)         -> the species table's rows

THE TABLES AND COLUMNS, verified live against Soil Data Access for the
reference parcel (branch 11, step 0, 2026-09-23; survey area PA003,
version of 5 September 2025):

    coforprod   one row per component per species the survey rates:
                plantsym, plantsciname, plantcomname, siteindexbase (the
                curve the index is read from -- Schnur 1937, Beck 1962,
                Carmean 1971 ...), siteindex_l/_r/_h (feet at the curve's
                base age), fprod_l/_r/_h (cubic feet per acre per year,
                the culmination of mean annual increment). 132 rows over
                the reference parcel's 30 components, 0 to 9 species each.
    coforprodo  the same in alternate units (board feet); two rows on
                the reference parcel. Not carried.
    cointerp    the FOR interpretations, the same shape soil_road_ratings
                reads for the ENG ones: depth 0 the class, depth 1 the
                features. Seventeen FOR rules are served for this survey;
                four are carried (INTERPRETATIONS below), every one rated
                at depth 0 on all 30 components.
    component, mapunit, legend, sacatalog   as soil_road_ratings.

FOUR INTERPRETATIONS, the classic woodland management limitations the
published surveys tabled, less plant competition (not served here):

    FOR - Harvest Equipment Operability     Well / Moderately / Poorly suited
    FOR - Potential Seedling Mortality      Low / Moderate / High
    FOR - Windthrow Hazard                  Slight / Moderate / Severe
    FOR - Potential Erosion Hazard (Off-Road/Off-Trail)
                                            Slight / Moderate / Severe / Very severe

Each is aggregated to a map unit by NRCS's dominant condition
(soil_road_ratings.dominant_condition: component percentages summed by
class, the largest total winning, a tie to the more limiting class) and
its features weighted by the class's components' shares
(soil_road_ratings.class_features). A "Not rated" component's feature
rows are reasons, not limitations.

THE SPECIES ARE CONDENSED, NOT LISTED. 132 rows are not a table. A
species is rated for a map unit when a MAJOR component (majcompflag)
carries a representative site index for it; the unit's ground is shared
among its major components by their percentages, so a species rated by a
30% major component gets 30/90 of a unit whose major components sum to
90%. Species are keyed by plant symbol, because the survey names one
species two ways (LITU is yellow-poplar on one component and tuliptree on
another). The site index is reported as the acre-weighted mean and the
range over the rating components, with the base curve named beside it:
an index is only comparable to another read from the same curve. A major
component with NO productivity rows takes no part and is reported
(`unrated_major`), so a unit whose 55% dominant component carries none
is not silently rated by its 35% companion.

THE PARCEL IS RATED WHOLE. The soil's capacity is the same under a field
as under a stand; the section never reads a canopy mask into this block.

ONE FETCH, TWO QUERIES, REPORT-TIME. Soil Data Access returns one result
set per request, and the productivity rows and the interpretation rows
have different shapes, so the fetch is two POSTs to the same service on
the same map-unit intersection Layer 1's soil queries use. A report-layer
fetch (report_data.py, DEGRADABLE), not a Layer 1 change: nothing a
design step consumes reads it.
"""

from typing import Optional

from soil_data import _run_sda_query, coordinates_to_wkt_polygon
from soil_road_ratings import NOT_RATED, _float, _text, class_features, dominant_condition

HARVEST_EQUIPMENT = "FOR - Harvest Equipment Operability"
SEEDLING_MORTALITY = "FOR - Potential Seedling Mortality"
WINDTHROW = "FOR - Windthrow Hazard"
EROSION_OFF_ROAD = "FOR - Potential Erosion Hazard (Off-Road/Off-Trail)"
INTERPRETATIONS = (HARVEST_EQUIPMENT, SEEDLING_MORTALITY, WINDTHROW, EROSION_OFF_ROAD)

# Each interpretation's classes, least to most limiting; the tie-break in
# dominant_condition() reads the order backwards.
INTERPRETATION_CLASSES = {
    HARVEST_EQUIPMENT: ("Well suited", "Moderately suited", "Poorly suited"),
    SEEDLING_MORTALITY: ("Low", "Moderate", "High"),
    WINDTHROW: ("Slight", "Moderate", "Severe"),
    EROSION_OFF_ROAD: ("Slight", "Moderate", "Severe", "Very severe"),
}
# The short names the section prints.
INTERPRETATION_LABELS = {
    HARVEST_EQUIPMENT: "Harvest equipment operability",
    SEEDLING_MORTALITY: "Seedling mortality",
    WINDTHROW: "Windthrow hazard",
    EROSION_OFF_ROAD: "Erosion hazard off-road",
}

SSURGO_CITATION = (
    "USDA Natural Resources Conservation Service, Soil Survey Geographic Database (SSURGO), tables coforprod, "
    "cointerp, component, mapunit, legend and sacatalog, served by Soil Data Access."
)


def productivity_sql(wkt_polygon: str) -> str:
    return f"""
        SELECT mu.mukey, mu.muname, l.areasymbol, sc.saverest,
               c.cokey, c.compname, c.comppct_r, c.majcompflag,
               cf.plantsym, cf.plantsciname, cf.plantcomname, cf.siteindexbase,
               cf.siteindex_l, cf.siteindex_r, cf.siteindex_h, cf.fprod_l, cf.fprod_r, cf.fprod_h
        FROM mapunit mu
        INNER JOIN legend l ON l.lkey = mu.lkey
        INNER JOIN sacatalog sc ON sc.areasymbol = l.areasymbol
        INNER JOIN component c ON c.mukey = mu.mukey
        LEFT JOIN coforprod cf ON cf.cokey = c.cokey
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
        ORDER BY mu.mukey, c.comppct_r DESC, c.cokey, cf.fprod_r DESC, cf.plantsym
    """


def limitations_sql(wkt_polygon: str, interpretations: tuple = INTERPRETATIONS) -> str:
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in interpretations)
    return f"""
        SELECT c.mukey, c.cokey, ci.mrulename, ci.ruledepth, ci.rulename, ci.seqnum, ci.interplr, ci.interplrc
        FROM component c
        INNER JOIN cointerp ci ON ci.cokey = c.cokey AND ci.mrulename IN ({names})
        WHERE c.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
        ORDER BY c.mukey, c.cokey, ci.mrulename, ci.ruledepth, ci.seqnum
    """


def _rows(result: dict) -> list:
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def get_woodland_for_polygon(wkt_polygon: str) -> dict:
    """The two row sets, as dicts of strings the way every soil_data
    query returns them: {'productivity': rows, 'limitations': rows}.
    Raises on a failed request."""
    return {
        "productivity": _rows(_run_sda_query(productivity_sql(wkt_polygon))),
        "limitations": _rows(_run_sda_query(limitations_sql(wkt_polygon))),
    }


def get_woodland_for_boundary(boundary_coordinates: list) -> dict:
    return get_woodland_for_polygon(coordinates_to_wkt_polygon(boundary_coordinates))


def parse_woodland(raw: dict) -> dict:
    """
    The rows -> the block:

        {'map_units': {mukey: {'muname', 'components': [cokey, ...]}},
         'components': {cokey: {'mukey', 'compname', 'comppct', 'major',
                                'species': [{'symbol', 'scientific', 'common', 'base',
                                             'site_index', 'site_index_low', 'site_index_high',
                                             'volume', 'volume_low', 'volume_high'}],
                                'ratings': {interpretation: {'class', 'value', 'features'}}}},
         'survey_areas': [{'areasymbol', 'saverest'}]}

    A component with no coforprod rows has an empty `species` list; one
    with no cointerp rows an empty `ratings` dict. The ratings take
    soil_road_ratings.parse_road_ratings()'s shape so dominant_condition()
    and class_features() read them unchanged.
    """
    map_units, components, surveys = {}, {}, {}
    for row in raw.get("productivity") or []:
        mukey, cokey = _text(row.get("mukey")), _text(row.get("cokey"))
        if mukey is None or cokey is None:
            continue
        if mukey not in map_units:
            map_units[mukey] = {"muname": _text(row.get("muname")), "components": []}
        if cokey not in components:
            components[cokey] = {
                "mukey": mukey,
                "compname": _text(row.get("compname")),
                "comppct": _float(row.get("comppct_r")) or 0.0,
                "major": (_text(row.get("majcompflag")) or "").lower() == "yes",
                "species": [],
                "ratings": {},
            }
            map_units[mukey]["components"].append(cokey)
        symbol = _text(row.get("areasymbol"))
        if symbol and symbol not in surveys:
            surveys[symbol] = {"areasymbol": symbol, "saverest": _text(row.get("saverest"))}
        plant = _text(row.get("plantsym"))
        if plant is None:
            continue
        components[cokey]["species"].append({
            "symbol": plant,
            "scientific": _text(row.get("plantsciname")),
            "common": _text(row.get("plantcomname")),
            "base": _text(row.get("siteindexbase")),
            "site_index": _float(row.get("siteindex_r")),
            "site_index_low": _float(row.get("siteindex_l")),
            "site_index_high": _float(row.get("siteindex_h")),
            "volume": _float(row.get("fprod_r")),
            "volume_low": _float(row.get("fprod_l")),
            "volume_high": _float(row.get("fprod_h")),
        })
    for row in raw.get("limitations") or []:
        cokey = _text(row.get("cokey"))
        interpretation = _text(row.get("mrulename"))
        if cokey not in components or interpretation is None:
            continue
        rating = components[cokey]["ratings"].setdefault(interpretation, {"class": None, "value": None, "features": []})
        if _text(row.get("ruledepth")) == "0":
            rating["class"] = _text(row.get("interplrc"))
            rating["value"] = _float(row.get("interplr"))
        else:
            rating["features"].append({
                "name": _text(row.get("interplrc")),
                "value": _float(row.get("interplr")),
                "rule": _text(row.get("rulename")),
            })
    return {"map_units": map_units, "components": components, "survey_areas": list(surveys.values())}


def unit_condition(block: dict, mukey: str, interpretation: str) -> dict:
    """One map unit's dominant condition for one interpretation, with its
    features -- {'class', 'weights', 'rated_pct', 'components', 'features':
    {name: share}}. class None when no component is rated."""
    unit = block["map_units"].get(mukey)
    if unit is None:
        return {"class": None, "weights": {}, "rated_pct": 0.0, "components": [], "features": {}}
    components = [block["components"][cokey] for cokey in unit["components"]]
    condition = dominant_condition(components, interpretation, INTERPRETATION_CLASSES[interpretation])
    features = {}
    if condition["class"] in INTERPRETATION_CLASSES[interpretation]:
        features = class_features(condition["components"], interpretation)
    return dict(condition, features=features)


def unit_species(block: dict, mukey: str) -> dict:
    """
    One map unit's rated species, shared among its MAJOR components by
    their percentages (the module docstring's rule):

        {'species': {symbol: {'symbol', 'common', 'scientific', 'share',
                              'site_index': [(value, comppct, base), ...],
                              'volume': [(value, comppct), ...]}},
         'major_pct': the major components' summed percentage,
         'rated_major': [{'compname', 'comppct'}], 'unrated_major': [...]}

    `share` is the fraction of the unit's ground the rating major
    components hold: their percentages over `major_pct`. A minor component
    takes no part; a major one with no rows is listed as unrated.
    """
    unit = block["map_units"].get(mukey)
    species, rated, unrated = {}, [], []
    major_pct = 0.0
    if unit is None:
        return {"species": species, "major_pct": 0.0, "rated_major": rated, "unrated_major": unrated}
    majors = [block["components"][cokey] for cokey in unit["components"] if block["components"][cokey]["major"]]
    major_pct = sum(c["comppct"] for c in majors)
    for component in majors:
        rows = [s for s in component["species"] if s["site_index"] is not None]
        entry = {"compname": component["compname"], "comppct": component["comppct"]}
        if not rows:
            unrated.append(entry)
            continue
        rated.append(entry)
        for row in rows:
            record = species.setdefault(row["symbol"], {
                "symbol": row["symbol"], "common": row["common"], "scientific": row["scientific"],
                "share": 0.0, "site_index": [], "volume": [],
            })
            record["share"] += component["comppct"] / major_pct if major_pct > 0 else 0.0
            record["site_index"].append((row["site_index"], component["comppct"], row["base"]))
            if row["volume"] is not None:
                record["volume"].append((row["volume"], component["comppct"]))
    return {"species": species, "major_pct": major_pct, "rated_major": rated, "unrated_major": unrated}


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    block = parse_woodland(get_woodland_for_boundary(REAL_BOUNDARY))
    print(block["survey_areas"])
    for mukey, unit in block["map_units"].items():
        print(mukey, unit["muname"][:44])
        for interpretation in INTERPRETATIONS:
            condition = unit_condition(block, mukey, interpretation)
            print("   ", INTERPRETATION_LABELS[interpretation], condition["class"],
                  {k: round(v, 2) for k, v in condition["features"].items()})
        rated = unit_species(block, mukey)
        print("    species", {s: round(r["share"], 2) for s, r in rated["species"].items()}, "unrated", rated["unrated_major"])
