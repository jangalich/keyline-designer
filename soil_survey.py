"""
soil_survey.py

THE CORE SOIL SURVEY READING for the site data report's Soils & geology
section: the map unit's own symbol, and, for every component, the surface
horizon the survey describes (its texture and particle sizes, water
capacity, organic matter and reaction), the depth at which something
stops a root, and the land capability and soil-loss tolerance the survey
assigns.

    get_survey_for_polygon(wkt)      -> the raw rows (a fetch, ONE query)
    get_survey_for_boundary(bounds)  -> the same, from a (lon, lat) ring
    parse_survey(rows)               -> the parsed block
    dominant_major(block, mukey)     -> one map unit's dominant component

THIS IS THE REPORT'S FOURTH PASS AT SSURGO AND IT REPEATS NOTHING. The
seasonal water table went to Water (soil_water_table), the road ratings
to Access (soil_road_ratings), the woodland productivity to Trees
(soil_woodland); drainage class, hydrologic group, saturated hydraulic
conductivity, the K factor, farmland classification and the map unit
polygons are all fetched at LAYER 1 (parcel_data) and are read off the
session, not fetched again. What is left -- and what this query is for --
is the survey's core description of the soil itself, which no section has
asked for yet.

THE COLUMNS, verified live against Soil Data Access for the reference
parcel (branch 12, step 0, 2026-09-23; survey area PA003, version of
5 September 2025; 30 rows in 0.57 s):

    mapunit         musym, the symbol the published survey prints inside
                    each polygon on its own map sheet. Layer 1's soil
                    queries do not select it, so the map's labels ride
                    this query.
    muaggatt        aws0150wta, the water the whole profile holds to
                    150 cm. A DIFFERENT DEPTH BASIS from the surface
                    horizon's awc_r below, so the two never share a table.
    component       nirrcapcl / nirrcapscl (non-irrigated land capability
                    class and subclass), tfact (soil-loss tolerance, tons
                    per acre per year). irrcapcl is served but is null
                    throughout Pennsylvania -- no irrigated capability is
                    assigned -- so it is not carried.
    chorizon        THE SURFACE HORIZON ONLY: hzname, hzdept_r, hzdepb_r,
                    sandtotal_r, silttotal_r, claytotal_r, awc_r, om_r,
                    ph1to1h2o_r. Plus the component's described profile
                    bottom, MAX(hzdepb_r), which is how deep the survey
                    looked.
    chtexturegrp    texdesc, the survey's own phrase for the horizon
    /chtexture      ("Channery silt loam"), and texcl, the base class
                    under it ("Silt loam"). rvindicator = 'Yes' picks the
                    REPRESENTATIVE group: two of the reference parcel's
                    seven units carry a second, non-representative texture
                    group, and without the filter both arrive.
    corestrictions  the shallowest BEDROCK restriction (reskind ending in
                    "bedrock" -- Lithic, Paralithic, Densic) with its
                    kind, and SEPARATELY the shallowest restriction that
                    is NOT bedrock, with its kind. See below.
    legend          areasymbol, and the survey version (saverest) the
    /sacatalog      figures came from.

THE SURFACE HORIZON IS soil_data's, NOT A NEW DEFINITION. The shallowest
horizon whose name does not start with O -- SURFACE_HORIZON_PREDICATE
below is the same SQL get_erosion_factor_for_polygon() and
get_saturated_hydraulic_conductivity_for_polygon() already use. That
matters more than any alternative: the K factor and the Ksat the section
prints beside these figures were read at that horizon, and a section
whose texture came from one definition and whose K factor came from
another would be describing two different depths in one row.

THE DEPTH IS NOT THE SAME EVERYWHERE, so it is carried per component and
printed per row. On the reference parcel the surface horizon runs 0-8 cm
under one unit, 0-24 cm under another, and 3-25 cm under a third, where
an O horizon sits on top and is skipped. One depth stated for a whole
table would be wrong for most of it.

A RESTRICTION THAT IS NOT BEDROCK IS STILL A RESTRICTION, and the two are
kept apart because conflating them misleads in both directions. The
reference parcel's Ernest component -- the dominant soil of a map unit
covering 3.1 acres -- has NO bedrock described at all, and muaggatt's
own brockdepmin reports 152 cm for that unit because a companion
component's bedrock is the only one there is. A reader given 152 cm
would conclude there is five feet of rooting. There is a FRAGIPAN at
71 cm. The section prints the bedrock depth in its column and the
non-bedrock restriction in the caption beneath, which is where a fact
that qualifies a figure belongs.

NO BEDROCK DESCRIBED IS NOT ZERO AND IS NOT UNKNOWN. A component with no
bedrock restriction was described to `described_bottom_cm` and bedrock
was not reached, so the honest statement is a BOUND -- deeper than the
depth the survey looked -- which is the cell kind the Water section's
water table already uses for exactly this shape.

ONE FETCH, ONE QUERY, REPORT-TIME. A report-layer fetch (report_data.py,
DEGRADABLE), not a Layer 1 change: nothing a design step consumes reads
it. musym is the one column the page cannot do without, and putting it on
Layer 1's hard-fail soil query -- the way hydgrp went there -- was
considered and rejected: hydgrp is consumed by a DESIGN step
(water_survey_areas' soil scorer), and musym is not consumed by anything
but this report. A degraded render draws the map unit polygons outlined
and unlabelled with the absence stated, which is honest; a widened
hard-fail contract for a report-only column would not be.
"""

from typing import Optional

from soil_data import _run_sda_query, coordinates_to_wkt_polygon

# The same surface-horizon rule get_erosion_factor_for_polygon() and
# get_saturated_hydraulic_conductivity_for_polygon() apply, so the
# texture, the K factor and the Ksat all describe one horizon. `ch` is
# the alias the query binds the chorizon join to.
SURFACE_HORIZON_PREDICATE = (
    "ch.hzdept_r = (SELECT MIN(hzdept_r) FROM chorizon "
    "WHERE hzname NOT LIKE 'O%' AND chorizon.cokey = ch.cokey)"
)

# SSURGO's corestrictions.reskind values for rock end in "bedrock"
# (Lithic bedrock, Paralithic bedrock, Densic bedrock); everything else
# it lists -- Fragipan, Densic material, Cemented horizon, Abrupt
# textural change, Natric, Permafrost, Petrocalcic, Salic, Sulfuric -- is
# a restriction that is NOT rock. One predicate, used twice with opposite
# senses, so the two can never overlap or leave a kind uncounted.
BEDROCK_PREDICATE = "cr.reskind LIKE '%bedrock'"

CAPABILITY_SUBCLASS_LABELS = {
    "e": "erosion",
    "w": "wetness",
    "s": "shallow, droughty or stony",
    "c": "climate",
}

SSURGO_CITATION = (
    "USDA Natural Resources Conservation Service, Soil Survey Geographic Database (SSURGO), tables mapunit, "
    "muaggatt, component, chorizon, chtexturegrp, chtexture, corestrictions, legend and sacatalog, served by "
    "Soil Data Access."
)


def survey_sql(wkt_polygon: str) -> str:
    return f"""
        SELECT mu.mukey, mu.musym, l.areasymbol, sc.saverest, ma.aws0150wta,
               c.cokey, c.compname, c.comppct_r, c.majcompflag,
               c.nirrcapcl, c.nirrcapscl, c.tfact,
               ch.hzname, ch.hzdept_r, ch.hzdepb_r,
               ch.sandtotal_r, ch.silttotal_r, ch.claytotal_r,
               ch.awc_r, ch.om_r, ch.ph1to1h2o_r,
               cg.texdesc, ct.texcl,
               (SELECT MAX(ch2.hzdepb_r) FROM chorizon ch2 WHERE ch2.cokey = c.cokey) AS described_bottom_cm,
               (SELECT MIN(cr.resdept_r) FROM corestrictions cr
                 WHERE cr.cokey = c.cokey AND {BEDROCK_PREDICATE}) AS bedrock_cm,
               (SELECT TOP 1 cr.reskind FROM corestrictions cr
                 WHERE cr.cokey = c.cokey AND {BEDROCK_PREDICATE}
                 ORDER BY cr.resdept_r) AS bedrock_kind,
               (SELECT MIN(cr.resdept_r) FROM corestrictions cr
                 WHERE cr.cokey = c.cokey AND NOT ({BEDROCK_PREDICATE})) AS restriction_cm,
               (SELECT TOP 1 cr.reskind FROM corestrictions cr
                 WHERE cr.cokey = c.cokey AND NOT ({BEDROCK_PREDICATE})
                 ORDER BY cr.resdept_r) AS restriction_kind
        FROM mapunit mu
        INNER JOIN legend l ON l.lkey = mu.lkey
        INNER JOIN sacatalog sc ON sc.areasymbol = l.areasymbol
        LEFT JOIN muaggatt ma ON ma.mukey = mu.mukey
        INNER JOIN component c ON c.mukey = mu.mukey
        LEFT JOIN chorizon ch ON ch.cokey = c.cokey AND {SURFACE_HORIZON_PREDICATE}
        LEFT JOIN chtexturegrp cg ON cg.chkey = ch.chkey AND cg.rvindicator = 'Yes'
        LEFT JOIN chtexture ct ON ct.chtgkey = cg.chtgkey
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
        ORDER BY mu.musym, c.comppct_r DESC, c.cokey
    """


def get_survey_for_polygon(wkt_polygon: str) -> list:
    """The joined rows, one per component, as dicts of strings the way
    every soil_data query returns them. Raises on a failed request."""
    result = _run_sda_query(survey_sql(wkt_polygon))
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def get_survey_for_boundary(boundary_coordinates: list) -> list:
    return get_survey_for_polygon(coordinates_to_wkt_polygon(boundary_coordinates))


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


def parse_survey(rows: list) -> dict:
    """
    The rows -> the block:

        {'map_units': {mukey: {'musym', 'aws0150_cm', 'components': [cokey, ...]}},
         'components': {cokey: {'mukey', 'compname', 'comppct', 'major',
                                'capability_class', 'capability_subclass', 'tfact',
                                'horizon': {'hzname', 'top_cm', 'bottom_cm', 'texture',
                                            'texture_class', 'sand_pct', 'silt_pct',
                                            'clay_pct', 'awc', 'om_pct', 'ph'} | None,
                                'described_bottom_cm',
                                'bedrock_cm', 'bedrock_kind',
                                'restriction_cm', 'restriction_kind'}},
         'survey_areas': [{'areasymbol', 'saverest'}]}

    `horizon` is None for a component the survey describes no non-O
    horizon for -- a real state on the reference parcel (a 5% minor), not
    an error. `bedrock_cm` None means no bedrock restriction was
    described, and `described_bottom_cm` is how deep the survey looked,
    so the caller states a bound rather than a depth.
    """
    map_units, components, surveys = {}, {}, {}
    for row in rows:
        mukey, cokey = _text(row.get("mukey")), _text(row.get("cokey"))
        if mukey is None or cokey is None:
            continue
        if mukey not in map_units:
            map_units[mukey] = {
                "musym": _text(row.get("musym")),
                "aws0150_cm": _float(row.get("aws0150wta")),
                "components": [],
            }
        if cokey not in components:
            horizon = None
            if _text(row.get("hzname")) is not None or _float(row.get("hzdept_r")) is not None:
                horizon = {
                    "hzname": _text(row.get("hzname")),
                    "top_cm": _float(row.get("hzdept_r")),
                    "bottom_cm": _float(row.get("hzdepb_r")),
                    "texture": _text(row.get("texdesc")),
                    "texture_class": _text(row.get("texcl")),
                    "sand_pct": _float(row.get("sandtotal_r")),
                    "silt_pct": _float(row.get("silttotal_r")),
                    "clay_pct": _float(row.get("claytotal_r")),
                    "awc": _float(row.get("awc_r")),
                    "om_pct": _float(row.get("om_r")),
                    "ph": _float(row.get("ph1to1h2o_r")),
                }
            components[cokey] = {
                "mukey": mukey,
                "compname": _text(row.get("compname")),
                "comppct": _float(row.get("comppct_r")) or 0.0,
                "major": (_text(row.get("majcompflag")) or "").lower() == "yes",
                "capability_class": _text(row.get("nirrcapcl")),
                "capability_subclass": _text(row.get("nirrcapscl")),
                "tfact": _float(row.get("tfact")),
                "horizon": horizon,
                "described_bottom_cm": _float(row.get("described_bottom_cm")),
                "bedrock_cm": _float(row.get("bedrock_cm")),
                "bedrock_kind": _text(row.get("bedrock_kind")),
                "restriction_cm": _float(row.get("restriction_cm")),
                "restriction_kind": _text(row.get("restriction_kind")),
            }
            map_units[mukey]["components"].append(cokey)
        symbol = _text(row.get("areasymbol"))
        if symbol and symbol not in surveys:
            surveys[symbol] = {"areasymbol": symbol, "saverest": _text(row.get("saverest"))}
    return {"map_units": map_units, "components": components, "survey_areas": list(surveys.values())}


def major_components(block: dict, mukey: str) -> list:
    """One map unit's major components, largest share first. The rows
    arrive comppct_r DESC, so this is the order the survey gave."""
    unit = (block or {}).get("map_units", {}).get(str(mukey))
    if not unit:
        return []
    return [block["components"][cokey] for cokey in unit["components"] if block["components"][cokey]["major"]]


def dominant_major(block: dict, mukey: str) -> Optional[dict]:
    """
    A map unit's DOMINANT MAJOR COMPONENT -- the largest by percentage,
    ties to the first the survey listed. This codebase's standing
    convention for "what is really there" (soil_data.get_soil_data_for_
    polygon's docstring; SSURGO's own muaggatt "dominant condition"
    rollups), and the one rule the whole physical-properties row obeys.

    ONE RULE FOR THE WHOLE ROW, deliberately. Sand, silt, clay, water
    capacity, organic matter and pH could each be shared among a unit's
    major components by their percentages the way Trees shares site
    index; the named texture class cannot be averaged at all. A row whose
    figures were weighted means and whose texture was one component's
    would describe no soil that exists, so every value in the row comes
    from the same component and the caption names the rule.
    """
    majors = major_components(block, mukey)
    if majors:
        return max(majors, key=lambda c: c["comppct"])
    unit = (block or {}).get("map_units", {}).get(str(mukey))
    if not unit or not unit["components"]:
        return None
    return max((block["components"][c] for c in unit["components"]), key=lambda c: c["comppct"])


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    parsed = parse_survey(get_survey_for_boundary(REAL_BOUNDARY))
    print(parsed["survey_areas"])
    for mukey, unit in parsed["map_units"].items():
        component = dominant_major(parsed, mukey)
        horizon = component["horizon"] or {}
        print(f"{unit['musym']:>5} {component['compname']:<12} {component['comppct']:>3.0f}%  "
              f"{horizon.get('texture')}  {horizon.get('top_cm')}-{horizon.get('bottom_cm')} cm  "
              f"awc {horizon.get('awc')}  om {horizon.get('om_pct')}  pH {horizon.get('ph')}  "
              f"cap {component['capability_class']}{component['capability_subclass'] or ''}  T {component['tfact']}  "
              f"bedrock {component['bedrock_cm']} ({component['bedrock_kind']})  "
              f"other {component['restriction_cm']} ({component['restriction_kind']})")
