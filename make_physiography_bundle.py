#!/usr/bin/env python3
"""
make_physiography_bundle.py

BUILDS THE BUNDLED PHYSIOGRAPHIC PROVINCES (class E) from the USGS
national physiographic divisions shapefile -- run by hand; the source is
a 1946 map and does not change.

    python3 make_physiography_bundle.py                  # download, build
    python3 make_physiography_bundle.py --from-zip physio_shp.zip

WHAT IT READS. physio_shp.zip (Fenneman and Johnson 1946, 501 polygons,
geographic NAD83), from the ScienceBase item physiography.SOURCE_URL
points at. Read with pyshp, a build-time tool, not a project dependency.

WHAT IT WRITES. physiography.BUNDLE_PATH: the polygons dissolved by
province (DIVISION, PROVINCE, PROVCODE -- the SECTION field is dropped,
see physiography.py), simplified at SIMPLIFY_DEGREES with topology
preserved, coordinates rounded to four decimals, as gzipped GeoJSON whose
top-level `properties` carries the format, the citation and the build
date. Polygons with no province name (six slivers in the source) are
dropped: a point in one reads as not mapped.
"""

import argparse
import gzip
import io
import json
import os
import sys
import zipfile
from datetime import date

import requests
import shapely
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

import physiography

DOWNLOAD_URL = "https://water.usgs.gov/GIS/dsdl/physio_shp.zip"
SIMPLIFY_DEGREES = 0.005


def _round(coords):
    if isinstance(coords, (list, tuple)) and coords and isinstance(coords[0], (int, float)):
        return [round(coords[0], 4), round(coords[1], 4)]
    return [_round(c) for c in coords]


def build(zip_bytes: bytes) -> dict:
    import shapefile

    archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = {os.path.splitext(n)[1].lower(): n for n in archive.namelist() if n.lower().startswith("physio.")}
    reader = shapefile.Reader(shp=io.BytesIO(archive.read(names[".shp"])), shx=io.BytesIO(archive.read(names[".shx"])),
                              dbf=io.BytesIO(archive.read(names[".dbf"])))
    groups = {}
    for record in reader.iterShapeRecords():
        props = record.record.as_dict()
        province = (props.get("PROVINCE") or "").strip()
        if not province:
            continue
        key = ((props.get("DIVISION") or "").strip(), province, props.get("PROVCODE"))
        groups.setdefault(key, []).append(shape(record.shape.__geo_interface__).buffer(0))
    features = []
    for (division, province, code), parts in sorted(groups.items(), key=lambda kv: kv[0][2] or 0):
        # Rounded to four decimals by snapping on a 1e-4 grid, which keeps
        # the polygons valid; rounding the coordinates after the fact does not.
        geometry = shapely.set_precision(unary_union(parts).simplify(SIMPLIFY_DEGREES, preserve_topology=True), 1e-4)
        if not geometry.is_valid:
            raise ValueError(f"{province}: invalid after simplification")
        geo = mapping(geometry)
        features.append({"type": "Feature", "properties": {"DIVISION": division, "PROVINCE": province, "PROVCODE": code},
                         "geometry": {"type": geo["type"], "coordinates": _round(geo["coordinates"])}})
    return {
        "type": "FeatureCollection",
        "properties": {"format": physiography.BUNDLE_FORMAT, "citation": physiography.CITATION,
                       "source": physiography.SOURCE_URL, "simplified_degrees": SIMPLIFY_DEGREES,
                       "built_on": date.today().isoformat()},
        "features": features,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-zip", help="read physio_shp.zip from disk instead of downloading")
    args = parser.parse_args(argv)
    if args.from_zip:
        with open(args.from_zip, "rb") as handle:
            data = handle.read()
    else:
        response = requests.get(DOWNLOAD_URL, timeout=120)
        response.raise_for_status()
        data = response.content
    collection = build(data)
    text = json.dumps(collection, separators=(",", ":"))
    os.makedirs(os.path.dirname(physiography.BUNDLE_PATH), exist_ok=True)
    with gzip.open(physiography.BUNDLE_PATH, "wt", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {physiography.BUNDLE_PATH}: {len(collection['features'])} provinces, "
          f"{len(text):,} bytes of GeoJSON, {os.path.getsize(physiography.BUNDLE_PATH):,} gzipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
