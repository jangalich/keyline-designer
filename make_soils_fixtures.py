"""
make_soils_fixtures.py

CAPTURES THE SOILS & GEOLOGY SECTION'S REFERENCE FIXTURES from the live
services -- one real response per source for the reference parcel
(reference_fixture.REAL_BOUNDARY), written under assets/reference/soils/
so every Soils test runs from them with the network refused
(soils_reference_fixture.py loads them).

    python3 make_soils_fixtures.py

What it writes, and the fetch each one is the verbatim answer to:

    soil_survey.json     soil_survey.get_survey_for_boundary() -- the
                         report layer's one SSURGO query: the map unit
                         symbols, the surface horizon's properties, land
                         capability, the T factor and the depth to
                         bedrock.
    soil_erosion.json    soil_data.get_erosion_factor_for_polygon() --
                         LAYER 1's K factor rows. The reference
                         ParcelData carried [] for this field because no
                         section had ever printed a K factor; the Soils
                         section does, off Layer 1 rather than fetching
                         it again, so the fixture has to carry what Layer
                         1 really returns.
    bedrock_geology.json bedrock_geology.get_geology_for_boundary() --
                         the envelope members, the centroid members and
                         the per-unit records, exactly as the fetch
                         assembles them.
    capture.json         when, and how big each response was

Run from a machine with network; the sandbox the tests run in has none.
"""

import json
import os
import sys
import time
from datetime import date

import bedrock_geology
import soil_data
import soil_survey
from reference_fixture import REAL_BOUNDARY

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "reference", "soils")


def _write_json(name, payload):
    path = os.path.join(OUT, name)
    text = json.dumps(payload, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    record = {"boundary": [list(p) for p in REAL_BOUNDARY], "captured_on": date.today().isoformat(), "files": {}}
    wkt = soil_data.coordinates_to_wkt_polygon(REAL_BOUNDARY)

    for name, fetch, describe in (
        ("soil_survey.json", lambda: soil_survey.get_survey_for_boundary(REAL_BOUNDARY), lambda v: f"{len(v)} rows"),
        ("soil_erosion.json", lambda: soil_data.get_erosion_factor_for_polygon(wkt), lambda v: f"{len(v)} rows"),
        ("bedrock_geology.json", lambda: bedrock_geology.get_geology_for_boundary(REAL_BOUNDARY),
         lambda v: f"{len(v['envelope'])} in envelope, {len(v['units'])} unit(s)"),
    ):
        started = time.time()
        payload = fetch()
        elapsed = time.time() - started
        size = _write_json(name, payload)
        record["files"][name] = {"bytes_on_disk": size, "seconds": round(elapsed, 1), "describes": describe(payload)}
        print(f"{name:24s} {size:>9,} B on disk  {elapsed:5.1f} s  {describe(payload)}")

    _write_json("capture.json", record)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
