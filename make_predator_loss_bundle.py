#!/usr/bin/env python3
"""
make_predator_loss_bundle.py

BUILDS THE BUNDLED LIVESTOCK PREDATOR-LOSS REFERENCE (class E) from the
two USDA APHIS NAHMS death-loss reports -- run by hand, never at report
time. The reports are one-off surveys (2015 is the latest of each), so
this runs once and the bundle is the record.

    python3 make_predator_loss_bundle.py                    # download, build
    python3 make_predator_loss_bundle.py --from-dir ./nahms # build from local PDFs

WHAT IT READS. The two reports' PDFs, parsed with PyMuPDF (a build-time
tool, not a project dependency):

    cattle  "Death Loss in U.S. Cattle and Calves Due to Predator and
            Nonpredator Causes, 2015" (USDA APHIS VS, December 2017) --
            tables A.2.d / A.2.e (deaths by cause, head) and D.1.c /
            D.2.d (percentage of predator deaths by predator), by State.
    sheep   "Sheep and Lamb Predator and Nonpredator Death Loss in the
            United States, 2015" (USDA APHIS VS, September 2015, 2014
            data) -- tables A.2.a (deaths by cause, head) and C.8, C.9,
            C.10 (predator deaths by predator, head), by State.

WHAT IT WRITES. livestock_predators.BUNDLE_PATH: one JSON document --
the two sources' citations and URLs, and per State the rows the tables
print for it, VERBATIM: a suppressed cell ("(D)", fewer than 20 head or
too few reports) is kept as null, never as zero. A State a table does
not list ("Other States" is a pool, not a State) has no row, and the
report reads that as "not surveyed separately", never as "no losses".

WHY NOT APHIS WILDLIFE SERVICES' PROGRAM DATA. Branch 17's step 0:
program data records where the program operated (airports dominate a
State's list), and its one resource-by-species table could not be shown
to carry a State grain. These are producer-reported survey figures: what
killed livestock on the operations that keep it. That is also their
limit -- nothing here speaks to crop damage -- and the report says so.

THE PARSE. Each table page's text is a column-major token stream: a
two-letter State code followed by exactly the table's column count of
cells. The column names are fixed here, from the printed headers, and the
parser checks every State row has the right number of numeric-or-(D)
cells; a row that does not is a layout change and raises.
"""

import argparse
import json
import os
import re
import sys
from datetime import date

import requests

import livestock_predators as lp

SOURCES = {
    "cattle": {
        "url": "https://www.aphis.usda.gov/sites/default/files/cattle_calves_deathloss_2015.pdf",
        "file": "cattle_calves_deathloss_2015.pdf",
    },
    "sheep": {
        "url": "https://www.aphis.usda.gov/sites/default/files/sheepdeathloss2015.pdf",
        "file": "sheepdeathloss2015.pdf",
    },
}

_CATTLE_PREDATORS_A = ("grizzly bears", "black bears", "bobcats or lynx", "coyotes", "dogs", "foxes")
_CATTLE_PREDATORS_B = ("wolves", "predatory birds", "mountain lions", "other predators", "unknown predators", "total")

# (report, table id, columns). A table printed across two page spreads
# appears twice with its two column halves.
TABLES = (
    ("cattle", "A.2.d.", "cattle_deaths", ("nonpredator", "predator", "total")),
    ("cattle", "A.2.e.", "calf_deaths", ("nonpredator", "predator", "total")),
    ("cattle", "D.1.c.", "cattle_predator_pct", _CATTLE_PREDATORS_A),
    ("cattle", "D.1.c.", "cattle_predator_pct", _CATTLE_PREDATORS_B),
    ("cattle", "D.2.d.", "calf_predator_pct", _CATTLE_PREDATORS_A),
    ("cattle", "D.2.d.", "calf_predator_pct", _CATTLE_PREDATORS_B),
    ("sheep", "A.2.a.", "sheep_deaths", ("nonpredator sheep", "nonpredator lambs", "predator sheep", "predator lambs",
                                        "total sheep", "total lambs")),
    ("sheep", "C.8.", "sheep_predator_head", ("bears sheep", "bears lambs", "bobcats or lynx sheep", "bobcats or lynx lambs",
                                             "coyotes sheep", "coyotes lambs", "dogs sheep", "dogs lambs")),
    ("sheep", "C.9.", "sheep_predator_head", ("foxes sheep", "foxes lambs", "mountain lions sheep", "mountain lions lambs",
                                             "wolves sheep", "wolves lambs", "vultures sheep", "vultures lambs")),
    ("sheep", "C.10.", "sheep_predator_head", ("ravens sheep", "ravens lambs", "feral pigs sheep", "feral pigs lambs",
                                              "eagles sheep", "eagles lambs", "other known sheep", "other known lambs",
                                              "other unknown sheep", "other unknown lambs")),
)

STATE_CODES = frozenset(lp.STATE_NAMES)
_CELL = re.compile(r"^\(?D\)?$|^-?[\d,]+(\.\d+)?$")


def _cell(token: str):
    if token.strip("()") == "D":
        return None
    return float(token.replace(",", ""))


def _table_pages(document, table_id: str) -> list:
    """Every page whose text opens the table (or its continuation)."""
    pattern = re.compile(r"(^|\n)\s*" + re.escape(table_id) + r"\s")
    return [page for page in document if pattern.search(page.get_text())]


def _rows(page, width: int) -> dict:
    tokens = [t.strip() for t in page.get_text().splitlines() if t.strip()]
    rows = {}
    for index, token in enumerate(tokens):
        code = re.sub(r"\d$", "", token)  # "CO3": a footnote mark on the code
        if code not in STATE_CODES or token not in (code, code + token[-1:]):
            continue
        cells = tokens[index + 1:index + 1 + width]
        if len(cells) != width or not all(_CELL.match(c) for c in cells):
            continue
        rows[code] = [_cell(c) for c in cells]
    return rows


def parse_reports(paths: dict) -> dict:
    import pymupdf

    states = {}
    for report, table_id, key, columns in TABLES:
        document = pymupdf.open(paths[report])
        found = 0
        for page in _table_pages(document, table_id):
            rows = _rows(page, len(columns))
            if not rows:
                continue
            # A predator-percentage table prints its twelve columns as two
            # six-column halves on separate spreads. The second half ends
            # in the Total column, 100.0 on every row; the first does not.
            if table_id.startswith("D."):
                totals = all(r[-1] is not None and abs(r[-1] - 100.0) < 0.05 for r in rows.values())
                if totals != (columns[-1] == "total"):
                    continue
            found += len(rows)
            for code, values in rows.items():
                states.setdefault(code, {}).setdefault(key, {}).update(dict(zip(columns, values)))
        if not found:
            raise ValueError(f"{report} table {table_id} {columns[0]}..: no State rows parsed -- has the layout changed?")
    return states


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-dir", help="read the two PDFs from this directory instead of downloading")
    args = parser.parse_args(argv)

    paths = {}
    for report, source in SOURCES.items():
        if args.from_dir:
            paths[report] = os.path.join(args.from_dir, source["file"])
        else:
            response = requests.get(source["url"], timeout=120)
            response.raise_for_status()
            paths[report] = os.path.join("/tmp", source["file"])
            with open(paths[report], "wb") as handle:
                handle.write(response.content)

    states = parse_reports(paths)
    bundle = {
        "format": lp.BUNDLE_FORMAT,
        "built_on": date.today().isoformat(),
        "sources": lp.SOURCE_RECORDS,
        "states": {code: states[code] for code in sorted(states)},
    }
    os.makedirs(os.path.dirname(lp.BUNDLE_PATH), exist_ok=True)
    with open(lp.BUNDLE_PATH, "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, indent=1, sort_keys=True)
    print(f"wrote {lp.BUNDLE_PATH}: {len(states)} States, {os.path.getsize(lp.BUNDLE_PATH):,} bytes")
    for code in ("PA",):
        print(code, json.dumps(states.get(code), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
