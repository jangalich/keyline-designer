"""
test_feature_schema.py

Offline sanity check for feature_schema.py and its use in hydrology_data.py
and soil_data.py — no network access required (unlike hydrology_data.py's
and soil_data.py's own __main__ blocks, which hit real USGS/USDA
endpoints). Confirms:

  - a hand-built FeatureCollection using realistic hydrology + soil
    properties passes validate_feature_collection()
  - every feature has a non-empty confidence_notes
  - the whole thing round-trips through json.dumps/loads (i.e. is valid
    GeoJSON, not just a schema-shaped Python dict)
  - make_feature() rejects a missing/empty confidence_notes, since that's
    the one field this schema treats as non-negotiable
"""

import json

from feature_schema import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    make_feature,
    make_feature_collection,
    validate_feature_collection,
)

stream_feature = make_feature(
    feature_id="nhd-streams-1234567",
    geometry={
        "type": "LineString",
        "coordinates": [[-79.9838, 40.6458], [-79.9836, 40.6429]],
    },
    layer="hydrology-streams",
    label="Montour Run",
    confidence=CONFIDENCE_MEDIUM,
    confidence_notes=(
        "USGS NHD stream geometry is compiled at roughly 1:24,000 scale; "
        "small or seasonal drainages may be missing or mislocated."
    ),
    extra_properties={"gnis_id": "12345", "feature_code": 46006},
)

soil_feature = make_feature(
    feature_id="ssurgo-mukey-987654",
    geometry={
        "type": "Polygon",
        "coordinates": [
            [
                [-79.9838, 40.6458],
                [-79.9836, 40.6429],
                [-79.9813, 40.6441],
                [-79.9838, 40.6458],
            ]
        ],
    },
    layer="soil",
    label="Gilpin-Upshur complex, 15 to 25 percent slopes",
    confidence=CONFIDENCE_HIGH,
    confidence_notes=(
        "SSURGO map unit boundaries are digitized at roughly 1:24,000 "
        "scale and may not reflect field-level detail."
    ),
    extra_properties={"mukey": "987654", "drainage_class": "Well drained"},
)

collection = make_feature_collection([stream_feature, soil_feature])

validate_feature_collection(collection)
print(f"Schema validation passed for {len(collection['features'])} feature(s).")

# GeoJSON validity means it survives a JSON round-trip cleanly, not just
# that it happens to be a Python dict shaped like one.
round_tripped = json.loads(json.dumps(collection))
assert round_tripped == collection
print("JSON round-trip OK — output is valid GeoJSON.")

for feature in collection["features"]:
    notes = feature["properties"]["confidence_notes"]
    assert notes and notes.strip(), f"feature {feature['id']} has empty confidence_notes"
print("Every feature has a populated confidence_notes field.")

# confidence_notes is required, not optional — make_feature() should refuse
# to build a feature without one.
try:
    make_feature(
        feature_id="should-fail",
        geometry={"type": "Point", "coordinates": [0, 0]},
        layer="hydrology-streams",
        label="Missing notes",
        confidence=CONFIDENCE_HIGH,
        confidence_notes="",
    )
    raise AssertionError("make_feature() should have rejected an empty confidence_notes")
except ValueError:
    print("make_feature() correctly rejects an empty confidence_notes.")

print("\nAll feature_schema checks passed.")
