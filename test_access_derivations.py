"""
test_access_derivations.py

THE ACCESS SECTION'S DERIVATIONS -- branch 10, phase 1 of
site-data-report-proposal.md's build sequence. Runs offline on the real
parcel: the captured 3DEP grid, the real SSURGO and NHD rows and the
parcel's own mapped roads (access_reference_fixture.py) through an
offline session, the captured SSURGO road ratings for the report layer.

  1. THE ROWS ARE THE FEATURES THE DESIGN STEP EXCLUDES: the section's
     reading of ParcelData.farm_roads buffered at the shared road buffer
     equals the session's existing-roads union, and that union rasterised
     over the slope-and-setback universe is the exclusion result's roads
     mask, cell for cell; and the mask is BYTE-IDENTICAL with the rows'
     new properties dict and with the rows stripped to name and geometry.
  2. THE CALL COUNT: building the derivations calls compute_slope_percent,
     delineate_valleys, the road fetch, the road union and Soil Data
     Access ZERO times; the session's warm-up fetched nothing either.
  3. EVERY FIGURE, TABLED against the values measured in step 0 and the
     diagnostic: the five segments, frontage by road at the 15 m
     tolerance (and the road the tolerance decides), the one track's
     length and grades, the boundary's drivable and undrivable lengths
     partitioning the perimeter exactly, the per-edge shares, no stream
     crossing, the soil partition summing to the cover with its limiting
     features, roadfill, the unpaved class.
  4. THE RULES ON SYNTHETIC CASES: a parcel with no frontage reports the
     nearest road's distance and bearing; with no road at all, nothing;
     a stream across the parcel splits it into a reachable piece and one
     beyond a crossing; dominant condition ties go to the more limiting
     class and unrated components take no part.
  5. THE DEGRADED CASE: no soil road ratings -> every map unit reads no
     data and the partition still sums to the cover.
"""

import copy
from unittest.mock import patch

import numpy as np
from shapely.geometry import LineString, Point, Polygon, box

import offline_harness

offline_harness.install()

import access_derivations as ad  # noqa: E402
import access_reference_fixture as fixture  # noqa: E402
import exclusion_zones  # noqa: E402
import farm_roads_data  # noqa: E402
import production_area  # noqa: E402
import soil_data  # noqa: E402
import soil_road_ratings as srr  # noqa: E402
import valley_delineation  # noqa: E402
import water_derivations as wd  # noqa: E402
from landform_section import SLOPE_CLASSES, allocate_exactly  # noqa: E402
from raster_grid import pixel_center_xy  # noqa: E402

FT = 1 / 0.3048

# ======================================================================
# 1. The rows are the features the design step excludes
# ======================================================================
print("1. the section's roads are the exclusion mask's roads, byte for byte, with and without properties")
DATA = fixture.report_data()
with fixture.Harness() as harness:
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    assert harness.roads_refetch.call_count == 0, "the warm-up reads ParcelData's rows, it does not refetch"
    with patch.object(production_area, "compute_slope_percent", wraps=production_area.compute_slope_percent) as slopes, \
         patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valleys, \
         patch.object(farm_roads_data, "get_farm_roads_for_boundary", wraps=farm_roads_data.get_farm_roads_for_boundary) as road_fetch, \
         patch.object(farm_roads_data, "get_road_exclusion_union_utm", wraps=farm_roads_data.get_road_exclusion_union_utm) as road_union, \
         patch.object(soil_data, "_run_sda_query", wraps=soil_data._run_sda_query) as sda:
        INPUTS = ad.access_inputs_from_context(CONTEXT, DOCUMENT, DATA)
        DERIVED = ad.derive(INPUTS)
    CALLS = (slopes.call_count, valleys.call_count, road_fetch.call_count, road_union.call_count, sda.call_count)
    ROADS_LAYER = CONTEXT.exclusion_zones["layers"]["roads"]
    UNIVERSE = CONTEXT.exclusion_zones["slope_only_mask"]
    EXISTING = CONTEXT.existing_roads
    # The stripped rows through the SAME warm-up code path: the union and the mask must not move.
    stripped_rows = [{"name": r["name"], "geometry": r["geometry"]} for r in CONTEXT.parcel_data.farm_roads]
    assert all("properties" in r for r in CONTEXT.parcel_data.farm_roads) and all(set(r) == {"name", "geometry"} for r in stripped_rows)
    STRIPPED_UNION = farm_roads_data.get_road_exclusion_union_utm(fixture.REAL_BOUNDARY, CONTEXT.dem, farm_roads=stripped_rows)
    STRIPPED = exclusion_zones.identify_exclusion_zones(
        fixture.REAL_BOUNDARY, dem=CONTEXT.dem, boundary_polygon_utm=CONTEXT.boundary_polygon_utm,
        canopy_height=CONTEXT.parcel_data.canopy_height, soil_components=CONTEXT.parcel_data.soil_components,
        soil_geometries=CONTEXT.parcel_data.soil_geometries, road_exclusion_union_utm=STRIPPED_UNION,
    )
assert len(INPUTS.farm_roads) == 5 and all(r["properties"]["layer"] == 32 for r in INPUTS.farm_roads)
assert DERIVED.exclusion_union is not None and EXISTING is not None
assert DERIVED.exclusion_union.equals(EXISTING), "the rows buffered at ROAD_EXCLUSION_BUFFER_METERS are the session's union"
assert STRIPPED_UNION.wkb == EXISTING.wkb, "the union is byte-identical with the rows stripped to name and geometry"
assert STRIPPED["layers"]["roads"]["mask"].tobytes() == ROADS_LAYER["mask"].tobytes(), "the roads mask is byte-identical"
assert STRIPPED["excluded_union_mask"].tobytes() == CONTEXT.exclusion_zones["excluded_union_mask"].tobytes()
assert STRIPPED["eligible_mask"].tobytes() == CONTEXT.exclusion_zones["eligible_mask"].tobytes()
# The section's union rasterised over the same universe IS the roads mask.
from shapely.prepared import prep  # noqa: E402

prepared = prep(DERIVED.exclusion_union)
rebuilt = np.zeros(UNIVERSE.shape, dtype=bool)
for r, c in np.argwhere(UNIVERSE):
    if prepared.contains(Point(pixel_center_xy(CONTEXT.dem, int(r), int(c)))):
        rebuilt[r, c] = True
assert rebuilt.tobytes() == ROADS_LAYER["mask"].tobytes(), "the section's union rasterised over the universe is the roads mask"
assert int(ROADS_LAYER["mask"].sum()) == 1 and ROADS_LAYER["data_available"] is True, "a nonzero mask: the assertion is reachable"
on_parcel_band = DERIVED.exclusion_union.intersection(INPUTS.boundary_polygon_utm).area
assert 400 < on_parcel_band < 450, on_parcel_band
print(f"   5 rows; union equal, roads mask {int(ROADS_LAYER['mask'].sum())} cell(s) equal with and without properties; "
      f"band on the parcel {on_parcel_band:.0f} m2 (the rest in the setback and slope gates)")

# ======================================================================
# 2. The call count
# ======================================================================
print("2. the derivations recompute and fetch nothing")
assert CALLS == (0, 0, 0, 0, 0), CALLS
assert INPUTS.unavailable == {} and INPUTS.soil_road_ratings is not None
assert ad.UNDRIVABLE_SLOPE_PCT == 15.0 == next(low for name, low, _ in SLOPE_CLASSES if name == "D")
assert ad.FRONTAGE_TOLERANCE_METERS == 15.0
print("   compute_slope_percent 0, delineate_valleys 0, road fetch 0, road union 0, SDA 0; the D-class break is the threshold")

# ======================================================================
# 3. Every figure, tabled
# ======================================================================
print("3. the figures: roads, frontage, the track, the boundary, crossings, the soil partition")
COVER = round(INPUTS.parcel_acres, 1)
CELLS = DERIVED.cells
assert COVER == 13.2 and CELLS["on_parcel_count"] == 2143
roads = {(r["name"], round(r["length_m"], 1)): r for r in DERIVED.roads}
assert [(r["name"], round(r["length_m"], 1), round(r["distance_m"], 1), round(r["length_on_parcel_m"], 1), round(r["frontage_m"], 1))
        for r in DERIVED.roads] == [
    ("Unnamed road", 166.7, 0.0, 38.3, 134.3),
    ("N Montour Rd", 1576.7, 63.0, 0.0, 0.0),
    ("N Montour Rd", 57.4, 8.6, 0.0, 15.7),
    ("N Montour Dr", 269.5, 15.5, 0.0, 0.0),
    ("N Montour Rd", 730.3, 7.0, 0.0, 349.0),
], [(r["name"], round(r["length_m"], 1), round(r["distance_m"], 1)) for r in DERIVED.roads]
assert all(r["class"] == "Local road" and r["route"] is None and r["properties"]["mtfcc_code"] == "S1400" for r in DERIVED.roads)
assert all(r["properties"]["source_datadesc"] == "2016 April MAFTIGER" for r in DERIVED.roads)
frontage = DERIVED.frontage
assert frontage["tolerance_m"] == 15.0 and round(frontage["perimeter_m"], 1) == 995.1
assert [(r["name"], r["class"], r["segments"], round(r["length_m"], 1)) for r in frontage["roads"]] == [
    ("N Montour Rd", "Local road", 2, 363.8), ("Unnamed road", "Local road", 1, 134.3)]
assert round(frontage["total_m"], 1) == 497.2 and frontage["nearest"] is None and frontage["mapped_roads"] == 5
assert round(frontage["total_m"] / frontage["perimeter_m"], 3) == 0.5
# N Montour Dr, 15.5 m off the north-east edge, is decided by the tolerance: 0 at 15 m, frontage at 20 m.
at_20 = ad.derive_frontage(INPUTS, ad.derive_roads(INPUTS, tolerance_m=20.0), tolerance_m=20.0)
assert [r["name"] for r in at_20["roads"]] == ["N Montour Rd", "Unnamed road", "N Montour Dr"]
assert round(next(r["length_m"] for r in at_20["roads"] if r["name"] == "N Montour Dr"), 1) == 72.0
# The track: the unnamed road's 38 m on the parcel, climbing 13.5% end to end.
tracks = DERIVED.tracks
assert tracks["count"] == 1 and round(tracks["total_m"], 1) == 38.3
track = tracks["tracks"][0]
profile = track["profile"]
assert track["name"] == "Unnamed road" and round(profile["end_to_end_grade_pct"], 1) == 13.5
assert round(profile["mean_grade_pct"], 1) == 12.9 and round(profile["max_grade_pct"], 1) == 19.4, (profile["mean_grade_pct"], profile["max_grade_pct"])
assert [round(z, 2) for z in profile["elevations_m"]] == [357.62, 358.11, 358.6, 359.36, 359.89, 360.37, 361.18, 362.8]
assert [round(s, 1) for s in track["entry_slopes_pct"]] == [27.3, 25.2], "the lane enters across ground above the threshold"
# The boundary: a partition of the perimeter, the frontage edge the gentlest.
boundary = DERIVED.boundary
lengths = boundary["lengths_m"]
assert round(sum(lengths.values()), 6) == round(boundary["perimeter_m"], 6) == round(sum(r["length_m"] for r in boundary["runs"]), 6)
assert round(lengths[ad.DRIVABLE], 1) == 395.0 and round(lengths[ad.UNDRIVABLE], 1) == 600.1 and lengths[ad.UNKNOWN] == 0.0
assert len(boundary["stations"]) == 200 and all(s["slope_pct"] is not None for s in boundary["stations"])
assert round(boundary["frontage_lengths_m"][ad.DRIVABLE], 1) == 178.1 and round(boundary["frontage_lengths_m"][ad.UNDRIVABLE], 1) == 320.3
edges = [(ad.compass_sector(e["midpoint_bearing_deg"]), round(e["length_m"], 1), round(e["mean_slope_pct"], 1), round(e["undrivable_share"] * 100))
         for e in boundary["edges"] if e["length_m"] > 1]
assert edges == [("W", 330.6, 13.8, 49), ("S", 235.8, 12.4, 17), ("E", 94.5, 21.4, 95), ("NE", 241.8, 23.3, 90), ("N", 91.5, 19.3, 100)], edges
assert all(r["geometry"] is None or isinstance(r["geometry"], LineString) for r in boundary["runs"])
assert {r["state"] for r in boundary["runs"]} == {ad.DRIVABLE, ad.UNDRIVABLE}
# No stream on the parcel, so nothing is beyond a crossing.
crossings = DERIVED.crossings
assert crossings["streams_on_parcel"] == [] and crossings["crossings_needed"] == 0 and crossings["beyond_crossing_m2"] == 0.0
# The soil partition: the map-unit cells are the Water section's own, the classes sum to the cover, nothing rates not limited.
soil = DERIVED.soil
assert soil["fetched"] and soil["survey_areas"] == [{"areasymbol": "PA003", "saverest": "9/5/2025 12:33:41 PM"}]
water_inputs = wd.WaterInputs(dem=INPUTS.dem, boundary_polygon_utm=INPUTS.boundary_polygon_utm, slope_pct=INPUTS.slope_pct, valleys=[],
                              water_features=INPUTS.water_features, soil_components=INPUTS.soil_components,
                              soil_geometries=INPUTS.soil_geometries, parcel_acres=INPUTS.parcel_acres, retrieved_on=INPUTS.retrieved_on,
                              nhd_points=None, nhdplus_hr=None, nwi=None, fema_nfhl=None, nlcd_landcover=None, soil_water_table=None)
hydric = wd.derive_hydric(water_inputs, wd.grid_bookkeeping(water_inputs))
assert {k: v["cells"] for k, v in soil["map_units"].items()} == {k: v["cells"] for k, v in hydric["map_units"].items()}
counts = soil["counts"]
assert list(counts) == list(ad.SOIL_CLASSES) and sum(counts.values()) == CELLS["on_parcel_count"]
assert counts == {"Not limited": 0, "Somewhat limited": 755, "Very limited": 1388, "Not rated": 0, "no data": 0, "no survey polygon": 0}, counts
acres = allocate_exactly([counts[k] for k in ad.SOIL_CLASSES], INPUTS.parcel_acres, 1)
assert round(sum(acres), 6) == COVER and acres[1] == 4.7 and acres[2] == 8.5
classes = {k: (v["paved"]["class"], v["unpaved"]["class"], v["roadfill"]["class"]) for k, v in soil["map_units"].items()}
assert classes == {
    "541658": ("Very limited", "Very limited", "Poor"), "541687": ("Very limited", "Very limited", "Poor"),
    "541700": ("Very limited", "Very limited", "Poor"), "541683": ("Very limited", "Very limited", "Poor"),
    "541690": ("Very limited", "Very limited", "Poor"), "3175296": ("Somewhat limited", "Somewhat limited", "Poor"),
    "541736": ("Somewhat limited", "Somewhat limited", "Poor"),
}, classes
assert soil["map_units"]["541658"]["paved"]["weights"] == {"Very limited": 95.0, "Not rated": 5.0}
assert soil["map_units"]["541736"]["paved"]["weights"] == {"Somewhat limited": 95.0, "Very limited": 5.0}
assert soil["unpaved_differs"] == [] and soil["roadfill_counts"]["Poor"] == CELLS["on_parcel_count"]
very = soil["features"]["Very limited"]
assert list(very)[:5] == ["Frost action", "Slope", "Depth to saturated zone", "Low strength", "Shrink-swell"], list(very)
assert [round(very[k] * CELLS["cell_acres"], 1) for k in list(very)[:5]] == [8.5, 8.2, 7.0, 6.8, 5.7]
assert all(v <= counts["Very limited"] + 1e-9 for v in very.values()), "no feature affects more ground than its class holds"
somewhat = soil["features"]["Somewhat limited"]
assert list(somewhat)[:4] == ["Slope", "Frost action", "Low strength", "Depth to saturated zone"], list(somewhat)
assert [round(somewhat[k] * CELLS["cell_acres"], 1) for k in list(somewhat)[:4]] == [4.7, 4.7, 4.5, 2.3]
assert soil["features"]["Not limited"] == {}
# Atkins: the water table the Water section reports is the saturated-zone feature here.
atkins = soil["map_units"]["541658"]
assert atkins["features"]["Depth to saturated zone"] == 1.0 and atkins["features"]["Frost action"] == 1.0
assert round(atkins["features"]["Ponding"], 2) == 0.89, "Philo, 10% of the unit, carries no ponding rule"
print(f"   frontage {frontage['total_m'] * FT:.0f} ft on {len(frontage['roads'])} roads; track {tracks['total_m'] * FT:.0f} ft at "
      f"{profile['end_to_end_grade_pct']:.1f}%; boundary drivable {lengths[ad.DRIVABLE] * FT:.0f} ft of {boundary['perimeter_m'] * FT:.0f}; "
      f"soil {acres} ac -> {sum(acres)}")

# ======================================================================
# 4. The rules on synthetic cases
# ======================================================================
print("4. no frontage -> the nearest road; no road -> nothing; a stream splits the parcel; the dominant condition's tie rule")
# A road 40 m east of the parcel and nothing else: no frontage, the nearest road's distance and bearing.
minx, miny, maxx, maxy = INPUTS.boundary_polygon_utm.bounds
from rasterio.warp import transform as warp_transform  # noqa: E402

xs, ys = warp_transform(INPUTS.dem["crs"], "EPSG:4326", [maxx + 40.0, maxx + 40.0], [miny, maxy])
far_row = {"name": "Far Rd", "geometry": {"type": "LineString", "coordinates": [[xs[0], ys[0]], [xs[1], ys[1]]]},
           "properties": {"layer": 31, "layer_name": "Local connecting road", "county_route": "T-412"}}
far = ad.derive(ad.AccessInputs(**{**INPUTS.__dict__, "farm_roads": [far_row]}))
assert far.frontage["roads"] == [] and far.frontage["total_m"] == 0.0 and far.frontage["geometry"] is None
nearest = far.frontage["nearest"]
assert nearest["name"] == "Far Rd" and nearest["class"] == "Local connecting road" and 39.0 < nearest["distance_m"] < 41.0
assert nearest["sector"] == "E" and 80 < nearest["bearing_deg"] < 100, nearest
assert far.roads[0]["route"] == "T-412" and far.tracks["count"] == 0
assert far.boundary["frontage_lengths_m"] == {ad.DRIVABLE: 0.0, ad.UNDRIVABLE: 0.0, ad.UNKNOWN: 0.0}
assert far.exclusion_union is not None and far.exclusion_union.intersection(INPUTS.boundary_polygon_utm).is_empty
# No road at all: nothing, said plainly by the empty structures.
none = ad.derive(ad.AccessInputs(**{**INPUTS.__dict__, "farm_roads": []}))
assert none.roads == [] and none.frontage["nearest"] is None and none.frontage["mapped_roads"] == 0 and none.exclusion_union is None
# A stream down the parcel 60 m from its east edge: two pieces, the west one touching the frontage, the east strip
# (136 m from any frontage) beyond a crossing. An east-west cut would not do: N Montour Rd fronts the whole west edge.
sx, sy = warp_transform(INPUTS.dem["crs"], "EPSG:4326", [maxx - 60.0, maxx - 60.0], [miny - 10, maxy + 10])
stream = {"name": "Test Run", "feature_code": 46006, "geometry": {"type": "LineString", "coordinates": [[sx[0], sy[0]], [sx[1], sy[1]]]}}
split = ad.derive(ad.AccessInputs(**{**INPUTS.__dict__, "water_features": {"streams": [stream], "water_bodies": []}}))
assert len(split.crossings["streams_on_parcel"]) == 1 and len(split.crossings["pieces"]) == 2
reachable = [p for p in split.crossings["pieces"] if p["reachable"]]
assert len(reachable) == 1 and split.crossings["crossings_needed"] == 1
assert 2600 < split.crossings["beyond_crossing_m2"] < 2800, split.crossings["beyond_crossing_m2"]
assert next(p for p in split.crossings["pieces"] if not p["reachable"])["geometry"].distance(DERIVED.frontage["geometry"]) > 100
assert abs(sum(p["area_m2"] for p in split.crossings["pieces"]) - INPUTS.boundary_polygon_utm.area) < 30, "the pieces are the parcel less the cut"
# Dominant condition: a tie goes to the more limiting class; unrated components take no part; all unrated is no class.
def _component(pct, cls, features=()):
    return {"comppct": pct, "ratings": {srr.LOCAL_ROADS: {"class": cls, "value": None, "features": [
        {"name": n, "value": v, "rule": n} for n, v in features]}}}

tie = srr.dominant_condition([_component(50, "Somewhat limited"), _component(50, "Very limited")], srr.LOCAL_ROADS)
assert tie["class"] == "Very limited" and tie["weights"] == {"Somewhat limited": 50, "Very limited": 50}
mostly_unrated = srr.dominant_condition([_component(60, "Not rated"), _component(40, "Not limited")], srr.LOCAL_ROADS)
assert mostly_unrated["class"] == "Not rated", "a unit mostly unrated reads Not rated, not a minor component's class"
no_rows = srr.dominant_condition([{"comppct": 100, "ratings": {}}], srr.LOCAL_ROADS)
assert no_rows["class"] is None and no_rows["components"] == []
shares = srr.class_features([_component(75, "Very limited", [("Slope", 1.0), ("Frost action", 0.5)]),
                             _component(25, "Very limited", [("Slope", 1.0), ("Wetness", 0.0)]),
                             _component(10, "Not rated", [("Not rated; no data", None)])], srr.LOCAL_ROADS)
assert shares == {"Slope": 1.0, "Frost action": 0.75}, shares
fill_tie = srr.dominant_condition([_component(50, "Good"), _component(50, "Poor")], srr.LOCAL_ROADS, srr.ROADFILL_CLASSES)
assert fill_tie["class"] == "Poor"
print(f"   nearest road {nearest['distance_m']:.0f} m to the {nearest['sector']}; a stream leaves {split.crossings['beyond_crossing_m2'] / 4046.86:.1f} ac beyond a crossing")

# ======================================================================
# 5. The degraded case
# ======================================================================
print("5. without the soil road ratings every map unit reads no data and the partition still sums")
degraded_data = fixture.report_data(soil_road_ratings_rows=None, unavailable={
    "soil_road_ratings": {"label": "soil road-construction ratings", "reason": "source_unavailable", "error": "down"}})
degraded_inputs = ad.access_inputs_from_context(CONTEXT, DOCUMENT, degraded_data)
assert degraded_inputs.soil_road_ratings is None and list(degraded_inputs.unavailable) == ["soil_road_ratings"]
degraded = ad.derive(degraded_inputs)
assert degraded.soil["fetched"] is False and all(u["paved"] is None for u in degraded.soil["map_units"].values())
assert degraded.soil["counts"]["no data"] == CELLS["on_parcel_count"] and sum(degraded.soil["counts"].values()) == CELLS["on_parcel_count"]
assert degraded.soil["features"] == {k: {} for k in srr.LIMITATION_CLASSES}
assert degraded.frontage["total_m"] == frontage["total_m"], "the roads do not depend on the soil layer"
# A block that answers without one map unit: that unit alone is no data.
partial = copy.deepcopy(DATA.soil_road_ratings)
del partial["map_units"]["541736"]
partial_derived = ad.derive(ad.AccessInputs(**{**INPUTS.__dict__, "soil_road_ratings": partial}))
assert partial_derived.soil["counts"]["no data"] == 397 and partial_derived.soil["counts"]["Somewhat limited"] == 358
assert sum(partial_derived.soil["counts"].values()) == CELLS["on_parcel_count"]
print("   no data 2143 cells; one missing unit -> 397 cells of no data, the rest unchanged")

print("\ntest_access_derivations.py: all sections passed")
print(offline_harness.summary())
