"""
livestock_predators.py

THE WILDLIFE LINE on the site overview: which animals producers in the
parcel's State reported killing their livestock, from the bundled USDA
APHIS NAHMS death-loss surveys (class E; make_predator_loss_bundle.py
builds the bundle, by hand, from the two reports' PDFs).

    load_bundle()                 -> the bundle, cached
    state_code(name)              -> "PA" for "Pennsylvania"
    predator_line(state_code)     -> the line's figures and its parts, or
                                     a not-surveyed answer

ONE OR TWO SOURCED SENTENCES, NOT A SECTION. Fencing & animals was
investigated and dropped (report_outline.py); what survives is the one
fact public data carries about animals and agriculture at State grain.

PRODUCER-REPORTED SURVEY DATA, AND WHY THAT IS THE SOURCE. The NAHMS
reports record what operations keeping cattle, calves, sheep and lambs
said killed them. The alternative, APHIS Wildlife Services' program data,
records where the program operated rather than what did the damage, and
its State tables are dominated by airport work. That is branch 17's step
0 finding and the reason this module has one source.

THE LIMIT IS PRINTED, NOT FOOTNOTED: livestock only, nothing about crop
damage (deer, birds), and the survey year. A State a report does not list
separately is "not surveyed separately", never "no losses", and the line
is then absent rather than borrowed from a neighbouring State.

SUPPRESSED CELLS STAY UNKNOWN. A "(D)" cell (fewer than 20 head, or too
few reports) is null in the bundle and is never counted as zero: it
cannot lead the line, and it is not named as a predator that "also" took
stock unless the same predator has a printed figure elsewhere.
"""

import json
import os
import threading
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_PATH = os.path.join(_HERE, "assets", "reference", "nahms_predator_losses_2015.json")
BUNDLE_FORMAT = "nahms-predator-losses-v1"

SOURCE_RECORDS = {
    "cattle": {
        "title": "Death Loss in U.S. Cattle and Calves Due to Predator and Nonpredator Causes, 2015",
        "publisher": "USDA APHIS Veterinary Services, National Animal Health Monitoring System",
        "published": "December 2017",
        "survey_year": 2015,
        "tables": "A.2.d, A.2.e, D.1.c, D.2.d",
        "url": "https://www.aphis.usda.gov/sites/default/files/cattle_calves_deathloss_2015.pdf",
    },
    "sheep": {
        "title": "Sheep and Lamb Predator and Nonpredator Death Loss in the United States, 2015",
        "publisher": "USDA APHIS Veterinary Services, National Animal Health Monitoring System",
        "published": "September 2015",
        "survey_year": 2014,
        "tables": "A.2.a, C.8, C.9, C.10",
        "url": "https://www.aphis.usda.gov/sites/default/files/sheepdeathloss2015.pdf",
    },
}

TERMS = "U.S. federal work; public domain."

# Printed as a sentence of its own after the line.
CAVEAT = ("Producer-reported survey figures for operations keeping livestock; "
          "they say nothing about crop damage by deer or birds.")

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

# Columns that are not a named predator: they can never lead the line.
_NOT_A_PREDATOR = ("other predators", "unknown predators", "total", "other known", "other unknown")

# The cattle tables split bears by species; the sheep tables do not. The
# `also` list names the animal once, as the coarser table does.
_CANONICAL = {"black bears": "bears", "grizzly bears": "bears"}

_LOCK = threading.Lock()
_LOADED = {}


def load_bundle(path: str = BUNDLE_PATH) -> dict:
    with _LOCK:
        if path not in _LOADED:
            with open(path, encoding="utf-8") as handle:
                bundle = json.load(handle)
            if bundle.get("format") != BUNDLE_FORMAT:
                raise ValueError(f"{path}: bundle format {bundle.get('format')!r}, expected {BUNDLE_FORMAT!r}")
            _LOADED[path] = bundle
        return _LOADED[path]


def state_code(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    for code, full in STATE_NAMES.items():
        if full.lower() == name.strip().lower():
            return code
    return None


def _lead(shares: dict) -> Optional[tuple]:
    """(predator, share) of the largest printed share among named predators."""
    named = [(k, v) for k, v in shares.items() if v is not None and k not in _NOT_A_PREDATOR and v > 0]
    if not named:
        return None
    return max(named, key=lambda kv: kv[1])


def _sheep_shares(row: dict) -> tuple:
    """Per named predator, sheep plus lambs head where either is printed,
    as a percentage of the State's printed predator deaths; and the set of
    predators with any printed head."""
    deaths = row.get("sheep_deaths") or {}
    total = (deaths.get("predator sheep") or 0) + (deaths.get("predator lambs") or 0)
    heads = {}
    for column, value in (row.get("sheep_predator_head") or {}).items():
        predator = column.rsplit(" ", 1)[0]
        if value is None:
            heads.setdefault(predator, None)
            continue
        heads[predator] = (heads.get(predator) or 0) + value
    shares = {p: (100.0 * h / total if (h is not None and total) else None) for p, h in heads.items()}
    return shares, total


def predator_line(code: Optional[str], bundle: Optional[dict] = None) -> dict:
    """
    {'state', 'surveyed': bool, 'cattle': {...} | None, 'sheep': {...} | None,
     'also': [predator, ...], 'sources': SOURCE_RECORDS, 'caveat'}

    `cattle` is {'calf_lead', 'calf_pct', 'cattle_lead', 'cattle_pct',
    'calf_predator_head', 'cattle_predator_head', 'year'}; `sheep`
    {'lead', 'pct', 'predator_head', 'year'}. `also` names every other
    predator with a printed, non-zero figure in any of the tables, in
    descending order of that figure's share.
    """
    bundle = bundle or load_bundle()
    row = (bundle.get("states") or {}).get(code or "")
    result = {"state": STATE_NAMES.get(code or ""), "surveyed": False, "cattle": None, "sheep": None, "also": [],
              "sources": SOURCE_RECORDS, "caveat": CAVEAT}
    if not row:
        return result
    mentions = {}
    calf = _lead(row.get("calf_predator_pct") or {})
    cattle = _lead(row.get("cattle_predator_pct") or {})
    if calf or cattle:
        result["cattle"] = {
            "calf_lead": calf[0] if calf else None, "calf_pct": calf[1] if calf else None,
            "cattle_lead": cattle[0] if cattle else None, "cattle_pct": cattle[1] if cattle else None,
            "calf_predator_head": (row.get("calf_deaths") or {}).get("predator"),
            "cattle_predator_head": (row.get("cattle_deaths") or {}).get("predator"),
            "year": SOURCE_RECORDS["cattle"]["survey_year"],
        }
        for table in ("calf_predator_pct", "cattle_predator_pct"):
            for k, v in (row.get(table) or {}).items():
                if v and k not in _NOT_A_PREDATOR:
                    k = _CANONICAL.get(k, k)
                    mentions[k] = max(mentions.get(k, 0.0), v)
    shares, total = _sheep_shares(row)
    sheep = _lead(shares)
    if sheep:
        result["sheep"] = {"lead": sheep[0], "pct": sheep[1], "predator_head": total,
                           "year": SOURCE_RECORDS["sheep"]["survey_year"]}
        for k, v in shares.items():
            if v and k not in _NOT_A_PREDATOR:
                mentions[k] = max(mentions.get(k, 0.0), v)
    leads = {_CANONICAL.get(x, x) for x in (calf and calf[0], cattle and cattle[0], sheep and sheep[0]) if x}
    result["also"] = [k for k, _ in sorted(mentions.items(), key=lambda kv: -kv[1]) if k not in leads]
    result["surveyed"] = bool(result["cattle"] or result["sheep"])
    return result
