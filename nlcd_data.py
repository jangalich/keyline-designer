"""
nlcd_data.py

Fetches USGS National Land Cover Database (NLCD) classification for a
property boundary from Microsoft's Planetary Computer STAC catalog — the
same free, no-API-key source imagery_data.py already uses for Sentinel-2 —
and returns both a plain percent-breakdown summary and a schema-conformant
GeoJSON FeatureCollection (see feature_schema.py), one feature per
land-cover class present within the boundary.

Why this exists: imagery_data.py's Sentinel-2 NDVI reading measures
photosynthetic vigor, not vegetation type, and can't reliably tell dense
pasture/hayfield from tree canopy — a well-watered hayfield in peak growing
season can score identically to mature forest canopy. NLCD is instead a
pre-classified, federally validated land-cover product: grassland/pasture/
hay, deciduous/evergreen/mixed forest, developed, water, wetland, etc. are
already distinguished in its own classification scheme, not inferred from
a vigor proxy. This module makes NLCD the authoritative source for
land-cover TYPE; NDVI's role after this module exists is downgraded to a
vigor/health signal read within whatever type NLCD has already
established (see report_generator.py), not a type-detector itself.

Docs:
  https://www.mrlc.gov/data
  https://www.mrlc.gov/data/legends/national-land-cover-database-class-legend-and-description
  https://planetarycomputer.microsoft.com/dataset/nlcd
"""

import time
from typing import Optional

import numpy as np
import planetary_computer
import rasterio
from pystac_client import Client
from rasterio.features import shapes as raster_shapes
from rasterio.mask import mask
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import unary_union

from feature_schema import CONFIDENCE_HIGH, make_feature, make_feature_collection, polygonal_parts

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "nlcd"

# Standard NLCD class legend (2019/2021 classification scheme). Codes and
# names per USGS's published legend — see module docstring for the URL.
# Not every code will show up in a given search result (e.g. dwarf scrub
# and the "Alaska only" classes are extremely unlikely in the continental
# US), but the full legend is kept here so an unexpected code still gets a
# real name instead of falling back to "Unknown class N".
NLCD_CLASSES = {
    11: "Open Water",
    12: "Perennial Ice/Snow",
    21: "Developed, Open Space",
    22: "Developed, Low Intensity",
    23: "Developed, Medium Intensity",
    24: "Developed, High Intensity",
    31: "Barren Land (Rock/Sand/Clay)",
    41: "Deciduous Forest",
    42: "Evergreen Forest",
    43: "Mixed Forest",
    51: "Dwarf Scrub",
    52: "Shrub/Scrub",
    71: "Grassland/Herbaceous",
    72: "Sedge/Herbaceous",
    73: "Lichens",
    74: "Moss",
    81: "Pasture/Hay",
    82: "Cultivated Crops",
    90: "Woody Wetlands",
    95: "Emergent Herbaceous Wetlands",
}

NLCD_CONFIDENCE_NOTES_TEMPLATE = (
    "USGS NLCD classifies land cover at 30m pixel resolution from its "
    "{year} survey. Small features narrower than about 30m (a single "
    "hedgerow, a footpath) may blend into a neighboring class, and any "
    "land-use change since {year} (recent clearing, planting, or "
    "construction) won't be reflected yet — but these are resolution/"
    "currency limits, not doubt about the classification itself."
)


def _boundary_to_polygon(boundary_coordinates: list) -> Polygon:
    """
    Converts a list of (longitude, latitude) tuples into a shapely Polygon,
    closing the ring if the input isn't already closed. Same convention as
    imagery_data.py's _boundary_to_polygon.
    """
    coords = list(boundary_coordinates)
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return Polygon(coords)


# Planetary Computer's STAC API and blob storage have been observed
# failing/timing out at a much higher rate (up to ~70% per attempt during
# periods of load) than the "usually fast, occasionally slow" case the
# 2-retries/fixed-2s-pause pattern elsewhere in this pipeline (soil_data.py,
# hydrology_data.py) was built for. At a 70% per-attempt failure rate, that
# pattern's 3 total attempts still fails end-to-end about 1 time in 3
# (0.7^3 ≈ 34%). 6 attempts (5 retries) brings that down to roughly 1 in 8
# (0.7^6 ≈ 12%), and exponential backoff between attempts (instead of a
# fixed 2s pause) gives transient load/connectivity issues more room to
# clear on later attempts instead of hammering the same failure right away.
DEFAULT_MAX_RETRIES = 5
_MAX_TIMEOUT_SECONDS = 90
_MAX_BACKOFF_SECONDS = 30


def _retry(operation, max_retries: int = DEFAULT_MAX_RETRIES):
    """
    Calls operation(timeout), giving later attempts progressively more
    time (ramping up, capped at _MAX_TIMEOUT_SECONDS so a single attempt
    can't balloon indefinitely) and backing off exponentially between
    attempts (2s, 4s, 8s, ... capped at _MAX_BACKOFF_SECONDS) rather than
    retrying immediately or waiting a fixed pause. See the module-level
    comment above DEFAULT_MAX_RETRIES for why this needed to be more
    aggressive than the fixed-2-retries pattern used elsewhere in this
    pipeline.
    """
    last_error = None

    for attempt in range(max_retries + 1):
        timeout = min(30 + (attempt * 15), _MAX_TIMEOUT_SECONDS)
        try:
            return operation(timeout)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                backoff = min(2 ** (attempt + 1), _MAX_BACKOFF_SECONDS)
                time.sleep(backoff)
                continue
            raise last_error


def _search_latest_nlcd_item(polygon: Polygon, max_retries: int = DEFAULT_MAX_RETRIES):
    """
    Searches Planetary Computer's "nlcd" STAC collection for the item
    (Planetary Computer publishes one per survey year/region) intersecting
    the boundary, sorted most-recent-first, and returns the first match —
    or None if nothing intersects.
    """
    def _do_search(timeout):
        catalog = Client.open(
            STAC_API_URL,
            modifier=planetary_computer.sign_inplace,
            timeout=timeout,
        )
        search = catalog.search(
            collections=[COLLECTION],
            intersects=mapping(polygon),
            sortby="-datetime",
            limit=10,
        )
        return list(search.items())

    items = _retry(_do_search, max_retries=max_retries)
    return items[0] if items else None


def _read_clipped_landcover(href: str, polygon: Polygon, timeout: float):
    """
    Opens the NLCD raster asset (over HTTPS, via a Planetary-Computer-
    signed URL) and clips it to the property boundary's bounding box,
    returning the clipped class-code band, its affine transform, and its
    CRS. Same clip-then-mask pattern as imagery_data.py's
    _read_clipped_band, but this also needs the transform and CRS back
    (not just the array) since the land-cover geometry gets vectorized
    downstream — imagery_data.py never needs to reproject pixels back into
    shapes, this module does.
    """
    with rasterio.Env(GDAL_HTTP_TIMEOUT=str(int(timeout)), GDAL_HTTP_CONNECTTIMEOUT=str(int(timeout))):
        with rasterio.open(href) as src:
            geom_in_raster_crs = transform_geom("EPSG:4326", src.crs, mapping(polygon))
            clipped, out_transform = mask(src, [geom_in_raster_crs], crop=True, filled=True, nodata=0)
            return clipped[0], out_transform, src.crs


def _fetch_nlcd_raster(boundary_coordinates: list) -> Optional[tuple]:
    """
    Network-fetching step: finds the most recent NLCD item intersecting
    the boundary and returns its clipped class-code array plus enough
    metadata (transform, CRS, survey year) to both summarize and vectorize
    it. Returns None if no NLCD coverage was found for this boundary.
    """
    polygon = _boundary_to_polygon(boundary_coordinates)
    item = _search_latest_nlcd_item(polygon)

    if item is None:
        return None

    asset_key = "landcover" if "landcover" in item.assets else next(iter(item.assets))
    href = item.assets[asset_key].href

    landcover, out_transform, raster_crs = _retry(
        lambda timeout: _read_clipped_landcover(href, polygon, timeout)
    )

    survey_year = str(item.datetime.year) if item.datetime else "unknown"

    return landcover, out_transform, raster_crs, survey_year


def _classify_landcover(landcover: np.ndarray) -> list[dict]:
    """
    Pure function (no network): given a clipped NLCD class-code array
    (0 = nodata, same convention as imagery_data.py's raster reads), returns
    the percent of valid pixels in each class present, sorted most-common
    first:

        [{'code': 81, 'name': 'Pasture/Hay', 'pct_of_boundary': 78.2}, ...]
    """
    valid = landcover > 0
    if not np.any(valid):
        return []

    codes, counts = np.unique(landcover[valid], return_counts=True)
    total = int(counts.sum())

    classes = [
        {
            "code": int(code),
            "name": NLCD_CLASSES.get(int(code), f"Unknown NLCD class {int(code)}"),
            "pct_of_boundary": round(100 * int(count) / total, 1),
        }
        for code, count in zip(codes, counts)
    ]
    classes.sort(key=lambda c: -c["pct_of_boundary"])
    return classes


def _vectorize_landcover(
    landcover: np.ndarray, out_transform, raster_crs, boundary_polygon: Polygon
) -> dict[int, dict]:
    """
    Pure function (no network): polygonizes contiguous same-class regions
    of the clipped land-cover raster (rasterio.features.shapes, in the
    raster's native CRS), merges same-class regions together, clips to the
    exact boundary polygon — mask()'s crop=True upstream only limited the
    read to the boundary's bounding box, so cells inside that box but
    outside the actual drawn shape still need excluding here — and
    reprojects each class's merged geometry back to WGS84.

    Returns {code: geojson_geometry_dict}, omitting any class whose only
    "geometry" turned out to be a degenerate edge/point touch after
    clipping (see feature_schema.polygonal_parts).
    """
    boundary_in_raster_crs = shape(
        transform_geom("EPSG:4326", raster_crs, mapping(boundary_polygon))
    )

    geoms_by_code: dict[int, list] = {}
    for geom, value in raster_shapes(landcover, mask=(landcover > 0), transform=out_transform):
        geoms_by_code.setdefault(int(value), []).append(shape(geom))

    result = {}
    for code, geoms in geoms_by_code.items():
        clipped = polygonal_parts(unary_union(geoms).intersection(boundary_in_raster_crs))
        if clipped is None:
            continue
        result[code] = mapping(shape(transform_geom(raster_crs, "EPSG:4326", mapping(clipped))))

    return result


def get_nlcd_summary_for_boundary(boundary_coordinates: list) -> Optional[dict]:
    """
    Given a property boundary (list of (longitude, latitude) points, same
    format as the rest of this pipeline), returns a plain percent-breakdown
    summary:

        {
            'survey_year': '2021',
            'classes': [
                {'code': 81, 'name': 'Pasture/Hay', 'pct_of_boundary': 78.2},
                {'code': 41, 'name': 'Deciduous Forest', 'pct_of_boundary': 15.4},
                ...
            ],
            'dominant_class': 'Pasture/Hay',
        }

    Returns None if no NLCD coverage was found for this boundary (should be
    rare in the continental US, but kept consistent with how
    imagery_data.py and hydrology_data.py handle a genuine "no data"
    outcome — callers should skip this section of the report rather than
    fail the whole pipeline).
    """
    fetched = _fetch_nlcd_raster(boundary_coordinates)
    if fetched is None:
        return None

    landcover, _out_transform, _raster_crs, survey_year = fetched
    classes = _classify_landcover(landcover)
    if not classes:
        return None

    return {
        "survey_year": survey_year,
        "classes": classes,
        "dominant_class": classes[0]["name"],
    }


def get_nlcd_features_geojson(boundary_coordinates: list) -> dict:
    """
    Same fetch as get_nlcd_summary_for_boundary, but returns the result as
    a GeoJSON FeatureCollection following the shared schema (see
    feature_schema.py): one Feature per land-cover class present within the
    boundary. Confidence is fixed at "high" — NLCD is a validated federal
    dataset, not an inferred/thresholded reading — and confidence_notes
    states NLCD's own known limitations (30m resolution, survey currency)
    rather than any NDVI-style type ambiguity.
    """
    fetched = _fetch_nlcd_raster(boundary_coordinates)
    if fetched is None:
        return make_feature_collection([])

    landcover, out_transform, raster_crs, survey_year = fetched
    boundary_polygon = _boundary_to_polygon(boundary_coordinates)

    geometries_by_code = _vectorize_landcover(landcover, out_transform, raster_crs, boundary_polygon)
    stats_by_code = {c["code"]: c for c in _classify_landcover(landcover)}
    confidence_notes = NLCD_CONFIDENCE_NOTES_TEMPLATE.format(year=survey_year)

    features = []
    for code, geometry in geometries_by_code.items():
        name = NLCD_CLASSES.get(code, f"Unknown NLCD class {code}")
        stats = stats_by_code.get(code, {})

        features.append(
            make_feature(
                feature_id=f"nlcd-{survey_year}-{code}",
                geometry=geometry,
                layer="land-cover",
                label=name,
                confidence=CONFIDENCE_HIGH,
                confidence_notes=confidence_notes,
                extra_properties={
                    "nlcd_code": code,
                    "survey_year": survey_year,
                    "pct_of_boundary": stats.get("pct_of_boundary"),
                },
            )
        )

    return make_feature_collection(features)


def summarize_nlcd(summary: Optional[dict]) -> str:
    """
    Plain-language summary of the NLCD land-cover breakdown — same purpose
    as soil_data.summarize_soil_report and imagery_data.summarize_imagery: a
    quick human-readable check before this feeds the Claude-generated
    narrative report.
    """
    if not summary:
        return "No USGS NLCD land cover data available for this property."

    lines = [f"USGS NLCD land cover ({summary['survey_year']} survey):"]
    for c in summary["classes"]:
        lines.append(f"  - {c['name']}: {c['pct_of_boundary']}% of boundary")
    return "\n".join(lines)


if __name__ == "__main__":
    # Test case: the user's own drawn property boundary (Richland Township, PA)
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Fetching USGS NLCD land cover for property boundary...\n")

    try:
        summary = get_nlcd_summary_for_boundary(property_boundary)
        print(summarize_nlcd(summary))

        geojson = get_nlcd_features_geojson(property_boundary)
        from feature_schema import validate_feature_collection

        validate_feature_collection(geojson)
        print(
            f"\nGeoJSON FeatureCollection: {len(geojson['features'])} feature(s), "
            "schema-valid, every feature has confidence_notes."
        )
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach Planetary "
            "Computer's STAC API and blob storage — not a fully sandboxed "
            "environment."
        )
