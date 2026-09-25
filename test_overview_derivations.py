"""
test_overview_derivations.py

SECTION I, SITE OVERVIEW: EVERY FIGURE, OFFLINE, on the reference parcel's
real session and the captured report layer (overview_reference_fixture):

  1. the acreage is the cover's and every acreage table's, and the
     perimeter is the one Access measures frontage against;
  2. the county and State off the geocoder fixture; the centroid is the
     report data's point;
  3. the elevation range is Landform's own lowest and highest ground;
  4. the context DEM: the 300-cell cap, the interval rule (20 ft, 100 ft
     index lines here), the rule itself at other reliefs;
  5. landscape position: the class and its rule, the printed radius, the
     class's sensitivity to that radius, the inflow from above;
  6. the physiographic province -- division and province, never a section;
  7. buildings on the parcel (none), the nearest outside, the source's
     450 sq ft floor;
  8. the nearest transmission line, and the nearest with a known voltage;
  9. the wildlife line: Pennsylvania's figures cell for cell against the
     two NAHMS reports, a suppressed cell kept unknown, a State not
     surveyed separately answered as such;
 10. each report-layer source degrading alone leaves its figure None and
     every other figure standing.

The network is refused (offline_harness); the figures are asserted, not
printed -- diagnose_site_overview.py prints them.
"""

import offline_harness

offline_harness.install()

import access_derivations as ad  # noqa: E402
import census_geography  # noqa: E402
import landform_section  # noqa: E402
import livestock_predators as lp  # noqa: E402
import overview_derivations as od  # noqa: E402
import overview_reference_fixture as fixture  # noqa: E402
import physiography  # noqa: E402
import site_report  # noqa: E402

FT = 1 / 0.3048

print("=" * 72)
print("test_overview_derivations.py -- I Site overview: the numbers")
print("=" * 72)

DATA = fixture.report_data()
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    INPUTS = od.overview_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    TERRAIN = landform_section.terrain_inputs_from_context(CONTEXT, DOCUMENT)
    ACCESS = ad.access_inputs_from_context(CONTEXT, DOCUMENT, DATA)
D = od.derive(INPUTS)

# ======================================================================
print("1. acreage agrees with the cover; perimeter agrees with Access")
assert D.acres == TERRAIN.parcel_acres
assert f"{round(D.acres, 1):,.1f}" == site_report.parcel_acres_label(TERRAIN) == "13.2"
access_roads = ad.derive_roads(ACCESS)
frontage = ad.derive_frontage(ACCESS, access_roads)
assert abs(D.perimeter_m - frontage["perimeter_m"]) < 1e-9, (D.perimeter_m, frontage["perimeter_m"])
assert round(D.perimeter_m * FT) == 3265
print(f"   {D.acres:.2f} ac (cover 13.2); perimeter {D.perimeter_m:.1f} m = {D.perimeter_m * FT:,.0f} ft, Access's own")

# ======================================================================
print("2. county, State and centroid")
assert census_geography.county_state_label(D.county_state) == "Allegheny County, Pennsylvania"
assert D.county_state["county_fips"] == "42003"
assert D.centroid == DATA.centroid
print(f"   {census_geography.county_state_label(D.county_state)}; centroid {D.centroid[0]:.5f}, {D.centroid[1]:.5f}")

# ======================================================================
print("3. the elevation range is Landform's")
section = landform_section.build_landform_section(TERRAIN, site_report.TOKENS)
lowest = [f for f in section["key_figures"] if f["label"] == "lowest elevation"][0]["value"]
highest = [f for f in section["key_figures"] if f["label"] == "highest elevation"][0]["value"]
assert lowest == f"{round(D.elevation['min_ft']):,} ft" and highest == f"{round(D.elevation['max_ft']):,} ft", (lowest, highest)
print(f"   {lowest} to {highest}, the Landform key figures")

# ======================================================================
print("4. the context DEM and its contour interval")
cdem = DATA.context_dem
assert cdem["array"].shape == (300, 300) and cdem["crs"] == TERRAIN.dem["crs"]
assert 11.5 < cdem["resolution_meters"][0] < 12.0 and 11.5 < cdem["resolution_meters"][1] < 12.0
cc = D.context_contours
assert cc["interval_ft"] == 20 and cc["index_every"] == 5 and round(cc["relief_ft"]) == 317
index = [level["elevation_ft"] for level in cc["levels"] if level["index"]]
assert index == [1100, 1200, 1300], index
assert len(cc["levels"]) == 16
# The rule at other reliefs: the smallest of 10/20/40/80 ft putting at most 20 levels across.
assert [od.context_contour_interval_ft(r) for r in (150, 200, 201, 400, 401, 800, 5000)] == [10, 10, 20, 20, 40, 40, 80]
print(f"   300 x 300 at {cdem['resolution_meters'][0]:.2f} m; relief {cc['relief_ft']:.0f} ft -> {cc['interval_ft']} ft, "
      f"{len(cc['levels'])} levels, index {index}")

# ======================================================================
print("5. landscape position")
L = D.landscape
assert L["radius_m"] == od.LANDSCAPE_TPI_RADIUS_M == 500.0
assert L["class"] == "mid slope" and -0.5 <= L["z"] <= 0.5 and L["median_slope_deg"] > 5.0
assert abs(L["z"] - (-0.279)) < 0.01, L["z"]
assert round(L["percentile_low"]) == 9 and round(L["percentile_high"]) == 69
assert not L["inflow"]["truncated"] and abs(L["inflow"]["acres"] - 15.93) < 0.01
# The rule's table.
assert [od.landscape_class(z, s) for z, s in ((1.2, 9), (0.8, 9), (0.0, 9), (0.0, 3), (-0.6, 9), (-2, 0))] == [
    "ridge", "upper slope", "mid slope", "bench", "valley floor", "valley floor"]
# THE RADIUS IS A CHOICE: the same parcel reads differently at 800 m, which is why the page states it.
saved = od.LANDSCAPE_TPI_RADIUS_M
try:
    od.LANDSCAPE_TPI_RADIUS_M = 800.0
    wide = od.derive_landscape(cdem, INPUTS.boundary_polygon_utm)
    od.LANDSCAPE_TPI_RADIUS_M = 300.0
    narrow = od.derive_landscape(cdem, INPUTS.boundary_polygon_utm)
finally:
    od.LANDSCAPE_TPI_RADIUS_M = saved
assert narrow["class"] == "mid slope" and wide["class"] == "valley floor", (narrow["class"], wide["class"])
print(f"   {L['class']} at {L['radius_m']:.0f} m (z {L['z']:+.2f}); 300 m {narrow['class']}, 800 m {wide['class']}; "
      f"spans percentiles {L['percentile_low']:.0f}-{L['percentile_high']:.0f}; {L['inflow']['acres']:.1f} ac drain in")

# ======================================================================
print("6. physiography: division and province, never a section")
assert D.physiography["division"] == "Appalachian Highlands" and D.physiography["province"] == "Appalachian Plateaus"
assert "section" not in D.physiography
bundle = physiography.load_bundle()
assert all(set(f["properties"]) == {"DIVISION", "PROVINCE", "PROVCODE"} for f in bundle["features"])
assert all(g.is_valid for g in bundle["geoms"])
assert physiography.province_at(30.0, -60.0) is None, "offshore: not mapped"
print(f"   {D.physiography['division']} / {D.physiography['province']}; {len(bundle['features'])} provinces bundled")

# ======================================================================
print("7. buildings")
B = D.buildings
assert B["count"] == 0 and B["on_parcel"] == [] and B["min_sqft"] == 450
assert abs(B["nearest_outside_m"] - 21.9) < 0.1 and B["nearest_outside"]["occupancy"] == "Agriculture"
assert B["image_dates"] == ["2019-05-27"] and B["production_dates"] == ["2020-05-06"]
print(f"   {B['count']} on the parcel; nearest {B['nearest_outside_m']:.1f} m outside "
      f"({B['nearest_outside']['occupancy']}, {B['nearest_outside']['sqft']:,.0f} sq ft); imagery {B['image_dates'][0]}")

# ======================================================================
print("8. transmission")
T = D.transmission
assert T["count"] == 11 and T["radius_m"] == 5 * 1609.344
assert T["nearest"]["voltage_kv"] is None and abs(T["nearest"]["distance_m"] - 4948.3) < 1
assert T["nearest_known_voltage"]["voltage_kv"] == 138.0 and abs(T["nearest_known_voltage"]["distance_m"] - 5365.5) < 1
assert T["nearest"]["distance_m"] <= T["nearest_known_voltage"]["distance_m"]
print(f"   nearest {T['nearest']['distance_m'] / 1609.344:.2f} mi (voltage not recorded); nearest known "
      f"{T['nearest_known_voltage']['voltage_kv']:.0f} kV at {T['nearest_known_voltage']['distance_m'] / 1609.344:.2f} mi")

# ======================================================================
print("9. the wildlife line")
W = D.wildlife
assert W["state"] == "Pennsylvania" and W["surveyed"]
# Cell for cell against the reports: cattle tables A.2.d/A.2.e and D.1.c/D.2.d, sheep A.2.a and C.8-C.10.
pa = lp.load_bundle()["states"]["PA"]
assert pa["cattle_deaths"] == {"nonpredator": 31840.0, "predator": 160.0, "total": 32000.0}
assert pa["calf_deaths"] == {"nonpredator": 34910.0, "predator": 1090.0, "total": 36000.0}
assert pa["cattle_predator_pct"]["coyotes"] == 65.7 and pa["cattle_predator_pct"]["unknown predators"] == 34.3
assert pa["calf_predator_pct"]["coyotes"] == 98.2 and pa["calf_predator_pct"]["black bears"] == 1.2
assert pa["calf_predator_pct"]["predatory birds"] == 0.6
assert pa["sheep_deaths"]["predator sheep"] == 142 and pa["sheep_deaths"]["predator lambs"] == 805
assert pa["sheep_predator_head"]["coyotes sheep"] == 84 and pa["sheep_predator_head"]["coyotes lambs"] == 713
assert pa["sheep_predator_head"]["bears lambs"] is None, "a (D) cell stays unknown, never zero"
assert pa["sheep_predator_head"]["foxes lambs"] == 32 and pa["sheep_predator_head"]["vultures lambs"] == 20
assert W["cattle"]["calf_lead"] == W["cattle"]["cattle_lead"] == W["sheep"]["lead"] == "coyotes"
assert W["also"] == ["bears", "foxes", "dogs", "vultures", "predatory birds"]
assert "livestock" in W["caveat"] and "crop damage" in W["caveat"]
assert lp.predator_line("CT")["surveyed"] is False, "not surveyed separately: no line, not 'no losses'"
assert len(lp.load_bundle()["states"]) == 39
print(f"   coyotes lead calves {W['cattle']['calf_pct']}%, cattle {W['cattle']['cattle_pct']}%, sheep "
      f"{W['sheep']['pct']:.0f}%; also {', '.join(W['also'])}; 39 States bundled")

# ======================================================================
print("10. each report-layer source degrading alone")
DEGRADABLE = {
    "context_dem": ("context_dem", ("landscape", "context_contours")),
    "county_state_raw": ("county_state", ("county_state", "wildlife")),
    "structures_raw": ("structures", ("buildings",)),
    "transmission_lines_raw": ("transmission_lines", ("transmission",)),
}
ALL = ("county_state", "landscape", "context_contours", "physiography", "buildings", "transmission", "wildlife")
for key, (layer, gone) in DEGRADABLE.items():
    degraded = fixture.report_data(**{key: None}, unavailable={layer: {"label": layer, "reason": "source_unavailable",
                                                                         "error": "down"}})
    with fixture.Harness():
        inputs = od.overview_inputs_from_context(CONTEXT, DOCUMENT, degraded)
    result = od.derive(inputs)
    for name in ALL:
        value = getattr(result, name)
        assert (value is None) == (name in gone), (key, name)
    assert list(result.unavailable) == [layer]
    assert result.acres == D.acres and result.elevation == D.elevation
print(f"   {len(DEGRADABLE)} sources, each down alone: only its own figures go")

print("\ntest_overview_derivations.py: all sections passed")
print(offline_harness.summary())
