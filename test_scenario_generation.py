"""
test_scenario_generation.py

Offline (no-network) checks for scenario_generation.py's PURE logic --
subset enumeration (Step 1), the "meaningfully different" diversity test
and its documented thresholds (Step 3), and dedup/selection/labeling
(Step 4). All against synthetic patch/metrics dicts, no DEM or real
geometry fetch involved -- same "pure logic, independent of real data
fetches" approach as test_production_suitability.py and
test_water_candidate_zones.py.

See test_scenario_generation_pipeline.py for the end-to-end synthetic-DEM
check that these pieces actually wire together correctly with the real
per-layer computation functions.
"""

from shapely.geometry import Point

from scenario_generation import (
    AVAILABLE_ACREAGE_ABSOLUTE_THRESHOLD_ACRES,
    AVAILABLE_ACREAGE_RELATIVE_THRESHOLD,
    SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS,
    WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS,
    _is_meaningfully_different,
    _label_and_rationale,
    _select_top_n_scenarios,
    enumerate_production_zone_subsets,
)


def _patch(patch_id, score):
    return {"id": patch_id, "suitability_score": score}


# --- Step 1: bounded subset enumeration ---

patches_4 = [_patch(i, 10.0 * i) for i in range(4)]  # n=4 <= default cap of 5
subsets_4 = enumerate_production_zone_subsets(patches_4)
assert len(subsets_4) == 2**4 - 1, f"expected every non-empty subset (15), got {len(subsets_4)}"

sizes_4 = {len(s) for s in subsets_4}
assert sizes_4 == {1, 2, 3, 4}, f"expected subset sizes 1-4 all represented, got {sizes_4}"

id_sets_4 = [frozenset(p["id"] for p in s) for s in subsets_4]
assert frozenset({0, 2}) in id_sets_4, "expected the {0,2} multi-zone combination to be enumerated"
print("Full enumeration (n<=cap): every non-empty subset produced, including genuine multi-zone combinations.")

# n=7 > default cap of 5 -- falls back to ranked top-K prefixes.
patches_7 = [_patch(i, float(i)) for i in range(7)]  # id 6 has the highest score
subsets_7 = enumerate_production_zone_subsets(patches_7, max_zones_for_full_enumeration=5)

assert len(subsets_7) == 7, f"expected exactly n=7 ranked subsets (top-1..top-7), got {len(subsets_7)}"
assert sorted(len(s) for s in subsets_7) == list(range(1, 8)), "expected one subset per size, 1 through 7"

top_1 = next(s for s in subsets_7 if len(s) == 1)
assert top_1[0]["id"] == 6, "top-1 must be the single highest-scoring patch"

top_3 = next(s for s in subsets_7 if len(s) == 3)
assert {p["id"] for p in top_3} == {4, 5, 6}, "top-3 must be exactly the three highest-scoring patches"

full_union = next(s for s in subsets_7 if len(s) == 7)
assert {p["id"] for p in full_union} == set(range(7)), "the full union (top-7) must include every candidate"

assert enumerate_production_zone_subsets([]) == []
print("Ranked top-K heuristic (n>cap): linear in n, spans single-zone through full-union, multi-zone "
      "combinations present, empty input handled.")


# --- Step 3: "meaningfully different" diversity thresholds ---


def _metrics(available_acres=10.0, top_water=None, top_solar=None):
    return {
        "production_acres": 5.0,
        "available_acres": available_acres,
        "water_zone_count": 1 if top_water else 0,
        "top_water_centroid": top_water,
        "solar_candidate_count": 1 if top_solar else 0,
        "solar_candidate_acres": 1.0,
        "top_solar_centroid": top_solar,
        "road_candidate_count": 1,
    }


water_point = Point(0, 0)
solar_point = Point(100, 100)
identical_a = _metrics(available_acres=10.0, top_water=water_point, top_solar=solar_point)
identical_b = _metrics(available_acres=10.0, top_water=water_point, top_solar=solar_point)
assert not _is_meaningfully_different(identical_a, identical_b), "identical downstream results must be duplicates"
print("Identical downstream results across two subsets are correctly treated as duplicates.")

moved_past = WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS + 1
assert _is_meaningfully_different(_metrics(top_water=Point(0, 0)), _metrics(top_water=Point(moved_past, 0))), (
    "top water candidate moving past its threshold must count as meaningfully different"
)
moved_under = WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS - 1
assert not _is_meaningfully_different(_metrics(top_water=Point(0, 0)), _metrics(top_water=Point(moved_under, 0))), (
    "a sub-threshold shift in the top water candidate must NOT count as meaningfully different"
)
print("Top water candidate location threshold (WATER_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS) enforced correctly.")

assert _is_meaningfully_different(_metrics(top_solar=Point(0, 0)), _metrics(top_solar=None)), (
    "one subset having a top solar candidate and the other having none at all must count as different"
)
solar_moved_past = SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS + 5
assert _is_meaningfully_different(_metrics(top_solar=Point(0, 0)), _metrics(top_solar=Point(0, solar_moved_past))), (
    "top solar candidate moving past its threshold must count as meaningfully different"
)
print("Top solar candidate appearance/disappearance and location threshold "
      "(SOLAR_TOP_CANDIDATE_CENTROID_THRESHOLD_METERS) enforced correctly.")

# Large relative swing but tiny absolute swing (below the absolute floor) -- NOT meaningfully different.
tiny_absolute_a = _metrics(available_acres=0.10)
tiny_absolute_b = _metrics(available_acres=0.10 + AVAILABLE_ACREAGE_ABSOLUTE_THRESHOLD_ACRES / 2)
assert not _is_meaningfully_different(tiny_absolute_a, tiny_absolute_b), (
    "an absolute acreage swing below the floor must not count as meaningfully different, even as a large "
    "percentage of a tiny baseline"
)

# Large absolute swing but on a huge baseline (below the relative floor) -- NOT meaningfully different.
huge_baseline = 10_000.0
tiny_relative_a = _metrics(available_acres=huge_baseline)
tiny_relative_b = _metrics(available_acres=huge_baseline + AVAILABLE_ACREAGE_ABSOLUTE_THRESHOLD_ACRES * 2)
assert not _is_meaningfully_different(tiny_relative_a, tiny_relative_b), (
    "an absolute acreage swing that's a tiny fraction of a huge baseline must not count as meaningfully different"
)

# Clears both bars -- meaningfully different.
clears_both_a = _metrics(available_acres=5.0)
clears_both_b = _metrics(available_acres=5.0 * (1 + AVAILABLE_ACREAGE_RELATIVE_THRESHOLD * 2))
assert _is_meaningfully_different(clears_both_a, clears_both_b), (
    "an acreage swing clearing both the absolute and relative bars must count as meaningfully different"
)
print("Available-acreage diversity metric correctly requires BOTH an absolute AND a relative threshold to be "
      "cleared before counting as meaningfully different.")


# --- Step 4: dedup, selection, and labeling ---


def _candidate(zone_ids, production_acres, available_acres, top_water=None, top_solar=None):
    return {
        "zone_ids": zone_ids,
        "layers": {},
        "metrics": {
            "production_acres": production_acres,
            "available_acres": available_acres,
            "water_zone_count": 1 if top_water else 0,
            "top_water_centroid": top_water,
            "solar_candidate_count": 1 if top_solar else 0,
            "solar_candidate_acres": 1.0,
            "top_solar_centroid": top_solar,
            "road_candidate_count": 1,
        },
    }


all_zones = _candidate(["0", "1", "2"], production_acres=10.0, available_acres=90.0, top_water=Point(0, 0))
near_duplicate = _candidate(["0", "1"], production_acres=9.9, available_acres=90.1, top_water=Point(0, 0))
genuinely_different = _candidate(["0"], production_acres=3.0, available_acres=97.0, top_water=Point(500, 500))

dedup_selected = _select_top_n_scenarios([all_zones, near_duplicate, genuinely_different], scenario_count=3)
dedup_zone_id_sets = [tuple(c["zone_ids"]) for c in dedup_selected]

assert len(dedup_selected) == 2, (
    f"expected the near-duplicate to be dropped, got {len(dedup_selected)} scenarios: {dedup_zone_id_sets}"
)
assert tuple(all_zones["zone_ids"]) in dedup_zone_id_sets
assert tuple(genuinely_different["zone_ids"]) in dedup_zone_id_sets
assert tuple(near_duplicate["zone_ids"]) not in dedup_zone_id_sets
print("Dedup correctly collapses a near-identical subset into its representative, keeping genuinely distinct ones.")

# Four genuinely distinct subsets, capped down to 3 by scenario_count.
max_production = _candidate(["0", "1", "2", "3"], production_acres=20.0, available_acres=80.0, top_water=Point(0, 0))
balanced = _candidate(["1", "2"], production_acres=10.0, available_acres=90.0, top_water=Point(1000, 0))
minimal = _candidate(["1"], production_acres=2.0, available_acres=98.0, top_water=Point(2000, 0))
close_to_balanced = _candidate(["1", "2", "3"], production_acres=10.5, available_acres=89.5, top_water=Point(1010, 0))

capped_selected = _select_top_n_scenarios([max_production, balanced, minimal, close_to_balanced], scenario_count=3)
assert len(capped_selected) == 3
capped_zone_id_sets = {tuple(c["zone_ids"]) for c in capped_selected}
assert tuple(max_production["zone_ids"]) in capped_zone_id_sets, "the largest-acreage scenario should always be kept"
assert tuple(minimal["zone_ids"]) in capped_zone_id_sets, (
    "the farthest-spread scenario should be picked over a near-duplicate of an already-kept one"
)
print("Selection caps at scenario_count while preferring the most spread-out (diverse) survivors, not an "
      "arbitrary prefix.")

high = _candidate(["0", "1"], production_acres=20.0, available_acres=80.0)
mid = _candidate(["0"], production_acres=10.0, available_acres=90.0)
low = _candidate(["1"], production_acres=2.0, available_acres=98.0)
label_selected = [high, mid, low]

high_label, high_rationale = _label_and_rationale(high, label_selected, parcel_acres=100.0)
mid_label, _ = _label_and_rationale(mid, label_selected, parcel_acres=100.0)
low_label, _ = _label_and_rationale(low, label_selected, parcel_acres=100.0)

assert high_label == "Maximum Production"
assert mid_label == "Balanced"
assert low_label == "Minimum Production Footprint"
assert "20.0 acres" in high_rationale, f"rationale should cite concrete acreage, got: {high_rationale}"
assert "0, 1" in high_rationale, f"rationale should cite the concrete zone ids included, got: {high_rationale}"

single = _candidate(["0"], production_acres=5.0, available_acres=95.0)
single_label, _ = _label_and_rationale(single, [single], parcel_acres=100.0)
assert single_label == "Maximum Production", "a lone selected scenario should read as the Maximum Production extreme"

print("Labels/rationale correctly reflect each scenario's own concrete acreage/zone differentiators "
      "(Maximum Production / Balanced / Minimum Production Footprint), not generic names.")

print("\nAll scenario_generation pure-logic checks passed.")
