"""
bedrock_geology.py

THE ROCK UNDER THE PARCEL, for the site data report's Soils & geology
section: the geologic unit (or units) the national compilation maps
across the boundary, named, dated and described.

    get_geology_for_boundary(boundary)  -> the raw responses (a fetch)
    parse_geology(raw)                  -> the parsed block

ONE LINE, NOT A MAP AND NOT A TABLE. The compilation is drawn for use at
about 1:1,000,000; at that scale a thirteen-acre parcel is a point, and
the honest product is the formation's name, its age and what it is made
of, set as a line of prose. Drawing a two-colour polygon map of a parcel
from linework generalised a thousand times coarser than the parcel would
imply a contact this source does not place.

THE SOURCE. USGS State Geologic Map Compilation (SGMC), Horton, J.D.,
2017, Version 1.1, https://doi.org/10.5066/F7WH2N65 (report
https://doi.org/10.3133/ds1052), served as WFS 2.0 at
mrdata.usgs.gov/services/wfs/sgmc2, layer ms:Lithology. No
authentication, no key, no fees.

TERMS, FROM THE METADATA RECORD, NOT THE LANDING PAGE -- the NWI
precedent. USGS_SGMC_Metadata.xml (the URL the WFS capabilities
advertises, .../metadata/sgmc2.xml, is a 404) states Access Constraints
"None", Fees "None. No fees are applicable for obtaining the data set",
and Use Constraints that restrict nothing commercially: no guarantee of
accuracy, acknowledgement of the USGS appreciated, the user agrees not to
misrepresent the data. A U.S. federal work in the public domain.

THE SCALE THE RECORD STATES, NOT THE ONE THE SERVICE ADVERTISES. The WFS
capabilities abstract says "1:500,000 scale". The metadata record says
the compilation is assembled from state maps ranging from 1:50,000 to
1:1,000,000 and is "intended for use at approximately 1:1,000,000 scale
or smaller". The record governs, and its number is the caveat the page
prints -- it is the stronger caveat of the two, and printing the
service's friendlier one would be printing the wrong fact.

A STATE SURVEY WOULD NOT BE BETTER HERE, AND WOULD NOT GENERALISE. The
compilation's Pennsylvania polygons ARE the state survey's: each unit
record cites Berg et al. 1980 and Miles & Whitfield 2001, "Bedrock
Geology of Pennsylvania", 1:250,000, by way of Dicken et al. 2008. Going
to the state directly would return the same linework under a second
schema and a second terms statement, and every state publishes on its
own schedule, scale, schema and terms -- the standing rule against
generalising a Pennsylvania source applies. SGMC is the only national
bedrock polygon set with one schema and one statement of terms.

TWO CALLS AT MOST FOR THE UNIT LIST, AND USUALLY ONE. The WFS has no
server-side clip (a feature's geometry is its whole outcrop belt: 5.07 MB
for the reference parcel's two, against 2.2 KB for the same query with
propertyName set and no geometry), and its fes:Intersects filter fails
server-side. So the fetch asks the parcel's ENVELOPE for the units it
touches, attributes only. When that returns ONE unit there is nothing
more to resolve and the fetch stops. When it returns more than one, a
second query at the centroid -- a degenerate bbox, which is how this
service answers a point -- says which unit is under the middle of the
parcel, so the line can lead with it. Each unit's name, age, province
and description then come from mrdata's own per-unit JSON.

THE REFERENCE PARCEL STRADDLES TWO (branch 12, step 0, verified live).
Casselman Formation over most of it, Glenshaw Formation along the
south-west valley bottom -- both Conemaugh Group, Pennsylvanian, which
SSURGO corroborates from the other side: its map unit is named "Rayne
silt loam, CONEMAUGH GEOLOGY, 8 to 15 percent slopes". A parcel with one
unit is the common case and the cheap one; this one is neither, and the
block carries however many there are.
"""

import re
from typing import Optional
from xml.etree import ElementTree

import requests
from shapely.geometry import Polygon

import fetch_attempts

SGMC_WFS = "https://mrdata.usgs.gov/services/wfs/sgmc2"
SGMC_UNIT_JSON = "https://mrdata.usgs.gov/geology/state/json/"
SGMC_LAYER = "ms:Lithology"

# The attributes the WFS carries; naming them is what keeps the response
# at kilobytes instead of megabytes (see the module docstring).
SGMC_FIELDS = ("state", "orig_label", "sgmc_label", "unit_link", "ref_id", "generalize", "src_url", "url")

# The degenerate bbox that asks this service for a point. Small enough to
# be a point at 1:1,000,000 (about 2 m), large enough that the server
# does not reject an empty envelope.
POINT_BBOX_DEGREES = 2e-5

MAPSERVER_NS = "http://mapserver.gis.umn.edu/mapserver"
WFS_NS = "http://www.opengis.net/wfs/2.0"

SGMC_METADATA_URL = "https://mrdata.usgs.gov/geology/state/USGS_SGMC_Metadata.xml"
SGMC_DOI = "https://doi.org/10.5066/F7WH2N65"
SGMC_ACCESS_CONSTRAINTS = "None. Please refer to 'Distribution Info' for details."
SGMC_USE_CONSTRAINTS = (
    "These data are intended for use at approximately 1:1,000,000 scale or smaller. There is no guarantee "
    "concerning the accuracy of the data. Acknowledgment of the U.S. Geological Survey would be appreciated "
    "in products derived from these data"
)
# The number the page prints, off the record above.
SGMC_INTENDED_SCALE = 1000000

SGMC_CITATION = (
    "Horton, J.D., 2017, The State Geologic Map Compilation (SGMC) Geodatabase of the Conterminous United "
    "States, Version 1.1: U.S. Geological Survey data release, https://doi.org/10.5066/F7WH2N65, served as "
    "WFS layer ms:Lithology by the USGS Mineral Resources Program."
)


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


class GeologyIncompleteError(RuntimeError):
    """The service answered and mapped no geologic unit at this parcel --
    the parcel is outside the conterminous-states compilation, or falls in
    a hole in it. A retry will not help, which is what distinguishes this
    from a RequestException."""


def _get(url: str, params: Optional[dict] = None, max_retries: int = 2):
    """One GET with the progressive timeouts and published attempts every
    fetch in this codebase uses (hydrology_data._query_layer's shape)."""
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        timeout = 30 + attempt * 30
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def _bbox_params(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> dict:
    # urn:ogc:def:crs:EPSG::4326 is LATITUDE FIRST -- the urn form carries
    # the authority's own axis order, unlike the "EPSG:4326" short form,
    # and getting it backwards silently returns a bbox in the Indian
    # Ocean rather than an error.
    return {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": SGMC_LAYER,
        "propertyName": ",".join(SGMC_FIELDS),
        "bbox": f"{min_lat},{min_lon},{max_lat},{max_lon},urn:ogc:def:crs:EPSG::4326",
        "count": 50,
    }


def _parse_members(xml_text: str) -> list:
    """The ms:Lithology members of a WFS FeatureCollection, as dicts of
    the fields asked for. Geometry is never present: propertyName left it
    out."""
    root = ElementTree.fromstring(xml_text)
    members = []
    for element in root.iter(f"{{{MAPSERVER_NS}}}Lithology"):
        member = {}
        for field in SGMC_FIELDS:
            child = element.find(f"{{{MAPSERVER_NS}}}{field}")
            member[field] = child.text.strip() if child is not None and child.text else None
        members.append(member)
    return members


def _unit_json(unit_link: str) -> dict:
    return _get(SGMC_UNIT_JSON + unit_link).json()


@fetch_attempts.publishes
def get_geology_for_boundary(boundary_coordinates: list) -> dict:
    """
    {'envelope': [member, ...], 'centroid': [member, ...] | None,
     'units': {unit_link: the unit's JSON}}

    One WFS call over the parcel's envelope; a second at the centroid ONLY
    when the first found more than one unit (see the module docstring);
    one JSON call per distinct unit. Raises GeologyIncompleteError when
    the envelope call maps nothing.
    """
    polygon = Polygon([(float(lon), float(lat)) for lon, lat in boundary_coordinates])
    min_lon, min_lat, max_lon, max_lat = polygon.bounds
    envelope = _parse_members(_get(SGMC_WFS, _bbox_params(min_lat, min_lon, max_lat, max_lon)).text)
    if not envelope:
        raise GeologyIncompleteError("SGMC maps no geologic unit over this parcel")

    centroid = None
    if len({m["unit_link"] for m in envelope}) > 1:
        point = polygon.centroid
        half = POINT_BBOX_DEGREES
        centroid = _parse_members(
            _get(SGMC_WFS, _bbox_params(point.y - half, point.x - half, point.y + half, point.x + half)).text
        )

    units = {}
    for member in envelope:
        link = member["unit_link"]
        if link and link not in units:
            units[link] = _unit_json(link)
    return {"envelope": envelope, "centroid": centroid, "units": units}


def _lithology_words(unit: dict) -> list:
    """The unit's MAJOR lithologies, lowercased and deduplicated in the
    order the record lists them -- the record's own `low_lith` (the
    narrowest term in each row's hierarchy), which is the word a reader
    recognises: "shale", not "Sedimentary - Clastic - Mudstone - Shale"."""
    words = []
    for row in unit.get("lith") or []:
        if (row.get("lith_rank") or "").lower() != "major":
            continue
        word = (row.get("low_lith") or "").strip().lower()
        if word and word not in words:
            words.append(word)
    return words


def _first_sentence(text: Optional[str]) -> Optional[str]:
    """The unit description's first clause, WITHOUT its terminator.

    These records are one long semicolon-joined sentence -- "Cyclic
    sequences of shale, siltstone, ...; red beds are associated with
    landslides; base is at top of Ames limestone." -- and the line only
    has room for the first clause. The terminator goes with the rest of
    the sentence it no longer introduces, so the section punctuates its
    own line rather than inheriting a dangling semicolon."""
    if not text:
        return None
    match = re.match(r"\s*(.+?)[.;](\s|$)", text)
    return (match.group(1) if match else text).strip().rstrip(".;,")


def parse_geology(raw: dict) -> dict:
    """
    The responses -> the block:

        {'units': [{'unit_link', 'label', 'name', 'age', 'age_min_ma',
                    'age_max_ma', 'province', 'description', 'lithologies',
                    'at_centroid': bool}],
         'at_centroid': unit_link | None,
         'straddles': bool}

    Units are ordered with the centroid's first -- the one the line leads
    with -- and the rest in the order the service returned them. `label`
    is the state's own map label (the letters printed on the state sheet);
    `name` is the formation. `straddles` is True when the compilation maps
    more than one unit across the parcel's extent, which is the fact the
    line has to state rather than pick a winner over.
    """
    units_json = raw.get("units") or {}
    at_centroid = None
    for member in raw.get("centroid") or []:
        if member.get("unit_link"):
            at_centroid = member["unit_link"]
            break

    seen, ordered = set(), []
    for member in raw.get("envelope") or []:
        link = member.get("unit_link")
        if not link or link in seen:
            continue
        seen.add(link)
        ordered.append(member)
    if at_centroid:
        ordered.sort(key=lambda m: m["unit_link"] != at_centroid)

    units = []
    for member in ordered:
        link = member["unit_link"]
        record = units_json.get(link) or {}
        ages = (record.get("age") or [{}])[0]
        units.append({
            "unit_link": link,
            "label": member.get("orig_label"),
            "name": record.get("unit_name"),
            "age": record.get("unit_age"),
            "age_min_ma": float(ages["min_ma"]) if ages.get("min_ma") else None,
            "age_max_ma": float(ages["max_ma"]) if ages.get("max_ma") else None,
            "province": record.get("province"),
            "description": _first_sentence(record.get("unitdesc")),
            "lithologies": _lithology_words(record),
            "at_centroid": link == at_centroid,
        })
    return {"units": units, "at_centroid": at_centroid, "straddles": len(units) > 1}


if __name__ == "__main__":
    from reference_fixture import REAL_BOUNDARY

    block = parse_geology(get_geology_for_boundary(REAL_BOUNDARY))
    print("straddles:", block["straddles"])
    for unit in block["units"]:
        print(f"  {unit['label']:>5} {unit['name']} ({unit['age']}, {unit['age_max_ma']}-{unit['age_min_ma']} Ma) "
              f"centroid={unit['at_centroid']} lith={unit['lithologies']}")
        print(f"        {unit['description']}")
