"""
make_naip_fixture.py

BUILDS assets/reference/design/naip_reference_fixture.{json,jpg}: the NAIP answer
for the reference parcel (reference_fixture.REAL_BOUNDARY), fetched LIVE
from Planetary Computer once and committed, so the layout map's tests
draw real photography offline.

    python make_naip_fixture.py

The pixels are stored as JPEG (quality 90) and everything else -- item
id, acquisition date, ground resolution, CRS, window transform -- as
JSON. naip_reference_fixture.raw_naip() reads them back into the shape
naip_imagery.get_naip_for_boundary() returns.
"""

import json
import os

import naip_imagery
from reference_fixture import REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
DIRECTORY = os.path.join(HERE, "assets", "reference", "design")


def main():
    raw = naip_imagery.get_naip_for_boundary(REAL_BOUNDARY)
    meta, images = naip_imagery.tiles_to_fixture(raw)
    for index, data in enumerate(images):
        name = f"naip_reference_fixture_{index}.jpg"
        with open(os.path.join(DIRECTORY, name), "wb") as handle:
            handle.write(data)
        meta["tiles"][index]["file"] = name
        print(f"{name}: {len(data):,} bytes, {meta['tiles'][index]['id']}, acquired {meta['tiles'][index]['acquired']}")
    with open(os.path.join(DIRECTORY, "naip_reference_fixture.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=1)
        handle.write("\n")


if __name__ == "__main__":
    main()
