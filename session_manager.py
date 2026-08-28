"""
session_manager.py

Session creation, and the one read path that ties the Design Document to
the session cache.

Two entry points, and deliberately no more:

    create_session(boundary, store)          -> document
    get_session_context(session_id, store)   -> SessionContext

WHAT CREATION IS. Validate the boundary; build a document; fetch Layer 1
EXACTLY ONCE; run the terrain warm-up; persist; cache; return the
document. The ORDER is the contract, not an implementation detail --
persistence is LAST among the fallible steps, so a run that fails
anywhere before it leaves nothing behind at all.

THE HARD-FAIL CONTRACT, INHERITED. parcel_data.fetch_parcel_data() raises
on any failure among its mandatory layers rather than returning a partial
Layer 1, because an incomplete raw layer means nothing downstream should
run against it (see that module's HARD-FAIL CONTRACT). This module keeps
that posture whole: a failed fetch creates NO session. No document is
persisted, no cache entry is written, and no "degraded" session is
handed back for the frontend to discover the hard way. The same is true
of a warm-up failure, by the same ordering -- a session whose eligibility
mask could not be computed is not a session someone can design in.

WHAT THIS MODULE IS NOT. No HTTP, no Flask, no routes -- an HTTP layer
calls these two functions, they know nothing about it. No Step Registry,
no generate/commit/reopen orchestration, no GeoJSON translation in either
direction, no job handling. Those are later branches, and every one of
them builds ON this rather than inside it.

BOUNDARY VALIDATION -- AND WHAT "CLOSED RING" MEANS HERE. Rejection is
early and loud: a bad boundary must fail before a single network byte is
spent, and it must fail with a message that says which rule it broke.

The one check that needs explaining is closure. This codebase's
convention is IMPLICIT closure -- soil_data.coordinates_to_wkt_polygon()
closes an open ring itself ("If the input isn't already closed, this
closes it automatically"), shapely's Polygon() does the same, and the
real drawn property boundary in generate_full_report.py is itself
implicitly closed: its last vertex sits ~0.9 m from its first, not on it.
Demanding a duplicated closing vertex would reject this project's own
real input, so both forms are accepted here.

What that leaves is the check with actual weight: the ring must CLOSE
INTO GROUND. A vertex list that closes into a line -- collinear points, a
half-drawn boundary, a polyline the user never finished -- encloses no
area, and it is that degenerate case, not a missing duplicate vertex,
that this module rejects as an open ring. The four gates are therefore:
a real ring (>= 3 distinct vertices that enclose area), coordinates that
are finite and on Earth, a SIMPLE ring (no self-intersection), and a
sane area.
"""

import math
from typing import Optional

from rasterio.warp import transform as warp_transform
from shapely.geometry import MultiPoint, Polygon
from shapely.validation import explain_validity

import session_cache
from dem_data import _utm_epsg_for_lonlat
from design_document import create_document
from raster_grid import SQUARE_METERS_PER_ACRE

# --- boundary limits -------------------------------------------------

# The fewest vertices that can enclose area. Below this there is no ring,
# only a point or a segment.
MIN_BOUNDARY_VERTICES = 3

# Under a quarter acre a 5 m DEM (dem_data.DEFAULT_RESOLUTION_METERS)
# puts roughly 40 cells inside the boundary. Flow routing, valley
# delineation and keypoint detection have nothing to work with at that
# size, and there is no keyline design to do on it either -- so this is a
# real floor, not a defensive one.
MIN_BOUNDARY_ACRES = 0.25

# An ABSURDITY ceiling, not a quality one, and the distinction matters.
# The quality threshold is lower and softer: dem_data.MAX_GRID_DIMENSION
# (300) caps the fetched grid, so past roughly 1300 m across -- about 420
# acres -- the DEM stops resolving at its intended 5 m and coarsens
# instead. That degrades honestly and is documented where it happens; it
# is not a reason to refuse a 600-acre farm. Past 5000 acres (~20 km²,
# coarser than 20 m/cell) a "property boundary" is a watershed, and the
# SSURGO map units the soil gates read are not meaningful at that scale.
# That is what this rejects. A future branch may want to WARN between the
# two numbers; deliberately not done here.
MAX_BOUNDARY_ACRES = 5000.0


class BoundaryValidationError(ValueError):
    """
    The submitted boundary is not one a session can be created on.
    Raised before any fetch -- see this module's docstring.
    """


def _boundary_points(boundary) -> list:
    if boundary is None:
        raise BoundaryValidationError("boundary is required")
    try:
        points = list(boundary)
    except TypeError:
        raise BoundaryValidationError(
            f"boundary must be a sequence of (lon, lat) pairs, got "
            f"{type(boundary).__name__}"
        ) from None

    cleaned = []
    for index, point in enumerate(points):
        try:
            lon, lat = point
        except (TypeError, ValueError):
            raise BoundaryValidationError(
                f"boundary vertex {index} is not a (lon, lat) pair: {point!r}"
            ) from None
        try:
            lon, lat = float(lon), float(lat)
        except (TypeError, ValueError):
            raise BoundaryValidationError(
                f"boundary vertex {index} has non-numeric coordinates: {point!r}"
            ) from None
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise BoundaryValidationError(
                f"boundary vertex {index} is not finite: {point!r}"
            )
        if not -180.0 <= lon <= 180.0:
            raise BoundaryValidationError(
                f"boundary vertex {index} longitude {lon} is outside [-180, 180]"
            )
        if not -90.0 <= lat <= 90.0:
            raise BoundaryValidationError(
                f"boundary vertex {index} latitude {lat} is outside [-90, 90]"
            )
        cleaned.append((lon, lat))
    return cleaned


# How much ground a vertex set must span, relative to its own extent, to
# be a ring at all: the area of its CONVEX HULL over its bounding-box
# diagonal squared. Scale-free, so one threshold serves a quarter-acre
# garden and a 500-acre farm alike.
#
# WHY THE HULL AND NOT THE RING'S OWN AREA. They differ on exactly the
# case that has to be told apart. A symmetric figure-eight's two lobes
# cancel, so its signed ring area is ZERO -- indistinguishable from a
# straight line by that measure, and it would be rejected with the wrong
# reason. Its hull is the full quadrilateral, so the hull test passes it
# straight through to the self-intersection gate below, which is the gate
# that actually describes what is wrong with it. The hull is zero if and
# only if every vertex lies on one line, which is precisely "this closes
# into a line, not around ground".
#
# WHY THE TEST IS IN LON/LAT AND NOT IN UTM. A ring whose vertices are
# collinear in lon/lat is NOT collinear once projected: a straight line
# in geographic coordinates bows in UTM, so a 2.7 km "line" comes back
# enclosing ~40 m² of real, projected area. That is geodesy, not float
# noise, and no meters-based area test can tell it from a genuinely
# skinny parcel. Measured here, against the coordinates the user actually
# drew: an exactly-degenerate ring scores ~0, the real 13-acre property
# scores ~0.28, and even a 5 m x 800 m strip scores ~9e-3 -- so 1e-9 sits
# orders of magnitude clear of both sides.
MIN_RING_AREA_RATIO = 1e-9


def _hull_area_ratio(points: list) -> float:
    """Convex-hull area of the vertex set over its bbox diagonal squared."""
    lons = [lon for lon, _ in points]
    lats = [lat for _, lat in points]
    diagonal_squared = (max(lons) - min(lons)) ** 2 + (max(lats) - min(lats)) ** 2
    if diagonal_squared == 0.0:
        return 0.0
    return MultiPoint(points).convex_hull.area / diagonal_squared


def _boundary_polygon_utm(points: list) -> Polygon:
    """
    The ring in its own UTM zone, in meters -- the only CRS an area test
    means anything in. The zone comes from the ring's own mean position
    via dem_data._utm_epsg_for_lonlat(), the same helper the DEM fetch
    uses to pick a CRS, so validation and the later fetch agree about
    where on Earth this parcel is.
    """
    mean_lon = sum(lon for lon, _ in points) / len(points)
    mean_lat = sum(lat for _, lat in points) / len(points)
    crs = f"EPSG:{_utm_epsg_for_lonlat(mean_lon, mean_lat)}"
    xs, ys = warp_transform(
        "EPSG:4326", crs, [lon for lon, _ in points], [lat for _, lat in points]
    )
    return Polygon(zip(xs, ys))


def validate_boundary(boundary) -> list:
    """
    Raises BoundaryValidationError on anything a session cannot be
    created on; returns the cleaned list of (lon, lat) float pairs
    otherwise. Never repairs, never coerces a broken ring into a working
    one -- same posture design_document.py and parcel_data.py take.
    """
    points = _boundary_points(boundary)

    # An explicitly duplicated closing vertex is accepted and does not
    # count toward the vertex minimum -- see the module docstring on
    # closure. Distinct positions are what has to be counted.
    distinct = list(dict.fromkeys(points))
    if len(distinct) < MIN_BOUNDARY_VERTICES:
        raise BoundaryValidationError(
            f"boundary has {len(distinct)} distinct vertex/vertices; a ring "
            f"needs at least {MIN_BOUNDARY_VERTICES}"
        )

    # The open-ring gate, in the coordinates the user drew -- see
    # MIN_RING_AREA_RATIO for why this cannot be a meters-based test.
    if _hull_area_ratio(distinct) < MIN_RING_AREA_RATIO:
        raise BoundaryValidationError(
            "boundary encloses no area -- its vertices are collinear, so the "
            "ring closes into a line rather than around ground. This is what "
            "a half-drawn boundary looks like."
        )

    polygon = _boundary_polygon_utm(distinct)

    if not polygon.is_valid:
        raise BoundaryValidationError(
            f"boundary ring is not simple ({explain_validity(polygon)}). A "
            "parcel boundary must be a single non-self-intersecting ring."
        )

    acres = polygon.area / SQUARE_METERS_PER_ACRE
    if acres < MIN_BOUNDARY_ACRES:
        raise BoundaryValidationError(
            f"boundary encloses {acres:.4f} acres; the minimum is "
            f"{MIN_BOUNDARY_ACRES} acres (below that a 5 m DEM has too few "
            f"cells inside the parcel to route flow over)"
        )
    if acres > MAX_BOUNDARY_ACRES:
        raise BoundaryValidationError(
            f"boundary encloses {acres:.1f} acres; the maximum is "
            f"{MAX_BOUNDARY_ACRES} acres (past that this is a watershed, not "
            f"a property -- see MAX_BOUNDARY_ACRES)"
        )

    return points


# --- the two entry points --------------------------------------------


def create_session(
    boundary,
    store,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> dict:
    """
    Create a session on `boundary` and return its Design Document.

    Steps, in the order that IS the contract:

      1. Validate the boundary. Rejects before any network call.
      2. create_document(boundary) -- the authoritative record.
      3. Fetch Layer 1 EXACTLY ONCE, through the fetch cache (so a second
         session on the same land fetches ZERO times, not once more).
      4. Terrain warm-up: valleys, keypoints, exclusion zones.
      5. Persist the document, then populate the session cache.
      6. Return the document.

    A failure in 1, 3 or 4 creates NO session: nothing is persisted and
    nothing is cached, because persistence comes after all of them. See
    the module docstring's HARD-FAIL CONTRACT section.

    The document is returned, not the context -- the document is what a
    caller can hold, serialize and come back with. The context is
    reachable by session_id through get_session_context(), which will
    rebuild it if it has since been evicted.
    """
    # `is None`, never `or`: both cache classes define __len__, so an
    # EMPTY caller-supplied cache is falsy and `or` would silently swap
    # in the process-wide default on exactly the first call of a fresh
    # cache's life.
    if fetch_cache is None:
        fetch_cache = session_cache.DEFAULT_FETCH_CACHE
    if cache is None:
        cache = session_cache.DEFAULT_SESSION_CACHE

    points = validate_boundary(boundary)
    document = create_document(points)

    # Steps 3 and 4 together. Anything raised here -- a hard-failed Layer
    # 1 fetch, a warm-up that could not compute the eligibility mask --
    # propagates out UNCAUGHT and uncached, with the document still only
    # a local value that no store has ever seen.
    context = session_cache.build_session_context(
        document["session_id"], document["boundary"], fetch_cache
    )

    store.put(document)
    cache.put(context)
    return document


def get_session_context(
    session_id: str,
    store,
    fetch_cache: Optional[session_cache.FetchCache] = None,
    cache: Optional[session_cache.SessionCache] = None,
) -> session_cache.SessionContext:
    """
    This session's live context. A cache hit returns it; a miss rebuilds
    it from the persisted document and repopulates the cache, which is
    slower and otherwise indistinguishable -- that is the whole point of
    the tier-2 cache being non-authoritative.

    A miss is NOT an error. An unknown session_id is: the store raises
    SessionNotFoundError, and that propagates, because the document is
    the authority and its absence means the session genuinely does not
    exist.
    """
    if fetch_cache is None:
        fetch_cache = session_cache.DEFAULT_FETCH_CACHE
    if cache is None:
        cache = session_cache.DEFAULT_SESSION_CACHE

    context = cache.get(session_id)
    if context is not None:
        return context

    document = store.get(session_id)
    context = session_cache.rebuild_session_context(document, fetch_cache)
    cache.put(context)
    return context
