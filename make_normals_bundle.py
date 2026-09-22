#!/usr/bin/env python3
"""
make_normals_bundle.py

BUILDS THE BUNDLED PRECIPITATION NORMALS (class E) from NCEI's archived
1991-2020 U.S. Climate Normals -- run by hand when NCEI revises the
archive, never at report time.

    python3 make_normals_bundle.py                       # download, build
    python3 make_normals_bundle.py --from-file x.tar.gz  # build from a local copy

WHAT IT READS. The by-station annual/seasonal archive,
us-climate-normals_1991-2020_v1.0.1_annualseasonal_multivariate_by-station_c20230404.tar.gz
(54 MB, 15,615 station files of about 2,100 columns each), from
https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/archive/.
THIS ARCHIVE, NOT THE ACCESS API. Branch 7 step 0 found that the
access data service's `normals-annualseasonal` dataset answers with the
1981-2010 normals (Pittsburgh airport 38.19 in, the 1981-2010 figure),
while the 1991-2020 archive and the per-station access files carry
39.61 in. The archive is versioned and dated, so the footer can name
exactly what it used.

WHAT IT WRITES. precipitation_normals.BUNDLE_PATH: one CSV row per
station that has an annual precipitation normal, with the columns
precipitation_normals.BUNDLE_COLUMNS -- id, latitude, longitude,
elevation (m), name, the annual precipitation normal in INCHES as the
archive stores it, its completeness flag and years, and the normal
count of days with at least 1.00 in of precipitation with its flag and
years. Roughly 9,800 rows, under a megabyte. The first line is a JSON
comment (# ...) naming the archive and its version, which
precipitation_normals.load_bundle() reads back as the vintage.

THE COMPLETENESS FLAG IS KEPT, NOT FILTERED HERE. NCEI marks each
normal S (standard, 30 years), R (representative, 10-29 years, adjusted
to the full period), P (provisional, 2-9 years) or Q (quasi-normal).
Which flags a consumer accepts is that consumer's rule
(precipitation_normals.ACCEPTED_FLAGS); the bundle keeps them all so
the rule can change without a rebuild.
"""

import argparse
import csv
import io
import json
import os
import sys
import tarfile
from datetime import datetime, timezone

import requests

import precipitation_normals

ARCHIVE_URL = (
    "https://www.ncei.noaa.gov/data/normals-annualseasonal/1991-2020/archive/"
    "us-climate-normals_1991-2020_v1.0.1_annualseasonal_multivariate_by-station_c20230404.tar.gz"
)

# Archive column -> bundle column.
COLUMN_MAP = (
    ("STATION", "station"),
    ("LATITUDE", "latitude"),
    ("LONGITUDE", "longitude"),
    ("ELEVATION", "elevation_m"),
    ("NAME", "name"),
    ("ANN-PRCP-NORMAL", "prcp_normal_in"),
    ("comp_flag_ANN-PRCP-NORMAL", "prcp_flag"),
    ("years_ANN-PRCP-NORMAL", "prcp_years"),
    ("ANN-PRCP-AVGNDS-GE100HI", "days_ge_1in"),
    ("comp_flag_ANN-PRCP-AVGNDS-GE100HI", "days_ge_1in_flag"),
    ("years_ANN-PRCP-AVGNDS-GE100HI", "days_ge_1in_years"),
)


def _download(url: str, into: str) -> str:
    path = os.path.join(into, os.path.basename(url))
    if not os.path.exists(path):
        print(f"downloading {url} ...")
        response = requests.get(url, timeout=600)
        response.raise_for_status()
        with open(path, "wb") as handle:
            handle.write(response.content)
    return path


def station_rows(archive_path: str):
    """One bundle row per station file that carries an annual
    precipitation normal."""
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(".csv"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
            record = next(reader, None)
            if record is None or "ANN-PRCP-NORMAL" not in record:
                continue
            value = (record.get("ANN-PRCP-NORMAL") or "").strip()
            if value in ("", "-9999", "-9999.0"):
                continue
            row = {}
            for source, target in COLUMN_MAP:
                row[target] = (record.get(source) or "").strip()
            yield row


def write_bundle(path: str, rows, vintage: dict) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("# " + json.dumps(vintage, sort_keys=True) + "\n")
        writer = csv.DictWriter(handle, fieldnames=list(precipitation_normals.BUNDLE_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-file", default=None, help="a local copy of the archive tar.gz")
    parser.add_argument("--out", default=precipitation_normals.BUNDLE_PATH)
    args = parser.parse_args(argv)

    if args.from_file:
        archive = args.from_file
    else:
        into = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_normals_download")
        os.makedirs(into, exist_ok=True)
        archive = _download(ARCHIVE_URL, into)

    vintage = {
        "format": precipitation_normals.BUNDLE_FORMAT,
        "source": "NCEI U.S. Climate Normals 1991-2020, annual/seasonal by-station archive",
        "archive": os.path.basename(ARCHIVE_URL),
        "archive_url": ARCHIVE_URL,
        "period": "1991-2020",
        "units": {"prcp_normal_in": "inches", "days_ge_1in": "days", "elevation_m": "m"},
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    count = write_bundle(args.out, station_rows(archive), vintage)
    print(f"wrote {args.out}: {count} stations with an annual precipitation normal, "
          f"{os.path.getsize(args.out) / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
