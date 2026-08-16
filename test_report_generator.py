"""
test_report_generator.py

Offline (no-network, no-LLM) checks for report_generator.py's own
pure-formatting helpers -- specifically _format_road_corridor_summary(),
which turns road_corridors.build_road_network()'s network dict into the
prose the report prompt consumes. No Anthropic API call is made here; this
exercises only the deterministic dict-to-string formatting.

Focus of this file: the steep-section clause added alongside the road
network's new per-branch cell-level metrics (max_grade_pct / steep_meters).
The clause must be gated on max_grade_pct (the steepest single CELL), NOT
on avg_grade_pct (the gentle centerline average) -- a route can average a
mild grade and still cross a short steep pitch, and that pitch is exactly
what must NOT be smoothed away by the low average.
"""

from report_generator import _format_keypoints_summary, _format_road_corridor_summary

_FT_PER_M = 1.0 / 0.3048


# =====================================================================
# steep-section clause: a branch with a gentle AVERAGE grade but a steep
# single CELL is still flagged -- the low average must not suppress it
# =====================================================================

steep_network = {
    "branches": [
        {
            "branch_index": 0,
            "branch_role": "trunk",
            "joins_branch_index": None,
            "length_meters": 300.0,
            "avg_grade_pct": 6.1,     # gentle overall
            "max_grade_pct": 24.0,    # but crosses a steep single cell
            "steep_meters": 18.0,     # 18m of it above the 10% threshold
            "newly_served_acres": 2.5,
            "crosses_floodplain": False,
            "crosses_production_zone": False,
        }
    ],
    "total_length_meters": 300.0,
    "total_served_acres": 2.5,
    "unserved_acres": 0.0,
    "stop_reason": "all_demand_served",
    "max_grade_pct": 24.0,
    "steep_meters": 18.0,
}

prose = _format_road_corridor_summary(steep_network)
print("----- _format_road_corridor_summary() output (gentle avg, steep cell) -----")
print(prose)
print("----- end output -----")

expected_steep_ft = round(18.0 * _FT_PER_M, 1)  # 59.1
assert "6.1%" in prose, "the gentle average grade should still be stated plainly"
assert "cut-and-fill or a switchback" in prose, (
    "a branch whose steepest cell (max_grade_pct=24.0%) exceeds the 10% threshold MUST get the "
    "steep-section clause -- the low 6.1% average must not suppress it"
)
assert "reaching 24.0%" in prose, "the steep clause must state the peak grade plainly"
assert f"{expected_steep_ft}ft above 10% grade" in prose, (
    f"the steep clause must state the steep length plainly ({expected_steep_ft}ft above 10% grade)"
)
print(
    f"Steep-section clause present on a trunk averaging only 6.1% grade: it reports {expected_steep_ft}ft "
    "above 10% grade reaching 24.0%, and is NOT suppressed by the low average."
)


# =====================================================================
# no steep branch => no clause added at all (every existing sentence, the
# stop_reason mapping, and the short-spur proportionality rule unchanged)
# =====================================================================

gentle_network = {
    "branches": [
        {
            "branch_index": 0,
            "branch_role": "trunk",
            "joins_branch_index": None,
            "length_meters": 300.0,
            "avg_grade_pct": 4.0,
            "max_grade_pct": 8.0,     # below the 10% threshold -- no clause
            "steep_meters": 0.0,
            "newly_served_acres": 2.5,
            "crosses_floodplain": False,
            "crosses_production_zone": False,
        }
    ],
    "total_length_meters": 300.0,
    "total_served_acres": 2.5,
    "unserved_acres": 0.0,
    "stop_reason": "all_demand_served",
    "max_grade_pct": 8.0,
    "steep_meters": 0.0,
}

gentle_prose = _format_road_corridor_summary(gentle_network)
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


print("\nAll report_generator checks passed.")
