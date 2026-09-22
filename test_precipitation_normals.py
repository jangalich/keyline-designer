"""
test_precipitation_normals.py

Offline checks for precipitation_normals.py -- the bundled NCEI 1991-2020
normals with their Daymet ratios and monthly heavy-day normals, the
nearest-station rule, the median-ratio correction and the heavy-rain
medians. The bundle is the committed one; nothing here touches the
network, and offline_harness is installed to prove it.

  1. The bundle: vintage naming the two versioned NCEI archives and the
     Daymet version behind the ratios, thousands of stations, every
     accepted-flag station carrying a normal in inches; Pittsburgh
     airport at 39.61 in -- the 1991-2020 figure, NOT the 1981-2010
     figure (38.19) the access API serves under the same name; Butler's
     ratio reproduces normal / Daymet from its own row.
  2. The nearest five for the reference parcel, in order, with distances;
     a station flagged P or E is never chosen; a station without a ratio
     is skipped unless asked for; the radius cap holds.
  3. The correction on the five: factor 0.960, the MEDIAN of the five
     ratios (the mean differs), each station row carrying normal, Daymet
     and ratio; fewer than three stations -> factor 1.0, not applied,
     with a reason.
  4. The heavy-rain normals: twelve monthly medians and the annual
     median across the same five; every month within the stations'
     range; not applied below three stations.
  5. Through report_data_from_fixtures(): ONE Daymet dict in, the
     climate block carries the factor, every precipitation figure is
     scaled by it, and the heavy-rain block rides beside the correction.
"""

import statistics

import offline_harness

offline_harness.install()

import precipitation_normals as pn
from daymet_data import parse_daymet_csv
from reference_fixture import REAL_BOUNDARY
from report_data import report_data_from_fixtures

PARCEL = (40.6443, -79.9821)
NEAREST = ["USC00362574", "USC00360022", "USC00363343", "USC00366151", "USC00361139"]

# ======================================================================
# 1. The bundle
# ======================================================================
print("1. the bundle is the versioned 1991-2020 archives plus bundled Daymet ratios, in inches")
vintage, stations = pn.load_bundle()
assert vintage["format"] == pn.BUNDLE_FORMAT and vintage["period"] == "1991-2020"
assert "v1.0.1" in vintage["annual_archive"] and "v1.0.1" in vintage["monthly_archive"]
assert vintage["daymet"]["period"] == "1991-2020" and vintage["daymet"]["stations_with_ratio"] > 5000
assert set(vintage["daymet"]["versions"]) == {"4.0"}, vintage["daymet"]["versions"]
assert len(stations) > 9000, len(stations)
by_id = {s["station"]: s for s in stations}
pit = by_id["USW00094823"]
assert pit["prcp_normal_in"] == 39.61 and pit["prcp_flag"] == "S" and pit["prcp_years"] == 30   # 1991-2020, not 38.19
butler = by_id["USC00361139"]
assert butler["prcp_normal_in"] == 43.21 and butler["prcp_flag"] == "R"
assert abs(butler["ratio"] - butler["prcp_normal_in"] * 25.4 / butler["daymet_annual_mm"]) < 1e-4
assert abs(butler["daymet_annual_mm"] / 25.4 - 44.70) < 0.01 and butler["daymet_version"] == "4.0"
assert butler["days_ge_1in"] == 7.9 and butler["days_ge_1in_monthly"] == [0.5, 0.1, 0.5, 0.6, 0.6, 0.8, 1.0, 1.0, 1.0, 0.5, 0.7, 0.6]
assert butler["days_ge_1in_monthly_flag"] == "S"
assert set(s["prcp_flag"] for s in stations) <= {"S", "R", "P", "E", "Q"}
accepted = [s for s in stations if s["prcp_flag"] in pn.ACCEPTED_FLAGS]
assert len(accepted) > 5000 and all(0.0 < s["prcp_normal_in"] < 300.0 for s in accepted)
with_ratio = [s for s in accepted if s["ratio"] is not None]
assert len(with_ratio) > 0.9 * len(accepted), (len(with_ratio), len(accepted))
assert all(0.3 < s["ratio"] < 3.0 for s in with_ratio), "a ratio far from one is a broken row"
# A station outside Daymet's coverage carries no ratio.
assert by_id["AQW00061705"]["ratio"] is None and by_id["AQW00061705"]["daymet_annual_mm"] is None   # Pago Pago
print(f"   {len(stations)} stations ({len(accepted)} S/R, {len(with_ratio)} with a ratio); Pittsburgh Intl AP 39.61 in")

# ======================================================================
# 2. The nearest five
# ======================================================================
print("2. the five nearest accepted stations with a ratio, nearest first")
nearest = pn.nearest_stations(*PARCEL)
assert [s["station"] for s in nearest] == NEAREST, [s["station"] for s in nearest]
distances = [s["distance_miles"] for s in nearest]
assert distances == sorted(distances) and 11.0 < distances[0] < 11.5 and distances[-1] < 14.5, distances
assert all(s["prcp_flag"] in ("S", "R") and s["ratio"] is not None for s in nearest)
provisional = pn.nearest_stations(*PARCEL, accepted_flags=("P",), require_ratio=False)
assert all(s["prcp_flag"] == "P" for s in provisional) and not set(s["station"] for s in provisional) & set(NEAREST)
assert pn.nearest_stations(*PARCEL, max_miles=5.0) == []
assert len(pn.nearest_stations(*PARCEL, count=2)) == 2
assert pn.nearest_stations(35.0, -60.0) == []          # the Atlantic
# Pago Pago has a normal but no ratio: chosen only when the ratio is not required.
pago = pn.nearest_stations(-14.33, -170.71, count=1, require_ratio=False)
assert pago and pago[0]["station"] == "AQW00061705" and pn.nearest_stations(-14.33, -170.71, count=1) == []
print("   " + ", ".join(f"{s['name'].split(',')[0]} {s['distance_miles']:.1f} mi" for s in nearest))

# ======================================================================
# 3. The correction
# ======================================================================
print("3. the factor is the median of the five bundled ratios")
block = pn.precipitation_correction(nearest, vintage)
assert block["applied"] is True and block["station_count"] == 5 and block["reason"] is None
ratios = [row["ratio"] for row in block["stations"]]
assert abs(block["factor"] - statistics.median(ratios)) < 1e-12
assert abs(block["factor"] - 0.960) < 0.0015, block["factor"]
assert abs(statistics.fmean(ratios) - block["factor"]) > 1e-4, "the mean would differ; the median is used"
assert all(0.9 < r < 1.0 for r in ratios), ratios
for row, station in zip(block["stations"], nearest):
    assert row["station"] == station["station"] and abs(row["normal_mm"] - station["prcp_normal_in"] * 25.4) < 1e-9
    assert abs(row["ratio"] - row["normal_mm"] / row["daymet_mm"]) < 1e-4
    assert row["flag"] in ("S", "R") and row["years"] >= 10 and row["daymet_version"] == "4.0"
assert block["normals_period"] == "1991-2020" and block["vintage"] is vintage
two = pn.precipitation_correction(nearest[:2], vintage)
assert two["applied"] is False and two["factor"] == 1.0 and "at least 3" in two["reason"]
print(f"   factor {block['factor']:.3f}; ratios {[round(r, 3) for r in ratios]}; two stations -> not applied")

# ======================================================================
# 4. The heavy-rain normals
# ======================================================================
print("4. days >= 1.00 in: monthly and annual medians across the same stations")
heavy = pn.heavy_rain_normals(nearest)
assert heavy["applied"] is True and heavy["station_count"] == 5 and len(heavy["monthly"]) == 12
for m in range(12):
    values = [row["monthly"][m] for row in heavy["stations"]]
    assert heavy["monthly"][m] == statistics.median(values) and min(values) <= heavy["monthly"][m] <= max(values)
annuals = [row["annual"] for row in heavy["stations"]]
assert heavy["annual"] == statistics.median(annuals) and 6.0 <= heavy["annual"] <= 9.0, heavy["annual"]
assert 5.5 <= sum(heavy["monthly"]) <= 9.5
assert max(heavy["monthly"]) == max(heavy["monthly"][5:9]), "the heaviest months are summer"
assert pn.heavy_rain_normals(nearest[:2])["applied"] is False
stripped = [dict(s, days_ge_1in_monthly=None) for s in nearest]
assert pn.heavy_rain_normals(stripped)["applied"] is False and pn.heavy_rain_normals(stripped)["station_count"] == 0
print(f"   annual {heavy['annual']:.1f} days; monthly {[round(v, 1) for v in heavy['monthly']]}")

# ======================================================================
# 5. Through the report data layer
# ======================================================================
print("5. report_data_from_fixtures() applies the bundled factor to every precipitation figure")
with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
    daily = parse_daymet_csv(handle.read())
corrected = report_data_from_fixtures(REAL_BOUNDARY, daily, severe_weather=False)
raw = report_data_from_fixtures(REAL_BOUNDARY, daily, severe_weather=False, correct_precipitation=False)
factor = corrected.precipitation_correction["factor"]
assert abs(factor - block["factor"]) < 1e-12 and corrected.climate["prcp_factor"] == factor
assert corrected.heavy_rain_normals["annual"] == heavy["annual"]
assert raw.precipitation_correction is None and raw.heavy_rain_normals is None and raw.climate["prcp_factor"] == 1.0
assert abs(corrected.climate["annual"]["prcp_total_mm"] - raw.climate["annual"]["prcp_total_mm"] * factor) < 1e-6
for c, r in zip(corrected.climate["monthly"], raw.climate["monthly"]):
    assert abs(c["prcp_total_mm"] - r["prcp_total_mm"] * factor) < 1e-6
    assert c["pet_mm"] == r["pet_mm"], "evaporation is untouched by the precipitation factor"
assert abs(corrected.climate["annual"]["largest_day"]["prcp_mm"] - raw.climate["annual"]["largest_day"]["prcp_mm"] * factor) < 1e-6
assert abs(corrected.climate["annual"]["prcp_total_mm"] / 25.4 - 43.7) < 0.1, corrected.climate["annual"]["prcp_total_mm"] / 25.4
assert not hasattr(corrected, "daymet_at_stations"), "one Daymet call: no station dicts on the report data"
print(f"   annual {raw.climate['annual']['prcp_total_mm'] / 25.4:.1f} in raw -> {corrected.climate['annual']['prcp_total_mm'] / 25.4:.1f} in corrected")

print("\ntest_precipitation_normals.py: all sections passed")
print(offline_harness.summary())
