"""
spc_reports.py

SEVERE-WEATHER REPORTS NEAR A POINT, from the bundled Storm Prediction
Center archive -- class E reference data: read from the repository,
never fetched at report time, so it carries no fetch risk and no
REPORT_FETCH_LAYERS entry.

    reports_within(lat, lon, radius_miles) -> the severe-weather block
    load_bundle()                          -> (header, records array)

THE BUNDLE. assets/reference/spc_reports_1995_2024.bin.gz, built by
make_spc_bundle.py from the SPC's national hail, damaging-wind and
tornado CSVs (https://www.spc.noaa.gov/wcm/#data): every report in the
window 1995-2024 with a location, as 9-byte records behind one JSON
header line. 800,442 reports, 3.5 MB gzipped. The header names the
source files and their Last-Modified dates -- the VINTAGE the footer
prints -- and the record count, which load_bundle() checks against the
bytes it read. Tornadoes are in once each, at their start point (the
builder keeps whole-track rows only); see make_spc_bundle.py for the
three rules that shape a count.

THE QUERY IS EXACT AT ANY RADIUS. A bounding-box prefilter in degrees
cuts 800 thousand rows to the few thousand near the point, then a
haversine distance on those decides membership in the circle. No grid,
so no cell-edge error and no border artefact between states. Loading the
bundle is one numpy frombuffer over a gzip read (about 50 ms), held on
the module after the first call.

WHAT COMES BACK, AND WHAT DOES NOT:

  counts by type over the window and PER YEAR ON AVERAGE (count / years
  in the window); hail and damaging-wind reports BY MONTH; the largest
  hail size reported; the tornado COUNT ONLY -- 36 tornadoes in 30 years
  within 25 miles of the reference parcel is one report per month-bucket
  every few years, which is noise dressed as a pattern, so no tornado
  month breakdown is computed. NO DIRECTION of any kind: storm and hail
  direction is not recorded in a usable form at parcel scale.

REPORTS ARE NOT MEASUREMENTS. The SPC's own caveats: these are the
reports the NWS uses for warning verification, they may not reflect
every event, and they cluster around roads, towns and people. Fewer
reports does not mean fewer storms. Magnitudes are as reported: hail in
inches, wind gusts in knots (most are estimated, and an unknown gust is
stored as unknown, not zero), tornado ratings on the F/EF scale.

THE RADIUS. 25 miles, chosen in branch 7 step 0: at 15 miles the
reference parcel has 13 tornadoes in 30 years, too few to say anything;
at 25 it has 613 hail and 1,915 wind reports, enough for a monthly
pattern, while staying at county scale.
"""

import gzip
import json
import math
import os
import threading
from typing import Optional

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_PATH = os.path.join(_HERE, "assets", "reference", "spc_reports_1995_2024.bin.gz")

BUNDLE_FORMAT = "spc-reports-packed-v1"
# type (u8), year - WINDOW_START (u8), month (u8), lat*100 (i16),
# lon*100 (i16), magnitude*100 (i16), little-endian.
RECORD_FORMAT = "<BBBhhh"
RECORD_DTYPE = np.dtype(
    [("type", "u1"), ("year_offset", "u1"), ("month", "u1"), ("lat", "<i2"), ("lon", "<i2"), ("mag", "<i2")]
)
WINDOW_START, WINDOW_END = 1995, 2024
TYPE_CODES = {"hail": 0, "wind": 1, "tornado": 2}
TYPE_NAMES = {code: name for name, code in TYPE_CODES.items()}
MAGNITUDE_UNITS = {"hail": "inches", "wind": "knots", "tornado": "F/EF scale"}
COORDINATE_SCALE = 100
MAGNITUDE_SCALE = 100
UNKNOWN_MAGNITUDE = -100      # -1.0 * MAGNITUDE_SCALE

DEFAULT_RADIUS_MILES = 25.0
EARTH_RADIUS_MILES = 3958.8

_LOCK = threading.Lock()
_LOADED = {}


def haversine_miles(lat1: float, lon1: float, lat2, lon2):
    """Great-circle distance in miles; lat2/lon2 may be numpy arrays."""
    p1, p2 = math.radians(lat1), np.radians(lat2)
    dlat = p2 - p1
    dlon = np.radians(lon2) - math.radians(lon1)
    a = np.sin(dlat / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))


def read_bundle(path: str = BUNDLE_PATH) -> tuple:
    """(header dict, structured numpy array) straight from disk, checked
    against the header's own record count and format name."""
    with gzip.open(path, "rb") as handle:
        header_line = handle.readline()
        body = handle.read()
    header = json.loads(header_line.decode("utf-8"))
    if header.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"{path}: bundle format {header.get('format')!r}, expected {BUNDLE_FORMAT!r}")
    records = np.frombuffer(body, dtype=RECORD_DTYPE)
    if len(records) != header.get("record_count"):
        raise ValueError(f"{path}: header says {header.get('record_count')} records, file holds {len(records)}")
    return header, records


def load_bundle(path: str = BUNDLE_PATH) -> tuple:
    """read_bundle(), once per path per process."""
    with _LOCK:
        if path not in _LOADED:
            _LOADED[path] = read_bundle(path)
        return _LOADED[path]


def reports_within(
    lat: float, lon: float, radius_miles: float = DEFAULT_RADIUS_MILES, path: str = BUNDLE_PATH
) -> dict:
    """
    The severe-weather block for a point:

        {
          'radius_miles', 'window': {'start', 'end'}, 'years': 30,
          'counts': {'hail': n, 'wind': n, 'tornado': n},      # in the window
          'per_year': {'hail': n/years, ...},
          'by_month': {'hail': [12 counts], 'wind': [12 counts]},  # NOT tornado
          'largest_hail_in': float | None,
          'hail_peak_month': 1..12 | None, 'wind_peak_month': 1..12 | None,
          'vintage': {type: {'file', 'last_modified'}},     # the header's
          'source_url': ...,
        }

    Counts are of REPORTS with a start point inside the circle.
    """
    header, records = load_bundle(path)
    start, end = header["window"]
    years = end - start + 1

    # Degrees per mile: 1/69.1 in latitude; longitude shrinks with cos(lat).
    dlat = radius_miles / 69.1
    dlon = radius_miles / (69.1 * max(math.cos(math.radians(lat)), 0.05))
    lats = records["lat"] / COORDINATE_SCALE
    lons = records["lon"] / COORDINATE_SCALE
    box = (np.abs(lats - lat) <= dlat) & (np.abs(lons - lon) <= dlon)
    near = records[box]
    inside = near[haversine_miles(lat, lon, lats[box], lons[box]) <= radius_miles]

    counts = {}
    by_month = {}
    for name, code in TYPE_CODES.items():
        of_type = inside[inside["type"] == code]
        counts[name] = int(len(of_type))
        if name != "tornado":
            months = np.bincount(of_type["month"], minlength=13)[1:13]
            by_month[name] = [int(n) for n in months]
    hail = inside[(inside["type"] == TYPE_CODES["hail"]) & (inside["mag"] != UNKNOWN_MAGNITUDE)]
    largest_hail = float(hail["mag"].max()) / MAGNITUDE_SCALE if len(hail) else None

    def _peak(name):
        months = by_month.get(name)
        return (int(np.argmax(months)) + 1) if months and max(months) > 0 else None

    return {
        "radius_miles": float(radius_miles),
        "window": {"start": start, "end": end},
        "years": years,
        "counts": counts,
        "per_year": {name: counts[name] / years for name in counts},
        "by_month": by_month,
        "largest_hail_in": largest_hail,
        "hail_peak_month": _peak("hail"),
        "wind_peak_month": _peak("wind"),
        "magnitude_units": dict(MAGNITUDE_UNITS),
        "vintage": header.get("sources", {}),
        "source_url": header.get("source_url"),
    }


def summarize_reports(block: dict) -> str:
    c = block["counts"]
    return (
        f"SPC reports within {block['radius_miles']:.0f} mi, {block['window']['start']}-{block['window']['end']}: "
        f"hail {c['hail']} ({block['per_year']['hail']:.1f}/yr), wind {c['wind']} ({block['per_year']['wind']:.1f}/yr), "
        f"tornado {c['tornado']} ({block['per_year']['tornado']:.1f}/yr); largest hail {block['largest_hail_in']} in"
    )


if __name__ == "__main__":
    print(summarize_reports(reports_within(40.6443, -79.9821)))
