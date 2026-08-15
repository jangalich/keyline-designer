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

from report_generator import _format_road_corridor_summary

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


print("\nAll report_generator checks passed.")
