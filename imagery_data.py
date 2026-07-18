"""
imagery_data.py

Fetches recent Sentinel-2 L2A satellite imagery for a property boundary from
Microsoft's Planetary Computer STAC catalog (free, no API key — signed access
URLs are generated automatically by the `planetary-computer` package) and
computes a coarse vegetation vigor breakdown from NDVI.

This does NOT return imagery itself — the report this feeds is a text
narrative, not a picture — it returns structured stats only: what fraction
of the parcel is bare/degraded ground, low-vigor vegetation, high-vigor
vegetation, or open water, plus the average NDVI and how current the source
scene is. Classification is done with fixed NDVI threshold ranges, not a
trained classifier — simple, fast, and explainable, which is all a vigor
snapshot needs to be.

NDVI measures photosynthetic vigor, not vegetation type or height — a
dense, well-watered hayfield in peak growing season can score just as high
as mature forest canopy, so this module has no way to tell them apart and
doesn't try to. Land-cover TYPE (forest vs. pasture/hay/grassland vs.
other) is nlcd_data.py's job, not this module's: it uses a pre-classified,
federally validated dataset instead of an NDVI proxy. This module's role is
downstream and narrower — given a type NLCD has already established for an
area, how vigorous/healthy does that area's vegetation currently look
(see report_generator.py, which cross-references the two).

This vigor picture should also be read alongside soil_data.py's drainage
data: a bare patch over a poorly-drained soil map unit and a bare patch
over a well-drained one imply very different things about the property
(report_generator.py's system prompt tells Claude to cross-reference the
two explicitly).

Docs:
  https://planetarycomputer.microsoft.com/docs/quickstarts/reading-stac-data/
  https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a
"""

import time
from datetime import date, timedelta
from typing import Optional

import numpy as np
import planetary_computer
import rasterio
from pystac_client import Client
from rasterio.mask import mask
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, mapping

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"

MAX_CLOUD_COVER_PCT = 20
LOOKBACK_DAYS = 365

RED_BAND = "B04"
NIR_BAND = "B08"

# NDVI thresholds for the coarse vigor buckets. Standard, widely-cited
# ranges for this kind of quick-look read — not tuned per-site. Anything
# below NDVI_WATER_MAX reads as open water; the rest is bucketed from bare
# ground up through high vigor — a vigor gradient only, not a type claim
# (see module docstring: nlcd_data.py handles type).
NDVI_WATER_MAX = 0.0
NDVI_BARE_MAX = 0.2
NDVI_LOW_VEG_MAX = 0.4


def _boundary_to_polygon(boundary_coordinates: list) -> Polygon:
    """
    Converts a list of (longitude, latitude) tuples into a shapely Polygon,
    closing the ring if the input isn't already closed. Same convention as
    soil_data.py's coordinates_to_wkt_polygon, just producing a shapely
    object instead of a WKT string since that's what the STAC search and
    raster clip below need.
    """
    coords = list(boundary_coordinates)
    if coords[0] != coords[-1]:
        coords = coords + [coords[0]]
    return Polygon(coords)


def _retry(operation, max_retries: int = 2):
    """
    Calls operation(timeout), giving later attempts progressively more time.

    Same reasoning as the retry logic in soil_data.py and hydrology_data.py:
    Planetary Computer's STAC API and blob storage are usually fast, but
    occasionally slow under load, and a single fixed timeout can fail a
    request that would have succeeded with a bit more patience.
    """
    last_error = None

    for attempt in range(max_retries + 1):
        timeout = 30 + (attempt * 30)
        try:
            return operation(timeout)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(2)
                continue
            raise last_error


def _search_scenes(
    polygon: Polygon, max_cloud_cover: float, lookback_days: int, max_retries: int = 2
) -> list:
    """
    Searches the Planetary Computer STAC catalog for Sentinel-2 L2A scenes
    intersecting the boundary, filtered to cloud cover under max_cloud_cover
    percent, within the last lookback_days days. Returns items sorted most-
    recent-first (empty list if nothing matched).
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    date_range = f"{start.isoformat()}/{end.isoformat()}"

    def _do_search(timeout):
        catalog = Client.open(
            STAC_API_URL,
            modifier=planetary_computer.sign_inplace,
            timeout=timeout,
        )
        search = catalog.search(
            collections=[COLLECTION],
            intersects=mapping(polygon),
            datetime=date_range,
            query={"eo:cloud_cover": {"lt": max_cloud_cover}},
            sortby="-datetime",
            limit=10,
        )
        return list(search.items())

    return _retry(_do_search, max_retries=max_retries)


def _read_clipped_band(href: str, polygon: Polygon, timeout: float) -> np.ndarray:
    """
    Opens a single raster band asset (over HTTPS, via a Planetary-Computer-
    signed URL) and clips it to the property boundary, returning the clipped
    band as a float array. GDAL's HTTP timeout is set to match the current
    retry attempt, same increasing-patience pattern as the other network
    calls in this module.
    """
    with rasterio.Env(GDAL_HTTP_TIMEOUT=str(int(timeout)), GDAL_HTTP_CONNECTTIMEOUT=str(int(timeout))):
        with rasterio.open(href) as src:
            # The boundary comes in as WGS84 lon/lat; Sentinel-2 tiles are in
            # a local UTM zone, so the clip geometry has to be reprojected to
            # match before rasterio.mask will clip correctly.
            geom_in_raster_crs = transform_geom("EPSG:4326", src.crs, mapping(polygon))
            clipped, _ = mask(src, [geom_in_raster_crs], crop=True, filled=True, nodata=0)
            return clipped[0].astype("float32")


def _classify_ndvi(ndvi: np.ndarray) -> dict:
    """
    Buckets a flat array of valid NDVI values into the four coarse land-
    cover categories using fixed threshold ranges, and returns the percent
    of pixels in each bucket alongside the average/min/max NDVI.
    """
    total = ndvi.size

    water = np.count_nonzero(ndvi < NDVI_WATER_MAX)
    bare = np.count_nonzero((ndvi >= NDVI_WATER_MAX) & (ndvi < NDVI_BARE_MAX))
    low_veg = np.count_nonzero((ndvi >= NDVI_BARE_MAX) & (ndvi < NDVI_LOW_VEG_MAX))
    dense_veg = np.count_nonzero(ndvi >= NDVI_LOW_VEG_MAX)

    return {
        "pct_open_water": round(float(100 * water / total), 1),
        "pct_bare_or_degraded_soil": round(float(100 * bare / total), 1),
        "pct_low_vegetation": round(float(100 * low_veg / total), 1),
        "pct_dense_vegetation": round(float(100 * dense_veg / total), 1),
        "avg_ndvi": round(float(np.mean(ndvi)), 3),
        "ndvi_min": round(float(np.min(ndvi)), 3),
        "ndvi_max": round(float(np.max(ndvi)), 3),
        "valid_pixel_count": int(total),
    }


def get_imagery_summary_for_boundary(
    boundary_coordinates: list,
    max_cloud_cover: float = MAX_CLOUD_COVER_PCT,
    lookback_days: int = LOOKBACK_DAYS,
) -> Optional[dict]:
    """
    Given a property boundary (list of (longitude, latitude) points, same
    format as elevation_data.get_elevation_grid and
    hydrology_data.get_water_features_for_boundary), finds the most recent
    Sentinel-2 L2A scene with cloud cover under max_cloud_cover percent
    within the last lookback_days days, clips it to the boundary, and
    returns a coarse NDVI-based vegetation vigor summary (not a land-cover
    TYPE classification — see module docstring):

        {
            'scene_date': '2026-05-14',
            'days_since_scene': 63,
            'cloud_cover_pct': 4.2,
            'pct_open_water': 2.0,
            'pct_bare_or_degraded_soil': 12.3,
            'pct_low_vegetation': 45.6,
            'pct_dense_vegetation': 40.1,
            'avg_ndvi': 0.35,
            'ndvi_min': -0.12,
            'ndvi_max': 0.82,
            'valid_pixel_count': 10234,
        }

    Returns None if no scene meeting the cloud-cover threshold was found
    intersecting this boundary within the lookback window. That's a genuine
    "no usable recent imagery" outcome (persistent cloud cover, a data gap
    for the area/season) rather than a failed request, so it's surfaced as
    an empty result instead of an exception — callers should treat it the
    same way climate_data/hydrology_data callers treat an empty result:
    skip this section of the report rather than fail the whole pipeline.
    """
    polygon = _boundary_to_polygon(boundary_coordinates)

    items = _search_scenes(polygon, max_cloud_cover, lookback_days)

    if not items:
        return None

    scene = items[0]  # already sorted most-recent-first

    red_href = scene.assets[RED_BAND].href
    nir_href = scene.assets[NIR_BAND].href

    red = _retry(lambda timeout: _read_clipped_band(red_href, polygon, timeout))
    nir = _retry(lambda timeout: _read_clipped_band(nir_href, polygon, timeout))

    # Sentinel-2 L2A uses 0 as nodata for both surface-reflectance bands, so
    # a pixel is only "valid" if both bands have real data there. clip's
    # crop=True already limited us to the boundary's bounding box in raster
    # space; this further restricts to pixels with actual reflectance values.
    valid = (red > 0) & (nir > 0)

    if not np.any(valid):
        return None

    red_valid = red[valid]
    nir_valid = nir[valid]

    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir_valid - red_valid) / (nir_valid + red_valid)

    scene_datetime = scene.properties["datetime"]
    scene_date = scene_datetime[:10]
    days_since_scene = (date.today() - date.fromisoformat(scene_date)).days

    summary = {
        "scene_date": scene_date,
        "days_since_scene": days_since_scene,
        "cloud_cover_pct": round(scene.properties.get("eo:cloud_cover", 0.0), 1),
    }
    summary.update(_classify_ndvi(ndvi))

    return summary


def summarize_imagery(summary: Optional[dict]) -> str:
    """
    Plain-language summary of the imagery-derived land-cover breakdown —
    same purpose as soil_data.summarize_soil_report and
    climate_data.summarize_climate: a quick human-readable check before this
    feeds the Claude-generated narrative report.
    """
    if not summary:
        return (
            "No recent low-cloud Sentinel-2 imagery was available for this "
            "property within the lookback window."
        )

    return (
        f"Scene date: {summary['scene_date']} ({summary['days_since_scene']} days ago), "
        f"cloud cover: {summary['cloud_cover_pct']}%\n"
        f"Vegetation vigor breakdown (see NLCD for cover type):\n"
        f"  Bare/degraded soil: {summary['pct_bare_or_degraded_soil']}%\n"
        f"  Low vigor vegetation: {summary['pct_low_vegetation']}%\n"
        f"  High vigor vegetation: {summary['pct_dense_vegetation']}%\n"
        f"  Open water: {summary['pct_open_water']}%\n"
        f"Average NDVI: {summary['avg_ndvi']} (range: {summary['ndvi_min']} to {summary['ndvi_max']})"
    )


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

    print("Fetching Sentinel-2 imagery summary for property boundary...\n")

    try:
        summary = get_imagery_summary_for_boundary(property_boundary)
        print(summarize_imagery(summary))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach Planetary "
            "Computer's STAC API and blob storage — not a fully sandboxed "
            "environment."
        )
