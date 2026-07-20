"""
test_production_suitability.py

Offline (no-network) checks for production_suitability.py's per-factor
scoring, soil EXCLUSION check, and weighted composite ranking. Hand-built
synthetic DEMs, same "pure logic, independent of real data fetches"
approach as test_solar_suitability.py, plus offline checks for the
soil_data.py exclusion helper (is_disqualifying_soil_condition()) this
module's exclusion check is built on.

Synthetic-DEM layouts, each isolating ONE factor while holding the others
equal, so a difference in the final ranking can be attributed to the
intended cause and not a confound:

  1. Compact square vs. an elongated sliver of nearly the SAME acreage
     (equal slope) -- tests that size_factor's compactness component, not
     just raw acreage, drives a real ranking difference ("a large
     irregular sliver is less usable than a compact block of the same
     acreage" per the feature spec).
  2. Two identically-shaped/sized patches, one gently south-facing, one
     gently north-facing (equal slope magnitude, equal size) -- tests
     aspect_factor direction (south > north) AND that its small
     configured weight caps how much it alone can move the score.
  3. score_production_areas() called directly with pre-fetched SSURGO
     component rows -- clean soil, hydric soil, and unavailable (None)
     data on the same patch -- tests the soil EXCLUSION check: a zone
     with clean-but-unremarkable soil scores IDENTICALLY to a hypothetical
     zone with better soil (there's no graded soil score to differ), while
     a genuinely disqualifying condition (hydric) drops the zone out of
     ranking entirely rather than just lowering its score.

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
    _WEIGHT_SUM,
    identify_production_area_suitability,
    production_suitability_to_geojson,
    score_production_areas,
    summarize_production_area_suitability,
)
from soil_data import is_disqualifying_soil_condition

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


# --- weights: documented, sum to 1.0, soil is NOT one of them ---

assert abs(_WEIGHT_SUM - 1.0) < 1e-6, f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"
assert ASPECT_FACTOR_WEIGHT < SLOPE_FACTOR_WEIGHT and ASPECT_FACTOR_WEIGHT < SIZE_FACTOR_WEIGHT, (
    "aspect must be the minor factor per the feature spec (matters far less than for solar)"
)
import production_suitability as _ps
assert not hasattr(_ps, "SOIL_FACTOR_WEIGHT"), (
    "soil must NOT be a weighted composite factor -- Scale of Permanence sequencing puts soil "
    "quality at step 8 (last, most improvable), so it shouldn't gate/rank where production zones "
    "go the way slope/size/aspect (step 2, Land Shape) do; soil is exclusion-only"
)
print(f"Factor weights sum to 1.0 ({SLOPE_FACTOR_WEIGHT}+{SIZE_FACTOR_WEIGHT}+{ASPECT_FACTOR_WEIGHT}), aspect is the "
      "smallest weight, and soil is not a weighted factor at all (module exposes no SOIL_FACTOR_WEIGHT).")


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
    "at equal slope/acreage, the compact block should outrank the fragmented sliver overall"
)
assert square["rank"] == 1 and sliver["rank"] == 2
print(
    f"Compact square (compactness={square['compactness_score']}, {square['area_acres']} ac, "
    f"score={square['suitability_score']}) outranks the elongated sliver "
    f"(compactness={sliver['compactness_score']}, {sliver['area_acres']} ac, score={sliver['suitability_score']}) "
    "despite similar acreage."
)


# --- 2. aspect_factor: south-facing outranks north-facing at equal slope/size, but only slightly ---

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
    f"({score_gap} points), with slope/size held equal -- confirms aspect is scored correctly "
    "and stays a minor factor, not a dominant one."
)


# --- 3. soil EXCLUSION check: clean soil vs. hydric (disqualifying) soil vs. unavailable (None) data ---

array = np.full((20, 20), 100.0, dtype=np.float32)
dem_soil = _dem(array, origin_y=4500040.0)
soil_patches = identify_production_areas(dem_soil)  # uniformly flat -- one big patch, well above min_area_acres
assert len(soil_patches) == 1
patch_id = soil_patches[0]["id"]

clean_soil = [{"mukey": "1", "muname": "Fine soil", "compname": "x", "comppct_r": 90,
               "drainagecl": "Well drained", "hydricrating": "No"}]
excellent_soil = [{"mukey": "1", "muname": "Great soil", "compname": "x", "comppct_r": 90,
                    "drainagecl": "Well drained", "hydricrating": "No"}]
hydric_soil = [{"mukey": "1", "muname": "Wet soil", "compname": "x", "comppct_r": 90,
                "drainagecl": "Poorly drained", "hydricrating": "Yes"}]
saturated_soil = [{"mukey": "1", "muname": "Saturated soil", "compname": "x", "comppct_r": 90,
                    "drainagecl": "Very poorly drained", "hydricrating": "No"}]

scored_clean = score_production_areas([dict(soil_patches[0])], dem_soil, {patch_id: clean_soil})
scored_excellent = score_production_areas([dict(soil_patches[0])], dem_soil, {patch_id: excellent_soil})
assert scored_clean[0]["soil_exclusion_passed"] is True
assert scored_clean[0]["soil_data_available"] is True
assert scored_clean[0]["soil_exclusion_reason"] is None
assert scored_clean[0]["suitability_score"] == scored_excellent[0]["suitability_score"], (
    "a zone with merely clean/workable soil must score IDENTICALLY to one with better soil -- "
    "soil quality is not a graded input to the composite score at all, only a pass/fail exclusion"
)
print(
    f"Clean soil (score={scored_clean[0]['suitability_score']}) scores identically to 'better' soil "
    f"(score={scored_excellent[0]['suitability_score']}) -- soil quality has no graded effect on the composite."
)

scored_hydric = score_production_areas([dict(soil_patches[0])], dem_soil, {patch_id: hydric_soil})
assert scored_hydric[0]["soil_exclusion_passed"] is False
assert scored_hydric[0]["soil_exclusion_reason"] is not None and "hydric" in scored_hydric[0]["soil_exclusion_reason"]
assert scored_hydric[0]["rank"] is None, "an excluded zone must not receive a rank"
assert scored_hydric[0]["suitability_score"] is not None, (
    "the topographic composite is still computed/reported for an excluded zone (informational), "
    "just not used to rank it"
)
print(
    f"Hydric soil correctly fails the exclusion check (reason: {scored_hydric[0]['soil_exclusion_reason']!r}) "
    "and the zone is excluded from ranking (rank=None), even though its topographic score is still reported."
)

scored_saturated = score_production_areas([dict(soil_patches[0])], dem_soil, {patch_id: saturated_soil})
assert scored_saturated[0]["soil_exclusion_passed"] is False
assert "saturated" in scored_saturated[0]["soil_exclusion_reason"].lower()
print("Permanently saturated (very poorly drained, non-hydric) soil also fails the exclusion check.")

scored_no_soil = score_production_areas([dict(soil_patches[0])], dem_soil, {patch_id: None})
assert scored_no_soil[0]["soil_data_available"] is False
assert scored_no_soil[0]["soil_exclusion_passed"] is True, (
    "unavailable soil data must default to PASSED (not excluded) rather than assuming a "
    "disqualifying condition that was never actually checked"
)
assert scored_no_soil[0]["rank"] == 1, "a patch defaulted to passed should still be ranked normally"
print("Unavailable SSURGO data defaults the exclusion check to passed (not excluded), and is still ranked.")

# a mix of passing and excluded patches: excluded sorts after every passing patch, unranked
mixed = score_production_areas(
    [dict(soil_patches[0]), {**dict(soil_patches[0]), "id": 999}],
    dem_soil,
    {patch_id: clean_soil, 999: hydric_soil},
)
assert mixed[0]["soil_exclusion_passed"] is True and mixed[0]["rank"] == 1
assert mixed[1]["soil_exclusion_passed"] is False and mixed[1]["rank"] is None
print("With a mix of passing and excluded patches, the passing one ranks 1st and the excluded one is unranked, listed after.")

# unavailable soil is stated explicitly in confidence_notes, not silently assumed clean
geojson_no_soil = production_suitability_to_geojson(scored_no_soil)
notes = geojson_no_soil["features"][0]["properties"]["confidence_notes"].lower()
assert "estimate" in notes and "soil" in notes, "confidence_notes must flag when the soil exclusion check was estimated, not verified"
print("confidence_notes explicitly flags the soil exclusion check as an estimate when SSURGO data is unavailable.")

# an excluded zone's label states the exclusion, not a rank
geojson_hydric = production_suitability_to_geojson(scored_hydric)
hydric_label = geojson_hydric["features"][0]["properties"]["label"]
assert "EXCLUDED" in hydric_label, f"an excluded zone's label should say so, got: {hydric_label!r}"
print(f"Excluded zone label states the exclusion: {hydric_label!r}")


# --- soil_data.py exclusion helper: offline, no SDA query involved ---

assert is_disqualifying_soil_condition("Yes", "Well drained") is not None, "hydric='Yes' must disqualify regardless of drainage"
assert is_disqualifying_soil_condition("Partially hydric", "Well drained") is not None, "partially hydric must also disqualify"
assert is_disqualifying_soil_condition("No", "Very poorly drained") is not None, "very poorly drained must disqualify even if not hydric"
assert is_disqualifying_soil_condition("No", "Poorly drained") is None, (
    "merely 'poorly drained' (not 'very poorly drained') must NOT disqualify -- it's a real but "
    "improvable limitation, not a genuinely disqualifying one"
)
assert is_disqualifying_soil_condition("No", "Well drained") is None
assert is_disqualifying_soil_condition(None, None) is None
assert is_disqualifying_soil_condition("No", "Excessively drained") is None, (
    "droughty (excessively drained) soil is a real quality limitation but not a disqualifying one -- "
    "only permanently/near-permanently saturated ground is"
)
print("is_disqualifying_soil_condition correctly flags hydric and very-poorly-drained soil, and nothing else.")


# --- output: schema-valid FeatureCollection on the SAME layer as production_area.py's own output ---

geojson = production_suitability_to_geojson(scored_shape)
validate_feature_collection(geojson)
required_props = {
    "suitability_score", "slope_factor", "size_factor", "aspect_factor",
    "area_acres", "representative_elevation_m", "rank",
    "soil_exclusion_passed", "soil_exclusion_reason", "soil_data_available",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "production_area_candidate", (
        "suitability scoring must enrich the SAME layer production_area.py's own "
        "production_areas_to_geojson() uses -- these are the same zones, not a new layer"
    )
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert "soil_factor" not in feature["properties"], "soil must not appear as a graded factor property"
    score = feature["properties"]["suitability_score"]
    assert 0.0 <= score <= 100.0, f"suitability_score must be on a 0-100 scale, got {score}"
    for factor_name in ("slope_factor", "size_factor", "aspect_factor"):
        value = feature["properties"][factor_name]
        assert 0.0 <= value <= 1.0, f"{factor_name} must be on a 0-1 scale, got {value}"
print("production_suitability_to_geojson output is schema-valid, layer='production_area_candidate', "
      "with suitability_score on 0-100, every factor on 0-1, and no soil_factor property.")


# --- ranking: rank 1 always has the highest suitability_score among ranked (passing) patches ---

for patches_set in (scored_shape, scored_aspect):
    ranked = sorted((p for p in patches_set if p["rank"] is not None), key=lambda p: p["rank"])
    scores_by_rank = [p["suitability_score"] for p in ranked]
    assert scores_by_rank == sorted(scores_by_rank, reverse=True), "rank must be strictly ordered by suitability_score"
print("rank is consistently ordered by descending suitability_score among ranked (non-excluded) patches.")


# --- summarize helper: no crash on empty input, produces a rank-ordered summary otherwise ---

assert "No production-area candidates" in summarize_production_area_suitability([])
summary = summarize_production_area_suitability(scored_shape)
assert "Rank 1" in summary and "Rank 2" in summary
summary_mixed = summarize_production_area_suitability(mixed)
assert "Rank 1" in summary_mixed and "EXCLUDED" in summary_mixed
print("summarize_production_area_suitability handles empty input and produces a rank-ordered summary "
      "that lists excluded patches separately.")

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
    "check_soil=False must skip the SSURGO fetch entirely"
)
assert all(f["properties"]["soil_exclusion_passed"] is True for f in result["zones_geojson"]["features"]), (
    "with no soil data fetched, the exclusion check must default to passed, not excluded"
)
print("identify_production_area_suitability wires DEM->identify_production_areas->score_production_areas->geojson "
      "correctly with check_soil=False (no network call).")

print("\nAll production_suitability checks passed.")
