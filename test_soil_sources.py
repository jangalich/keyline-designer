"""
test_soil_sources.py

THE SOILS & GEOLOGY SECTION'S TWO NEW FETCH MODULES, offline: the
requests each makes (mocked at the HTTP call, every parameter
inspected), the shapes each parser returns on the CAPTURED REAL
responses and on synthetic ones, and what each does under the refused
network.

  1. SSURGO (soil_survey): the one query's SELECT list and its joins --
     the surface-horizon rule, the representative texture filter, the
     bedrock/not-bedrock split; the parser's map units, components and
     survey areas on the captured rows; a component with no non-O
     horizon; the dominant-major rule.
  2. SGMC (bedrock_geology): the WFS call asks for ATTRIBUTES ONLY and
     in the urn CRS's latitude-first bbox order; ONE call when the
     envelope maps one unit, a second at the centroid when it maps more;
     the GML parser; the unit records; an envelope that maps nothing
     raises GeologyIncompleteError, which is a no-data answer and not an
     outage.
  3. Under the refused network both fetches raise a RequestException at
     once, with attempts published.
"""

import re
from unittest import mock

import requests

import offline_harness

offline_harness.install()

import bedrock_geology  # noqa: E402
import fetch_attempts  # noqa: E402
import soil_data  # noqa: E402
import soil_survey as ss  # noqa: E402
import soils_reference_fixture as fixture  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402

WKT = soil_data.coordinates_to_wkt_polygon(REAL_BOUNDARY)

# ======================================================================
# 1. SSURGO: the survey query
# ======================================================================
print("1. soil_survey: one query, the surface-horizon rule, the parsed block")

SQL = ss.survey_sql(WKT)
squashed = re.sub(r"\s+", " ", SQL)
# The columns the section cannot be built without.
for column in ("mu.musym", "ma.aws0150wta", "c.nirrcapcl", "c.nirrcapscl", "c.tfact", "ch.sandtotal_r",
               "ch.silttotal_r", "ch.claytotal_r", "ch.awc_r", "ch.om_r", "ch.ph1to1h2o_r", "cg.texdesc",
               "ct.texcl", "sc.saverest", "l.areasymbol"):
    assert column in squashed, column
# The columns Layer 1 already holds are NOT re-selected.
for column in ("drainagecl", "hydgrp", "farmlndcl", "kwfact", "ksat_r"):
    assert column not in squashed, f"{column} is already on ParcelData and must not be fetched again"
# The representative texture group only: without this filter two of the
# reference parcel's units return a second, non-representative texture.
assert "cg.rvindicator = 'Yes'" in squashed
# The bedrock split: one predicate, used with both senses, so no reskind
# can fall in both buckets or in neither.
# Four uses of one predicate: depth and kind for the bedrock, depth and
# kind for whatever is not bedrock. Writing the negative sense out by
# hand is how a reskind ends up in both buckets or in neither.
assert squashed.count(ss.BEDROCK_PREDICATE) == 4, squashed.count(ss.BEDROCK_PREDICATE)
assert squashed.count(f"NOT ({ss.BEDROCK_PREDICATE})") == 2
assert ss.SURFACE_HORIZON_PREDICATE.replace("  ", " ") in squashed.replace("  ", " ")
# The map unit filter is the same intersection every soil query uses.
assert "SDA_Get_Mukey_from_intersection_with_WktWgs84" in squashed and WKT in SQL

# ONE query per fetch, and the WKT reaches it. The name is patched on
# soil_survey, not soil_data: every SSURGO module here binds
# _run_sda_query at import, the way soil_water_table and soil_woodland do.
with mock.patch.object(ss, "_run_sda_query", return_value={"columns": ["mukey"], "rows": []}) as run:
    assert ss.get_survey_for_boundary(REAL_BOUNDARY) == []
assert run.call_count == 1 and WKT in run.call_args[0][0]

ROWS = fixture.load("soil_survey.json")
BLOCK = ss.parse_survey(ROWS)
assert len(ROWS) == 30 and len(BLOCK["components"]) == 30 and len(BLOCK["map_units"]) == 7
assert BLOCK["survey_areas"] == [{"areasymbol": "PA003", "saverest": "9/5/2025 12:33:41 PM"}]
assert {u["musym"] for u in BLOCK["map_units"].values()} == {"At", "EvC", "GSF", "GlD", "GvD", "RycC", "WhC"}
assert sum(len(u["components"]) for u in BLOCK["map_units"].values()) == 30, "every component belongs to one unit"
# Types: numbers are numbers, not the strings SDA sends.
sample = ss.dominant_major(BLOCK, [m for m, u in BLOCK["map_units"].items() if u["musym"] == "RycC"][0])
assert sample["compname"] == "Rayne" and sample["comppct"] == 90.0 and sample["major"] is True
assert sample["horizon"]["awc"] == 0.15 and sample["horizon"]["texture"] == "Silt loam"
assert sample["tfact"] == 4.0 and sample["capability_class"] == "3" and sample["capability_subclass"] == "e"
assert sample["bedrock_cm"] == 114.0 and sample["bedrock_kind"] == "Paralithic bedrock"
# The one component the survey describes no non-O horizon for.
bare = [c for c in BLOCK["components"].values() if c["horizon"] is None]
assert len(bare) == 1 and bare[0]["compname"] == "Ernest" and not bare[0]["major"]
# Majors are a subset of components, ordered largest first.
for mukey in BLOCK["map_units"]:
    majors = ss.major_components(BLOCK, mukey)
    assert majors, mukey
    assert [c["comppct"] for c in majors] == sorted((c["comppct"] for c in majors), reverse=True)
    assert ss.dominant_major(BLOCK, mukey) is majors[0]
# An empty answer parses to an empty block rather than raising.
assert ss.parse_survey([]) == {"map_units": {}, "components": {}, "survey_areas": []}
assert ss.parse_survey([{"mukey": None, "cokey": None}])["components"] == {}
print(f"   {len(ROWS)} rows -> {len(BLOCK['map_units'])} units, {len(BLOCK['components'])} components, "
      f"1 with no non-O horizon; no Layer 1 column re-selected")

# ======================================================================
# 2. SGMC: the geology fetch
# ======================================================================
print("2. bedrock_geology: attributes only, latitude-first bbox, one call when one unit")

RAW = fixture.load("bedrock_geology.json")


def _response(text):
    reply = mock.Mock()
    reply.text = text
    reply.raise_for_status = mock.Mock()
    reply.json = mock.Mock(return_value={})
    return reply


def _collection(members):
    body = "".join(
        "<wfs:member><ms:Lithology>"
        + "".join(f"<ms:{k}>{v}</ms:{k}>" for k, v in member.items() if v is not None)
        + "</ms:Lithology></wfs:member>"
        for member in members
    )
    return ('<?xml version="1.0"?><wfs:FeatureCollection xmlns:ms="http://mapserver.gis.umn.edu/mapserver" '
            'xmlns:wfs="http://www.opengis.net/wfs/2.0">' + body + "</wfs:FeatureCollection>")


ONE = [{"state": "PA", "orig_label": "Pcc", "sgmc_label": "PAcc;6", "unit_link": "PAPAcc;6", "ref_id": "PA002",
        "generalize": "Sedimentary, clastic", "src_url": "http://example.invalid", "url": "http://example.invalid"}]
TWO = ONE + [{"state": "PA", "orig_label": "Pcg", "sgmc_label": "PAcg;6", "unit_link": "PAPAcg;6",
              "ref_id": "PA002", "generalize": "Sedimentary, clastic", "src_url": "http://example.invalid",
              "url": "http://example.invalid"}]

# ONE UNIT IN THE ENVELOPE -> ONE WFS CALL. The centroid query is only
# worth making when there is something to resolve.
calls = []


def _one_unit(url, params=None, **kwargs):
    calls.append((url, params))
    if url == bedrock_geology.SGMC_WFS:
        return _response(_collection(ONE))
    reply = mock.Mock()
    reply.raise_for_status = mock.Mock()
    reply.json = mock.Mock(return_value={"unit_name": "Casselman Formation", "unit_age": "Pennsylvanian",
                                         "province": "Appalachian Plateau", "unitdesc": "Shale. And more.",
                                         "age": [{"min_ma": "299.05", "max_ma": "323.6"}],
                                         "lith": [{"lith_rank": "Major", "low_lith": "Shale"}]})
    return reply


with mock.patch.object(bedrock_geology.requests, "get", side_effect=_one_unit):
    single = bedrock_geology.get_geology_for_boundary(REAL_BOUNDARY)
wfs_calls = [c for c in calls if c[0] == bedrock_geology.SGMC_WFS]
assert len(wfs_calls) == 1, "one unit in the envelope needs no centroid query"
assert single["centroid"] is None
params = wfs_calls[0][1]
# ATTRIBUTES ONLY: propertyName names every field and geometry is not
# among them -- the difference between 2 KB and 5 MB on this parcel.
assert params["propertyName"] == ",".join(bedrock_geology.SGMC_FIELDS)
assert "msGeometry" not in params["propertyName"] and params["typeNames"] == bedrock_geology.SGMC_LAYER
assert params["request"] == "GetFeature" and params["version"] == "2.0.0"
# THE URN CRS IS LATITUDE FIRST. Getting this backwards returns a bbox in
# the Indian Ocean rather than an error, so it is asserted numerically.
minx, miny = min(p[0] for p in REAL_BOUNDARY), min(p[1] for p in REAL_BOUNDARY)
maxx, maxy = max(p[0] for p in REAL_BOUNDARY), max(p[1] for p in REAL_BOUNDARY)
bbox = params["bbox"].split(",")
assert bbox[4] == "urn:ogc:def:crs:EPSG::4326"
assert [float(v) for v in bbox[:4]] == [miny, minx, maxy, maxx], params["bbox"]
single_block = bedrock_geology.parse_geology(single)
assert not single_block["straddles"] and len(single_block["units"]) == 1
assert single_block["units"][0]["name"] == "Casselman Formation" and single_block["at_centroid"] is None
assert single_block["units"][0]["description"] == "Shale", "the description is cut to its first clause"
assert single_block["units"][0]["lithologies"] == ["shale"]

# TWO UNITS -> a second call at the centroid, which is a degenerate bbox.
calls.clear()


def _two_units(url, params=None, **kwargs):
    calls.append((url, params))
    if url == bedrock_geology.SGMC_WFS:
        first = [c for c in calls if c[0] == bedrock_geology.SGMC_WFS]
        return _response(_collection(TWO if len(first) == 1 else [TWO[1]]))
    return _one_unit(url, params, **kwargs)


with mock.patch.object(bedrock_geology.requests, "get", side_effect=_two_units):
    straddling = bedrock_geology.get_geology_for_boundary(REAL_BOUNDARY)
wfs_calls = [c for c in calls if c[0] == bedrock_geology.SGMC_WFS]
assert len(wfs_calls) == 2, "more than one unit is resolved at the centroid"
centre_bbox = [float(v) for v in wfs_calls[1][1]["bbox"].split(",")[:4]]
span = 2 * bedrock_geology.POINT_BBOX_DEGREES
assert abs((centre_bbox[2] - centre_bbox[0]) - span) < 1e-12, centre_bbox
assert abs((centre_bbox[3] - centre_bbox[1]) - span) < 1e-12, centre_bbox
assert miny < centre_bbox[0] < maxy and minx < centre_bbox[1] < maxx, "the point query sits inside the parcel"
straddled = bedrock_geology.parse_geology(straddling)
assert straddled["straddles"] and straddled["at_centroid"] == "PAPAcg;6"
assert straddled["units"][0]["unit_link"] == "PAPAcg;6" and straddled["units"][0]["at_centroid"]
assert not straddled["units"][1]["at_centroid"], "the unit under the centre leads, the rest follow"

# THE CAPTURED REAL ANSWER, parsed.
real = bedrock_geology.parse_geology(RAW)
assert [u["label"] for u in real["units"]] == ["Pcc", "Pcg"] and real["straddles"]
assert real["units"][0]["name"] == "Casselman Formation" and real["units"][0]["at_centroid"]
assert real["units"][0]["age"] == "Pennsylvanian" and real["units"][0]["province"] == "Appalachian Plateau"
assert all(not u["description"].endswith((";", ".")) for u in real["units"]), \
    "the first clause carries no dangling terminator; the section punctuates its own line"
assert real["units"][0]["description"].endswith("thin, nonpersistent coal"), real["units"][0]["description"]
# The GML parser reads the service's own namespaced body.
members = bedrock_geology._parse_members(_collection(TWO))
assert [m["orig_label"] for m in members] == ["Pcc", "Pcg"] and set(members[0]) == set(bedrock_geology.SGMC_FIELDS)
assert bedrock_geology._parse_members(_collection([])) == []

# NOTHING MAPPED IS A NO-DATA ANSWER, not an outage: a retry will not help.
with mock.patch.object(bedrock_geology.requests, "get", side_effect=lambda *a, **k: _response(_collection([]))):
    try:
        bedrock_geology.get_geology_for_boundary(REAL_BOUNDARY)
    except bedrock_geology.GeologyIncompleteError:
        pass
    else:
        raise AssertionError("an empty envelope must raise GeologyIncompleteError")

# The terms this module prints come off the metadata record, and the scale
# is the record's, not the friendlier one the service advertises.
assert bedrock_geology.SGMC_INTENDED_SCALE == 1000000
assert "1:1,000,000" in bedrock_geology.SGMC_USE_CONSTRAINTS
assert bedrock_geology.SGMC_DOI in bedrock_geology.SGMC_CITATION
print(f"   1 call for one unit, 2 for two; bbox {bbox[:4]} latitude-first; "
      f"{len(real['units'])} units on the reference parcel, {real['units'][0]['name']} at the centroid")

# ======================================================================
# 3. The refused network
# ======================================================================
print("3. under the refused network both fetches raise at once, attempts published")

offline_harness.clear()
for label, call in (("soil_survey", lambda: ss.get_survey_for_boundary(REAL_BOUNDARY)),
                    ("bedrock_geology", lambda: bedrock_geology.get_geology_for_boundary(REAL_BOUNDARY))):
    before = len(offline_harness.refused())
    try:
        call()
    except requests.exceptions.RequestException:
        pass
    else:
        raise AssertionError(f"{label} did not raise under the refused network")
    tried = len(offline_harness.refused()) - before
    assert tried >= 1, f"{label} did not reach the network at all"
    assert tried <= 3, f"{label} retried {tried} times; the budget is one call plus two retries"

# ATTEMPTS ARE PUBLISHED by the module that owns the service.
# bedrock_geology reaches its own, so its entry point is decorated and
# reports what it burned. soil_survey shares soil_data's _run_sda_query
# with every other SSURGO query, exactly as soil_water_table,
# soil_road_ratings and soil_woodland do, and publishes nothing of its
# own -- asserted here so the shape is a decision on the record rather
# than an omission.
assert isinstance(bedrock_geology.LAST_FETCH_ATTEMPTS, int) and bedrock_geology.LAST_FETCH_ATTEMPTS >= 1
assert not fetch_attempts.publishes_attempts(ss.get_survey_for_polygon)
for module in (__import__("soil_water_table"), __import__("soil_road_ratings"), __import__("soil_woodland")):
    entry = [v for k, v in vars(module).items() if k.startswith("get_") and k.endswith("_for_polygon")]
    assert all(not fetch_attempts.publishes_attempts(f) for f in entry), module.__name__
print(f"   both raise RequestException within the retry budget; geology published "
      f"{bedrock_geology.LAST_FETCH_ATTEMPTS} attempts; no response is invented")

print("\ntest_soil_sources.py: all sections passed")
print(offline_harness.summary())
