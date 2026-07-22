"""
test_farmland_classification.py

Offline (no-network, mocked SDA response) checks for soil_data.py's
get_farmland_classification_for_polygon() and is_prime_farmland() —
the SSURGO-based prime-farmland conflict check solar_suitability.py uses
to flag (not exclude) high-scoring solar zones on prime agricultural soil.
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
    "columns": ["mukey", "muname", "farmlndcl"],
    "rows": [
        ["111", "Rayne silt loam, 3 to 8 percent slopes", "All areas are prime farmland"],
        ["222", "Gilpin-Upshur complex, 15 to 25 percent slopes", "Not prime farmland"],
        ["333", "Wheeling silt loam, 0 to 3 percent slopes, drained", "Prime farmland if drained"],
    ],
}

captured = {}


def fake_run_sda_query(sql, max_retries=2):
    captured["sql"] = sql
    return mocked_result


with patch.object(soil_data, "_run_sda_query", fake_run_sda_query):
    classifications = soil_data.get_farmland_classification_for_polygon(wkt_polygon)

assert "muaggatt" not in captured["sql"], (
    "farmlndcl lives on mapunit in SDA's real schema, not muaggatt -- joining muaggatt for this "
    "field is exactly the wrong-table bug that caused a real HTTP 400 live (confirmed against "
    "USDA's own published Advanced Queries SDA example, see get_farmland_classification_for_"
    "polygon()'s own docstring)"
)
assert "mapunit" in captured["sql"] and "farmlndcl" in captured["sql"]
assert len(classifications) == 3

by_mukey = {c["mukey"]: c for c in classifications}
assert by_mukey["111"]["farmland_classification"] == "All areas are prime farmland"
assert by_mukey["222"]["farmland_classification"] == "Not prime farmland"
assert by_mukey["333"]["farmland_classification"] == "Prime farmland if drained"
print("get_farmland_classification_for_polygon parses mukey/muname/farmland_classification correctly.")

assert soil_data.is_prime_farmland("All areas are prime farmland") is True
assert soil_data.is_prime_farmland("Prime farmland if drained") is True
assert soil_data.is_prime_farmland("Prime farmland if irrigated") is True
assert soil_data.is_prime_farmland("Not prime farmland") is False
assert soil_data.is_prime_farmland(None) is False
assert soil_data.is_prime_farmland("") is False
print("is_prime_farmland correctly classifies prime, conditionally-prime, and non-prime values.")

print("\nAll farmland classification checks passed.")
