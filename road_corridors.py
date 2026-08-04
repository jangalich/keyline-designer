"""
road_corridors.py

Computes a single suggested road corridor from the DEM, for properties
that lack existing farm road/access data — this REPLACES having Claude
infer a plausible-sounding corridor in prose during report generation
(report_generator.py's Farm Roads step used to do exactly that, with an
explicit "no dedicated access data" disclaimer). Same pattern as
water_candidate_zones.py and solar_suitability.py: the backend computes
real geometry, Claude narrates from the result.

RIDGE-LINE ROUTING (this module's current design, replacing an earlier
fan-based multi-destination-per-anchor approach — see git history,
`git log --all -- road_corridors.py`, for that one): a road follows a
genuine RIDGE LINE on the property, not an arbitrary farthest-point or
compass-fan destination — a ridge is naturally well-drained, gently
graded (relative to its surroundings), and a sensible real-world place to
put a farm road. Ridge lines are identified by reusing
valley_delineation.py's own D8 flow-routing pipeline (fill -> flow
direction -> flow accumulation) against an INVERTED DEM — a standard GIS
technique: a ridge in the real terrain is a valley in its negation (see
_identify_ridge_cell_mask()). This recovers a pre-fan-routing version of
this module's own ridge logic from git history, restructured around a
boolean per-cell mask + raster_grid.connected_components() (one "network"
per originally-contiguous ridge landform) rather than
valley_delineation.delineate_valleys()'s own ordered head-to-outlet
branch chains — this module only ever needs "is this cell on a ridge, and
which ridge network is it part of," not individually-traced branches.

Pipeline (find_road_routes()):

  1. IDENTIFY: _identify_ridge_cell_mask(dem) — boolean, per-cell, over
     the whole DEM.
  2. GROUP: connected_components() on that mask — one candidate "network"
     per originally-contiguous ridge landform.
  3. PRUNE: _prune_ridge_networks() removes every cell inside
     excluded_mask (the HARD water-zone + boundary exclusions below —
     production is NOT part of this mask anymore, see constraint stack
     below) from each network, then keeps only the single LARGEST
     surviving fragment per original network — an exclusion can sever one
     ridge into several disconnected pieces; only the biggest piece per
     original ridge stays a candidate (a severed sliver isn't a
     meaningfully separate ridge worth scoring on its own). A ridge may
     freely walk through a production zone now; that's what the 3rd
     scoring term below is for. A surviving fragment shorter than
     RIDGE_MIN_FRAGMENT_CELLS is dropped outright here too, whether it
     started that short (a genuinely tiny original network) or got cut
     down to it by pruning — see that constant's own comment.
  4. SCORE: _score_ridge_candidates() ranks the surviving fragments on
     three normalized, equally-weighted-by-default terms — how gentle the
     fragment's own average slope is, how long it is, and how few of its
     own cells fall inside a production zone — see that function's own
     docstring for the exact formula and RIDGE_SLOPE_WEIGHT /
     RIDGE_LENGTH_WEIGHT / RIDGE_PRODUCTION_WEIGHT below. There is no
     separate proximity-to-anchor term anymore — road_cost_path.
     least_cost_path()'s own total_cost already governs which reachable
     entry point each candidate connects through; scoring the resulting
     cost again on top of that was redundant.
  5. SELECT: the single highest weighted-score candidate wins. The final
     route is that candidate's own CONNECTOR path (anchor -> whichever
     ridge cell the connector actually reaches) concatenated with the
     winning fragment itself (that entry cell -> the fragment's own far
     end) — one continuous line, not two separate pieces.

This produces exactly ONE road corridor per property — there is no ranked
list of alternates the way an earlier fan-based version of this module
returned up to three. Per product decision, this app targets small farms
only, where one well-suited road corridor is sufficient.

Constraint stack, current design — the HARD/SOFT split is no longer a
single property-wide line; it now differs between the ridge fragment
itself and the anchor-to-ridge CONNECTOR:
  - HARD exclusions on the RIDGE (stops the ridge walk outright —
    _prune_ridge_networks() strips these cells before scoring ever
    starts, and _order_fragment_from_entry()'s own BFS can never route
    through them either, since they're gone from the fragment entirely):
      - outside the single SELECTED water-system candidate zone
        (water_suitability.fetch_and_select_optimal_water_zone() — the
        one rank-1 zone this property's own water-suitability scoring
        actually picked, not every unscored candidate zone
        water_candidate_zones.py generates), buffered wider — routing on
        the uphill side of a pond/dam, not across its face or catchment
        inlet
      - the parcel boundary itself (off-parcel cells)
  - HARD exclusion on the CONNECTOR only (baked into cost_raster as
    np.inf, via build_cost_raster()'s own excluded_mask argument):
      - production zone(s) (production_area_ceiling.py's own optimized/
        final geometry) — the anchor-to-ridge connector may never cross
        or sit on production land, same as before.
  - SOFT, cell-based SCORING term on the RIDGE fragment itself (one of
    _score_ridge_candidates()'s three terms — see RIDGE_PRODUCTION_WEIGHT
    below):
      - production zone(s) — a ridge fragment MAY now walk across
        production land (it's no longer pruned out), but a fragment with
        more of its own cells inside a production zone scores worse,
        exactly the same normalized "more is worse, relative to the
        worst candidate in the set" shape length_score already uses.

A surviving ridge fragment (after the HARD exclusions above are stripped
out and only the largest remaining piece per original network is kept —
see _prune_ridge_networks()) that's still shorter than
RIDGE_MIN_FRAGMENT_CELLS is dropped before scoring ever runs at all — a
real live bug: even with production no longer able to shrink a ridge,
6-7 cell fragments (a genuinely tiny original network, or a larger one
severed down to a remnant by the water-zone/boundary exclusions above)
could still surface as the winning "road corridor." Not a meaningful
road-worthy landform on its own.
  - SOFT cost preference (connector only):
      - floodplain/hydric ground, sourced from real NHD stream/water-body
        buffers + SSURGO hydric soil polygons (NOT inferred from DEM
        elevation alone) — falls back to a buffer around delineated
        valley lines only if neither NHD nor SSURGO data is reachable,
        and flags that fallback explicitly in confidence_notes. The
        connector may still cross it when the alternative is a costly
        enough detour (road_cost_path.FLOODPLAIN_CROSSING_COST_PENALTY).
      - grade — UNBOUNDED, via road_cost_path.build_cost_raster()'s own
        quadratic grade penalty; there is no pinned maximum-grade ceiling
        in this design at all. The connector can still climb an
        arbitrarily steep slope if the alternative is expensive enough,
        it just costs more per cell the steeper it gets.

Erosion-prone soil (SSURGO K-factor) is deliberately NOT part of this
module's constraint stack at all, hard or soft — this pipeline's own
KSOP (Keyline Scale of Permanence) ordering puts Soil at step 8, well
below Farm Roads at step 4; scoring a step-4 feature against step-8 data
inverted that ordering, so the erosion-avoidance preference this module
used to carry has been removed outright, not just softened further.

No named-road anchoring/connector search happens in this flow at all
(an earlier version of this module snapped the near-boundary end of a
generated corridor to the nearest real, named mapped road) — routing now
starts FROM a given real anchor point and ends at the winning ridge
fragment's own far end, full stop; there's no separate "reach the
boundary" step to connect afterward. Severing a route at a structure site
(once one has been sited) is out of scope here too — a future follow-up
once real routes exist to sever.

road_cost_path.k_shortest_paths() stays defined and exported (used
nowhere in this module) for a later prompt to layer route ALTERNATES on
top of this design.

find_road_routes() is the pure geometric/scoring core — see
water_candidate_zones.py's and solar_suitability.py's docstrings for why
this split matters (independently testable against a synthetic DEM,
without any of the several real network fetches this feature depends on).
"""

import math
from collections import deque
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union
from shapely.prepared import prep
import requests

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from hydrology_data import get_water_features_for_boundary
from production_area_ceiling import identify_optimized_production_areas
from raster_grid import D8_OFFSETS, cell_area_acres, cell_union_footprint, connected_components, pixel_center_xy
from road_cost_path import build_cost_raster, least_cost_path, path_cells_to_points_xyz
from soil_data import (
    coordinates_to_wkt_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    hydric_disqualifying_mukeys,
)
from terrain_metrics import compute_slope_and_aspect
from valley_delineation import delineate_valleys, get_flow_accumulation_for_dem
from water_suitability import fetch_and_select_optimal_water_zone

METERS_PER_FOOT = 0.3048

# Historical reference figure only now, NOT an enforced hard ceiling
# anymore (see module docstring -- grade is an unbounded soft cost penalty
# under the current design, road_cost_path.build_cost_raster()'s own job).
# Kept purely as the documented "widely-cited practical ceiling" context
# STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT's own reasoning below is
# expressed relative to. 10% is a widely-cited practical ceiling for
# sustained grade on a low-volume gravel farm/ranch road in USDA NRCS and
# US Forest Service low-standard-road design guidance; 15% reflects that
# short pitches up to ~12-15% are tolerated in the wild on low-volume
# farm/ranch roads. Not a single pinned regulatory document — stated
# plainly since this environment has no live network access to verify a
# specific citation.
MAX_ROAD_GRADE_PCT = 15.0

# Above this average grade, a route is steep enough that confidence_notes
# flags it as needing real engineering consideration (surface material,
# drainage/culverts, water bars) before construction -- not just the
# blanket "topographic suggestion, not a surveyed alignment" caveat every
# route already carries. Set at the OLD 10% ceiling (rather than some
# fraction of the 15% reference figure above): grade at or below what
# used to be the hard cutoff still reads as comfortably within normal
# gravel-road practice; anything above it is in the range this codebase's
# own MAX_ROAD_GRADE_PCT reasoning above already treats as "tolerated but
# needs real engineering attention," not a new/arbitrary line. CONFIGURABLE.
STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT = 10.0

# Buffer beyond a pond/water-system candidate zone's own footprint -- this
# needs to keep a route off a dam face or catchment inlet, not just the
# pond footprint itself. CONFIGURABLE.
POND_ZONE_EXCLUSION_BUFFER_METERS = 25.0

# Buffer applied to NHD streams/water bodies (and, in the elevation-only
# fallback case, to delineated valley lines) as the floodplain/riparian
# cost-penalty region. 30m (~100ft) is a commonly-used minimal riparian
# buffer rule of thumb, not a site-specific regulatory setback.
# CONFIGURABLE.
FLOODPLAIN_STREAM_BUFFER_METERS = 30.0

# How far past the parcel boundary to still care about a fetched NHD
# stream/water-body's own geometry before it gets buffered into the
# floodplain cost-penalty union. REAL BUG, FOUND LIVE (fixed here):
# hydrology_data.py's NHD query (an ArcGIS `query` operation) returns
# each matching feature's FULL, un-clipped geometry for anything that
# merely intersects the query bounding box -- a long stream or a large
# waterbody that just touches that box can come back with geometry
# extending far past the property, which then got buffered (widening it
# further) and unioned into this mask WHOLESALE. Confirmed live: a
# 33.9-ACRE floodplain/hydric union on a 13.23-acre parcel. Same
# root-cause CATEGORY (a fetch returning geometry far beyond the input
# boundary's actual relevant extent, not clipped to it) as the earlier
# soil_data.get_soil_geometries_for_polygon() bug -- that one's already
# fixed at the SSURGO query itself (real STIntersects + STIntersection
# clipping); NHD's ArcGIS `query` endpoint has no equivalent server-side
# clip parameter here, so this clips CLIENT-SIDE instead, against a
# generous context region around the parcel -- comfortably larger than
# both dem_data.py's own ~100m DEM fetch buffer and
# FLOODPLAIN_STREAM_BUFFER_METERS itself, so clipping here can never cut
# off a stream segment that would otherwise matter to on-parcel routing.
# CONFIGURABLE.
FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS = 200.0

# SECOND real bug, found live AFTER the fetch-context clip above landed:
# that clip is applied to each stream/water-body feature's RAW geometry
# BEFORE it gets buffered by FLOODPLAIN_STREAM_BUFFER_METERS -- nothing
# bounded the geometry AFTER buffering, so the buffer stroke itself (and,
# more significantly, however much of the stream's length survived the
# 200m fetch-context clip) could still produce a final piece extending
# well past any distance actually relevant to this parcel. Confirmed live
# and visually (plotted GeoJSON against the real property): an 11.2-acre
# union where only 0.077 acres (0.6% of the parcel) actually overlapped
# it -- the rest was a long buffered band running along Montour Run,
# entirely on the far side of N Montour Rd from the field, nowhere near
# the parcel. This constant bounds the FINAL unioned/buffered result
# (after FLOODPLAIN_STREAM_BUFFER_METERS is applied) to a region tied to
# the parcel itself, independent of how far the original fetched geometry
# or its buffer stroke reached. Deliberately meaningfully SMALLER than
# FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS (200m) -- that larger buffer's
# job is only to avoid losing a stream segment's geometry at fetch time;
# this one's job is to bound what actually counts as "close enough to this
# parcel to matter." Still meaningfully LARGER than
# FLOODPLAIN_STREAM_BUFFER_METERS (30m) alone, so a stream running close
# to (but not quite touching) the parcel -- genuine near-parcel floodplain
# risk on a differently-shaped property -- doesn't get clipped away just
# because its own 30m buffer stroke doesn't quite reach the boundary.
# CONFIGURABLE.
FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS = 75.0

# Drop routes shorter than this -- not a meaningful road. CONFIGURABLE.
MIN_CORRIDOR_LENGTH_METERS = 30.0

# Same contributing-area threshold valley_delineation.py's own
# MIN_STREAM_CONTRIBUTING_AREA_ACRES uses, applied instead to an INVERTED
# DEM (see _identify_ridge_cell_mask()) -- so "contributing area" here
# means "ridge crest prominence," the inverted-terrain analog. Smaller
# than valley_delineation's own default: a road-worthy ridge doesn't need
# to be as major a landform feature as a "primary valley" does.
# CONFIGURABLE.
RIDGE_MIN_AREA_ACRES = 0.5

# A surviving ridge fragment (post-pruning, see _prune_ridge_networks())
# shorter than this many cells is dropped outright before scoring ever
# starts -- REAL BUG, FOUND LIVE: even with production no longer able to
# prune the ridge at all (see module docstring), a 6- or 7-cell fragment
# (a handful of meters at typical DEM resolution) could still surface as
# the winning "road corridor," either because that's genuinely all a tiny
# original ridge network ever was, or because water-zone/boundary pruning
# cut a larger one down to a remnant that small. Neither is a meaningful
# road-worthy landform on its own. 10 cells is a deliberately blunt floor,
# not derived from any per-property length target -- CONFIGURABLE, same
# unvalidated-starting-value caveat as every other threshold here.
RIDGE_MIN_FRAGMENT_CELLS = 10

# Weights for _score_ridge_candidates()'s three-term weighted score
# (slope, length, production-zone crossing -- see that function's own
# docstring for the exact normalized formula each term feeds; there is no
# separate proximity-to-anchor term -- see that function's own docstring
# for why scoring road_cost_path.least_cost_path()'s own total_cost again
# on top of the connector it already produces was redundant). Equal-
# weighted (as close to 1/3 each as three two-decimal numbers summing to
# 1.0 allow) -- CONFIGURABLE, deliberately UNVALIDATED starting values,
# same caveat as every other weight/threshold in this pipeline: pending a
# real diagnostic sweep against actual scored candidates on a real
# property, not tuned against anything yet.
RIDGE_SLOPE_WEIGHT = 0.34
RIDGE_LENGTH_WEIGHT = 0.33
RIDGE_PRODUCTION_WEIGHT = 0.33

ROAD_CORRIDOR_CONFIDENCE_NOTES_TEMPLATE = (
    "This is a TOPOGRAPHIC SUGGESTION only, not a surveyed road alignment — "
    "generated by identifying ridge lines from a DEM-based flow-accumulation "
    "model (see road_corridors.py) and connecting the highest-scoring ridge "
    "to a single given anchor point via a least-cost path (an unbounded grade "
    "penalty plus a floodplain cost surface — see road_cost_path.py), not a "
    "civil engineering design. The single selected water-system candidate "
    "zone is HARD-excluded from the ridge itself — a route cannot be "
    "generated through it at all. Production zone(s) are a SOFT scoring "
    "preference on the ridge itself (see properties.ridge_production_score "
    "and properties.crosses_production_zone) but stay a HARD exclusion on "
    "the anchor-to-ridge connector — the connector may never cross "
    "production land, only the ridge fragment itself may. Floodplain/hydric "
    "ground is a SOFT cost preference on the anchor-to-ridge connector only "
    "— it may still cross it when the alternative is a costly enough detour "
    "(see properties.crosses_floodplain). {floodplain_crossing_note}"
    "{production_crossing_note}{steep_grade_note}{floodplain_fallback_note}"
    "Treat this as a starting point for a site visit and real survey, not a "
    "construction-ready alignment."
)

FLOODPLAIN_CROSSING_NOTE = (
    "This specific route DOES cross floodplain/hydric ground — that's intentional/expected under "
    "this model, not a caveat to apologize for; the least-cost-path routing found crossing it was "
    "still cheaper overall than detouring around it. "
)

PRODUCTION_CROSSING_NOTE = (
    "This specific route's ridge fragment DOES cross a production zone — that's intentional/expected "
    "under this model, not a caveat to apologize for; production crossing is a soft scoring preference "
    "on the ridge now, not a hard exclusion, so the highest-scoring ridge can still cross production "
    "land when its other terms (slope/length) outweigh the penalty. "
)

# Additive caveat (see STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT above) —
# appended only for routes steep enough that routine grading isn't
# enough; the blanket "topographic suggestion" disclaimer above still
# applies to every route regardless of grade.
STEEP_GRADE_ENGINEERING_NOTE = (
    "This route's average grade ({avg_grade_pct}%) is steep enough for an unpaved farm road that "
    "real engineering consideration — surface material, drainage/culverts, water bars — is needed "
    "before construction, not just routine grading: erosion, traction, and washout risk all "
    "increase meaningfully above {threshold_pct:.0f}%, especially under heavy rainfall or "
    "freeze-thaw conditions. "
)


def _build_exclusion_cell_mask(dem: dict, excluded_prepared, boundary_prepared) -> np.ndarray:
    """Per-cell boolean mask (True = excluded), built once by testing
    every valid cell center against the (prepared, unioned) exclusion
    geometry AND against the real parcel boundary — reused once for the
    whole property rather than re-testing point-in-polygon per candidate
    route.

    A cell outside boundary_prepared is excluded here the same way an
    excluded-zone cell is — this is what keeps generated routes from
    being built out of the DEM's buffered area past the drawn boundary
    (dem_data.py fetches ~100m past the parcel on purpose; without this,
    a route could be built entirely from off-parcel cells)."""
    array = dem["array"]
    rows, cols = array.shape
    excluded = np.zeros((rows, cols), dtype=bool)

    valid = ~np.isnan(array)
    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            point = Point(pixel_center_xy(dem, r, c))
            if not boundary_prepared.contains(point):
                excluded[r, c] = True
            elif excluded_prepared is not None and excluded_prepared.contains(point):
                excluded[r, c] = True
    return excluded


def _build_production_cell_mask(dem: dict, production_prepared, boundary_prepared) -> np.ndarray:
    """Per-cell boolean mask (True = cell center falls inside the given
    prepared geometry AND on-parcel) -- same iteration structure as
    _build_exclusion_cell_mask(), different (and simpler) boundary
    handling: True only when BOTH conditions hold, rather than "excluded
    if either."

    Despite the name (this function's own logic is unchanged from when it
    was first added purely for production-zone masking), find_road_routes()
    now reuses it for TWO different geometries: production_areas' own
    render_fill_polygon_utm union (this mask's own cells are now used
    THREE ways -- OR'd into the connector's own cost_raster excluded_mask
    as a HARD exclusion, counted per-candidate as _score_ridge_candidates()'s
    4th, SOFT scoring term against the ridge fragment itself, and reused
    to compute crosses_production_zone -- see module docstring) AND,
    separately, the floodplain/hydric union (a SOFT cost penalty on the
    connector only) -- the underlying "is this on-parcel cell inside that
    prepared geometry" test is identical either way, so there's no reason
    to duplicate it under a second name.

    production_prepared may be None (no production zones at all, or --
    when reused for floodplain -- no floodplain data at all), in which
    case every cell is simply False."""
    array = dem["array"]
    rows, cols = array.shape
    production_mask = np.zeros((rows, cols), dtype=bool)

    if production_prepared is None:
        return production_mask

    valid = ~np.isnan(array)
    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            point = Point(pixel_center_xy(dem, r, c))
            if boundary_prepared.contains(point) and production_prepared.contains(point):
                production_mask[r, c] = True
    return production_mask


def _snap_anchor_to_eligible_cell(
    dem: dict,
    cost_raster: np.ndarray,
    anchor_lon_lat: tuple[float, float],
) -> Optional[tuple[int, int]]:
    """
    Warps the given (lon, lat) anchor point into the DEM's own UTM CRS
    (same warp_transform pattern used throughout this module) and returns
    the nearest cell where np.isfinite(cost_raster[r, c]) -- routing
    starts from wherever eligible ground is actually closest to the given
    anchor, not from the anchor's own raw grid cell (which might itself be
    off-parcel, in a hard-excluded zone, or simply not a valid DEM cell at
    all).

    The anchor's approximate (row, col) is computed as the direct algebraic
    inverse of raster_grid.pixel_center_xy() (solved for row/col given x/y,
    rather than searched for), then the actual nearest ELIGIBLE cell is
    found by comparing every finite-cost cell's own grid distance to that
    point -- the direct inverse alone can legitimately land on a
    hard-excluded cell, so it's only a starting estimate, not the answer.

    Returns None if cost_raster has no finite-cost cell anywhere at all
    (the whole property is hard-excluded) -- a real, reportable "nothing
    to route from" outcome, matching this module's own least_cost_path()-
    style None-on-failure convention, rather than returning a nonsensical
    cell or raising.
    """
    eligible_rc = np.argwhere(np.isfinite(cost_raster))
    if eligible_rc.size == 0:
        return None

    anchor_xs, anchor_ys = warp_transform("EPSG:4326", dem["crs"], [anchor_lon_lat[0]], [anchor_lon_lat[1]])
    anchor_x, anchor_y = anchor_xs[0], anchor_ys[0]

    px, py = dem["resolution_meters"]
    approx_col = (anchor_x - dem["origin_x"]) / px - 0.5
    approx_row = (dem["origin_y"] - anchor_y) / py - 0.5

    distances_sq = (eligible_rc[:, 0] - approx_row) ** 2 + (eligible_rc[:, 1] - approx_col) ** 2
    nearest = eligible_rc[np.argmin(distances_sq)]
    return (int(nearest[0]), int(nearest[1]))


def _invert_dem(dem: dict) -> dict:
    """A ridge in real terrain is a valley in its negation -- standard GIS
    technique. Every other field (resolution, origin, crs) is shared
    as-is with the original dem dict; only the elevation array itself is
    negated, so downstream code that reads resolution_meters/origin_x/
    origin_y/crs off the result still sees the real, un-inverted values."""
    return {**dem, "array": -dem["array"]}


def _identify_ridge_cell_mask(dem: dict) -> np.ndarray:
    """
    Boolean ridge-cell mask, same shape as dem['array'] (True = ridge
    cell) -- recovers this module's own pre-fan-routing ridge-
    identification logic (git history: _generate_ridge_candidate_runs()/
    _invert_dem(), removed when this module was first rewired onto
    road_cost_path.py's routing), restructured around a boolean cell mask
    instead of valley_delineation.delineate_valleys()'s ordered
    head-to-outlet branch chains -- this module doesn't need individually
    traced branches, just "is this cell on a ridge at all," which
    connected_components() (run by this function's own caller, against
    this mask) groups into whole-parcel ridge NETWORKS on its own.

    Reuses valley_delineation's own D8 pipeline directly via
    get_flow_accumulation_for_dem() (fill -> flow direction -> flow
    accumulation) rather than delineate_valleys() itself -- thresholding
    the resulting per-cell contributing-area grid is exactly what
    delineate_valleys() does internally before it ever traces a branch;
    this just stops there instead of also tracing.

    Run against an INVERTED DEM (_invert_dem()) -- a ridge in real terrain
    is a valley in its negation, so flow "accumulates" along ridge crests
    in the inverted surface the same way it accumulates along real
    drainage lines in the original one.
    """
    accumulation = get_flow_accumulation_for_dem(_invert_dem(dem))
    contributing_area_acres = accumulation * cell_area_acres(dem)
    return contributing_area_acres >= RIDGE_MIN_AREA_ACRES


def _prune_ridge_networks(
    ridge_mask: np.ndarray, excluded_mask: np.ndarray
) -> list[list[tuple[int, int]]]:
    """
    Takes _identify_ridge_cell_mask()'s own whole-DEM boolean mask,
    groups it into whole-parcel ridge NETWORKS (connected_components(),
    one network per originally-contiguous ridge landform), then strips
    every HARD-excluded cell (excluded_mask -- the selected water zone
    (already buffered/prepared by the caller) + off-parcel/boundary cells
    ONLY; production zones are deliberately NOT part of this mask
    anymore -- a ridge fragment may walk straight through a production
    zone now, scored (not pruned) on how much of it does, see
    _score_ridge_candidates()'s 4th term; floodplain also stays a soft
    cost penalty applied only to the anchor-to-ridge connector, never to
    the ridge itself -- see module docstring) out of each network.

    Excluding cells can sever one originally-contiguous ridge into
    several disconnected pieces -- re-running connected_components()
    WITHIN each network's own remaining cells and keeping only the single
    LARGEST resulting fragment reflects that: a severed sliver isn't a
    meaningfully separate ridge candidate worth scoring on its own, just
    a remnant of one that got cut down.

    That largest surviving fragment is then dropped outright if it's
    still shorter than RIDGE_MIN_FRAGMENT_CELLS -- whether it started
    that short (a genuinely tiny original ridge network, no exclusion
    involved at all) or got cut down to it here. Applied AFTER picking
    the largest fragment, not before, so a network that's plenty long
    overall but happens to get severed into several small pieces is
    judged on its own best surviving piece, not on every piece's size.

    Returns one candidate (a plain list of (row, col) cells) per
    surviving original network that clears RIDGE_MIN_FRAGMENT_CELLS -- a
    network entirely inside excluded_mask, or one that's too short even
    after surviving, contributes nothing. Order is not meaningful;
    _score_ridge_candidates() is what actually ranks these.
    """
    network_labels, num_networks = connected_components(ridge_mask)

    candidates: list[list[tuple[int, int]]] = []
    for network_id in range(num_networks):
        remaining_mask = (network_labels == network_id) & ~excluded_mask
        if not np.any(remaining_mask):
            continue

        fragment_labels, num_fragments = connected_components(remaining_mask)
        fragment_sizes = [int(np.sum(fragment_labels == fid)) for fid in range(num_fragments)]
        largest_fragment_id = int(np.argmax(fragment_sizes))

        if fragment_sizes[largest_fragment_id] < RIDGE_MIN_FRAGMENT_CELLS:
            continue

        candidates.append(
            [(int(r), int(c)) for r, c in np.argwhere(fragment_labels == largest_fragment_id)]
        )

    return candidates


def _order_fragment_from_entry(
    fragment_cells: list[tuple[int, int]], entry_cell: tuple[int, int]
) -> list[tuple[int, int]]:
    """
    Orders one ridge fragment's own (unordered) cell set into a single
    entry-to-far-end path: 8-connected BFS confined to the fragment,
    rooted at entry_cell (the specific cell the anchor's own connector
    path actually reaches, see _score_ridge_candidates()). BFS explores
    in non-decreasing hop-distance order, so the LAST cell dequeued is
    guaranteed to be at the fragment's own maximum hop distance from
    entry_cell -- its "far end" relative to where the connector reaches
    it -- and walking came_from back from there to entry_cell gives the
    shortest (fewest-cell) path between the two.

    A ridge fragment is a NETWORK, not necessarily a single line (a
    branching confluence is real, expected drainage-style topology, same
    as valley_delineation's own valley networks) -- this picks one
    entry-to-far-end backbone through it for the final route's own
    geometry, not every branch.
    """
    fragment_set = set(fragment_cells)
    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    visited = {entry_cell}
    queue = deque([entry_cell])
    far_end = entry_cell

    while queue:
        current = queue.popleft()
        far_end = current
        r, c = current
        for dr, dc in D8_OFFSETS:
            neighbor = (r + dr, c + dc)
            if neighbor in fragment_set and neighbor not in visited:
                visited.add(neighbor)
                came_from[neighbor] = current
                queue.append(neighbor)

    path = [far_end]
    while path[-1] != entry_cell:
        path.append(came_from[path[-1]])
    path.reverse()
    return path


def _score_ridge_candidates(
    dem: dict,
    cost_raster: np.ndarray,
    ridge_candidates: list[list[tuple[int, int]]],
    anchor_cell: tuple[int, int],
    production_mask: np.ndarray,
) -> list[dict]:
    """
    Scores each surviving ridge fragment (_prune_ridge_networks()'s own
    output) on three normalized terms, then combines them into one
    RIDGE_SLOPE_WEIGHT/RIDGE_LENGTH_WEIGHT/RIDGE_PRODUCTION_WEIGHT-
    weighted score:

      - avg_slope_pct: mean per-cell slope across the fragment's own
        cells (gentler is better).
      - length_m: fragment cell count * the DEM's own average cell size
        (longer is better -- a longer usable ridge is a more valuable
        road corridor).
      - production_cells_crossed: count of the fragment's own cells that
        fall inside production_mask (fewer is better) -- production is no
        longer pruned out of a ridge candidate outright (see
        _prune_ridge_networks()), just scored, the same cell-count basis
        length_m already uses.

    road_cost_path.least_cost_path(dem, cost_raster, [anchor_cell],
    candidate_cells) is still called once per candidate -- it's still
    what determines the actual connector route (anchor -> whichever of
    the candidate's own cells the search reaches first/cheapest,
    "cells"/"destination_cell" in its return) and, just as importantly,
    whether the candidate is reachable at all -- but its own total_cost
    is NOT a separate scoring term anymore: least_cost_path() already
    picks each candidate's cheapest reachable entry point on its own, so
    scoring that same cost again as a fourth "proximity" term double-
    counted it without adding real information. cost_raster is expected
    to already hard-exclude production_mask (see find_road_routes()) --
    the connector itself can never cross production land even though the
    fragment it's reaching for may.

    Normalized against the candidate SET (not any fixed reference value):
        slope_score       = 1 - avg_slope_pct / max_avg_slope_pct
        length_score       = length_m / max_length_m
        production_score   = 1 - production_cells_crossed / max_production_cells_crossed
    each clamped to [0, 1] and, where the max itself is 0 (every surviving
    candidate tied at the best possible value for that term -- e.g. every
    ridge fragment perfectly flat, or none of them touching a production
    zone at all), treated as 1.0 rather than dividing by zero -- every
    candidate is equally best on that term, not equally worst.

    A candidate that road_cost_path.least_cost_path() can't reach at all
    from anchor_cell (blocked by hard exclusions on every side) is
    dropped outright -- an unreachable ridge isn't a real routing option,
    same "real, reportable non-outcome" pattern this module uses
    elsewhere rather than raising or forcing a result.

    Returns one dict per scored (reachable) candidate:
        {
            'cells': [(row, col), ...],            # the ridge fragment itself
            'avg_slope_pct': float,
            'length_m': float,
            'total_cost': float,                    # anchor -> fragment Dijkstra cost -- diagnostic only, NOT scored (see above)
            'production_cells_crossed': int,         # count of the fragment's own cells inside a production zone
            'connector_cells': [(row, col), ...],    # anchor -> entry_cell, in order
            'entry_cell': (row, col),                # whichever fragment cell the connector reaches
            'slope_score': float,                    # each in [0, 1]
            'length_score': float,
            'production_score': float,
            'weighted_score': float,
        }
    Returns [] if ridge_candidates is empty, or if none of them are
    reachable from anchor_cell at all.
    """
    if not ridge_candidates:
        return []

    slope_pct, _aspect_deg = compute_slope_and_aspect(dem["array"], dem["resolution_meters"])
    px, py = dem["resolution_meters"]
    cell_size_m = (px + py) / 2.0

    scored = []
    for cells in ridge_candidates:
        connector = least_cost_path(dem, cost_raster, [anchor_cell], cells)
        if connector is None:
            continue  # no eligible route reaches this ridge network at all

        cell_slopes = [float(slope_pct[r, c]) for r, c in cells if not np.isnan(slope_pct[r, c])]
        avg_slope_pct = float(np.mean(cell_slopes)) if cell_slopes else 0.0
        production_cells_crossed = sum(1 for r, c in cells if production_mask[r, c])

        scored.append(
            {
                "cells": cells,
                "avg_slope_pct": avg_slope_pct,
                "length_m": len(cells) * cell_size_m,
                "total_cost": connector["total_cost"],
                "production_cells_crossed": production_cells_crossed,
                "connector_cells": connector["cells"],
                "entry_cell": connector["destination_cell"],
            }
        )

    if not scored:
        return []

    max_avg_slope_pct = max(c["avg_slope_pct"] for c in scored)
    max_length_m = max(c["length_m"] for c in scored)
    max_production_cells_crossed = max(c["production_cells_crossed"] for c in scored)

    for candidate in scored:
        slope_score = 1.0 - candidate["avg_slope_pct"] / max_avg_slope_pct if max_avg_slope_pct > 0 else 1.0
        length_score = candidate["length_m"] / max_length_m if max_length_m > 0 else 1.0
        production_score = (
            1.0 - candidate["production_cells_crossed"] / max_production_cells_crossed
            if max_production_cells_crossed > 0
            else 1.0
        )

        candidate["slope_score"] = max(0.0, min(1.0, slope_score))
        candidate["length_score"] = max(0.0, min(1.0, length_score))
        candidate["production_score"] = max(0.0, min(1.0, production_score))
        candidate["weighted_score"] = (
            RIDGE_SLOPE_WEIGHT * candidate["slope_score"]
            + RIDGE_LENGTH_WEIGHT * candidate["length_score"]
            + RIDGE_PRODUCTION_WEIGHT * candidate["production_score"]
        )

    return scored


def _grade_stats(points_xyz: list[tuple[float, float, float]]) -> tuple[float, float]:
    grades = []
    for (x1, y1, z1), (x2, y2, z2) in zip(points_xyz, points_xyz[1:]):
        distance = math.hypot(x2 - x1, y2 - y1)
        if distance > 0:
            grades.append(abs(z2 - z1) / distance * 100.0)
    if not grades:
        return 0.0, 0.0
    return float(np.mean(grades)), float(np.std(grades))


def find_road_routes(
    dem: dict,
    production_areas: list[dict],
    selected_water_zone: Optional[dict],
    boundary_polygon_utm: Polygon,
    anchor_lon_lat: tuple[float, float],
    hydric_floodplain_union=None,
    min_corridor_length_meters: float = MIN_CORRIDOR_LENGTH_METERS,
) -> list[dict]:
    """
    Pure geometric/scoring core — see module docstring for why this takes
    already-computed inputs (production areas, the single selected water
    zone, floodplain cost-penalty union) rather than fetching or
    delineating anything itself.

    selected_water_zone is water_suitability.fetch_and_select_optimal_
    water_zone()'s own single rank-1 answer (or None if no water zone was
    identified) -- each entry carrying 'render_fill_polygon_utm' (SAME
    optimized/final geometry production_areas below uses, not the raw
    'polygon_utm'), HARD-excluded (buffered) from the ridge itself.
    production_areas is production_area_ceiling.identify_optimized_
    production_areas()'s own 'scored_patches' -- the OPTIMIZED/final,
    ceiling-trimmed patch shape (each entry carrying
    'render_fill_polygon_utm') -- HARD-excluded from the anchor-to-ridge
    CONNECTOR only; the ridge fragment itself may cross it now, scored
    (not pruned) by _score_ridge_candidates()'s 4th, production-crossing
    term (see module docstring; an earlier version of this module treated
    production as hard on the ridge too).
    hydric_floodplain_union is a shapely geometry (or None to skip that
    penalty entirely) already in dem['crs'] -- a SOFT cost penalty now,
    not a hard exclusion.

    anchor_lon_lat is the single (lon, lat) point the winning ridge
    candidate's own connector routes FROM (snapped to the nearest
    eligible cell -- see _snap_anchor_to_eligible_cell()). There is no
    named-road anchoring or boundary-connector step here at all (see
    module docstring) -- the final route IS the anchor-to-ridge connector
    concatenated with the winning ridge fragment itself, not something
    extended afterward.

    boundary_polygon_utm is the hard limit on which DEM cells routing can
    draw from at all, via _build_exclusion_cell_mask() -- the DEM covers
    ~100m past the drawn boundary on purpose (dem_data.py), but a route's
    own geometry must come from on-parcel cells only.

    Returns a list holding at most ONE route (this module's current
    design selects a single winning ridge candidate, not a ranked
    alternates list -- see module docstring):
        {
            'rank': 1,
            'suitability_score': float,      # 0-100, the winning candidate's own weighted_score * 100
            'avg_grade_pct': float,
            'length_m': float,
            'crosses_floodplain': bool,
            'crosses_production_zone': bool, # True if the winning ridge fragment itself enters a production zone
            'avg_slope_pct': float,          # the winning ridge fragment's own mean per-cell slope
            'production_cells_crossed': int, # count of the fragment's own cells inside a production zone
            'slope_score': float,            # each in [0, 1] -- see _score_ridge_candidates()
            'length_score': float,
            'production_score': float,
            'weighted_score': float,
            'points_xyz': [(x, y, elevation_m), ...],
            'line_utm': LineString,          # zero-width, dem['crs'] -- length_m/avg_grade_pct's own source geometry
            'cell_footprint_polygon_utm': Polygon/MultiPolygon,  # REAL, non-zero-width footprint --
                                              # see this function's own body for why
            'geometry_wgs84': GeoJSON LineString,
        }

    Returns [] if the anchor snaps to no eligible cell at all, if no
    ridge candidate survives pruning (_prune_ridge_networks() returns
    []), if none of the surviving candidates are reachable from the
    anchor at all (_score_ridge_candidates() returns []), or if the
    winning candidate's own final route is dropped by
    min_corridor_length_meters -- a real, reportable "no route" outcome,
    not an error.
    """
    slope_pct, _aspect_deg = compute_slope_and_aspect(dem["array"], dem["resolution_meters"])

    pond_union = (
        selected_water_zone["render_fill_polygon_utm"].buffer(POND_ZONE_EXCLUSION_BUFFER_METERS)
        if selected_water_zone is not None
        else None
    )
    hard_exclusion_pieces = [u for u in (pond_union,) if u is not None and not u.is_empty]
    combined_hard_exclusion_union = unary_union(hard_exclusion_pieces) if hard_exclusion_pieces else None
    excluded_prepared = prep(combined_hard_exclusion_union) if combined_hard_exclusion_union is not None else None
    boundary_prepared = prep(boundary_polygon_utm)
    # Water zone + off-parcel/boundary cells ONLY -- the only two things
    # that stop the ridge walk itself (see module docstring). Production
    # is deliberately NOT folded in here anymore: _prune_ridge_networks()
    # (below) receives this mask as-is, so a ridge fragment may freely
    # walk through a production zone now, scored (not pruned) by
    # _score_ridge_candidates()'s 4th, production-crossing term.
    ridge_exclusion_mask = _build_exclusion_cell_mask(dem, excluded_prepared, boundary_prepared)

    # Production zones stay a HARD exclusion for the anchor-to-ridge
    # CONNECTOR only (see module docstring) -- _build_production_cell_
    # mask()'s own output is OR'd into a SEPARATE mask used just for
    # cost_raster/the connector's own least_cost_path() search, never
    # passed to _prune_ridge_networks(). Also reused, unmodified, as
    # _score_ridge_candidates()'s own per-candidate production-crossing
    # count against the ridge fragment itself.
    raw_production_union = (
        unary_union([p["render_fill_polygon_utm"] for p in production_areas]) if production_areas else None
    )
    production_prepared = (
        prep(raw_production_union) if raw_production_union is not None and not raw_production_union.is_empty else None
    )
    production_mask = _build_production_cell_mask(dem, production_prepared, boundary_prepared)
    connector_exclusion_mask = ridge_exclusion_mask | production_mask

    # Floodplain/hydric is a SOFT cost penalty under the current design --
    # NOT folded into either exclusion mask at all, passed to
    # build_cost_raster() as its own floodplain_mask instead. Reuses
    # _build_production_cell_mask()'s exact "on-parcel cell inside this
    # prepared geometry" test (see that function's own docstring) against
    # a different geometry.
    floodplain_mask = None
    if hydric_floodplain_union is not None and not hydric_floodplain_union.is_empty:
        floodplain_prepared = prep(hydric_floodplain_union)
        floodplain_mask = _build_production_cell_mask(dem, floodplain_prepared, boundary_prepared)

    cost_raster = build_cost_raster(dem, slope_pct, connector_exclusion_mask, floodplain_mask=floodplain_mask)

    source_cell = _snap_anchor_to_eligible_cell(dem, cost_raster, anchor_lon_lat)
    if source_cell is None:
        return []

    ridge_mask = _identify_ridge_cell_mask(dem)
    _pre_prune_labels, _pre_prune_count = connected_components(ridge_mask)
    print(
        f"  find_road_routes: {int(ridge_mask.sum())} ridge cell(s) identified across "
        f"{_pre_prune_count} original network(s) (sizes: "
        f"{sorted((int((_pre_prune_labels == nid).sum()) for nid in range(_pre_prune_count)), reverse=True)})."
    )

    ridge_candidates = _prune_ridge_networks(ridge_mask, ridge_exclusion_mask)
    print(
        f"  find_road_routes: {len(ridge_candidates)} candidate(s) survive water/boundary pruning "
        f"(sizes: {sorted((len(c) for c in ridge_candidates), reverse=True)})."
    )
    if not ridge_candidates:
        return []

    scored_candidates = _score_ridge_candidates(dem, cost_raster, ridge_candidates, source_cell, production_mask)
    for c in scored_candidates:
        print(
            f"  find_road_routes: candidate ({len(c['cells'])} cells) -- avg_slope_pct={c['avg_slope_pct']:.1f} "
            f"length_m={c['length_m']:.0f} total_cost={c['total_cost']:.1f} (diagnostic only, not scored) "
            f"production_cells_crossed={c['production_cells_crossed']} -> slope_score={c['slope_score']:.3f} "
            f"length_score={c['length_score']:.3f} production_score={c['production_score']:.3f} "
            f"weighted_score={c['weighted_score']:.3f}"
        )
    if not scored_candidates:
        print("  find_road_routes: no candidate is reachable from the anchor at all.")
        return []

    winner = max(scored_candidates, key=lambda c: c["weighted_score"])
    print(f"  find_road_routes: winner has {len(winner['cells'])} cells, weighted_score={winner['weighted_score']:.3f}.")

    fragment_path = _order_fragment_from_entry(winner["cells"], winner["entry_cell"])
    # winner['connector_cells'] already ends at entry_cell (least_cost_
    # path()'s own destination_cell); fragment_path starts there too --
    # drop the duplicate so the concatenated route has each cell exactly
    # once, connector then fragment, one continuous line.
    result_cells = winner["connector_cells"] + fragment_path[1:]

    if len(result_cells) < 2:
        # The anchor snapped directly onto the ridge's own entry cell
        # with no fragment beyond it (a real, if unlikely, degenerate
        # case) -- no length/direction to form a LineString from at all.
        return []

    points = path_cells_to_points_xyz(dem, result_cells)
    line = LineString([(p[0], p[1]) for p in points])

    length_m = line.length
    if length_m < min_corridor_length_meters:
        return []

    # The REAL, non-zero-width footprint of this route's own path cells --
    # result_cells is exactly the cell-based form routing itself computed
    # (least_cost_path()'s connector cells + the winning ridge fragment's
    # own cells, in walk order), traced back to BEFORE path_cells_to_
    # points_xyz()/LineString above discard it down to a zero-width
    # centerline. raster_grid.cell_union_footprint() -- the SAME real
    # per-cell-square union every other cell-clustering consumer in this
    # pipeline uses (production_area.py's own _cell_union_footprint(),
    # water_candidate_zones.py's) -- turns it back into a real polygon:
    # one DEM cell wide along the whole route, not a hull or a buffer.
    # This exists specifically so a caller that needs to treat "ground
    # this route actually occupies" as REAL, non-negligible area (e.g.
    # tree_zone_candidates.py's own claimed-geometry exclusion) has a
    # real polygon to subtract, instead of line_utm's own zero-width
    # LineString (kept below, unchanged, as length_m/avg_grade_pct's own
    # source geometry -- this new field is purely additive).
    result_cell_mask = np.zeros(dem["array"].shape, dtype=bool)
    for cell_r, cell_c in result_cells:
        result_cell_mask[cell_r, cell_c] = True
    cell_footprint_polygon_utm = cell_union_footprint(dem, result_cell_mask)

    avg_grade_pct, _grade_stddev_pct = _grade_stats(points)
    crosses_floodplain = hydric_floodplain_union is not None and line.intersects(hydric_floodplain_union)
    crosses_production_zone = winner["production_cells_crossed"] > 0

    suitability_score = max(0.0, min(100.0, winner["weighted_score"] * 100.0))

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", xs, ys)

    route = {
        "rank": 1,
        "points_xyz": points,
        "line_utm": line,
        "cell_footprint_polygon_utm": cell_footprint_polygon_utm,
        "length_m": length_m,
        "avg_grade_pct": avg_grade_pct,
        "crosses_floodplain": crosses_floodplain,
        "crosses_production_zone": crosses_production_zone,
        "avg_slope_pct": winner["avg_slope_pct"],
        "production_cells_crossed": winner["production_cells_crossed"],
        "slope_score": winner["slope_score"],
        "length_score": winner["length_score"],
        "production_score": winner["production_score"],
        "weighted_score": winner["weighted_score"],
        "suitability_score": suitability_score,
        "geometry_wgs84": {"type": "LineString", "coordinates": list(zip(lons, lats))},
    }

    return [route]


def select_optimal_road_corridor(scored_routes: list[dict]) -> Optional[dict]:
    """
    Explicit selection step on top of find_road_routes()'s own ranking:
    returns the single route with rank == 1 (highest suitability_score) --
    no logic beyond that. Same pattern as water_suitability.
    select_optimal_water_zone(); per product decision, this app targets
    small farms only, where one well-suited road route is sufficient --
    no multi-route coexistence logic is needed here.

    Returns None if scored_routes is empty -- a real, reportable "no
    routes at all" outcome, not an error.
    """
    if not scored_routes:
        return None
    return max(scored_routes, key=lambda r: r["suitability_score"])


def _confidence_notes_for_route(
    floodplain_data_is_fallback: bool,
    avg_grade_pct: float,
    crosses_floodplain: bool,
    crosses_production_zone: bool,
) -> str:
    floodplain_crossing_note = FLOODPLAIN_CROSSING_NOTE if crosses_floodplain else ""
    production_crossing_note = PRODUCTION_CROSSING_NOTE if crosses_production_zone else ""

    steep_grade_note = (
        STEEP_GRADE_ENGINEERING_NOTE.format(
            avg_grade_pct=round(avg_grade_pct, 1), threshold_pct=STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT
        )
        if avg_grade_pct > STEEP_GRADE_ENGINEERING_NOTE_THRESHOLD_PCT
        else ""
    )
    floodplain_fallback_note = (
        "Floodplain/wet-ground cost scoring used a DEM-only fallback (buffered delineated valley "
        "lines), not real NHD/SSURGO data, because that data wasn't available for this run. "
        if floodplain_data_is_fallback
        else ""
    )
    return ROAD_CORRIDOR_CONFIDENCE_NOTES_TEMPLATE.format(
        floodplain_crossing_note=floodplain_crossing_note,
        production_crossing_note=production_crossing_note,
        steep_grade_note=steep_grade_note,
        floodplain_fallback_note=floodplain_fallback_note,
    )


def corridors_to_geojson(
    routes: list[dict],
    floodplain_data_is_fallback: bool = False,
) -> dict:
    """Wraps find_road_routes() output as the schema-conformant GeoJSON
    FeatureCollection this feature delivers (layer="suggested_road_corridor")."""
    features = []
    for route in routes:
        features.append(
            make_feature(
                feature_id=f"road-corridor-{route['rank']}",
                geometry=route["geometry_wgs84"],
                layer="suggested_road_corridor",
                label="Suggested road corridor",
                confidence=CONFIDENCE_LOW,
                confidence_notes=_confidence_notes_for_route(
                    floodplain_data_is_fallback,
                    route["avg_grade_pct"],
                    route["crosses_floodplain"],
                    route["crosses_production_zone"],
                ),
                extra_properties={
                    "rank": route["rank"],
                    "suitability_score": round(route["suitability_score"], 1),
                    "avg_grade_pct": round(route["avg_grade_pct"], 1),
                    "length_ft": round(route["length_m"] / METERS_PER_FOOT, 1),
                    "crosses_floodplain": route["crosses_floodplain"],
                    "crosses_production_zone": route["crosses_production_zone"],
                    # Diagnostic breakdown of _score_ridge_candidates()'s
                    # own three-term weighted score for the winning ridge
                    # candidate -- lets a caller (or a live diagnostic
                    # sweep, see RIDGE_SLOPE_WEIGHT/RIDGE_LENGTH_WEIGHT/
                    # RIDGE_PRODUCTION_WEIGHT's own unvalidated-starting-
                    # value caveat) see how each term actually contributed,
                    # not just the combined suitability_score. No
                    # ridge_proximity_score anymore -- see
                    # _score_ridge_candidates()'s own docstring for why a
                    # separate proximity-to-anchor term was redundant.
                    "ridge_avg_slope_pct": round(route["avg_slope_pct"], 1),
                    "ridge_production_cells_crossed": route["production_cells_crossed"],
                    "ridge_slope_score": round(route["slope_score"], 3),
                    "ridge_length_score": round(route["length_score"], 3),
                    "ridge_production_score": round(route["production_score"], 3),
                    "ridge_weighted_score": round(route["weighted_score"], 3),
                    # Only the selected water zone is a genuinely HARD
                    # exclusion the WHOLE route satisfies under the
                    # current design (see module docstring). Production is
                    # NOT listed here anymore -- it's hard on the
                    # connector only, but a SOFT scoring preference on the
                    # ridge fragment itself (see crosses_production_zone/
                    # ridge_production_score above), so the route as a
                    # whole no longer unconditionally satisfies it. Grade
                    # and floodplain stay excluded from this list entirely
                    # for the same reason they always were: both are soft
                    # cost penalties a route may or may not have paid, so
                    # claiming either "satisfied" would be misleading.
                    "constraints_satisfied": ["outside_pond_zone"],
                },
            )
        )

    return make_feature_collection(features)


def _log_fetch_failure(label: str, exc: Exception) -> None:
    """
    Every real-data fetch in this module degrades gracefully on failure
    (see the module docstring), but "gracefully" shouldn't mean "silently
    identical whether the cause was a transient network timeout or a real
    bug in the query itself." A 4xx HTTP status means the server rejected
    the REQUEST as malformed (bad SQL, a column/table that doesn't exist)
    — that will fail exactly the same way on every future run, unlike a
    timeout or connection error, which might not. Anything that isn't
    even a requests error (a KeyError from an unexpected response shape,
    etc.) is just as clearly a real bug, not network flakiness. Print a
    distinct message for each case so this doesn't look like ordinary
    network unavailability in the logs.
    """
    if isinstance(exc, requests.exceptions.RequestException):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None and 400 <= status < 500:
            print(
                f"  {label}: request rejected by the server (HTTP {status}) — likely a real "
                f"query/schema bug, not transient network unavailability: {exc}"
            )
        else:
            print(f"  {label}: network request failed ({exc}), continuing without it.")
    else:
        print(f"  {label}: unexpected failure, not a network error ({type(exc).__name__}: {exc}).")


def _fetch_floodplain_hydric_union(
    boundary_coordinates, dem, valleys, boundary_polygon_utm
) -> tuple[Optional[object], bool]:
    """NHD stream/water-body buffers + SSURGO hydric soil polygons,
    unioned; falls back to buffering the already-computed delineated
    valley lines (an elevation-only proxy) only if BOTH real sources are
    unreachable. Returns (union_or_None, is_fallback). This union now
    feeds a SOFT cost penalty (see module docstring), not a hard
    exclusion -- the fetch/clip/buffer logic that builds it is otherwise
    unchanged.

    Each fetched NHD feature is clipped to a generous context region
    around boundary_polygon_utm (FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS)
    BEFORE being buffered into the union — see that constant's own
    comment for the real bug this fixes: NHD's query returns each
    matching feature's full, un-clipped geometry, so a stream or
    waterbody merely touching the (already-buffered) fetch bounding box
    could come back with geometry extending far past the property,
    ballooning the resulting union to many times the parcel's own size.
    The SSURGO hydric piece needs no equivalent clip here — it already
    comes back clipped to the parcel's own wkt_polygon from
    get_soil_geometries_for_polygon() itself (STIntersection), so it can
    never exceed the parcel's own area in the first place.

    A mukey only contributes its hydric geometry here if its SUMMED hydric
    component percentage meets soil_data.MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE
    (soil_data.hydric_disqualifying_mukeys()) — a real, second bug found
    live alongside the NHD-clipping one above: a map unit that's 99%+
    well/moderately-well-drained but has a single 1%-of-composition hydric
    component was previously flagged the SAME as a map unit that's 85%+
    hydric, and its entire (much larger) mapped polygon got included right
    alongside genuinely wet ground.

    Each fetched NHD feature's BUFFERED piece is additionally intersected
    against boundary_polygon_utm.buffer(FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS)
    before being added to the union — see that constant's own comment for
    the THIRD bug this fixes: the fetch-context clip above bounds each raw
    stream feature before it's buffered, but nothing previously bounded
    the result AFTER buffering, so the buffer stroke (and however much
    stream length survived the looser fetch-context clip) could still
    produce a final piece extending well past any distance actually
    relevant to this parcel. Confirmed live: an 11.2-acre union of which
    only 0.077 acres (0.6%) actually overlapped the real parcel — a long
    buffered band along Montour Run, entirely on the far side of N Montour
    Rd from the field.
    """
    context_region = boundary_polygon_utm.buffer(FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS)
    final_relevance_region = boundary_polygon_utm.buffer(FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS)
    pieces = []

    try:
        water_features = get_water_features_for_boundary(boundary_coordinates)
        for feature in water_features["streams"] + water_features["water_bodies"]:
            geometry = feature.get("geometry")
            if geometry is None:
                continue
            utm_geometry = shape(transform_geom("EPSG:4326", dem["crs"], geometry))
            clipped_geometry = utm_geometry.intersection(context_region)
            if clipped_geometry.is_empty:
                continue
            buffered_geometry = clipped_geometry.buffer(FLOODPLAIN_STREAM_BUFFER_METERS)
            relevant_geometry = buffered_geometry.intersection(final_relevance_region)
            if relevant_geometry.is_empty:
                continue
            pieces.append(relevant_geometry)
    except Exception as e:
        _log_fetch_failure("NHD stream/water-body fetch", e)

    try:
        wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
        soil_components = get_soil_data_for_polygon(wkt_polygon)
        hydric_mukeys = hydric_disqualifying_mukeys(soil_components)
        if hydric_mukeys:
            geometries_by_mukey = get_soil_geometries_for_polygon(wkt_polygon)
            for mukey in hydric_mukeys:
                geometry = geometries_by_mukey.get(mukey)
                if geometry is not None:
                    pieces.append(shape(transform_geom("EPSG:4326", dem["crs"], geometry)))
    except Exception as e:
        _log_fetch_failure("SSURGO hydric soil fetch", e)

    if pieces:
        return unary_union(pieces), False

    # Fallback: neither NHD nor SSURGO reachable -- use the valley network
    # already computed for pond-zone identification as a coarse,
    # elevation-only "probably wet ground follows drainage lines" proxy.
    fallback_pieces = []
    for valley in valleys:
        for branch in valley["branches_utm"]:
            line = LineString([(p[0], p[1]) for p in branch])
            fallback_pieces.append(line.buffer(FLOODPLAIN_STREAM_BUFFER_METERS))

    return (unary_union(fallback_pieces) if fallback_pieces else None), True


def identify_road_corridor_candidates(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    **corridor_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches/derives everything find_road_
    routes() needs (optimized production areas, the single selected water
    zone, floodplain cost-penalty union) and returns the
    "suggested_road_corridor" GeoJSON FeatureCollection. Every real-data
    fetch degrades independently and gracefully, same pattern as
    water_candidate_zones.py and solar_suitability.py.

    anchor_lon_lat (lon, lat) is the real, chosen access point routing
    starts from (see find_road_routes()) -- kept Optional here (default
    None) purely so every EXISTING caller of this entry point that hasn't
    been updated to supply a real anchor point yet keeps working exactly
    as it did for "no road route available" (the same empty-result shape
    any other not-yet-computable layer in this pipeline already returns)
    rather than a hard TypeError. This is NOT a fabricated fallback anchor
    point -- there is no reasonable one to invent (a real anchor is a
    product decision -- where does this property actually connect to the
    outside world -- not something derivable from the boundary alone), so
    None here simply means no routes can be generated at all yet.

    Returns:
        {
            'zones_geojson': dict,                       # every scored route, ranked
            'all_scored_candidates': list[dict],         # find_road_routes()'s own
                                                            # raw scored list (carries line_utm)
            'selected_road_corridor': Optional[dict],    # select_optimal_road_corridor()'s single
                                                            # rank-1 answer, or None if no routes exist
        }
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    if anchor_lon_lat is None:
        return {
            "zones_geojson": corridors_to_geojson([]),
            "all_scored_candidates": [],
            "selected_road_corridor": None,
        }

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    # Optimized/final production geometry (production_area_ceiling.py's
    # own ceiling-trimmed, clustered/gated result) -- NOT production_area.
    # identify_production_areas()'s raw pre-optimization patches. Each
    # patch is expected to carry 'render_fill_polygon_utm' (production_
    # area.py's own render-only convex-hull field), the same field
    # find_road_routes() hard-excludes against below; fail loudly here
    # rather than let a KeyError surface deep inside that masking code if
    # this pipeline's own patch shape ever changes.
    production_areas = identify_optimized_production_areas(boundary_coordinates, dem=dem)["scored_patches"]
    if production_areas and "render_fill_polygon_utm" not in production_areas[0]:
        raise RuntimeError(
            "identify_optimized_production_areas()'s scored_patches no longer carry "
            "'render_fill_polygon_utm' -- road_corridors.py's production-zone hard exclusion "
            "depends on this field; update find_road_routes() to match the new shape."
        )

    valleys = delineate_valleys(dem)  # reused for the floodplain fallback below

    # The single water zone this property's OWN water-suitability scoring
    # actually selected (rank 1), not every unscored candidate zone
    # water_candidate_zones.py generates -- see module docstring.
    selected_water_zone = fetch_and_select_optimal_water_zone(boundary_coordinates, dem=dem)

    hydric_floodplain_union, floodplain_data_is_fallback = _fetch_floodplain_hydric_union(
        boundary_coordinates, dem, valleys, boundary_polygon_utm
    )

    routes = find_road_routes(
        dem,
        production_areas,
        selected_water_zone,
        boundary_polygon_utm,
        anchor_lon_lat,
        hydric_floodplain_union=hydric_floodplain_union,
        **corridor_kwargs,
    )

    return {
        "zones_geojson": corridors_to_geojson(
            routes,
            floodplain_data_is_fallback=floodplain_data_is_fallback,
        ),
        "all_scored_candidates": routes,
        "selected_road_corridor": select_optimal_road_corridor(routes),
    }


def fetch_and_select_optimal_road_corridor(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    **corridor_kwargs,
) -> Optional[dict]:
    """
    Convenience wrapper for callers (e.g. render_layout_map.py) that want
    a single best suggested road route rather than the full ranked
    FeatureCollection -- identify_road_corridor_candidates() already
    returns routes rank-ordered best-first (feature 0 = rank 1), so this
    just returns that top GeoJSON Feature (or None if nothing cleared the
    constraint stack, or no anchor_lon_lat was given). Selects nothing
    new -- it picks the #1 entry an existing, unchanged ranking already
    produced.
    """
    result = identify_road_corridor_candidates(boundary_coordinates, dem=dem, **corridor_kwargs)
    features = result["zones_geojson"]["features"]
    return features[0] if features else None


def summarize_road_corridor_candidates(result: dict) -> str:
    features = result["zones_geojson"]["features"]
    if not features:
        return "No road routes identified (no anchor point given, or nothing cleared the constraint stack)."

    lines = [f"Road routes identified: {len(features)}"]
    for feature in features:
        props = feature["properties"]
        crossing = " [crosses floodplain]" if props.get("crosses_floodplain") else ""
        crossing += " [crosses production zone]" if props.get("crosses_production_zone") else ""
        lines.append(
            f"  - Rank {props['rank']}: score {props['suitability_score']}/100, "
            f"{props['avg_grade_pct']}% avg grade, {props['length_ft']}ft{crossing}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]
    anchor_lon_lat = (-79.98356157031265, 40.64303511679458)

    print("Identifying suggested road routes for property boundary...\n")

    try:
        result = identify_road_corridor_candidates(property_boundary, anchor_lon_lat=anchor_lon_lat)
        print(summarize_road_corridor_candidates(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
