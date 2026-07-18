"""
test_erosion_hydric_soil.py

Offline (no-network, mocked SDA response) checks for soil_data.py's
erosion-factor and hydric helpers — the SSURGO-based erosion-prone-soil
and floodplain/hydric-soil exclusion signals road_corridors.py uses.

Also a regression test for a real bug — two rounds of it. Round 1:
get_erosion_factor_for_polygon() originally selected c.kwfact directly
off the component table, but kwfact is a chorizon-table field (per-
horizon, not per-component). Round 2: the fix for that joined chorizon
on cokey and filtered to "ch.rvindicator = 'Yes'", assuming the same
representative-row convention used elsewhere in SSURGO applied to
chorizon itself — it doesn't; rvindicator only exists on chorizon's
child tables (chtexturegrp, chstructgrp, etc.), not chorizon. Both
versions got an HTTP 400 from SDA every single time, live against a real
property. Because the mocked _run_sda_query below doesn't validate
against a real schema, a test that only checks the *parsed result* shape
(as this file originally did) would have kept passing straight through
both bugs — mocking away the network call also mocks away SDA's own
schema validation. So this file asserts the actual SQL text takes the
correct, documented shape (chorizon joined on cokey, representative
horizon selected by shallowest depth via a correlated MIN(hzdept_r)
subquery, per a real published SDA example query) AND explicitly asserts
neither previous wrong guess (c.kwfact, ch.rvindicator) is present — the
same kind of "check the query, not just the result" regression guard
test_soil_geometry_scope.py uses for its own bug.
"""

from unittest.mock import patch

import soil_data

boundary = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
wkt_polygon = soil_data.coordinates_to_wkt_polygon(boundary)

mocked_result = {
    "columns": ["mukey", "muname", "compname", "comppct_r", "kwfact"],
    "rows": [
        ["111", "Rayne silt loam", "Rayne", 65, "0.37"],  # erosion-prone
        ["111", "Rayne silt loam", "Cavode", 25, "0.20"],  # not dominant -- should be dropped
        ["222", "Gilpin channery loam", "Gilpin", 80, "0.28"],  # not erosion-prone
        ["333", "Wheeling silt loam", "Wheeling", 90, None],  # missing kwfact
    ],
}

captured = {}


def fake_run_sda_query(sql, max_retries=2):
    captured["sql"] = sql
    return mocked_result


with patch.object(soil_data, "_run_sda_query", fake_run_sda_query):
    erosion_data = soil_data.get_erosion_factor_for_polygon(wkt_polygon)

sql_sent = captured["sql"]
assert "kwfact" in sql_sent
assert "chorizon" in sql_sent, "kwfact lives on chorizon, not component -- the query must join it in"
assert "cokey" in sql_sent, "the chorizon join must be keyed on cokey"
assert "hzdept_r" in sql_sent and "MIN(hzdept_r)" in sql_sent, (
    "must select the representative horizon by shallowest depth (a correlated MIN(hzdept_r) "
    "subquery) -- the documented SDA pattern; chorizon has no representative-row flag column"
)
assert "hzname" in sql_sent, (
    "should exclude organic surface horizons (hzname NOT LIKE 'O%') in favor of the first "
    "mineral horizon -- see get_erosion_factor_for_polygon()'s docstring for why"
)
assert "c.kwfact" not in sql_sent, (
    "regression guard (round 1): c.kwfact (component.kwfact) doesn't exist in SSURGO's schema"
)
assert "rvindicator" not in sql_sent, (
    "regression guard (round 2): rvindicator doesn't exist on chorizon itself, only on "
    "chorizon's child tables (chtexturegrp, chstructgrp, etc.) -- this was the second wrong guess"
)
print("SQL correctly selects the shallowest mineral horizon per component via chorizon (not component.kwfact or chorizon.rvindicator).")

assert len(erosion_data) == 3, f"expected one dominant-component row per mukey (3 mukeys), got {len(erosion_data)}"

by_mukey = {row["mukey"]: row for row in erosion_data}
assert by_mukey["111"]["compname"] == "Rayne", "should keep the dominant (comppct_r DESC first) component per mukey"
assert by_mukey["111"]["kwfact"] == "0.37"
print("get_erosion_factor_for_polygon returns one dominant-component row per mukey.")

assert soil_data.is_erosion_prone(by_mukey["111"]["kwfact"]) is True
assert soil_data.is_erosion_prone(by_mukey["222"]["kwfact"]) is False
assert soil_data.is_erosion_prone(by_mukey["333"]["kwfact"]) is False, "missing/unparseable kwfact should not be treated as erosion-prone"
assert soil_data.is_erosion_prone("0.32") is True, "the threshold itself should count as erosion-prone (>=)"
assert soil_data.is_erosion_prone(None) is False
print("is_erosion_prone correctly classifies erosion-prone, non-erosion-prone, and missing values.")

assert soil_data.is_hydric("Yes") is True
assert soil_data.is_hydric("Partially hydric") is True
assert soil_data.is_hydric("No") is False
assert soil_data.is_hydric(None) is False
assert soil_data.is_hydric("") is False
print("is_hydric correctly classifies hydric, partially-hydric, and non-hydric values.")

print("\nAll erosion/hydric soil checks passed.")
