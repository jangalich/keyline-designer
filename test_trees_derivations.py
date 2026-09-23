"""
test_trees_derivations.py

THE TREES & FORESTRY SECTION'S DERIVATIONS -- branch 11, phase 1 of
site-data-report-proposal.md's build sequence. Runs offline on the real
parcel: the captured 3DEP grid, the parcel's own lidar HAG, the real
SSURGO, NHD and road rows (trees_reference_fixture.py) through an offline
session, the captured forest type group and woodland rows for the report
layer, and the same session again on the captured NLCD TCC dict for the
fallback path.

  1. THE SOURCE AGREES: the canopy dict the session holds is the one the
     trees step's registry edge consumes; its source is the source the
     exclusion result recorded; the section reads and states that one.
     A dict whose source disagrees with the record is refused.
  2. THE CANOPY INPUT AGREES: the section's canopy cells, dilated by the
     shared root-zone buffer over the slope-and-setback universe, are the
     exclusion result's canopy mask byte for byte -- on both paths -- and
     the mask is nonzero, so the assertion is reachable.
  3. THE CALL COUNT: building the derivations calls the HAG fetch, the TCC
     fetch, compute_slope_percent, delineate_valleys and Soil Data Access
     ZERO times; the warm-up fetched nothing either.
  4. EVERY FIGURE, TABLED against the values measured in step 0 and the
     diagnostic: canopy extent by source, the height classes and their
     breaks, closure at the 30 m grain, the forest type group by area,
     the species condensed with their site indices and base curves, the
     four limitations by acres with their features; every partition
     summing to the on-parcel cells and every acreage column to the cover.
  5. THE FALLBACK, END TO END: the TCC session's exclusion gate ran on
     percent cover, the derivations say so, the heights are ABSENT (None,
     not an empty table), the cover classes stand in for closure, the
     year is the pinned one.
  6. THE RULES ON SYNTHETIC CASES: the height and density class breaks,
     a species named two ways condensed to one, an unrated major
     component reported not weighted around, the degraded layers.
"""

import copy
import re
from unittest.mock import patch

import numpy as np

import offline_harness

offline_harness.install()

import canopy_cover_data as ccd  # noqa: E402
import canopy_height_data as chd  # noqa: E402
import forest_type_data as ftd  # noqa: E402
import production_area  # noqa: E402
import soil_data  # noqa: E402
import soil_woodland as sw  # noqa: E402
import trees_derivations as td  # noqa: E402
import trees_reference_fixture as fixture  # noqa: E402
import valley_delineation  # noqa: E402
import water_derivations as wd  # noqa: E402
from landform_section import allocate_exactly  # noqa: E402
from raster_grid import binary_dilate  # noqa: E402
from step_registry import TREES  # noqa: E402

FT = 1 / 0.3048


def _acres(counts: list, total: float) -> list:
    return allocate_exactly(counts, total, 1)


# ======================================================================
# 1. The source agrees
# ======================================================================
print("1. the canopy the section reads is the trees step's input, and its source is the recorded one")
DATA = fixture.report_data()
assert DATA.unavailable == {} and DATA.forest_type_group is not None and DATA.soil_woodland is not None
with fixture.Harness() as harness:
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    assert harness.canopy_refetch.call_count == 0 and harness.canopy_module_refetch.call_count == 0
    with patch.object(production_area, "compute_slope_percent", wraps=production_area.compute_slope_percent) as slopes, \
         patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valleys, \
         patch.object(chd, "get_canopy_height_for_boundary", wraps=chd.get_canopy_height_for_boundary) as hag_fetch, \
         patch.object(ccd, "get_tree_canopy_cover_for_boundary", wraps=ccd.get_tree_canopy_cover_for_boundary) as tcc_fetch, \
         patch.object(soil_data, "_run_sda_query", wraps=soil_data._run_sda_query) as sda:
        INPUTS = td.trees_inputs_from_context(CONTEXT, DOCUMENT, DATA)
        DERIVED = td.derive(INPUTS)
    CALLS = (slopes.call_count, valleys.call_count, hag_fetch.call_count, tcc_fetch.call_count, sda.call_count)
EXCLUSION = CONTEXT.exclusion_zones
CANOPY_LAYER = EXCLUSION["layers"]["canopy"]
# The registry edge: the trees step consumes parcel_data.canopy_height off the cache, which is the object read here.
edge = next(c for c in TREES.consumes if c.name == "canopy_height")
assert edge.cache_path == "parcel_data.canopy_height" and edge.forward_as == "canopy_height"
assert INPUTS.canopy is CONTEXT.parcel_data.canopy_height, "the same object, not a copy"
assert chd.canopy_source(INPUTS.canopy) == chd.CANOPY_SOURCE_LIDAR_HAG == INPUTS.canopy_source_recorded == CANOPY_LAYER["data_source"]
assert DERIVED.canopy["source"] == chd.CANOPY_SOURCE_LIDAR_HAG and DERIVED.canopy["units"] == "meters_above_ground"
assert DERIVED.canopy["source_item_id"] == "PA_WesternPA_2_2019-hag-2m-5-4" and DERIVED.canopy["threshold_m"] == 4.5
assert DERIVED.canopy["native_resolution_m"] == 2.0
# A dict whose source disagrees with the record is refused, whichever way round.
mismatch = td.TreesInputs(**{**INPUTS.__dict__, "canopy_source_recorded": chd.CANOPY_SOURCE_NLCD_TCC})
try:
    td.derive(mismatch)
except ValueError as exc:
    assert "recorded" in str(exc)
else:
    raise AssertionError("a source mismatch must raise")
unrecorded = td.TreesInputs(**{**INPUTS.__dict__, "canopy_source_recorded": None})
try:
    td.derive(unrecorded)
except ValueError:
    pass
else:
    raise AssertionError("an unrecorded source must raise")
print(f"   source {DERIVED.canopy['source']} on the dict, the exclusion record and the section; item {DERIVED.canopy['source_item_id']}")

# ======================================================================
# 2. The canopy input agrees
# ======================================================================
print("2. the section's canopy cells, dilated by the shared buffer over the slope-only universe, are the exclusion canopy mask")
UNIVERSE = EXCLUSION["slope_only_mask"]
radius = td.root_zone_radius_cells(INPUTS.dem)
assert radius == 1 and chd.TREE_ROOT_ZONE_BUFFER_METERS == 10 * 0.3048
from tree_zone_candidates import TREE_ZONE_CANOPY_BUFFER_METERS  # noqa: E402

assert TREE_ZONE_CANOPY_BUFFER_METERS == chd.TREE_ROOT_ZONE_BUFFER_METERS, "the trees step's own gate uses the same buffer"
rebuilt = binary_dilate(DERIVED.canopy["grid_mask"], radius) & UNIVERSE
assert rebuilt.tobytes() == CANOPY_LAYER["mask"].tobytes(), "the canopy mask is byte-identical"
assert int(CANOPY_LAYER["mask"].sum()) == 168 and CANOPY_LAYER["data_available"] is True, "a nonzero mask: the assertion is reachable"
# And the design's own derivation from the same dict agrees with the section's un-dilated cells.
design_mask = chd.root_zone_mask_from_canopy(INPUTS.canopy)
assert design_mask.tobytes() == binary_dilate(DERIVED.canopy["grid_mask"], radius).tobytes()
assert DERIVED.canopy["grid_mask"].tobytes() == chd.tree_root_zone_mask(INPUTS.canopy["array"], INPUTS.dem["resolution_meters"], buffer_meters=0).tobytes()
# The exclusion layer is NOT the canopy: it is smaller on this parcel (steep canopy falls outside the universe).
assert int(CANOPY_LAYER["mask"].sum()) < DERIVED.canopy["counts"][td.CANOPY]
print(f"   mask {int(CANOPY_LAYER['mask'].sum())} cells equal; canopy cells {DERIVED.canopy['counts'][td.CANOPY]}; radius {radius} cell")

# ======================================================================
# 3. The call count
# ======================================================================
print("3. the derivations recompute and fetch nothing")
assert CALLS == (0, 0, 0, 0, 0), CALLS
assert harness.canopy_refetch.call_count == 0 and harness.canopy_module_refetch.call_count == 0 and harness.roads_refetch.call_count == 0
print("   compute_slope_percent 0, delineate_valleys 0, HAG fetch 0, TCC fetch 0, SDA 0")

# ======================================================================
# 4. Every figure, tabled
# ======================================================================
print("4. every figure against step 0's measurements; every partition sums; every acreage column sums to the cover")
CELLS = DERIVED.cells
ON = CELLS["on_parcel_count"]
COVER = round(INPUTS.parcel_acres, 1)
assert ON == 2143 and COVER == 13.2
# Canopy extent.
canopy = DERIVED.canopy["counts"]
assert canopy == {td.CANOPY: 258, td.OPEN: 1872, td.NO_DATA: 13} and sum(canopy.values()) == ON
canopy_acres = _acres([canopy[td.CANOPY], canopy[td.OPEN], canopy[td.NO_DATA]], INPUTS.parcel_acres)
canopy_pct = _acres([canopy[td.CANOPY], canopy[td.OPEN], canopy[td.NO_DATA]], 100.0)
assert canopy_acres == [1.6, 11.5, 0.1] and round(sum(canopy_acres), 6) == COVER
assert canopy_pct == [12.0, 87.4, 0.6] and round(sum(canopy_pct), 6) == 100.0
# Height classes: the first break is the design threshold itself, the rest 30, 50 and 80 ft.
assert [name for name, _, _ in td.HEIGHT_CLASSES] == ["under", "15-30", "30-50", "50-80", "80+"]
assert td.HEIGHT_CLASSES[0][2] == chd.CANOPY_HEIGHT_THRESHOLD_METERS == td.HEIGHT_CLASSES[1][1]
assert [round(high * FT) for _, _, high in td.HEIGHT_CLASSES[1:4]] == [30, 50, 80] and td.HEIGHT_CLASSES[4][2] is None
heights = DERIVED.heights
assert heights["counts"] == {"under": 1872, "15-30": 53, "30-50": 89, "50-80": 109, "80+": 7, td.NO_DATA: 13}
assert sum(heights["counts"].values()) == ON
assert sum(heights["counts"][name] for name in td.CANOPY_CLASSES) == canopy[td.CANOPY], "the classed cells are the canopy cells"
assert heights["counts"]["under"] == canopy[td.OPEN]
height_names = [name for name, _, _ in td.HEIGHT_CLASSES] + [td.NO_DATA]
height_acres = _acres([heights["counts"][n] for n in height_names], INPUTS.parcel_acres)
assert height_acres == [11.5, 0.3, 0.6, 0.7, 0.0, 0.1] and round(sum(height_acres), 6) == COVER
assert round(heights["max_m"] * FT) == 86 and 14.0 < heights["canopy_mean_m"] < 15.0 and 14.0 < heights["canopy_median_m"] < 15.0
# Closure at the 30 m grain.
closure = DERIVED.closure
assert closure["block_cells"] == 6 and abs(closure["block_m"] - 30.0) < 0.1
assert closure["blocks"] == 27 and closure["canopy_cells"] == 258 == canopy[td.CANOPY] and closure["valid_cells"] == 679
assert round(closure["mean_pct"], 1) == 38.0
assert {k: v["blocks"] for k, v in closure["classes"].items()} == {"1-25": 11, "26-50": 6, "51-75": 4, "76-100": 6}
assert sum(v["blocks"] for v in closure["classes"].values()) == closure["blocks"]
assert sum(v["cells"] for v in closure["classes"].values()) == closure["valid_cells"]
assert sum(v["canopy_cells"] for v in closure["classes"].values()) == closure["canopy_cells"]
assert closure["grid"].shape == (18, 16) and int(np.count_nonzero(~np.isnan(closure["grid"]))) == 27
assert DERIVED.cover is None, "no cover classes on the lidar path"
# Forest type group: one group, 4.9% of the parcel against 12.0% canopy -- reported beside it, never reconciled.
forest = DERIVED.forest_type
assert forest["fetched"] and forest["counts"] == {0: 2038, 500: 105} and forest["nodata"] == 0
assert forest["single"] == 500 and ftd.FOREST_TYPE_GROUPS[500] == "Oak / hickory" and forest["groups"] == [500, 0]
assert forest["forest_cells"] == 105 and sum(forest["counts"].values()) + forest["nodata"] == ON
assert _acres([105, 2038], INPUTS.parcel_acres) == [0.6, 12.6] and _acres([105, 2038], 100.0) == [4.9, 95.1]
assert forest["vintage"] == "2014–2018" and forest["native_resolution_m"] == 30.0
# The map-unit grid is the Water section's: the same seven units, the same cells.
hydric = DERIVED.hydric
assert {k: v["cells"] for k, v in hydric["map_units"].items()} == {"541658": 14, "541687": 10, "541700": 722, "541683": 497,
                                                                    "541690": 145, "3175296": 358, "541736": 397}
assert sum(v["cells"] for v in hydric["map_units"].values()) == ON and hydric["counts"][wd.HYDRIC_NO_POLYGON] == 0
# Productivity: fifteen species by symbol, most ground first; the site index acre-weighted, ranged, its curves named.
prod = DERIVED.productivity
assert prod["fetched"] and prod["rated_cells"] == ON and prod["no_data_cells"] == 0
species = {s["symbol"]: s for s in prod["species"]}
assert len(prod["species"]) == 15 and [s["symbol"] for s in prod["species"][:5]] == ["QURU", "LITU", "ACSA3", "FRAM2", "PIVI2"]
assert all(a["cells"] >= b["cells"] for a, b in zip(prod["species"], prod["species"][1:]))
assert species["LITU"]["common"] in ("yellow-poplar", "tuliptree") and species["LITU"]["scientific"] == "Liriodendron tulipifera"
assert round(species["QURU"]["cells"], 1) == 1687.8 and round(species["QURU"]["cells"] * CELLS["cell_acres"], 1) == 10.4
assert round(species["QURU"]["site_index_mean"]) == 78 and (species["QURU"]["site_index_min"], species["QURU"]["site_index_max"]) == (55.0, 81.0)
assert species["QURU"]["bases"] == ["Schnur 1937 (820)"] and round(species["QURU"]["volume_mean"]) == 57
assert round(species["LITU"]["site_index_mean"]) == 89 and species["LITU"]["bases"] == ["Schlaegel, Kulow, Baughman 1969 (355)", "Beck 1962 (360)"]
assert round(species["PIST"]["site_index_mean"]) == 90 and round(species["PIST"]["volume_mean"]) == 143 and species["PIST"]["units"] == ["3175296"]
assert round(species["PODE3"]["cells"]) == 14 and species["PODE3"]["bases"] == ["Broadfoot 1960 (710)"]
# Guernsey, 55% of the 4.5-acre Guernsey-Vandergrift unit, carries no rows: reported, not weighted around.
guernsey = prod["units"]["541700"]
assert guernsey["unrated_major"] == [{"compname": "Guernsey", "comppct": 55.0}] and guernsey["major_pct"] == 90.0
assert round(guernsey["species"]["QURU"], 3) == round(35 / 90, 3) and round(guernsey["cells"] * CELLS["cell_acres"], 1) == 4.5
assert all(not u["unrated_major"] for k, u in prod["units"].items() if k != "541700")
# Limitations: four interpretations, each a partition, the features by ground.
lim = DERIVED.limitations
assert lim["fetched"] and list(lim["interpretations"]) == list(sw.INTERPRETATIONS)
expected = {
    sw.HARVEST_EQUIPMENT: {"Well suited": 2119, "Moderately suited": 0, "Poorly suited": 24},
    sw.SEEDLING_MORTALITY: {"Low": 397, "Moderate": 1732, "High": 14},
    sw.WINDTHROW: {"Slight": 513, "Moderate": 1630, "Severe": 0},
    sw.EROSION_OFF_ROAD: {"Slight": 14, "Moderate": 1252, "Severe": 867, "Very severe": 10},
}
for name, classes in expected.items():
    block = lim["interpretations"][name]
    assert {k: block["counts"][k] for k in classes} == classes, (name, block["counts"])
    assert block["counts"][sw.NOT_RATED] == 0 and block["counts"][td.SOIL_NO_DATA] == 0 and block["counts"][wd.HYDRIC_NO_POLYGON] == 0
    assert sum(block["counts"].values()) == ON
    acres = _acres(list(block["counts"].values()), INPUTS.parcel_acres)
    assert round(sum(acres), 6) == COVER
windthrow = lim["interpretations"][sw.WINDTHROW]
assert {k: round(v) for k, v in windthrow["features"]["Moderate"].items()} == {"Hillslope position": 1630, "Water table depth": 1608,
                                                                                 "Low cohesion": 372, "Depth to root restriction": 363}
assert windthrow["units"]["541700"]["class"] == "Moderate" and windthrow["units"]["541690"]["class"] == "Slight"
harvest = lim["interpretations"][sw.HARVEST_EQUIPMENT]
assert {k: round(v) for k, v in harvest["features"]["Poorly suited"].items()} == {"Low strength": 20, "Wetness": 14, "Slope": 10}
assert DATA.wind["seasons"]["winter"]["prevailing_sector"] == "W", "the winter rose the windthrow caption will name"
print(f"   canopy {canopy_acres[0]} ac ({canopy_pct[0]}%), heights {dict(zip(height_names, height_acres))}, closure {closure['mean_pct']:.0f}% "
      f"over {closure['blocks']} blocks, oak/hickory 0.6 ac, {len(prod['species'])} species, windthrow moderate on "
      f"{_acres([1630, 513], INPUTS.parcel_acres)[0]} ac")

# ======================================================================
# 5. The fallback, end to end
# ======================================================================
print("5. the TCC session: the exclusion gate ran on percent cover, the section says so, the heights are absent")
with fixture.Harness(canopy="tcc") as tcc_harness:
    TCC_SESSION = fixture.Session()
    TCC_CONTEXT = TCC_SESSION.context()
    TCC_DOCUMENT = TCC_SESSION.stored()
    with patch.object(chd, "get_canopy_height_for_boundary", wraps=chd.get_canopy_height_for_boundary) as hag_fetch, \
         patch.object(ccd, "get_tree_canopy_cover_for_boundary", wraps=ccd.get_tree_canopy_cover_for_boundary) as tcc_fetch, \
         patch.object(soil_data, "_run_sda_query", wraps=soil_data._run_sda_query) as sda:
        TCC_INPUTS = td.trees_inputs_from_context(TCC_CONTEXT, TCC_DOCUMENT, DATA)
        TCC = td.derive(TCC_INPUTS)
    assert (hag_fetch.call_count, tcc_fetch.call_count, sda.call_count) == (0, 0, 0)
tcc_layer = TCC_CONTEXT.exclusion_zones["layers"]["canopy"]
assert tcc_layer["data_source"] == chd.CANOPY_SOURCE_NLCD_TCC == TCC.canopy["source"] == chd.canopy_source(TCC_INPUTS.canopy)
assert TCC.canopy["units"] == "percent_cover" and TCC.canopy["year"] == ccd.TCC_YEAR == 2025 and TCC.canopy["product_version"] == "v2025-6"
assert TCC.canopy["threshold_m"] is None and TCC.canopy["native_resolution_m"] == 30.0
assert TCC.heights is None and TCC.closure is None, "no heights: a stated absence, not an empty table"
tcc_counts = TCC.canopy["counts"]
assert tcc_counts == {td.CANOPY: 193, td.OPEN: 1950, td.NO_DATA: 0} and sum(tcc_counts.values()) == ON
assert _acres([193, 1950, 0], TCC_INPUTS.parcel_acres) == [1.2, 12.0, 0.0] and _acres([193, 1950, 0], 100.0) == [9.0, 91.0, 0.0]
cover = TCC.cover
assert cover["classes"] == {"1-25": 102, "26-50": 46, "51-75": 19, "76-100": 26} and sum(cover["classes"].values()) == 193
assert round(cover["mean_pct"], 1) == 28.7 and cover["max_pct"] == 85.0
# The agreement assertion holds on this path too, against this session's own gate.
tcc_rebuilt = binary_dilate(TCC.canopy["grid_mask"], radius) & TCC_CONTEXT.exclusion_zones["slope_only_mask"]
assert tcc_rebuilt.tobytes() == tcc_layer["mask"].tobytes() and int(tcc_layer["mask"].sum()) == 158
assert TCC.canopy["grid_mask"].tobytes() == ccd.canopy_cover_cell_mask(TCC_INPUTS.canopy["array"]).tobytes()
# The two sessions' soil and forest reads are the same reads: the ground did not change with the canopy product.
assert TCC.forest_type["counts"] == forest["counts"] and TCC.limitations["interpretations"][sw.WINDTHROW]["counts"] == windthrow["counts"]
assert [s["symbol"] for s in TCC.productivity["species"]] == [s["symbol"] for s in prod["species"]]
# The pinned year is in the request, and the dict records it: the fixture's own meta says so.
assert fixture.load("canopy_tcc.json")["year"] == 2025 and "beginyear=2025" in ccd.mosaic_rule()
print(f"   TCC {_acres([193, 1950, 0], TCC_INPUTS.parcel_acres)[0]} ac (9.0%), year {TCC.canopy['year']}, cover classes {cover['classes']}, "
      f"heights {TCC.heights}, mask {int(tcc_layer['mask'].sum())} cells equal")

# ======================================================================
# 6. The rules on synthetic cases
# ======================================================================
print("6. the class breaks, a species named two ways, the unrated major component, the degraded layers")
assert td.height_class(float("nan")) is None and td.height_class(0.0) == "under" and td.height_class(4.49) == "under"
assert td.height_class(4.5) == "15-30" and td.height_class(30 * 0.3048) == "30-50" and td.height_class(24.384) == "80+"
assert td.height_class(80 * 0.3048 - 0.001) == "50-80" and td.height_class(1000.0) == "80+"
assert td.density_class(0.0) is None and td.density_class(0.1) == "1-25" and td.density_class(25.0) == "1-25"
assert td.density_class(25.01) == "26-50" and td.density_class(75.0) == "51-75" and td.density_class(100.0) == "76-100"
# A species rated under two common names on two components is one row, keyed by its symbol; a minor component takes no part;
# a major component with no rows is unrated and shares no ground.
raw = {
    "productivity": [
        {"mukey": "1", "muname": "Unit", "areasymbol": "XX", "saverest": "1/1/2026", "cokey": "a", "compname": "A", "comppct_r": "60",
         "majcompflag": "Yes", "plantsym": "LITU", "plantsciname": "Liriodendron tulipifera", "plantcomname": "yellow-poplar",
         "siteindexbase": "Beck 1962 (360)", "siteindex_r": "90", "fprod_r": "86"},
        {"mukey": "1", "muname": "Unit", "areasymbol": "XX", "saverest": "1/1/2026", "cokey": "b", "compname": "B", "comppct_r": "30",
         "majcompflag": "Yes", "plantsym": "LITU", "plantsciname": "Liriodendron tulipifera", "plantcomname": "tuliptree",
         "siteindexbase": "Beck 1962 (360)", "siteindex_r": "60", "fprod_r": "40"},
        {"mukey": "1", "muname": "Unit", "areasymbol": "XX", "saverest": "1/1/2026", "cokey": "c", "compname": "C", "comppct_r": "10",
         "majcompflag": "No ", "plantsym": "QURU", "plantsciname": "Quercus rubra", "plantcomname": "northern red oak",
         "siteindexbase": "Schnur 1937 (820)", "siteindex_r": "70", "fprod_r": "50"},
        {"mukey": "2", "muname": "Other", "areasymbol": "XX", "saverest": "1/1/2026", "cokey": "d", "compname": "D", "comppct_r": "55",
         "majcompflag": "Yes", "plantsym": None},
        {"mukey": "2", "muname": "Other", "areasymbol": "XX", "saverest": "1/1/2026", "cokey": "e", "compname": "E", "comppct_r": "35",
         "majcompflag": "Yes", "plantsym": "QURU", "plantsciname": "Quercus rubra", "plantcomname": "northern red oak",
         "siteindexbase": "Schnur 1937 (820)", "siteindex_r": "80", "fprod_r": "57"},
    ],
    "limitations": [
        {"mukey": "1", "cokey": "a", "mrulename": sw.WINDTHROW, "ruledepth": "0", "rulename": sw.WINDTHROW, "seqnum": "0", "interplr": "0.5", "interplrc": "Moderate"},
        {"mukey": "1", "cokey": "a", "mrulename": sw.WINDTHROW, "ruledepth": "1", "rulename": "x", "seqnum": "1", "interplr": "1", "interplrc": "Water table depth"},
        {"mukey": "1", "cokey": "b", "mrulename": sw.WINDTHROW, "ruledepth": "0", "rulename": sw.WINDTHROW, "seqnum": "0", "interplr": "0", "interplrc": "Slight"},
        {"mukey": "1", "cokey": "c", "mrulename": sw.WINDTHROW, "ruledepth": "0", "rulename": sw.WINDTHROW, "seqnum": "0", "interplr": "1", "interplrc": "Severe"},
        {"mukey": "2", "cokey": "d", "mrulename": sw.WINDTHROW, "ruledepth": "0", "rulename": sw.WINDTHROW, "seqnum": "0", "interplr": "0", "interplrc": "Slight"},
        {"mukey": "2", "cokey": "e", "mrulename": sw.WINDTHROW, "ruledepth": "0", "rulename": sw.WINDTHROW, "seqnum": "0", "interplr": "0", "interplrc": "Slight"},
    ],
}
block = sw.parse_woodland(raw)
assert len(block["components"]) == 5 and block["components"]["d"]["species"] == [] and block["survey_areas"] == [{"areasymbol": "XX", "saverest": "1/1/2026"}]
one = sw.unit_species(block, "1")
assert list(one["species"]) == ["LITU"] and abs(one["species"]["LITU"]["share"] - 1.0) < 1e-9 and one["major_pct"] == 90.0
assert one["species"]["LITU"]["site_index"] == [(90.0, 60.0, "Beck 1962 (360)"), (60.0, 30.0, "Beck 1962 (360)")]
assert one["unrated_major"] == [] and "QURU" not in one["species"], "the minor component's oak takes no part"
two = sw.unit_species(block, "2")
assert two["unrated_major"] == [{"compname": "D", "comppct": 55.0}] and abs(two["species"]["QURU"]["share"] - 35 / 90) < 1e-9
# Dominant condition on the unit: 60% Moderate beats 30% Slight; the minor component's Severe (10%) does not tip it.
condition = sw.unit_condition(block, "1", sw.WINDTHROW)
assert condition["class"] == "Moderate" and condition["weights"] == {"Moderate": 60.0, "Slight": 30.0, "Severe": 10.0}
assert condition["features"] == {"Water table depth": 1.0}
assert sw.unit_condition(block, "2", sw.WINDTHROW)["class"] == "Slight" and sw.unit_condition(block, "9", sw.WINDTHROW)["class"] is None
# A tie goes to the more limiting class.
tied = copy.deepcopy(raw)
tied["productivity"][0]["comppct_r"] = "30"
assert sw.unit_condition(sw.parse_woodland(tied), "1", sw.WINDTHROW)["class"] == "Moderate"
# The degraded layers: the statements' inputs, the partitions still summing to the parcel.
degraded_data = fixture.report_data(forest_type_group=None, soil_woodland_rows=None, unavailable={
    "forest_type_group": {"label": "forest type group", "reason": "source_unavailable", "error": "down"},
    "soil_woodland": {"label": "soil woodland ratings", "reason": "source_unavailable", "error": "down"}})
degraded = td.derive(td.trees_inputs_from_context(CONTEXT, DOCUMENT, degraded_data))
assert degraded.forest_type == {"fetched": False, "counts": {}, "nodata": 0, "forest_cells": 0, "groups": [], "single": None}
assert degraded.productivity["fetched"] is False and degraded.productivity["species"] == [] and degraded.productivity["no_data_cells"] == ON
assert degraded.limitations["fetched"] is False
for name in sw.INTERPRETATIONS:
    counts = degraded.limitations["interpretations"][name]["counts"]
    assert counts[td.SOIL_NO_DATA] == ON and sum(counts.values()) == ON
assert degraded.canopy["counts"] == canopy and degraded.heights["counts"] == heights["counts"], "the canopy stands without the report layers"
# The forest type module's own table and counts.
assert ftd.FOREST_TYPE_GROUPS[0] == "Non-forest" and ftd.FOREST_TYPE_GROUPS[999] == "Nonstocked" and len(ftd.FOREST_TYPE_GROUPS) == 35
sample = np.array([[0, 500, np.nan], [500, 800, 7]], dtype=np.float32)
assert ftd.class_counts(sample) == {0: 1, 500: 2, 800: 1}, "7 is not a published code and is not counted"
assert ftd.class_counts(sample, np.array([[True, False, True], [True, True, True]])) == {0: 1, 500: 1, 800: 1}
# The fixture record: what was captured, and how big.
record = fixture.capture_record()
assert set(record["files"]) == {"canopy_hag", "canopy_tcc", "forest_type.tif", "soil_woodland.json"}
assert record["files"]["canopy_hag"]["source_item_id"] == "PA_WesternPA_2_2019-hag-2m-5-4" and record["files"]["canopy_tcc"]["year"] == 2025
assert record["files"]["soil_woodland.json"]["rows"] == {"productivity": 134, "limitations": 288}
assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["captured_on"])
print("   breaks at 4.5 m and 30/50/80 ft; LITU condensed to one row; D unrated, E rated 35/90; ties to the more limiting class; degraded partitions sum")

print("\ntest_trees_derivations.py: all sections passed")
print(offline_harness.summary())
