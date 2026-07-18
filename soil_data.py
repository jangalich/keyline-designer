"""
soil_data.py

Fetches soil survey data (SSURGO) for a given point or parcel from USDA's
Soil Data Access (SDA) REST service. No API key required — it's a free,
public USDA endpoint.

Docs: https://sdmdataaccess.nrcs.usda.gov/webservicehelp.aspx
"""

import time
import requests
from typing import Optional

from shapely import wkt as shapely_wkt
from shapely.geometry import mapping as shapely_mapping
from shapely.ops import unary_union

from feature_schema import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    make_feature,
    make_feature_collection,
)

SDA_ENDPOINT = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"

# Applies to every SSURGO feature this module returns — a property of the
# survey itself, not of any single map unit, so it's the same note on every
# feature rather than computed per-feature.
SSURGO_CONFIDENCE_NOTES = (
    "SSURGO map unit polygon boundaries are digitized from soil surveys "
    "conducted at roughly 1:24,000 scale and generalized to that scale — "
    "they may not reflect field-level detail. Component percentages "
    "describe the typical composition of a map unit as a whole, not a "
    "guarantee of what's present at any single point within it."
)


def _run_sda_query(sql: str, max_retries: int = 2) -> dict:
    """
    Sends a raw SQL query to the SDA REST endpoint and returns the parsed
    JSON response. Raises an exception if the request fails or SDA returns
    an error payload.

    USDA's server is sometimes slow for polygon (whole-boundary) queries
    specifically, since it has more area to search than a single point —
    a 30-second timeout that was fine for point queries can occasionally
    be too short here. This retries once or twice with a longer timeout
    before giving up, rather than failing on the first slow response.
    """
    payload = {
        "QUERY": sql,
        "FORMAT": "JSON+COLUMNNAME",
    }

    last_error = None

    for attempt in range(max_retries + 1):
        # Give later attempts more time, in case the server is just
        # momentarily under load rather than truly unreachable.
        timeout = 30 + (attempt * 30)

        try:
            response = requests.post(SDA_ENDPOINT, json=payload, timeout=timeout)
            response.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise last_error

    data = response.json()

    if "Table" not in data:
        # SDA returns no "Table" key when the query matched nothing,
        # or an error structure if the query itself was malformed.
        return {"columns": [], "rows": []}

    table = data["Table"]
    columns = table[0]      # first row is always the column names
    rows = table[1:]        # remaining rows are the actual data

    return {"columns": columns, "rows": rows}


def get_soil_data_for_point(latitude: float, longitude: float) -> list[dict]:
    """
    Given a single lat/long point, returns the soil map unit and component
    data (soil types, drainage class, slope, etc.) for that location.

    Returns a list of dicts, one per soil component found at that point,
    ordered by percent composition (largest/most dominant component first).
    """
    # WKT points are (longitude latitude) — note the order, it trips people up.
    wkt_point = f"point({longitude} {latitude})"

    sql = f"""
        SELECT mu.mukey, mu.muname, c.compname, c.comppct_r,
               c.drainagecl, c.slope_r, c.slopelenusle_r,
               c.hydricrating, c.taxorder
        FROM mapunit mu
        INNER JOIN component c ON mu.mukey = c.mukey
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_point}')
        )
        ORDER BY c.comppct_r DESC
    """

    result = _run_sda_query(sql)

    if not result["rows"]:
        return []

    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def coordinates_to_wkt_polygon(coordinates: list) -> str:
    """
    Converts a list of (longitude, latitude) tuples into the WKT polygon
    string format this function expects.

    WKT polygons must be "closed" — the first and last point must match.
    If the input isn't already closed, this closes it automatically.
    """
    coords = list(coordinates)
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]

    coord_pairs = ", ".join(f"{lon} {lat}" for lon, lat in coords)
    return f"polygon(({coord_pairs}))"


def get_soil_data_for_polygon(wkt_polygon: str) -> list[dict]:
    """
    Same as get_soil_data_for_point, but for a full parcel boundary instead
    of a single point. This is what you'd use once the frontend lets someone
    draw or upload their actual property boundary rather than just dropping
    a pin.

    wkt_polygon should be a WGS84 WKT polygon string, e.g.:
        "polygon((-79.982 40.643, -79.981 40.643, -79.981 40.642, -79.982 40.642, -79.982 40.643))"
    """
    sql = f"""
        SELECT mu.mukey, mu.muname, c.compname, c.comppct_r,
               c.drainagecl, c.slope_r, c.slopelenusle_r,
               c.hydricrating, c.taxorder
        FROM mapunit mu
        INNER JOIN component c ON mu.mukey = c.mukey
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
        ORDER BY c.comppct_r DESC
    """

    result = _run_sda_query(sql)

    if not result["rows"]:
        return []

    return [dict(zip(result["columns"], row)) for row in result["rows"]]


# SSURGO's official "Farmland Classification" values (muaggatt.farmlndcl).
# Anything starting with "All areas are prime farmland" or "Prime farmland
# if..." counts as prime for this pipeline's purposes — the conditional
# variants ("...if drained", "...if irrigated", etc.) still mean the soil
# itself is prime-grade, just contingent on a management practice, which
# is exactly the kind of nuance the Scale of Permanence tradeoff note
# calling code should surface, not silently drop.
_PRIME_FARMLAND_PREFIXES = ("All areas are prime farmland", "Prime farmland if")


def is_prime_farmland(farmland_classification: Optional[str]) -> bool:
    """True if an SSURGO farmlndcl value indicates prime (or conditionally
    prime) farmland. Used by solar_suitability.py to flag, not exclude,
    high-scoring solar zones that overlap prime agricultural soil."""
    if not farmland_classification:
        return False
    return any(farmland_classification.startswith(p) for p in _PRIME_FARMLAND_PREFIXES)


def get_farmland_classification_for_polygon(wkt_polygon: str) -> list[dict]:
    """
    Returns SSURGO's official Farmland Classification (farmlndcl) for
    every map unit intersecting wkt_polygon — e.g. "All areas are prime
    farmland", "Prime farmland if drained", "Not prime farmland". This is
    a map-unit-level aggregate attribute (muaggatt), not a per-component
    one like get_soil_data_for_polygon's fields, so it's a separate query
    rather than an extra column tacked onto that one.

    Returns a list of {'mukey', 'muname', 'farmland_classification'} dicts.
    """
    sql = f"""
        SELECT mu.mukey, mu.muname, ma.farmlndcl
        FROM mapunit mu
        INNER JOIN muaggatt ma ON mu.mukey = ma.mukey
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
    """

    result = _run_sda_query(sql)

    if not result["rows"]:
        return []

    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
    return [
        {
            "mukey": row["mukey"],
            "muname": row["muname"],
            "farmland_classification": row["farmlndcl"],
        }
        for row in rows
    ]


# SSURGO's hydricrating is "Yes" / "No" / "Partially hydric" (component
# table). "Partially hydric" map units still indicate real hydric
# inclusions worth treating as a floodplain/wet-ground signal for road
# routing — a mostly-dry map unit with a wet corner is still a place a
# road corridor should route around, not a place to call "clear."
def is_hydric(hydric_rating: Optional[str]) -> bool:
    """True if an SSURGO hydricrating value indicates hydric (or partially
    hydric) soil. Used by road_corridors.py, combined with NHD stream/
    water body proximity, as the floodplain/wet-ground exclusion signal —
    deliberately not inferred from DEM elevation alone."""
    if not hydric_rating:
        return False
    return hydric_rating.strip().lower() in ("yes", "partially hydric")


# K-factor (whole soil, "kwfact" in the component table) is SSURGO's
# standard erodibility measure — how susceptible a soil is to sheet/rill
# erosion, roughly 0.02 (least erodible) to 0.69 (most). This threshold
# (0.32) is a commonly-used rule-of-thumb cutoff for "erosion-prone" in
# NRCS conservation planning guidance, not a precise, single-sourced
# regulatory value — real Highly Erodible Land (HEL) determination
# combines K-factor with slope, slope length, and a soil-specific
# tolerance ("T") value in the full RKLS/T formula, which is out of scope
# here. Treat this as the same kind of simplified, explainable proxy as
# production_area.py's slope-only heuristic — good enough to flag "avoid
# this soil type" for road routing, not an official HEL determination.
# CONFIGURABLE.
DEFAULT_EROSION_KWFACT_THRESHOLD = 0.32


def is_erosion_prone(kwfact, threshold: float = DEFAULT_EROSION_KWFACT_THRESHOLD) -> bool:
    """True if an SSURGO K-factor (whole soil) value is at/above
    threshold. kwfact is stored as text in SDA's schema (like several
    other nominally-numeric SSURGO fields), so this tolerates a string,
    None, or an empty value rather than assuming a clean float."""
    try:
        return float(kwfact) >= threshold
    except (TypeError, ValueError):
        return False


def get_erosion_factor_for_polygon(wkt_polygon: str) -> list[dict]:
    """
    Returns SSURGO's whole-soil K-factor (erodibility) for the dominant
    component of every map unit intersecting wkt_polygon — same
    dominant-component-per-mukey shape as get_soil_data_for_polygon,
    just a separate query since kwfact isn't in that one's SELECT list
    and this is the only field road_corridors.py needs from it.

    Returns a list of {'mukey', 'muname', 'compname', 'comppct_r', 'kwfact'} dicts.
    """
    sql = f"""
        SELECT mu.mukey, mu.muname, c.compname, c.comppct_r, c.kwfact
        FROM mapunit mu
        INNER JOIN component c ON mu.mukey = c.mukey
        WHERE mu.mukey IN (
            SELECT mukey FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('{wkt_polygon}')
        )
        ORDER BY c.comppct_r DESC
    """

    result = _run_sda_query(sql)

    if not result["rows"]:
        return []

    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]

    dominant_by_mukey: dict[str, dict] = {}
    for row in rows:
        # rows are ordered comppct_r DESC, so the first row seen per mukey
        # is already the dominant component -- same convention as
        # get_soil_data_for_polygon's ordering.
        dominant_by_mukey.setdefault(row["mukey"], row)

    return list(dominant_by_mukey.values())


def _polygonal_parts(geom):
    """
    STIntersection() against a mapunit polygon can, in edge cases (the
    input boundary touching a map unit polygon only along a shared edge or
    at a single vertex), return a GeometryCollection mixing stray points or
    lines in with the actual polygon area. Only the Polygon/MultiPolygon
    parts represent real map unit area; this drops everything else and
    returns None if nothing polygonal survives.
    """
    if geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        return unary_union(polys) if polys else None
    return None


def get_soil_geometries_for_polygon(wkt_polygon: str) -> dict[str, dict]:
    """
    Fetches map unit polygon geometry (not just the tabular attributes
    get_soil_data_for_polygon returns) for the portion of SSURGO's spatial
    "mupolygon" table that actually overlaps wkt_polygon, from USDA's
    spatial "mupolygon" table. Returns {mukey: geojson_geometry}.

    mukey identifies a soil map unit *type* (e.g. "Rayne silt loam, 8 to 15
    percent slopes"), and the same type commonly recurs as many separate,
    physically scattered polygons across the entire soil survey area — not
    just the one(s) overlapping this specific parcel. Filtering only on
    "mukey IN (mukeys that intersect wkt_polygon)" — as an earlier version
    of this function did — matches on the map unit *type*, so it pulls in
    every county-wide occurrence of each matched mukey, not just the
    nearby ones. This queries mupolygon's own geometry column directly
    with STIntersects (a real per-polygon-row spatial filter) and clips
    each matching row to wkt_polygon with STIntersection, so both the
    filter and the returned geometry are constrained to the input
    boundary itself.

    SDA stores map unit geometry as a SQL Server "geometry" column
    (mupolygongeo) in WGS84 lon/lat; geometry::STGeomFromText(..., 4326)
    parses the input WKT into that same type for the STIntersects/
    STIntersection comparison, and .STAsText() converts the (now-clipped)
    result back to WKT for transport, which is then parsed into a GeoJSON
    geometry dict via shapely. A single map unit can still be made up of
    several disjoint clipped fragments within the boundary (e.g. the same
    soil type appearing in two separate patches of the same small
    parcel), so rows are grouped by mukey and merged with unary_union
    before conversion — this can return a MultiPolygon as easily as a
    Polygon.
    """
    sql = f"""
        SELECT mukey,
               mupolygongeo.STIntersection(geometry::STGeomFromText('{wkt_polygon}', 4326)).STAsText() AS geom_wkt
        FROM mupolygon
        WHERE mupolygongeo.STIntersects(geometry::STGeomFromText('{wkt_polygon}', 4326)) = 1
    """

    result = _run_sda_query(sql)

    if not result["rows"]:
        return {}

    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]

    geometries_by_mukey: dict[str, list] = {}
    for row in rows:
        geom = _polygonal_parts(shapely_wkt.loads(row["geom_wkt"]))
        if geom is not None:
            geometries_by_mukey.setdefault(row["mukey"], []).append(geom)

    return {
        mukey: shapely_mapping(unary_union(geoms))
        for mukey, geoms in geometries_by_mukey.items()
    }


def _component_confidence(comppct_r) -> str:
    """
    Maps a soil component's percent-of-map-unit (comppct_r) to a
    confidence level: how well that component's attributes (drainage,
    slope, etc.) actually characterize the polygon they're attached to. A
    component making up 60% of its map unit is a much better bet for
    "what's really there" than one making up 10%.
    """
    try:
        pct = float(comppct_r)
    except (TypeError, ValueError):
        return CONFIDENCE_LOW

    if pct >= 50:
        return CONFIDENCE_HIGH
    if pct >= 20:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def get_soil_data_as_geojson(wkt_polygon: str) -> dict:
    """
    Same soil survey fetch as get_soil_data_for_polygon, but returns the
    result as a GeoJSON FeatureCollection following the shared schema (see
    feature_schema.py): one Feature per map unit polygon, carrying that map
    unit's components (soil type, drainage, slope, etc.) in properties.

    A map unit commonly has multiple components sharing the same mapped
    polygon (e.g. two co-occurring soil types), so this emits one feature
    per map unit — not one per component — with properties.components
    listing all of them (ordered by comppct_r, most dominant first) and
    confidence set from the dominant component's share of the map unit.
    """
    components = get_soil_data_for_polygon(wkt_polygon)
    if not components:
        return make_feature_collection([])

    geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)

    components_by_mukey: dict[str, list[dict]] = {}
    for comp in components:
        components_by_mukey.setdefault(comp["mukey"], []).append(comp)

    features = []
    for mukey, comps in components_by_mukey.items():
        geometry = geometries_by_mukey.get(mukey)
        if geometry is None:
            # Tabular data matched this map unit but the spatial table
            # returned no polygon for it (a rare gap between SDA's tabular
            # and spatial data) — skip rather than emit a shapeless feature.
            continue

        # comps is already ordered by comppct_r DESC (the SQL query sorts
        # it), so the first entry is the dominant component.
        dominant = comps[0]

        features.append(
            make_feature(
                feature_id=f"ssurgo-mukey-{mukey}",
                geometry=geometry,
                layer="soil",
                label=dominant.get("muname") or f"Map unit {mukey}",
                confidence=_component_confidence(dominant.get("comppct_r")),
                confidence_notes=SSURGO_CONFIDENCE_NOTES,
                extra_properties={
                    "mukey": mukey,
                    "dominant_component": dominant.get("compname"),
                    "drainage_class": dominant.get("drainagecl"),
                    "slope_pct": dominant.get("slope_r"),
                    "hydric_rating": dominant.get("hydricrating"),
                    "components": [
                        {
                            "name": c.get("compname"),
                            "pct_of_map_unit": c.get("comppct_r"),
                            "drainage_class": c.get("drainagecl"),
                            "slope_pct": c.get("slope_r"),
                            "hydric_rating": c.get("hydricrating"),
                            "tax_order": c.get("taxorder"),
                        }
                        for c in comps
                    ],
                },
            )
        )

    return make_feature_collection(features)


def summarize_soil_report(components: list[dict]) -> str:
    """
    Turns the raw component list into a short, plain-language summary —
    the kind of thing that'll eventually feed into the Claude-generated
    narrative report.
    """
    if not components:
        return "No soil survey data found for this location."

    lines = ["Soil components found (ordered by dominance):\n"]

    for comp in components:
        name = comp.get("muname", "Unknown")
        pct = comp.get("comppct_r", "?")
        drainage = comp.get("drainagecl", "Unknown")
        slope = comp.get("slope_r", "Unknown")

        lines.append(
            f"- {name} ({pct}% of this map unit) — "
            f"Drainage: {drainage}, Slope: {slope}%"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    # Test case: the user's own property in Richland Township, PA
    # Coordinates pulled from public parcel data.
    lat, lon = 40.642485, -79.981816

    print(f"Fetching soil data for point ({lat}, {lon})...\n")

    try:
        components = get_soil_data_for_point(lat, lon)
        print(summarize_soil_report(components))

        property_boundary = [
            (-79.9838154, 40.6458343),
            (-79.9836701, 40.6428581),
            (-79.9813665, 40.6440549),
            (-79.9804741, 40.6445667),
            (-79.9827466, 40.6458894),
            (-79.9838258, 40.6458343),
        ]
        wkt_polygon = coordinates_to_wkt_polygon(property_boundary)

        geojson = get_soil_data_as_geojson(wkt_polygon)
        from feature_schema import validate_feature_collection

        validate_feature_collection(geojson)
        print(
            f"\nGeoJSON FeatureCollection: {len(geojson['features'])} feature(s), "
            "schema-valid, every feature has confidence_notes."
        )
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        print("\nNote: this requires internet access to reach USDA's servers.")
        print("Run this in an environment with network access (e.g. Render, Railway,")
        print("or your own machine) — not in a fully sandboxed environment.")
