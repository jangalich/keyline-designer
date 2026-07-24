"""
test_production_suitability.py

Offline (no-network, and now no-soil-fetch-at-all) checks for
production_suitability.py -- STEP 4 of the consolidated production-zone
pipeline: advisory-only description of clusters STEP 3
(production_area.cluster_and_gate()) has already produced. This module no
longer does any carving, soil fetching, slope/aspect recomputation, or
mask recovery -- it purely averages STEP 1's already-computed per-cell
scores over each cluster's own cells and computes fresh compactness/size
(the one thing that can't exist before clustering).

Synthetic-DEM layouts, each isolating ONE thing while holding the others
equal:

  1. Compact square vs. an elongated sliver of nearly the SAME acreage --
     tests that size_factor's compactness component, not just raw
     acreage, drives a real ranking difference.
  2. Two identically-shaped/sized clusters, one gently south-facing, one
     gently north-facing -- tests aspect_factor direction and weight cap.
  3. A boundary that clips the compact square (from #1) down to roughly
     half its footprint -- tests that STEP 1/STEP 3 (production_area.py,
     unchanged here) still drive the correct on-parcel clipping this
     module's scoring reflects.
  4. Soil bookkeeping: STEP 1's soil_carved_acres_by_cell/
     soil_carved_pct_by_cell (already computed before this module ever
     runs) are correctly read and attached per cluster via its own
     source_patch_id -- not recomputed, not a second carving pass.

Every synthetic DEM using a steep background uses
elevation = 1000 + (row+col)*constant so that ONLY the deliberately
carved flat patches qualify as production-area candidates at all.
"""

import numpy as np
from shapely.geometry import box

from feature_schema import validate_feature_collection
from production_area import cluster_and_gate, compute_step1_eligible_cells
from production_suitability import (
    ASPECT_FACTOR_WEIGHT,
    SIZE_FACTOR_WEIGHT,
    SLOPE_FACTOR_WEIGHT,
    _WEIGHT_SUM,
    production_suitability_to_geojson,
    score_production_areas,
    summarize_production_area_suitability,
)

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


def _full_extent_boundary(dem: dict):
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    return box(x0, y0 - rows * py, x0 + cols * px, y0)


def _step1_and_patches(dem, boundary, disqualifying_soil_union_utm=None):
    step1 = compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=disqualifying_soil_union_utm)
    patches = cluster_and_gate(step1["eligible_mask"], dem, boundary, step1)
    return step1, patches


# --- weights: documented, sum to 1.0, soil is NOT one of them ---

assert abs(_WEIGHT_SUM - 1.0) < 1e-6, f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"
assert ASPECT_FACTOR_WEIGHT < SLOPE_FACTOR_WEIGHT and ASPECT_FACTOR_WEIGHT < SIZE_FACTOR_WEIGHT, (
    "aspect must be the minor factor per the feature spec (matters far less than for solar)"
)
import production_suitability as ps  # noqa: E402  (kept near use)

assert not hasattr(ps, "SOIL_FACTOR_WEIGHT"), (
    "soil must NOT be a weighted composite factor -- it's a hard STEP 1 exclusion, not a graded score"
)
assert not hasattr(ps, "_carve_soil_from_patch"), "the old continuous-geometry carving machinery must be gone"
assert not hasattr(ps, "_fetch_disqualifying_soil_union"), (
    "the soil fetch now lives in production_area.py (STEP 1 needs it before clustering) -- "
    "production_suitability.py must not still own a second copy"
)
print(f"Factor weights sum to 1.0 ({SLOPE_FACTOR_WEIGHT}+{SIZE_FACTOR_WEIGHT}+{ASPECT_FACTOR_WEIGHT}), aspect is the "
      "smallest weight, soil is not a weighted factor, and the old carving machinery is gone entirely.")


# --- 1. size_factor: compact square outranks an elongated sliver of similar acreage ---

rows, cols = 30, 90
array = _steep_background(rows, cols)
for r in range(3, 23):        # 20x20 square
    for c in range(3, 23):
        array[r, c] = 100.0
for r in range(3, 12):        # 9x51 sliver, close in area to the square
    for c in range(30, 81):
        array[r, c] = 100.0

dem_shape = _dem(array)
full_extent_shape = _full_extent_boundary(dem_shape)
step1_shape, shape_patches = _step1_and_patches(dem_shape, full_extent_shape)
assert len(shape_patches) == 2, f"expected 2 isolated clusters, got {len(shape_patches)}"

scored_shape = score_production_areas(shape_patches, dem_shape, step1_shape)
square = next(p for p in scored_shape if p["compactness_score"] == max(x["compactness_score"] for x in scored_shape))
sliver = next(p for p in scored_shape if p is not square)

assert abs(square["area_acres"] - sliver["area_acres"]) / square["area_acres"] < 0.15, (
    "the two regions should be close in acreage -- the test isolates SHAPE, not size"
)
assert square["compactness_score"] > sliver["compactness_score"]
assert square["size_factor"] > sliver["size_factor"], "higher compactness should lift size_factor at equal acreage"
assert square["suitability_score"] > sliver["suitability_score"]
assert square["rank"] == 1 and sliver["rank"] == 2
assert square["soil_carved_acres"] == 0.0 and sliver["soil_carved_acres"] == 0.0, "no soil union was passed -- nothing should be carved"
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
full_extent_aspect = _full_extent_boundary(dem_aspect)
step1_aspect, aspect_patches = _step1_and_patches(dem_aspect, full_extent_aspect)
assert len(aspect_patches) == 2

scored_aspect = score_production_areas(aspect_patches, dem_aspect, step1_aspect)
south = next(p for p in scored_aspect if p["aspect_deg"] is not None and abs(p["aspect_deg"] - 180.0) < 1.0)
north = next(p for p in scored_aspect if p["aspect_deg"] is not None and abs(p["aspect_deg"] - 0.0) < 1.0)

assert south["aspect_factor"] == 1.0 and north["aspect_factor"] == 0.0
assert abs(south["slope_factor"] - north["slope_factor"]) < 1e-6
assert abs(south["size_factor"] - north["size_factor"]) < 1e-6
assert south["suitability_score"] > north["suitability_score"]

score_gap = south["suitability_score"] - north["suitability_score"]
max_possible_gap_from_aspect_alone = ASPECT_FACTOR_WEIGHT * 100
assert score_gap <= max_possible_gap_from_aspect_alone + 0.1, (
    "with every other factor equal, aspect_factor alone must not be able to move the score by more "
    "than its own configured weight"
)
print(
    f"South-facing (score={south['suitability_score']}) outranks north-facing "
    f"(score={north['suitability_score']}) by exactly the aspect weight's worth ({score_gap} points)."
)


# --- 3. boundary clipping: STEP 1/STEP 3 (production_area.py) drive the on-parcel cells this module scores ---

west_half_boundary = box(500000.0, 4500120.0 - rows * 5.0, 500000.0 + 13 * 5.0, 4500120.0)
step1_clipped, clipped_patches = _step1_and_patches(dem_shape, west_half_boundary)
clipped_square = next(p for p in clipped_patches if p["id"] == square["id"])
assert clipped_square["area_acres"] < square["area_acres"]

clipped_scored = score_production_areas([dict(clipped_square)], dem_shape, step1_clipped)
assert clipped_scored[0]["area_score"] < square["area_score"], (
    "clipped area_score should be lower than the unclipped square's -- confirms scoring reflects the "
    "ON-PARCEL cells STEP 1/STEP 3 already clipped to"
)
print(
    f"Clipping the compact square to its west half correctly shrinks both its reported area_acres "
    f"({square['area_acres']} -> {clipped_square['area_acres']}) AND its scored area_score "
    f"({square['area_score']} -> {clipped_scored[0]['area_score']})."
)


# --- 4. soil bookkeeping: STEP 1's already-computed per-region values are read, not recomputed ---

soil_array = np.full((20, 20), 100.0, dtype=np.float32)
dem_soil = _dem(soil_array, origin_y=4500040.0)
full_extent_soil = _full_extent_boundary(dem_soil)

# 4a. Never checked (soil union omitted) -- soil_data_available=False, nothing carved.
step1_unchecked = compute_step1_eligible_cells(dem_soil, full_extent_soil)
patches_unchecked = cluster_and_gate(step1_unchecked["eligible_mask"], dem_soil, full_extent_soil, step1_unchecked)
scored_unchecked = score_production_areas([dict(p) for p in patches_unchecked], dem_soil, step1_unchecked)
assert len(scored_unchecked) == 1
assert scored_unchecked[0]["soil_carved_acres"] == 0.0
assert scored_unchecked[0]["soil_data_available"] is False
print("Soil never checked -> soil_data_available=False, soil_carved_acres=0.0 (unmodified passthrough).")

# 4b. Checked and genuinely clean (None) -- soil_data_available=True, nothing carved, same score as unchecked.
step1_clean = compute_step1_eligible_cells(dem_soil, full_extent_soil, disqualifying_soil_union_utm=None)
patches_clean = cluster_and_gate(step1_clean["eligible_mask"], dem_soil, full_extent_soil, step1_clean)
scored_clean = score_production_areas([dict(p) for p in patches_clean], dem_soil, step1_clean)
assert scored_clean[0]["soil_carved_acres"] == 0.0
assert scored_clean[0]["soil_data_available"] is True
assert scored_clean[0]["suitability_score"] == scored_unchecked[0]["suitability_score"], (
    "checked-and-clean vs never-checked must score identically -- only real disqualifying geometry "
    "(not availability itself) may change the score"
)
print("Soil checked and genuinely clean -> soil_data_available=True, still unmodified, same score as unchecked.")

# 4c. A real partial hydric overlap -- carves part of the region at STEP 1, before this cluster ever formed.
corner_hydric = box(500000.0, 4499940.0, 500047.5, 4499987.5)
step1_carved, patches_carved = _step1_and_patches(dem_soil, full_extent_soil, disqualifying_soil_union_utm=corner_hydric)
assert len(patches_carved) == 1, "a corner exclusion should leave one smaller connected remainder"
scored_carved = score_production_areas([dict(p) for p in patches_carved], dem_soil, step1_carved)
assert scored_carved[0]["soil_carved_acres"] > 0
assert scored_carved[0]["soil_carved_pct"] > 0
assert scored_carved[0]["area_acres"] < scored_unchecked[0]["area_acres"]
assert scored_carved[0]["suitability_score"] != scored_unchecked[0]["suitability_score"]
print(
    f"A real corner hydric overlap is excluded at STEP 1 (before clustering): soil_carved_acres="
    f"{scored_carved[0]['soil_carved_acres']} ({scored_carved[0]['soil_carved_pct']}%), area "
    f"{scored_unchecked[0]['area_acres']} -> {scored_carved[0]['area_acres']} acres."
)

# 4d. A mid-band hydric overlap that splits the original slope-eligible region into two clusters -- both
# pieces must report the SAME soil_carved_acres/pct (their shared source_patch_id's own bookkeeping),
# same convention the pre-consolidation architecture used for lettered sub-patches split from one original.
midband_hydric = box(500045.0, 4499930.0, 500055.0, 4500050.0)
step1_split, patches_split = _step1_and_patches(dem_soil, full_extent_soil, disqualifying_soil_union_utm=midband_hydric)
assert len(patches_split) == 2, f"expected a 2-way split, got {len(patches_split)} piece(s)"
scored_split = score_production_areas([dict(p) for p in patches_split], dem_soil, step1_split)
assert scored_split[0]["source_patch_id"] == scored_split[1]["source_patch_id"], (
    "both pieces must trace back to the SAME STEP 1 slope-only source region"
)
assert scored_split[0]["soil_carved_acres"] == scored_split[1]["soil_carved_acres"], (
    "soil_carved_acres is the ORIGINAL source region's total carved acreage -- same value on every "
    "piece split from it, exactly as the pre-consolidation architecture's lettered sub-patches did"
)
for piece in scored_split:
    assert piece["area_acres"] < scored_unchecked[0]["area_acres"] / 2 + 0.1
print(
    f"A mid-band hydric overlap splits one original source region into {len(scored_split)} clusters "
    f"(areas: {[p['area_acres'] for p in scored_split]}), both correctly sharing source_patch_id="
    f"{scored_split[0]['source_patch_id']} and the same soil_carved_acres={scored_split[0]['soil_carved_acres']}."
)

# 4e. Full cover -- the entire slope-eligible region is disqualifying soil: STEP 3 never even forms a cluster.
full_cover_hydric = box(500000.0, 4499940.0, 500100.0, 4500040.0)
step1_full_cover, patches_full_cover = _step1_and_patches(dem_soil, full_extent_soil, disqualifying_soil_union_utm=full_cover_hydric)
assert patches_full_cover == [], "a region entirely covered by disqualifying soil must produce zero clusters at STEP 3"
print("A region entirely covered by disqualifying soil produces zero surviving clusters (nothing to score).")


# --- confidence_notes: populated directly on the score_production_areas() dicts, not just the eventual GeoJSON ---

for result_set, label in (
    (scored_unchecked, "unchecked"), (scored_clean, "checked-clean"), (scored_carved, "corner-carved"),
    (scored_split, "split"),
):
    for p in result_set:
        notes = p.get("confidence_notes")
        assert notes and notes.strip(), f"[{label}] scored_patches entry (id={p.get('id')}) has empty confidence_notes"
        assert "not a certainty" in notes.lower(), (
            f"[{label}] confidence_notes must carry the same tone every other layer's confidence_notes uses"
        )
print("Every scored_patches entry (not just the GeoJSON wrapper) carries a real, non-empty confidence_notes "
      "with the required 'not a certainty' framing.")

geojson_carved = production_suitability_to_geojson(scored_carved)
assert geojson_carved["features"][0]["properties"]["confidence_notes"] == scored_carved[0]["confidence_notes"], (
    "production_suitability_to_geojson() must reuse the same confidence_notes already computed on the "
    "patch dict, not recompute a second, potentially-diverging copy"
)
print("confidence_notes is identical between scored_patches and the GeoJSON feature built from it.")


# --- output: schema-valid FeatureCollection on the SAME layer as production_area.py's own output ---

geojson = production_suitability_to_geojson(scored_split)
validate_feature_collection(geojson)
required_props = {
    "suitability_score", "slope_factor", "size_factor", "aspect_factor",
    "area_acres", "representative_elevation_m", "rank",
    "soil_carved_acres", "soil_carved_pct", "soil_data_available", "source_patch_id",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "production_area_candidate"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert "soil_factor" not in feature["properties"], "soil must not appear as a graded factor property"
    score = feature["properties"]["suitability_score"]
    assert 0.0 <= score <= 100.0
    for factor_name in ("slope_factor", "size_factor", "aspect_factor"):
        assert 0.0 <= feature["properties"][factor_name] <= 1.0
    assert feature["properties"]["soil_carved_acres"] > 0
print("production_suitability_to_geojson output is schema-valid, layer='production_area_candidate', "
      "with soil_carved_acres/soil_carved_pct/source_patch_id and no soil_factor property.")


# --- ranking: rank 1 always has the highest suitability_score ---

for patches_set in (scored_shape, scored_aspect, scored_split):
    ranked = sorted(patches_set, key=lambda p: p["rank"])
    scores_by_rank = [p["suitability_score"] for p in ranked]
    assert scores_by_rank == sorted(scores_by_rank, reverse=True)
print("rank is consistently ordered by descending suitability_score across every scored set.")


# --- summarize helper ---

assert "No production-area candidates" in summarize_production_area_suitability([])
summary = summarize_production_area_suitability(scored_shape)
assert "Rank 1" in summary and "Rank 2" in summary
summary_split = summarize_production_area_suitability(scored_split)
assert "soil-carved" in summary_split
print("summarize_production_area_suitability handles empty input and mentions soil-carving when relevant.")

print("\nAll production_suitability checks passed.")
