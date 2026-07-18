"""
test_nlcd_classification.py

Offline test for nlcd_data.py's pure classification/vectorization logic —
no network access required (Planetary Computer's STAC API isn't reachable
from this sandbox; confirmed separately, same restriction as USGS/USDA).

This is the ground-truth check the NLCD integration exists for: the
property this pipeline was built against had NDVI-threshold classification
mislabel a pasture area as "dense vegetation (tree canopy/forest)" during
peak growing season, because NDVI measures vigor, not type, and a
healthy hayfield can score identically to forest canopy. NLCD is a
pre-classified dataset that distinguishes Pasture/Hay (code 81) from
Deciduous/Evergreen/Mixed Forest (codes 41/42/43) directly — this test
builds a synthetic raster with both cover types actually present and
confirms nlcd_data.py keeps them as separate, correctly-labeled classes
instead of merging them into one ambiguous bucket the way the old
NDVI-only approach would have.

Also confirms the vectorized output is schema-conformant GeoJSON with
"high" confidence and NLCD-specific (not NDVI-ambiguity) confidence_notes.
"""

import numpy as np
from affine import Affine
from shapely.geometry import Polygon

import nlcd_data
from feature_schema import validate_feature_collection

# A 10x10 synthetic land-cover raster: majority pasture, minority forest —
# same shape as the real ground-truth property (predominantly open pasture
# with some tree cover), at a scale (0.001 deg/pixel, ~111m) that keeps this
# test fast without needing NLCD's real 30m grid or real projection math.
landcover = np.zeros((10, 10), dtype=np.uint8)
landcover[1:7, 1:9] = 81  # Pasture/Hay
landcover[7:9, 1:9] = 41  # Deciduous Forest

transform = Affine.translation(-79.99, 40.65) * Affine.scale(0.001, -0.001)
boundary_polygon = Polygon(
    [(-79.99, 40.65), (-79.98, 40.65), (-79.98, 40.64), (-79.99, 40.64), (-79.99, 40.65)]
)

# --- _classify_landcover: percent breakdown, no network ---
classes = nlcd_data._classify_landcover(landcover)
names = {c["name"] for c in classes}
assert names == {"Pasture/Hay", "Deciduous Forest"}, (
    f"expected exactly Pasture/Hay and Deciduous Forest, got {names}"
)

dominant = max(classes, key=lambda c: c["pct_of_boundary"])
assert dominant["name"] == "Pasture/Hay", (
    f"dominant class should be Pasture/Hay (majority of the synthetic raster), "
    f"got {dominant['name']} — this is exactly the type of mislabeling NLCD "
    f"replaces NDVI-threshold guessing to avoid"
)
print(f"Classification: {classes}")
print("Pasture and forest correctly identified as separate classes, dominant class correct.")

# --- _vectorize_landcover: polygonize + merge + clip to boundary ---
geometries_by_code = nlcd_data._vectorize_landcover(
    landcover, transform, "EPSG:4326", boundary_polygon
)
assert set(geometries_by_code.keys()) == {81, 41}, (
    f"expected geometry for both pasture (81) and forest (41) codes, "
    f"got {set(geometries_by_code.keys())}"
)
print("Vectorization produced separate geometry for pasture and forest — not merged.")

# --- Build schema-conformant features exactly as get_nlcd_features_geojson does ---
from feature_schema import make_feature, make_feature_collection, CONFIDENCE_HIGH

confidence_notes = nlcd_data.NLCD_CONFIDENCE_NOTES_TEMPLATE.format(year="2021")
features = [
    make_feature(
        feature_id=f"nlcd-2021-{code}",
        geometry=geom,
        layer="land-cover",
        label=nlcd_data.NLCD_CLASSES[code],
        confidence=CONFIDENCE_HIGH,
        confidence_notes=confidence_notes,
        extra_properties={"nlcd_code": code},
    )
    for code, geom in geometries_by_code.items()
]
collection = make_feature_collection(features)
validate_feature_collection(collection)

for feature in collection["features"]:
    assert feature["properties"]["confidence"] == "high"
    notes = feature["properties"]["confidence_notes"]
    assert "30m" in notes, "confidence_notes should state NLCD's resolution limitation"
    assert "tell" not in notes.lower() and "ambiguous" not in notes.lower(), (
        "confidence_notes should describe NLCD's own limitations (resolution, "
        "survey currency), not the old NDVI type-ambiguity hedge"
    )
print("Every feature is schema-valid, high confidence, with NLCD-specific confidence_notes.")

print("\nAll NLCD classification checks passed.")
