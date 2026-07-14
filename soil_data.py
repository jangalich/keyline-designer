"""
soil_data.py

Fetches soil survey data (SSURGO) for a given point or parcel from USDA's
Soil Data Access (SDA) REST service. No API key required — it's a free,
public USDA endpoint.

Docs: https://sdmdataaccess.nrcs.usda.gov/webservicehelp.aspx
"""

import requests
from typing import Optional

SDA_ENDPOINT = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"


def _run_sda_query(sql: str) -> dict:
    """
    Sends a raw SQL query to the SDA REST endpoint and returns the parsed
    JSON response. Raises an exception if the request fails or SDA returns
    an error payload.
    """
    payload = {
        "QUERY": sql,
        "FORMAT": "JSON+COLUMNNAME",
    }

    response = requests.post(SDA_ENDPOINT, json=payload, timeout=30)
    response.raise_for_status()

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
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        print("\nNote: this requires internet access to reach USDA's servers.")
        print("Run this in an environment with network access (e.g. Render, Railway,")
        print("or your own machine) — not in a fully sandboxed environment.")
