"""
test_water_derivations.py

THE WATER & HYDROLOGY SECTION'S NUMBERS ON THE REAL PARCEL -- branch 9,
phase 1 of site-data-report-proposal.md's build sequence. Runs offline:
the captured 3DEP grid, the real SSURGO and NHD rows, and one real
response per Water source (water_reference_fixture.py, assets/reference/
water/) through an offline session, so every figure below is read the
way the report job reads it and the network stays refused.

  1. THE FIXTURES: every captured file loads and parses to the shapes the
     step 0 probes recorded -- three NHD flowlines, no waterbody, no
     point; three NHDPlus HR reaches; five NWI features, four fine and one
     coarse, every geometry valid; a digital FIRM with two zones and its
     panel; a 2024 NLCD grid the DEM's own shape; seven map units, thirty
     components, twelve months each.
  2. THE CALL COUNT: building the derivations from the session calls
     delineate_valleys(), compute_slope_percent() and the water step
     ZERO times and the flow pass ONCE for Landform and Water together;
     handed no flow pass, the water derivations run it once themselves.
  3. THE SCREENS AGREE: raw TWI, depression depth, accumulation and the
     filled surface equal the water step's own screens cell for cell, and
     the threshold is the step's own full-credit breakpoint.
  4. Surface water: Montour Run perennial, order 2, 532 acres at the
     reach; the tributary intermittent, order 1; nothing on the parcel,
     the nearest stream 78 m off; no waterbody; springs fetched, none.
  5. Wet ground three ways, on cells: the hydric, wetland and terrain
     masks; their overlaps close as set identities; the wettest terrain
     cell sits on a map unit the survey maps as partially hydric, not
     predominantly -- the water standards finding, measured.
  6. The seasonal water table: the monthly derivation CLOSES against
     muaggatt's wtdepannmin; the three states (wet, deeper than the
     described profile, no data) are distinct; the parcel row's depth is
     over its wet share and the share rides beside it; flooding and
     ponding shares by month.
  7. Catchment: NHDPlus HR's reach figure leads; the window-derived
     watershed is split on- and off-parcel with rim cells counted and the
     truncation flag set; land cover shares over it and over the parcel.
  8. Flood: a digital FIRM, Zone X over the whole parcel, Zone A within
     150 m and nowhere on the parcel; without a study, every cell reads
     "no digital flood map".
  9. EVERY ACREAGE PARTITION SUMS TO THE COVER'S ACREAGE through
     allocate_exactly, and each degradable block absent leaves its
     fetched flag False and the rest intact.
"""

import copy
from unittest.mock import patch

import numpy as np

import offline_harness

offline_harness.install()

import exclusion_zones  # noqa: E402
import landform_derivations  # noqa: E402
import landform_section  # noqa: E402
import nlcd_landcover_data  # noqa: E402
import nwi_data  # noqa: E402
import valley_delineation  # noqa: E402
import water_derivations as wd  # noqa: E402
import water_reference_fixture as fixture  # noqa: E402
import water_survey_areas  # noqa: E402
from landform_section import allocate_exactly  # noqa: E402
from reference_fixture import PARCEL_ACRES  # noqa: E402

# ======================================================================
# 1. The fixtures
# ======================================================================
print("1. the captured responses load and parse to the shapes step 0 recorded")
record = fixture.capture_record()
assert set(record["files"]) >= {"water_features.json", "nhd_points.json", "nhdplus_hr.json", "nwi.json.gz", "nfhl.json.gz",
                                "soil_water_table.json", "soil_components.json", "soil_geometries.json", "nlcd.tif"}
assert record["files"]["nwi.json.gz"]["bytes_raw"] < 1_000_000, "the two-stage NWI fetch, not the 42 MB riverine polygon"
assert record["files"]["nfhl.json.gz"]["bytes_raw"] < 500_000, "the generalised Zone X, not the 29 MB polygon"
layers = fixture.water_layers()
features = fixture.load("water_features.json")
assert len(features["streams"]) == 3 and features["water_bodies"] == []
assert sorted(s["feature_code"] for s in features["streams"]) == [46003, 46006, 46006]
assert layers["nhd_points"] == []
hr = layers["nhdplus_hr"]
assert len(hr) == 3 and {row["stream_order"] for row in hr.values()} == {1, 2}
assert max(row["total_drainage_acres"] for row in hr.values()) > 500
nwi = layers["nwi"]
assert len(nwi["features"]) == 5
assert [f["geometry_detail"] for f in nwi["features"]].count(nwi_data.DETAIL_FINE) == 4
coarse = [f for f in nwi["features"] if f["geometry_detail"] == nwi_data.DETAIL_COARSE]
assert len(coarse) == 1 and coarse[0]["code"] == "R2UBH" and coarse[0]["acres_mapped"] > nwi_data.NWI_LARGE_FEATURE_ACRES
assert all(f["geometry_utm"] is not None and f["geometry_utm"].is_valid for f in nwi["features"])
assert nwi["project"]["image_year"] == 2023 and nwi["project"]["name"] == "PA NWI Research Methods"
fema = layers["fema_nfhl"]
assert fema["available"] is True and fema["study_ids"] == ["42003C"]
assert sorted(z["zone"] for z in fema["zones"]) == ["A", "X"]
assert all(z["geometry_utm"] is not None and z["geometry_utm"].is_valid for z in fema["zones"])
assert any(p["firm_pan"] == "42003C0065H" and str(p["effective_on"]) == "2014-09-26" for p in fema["panels"])
nlcd = layers["nlcd_landcover"]
assert nlcd["year"] == 2024 and nlcd["array"].shape == (108, 96) and nlcd["nodata_cells"] == 0
assert set(np.unique(nlcd["array"][~np.isnan(nlcd["array"])]).astype(int)) <= set(nlcd_landcover_data.NLCD_CLASSES)
wt = layers["soil_water_table"]
assert len(wt["map_units"]) == 7 and len(wt["components"]) == 30
assert all(len(c["months"]) == 12 for c in wt["components"].values()), "every component carries twelve month rows"
assert wt["survey_areas"] == [{"areasymbol": "PA003", "saverest": "9/5/2025 12:33:41 PM"}]
print(f"   NHD 3 flowlines, NHDPlus HR 3 reaches, NWI 5 features (4 fine, 1 coarse), FEMA 2 zones, NLCD {nlcd['array'].shape}, "
      f"SSURGO 7 map units / 30 components; nwi {record['files']['nwi.json.gz']['bytes_raw']:,} B raw")

# ======================================================================
# 2. The call count
# ======================================================================
print("2. Landform and Water share ONE flow pass; nothing the warm-up built is recomputed")
DATA = fixture.report_data()
assert DATA.unavailable == {}
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    assert CONTEXT.parcel_data.soil_components[0]["mukey"] in {"541658", "541683", "541687", "541690", "541700", "541736", "3175296"}
    assert len(CONTEXT.parcel_data.water_features["streams"]) == 3, "the session holds the real NHD rows"
    with patch.object(valley_delineation, "fill_and_resolve", wraps=valley_delineation.fill_and_resolve) as fills, \
         patch.object(valley_delineation, "compute_flow_accumulation", wraps=valley_delineation.compute_flow_accumulation) as flows, \
         patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valleys, \
         patch.object(exclusion_zones, "compute_slope_percent", wraps=exclusion_zones.compute_slope_percent) as slopes, \
         patch.object(water_survey_areas, "compute_water_survey_areas", wraps=water_survey_areas.compute_water_survey_areas) as step, \
         patch.object(water_survey_areas, "identify_water_survey_areas", wraps=water_survey_areas.identify_water_survey_areas) as step_entry:
        TERRAIN = landform_section.terrain_inputs_from_context(CONTEXT, DOCUMENT)
        FLOW = landform_derivations.derive(TERRAIN)
        INPUTS = wd.water_inputs_from_context(CONTEXT, DOCUMENT, DATA)
        DERIVED = wd.derive(INPUTS, FLOW)
    assert fills.call_count == 1 and flows.call_count == 1, (fills.call_count, flows.call_count)
    assert valleys.call_count == 0 and slopes.call_count == 0 and step.call_count == 0 and step_entry.call_count == 0
    assert DERIVED.flow is FLOW and INPUTS.slope_pct is CONTEXT.exclusion_zones["slope_pct"]
    assert INPUTS.valleys == CONTEXT.valleys and INPUTS.water_features is CONTEXT.parcel_data.water_features
    # Handed no pass, the water derivations run it once themselves.
    with patch.object(valley_delineation, "fill_and_resolve", wraps=valley_delineation.fill_and_resolve) as fills:
        ALONE = wd.derive(INPUTS)
    assert fills.call_count == 1 and np.array_equal(ALONE.flow.accumulation, FLOW.accumulation)
    # The water step itself, for section 3 -- the same overrides the session forwards.
    STEP = water_survey_areas.compute_water_survey_areas(
        CONTEXT.dem, CONTEXT.boundary_polygon_utm,
        soil_inputs=water_survey_areas.soil_inputs_for_parcel_data(CONTEXT.parcel_data),
    )
print("   fill_and_resolve 1, compute_flow_accumulation 1, delineate_valleys 0, compute_slope_percent 0, the water step 0")

# ======================================================================
# 3. The screens agree
# ======================================================================
print("3. the section's TWI, depression depth and flow arrays are the water step's own, cell for cell")
screens = STEP["screens"]
wet = DERIVED.wetness
assert np.array_equal(screens["twi_raw"], wet["twi_raw"], equal_nan=True)
assert np.array_equal(screens["depression_depth"], wet["depression_depth"], equal_nan=True)
assert np.array_equal(screens["flow_accumulation"], FLOW.accumulation) and np.array_equal(screens["filled"], FLOW.filled)
assert np.array_equal(screens["slope_pct"], INPUTS.slope_pct, equal_nan=True)
assert wet["threshold"] == STEP["twi_breakpoints"]["full_credit"] == wet["breakpoints"]["full_credit"]
assert wet["breakpoints"]["floor"] == STEP["twi_breakpoints"]["floor"]
assert wet["breakpoints"]["full_credit_percentile"] == water_survey_areas.TWI_WINDOW_FULL_CREDIT_PERCENTILE == 90.0
assert wet["reference"]["snapped"] is True
print(f"   threshold TWI {wet['threshold']:.3f} = the step's full-credit breakpoint (window p90); floor {wet['breakpoints']['floor']:.3f}")

# ======================================================================
# 4. Surface water
# ======================================================================
print("4. surface water: NHD on and adjacent, permanence, order, the reach's drainage, the nearest stream")
CELLS = DERIVED.cells
COVER_ACRES = round(INPUTS.parcel_acres, 1)
assert abs(INPUTS.parcel_acres - PARCEL_ACRES) < 1e-6 and CELLS["on_parcel_count"] == 2143
surface = DERIVED.surface_water
streams = {(s["name"], s["permanent_identifier"]): s for s in surface["streams"]}
assert len(streams) == 3 and surface["waterbodies"] == []
montour = [s for s in surface["streams"] if s["name"] == "Montour Run"]
assert len(montour) == 2 and all(s["permanence"] == "perennial" and s["stream_order"] == 2 for s in montour)
assert max(s["total_drainage_acres"] for s in montour) == max(row["total_drainage_acres"] for row in hr.values())
assert round(max(s["total_drainage_acres"] for s in montour)) == 532 and round(min(s["total_drainage_acres"] for s in montour)) == 383
tributary = next(s for s in surface["streams"] if s["name"] != "Montour Run")
assert tributary["permanence"] == "intermittent" and tributary["stream_order"] == 1 and round(tributary["total_drainage_acres"]) == 56
assert all(s["length_on_parcel_m"] == 0.0 and s["on_parcel"] is None for s in surface["streams"]), "no NHD line crosses this parcel"
# "Within 150 m" is a true distance from the boundary: the two Montour Run reaches (78 m and 149 m) are, the
# tributary at 266 m is not, although the fetch box (the bbox + 150 m) reached it at a corner.
assert [s["length_in_window_m"] > 0 for s in surface["streams"]] == [s["distance_m"] <= wd.ADJACENCY_BUFFER_METERS for s in surface["streams"]]
assert tributary["length_in_window_m"] == 0.0 and tributary["distance_m"] > wd.ADJACENCY_BUFFER_METERS
assert 230 < max(s["length_in_window_m"] for s in montour) < 250
assert 77 < surface["nearest_stream_distance_m"] < 78, surface["nearest_stream_distance_m"]
assert surface["streams_in_window_by_permanence"] == {"perennial": 2}
assert surface["springs_fetched"] is True and surface["springs"] == [] and surface["order_available"] is True
# Without the NHDPlus join the order and the reach figure read None, the rest unchanged.
without = wd.derive_surface_water(wd.WaterInputs(**{**INPUTS.__dict__, "nhdplus_hr": None}))
assert all(s["stream_order"] is None and s["total_drainage_acres"] is None for s in without["streams"]) and without["order_available"] is False
assert without["nearest_stream_distance_m"] == surface["nearest_stream_distance_m"]
# Without the point layer, springs are "not fetched", never "none".
no_points = wd.derive_surface_water(wd.WaterInputs(**{**INPUTS.__dict__, "nhd_points": None}))
assert no_points["springs"] is None and no_points["springs_fetched"] is False
print(f"   Montour Run perennial order 2, {round(max(s['total_drainage_acres'] for s in montour))} ac at the reach; nearest "
      f"{surface['nearest_stream_distance_m']:.1f} m; tributary intermittent order 1; springs fetched, none mapped")

# ======================================================================
# 5. Wet ground three ways
# ======================================================================
print("5. hydric, wetland and terrain wetness as cell masks; the overlaps close; the wettest cell's survey class")
hydric = DERIVED.hydric
assert sum(hydric["counts"].values()) == CELLS["on_parcel_count"], hydric["counts"]
assert sum(u["cells"] for u in hydric["map_units"].values()) == CELLS["on_parcel_count"] and hydric["counts"][wd.HYDRIC_NO_POLYGON] == 0
atkins = hydric["map_units"]["541658"]
assert atkins["class"] == wd.HYDRIC_PREDOMINANT and atkins["hydric_pct"] == 85.0 and atkins["cells"] == hydric["counts"][wd.HYDRIC_PREDOMINANT] == 14
assert hydric["map_units"]["541683"]["class"] == wd.HYDRIC_PARTIAL and hydric["map_units"]["541683"]["hydric_pct"] == 8.0
assert hydric["map_units"]["541700"]["class"] == wd.HYDRIC_PARTIAL and hydric["map_units"]["541700"]["hydric_pct"] == 1.0
assert {hydric["map_units"][k]["class"] for k in ("541687", "541690", "3175296", "541736")} == {wd.HYDRIC_NONE}
assert hydric["mask_predominant"].sum() == 14 and hydric["mask_any"].sum() == 14 + hydric["counts"][wd.HYDRIC_PARTIAL]
assert hydric["threshold_pct"] == 50.0
wetlands = DERIVED.wetlands
assert wetlands["fetched"] is True and wetlands["on_parcel_cells"] == 0 and wetlands["counts_by_type"] == {}
assert all(60 <= f["distance_m"] <= 140 for f in wetlands["features"]), [f["distance_m"] for f in wetlands["features"]]
window_acres = {k: v / 4046.8564224 for k, v in wetlands["window_area_by_type_m2"].items()}
assert 4.1 < window_acres["Freshwater Forested/Shrub Wetland"] < 4.4 and 0.4 < window_acres["Riverine"] < 0.5, window_acres
assert wetlands["project"]["image_year"] == 2023
assert wet["wet_cells"] == int(wet["mask"].sum()) == 182 and wet["measured_cells"] == CELLS["on_parcel_count"]
assert wet["percentiles_on_parcel"]["p90"] < wet["threshold"] < wet["percentiles_on_parcel"]["max"]
assert wet["depression_cells"] == 5 and 0.3 < wet["depression_max_m"] < 0.4
c = DERIVED.comparison
h, w, t = c["hydric"], c["wetland"], c["terrain"]
assert (h, w, t) == (14, 0, 182)
assert c["any"] == h + w + t - c["hydric_and_wetland"] - c["hydric_and_terrain"] - c["wetland_and_terrain"] + c["all_three"]
assert c["any"] + c["none"] == CELLS["on_parcel_count"]
assert c["hydric_only"] + c["hydric_and_wetland"] + c["hydric_and_terrain"] - c["all_three"] == h
assert c["terrain_only"] + c["hydric_and_terrain"] + c["wetland_and_terrain"] - c["all_three"] == t
assert c["hydric_and_terrain"] == 1 and c["terrain_only"] == 181 and c["terrain_on_partially_hydric"] == 125
# THE STANDARDS FINDING, MEASURED: the wettest terrain cell is on ground the survey maps as partially hydric
# (1% of its map unit), not predominantly, and in no mapped wetland.
wettest = c["wettest_cell"]
assert wettest["rowcol"] == (50, 74) and wettest["hydric_class"] == wd.HYDRIC_PARTIAL and wettest["mukey"] == "541700"
assert wettest["in_wetland"] is False and wettest["twi"] == wet["percentiles_on_parcel"]["max"]
assert not hydric["mask_predominant"][wettest["rowcol"]]
print(f"   hydric {h} cells, wetland {w}, terrain {t}; hydric∩terrain {c['hydric_and_terrain']}; the wettest cell (TWI "
      f"{wettest['twi']:.2f}) on {wettest['muname'][:24]}, {wettest['hydric_class']} hydric")

# ======================================================================
# 6. The seasonal water table
# ======================================================================
print("6. the water table by month: the derivation closes against wtdepannmin; three states; the parcel row")
table = DERIVED.water_table
assert table["fetched"] is True and table["weighted_cells"] == CELLS["on_parcel_count"]
assert table["survey_areas"][0]["areasymbol"] == "PA003"
IN = 1 / 2.54
units = table["map_units"]
# Atkins: one major component, Wet at 20 cm in all twelve months; muaggatt says 20.
assert all(units["541658"]["months"][m]["state"] == wd.WT_WET and units["541658"]["months"][m]["depth_cm"] == 20.0 for m in range(1, 13))
assert wt["map_units"]["541658"]["wtdepannmin_cm"] == 20.0
# Ernest-Vandergrift in January: Ernest 50% at 53 cm and Vandergrift 30% at 36 cm -> the weighted 46.6; muaggatt's
# annual minimum is the shallowest major, 36.
january = units["541683"]["months"][1]
assert january["state"] == wd.WT_WET and abs(january["depth_cm"] - (50 * 53 + 30 * 36) / 80) < 1e-9 and january["wet_share"] == 1.0
assert wt["map_units"]["541683"]["wtdepannmin_cm"] == 36.0
assert min(min(c["months"][m]["wet_top_cm"] for m in c["months"] if c["months"][m]["wet_top_cm"] is not None)
           for c in wt["components"].values() if c["mukey"] == "541683" and c["major"]) == 36.0
# April: Vandergrift no longer Wet -> the unit's depth is Ernest's alone and the share drops to 50/80.
april = units["541683"]["months"][4]
assert april["state"] == wd.WT_WET and april["depth_cm"] == 53.0 and abs(april["wet_share"] - 50 / 80) < 1e-9
# July: no major component Wet -> DEEPER THAN the shallowest described bottom, a positive statement.
july = units["541683"]["months"][7]
assert july["state"] == wd.WT_DEEPER and july["depth_cm"] is None and july["wet_share"] == 0.0
# Ernest and Vandergrift carry comonth rows and NO moisture layer from May to November; the profile they describe
# in the other months bottoms at 183 cm, and that is the depth the summer water table is deeper than.
assert july["deeper_than_cm"] == 183.0, july
# Gilpin: a Moist layer to 76 cm in every month and never a Wet one -> deeper than 76, never "no data".
assert all(units["541690"]["months"][m]["state"] == wd.WT_DEEPER and units["541690"]["months"][m]["deeper_than_cm"] == 76.0 for m in range(1, 13))
assert all(units[k]["has_data"] for k in units)
# A unit whose components carry no month rows is NO DATA in every month -- distinct from deeper.
stripped = copy.deepcopy(DATA.soil_water_table)
for cokey in stripped["map_units"]["541690"]["components"]:
    stripped["components"][cokey]["months"] = {}
    stripped["components"][cokey]["has_rows"] = False
no_rows = wd.derive_water_table(wd.WaterInputs(**{**INPUTS.__dict__, "soil_water_table": stripped}), hydric)
assert no_rows["map_units"]["541690"]["has_data"] is False and no_rows["map_units"]["541690"]["months"] == {}
assert no_rows["weighted_cells"] == CELLS["on_parcel_count"] - units["541690"]["cells"]
# The parcel row: January's depth is the cell-and-share-weighted mean over the wet ground, and three quarters of the
# parcel has a water table; July's depth is Atkins alone and its share says so.
parcel = table["parcel"]
assert 0.75 < parcel[1]["wet_share"] < 0.77 and 17 < parcel[1]["depth_cm"] * IN < 19, parcel[1]
assert parcel[7]["wet_share"] < 0.01 and parcel[7]["depth_cm"] == 20.0 and parcel[7]["data_share"] == 1.0
assert all(abs(sum(parcel[m]["flood"].values()) - 1.0) < 1e-9 and abs(sum(parcel[m]["pond"].values()) - 1.0) < 1e-9 for m in range(1, 13))
assert parcel[1]["flood"].get("Frequent", 0) > 0 and "Frequent" not in parcel[7]["flood"], "Atkins floods Jan-Apr only"
assert parcel[5]["pond"].get("Frequent", 0) > 0 and "Frequent" not in parcel[1]["pond"], "Atkins ponds Apr-Sep only"
assert parcel[5]["flood"].get("not stated", 0) > 0, "Atkins' NULL May-Dec flooding is 'not stated', never 'None'"
# Absent, the block says so.
absent = wd.derive_water_table(wd.WaterInputs(**{**INPUTS.__dict__, "soil_water_table": None}), hydric)
assert absent["fetched"] is False and absent["parcel"] == {}
print(f"   Ernest-Vandergrift Jan {january['depth_cm']:.1f} cm (min major 36 = wtdepannmin); Atkins 20 all year; Gilpin deeper than "
      f"{units['541690']['months'][1]['deeper_than_cm']:.0f} cm; parcel Jan {parcel[1]['depth_cm'] * IN:.0f} in over {parcel[1]['wet_share'] * 100:.0f}%")

# ======================================================================
# 7. Catchment and land cover
# ======================================================================
print("7. catchment: the reach's drainage area leads; the window watershed is truncated and says so; land cover shares")
catchment = DERIVED.catchment
assert round(catchment["stream_catchment_acres"]) == 532
assert catchment["outlets"] == 110 and catchment["watershed_cells"] == 4421
assert catchment["on_parcel_cells"] == CELLS["on_parcel_count"] and catchment["off_parcel_cells"] == 4421 - 2143
assert catchment["rim_cells"] == 77 and catchment["truncated"] is True
assert catchment["largest_outlet"] == {"rowcol": (50, 74), "cells": 1722, "rim_cells": 69, "on_parcel_cells": 10}
assert catchment["mask"].sum() == 4421 and (catchment["mask"] & CELLS["on_parcel"]).sum() == 2143
land = DERIVED.land_cover
assert land["fetched"] is True and land["year"] == 2024
assert sum(land["catchment"]["groups"].values()) + land["catchment"]["nodata"] == 4421
assert sum(land["parcel"]["groups"].values()) + land["parcel"]["nodata"] == CELLS["on_parcel_count"]
assert set(land["catchment"]["groups"]) == {"forest", "pasture", "developed"}
assert land["parcel"]["groups"]["pasture"] > 0.85 * CELLS["on_parcel_count"]
assert 0.44 < land["catchment"]["groups"]["forest"] / 4421 < 0.48
drainage = DERIVED.drainage
assert drainage["valleys"] == 4 and len(drainage["flow_paths"]) > 0 and drainage["length_on_parcel_m"] > 500
assert all(p["geometry"].within(INPUTS.boundary_polygon_utm.buffer(0.01)) for p in drainage["flow_paths"])
print(f"   NHDPlus HR {round(catchment['stream_catchment_acres'])} ac; window {catchment['watershed_cells']} cells on {catchment['on_parcel_cells']}"
      f" / off {catchment['off_parcel_cells']}, {catchment['rim_cells']} rim cells -> truncated; catchment cover {land['catchment']['groups']}")

# ======================================================================
# 8. Flood
# ======================================================================
print("8. flood: a digital FIRM; Zone X over the parcel, Zone A next door; no study -> no digital flood map")
flood = DERIVED.flood
assert flood["fetched"] and flood["available"] is True and flood["study_ids"] == ["42003C"]
assert flood["panel"]["firm_pan"] == "42003C0065H" and str(flood["panel"]["effective_on"]) == "2014-09-26"
labels = {z["label"]: z for z in flood["zones"]}
assert set(labels) == {"Zone A", "Zone X, area of minimal flood hazard"}
assert labels["Zone A"]["sfha"] is True and labels["Zone A"]["cells_on_parcel"] == 0 and 4.5 < labels["Zone A"]["area_in_window_m2"] / 4046.86 < 5
assert labels["Zone X, area of minimal flood hazard"]["cells_on_parcel"] == CELLS["on_parcel_count"]
assert flood["counts"] == {"Zone X, area of minimal flood hazard": 2143, wd.FLOOD_NOT_IN_ANY_ZONE: 0}
assert flood["sfha_cells"] == 0 and flood["unmapped_label"] == wd.FLOOD_NOT_IN_ANY_ZONE
unstudied = copy.deepcopy(DATA.fema_nfhl)
unstudied.update({"available": False, "study_ids": [], "zones": [], "panels": []})
no_map = wd.derive_flood(wd.WaterInputs(**{**INPUTS.__dict__, "fema_nfhl": unstudied}), CELLS)
assert no_map["available"] is False and no_map["counts"] == {wd.FLOOD_NO_DIGITAL_MAP: 2143} and no_map["panel"] is None
absent = wd.derive_flood(wd.WaterInputs(**{**INPUTS.__dict__, "fema_nfhl": None}), CELLS)
assert absent["fetched"] is False and absent["counts"] == {}
print("   Zone X 2143 cells, Zone A 0 on the parcel and 5.4 ac within 150 m; unstudied -> 'no digital flood map' on every cell")

# ======================================================================
# 9. Acreage sums and degraded blocks
# ======================================================================
print("9. every partition sums to the cover's acreage; each absent layer degrades alone")
partitions = {
    "hydric": hydric["counts"],
    "terrain wetness": {"wet": wet["wet_cells"], "dry": CELLS["on_parcel_count"] - wet["wet_cells"]},
    "wet ground": {"any": c["any"], "none": c["none"]},
    "land cover": dict(land["parcel"]["groups"], **({"no data": land["parcel"]["nodata"]} if land["parcel"]["nodata"] else {})),
    "flood": flood["counts"],
    "map units": {k: u["cells"] for k, u in hydric["map_units"].items()},
}
for name, counts in partitions.items():
    assert sum(counts.values()) == CELLS["on_parcel_count"], (name, counts)
    acres = allocate_exactly(list(counts.values()), INPUTS.parcel_acres, 1)
    assert round(sum(acres), 6) == COVER_ACRES, (name, acres)
    assert round(sum(allocate_exactly(list(counts.values()), 100.0, 1)), 6) == 100.0
degraded = fixture.report_data(nwi=None, fema_nfhl=None, nlcd_landcover=None, nhdplus_hr=None, nhd_points=None, soil_water_table_rows=None)
assert all(getattr(degraded, name) is None for name in ("nwi", "fema_nfhl", "nlcd_landcover", "nhdplus_hr", "nhd_points", "soil_water_table"))
bare = wd.derive(wd.water_inputs_from_context(CONTEXT, DOCUMENT, degraded), FLOW)
assert bare.wetlands["fetched"] is False and bare.flood["fetched"] is False and bare.land_cover["fetched"] is False
assert bare.water_table["fetched"] is False and bare.surface_water["springs"] is None and bare.surface_water["order_available"] is False
assert bare.comparison["terrain"] == t and bare.comparison["wetland"] == 0 and bare.comparison["wetlands_fetched"] is False
assert bare.catchment["stream_catchment_acres"] is None and bare.catchment["watershed_cells"] == 4421
assert bare.hydric["counts"] == hydric["counts"]
print(f"   {len(partitions)} partitions of {CELLS['on_parcel_count']} cells each sum to {COVER_ACRES} ac; the six Water layers absent leave "
      "hydric, terrain and the window catchment intact")

print("\ntest_water_derivations.py: all sections passed")
print(offline_harness.summary())
