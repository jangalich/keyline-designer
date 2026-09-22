#!/usr/bin/env python3
"""
make_normals_bundle.py

BUILDS THE BUNDLED PRECIPITATION NORMALS (class E) from NCEI's archived
1991-2020 U.S. Climate Normals and the Daymet-at-station cache -- run by
hand when NCEI revises the archive or Daymet publishes a new version,
never at report time.

    python3 make_normals_bundle.py                                 # download, build
    python3 make_normals_bundle.py --annual x.tar.gz --monthly y.tar.gz

WHAT IT READS. Three inputs:

  1. The ANNUAL/SEASONAL by-station archive (54 MB, 15,615 station files
     of about 2,100 columns): the annual precipitation normal
     (ANN-PRCP-NORMAL, inches), its completeness flag and years, and the
     annual normal count of days with at least 1.00 in
     (ANN-PRCP-AVGNDS-GE100HI).
  2. The MONTHLY by-station archive (30 MB, twelve rows per station): the
     monthly normal count of days with at least 1.00 in
     (MLY-PRCP-AVGNDS-GE100HI) -- the heavy-rain figure the report
     prints, by decision: Daymet undercounts heavy days because
     interpolation smooths daily peaks, and an annual scale factor cannot
     fix an extreme.
  3. The Daymet cache (make_normals_daymet_cache.py): Daymet's mean
     annual precipitation 1991-2020 at each station's coordinates, the
     denominator of the station's ratio, fetched once and refreshed with
     the bundle.

THE ARCHIVES, NOT THE ACCESS API. Branch 7 step 0 found that the access
data service's `normals-annualseasonal` dataset answers with the
1981-2010 normals (Pittsburgh airport 38.19 in, the 1981-2010 figure)
while the 1991-2020 archives and the per-station access files carry
39.61 in. The archives are versioned and dated, so the footer can name
exactly what it used.

WHAT IT WRITES. precipitation_normals.BUNDLE_PATH: one CSV row per
station that has an annual precipitation normal, with the columns
precipitation_normals.BUNDLE_COLUMNS: id, coordinates, elevation, name;
the annual normal in INCHES as the archive stores it with its flag and
years; Daymet's annual at the station in mm, the Daymet version served
and the RATIO normal / Daymet (blank where Daymet has no cell for the
station); the annual days >= 1.00 in with its flag; and the twelve
monthly days >= 1.00 in with the worst of their twelve flags. About
15,000 rows, 1.5 MB. The first line is a JSON comment naming both
archives and the Daymet version, which precipitation_normals.
load_bundle() reads back as the vintage.

THE COMPLETENESS FLAG IS KEPT, NOT FILTERED HERE. NCEI marks each
normal S (standard, 24+ years), R (representative, 10+ years, gaps
filled from neighbours), P (provisional, 10+ years, gaps unfillable) or
E (estimated, 2+ years). Which flags a consumer accepts is that
consumer's rule (precipitation_normals.ACCEPTED_FLAGS); the bundle
keeps them all so the rule can change without a rebuild.
"""

import argparse
import csv
import io
import json
import os
import sys
import tarfile
from collections import Counter
from datetime import datetime, timezone

import requests

import precipitation_normals
from make_normals_daymet_cache import CACHE_PATH, load_cache

ARCHIVE_BASE = "https://www.ncei.noaa.gov/data/"
ANNUAL_ARCHIVE = (
    "normals-annualseasonal/1991-2020/archive/"
    "us-climate-normals_1991-2020_v1.0.1_annualseasonal_multivariate_by-station_c20230404.tar.gz"
)
MONTHLY_ARCHIVE = (
    "normals-monthly/1991-2020/archive/"
    "us-climate-normals_1991-2020_v1.0.1_monthly_multivariate_by-station_c20230404.tar.gz"
)

# Completeness, best to worst, for the monthly set's summary flag.
FLAG_ORDER = {"S": 0, "R": 1, "P": 2, "E": 3, "Q": 4, "": 5}


def _download(relative: str, into: str) -> str:
    path = os.path.join(into, os.path.basename(relative))
    if not os.path.exists(path):
        print(f"downloading {ARCHIVE_BASE}{relative} ...")
        response = requests.get(ARCHIVE_BASE + relative, timeout=600)
        response.raise_for_status()
        with open(path, "wb") as handle:
            handle.write(response.content)
    return path


def _station_files(archive_path: str):
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(".csv"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            yield list(csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline="")))


def _clean(value) -> str:
    value = (value or "").strip()
    return "" if value in ("-9999", "-9999.0", "-9999.00") else value


def annual_rows(archive_path: str) -> dict:
    """{station: partial bundle row} from the annual archive."""
    rows = {}
    for records in _station_files(archive_path):
        if not records or "ANN-PRCP-NORMAL" not in records[0]:
            continue
        record = records[0]
        normal = _clean(record.get("ANN-PRCP-NORMAL"))
        if not normal:
            continue
        rows[record["STATION"].strip()] = {
            "station": record["STATION"].strip(),
            "latitude": _clean(record.get("LATITUDE")),
            "longitude": _clean(record.get("LONGITUDE")),
            "elevation_m": _clean(record.get("ELEVATION")),
            "name": _clean(record.get("NAME")),
            "prcp_normal_in": normal,
            "prcp_flag": _clean(record.get("comp_flag_ANN-PRCP-NORMAL")),
            "prcp_years": _clean(record.get("years_ANN-PRCP-NORMAL")),
            "days_ge_1in": _clean(record.get("ANN-PRCP-AVGNDS-GE100HI")),
            "days_ge_1in_flag": _clean(record.get("comp_flag_ANN-PRCP-AVGNDS-GE100HI")),
        }
    return rows


def monthly_days(archive_path: str) -> dict:
    """{station: ([12 monthly days >= 1 in as str], worst flag)} from the
    monthly archive; a station missing any month is left out."""
    out = {}
    for records in _station_files(archive_path):
        if not records or "MLY-PRCP-AVGNDS-GE100HI" not in records[0]:
            continue
        by_month = {}
        flags = []
        for record in records:
            try:
                month = int(record.get("month", "").strip())
            except ValueError:
                continue
            value = _clean(record.get("MLY-PRCP-AVGNDS-GE100HI"))
            if value:
                by_month[month] = value
                flags.append(_clean(record.get("comp_flag_MLY-PRCP-AVGNDS-GE100HI")))
        if len(by_month) == 12:
            worst = max(flags, key=lambda f: FLAG_ORDER.get(f, 5))
            out[records[0]["STATION"].strip()] = ([by_month[m] for m in range(1, 13)], worst)
    return out


def assemble(annual: dict, monthly: dict, cache: dict) -> list:
    rows = []
    for station, row in sorted(annual.items()):
        months, flag = monthly.get(station, ([""] * 12, ""))
        for index, value in enumerate(months, start=1):
            row[f"days_ge_1in_m{index:02d}"] = value
        row["days_ge_1in_monthly_flag"] = flag
        cached = cache.get("stations", {}).get(station, {})
        daymet_mm = cached.get("daymet_annual_mm")
        if daymet_mm and daymet_mm > 0:
            row["daymet_annual_mm"] = f"{daymet_mm:.2f}"
            row["daymet_version"] = cached.get("daymet_version") or ""
            row["ratio"] = f"{float(row['prcp_normal_in']) * precipitation_normals.MM_PER_INCH / daymet_mm:.4f}"
        else:
            row["daymet_annual_mm"] = ""
            row["daymet_version"] = ""
            row["ratio"] = ""
        rows.append({column: row.get(column, "") for column in precipitation_normals.BUNDLE_COLUMNS})
    return rows


def write_bundle(path: str, rows: list, vintage: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("# " + json.dumps(vintage, sort_keys=True) + "\n")
        writer = csv.DictWriter(handle, fieldnames=list(precipitation_normals.BUNDLE_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--annual", default=None, help="a local copy of the annual/seasonal archive")
    parser.add_argument("--monthly", default=None, help="a local copy of the monthly archive")
    parser.add_argument("--cache", default=CACHE_PATH, help="the Daymet-at-station cache JSON")
    parser.add_argument("--out", default=precipitation_normals.BUNDLE_PATH)
    args = parser.parse_args(argv)

    into = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_normals_download")
    os.makedirs(into, exist_ok=True)
    annual_path = args.annual or _download(ANNUAL_ARCHIVE, into)
    monthly_path = args.monthly or _download(MONTHLY_ARCHIVE, into)
    cache = load_cache(args.cache)

    annual = annual_rows(annual_path)
    monthly = monthly_days(monthly_path)
    rows = assemble(annual, monthly, cache)

    versions = Counter(r["daymet_version"] for r in rows if r["daymet_version"])
    vintage = {
        "format": precipitation_normals.BUNDLE_FORMAT,
        "source": "NCEI U.S. Climate Normals 1991-2020, annual/seasonal and monthly by-station archives",
        "annual_archive": os.path.basename(ANNUAL_ARCHIVE),
        "monthly_archive": os.path.basename(MONTHLY_ARCHIVE),
        "archive_url_base": ARCHIVE_BASE,
        "period": "1991-2020",
        "daymet": {
            "period": "1991-2020",
            "versions": dict(versions),
            "stations_with_ratio": sum(1 for r in rows if r["ratio"]),
        },
        "units": {"prcp_normal_in": "inches", "daymet_annual_mm": "mm", "days_ge_1in": "days", "elevation_m": "m"},
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    write_bundle(args.out, rows, vintage)
    with_months = sum(1 for r in rows if r["days_ge_1in_m01"])
    print(f"wrote {args.out}: {len(rows)} stations with an annual precipitation normal, "
          f"{with_months} with monthly heavy-day normals, {vintage['daymet']['stations_with_ratio']} with a Daymet ratio "
          f"(Daymet versions {dict(versions)}), {os.path.getsize(args.out) / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
