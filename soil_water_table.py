"""
soil_water_table.py

SSURGO'S SEASONAL WATER TABLE, FLOODING AND PONDING BY MONTH for the site
data report's Water & hydrology section -- the section's centrepiece.

    get_seasonal_water_table_for_polygon(wkt)  -> the rows (a fetch, one query)
    parse_seasonal_water_table(rows)           -> the parsed block

THE TABLES AND COLUMNS, verified live against Soil Data Access for the
reference parcel (step 0):

    comonth        one row per component per month: flodfreqcl,
                   floddurcl, pondfreqcl, ponddurcl, ponddep_r
    cosoilmoist    one row per soil-moisture layer within a component-
                   month: soimoistdept_r (top), soimoistdepb_r (bottom),
                   soimoiststat ('Wet' | 'Moist' | 'Dry')
    muaggatt       the map-unit rollups NRCS publishes from the same
                   rows: wtdepannmin, wtdepaprjunmin, flodfreqdcd,
                   pondfreqprs, drclassdcd, hydclprs
    sacatalog      the survey area and its version date, for the footer

DEPTH TO WATER TABLE IN A MONTH is the top of the shallowest cosoilmoist
layer whose status is Wet -- NRCS's own definition behind
muaggatt.wtdepannmin, and it closes: on the reference parcel Atkins reads
20 cm in every month against a wtdepannmin of 20, and Ernest 53 with
Vandergrift 36 give their map unit's 36.

THREE STATES, KEPT APART, because collapsing them throws information
away (the author's decision in step 0):

    a Wet layer exists         -> a depth, in cm
    rows exist, none is Wet    -> the water table is DEEPER THAN the
                                  described profile's bottom (the deepest
                                  soimoistdepb_r that month): a positive
                                  statement about a real observation,
                                  rendered "deeper than N in"
    no comonth rows at all     -> NO DATA, rendered as the dash, never
                                  as "deep"

The query LEFT JOINs comonth so a component with no rows still arrives
(month None) and the third state is visible rather than absent.

FLOODING AND PONDING carry the same distinction: a frequency class
('None', 'Rare', 'Occasional', 'Frequent', 'Very frequent'), or NULL
where NRCS recorded nothing for that month -- on the reference parcel
Atkins carries 'Frequent' for January to April and NULL for the rest,
while every other component carries the string 'None'. NULL is "not
stated", not "none".

ONE QUERY. The map units come from the same WKT intersection Layer 1's
soil queries use, so the report layer needs only the boundary; muaggatt
and the survey area ride the same round trip as extra columns. This is a
REPORT-TIME query in the report layer (report_data.py), not a Layer 1
change: nothing a design step consumes reads it.
"""

from typing import Optional

from soil_data import _run_sda_query, coordinates_to_wkt_polygon

MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December")

FREQUENCY_CLASSES = ("None", "Rare", "Occasional", "Frequent", "Very frequent")

SSURGO_CITATION = (
    "USDA Natural Resources Conservation Service, Soil Survey Geographic Database (SSURGO), "
    "tables comonth, cosoilmoist, muaggatt and sacatalog, served by Soil Data Access."
)


def seasonal_water_table_sql(wkt_polygon: str) -> str:
    return f"""
        SELECT mu.mukey, mu.muname, l.areasymbol, sc.saverest,
               ma.wtdepannmin, ma.wtdepaprjunmin, ma.flodfreqdcd, ma.pondfreqprs, ma.drclassdcd, ma.hydclprs,
               c.cokey, c.compname, c.comppct_r, c.majcompflag, c.hydricrating, c.drainagecl,
               cm.comonthkey, cm.month, cm.monthseq, cm.flodfreqcl, cm.floddurcl, cm.pondfreqcl, cm.ponddurcl, cm.ponddep_r,
               (SELECT MIN(sm.soimoistdept_r) FROM cosoilmoist sm
                 WHERE sm.comonthkey = cm.comonthkey AND sm.soimoiststat = 'Wet') AS wet_top_cm,
               (SELECT MAX(sm.soimoistdepb_r) FROM cosoilmoist sm
                 WHERE sm.comonthkey = cm.comonthkey) AS described_bottom_cm,
               (SELECT COUNT(*) FROM cosoilmoist sm WHERE sm.comonthkey = cm.comonthkey) AS layer_count
        FROM mapunit mu
        INNER JOIN legend l ON l.lkey = mu.lkey
        INNER JOIN sacatalog sc ON sc.areasymbol = l.areasymbol
        LEFT JOIN muaggatt ma ON ma.mukey = mu.mukey
        INNER JOIN component c ON c.mukey = mu.mukey
        LEFT JOIN comonth cm ON cm.cokey = c.cokey
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
        ORDER BY mu.mukey, c.comppct_r DESC, c.cokey, cm.monthseq
    """


def get_seasonal_water_table_for_polygon(wkt_polygon: str) -> list:
    """The joined rows, one per component-month (or one per component
    with month None when it has no comonth rows), as dicts of strings the
    way every soil_data query returns them. Raises on a failed request."""
    result = _run_sda_query(seasonal_water_table_sql(wkt_polygon))
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def get_seasonal_water_table_for_boundary(boundary_coordinates: list) -> list:
    return get_seasonal_water_table_for_polygon(coordinates_to_wkt_polygon(boundary_coordinates))


def _float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> Optional[int]:
    number = _float(value)
    return None if number is None else int(number)


def _text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_seasonal_water_table(rows: list) -> dict:
    """
    The rows -> the block:

        {'map_units': {mukey: {'muname', 'wtdepannmin_cm', 'wtdepaprjunmin_cm',
                               'flodfreqdcd', 'pondfreqprs', 'drclassdcd', 'hydclprs',
                               'components': [cokey, ...]}},
         'components': {cokey: {'mukey', 'compname', 'comppct', 'major',
                                'hydricrating', 'drainagecl', 'has_rows': bool,
                                'months': {1..12: {'wet_top_cm', 'described_bottom_cm',
                                                    'layer_count', 'flood', 'flood_duration',
                                                    'pond', 'pond_duration', 'pond_depth_cm'}}}},
         'survey_areas': [{'areasymbol', 'saverest'}]}

    A component whose rows carried no month has has_rows False and an
    empty months dict. Within a month, wet_top_cm None with layer_count
    > 0 is the deeper-than state; the caller reads described_bottom_cm
    for the depth it is deeper than.
    """
    map_units, components, surveys = {}, {}, {}
    for row in rows:
        mukey, cokey = _text(row.get("mukey")), _text(row.get("cokey"))
        if mukey is None or cokey is None:
            continue
        if mukey not in map_units:
            map_units[mukey] = {
                "muname": _text(row.get("muname")),
                "wtdepannmin_cm": _float(row.get("wtdepannmin")),
                "wtdepaprjunmin_cm": _float(row.get("wtdepaprjunmin")),
                "flodfreqdcd": _text(row.get("flodfreqdcd")),
                "pondfreqprs": _float(row.get("pondfreqprs")),
                "drclassdcd": _text(row.get("drclassdcd")),
                "hydclprs": _float(row.get("hydclprs")),
                "components": [],
            }
        if cokey not in components:
            components[cokey] = {
                "mukey": mukey,
                "compname": _text(row.get("compname")),
                "comppct": _float(row.get("comppct_r")) or 0.0,
                "major": (_text(row.get("majcompflag")) or "").lower() == "yes",
                "hydricrating": _text(row.get("hydricrating")),
                "drainagecl": _text(row.get("drainagecl")),
                "has_rows": False,
                "months": {},
            }
            map_units[mukey]["components"].append(cokey)
        symbol = _text(row.get("areasymbol"))
        if symbol and symbol not in surveys:
            surveys[symbol] = {"areasymbol": symbol, "saverest": _text(row.get("saverest"))}
        month = _int(row.get("monthseq"))
        if month is None:
            name = _text(row.get("month"))
            month = MONTHS.index(name) + 1 if name in MONTHS else None
        if month is None:
            continue
        components[cokey]["has_rows"] = True
        components[cokey]["months"][month] = {
            "wet_top_cm": _float(row.get("wet_top_cm")),
            "described_bottom_cm": _float(row.get("described_bottom_cm")),
            "layer_count": _int(row.get("layer_count")) or 0,
            "flood": _text(row.get("flodfreqcl")),
            "flood_duration": _text(row.get("floddurcl")),
            "pond": _text(row.get("pondfreqcl")),
            "pond_duration": _text(row.get("ponddurcl")),
            "pond_depth_cm": _float(row.get("ponddep_r")),
        }
    return {"map_units": map_units, "components": components, "survey_areas": list(surveys.values())}


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    block = parse_seasonal_water_table(get_seasonal_water_table_for_boundary(REAL_BOUNDARY))
    print(block["survey_areas"])
    for cokey, c in block["components"].items():
        if c["major"]:
            print(c["compname"], c["comppct"], [c["months"].get(m, {}).get("wet_top_cm") for m in range(1, 13)])
