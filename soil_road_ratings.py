"""
soil_road_ratings.py

SSURGO'S ROAD-CONSTRUCTION INTERPRETATIONS for the site data report's
Access section: how limited the parcel's soils are for a local road, and
why -- frost action, low strength, shrink-swell, a shallow water table,
slope, flooding, bedrock or a fragipan.

    get_road_ratings_for_polygon(wkt)   -> the rows (a fetch, one query)
    parse_road_ratings(rows)            -> the parsed block
    dominant_condition(components, ...) -> one map unit's class, NRCS's rule

THE TABLE AND COLUMNS, verified live against Soil Data Access for the
reference parcel (branch 10, step 0, 2026-09-23; survey area PA003,
version of 5 September 2025):

    cointerp    one row per component per interpretation per rule:
                mrulename (the interpretation), ruledepth (0 = the overall
                rating, 1 = one limiting feature), rulename (the rule that
                fired), interplr (0-1, the degree of limitation),
                interplrc (the class or, at depth 1, the feature's name),
                seqnum. SDA's cointerp carries depths 0 and 1 only, which
                is exactly the rating and its features.
    component   cokey, compname, comppct_r, majcompflag
    mapunit, legend, sacatalog   the map unit, its survey area and version

THREE INTERPRETATIONS RIDE ONE QUERY, filtered by mrulename:

    ENG - Local Roads and Streets           Not limited / Somewhat limited /
                                            Very limited / Not rated
    ENG - Unpaved Local Roads and Streets   the same classes; on the
                                            reference parcel identical to
                                            the paved rating apart from a
                                            minor dustiness feature
    ENG - Construction Materials; Roadfill  Good / Fair / Poor

The section's table is the PAVED interpretation, the one the published
soil survey's tables report; the unpaved class is noted where it differs
and roadfill is a clause. The natural-surface forestry interpretation is
available and deliberately not carried: a second vocabulary on a one-page
section (the author's decision, step 0).

THE MAP-UNIT CLASS IS NRCS'S DOMINANT CONDITION: component percentages
summed by class over every component that carries a rating, the class
with the largest total winning and a tie going to the MORE limiting class
-- Web Soil Survey's own aggregation for an interpretation. A component
with no rows for the interpretation takes no part; a map unit whose
components all lack rows has no data and says so.

THE LIMITING FEATURES of a map unit are those of its components in the
winning class, each feature weighted by the component's share of that
class: the depth-1 rows with a nonzero degree. A "Not rated" component's
feature rows name why it was not rated and are not limitations.

ONE QUERY, REPORT-TIME. The map units come from the same WKT
intersection Layer 1's soil queries use; this is a report-layer query
(report_data.py, DEGRADABLE), not a Layer 1 change: nothing a design
step consumes reads it.
"""

from typing import Optional

from soil_data import _run_sda_query, coordinates_to_wkt_polygon

LOCAL_ROADS = "ENG - Local Roads and Streets"
UNPAVED_LOCAL_ROADS = "ENG - Unpaved Local Roads and Streets"
ROADFILL = "ENG - Construction Materials; Roadfill"
INTERPRETATIONS = (LOCAL_ROADS, UNPAVED_LOCAL_ROADS, ROADFILL)

NOT_LIMITED = "Not limited"
SOMEWHAT_LIMITED = "Somewhat limited"
VERY_LIMITED = "Very limited"
NOT_RATED = "Not rated"
# Least to most limiting; the tie-break reads this order backwards.
LIMITATION_CLASSES = (NOT_LIMITED, SOMEWHAT_LIMITED, VERY_LIMITED)
ROADFILL_CLASSES = ("Good", "Fair", "Poor")

SSURGO_CITATION = (
    "USDA Natural Resources Conservation Service, Soil Survey Geographic Database (SSURGO), tables cointerp, "
    "component, mapunit, legend and sacatalog, served by Soil Data Access."
)


def road_ratings_sql(wkt_polygon: str, interpretations: tuple = INTERPRETATIONS) -> str:
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in interpretations)
    return f"""
        SELECT mu.mukey, mu.muname, l.areasymbol, sc.saverest,
               c.cokey, c.compname, c.comppct_r, c.majcompflag,
               ci.mrulename, ci.ruledepth, ci.rulename, ci.seqnum, ci.interplr, ci.interplrc
        FROM mapunit mu
        INNER JOIN legend l ON l.lkey = mu.lkey
        INNER JOIN sacatalog sc ON sc.areasymbol = l.areasymbol
        INNER JOIN component c ON c.mukey = mu.mukey
        LEFT JOIN cointerp ci ON ci.cokey = c.cokey AND ci.mrulename IN ({names})
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
        ORDER BY mu.mukey, c.comppct_r DESC, c.cokey, ci.mrulename, ci.ruledepth, ci.seqnum
    """


def get_road_ratings_for_polygon(wkt_polygon: str) -> list:
    """The joined rows, one per component-interpretation-rule (or one per
    component with mrulename None when it carries no rating), as dicts of
    strings the way every soil_data query returns them. Raises on a
    failed request."""
    result = _run_sda_query(road_ratings_sql(wkt_polygon))
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def get_road_ratings_for_boundary(boundary_coordinates: list) -> list:
    return get_road_ratings_for_polygon(coordinates_to_wkt_polygon(boundary_coordinates))


def _float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_road_ratings(rows: list) -> dict:
    """
    The rows -> the block:

        {'map_units': {mukey: {'muname', 'components': [cokey, ...]}},
         'components': {cokey: {'mukey', 'compname', 'comppct', 'major',
                                'ratings': {interpretation: {
                                    'class': str | None, 'value': float | None,
                                    'features': [{'name', 'value', 'rule'}, ...]}}}},
         'survey_areas': [{'areasymbol', 'saverest'}]}

    A component with no cointerp rows has an empty `ratings` dict. A
    rating whose class is "Not rated" keeps its class and its feature
    rows; dominant_condition() treats it as its own class.
    """
    map_units, components, surveys = {}, {}, {}
    for row in rows:
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
                "ratings": {},
            }
            map_units[mukey]["components"].append(cokey)
        symbol = _text(row.get("areasymbol"))
        if symbol and symbol not in surveys:
            surveys[symbol] = {"areasymbol": symbol, "saverest": _text(row.get("saverest"))}
        interpretation = _text(row.get("mrulename"))
        if interpretation is None:
            continue
        rating = components[cokey]["ratings"].setdefault(interpretation, {"class": None, "value": None, "features": []})
        depth = _text(row.get("ruledepth"))
        if depth == "0":
            rating["class"] = _text(row.get("interplrc"))
            rating["value"] = _float(row.get("interplr"))
        else:
            rating["features"].append({
                "name": _text(row.get("interplrc")),
                "value": _float(row.get("interplr")),
                "rule": _text(row.get("rulename")),
            })
    return {"map_units": map_units, "components": components, "survey_areas": list(surveys.values())}


def dominant_condition(components: list, interpretation: str, classes: tuple = LIMITATION_CLASSES) -> dict:
    """
    NRCS's dominant-condition aggregation for ONE map unit: `components`
    are the unit's parsed component dicts. Returns

        {'class': the winning class or None (no component rated),
         'weights': {class: summed comppct}, 'rated_pct': the sum over
         rated components, 'cokeys': the components in the winning class}

    `classes` runs least to most limiting; a tie goes to the class later
    in it. "Not rated" is its own class, ranked below every real one, so
    a unit mostly made of unrated components reads Not rated rather than
    borrowing a minor component's rating. A class the table does not
    name (a new NRCS vocabulary) is kept as itself and ranked after the
    named ones by its position of first appearance.
    """
    weights, members = {}, {}
    for component in components:
        rating = component["ratings"].get(interpretation)
        if not rating or rating["class"] is None:
            continue
        weights[rating["class"]] = weights.get(rating["class"], 0.0) + component["comppct"]
        members.setdefault(rating["class"], []).append(component)
    if not weights:
        return {"class": None, "weights": {}, "rated_pct": 0.0, "components": []}
    order = [NOT_RATED] + list(classes) + [c for c in weights if c not in classes and c != NOT_RATED]

    def rank(name):
        return order.index(name) if name in order else -1

    winner = max(weights, key=lambda name: (weights[name], rank(name)))
    return {
        "class": winner,
        "weights": weights,
        "rated_pct": sum(weights.values()),
        "components": members[winner],
    }


def class_features(components: list, interpretation: str) -> dict:
    """
    The limiting features of the components in one class, weighted by
    each component's share of the class: {feature name: share}, shares
    summing to at most 1 per feature. A feature counts where its degree
    is above zero; the feature rows of a "Not rated" component are
    reasons it was not rated, not limitations, and are skipped.
    """
    rated = [c for c in components if (c["ratings"].get(interpretation) or {}).get("class") not in (None, NOT_RATED)]
    total = sum(c["comppct"] for c in rated)
    shares = {}
    if total <= 0:
        return shares
    for component in rated:
        weight = component["comppct"] / total
        for feature in component["ratings"][interpretation]["features"]:
            if feature["name"] and feature["value"] is not None and feature["value"] > 0:
                shares[feature["name"]] = shares.get(feature["name"], 0.0) + weight
    return shares


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    block = parse_road_ratings(get_road_ratings_for_boundary(REAL_BOUNDARY))
    print(block["survey_areas"])
    for mukey, unit in block["map_units"].items():
        comps = [block["components"][k] for k in unit["components"]]
        paved = dominant_condition(comps, LOCAL_ROADS)
        fill = dominant_condition(comps, ROADFILL, ROADFILL_CLASSES)
        print(mukey, unit["muname"][:40], paved["class"], paved["weights"], "| roadfill", fill["class"],
              "| features", {k: round(v, 2) for k, v in class_features(paved["components"], LOCAL_ROADS).items()})
