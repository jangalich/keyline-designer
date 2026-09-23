"""
test_soils_derivations.py

THE SOILS & GEOLOGY SECTION'S DERIVATIONS -- branch 12, phase 1 of
site-data-report-proposal.md's build sequence. Runs offline on the real
parcel: the captured 3DEP grid, the real SSURGO rows and map unit
polygons, the real K factors and the captured survey and geology
responses (soils_reference_fixture.py) through an offline session.

  1. THE FOURTH PASS REPEATS NOTHING. Every SSURGO column this section's
     query selects, set against every column the other three report
     queries and Layer 1's five select: the MEASURED columns are
     disjoint, and the only overlap is the identity, join and vintage
     set every SSURGO query in this repository carries. The surface
     horizon is soil_data's own rule, SQL for SQL.
  2. THE CALL COUNT: building the derivations reaches Soil Data Access,
     the geology service and the terrain passes ZERO times; the four
     Layer 1 readings it prints come off the session.
  3. ONE PARTITION, NOT TWO: the map-unit cell grid is Water's own, cell
     for cell, over the same Layer 1 polygons.
  4. EVERY FIGURE, TABLED against the values measured in step 0, with
     every acreage column summing to the cover's parcel acreage and
     every percent column to 100.0 through the shared allocator.
  5. THE RULES ON SYNTHETIC CASES: the dominant-component rule, the
     bedrock bound, the non-bedrock restriction, the capability and
     farmland ordering, the texture headline, and the degraded layers.
  6. NO ADVICE ANYWHERE IN THE BLOCK'S WORDS.
"""

import re
from unittest.mock import patch

import offline_harness

offline_harness.install()

import bedrock_geology  # noqa: E402
import soil_data  # noqa: E402
import soil_road_ratings as srr  # noqa: E402
import soil_survey as ss  # noqa: E402
import soil_water_table as swt  # noqa: E402
import soil_woodland as sw  # noqa: E402
import soils_derivations as sd  # noqa: E402
import soils_reference_fixture as fixture  # noqa: E402
import valley_delineation  # noqa: E402
import water_derivations as wd  # noqa: E402
from landform_section import allocate_exactly  # noqa: E402

WKT = "polygon((-80 40, -80 41, -79 41, -79 40, -80 40))"

# ======================================================================
# 1. The fourth pass repeats nothing
# ======================================================================
print("1. no SSURGO column is fetched twice across the four sections")

# Every `alias.column` a query's SELECT list names, ignoring the WHERE and
# ORDER BY clauses -- what the fetch actually carries back.
_SELECT = re.compile(r"\bSELECT\b(.*?)\bFROM\b", re.S | re.I)
_COLUMN = re.compile(r"\b[a-z][a-z0-9]*\.([a-z0-9_]+)\b", re.I)


def selected_columns(sql: str) -> set:
    names = set()
    for chunk in _SELECT.findall(sql):
        names |= {m.lower() for m in _COLUMN.findall(chunk)}
    # The mukey the subselect in each WHERE clause names is a filter, not
    # a carried column, and MIN/MAX/COUNT wrappers carry no column of
    # their own.
    return names - {"mukey_from_intersection_with_wktwgs84"}


# The identity, join and vintage columns EVERY SSURGO query here carries:
# without them a row cannot be attached to its map unit or its survey
# version. Selecting these more than once is not duplication of data, it
# is how four result sets are joined back together in Python.
IDENTITY = {"mukey", "muname", "cokey", "compname", "comppct_r", "majcompflag", "areasymbol", "saverest"}

QUERIES = {
    "soil_survey (VII, this section)": ss.survey_sql(WKT),
    "soil_water_table (IV)": swt.seasonal_water_table_sql(WKT),
    "soil_road_ratings (V)": srr.road_ratings_sql(WKT),
    "soil_woodland productivity (VI)": sw.productivity_sql(WKT),
    "soil_woodland limitations (VI)": sw.limitations_sql(WKT),
}
MEASURED = {name: selected_columns(sql) - IDENTITY for name, sql in QUERIES.items()}
survey_measured = MEASURED["soil_survey (VII, this section)"]
for name, columns in MEASURED.items():
    if name.startswith("soil_survey"):
        continue
    overlap = survey_measured & columns
    assert not overlap, f"this section re-fetches {sorted(overlap)} already fetched by {name}"
    assert selected_columns(QUERIES[name]) & selected_columns(QUERIES["soil_survey (VII, this section)"]) <= IDENTITY

# LAYER 1's five soil queries, the same test. Its columns reach the report
# path on ParcelData, so re-selecting one here would be a second fetch of
# something the session already holds.
LAYER_ONE = {
    "soil_components": soil_data.get_soil_data_for_polygon,
    "farmland_classification": soil_data.get_farmland_classification_for_polygon,
    "erosion_factor": soil_data.get_erosion_factor_for_polygon,
    "saturated_hydraulic_conductivity": soil_data.get_saturated_hydraulic_conductivity_for_polygon,
}
captured = {}
for name, function in LAYER_ONE.items():
    with patch.object(soil_data, "_run_sda_query", side_effect=lambda sql, **k: captured.setdefault(name, sql) and None
                      or {"columns": [], "rows": []}):
        function(WKT)
for name, sql in captured.items():
    overlap = (selected_columns(sql) - IDENTITY) & survey_measured
    assert not overlap, f"this section re-fetches {sorted(overlap)} already held on ParcelData.{name}"
# And the four Layer 1 readings the section PRINTS are named, so the test
# fails if one is quietly dropped or re-fetched later.
assert {"drainagecl", "hydgrp"} <= selected_columns(captured["soil_components"])
assert "farmlndcl" in selected_columns(captured["farmland_classification"])
assert "kwfact" in selected_columns(captured["erosion_factor"])
assert "ksat_r" in selected_columns(captured["saturated_hydraulic_conductivity"])

# THE SURFACE HORIZON IS soil_data's OWN RULE, not a second definition:
# the same SQL, whitespace aside, as the K factor and the Ksat were read
# with -- which is what lets the section print texture, K and Ksat in one
# row and mean one horizon.
def _squash(text):
    """SQL with its whitespace normalised, INCLUDING around brackets, so
    two spellings of one predicate compare equal. soil_data.py breaks its
    subselect over four lines and this module writes it on one; they are
    the same SQL and the point of the assertion is that they stay so."""
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s*([()])\s*", r"\1", text).strip()


erosion_sql = captured["erosion_factor"]
ksat_sql = captured["saturated_hydraulic_conductivity"]
predicate = _squash(ss.SURFACE_HORIZON_PREDICATE)
for sql, label in ((erosion_sql, "erosion_factor"), (ksat_sql, "saturated_hydraulic_conductivity")):
    assert predicate in _squash(sql), f"{label} reads a different surface horizon"
assert predicate in _squash(ss.survey_sql(WKT))
print(f"   {len(survey_measured)} measured columns, none of them any other query's; "
      f"surface horizon identical to Layer 1's K factor and Ksat")

# ======================================================================
# 2. The call count
# ======================================================================
print("2. the derivations fetch nothing and recompute nothing")

DATA = fixture.report_data()
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    INPUTS = sd.soils_inputs_from_context(CONTEXT, DOCUMENT, DATA)

with patch.object(ss, "_run_sda_query", side_effect=AssertionError("Soil Data Access reached")) as sda, \
        patch.object(soil_data, "_run_sda_query", side_effect=AssertionError("Soil Data Access reached")) as sda_layer1, \
        patch.object(bedrock_geology, "_get", side_effect=AssertionError("the geology service reached")) as geology, \
        patch.object(valley_delineation, "compute_flow_accumulation",
                     side_effect=AssertionError("a flow pass recomputed")) as flow, \
        patch.object(valley_delineation, "fill_and_resolve",
                     side_effect=AssertionError("a fill pass recomputed")) as fill:
    DERIVED = sd.derive(INPUTS)
assert sda.call_count == sda_layer1.call_count == geology.call_count == flow.call_count == fill.call_count == 0
# The four Layer 1 readings came off the session, populated.
assert INPUTS.soil_components and INPUTS.soil_geometries and INPUTS.farmland_classification
assert INPUTS.erosion_factor and INPUTS.saturated_hydraulic_conductivity
print(f"   0 SDA calls, 0 geology calls, 0 terrain passes; Layer 1 gave "
      f"{len(INPUTS.soil_components)} component rows, {len(INPUTS.soil_geometries)} polygons, "
      f"{len(INPUTS.erosion_factor)} K rows, {len(INPUTS.saturated_hydraulic_conductivity)} Ksat rows")

# ======================================================================
# 3. One partition, not two
# ======================================================================
print("3. the map-unit partition is Water's own, cell for cell")

WATER_INPUTS = wd.water_inputs_from_context(CONTEXT, DOCUMENT, DATA)
WATER_CELLS = wd.grid_bookkeeping(WATER_INPUTS)
WATER_HYDRIC = wd.derive_hydric(WATER_INPUTS, WATER_CELLS)
assert (DERIVED.cells["on_parcel"] == WATER_CELLS["on_parcel"]).all()
assert DERIVED.cells["on_parcel_count"] == WATER_CELLS["on_parcel_count"]
assert DERIVED.cells["cell_acres"] == WATER_CELLS["cell_acres"]
for mukey, unit in WATER_HYDRIC["map_units"].items():
    assert DERIVED.map_units[mukey]["cells"] == unit["cells"], mukey
assert set(DERIVED.map_units) == set(WATER_HYDRIC["map_units"])
assert sum(u["cells"] for u in DERIVED.map_units.values()) == DERIVED.cells["on_parcel_count"], \
    "every on-parcel cell falls in exactly one map unit"
print(f"   {len(DERIVED.map_units)} map units over {DERIVED.cells['on_parcel_count']} cells, identical to Water's")

# ======================================================================
# 4. Every figure, tabled
# ======================================================================
print("4. every figure against step 0, every acreage column summing to the cover")

PARCEL_ACRES = DERIVED.cells["on_parcel_count"] * DERIVED.cells["cell_acres"]
assert abs(PARCEL_ACRES - INPUTS.parcel_acres) < 0.05, (PARCEL_ACRES, INPUTS.parcel_acres)
COVER = round(INPUTS.parcel_acres, 1)

# THE MAP UNIT TABLE, measured live in step 0.
EXPECTED = {
    #  musym: (cells, drainage, hydgrp, ksat, capability, farmland starts with, K, T)
    "GvD": (722, "Moderately well drained", "C/D", 9.17, "4e", "Not prime", 0.37, 5.0),
    "EvC": (497, "Moderately well drained", "C/D", 9.17, "3e", "Farmland of statewide", 0.37, 4.0),
    "WhC": (397, "Moderately well drained", "C/D", 9.0, "3e", "Farmland of statewide", 0.32, 5.0),
    "RycC": (358, "Well drained", "B", 9.0, "3e", "Farmland of statewide", 0.37, 4.0),
    "GlD": (145, "Well drained", "C", 9.17, "4e", "Not prime", 0.37, 3.0),
    "At": (14, "Poorly drained", "B/D", 9.0, "3w", "Not prime", 0.32, 5.0),
    "GSF": (10, "Well drained", "C", 9.17, "7e", "Not prime", 0.20, 2.0),
}
assert [DERIVED.map_units[m]["musym"] for m in DERIVED.order] == list(EXPECTED), \
    [DERIVED.map_units[m]["musym"] for m in DERIVED.order]
for mukey in DERIVED.order:
    unit = DERIVED.map_units[mukey]
    cells, drainage, hydgrp, ksat, capability, farmland, kwfact, tfact = EXPECTED[unit["musym"]]
    assert unit["cells"] == cells, (unit["musym"], unit["cells"])
    assert unit["drainage_class"] == drainage and unit["hydrologic_group"] == hydgrp, unit["musym"]
    assert unit["ksat_um_s"] == ksat, unit["musym"]
    assert sd.capability_label(unit["capability_class"], unit["capability_subclass"]) == capability
    assert unit["farmland"].startswith(farmland), unit["farmland"]
    assert abs(DERIVED.erosion[mukey]["kwfact"] - kwfact) < 1e-9, unit["musym"]
    assert DERIVED.erosion[mukey]["tfact"] == tfact, unit["musym"]
    assert unit["muname"] and unit["geometry_utm"] is not None

# THE ACREAGE COLUMN SUMS TO THE COVER, through the shared allocator.
acres, percents = sd.acre_values(DERIVED, [DERIVED.map_units[m]["cells"] for m in DERIVED.order])
assert abs(sum(acres) - COVER) < 1e-9, (sum(acres), COVER)
assert abs(sum(percents) - 100.0) < 1e-9, sum(percents)
assert acres == [4.4, 3.1, 2.4, 2.2, 0.9, 0.1, 0.1], acres
assert percents == [33.7, 23.2, 18.5, 16.7, 6.8, 0.6, 0.5], percents

# PHYSICAL PROPERTIES: the dominant major component's surface horizon, its
# depth per row because the depth is not the same twice.
PROPERTIES = {
    #  musym: (compname, top, bottom, texdesc, texcl, sand, silt, clay, awc, om, ph)
    "GvD": ("Guernsey", 0.0, 18.0, "Silt loam", "Silt loam", 24.0, 51.0, 25.0, 0.22, 2.0, 5.9),
    "EvC": ("Ernest", 0.0, 15.0, "Silt loam", "Silt loam", 22.3, 59.7, 18.0, 0.17, 3.0, 5.3),
    "WhC": ("Wharton", 0.0, 23.0, "Silt loam", "Silt loam", 29.0, 53.0, 18.0, 0.23, 2.5, 5.6),
    "RycC": ("Rayne", 0.0, 24.0, "Silt loam", "Silt loam", 15.0, 69.0, 16.0, 0.15, 2.5, 5.0),
    "GlD": ("Gilpin", 0.0, 8.0, "Silt loam", "Silt loam", 16.0, 70.0, 14.0, 0.21, 3.9, 5.3),
    "At": ("Atkins", 0.0, 20.0, "Silt loam", "Silt loam", 22.0, 55.0, 23.0, 0.23, 3.0, 5.7),
    # The O horizon is skipped, so this one's surface does not start at zero.
    "GSF": ("Gilpin", 3.0, 20.0, "Channery silt loam", "Silt loam", 30.0, 56.0, 14.0, 0.18, 3.9, 5.3),
}
for mukey in DERIVED.order:
    block = DERIVED.properties[mukey]
    expected = PROPERTIES[DERIVED.map_units[mukey]["musym"]]
    got = (block["compname"], block["top_cm"], block["bottom_cm"], block["texture"], block["texture_class"],
           block["sand_pct"], block["silt_pct"], block["clay_pct"], block["awc"], block["om_pct"], block["ph"])
    assert got == expected, (DERIVED.map_units[mukey]["musym"], got, expected)
depths = {(b["top_cm"], b["bottom_cm"]) for b in DERIVED.properties.values()}
assert len(depths) > 1, "the surface horizon's depth is stated per row because it is not one depth"
assert (3.0, 20.0) in depths, "the unit with an O horizon on top starts below the surface"

# DEPTH TO BEDROCK: a depth where the survey reached rock, a BOUND where
# it did not, never a blank.
BEDROCK = {"GvD": (152.0, False, "Lithic bedrock"), "EvC": (183.0, True, None), "WhC": (175.0, False, "Paralithic bedrock"),
           "RycC": (114.0, False, "Paralithic bedrock"), "GlD": (76.0, False, "Paralithic bedrock"),
           "At": (152.0, True, None), "GSF": (84.0, False, "Lithic bedrock")}
for mukey in DERIVED.order:
    block = DERIVED.properties[mukey]
    assert (block["bedrock_cm"], block["bedrock_is_bound"], block["bedrock_kind"]) == \
        BEDROCK[DERIVED.map_units[mukey]["musym"]], DERIVED.map_units[mukey]["musym"]
    assert block["bedrock_cm"] is not None

# THE FRAGIPAN. The one non-bedrock restriction on the parcel, on the unit
# whose bedrock column reads as open ground -- and muaggatt's own rollup
# for that unit says 152 cm, which is the reading this note exists to stop.
notes = sd.restriction_notes(DERIVED.properties, DERIVED.map_units)
assert len(notes) == 1, notes
note = notes[0]
assert (note["musym"], note["compname"], note["kind"], note["depth_cm"]) == ("EvC", "Ernest", "Fragipan", 71.0), note
assert note["bedrock_is_bound"] and note["bedrock_cm"] == 183.0
assert DERIVED.map_units[note["mukey"]]["cells"] == 497, "the unit it qualifies is 3.1 acres, not a sliver"

# THE PROFILE TOTAL, a different depth basis and so never in that table.
assert {DERIVED.map_units[m]["musym"]: DERIVED.properties[m]["aws0150_cm"] for m in DERIVED.order} == \
    {"GvD": 20.52, "EvC": 18.51, "WhC": 21.7, "RycC": 14.77, "GlD": 12.87, "At": 29.85, "GSF": 9.66}

# CLASSIFICATION BY ACRES.
assert DERIVED.capability["labels"] == ["3e", "3w", "4e", "7e"], DERIVED.capability["labels"]
assert DERIVED.capability["counts"] == {"3e": 1252, "3w": 14, "4e": 867, "7e": 10}
capability_acres, capability_pct = sd.acre_values(DERIVED, [DERIVED.capability["counts"][l] for l in DERIVED.capability["labels"]])
assert abs(sum(capability_acres) - COVER) < 1e-9 and abs(sum(capability_pct) - 100.0) < 1e-9
assert capability_acres == [7.7, 0.1, 5.3, 0.1], capability_acres
assert DERIVED.farmland["values"] == ["Farmland of statewide importance", "Not prime farmland"]
assert DERIVED.farmland["counts"] == {"Farmland of statewide importance": 1252, "Not prime farmland": 891}
farmland_acres, farmland_pct = sd.acre_values(DERIVED, [DERIVED.farmland["counts"][v] for v in DERIVED.farmland["values"]])
assert abs(sum(farmland_acres) - COVER) < 1e-9 and abs(sum(farmland_pct) - 100.0) < 1e-9
assert farmland_acres == [7.7, 5.5], farmland_acres
assert sum(DERIVED.capability["counts"].values()) == sum(DERIVED.farmland["counts"].values()) \
    == DERIVED.cells["on_parcel_count"], "both classifications partition the same ground"

# TEXTURE LEADS: the base class, not the modified phrase, so the channery
# unit does not split one headline into two.
headline = sd.texture_headline(DERIVED.properties, DERIVED.map_units)
assert headline == {"texture": "Silt loam", "cells": 2143, "units": 7}, headline

# GEOLOGY AS A VALUE.
units = DERIVED.geology["units"]
assert DERIVED.geology["straddles"] and len(units) == 2
assert [u["name"] for u in units] == ["Casselman Formation", "Glenshaw Formation"]
assert units[0]["at_centroid"] and not units[1]["at_centroid"], "the unit under the centre leads"
assert [u["label"] for u in units] == ["Pcc", "Pcg"]
assert all(u["age"] == "Pennsylvanian" and u["province"] == "Appalachian Plateau" for u in units)
assert units[0]["age_max_ma"] == 323.6 and units[0]["age_min_ma"] == 299.05
assert units[0]["lithologies"] == ["clastic", "shale"] and units[0]["description"].startswith("Cyclic sequences of shale")
assert DERIVED.survey_areas == [{"areasymbol": "PA003", "saverest": "9/5/2025 12:33:41 PM"}]
assert DERIVED.representative_only is True
print(f"   7 units summing to {sum(acres):.1f} ac = the cover; capability {capability_acres}; farmland {farmland_acres}; "
      f"headline {headline['texture']}; {units[0]['name']} at the centroid")

# ======================================================================
# 5. The rules, on cases the reference parcel does not carry
# ======================================================================
print("5. the dominant rule, the bound, the orderings and the degraded layers")

SYNTHETIC = ss.parse_survey([
    # A unit whose dominant MAJOR is not its first-listed component, and
    # whose largest component is a MINOR one: the major rule wins.
    {"mukey": "1", "musym": "Zz", "areasymbol": "XX001", "saverest": "1/1/2025", "aws0150wta": "10",
     "cokey": "a", "compname": "Minor", "comppct_r": "60", "majcompflag": "No", "nirrcapcl": "2", "nirrcapscl": "s",
     "tfact": "5", "hzname": "Ap", "hzdept_r": "0", "hzdepb_r": "10", "sandtotal_r": "80", "silttotal_r": "10",
     "claytotal_r": "10", "awc_r": "0.05", "om_r": "1", "ph1to1h2o_r": "7.1", "texdesc": "Loamy sand",
     "texcl": "Loamy sand", "described_bottom_cm": "150", "bedrock_cm": None, "bedrock_kind": None,
     "restriction_cm": None, "restriction_kind": None},
    {"mukey": "1", "musym": "Zz", "areasymbol": "XX001", "saverest": "1/1/2025", "aws0150wta": "10",
     "cokey": "b", "compname": "Major", "comppct_r": "40", "majcompflag": "Yes", "nirrcapcl": "6", "nirrcapscl": None,
     "tfact": "2", "hzname": "A", "hzdept_r": "0", "hzdepb_r": "12", "sandtotal_r": "20", "silttotal_r": "60",
     "claytotal_r": "20", "awc_r": "0.18", "om_r": "4", "ph1to1h2o_r": "5.0", "texdesc": "Silt loam",
     "texcl": "Silt loam", "described_bottom_cm": "150", "bedrock_cm": None, "bedrock_kind": None,
     "restriction_cm": "40", "restriction_kind": "Cemented horizon"},
    # A component the survey describes no non-O horizon for: a real state.
    {"mukey": "2", "musym": "Yy", "areasymbol": "XX001", "saverest": "1/1/2025", "aws0150wta": None,
     "cokey": "c", "compname": "Bare", "comppct_r": "100", "majcompflag": "Yes", "nirrcapcl": None,
     "nirrcapscl": None, "tfact": None, "hzname": None, "hzdept_r": None, "hzdepb_r": None, "sandtotal_r": None,
     "silttotal_r": None, "claytotal_r": None, "awc_r": None, "om_r": None, "ph1to1h2o_r": None, "texdesc": None,
     "texcl": None, "described_bottom_cm": None, "bedrock_cm": "30", "bedrock_kind": "Lithic bedrock",
     "restriction_cm": None, "restriction_kind": None},
])
assert ss.dominant_major(SYNTHETIC, "1")["compname"] == "Major", "a 60% MINOR does not carry the row"
assert ss.dominant_major(SYNTHETIC, "2")["compname"] == "Bare"
assert SYNTHETIC["components"]["c"]["horizon"] is None, "no non-O horizon is None, not an empty dict"
assert [c["compname"] for c in ss.major_components(SYNTHETIC, "1")] == ["Major"]
assert ss.dominant_major(SYNTHETIC, "missing") is None

SYNTHETIC_UNITS = {
    "1": {"mukey": "1", "musym": "Zz", "muname": "Zz", "cells": 100, "geometry_utm": None, "drainage_class": None,
          "hydrologic_group": None, "ksat_um_s": None, "capability_class": "6", "capability_subclass": None,
          "farmland": None, "dominant_compname": "Major", "dominant_comppct": 40.0},
    "2": {"mukey": "2", "musym": "Yy", "muname": "Yy", "cells": 50, "geometry_utm": None, "drainage_class": None,
          "hydrologic_group": None, "ksat_um_s": None, "capability_class": None, "capability_subclass": None,
          "farmland": "All areas are prime farmland", "dominant_compname": "Bare", "dominant_comppct": 100.0},
}
SYNTHETIC_INPUTS = sd.SoilsInputs(
    dem={}, boundary_polygon_utm=None, soil_components=[], soil_geometries={}, farmland_classification=[],
    erosion_factor=[], saturated_hydraulic_conductivity=[], parcel_acres=1.0, retrieved_on=INPUTS.retrieved_on,
    soil_survey=SYNTHETIC, bedrock_geology=None,
)
synthetic_properties = sd.derive_properties(SYNTHETIC_INPUTS, SYNTHETIC_UNITS)
# The bound: no bedrock described, so the described bottom stands in and says so.
assert synthetic_properties["1"]["bedrock_cm"] == 150.0 and synthetic_properties["1"]["bedrock_is_bound"]
# A real depth is not a bound.
assert synthetic_properties["2"]["bedrock_cm"] == 30.0 and not synthetic_properties["2"]["bedrock_is_bound"]
# A component with no horizon carries the depths and no properties, never a crash.
assert synthetic_properties["2"]["texture"] is None and synthetic_properties["2"]["ph"] is None
# A non-bedrock restriction shallower than the bedrock column is noted; one deeper is not.
synthetic_notes = sd.restriction_notes(synthetic_properties, SYNTHETIC_UNITS)
assert [(n["musym"], n["kind"], n["depth_cm"]) for n in synthetic_notes] == [("Zz", "Cemented horizon", 40.0)]
deeper = {"1": dict(synthetic_properties["1"], bedrock_cm=20.0, bedrock_is_bound=False)}
assert sd.restriction_notes(deeper, SYNTHETIC_UNITS) == [], "a restriction below the rock is not a shallower limit"

# Capability ordering, and the classes the reference parcel does not carry.
ordered = sd.derive_capability({
    "a": dict(SYNTHETIC_UNITS["1"], capability_class="7", capability_subclass="e", cells=1),
    "b": dict(SYNTHETIC_UNITS["1"], capability_class="2", capability_subclass="w", cells=2),
    "c": dict(SYNTHETIC_UNITS["1"], capability_class="2", capability_subclass="e", cells=3),
    "d": dict(SYNTHETIC_UNITS["1"], capability_class=None, capability_subclass=None, cells=4),
    "e": dict(SYNTHETIC_UNITS["1"], capability_class="10", capability_subclass="x", cells=5),
})
assert ordered["labels"] == ["2e", "2w", "7e", "10x", sd.NO_DATA], ordered["labels"]
assert sd.capability_label("4", None) == "4" and sd.capability_label(None, "e") == sd.NO_DATA
# Farmland ordering: prime first, unclassified last, and "not classified"
# is not the same answer as "Not prime farmland".
farm = sd.derive_farmland({
    "a": dict(SYNTHETIC_UNITS["1"], farmland="Not prime farmland", cells=1),
    "b": dict(SYNTHETIC_UNITS["1"], farmland="All areas are prime farmland", cells=2),
    "c": dict(SYNTHETIC_UNITS["1"], farmland="Prime farmland if drained", cells=3),
    "d": dict(SYNTHETIC_UNITS["1"], farmland=None, cells=4),
})
assert farm["values"] == ["All areas are prime farmland", "Prime farmland if drained", "Not prime farmland",
                          sd.NOT_CLASSIFIED], farm["values"]
assert sd.NOT_CLASSIFIED != "Not prime farmland"

# The texture headline picks the class on the most GROUND, not in the most units.
wide = {"1": dict(synthetic_properties["1"], texture_class="Silt loam"),
        "2": dict(synthetic_properties["2"], texture_class="Loam")}
assert sd.texture_headline(wide, SYNTHETIC_UNITS)["texture"] == "Silt loam"
assert sd.texture_headline({}, {}) is None

# THE DEGRADED LAYERS. Without the survey, the section still has the
# polygons, the acreages and the four Layer 1 readings; what it loses is
# the symbols, the properties and the capability -- never a crash.
DEGRADED = sd.derive(sd.SoilsInputs(**{**INPUTS.__dict__, "soil_survey": None, "bedrock_geology": None}))
assert DEGRADED.properties == {} and DEGRADED.geology is None and DEGRADED.survey_areas == []
assert len(DEGRADED.map_units) == 7 and DEGRADED.cells["on_parcel_count"] == DERIVED.cells["on_parcel_count"]
assert all(u["musym"] is None and u["capability_class"] is None for u in DEGRADED.map_units.values())
assert all(u["drainage_class"] and u["hydrologic_group"] and u["ksat_um_s"] and u["farmland"]
           for u in DEGRADED.map_units.values()), "the Layer 1 readings survive the report layer degrading"
assert all(e["kwfact"] is not None and e["tfact"] is None for e in DEGRADED.erosion.values()), "K survives, T does not"
assert DEGRADED.capability["labels"] == [sd.NO_DATA]
degraded_acres, _ = sd.acre_values(DEGRADED, [DEGRADED.map_units[m]["cells"] for m in DEGRADED.order])
assert abs(sum(degraded_acres) - COVER) < 1e-9, "the acreage column still sums to the cover"
# The geology layer answering that it maps nothing is a no-data answer.
try:
    bedrock_geology.parse_geology({"envelope": [], "centroid": None, "units": {}})
except Exception as exc:  # noqa: BLE001
    raise AssertionError(f"an empty block must parse, not raise: {exc}")
assert bedrock_geology.parse_geology({"envelope": [], "centroid": None, "units": {}}) == \
    {"units": [], "at_centroid": None, "straddles": False}
print("   dominant-major, bound, restriction, orderings, headline and both degraded layers hold")

# ======================================================================
# 6. No advice
# ======================================================================
print("6. the block describes the soil that is there: no management language")

WORDS = " ".join(str(v) for v in (
    [u["muname"] for u in DERIVED.map_units.values()]
    + [u["farmland"] for u in DERIVED.map_units.values()]
    + [b["texture"] for b in DERIVED.properties.values()]
    + [n["kind"] for n in notes]
    + [u["description"] for u in units] + [u["name"] for u in units]
    + [ss.SSURGO_CITATION, bedrock_geology.SGMC_CITATION, bedrock_geology.SGMC_USE_CONSTRAINTS]
)).lower()
BANNED = (r"\bamend", r"\blime\b", r"\bliming", r"\bfertilis", r"\bfertiliz", r"\bmanure", r"\bcompost",
          r"\bapply\b", r"\bapplication rate", r"\btill(age|ing)?\b", r"\bcrop\b", r"\bplant\b", r"\bsow\b",
          r"\brecommend", r"\bshould\b", r"\bsuitab", r"\bimprove", r"\bmanage(ment)?\b", r"\bremediat",
          r"\bprescrib", r"\btreat(ment)?\b", r"\byield\b", r"\bproductiv")
found = [p for p in BANNED if re.search(p, WORDS)]
assert not found, f"management language in the block's own words: {found}"
print(f"   {len(BANNED)} patterns, none found")

print("\ntest_soils_derivations.py: all sections passed")
print(offline_harness.summary())
