"""
naip_imagery.py

THE LAYOUT MAP'S PHOTOGRAPHY: USDA NAIP orthoimagery for the ground the
layout map shows, fetched once per boundary as a report layer, warped at
render time into the map's own UTM extent.

    get_naip_for_boundary(boundary)               -> raw: the tiles as read
    parse_naip(raw)                               -> the block ReportData carries
    layout_extent_utm(boundary)                   -> (crs, extent) the map draws
    underlay(block, crs, extent, meters_per_pt)   -> {href, extent_utm, ...}
    format_acquired(block)                        -> "21 June 2022"

WHY NAIP, AND NOT THE TILES THE OLD LAYOUT MAP DREW. The retired matplotlib
map composited USGS Imagery Only tiles, and that service publishes no
acquisition date -- its only date is the cache's refresh. A map someone
carries into the field three years from now has to say what year its
photograph is, so the source has to know. Every NAIP item on Microsoft's
Planetary Computer carries its acquisition datetime, and that date is
printed beneath the map (design_section).

TERMS -- VERIFY BEFORE BUILD (site-data-report-proposal.md section 10).
NAIP is USDA Farm Service Agency imagery, generally held to be public
domain with commercial use unrestricted; Planetary Computer lists the
collection's licence as "proprietary" with a link to FSA's policies page.
Both the FSA terms and Planetary Computer's hosting terms are to be
confirmed before v1 ships.

WHAT IS FETCHED: the latest NAIP year that covers the parcel, every item
of that year touching the map's extent, each read as a window in its own
CRS at its native resolution (0.6 m since 2018 in Pennsylvania). Nothing
is warped at fetch time: the fetch is raw data, cached per boundary like
every report layer, and the warp into the map's UTM grid is a pure
computation done when the page is drawn.

THE EXTENT is the layout map's own: report_map.LAYOUT_FRAME's whole
frame, drawn unfitted, over the parcel projected into the UTM zone the pipeline
computes in (dem_data._utm_epsg_for_lonlat at the centroid), plus
FETCH_MARGIN_M. The fetch and the map therefore ask for the same ground.

DEGRADABLE (report_data.REPORT_FETCH_LAYERS). Without imagery the map
draws on white -- a legitimate render -- and the page says the imagery is
unavailable where the acquisition date would have been.

THE UNDERLAY IS A JPEG in an SVG <image>. WeasyPrint embeds it as one
image object and keeps every path and glyph above it vector (branch 13
step 0: an 82 KB JPEG against 760 KB as PNG for the same pixels). It is
warped at the imagery's native ground resolution unless that would exceed
MAX_UNDERLAY_DPI on the page, where it is resampled down: finer than the
photograph adds bytes and no detail, finer than print adds bytes and
nothing a printer can show. Ground the imagery does not cover is left the
page colour, never black.
"""

import base64
import io
from datetime import date
from typing import Optional

import numpy as np
from PIL import Image
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds, transform_geom
from shapely.geometry import Polygon, box, mapping, shape

import report_map
from dem_data import _utm_epsg_for_lonlat

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "naip"
SOURCE_LABEL = "USDA NAIP orthoimagery"

FETCH_MARGIN_M = 20.0
MAX_UNDERLAY_DPI = 200.0
JPEG_QUALITY = 82
RGB_BANDS = (1, 2, 3)

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


class NaipIncompleteError(RuntimeError):
    """NAIP answered without imagery for this parcel."""


# ======================================================================
# The extent
# ======================================================================


def boundary_utm(boundary) -> tuple:
    """(crs, polygon in metres): the boundary in the UTM zone the pipeline
    computes in."""
    ring = Polygon([(float(lon), float(lat)) for lon, lat in boundary])
    centroid = ring.centroid
    crs = f"EPSG:{_utm_epsg_for_lonlat(centroid.x, centroid.y)}"
    return crs, shape(transform_geom("EPSG:4326", crs, mapping(ring)))


def layout_extent_utm(boundary) -> tuple:
    """(crs, (minx, miny, maxx, maxy)): the ground the layout map's whole
    frame covers, in that zone -- the photograph fills the frame, the
    scale bar's band included."""
    crs, polygon = boundary_utm(boundary)
    return crs, report_map.frame_extent_utm(polygon, report_map.LAYOUT_FRAME, fit=False)


# ======================================================================
# The fetch
# ======================================================================


def get_naip_for_boundary(boundary, client=None) -> dict:
    """
    The raw answer: {"tiles": [{id, acquired (ISO date), year, gsd, crs,
    transform (6 floats), rgb (uint8, 3 x H x W)}], "crs", "extent_utm"}.
    Raises NaipIncompleteError when no NAIP item covers the extent, and
    lets the network's own exceptions through for the caller to degrade.
    """
    import planetary_computer
    import rasterio
    from pystac_client import Client

    crs, extent = layout_extent_utm(boundary)
    fetch = box(*extent).buffer(FETCH_MARGIN_M, join_style=2)
    area = shape(transform_geom(crs, "EPSG:4326", mapping(fetch)))
    client = client or Client.open(STAC_API_URL, modifier=planetary_computer.sign_inplace)
    items = list(client.search(collections=[COLLECTION], intersects=mapping(area)).items())
    if not items:
        raise NaipIncompleteError("no NAIP imagery covers this parcel")
    latest = max(int(item.properties.get("naip:year") or item.datetime.year) for item in items)
    chosen = sorted((i for i in items if int(i.properties.get("naip:year") or i.datetime.year) == latest), key=lambda i: i.id)
    tiles = []
    for item in chosen:
        with rasterio.open(item.assets["image"].href) as src:
            window_bounds = transform_bounds(crs, src.crs, *fetch.bounds, densify_pts=21)
            window = src.window(*window_bounds).round_offsets().round_lengths()
            window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
            rgb = src.read(list(RGB_BANDS), window=window)
            transform = src.window_transform(window)
            tiles.append({
                "id": item.id,
                "acquired": item.datetime.date().isoformat(),
                "year": latest,
                "gsd": float(item.properties.get("gsd") or abs(transform.a)),
                "crs": str(src.crs),
                "transform": list(transform)[:6],
                "rgb": rgb,
            })
    return {"tiles": tiles, "crs": crs, "extent_utm": list(extent)}


def parse_naip(raw: dict) -> dict:
    """The block ReportData carries: the tiles, and the acquisition dates
    and year the page prints. Raises NaipIncompleteError for an answer
    with no pixels."""
    tiles = [t for t in raw.get("tiles") or [] if t["rgb"].size and t["rgb"].any()]
    if not tiles:
        raise NaipIncompleteError("the NAIP answer carries no pixels for this parcel")
    acquired = sorted({t["acquired"] for t in tiles})
    return {
        "source": SOURCE_LABEL,
        "tiles": tiles,
        "acquired": acquired,
        "years": sorted({date.fromisoformat(a).year for a in acquired}),
        "gsd": min(t["gsd"] for t in tiles),
        "crs": raw.get("crs"),
        "extent_utm": raw.get("extent_utm"),
    }


def format_acquired(block: dict) -> str:
    """'21 June 2022', or '14 June and 2 July 2022' / '… 2021 and … 2022'
    when the extent spans items flown on different days."""
    dates = [date.fromisoformat(a) for a in block["acquired"]]
    words = [f"{d.day} {_MONTHS[d.month - 1]}" + ("" if len({x.year for x in dates}) == 1 else f" {d.year}") for d in dates]
    tail = f" {dates[0].year}" if len({x.year for x in dates}) == 1 else ""
    if len(words) == 1:
        return words[0] + tail
    return ", ".join(words[:-1]) + " and " + words[-1] + tail


# ======================================================================
# The underlay
# ======================================================================


def underlay(block: dict, crs: str, extent, meters_per_pt: float, page_rgb=(255, 255, 255)) -> dict:
    """
    The imagery warped onto a north-up grid over `extent` in `crs` -- the
    map's own projection -- as {'href': data URI, 'extent_utm', 'pixels':
    (w, h), 'bytes', 'meters_per_pixel', 'coverage'}. Pixel size is the
    imagery's native ground resolution, coarsened only if that would print
    finer than MAX_UNDERLAY_DPI at the map's scale.
    """
    minx, miny, maxx, maxy = extent
    print_limit = meters_per_pt * 72.0 / MAX_UNDERLAY_DPI
    pixel = max(block["gsd"], print_limit)
    width = max(1, int(round((maxx - minx) / pixel)))
    height = max(1, int(round((maxy - miny) / pixel)))
    target = from_bounds(minx, miny, maxx, maxy, width, height)
    out = np.zeros((3, height, width), dtype=np.uint8)
    covered = np.zeros((height, width), dtype=bool)
    for tile in block["tiles"]:
        source_transform = Affine(*tile["transform"])
        band_out = np.zeros((3, height, width), dtype=np.uint8)
        for band in range(3):
            reproject(tile["rgb"][band], band_out[band], src_transform=source_transform, src_crs=tile["crs"],
                      dst_transform=target, dst_crs=crs, resampling=Resampling.bilinear)
        mask = np.zeros((height, width), dtype=np.uint8)
        reproject(np.ones(tile["rgb"].shape[1:], dtype=np.uint8), mask, src_transform=source_transform, src_crs=tile["crs"],
                  dst_transform=target, dst_crs=crs, resampling=Resampling.nearest)
        fresh = (mask > 0) & ~covered
        out[:, fresh] = band_out[:, fresh]
        covered |= fresh
    for band, value in enumerate(page_rgb):
        out[band][~covered] = value
    buffer = io.BytesIO()
    Image.fromarray(np.moveaxis(out, 0, -1)).save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
    data = buffer.getvalue()
    return {
        "href": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
        "extent_utm": (minx, miny, maxx, maxy),
        "pixels": (width, height),
        "bytes": len(data),
        "meters_per_pixel": pixel,
        "coverage": float(covered.mean()),
    }


def page_rgb(tokens: dict) -> tuple:
    """The page token as an RGB triple, for ground the imagery misses."""
    value = tokens["page"].lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def tiles_to_fixture(raw: dict) -> tuple:
    """(metadata, [JPEG bytes per tile]) -- how naip_reference_fixture
    stores a fetch: the pixels as JPEG, everything else as JSON."""
    meta = {k: v for k, v in raw.items() if k != "tiles"}
    meta["tiles"] = []
    images = []
    for tile in raw["tiles"]:
        buffer = io.BytesIO()
        Image.fromarray(np.moveaxis(tile["rgb"], 0, -1)).save(buffer, "JPEG", quality=90)
        images.append(buffer.getvalue())
        meta["tiles"].append({k: v for k, v in tile.items() if k != "rgb"})
    return meta, images


def tiles_from_fixture(meta: dict, images: list) -> dict:
    raw = {k: v for k, v in meta.items() if k != "tiles"}
    raw["tiles"] = []
    for tile, data in zip(meta["tiles"], images):
        rgb = np.moveaxis(np.asarray(Image.open(io.BytesIO(data)).convert("RGB")), -1, 0).copy()
        raw["tiles"].append({**tile, "rgb": rgb})
    return raw
