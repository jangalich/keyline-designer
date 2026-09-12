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


def _road_narrative(
    branches,
    stop_reason,
    served_acres,
    unserved_acres,
    max_grade_pct,
    steep_ft,
    terrain_quality_score=76.7,
    cost_per_meter_ratio=2.33,
    total_path_cost=1165.0,
):
    """A road narrative block in road_corridors.build_narrative_data()'s own
    shape -- values already FINAL (feet, 1-decimal), exactly as the real
    block delivers them. The quality defaults are the scale's own
    sustained-10%-grade anchor (ratio 2.33 -> 76.7); pass
    terrain_quality_score=None for a block with no network to score."""
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
        "quality": {
            "terrain_quality_score": terrain_quality_score,
            "cost_per_meter_ratio": cost_per_meter_ratio,
            "total_path_cost": total_path_cost,
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
    ("all_branches_below_minimum", "came out as a stub too short to be worth building"),
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
# terrain quality: the score is STATED PLAINLY when a network exists,
# with one clause on what it reflects (the ground the route runs on) and
# no invented qualitative band; and NOTHING quality-related is said at
# all for an empty network.
# =====================================================================

_quality_prose = _format_road_corridor_summary(
    _road_narrative(
        branches=[
            {
                "branch_index": 0, "role": "trunk", "joins_branch_index": None,
                "length_ft": 984.3, "newly_served_acres": 2.5, "avg_grade_pct": 4.0,
                "max_grade_pct": 8.0, "steep_ft": 0.0,
                "crosses_floodplain": False, "crosses_production_zone": False,
            }
        ],
        stop_reason="all_demand_served",
        served_acres=2.5, unserved_acres=0.0, max_grade_pct=8.0, steep_ft=0.0,
        terrain_quality_score=76.7,
    )
)
print("----- _format_road_corridor_summary() output (network with a terrain quality score) -----")
print(_quality_prose)
print("----- end output -----")

assert "76.7" in _quality_prose, "the score itself must appear in the prose"
assert "out of 100" in _quality_prose, "the score must be stated against its own scale, not bare"
assert "the ground the route actually runs on" in _quality_prose, (
    "one clause must say what the score reflects -- the terrain the route runs on"
)
# NO INVENTED BANDS. The block publishes a number on a documented scale;
# turning a range into a word ("excellent", "good", "fair", "poor") is
# inventing a claim the score does not make. Tone is the model's job.
for _band in ("excellent", "poor", "fair", "good ground", "grade A", "mediocre"):
    assert _band not in _quality_prose.lower(), (
        f"the summary must not invent the qualitative band {_band!r} -- report the number and let "
        "the model's own prose handle tone"
    )

# EMPTY NETWORK: nothing quality-related at all. The empty-network early
# return never reaches the quality clause, so a block that still carries a
# score must not produce one.
_empty_quality_prose = _format_road_corridor_summary(
    _road_narrative([], "no_demand", served_acres=0.0, unserved_acres=0.0, max_grade_pct=0.0, steep_ft=0.0)
)
for _fragment in ("Terrain quality", "out of 100", "76.7"):
    assert _fragment not in _empty_quality_prose, (
        f"an empty network must say nothing about quality, but {_fragment!r} appeared in "
        f"{_empty_quality_prose!r}"
    )

# A None score (no road to score) stays silent too, even on a block that
# reports a network -- never a 0 that would read as terrible ground.
_none_quality_prose = _format_road_corridor_summary(
    _road_narrative(
        branches=[
            {
                "branch_index": 0, "role": "trunk", "joins_branch_index": None,
                "length_ft": 100.0, "newly_served_acres": 1.0, "avg_grade_pct": 3.0,
                "max_grade_pct": 5.0, "steep_ft": 0.0,
                "crosses_floodplain": False, "crosses_production_zone": False,
            }
        ],
        stop_reason="all_demand_served",
        served_acres=1.0, unserved_acres=0.0, max_grade_pct=5.0, steep_ft=0.0,
        terrain_quality_score=None, cost_per_meter_ratio=None, total_path_cost=0.0,
    )
)
assert "Terrain quality" not in _none_quality_prose and "out of 100" not in _none_quality_prose, (
    "a None score must stay silent rather than be narrated as a 0"
)

# A STORED BLOCK THAT PREDATES THE QUALITY KEY still produces a report --
# the same reasoning the retained stop_reason vocabulary carries. Not a
# KeyError, and not a quality sentence either.
_legacy_narrative = _road_narrative(
    branches=[
        {
            "branch_index": 0, "role": "trunk", "joins_branch_index": None,
            "length_ft": 984.3, "newly_served_acres": 2.5, "avg_grade_pct": 4.0,
            "max_grade_pct": 8.0, "steep_ft": 0.0,
            "crosses_floodplain": False, "crosses_production_zone": False,
        }
    ],
    stop_reason="all_demand_served",
    served_acres=2.5, unserved_acres=0.0, max_grade_pct=8.0, steep_ft=0.0,
)
del _legacy_narrative["quality"]
_legacy_prose = _format_road_corridor_summary(_legacy_narrative)
assert "Total network length: 984.3ft" in _legacy_prose and "out of 100" not in _legacy_prose, (
    "a stored narrative block predating the quality key must still format, without a quality sentence"
)

print(
    "Terrain quality: a network with a score states 76.7 out of 100 plainly, with one clause on the "
    "ground the route runs on and no invented qualitative band; an empty network, a None score, and a "
    "stored block predating the key all say nothing quality-related at all."
)


# =====================================================================
# _format_keypoints_summary(): now WIRED into the report prompt (the
# Landform section names Keypoint Candidates). Imperial (feet) at this
# boundary, with a per-keypoint cardinal position when the parcel
# boundary is supplied. Pure dict-to-string formatting, exercised
# directly.
# =====================================================================

from shapely.geometry import Point, box  # noqa: E402

_empty_keypoints_prose = _format_keypoints_summary([])
assert "No keypoints detected" in _empty_keypoints_prose, (
    "an empty keypoint list must format as an honest 'none detected' line, not a placeholder"
)
assert _format_keypoints_summary(None) == _empty_keypoints_prose, (
    "None (detection unavailable) must format the same as an empty list"
)

_kp_boundary = box(0.0, 0.0, 90.0, 90.0)
_kp_fixture = [
    {
        "id": 0,
        "valley_id": 3,
        "point_utm": Point(10.0, 80.0),  # west third, north third -> "northwest"
        "elevation_m": 346.5,            # -> 1136.8 ft
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
        "point_utm": Point(80.0, 45.0),  # east third, middle third -> "east-central"
        "elevation_m": 347.0,            # -> 1138.5 ft
        "contributing_acres": 6.69,
        "slope_above_pct": 15.0,
        "slope_below_pct": 5.5,
        "slope_drop_pct": 9.5,
        "on_parcel": False,
        "distance_outside_boundary_m": 14.0,  # -> ~46 ft
    },
]
_kp_prose = _format_keypoints_summary(_kp_fixture, _kp_boundary)
assert "2 keypoint(s) detected" in _kp_prose
assert "Keypoint 1 (valley 3)" in _kp_prose and "1136.8 ft elevation" in _kp_prose and "6.36 ac" in _kp_prose, (
    "keypoint numbers must display 1-based (id 0 -> 'Keypoint 1') -- ids stay zero-indexed upstream"
)
assert "Keypoint 2 (valley 7)" in _kp_prose and "Keypoint 0" not in _kp_prose
assert "in the parcel's northwest" in _kp_prose, "keypoint 0's cardinal position must be stated"
assert "in the parcel's east-central" in _kp_prose, (
    "keypoint 1's mid-band position must use the '-central' compound form"
)
assert "on parcel" in _kp_prose, "an on-parcel keypoint must be stated as such"
assert "~46 ft outside the boundary" in _kp_prose, "an off-parcel keypoint must state its distance in feet"
assert "12.3%" in _kp_prose, "the slope drop must be stated plainly"
assert " m elevation" not in _kp_prose and " m outside" not in _kp_prose, "no metric units may remain"

_kp_prose_no_boundary = _format_keypoints_summary(_kp_fixture)
assert "in the parcel's" not in _kp_prose_no_boundary, (
    "with no boundary supplied, the position clause is omitted -- never invented"
)
print(
    "_format_keypoints_summary() renders the keypoint list in feet with per-keypoint cardinal "
    "positions (northwest / east-central) when the boundary is supplied, omits positions when it "
    "isn't, and keeps the honest empty line for [] / None."
)


# =====================================================================
# _locative_descriptor(): thirds-of-bounding-box cardinal naming, with
# off-bbox centroids clamped (an off-parcel keypoint within the margin).
# =====================================================================

for _pt, _expected in (
    (Point(10, 80), "northwest"),
    (Point(45, 80), "north-central"),
    (Point(80, 45), "east-central"),
    (Point(45, 45), "central"),
    (Point(80, 10), "southeast"),
    (Point(10, 45), "west-central"),
    (Point(45, 10), "south-central"),
    (Point(-5, 95), "northwest"),  # clamped, not rejected
):
    _got = report_generator._locative_descriptor(_pt, _kp_boundary)
    assert _got == _expected, f"_locative_descriptor({_pt.x}, {_pt.y}) -> {_got!r}, expected {_expected!r}"
print("_locative_descriptor(): all nine cells named correctly; off-bbox centroids clamp to the edge cell.")


# =====================================================================
# Imperial units at the raw-layer formatter boundary: climate (inches,
# degF) and elevation (feet, trimmed to range/relief only -- the raw
# per-point coordinate dump is gone). The elevation figures come off the
# DEM now (raster_grid.elevation_range_in_polygon()'s dict), not off a
# fetched 36-point EPQS lattice, so the sentence counts in-boundary DEM
# CELLS rather than "sample points" -- asserted below.
# =====================================================================

_climate_fixture = {
    "prevailing_wind_direction": "WSW",
    "prevailing_wind_direction_degrees": 245.0,
    "avg_annual_precipitation_mm": 1020.0,   # -> 40.2 in
    "max_daily_precipitation_mm": 95.0,      # -> 3.7 in
    "avg_high_temp_c": 16.5,                 # -> 61.7 F
    "avg_low_temp_c": 6.0,                   # -> 42.8 F
    "record_high_temp_c": 37.0,              # -> 98.6 F
    "record_low_temp_c": -22.0,              # -> -7.6 F
    "years_analyzed": 10,
}
_climate_prose = report_generator._format_climate_summary(_climate_fixture)
assert "40.2 in" in _climate_prose and "3.7 in" in _climate_prose
assert "61.7°F" in _climate_prose and "42.8°F" in _climate_prose
assert "98.6°F" in _climate_prose and "-7.6°F" in _climate_prose
assert " mm" not in _climate_prose and "°C" not in _climate_prose, "no metric units may remain"

_elev_fixture = {
    "min_meters": 326.7,   # -> 1072 ft
    "max_meters": 344.2,   # -> 1129 ft
    "relief_meters": 17.5,
    "cell_count": 4180,
    "resolution_meters": 5.0,
}
_elev_prose = report_generator._format_elevation_summary(_elev_fixture)
assert "Elevation range: 1072ft to 1129ft (total relief: 57ft)" in _elev_prose
assert "40.64286" not in _elev_prose and "-79.98383" not in _elev_prose, (
    "the raw per-point coordinate dump must be gone -- range and relief only"
)
# THE SENTENCE NAMES ITS REAL BASIS. "sample points" was true of a
# 36-point EPQS lattice and is false of a DEM read; a sentence that kept
# the old wording over the new source would be a lie the report tells.
assert "across 4180 DEM cells inside the boundary" in _elev_prose, _elev_prose
assert "sample point" not in _elev_prose, (
    "the elevation figures come off the DEM now -- nothing is sampled"
)
assert report_generator._format_elevation_summary({}) == "No elevation data available."
assert "m\n" not in _elev_prose and " m " not in _elev_prose
print("Climate reads in inches/degF and elevation in feet, range/relief only -- no metric units, no point dump.")
print(f"The report's elevation sentence, from the DEM: {_elev_prose}")


# =====================================================================
# irradiance injection: irradiance= is now WIRED into the prompt as the
# SOLAR IRRADIANCE data block (the Permanent Building Site section's
# rooftop solar viability read) -- the stored-seam inertness earlier
# branches enforced here is deliberately closed by the SoP prompt
# rewrite. A real 'ok' baseline renders its figures; a missing or
# non-'ok' baseline reads as honest no-data. The Anthropic client is
# fully mocked, so no network/LLM call is made.
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
_irr_elevation = {
    "min_meters": 326.7,
    "max_meters": 344.2,
    "relief_meters": 17.5,
    "cell_count": 4180,
    "resolution_meters": 5.0,
}
_irr_water = {"streams": [{"name": "Montour Run", "feature_code": None, "geometry": None}], "water_bodies": []}

# A realistic ParcelData.irradiance dict in get_regional_irradiance_
# baseline()'s own real shape ('ok' status with real figures).
_irr_fixture = {
    "status": "ok",
    "annual_ac_kwh_per_kw": 1240.5,
    "avg_solar_radiation_kwh_per_m2_per_day": 4.21,
    "capacity_factor_pct": 14.2,
    "station_distance_miles": 9.6,
}

_with_irr_content = _capture_prompt(
    soil_components=_irr_soil,
    elevation_summary=_irr_elevation,
    water_features=_irr_water,
    irradiance=_irr_fixture,
)
assert "SOLAR IRRADIANCE (regional baseline):" in _with_irr_content, (
    "the SOLAR IRRADIANCE data block must be injected into the prompt"
)
assert "~1240 AC kWh per kW" in _with_irr_content and "4.21 kWh/m2/day" in _with_irr_content
assert "capacity factor 14.2%" in _with_irr_content and "9.6 miles" in _with_irr_content
assert "informs rooftop solar viability, not site choice" in _with_irr_content

_without_irr_content = _capture_prompt(
    soil_components=_irr_soil,
    elevation_summary=_irr_elevation,
    water_features=_irr_water,
)
assert "No regional irradiance baseline available" in _without_irr_content, (
    "with no irradiance supplied, the block must read as honest no-data"
)
assert "No regional irradiance baseline available" in report_generator._format_irradiance_summary(
    {"status": "fetch_failed", "annual_ac_kwh_per_kw": None}
), "a non-'ok' status must read as no-data, never quote a figure"
print(
    "irradiance injection: an 'ok' baseline renders its figures in the SOLAR IRRADIANCE block; "
    "missing or failed baselines read as honest no-data."
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
    _format_water_survey_areas_summary,
)

# One keypoint-nominated candidate and one
# accumulation-nominated candidate, plus a keypoint that produced nothing
# -- so the prose exercises provenance, flags, the level-pool block and the
# unproductive-keypoint reason-code line in a single fixture.
def _water_zone_block(zone_id, nominated_by, keypoint_id, valley_id, off_parcel, flags, abut_left_found):
    return {
        "id": zone_id,
        "area_acres": 0.5,
        "provenance": {
            "nominated_by": nominated_by,
            "keypoint_id": keypoint_id,
            "valley_id": valley_id,
            "anchor_off_parcel": off_parcel,
            "anchor_distance_outside_boundary_ft": 24.6 if off_parcel else 0.0,
            # The wall sits below the keypoint; family 2's anchor IS its
            # wall, so it carries 0.0.
            "wall_offset_downstream_ft": 410.1 if nominated_by == "keypoint" else 0.0,
        },
        "flags": flags,
        "location": {"position_in_parcel": "southwest", "elevation_percentile_of_parcel": 22.7},
        "drainage": {
            "contributing_area_acres": 1.9,
            "contributing_area_ceiling_acres": 20.0,
            "slope_median_pct": 40.0,
        },
        "level_pool": {
            "reference_height_ft": 8.2,
            "dam_band_width_ft": 114.8,
            "abutment_found_left": abut_left_found,
            "abutment_found_right": True,
            "abutment_distance_left_ft": 49.2 if abut_left_found else None,
            "abutment_distance_right_ft": 49.2,
            "crosses_major_drainage_left": False,
            # The RIGHT side is truncated at a neighbouring drainage -- a
            # different finding from an abutment that was not found, and
            # the prose must say so distinctly.
            "crosses_major_drainage_right": not abut_left_found,
            "major_drainage_distance_left_ft": None,
            "major_drainage_distance_right_ft": None if abut_left_found else 82.0,
            "backwater_cell_count": 65,
            "stem_upstream_length_ft": 246.1,
            "anchor_bearing_deg": 161.6,
            "stations": [
                {"station_index": 0, "offset_upstream_ft": 0.0, "status": "measured",
                 "along_stem_distance_ft": 0.0, "bearing_deg": 161.6, "flooded_width_ft": 82.0,
                 "flooded_cross_section_area_sqft": 349.8},
                {"station_index": 1, "offset_upstream_ft": 82.0, "status": "measured",
                 "along_stem_distance_ft": 82.0, "bearing_deg": 180.0, "flooded_width_ft": 49.2,
                 "flooded_cross_section_area_sqft": 215.3},
                # A station past the end of the traced channel: NOT dry,
                # unmeasured. The prose must say so rather than printing
                # a zero width.
                {"station_index": 2, "offset_upstream_ft": 164.0, "status": "unreachable_stem_end",
                 "along_stem_distance_ft": 98.4, "bearing_deg": None, "flooded_width_ft": None,
                 "flooded_cross_section_area_sqft": None},
            ],
        },
        "overlap": {"canopy_overlap_pct": 12.5, "road_overlap_pct": 0.0},
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
    }


_water_zone_0 = _water_zone_block(
    0, "keypoint", 1, 0, True, ["anchor_off_parcel", "truncated_by_boundary"], True
)
_water_zone_1 = _water_zone_block(1, "accumulation", None, None, False, [], False)
_water_nd = {
    "zone_found": True,
    "candidate_count": 2,
    "production_area_count": 2,
    "gates": {"canopy_data_available": True, "road_data_available": True},
    "nomination": {
        "keypoints_considered": 3,
        "keypoint_outcomes": [
            {"keypoint_id": 1, "valley_id": 0, "contributing_acres": 8.0, "outcome": "nominated",
             "candidate_id": 0, "on_parcel": False, "distance_outside_boundary_ft": 24.6,
             "flags": ["anchor_off_parcel"]},
            {"keypoint_id": 0, "valley_id": 0, "contributing_acres": 3.0,
             "outcome": "too_close_to_candidate_0", "candidate_id": None, "on_parcel": True,
             "distance_outside_boundary_ft": 0.0, "flags": []},
            {"keypoint_id": 2, "valley_id": 1, "contributing_acres": 4.0,
             "outcome": "below_min_area", "candidate_id": None, "on_parcel": False,
             "distance_outside_boundary_ft": 41.0, "flags": ["anchor_off_parcel"]},
        ],
        "accumulation_seeds": [{"outcome": "nominated", "candidate_id": 1, "flags": []}],
    },
    "zones": [_water_zone_0, _water_zone_1],
    "zone": _water_zone_0,
}
_water_prose = _format_water_candidate_zones_summary(_water_nd)
assert "2 candidate survey area(s) identified" in _water_prose
assert "3 detected keypoint(s)" in _water_prose
assert "in the parcel's southwest" in _water_prose and "22.7 elevation percentile" in _water_prose
assert "1.9 acre(s)" in _water_prose and "20.0-acre siltation/peak-flow" in _water_prose
assert "nominated from keypoint 1 (valley 0)" in _water_prose
# THE KEYPOINT IS THE POOL'S TAIL. The prose must place the wall
# downstream of it, or a reader puts the structure on the keypoint marker.
assert "anchored 410.1 ft DOWNSTREAM of it" in _water_prose, (
    "a keypoint candidate must state how far below its keypoint the wall stands"
)
assert "the keypoint is the upstream tail of the water it would hold" in _water_prose
assert "such an anchor is already a wall site" in _water_prose, (
    "family 2 has no keypoint above it, and the prose must say so rather than reporting a bare 0 ft "
    "offset that reads like a coincidence"
)
assert "24.6 ft" in _water_prose and "OUTSIDE the drawn boundary" in _water_prose, (
    "an off-parcel anchor must be stated as a dam at the property edge, with its measured distance"
)
assert "ON-PARCEL portion of the pool" in _water_prose
assert "nominated from the highest remaining flow accumulation" in _water_prose
assert "NOT A PROPOSED DAM HEIGHT" in _water_prose
assert "8.2 ft reference waterline" in _water_prose
assert "49.2 ft out" in _water_prose
assert "NOT FOUND within the search width" in _water_prose, (
    "an abutment that was not found must be stated as a finding, never omitted or shown as 0 ft"
)
assert "flooded cross-sectional area 349.8 sq ft" in _water_prose
assert "bearing 161.6 deg" in _water_prose, "a measured station states the direction it faces"
assert "NOT MEASURED" in _water_prose and "98.4 ft upstream of the dam line" in _water_prose, (
    "a station past the end of the traced channel must be stated as unmeasured, with how far the channel "
    "actually reached"
)
assert "absence of a measurement, not a dry cross-section" in _water_prose
assert "NOT a storage capacity" in _water_prose and "no volume is computed" in _water_prose
assert "canopy 12.5%" in _water_prose and "NOT used to shrink the zone" in _water_prose
assert "Flags: anchor_off_parcel, truncated_by_boundary." in _water_prose
assert "too_close_to_candidate_0" in _water_prose and "below_min_area" in _water_prose, (
    "keypoints that produced no candidate must be reported with the reason code that stopped them"
)
assert "41.0 ft off parcel" in _water_prose, (
    "an off-parcel keypoint's distance must sit next to its outcome -- it is what makes the outcome legible"
)
assert "SECOND major drainage" in _water_prose and "82.0 ft" in _water_prose, (
    "a dam axis truncated at a neighbouring drainage is a distinct finding and must be stated as one"
)
assert "can_gravity_feed: True" in _water_prose and "A gravity-feed relationship." in _water_prose
assert "would need a pump" in _water_prose, "a below-elevation relationship must be stated as pump-required"
assert "gradient undefined" in _water_prose, "a distance-0 relationship must state its gradient is undefined, not 0%"
assert "NOT specific pond/dam sites" in _water_prose
assert "No water system candidate survey area identified" in _format_water_candidate_zones_summary(None)
assert "No water system candidate survey area identified" in _format_water_candidate_zones_summary(
    {"zone_found": False, "candidate_count": 0, "production_area_count": 0, "gates": {},
     "nomination": {"keypoints_considered": 0, "keypoint_outcomes": [], "accumulation_seeds": []},
     "zones": [], "zone": None}
)
print("_format_water_candidate_zones_summary(): N candidates with provenance, flags, level-pool "
      "measurements (no capacity anywhere), reported overlaps, per-keypoint reason codes, and "
      "gravity-vs-pump all rendered from the block; no-candidate and missing blocks read as no data. "
      "(The formatter belongs to the DEMOTED level-pool layer and is off the pipeline path.)")

# =====================================================================
# _format_water_survey_areas_summary(): the survey-area water step's
# narrative block (water_survey_areas.build_narrative_data()'s shape) --
# the formatter the pipeline path actually calls now.
# =====================================================================

_survey_nd = {
    "zone_found": True,
    "zone_count": 2,
    "dropped_count": 1,
    "member_region_count": 3,
    "embankment_zone_count": 1,
    "excavated_zone_count": 1,
    "suitability_threshold": 0.5,
    "grouping_distance_meters": 30.0,
    "twi_is_absolute": True,
    "twi_note": (
        "Topographic wetness is scored on a FIXED ABSOLUTE curve over raw ln(a/tan(beta)), so a "
        "given cell scores the same whatever boundary is drawn around it. The one caveat is "
        "calibration: the breakpoints are calibrated at the 5 m reference DEM's resolution."
    ),
    "gates": {"on_parcel_cells": 612, "ceiling_removed_cells": 4, "setback_removed_cells": 0,
              "gated_cells": 608, "max_contributing_area_acres": 20.0},
    "soil_checked": True,
    "embankment_generation": "seed_compartment",
    "embankment_seed_count": 3,
    "embankment_failed_seed_count": 2,
    "embankment_failed_seeds": [
        {"rowcol": [12, 4], "blend_score": 0.58, "reason_code": "no_constriction"},
        {"rowcol": [30, 9], "blend_score": 0.52, "reason_code": "duplicate_of_zone_1"},
    ],
    "selection": {"selected_zone_id": 0, "selected_survey_type": "excavated",
                  "selection_rule": "pooled_member_mean_suitability_member_acreage_tiebreak"},
    # THE PRESENTATION BLOCK: the rule applied and the per-type SURVIVOR
    # totals. Two survivors, both presented -- the count is a cap, so
    # there is nothing to hold back and the prose must not pretend there
    # is (the "first N are the ones to lead with" phrasing would invent
    # a distinction this run does not have).
    "presentation": {
        "presentation_count": 4,
        "per_type_count": 2,
        "survivor_counts": {"embankment": 1, "excavated": 1},
        "presented_count": 2,
        "presented_counts": {"embankment": 1, "excavated": 1},
        "base_counts": {"embankment": 1, "excavated": 1},
        "backfill_counts": {"embankment": 0, "excavated": 0},
        "backfill_applied": False,
        "presented_zone_ids": [1, 0],
        "rule_applied": "1 embankment + 1 excavated",
    },
    "zones": [
        # LISTED IN PRESENTATION ORDER, the shape build_narrative_data()
        # emits: the embankment compartment is presentation #1 (the
        # interleave leads with embankment), the excavated zone #2. Both
        # are presented here -- two survivors is under the cap -- so this
        # fixture drives the "everything surviving is also presented" arm
        # of the counts line; the capped arm has its own fixture below.
        # The embankment block is a VALLEY COMPARTMENT since the
        # compartment change: no members, the SEED's anchor claim beside
        # the compartment's own criterion means (the honesty split).
        {"id": 1, "survey_type": "embankment", "rank": 1, "presented": True, "presentation_order": 1, "zone_acres": 0.8,
         "mean_suitability": 0.55, "max_suitability": 0.61,
         "seed_blend_score": 0.73,
         "seed_criteria_signature": {"slope": 1.0, "soil": 0.5, "twi": 0.9},
         # THE FILL CLAIM, the third separately-reported number: the
         # catchment at the DAM REACH (not at the seed) and the
         # unchanged drainage band scored on it, plus the composite the
         # rank was assigned on. 6.2 ac is past the band's 2 ac full
         # credit and well under the 20 ac ceiling -> 1.0.
         "pinch_catchment_acres": 6.2, "pinch_drainage_score": 1.0,
         "catchment_ceiling_acres": 20.0, "compartment_rank_score": 0.865,
         "pinch_width_ft": 88.6, "pinch_walk_distance_ft": 137.8, "baseline_length_ft": 137.8,
         # A TERMINAL pinch at the property line, still narrowing -- the
         # accepted-not-refused disclosure the caveat sentence keys on.
         "pinch_terminal": "boundary", "still_narrowing_at_termination": True,
         "width_profile_min_ft": 88.6, "width_profile_max_ft": 137.8,
         "truncated_by_boundary": True, "truncated_by_road": False, "half_width_bound_hit": False,
         "criteria": {"slope": {"weight": 0.36, "mean_score": 0.6},
                      "soil": {"weight": 0.36, "mean_score": 0.5},
                      "twi": {"weight": 0.28, "mean_score": 0.7}},
         "twi_score_mean": 0.9, "depression_depth_max_ft": 0.0,
         "contributing_area_acres_at_wettest_cell": 5.4, "boundary_adjacency_pct": 0.0,
         "overlaps": {"canopy_pct": 0.0, "road_pct": 0.0, "production_pct": None},
         "gravity": {"has_service_relationship": False, "can_gravity_feed": None},
         "flags": ["truncated_by_boundary", "no_service_relationship"], "below_min_area": False,
         "cross_type_overlaps": [], "either_type_candidate": False,
         "confidence": "medium"},
        {"id": 0, "survey_type": "excavated", "rank": 1, "presented": True, "presentation_order": 2, "member_count": 2,
         "member_acres": 1.4, "zone_acres": 2.6,
         "mean_suitability": 0.71, "max_suitability": 0.83,
         "criteria": {"wetness": {"weight": 0.35, "mean_score": 0.82},
                      "soil": {"weight": 0.30, "mean_score": 0.9},
                      "slope": {"weight": 0.25, "mean_score": 0.95},
                      "drainage_runon": {"weight": 0.10, "mean_score": 0.1}},
         "twi_score_mean": 0.88, "depression_depth_max_ft": 1.3,
         "contributing_area_acres_at_wettest_cell": 3.2, "boundary_adjacency_pct": 42.0,
         "overlaps": {"canopy_pct": 12.5, "road_pct": None, "production_pct": 0.0},
         "gravity": {"has_service_relationship": True, "can_gravity_feed": False,
                     "production_area_id": 3, "elevation_differential_ft": -18.4, "distance_ft": 210.0},
         "flags": ["sparse_anchor"], "below_min_area": False, "sparse_anchor": True,
         "truncated_by_road": False,
         "cross_type_overlaps": [{"zone_id": 1, "overlap_pct": 62.0}], "either_type_candidate": True,
         "confidence": "high"},

    ],
}
_survey_prose = _format_water_survey_areas_summary(_survey_nd)
assert "2 water SURVEY ZONE(S)" in _survey_prose
assert "1 embankment-type" in _survey_prose and "1 excavated-type" in _survey_prose
assert "3 member region(s)" in _survey_prose, "the member count travels with the excavated framing"
assert "GENERATED DIFFERENTLY" in _survey_prose and "VALLEY COMPARTMENT" in _survey_prose, (
    "the opener states the per-type generation mechanisms -- the compartment change's design claim"
)
assert "GENERAL AREAS WORTH SURVEYING" in _survey_prose
assert "no pool, wall, volume, or station" in _survey_prose, (
    "the redesign's central promise -- no precision theater -- must be stated in the prose"
)
# THE CAVEAT SWAP: TWI is absolute now, so the old "wettest on THIS
# parcel" claim is FALSE and must not survive anywhere in the prose; the
# resolution-calibration caveat is the one that is still true and still
# has to reach the prompt.
assert "THIS parcel" not in _survey_prose, (
    "the retired parcel-relative TWI claim must not survive in the prose -- TWI is absolute"
)
assert "calibrated at the 5 m reference DEM" in _survey_prose, (
    "the resolution-calibration caveat that REPLACED it must reach the prompt, so the report "
    "states the real limitation instead of the retired one"
)
assert "All 2 surviving zone(s) are listed and presented, ranked per type" in _survey_prose, (
    "with every survivor inside the presented set the counts line says exactly that -- no "
    "'the first N are the ones to lead with' distinction this run does not have"
)
assert "nothing surviving is held back" in _survey_prose
assert "you decide which to walk" in _survey_prose
assert "presented #1" in _survey_prose and "presented #2" in _survey_prose, (
    "each zone's sentence carries its place in the presented set, so the prose's order is "
    "attributable to the rule rather than to the order the blocks happened to arrive in"
)
assert "1 zone(s) were dropped (under the 0.1-acre floor, or a duplicate of a better-seeded compartment)" in _survey_prose, (
    "the drops are stated with their possible reasons, never silent"
)
assert "each pond type's best zone appears" not in _survey_prose, (
    "the deleted per-type guarantee's phrasing must not resurface"
)
assert "2 of 3 embankment seed(s) produced NO compartment" in _survey_prose, (
    "the failed-seed accounting reaches the prose -- a reach with no on-parcel pinch reports honestly"
)
assert "blend 0.58: no_constriction" in _survey_prose, (
    "each failed seed's reason code is named in the prose"
)
assert "EITHER-TYPE CANDIDATE" in _survey_prose and "zone 1 (62.0% of this envelope)" in _survey_prose, (
    "the cross-type agreement renders as the consultant either-type line with its overlap numbers"
)
assert "evaluate both approaches during the survey" in _survey_prose
assert "SPARSE ANCHOR" in _survey_prose and "scattered good ground within a larger area" in _survey_prose, (
    "the sparse-anchor honesty line renders when the flag is set (excavated-only since the change)"
)
assert "dugout or seep-fed excavated pond" in _survey_prose, "the seep-widened excavated framing reaches the prose"
assert "Selected for downstream planning: zone 0 (excavated-type)" in _survey_prose
assert "provisional selection rule" in _survey_prose
assert (
    "embankment by its compartment rank score -- seed blend combined with the catchment at its "
    "dam reach -- excavated by member-mean suitability"
) in _survey_prose, (
    "the pooled rule states each type's own instrument"
)
assert "2.6 acres to survey, anchored by 1.4 acres of high-suitability ground" in _survey_prose, (
    "the DUAL-ACREAGE sentence is the excavated narrative's spine -- both numbers, both labeled"
)
assert "0.8 acres to survey -- a valley compartment anchored by a 0.73-scoring storage cell, dam reach at the downstream end" in _survey_prose, (
    "the compartment sentence carries the seed's anchor claim, verbatim per the design"
)
assert "THE HONESTY SPLIT" in _survey_prose and "twi 0.9" in _survey_prose and "twi 0.7 (weight 0.28)" in _survey_prose, (
    "seed signature and compartment means are BOTH in the prose, distinct -- the reporting honesty split"
)
# --- THE DISPLAY SCALE IN THE PROSE ---------------------------------
# The suitability figures -- and ONLY those -- are rendered on the 0-100
# KSOP display scale, with "/100" on every occurrence so no figure in
# this paragraph can be read as either scale. The block they were read
# from is the measurement record and is untouched 0-1; the conversion
# happens here, at the point of rendering, through the one shared helper.
assert "compartment mean 55/100, max 61/100" in _survey_prose, (
    f"the embankment compartment's mean/max render on the display scale: {_survey_prose}"
)
assert "member-cell mean suitability 71/100 (max 83/100)" in _survey_prose, (
    "the excavated zone's mean/max render on the display scale, with the scale spelled out"
)
assert "mean 0.55" not in _survey_prose and "suitability 0.71" not in _survey_prose, (
    "the 0-1 spelling of a suitability figure must not survive in the prose -- two scales for one "
    "number in one paragraph is the ambiguity this branch exists to prevent"
)
assert _survey_nd["zones"][0]["mean_suitability"] == 0.55, (
    "the formatter READS narrative_data; it never rescales the block it was handed"
)
assert _survey_nd["zones"][1]["max_suitability"] == 0.83
# EVERYTHING ELSE ON THESE LINES STAYS 0-1, printed beside the recipe
# that produced it: the criterion means, the seed blend, the drainage
# score, the rank composite.
assert "0.73-scoring storage cell" in _survey_prose, "the seed blend score stays 0-1"
assert "slope 0.6 (weight 0.36)" in _survey_prose, "a criterion mean stays 0-1"
assert "scoring 1.0 on the drainage band" in _survey_prose, "the drainage score stays 0-1"
assert "Rank score 0.865" in _survey_prose, "the ranking composite stays 0-1"
assert "drainage_area" not in _survey_prose, (
    "contributing area is no longer a per-cell embankment criterion, so it must not appear as one "
    "in the prose -- it appears as the compartment's FILL CLAIM instead"
)
# THE FILL CLAIM AS ITS OWN SENTENCE, with all three numbers legible and
# none of them standing in for another: the catchment, the band score,
# and the composite whose recipe the sentence names.
assert "6.2 acres of catchment drain through the dam reach" in _survey_prose, (
    "the catchment is measured AT THE PINCH and the prose says where"
)
assert "scoring 1.0 on the drainage band" in _survey_prose
assert "not at the seed" in _survey_prose, (
    "the sentence states which cell was measured -- the whole finding is that it used to be the "
    "wrong one"
)
assert "Rank score 0.865" in _survey_prose and "provisional" in _survey_prose, (
    "the composite is published with its inputs and flagged as the v1 prior it is"
)
assert "OVERSTATES dam length" in _survey_prose, (
    "crest-to-crest width is a survey measure, not a dam length -- the caveat must reach the prose"
)
assert "TERMINAL PINCH: the valley continues to narrow beyond the property line" in _survey_prose, (
    "the accepted terminal pinch discloses its terminator with the still-narrowing clause"
)
assert "narrowest buildable crossing within the surveyed extent" in _survey_prose, (
    "the dam-at-the-edge doctrine's claim, verbatim intent"
)
assert "88.6-137.8 ft" in _survey_prose, "the walked width profile's extremes ride the caveat"
assert "TRUNCATED by the property boundary" in _survey_prose, (
    "a clipped compartment says where its drawn geometry stops"
)
assert "wetness 0.82 (weight 0.35)" in _survey_prose, (
    "per-criterion mean scores are the narrative-honesty mechanism -- prose may only claim what a "
    "criterion actually scored, so the scores themselves must be in the prompt"
)
assert "over the ANCHORING ground only" in _survey_prose, (
    "the excavated prose must say the scores describe member cells, never the envelope"
)
assert "PUMP-REQUIRED" in _survey_prose and "18.4 ft BELOW production area 3" in _survey_prose
assert "No production area within service range" in _survey_prose, (
    "the no-service case reads as a flag, never as a dropped region"
)
assert "roads NOT CHECKED" in _survey_prose, "a never-checked overlap must read NOT CHECKED, not 0%"
assert "the road exclusion REMOVED" in _survey_prose, (
    "the road figure's clipped-geometry semantics are stated where the numbers are"
)
assert "production ground NOT CHECKED" in _survey_prose
assert "canopy 12.5%" in _survey_prose
assert "42.0% of this area's perimeter" in _survey_prose, "boundary adjacency is site-visit context"
assert "Flags: truncated_by_boundary, no_service_relationship." in _survey_prose, (
    "flags ride into the prose -- flagged, never filtered"
)
assert "TUNE FROM FIRST RUN" in _survey_prose and "isobands" in _survey_prose
assert "No water survey areas were identified" in _format_water_survey_areas_summary(None)
assert "No water survey areas were identified" in _format_water_survey_areas_summary({"region_found": False})
print("_format_water_survey_areas_summary(): excavated dual-acreage and compartment honesty-split "
      "sentences, seed-failure accounting, the TWI caveat, pump/no-service gravity cases, overlap "
      "sentinels, flags, and the tuning note all rendered; no-region and missing blocks read as no data.")
print("  ... and the suitability figures ALONE render on the 0-100 display scale (55/100, 61/100, "
      "71/100, 83/100 off an untouched 0.55/0.61/0.71/0.83 block) while the criterion means, seed "
      "blend, drainage score and rank composite stay 0-1 beside their own recipes.")

# --- THE CAPPED ARM of the counts line: MORE SURVIVORS THAN THE
# PRESENTED SET HOLDS. This is the arm that has to get the claim exactly
# right, because it is the one where the prose could mislead: the
# presented set leads, the rest are still described in full below it,
# and the sentence must say so rather than reading as a shortlist. Built
# from the fixture above with three extra embankment survivors, so the
# rule backfills a third embankment into the excavated zone's missing
# second slot.
_capped_nd = dict(_survey_nd)
_capped_extra = []
for _extra_rank, _extra_id in ((2, 2), (3, 3), (4, 4)):
    _extra = dict(_survey_nd["zones"][0])
    _extra.update({
        "id": _extra_id,
        "rank": _extra_rank,
        "presented": _extra_rank <= 3,
        "presentation_order": {2: 3, 3: 4}.get(_extra_rank),
        "cross_type_overlaps": [],
        "either_type_candidate": False,
    })
    _capped_extra.append(_extra)
_capped_nd["zones"] = [
    _survey_nd["zones"][0], _survey_nd["zones"][1], _capped_extra[0], _capped_extra[1],
    _capped_extra[2],
]
_capped_nd["zone_count"] = 5
_capped_nd["embankment_zone_count"] = 4
_capped_nd["presentation"] = {
    "presentation_count": 4,
    "per_type_count": 2,
    "survivor_counts": {"embankment": 4, "excavated": 1},
    "presented_count": 4,
    "presented_counts": {"embankment": 3, "excavated": 1},
    "base_counts": {"embankment": 2, "excavated": 1},
    "backfill_counts": {"embankment": 1, "excavated": 0},
    "backfill_applied": True,
    "presented_zone_ids": [1, 0, 2, 3],
    "rule_applied": "2 embankment + 1 excavated + 1 embankment backfill",
}
_capped_prose = _format_water_survey_areas_summary(_capped_nd)
assert "All 5 surviving zone(s) are listed, ranked per type (4 embankment, 1 excavated)" in _capped_prose, (
    "WHAT WAS CONSIDERED, stated as per-type survivor totals, beside what is shown"
)
assert "The first 4 below are the ones to lead with" in _capped_prose, "what is shown, and that it leads"
assert "2 embankment + 1 excavated + 1 embankment backfill" in _capped_prose, (
    "the rule itself, in the prose -- the report explains its own presented set"
)
assert "the rest are described in full after them and are not ruled out" in _capped_prose, (
    "THE CLAIM THAT KEEPS THIS HONEST: an unpresented survivor is not a rejected one, and the "
    "prose that leads with four must say so or it reads as a shortlist"
)
assert "presented #4" in _capped_prose, "the fourth slot -- the backfilled compartment -- is marked"
assert "also surviving -- below the presented set" in _capped_prose, (
    "and the unpresented one says what it is, rather than reading as an unexplained extra"
)
assert "nothing surviving is held back" not in _capped_prose, (
    "the uncapped arm's sentence must not appear on a run that does hold zones back from the lead"
)
print("_format_water_survey_areas_summary(): the capped arm names the presented set, the rule that "
      "produced it, the per-type survivor totals, and states that the rest are described below and "
      "not ruled out.")

_solar_nd = {
    "site_found": True,
    "candidate_count": 5,
    "road_proximity_source": "selected_road_corridor",
    "no_candidates": None,
    "gates": {
        "existing_canopy_excluded": True,
        "water_zone_excluded": True,
        "tree_zone_exclusion_checked": True,
        "road_proximity_source": "selected_road_corridor",
        "prime_farmland_checked": True,
        "hydric_gate_checked": True,
        "floodplain_gate_checked": True,
        "drainage_gates_checked": ["outside_hydric_soil", "outside_floodplain"],
    },
    "selected_site": {
        "score": 87.5,
        "footprint_acres": 0.1,
        "solar_rating": "excellent",
        "solar_value": 97.5,
        "location": {
            "position_in_parcel": "southeast",
            "elevation_percentile_of_parcel": 71.4,
            "elevation_position": "upper field",
            "production_zone_relationship": "inside",
            "distance_to_production_edge_ft": 42.7,
            # NEGATIVE: this site's point sits INSIDE a block, 42.7 ft in
            # from its nearest edge -- which is what makes "42.7ft in from
            # its nearest edge" and "42.7ft to the nearest block" two
            # different sentences instead of one ambiguous number.
            "signed_distance_to_production_ft": -42.7,
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
# BLOCK, not "production zone" -- the interface's word, as the roads
# section already requires.
assert "INSIDE a BLOCK" in _solar_prose
assert "42.7ft in from its nearest edge" in _solar_prose, (
    "inside a block the prose must say how far IN, off the signed distance -- not repeat the "
    "unsigned number as though it were a distance TO production"
)
assert "This is NOT 'distance to production'" in _solar_prose
assert "18.0ft to the property's own selected road corridor" in _solar_prose
assert "facing south" in _solar_prose and "slope 84.0" in _solar_prose
assert "PRIME FARMLAND CONFLICT" in _solar_prose
# THE SOLAR RATING IS A BAND, AND THE PROSE MUST NOT LET IT READ AS A RANK.
assert "Solar rating: excellent (97.5/100" in _solar_prose
assert "ABSOLUTE band, not a rank" in _solar_prose
# THE ELEVATION POSITION, with the percentile explained inline.
assert "on its upper field" in _solar_prose and "71.4 elevation percentile" in _solar_prose
# BOTH DRAINAGE GATES RAN, so the site is reported clear of both.
assert "clear of poorly drained (hydric) soil and floodplain" in _solar_prose

# AND A GATE THAT DID NOT RUN IS NOT A GATE THE SITE PASSED.
import copy as _copy_for_drainage  # noqa: E402

_solar_nd_no_hydric = _copy_for_drainage.deepcopy(_solar_nd)
_solar_nd_no_hydric["gates"]["hydric_gate_checked"] = False
_solar_nd_no_hydric["gates"]["drainage_gates_checked"] = ["outside_floodplain"]
_no_hydric_prose = _format_solar_candidate_zones_summary(_solar_nd_no_hydric)
assert "clear of floodplain." in _no_hydric_prose
assert "SSURGO hydric-soil data was NOT available this run" in _no_hydric_prose
assert "NOT confirmed clear of that ground" in _no_hydric_prose

# OUTSIDE a block reads as a distance TO it, with no sign in the prose.
_solar_nd_outside = _copy_for_drainage.deepcopy(_solar_nd)
_solar_nd_outside["selected_site"]["location"]["production_zone_relationship"] = "adjacent"
_solar_nd_outside["selected_site"]["location"]["signed_distance_to_production_ft"] = 42.7
_outside_prose = _format_solar_candidate_zones_summary(_solar_nd_outside)
assert "42.7ft OUTSIDE the nearest BLOCK's edge" in _outside_prose
assert "INSIDE a BLOCK" not in _outside_prose
assert "ft from the selected water-system zone" not in _solar_prose and "210.0" not in _solar_prose, (
    "distance_to_water_zone_ft stays on the narrative block but must NOT be reported -- "
    "building-to-future-water distance isn't actionable and reads as filler (the water-zone HARD "
    "EXCLUSION mention in the closing guidance is fine; the distance figure is what must be gone)"
)

import copy  # noqa: E402

_solar_nd_unchecked = copy.deepcopy(_solar_nd)
_solar_nd_unchecked["gates"]["prime_farmland_checked"] = False
_solar_nd_unchecked["selected_site"]["benefits"]["prime_farmland_conflict"] = None
assert "NOT checked this run" in _format_solar_candidate_zones_summary(_solar_nd_unchecked), (
    "an unchecked prime-farmland flag (None) must read as not-checked, never as no-conflict"
)
assert "No solar structure candidate site identified" in _format_solar_candidate_zones_summary(None)

# --- THE ZERO-CANDIDATE OUTCOME NAMES THE GROUND THAT DID IT ----------
# A fully gated parcel is a real finding, not a failed run, and the prompt
# has to say so or the report will hedge it into nothing.
_solar_nd_gated = {
    "site_found": False,
    "candidate_count": 0,
    "road_proximity_source": "selected_road_corridor",
    "gates": dict(_solar_nd["gates"]),
    "selected_site": None,
    "no_candidates": {
        "pads_sampled": 48,
        "pads_measurable": 41,
        "blocking_gates": [
            {"gate": "outside_hydric_soil", "pads_rejected": 33},
            {"gate": "outside_floodplain", "pads_rejected": 19},
            {"gate": "outside_existing_canopy", "pads_rejected": 5},
        ],
        "reason": "every_pad_failed_a_gate",
    },
}
_gated_prose = _format_solar_candidate_zones_summary(_solar_nd_gated)
assert "all 41 measurable building pad(s)" in _gated_prose
assert "poorly drained (hydric) soil (33 of 41 pad(s))" in _gated_prose, _gated_prose
assert "floodplain (19 of 41 pad(s))" in _gated_prose
assert "existing tree canopy (5 of 41 pad(s))" in _gated_prose
assert "REAL FINDING ABOUT THIS PROPERTY" in _gated_prose
assert "do NOT suggest a site anyway" in _gated_prose
# The gates are named most-rejections-first, which is the order that says
# which one to argue with.
assert _gated_prose.index("hydric") < _gated_prose.index("floodplain") < _gated_prose.index("canopy")

# "nothing could hold a pad at all" is a DIFFERENT sentence.
_solar_nd_unpaddable = {
    "site_found": False, "candidate_count": 0, "road_proximity_source": "unavailable",
    "gates": dict(_solar_nd["gates"]), "selected_site": None,
    "no_candidates": {
        "pads_sampled": 9, "pads_measurable": 0, "blocking_gates": [],
        "reason": "no_pad_was_measurable",
    },
}
_unpaddable_prose = _format_solar_candidate_zones_summary(_solar_nd_unpaddable)
assert "of 9 grid position(s) tested" in _unpaddable_prose
assert "NONE could hold a building pad at all" in _unpaddable_prose

print("_format_solar_candidate_zones_summary(): selected site's location/benefits rendered from the block; "
      "unchecked prime farmland reads as not-checked; missing block reads as no data.")
print(
    "  BLOCK prose, the SIGNED production distance ('42.7ft in from its nearest edge' inside vs "
    "'42.7ft OUTSIDE the nearest BLOCK's edge' outside), the solar rating stated as an ABSOLUTE "
    "band rather than a rank, the elevation position with its percentile explained, a drainage "
    "gate that did not run reported as NOT confirmed clear, and the ZERO-CANDIDATE outcome naming "
    "every blocking ground most-rejections-first -- 'no_pad_was_measurable' kept a separate "
    "sentence from 'every_pad_failed_a_gate'."
)

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
assert "Patch 1 (rank 1): 9.1 acres (52.3% of parcel), in the parcel's southeast, score 78.2/100" in _prod_prose, (
    "patch numbers must display 1-based (id 0 -> 'Patch 1') -- ids stay zero-indexed upstream"
)
assert "Patch 0" not in _prod_prose, "no zero-indexed patch number may reach the narrative"
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
assert "hydric overlap 100.0" in _tree_prose and "slope 36.6" in _tree_prose
assert "soil marginality" not in _tree_prose and "stream proximity" not in _tree_prose, (
    "the soil marginality and stream proximity factors (and the factor weights) stay on the "
    "narrative block but must NOT be reported -- internal scoring inputs a farmer can't read"
)
assert "40.0%" not in _tree_prose and "10.0%" not in _tree_prose, (
    "the four factor weights are internal scoring configuration and must not reach the prompt"
)
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
    "water_survey_areas": _survey_nd,
    "road_corridors": gentle_narrative,
    "solar_suitability": _solar_nd,
    "tree_zone_candidates": _tree_nd,
}
_wired_prompt = _capture_prompt(
    soil_components=_irr_soil,
    elevation_summary=_irr_elevation,
    water_features=_irr_water,
    narrative_data=_full_narrative,
)
for _header in (
    "PRODUCTION AREAS",
    "KEYPOINT CANDIDATES",
    "WATER SYSTEM SURVEY AREAS",
    "SUGGESTED ROAD CORRIDOR",
    "TREE CROP AREAS",
    "PERMANENT BUILDING SITE",
    "SOLAR IRRADIANCE",
):
    assert _header in _wired_prompt, f"data_summary must carry the {_header} section"
assert "2 water SURVEY ZONE(S)" in _wired_prompt and "PUMP-REQUIRED" in _wired_prompt, (
    "the survey-zone water block (narrative_data['water_survey_areas']) must land in the prompt"
)
assert "The network reaches all identified production ground." in _wired_prompt
assert "Patch 1 (rank 1)" in _wired_prompt and "Rank 1: 0.4 acres" in _wired_prompt

_unwired_prompt = _capture_prompt(
    soil_components=_irr_soil,
    elevation_summary=_irr_elevation,
    water_features=_irr_water,
)
for _no_data in (
    "No production-area candidate data available",
    "No water survey areas were identified",
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


# =====================================================================
# SYSTEM_PROMPT sanity: the rewritten Scale of Permanence prompt carries
# all ten sections in order and the legend-name discipline.
# =====================================================================

_sp = report_generator.SYSTEM_PROMPT
assert "write all ten" in _sp, "the section header line must ask for all TEN sections"
_sp_sections = (
    "1. Introduction", "2. Climate", "3. Landform", "4. Water System Survey Area",
    "5. Suggested Road Corridor", "6. Tree Crop Areas", "7. Permanent Building Site",
    "8. Fencing", "9. Soil", "10. Summary",
)
_last = -1
for _sec in _sp_sections:
    _idx = _sp.find(_sec)
    assert _idx != -1, f"SYSTEM_PROMPT must contain section {_sec!r}"
    assert _idx > _last, f"section {_sec!r} out of order"
    _last = _idx
assert "feet, acres, inches, and °F" in _sp, "the imperial-units instruction must be present"
assert "Keypoint Candidates" in _sp and "Water System Survey Area" in _sp, (
    "the legend-name list must be present"
)
# Round-two refinements: the no-numeric-scores rule, the reworked
# framework paragraph (read-not-decided, influence-not-constraint), and
# the deleted redundant tree question.
assert "Never state a numeric score" in _sp, "the no-numeric-scores rule must be present"
assert "read and understood first" in _sp and "informs the ones that follow" in _sp, (
    "the framework paragraph must frame climate/landform as read and factors as informing, not constraining"
)
assert "fight the landscape" not in _sp and "is decided within the constraints" not in _sp, (
    "the old decided-in-order/fight-the-landscape framing must be gone"
)
assert "How were the Tree Crop Areas determined?" not in _sp, (
    "the redundant tree-determination question must be deleted"
)
# Round-three refinements: internal thresholds banned alongside scores;
# roads report access without unserved acreage or stop framing; fencing
# doesn't default to production perimeters; the Summary asks for an
# enterprise combination and dependency-sequenced next steps.
assert "thresholds and ceilings" in _sp and "not how a farmer thinks about their" in _sp, (
    "the internal-thresholds rule must sit alongside the no-scores rule"
)
assert "don't report unserved acreage" in _sp, "the road note must ban unserved-acreage reporting"
assert "frame it as extension available if the reader wants it later" not in _sp, (
    "the old extension-available road framing must be replaced"
)
assert "Production Areas do not automatically need permanent perimeter" in _sp, (
    "the fencing note must say production perimeters aren't a given"
)
assert "one's output become another's input" in _sp and "synthesis question, not an inventory" in _sp, (
    "the Summary must carry the enterprise-combination question and its synthesis note"
)
assert "sequence the work by dependency" in _sp, "the Summary note must ask for dependency-ordered next steps"
assert "diversified farm" not in _sp, "the old diversified-farm enterprise question must be replaced"
assert "riparian-buffer benefit" not in _sp, (
    "the stream-proximity explanation must be deleted along with the factor it explained"
)
# Round-four refinements: tree categories are approach-only judgment
# (no reasoning from a scoring factor to a category, no riparian buffer
# suggested anywhere), and the Summary must demand a cultivation answer.
assert "riparian" not in _sp, "riparian buffer must not be suggested anywhere in the prompt"
assert "riparian" not in _tree_prose, (
    "riparian buffer must not be suggested in the tree data block either"
)
assert "Recommending categories of tree crop is your judgment" in _sp and "silvopasture" in _sp, (
    "the tree category guidance must be the approach-only judgment version"
)
assert "don't explain what any scoring factor does or doesn't" in _sp, (
    "the prompt must forbid explaining what a scoring factor implies"
)
assert "not a judgment on cultivation" not in _sp, (
    "the old scoring-factors-vs-cultivation instruction must be deleted"
)
assert "has to say what gets grown or" in _sp and "missed the property's primary land use" in _sp, (
    "the Summary note must require an answer covering what is cultivated on the Production Areas"
)
print(
    "SYSTEM_PROMPT: all ten sections present in order; imperial-units, legend-name, and "
    "no-numeric-scores rules intact; reworked framework paragraph in place; redundant tree "
    "question gone."
)


print("\nAll report_generator checks passed.")
