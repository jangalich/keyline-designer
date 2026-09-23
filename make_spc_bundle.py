#!/usr/bin/env python3
"""
make_spc_bundle.py

BUILDS THE BUNDLED SEVERE-WEATHER REFERENCE (class E) from the Storm
Prediction Center's national report archives -- run by hand when the SPC
posts a new year, never at report time.

    python3 make_spc_bundle.py                      # download, build
    python3 make_spc_bundle.py --from-dir ./spc     # build from local CSVs

WHAT IT READS. The three national CSVs the SPC publishes under
https://www.spc.noaa.gov/wcm/data/ -- tornado (1950-), hail (1955-) and
damaging wind (1955-) -- each as the zip the site serves. One row is one
report: date, start latitude and longitude, magnitude, and (for
tornadoes) the segment flags that let a multi-state track appear once
per state.

WHAT IT WRITES. spc_reports.BUNDLE_PATH: a gzip of one JSON header line
followed by fixed-width records, one per report inside the bundle's
window (spc_reports.WINDOW_START .. WINDOW_END, 1995-2024). Each record
is spc_reports.RECORD_FORMAT: type code, year offset, month, latitude and
longitude in hundredths of a degree, magnitude in hundredths -- 9 bytes.
Measured on the 2025-05-13 files: 800,442 reports, 7.2 MB packed, 3.5 MB
gzipped, against 112 MB of source CSV. The header carries the source
file names and their Last-Modified dates so the report's footer can
print the bundle's vintage, and the record count so a reader can check
the file is whole.

THE THREE RULES APPLIED HERE, each of which changes a count:

  1. TORNADOES ARE COUNTED ONCE. The SPC tornado file carries one row per
     STATE SEGMENT of a track as well as one row for the whole track (the
     `sg` column: 1 = the entire track, 2 = a state's segment of a
     multi-state tornado). Only sg == 1 rows are kept, and a tornado's
     point is its START point (`slat`, `slon`). Hail and wind reports are
     points already.
  2. A REPORT WITHOUT A LOCATION IS DROPPED. A handful of rows carry 0.0
     for latitude or longitude; they cannot be inside any radius.
  3. MAGNITUDE IS KEPT IN THE FILE'S OWN UNIT and its unknowns are kept
     unknown: hail size in inches, wind gust in knots (the file uses 0
     for an unknown gust), tornado rating on the F/EF scale (the file
     uses -9 for unknown). A missing or unknown magnitude is stored as
     -1 so a consumer cannot mistake it for a measurement.

Quantising the coordinates to 0.01 degrees moves a report by at most
about 0.6 km, which is well inside the precision of the reports
themselves (SPC locates most to the nearest town or road junction) and
far inside the 25-mile radius the report uses.
"""

import argparse
import csv
import gzip
import io
import json
import os
import struct
import sys
import zipfile
from datetime import datetime, timezone

import requests

import spc_reports

SPC_DATA_URL = "https://www.spc.noaa.gov/wcm/data/"

# (type name, zip file name as the SPC serves it)
SOURCE_FILES = (
    ("hail", "1955-2024_hail.csv.zip"),
    ("wind", "1955-2024_wind.csv.zip"),
    ("tornado", "1950-2024_torn.csv.zip"),
)


def _download(name: str, into: str) -> str:
    path = os.path.join(into, name)
    if not os.path.exists(path):
        print(f"downloading {SPC_DATA_URL}{name} ...")
        response = requests.get(SPC_DATA_URL + name, timeout=300)
        response.raise_for_status()
        with open(path, "wb") as handle:
            handle.write(response.content)
        modified = response.headers.get("Last-Modified")
        if modified:
            with open(path + ".last-modified", "w", encoding="utf-8") as handle:
                handle.write(modified)
    return path


def _last_modified(path: str) -> str:
    sidecar = path + ".last-modified"
    if os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as handle:
            return handle.read().strip()
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _rows_from_zip(path: str):
    with zipfile.ZipFile(path) as archive:
        # The zips carry a __MACOSX resource fork beside the CSV; only a
        # top-level, non-dot CSV is the data.
        names = [
            n for n in archive.namelist()
            if n.lower().endswith(".csv") and "/" not in n and not n.startswith(".")
        ]
        if len(names) != 1:
            raise ValueError(f"{path}: expected one CSV inside, found {names}")
        with archive.open(names[0]) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))


def _magnitude(text: str) -> float:
    text = (text or "").strip()
    if text in ("", "-9", "-9.0", "-9.00", "-9.0000"):
        return -1.0
    value = float(text)
    return value if value >= 0 else -1.0


def pack_reports(sources, window=(spc_reports.WINDOW_START, spc_reports.WINDOW_END)) -> tuple:
    """(records bytes, counts by type, dropped counts) from
    {type: iterable of CSV dict rows}."""
    start, end = window
    out = bytearray()
    counts = {}
    dropped = {"outside_window": 0, "segment_row": 0, "no_location": 0}
    for kind, rows in sources.items():
        code = spc_reports.TYPE_CODES[kind]
        for row in rows:
            year = int(row["yr"])
            if not start <= year <= end:
                dropped["outside_window"] += 1
                continue
            if kind == "tornado" and row.get("sg", "1").strip() != "1":
                dropped["segment_row"] += 1
                continue
            lat, lon = float(row["slat"]), float(row["slon"])
            if lat == 0.0 or lon == 0.0:
                dropped["no_location"] += 1
                continue
            out += struct.pack(
                spc_reports.RECORD_FORMAT,
                code,
                year - start,
                int(row["mo"]),
                round(lat * 100),
                round(lon * 100),
                round(_magnitude(row["mag"]) * 100),
            )
            counts[kind] = counts.get(kind, 0) + 1
    return bytes(out), counts, dropped


def write_bundle(path: str, records: bytes, header: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wb", compresslevel=9) as handle:
        handle.write((json.dumps(header, sort_keys=True) + "\n").encode("utf-8"))
        handle.write(records)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-dir", default=None, help="directory holding the three SPC zips (skips download)")
    parser.add_argument("--out", default=spc_reports.BUNDLE_PATH)
    args = parser.parse_args(argv)

    into = args.from_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_spc_download")
    os.makedirs(into, exist_ok=True)

    sources = {}
    files = {}
    for kind, name in SOURCE_FILES:
        path = _download(name, into) if not args.from_dir else os.path.join(into, name)
        if not os.path.exists(path):
            print(f"missing {path}", file=sys.stderr)
            return 1
        sources[kind] = _rows_from_zip(path)
        files[kind] = {"file": name, "last_modified": _last_modified(path)}

    records, counts, dropped = pack_reports(sources)
    header = {
        "format": spc_reports.BUNDLE_FORMAT,
        "record_format": spc_reports.RECORD_FORMAT,
        "window": [spc_reports.WINDOW_START, spc_reports.WINDOW_END],
        "type_codes": spc_reports.TYPE_CODES,
        "magnitude_units": spc_reports.MAGNITUDE_UNITS,
        "coordinate_scale": spc_reports.COORDINATE_SCALE,
        "magnitude_scale": spc_reports.MAGNITUDE_SCALE,
        "unknown_magnitude": spc_reports.UNKNOWN_MAGNITUDE,
        "source_url": SPC_DATA_URL,
        "sources": files,
        "counts": counts,
        "dropped": dropped,
        "record_count": sum(counts.values()),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    write_bundle(args.out, records, header)
    size = os.path.getsize(args.out)
    print(f"wrote {args.out}: {header['record_count']} reports {counts}, dropped {dropped}, "
          f"{len(records) / 1e6:.1f} MB packed, {size / 1e6:.1f} MB gzipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
