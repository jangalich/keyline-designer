"""
water_candidate_zones.py

Step 3 of water-system candidate-zone identification: a per-cell
eligibility mask, TWO-FAMILY NOMINATION of anchor cells on it, and a
LEVEL-POOL DELINEATION of the zone around each anchor.

    DEM (dem_data.py)
        --> filled DEM + D8 flow direction/accumulation + upstream map
            (valley_delineation.py / keypoint_detection.build_upstream_map)
        --> production areas (production_area.py)
        --> [this module] per-DEM-cell eligibility mask (ABSOLUTE
            contributing-area ceiling + on-parcel + max service distance +
            boundary setback (now 0.0, inert) + canopy root-zone
            exclusion + existing-road exclusion)
        --> NOMINATION of up to MAX_WATER_ZONE_CANDIDATES anchor cells:
              family 1 -- keypoints (keypoint_detection.detect_keypoints()),
                          ordered by contributing_acres descending, each
                          snapped to the nearest eligible cell within
                          WATER_KEYPOINT_SEED_SNAP_METERS
              family 2 -- the highest-flow-accumulation unclaimed eligible
                          cells, until the candidate cap or the seeds run out
        --> per anchor: valley_level_pool.delineate_level_pool() -- the
            backwater region at POOL_REFERENCE_HEIGHT_METERS plus the
            dam-axis band, clipped to the parcel boundary ONLY
        --> area floor/cap, service-distance relationship, whole-zone
            aggregates -> N candidate-zone polygons with provenance,
            measurements, reason codes and flags

WHAT THIS REPLACED, AND WHY. The previous design took the same eligibility
mask, ran 4-connected CONNECTED COMPONENTS over it, grew each component
greedily to a fixed survey-area target constant, and returned
exactly ONE zone (the highest post-growth summed accumulation). Both
halves of that are gone:

  - A CONNECTED COMPONENT OF ELIGIBLE CELLS IS NOT A POND SITE. The
    component's shape was an artifact of where the gates happened to cut,
    so its extent said nothing about where water would actually stand. A
    level pool at a chosen anchor is a statement about the terrain: this
    ground sits below that waterline, and this is the line a wall would
    have to span. Zone size is no longer configured at all -- it EMERGES
    from the terrain, which is why the survey-area target constant and
    the greedy-growth helper are both DELETED rather than retuned, and why
    MAX_WATER_ZONE_AREA_ACRES exists purely as an outer sanity bound
    rather than as a target.
  - ONE ZONE WAS NEVER THE RIGHT ANSWER for a survey deliverable. The
    single-winner rule (and the "second pass deferred" note that used to
    stand here) meant a parcel with three genuinely different reaches
    reported one of them. Nomination now returns up to
    MAX_WATER_ZONE_CANDIDATES, each carrying its own provenance
    (nominated_by, and for family 1 the keypoint/valley it came from), so
    the report can compare them and the scoring layer can rank them.

Adjacent family-2 seeds along one drainage may legitimately produce
upstream/downstream neighbour candidates on the same valley. That is
ACCEPTED in this version -- "survey this reach at two stations" is a real
answer -- so there is deliberately no per-valley diversity rule.

WHAT IS NOT DECIDED HERE. Scoring is water_suitability.py's job and is
untouched by this design: this module nominates, delineates, measures, and
reports; it does not weigh. The level-pool measurements it stores (flooded
width and flooded cross-sectional area per station, abutment distances,
canopy/road overlap percentages) exist for that scoring layer to read.
NO VOLUME is computed anywhere -- see valley_level_pool.py's own module
docstring for why a storage figure off a public DEM would be a fabricated
engineering number.

THE PRODUCTION-OVERLAP EXCLUSION IS DELETED. Water-zone cells used to be
hard-excluded from any production area's render fill plus a 5 m build
margin. The constant, the gate and its parameter are all deleted. This
is the SAME gate-to-preference move this
pipeline already made for gravity (this module's own
production_area_relationships, scored rather than gated), for soil
(production_suitability.py's docstring) and for prime farmland: the
premise changed rather than the value being wrong. Whether a pond may sit
on ground currently read as production land is the USER'S call in the
interactive design -- the two uses genuinely compete, and a generation-time
gate silently made that call for them, deleting the best drainage on
parcels where production covers the valley floor. In the follow-up scoring
branch, production overlap becomes a scoring FACTOR; this branch already
reports the geometry it needs. (Contrast MIN_BOUNDARY_SETBACK_METERS,
which was ZEROED rather than deleted: there the constant still answers a
real question, its VALUE was simply wrong.)

production_areas remains a REQUIRED argument regardless: the
max-service-distance gate and the per-zone production-area relationships
both still need it.

All other gates are unchanged: contributing-area ceiling, on-parcel, the
inert boundary setback, the canopy root zone, the road exclusion, and max
service distance. Note the canopy and road exclusions gate ELIGIBILITY (an
anchor may not be nominated on a tree or a road) but never CLIP a
delineated pool -- see find_candidate_zones() for why a pool clipped by a
root-zone mask would misrepresent the physics.

find_candidate_zones() below is deliberately a pure function over an
already-fetched dem plus already-computed production_areas/boundary -- no
DEM fetch, no network. That split is what makes Stage 2 ("is the
zone-filtering logic correct") testable independently of Stage 1 ("is the
DEM/valley delineation accurate") -- see test_water_candidate_zones.py and
test_valley_level_pool.py, and the module docstrings on dem_data.py/
valley_delineation.py/production_area.py for the same reasoning applied to
the layers underneath this one. keypoints, valleys, the filled array and
the flow arrays are all optional overrides in the same self-computing
family, forwarded rather than re-derived, so a caller holding them (e.g.
pipeline_context.build_pipeline_context(), which computes keypoints once
per run) never pays for a second detection pass.

REASON CODES. Every candidate that is NOT produced records WHY, as one of
a module-level enumeration of string constants (nominated,
seed_snapped, no_eligible_cell_within_snap, too_close_to_candidate_<id>,
below_min_area, no_service_relationship, ...) rather than an ad-hoc
literal at each site, and every flag on a candidate that WAS produced
(truncated_by_boundary, truncated_by_cap, overlap_trimmed) is drawn from
the same enumeration. An empty or partial result is then explainable
rather than merely empty. This is the start of a convention other KSOP
modules can adopt.

Each zone also carries render_fill_polygon_utm/render_fill_geometry_wgs84.
For water zones this is a bounded morphological OPENING of the zone's own
cell mask, clipped to polygon_utm -- the same disc opening production zones
use (raster_grid.eroded_cell_mask()/binary_dilate() with element="disc"),
but at a DELIBERATELY tiny radius (WATER_ZONE_RENDER_OPENING_RADIUS_METERS)
and with NO lead erode, because a small zone (a fraction of an acre, ~9x9
cells on a 5m grid) cannot afford to lose a cell off every edge. The
opening softens the blocky cell-union edge and trims single-cell
protrusions; it can also sever a genuinely too-narrow pinch, so
render_fill_polygon_utm may be a MultiPolygon (acceptable -- the pinch is
too narrow to be one coherent survey area). A zone thinner than the opening
radius throughout erodes to nothing; render_fill_polygon_utm then falls
back to polygon_utm (non-empty, logged once). The invariant
render_fill_polygon_utm is a subset of polygon_utm is asserted, raising on
violation. polygon_utm stays the real, unsmoothed cell-union footprint of
the delineated pool + dam band; render_fill_polygon_utm is smaller, the
same way production's drawn fill runs a fraction of the eligible footprint.
"""

import logging
import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely import contains_xy
from shapely.geometry import Point, Polygon, mapping
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from keypoint_detection import build_upstream_map, detect_keypoints
from production_area import (
    METERS_PER_FOOT,
    _fetch_road_exclusion_union_utm,
    compute_slope_percent,
    get_required_tree_root_zone_mask_utm,
    identify_production_areas,
    production_areas_to_geojson,
)
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    binary_dilate,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    eroded_cell_mask,
    pixel_center_xy,
    waist_erosion_radius_cells,
)
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    delineate_valleys,
    fill_depressions,
    get_flow_accumulation_for_dem,
    valleys_to_geojson,
)
from valley_level_pool import (
    ABUTMENT_SEARCH_HALF_WIDTH_METERS,
    CROSS_SECTION_STATION_SPACING_METERS,
    CROSS_SECTION_STATIONS,
    MAX_BACKWATER_UPSTREAM_METERS,
    POOL_REFERENCE_HEIGHT_METERS,
    delineate_level_pool,
)

_LOGGER = logging.getLogger(__name__)

# Setback from the property boundary applied to candidate water-zone
# cells. ZEROED (was 15.0). Flow accumulation is maximal where water
# leaves the parcel, which is always near an edge -- on the reference
# property every high-accumulation cell sat 0.87-10.8 m from the boundary
# and was eliminated by the old 15 m setback, discarding the best sites.
# The constant, its docstring, and every code path that reads it are kept
# so a setback can be reintroduced without a schema change; only its VALUE
# is 0.0, which makes the setback test (distance < 0.0) inert. What would
# justify reintroducing a nonzero value: real, parcel-specific setback/
# easement/access rules this pipeline currently has no data on. Note the
# on-parcel containment test is a SEPARATE, independent guard (see
# compute_water_eligible_cells()) -- zeroing this setback does not weaken
# off-parcel exclusion. CONFIGURABLE.
MIN_BOUNDARY_SETBACK_METERS = 0.0

# How far downhill a candidate cell's elevation advantage is considered
# relevant to a given production-area patch at all. Beyond this, even a
# technically-qualifying gradient isn't a plausible single contour-channel
# run. CONFIGURABLE.
MAX_SERVICE_DISTANCE_METERS = 800.0

# Absolute ceiling on a cell's own contributing area. Above roughly this,
# a pond site silts in, runs turbid, and needs engineered spillway capacity
# regardless of pond size (NRCS CPS 378 changes freeboard requirements above
# 20 drainage acres). This is a peak-flow and sediment limit, NOT a fill-rate
# ratio -- it does not scale with target pond size, which is why it is a flat
# value rather than a multiple of anything.
#
# This ABSOLUTE ceiling replaces the old boundary-dependent percentile band
# (VALLEY_ACCUMULATION_PERCENTILE_LOW/HIGH) and the old lower gate
# (MIN_VALLEY_CONTRIBUTING_AREA_ACRES). A percentile is defined relative to
# its population -- the cells inside the drawn boundary -- so moving the
# boundary moved the selected band even though the terrain was unchanged
# (the core bug this rewrite fixes). Contributing area in acres is a
# physical property of the terrain and does not depend on where a line was
# drawn, so the gate is now absolute. There is deliberately NO lower
# bound: the deliverable is a survey area, not a pass/fail on pond
# viability, so water zones report the best available site rather than
# returning nothing near the top of a watershed. CONFIGURABLE.
MAX_VALLEY_CONTRIBUTING_AREA_ACRES = 20.0

# Drop tiny, noise-sized eligible-cell clusters below this real cell-union
# footprint area. A small first-pass default, deliberately NOT yet
# validated against a real property the way production_area.py's own
# MIN_PRODUCTION_AREA_ACRES has been — tune once ground-truthed. This is
# the cluster-size floor -- the direct analogue of production's
# MIN_PRODUCTION_AREA_ACRES -- NOT a contributing-area floor; there is no
# contributing-area minimum in this design. CONFIGURABLE.
MIN_WATER_ZONE_AREA_ACRES = 0.1

# Outer sanity bound on a delineated candidate's own boundary-clipped
# footprint. There is deliberately NO target size any more (the retired
# survey-area target constant): zone size EMERGES from the terrain -- from how
# far the reference waterline actually reaches -- so a target would be a
# number invented about ground the level pool already measured.
#
# What this cap is really for is FLAT GROUND. On a genuinely level draw a
# 2.5 m waterline floods absurdly far, and the along-path reach cap in
# valley_level_pool.py (MAX_BACKWATER_UPSTREAM_METERS) bounds the walk's
# LENGTH without bounding its AREA -- a full fan-out across a plain stays
# within 150 m of the anchor and still covers acres. This bounds the area
# directly, as the second half of that pair.
#
# Truncation drops the FARTHEST-UPSTREAM backwater cells first, by
# along-path distance from the anchor (never dam-band cells, which are the
# structure line itself), and sets the truncated_by_cap flag so a
# truncated candidate is never mistaken for one the terrain bounded on its
# own. Dropping by descending along-path distance preserves connectivity
# by construction: a cell's distance is strictly greater than its
# downstream parent's, so any suffix that is removed leaves every
# surviving cell's path to the anchor intact.
#
# 2.0 acres is a first calibration -- roughly the largest single water
# feature that reads as one survey area on a small farm. NOT validated
# beyond the reference property. CONFIGURABLE.
MAX_WATER_ZONE_AREA_ACRES = 2.0

# How many candidate zones a single run may return, across BOTH nomination
# families combined.
#
# Three is a report-scale decision, not a terrain one: the deliverable is
# "here are the places worth walking," and a list long enough to need
# skimming stops being that. It is also the point past which family 2
# (highest remaining flow accumulation) starts nominating genuinely
# marginal ground on a small parcel. Raising it returns more, weaker
# candidates rather than failing; it does not change how any one candidate
# is delineated. NOT validated beyond the reference property.
# CONFIGURABLE.
MAX_WATER_ZONE_CANDIDATES = 3

# How far a keypoint may be from the nearest ELIGIBLE cell and still seed a
# candidate there (family 1).
#
# A keypoint is detected on pure terrain with no eligibility gates at all
# (keypoint_detection.py is deliberately independent of KSOP), so a real
# keypoint routinely lands one or two cells inside a canopy root zone, on a
# road buffer, or just past the contributing-area ceiling. Refusing to
# nominate at all in that case would throw away the single most meaningful
# anchor on the parcel over a few meters of raster edge; snapping to the
# nearest eligible cell keeps the nomination and records the move
# (seed_snapped + the snap distance), and the candidate carries BOTH
# positions so a map can draw the keypoint where it really is and the
# anchor where the pool was actually delineated.
#
# 15 m is three cells on the 5 m DEM -- wide enough to clear a
# mask edge, narrow enough that the snapped anchor is unarguably the same
# landform feature as the keypoint. Beyond it, the honest answer is
# no_eligible_cell_within_snap and no candidate. NOT validated beyond the
# reference property. CONFIGURABLE.
WATER_KEYPOINT_SEED_SNAP_METERS = 15.0

# Minimum distance between a proposed seed and the footprint of any
# candidate already delineated in this run.
#
# Without it, the second-highest keypoint (or the second-highest
# accumulation cell) is usually the cell immediately next to the first, and
# the run returns three near-identical zones on one dam line. This is a
# NOMINATION spacing rule, not a de-duplication rule applied afterwards: it
# is cheaper and more honest to decline to nominate (with the
# too_close_to_candidate_<id> reason code) than to delineate a pool and
# then discard most of it.
#
# 30 m is roughly six cells at 5 m -- past the width of a single small dam
# line, so two seeds that clear it are genuinely different stations even
# when they sit on the same drainage (which is explicitly allowed; see the
# module docstring). It is deliberately NOT as large as
# MAX_BACKWATER_UPSTREAM_METERS: overlapping backwaters between two
# legitimately-separate stations are handled by the overlap trim, not by
# forbidding the second station. NOT validated beyond the reference
# property. CONFIGURABLE.
MIN_WATER_SEED_SEPARATION_METERS = 30.0

# Opening radius for the water zone render fill. DELIBERATELY tiny compared
# with production's 24m: a 0.5-acre zone is roughly 81 cells (~9x9 on a 5m
# grid), and an opening removes features narrower than 2r. At r = 1 cell this
# trims single-cell protrusions and softens the blocky cell-union edge; at
# r = 2 cells it would remove anything under 20m, which on a 9-cell-wide shape
# is most of the zone. No lead erode -- a 0.5-acre zone cannot afford an extra
# cell off every edge. CONFIGURABLE.
WATER_ZONE_RENDER_OPENING_RADIUS_METERS = 5.0

# How far past a tree-cell's own footprint the woody-vegetation hard
# exclusion extends for water zones specifically -- reuses canopy_height_
# data.tree_root_zone_mask() (the SAME threshold-then-dilate raster
# operation production_area.py's own woody-vegetation gate uses), but at
# its OWN buffer distance, not canopy_height_data.TREE_ROOT_ZONE_BUFFER_
# METERS directly: a separate, independently-named constant even though
# it happens to currently equal the same 10ft value -- same "constants
# stay separate even when numerically identical" convention this
# pipeline already applies elsewhere (e.g. this module's own
# MIN_BOUNDARY_SETBACK_METERS/MIN_DOWNSTREAM_CLEARANCE_METERS history),
# so a future retune of one doesn't silently couple to the other.
# CONFIGURABLE.
WATER_ZONE_CANOPY_BUFFER_METERS = 3.048  # 10ft

# The road exclusion deliberately has NO per-module buffer constant of its
# own (unlike WATER_ZONE_CANOPY_BUFFER_METERS above): this module's road
# gate reads farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS -- the single
# definition every consumer of the existing-road exclusion shares -- via
# _fetch_road_exclusion_union_utm()'s own default, same as production/
# ceiling/exclusion_zones. A separate per-module water road-buffer
# constant (3.048m/10ft) used to live here on the general separate-
# constants principle; it was DELETED because it answered the same single question
# ("how far should a proposed feature stay off an existing road?") as the
# shared constant, not a different one -- see ROAD_EXCLUSION_BUFFER_
# METERS's own docstring for the full reasoning, and for when a split
# would become legitimate again.
#
# THE PRODUCTION-OVERLAP EXCLUSION USED TO LIVE HERE, as a constant
# (a 5.0 m setback distance), its own function parameter, and gate 6 of
# compute_water_eligible_cells().
# All three are DELETED -- not zeroed. Water-zone cells inside (or within a
# build margin of) a production area's render fill are eligible now.
#
# DELETED, NOT ZEROED, because the PREMISE changed rather than the value
# being wrong. The constant answered "how much ground must sit between a
# pond wall and worked production ground," which was a coherent question
# only while production overlap was disqualifying at all. It is not: the
# two uses genuinely compete for the same ground, and which wins is the
# USER'S call in the interactive design, not a generation-time rule that
# silently deletes the best drainage on any parcel whose production
# footprint covers the valley floor. In the follow-up scoring branch,
# production overlap becomes a scoring FACTOR reading the same
# production_area_relationships this module already attaches.
#
# This is the same gate-to-preference move the pipeline already made for
# gravity (this module's own relationships, scored not gated), for soil
# (production_suitability.py's own docstring) and for prime farmland. It
# is the OPPOSITE of what happened to MIN_BOUNDARY_SETBACK_METERS above,
# which was kept and zeroed precisely because its question is still real
# and only its value was wrong -- keep the two cases distinct when reading
# this module's history.
#
# What would justify reintroducing it: a real, parcel-specific rule about
# construction clearance that this pipeline has data for. "It feels
# untidy for the two footprints to touch" is not that rule.

# Sentinel distinguishing "the canopy/road check genuinely ran" from
# "never checked at all" -- same convention production_area.py's own
# _CANOPY_CHECK_UNCHECKED/_ROAD_CHECK_UNCHECKED use, for the same reason:
# compute_water_eligible_cells()/find_candidate_zones() stay pure,
# network-free, and directly unit-testable with these gates simply
# skipped by default (see test_water_candidate_zones.py, which never
# fetches either), while identify_water_system_candidate_zones() (the
# real network entry point) is what actually makes the canopy gate
# MANDATORY -- by always calling get_required_tree_root_zone_mask_utm()
# (which itself either returns a real mask or raises/propagates, see that
# function's own docstring), never leaving this sentinel active on the
# real path. The road gate stays genuinely optional even at the network
# layer (graceful degrade on fetch failure), same as production's own
# check_roads handling.
_CANOPY_CHECK_UNCHECKED = object()
_ROAD_CHECK_UNCHECKED = object()

# A THIRD, separate sentinel -- for identify_water_system_candidate_zones()'s
# road_exclusion_union_utm PARAMETER, distinguishing "the caller supplied
# nothing, self-fetch" from "the caller supplied a real None". It is NOT
# _ROAD_CHECK_UNCHECKED above, and the difference matters: a real None is
# farm_roads_data.get_road_exclusion_union_utm()'s own clean result --
# "checked, and genuinely no mapped road nearby", the common case on a rural
# parcel -- so it must be REUSED (road_data_available True, no second fetch),
# while _ROAD_CHECK_UNCHECKED means the check never ran at all and the gate
# is skipped. Collapsing the two would reintroduce the redundant fetch on
# exactly the parcels the pass-through exists to spare, which is the trap
# exclusion_zones._ROAD_UNION_NOT_SUPPLIED was introduced to avoid on the
# other side of the same union.
_ROAD_UNION_NOT_SUPPLIED = object()


# =====================================================================
# REASON CODES
# =====================================================================
# An enumeration of the strings this module uses to say WHY a nomination
# did not become a candidate, and WHAT was done to one that did. They are
# module-level constants, deliberately not ad-hoc literals at each site:
# a caller (or a test) that wants to react to "the seed was snapped" must
# be able to compare against a name rather than re-type a string, and a
# retitled code must break loudly at import rather than quietly stop
# matching.
#
# WHY THIS EXISTS AT ALL. "No water zones found" is the least useful
# possible answer to a farmer looking at an empty map. Every path that
# returns nothing (or returns fewer candidates than the cap allows) now
# records which specific rule stopped it and where, and
# find_candidate_zones()'s diagnostics dict carries a per-keypoint outcome
# list plus a family-2 seed log. This is intended as a CONVENTION other
# KSOP modules can adopt for their own empty/partial results, which is why
# the names describe the rule that fired rather than the module that fired
# it.
#
# Outcomes -- exactly one per nomination attempt:
REASON_NOMINATED = "nominated"
REASON_NO_ELIGIBLE_CELL_WITHIN_SNAP = "no_eligible_cell_within_snap"
REASON_BELOW_MIN_AREA = "below_min_area"
REASON_NO_SERVICE_RELATIONSHIP = "no_service_relationship"
REASON_EMPTY_AFTER_BOUNDARY_CLIP = "empty_after_boundary_clip"
REASON_EMPTY_AFTER_OVERLAP_TRIM = "empty_after_overlap_trim"
REASON_CANDIDATE_CAP_REACHED = "candidate_cap_reached"

# Flags -- zero or more per nomination, recorded alongside the outcome and
# carried onto the produced zone dict:
FLAG_SEED_SNAPPED = "seed_snapped"
FLAG_TRUNCATED_BY_BOUNDARY = "truncated_by_boundary"
FLAG_TRUNCATED_BY_CAP = "truncated_by_cap"
FLAG_OVERLAP_TRIMMED = "overlap_trimmed"
FLAG_ABUTMENT_NOT_FOUND_LEFT = "abutment_not_found_left"
FLAG_ABUTMENT_NOT_FOUND_RIGHT = "abutment_not_found_right"
FLAG_BACKWATER_DISTANCE_LIMITED = "backwater_distance_limited"

# Parameterised outcome: which specific already-delineated candidate the
# seed was too close to. A function rather than a bare constant because
# the id is part of the answer -- "too close to something" is not
# actionable, "too close to candidate 0" is.
_REASON_TOO_CLOSE_TO_CANDIDATE_PREFIX = "too_close_to_candidate_"


def reason_too_close_to_candidate(candidate_id: int) -> str:
    """The too_close_to_candidate_<id> reason code for a seed rejected by
    the MIN_WATER_SEED_SEPARATION_METERS spacing rule -- see that
    constant's own docstring."""
    return f"{_REASON_TOO_CLOSE_TO_CANDIDATE_PREFIX}{int(candidate_id)}"


# Provenance values for a zone's own nominated_by field.
NOMINATED_BY_KEYPOINT = "keypoint"
NOMINATED_BY_ACCUMULATION = "accumulation"

WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES = (
    "This identifies a general candidate zone for water-system "
    "infrastructure (keyline plowing patterns, pond/dam potential, ram "
    "pump routing) — the ground a LEVEL POOL at a fixed reference "
    "waterline would cover upstream of one anchor cell, plus the "
    "dam-axis band across the valley at that cell. The anchor was "
    "nominated either from a detected keypoint or from the highest "
    "remaining flow accumulation (see properties.nominated_by), and "
    "cleared the eligibility gates: contributing-area ceiling, "
    "on-parcel, plausible service distance to a candidate production "
    "area, clear of the woody-vegetation root zone and mapped roads. "
    "THE REFERENCE WATERLINE IS A MEASURING STICK, NOT A PROPOSED DAM "
    "HEIGHT: every candidate is delineated at the same height so their "
    "terrain can be compared, and no dam of that (or any) height is "
    "being recommended. NO STORAGE VOLUME IS REPORTED ANYWHERE, "
    "deliberately — the per-station flooded widths and cross-sectional "
    "areas are relative ranking measurements off a coarse public DEM, "
    "and turning them into a capacity figure would be a fabricated "
    "engineering number. "
    "Elevation relative to the production area a zone could serve is NOT "
    "a generation-time filter: a candidate sitting BELOW its nearest "
    "production area (which would need a pump to deliver water uphill) "
    "is still reported, same as one sitting comfortably above it (which "
    "could gravity-feed) — see properties.production_area_relationships "
    "for the real elevation differential/gradient this candidate was "
    "measured against, and water_suitability.py for how that's turned "
    "into a real, weighted preference score rather than a pass/fail "
    "gate. Overlap with a candidate production area is likewise NOT a "
    "filter — production ground and water ground genuinely compete, and "
    "which wins is the designer's call, not this layer's. This is NOT a "
    "specific pond or dam site: actual siting requires separate, more "
    "detailed analysis (a real survey, dam wall geometry, spillway "
    "design, geotechnical work at the abutments) not covered here. It "
    "also inherits the limitations of the layers it's built on — "
    "DEM-derived flow accumulation, D8 hydrology and a slope-only "
    "production-area heuristic — so treat this as a starting area to "
    "walk and ground-truth, not a final answer."
)


def compute_water_eligible_cells(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    max_valley_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    flow_accumulation: Optional[np.ndarray] = None,
) -> np.ndarray:
    """
    Cell-based STEP 1: takes the raw flow-accumulation grid (self-computed
    from `dem` via valley_delineation.get_flow_accumulation_for_dem() when
    `flow_accumulation` is not supplied — the same contributing-cell-count
    grid delineate_valleys() thresholds/traces internally) and applies a
    set of HARD EXCLUSION GATES. A cell is eligible unless ANY of these
    holds:

      1. Its own contributing area exceeds max_valley_contributing_area_
         acres -- an ABSOLUTE ceiling, NOT a boundary-dependent percentile
         band and NOT a lower threshold. Contributing area in acres is a
         physical property of the terrain (flow_accumulation_cells value *
         cell area) and does not depend on where a boundary was drawn, so
         the gate is absolute: it cannot move when the boundary moves,
         which was the core bug in the old percentile band (a percentile
         is defined relative to its population -- the on-parcel cells --
         so moving the boundary moved the selected band even though the
         terrain was unchanged). There is deliberately NO lower bound: the
         deliverable is a survey area, not a pass/fail on pond viability,
         so a cell is never excluded merely for LOW contributing area --
         the best available site is reported rather than nothing. See
         MAX_VALLEY_CONTRIBUTING_AREA_ACRES's own docstring for the NRCS
         CPS 378 / siltation reasoning behind the 20-acre ceiling.

      2. It is OFF-PARCEL (not boundary_polygon_utm.contains(cell center))
         -- a hard exclusion in its own right. This is a SEPARATE,
         independent test from the boundary setback below: even with
         min_boundary_setback_meters == 0.0 (the current value), an
         off-parcel cell is still excluded here. The setback is an
         additional, and now inert, test on top of this one, NOT a
         replacement for it.

      3. It FAILS the max-service-distance gate: it is NOT within
         max_service_distance_meters of any production area's polygon_utm.
         A pond too far from the ground it serves is useless regardless of
         how good the drainage is, so this remains a real generation-time
         filter. There is deliberately NO minimum-service-distance gate:
         an earlier 10 m "too close to a production edge" rule was removed
         (water zones may butt right up to production -- there is no siting
         reason to hold a pond off a field edge), so a cell at ANY distance
         at or below max_service_distance_meters (distance == 0 included,
         i.e. inside/touching a patch) passes here. Production overlap is
         still excluded -- by the separate production-exclusion gate (6),
         not by this one. This gate only tests whether ANY production area
         is within range -- it does NOT pick a "best" one; that's
         find_candidate_zones()'s own whole-zone
         scoring now, computed once per surviving cluster from a single
         representative point, not per cell (see that function's own
         docstring for why per-cell tagging + per-cluster aggregation was
         removed in favor of this).

      4. It is INSIDE the woody-vegetation root zone: canopy_root_zone_mask_
         utm (canopy_height_data.tree_root_zone_mask()'s own output, at
         this module's own WATER_ZONE_CANOPY_BUFFER_METERS -- see that
         constant's docstring) must be False at this cell. Reuses
         production_area.py's already-validated canopy gate directly
         (same fetch, same threshold-then-dilate raster mask, just a
         separately-tunable buffer distance) rather than reimplementing
         it -- pure raster AND, never vectorized into a polygon buffer/
         difference (see canopy_height_data.py's own module docstring for
         why that path caused the original hydric-soil sliver-
         fragmentation bug). If canopy_root_zone_mask_utm is left at its
         default sentinel (_CANOPY_CHECK_UNCHECKED -- "never checked at
         all," the same convention production_area.py's own
         _CANOPY_CHECK_UNCHECKED uses), this gate is skipped entirely --
         see identify_water_system_candidate_zones() for how the real
         network entry point makes this gate MANDATORY instead, by always
         fetching-or-raising before this function is ever called.

      5. It is INSIDE road_exclusion_union_utm: real road/right-of-way
         vector geometry (farm_roads_data.get_road_exclusion_union_utm()'s
         own output, at the shared farm_roads_data.ROAD_EXCLUSION_BUFFER_
         METERS -- the single definition of "how far off an existing road"
         every consumer reads; this module's former separate copy was
         deleted, see the constants section above),
         tested via cell-center .contains(), same pattern production_
         area.py's own existing-road gate uses -- a genuinely NEW
         exclusion for this module (water zones haven't excluded roads
         before). A real None (checked, no roads found nearby -- the
         common, clean case) or the default sentinel (_ROAD_CHECK_
         UNCHECKED, "never checked at all") both mean this gate is
         skipped; only a real fetched union polygon excludes anything.
         Unlike canopy, this stays OPTIONAL even on the real network path
         (graceful degrade on fetch failure, mirroring production_area.py's
         own check_roads handling) -- see module docstring.

    THERE IS NO GATE 6. A production-overlap exclusion used to sit here (a
    cell inside any production area's render_fill_polygon_utm, plus a build
    margin, was excluded outright). It is DELETED -- gate, constant and
    parameter -- and a cell is now eligible regardless of what production
    footprint it sits in. See the constants section above and the module
    docstring for the full reasoning; the short version is that production
    overlap is a competing-use question the user answers in the interactive
    design, and turning it into a scoring factor is the follow-up branch's
    job. production_areas is still a REQUIRED argument: gate 3 above and
    find_candidate_zones()'s own production_area_relationships both need
    it.

    The boundary setback (min_boundary_setback_meters) is still applied as
    an additional test on top of gate 2, but its value is 0.0 (see
    MIN_BOUNDARY_SETBACK_METERS), so "distance < 0.0" is never true and the
    setback is inert. Zeroing it does NOT weaken the off-parcel exclusion
    (gate 2), which is a separate containment test.

    There is NO percentile band, NO minimum contributing area, and NO
    survey-buffer dilation here anymore -- the eligible mask is a broad
    per-cell gate result, not a one-cell-wide hairline, so nothing needs
    widening before clustering. Every eligible cell independently cleared
    the absolute contributing-area ceiling; there is no dilation step that
    could admit a cell above the ceiling by adjacency.

    Elevation/gradient is deliberately NOT a gate here (see module
    docstring's "gravity is a preference, not a gate" framing) — do not
    add a min-gradient or elevation-band exclusion; a cell otherwise
    eligible is never excluded for sitting below its best-matching
    production area.

    Returns eligible_mask: np.ndarray[bool], same shape as dem['array'].
    """
    flow_accumulation_cells = (
        get_flow_accumulation_for_dem(dem) if flow_accumulation is None else flow_accumulation
    )
    area_per_cell = cell_area_acres(dem)
    max_contributing_cells = max_valley_contributing_area_acres / area_per_cell
    # Absolute contributing-area ceiling: eligible cells are those AT OR
    # BELOW the ceiling (no lower bound). NaN accumulation compares False,
    # so NaN cells are already excluded here as well as by the elevation
    # NaN guard below.
    ceiling_mask = flow_accumulation_cells <= max_contributing_cells

    rows, cols = dem["array"].shape
    eligible_mask = np.zeros((rows, cols), dtype=bool)
    array = dem["array"]

    boundary_prepared = prep(boundary_polygon_utm)
    boundary_line = boundary_polygon_utm.boundary

    canopy_checked = canopy_root_zone_mask_utm is not _CANOPY_CHECK_UNCHECKED

    road_union = (
        road_exclusion_union_utm
        if road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED and road_exclusion_union_utm is not None
        else None
    )
    road_prepared = prep(road_union) if road_union is not None else None

    for r, c in np.argwhere(ceiling_mask):
        r, c = int(r), int(c)
        elevation = float(array[r, c])
        if np.isnan(elevation):
            continue

        x, y = pixel_center_xy(dem, r, c)
        point = Point(x, y)

        if not boundary_prepared.contains(point):
            continue
        if point.distance(boundary_line) < min_boundary_setback_meters:
            continue
        if canopy_checked and canopy_root_zone_mask_utm[r, c]:
            continue
        if road_prepared is not None and road_prepared.contains(point):
            continue

        within_service_distance = False
        for patch in production_areas:
            distance = point.distance(patch["polygon_utm"])
            if distance > max_service_distance_meters:
                continue
            within_service_distance = True
            break

        if not within_service_distance:
            continue

        eligible_mask[r, c] = True

    return eligible_mask


def _zone_production_area_relationships(
    representative_point: Point,
    representative_elevation_m: float,
    production_areas: list[dict],
    max_service_distance_meters: float,
) -> list[dict]:
    """
    Whole-zone version of the old per-cell "best production-area
    relationship" tagging + per-cluster median aggregation: computed ONCE
    per surviving cluster from a single representative point/elevation
    (see find_candidate_zones()'s own docstring) rather than rolled up
    (median) across every member cell's own per-cell tag. Same output
    shape the old aggregation produced, so every downstream consumer
    (zones_to_geojson(), water_suitability.py) is unaffected by this
    change:

        {
            'production_area_id': int,
            'elevation_differential_m': float,  # + = zone sits above the
                                                  # production area
                                                  # (gravity-favorable);
                                                  # - = below (would need a
                                                  # pump)
            'distance_m': float,
            'gradient_pct': float,               # elevation_differential_m
                                                  # / distance_m * 100 —
                                                  # can be negative
            'above_production_area': bool,
        }
    sorted by elevation_differential_m descending (most gravity-favorable
    first), same convention as before.

    Only production areas within max_service_distance_meters are included
    (the same max-service-distance gate compute_water_eligible_cells()
    applies; there is no minimum-service-distance gate) -- a zone
    whose representative point falls outside every production area's
    service-distance window returns [] (see find_candidate_zones()'s own
    handling of this case: such a zone is dropped, since there's no single
    headline "served" relationship left to report for it).
    """
    relationships = []
    for patch in production_areas:
        distance = representative_point.distance(patch["polygon_utm"])
        if distance > max_service_distance_meters:
            continue

        elevation_differential_m = representative_elevation_m - patch["representative_elevation_m"]
        gradient_pct = (elevation_differential_m / distance * 100) if distance > 0 else 0.0
        relationships.append(
            {
                "production_area_id": patch["id"],
                "elevation_differential_m": round(elevation_differential_m, 2),
                "distance_m": round(distance, 1),
                "gradient_pct": round(gradient_pct, 2),
                "above_production_area": elevation_differential_m > 0,
            }
        )

    relationships.sort(key=lambda r: -r["elevation_differential_m"])
    return relationships


def _flow_arrays_for_dem(
    dem: dict,
    filled=None,
    flow_to_row=None,
    flow_to_col=None,
    flow_accumulation=None,
):
    """
    Resolves the four D8 hydrology arrays this module needs, self-computing
    only what was not supplied and FORWARDING what was into each derived
    step -- the same self-computing-override pattern keypoint_detection.
    detect_keypoints() uses, applied to the same four arrays so the two can
    share one caller's copies.

    The forwarding matters more than the convenience: fill_depressions() and
    compute_flow_direction() are per-cell Python loops, so a path that
    accepts `filled` and then recomputes it from `dem` anyway has not saved
    anything, and worse, a caller that supplied a DIFFERENT filled array
    (a test fixture, say) would silently have it ignored. Nothing here
    re-derives an argument it was handed.

    Returns (filled, flow_to_row, flow_to_col, flow_accumulation).
    """
    if filled is None:
        filled = fill_depressions(dem["array"])
    if flow_to_row is None or flow_to_col is None:
        flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    if flow_accumulation is None:
        flow_accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)
    return filled, flow_to_row, flow_to_col, flow_accumulation


def _nearest_eligible_cell(
    dem: dict,
    eligible_mask: np.ndarray,
    cell: tuple[int, int],
    snap_radius_meters: float,
) -> tuple[Optional[tuple[int, int]], float]:
    """
    The eligible cell nearest to `cell` (by real ground distance between
    cell centers) within snap_radius_meters, as (cell_or_None, distance).

    Searches only the square cell window the radius can reach, so cost is
    bounded by the radius rather than by grid size. Ties break
    deterministically on (row, col) so the same DEM always snaps the same
    way. Returns (None, inf) when nothing eligible is in range -- the
    honest no_eligible_cell_within_snap answer, never a widened search.
    """
    rows, cols = eligible_mask.shape
    px, py = dem["resolution_meters"]
    reach_rows = int(math.ceil(snap_radius_meters / float(py)))
    reach_cols = int(math.ceil(snap_radius_meters / float(px)))
    r0, c0 = cell
    best = None
    best_distance = float("inf")
    for r in range(max(0, r0 - reach_rows), min(rows, r0 + reach_rows + 1)):
        for c in range(max(0, c0 - reach_cols), min(cols, c0 + reach_cols + 1)):
            if not eligible_mask[r, c]:
                continue
            distance = math.hypot((c - c0) * float(px), (r - r0) * float(py))
            if distance > snap_radius_meters:
                continue
            if distance < best_distance or (distance == best_distance and best is not None and (r, c) < best):
                best = (r, c)
                best_distance = distance
    return best, best_distance


def _zone_footprint(dem: dict, cells, boundary_polygon_utm: Polygon):
    """The real cell-union footprint of `cells`, clipped to the parcel
    boundary -- raster_grid.cell_union_footprint() then .intersection(),
    the same construction every other zone footprint in this pipeline
    uses."""
    mask = np.zeros(dem["array"].shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return mask, cell_union_footprint(dem, mask).intersection(boundary_polygon_utm)


def _overlap_fraction_pct(cells, dem: dict, checked: bool, mask_utm=None, prepared_union=None) -> Optional[float]:
    """
    Percentage of `cells` whose centers fall inside a raster mask (canopy)
    or a prepared vector union (roads) -- a REPORTED PROPERTY, never a
    filter.

    `checked` is what separates the two things a caller must never
    conflate: None means THE CHECK NEVER RAN, and 0.0 means it ran and
    found nothing. That distinction is load-bearing for the road gate in
    particular, where a real None union is farm_roads_data.get_road_
    exclusion_union_utm()'s own CLEAN answer ("checked, and genuinely no
    mapped road nearby" -- the common case on a rural parcel), not a
    missing check. Reporting None there would tell a narrative "we don't
    know" about a parcel we do know is clear, which is the same trap
    _ROAD_UNION_NOT_SUPPLIED exists to avoid one layer up.
    """
    if not checked:
        return None
    if not cells:
        return 0.0
    hits = 0
    for r, c in cells:
        if mask_utm is not None:
            if mask_utm[r, c]:
                hits += 1
                continue
        if prepared_union is not None:
            x, y = pixel_center_xy(dem, r, c)
            if prepared_union.contains(Point(x, y)):
                hits += 1
    return round(hits / len(cells) * 100.0, 1)


def find_candidate_zones(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    max_valley_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_water_zone_area_acres: float = MIN_WATER_ZONE_AREA_ACRES,
    max_water_zone_area_acres: float = MAX_WATER_ZONE_AREA_ACRES,
    max_water_zone_candidates: int = MAX_WATER_ZONE_CANDIDATES,
    keypoint_seed_snap_meters: float = WATER_KEYPOINT_SEED_SNAP_METERS,
    min_water_seed_separation_meters: float = MIN_WATER_SEED_SEPARATION_METERS,
    pool_reference_height_meters: float = POOL_REFERENCE_HEIGHT_METERS,
    abutment_search_half_width_meters: float = ABUTMENT_SEARCH_HALF_WIDTH_METERS,
    max_backwater_upstream_meters: float = MAX_BACKWATER_UPSTREAM_METERS,
    canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    keypoints: Optional[list[dict]] = None,
    valleys: Optional[list[dict]] = None,
    filled: Optional[np.ndarray] = None,
    flow_to_row: Optional[np.ndarray] = None,
    flow_to_col: Optional[np.ndarray] = None,
    flow_accumulation: Optional[np.ndarray] = None,
    diagnostics: Optional[dict] = None,
) -> list[dict]:
    """
    NOMINATION + LEVEL-POOL DELINEATION (Step 3) — see the module docstring
    for why this takes an already-fetched `dem` plus already-computed
    production_areas rather than pre-traced valley branches, and for why
    elevation/gradient is not one of the filters applied here
    (min_gravity_gradient is not part of this signature at all — it's
    water_suitability.py's scoring concern, not a generation-time
    parameter).

    THE PIPELINE, in order:

      1. Build the per-cell eligibility mask
         (compute_water_eligible_cells() — the absolute contributing-area
         ceiling, on-parcel containment, the inert boundary setback, the
         canopy root zone, the road exclusion, and max service distance;
         there is no production-overlap gate any more, see that function's
         own docstring).

      2. FAMILY 1 — KEYPOINT-NOMINATED CANDIDATES. Keypoints are ordered by
         contributing_acres DESCENDING. That is deliberately NOT
         keypoint_detection.py's own ordering, which ranks by slope_drop_pct
         (how sharp the inflection is) and is right for what that layer
         delivers; it is untouched here. For a WATER system the question is
         how much watershed arrives, so catchment orders the nominations.
         Per keypoint:
           a. Seed at the keypoint's own cell if the eligibility mask is
              True there; otherwise the nearest eligible cell within
              keypoint_seed_snap_meters (recording FLAG_SEED_SNAPPED and the
              snap distance); otherwise
              REASON_NO_ELIGIBLE_CELL_WITHIN_SNAP and no candidate.
           b. Skip with reason_too_close_to_candidate(id) if the seed lies
              within min_water_seed_separation_meters of an
              already-delineated candidate's footprint.
           c. Delineate the level pool at the seed and finish it (below).

      3. FAMILY 2 — ACCUMULATION-NOMINATED CANDIDATES, until the candidate
         cap is reached or no seed remains: the anchor is the
         highest-flow-accumulation eligible cell that is unclaimed and not
         within the separation distance of any existing candidate's
         footprint. Delineated identically — the two families differ only
         in how the anchor was chosen, never in what is done with it.

    FINISHING A DELINEATED POOL (identical for both families):

      * CLIP TO THE PARCEL BOUNDARY, AND TO NOTHING ELSE. Not canopy, not
        roads, not production. A pool clipped by a root-zone mask would
        MISREPRESENT THE PHYSICS: water does not stop at the edge of a tree
        canopy, and a footprint drawn as if it did would tell a farmer the
        pool is smaller than it is. Canopy and road overlap are computed
        and attached as REPORTED PROPERTIES instead (canopy_overlap_pct /
        road_overlap_pct, unweighted in this branch — the scoring branch
        reads them). Those masks still gate ELIGIBILITY, i.e. where an
        anchor may be nominated; they never reshape a pool.
      * If the boundary clip removed anything, FLAG_TRUNCATED_BY_BOUNDARY
        is set — loudly, because backwater reaching the property line means
        flooding a neighbour, which is among the most important things this
        survey can find. It is a flag, never a rejection.
      * Area floor: below min_water_zone_area_acres → REASON_BELOW_MIN_AREA.
      * Area cap: above max_water_zone_area_acres → drop the
        farthest-upstream backwater cells (by along-path distance from the
        anchor; dam-band cells are never dropped) until under the cap, and
        set FLAG_TRUNCATED_BY_CAP.
      * OVERLAP TRIM: any cell already claimed by an earlier candidate is
        removed, then only the connected component containing the anchor is
        kept, and FLAG_OVERLAP_TRIMMED is set. NON-OVERLAP BETWEEN
        CANDIDATES IS AN INVARIANT and is asserted before returning. The
        trim runs for BOTH families, not just family 2: the separation rule
        keeps SEEDS apart, but two seeds a legitimate 40 m apart on one
        drainage can still delineate overlapping backwaters, and the
        invariant has to hold across every pair.
        The component retention uses 8-CONNECTIVITY, matching the D8 flow
        adjacency the pool was built from — a 4-connected test would
        declare a genuinely water-connected diagonal step to be two
        separate pools.
      * A zone whose representative point clears no production area within
        max_service_distance_meters is dropped, now with
        REASON_NO_SERVICE_RELATIONSHIP — there is no single headline
        "served" relationship left to report for it.

    keypoints/valleys/filled/flow_to_row/flow_to_col/flow_accumulation are
    OPTIONAL OVERRIDES in the same self-computing family the rest of this
    pipeline uses, each independent of the others. When `keypoints` is None
    this function calls keypoint_detection.detect_keypoints() itself and
    FORWARDS every override it holds into that call (dem, the boundary
    polygon, valleys, the filled array and all three flow arrays), so the
    self-compute path never re-derives an array that was passed in. A
    caller that already detected keypoints for this exact DEM (build_
    pipeline_context() does, once per run) passes them straight through and
    no detection runs here at all.

    diagnostics, if a dict is passed, is populated IN PLACE with the
    nomination record — a per-keypoint outcome list and a family-2 seed log,
    each entry carrying its reason code and flags — plus the eligible-cell
    count and the resulting candidate count. It is a reporting hook only and
    does not affect the return value, the same convention keypoint_
    detection.detect_keypoints()'s own diagnostics parameter follows.

    Returns a list of AT MOST max_water_zone_candidates zone dicts (or []),
    ordered by nomination (family 1 by catchment descending, then family 2
    by accumulation descending) — NOT by suitability, which is
    water_suitability.py's ranking:

        {
            'id': int,                     # 0-based, nomination order
            'nominated_by': str,           # NOMINATED_BY_KEYPOINT / _ACCUMULATION
            'keypoint_id': int or None,    # family 1 only
            'valley_id': int or None,      # family 1 only
            'keypoint_rowcol': (row, col) or None,   # where the keypoint IS
            'keypoint_point_utm': shapely Point or None,
            'seed_snapped': bool,
            'seed_snap_distance_m': float,
            'anchor_rowcol': (row, col),   # where the pool was delineated
            'anchor_point_utm': shapely Point,
            'anchor_elevation_m': float,   # RAW elevation at the anchor
            'level_pool': dict,            # the measurement/geometry block —
                #   waterline, reference height, per-station flooded width and
                #   flooded cross-sectional area, dam-band width, pool/band cell
                #   counts. NO VOLUME, here or anywhere downstream (see
                #   valley_level_pool.py's module docstring).
            'abutments': dict,             # per-side {found, lateral_distance_m,
                #   rowcol, elevation_m, searched_distance_m, left_grid}
            'abutment_found_left': bool,
            'abutment_found_right': bool,
            'flags': [str, ...],           # the FLAG_* codes that fired
            'truncated_by_boundary': bool,
            'truncated_by_cap': bool,
            'overlap_trimmed': bool,
            'canopy_overlap_pct': float or None,   # reported, unweighted
            'road_overlap_pct': float or None,     # reported, unweighted
            'served_production_area_ids': [int, ...],
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'render_fill_polygon_utm': shapely Polygon/MultiPolygon,
            'render_fill_geometry_wgs84': GeoJSON geometry dict,
            'production_area_relationships': [...],
            'primary_production_area_relationship': dict,
            'contributing_area_cells': float,   # median across member cells
            'slope_pct': float,                 # median across member cells
            'representative_elevation_m': float,
            'cells': [(row, col), ...],
        }
    """
    if not production_areas:
        if diagnostics is not None:
            diagnostics.update(
                {
                    "eligible_cell_count": 0,
                    "keypoint_outcomes": [],
                    "accumulation_seeds": [],
                    "candidate_count": 0,
                    "keypoints_considered": 0,
                }
            )
        return []

    filled, flow_to_row, flow_to_col, flow_accumulation = _flow_arrays_for_dem(
        dem, filled, flow_to_row, flow_to_col, flow_accumulation
    )

    eligible_mask = compute_water_eligible_cells(
        dem,
        production_areas,
        boundary_polygon_utm,
        max_valley_contributing_area_acres,
        max_service_distance_meters,
        min_boundary_setback_meters,
        canopy_root_zone_mask_utm,
        road_exclusion_union_utm,
        flow_accumulation=flow_accumulation,
    )

    if keypoints is None:
        # Self-compute, forwarding EVERY override this function holds so the
        # nested call re-derives nothing it was handed (see the docstring).
        keypoints = detect_keypoints(
            dem,
            boundary_polygon_utm,
            flow_to_row=flow_to_row,
            flow_to_col=flow_to_col,
            flow_accumulation=flow_accumulation,
            filled=filled,
            valleys=valleys,
        )

    upstream_map = build_upstream_map(flow_to_row, flow_to_col)
    slope_pct_grid = compute_slope_percent(dem["array"], dem["resolution_meters"])
    array = dem["array"]
    grid_shape = array.shape
    area_per_cell = cell_area_acres(dem)

    canopy_checked = canopy_root_zone_mask_utm is not _CANOPY_CHECK_UNCHECKED
    canopy_mask = canopy_root_zone_mask_utm if canopy_checked else None
    # ROAD: "checked" is the sentinel test, NOT "is there a union". A real
    # None is the road fetch's own clean "no mapped road nearby" answer, so
    # the check DID run and road_overlap_pct must read 0.0, never None.
    road_checked = road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED
    road_union = road_exclusion_union_utm if road_checked and road_exclusion_union_utm is not None else None
    road_prepared = prep(road_union) if road_union is not None else None

    zones: list[dict] = []
    claimed: set[tuple[int, int]] = set()
    keypoint_outcomes: list[dict] = []
    accumulation_seeds: list[dict] = []

    def _too_close_candidate_id(seed_cell):
        """The id of the first already-delineated candidate whose own
        footprint sits within min_water_seed_separation_meters of this
        seed, or None."""
        x, y = pixel_center_xy(dem, seed_cell[0], seed_cell[1])
        point = Point(x, y)
        for zone in zones:
            if point.distance(zone["polygon_utm"]) < min_water_seed_separation_meters:
                return zone["id"]
        return None

    def _build_candidate(anchor, provenance, keypoint=None, seed_snapped=False, snap_distance=0.0):
        """Delineates, clips, bounds, trims and packages ONE candidate at
        `anchor`. Returns (zone_or_None, outcome_reason, flags)."""
        flags: list[str] = []
        pool = delineate_level_pool(
            dem,
            filled,
            flow_to_row,
            flow_to_col,
            flow_accumulation,
            upstream_map,
            anchor,
            reference_height_meters=pool_reference_height_meters,
            abutment_search_half_width_meters=abutment_search_half_width_meters,
            max_backwater_upstream_meters=max_backwater_upstream_meters,
        )
        if seed_snapped:
            flags.append(FLAG_SEED_SNAPPED)
        if not pool["abutment_found_left"]:
            flags.append(FLAG_ABUTMENT_NOT_FOUND_LEFT)
        if not pool["abutment_found_right"]:
            flags.append(FLAG_ABUTMENT_NOT_FOUND_RIGHT)
        if pool["backwater_distance_limited"]:
            flags.append(FLAG_BACKWATER_DISTANCE_LIMITED)

        band_cells = set(pool["band_cells"])
        pool_distance = pool["pool_cell_distance_m"]

        # --- boundary clip (and NOTHING else -- see the docstring) -------
        on_parcel_prepared = prep(boundary_polygon_utm)
        kept = []
        for cell in pool["zone_cells"]:
            x, y = pixel_center_xy(dem, cell[0], cell[1])
            if on_parcel_prepared.contains(Point(x, y)):
                kept.append(cell)
        if len(kept) != len(pool["zone_cells"]):
            flags.append(FLAG_TRUNCATED_BY_BOUNDARY)
        if anchor not in kept:
            return None, REASON_EMPTY_AFTER_BOUNDARY_CLIP, flags

        # --- overlap trim against everything already claimed -------------
        if any(cell in claimed for cell in kept):
            flags.append(FLAG_OVERLAP_TRIMMED)
            kept = [cell for cell in kept if cell not in claimed]
            if anchor not in kept:
                return None, REASON_EMPTY_AFTER_OVERLAP_TRIM, flags
            trim_mask = np.zeros(grid_shape, dtype=bool)
            for r, c in kept:
                trim_mask[r, c] = True
            labels, _count = connected_components(trim_mask, connectivity=8)
            anchor_label = labels[anchor[0], anchor[1]]
            kept = [cell for cell in kept if labels[cell[0], cell[1]] == anchor_label]

        # --- area cap: drop the farthest-upstream backwater cells --------
        max_cells = max(1, int(math.floor(max_water_zone_area_acres / area_per_cell + 1e-9)))
        if len(kept) > max_cells:
            flags.append(FLAG_TRUNCATED_BY_CAP)
            band_kept = [cell for cell in kept if cell in band_cells]
            pool_kept = [cell for cell in kept if cell not in band_cells]
            # Nearest-first by along-path distance from the anchor, so the
            # cells dropped are the farthest upstream. A cell's along-path
            # distance is strictly greater than its downstream parent's, so
            # keeping a prefix keeps the survivors connected to the anchor.
            pool_kept.sort(key=lambda cell: (pool_distance.get(cell, float("inf")), cell))
            keep_pool = max(0, max_cells - len(band_kept))
            kept = band_kept + pool_kept[:keep_pool]

        mask, polygon_utm = _zone_footprint(dem, kept, boundary_polygon_utm)
        if polygon_utm.is_empty:
            return None, REASON_EMPTY_AFTER_BOUNDARY_CLIP, flags
        if polygon_utm.area / SQUARE_METERS_PER_ACRE < min_water_zone_area_acres:
            return None, REASON_BELOW_MIN_AREA, flags

        representative_elevation_m = float(np.median([array[r, c] for r, c in kept]))
        representative_point = polygon_utm.centroid
        relationships = _zone_production_area_relationships(
            representative_point,
            representative_elevation_m,
            production_areas,
            max_service_distance_meters,
        )
        if not relationships:
            return None, REASON_NO_SERVICE_RELATIONSHIP, flags

        render_fill_polygon_utm = _render_opening(mask, kept, grid_shape, dem, polygon_utm)
        # Invariant: render_fill_polygon_utm is a subset of polygon_utm (the
        # opening is clipped to it, so this holds by construction) -- assert
        # and raise on violation, matching production_area.cluster_and_gate()'s
        # hard-containment discipline.
        if render_fill_polygon_utm.area > polygon_utm.area * (1 + 1e-9) + 1e-6:
            raise ValueError(
                "find_candidate_zones: render_fill_polygon_utm.area "
                f"({render_fill_polygon_utm.area:.6f} m^2) exceeds polygon_utm.area "
                f"({polygon_utm.area:.6f} m^2) -- the opening's clip to polygon_utm must keep the "
                "drawn fill within the real cell-gated, boundary-clipped footprint."
            )

        cluster_slopes = [
            float(slope_pct_grid[r, c]) for r, c in kept if not np.isnan(slope_pct_grid[r, c])
        ]
        anchor_x, anchor_y = pixel_center_xy(dem, anchor[0], anchor[1])

        zone = {
            "id": len(zones),
            "nominated_by": provenance,
            "keypoint_id": int(keypoint["id"]) if keypoint is not None else None,
            "valley_id": int(keypoint["valley_id"]) if keypoint is not None else None,
            # The keypoint's OWN position and the (possibly snapped) anchor
            # position are carried SEPARATELY: after a snap they legitimately
            # differ, and a map that drew only one of them would either hide
            # where the detected keypoint is or claim the pool was delineated
            # somewhere it was not.
            "keypoint_rowcol": tuple(keypoint["rowcol"]) if keypoint is not None else None,
            "keypoint_point_utm": keypoint["point_utm"] if keypoint is not None else None,
            "seed_snapped": bool(seed_snapped),
            "seed_snap_distance_m": round(float(snap_distance), 2),
            "anchor_rowcol": anchor,
            "anchor_point_utm": Point(anchor_x, anchor_y),
            "anchor_elevation_m": pool["anchor_elevation_m"],
            "level_pool": {
                "waterline_elevation_m": pool["waterline_elevation_m"],
                "reference_height_meters": pool["reference_height_meters"],
                "dam_band_width_m": pool["dam_band_width_m"],
                "valley_axis_unit": pool["valley_axis_unit"],
                "stations": pool["stations"],
                "pool_cell_count": len(pool["pool_cells"]),
                "band_cell_count": len(pool["band_cells"]),
                "delineated_cell_count": len(pool["zone_cells"]),
                "retained_cell_count": len(kept),
                "backwater_distance_limited": pool["backwater_distance_limited"],
            },
            "abutments": pool["abutments"],
            "abutment_found_left": pool["abutment_found_left"],
            "abutment_found_right": pool["abutment_found_right"],
            "flags": flags,
            "truncated_by_boundary": FLAG_TRUNCATED_BY_BOUNDARY in flags,
            "truncated_by_cap": FLAG_TRUNCATED_BY_CAP in flags,
            "overlap_trimmed": FLAG_OVERLAP_TRIMMED in flags,
            "canopy_overlap_pct": _overlap_fraction_pct(kept, dem, canopy_checked, mask_utm=canopy_mask),
            "road_overlap_pct": _overlap_fraction_pct(kept, dem, road_checked, prepared_union=road_prepared),
            "served_production_area_ids": sorted(r["production_area_id"] for r in relationships),
            "polygon_utm": polygon_utm,
            "geometry_wgs84": transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm)),
            "render_fill_polygon_utm": render_fill_polygon_utm,
            "render_fill_geometry_wgs84": transform_geom(
                dem["crs"], "EPSG:4326", mapping(render_fill_polygon_utm)
            ),
            "production_area_relationships": relationships,
            "primary_production_area_relationship": relationships[0],
            "contributing_area_cells": round(
                float(np.median([flow_accumulation[r, c] for r, c in kept])), 2
            ),
            "slope_pct": round(float(np.median(cluster_slopes)) if cluster_slopes else 0.0, 2),
            "representative_elevation_m": representative_elevation_m,
            "cells": kept,
        }
        return zone, REASON_NOMINATED, flags

    # --- FAMILY 1: keypoint-nominated ------------------------------------
    # Catchment ordering, NOT keypoint_detection.py's own slope-drop
    # ordering (see the docstring). Ties break on the keypoint's own id so
    # the same DEM always nominates in the same order.
    ordered_keypoints = sorted(keypoints, key=lambda k: (-float(k["contributing_acres"]), int(k["id"])))
    for keypoint in ordered_keypoints:
        outcome = {
            "keypoint_id": int(keypoint["id"]),
            "valley_id": int(keypoint["valley_id"]),
            "keypoint_rowcol": tuple(keypoint["rowcol"]),
            "contributing_acres": float(keypoint["contributing_acres"]),
            "anchor_rowcol": None,
            "seed_snapped": False,
            "seed_snap_distance_m": 0.0,
            "candidate_id": None,
            "outcome": None,
            "flags": [],
        }
        keypoint_outcomes.append(outcome)

        if len(zones) >= max_water_zone_candidates:
            # Recorded, not silently skipped: "there were more keypoints
            # than the cap allowed" is a real, reportable reason a
            # keypoint produced nothing.
            outcome["outcome"] = REASON_CANDIDATE_CAP_REACHED
            continue

        cell = tuple(int(v) for v in keypoint["rowcol"])
        rows, cols = grid_shape
        if 0 <= cell[0] < rows and 0 <= cell[1] < cols and eligible_mask[cell[0], cell[1]]:
            anchor, snapped, snap_distance = cell, False, 0.0
        else:
            nearest, snap_distance = _nearest_eligible_cell(
                dem, eligible_mask, cell, keypoint_seed_snap_meters
            )
            if nearest is None:
                outcome["outcome"] = REASON_NO_ELIGIBLE_CELL_WITHIN_SNAP
                continue
            anchor, snapped = nearest, True

        outcome["anchor_rowcol"] = anchor
        outcome["seed_snapped"] = snapped
        outcome["seed_snap_distance_m"] = round(float(snap_distance), 2)

        too_close = _too_close_candidate_id(anchor)
        if too_close is not None:
            outcome["outcome"] = reason_too_close_to_candidate(too_close)
            continue

        zone, reason, flags = _build_candidate(
            anchor, NOMINATED_BY_KEYPOINT, keypoint=keypoint, seed_snapped=snapped, snap_distance=snap_distance
        )
        outcome["outcome"] = reason
        outcome["flags"] = flags
        if zone is not None:
            zones.append(zone)
            claimed.update(zone["cells"])
            outcome["candidate_id"] = zone["id"]

    # --- FAMILY 2: accumulation-nominated --------------------------------
    # Highest remaining flow accumulation first. Rebuilt each round rather
    # than precomputed once, because "unclaimed and not too close to an
    # existing candidate" changes as candidates are added.
    eligible_cells = [(int(r), int(c)) for r, c in np.argwhere(eligible_mask)]
    eligible_cells.sort(key=lambda cell: (-float(flow_accumulation[cell[0], cell[1]]), cell))
    exhausted: set[tuple[int, int]] = set()
    while len(zones) < max_water_zone_candidates:
        anchor = None
        for cell in eligible_cells:
            if cell in claimed or cell in exhausted:
                continue
            if _too_close_candidate_id(cell) is not None:
                continue
            anchor = cell
            break
        if anchor is None:
            break
        exhausted.add(anchor)

        seed_log = {
            "anchor_rowcol": anchor,
            "flow_accumulation_cells": float(flow_accumulation[anchor[0], anchor[1]]),
            "candidate_id": None,
            "outcome": None,
            "flags": [],
        }
        accumulation_seeds.append(seed_log)

        zone, reason, flags = _build_candidate(anchor, NOMINATED_BY_ACCUMULATION)
        seed_log["outcome"] = reason
        seed_log["flags"] = flags
        if zone is not None:
            zones.append(zone)
            claimed.update(zone["cells"])
            seed_log["candidate_id"] = zone["id"]

    # NON-OVERLAP IS AN INVARIANT, not an expectation: every candidate's
    # cells were removed from every later candidate's, so two candidates
    # sharing a cell means the trim (or the claim bookkeeping) is broken and
    # the footprints on the map would double-count ground.
    seen_cells: set[tuple[int, int]] = set()
    for zone in zones:
        overlap = seen_cells.intersection(zone["cells"])
        if overlap:
            raise ValueError(
                f"find_candidate_zones: candidate {zone['id']} shares {len(overlap)} cell(s) with an "
                f"earlier candidate (e.g. {sorted(overlap)[0]}) -- candidate footprints must not overlap"
            )
        seen_cells.update(zone["cells"])

    if diagnostics is not None:
        diagnostics.update(
            {
                "eligible_cell_count": int(eligible_mask.sum()),
                "keypoints_considered": len(ordered_keypoints),
                "keypoint_outcomes": keypoint_outcomes,
                "accumulation_seeds": accumulation_seeds,
                "candidate_count": len(zones),
            }
        )

    _LOGGER.info(
        "water candidate nomination: eligible_cells=%d keypoints=%d candidates=%d (keypoint=%d accumulation=%d)",
        int(eligible_mask.sum()),
        len(ordered_keypoints),
        len(zones),
        sum(1 for z in zones if z["nominated_by"] == NOMINATED_BY_KEYPOINT),
        sum(1 for z in zones if z["nominated_by"] == NOMINATED_BY_ACCUMULATION),
    )
    return zones


def _render_opening(sub_mask, cells, grid_shape, dem, polygon_utm):
    """
    Bounded morphological OPENING of the zone's own cell mask, clipped to
    polygon_utm -- the display fill. Disc erode-then-dilate at
    WATER_ZONE_RENDER_OPENING_RADIUS_METERS with NO lead erode (a ~0.5-acre
    zone cannot afford an extra cell off every edge). Same construction
    production_area.cluster_and_gate() uses for its own render fill, minus
    the lead erode and at a much smaller radius.

    The opening softens the blocky cell-union edge and trims single-cell
    protrusions; it can also sever a genuinely too-narrow pinch, so the
    result may be a MultiPolygon (acceptable). A zone thinner than the
    opening radius throughout erodes to nothing -- in that case fall back to
    polygon_utm (non-empty) and log once.
    """
    radius_cells = waist_erosion_radius_cells(dem, WATER_ZONE_RENDER_OPENING_RADIUS_METERS)
    opened = binary_dilate(
        eroded_cell_mask(cells, grid_shape, dem, WATER_ZONE_RENDER_OPENING_RADIUS_METERS, element="disc"),
        radius_cells,
        element="disc",
    )
    if opened.any():
        return cell_union_footprint(dem, opened).intersection(polygon_utm)

    _LOGGER.warning(
        "find_candidate_zones: the WATER_ZONE_RENDER_OPENING_RADIUS_METERS=%.1fm opening eroded a "
        "%d-cell zone to nothing; render_fill_polygon_utm falling back to polygon_utm.",
        WATER_ZONE_RENDER_OPENING_RADIUS_METERS,
        len(cells),
    )
    return polygon_utm


def zones_to_geojson(zones: list[dict]) -> dict:
    """Wraps find_candidate_zones() output as the schema-conformant
    GeoJSON FeatureCollection this feature actually delivers
    (layer="water_system_candidate"). This is the UNSCORED diagnostic
    layer — confidence stays CONFIDENCE_LOW/flat here, same as
    production_area.py's own production_areas_to_geojson() before
    production_suitability.py enriches it; water_suitability.py is where
    real, differentiated confidence/suitability_score get added, on this
    same layer, following that exact precedent."""
    features = [
        make_feature(
            feature_id=f"water-system-candidate-{z['id']}",
            geometry=z["geometry_wgs84"],
            layer="water_system_candidate",
            label=f"Water system candidate zone {z['id']}",
            confidence=CONFIDENCE_LOW,
            confidence_notes=WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES,
            extra_properties={
                "served_production_area_ids": z["served_production_area_ids"],
                "production_area_relationships": z["production_area_relationships"],
                "primary_production_area_relationship": z["primary_production_area_relationship"],
                "contributing_area_cells": z["contributing_area_cells"],
                "slope_pct": z["slope_pct"],
                "render_fill_geometry_wgs84": z["render_fill_geometry_wgs84"],
                # PURELY ADDITIVE provenance/measurement properties -- every
                # property above is unchanged, so no existing consumer of this
                # layer is affected. Shapely geometry stays OFF the feature
                # (keypoint_point_utm/anchor_point_utm live on the zone dict
                # only); what travels here is the row/col pair and the plain
                # numbers, so the FeatureCollection remains JSON-serialisable.
                "nominated_by": z["nominated_by"],
                "keypoint_id": z["keypoint_id"],
                "valley_id": z["valley_id"],
                "keypoint_rowcol": list(z["keypoint_rowcol"]) if z["keypoint_rowcol"] else None,
                "anchor_rowcol": list(z["anchor_rowcol"]),
                "anchor_elevation_m": z["anchor_elevation_m"],
                "seed_snapped": z["seed_snapped"],
                "seed_snap_distance_m": z["seed_snap_distance_m"],
                "level_pool": z["level_pool"],
                "abutment_found_left": z["abutment_found_left"],
                "abutment_found_right": z["abutment_found_right"],
                "abutment_distance_left_m": z["abutments"]["left"]["lateral_distance_m"],
                "abutment_distance_right_m": z["abutments"]["right"]["lateral_distance_m"],
                "canopy_overlap_pct": z["canopy_overlap_pct"],
                "road_overlap_pct": z["road_overlap_pct"],
                "flags": z["flags"],
            },
        )
        for z in zones
    ]
    return make_feature_collection(features)


# =====================================================================
# NARRATIVE DATA -- report-facing, FINAL values only
# =====================================================================
# Everything below exists to answer THREE report questions about this
# module's deliverable, and nothing else:
#
#   1. WHERE is the water-system survey area on the map?
#   2. WHY is this area conducive to a water system?
#   3. HOW does it serve the farm?
#
# The same two hard rules production_area_ceiling.py's narrative block
# established govern every value here:
#
#   1. FINAL. The consumer must never convert, calculate, or relate two
#      values to get a third. Imperial at this boundary (acres, feet --
#      never metres or cell counts); slope/gradient in percent; position
#      as a compass word, not coordinates; everything rounded to 1
#      decimal place, because the precision emitted is the precision
#      narrated.
#   2. DERIVED, NEVER RECOMPUTED. Every figure is read off the zone dict
#      find_candidate_zones() already returns -- no gate, growth pass, or
#      selection is re-run to report on itself. Two narrow, documented
#      exceptions, same class as production_area_ceiling.py's own
#      _on_parcel_cell_mask() carve-out: the parcel-wide elevation range
#      (one vectorised containment test no pipeline step computes, needed
#      to place the zone as upper/lower ground) and the centroid bearing
#      (trivial arithmetic on footprints already in hand).
#
# The output is plain JSON: numbers, booleans, strings, dicts, lists.
# json.dumps() must work on it with no custom encoder.
#
# UNAVAILABLE IS None, NEVER 0.0. A gradient of 0.0 at distance 0 is a
# div-by-zero placeholder, not a measured level grade (the same live bug
# water_suitability._gravity_feed_factor() exists to sidestep) -- it is
# emitted as None here so a narrative cannot read it as a measurement.
#
# NO REASON STRINGS. This block emits values; the report writes prose --
# same division of labor as production_area_ceiling.build_narrative_data().

# 8-point compass words for position_in_parcel -- whole words, because this
# feeds narrative prose directly. Deliberately a separate tuple from
# production_area_ceiling.py's own private _COMPASS_WORDS (same "constants
# stay separate even when identical" convention this module already applies
# to its buffer distances).
_COMPASS_WORDS = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
)

# A zone whose centroid sits within this fraction of the parcel's
# equivalent-circle radius of the parcel's own centroid reads as "center"
# rather than a compass direction -- naming a bearing for a near-central
# offset would narrate precision the position doesn't have. The
# equivalent-circle radius (sqrt(area/pi)) makes the test scale with the
# parcel rather than with any fixed distance. CONFIGURABLE.
_CENTER_POSITION_MAX_OFFSET_FRACTION = 0.2


def _round1(value):
    """1 decimal place, or None passed straight through -- the single
    rounding boundary for this whole block. None means 'not known', and
    must never be silently rounded into a 0.0 that reads as a
    measurement."""
    return None if value is None else round(float(value), 1)


def _feet(meters):
    """Metres to feet at this block's own rounding boundary, None passed
    straight through -- the metric-to-imperial conversion happens HERE, in
    the module, never downstream in the report."""
    return None if meters is None else round(float(meters) / METERS_PER_FOOT, 1)


def _parcel_elevation_range(dem: dict, boundary_polygon_utm: Polygon):
    """
    (low, high) elevation across DEM cells whose CENTER falls inside the
    real parcel boundary, or None on a parcel with no relief at all (where
    "upper" and "lower" ground mean nothing). This is one of the two
    values in this block no pipeline step already computes -- one
    vectorised shapely.contains_xy() call over the whole grid, the same
    cell-center convention and same documented exception
    production_area_ceiling.py's narrative block already makes.
    """
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    col_centers = dem["origin_x"] + (np.arange(cols) + 0.5) * px
    row_centers = dem["origin_y"] - (np.arange(rows) + 0.5) * py
    xs, ys = np.meshgrid(col_centers, row_centers)
    on_parcel = np.asarray(contains_xy(boundary_polygon_utm, xs, ys), dtype=bool)
    elevations = dem["array"][on_parcel]
    elevations = elevations[~np.isnan(elevations)]
    if elevations.size and float(elevations.max()) > float(elevations.min()):
        return float(elevations.min()), float(elevations.max())
    return None


def _position_in_parcel(zone_polygon_utm, boundary_polygon_utm: Polygon) -> str:
    """
    Where the zone sits within the parcel, as an 8-point compass word (or
    "center") -- the bearing from the parcel's own centroid to the zone's
    footprint centroid, the same centroid find_candidate_zones() already
    used as the zone's representative point. UTM axes are +x east / +y
    north, so atan2(dx, dy) IS a compass bearing (0 = north, clockwise).
    """
    parcel_centroid = boundary_polygon_utm.centroid
    zone_centroid = zone_polygon_utm.centroid
    dx = zone_centroid.x - parcel_centroid.x
    dy = zone_centroid.y - parcel_centroid.y
    equivalent_radius = math.sqrt(boundary_polygon_utm.area / math.pi)
    if equivalent_radius <= 0 or math.hypot(dx, dy) <= equivalent_radius * _CENTER_POSITION_MAX_OFFSET_FRACTION:
        return "center"
    bearing_deg = math.degrees(math.atan2(dx, dy)) % 360.0
    return _COMPASS_WORDS[int(round(bearing_deg / 45.0)) % 8]


def _relationship_narrative(relationship: dict) -> dict:
    """
    One production-area relationship, restated in this block's own FINAL
    units -- read off the already-computed (and already-rounded)
    production_area_relationships entry, never re-measured.

    can_gravity_feed is the narrative reading of above_production_area: a
    zone sitting above the ground it serves can deliver water downhill by
    gravity; one sitting below would need a pump -- a real cost/maintenance
    tradeoff, not a defect (see module docstring's "gravity is a
    preference, not a gate" framing).

    gradient_pct is None -- not 0.0 -- when distance is 0: rise-over-run is
    mathematically undefined with no run, and the raw relationship's 0.0
    there is a div-by-zero placeholder a narrative would misread as
    measured level ground (the exact bug water_suitability.
    _gravity_feed_factor() documents). The real elevation differential
    still carries the signal in that case.
    """
    distance_m = float(relationship["distance_m"])
    return {
        "production_area_id": int(relationship["production_area_id"]),
        "can_gravity_feed": bool(relationship["above_production_area"]),
        "elevation_differential_ft": _feet(relationship["elevation_differential_m"]),
        "distance_ft": _feet(distance_m),
        "gradient_pct": None if distance_m == 0 else _round1(relationship["gradient_pct"]),
    }


def _feet_from_meters_or_none(meters):
    """_feet() under a name that reads correctly at a call site where the
    input may legitimately be None (an abutment that was not found has no
    distance) -- same conversion, no second rounding boundary."""
    return _feet(meters)


def _level_pool_narrative(zone: dict) -> dict:
    """
    The level-pool block, restated in this section's FINAL units -- read off
    the zone's own already-computed measurements, never re-measured.

    reference_height_ft is reported so a narrative can say WHAT WATERLINE
    everything below was measured at, and it must always be narrated as a
    measuring stick rather than a proposal: no dam of this height is being
    recommended (see valley_level_pool.POOL_REFERENCE_HEIGHT_METERS).

    NO VOLUME. The stations carry flooded width and flooded cross-sectional
    area only; nothing here multiplies them into a capacity, and a narrative
    must not either.

    An abutment that was NOT found reports distance None, never a number:
    "the ground never rose to the waterline within the search width" is a
    real finding, and a 0.0 there would read as "the abutment is right at
    the anchor," the opposite of the truth.
    """
    pool = zone["level_pool"]
    left = zone["abutments"]["left"]
    right = zone["abutments"]["right"]
    return {
        "reference_height_ft": _feet(pool["reference_height_meters"]),
        "dam_band_width_ft": _feet(pool["dam_band_width_m"]),
        "abutment_found_left": bool(zone["abutment_found_left"]),
        "abutment_found_right": bool(zone["abutment_found_right"]),
        "abutment_distance_left_ft": _feet_from_meters_or_none(left["lateral_distance_m"]),
        "abutment_distance_right_ft": _feet_from_meters_or_none(right["lateral_distance_m"]),
        "backwater_cell_count": int(pool["pool_cell_count"]),
        "stations": [
            {
                "station_index": int(station["station_index"]),
                "offset_upstream_ft": _feet(station["offset_upstream_m"]),
                "flooded_width_ft": _feet_from_meters_or_none(station["flooded_width_m"]),
                # Square METRES converted to square FEET at this block's own
                # rounding boundary -- a cross-section, never a capacity.
                "flooded_cross_section_area_sqft": (
                    None
                    if station["flooded_cross_section_area_m2"] is None
                    else _round1(station["flooded_cross_section_area_m2"] / (METERS_PER_FOOT ** 2))
                ),
            }
            for station in pool["stations"]
        ],
    }


def _zone_narrative(
    zone: dict,
    dem: dict,
    boundary_polygon_utm: Polygon,
    elevation_range,
    contributing_area_ceiling_acres: float,
) -> dict:
    """One candidate's own narrative block -- the same WHERE/WHY/HOW shape
    this section has always emitted, plus the provenance, flags and
    level-pool measurements this branch's candidates carry."""
    area_per_cell = cell_area_acres(dem)
    relationships = zone["production_area_relationships"]

    if elevation_range is None:
        elevation_percentile = None
    else:
        low, high = elevation_range
        elevation_percentile = _round1(
            max(0.0, min(100.0, (float(zone["representative_elevation_m"]) - low) / (high - low) * 100.0))
        )

    return {
        "id": int(zone["id"]),
        "area_acres": _round1(zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE),
        "provenance": {
            "nominated_by": zone["nominated_by"],
            "keypoint_id": zone["keypoint_id"],
            "valley_id": zone["valley_id"],
            "seed_snapped": bool(zone["seed_snapped"]),
            "seed_snap_distance_ft": _feet(zone["seed_snap_distance_m"]),
        },
        "flags": list(zone["flags"]),
        "location": {
            "position_in_parcel": _position_in_parcel(zone["polygon_utm"], boundary_polygon_utm),
            "elevation_percentile_of_parcel": elevation_percentile,
        },
        "drainage": {
            "contributing_area_acres": _round1(float(zone["contributing_area_cells"]) * area_per_cell),
            "contributing_area_ceiling_acres": _round1(contributing_area_ceiling_acres),
            "slope_median_pct": _round1(zone["slope_pct"]),
        },
        "level_pool": _level_pool_narrative(zone),
        "overlap": {
            # REPORTED, UNWEIGHTED. Canopy and roads gate where an anchor may
            # be nominated; they never clip a delineated pool (see find_
            # candidate_zones()). None means the corresponding check never
            # ran -- which is not the same as 0.0.
            "canopy_overlap_pct": _round1(zone["canopy_overlap_pct"]),
            "road_overlap_pct": _round1(zone["road_overlap_pct"]),
        },
        "service": {
            "served_production_area_count": len(relationships),
            "served_production_area_ids": [int(i) for i in zone["served_production_area_ids"]],
            "relationships": [_relationship_narrative(r) for r in relationships],
        },
    }


def build_narrative_data(
    zones: list[dict],
    dem: dict,
    boundary_polygon_utm: Polygon,
    production_area_count: int,
    canopy_data_available: bool,
    road_data_available: bool,
    contributing_area_ceiling_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    nomination_diagnostics: Optional[dict] = None,
) -> dict:
    """
    The 'narrative_data' block identify_water_system_candidate_zones()
    attaches to its result -- pre-computed, FINAL, JSON-serialisable
    values answering the three report questions in this section's header
    comment. Data only: no prose, no interpretation. zones is
    find_candidate_zones()'s own return value, unread beyond its fields.

    WHAT CHANGED WITH MULTI-CANDIDATE NOMINATION, mechanically: this block
    used to describe ONE zone grown toward a fixed survey-area target. The
    target is retired (zone size emerges from the terrain now), so the
    target_acres parameter and the field that reported it are GONE, and
    the block describes N candidates -- 'zones' is the full list, each
    entry carrying its own provenance (which family nominated it, and for
    a keypoint nomination which keypoint/valley), its flags, and its
    level-pool measurements. 'zone' remains, as candidates[0], so a
    consumer wanting one headline candidate still has one; 'nomination'
    carries the per-keypoint outcome list and the family-2 seed log so a
    narrative can say WHY there are three candidates, or one, or none.
    Nothing else about the block's contract moved: FINAL imperial values,
    1 decimal place, json.dumps()-clean, unavailable is None and never 0.0.

    canopy_data_available / road_data_available say whether each optional
    exclusion gate genuinely ran on the path that produced `zones` --
    identify_water_system_candidate_zones() passes True for canopy always
    (its canopy gate is fetch-or-raise, so any result it returns at all
    was canopy-checked) and True for road only when the road fetch
    actually succeeded. Without these a narrative could claim "verified
    clear of mapped roads" off a run where the road service was down.

    contributing_area_ceiling_acres is the value the run ACTUALLY used (a
    zone_kwargs override, or this module's default) -- the caller passes it
    so this block never guesses at configuration.

    nomination_diagnostics is find_candidate_zones()'s own diagnostics dict
    from the SAME run. Reason codes travel through it verbatim (they are an
    enumeration, not prose -- see the REASON CODES section), so a narrative
    can map a code to a sentence without this block inventing one.

    Shape:

        {
          'zone_found': bool,             # any candidate at all
          'candidate_count': int,
          'production_area_count': int,   # candidate production areas that
                                          #   existed to serve -- 0 explains
                                          #   a no-candidate outcome by itself
          'gates': {'canopy_data_available', 'road_data_available'},
          'nomination': {
            'keypoints_considered': int,
            'keypoint_outcomes': [ {keypoint_id, valley_id, contributing_acres,
                                    outcome, candidate_id, seed_snapped,
                                    seed_snap_distance_ft, flags}, ... ],
            'accumulation_seeds': [ {candidate_id, outcome, flags}, ... ],
          },
          'zones': [ per-candidate block, see _zone_narrative() ],
          'zone': zones[0] or None,       # headline candidate
        }
    """
    diagnostics = nomination_diagnostics or {}
    data = {
        "zone_found": bool(zones),
        "candidate_count": len(zones),
        "production_area_count": int(production_area_count),
        "gates": {
            "canopy_data_available": bool(canopy_data_available),
            "road_data_available": bool(road_data_available),
        },
        "nomination": {
            "keypoints_considered": int(diagnostics.get("keypoints_considered", 0)),
            "keypoint_outcomes": [
                {
                    "keypoint_id": int(outcome["keypoint_id"]),
                    "valley_id": int(outcome["valley_id"]),
                    "contributing_acres": _round1(outcome["contributing_acres"]),
                    "outcome": outcome["outcome"],
                    "candidate_id": outcome["candidate_id"],
                    "seed_snapped": bool(outcome["seed_snapped"]),
                    "seed_snap_distance_ft": _feet(outcome["seed_snap_distance_m"]),
                    "flags": list(outcome["flags"]),
                }
                for outcome in diagnostics.get("keypoint_outcomes", [])
            ],
            "accumulation_seeds": [
                {
                    "outcome": seed["outcome"],
                    "candidate_id": seed["candidate_id"],
                    "flags": list(seed["flags"]),
                }
                for seed in diagnostics.get("accumulation_seeds", [])
            ],
        },
        "zones": [],
        "zone": None,
    }
    if not zones:
        return data

    elevation_range = _parcel_elevation_range(dem, boundary_polygon_utm)
    data["zones"] = [
        _zone_narrative(zone, dem, boundary_polygon_utm, elevation_range, contributing_area_ceiling_acres)
        for zone in zones
    ]
    data["zone"] = data["zones"][0]
    return data


def identify_water_system_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    valleys: Optional[list[dict]] = None,
    production_areas: Optional[list[dict]] = None,
    keypoints: Optional[list[dict]] = None,
    canopy_height: Optional[dict] = None,
    road_exclusion_union_utm=_ROAD_UNION_NOT_SUPPLIED,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in —
    e.g. reused from generate_full_report.py already fetching it, or
    supplied directly in a test), identifies production-area candidates,
    and returns:

        {
            'zones_geojson': FeatureCollection,             # layer="water_system_candidate" — the deliverable
            'valleys_geojson': FeatureCollection,            # layer="valley" — diagnostic (Stage 1)
            'production_areas_geojson': FeatureCollection,   # layer="production_area_candidate" — diagnostic
            'narrative_data': dict,                          # report-facing, FINAL, JSON-serialisable
                                                             #   values — see build_narrative_data()
        }

    'narrative_data' is PURELY ADDITIVE: every other key above, and every
    field on every zone/feature, is byte-identical to what this function
    returned before it existed. It answers three report questions (where
    is the survey area on the map / why is this area conducive to a water
    system / how does it serve the farm) with pre-computed, imperial,
    rounded values a narrative can quote directly -- derived entirely from
    the zone dict find_candidate_zones() already returned, so adding it
    re-runs no gate, no growth pass, and no selection. See
    build_narrative_data()'s own docstring for the field contract.

    dem, boundary_polygon_utm, valleys, and production_areas are all
    optional overrides, independently of one another (supplying one does
    not require supplying the rest) — each falls back to being self-
    computed exactly as before if not supplied. This lets a caller that
    has already computed some or all of these upstream (e.g. a shared
    pipeline-context orchestrator reusing one DEM/boundary/valleys/
    production-areas pass across several KSOP steps) pass them straight
    through instead of this function re-deriving or re-fetching its own
    copies:
      - boundary_polygon_utm: the same warp_transform-then-Polygon(...)
        reprojection this function has always done inline, computed only
        if not supplied.
      - valleys: valley_delineation.delineate_valleys(dem)'s own output
        by default. Only ever consumed by this function's own diagnostic
        valleys_geojson output below — find_candidate_zones() itself
        never reads valleys at all (it derives its own flow-accumulation
        grid directly from `dem`, see that function's own docstring), so
        supplying/omitting this override changes valleys_geojson only,
        never zones_geojson.
      - production_areas: production_area.identify_production_areas(dem,
        boundary_polygon_utm)'s own raw (un-ceiling-trimmed) patches by
        default, but an override doesn't have to match that exact shape —
        find_candidate_zones() only ever reads a patch's 'polygon_utm',
        'render_fill_polygon_utm', 'id', and 'representative_elevation_m'
        fields, so production_area_ceiling.
        identify_optimized_production_areas()'s scored_patches (a strict
        superset of those same fields) is a valid drop-in override too.
      - keypoints: keypoint_detection.detect_keypoints()'s own list of
        per-valley keypoint dicts, forwarded straight into find_candidate_
        zones() as the FAMILY 1 nomination source. When None,
        find_candidate_zones() detects them itself from the same dem/
        boundary/valleys. Supplying them is what keeps keypoint detection
        to EXACTLY ONE run per pipeline pass: build_pipeline_context()
        already computes keypoints (it needs them for its own map/report
        layer) and hands them here, and to water_suitability.identify_
        water_suitability(), so neither water path re-detects. Note that
        `valleys` is forwarded into find_candidate_zones() too, so even the
        self-compute path reuses this function's already-delineated valleys
        rather than delineating a second set.

    valleys_geojson is still produced via valley_delineation.
    delineate_valleys() purely as diagnostic output (unchanged, own
    traced-branch geometry, own thresholds) — useful for inspecting "did
    we find the right valleys" independently of "is the zone logic
    right," per this feature's stated debugging goal — but
    find_candidate_zones() itself no longer consumes delineate_valleys()'s
    traced branches at all; it derives its own flow-accumulation grid
    directly from `dem` (see find_candidate_zones()'s own docstring).

    This is the network entry point that makes find_candidate_zones()'s
    canopy exclusion genuinely MANDATORY: it always calls production_area.
    get_required_tree_root_zone_mask_utm() (at this module's own
    WATER_ZONE_CANOPY_BUFFER_METERS) before find_candidate_zones() ever
    runs, so a missing/unreliable canopy fetch raises (RuntimeError for no
    coverage at all, canopy_height_data.CanopyCoverageIncompleteError for
    coverage too sparse to trust) rather than silently proceeding without
    the check -- same hard-fail behavior as production_area.identify_
    production_areas()'s own woody-vegetation gate, reusing that exact
    function rather than a parallel copy. The road exclusion, by
    contrast, is fetched too but degrades GRACEFULLY on failure (falls
    back to "not checked," same as production's own check_roads default)
    -- see compute_water_eligible_cells()'s own docstring (gates 4/5).

    canopy_height is an optional pre-fetched override in the same family as
    the dem/boundary/valleys/production_areas overrides above: the SAME
    dict canopy_height_data.get_canopy_height_for_boundary() returns (e.g.
    parcel_data.ParcelData.canopy_height). When supplied it is forwarded to
    BOTH canopy consumers on this path -- the default production_areas
    fetch (identify_production_areas() above) and this module's own
    mandatory get_required_tree_root_zone_mask_utm() call -- so neither
    re-fetches canopy from the network; when None (the default) both fetch
    as before, leaving the mandatory-gate semantics unchanged.

    road_exclusion_union_utm is the road gate's own pre-fetched union
    (farm_roads_data.get_road_exclusion_union_utm()'s output, reprojected
    into dem['crs'] and buffered at ROAD_EXCLUSION_BUFFER_METERS), so a
    caller that already built one for this exact boundary -- build_
    pipeline_context() does, for the exclusion-zones and production gates
    -- does not pay for a third, identical fetch here. Interchangeable
    because that buffer is a SINGLE SHARED CONSTANT read by every consumer,
    not three constants that happen to be equal; test_water_candidate_
    zones.py asserts the producer's and this module's self-fetch defaults
    are the same value so a future divergence fails loudly instead of
    silently substituting a wrong-buffer union.

    UNLIKE dem/boundary_polygon_utm/valleys/production_areas above, its
    "not supplied" is an explicit sentinel rather than None -- a real None
    is get_road_exclusion_union_utm()'s own clean answer ("checked, and
    genuinely no mapped road nearby", the common case) and is REUSED, not
    treated as missing. Same distinction exclusion_zones.identify_
    exclusion_zones() draws for the same union, and for the same reason:
    collapsing the two would reintroduce the redundant fetch on exactly
    the parcels the pass-through exists to spare.
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    if boundary_polygon_utm is None:
        boundary_xs, boundary_ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    if valleys is None:
        valleys = delineate_valleys(dem)

    if production_areas is None:
        production_areas = identify_production_areas(dem, boundary_polygon_utm, canopy_height=canopy_height)

    canopy_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=WATER_ZONE_CANOPY_BUFFER_METERS, canopy_height=canopy_height
    )

    if road_exclusion_union_utm is _ROAD_UNION_NOT_SUPPLIED:
        try:
            # No buffer_meters override: the default IS the intended value --
            # farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS, the single shared
            # definition of "how far off an existing road" (see that
            # constant's docstring), same as every other consumer. That single
            # shared definition is also what makes the override above safe: a
            # caller-supplied union is interchangeable with this one because
            # both are built at the same buffer BY DEFINITION, which
            # test_water_candidate_zones.py asserts against the two signature
            # defaults rather than taking on trust.
            road_exclusion_union_utm = _fetch_road_exclusion_union_utm(boundary_coordinates, dem)
        except Exception:
            road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    nomination_diagnostics: dict = {}
    zones = find_candidate_zones(
        dem,
        production_areas,
        boundary_polygon_utm,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
        keypoints=keypoints,
        valleys=valleys,
        diagnostics=nomination_diagnostics,
        **zone_kwargs,
    )

    return {
        "zones_geojson": zones_to_geojson(zones),
        "valleys_geojson": valleys_to_geojson(valleys),
        "production_areas_geojson": production_areas_to_geojson(production_areas),
        "narrative_data": build_narrative_data(
            zones,
            dem,
            boundary_polygon_utm,
            production_area_count=len(production_areas),
            # Canopy is fetch-or-raise on this path (get_required_tree_root_
            # zone_mask_utm() above), so any result returned at all was
            # canopy-checked; the road fetch degrades gracefully, so its
            # flag reports whether the check genuinely ran.
            canopy_data_available=True,
            road_data_available=road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED,
            # The values this RUN actually used -- a zone_kwargs override,
            # or this module's defaults -- so the narrative never reports a
            # configured constant a caller overrode away.
            contributing_area_ceiling_acres=zone_kwargs.get(
                "max_valley_contributing_area_acres", MAX_VALLEY_CONTRIBUTING_AREA_ACRES
            ),
            # The SAME run's nomination record -- reason codes and flags
            # travel verbatim, so the narrative explains a partial or empty
            # result instead of merely reporting one.
            nomination_diagnostics=nomination_diagnostics,
        ),
    }


def summarize_water_system_candidate_zones(result: dict) -> str:
    features = result["zones_geojson"]["features"]
    zone_count = len(features)
    valley_count = len(result["valleys_geojson"]["features"])
    production_area_count = len(result["production_areas_geojson"]["features"])
    nomination = result["narrative_data"]["nomination"]

    if zone_count == 0:
        # An empty answer is explained by its REASON CODES, not just
        # reported -- see the REASON CODES section for why.
        outcomes = sorted({o["outcome"] for o in nomination["keypoint_outcomes"] if o["outcome"]})
        outcomes += sorted({s["outcome"] for s in nomination["accumulation_seeds"] if s["outcome"]})
        reason_clause = f" Reason codes: {', '.join(outcomes)}." if outcomes else ""
        return (
            f"{valley_count} primary valley(s), {production_area_count} "
            f"production-area candidate(s) and "
            f"{nomination['keypoints_considered']} keypoint(s) found, but no "
            "anchor produced a qualifying level pool — no water system "
            f"candidate zones identified.{reason_clause}"
        )

    lines = [
        f"Water system candidate zones: {zone_count} "
        f"(from {valley_count} primary valley(s), {production_area_count} "
        f"production-area candidate(s), {nomination['keypoints_considered']} keypoint(s))"
    ]
    for feature in features:
        properties = feature["properties"]
        provenance = properties["nominated_by"]
        if properties["keypoint_id"] is not None:
            provenance += f" {properties['keypoint_id']} (valley {properties['valley_id']})"
        flag_clause = f", flags: {', '.join(properties['flags'])}" if properties["flags"] else ""
        lines.append(f"  - {feature['id']}: nominated by {provenance}{flag_clause}")
    return "\n".join(lines)


if __name__ == "__main__":
    # Test case: the user's own drawn property boundary.
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Identifying water system candidate zones for property boundary...\n")

    try:
        result = identify_water_system_candidate_zones(property_boundary)
        print(summarize_water_system_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer — not a fully sandboxed environment."
        )
