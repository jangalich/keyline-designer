"""
test_spc_reports.py

Offline checks for spc_reports.py and make_spc_bundle.py -- the bundled
Storm Prediction Center reports and the radius query. The bundle under
test is the committed one (assets/reference/spc_reports_1995_2024.bin.gz);
no network is involved at any point, and offline_harness is installed to
prove it.

  1. The bundle: format, window, the header's record count matching the
     bytes, the vintage of each source file, magnitudes in the file's
     own units.
  2. The reference parcel at 25 miles: counts by type, per-year rates,
     hail and wind by month with June the peak, the largest hail, NO
     tornado month breakdown and NO direction of any kind.
  3. THE CIRCLE IS EXACT: every report counted is inside the radius by an
     independent haversine, none outside is counted, the count at a
     larger radius is at least the count at a smaller one, and a point in
     the Atlantic counts nothing.
  4. The builder's three rules on synthetic rows: a tornado's state
     segment is dropped and the whole track kept, a report without a
     location is dropped, an unknown magnitude is stored as unknown; a
     built bundle reads back through read_bundle().
"""

import gzip
import json
import math
import os
import tempfile

import offline_harness

offline_harness.install()

import numpy as np

import make_spc_bundle
import spc_reports
from spc_reports import (
    BUNDLE_FORMAT,
    MAGNITUDE_SCALE,
    TYPE_CODES,
    UNKNOWN_MAGNITUDE,
    haversine_miles,
    load_bundle,
    read_bundle,
    reports_within,
)

PARCEL = (40.6443, -79.9821)

# ======================================================================
# 1. The bundle
# ======================================================================
print("1. the committed bundle is whole, versioned and in the file's own units")
header, records = load_bundle()
assert header["format"] == BUNDLE_FORMAT and header["window"] == [1995, 2024]
assert header["record_count"] == len(records) == 800442, (header["record_count"], len(records))
assert header["counts"] == {"hail": 331576, "wind": 431247, "tornado": 37619}, header["counts"]
assert set(header["sources"]) == {"hail", "wind", "tornado"}
for kind, source in header["sources"].items():
    assert source["file"].endswith(".csv.zip") and "2025" in source["last_modified"], source
assert header["magnitude_units"] == {"hail": "inches", "wind": "knots", "tornado": "F/EF scale"}
hail = records[records["type"] == TYPE_CODES["hail"]]
known = hail[hail["mag"] != UNKNOWN_MAGNITUDE]
# Hail sizes run from pea (0.13 in is the smallest reported) to 8 in; every one is a size, none unknown.
assert 0.0 < known["mag"].min() / MAGNITUDE_SCALE < 0.5 and known["mag"].max() / MAGNITUDE_SCALE <= 10.0
assert len(known) == len(hail)
wind = records[records["type"] == TYPE_CODES["wind"]]
assert wind["mag"].max() / MAGNITUDE_SCALE > 100.0          # knots, not mph or m/s
torn = records[records["type"] == TYPE_CODES["tornado"]]
assert torn["mag"].max() / MAGNITUDE_SCALE == 5.0 and (torn["mag"][torn["mag"] != UNKNOWN_MAGNITUDE] >= 0).all()
assert records["year_offset"].min() == 0 and records["year_offset"].max() == 29
assert records["month"].min() == 1 and records["month"].max() == 12
print(f"   {len(records)} reports, {header['counts']}, vintage {header['sources']['hail']['last_modified']}")

# ======================================================================
# 2. The reference parcel
# ======================================================================
print("2. within 25 miles of the reference parcel: counts, rates, months, largest hail, no tornado months")
block = reports_within(*PARCEL)
assert block["radius_miles"] == 25.0 and block["years"] == 30 and block["window"] == {"start": 1995, "end": 2024}
assert block["counts"] == {"hail": 614, "wind": 1916, "tornado": 36}, block["counts"]
assert abs(block["per_year"]["hail"] - 614 / 30) < 1e-9 and abs(block["per_year"]["tornado"] - 1.2) < 1e-9
assert set(block["by_month"]) == {"hail", "wind"}, "tornadoes are a count only"
assert sum(block["by_month"]["hail"]) == 614 and sum(block["by_month"]["wind"]) == 1916
assert block["hail_peak_month"] == 6 and block["wind_peak_month"] == 6
assert block["largest_hail_in"] == 2.5
assert block["magnitude_units"]["hail"] == "inches"
assert block["vintage"] == header["sources"] and block["source_url"] == "https://www.spc.noaa.gov/wcm/data/"


def _no_direction(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert "direction" not in str(key).lower() and "bearing" not in str(key).lower(), key
            _no_direction(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _no_direction(value)


_no_direction(block)
print(f"   hail {block['counts']['hail']} ({block['per_year']['hail']:.1f}/yr), wind {block['counts']['wind']}, "
      f"tornado {block['counts']['tornado']}; peaks June/June; largest hail {block['largest_hail_in']} in")

# ======================================================================
# 3. The circle is exact
# ======================================================================
print("3. membership is by great-circle distance, checked independently")


def _independent_miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(a))


assert abs(haversine_miles(40.0, -80.0, 41.0, -80.0) - 69.09) < 0.1
lats = records["lat"] / 100.0
lons = records["lon"] / 100.0
inside = 0
for lat, lon, kind in zip(lats, lons, records["type"]):
    if abs(lat - PARCEL[0]) > 0.6 or abs(lon - PARCEL[1]) > 0.8:
        continue
    if _independent_miles(PARCEL[0], PARCEL[1], lat, lon) <= 25.0:
        inside += 1
assert inside == sum(block["counts"].values()), (inside, block["counts"])
smaller = reports_within(*PARCEL, radius_miles=15.0)
larger = reports_within(*PARCEL, radius_miles=50.0)
for kind in TYPE_CODES:
    assert smaller["counts"][kind] <= block["counts"][kind] <= larger["counts"][kind], kind
assert smaller["counts"]["tornado"] == 13 and larger["counts"]["tornado"] == 106, (smaller["counts"], larger["counts"])
ocean = reports_within(35.0, -60.0)
assert ocean["counts"] == {"hail": 0, "wind": 0, "tornado": 0}
assert ocean["largest_hail_in"] is None and ocean["hail_peak_month"] is None
print(f"   independent count inside 25 mi = {inside}; 15 mi <= 25 mi <= 50 mi; the Atlantic counts nothing")

# ======================================================================
# 4. The builder's rules
# ======================================================================
print("4. pack_reports() drops state segments and unlocated rows, keeps unknown magnitudes unknown")


def _row(yr, mo, lat, lon, mag, sg="1"):
    return {"yr": str(yr), "mo": str(mo), "slat": str(lat), "slon": str(lon), "mag": mag, "sg": sg}


sources = {
    "hail": [_row(1994, 5, 40.0, -80.0, "1.75"), _row(2000, 6, 40.0, -80.0, "1.75"), _row(2001, 7, 0.0, 0.0, "1.00")],
    "wind": [_row(2010, 6, 40.5, -80.5, "0.0000"), _row(2011, 7, 40.5, -80.5, "65.0000")],
    "tornado": [_row(1999, 5, 40.1, -80.1, "2", sg="1"), _row(1999, 5, 40.1, -80.1, "2", sg="2"), _row(2005, 4, 40.2, -80.2, "-9")],
}
packed, counts, dropped = make_spc_bundle.pack_reports(sources)
assert counts == {"hail": 1, "wind": 2, "tornado": 2}, counts
assert dropped == {"outside_window": 1, "segment_row": 1, "no_location": 1}, dropped
rows = np.frombuffer(packed, dtype=spc_reports.RECORD_DTYPE)
assert len(rows) == 5
assert rows[0]["mag"] == 175 and rows[0]["year_offset"] == 5 and rows[0]["month"] == 6
assert rows[1]["mag"] == 0 and rows[2]["mag"] == 6500       # a 0-knot gust is the file's own "unknown" and is kept as served
assert rows[3]["mag"] == 200 and rows[4]["mag"] == UNKNOWN_MAGNITUDE
assert rows[0]["lat"] == 4000 and rows[0]["lon"] == -8000

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "bundle.bin.gz")
    make_spc_bundle.write_bundle(path, packed, {"format": BUNDLE_FORMAT, "record_count": 5, "window": [1995, 2024], "sources": {}})
    h, r = read_bundle(path)
    assert h["record_count"] == 5 and len(r) == 5 and (r == rows).all()
    make_spc_bundle.write_bundle(path, packed, {"format": BUNDLE_FORMAT, "record_count": 4, "window": [1995, 2024]})
    try:
        read_bundle(path)
    except ValueError as exc:
        assert "header says 4" in str(exc)
    else:
        raise AssertionError("a bundle whose count disagrees with its bytes must be refused")
    with gzip.open(path, "wb") as handle:
        handle.write((json.dumps({"format": "other"}) + "\n").encode())
    try:
        read_bundle(path)
    except ValueError as exc:
        assert "format" in str(exc)
    else:
        raise AssertionError("a foreign format must be refused")
print("   5 of 8 synthetic rows kept; round trip through write_bundle/read_bundle; count and format checked")

print("\ntest_spc_reports.py: all sections passed")
print(offline_harness.summary())
