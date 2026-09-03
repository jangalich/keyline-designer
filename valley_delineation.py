"""
valley_delineation.py

Delineates primary valleys (drainage/flow paths) from a DEM grid — the
terrain-analysis step this pipeline's README long flagged as a future
piece ("real LiDAR-based keypoint detection... running real terrain-
analysis tools to mathematically locate keypoints and candidate keylines").

Pipeline, standard D8 hydrology terrain analysis (Barnes-style priority-
flood fill, then Garbrecht-Martz flat resolution, then D8 flow direction,
then flow accumulation):

    DEM grid --> fill depressions --> RESOLVE FLATS --> D8 flow direction
    --> flow accumulation --> threshold to valley cells --> group into
    connected drainage networks --> trace head-to-outlet branches --> line
    geometry

The conditioning prefix is one call, fill_and_resolve(), and every
consumer in this repo goes through it.

This module takes a plain DEM dict (see raster_grid.py's docstring for the
shape) and does pure numpy computation — no network calls, no rasterio
dependency beyond the one WGS84-reprojection step needed to produce
lon/lat output geometry. That split is deliberate: it's what makes this
logic unit-testable against a small synthetic DEM (see
test_valley_delineation.py) independently of whether the real DEM fetch
(dem_data.py) is working — the two failure modes ("is the terrain data
right" vs "is the valley-finding logic right") are easy to conflate
otherwise, per this feature's actual debugging history.

Known limitations, stated plainly rather than glossed over:
  - D8 (steepest single-neighbor descent) is the simplest standard flow
    model. It can't represent flow splitting/braiding: a cell gets one
    exit path even where real water would spread. Flat TIES are no longer
    part of that limitation — resolve_flats() lays a Garbrecht-Martz
    gradient across every filled pit or plateau, away from the higher
    ground that feeds it and toward the lower ground it spills to, so the
    flat slopes gently toward its own outlet and every cell on it routes.
    The direction that gradient assigns is a function of the flat's own
    inlets and outlets — NOT, as under the epsilon fill this replaced, of
    the order a priority queue happened to pop its cells. The -1
    "no downhill neighbour" sentinel survives, and correctly, at cells
    whose water has nowhere to go: the grid's own border outlets, and
    valid cells walled off from the border by nodata.
  - Depression filling assumes the fetched DEM's outer edge (the buffer
    around the property from dem_data.py) is a legitimate place for water
    to exit the grid. That's a reasonable assumption for a property-scale
    extract, not a full watershed delineation.
  - Resolution is whatever dem_data.py fetched (5m by default) — real
    micro-topography (a foot-wide rill) below that resolution won't show
    up. This is a first-pass, coarse valley network, not a survey.
"""

import heapq
import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform

from raster_grid import D8_OFFSETS, cell_area_acres, connected_components, pixel_center_xy

# Minimum upstream contributing area for a cell to count as "valley" at
# all (i.e. concentrated flow, not just diffuse sheet flow off a slope).
# CONFIGURABLE — tune against your own property: if the delineated network
# misses a drainage line you know is real on the ground, lower this; if it
# flags every minor swale as a "valley," raise it.
MIN_STREAM_CONTRIBUTING_AREA_ACRES = 0.5

# Minimum contributing area anywhere along a drainage network for it to
# count as a "primary" valley worth flagging as a water-system candidate,
# as opposed to a minor tributary. CONFIGURABLE, same tuning approach as
# above. Must be >= MIN_STREAM_CONTRIBUTING_AREA_ACRES.
MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES = 2.0

# The epsilon increment the depression fill adds per cell along a filled
# flat's own drainage path, so a filled cell never TIES with the
# neighbour it drains to.
#
# WHY IT EXISTS. fill_depressions() used to be the PLAIN priority-flood:
# it raised a pit to EXACTLY its spill elevation, so the filled cell and
# the neighbour it should drain to sat at the same elevation.
# compute_flow_direction() requires a STRICTLY positive slope, so every
# one of those tied cells got the -1 "no downhill neighbour" sentinel and
# was unroutable -- and every consumer that walks the flow field died
# there (truncated backwaters, wall walks ending flat_tie_sentinel,
# unreachable_stem_end cross-section stations, embankment seeds
# terminating flow_end with 0-2 stations measured). The epsilon variant
# (Barnes et al. 2014's own "Priority-Flood+epsilon") raises each cell to
# at least its predecessor's FILLED elevation plus this increment, so the
# filled surface slopes gently toward the outlet and every filled cell
# has a defined direction.
#
# WHY 0.001 m, stated as the two bounds it has to sit between:
#
#   UPPER BOUND -- it must be far too small to manufacture terrain
#   signal. USGS 3DEP's vertical accuracy is specified in the 0.1 m
#   neighbourhood (QL2 lidar's 10 cm RMSEz; the coarser 1/3 arc-second
#   product is worse), and this repo's own two independent "is this fill
#   real or noise" thresholds sit at 0.10 m
#   (water_survey_areas.DEPRESSION_NOISE_FLOOR_METERS) and 0.15 m
#   (keypoint_detection.KEYPOINT_FILL_ARTIFACT_THRESHOLD_M). At 0.001 m
#   a filled flat has to be 100 D8 HOPS across -- 500 m at this DEM's 5 m
#   resolution, in one dead-level piece -- before the accumulated rise
#   reaches even the lower of those two. (Hops, not cells: the flood
#   spreads outward from the spill, so a cell's increment count is its
#   Chebyshev distance from the spill point, not the flat's area. A
#   40x40 flat is 1600 cells and accumulates 40 increments, measured in
#   test_epsilon_fill.py.) That is two orders of magnitude below the
#   DEM's own ability to tell ground apart.
#
#   LOWER BOUND -- it must not vanish to floating-point rounding.
#   dem_data.get_dem_for_boundary() returns float32 (`src.read(1).
#   astype("float32")`), and fill_depressions() preserves its input's
#   dtype, so the increment is added at float32 precision. float32's ulp
#   at an elevation z is 2^(floor(log2 z) - 23): at the reference
#   property's ~346 m that is 3.05e-5 m, so 0.001 m is ~33 ulps and every
#   increment lands on a strictly greater float32. The margin holds up to
#   z = 0.001 * 2^23 = 8388 m, above every land elevation this tool can
#   be pointed at. (test_epsilon_fill.py asserts this against the real
#   float32 dtype via np.spacing() rather than taking it on faith.)
#
# CONFIGURABLE, but not a tuning knob in the ordinary sense: raising it
# buys nothing and starts eating into the noise floor above; lowering it
# eventually loses the strict inequality it exists to guarantee. v1
# prior -- if a filled flat on some property is ever long enough for the
# accumulated rise to approach DEPRESSION_NOISE_FLOOR_METERS, that is a
# finding to report, not a number to retune away.
#
# ITS RELATIONSHIP TO FLAT RESOLUTION, stated explicitly because the two
# increments look alike and are NOT interchangeable. On the PIPELINE path
# the epsilon is SUBSUMED, not kept as a fallback: every pipeline consumer
# now goes through fill_and_resolve(), which runs the PLAIN priority-flood
# (epsilon_meters=0.0) and then resolve_flats(), and it is
# resolve_flats()'s own FLAT_RESOLUTION_INCREMENT_METERS that supplies the
# strict descent on filled ground. Running BOTH would be actively wrong --
# the epsilon would tilt every flat by flood order first, leaving
# resolve_flats() no ties to resolve and re-importing the very artifact
# this branch removes.
#
# WHY NOT KEEP IT AS A FALLBACK for flats resolve_flats() cannot decide.
# Because there are none it could help. A flat is undecidable only when it
# has no outlet at all -- no D8 neighbour strictly below it and no contact
# with the grid border -- and after a plain fill that describes exactly one
# class of ground: a region the flood never reached because nodata walls it
# off from every border. The epsilon never reached those cells either (the
# flood is what applies it), so they carry the -1 sentinel today and carry
# it after this branch, unchanged and correctly. Tilting them by an epsilon
# would not route them anywhere -- there is nowhere to route TO -- it would
# only manufacture the artifact again on the one ground that has no
# geometry to derive a direction from. resolve_flats() leaves them alone
# and says so.
#
# The epsilon is NOT deleted. fill_depressions() keeps it as its default,
# because it is the correct behaviour for that function considered alone,
# because test_epsilon_fill.py pins the guarantee it bought, and because
# fill_and_resolve() needs the epsilon_meters=0.0 plain variant that the
# same parameter already provides. What changed is which of the two the
# pipeline reads.
FILL_EPSILON_METERS = 0.001

# The per-hop elevation increment the FLAT RESOLUTION pass (resolve_flats())
# lays across a flat to turn its geometry into a drainage pattern. Its own
# constant, deliberately not FILL_EPSILON_METERS reused under another name:
# the two increments answer different questions and are free to move apart.
#
# WHY IT EXISTS. Priority-Flood+epsilon (fill_depressions(), above) gave
# every filled cell a defined flow direction and that fixed a real defect
# -- but on GENUINELY LEVEL ground the direction it assigns is an artifact
# of FLOOD ORDER, not of terrain. The epsilon rides outward from whichever
# cell the priority queue happened to pop first, so a dead-flat DEM comes
# out with a drainage pattern that reflects the heap's tie-breaking and
# nothing about the land. Measured on a 20x20 dead-flat DEM, that
# manufactured signal moved flow accumulation off 1-everywhere and opened a
# TWI spread out of perfectly uniform ground -- and TWI feeds two scored
# criteria (the embankment blend's twi term, and half of the excavated
# blend's wetness term) plus the drainage band read at every compartment's
# pinch cell. Garbrecht & Martz (1997) is the standard principled
# replacement: route a flat by its OWN geometry -- away from the higher
# ground that feeds it, toward the lower ground it spills to -- so the
# imposed pattern is a property of the flat's inlets and outlets and is
# reproducible from them.
#
# WHY 0.001 m, stated as the two bounds it has to sit between. These are
# the same two bounds FILL_EPSILON_METERS carries, re-argued for this
# pass's own arithmetic rather than inherited by assertion:
#
#   LOWER BOUND -- it must not vanish to floating-point rounding.
#   dem_data.get_dem_for_boundary() returns float32 (`src.read(1).
#   astype("float32")`) and this pass preserves its input's dtype, so the
#   increment lands at float32 precision. The SMALLEST elevation
#   difference resolve_flats() ever creates between a cell and the cell it
#   drains into is exactly ONE of these increments -- see the descent
#   proof in resolve_flats(), where the outlet-distance term's weight
#   (FLAT_RESOLUTION_OUTLET_WEIGHT) exceeds the inlet term's full range by
#   exactly 1. So this constant's float32 margin IS the epsilon's, cell
#   for cell and unchanged: float32's ulp at elevation z is
#   2^(floor(log2 z) - 23), which at the reference property's ~346 m is
#   3.05e-5 m, so 0.001 m is ~33 ulps and every increment lands on a
#   strictly greater float32. The margin holds to z = 0.001 * 2^23 =
#   8388 m, above every land elevation this tool can be pointed at.
#   (test_flat_resolution.py asserts this against the real float32 dtype
#   via np.spacing(), not on faith.)
#
#   UPPER BOUND -- it must be far too small to manufacture terrain signal.
#   USGS 3DEP's vertical accuracy sits in the 0.1 m neighbourhood (QL2
#   lidar's 10 cm RMSEz; the 1/3 arc-second product is worse), and this
#   repo's two independent "is this fill real or noise" thresholds are
#   0.10 m (water_survey_areas.DEPRESSION_NOISE_FLOOR_METERS) and 0.15 m
#   (keypoint_detection.KEYPOINT_FILL_ARTIFACT_THRESHOLD_M). The rise this
#   pass accumulates across a flat is bounded by
#   FLAT_RESOLUTION_OUTLET_WEIGHT * increment per D8 hop of outlet
#   distance -- 0.002 m per hop -- so a flat must be 50 HOPS from its
#   furthest cell to its nearest outlet (250 m at this DEM's 5 m
#   resolution, in one dead-level piece) before the accumulated rise
#   reaches even the lower threshold. That is half the epsilon's own
#   100-hop headroom, and the halving is the honest price of carrying two
#   gradients instead of one; both numbers are measured, not asserted, in
#   test_flat_resolution.py.
#
# CONFIGURABLE, on the same terms as FILL_EPSILON_METERS: raising it buys
# nothing and eats into the noise floor above; lowering it eventually
# loses the strict inequality it exists to guarantee. If a flat on some
# property is ever long enough for the accumulated rise to approach
# DEPRESSION_NOISE_FLOOR_METERS, that is a finding to report, not a number
# to retune away.
FLAT_RESOLUTION_INCREMENT_METERS = 0.001

# The relative weight of the TOWARD-LOWER (outlet-distance) gradient
# against the AWAY-FROM-HIGHER (inlet-distance) one, in units of
# FLAT_RESOLUTION_INCREMENT_METERS per D8 hop.
#
# THIS IS THE DRAINAGE GUARANTEE, expressed as a number. resolve_flats()
# normalises the away-from-higher term into [0, 1] and gives the
# toward-lower term this weight, so the increment at a cell is
#
#     increment = INCREMENT_M * (OUTLET_WEIGHT * d_outlet + g_inlet)
#     with g_inlet in [0, 1]
#
# Because a breadth-first outlet distance guarantees every cell at
# d_outlet = k has a D8 neighbour at k - 1, the drop to that neighbour is
# at least INCREMENT_M * (OUTLET_WEIGHT - 1) -- which is why this weight
# must be STRICTLY GREATER THAN 1, and why at exactly 2 the guaranteed
# drop is exactly one increment. Every cell on a resolved flat therefore
# has a strictly lower neighbour BY CONSTRUCTION, and the epsilon fill's
# hard-won "no filled cell without a flow direction" is preserved rather
# than traded away.
#
# WHERE THIS DIVERGES FROM GARBRECHT & MARTZ AS PUBLISHED, stated plainly
# rather than glossed: GM97 weight the away-from-higher gradient MORE
# heavily than the toward-lower one (2:1 the other way) and then repair,
# in correction passes, the cells that combination leaves without a
# descent. Barnes, Lehman & Mulla (2014) document exactly that repair
# burden. This implementation inverts the weights so the descent is
# structural instead of repaired: the outlet gradient sets the drainage
# skeleton and the inlet gradient chooses among the cells that skeleton
# leaves tied. The ROUTING GEOMETRY both weightings produce is the same in
# the sense that matters here -- flow runs from the flat's inlets to its
# outlets, and the pattern is derived from those and nothing else -- but
# this one cannot strand a cell, and that was the branch's one hard
# requirement.
#
# THIS WEIGHT IS ALSO THE NOISE-FLOOR HEADROOM, and the two cannot be
# separated. The rise accumulates at OUTLET_WEIGHT * INCREMENT_M per hop
# while the guaranteed descent is (OUTLET_WEIGHT - 1) * INCREMENT_M, so
# the hop count a flat can reach before its accumulated rise hits
# DEPRESSION_NOISE_FLOOR_METERS is
#
#     floor / (OUTLET_WEIGHT * INCREMENT_M)
#
# = 50 hops at these values, against the epsilon's 100. Halving the
# headroom is the intrinsic price of carrying a second gradient at all:
# an away-from-higher term that spans a full outlet step costs exactly
# one extra step per hop, and no choice of INCREMENT_M changes the RATIO.
# What DOES change it is this weight. Lowering it toward 1 buys headroom
# back -- 1.5 gives 67 hops, with the away-from-higher term then spanning
# half an outlet step instead of a whole one, which still decides every
# tie WITHIN an outlet ring (the outlet term is constant there, so any
# positive span decides it) and only gives up the ability to override a
# cardinal-vs-diagonal preference ACROSS rings. That is the lever to
# reach for if a real property ever turns up a flat long enough to
# matter; it is deliberately NOT pre-tuned here, on the same terms as
# every other constant in this arc -- a flat that approaches the floor is
# a finding to report, not a number to retune away in advance.
FLAT_RESOLUTION_OUTLET_WEIGHT = 2.0

VALLEY_CONFIDENCE_NOTES = (
    "Valley lines are computed from an interpolated DEM (LiDAR-derived "
    "where flown, coarser 1/3 arc-second elsewhere — see dem_data.py) "
    "using standard D8 flow-direction/accumulation terrain analysis, not "
    "surveyed or field-verified. Flow is modeled as single-direction "
    "steepest descent between grid cells, which can't represent braided "
    "or split flow, and resolution-scale features (below the DEM's pixel "
    "size) won't appear. Treat this as a first-pass drainage network for "
    "design purposes, not a wetlands or hydrology survey. A valley line "
    "may legitimately extend past the drawn property boundary into the "
    "DEM's buffered surrounding area (see dem_data.py) — flow paths don't "
    "stop at a property line, so a branch's head or an inflection point "
    "sitting just off-parcel is real upstream/downstream context, not an "
    "error; this is intentionally NOT clipped to the boundary the way "
    "production-area and other candidate-zone layers are."
)


def _valid_mask(array: np.ndarray) -> np.ndarray:
    return ~np.isnan(array)


def fill_depressions(array: np.ndarray, epsilon_meters: float = FILL_EPSILON_METERS) -> np.ndarray:
    """
    Priority-flood depression filling, EPSILON variant (Barnes et al.
    2014's Priority-Flood+epsilon): raises every interior cell to at
    least its own flood predecessor's FILLED elevation plus
    epsilon_meters, so every valid cell has a STRICTLY DECREASING path to
    the grid's valid border. Without any fill, a local pit (real or a DEM
    interpolation artifact) traps flow accumulation and breaks valley
    tracing at that point.

    THE EPSILON IS THE POINT, not a detail. The plain variant this
    replaced raised a pit to EXACTLY its spill elevation, which left the
    filled cell TIED with the neighbour it should drain to;
    compute_flow_direction() requires a strictly positive slope, so every
    such cell got the -1 sentinel and was unroutable, and every consumer
    that walks the flow field stopped there. Raising by epsilon per cell
    gives the filled flat a defined flow direction toward its outlet. See
    FILL_EPSILON_METERS for the increment's two bounds (well under the
    DEM's vertical accuracy, well over float32's resolution) and for what
    the accumulated rise along a long flat costs.

    A cell already MORE than epsilon_meters above its predecessor is left
    alone — this raises only what it has to, so terrain outside the
    depressions and flats is bitwise unchanged. The RAW input array is
    never modified (a copy is filled and returned); the raw/filled
    division of labour — connectivity from the filled field, elevation
    truth from the raw — is a hard architectural boundary this function
    is on one side of.

    Passing epsilon_meters=0.0 reproduces the plain variant exactly, for
    before/after measurement only; nothing in the pipeline does that.

    nodata (np.nan) cells are excluded entirely — treated as barriers, not
    filled or flowed through.
    """
    rows, cols = array.shape
    valid = _valid_mask(array)
    filled = array.copy()
    closed = ~valid

    heap: list[tuple[float, int, int, int]] = []
    counter = 0

    def _seed(r: int, c: int) -> None:
        nonlocal counter
        if valid[r, c] and not closed[r, c]:
            heapq.heappush(heap, (float(filled[r, c]), counter, r, c))
            closed[r, c] = True
            counter += 1

    for c in range(cols):
        _seed(0, c)
        _seed(rows - 1, c)
    for r in range(rows):
        _seed(r, 0)
        _seed(r, cols - 1)

    while heap:
        elevation, _, r, c = heapq.heappop(heap)
        minimum_neighbor_elevation = elevation + epsilon_meters
        for dr, dc in D8_OFFSETS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc] and not closed[nr, nc]:
                closed[nr, nc] = True
                if filled[nr, nc] < minimum_neighbor_elevation:
                    # The epsilon increment: this cell now sits strictly
                    # above the cell it drains to, so it routes.
                    filled[nr, nc] = minimum_neighbor_elevation
                heapq.heappush(heap, (float(filled[nr, nc]), counter, nr, nc))
                counter += 1

    return filled



def find_flat_regions(filled: np.ndarray) -> tuple[np.ndarray, list[dict]]:
    """
    Every FLAT REGION on a filled surface: a maximal D8-connected set of
    valid cells at EXACTLY one elevation, containing at least one cell
    with no strictly lower valid neighbour (i.e. at least one cell
    compute_flow_direction() would hand the -1 sentinel).

    Intended for the PLAIN-filled surface -- fill_depressions(array,
    epsilon_meters=0.0). Run against an EPSILON-filled surface it finds
    almost nothing, and correctly so: the epsilon has already tilted every
    flat by flood order, which is the artifact resolve_flats() exists to
    replace rather than to inherit.

    Returns (labels, regions):

      labels    int32, -1 off any flat, otherwise the region's index into
                `regions`. Same shape/alignment as `filled`.
      regions   one dict per region, in label order:
                    'cells'   list[(row, col)], the whole region
                    'outlets' list[(row, col)], region cells that DRAIN
                    'inlets'  list[(row, col)], region cells that are FED

    OUTLETS are where the flat's water leaves, and there are two kinds,
    both real:
      * a region cell with a valid D8 neighbour STRICTLY LOWER than the
        region's elevation -- the spill, the ordinary case; and
      * a region cell on the GRID BORDER, which is an outlet by the same
        assumption the depression fill is already built on and states in
        the module docstring: the fetched DEM's outer edge is a legitimate
        place for water to leave the grid. Without this a dead-flat
        extract -- every cell one elevation, no spill anywhere -- would
        have no outlet at all and no derivable drainage, when in fact its
        geometry says exactly one thing, that water leaves at the rim.

    INLETS are region cells with a valid D8 neighbour STRICTLY HIGHER than
    the region's elevation: the higher ground that feeds the flat. A
    region can legitimately have none (a flat with nothing above it), and
    resolve_flats() handles that rather than assuming it away.

    nodata is a BARRIER throughout, never an outlet and never an inlet --
    the same treatment fill_depressions() gives it. A region walled off
    from every border by nodata therefore has no outlet of either kind,
    and is reported here with an empty 'outlets' list rather than being
    silently dropped; resolve_flats() is where that case is decided.
    """
    rows, cols = filled.shape
    valid = _valid_mask(filled)

    # A cell is a flat SEED if it is valid and has no strictly lower valid
    # neighbour -- exactly compute_flow_direction()'s -1 condition.
    has_lower = np.zeros((rows, cols), dtype=bool)
    for dr, dc in D8_OFFSETS:
        shifted = np.full((rows, cols), np.inf)
        r0, r1 = max(0, -dr), min(rows, rows - dr)
        c0, c1 = max(0, -dc), min(cols, cols - dc)
        shifted[r0:r1, c0:c1] = filled[r0 + dr : r1 + dr, c0 + dc : c1 + dc]
        with np.errstate(invalid="ignore"):
            has_lower |= shifted < filled
    seeds = valid & ~has_lower

    labels = np.full((rows, cols), -1, dtype=np.int32)
    regions: list[dict] = []

    for sr in range(rows):
        for sc in range(cols):
            if not seeds[sr, sc] or labels[sr, sc] >= 0:
                continue
            elevation = filled[sr, sc]
            label = len(regions)

            # Grow the whole equal-elevation component, INCLUDING cells
            # that are not seeds themselves -- those are the spill cells,
            # and they are the region's outlets.
            cells: list[tuple[int, int]] = []
            queue = [(sr, sc)]
            labels[sr, sc] = label
            while queue:
                r, c = queue.pop()
                cells.append((r, c))
                for dr, dc in D8_OFFSETS:
                    nr, nc = r + dr, c + dc
                    if (
                        0 <= nr < rows
                        and 0 <= nc < cols
                        and valid[nr, nc]
                        and labels[nr, nc] < 0
                        and filled[nr, nc] == elevation
                    ):
                        labels[nr, nc] = label
                        queue.append((nr, nc))

            outlets: list[tuple[int, int]] = []
            inlets: list[tuple[int, int]] = []
            for r, c in cells:
                on_border = r == 0 or c == 0 or r == rows - 1 or c == cols - 1
                drains = on_border
                fed = False
                for dr, dc in D8_OFFSETS:
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                        if filled[nr, nc] < elevation:
                            drains = True
                        elif filled[nr, nc] > elevation:
                            fed = True
                if drains:
                    outlets.append((r, c))
                if fed:
                    inlets.append((r, c))

            regions.append({"cells": cells, "outlets": outlets, "inlets": inlets})

    return labels, regions


def _bfs_hops(
    sources: list[tuple[int, int]], member: set[tuple[int, int]], shape: tuple[int, int]
) -> dict[tuple[int, int], int]:
    """
    D8 breadth-first hop count from `sources`, expanding only through
    `member` cells. Sources are at 0. Cells `member` cannot reach from any
    source are ABSENT from the result rather than carrying a sentinel --
    callers have to decide what unreachable means for them, and both of
    resolve_flats()'s two callers decide differently.
    """
    rows, cols = shape
    distance: dict[tuple[int, int], int] = {}
    frontier: list[tuple[int, int]] = []
    for cell in sources:
        if cell in member and cell not in distance:
            distance[cell] = 0
            frontier.append(cell)

    hop = 0
    while frontier:
        hop += 1
        nxt: list[tuple[int, int]] = []
        for r, c in frontier:
            for dr, dc in D8_OFFSETS:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    neighbour = (nr, nc)
                    if neighbour in member and neighbour not in distance:
                        distance[neighbour] = hop
                        nxt.append(neighbour)
        frontier = nxt

    return distance


def resolve_flats(
    filled: np.ndarray,
    increment_meters: float = FLAT_RESOLUTION_INCREMENT_METERS,
    outlet_weight: float = FLAT_RESOLUTION_OUTLET_WEIGHT,
) -> np.ndarray:
    """
    Garbrecht & Martz (1997) flat resolution: give every flat region on a
    PLAIN-filled surface a drainage pattern derived from ITS OWN GEOMETRY
    -- away from the higher ground that feeds it, toward the lower ground
    it spills to -- instead of from the order a priority queue happened to
    pop its cells.

    THE DEFECT THIS REPLACES. Priority-Flood+epsilon (fill_depressions()'s
    default) does give every filled cell a flow direction, and that fixed
    a real and separately-reported defect. But the direction it assigns on
    LEVEL ground is an artifact of flood order: the epsilon rides outward
    from whichever cell the heap popped first, so a dead-flat DEM acquires
    a drainage pattern that says nothing about the land. On a 20x20
    dead-flat DEM that manufactured pattern moved flow accumulation off
    1-everywhere and opened a spread in TWI out of perfectly uniform
    ground -- and TWI is scored. Flat resolution replaces that pattern
    with one that is a function of the flat's inlets and outlets alone,
    and is therefore reproducible from the terrain and symmetric wherever
    the terrain is.

    THE TWO GRADIENTS, per region (see find_flat_regions() for how a
    region, its outlets and its inlets are identified):

      d_outlet   D8 breadth-first hop count from ALL of the region's
                 outlet cells at once. Increases INTO the flat, away from
                 where its water leaves. This is the gradient TOWARD LOWER
                 GROUND: adding it makes far-from-outlet ground higher, so
                 flow runs to the nearest outlet.

      g_inlet    the region's inlet cells at 1, falling linearly to 0 at
                 the cell furthest (in D8 hops) from any inlet. This is
                 the gradient AWAY FROM HIGHER GROUND: adding it makes
                 ground next to the higher terrain that feeds the flat
                 higher, so flow runs away from the inflow edge. It is
                 NORMALISED into [0, 1] per region, which is what bounds
                 it against the outlet term below.

    THE COMBINATION, and why it is this one:

        increment = increment_meters * (outlet_weight * d_outlet + g_inlet)

    A breadth-first distance guarantees every cell at d_outlet = k has a
    D8 neighbour at d_outlet = k - 1. The drop to that neighbour is

        increment_meters * (outlet_weight * 1 + g_inlet(cell)
                            - g_inlet(neighbour))
        >= increment_meters * (outlet_weight - 1)

    because g_inlet lands in [0, 1]. With outlet_weight = 2 that is
    exactly ONE increment_meters, always, and it is why EVERY CELL OF A
    RESOLVED FLAT HAS A STRICTLY LOWER NEIGHBOUR BY CONSTRUCTION. The
    epsilon fill's guarantee is preserved, not traded for a new source of
    ties -- which was this branch's one hard requirement. See
    FLAT_RESOLUTION_OUTLET_WEIGHT for where this weighting departs from
    GM97 as published, and why.

    Within one d_outlet ring the outlet term is constant, so g_inlet is
    what picks the cell's exit -- steepest descent prefers the neighbour
    with the SMALLER g_inlet, i.e. the one further from the inflow edge.
    That is exactly the away-from-higher-terrain effect GM97 is for, and
    it does its work without ever threatening the descent above.

    THE THREE DEGENERATE REGIONS, each decided rather than assumed away:

      NO INLETS (a closed basin's floor, a flat with nothing above it):
      g_inlet is 0 everywhere and the outlet distance alone routes the
      flat. Defensible because it is honest -- with no higher ground
      touching the flat there is no away-from-higher information to be
      had, and the flat's geometry supplies exactly one fact, where its
      water leaves. The result is the distance transform from the outlet,
      which is the most that geometry supports.

      NO OUTLETS (a region nodata walls off from every border): LEFT
      ENTIRELY ALONE. Not tilted, not epsilon-ed. There is nowhere for its
      water to go, so any pattern imposed on it would be manufactured, and
      it is the one case where the honest answer is the -1 sentinel --
      which these cells already carry today under the epsilon fill, for
      the same reason (the flood never reaches them, so the epsilon never
      applies to them either). No regression: see FILL_EPSILON_METERS.

      SINGLE-CELL REGION: falls out of the general case with no special
      handling and needs none. Such a cell is its own outlet (it is on the
      grid border, or it has a lower neighbour), so d_outlet = 0; it is
      also its own entire inlet set if it is fed at all, so the
      away-from-higher term is uniform and therefore zero by the rule
      above. Its increment is exactly 0.0 and it is left at its filled
      elevation, bitwise.

    Returns a NEW array; `filled` is never modified. dtype is preserved,
    which is what makes the increment a float32 question -- see
    FLAT_RESOLUTION_INCREMENT_METERS for the ulp margin that answers it.
    Cells outside every flat region are BITWISE UNCHANGED.

    COST, measured rather than hand-waved, on the 108x98 parcel
    diagnose_flat_resolution_pipeline.py builds: 0.006 s where the surface
    carries four hand-placed flats, 0.077 s where it is quantised to 0.5 m
    so 147 flats cover 90% of the grid. Against the depression fill's own
    0.023 s that is 1.3x to 4.6x the conditioning step, and against
    compute_flow_direction()'s 0.055 s it is the same order as a per-cell
    Python loop this module already runs every time -- so it is a real
    cost on flat-heavy ground, not a new order of magnitude.
    """
    resolved = filled.copy()
    _labels, regions = find_flat_regions(filled)

    for region in regions:
        cells = region["cells"]
        outlets = region["outlets"]
        inlets = region["inlets"]

        if not outlets:
            # Nowhere for this flat's water to go. Imposing a pattern here
            # would be manufacturing one; the sentinel is the true answer.
            continue

        member = set(cells)
        shape = filled.shape
        outlet_set = set(outlets)

        d_outlet = _bfs_hops(outlets, member, shape)
        d_inlet = _bfs_hops(inlets, member, shape) if inlets else {}

        # Normalise the away-from-higher term into [0, 1]: 1 at the inlets,
        # falling linearly to 0 at the cell furthest from any of them. The
        # bound is what keeps it strictly under one outlet_weight step, and
        # the descent proof in this docstring depends on it.
        span = max(d_inlet.values()) if d_inlet else 0
        for r, c in cells:
            hops = d_outlet.get((r, c))
            if hops is None:
                # Unreachable from any outlet WITHIN the region. The region
                # is D8-connected and the outlets are members of it, so BFS
                # over the same connectivity reaches every cell; this
                # cannot fire. Skipping rather than guessing keeps that a
                # provable no-op instead of a silent tilt.
                continue
            if (r, c) in outlet_set:
                # AN OUTLET IS PINNED AT EXACTLY ZERO -- not merely at
                # d_outlet = 0, which would still leave it carrying the
                # away-from-higher term. This is where the flat's water
                # LEAVES, and lifting it buys nothing:
                #
                #   * A SPILL outlet already drains to its lower
                #     neighbour, so an increment only raises ground the
                #     fill did not raise -- straight into depression
                #     depth, which the excavated wetness criterion scores.
                #
                #   * A GRID-BORDER outlet is the case that made this
                #     explicit. Tilting a rim flat by the inlet term gave
                #     its border cells an IN-GRID direction, so water that
                #     should leave the window instead ran ALONG the rim
                #     and piled up -- measured at 3348 cells of
                #     accumulation carried sideways on one synthetic
                #     parcel, merging two drainage networks that the DEM's
                #     crop had no business joining. That is manufactured
                #     signal of exactly the kind this branch exists to
                #     remove, so the rim keeps its -1 sentinel and the
                #     water leaves, as it does under the epsilon fill.
                #
                # The descent guarantee only gets STRONGER: a cell at
                # d_outlet = 1 now drops a full outlet_weight increments
                # into the outlet instead of (outlet_weight - 1).
                continue
            # The away-from-higher term, normalised into [0, 1]: 1 at the
            # inlets, falling linearly to 0 at the region cell furthest
            # from any of them. A zero span means the term is UNIFORM --
            # the region has no inlets at all, or every one of its cells
            # touches higher ground -- so it carries no information and
            # can break no tie. Zero is the right constant for that case
            # rather than any other: it declines to lift ground the fill
            # itself never raised, which is what keeps this pass off
            # depression depth (guarded in test_flat_resolution.py).
            g_inlet = (span - d_inlet.get((r, c), span)) / span if span > 0 else 0.0
            resolved[r, c] = filled[r, c] + increment_meters * (
                outlet_weight * hops + g_inlet
            )

    return resolved


def fill_and_resolve(
    array: np.ndarray,
    increment_meters: float = FLAT_RESOLUTION_INCREMENT_METERS,
    outlet_weight: float = FLAT_RESOLUTION_OUTLET_WEIGHT,
) -> np.ndarray:
    """
    THE PIPELINE'S HYDROLOGIC CONDITIONING, one call: plain priority-flood
    depression fill, then Garbrecht-Martz flat resolution over the flats
    that fill leaves. This is what every consumer should call, and after
    this branch every consumer in this repo does; a bare
    fill_depressions() on the pipeline path would tilt the flats by flood
    order before resolve_flats() ever saw them.

    THE EPSILON IS NOT APPLIED HERE, deliberately: this runs
    fill_depressions(array, epsilon_meters=0.0), the PLAIN variant, so the
    flats survive the fill as genuine ties for resolve_flats() to decide
    on their own geometry. The strict descent the epsilon used to supply
    is supplied instead by resolve_flats()'s own increment, and is
    guaranteed by construction rather than by flood order -- see
    FILL_EPSILON_METERS for the full statement of what subsumes what, and
    resolve_flats() for the proof.

    Preserves the input's dtype and never modifies it. Terrain that is
    neither a depression nor a flat comes back BITWISE UNCHANGED, exactly
    as it did under the epsilon fill.
    """
    plain = fill_depressions(array, epsilon_meters=0.0)
    return resolve_flats(plain, increment_meters=increment_meters, outlet_weight=outlet_weight)

def compute_flow_direction(
    filled: np.ndarray, resolution_meters: tuple[float, float]
) -> tuple[np.ndarray, np.ndarray]:
    """
    D8 flow direction: each valid cell points to whichever of its up-to-8
    neighbors gives the steepest downhill slope (elevation drop / real
    ground distance, so diagonal neighbors are correctly penalized by
    sqrt(2)x the distance of cardinal ones).

    Returns (flow_to_row, flow_to_col), each shaped like `filled`, with -1
    at cells that have no downhill neighbor.

    WHERE THE -1 SENTINEL LEGITIMATELY REMAINS, on a surface conditioned by
    fill_and_resolve(): a GRID-EDGE OUTLET — a border cell that is the
    local minimum of its own neighbourhood — and a valid cell the flood
    cannot reach at all because nodata walls it off from every border.
    Both are cells whose water leaves, or has nowhere to go; neither is a
    stranding.

    The grid-edge case is deliberate rather than incidental.
    resolve_flats() PINS every outlet cell's increment to zero, and a
    border cell on a flat is an outlet, so a rim flat stays level and its
    border cells keep this sentinel. Tilting them instead would give
    water that should LEAVE the window an in-grid direction and run it
    along the DEM's own arbitrary crop edge — measured, on a synthetic
    parcel, at 3348 cells of accumulation carried sideways and two
    drainage networks spuriously merged.

    Every other cell on filled ground gets a real direction. Ordinary
    ground has a strictly lower neighbour already; a flat gets one by
    construction from resolve_flats()'s outlet gradient (see its own
    descent proof). Interior flat ties — the old dominant source of this
    sentinel, and the defect the epsilon fill was introduced to remove —
    stay gone. Downstream code still has to handle -1; it means "no
    direction here" and that is still a real answer at an outlet.
    """
    rows, cols = filled.shape
    valid = _valid_mask(filled)
    px, py = resolution_meters

    flow_to_row = np.full((rows, cols), -1, dtype=np.int32)
    flow_to_col = np.full((rows, cols), -1, dtype=np.int32)

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c]:
                continue
            best_slope = 0.0
            best = None
            for dr, dc in D8_OFFSETS:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    distance = math.hypot(dc * px, dr * py)
                    drop = filled[r, c] - filled[nr, nc]
                    slope = drop / distance
                    if slope > best_slope:
                        best_slope = slope
                        best = (nr, nc)
            if best is not None:
                flow_to_row[r, c], flow_to_col[r, c] = best

    return flow_to_row, flow_to_col


def compute_flow_accumulation(
    filled: np.ndarray, flow_to_row: np.ndarray, flow_to_col: np.ndarray
) -> np.ndarray:
    """
    Upstream contributing cell count per cell: each cell starts counting
    itself (1), then that total is added into whatever cell it flows into.
    Processing cells in descending filled-elevation order guarantees every
    cell has received all of its upstream contributions before it passes
    its own total downstream — flow direction only ever points to a
    strictly lower (or off-grid) cell, so there are no cycles to worry
    about.
    """
    rows, cols = filled.shape
    valid = _valid_mask(filled)
    accumulation = valid.astype(np.int64)

    sort_key = np.where(valid, filled, -np.inf)
    order = np.argsort(sort_key, axis=None)[::-1]  # descending elevation

    for flat_index in order:
        r, c = divmod(int(flat_index), cols)
        if not valid[r, c]:
            continue
        tr, tc = flow_to_row[r, c], flow_to_col[r, c]
        if tr >= 0:
            accumulation[tr, tc] += accumulation[r, c]

    return accumulation


def _trace_branches(
    component_cells: set[tuple[int, int]],
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
) -> list[list[tuple[int, int]]]:
    """
    Traces each "head" of a valley connected-component (a cell no other
    in-component cell flows into) downstream through the component to
    where it exits (an outlet — the next flow target isn't part of this
    component, i.e. drops below the stream threshold or leaves the grid).

    A component with tributaries produces multiple branches that share
    cells near their confluence — that's expected and matches how a real
    drainage network looks (tributaries merging into a main stem), not a
    bug.
    """
    in_degree = {cell: 0 for cell in component_cells}
    for r, c in component_cells:
        target = (int(flow_to_row[r, c]), int(flow_to_col[r, c]))
        if target in in_degree:
            in_degree[target] += 1

    heads = [cell for cell, degree in in_degree.items() if degree == 0]

    branches = []
    for head in heads:
        path = [head]
        visited = {head}
        r, c = head
        while True:
            target = (int(flow_to_row[r, c]), int(flow_to_col[r, c]))
            if target not in component_cells or target in visited:
                break
            path.append(target)
            visited.add(target)
            r, c = target
        branches.append(path)

    return branches


def _branch_to_wgs84_linestring(dem: dict, branch: list[tuple[int, int]]) -> dict:
    utm_points = [pixel_center_xy(dem, r, c) for r, c in branch]
    xs = [p[0] for p in utm_points]
    ys = [p[1] for p in utm_points]
    lons, lats = warp_transform(dem["crs"], "EPSG:4326", xs, ys)
    return {"type": "LineString", "coordinates": list(zip(lons, lats))}


def get_flow_direction_for_dem(dem: dict) -> np.ndarray:
    """
    Runs the fill -> flow-direction prefix of the delineate_valleys()
    pipeline (see module docstring) and stops there -- one step before
    get_flow_accumulation_for_dem() itself, and well before the stream
    threshold/tracing steps that turn any of this into discrete valley
    branches.

    Returns a single (rows, cols, 2) int32 array, same rows/cols shape and
    alignment as dem['array'] and get_flow_accumulation_for_dem()'s own
    output (a cell at (row, col) means the same thing across all three).

    ENCODING: this is NOT the classic D8 8-direction bitmask/integer code
    (1, 2, 4, 8, 16, 32, 64, 128 for N/NE/E/SE/S/SW/W/NW) --
    compute_flow_direction() itself never computes that code, so this
    doesn't invent one just to expose it. Instead, the last axis holds
    compute_flow_direction()'s own (flow_to_row, flow_to_col) pair,
    stacked into one array:

        target_row, target_col = get_flow_direction_for_dem(dem)[row, col]

    i.e. the ABSOLUTE (row, col) of the single downhill neighbor that
    cell flows into (not a relative offset or direction code). (-1, -1)
    means no downhill neighbor at all (a grid-edge outlet, or a valid
    cell nodata walls off from the border -- see compute_flow_direction()
    for where the sentinel legitimately survives the epsilon fill) --
    matches compute_flow_direction()'s own -1 sentinel exactly.

    A caller walking the grid cell-to-cell reads target_row, target_col =
    get_flow_direction_for_dem(dem)[row, col]; if target_row < 0, stop
    (outlet); otherwise move to (target_row, target_col) and repeat.
    """
    filled = fill_and_resolve(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    return np.stack([flow_to_row, flow_to_col], axis=-1)


def get_flow_accumulation_for_dem(dem: dict) -> np.ndarray:
    """
    Runs the fill -> flow-direction -> flow-accumulation prefix of the
    delineate_valleys() pipeline (see module docstring) and stops there,
    before the stream threshold/tracing steps that turn it into discrete
    valley branches.

    Returns the raw upstream contributing-cell-count grid (same shape as
    dem["array"], same row/col alignment as every other DEM-shaped array
    in this codebase — nodata cells count as 1, matching what
    compute_flow_accumulation always does; they're never valley cells
    themselves but flow accumulation doesn't zero them out). This is for
    a downstream module that wants to test individual DEM cells against
    the accumulation grid directly (e.g. "is this cell on high-enough
    contributing area to be a channel") rather than only consuming traced
    branch polylines. It intentionally does not multiply by
    cell_area_acres(dem) or apply either area threshold — callers that
    want acres or "is this a valley cell" should do that conversion
    themselves, same as delineate_valleys() does internally.
    """
    filled = fill_and_resolve(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    return compute_flow_accumulation(filled, flow_to_row, flow_to_col)


def delineate_valleys(
    dem: dict,
    min_stream_area_acres: float = MIN_STREAM_CONTRIBUTING_AREA_ACRES,
    min_primary_valley_area_acres: float = MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES,
) -> list[dict]:
    """
    Runs the full fill -> flow direction -> flow accumulation -> threshold
    -> trace pipeline on `dem` (see raster_grid.py for the DEM dict shape)
    and returns one entry per primary valley:

        {
            'id': int,
            'max_contributing_area_acres': float,
            'branches_rowcol': [[(row, col), ...], ...],  # head -> outlet
            'branches_utm': [[(x, y, elevation_m), ...], ...],
            'geometry_wgs84': GeoJSON LineString or MultiLineString,
        }

    branches_utm is what water_candidate_zones.py actually reasons over
    (real-meter coordinates + elevation, in the DEM's own projected CRS,
    so gradient/distance math is exact); geometry_wgs84 is only for
    output/display.
    """
    array = dem["array"]
    filled = fill_and_resolve(array)
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)

    area_per_cell = cell_area_acres(dem)
    contributing_area_acres = accumulation * area_per_cell

    valley_mask = contributing_area_acres >= min_stream_area_acres
    labels, num_components = connected_components(valley_mask)

    valleys = []
    for component_id in range(num_components):
        rows_cols = np.argwhere(labels == component_id)
        component_cells = {(int(r), int(c)) for r, c in rows_cols}

        max_area = max(contributing_area_acres[r, c] for r, c in component_cells)
        if max_area < min_primary_valley_area_acres:
            continue  # a real drainage line, but too minor to call "primary"

        branches_rowcol = _trace_branches(component_cells, flow_to_row, flow_to_col)
        # A branch needs at least 2 cells to be a line at all; a component
        # that reduces to single isolated cells (a rare, degenerate case)
        # isn't a meaningful valley line and is dropped rather than
        # emitting invalid single-point GeoJSON LineString geometry.
        branches_rowcol = [b for b in branches_rowcol if len(b) >= 2]
        if not branches_rowcol:
            continue

        branches_utm = [
            [(*pixel_center_xy(dem, r, c), float(filled[r, c])) for r, c in branch]
            for branch in branches_rowcol
        ]

        line_geometries = [_branch_to_wgs84_linestring(dem, b) for b in branches_rowcol]
        geometry_wgs84 = (
            line_geometries[0]
            if len(line_geometries) == 1
            else {
                "type": "MultiLineString",
                "coordinates": [g["coordinates"] for g in line_geometries],
            }
        )

        valleys.append(
            {
                "id": component_id,
                "max_contributing_area_acres": round(float(max_area), 2),
                "branches_rowcol": branches_rowcol,
                "branches_utm": branches_utm,
                "geometry_wgs84": geometry_wgs84,
            }
        )

    return valleys


def valleys_to_geojson(valleys: list[dict]) -> dict:
    """
    Wraps delineate_valleys() output as a schema-conformant GeoJSON
    FeatureCollection (layer="valley") — the discrete-feature output Step 1
    calls for, separate from the raw DEM raster itself.

    CONSOLIDATED: the body now lives in wire_translation.py (the outbound
    half of the translation boundary), which is where every layer's
    internal-result-to-feature_schema conversion is kept so the inbound
    half added later has one place to be checked against. This name stays
    as the module's own established entry point; it is a thin forward, not
    a second implementation.
    """
    from wire_translation import valleys_to_feature_collection

    return valleys_to_feature_collection(valleys)


def summarize_valleys(valleys: list[dict]) -> str:
    if not valleys:
        return "No primary valleys identified on this property."

    lines = [f"Primary valleys found: {len(valleys)}"]
    for v in sorted(valleys, key=lambda x: -x["max_contributing_area_acres"]):
        lines.append(
            f"  - Valley {v['id']}: {len(v['branches_rowcol'])} branch(es), "
            f"up to {v['max_contributing_area_acres']} acres contributing area"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # Offline smoke test with a synthetic V-shaped valley DEM — see
    # test_valley_delineation.py for the real (assertion-based) version of
    # this. This is just a quick eyeball check when iterating on the
    # algorithm directly.
    size = 30
    array = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        for col in range(size):
            # Elevation rises with distance from the diagonal "valley
            # floor" and slopes down along it (south to north), so flow
            # should converge onto the diagonal and head toward row 0.
            distance_from_valley = abs((size - 1 - row) - col)
            array[row, col] = distance_from_valley * 2.0 + row * 0.5

    synthetic_dem = {
        "array": array,
        "resolution_meters": (5.0, 5.0),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }

    valleys = delineate_valleys(synthetic_dem)
    print(summarize_valleys(valleys))
