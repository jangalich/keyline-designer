"""
test_report_generator.py

Offline (no-network, no-LLM) checks for report_generator.py's own
pure-formatting helpers -- specifically _format_road_corridor_summary(),
which turns road_corridors.py's own 'narrative_data' block (see
build_narrative_data() there -- pre-digested, FINAL, feet/acres values)
into the prose the report prompt consumes. No Anthropic API call is made
here; this exercises only the deterministic dict-to-string formatting.

Focus of this file: the steep-section clause carried by the road
narrative's per-branch cell-level metrics (max_grade_pct / steep_ft).
The clause must be gated on max_grade_pct (the steepest single CELL), NOT
on avg_grade_pct (the gentle centerline average) -- a route can average a
mild grade and still cross a short steep pitch, and that pitch is exactly
what must NOT be smoothed away by the low average.
"""

import os
from unittest.mock import Mock, patch as mock_patch

import report_generator
from report_generator import (
    _format_keypoints_summary,
    _format_road_corridor_summary,
    generate_scale_of_permanence_report,
)


def _road_narrative(branches, stop_reason, served_acres, unserved_acres, max_grade_pct, steep_ft):
    """A road narrative block in road_corridors.build_narrative_data()'s own
    shape -- values already FINAL (feet, 1-decimal), exactly as the real
    block delivers them."""
    total_length_ft = round(sum(b["length_ft"] for b in branches), 1)
    total = served_acres + unserved_acres
    return {
        "network_found": bool(branches),
        "stop_reason": stop_reason,
        "determination": {
            "grade_ceiling_pct": 35.0,
            "steep_grade_threshold_pct": 10.0,
            "max_grade_pct": max_grade_pct,
            "steep_ft": steep_ft,
            "water_zone_excluded": True,
            "floodplain_data_available": True,
            "floodplain_data_is_fallback": False,
            "canopy_data_available": True,
        },
        "access": {
            "branch_count": len(branches),
            "total_length_ft": total_length_ft,
            "served_acres": served_acres,
            "unserved_acres": unserved_acres,
            "served_pct_of_production": round(served_acres / total * 100.0, 1) if total > 0 else None,
            "service_radius_ft": 200.0,
            "reaches_water_zone": any(b["role"] == "water_spur" for b in branches),
        },
        "branches": branches,
    }


# =====================================================================
# steep-section clause: a branch with a gentle AVERAGE grade but a steep
# single CELL is still flagged -- the low average must not suppress it
# =====================================================================

steep_narrative = _road_narrative(
    branches=[
        {
            "branch_index": 0,
            "role": "trunk",
            "joins_branch_index": None,
            "length_ft": 984.3,
            "newly_served_acres": 2.5,
            "avg_grade_pct": 6.1,   # gentle overall
            "max_grade_pct": 24.0,  # but crosses a steep single cell
            "steep_ft": 59.1,       # ~18m of it above the 10% threshold
            "crosses_floodplain": False,
            "crosses_production_zone": False,
        }
    ],
    stop_reason="all_demand_served",
    served_acres=2.5,
    unserved_acres=0.0,
    max_grade_pct=24.0,
    steep_ft=59.1,
)

prose = _format_road_corridor_summary(steep_narrative)
print("----- _format_road_corridor_summary() output (gentle avg, steep cell) -----")
print(prose)
print("----- end output -----")

assert "6.1%" in prose, "the gentle average grade should still be stated plainly"
assert "cut-and-fill or a switchback" in prose, (
    "a branch whose steepest cell (max_grade_pct=24.0%) exceeds the 10% threshold MUST get the "
    "steep-section clause -- the low 6.1% average must not suppress it"
)
assert "reaching 24.0%" in prose, "the steep clause must state the peak grade plainly"
assert "59.1ft above 10% grade" in prose, (
    "the steep clause must state the steep length plainly (59.1ft above 10% grade)"
)
print(
    "Steep-section clause present on a trunk averaging only 6.1% grade: it reports 59.1ft "
    "above 10% grade reaching 24.0%, and is NOT suppressed by the low average."
)


# =====================================================================
# no steep branch => no clause added at all (every existing sentence, the
# stop_reason mapping, and the short-spur proportionality rule unchanged)
# =====================================================================

gentle_narrative = _road_narrative(
    branches=[
        {
            "branch_index": 0,
            "role": "trunk",
            "joins_branch_index": None,
            "length_ft": 984.3,
            "newly_served_acres": 2.5,
            "avg_grade_pct": 4.0,
            "max_grade_pct": 8.0,   # below the 10% threshold -- no clause
            "steep_ft": 0.0,
            "crosses_floodplain": False,
            "crosses_production_zone": False,
        }
    ],
    stop_reason="all_demand_served",
    served_acres=2.5,
    unserved_acres=0.0,
    max_grade_pct=8.0,
    steep_ft=0.0,
)

gentle_prose = _format_road_corridor_summary(gentle_narrative)
assert "cut-and-fill or a switchback" not in gentle_prose, (
    "no branch is steep here (max_grade_pct 8.0% <= 10%), so NO steep-section clause should be added"
)
assert "The network reaches all identified production ground." in gentle_prose, (
    "the existing stop_reason sentence must be unchanged"
)
assert "ONE road NETWORK grown from the property's real access point" in gentle_prose, (
    "the existing closing guidance (short-spur proportionality rule) must be unchanged"
)
print("A network with no steep branch adds no steep-section clause and leaves every existing sentence intact.")


# =====================================================================
# empty-network stop reasons: the block carries stop_reason for EVERY
# outcome (including the pre/post-router ones road_corridors.py itself
# adds), and each maps to its own real sentence; None/missing narrative
# falls back to the honest no-data text.
# =====================================================================

for _sr, _fragment in (
    ("no_anchor_given", "No access point was provided"),
    ("no_eligible_anchor", "could not be connected to any routable ground"),
    ("corridor_too_short", "shorter than the minimum meaningful road length"),
    ("no_demand", "No production area was identified"),
):
    _empty_prose = _format_road_corridor_summary(
        _road_narrative([], _sr, served_acres=0.0, unserved_acres=0.0, max_grade_pct=0.0, steep_ft=0.0)
    )
    assert _fragment in _empty_prose, f"stop_reason {_sr!r} must map to its own sentence, got {_empty_prose!r}"

assert "No road network data available" in _format_road_corridor_summary(None), (
    "a missing narrative block must fall back to the honest no-data text"
)
print("Every empty-network stop_reason maps to its own sentence; a missing block reads as no data.")


# =====================================================================
# _format_keypoints_summary(): the staged, ready-to-wire keypoint data
# block (carried through generate_scale_of_permanence_report() but NOT yet
# injected into the LLM prompt -- see that function's docstring). Pure
# dict-to-string formatting, so it is exercised here directly.
# =====================================================================

_empty_keypoints_prose = _format_keypoints_summary([])
assert "No keypoints detected" in _empty_keypoints_prose, (
    "an empty keypoint list must format as an honest 'none detected' line, not a placeholder"
)
assert _format_keypoints_summary(None) == _empty_keypoints_prose, (
    "None (detection unavailable) must format the same as an empty list"
)

_kp_fixture = [
    {
        "id": 0,
        "valley_id": 3,
        "elevation_m": 346.5,
        "contributing_acres": 6.36,
        "slope_above_pct": 18.4,
        "slope_below_pct": 6.1,
        "slope_drop_pct": 12.3,
        "on_parcel": True,
        "distance_outside_boundary_m": 0.0,
    },
    {
        "id": 1,
        "valley_id": 7,
        "elevation_m": 347.0,
        "contributing_acres": 6.69,
        "slope_above_pct": 15.0,
        "slope_below_pct": 5.5,
        "slope_drop_pct": 9.5,
        "on_parcel": False,
        "distance_outside_boundary_m": 14.0,
    },
]
_kp_prose = _format_keypoints_summary(_kp_fixture)
assert "2 keypoint(s) detected" in _kp_prose
assert "Keypoint 0 (valley 3)" in _kp_prose and "346.5 m" in _kp_prose and "6.36 ac" in _kp_prose
assert "on parcel" in _kp_prose, "an on-parcel keypoint must be stated as such"
assert "~14 m outside the boundary" in _kp_prose, "an off-parcel keypoint must state its distance"
assert "12.3%" in _kp_prose, "the slope drop must be stated plainly"
print(
    "_format_keypoints_summary() renders the keypoint list as a factual data block (elevation, "
    "catchment, slope drop, on/off-parcel) and an honest empty line for [] / None -- ready to wire, "
    "no narration."
)


# =====================================================================
# irradiance inertness: irradiance= is accepted and STORED by generate_
# scale_of_permanence_report() but deliberately NOT injected into the LLM
# prompt yet (the reviewer decides the narrative wording later -- see that
# function's docstring). This mirrors keypoints' own already-established
# "forward-compatible seam, changes nothing about the report today"
# discipline: supplying irradiance= must leave the generated data_summary
# byte-for-byte identical to omitting it, and the word "irradiance" must
# not appear anywhere in it. Unlike keypoints there is deliberately NO
# _format_irradiance_summary() to exercise (that formatting + wiring is the
# out-of-scope deferred narrative work), so inertness is proved directly on
# the prompt the function actually builds -- with the Anthropic client fully
# mocked so no network/LLM call is made.
# =====================================================================


def _capture_prompt(**call_kwargs) -> str:
    """Calls generate_scale_of_permanence_report() with the Anthropic client
    mocked out and returns the single user-message string the function built
    (which embeds its internal data_summary). No network, no LLM call."""
    captured = {}

    def _fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        block = Mock()
        block.type = "text"
        block.text = "MOCKED REPORT"
        message = Mock()
        message.content = [block]
        return message

    fake_client = Mock()
    fake_client.messages.create.side_effect = _fake_create

    with mock_patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-not-real"}):
        with mock_patch.object(report_generator, "Anthropic", return_value=fake_client):
            result = generate_scale_of_permanence_report(**call_kwargs)

    assert result == "MOCKED REPORT", "the mocked client's text block should be returned verbatim"
    fake_client.messages.create.assert_called_once()
    return captured["messages"][0]["content"]


_irr_soil = [
    {"muname": "Gilpin-Upshur complex", "comppct_r": 50, "drainagecl": "Well drained", "slope_r": 20},
]
_irr_elevation = [
    {"latitude": 40.64286, "longitude": -79.98383, "elevation": 326.7},
    {"latitude": 40.64528, "longitude": -79.98383, "elevation": 344.2},
]
_irr_water = {"streams": [{"name": "Montour Run", "feature_code": None, "geometry": None}], "water_bodies": []}

# A realistic ParcelData.irradiance dict (get_regional_irradiance_baseline()'s
# own shape) -- deliberately full of numbers and the literal word so that IF
# it were ever injected, the assertions below would catch it.
_irr_fixture = {
    "status": "ok",
    "annual_ghi_kwh_m2_day": 4.21,
    "annual_dni_kwh_m2_day": 4.98,
    "source": "NREL PVWatts v8",
    "note": "regional irradiance baseline",
}

_base_content = _capture_prompt(
    soil_components=_irr_soil,
    elevation_grid=_irr_elevation,
    water_features=_irr_water,
)
_with_irr_content = _capture_prompt(
    soil_components=_irr_soil,
    elevation_grid=_irr_elevation,
    water_features=_irr_water,
    irradiance=_irr_fixture,
)

assert "irradiance" not in _with_irr_content.lower(), (
    "supplying irradiance= must NOT inject the word 'irradiance' (or any irradiance data block) "
    "into the prompt -- it is a stored, forward-compatible seam, not yet-wired narrative content"
)
assert "4.21" not in _with_irr_content and "PVWatts" not in _with_irr_content, (
    "no irradiance value/source string may leak into the prompt while the parameter is inert"
)
assert _with_irr_content == _base_content, (
    "the generated prompt must be byte-for-byte identical whether or not irradiance= is supplied -- "
    "irradiance changes nothing about the report today (same inertness discipline keypoints follows)"
)
print(
    "irradiance inertness: irradiance= is accepted and stored but leaves the generated prompt "
    "byte-for-byte identical to omitting it -- the word 'irradiance' and every value never appear."
)


# =====================================================================
# narrative_data-fed formatters: water / solar / production / tree data
# blocks are rendered from each module's own narrative block (pre-
# digested, FINAL values -- no unit conversion or computation happens
# here), and a missing block reads as honest no-data text, never stale
# or invented content. Fixtures are in each module's own
# build_narrative_data() shape.
# =====================================================================

from report_generator import (  # noqa: E402
    _format_production_areas_summary,
    _format_solar_candidate_zones_summary,
    _format_tree_zones_summary,
    _format_water_candidate_zones_summary,
)

_water_nd = {
    "zone_found": True,
    "production_area_count": 2,
    "gates": {"canopy_data_available": True, "road_data_available": True},
    "zone": {
        "area_acres": 0.5,
        "target_acres": 0.5,
        "location": {"position_in_parcel": "southwest", "elevation_percentile_of_parcel": 22.7},
        "drainage": {
            "contributing_area_acres": 1.9,
            "contributing_area_ceiling_acres": 20.0,
            "slope_median_pct": 40.0,
        },
        "service": {
            "served_production_area_count": 2,
            "served_production_area_ids": [0, 1],
            "relationships": [
                {"production_area_id": 0, "can_gravity_feed": True, "elevation_differential_ft": 26.2,
                 "distance_ft": 73.8, "gradient_pct": 35.6},
                {"production_area_id": 1, "can_gravity_feed": False, "elevation_differential_ft": -9.8,
                 "distance_ft": 0.0, "gradient_pct": None},
            ],
        },
    },
}
_water_prose = _format_water_candidate_zones_summary(_water_nd)
assert "in the parcel's southwest" in _water_prose and "22.7 elevation percentile" in _water_prose
assert "1.9 acre(s)" in _water_prose and "20.0-acre siltation/peak-flow" in _water_prose
assert "can_gravity_feed: True" in _water_prose and "A gravity-feed relationship." in _water_prose
assert "would need a pump" in _water_prose, "a below-elevation relationship must be stated as pump-required"
assert "gradient undefined" in _water_prose, "a distance-0 relationship must state its gradient is undefined, not 0%"
assert "NOT a specific pond/dam site" in _water_prose
assert "No water system candidate survey area identified" in _format_water_candidate_zones_summary(None)
assert "No water system candidate survey area identified" in _format_water_candidate_zones_summary(
    {"zone_found": False, "production_area_count": 0, "gates": {}, "zone": None}
)
print("_format_water_candidate_zones_summary(): position/drainage/gravity-vs-pump rendered from the block; "
      "no-zone and missing blocks read as no data.")

_solar_nd = {
    "site_found": True,
    "candidate_count": 5,
    "gates": {
        "existing_canopy_excluded": True,
        "water_zone_excluded": True,
        "tree_zone_exclusion_checked": True,
        "road_proximity_source": "selected_road_corridor",
        "prime_farmland_checked": True,
    },
    "selected_site": {
        "score": 87.5,
        "footprint_acres": 0.1,
        "location": {
            "position_in_parcel": "southeast",
            "production_zone_relationship": "inside",
            "distance_to_production_edge_ft": 42.7,
            "distance_to_road_ft": 18.0,
            "distance_to_water_zone_ft": 210.0,
        },
        "benefits": {
            "avg_slope_pct": 3.2,
            "facing": "south",
            "factors": {"slope": 84.0, "aspect": 100.0, "shading": 95.0, "production_proximity": 71.0},
            "prime_farmland_conflict": True,
        },
    },
}
_solar_prose = _format_solar_candidate_zones_summary(_solar_nd)
assert "rank 1 of 5" in _solar_prose and "score 87.5/100" in _solar_prose
assert "the parcel's southeast" in _solar_prose
assert "INSIDE a production zone" in _solar_prose
assert "18.0ft to the property's own selected road corridor" in _solar_prose
assert "facing south" in _solar_prose and "slope 84.0" in _solar_prose
assert "PRIME FARMLAND CONFLICT" in _solar_prose

import copy  # noqa: E402

_solar_nd_unchecked = copy.deepcopy(_solar_nd)
_solar_nd_unchecked["gates"]["prime_farmland_checked"] = False
_solar_nd_unchecked["selected_site"]["benefits"]["prime_farmland_conflict"] = None
assert "NOT checked this run" in _format_solar_candidate_zones_summary(_solar_nd_unchecked), (
    "an unchecked prime-farmland flag (None) must read as not-checked, never as no-conflict"
)
assert "No solar structure candidate site identified" in _format_solar_candidate_zones_summary(None)
print("_format_solar_candidate_zones_summary(): selected site's location/benefits rendered from the block; "
      "unchecked prime farmland reads as not-checked; missing block reads as no data.")

_prod_nd = {
    "scales": {"range": [0.0, 100.0], "direction": "higher_is_better"},
    "parcel": {"total_acres": 17.4, "slope_passing_acres": 15.0, "eligible_acres": 13.2,
               "selected_acres": 12.4, "selected_pct_of_parcel": 71.3},
    "ceiling": {"cap_pct_of_parcel": 80.0, "bound": True, "acres_trimmed": 0.8},
    "gates": {
        "universe": "slope_passing_on_parcel",
        "canopy_excluded_acres": 1.2, "canopy_only_excluded_acres": 1.0,
        "hydric_excluded_acres": None, "hydric_only_excluded_acres": None,
        "farm_roads_excluded_acres": 0.3, "farm_roads_only_excluded_acres": 0.3,
        "boundary_setback_excluded_acres": 0.9, "boundary_setback_only_excluded_acres": None,
        "boundary_setback_feet": 10.0,
        "soil_data_available": False, "canopy_data_available": True, "road_data_available": True,
    },
    "patches": [
        {
            "id": 0, "rank": 1, "area_acres": 9.1, "percent_of_parcel": 52.3,
            "position_in_parcel": "southeast",
            "slope_min_pct": 1.2, "slope_max_pct": 18.9, "slope_median_pct": 8.4, "avg_slope_pct": 8.9,
            "dominant_aspect": "southeast", "aspect_consistency_pct": 84, "aspect_available": True,
            "score": 78.2,
            "factors": {"slope_factor": 71.0, "size_factor": 80.0, "aspect_factor": 88.0},
            "area_score": 90.0, "compactness_score": 70.0,
            "soil_components": None, "drainage_class": None, "source_region_hydric_pct": None,
            "elevation_percentile_of_parcel": 62.0, "hole_count": 1, "hole_acres": 0.2,
            "from_waist_split": False, "source_patch_id": 0,
        },
    ],
}
_prod_prose = _format_production_areas_summary(_prod_nd)
assert "17.4 acres total" in _prod_prose and "12.4 acres selected" in _prod_prose
assert "80.0%-of-parcel ceiling trimmed 0.8" in _prod_prose
assert "hydric soil: not checked this run" in _prod_prose, (
    "an unavailable soil check must read as not-checked, never as 0 acres excluded"
)
assert "existing tree canopy: 1.2 acres excluded" in _prod_prose
assert "Patch 0 (rank 1): 9.1 acres (52.3% of parcel), in the parcel's southeast, score 78.2/100" in _prod_prose
assert "faces southeast (84% of its cells" in _prod_prose
assert "62.0 elevation percentile" in _prod_prose
assert "1 interior exclusion hole(s) totalling 0.2 acres" in _prod_prose
assert "No production-area candidate data available" in _format_production_areas_summary(None)
print("_format_production_areas_summary(): parcel/ceiling/gates/patches rendered from the block; "
      "unchecked gates read as not-checked; missing block reads as no data.")

_tree_nd = {
    "candidate_count": 2,
    "search_space": {"parcel_acres": 12.0, "claimed_acres": 9.0, "search_space_acres": 3.0,
                     "search_space_pct_of_parcel": 25.0, "boundary_setback_ft": 16.4,
                     "production_clearance_ft": 16.4, "water_clearance_ft": 16.4},
    "selection": {"min_suitability_score": 31.0, "min_zone_acres": 0.1, "existing_canopy_excluded": True,
                  "factor_weights_pct": {"hydric_overlap": 40.0, "slope": 30.0,
                                          "soil_marginality": 20.0, "stream_proximity": 10.0}},
    "gates": {"soil_marginality_data_available": True, "hydric_data_available": True,
              "stream_data_available": False},
    "zones": [
        {"rank": 1, "position_in_parcel": "northwest", "area_acres": 0.4, "score": 62.6,
         "avg_slope_pct": 18.3,
         "factors": {"hydric_overlap": 100.0, "slope": 36.6, "soil_marginality": 100.0,
                     "stream_proximity": 25.0}},
        {"rank": 2, "position_in_parcel": "center", "area_acres": 0.2, "score": 34.0,
         "avg_slope_pct": 8.0,
         "factors": {"hydric_overlap": 0.0, "slope": 20.0, "soil_marginality": 100.0,
                     "stream_proximity": 80.0}},
    ],
}
_tree_prose = _format_tree_zones_summary(_tree_nd)
assert "3.0 of the parcel's 12.0 acres (25.0%)" in _tree_prose
assert "at least 31.0/100" in _tree_prose and "at least 0.1 acres" in _tree_prose
assert "stream data was unavailable" in _tree_prose, (
    "an unavailable stream fetch must be flagged so its factor isn't quoted as a measurement"
)
assert "Rank 1: 0.4 acres, in the parcel's northwest, score 62.6/100" in _tree_prose
assert "assigning each zone a function" in _tree_prose
assert "No tree zone candidate data available" in _format_tree_zones_summary(None)
print("_format_tree_zones_summary(): search space/selection rules/zones rendered from the block; "
      "unavailable factors flagged; missing block reads as no data.")


# =====================================================================
# end-to-end prompt wiring: generate_scale_of_permanence_report() formats
# every KSOP data block from narrative_data (the per-module blocks
# captured on PipelineContext); with no narrative_data at all, every
# block degrades to its own honest no-data text -- exactly like a data
# outage, never stale or invented content.
# =====================================================================

_full_narrative = {
    "production_area_ceiling": _prod_nd,
    "water_candidate_zones": _water_nd,
    "road_corridors": gentle_narrative,
    "solar_suitability": _solar_nd,
    "tree_zone_candidates": _tree_nd,
}
_wired_prompt = _capture_prompt(
    soil_components=_irr_soil,
    elevation_grid=_irr_elevation,
    water_features=_irr_water,
    narrative_data=_full_narrative,
)
for _header in (
    "PRODUCTION AREA CANDIDATES",
    "WATER SYSTEM CANDIDATE SURVEY AREA",
    "ROAD NETWORK",
    "TREE ZONE CANDIDATES",
    "SOLAR STRUCTURE CANDIDATE",
):
    assert _header in _wired_prompt, f"data_summary must carry the {_header} section"
assert "score 87.5/100" in _wired_prompt and "in the parcel's southwest" in _wired_prompt
assert "The network reaches all identified production ground." in _wired_prompt
assert "Patch 0 (rank 1)" in _wired_prompt and "Rank 1: 0.4 acres" in _wired_prompt

_unwired_prompt = _capture_prompt(
    soil_components=_irr_soil,
    elevation_grid=_irr_elevation,
    water_features=_irr_water,
)
for _no_data in (
    "No production-area candidate data available",
    "No water system candidate survey area identified",
    "No road network data available",
    "No tree zone candidate data available",
    "No solar structure candidate site identified",
):
    assert _no_data in _unwired_prompt, (
        f"with no narrative_data, every KSOP block must degrade to its own no-data text ({_no_data!r})"
    )
print(
    "End-to-end prompt wiring: all five KSOP data blocks are formatted from narrative_data and land in "
    "the generated prompt; with no narrative_data every block reads as honest no-data text."
)


print("\nAll report_generator checks passed.")
