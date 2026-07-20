"""
test_production_suitability.py

Offline (no-network) checks for production_suitability.py's per-factor
scoring and weighted composite ranking. Hand-built synthetic DEMs, same
"pure logic, independent of real data fetches" approach as
test_solar_suitability.py, plus offline checks for the new
soil_data.py scoring helpers (farmland_classification_score(),
drainage_class_score()) this module's soil_factor is built on.

Three synthetic-DEM layouts, each isolating ONE factor while holding the
others equal, so a difference in the final ranking can be attributed to
the intended cause and not a confound:

  1. Compact square vs. an elongated sliver of nearly the SAME acreage
     (equal slope, equal soil) -- tests that size_factor's compactness
     component, not just raw acreage, drives a real ranking difference
     ("a large irregular sliver is less usable than a compact block of
     the same acreage" per the feature spec).
  2. Two identically-shaped/sized patches, one gently south-facing, one
     gently north-facing (equal slope magnitude, equal size, equal soil)
     -- tests aspect_factor direction (south > north) AND that its small
     configured weight caps how much it alone can move the score.
  3. score_production_areas() called directly with pre-fetched SSURGO
     rows (real soil signal) vs. None (data unavailable) for the same
     patch -- tests soil_factor's availability handling and the
     confidence_notes caveat, without a network call.

Every synthetic DEM here uses a very steep background ramp
(elevation = 1000 + (row+col)*constant) so that ONLY the deliberately
carved flat patches qualify as production-area candidates at all --
a uniform flat background (as in production_area.py's own simpler test)
would itself read as zero-slope and merge into one giant patch, which
would defeat the point of isolating two separate, comparable regions.
"""

import numpy as np

from feature_schema import validate_feature_collection
from production_area import identify_production_areas
from production_suitability import (
    ASPECT_FACTOR_WEIGHT,
    SIZE_FACTOR_WEIGHT,
    SLOPE_FACTOR_WEIGHT,
    SOIL_FACTOR_WEIGHT,
    _WEIGHT_SUM,
    identify_production_area_suitability,
    production_suitability_to_geojson,
    score_production_areas,
    summarize_production_area_suitability,
)
from soil_data import drainage_class_score, farmland_classification_score

RESOLUTION = (5.0, 5.0)
CRS = "EPSG:32617"


def _steep_background(rows: int, cols: int) -> np.ndarray:
    array = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            array[r, c] = 1000.0 + (r + c) * 200.0  # ~10,000%+ grade -- always excluded
    return array


def _dem(array: np.ndarray, origin_y: float = 4500120.0) -> dict:
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": origin_y,
        "crs": CRS,
    }


# --- weights: documented, sum to 1.0 (composite score is meaningless otherwise) ---

assert abs(_WEIGHT_SUM - 1.0) < 1e-6, f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"
assert ASPECT_FACTOR_WEIGHT < SLOPE_FACTOR_WEIGHT and ASPECT_FACTOR_WEIGHT < SOIL_FACTOR_WEIGHT, (
    "aspect must be the minor factor per the feature spec (matters far less than for solar)"
)
print(f"Factor weights sum to 1.0 ({SLOPE_FACTOR_WEIGHT}+{SOIL_FACTOR_WEIGHT}+{SIZE_FACTOR_WEIGHT}+{ASPECT_FACTOR_WEIGHT}), aspect is the smallest weight.")


# --- 1. size_factor: compact square outranks an elongated sliver of similar acreage ---

rows, cols = 60, 80
array = _steep_background(rows, cols)
for r in range(3, 17):        # compact 14x14 square
    for c in range(3, 17):
        array[r, c] = 100.0
for r in range(3, 10):        # elongated 7x28 sliver, similar total cell count
    for c in range(30, 58):
        array[r, c] = 100.0

dem_shape = _dem(array)
shape_patches = identify_production_areas(dem_shape)  # default min_area_acres -- both regions clear it
assert len(shape_patches) == 2, f"expected 2 isolated patches, got {len(shape_patches)}"

scored_shape = score_production_areas(shape_patches, dem_shape)
square = next(p for p in scored_shape if p["compactness_score"] == max(x["compactness_score"] for x in scored_shape))
sliver = next(p for p in scored_shape if p is not square)

assert abs(square["area_acres"] - sliver["area_acres"]) / square["area_acres"] < 0.15, (
    "the two regions should be close in acreage -- the test isolates SHAPE, not size"
)
assert square["compactness_score"] > sliver["compactness_score"], (
    "the compact square should score more compact than the elongated sliver"
)
assert square["size_factor"] > sliver["size_factor"], "higher compactness should lift size_factor at equal acreage"
assert square["suitability_score"] > sliver["suitability_score"], (
    "at equal slope/soil/acreage, the compact block should outrank the fragmented sliver overall"
)
assert square["rank"] == 1 and sliver["rank"] == 2
print(
    f"Compact square (compactness={square['compactness_score']}, {square['area_acres']} ac, "
    f"score={square['suitability_score']}) outranks the elongated sliver "
    f"(compactness={sliver['compactness_score']}, {sliver['area_acres']} ac, score={sliver['suitability_score']}) "
    "despite similar acreage."
)


# --- 2. aspect_factor: south-facing outranks north-facing at equal slope/size/soil, but only slightly ---

rows, cols = 60, 80
array = _steep_background(rows, cols)
for r in range(3, 17):
    for c in range(3, 17):
        array[r, c] = 100.0 - (r - 3) * 0.1  # south-facing, ~5% grade
for r in range(3, 17):
    for c in range(30, 44):
        array[r, c] = 100.0 + (r - 3) * 0.1  # north-facing, ~5% grade (mirrored)

dem_aspect = _dem(array)
aspect_patches = identify_production_areas(dem_aspect)
assert len(aspect_patches) == 2

scored_aspect = score_production_areas(aspect_patches, dem_aspect)
south = next(p for p in scored_aspect if p["aspect_deg"] is not None and abs(p["aspect_deg"] - 180.0) < 1.0)
north = next(p for p in scored_aspect if p["aspect_deg"] is not None and abs(p["aspect_deg"] - 0.0) < 1.0)

assert south["aspect_factor"] == 1.0, f"due-south aspect should score aspect_factor 1.0, got {south['aspect_factor']}"
assert north["aspect_factor"] == 0.0, f"due-north aspect should score aspect_factor 0.0, got {north['aspect_factor']}"
assert abs(south["slope_factor"] - north["slope_factor"]) < 1e-6, "the two regions have mirrored, equal-magnitude slope"
assert abs(south["size_factor"] - north["size_factor"]) < 1e-6, "the two regions are identically shaped/sized"
assert south["suitability_score"] > north["suitability_score"], "south-facing should outrank north-facing overall"

score_gap = south["suitability_score"] - north["suitability_score"]
max_possible_gap_from_aspect_alone = ASPECT_FACTOR_WEIGHT * 100
assert score_gap <= max_possible_gap_from_aspect_alone + 0.1, (
    f"with every other factor equal, the score gap ({score_gap}) should be bounded by aspect_factor's own "
    f"weight ({max_possible_gap_from_aspect_alone}) -- aspect must stay a MINOR factor, not dominate the ranking"
)
print(
    f"South-facing (aspect_factor=1.0, score={south['suitability_score']}) outranks north-facing "
    f"(aspect_factor=0.0, score={north['suitability_score']}) by exactly the aspect weight's worth "
    f"({score_gap} points), with slope/size/soil held equal -- confirms aspect is scored correctly "
    "and stays a minor factor, not a dominant one."
)


# --- 3. soil_factor: real SSURGO data vs. unavailable (None) data on the same patch ---

array = np.full((20, 20), 100.0, dtype=np.float32)
dem_soil = _dem(array, origin_y=4500040.0)
soil_patches = identify_production_areas(dem_soil)  # uniformly flat -- one big patch, well above min_area_acres
assert len(soil_patches) == 1
patch_id = soil_patches[0]["id"]

prime_farmland = [{"mukey": "1", "muname": "Best soil", "farmland_classification": "All areas are prime farmland"}]
well_drained = [{"mukey": "1", "muname": "Best soil", "compname": "x", "comppct_r": 90, "drainagecl": "Well drained"}]

scored_with_soil = score_production_areas(
    [dict(soil_patches[0])], dem_soil, {patch_id: prime_farmland}, {patch_id: well_drained}
)
assert scored_with_soil[0]["soil_data_available"] is True
assert scored_with_soil[0]["soil_factor"] == 1.0, (
    f"prime farmland + well-drained should score soil_factor 1.0, got {scored_with_soil[0]['soil_factor']}"
)

scored_no_soil = score_production_areas(
    [dict(soil_patches[0])], dem_soil, {patch_id: None}, {patch_id: None}
)
assert scored_no_soil[0]["soil_data_available"] is False
assert scored_no_soil[0]["soil_factor"] == 0.5, (
    f"unavailable soil data should default soil_factor to the neutral 0.5, got {scored_no_soil[0]['soil_factor']}"
)
assert scored_with_soil[0]["suitability_score"] > scored_no_soil[0]["suitability_score"], (
    "real prime/well-drained soil data should score higher than the neutral default"
)
print(
    f"soil_factor uses real SSURGO data when available (prime + well-drained -> {scored_with_soil[0]['soil_factor']}) "
    f"and defaults to the neutral 0.5 when unavailable ({scored_no_soil[0]['soil_factor']})."
)

# unavailable soil is stated explicitly in confidence_notes, not silently defaulted
geojson_no_soil = production_suitability_to_geojson(scored_no_soil)
notes = geojson_no_soil["features"][0]["properties"]["confidence_notes"].lower()
assert "estimated" in notes and "soil" in notes, "confidence_notes must flag when soil_factor was estimated, not measured"
print("confidence_notes explicitly flags soil_factor as estimated when SSURGO data is unavailable.")


# --- soil_data.py scoring helpers: offline, no SDA query involved ---

assert farmland_classification_score("All areas are prime farmland") == 1.0
assert farmland_classification_score("Prime farmland if drained") == 0.75
assert farmland_classification_score("Farmland of statewide importance") == 0.6
assert farmland_classification_score("Not prime farmland") == 0.2
assert farmland_classification_score(None) == 0.5
assert farmland_classification_score("") == 0.5
assert (
    farmland_classification_score("All areas are prime farmland")
    > farmland_classification_score("Farmland of statewide importance")
    > farmland_classification_score("Not prime farmland")
), "farmland score ordering must follow SSURGO's own prime > statewide > not-prime hierarchy"
print("farmland_classification_score maps SSURGO farmlndcl values to a correctly-ordered 0-1 scale.")

assert drainage_class_score("Well drained") == 1.0
assert drainage_class_score("Moderately well drained") == 0.85
assert drainage_class_score("Very poorly drained") == 0.1
assert drainage_class_score(None) == 0.5
assert drainage_class_score("Unrecognized value") == 0.5
assert drainage_class_score("Well drained") > drainage_class_score("Excessively drained"), (
    "well drained should score above the droughty extreme"
)
assert drainage_class_score("Well drained") > drainage_class_score("Very poorly drained"), (
    "well drained should score above the waterlogged extreme"
)
assert drainage_class_score("Excessively drained") > drainage_class_score("Very poorly drained"), (
    "both drainage extremes are penalized, but the mapping isn't symmetric/monotonic -- it's a real, "
    "documented per-class table, not a single linear function of one drainage axis"
)
print("drainage_class_score penalizes both drainage extremes relative to well/moderately-well drained soil.")


# --- output: schema-valid FeatureCollection on the SAME layer as production_area.py's own output ---

geojson = production_suitability_to_geojson(scored_shape)
validate_feature_collection(geojson)
required_props = {
    "suitability_score", "slope_factor", "soil_factor", "size_factor", "aspect_factor",
    "area_acres", "representative_elevation_m", "rank",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "production_area_candidate", (
        "suitability scoring must enrich the SAME layer production_area.py's own "
        "production_areas_to_geojson() uses -- these are the same zones, not a new layer"
    )
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    score = feature["properties"]["suitability_score"]
    assert 0.0 <= score <= 100.0, f"suitability_score must be on a 0-100 scale, got {score}"
    for factor_name in ("slope_factor", "soil_factor", "size_factor", "aspect_factor"):
        value = feature["properties"][factor_name]
        assert 0.0 <= value <= 1.0, f"{factor_name} must be on a 0-1 scale, got {value}"
print("production_suitability_to_geojson output is schema-valid, layer='production_area_candidate', "
      "with suitability_score on 0-100 and every factor on 0-1.")


# --- ranking: rank 1 always has the highest suitability_score ---

for patches_set in (scored_shape, scored_aspect):
    by_rank = sorted(patches_set, key=lambda p: p["rank"])
    scores_by_rank = [p["suitability_score"] for p in by_rank]
    assert scores_by_rank == sorted(scores_by_rank, reverse=True), "rank must be strictly ordered by suitability_score"
print("rank is consistently ordered by descending suitability_score across every scored set.")


# --- summarize helper: no crash on empty input, produces a rank-ordered summary otherwise ---

assert "No production-area candidates" in summarize_production_area_suitability([])
summary = summarize_production_area_suitability(scored_shape)
assert "Rank 1" in summary and "Rank 2" in summary
print("summarize_production_area_suitability handles empty input and produces a rank-ordered summary.")

# --- identify_production_area_suitability: full orchestrator wiring, network-free via dem=+check_soil=False ---

boundary = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
result = identify_production_area_suitability(boundary, dem=dem_shape, check_soil=False)
assert result["zones_geojson"]["features"], "expected at least one scored feature from the orchestrator"
assert all(f["properties"]["soil_data_available"] is False for f in result["zones_geojson"]["features"]), (
    "check_soil=False must skip the SSURGO fetch entirely -- soil_factor should be the neutral default"
)
print("identify_production_area_suitability wires DEM->identify_production_areas->score_production_areas->geojson "
      "correctly with check_soil=False (no network call).")

print("\nAll production_suitability checks passed.")
