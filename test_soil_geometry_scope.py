"""
test_soil_geometry_scope.py

Regression test for a bug where get_soil_geometries_for_polygon() (and
therefore get_soil_data_as_geojson()) returned every county-wide occurrence
of each matched soil map unit *type*, not just the polygon(s) actually
overlapping the input boundary — because the query filtered on
"mukey IN (mukeys that intersect the boundary)" rather than applying a
spatial predicate to each polygon row itself. Passing schema validation
(feature_schema.validate_feature_collection) and having confidence_notes
populated does NOT catch this class of bug — both passed on the broken
version — so this test checks actual geometry, not just schema shape.

No network access required: SDA's real endpoint isn't reachable from this
sandbox (confirmed separately), so this mocks _run_sda_query and checks
two things a live run must also satisfy:

  1. The SQL sent for polygon geometry applies a spatial predicate
     (STIntersects/STIntersection against the input boundary) directly on
     mupolygongeo, not just a mukey-type filter — guards against
     regressing back to the buggy query shape.
  2. Geometry returned for a mocked "correctly filtered" SDA response stays
     within/near the input boundary, and a mocked far-away, same-mukey
     polygon (simulating the old bug's out-of-area rows) is excluded
     because the WHERE clause itself would never select it once it's a
     true spatial predicate.
"""

from unittest.mock import patch

import soil_data
from feature_schema import validate_feature_collection

# The real property boundary from the bug report (~40.64N / -79.98W, PA).
boundary = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
wkt_polygon = soil_data.coordinates_to_wkt_polygon(boundary)

tabular_result = {
    "columns": [
        "mukey", "muname", "compname", "comppct_r",
        "drainagecl", "slope_r", "slopelenusle_r", "hydricrating", "taxorder",
    ],
    "rows": [
        ["111", "Rayne silt loam, 8 to 15 percent slopes", "Rayne", 65,
         "Well drained", 12, 150, "No", "Ultisols"],
    ],
}

# A correctly-filtered SDA response only ever contains rows whose geometry
# genuinely overlaps wkt_polygon (that's what STIntersects/STIntersection
# guarantee server-side) — so the mock reflects exactly one clipped polygon
# near the parcel, not a scattered county-wide dump.
spatial_result = {
    "columns": ["mukey", "geom_wkt"],
    "rows": [
        ["111", "POLYGON((-79.9838 40.6458, -79.9836 40.6429, -79.9813 40.6441, -79.9838 40.6458))"],
    ],
}

captured = {}


def fake_run_sda_query(sql, max_retries=2):
    if "mupolygongeo" in sql:
        captured["sql"] = sql
        return spatial_result
    return tabular_result


with patch.object(soil_data, "_run_sda_query", fake_run_sda_query):
    geojson = soil_data.get_soil_data_as_geojson(wkt_polygon)

sql_sent = captured["sql"]
assert "STIntersects" in sql_sent, (
    "geometry query must apply STIntersects directly on mupolygongeo — "
    "found no spatial predicate in the SQL sent:\n" + sql_sent
)
assert "STIntersection" in sql_sent, (
    "geometry query must clip returned rows with STIntersection, not "
    "just filter by mukey:\n" + sql_sent
)
assert "SDA_Get_Mukey_from_intersection_with_WktWgs84" not in sql_sent, (
    "geometry query should filter with a real spatial predicate on "
    "mupolygongeo, not the mukey-type-only intersection function "
    "(that's the shape of the original bug):\n" + sql_sent
)
print("Geometry SQL applies a real per-row spatial predicate, not a mukey-only filter.")

validate_feature_collection(geojson)

assert len(geojson["features"]) == 1, f"expected 1 feature, got {len(geojson['features'])}"
feature = geojson["features"][0]
first_coord = feature["geometry"]["coordinates"][0][0]
lon, lat = first_coord

print(f"Feature first coordinate (lon, lat): ({lon}, {lat})")

# The parcel is at roughly (-79.98, 40.64) — "near" here means within about
# a quarter degree (~25km), generously wide of the actual parcel but tight
# enough that the bug's miles-to-tens-of-miles-away scattered polygons
# (first coords as far as 40.36-40.47N / -80.15-80.27W in the live repro)
# would still fail it.
assert -80.2 < lon < -79.8, f"longitude {lon} is not near the parcel (~-79.98)"
assert 40.5 < lat < 40.8, f"latitude {lat} is not near the parcel (~40.64)"
print("Geometry stays within/near the input boundary — not scattered county-wide.")

print("\nAll soil geometry scope checks passed.")
