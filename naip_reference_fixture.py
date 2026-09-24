"""
naip_reference_fixture.py

The reference parcel's NAIP answer, offline: assets/reference/design/
naip_reference_fixture.json and its JPEG tiles, written once by
make_naip_fixture.py from a live Planetary Computer fetch.

    raw_naip()   -> what naip_imagery.get_naip_for_boundary() returned
    naip_block() -> naip_imagery.parse_naip() of it
"""

import json
import os

import naip_imagery

DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "reference", "design")


def raw_naip() -> dict:
    with open(os.path.join(DIRECTORY, "naip_reference_fixture.json"), encoding="utf-8") as handle:
        meta = json.load(handle)
    images = []
    for tile in meta["tiles"]:
        with open(os.path.join(DIRECTORY, tile["file"]), "rb") as handle:
            images.append(handle.read())
    return naip_imagery.tiles_from_fixture(meta, images)


def naip_block() -> dict:
    return naip_imagery.parse_naip(raw_naip())
