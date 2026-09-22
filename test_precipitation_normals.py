"""
test_precipitation_normals.py

Offline checks for precipitation_normals.py -- the bundled NCEI 1991-2020
normals, the nearest-station rule and the median-ratio correction. The
bundle is the committed one; the five station fixtures under
daymet_station_fixtures/ are REAL Daymet single-pixel output (prcp only,
1991-2020) at the five stations nearest the reference parcel. The
network is refused by offline_harness.

  1. The bundle: vintage naming the versioned NCEI archive, thousands of
     stations, every accepted-flag station carrying a normal in inches;
     Pittsburgh airport at 39.61 in -- the 1991-2020 figure, NOT the
     1981-2010 figure (38.19) the access API serves under the same name.
  2. The nearest five for the reference parcel, in order, with distances;
     a station flagged P or E is never chosen; the radius cap holds.
  3. annual_precipitation_mm() on a station fixture is the mean of the
     thirty annual totals.
  4. The correction on the five fixtures: factor 0.960, the MEDIAN of the
     five ratios (the mean differs), each station row carrying normal,
     Daymet and ratio; fewer than three stations -> factor 1.0, not
     applied, with a reason.
  5. Through report_data_from_fixtures(): the climate block carries the
     factor and every precipitation figure is scaled by it.
"""

import os
import statistics

import offline_harness

offline_harness.install()

import precipitation_normals as pn
from daymet_data import parse_daymet_csv
from reference_fixture import REAL_BOUNDARY
from report_data import report_data_from_fixtures

PARCEL = (40.6443, -79.9821)
FIXTURE_DIR = "daymet_station_fixtures"
NEAREST = ["USC00362574", "USC00360022", "USC00363343", "USC00366151", "USC00361139"]

# ======================================================================
# 1. The bundle
# ======================================================================
print("1. the bundle is the versioned 1991-2020 archive, in inches")
vintage, stations = pn.load_bundle()
assert vintage["format"] == pn.BUNDLE_FORMAT and vintage["period"] == "1991-2020"
assert "v1.0.1" in vintage["archive"] and vintage["archive"].endswith(".tar.gz")
assert len(stations) > 9000, len(stations)
by_id = {s["station"]: s for s in stations}
assert by_id["USW00094823"]["prcp_normal_in"] == 39.61, by_id["USW00094823"]   # Pittsburgh Intl AP, 1991-2020
assert by_id["USW00094823"]["prcp_flag"] == "S" and by_id["USW00094823"]["prcp_years"] == 30
assert by_id["USC00361139"]["prcp_normal_in"] == 43.21 and by_id["USC00361139"]["prcp_flag"] == "R"
assert set(s["prcp_flag"] for s in stations) <= {"S", "R", "P", "E", "Q"}, set(s["prcp_flag"] for s in stations)
accepted = [s for s in stations if s["prcp_flag"] in pn.ACCEPTED_FLAGS]
assert len(accepted) > 5000 and all(0.0 < s["prcp_normal_in"] < 300.0 for s in accepted)
print(f"   {len(stations)} stations ({len(accepted)} S/R); Pittsburgh Intl AP 39.61 in")

# ======================================================================
# 2. The nearest five
# ======================================================================
print("2. the five nearest accepted stations, nearest first")
nearest = pn.nearest_stations(*PARCEL)
assert [s["station"] for s in nearest] == NEAREST, [s["station"] for s in nearest]
distances = [s["distance_miles"] for s in nearest]
assert distances == sorted(distances) and 11.0 < distances[0] < 11.5 and distances[-1] < 14.5, distances
assert all(s["prcp_flag"] in ("S", "R") for s in nearest)
provisional = pn.nearest_stations(*PARCEL, accepted_flags=("P",))
assert all(s["prcp_flag"] == "P" for s in provisional) and not set(s["station"] for s in provisional) & set(NEAREST)
assert pn.nearest_stations(*PARCEL, max_miles=5.0) == []
assert len(pn.nearest_stations(*PARCEL, count=2)) == 2
assert pn.nearest_stations(35.0, -60.0) == []          # the Atlantic
print("   " + ", ".join(f"{s['name'].split(',')[0]} {s['distance_miles']:.1f} mi" for s in nearest))

# ======================================================================
# 3. annual_precipitation_mm
# ======================================================================
print("3. annual_precipitation_mm() is the mean of the thirty annual totals")
station_daily = {}
for sid in NEAREST:
    with open(os.path.join(FIXTURE_DIR, sid + ".csv"), encoding="utf-8") as handle:
        station_daily[sid] = parse_daymet_csv(handle.read(), required_variables=("prcp",))
    assert station_daily[sid]["years"] == pn.NORMALS_YEARS and station_daily[sid]["row_count"] == 30 * 365
    assert "tmax" not in station_daily[sid], "the station fetch is precipitation only"
butler = station_daily["USC00361139"]
totals = {}
for y, p in zip(butler["year"], butler["prcp"]):
    totals[y] = totals.get(y, 0.0) + p
assert abs(pn.annual_precipitation_mm(butler) - statistics.fmean(totals.values())) < 1e-9
assert abs(pn.annual_precipitation_mm(butler) / 25.4 - 44.70) < 0.01
assert abs(butler["latitude"] - by_id["USC00361139"]["latitude"]) < 0.001, "fetched at the station's own coordinates"
print(f"   Butler 2 SW: Daymet {pn.annual_precipitation_mm(butler) / 25.4:.2f} in against a normal of 43.21 in")

# ======================================================================
# 4. The correction
# ======================================================================
print("4. the factor is the median of the five ratios")
annual = {sid: pn.annual_precipitation_mm(daily) for sid, daily in station_daily.items()}
block = pn.precipitation_correction(nearest, annual, vintage)
assert block["applied"] is True and block["station_count"] == 5 and block["reason"] is None
ratios = [row["ratio"] for row in block["stations"]]
assert abs(block["factor"] - statistics.median(ratios)) < 1e-12
assert abs(block["factor"] - 0.960) < 0.0015, block["factor"]
assert abs(statistics.fmean(ratios) - block["factor"]) > 1e-4, "the mean would differ; the median is used"
assert all(0.9 < r < 1.0 for r in ratios), ratios
for row, station in zip(block["stations"], nearest):
    assert row["station"] == station["station"] and abs(row["normal_mm"] - station["prcp_normal_in"] * 25.4) < 1e-9
    assert abs(row["ratio"] - row["normal_mm"] / row["daymet_mm"]) < 1e-12
    assert row["flag"] in ("S", "R") and row["years"] >= 10
assert block["normals_period"] == "1991-2020" and block["vintage"] is vintage

two = pn.precipitation_correction(nearest[:2], annual, vintage)
assert two["applied"] is False and two["factor"] == 1.0 and "at least 3" in two["reason"]
missing = pn.precipitation_correction(nearest, {sid: annual[sid] for sid in NEAREST[:2]}, vintage)
assert missing["applied"] is False and missing["station_count"] == 2
print(f"   factor {block['factor']:.3f}; ratios {[round(r, 3) for r in ratios]}; two stations -> not applied")

# ======================================================================
# 5. Through the report data layer
# ======================================================================
print("5. report_data_from_fixtures() applies the factor to every precipitation figure")
with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
    daily = parse_daymet_csv(handle.read())
corrected = report_data_from_fixtures(REAL_BOUNDARY, daily, station_daily=station_daily, severe_weather=False)
raw = report_data_from_fixtures(REAL_BOUNDARY, daily, severe_weather=False)
factor = corrected.precipitation_correction["factor"]
assert abs(factor - block["factor"]) < 1e-12 and corrected.climate["prcp_factor"] == factor
assert raw.precipitation_correction is None and raw.climate["prcp_factor"] == 1.0
assert abs(corrected.climate["annual"]["prcp_total_mm"] - raw.climate["annual"]["prcp_total_mm"] * factor) < 1e-6
for c, r in zip(corrected.climate["monthly"], raw.climate["monthly"]):
    assert abs(c["prcp_total_mm"] - r["prcp_total_mm"] * factor) < 1e-6
    assert c["pet_mm"] == r["pet_mm"], "evaporation is untouched by the precipitation factor"
assert abs(corrected.climate["annual"]["largest_day"]["prcp_mm"] - raw.climate["annual"]["largest_day"]["prcp_mm"] * factor) < 1e-6
assert corrected.climate["annual"]["heavy_rain_days"] <= raw.climate["annual"]["heavy_rain_days"]
assert abs(corrected.climate["annual"]["prcp_total_mm"] / 25.4 - 43.7) < 0.1, corrected.climate["annual"]["prcp_total_mm"] / 25.4
print(f"   annual {raw.climate['annual']['prcp_total_mm'] / 25.4:.1f} in raw -> {corrected.climate['annual']['prcp_total_mm'] / 25.4:.1f} in corrected")

print("\ntest_precipitation_normals.py: all sections passed")
print(offline_harness.summary())
