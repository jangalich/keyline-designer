"""
physiography.py

THE PHYSIOGRAPHIC DIVISION AND PROVINCE the parcel lies in, for the site
overview, from a bundled copy of the USGS national physiographic
divisions (class E; make_physiography_bundle.py builds it, by hand).

    load_bundle()                  -> the bundle, cached
    province_at(lat, lon)          -> {'division', 'province', ...} or None

THE SOURCE. Fenneman, N.M. and Johnson, D.W., 1946, Physiographic
divisions of the conterminous U.S.: U.S. Geological Survey data release,
https://doi.org/10.5066/P9B1S3K8 -- digitised from the 1:7,000,000
"Physical Divisions of the United States". Its FGDC record states
Access_Constraints "None" and Use_Constraints "The scale of the data
(1:7,000,000) limits its use to broad overviews of physiographic
divisions." A U.S. federal work; nothing restricts commercial use.

DIVISION AND PROVINCE ONLY, NEVER THE SECTION. The source also names a
section, and for the reference parcel it names "Kanawha" -- where the
Pennsylvania state survey (DCNR Map 13) names the same ground the
Pittsburgh Low Plateau Section. A reader who knows the state's name reads
the national one as an error, and there is no national section-level
source that agrees with the state ones, so the bundle does not carry
sections at all: a field the report may not print is a field someone
later prints. At 1:7,000,000 a province is the honest grain -- the
reference parcel's nearest province boundary is 129 km away.

THE BUNDLE is the source dissolved to its 24 named provinces and simplified at
0.005 degrees (about 500 m, far inside the source's own 1:7,000,000
linework), coordinates to four decimals, gzipped GeoJSON. A point that
falls in no province (offshore, outside the conterminous U.S.) is None,
which the report prints as not mapped.
"""

import gzip
import json
import os
import threading
from typing import Optional

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

_HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_PATH = os.path.join(_HERE, "assets", "reference", "physiographic_provinces.json.gz")
BUNDLE_FORMAT = "usgs-physiographic-provinces-v1"

CITATION = ("Fenneman, N.M. and Johnson, D.W., 1946, Physiographic divisions of the conterminous U.S.: "
            "U.S. Geological Survey data release, https://doi.org/10.5066/P9B1S3K8.")
SOURCE_URL = "https://www.sciencebase.gov/catalog/item/631405bbd34e36012efa304e"
TERMS = ('FGDC metadata: Access_Constraints "None"; Use_Constraints "The scale of the data (1:7,000,000) limits '
         'its use to broad overviews of physiographic divisions." U.S. federal work; public domain.')
SCALE = 7000000

_LOCK = threading.Lock()
_LOADED = {}


def load_bundle(path: str = BUNDLE_PATH) -> dict:
    """{'header': {...}, 'features': [...], 'tree': STRtree, 'geoms': [...]}"""
    with _LOCK:
        if path not in _LOADED:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                collection = json.load(handle)
            header = collection.get("properties") or {}
            if header.get("format") != BUNDLE_FORMAT:
                raise ValueError(f"{path}: bundle format {header.get('format')!r}, expected {BUNDLE_FORMAT!r}")
            geoms = [shape(f["geometry"]) for f in collection["features"]]
            _LOADED[path] = {"header": header, "features": collection["features"], "geoms": geoms,
                             "tree": STRtree(geoms)}
        return _LOADED[path]


def _title(name: Optional[str]) -> Optional[str]:
    """The source's names are upper case ("APPALACHIAN PLATEAUS"); the
    page sets them as proper nouns."""
    if not name:
        return None
    small = {"and", "of", "the"}
    words = name.strip().lower().split()
    return " ".join(w if (i and w in small) else w.capitalize() for i, w in enumerate(words))


def province_at(lat: float, lon: float, path: str = BUNDLE_PATH) -> Optional[dict]:
    """{'division', 'province', 'province_code', 'citation', 'scale'} for
    the province containing the point, or None where none does."""
    bundle = load_bundle(path)
    point = Point(lon, lat)
    for index in bundle["tree"].query(point):
        if bundle["geoms"][index].covers(point):
            props = bundle["features"][index]["properties"]
            return {
                "division": _title(props.get("DIVISION")),
                "province": _title(props.get("PROVINCE")),
                "province_code": props.get("PROVCODE"),
                "citation": CITATION,
                "scale": SCALE,
            }
    return None
