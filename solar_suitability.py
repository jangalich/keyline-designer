"""
solar_suitability.py

Solar/structure siting data layer for permanent building placement (Scale
of Permanence step 6): ranks candidate SITES for a small, fixed-footprint
solar-generating structure (e.g. a barn or shed with rooftop panels), not
a large ground-mounted array. This produces RANKED CANDIDATES, not a
single placement decision — Claude narrates the tradeoffs between them in
the report (see report_generator.py step 6). Finding "the one best spot"
is explicitly not this module's job.

--- THIS PASS: THE MEASUREMENT, TWO DRAINAGE GATES, AND THE PANEL ---

FOUR CHANGES, none of them to the four scoring factors or their equal
weights, and none of them to the 0.1-acre pad:

  1. EVERY REPORTED DISTANCE IS NOW MEASURED FROM THE SITE'S POINT, not
     from its pad. One cause produced two wrong answers: shapely's
     .distance() returns 0.0 both when geometries INTERSECT and when one
     CONTAINS the other. The road-proximity constraint tunes candidates
     to sit close to a road, so the pad intersected the road on
     essentially every candidate and "ft to road" -- the data panel's
     HEADLINE figure -- read 0.0 on nine of the eleven clearing
     footprints on the reference parcel. Production distance, measured to
     a block's EDGE, gave a site buried inside a block a positive number
     indistinguishable from one that far outside. The pad is still what
     is SCORED over and what every hard gate tests (a gate asks what
     ground the building occupies); the point is what distances are
     measured from (a distance asks where the building is). The road GATE
     moved with its distance, and 'signed_distance_to_production_m' --
     negative inside a block, positive outside, 0.0 on the edge -- is the
     other half of the production fix. See find_candidate_solar_zones().

  2. TWO HARD DRAINAGE GATES, hydric soil and floodplain, SEPARATELY.
     This step had no drainage constraint at all: it received
     hydric_floodplain_union -- roads' COMBINED soft cost-penalty shape --
     and forwarded it into its nested calls without ever consuming it, so
     live testing found a structure sited on wet ground. Drainage under a
     foundation and flood risk around a building are different problems,
     a site can break either or both, and constraints_violated must name
     WHICH; road_corridors._fetch_floodplain_hydric_unions() now keeps the
     two halves apart for this step alone. HARD for a GENERATED candidate,
     CAUTION for a PLACED one -- landform's rule exactly. See THE TWO
     DRAINAGE GATES below.

  3. THE SOLAR RATING, a WORD on the two solar factors together, banded
     on the backend (SOLAR_RATING_BANDS, shipped in narrative_data
     ['scales']) so the frontend holds no threshold. A BAND, not a rank.

  4. THE PANEL'S REMAINING TWO VALUES: 'elevation_position', classified
     by production's own imported ELEVATION_POSITION_BANDS (never a second
     copy of the cuts), and 'road_proximity_source' promoted to a
     STEP-LEVEL narrative_data key, since which access tier answered is
     true of every candidate in the run and decides what the headline
     ft-to-road figure MEANS.

ROAD_CORRIDOR_PROXIMITY_METERS went 15 m -> 30 m with change 1, and the
two are one decision: a pad-based 15 m gate really admitted a point ~25 m
out, so moving the gate to the point tightened the requirement by a
half-pad. See that constant.

CONSTRAINT STACK, an earlier pass (brings this layer in line with the rest
of the pipeline, which has since moved to render_fill_polygon_utm-based
exclusions, optimized/ceiling-trimmed production geometry, and a real
selected road corridor):
  - Production geometry is now production_area_ceiling.
    identify_optimized_production_areas()'s OPTIMIZED, ceiling-trimmed
    'scored_patches' (NOT production_area.identify_production_areas()'s
    raw, un-trimmed candidates), and every union built from it uses each
    patch's own 'render_fill_polygon_utm' (NOT 'polygon_utm') — same
    "reads as one coherent shape" field/reasoning tree_zone_candidates.py
    already uses. Still NOT a hard exclusion for solar — production stays
    a scored edge-proximity PREFERENCE (see PRODUCTION_PROXIMITY_SCORE_
    WEIGHT below); only the source geometry/field changed.
  - Water-candidate zone exclusion now also uses the single selected
    water zone's own 'render_fill_polygon_utm' (not 'polygon_utm') —
    still a HARD, buffered exclusion (POND_ZONE_EXCLUSION_BUFFER_METERS,
    reused from road_corridors.py), unchanged in spirit.
  - Real, existing tree canopy is now a NEW hard exclusion: a candidate
    footprint touching real USGS 3DEP lidar canopy coverage (production_
    area.get_required_tree_root_zone_mask_utm(), buffered by
    TREE_ROOT_ZONE_BUFFER_METERS) is excluded outright, before any
    scoring happens — same "excluded before scoring" treatment as water.
    THIS FETCH IS MANDATORY AND DOES NOT DEGRADE: unlike every other
    network-backed layer in this module (which all fail gracefully — a
    real outage shouldn't block candidates from being identified at
    all), a canopy-data outage here is left to propagate UNCAUGHT and
    fails the whole run. This is a deliberate, intentional asymmetry, not
    an oversight: siting a structure on or under real, existing tree
    cover without knowing it is a genuine physical siting error, not a
    lower-confidence result worth handing back with a caveat — the exact
    same "can't verify this is free of tree cover, refuse rather than
    guess" reasoning production_area.py's/production_area_ceiling.py's/
    tree_zone_candidates.py's own mandatory canopy gates already use.
    Switching production to optimized geometry (above) already pulls in
    a SECOND, fully independent mandatory canopy gate inside
    identify_optimized_production_areas() itself (its own
    CanopyCoverageIncompleteError/RuntimeError) — both are expected to
    hard-fail independently on a canopy outage; neither is caught here.
  - A NEW hard exclusion against EVERY ranked tree-zone candidate
    (tree_zone_candidates.identify_tree_zone_candidates()'s own 'patches'
    — the full ranked list, not just the top one: unlike water/road,
    trees has no single "selected" zone by design, since each ranked
    patch is independently, separately plantable — OR, on the interactive
    path, the trees step's COMMITTED selection supplied through
    identify_solar_candidate_zones()'s tree_zone_patches= override, which
    is the same list shape and closes the self-compute). A candidate footprint
    intersecting the union of every tree-zone candidate's own
    'render_fill_polygon_utm', buffered by
    TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS (10ft — a real,
    independently-tunable clearance, NOT reused from
    POND_ZONE_EXCLUSION_BUFFER_METERS or TREE_ROOT_ZONE_BUFFER_METERS,
    which mean different things), is excluded. UNLIKE the canopy gate
    above, this call degrades GRACEFULLY on an ordinary fetch failure
    (network outage, etc. — noted in confidence_notes) — identify_tree_
    zone_candidates() itself is a real, network-fetch-heavy dependency
    (it calls into production/water/road and does its own soil/stream
    fetches), not a single mandatory building block the way the canopy
    mask itself is. The ONE exception: if that failure is specifically
    canopy_height_data.CanopyCoverageIncompleteError bubbling up from
    INSIDE identify_tree_zone_candidates()'s own mandatory canopy gate,
    it is left to propagate uncaught here too, same reasoning as above —
    not caught and downgraded to "couldn't check this run."
  - Road proximity is now TWO-TIER instead of "real road, else a
    DEM-only suggested corridor treated as a road stand-in":
      Tier 1 (primary): the property's own single SELECTED road corridor
        (road_corridors.identify_road_corridor_candidates(), given this
        module's own anchor_lon_lat parameter — the real, user-picked
        access point, threaded down from generate_full_report.py) — its
        'cell_footprint_polygon_utm', within ROAD_CORRIDOR_PROXIMITY_METERS
        (15m). This corridor is the real primary source now, not a
        stand-in for missing data.
      Tier 2 (fallback): only if Tier 1 produces ZERO candidates (no
        selected corridor exists at all, OR one exists but nothing
        survives every other constraint near it) — real mapped roads
        (farm_roads_data.get_farm_roads_for_boundary()), at the original
        ROAD_PROXIMITY_BUFFER_METERS (150m). This is exactly the
        pre-this-pass road-proximity logic, demoted from primary to
        fallback.
    If Tier 2's own fetch fails outright (a network error, not "zero
    roads found") AND Tier 1 also produced nothing, the road constraint
    is disabled entirely (flagged in confidence_notes) — same terminal
    fallback behavior as before. properties.road_proximity_source
    reports which tier actually produced the result: "selected_road_
    corridor" | "real_mapped_road" | "unavailable".
  - Before grid-sampling candidate points at all, the SEARCH REGION is
    restricted to boundary_polygon_utm intersected with whichever tier's
    road source geometry, buffered by its own proximity buffer plus one
    footprint side length (so a footprint reaching the buffer from just
    outside isn't missed) — a pure GENERATION-TIME optimization, not a
    correctness change: the real per-footprint distance gate
    (footprint.distance(road_union) <= proximity_buffer) still runs
    unchanged and is what actually decides eligibility.
  - Scoring weights are now an even 0.25/0.25/0.25/0.25 split across
    slope/aspect/shading/production-proximity (see SLOPE_SCORE_WEIGHT
    etc. below) — a deliberate simplification from the previous
    0.35/0.25/0.25/0.15 split, unrelated to this pass's constraint
    changes.

POINT-CANDIDATE MODEL (this module's second design; see below for why the
first one — a broad eligible-AREA polygon — was replaced):

    DEM (dem_data.py, already in main)
        --> slope/aspect/shading (terrain_metrics.py)
        --> candidate points, sampled on a grid across the property
        --> [this module] per-point scoring:
                slope + aspect + shading (real DEM signals, averaged over
                    a small local window matching the candidate's own
                    capped footprint)
                + production-zone-edge PROXIMITY (a preference, not an
                    exclusion — see below)
        --> water-candidate zones (water_candidate_zones.py) -- still a
            HARD exclusion (buffered), unchanged in spirit (source field
            updated, see CONSTRAINT STACK above)
        --> real, existing tree canopy -- NEW hard exclusion, mandatory/
            non-degrading (see CONSTRAINT STACK above)
        --> every ranked tree-zone candidate (tree_zone_candidates.py) --
            NEW hard exclusion, buffered, gracefully degrading (see
            CONSTRAINT STACK above)
        --> SSURGO hydric soil and NHD floodplain -- TWO independent hard
            exclusions, unbuffered, applied to a GENERATED candidate only
            (a PLACED site is scored and told which one it broke). See
            THE TWO DRAINAGE GATES below
        --> the selected road corridor, else farm roads (farm_roads_data.py)
            -- two-tier hard proximity constraint + reported distance (see
            CONSTRAINT STACK above)
        --> ranked candidate structure-footprint polygons
            (layer="solar_infrastructure")

WHY THIS REPLACED THE EARLIER ZONE MODEL: the original version of this
layer computed one broad eligible-AREA polygon per connected component of
low-slope, well-scored ground (the same "zone" shape production_area.py
uses), then hard-EXCLUDED it from production zones (buffered) — the same
mental model as production, water, or a future trees/windbreak layer,
which really are competing land uses over a large area. A small solar-
generating STRUCTURE is not that: it's a point-footprint building that can
genuinely coexist with production land around or even under its own small
footprint, the same way a shed can sit at the edge of a field without
taking that field out of production. Modeling it as a broad excluded zone
was the wrong shape for what it represents — and that mismatch became a
real, live bug once production_area.py's own slope ceiling was raised to
match this module's MAX_SOLAR_SLOPE_PCT (both 20%): with both layers
drawing eligibility from nearly the same gentle-ground footprint,
production's zone exclusion consumed essentially all of solar's own
eligible area, and real-property runs started returning ZERO candidates.
Modeling this as small, independently-scored POINT candidates instead of
one shared area-based eligibility pool fixes that at the root, not just
by re-tuning thresholds again.

Water-candidate zones (pond/dam siting ground) are still HARD-excluded
(buffered) — a solar-generating structure sitting on or immediately
against a candidate pond/dam site is a real physical conflict a small
building can't route around the way it can sit near/inside production
land. The exclusion buffer reuses road_corridors.py's own
POND_ZONE_EXCLUSION_BUFFER_METERS rather than a new constant, same as
before. find_candidate_solar_zones() itself still hard-excludes every
water zone it's GIVEN (unchanged) — but identify_solar_candidate_zones(),
its full-pipeline caller, now passes only the SINGLE selected water zone
(water_suitability.select_optimal_water_zone(), same selection already
reused by tree_zone_candidates.py in this pipeline), not every water
candidate. Confirmed live: excluding all of a real property's several
separately-legitimate, separately-buffered water zones can together cover
enough of a small parcel to zero out every solar candidate, even though
each zone's own geometry is individually normal — per product decision,
this app targets small farms only, so one well-suited water zone is
sufficient to exclude against.

find_candidate_solar_zones() is the geometric/scoring core: it takes an
already-fetched DEM dict, production areas, water-candidate zones, a
road source geometry (called once per tier — see CONSTRAINT STACK above),
an already-fetched canopy mask, and an already-computed tree-zone
exclusion polygon (all in the DEM's own projected CRS) and does no
network I/O itself — same reason as water_candidate_zones.py's
find_candidate_zones(): so the scoring logic is unit-testable against a
synthetic DEM independent of whether any of the real data fetches (DEM,
roads, SSURGO, canopy) are working. canopy_mask_utm/tree_zone_exclusion_
polygon_utm both default to None ("gate not applied at all" — useful for
callers/tests that don't care about either exclusion, same "pure-logic
core's own default" reasoning tree_zone_candidates.score_tree_search_
space() already uses for its own canopy mask parameter); identify_solar_
candidate_zones() (the full pipeline entry point) always supplies a real
canopy mask, since its own fetch is mandatory and non-degrading.
flag_prime_farmland_conflicts() is a second, separate pure function for
exactly the same reason, applied to the SSURGO farmland lookup
specifically — unchanged by this pass.
"""

import hashlib
import math
from collections import OrderedDict
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

from canopy_height_data import CanopyCoverageIncompleteError, TREE_ROOT_ZONE_BUFFER_METERS
from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from production_area import get_required_tree_root_zone_mask_utm
# ELEVATION_POSITION_BANDS AND ITS CLASSIFIER ARE PRODUCTION'S, IMPORTED,
# NOT REDECLARED. Production owns the cuts for the trees and structures
# steps alike (see its own docstring, and tree_zone_candidates.py, which
# imports the same two names); production is UPSTREAM of this module, so
# reading its constant is permitted -- the opposite of _position_in_parcel
# below, which water_candidate_zones.py duplicates because water is
# DOWNSTREAM. A second copy of the cuts here would let "upper field" mean
# one thing on a tree zone and another on a structure site.
from production_area_ceiling import (
    ELEVATION_POSITION_BANDS,
    _elevation_position,
    _on_parcel_cell_mask,
    identify_optimized_production_areas,
)
from raster_grid import SQUARE_METERS_PER_ACRE, pixel_center_xy
from road_corridors import NO_ROAD_CORRIDOR, POND_ZONE_EXCLUSION_BUFFER_METERS, identify_road_corridor_candidates
from soil_data import coordinates_to_wkt_polygon, get_farmland_classification_for_polygon, is_prime_farmland
from terrain_metrics import aspect_score, aspect_to_compass_label, compute_shading_score, compute_slope_and_aspect
from tree_zone_candidates import identify_tree_zone_candidates
from water_candidate_zones import _position_in_parcel
from water_suitability import NO_WATER_ZONE, identify_water_suitability

METERS_PER_FOOT = 0.3048

# Ground a solar-generating structure sits on can tolerate more grade than
# row-crop production land, but not arbitrarily much — beyond this, site
# prep (grading, foundation work) starts dominating cost. A candidate
# point whose local average slope exceeds this is hard-excluded, not just
# scored down: this is a real buildability ceiling, unrelated to
# production-zone proximity (see PRODUCTION_PROXIMITY_SCORE_WEIGHT below
# for how proximity is handled — as a preference, not a constraint).
# CONFIGURABLE.
MAX_SOLAR_SLOPE_PCT = 20.0

# How many candidate points to sample per acre... no — see
# CANDIDATE_POINT_SPACING_METERS: sampling is grid-spacing-based, not
# count-based, so density scales naturally with property size.

# Grid spacing (meters) between sampled candidate points (Step 1 of the
# point-candidate model). Deliberately LARGER than MAX_STRUCTURE_FOOTPRINT_ACRES's
# own footprint side length (~20.1m at the 0.1-acre default — see
# _footprint_side_meters()) so neighboring candidates' footprints never
# overlap by construction: each sampled point becomes its own genuinely
# distinct siting option for Claude/the user to compare, not a cloud of
# near-duplicate, heavily-overlapping candidates the way a tighter grid
# would produce. 25m leaves a real (~4.9m) gap between adjacent
# candidate footprints — plausible real spacing between structures on a
# working farm, not just the mathematical minimum. CONFIGURABLE — must
# stay above the footprint side length or neighboring candidates will
# start overlapping. Kept as a flat literal in manual sync with
# MAX_STRUCTURE_FOOTPRINT_ACRES rather than derived from it, since
# _footprint_side_meters() is defined further down the module.
CANDIDATE_POINT_SPACING_METERS = 25.0

# Maximum footprint for one candidate structure (Step 3): this models a
# small building (a barn/shed with rooftop panels), not a ground-mounted
# array, so its footprint is capped small and fixed, not sized to however
# much contiguous eligible ground happens to exist at that point. Named,
# documented constant rather than a literal so the "how big is a
# candidate structure" assumption is visible and tunable in one place.
# CONFIGURABLE.
MAX_STRUCTURE_FOOTPRINT_ACRES = 0.1

# A candidate point near the property boundary can have its nominal
# footprint clipped down by boundary_polygon_utm (see
# find_candidate_solar_zones()'s parcel-clipping reasoning, same pattern
# as every other layer in this pipeline). Below this fraction of the
# nominal footprint's own area, what's left is more sliver than usable
# building pad, and the candidate is dropped rather than reported as a
# technically-nonempty but meaningless remainder. CONFIGURABLE.
MIN_STRUCTURE_FOOTPRINT_FRACTION = 0.5

# Weights for the combined 0-1 suitability score (must sum to 1.0). Even
# 0.25 split across all four factors — slope/aspect/shading determine
# whether a site is buildable and productive at all, while production
# proximity is a layout nicety on top of that; an equal split is a
# deliberate simplification from an earlier 0.35/0.25/0.25/0.15 split
# that weighted slope highest and production proximity lowest.
# CONFIGURABLE — tune against your own property once real production
# data is available to check the ranking against.
SLOPE_SCORE_WEIGHT = 0.25
ASPECT_SCORE_WEIGHT = 0.25
SHADING_SCORE_WEIGHT = 0.25
PRODUCTION_PROXIMITY_SCORE_WEIGHT = 0.25

_WEIGHT_SUM = SLOPE_SCORE_WEIGHT + ASPECT_SCORE_WEIGHT + SHADING_SCORE_WEIGHT + PRODUCTION_PROXIMITY_SCORE_WEIGHT
assert math.isclose(_WEIGHT_SUM, 1.0, abs_tol=1e-6), f"solar suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"

# Below this combined score (0-1 scale), a point isn't worth surfacing as
# a candidate at all, even if it technically clears every hard
# constraint. CONFIGURABLE.
MIN_SUITABILITY_SCORE = 0.4

# Production-zone-edge PROXIMITY scoring (Step 4 — NOT an exclusion; see
# module docstring for why production zones are no longer hard-excluded
# here). distance_to_production_zone_edge is measured to the nearest
# production zone's own BOUNDARY LINE, not its filled area — this is
# deliberate: a shapely .distance() to a filled polygon reads 0 for BOTH
# "just touching the edge" and "buried deep in the middle," which would
# make a candidate near an edge indistinguishable from one that isn't.
# Measuring to the boundary line instead means proximity peaks right at
# an edge (approached from either inside or outside) and falls off in
# both directions from there — exactly the "prefer the edge, don't
# require production land at all, don't over-reward the deep interior"
# shape this feature asks for.
#
# Score is 1.0 right at a production zone's edge, falling linearly to 0.0
# at or beyond this reference distance (in either direction — outside
# past this range, or buried this deep inside a large zone, away from any
# edge). CONFIGURABLE.
PRODUCTION_PROXIMITY_REFERENCE_METERS = 100.0

# Within this distance of a production zone's own edge (but not
# overlapping it), a candidate is classified "adjacent" rather than
# "outside" in properties.production_zone_relationship (see
# _classify_production_zone_relationship()). Reuses the same distance
# value this module's PREVIOUS zone-exclusion-buffer constant used
# (production zones used to be hard-excluded by this same margin) —
# repurposed here as a classification threshold, not an exclusion.
# CONFIGURABLE.
PRODUCTION_EDGE_ADJACENCY_METERS = 15.0

# TIER 1 (primary): candidates must be within this distance of the
# property's own single SELECTED road corridor (road_corridors.
# identify_road_corridor_candidates()'s own 'cell_footprint_polygon_utm')
# to be considered reachable/wireable at all — a hard constraint, not
# just a scoring input. Deliberately much tighter than the TIER 2
# fallback buffer below: a corridor is a real, specific routed alignment
# on THIS property, not a generic "somewhere near a mapped road" signal,
# so a candidate can reasonably be expected to sit close to it, not just
# within the same broad neighborhood. CONFIGURABLE.
#
# 15.0 -> 30.0, AND THE RIGHT VALUE WAS NOT PICKABLE UNTIL THE
# MEASUREMENT WAS FIXED. This gate used to be measured from the 0.1-acre
# PAD, and a pad is 20.1 m per side -- so a pad-based 15 m gate admitted
# a point up to 15 + 10.05 = 25.05 m from the corridor measured
# perpendicular, and ~29 m on a diagonal. The constant said 15 m; the
# requirement was really ~25 m, and the reported distance was 0.0 on nine
# of the eleven clearing footprints on the reference parcel, because the
# pad INTERSECTED the road (see _measure_footprint()). The gate now
# measures from the site's own POINT like the distance it gates, which
# takes that half-pad of slack away: at 15 m point-based the requirement
# is a real 15 m and is TIGHTER than what shipped, which nobody asked
# for.
#
# 30.0 m (98 ft) restores the reach the pad-based gate actually had
# (25.05 m) and adds to it, which is the increase this branch was asked
# for, and it is a plausible service-drive-and-conduit run from a routed
# corridor to a barn. MEASURED on the reference parcel: the clearing set
# goes 11 -> 14 pads, and the reported ft-to-road spreads over
# 8.6-98.4 ft instead of collapsing onto one value (every clearing
# candidate read 19.9-21.5 ft at 25 m and under, so the panel's headline
# figure could not tell two candidates apart). Still five times tighter
# than TIER 2's 150 m, which is the whole distinction between "this
# property's own routed alignment" and "somewhere near a mapped road".
ROAD_CORRIDOR_PROXIMITY_METERS = 30.0

# TIER 2 (fallback, only used when Tier 1 produces zero candidates — see
# module docstring): candidates must be within this distance of a real
# mapped road (farm_roads_data.py) instead. This is the original,
# pre-this-pass road-proximity buffer, demoted from primary to fallback.
# CONFIGURABLE.
ROAD_PROXIMITY_BUFFER_METERS = 150.0

# Buffer (meters) around the union of EVERY ranked tree-zone candidate
# (tree_zone_candidates.identify_tree_zone_candidates()'s own 'patches' —
# the full list, not just the top-ranked one, since trees has no single
# "selected" zone by design) within which a solar candidate footprint is
# HARD-excluded — see module docstring. Deliberately NOT the same
# constant as POND_ZONE_EXCLUSION_BUFFER_METERS (a dam-face/catchment-
# inlet clearance) or TREE_ROOT_ZONE_BUFFER_METERS (an existing-canopy
# root-zone clearance) even though the values may currently be close —
# this one means "structure-to-planned-tree-zone clearance" specifically
# and should be free to drift independently later. 10ft. CONFIGURABLE.
TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS = 10 * METERS_PER_FOOT

# How many top-ranked candidates to return. Deliberately more than 1 —
# per this feature's framing, ties/close calls should surface as multiple
# candidates for Claude to compare, not get silently collapsed into one.
# THREE, down from five, with the structures registry entry: the
# interactive step is select-only over these plus up to two sites the user
# PLACES themselves (see score_placed_structure_site()), and five generated
# footprints beside two placed ones is more shortlist than a small parcel's
# map has room to compare. The ranking is unchanged -- this is the same
# best-first list, cut two shorter -- and the dropped ranks 4 and 5 are
# reported on the reference parcel in test_structures_step.py.
# CONFIGURABLE.
MAX_CANDIDATES = 3

# --------------------------------------------------------------------
# THE TWO DRAINAGE GATES (hydric soil, floodplain)
# --------------------------------------------------------------------
# NO CONSTANTS, DELIBERATELY: both gates are geometry the caller hands
# in, and neither takes a buffer of its own. A hydric map unit's polygon
# and a floodplain's buffered stream band are already the ground they
# describe -- unlike planned tree zones or a pond site, where the
# clearance IS the judgement (TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS,
# POND_ZONE_EXCLUSION_BUFFER_METERS). A structure pad is excluded when it
# SITS on that ground, not when it comes near it, so there is no distance
# to tune and no constant to add.
#
# TWO GATES, NOT ONE, and that is the whole point of them. This module
# used to receive hydric_floodplain_union -- the two COMBINED, roads'
# own soft cost-penalty shape -- and forward it into its nested road and
# tree calls without ever consuming it, so structures had NO drainage
# constraint at all: its four factors are gentle ground, sun-facing,
# open to the sky and edge of production ground, all about solar and
# placement, and the only soil it read was prime farmland (to avoid
# siting on good cropland). Live testing found a structure sited inside a
# tree zone that scored for wet ground -- a building on poorly drained
# soil. Drainage under a foundation and flood risk around a building are
# DIFFERENT problems, a site can break either or both, and
# constraints_violated must name WHICH; a union cannot answer that, which
# is why road_corridors._fetch_floodplain_hydric_unions() now keeps the
# two halves apart and this module takes them as separate parameters.
#
# HARD FOR GENERATED, CAUTION FOR PLACED -- landform's rule exactly,
# applied to a point rather than a polygon. A GENERATED candidate is
# gated: it can never land on hydric soil or in a floodplain. A PLACED
# site is scored and committable as before, with the gate it broke named
# in constraints_violated -- the same treatment a user-drawn landform
# zone gets over excluded ground (it commits, and the crossing is
# recorded), and the same treatment this step already gives a placed site
# on the canopy block, which scores and names the gates it breaks.
#
# EITHER GATE MAY BE ABSENT, AND ABSENT IS NOT CLEAR. A None union means
# that source found nothing on this parcel OR never answered; the gate
# then has no entry in `constraints` at all rather than a trivially-True
# one, exactly as an unavailable road source or tree-zone polygon does
# (see _measure_footprint()). run_flags.drainage_gates_checked names the
# gates a run actually applied, so a candidate is never reported clear of
# a check that did not run.

# HOW THE COMBINED SOLAR VALUE READS AS A WORD -- "fair" / "good" /
# "great" / "excellent" -- on the two SOLAR factors together (aspect_
# score and shading_score, sun-facing and open-to-sky), rescaled to
# 0-100. Those two are HALF the composite by weight; the other half
# (slope, production proximity) is deliberately NOT in this rating, which
# is why it is published beside `suitability_score` rather than instead
# of it.
#
# NOT A RANK, and that distinction is the reason thresholds were chosen
# over ordering. With up to three generated candidates plus two placed
# ones, a "rating" that was really a ranking would force one of three
# sites to read "best" on a parcel where none of them is good, and would
# have nothing to say at five candidates. A band is an absolute
# statement: two parcels' "excellent" mean the same thing, and a PLACED
# site gets an honest rating rather than a slot in someone else's
# ordering.
#
# BAND BOUNDS: production_area_ceiling._SCORE_BANDS' convention exactly --
# lower-inclusive, upper-EXCLUSIVE, top band closing at 100 -- because
# solar_value is a float rounded to 1 decimal place and closed integer
# bands would leave 59.5 belonging to no band.
#
# THE CUTS ARE DELIBERATELY UNEVEN, and MEASURED rather than guessed.
# Even quarters are wrong here for the same reason production's are:
# the reference parcel's 534 measurable pads run 26.1 to 56.5 (p25 39.8,
# median 42.7, p75 44.9) -- a tight cluster, because a parcel's terrain
# has ONE prevailing aspect and the horizon barely moves across 13 acres
# -- while the values observed on the live reference run were 96.7 and
# 97.2, a tight cluster at the other end. So the distribution is tight
# WITHIN a parcel and wide BETWEEN parcels, and what the word has to do
# is separate parcels honestly, not manufacture spread inside one.
#
#   "fair" spans 60 points on purpose. Below the realistic midpoint the
#   ground is not solar ground, and the difference between a 26 and a 50
#   changes no decision -- the same argument that gives production's
#   "poor" a 40-point span. The bands NARROW going up (18, 12, 10),
#   because near the top a few points is a real difference: at 90+ a
#   site is within reach of ideal orientation under open sky, and at 60
#   it is facing sideways.
#
# ANCHORS, so the cuts can be argued with rather than only tuned.
# aspect_score is (1 + cos(aspect - 180))/2: 1.0 due south, 0.5 due
# east/west, 0.0 due north, and 1.0 on flat ground (no unfavourable
# orientation to penalise). shading_score on open ground runs about
# 0.78-1.0. So:
#   due east/west under a perfect horizon  -> (0.50 + 1.00)/2 = 75.0  "good"
#   southeast-facing under a good horizon  -> (0.85 + 0.90)/2 = 87.7  "great"
#   due south under a good horizon         -> (1.00 + 0.90)/2 = 95.0  "excellent"
#   north-facing open ground               -> (0.05 + 0.85)/2 = 45.0  "fair"
#
# CONFIGURABLE -- tune against a real property, same as every other
# threshold in this pipeline, and unvalidated starting values until one
# has been.
SOLAR_RATING_BANDS = {
    "fair": [0.0, 60.0],
    "good": [60.0, 78.0],
    "great": [78.0, 90.0],
    "excellent": [90.0, 100.0],
}

# HOW THE SOLAR RATING IS TO BE READ, as its own named sub-scale inside
# _SCALES below -- production's _ELEVATION_POSITION_SCALE pattern, for the
# same reason: it is a value on the block's 0-100 higher-is-better axis,
# but it describes only TWO of the four factors, and a consumer that took
# it for the composite would misread the panel.
_SOLAR_RATING_SCALE = {
    "range": [0.0, 100.0],
    "direction": "higher_is_better",
    "bands": SOLAR_RATING_BANDS,
    "band_bounds": "lower_inclusive_upper_exclusive_last_band_inclusive",
    "composed_of": ["aspect_score", "shading_score"],
    "weight_of_composite_pct": round((ASPECT_SCORE_WEIGHT + SHADING_SCORE_WEIGHT) * 100, 1),
    "not_a": "rank_among_candidates",
    "calibration": "unvalidated_starting_values",
    "applies_to": ["solar_value", "solar_rating"],
}

# WHERE A SITE SITS BETWEEN THE PARCEL'S LOWEST AND HIGHEST GROUND, as
# production's own bands -- IMPORTED, not redeclared (see the import
# above). 'direction' is not higher_is_better: an elevation percentile is
# a position, not a quality.
_ELEVATION_POSITION_SCALE = {
    "range": [0.0, 100.0],
    "direction": "higher_is_upslope",
    "bands": ELEVATION_POSITION_BANDS,
    "band_bounds": "lower_inclusive_upper_exclusive_last_band_inclusive",
    "applies_to": ["elevation_percentile_of_parcel", "elevation_position"],
}

# How every score and factor this module publishes is to be read --
# declared ONCE, on the wire, so the frontend never holds a threshold.
# production_area_ceiling.py's and road_corridors.py's own convention: one
# entry per value that is not on the block's primary scale, each declaring
# its own.
# NO BAND SET ON THE COMPOSITE, and that is a decision rather than an
# omission: this branch was asked for the SOLAR rating's cuts and nothing
# else, and production's own composite bands (_SCORE_BANDS there) are
# private to that module -- importing them would put production's
# "excellent" on a structures score built from entirely different
# factors. The composite's range and direction are declared; its words,
# if it ever needs any, are a later decision made against its own
# distribution.
_SCALES = {
    "range": [0.0, 100.0],
    "direction": "higher_is_better",
    "applies_to": ["score", "factors.*"],
    "solar_rating": _SOLAR_RATING_SCALE,
    "elevation_position": _ELEVATION_POSITION_SCALE,
    # A MEASUREMENT, NOT A SCORE, and it gets an entry for the one thing a
    # reader cannot infer from the number: which side of the block's edge
    # it is on. NEGATIVE means the site's own point sits INSIDE a block,
    # that many feet past the nearest edge; POSITIVE means outside;
    # exactly 0.0 means on the edge. null is "no blocks on this parcel",
    # never "on the edge".
    "signed_distance_to_production_ft": {
        "unit": "feet",
        "direction": "signed",
        "negative_means": "inside_a_block_this_far_past_its_nearest_edge",
        "positive_means": "outside_every_block_this_far_from_the_nearest_edge",
        "zero_means": "on_a_block_edge",
        "null_means": "no_production_blocks_on_this_parcel",
        "measured_from": "the site's own point, not its pad",
        "applies_to": ["location.signed_distance_to_production_ft"],
    },
}

SOLAR_CONFIDENCE_NOTES_TEMPLATE = (
    "This identifies a ranked CANDIDATE SITE for a small, fixed-footprint solar-generating "
    "structure (e.g. a barn or shed with rooftop panels) — NOT a large ground-mounted array, and "
    "NOT a final placement decision; see the report's Permanent Buildings section for tradeoffs "
    "against other ranked candidates. Each candidate is a real point sampled on a {spacing_m:.0f}m "
    "grid across the property, scored from real DEM-derived slope and aspect and a shading proxy "
    "averaged over a small local window matching its own capped footprint (at most "
    "{max_footprint_acres} acre(s), {footprint_side_m:.0f}m per side) — a small structure footprint, "
    "not a large connected-component eligible-area polygon. Shading is {shading_caveat} "
    "Candidates MAY sit fully inside a production zone — that is INTENTIONAL, not a caveat to "
    "apologize for: a small structure genuinely can coexist with production land around and under "
    "it, unlike a pond/dam site. Proximity to a production zone's own edge is a scored PREFERENCE, "
    "not a requirement (see properties.production_zone_relationship and "
    "properties.distance_to_production_zone_ft) — a candidate far from any production zone, or "
    "sitting deep inside a large one away from its edge, is still a valid, real candidate, just a "
    "lower-preference one. Water-candidate (pond/dam siting) zones ARE still hard-excluded "
    "(buffered) — a structure should not sit on or immediately against that ground. Real, EXISTING "
    "tree canopy (USGS 3DEP lidar) is ALSO hard-excluded (buffered by "
    "{canopy_buffer_ft:.0f}ft) — this check is MANDATORY and does not degrade; a canopy-data outage "
    "fails this run outright rather than silently skip it. Every ranked TREE-ZONE CANDIDATE "
    "(tree_zone_candidates.py, the full ranked list, not just the top one) is ALSO hard-excluded, "
    "buffered by {tree_zone_buffer_ft:.0f}ft{tree_zone_availability_note}. {drainage_gate_note}It also "
    "inherits the limitations of production_area_ceiling.py (a slope-only production-zone "
    "heuristic, ceiling-trimmed), water_candidate_zones.py (a DEM-derived valley/gradient "
    "heuristic), road_corridors.py (a DEM-only topographic suggestion, not a surveyed alignment), "
    "and farm_roads_data.py (public road/right-of-way data only — may miss private farm tracks). "
    "{road_proximity_note}{farmland_note}Treat this as a starting shortlist to walk and "
    "ground-truth, not a final site plan."
)

ROAD_PROXIMITY_NOTE_BY_SOURCE = {
    "selected_road_corridor": (
        "Road-proximity scoring (distance_to_road_ft) is measured against the property's own single "
        "SELECTED road corridor (road_corridors.py) — a real, ridge-routed topographic suggestion "
        "specific to this property, not a surveyed alignment — within "
        f"{ROAD_CORRIDOR_PROXIMITY_METERS:.0f}m. "
    ),
    "real_mapped_road": (
        "No selected road corridor was available (or nothing survived the constraint stack near it), "
        "so road-proximity scoring fell back to real mapped road data (farm_roads_data.py, public "
        f"road/right-of-way data only) within {ROAD_PROXIMITY_BUFFER_METERS:.0f}m instead. "
    ),
    "unavailable": (
        "Neither a selected road corridor nor real mapped road data was available for this run, so "
        "the road-proximity constraint is disabled entirely for these candidates — "
        "distance_to_road_ft is null. "
    ),
}

# THE TWO DRAINAGE GATES, in the shared confidence notes -- one sentence
# per state, because "gated and clear" and "never checked" are different
# statements about a site and a reader must not have to guess which one a
# silent note means. Keyed by the tuple of gate names the run applied,
# which is run_flags.drainage_gates_checked.
DRAINAGE_GATE_NOTE_BY_GATES_CHECKED = {
    ("outside_hydric_soil", "outside_floodplain"): (
        "Poorly drained (hydric) SSURGO soil and NHD floodplain are TWO INDEPENDENT HARD "
        "EXCLUSIONS for a GENERATED candidate: drainage under a foundation and flood risk around "
        "a building are different problems, so a generated site sits on neither. A site the USER "
        "PLACES is still scored and still committable on either ground, with the gate it broke "
        "named in properties.constraints_violated. "
    ),
    ("outside_hydric_soil",): (
        "Poorly drained (hydric) SSURGO soil is a HARD EXCLUSION for a GENERATED candidate (a "
        "placed site is scored anyway, with the gate named in properties.constraints_violated). "
        "NHD floodplain data was NOT available for this run, so these candidates are NOT confirmed "
        "clear of floodplain. "
    ),
    ("outside_floodplain",): (
        "NHD floodplain is a HARD EXCLUSION for a GENERATED candidate (a placed site is scored "
        "anyway, with the gate named in properties.constraints_violated). SSURGO hydric-soil data "
        "was NOT available for this run, so these candidates are NOT confirmed clear of poorly "
        "drained ground. "
    ),
    (): (
        "Neither SSURGO hydric-soil nor NHD floodplain data was available for this run, so the two "
        "drainage exclusions could not be checked -- these candidates are NOT confirmed clear of "
        "poorly drained or flood-prone ground. "
    ),
}

TREE_ZONE_EXCLUSION_UNAVAILABLE_NOTE = (
    " (tree-zone candidate data was not available for this run, so this exclusion could not be "
    "checked — candidates here are NOT confirmed clear of planned tree-zone ground)"
)

SHADING_CAVEAT_HORIZON_ONLY = (
    "estimated from a DEM-only horizon/terrain-shading proxy (terrain_metrics.py) — "
    "this has no way to see vegetation or tree canopy, since no canopy height model "
    "(DSM) or NDVI data was available/used for this run. A real canopy height model "
    "would be a meaningfully more accurate shading signal than this."
)


def _footprint_side_meters(max_structure_footprint_acres: float) -> float:
    """Side length of the square footprint a candidate is capped at —
    derived from the acreage cap rather than a separately-configured
    literal, so the two can never drift apart."""
    return math.sqrt(max_structure_footprint_acres * SQUARE_METERS_PER_ACRE)


def _slope_score(slope_pct: float, max_slope_pct: float) -> float:
    return max(0.0, 1.0 - slope_pct / max_slope_pct)


def _production_proximity_score(
    distance_to_production_edge_m: Optional[float],
    reference_meters: float = PRODUCTION_PROXIMITY_REFERENCE_METERS,
) -> float:
    """
    0-1 preference score for how close a candidate sits to a production
    zone's own EDGE (see PRODUCTION_PROXIMITY_REFERENCE_METERS above for
    why distance is measured to the boundary line, not the filled area):
    1.0 right at an edge, falling linearly to 0.0 at reference_meters or
    beyond — whether that's out past every production zone, or buried
    that deep inside a large one, away from its own boundary.

    None (no production zones exist on this property at all) scores a
    neutral 0.5 — there's no production geometry to be near or far from,
    so this axis shouldn't reward or penalize the candidate either way.
    """
    if distance_to_production_edge_m is None:
        return 0.5
    return max(0.0, 1.0 - distance_to_production_edge_m / reference_meters)


def _classify_production_zone_relationship(
    site_point,
    raw_production_union,
    distance_to_production_edge_m: Optional[float],
    adjacency_meters: float,
) -> str:
    """properties.production_zone_relationship: 'inside' if the site's own
    POINT sits on block ground, 'adjacent' if it doesn't but sits within
    adjacency_meters of a block's edge, else 'outside' (including the case
    where no production blocks exist on this property at all).

    FROM THE POINT, LIKE THE DISTANCE IT SITS BESIDE. This used to test
    the PAD's overlap while the distance was measured off the pad too;
    with the distance moved to the point, a pad test would let the two
    disagree -- a pad clipping a block's corner from outside would read
    'inside' beside a positive signed distance, which is a contradiction
    a reader cannot resolve. The point is also the honest reference for
    the panel, which draws the locator marker. The pad and the point
    differ only for a site within half a pad (about 10 m) of a block
    edge, and there 'adjacent' is the better word anyway."""
    if raw_production_union is not None and raw_production_union.contains(site_point):
        return "inside"
    if distance_to_production_edge_m is not None and distance_to_production_edge_m <= adjacency_meters:
        return "adjacent"
    return "outside"


def _combined_solar_value(aspect_score_value: float, shading_score_value: float) -> float:
    """
    The two SOLAR factors together, on the 0-100 axis SOLAR_RATING_BANDS
    cuts -- sun-facing and open-to-sky, weighted by their own scoring
    weights and renormalised so the result is a 0-100 value in its own
    right rather than a fraction of the composite.

    Weighted, not averaged, deliberately: the two weights happen to be
    equal today (0.25 each) and a plain mean would be numerically
    identical, but the rating is defined as "the solar half of the score",
    and reading the weights means retuning them retunes the rating with
    them instead of silently decoupling the word from the number it is
    supposed to describe.
    """
    weight_sum = ASPECT_SCORE_WEIGHT + SHADING_SCORE_WEIGHT
    combined = (ASPECT_SCORE_WEIGHT * aspect_score_value + SHADING_SCORE_WEIGHT * shading_score_value) / weight_sum
    return 100.0 * combined


def _solar_rating(solar_value: Optional[float]) -> Optional[str]:
    """
    SOLAR_RATING_BANDS' word for a combined solar value -- "fair" /
    "good" / "great" / "excellent" -- or None for a None value.

    Bounds are lower-inclusive / upper-exclusive with the top band closing
    at 100, read off the constant rather than hardcoded here, so retuning
    the bands retunes this function with them -- production_area_ceiling.
    _elevation_position()'s own shape, for its own reason.
    """
    if solar_value is None:
        return None
    value = float(solar_value)
    for word, (low, high) in sorted(SOLAR_RATING_BANDS.items(), key=lambda kv: kv[1][0]):
        if low <= value < high:
            return word
    # The top band's closing edge: 100.0 itself, which the exclusive upper
    # bound above cannot match. Both factors are clamped to [0, 1] where
    # they are computed, so nothing can fall outside [0, 100] -- this is
    # the last band, not a fallback for an out-of-range value.
    return max(SOLAR_RATING_BANDS.items(), key=lambda kv: kv[1][0])[0]


def _circular_mean_aspect_deg(aspect_values_deg: list[float]) -> Optional[float]:
    """Mean compass bearing via vector averaging (a plain arithmetic mean
    of e.g. 350 deg and 10 deg would wrongly give 180 instead of 0).
    Returns None if every input is undefined (an all-flat candidate)."""
    valid = [a for a in aspect_values_deg if not math.isnan(a)]
    if not valid:
        return None
    sin_sum = sum(math.sin(math.radians(a)) for a in valid)
    cos_sum = sum(math.cos(math.radians(a)) for a in valid)
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def _generate_candidate_points(
    boundary_polygon_utm: Polygon, spacing_meters: float = CANDIDATE_POINT_SPACING_METERS
) -> list[tuple[float, float]]:
    """
    Step 1 of the point-candidate model: samples candidate locations on a
    regular grid across the property's bounding box at spacing_meters
    spacing, keeping only points that fall on-parcel (boundary_polygon_utm
    is the real drawn parcel, not the DEM's buffered fetch extent — same
    on-parcel reasoning every other layer in this pipeline already uses).
    """
    minx, miny, maxx, maxy = boundary_polygon_utm.bounds
    boundary_prepared = prep(boundary_polygon_utm)

    points = []
    y = miny
    while y <= maxy:
        x = minx
        while x <= maxx:
            point = Point(x, y)
            if boundary_prepared.contains(point):
                points.append((x, y))
            x += spacing_meters
        y += spacing_meters
    return points


def _cells_within_polygon(dem: dict, polygon, rows: int, cols: int) -> list[tuple[int, int]]:
    """DEM cell (row, col) indices whose pixel-center point falls within
    polygon — the local scoring window for one candidate's footprint
    (Step 2). Scans only polygon's own bounding box (padded by one cell),
    not the whole grid: a ~0.1-acre footprint only ever touches a handful
    of cells, so this stays cheap even for a dense sample grid."""
    minx, miny, maxx, maxy = polygon.bounds
    px, py = dem["resolution_meters"]
    origin_x, origin_y = dem["origin_x"], dem["origin_y"]

    col_lo = max(0, int((minx - origin_x) / px) - 1)
    col_hi = min(cols - 1, int((maxx - origin_x) / px) + 1)
    row_lo = max(0, int((origin_y - maxy) / py) - 1)
    row_hi = min(rows - 1, int((origin_y - miny) / py) + 1)

    prepared = prep(polygon)
    cells = []
    for r in range(row_lo, row_hi + 1):
        for c in range(col_lo, col_hi + 1):
            if prepared.contains(Point(pixel_center_xy(dem, r, c))):
                cells.append((r, c))
    return cells


def _empty_rejection_tally() -> dict:
    """The shape find_candidate_solar_zones() fills into a caller-supplied
    `rejection_tally` -- see that parameter, and build_narrative_data()'s
    own `no_candidates` block, which is what it exists for."""
    return {"sampled": 0, "not_measurable": 0, "gates": {}, "cleared": 0}


def find_candidate_solar_zones(
    dem: dict,
    production_areas: list[dict],
    water_zones: list[dict],
    road_geometries_utm: Optional[list],
    boundary_polygon_utm: Polygon,
    canopy_mask_utm: Optional[np.ndarray] = None,
    tree_zone_exclusion_polygon_utm: Optional[object] = None,
    hydric_union_utm: Optional[object] = None,
    floodplain_union_utm: Optional[object] = None,
    rejection_tally: Optional[dict] = None,
    max_solar_slope_pct: float = MAX_SOLAR_SLOPE_PCT,
    min_suitability_score: float = MIN_SUITABILITY_SCORE,
    water_zone_exclusion_buffer_meters: float = POND_ZONE_EXCLUSION_BUFFER_METERS,
    road_proximity_buffer_meters: float = ROAD_PROXIMITY_BUFFER_METERS,
    max_structure_footprint_acres: float = MAX_STRUCTURE_FOOTPRINT_ACRES,
    candidate_point_spacing_meters: float = CANDIDATE_POINT_SPACING_METERS,
    production_edge_adjacency_meters: float = PRODUCTION_EDGE_ADJACENCY_METERS,
    production_proximity_reference_meters: float = PRODUCTION_PROXIMITY_REFERENCE_METERS,
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict]:
    """
    Pure point-candidate scoring core — see module docstring for why this
    takes already-computed inputs rather than fetching anything, and for
    why this samples independently-scored POINT candidates rather than
    computing one shared eligible-AREA polygon the way this module used
    to. Called ONCE PER ROAD TIER by identify_solar_candidate_zones() (see
    module docstring) — road_geometries_utm/road_proximity_buffer_meters
    are the generalized "road source geometry + its own proximity buffer"
    parameters that make that possible without duplicating the rest of
    this scoring core per tier.

    water_zones is water_suitability.py's own selected-zone shape (each
    entry carrying 'render_fill_polygon_utm', NOT 'polygon_utm' — see
    module docstring) — hard-excluded (buffered) exactly as before;
    production_areas is production_area_ceiling.identify_optimized_
    production_areas()'s own OPTIMIZED 'scored_patches' shape (each entry
    carrying 'render_fill_polygon_utm', NOT 'polygon_utm') but is NOT a
    hard exclusion here — see PRODUCTION_PROXIMITY_SCORE_WEIGHT above for
    how it's used instead (a scored edge-proximity preference).

    canopy_mask_utm is a per-cell boolean np.ndarray on the DEM's own
    grid (production_area.get_required_tree_root_zone_mask_utm()'s own
    output) or None. A candidate whose footprint touches ANY True cell is
    HARD-excluded before any scoring happens, same "excluded before
    scoring, not merely scored low" treatment as water. None means "gate
    not applied at all" — this pure-logic core's own default, useful for
    callers/tests that don't care about canopy (same convention tree_
    zone_candidates.score_tree_search_space() already uses for its own
    canopy mask parameter); identify_solar_candidate_zones() (the full
    pipeline entry point) always supplies a real mask, since its own
    canopy fetch is MANDATORY and does not degrade (see module
    docstring).

    tree_zone_exclusion_polygon_utm is an already-buffered shapely
    geometry (the union of every ranked tree-zone candidate's own
    'render_fill_polygon_utm', buffered by
    TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS — see module docstring)
    or None. A candidate footprint intersecting it is HARD-excluded, same
    pattern as the water exclusion below. None means either "gate not
    applied" (a caller/test that doesn't care) or "checked, but genuinely
    no tree-zone candidates exist on this property" — both cases result
    in no exclusion, which is correct either way; a fetch that failed
    outright is the caller's (identify_solar_candidate_zones()'s) own
    concern to flag in confidence_notes, not this pure core's.

    road_geometries_utm=None means "road data unavailable for this tier"
    (the fetch itself failed, or there's no selected road corridor at
    all) and disables the road-proximity constraint entirely for this
    call (with that noted by the caller); an empty list [] means
    "fetched successfully, no roads found nearby" and is treated as a
    real, binding constraint (nothing will qualify) — unchanged from
    before.

    boundary_polygon_utm is the real parcel (NOT the DEM's buffered
    extent — dem_data.py fetches ~100m past the drawn boundary on
    purpose). Each candidate's nominal (fixed-size) footprint is
    intersected with it — a footprint sampled near the boundary can come
    back smaller than the nominal cap, or be dropped entirely if too
    little survives (see MIN_STRUCTURE_FOOTPRINT_FRACTION). Before
    sampling even starts, the SEARCH REGION passed to
    _generate_candidate_points() is further restricted to
    boundary_polygon_utm.intersection(road_union.buffer(road_proximity_
    buffer_meters + footprint_side_m)) whenever the road constraint is
    active — a pure GENERATION-TIME optimization (avoids sampling points
    that can never survive the real per-footprint distance gate below),
    not a correctness change: that real gate still runs unchanged and is
    what actually decides eligibility, since a footprint can straddle the
    restricted region's own edge.

    EVERY REPORTED DISTANCE IS MEASURED FROM THE SITE'S OWN POINT -- the
    locator marker the user sees -- and not from its pad. The pad is still
    what is SCORED over (its cells' slope, aspect and shading) and what
    every hard exclusion gate tests, because a gate asks what ground the
    building occupies; a distance asks where the building is.

    THIS IS A FIX, NOT A PREFERENCE, AND ONE CAUSE PRODUCED TWO WRONG
    ANSWERS. shapely's .distance() returns 0.0 both when two geometries
    INTERSECT and when one CONTAINS the other, so a 0.1-acre pad gave:

      ROAD DISTANCE 0.0 EVERY TIME. The road-proximity constraint tunes
        candidates to sit close to a road, so the pad intersects the road
        on essentially every candidate -- nine of the eleven clearing
        footprints on the reference parcel read exactly 0.0 ft, and the
        other two read 0.3 and 4.9. "How far is this site from a road" is
        the panel's headline figure, and it was answering zero.

      A POSITIVE DISTANCE FROM INSIDE A BLOCK. Production distance is
        measured to a block's EDGE (deliberately -- see PRODUCTION_
        PROXIMITY_SCORE_WEIGHT), so a site well inside a block reports
        some feet "to production" and reads exactly like one that far
        OUTSIDE. Moving to the point fixes half of that; the other half is
        'signed_distance_to_production_m' below, whose SIGN says which
        side of the edge the site is on.

    The road GATE moved with the distance, for one definition of "how far
    is this site from a road" -- see _measure_footprint(). Water-zone
    distance moved too, for consistency; nothing reads it as a headline.

    Returns up to max_candidates entries, ranked best-first:
        {
            'rank': int,
            'suitability_score': float,        # 0-100
            'slope_score': float,               # 0-1 -- the composite's own four
            'aspect_score': float,              #   components, stored rather than
            'shading_score': float,             #   discarded (additive; see the
            'production_proximity_score': float,  # inline comment where they're set)
            'avg_slope_pct': float,
            'aspect_deg': Optional[float],      # None if the candidate is essentially flat
            'aspect_label': str,
            'solar_value': float,               # 0-100, the two SOLAR factors together
            'solar_rating': str,                # SOLAR_RATING_BANDS' word for it
            'elevation_percentile_of_parcel': Optional[float],  # 0 = the parcel's lowest
                                                #   ground, 100 = its highest; None on a
                                                #   parcel with no relief at all
            'elevation_position': Optional[str],  # the line above as words -- production's
                                                #   own ELEVATION_POSITION_BANDS, IMPORTED
            'distance_to_road_m': Optional[float],   # FROM THE POINT
            'distance_to_production_zone_m': Optional[float],  # to the nearest block EDGE, from
                                                #   the POINT, UNSIGNED; None only if no blocks
                                                #   exist at all
            'signed_distance_to_production_m': Optional[float],  # the same distance SIGNED:
                                                #   negative INSIDE a block, positive outside,
                                                #   0.0 on the edge, None if no blocks exist
            'production_zone_relationship': str,  # 'inside' | 'adjacent' | 'outside'
            'distance_to_water_zone_m': Optional[float],
            'footprint_area_acres': float,      # <= max_structure_footprint_acres; smaller only if boundary-clipped
            'polygon_utm': shapely Polygon,
            'geometry_wgs84': GeoJSON geometry dict,
        }
    """
    run = _prepare_scoring_run(
        dem,
        production_areas,
        water_zones,
        road_geometries_utm,
        boundary_polygon_utm,
        canopy_mask_utm=canopy_mask_utm,
        tree_zone_exclusion_polygon_utm=tree_zone_exclusion_polygon_utm,
        hydric_union_utm=hydric_union_utm,
        floodplain_union_utm=floodplain_union_utm,
        max_solar_slope_pct=max_solar_slope_pct,
        min_suitability_score=min_suitability_score,
        water_zone_exclusion_buffer_meters=water_zone_exclusion_buffer_meters,
        road_proximity_buffer_meters=road_proximity_buffer_meters,
        max_structure_footprint_acres=max_structure_footprint_acres,
        production_edge_adjacency_meters=production_edge_adjacency_meters,
        production_proximity_reference_meters=production_proximity_reference_meters,
    )

    # Generation-time-only optimization (see this function's own
    # docstring): restrict the sampled search region to stay near the
    # active road source, rather than scanning the full parcel. The real
    # per-footprint distance gate inside _measure_footprint() is unchanged
    # and is what actually decides eligibility -- this purely avoids
    # wasting a sample point on ground that gate could never let through.
    search_region = boundary_polygon_utm
    road_union = run["road_union"]
    if run["apply_road_constraint"] and road_union is not None and not road_union.is_empty:
        restricted = boundary_polygon_utm.intersection(
            road_union.buffer(road_proximity_buffer_meters + run["footprint_side_m"])
        )
        if not restricted.is_empty:
            search_region = restricted

    candidates = []
    tally = _empty_rejection_tally()

    for x, y in _generate_candidate_points(search_region, candidate_point_spacing_meters):
        tally["sampled"] += 1
        measured = _measure_footprint(x, y, run)
        if measured is None:
            # off-parcel, too little pad survives clipping, or no measurable DEM under it
            tally["not_measurable"] += 1
            continue
        # A GENERATED CANDIDATE PASSES EVERY GATE OR IS NOT ONE. The
        # conjunction the inline loop used to express as a chain of
        # `continue`s; the outcomes themselves are not stored on a
        # generated candidate (they are all True by construction), which
        # keeps its dict exactly the shape it has always been.
        constraints = measured.pop("constraints")
        failed = [name for name, satisfied in constraints.items() if not satisfied]
        if failed:
            # EVERY gate it failed, not just the first -- a fully gated
            # parcel has to be able to say which grounds left nothing, and
            # "the first gate in dict order" would name whichever one
            # happens to be tested earliest rather than the binding one.
            for name in failed:
                tally["gates"][name] = tally["gates"].get(name, 0) + 1
            continue
        tally["cleared"] += 1
        candidates.append(measured)

    if rejection_tally is not None:
        rejection_tally.clear()
        rejection_tally.update(tally)

    candidates.sort(key=lambda cand: -cand["suitability_score"])
    candidates = candidates[:max_candidates]
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank

    return candidates


# =====================================================================
# THE SCORING RUN, AND ONE FOOTPRINT'S MEASUREMENT
# =====================================================================
#
# find_candidate_solar_zones() used to hold its whole per-point loop
# inline. It is split into the two pieces below so a site the USER PLACES
# can be measured by exactly the code that measures a sampled candidate
# (score_placed_structure_site(), further down) rather than by a second
# scorer that would drift from the first:
#
#   _prepare_scoring_run()   everything computed ONCE per run: the terrain
#                            derivatives over the whole DEM, the production
#                            edge line, the buffered water exclusion, the
#                            road union, the footprint dimensions.
#   _measure_footprint()     the FULL measurement of one footprint against
#                            that run -- every field a candidate carries --
#                            PLUS the outcome of every hard constraint,
#                            keyed by the constraint's own wire name.
#
# BEHAVIOUR-PRESERVING FOR THE GENERATOR, and asserted rather than assumed
# (test_solar_suitability.py runs the fixtures it always has, unchanged).
# The generator keeps a footprint iff every constraint outcome is True --
# the same conjunction the inline loop expressed by `continue`-ing at the
# first failed gate. The one difference is cost, not outcome: a footprint
# the old loop dropped at its first failed gate now has its remaining
# gates evaluated too, over a handful of cells, before it is dropped. What
# is measured, how it is rounded and what is stored are unchanged; the
# candidate dict literal is the same literal, moved.


# THE SHADING SCORE, ONCE PER DEM. compute_shading_score() is a horizon
# search over every cell's southern arc -- 0.6 s on a 13-acre parcel at
# 5 m, and it grows with the cell count and the search radius. It reads
# the DEM and nothing else, yet _prepare_scoring_run() recomputed it on
# every structures generate AND on every placed-site scoring: a user
# dragging a structure pin paid for the whole grid again per drop, and
# test_structures_step.py paid for it thirty times on one unchanging DEM.
# Keyed on the array's bytes, shape, dtype and resolution, so a different
# DEM -- a different parcel, a re-fetched grid, a synthetic fixture with
# one cell changed -- is a different entry, and an identical one is a hit
# whatever object carries it. A few entries: a process serves a handful
# of sessions at a time and each session has one DEM. The cached array is
# marked read-only so a consumer that tried to write into it would raise
# rather than corrupt the next caller's copy; every reader below only
# indexes it.
_SHADING_CACHE_ENTRIES = 8
_SHADING_CACHE: "OrderedDict[tuple, np.ndarray]" = OrderedDict()


def _shading_score_for_dem(array: np.ndarray, resolution_meters) -> np.ndarray:
    contiguous = np.ascontiguousarray(array)
    key = (
        hashlib.blake2b(contiguous.tobytes(), digest_size=16).hexdigest(),
        contiguous.shape,
        str(contiguous.dtype),
        tuple(float(r) for r in resolution_meters),
    )
    cached = _SHADING_CACHE.get(key)
    if cached is not None:
        _SHADING_CACHE.move_to_end(key)
        return cached
    shading = compute_shading_score(array, resolution_meters)
    shading.flags.writeable = False
    _SHADING_CACHE[key] = shading
    while len(_SHADING_CACHE) > _SHADING_CACHE_ENTRIES:
        _SHADING_CACHE.popitem(last=False)
    return shading


def _prepare_scoring_run(
    dem: dict,
    production_areas: list[dict],
    water_zones: list[dict],
    road_geometries_utm: Optional[list],
    boundary_polygon_utm: Polygon,
    canopy_mask_utm: Optional[np.ndarray] = None,
    tree_zone_exclusion_polygon_utm: Optional[object] = None,
    hydric_union_utm: Optional[object] = None,
    floodplain_union_utm: Optional[object] = None,
    max_solar_slope_pct: float = MAX_SOLAR_SLOPE_PCT,
    min_suitability_score: float = MIN_SUITABILITY_SCORE,
    water_zone_exclusion_buffer_meters: float = POND_ZONE_EXCLUSION_BUFFER_METERS,
    road_proximity_buffer_meters: float = ROAD_PROXIMITY_BUFFER_METERS,
    max_structure_footprint_acres: float = MAX_STRUCTURE_FOOTPRINT_ACRES,
    production_edge_adjacency_meters: float = PRODUCTION_EDGE_ADJACENCY_METERS,
    production_proximity_reference_meters: float = PRODUCTION_PROXIMITY_REFERENCE_METERS,
) -> dict:
    """The per-run state one measurement reads. Parameters are
    find_candidate_solar_zones()'s own, with the same meanings."""
    array = dem["array"]
    resolution = dem["resolution_meters"]
    rows, cols = array.shape

    slope_pct, aspect_deg = compute_slope_and_aspect(array, resolution)
    shading = _shading_score_for_dem(array, resolution)

    raw_production_union = (
        unary_union([p["render_fill_polygon_utm"] for p in production_areas]) if production_areas else None
    )
    production_boundary_geom = raw_production_union.boundary if raw_production_union is not None else None

    raw_water_union = (
        unary_union([z["render_fill_polygon_utm"] for z in water_zones]) if water_zones else None
    )
    water_exclusion = (
        raw_water_union.buffer(water_zone_exclusion_buffer_meters) if raw_water_union is not None else None
    )

    road_union = unary_union(road_geometries_utm) if road_geometries_utm else None
    apply_road_constraint = road_geometries_utm is not None  # None = data unavailable, don't apply

    # THE PARCEL'S OWN ELEVATION RANGE, once per run -- what every
    # candidate's elevation_percentile_of_parcel is measured against.
    # Against the WHOLE parcel, not the road-restricted search region: a
    # site's elevation position is a statement about where it sits on the
    # PROPERTY ("upper field"), so the ground it is positioned against has
    # to be the whole property, exactly as tree_zone_candidates.py argues
    # for its own patches.
    #
    # None -- never a range of zero width -- on a parcel with no relief at
    # all, where "upper" and "lower" name nothing. That None carries
    # through to every candidate's percentile and from there to its
    # position word; see production_area_ceiling._elevation_position() for
    # why a default word is never the answer.
    #
    # PURE AND LOCAL, no fetch: the DEM is already in hand and the mask is
    # one vectorised contains_xy() call over the grid, through the same
    # imported helper production and trees both use.
    parcel_elevations = array[_on_parcel_cell_mask(dem, boundary_polygon_utm)]
    parcel_elevations = parcel_elevations[~np.isnan(parcel_elevations)]
    if parcel_elevations.size and float(parcel_elevations.max()) > float(parcel_elevations.min()):
        parcel_elevation_range = (float(parcel_elevations.min()), float(parcel_elevations.max()))
    else:
        parcel_elevation_range = None

    footprint_side_m = _footprint_side_meters(max_structure_footprint_acres)
    min_footprint_area_m2 = (
        max_structure_footprint_acres * SQUARE_METERS_PER_ACRE * MIN_STRUCTURE_FOOTPRINT_FRACTION
    )

    return {
        "dem": dem,
        "rows": rows,
        "cols": cols,
        "slope_pct": slope_pct,
        "aspect_deg": aspect_deg,
        "shading": shading,
        "boundary_polygon_utm": boundary_polygon_utm,
        "raw_production_union": raw_production_union,
        "production_boundary_geom": production_boundary_geom,
        "raw_water_union": raw_water_union,
        "water_exclusion": water_exclusion,
        "road_union": road_union,
        "apply_road_constraint": apply_road_constraint,
        "road_proximity_buffer_meters": road_proximity_buffer_meters,
        "canopy_mask_utm": canopy_mask_utm,
        "tree_zone_exclusion_polygon_utm": tree_zone_exclusion_polygon_utm,
        "hydric_union_utm": hydric_union_utm,
        "floodplain_union_utm": floodplain_union_utm,
        "parcel_elevation_range": parcel_elevation_range,
        "array": array,
        "max_solar_slope_pct": max_solar_slope_pct,
        "min_suitability_score": min_suitability_score,
        "footprint_side_m": footprint_side_m,
        "min_footprint_area_m2": min_footprint_area_m2,
        "production_edge_adjacency_meters": production_edge_adjacency_meters,
        "production_proximity_reference_meters": production_proximity_reference_meters,
    }


def _measure_footprint(x: float, y: float, run: dict) -> Optional[dict]:
    """
    The full measurement of the fixed-size footprint centred on (x, y),
    against a prepared run. Returns None when the footprint is NOT
    MEASURABLE AT ALL -- off-parcel, too little of the nominal pad survives
    clipping to the parcel (MIN_STRUCTURE_FOOTPRINT_FRACTION), no DEM cell
    centre under it, or no cell with a defined slope (Horn's method needs
    a full 3x3 neighbourhood, so an edge/nodata-adjacent footprint can end
    up empty). Those are absences of measurement, not failed gates, and a
    caller cannot report a score for them.

    Otherwise returns the candidate dict find_candidate_solar_zones() has
    always built -- every measured field, the four stored factor scores,
    the composite -- plus ONE extra key, `constraints`: {wire name -> bool}
    for every hard constraint the run applied, in the order the generator
    tests them. The keys are the exact strings structure_sites_to_feature_
    collection() publishes under `constraints_satisfied`, so a placed
    site's outcomes reach the wire under the names a generated candidate's
    guarantees already use. A constraint the run does NOT apply (no road
    source, no tree-zone exclusion polygon, no hydric or floodplain union)
    has no key -- absent, not trivially True -- for the same reason an
    unavailable exclusion gate is omitted from a crossing record rather
    than reported clear.

    TWO GEOMETRIES, TWO JOBS, and the split is the whole of this pass's
    first change. THE PAD -- the clipped, fixed-size footprint -- is what
    is scored over (its cells' slope, aspect and shading) and what EVERY
    HARD GATE tests, because a gate asks what ground the building
    occupies. THE POINT -- (x, y), the locator marker the user sees on the
    map -- is what EVERY REPORTED DISTANCE is measured from, because a
    distance asks where the building is. The road gate is the one
    exception to the first half and deliberately so: it gates on the
    POINT, so that the gate and the figure it bounds are one definition of
    "how far is this site from a road" rather than two. See
    find_candidate_solar_zones() for the two wrong answers a pad-based
    measurement produced and why one cause explains both.
    """
    dem = run["dem"]
    footprint_side_m = run["footprint_side_m"]
    # THE SITE'S OWN POINT -- the locator marker the user sees on the map,
    # and what EVERY REPORTED DISTANCE below is measured from. See this
    # function's DISTANCES ARE MEASURED FROM THE POINT note.
    point = Point(x, y)
    nominal_footprint = box(
        x - footprint_side_m / 2, y - footprint_side_m / 2, x + footprint_side_m / 2, y + footprint_side_m / 2
    )

    footprint = nominal_footprint.intersection(run["boundary_polygon_utm"])
    if footprint.is_empty or footprint.area < run["min_footprint_area_m2"]:
        return None  # off-parcel, or too little of the nominal footprint survives clipping to be a real pad

    cells = _cells_within_polygon(dem, footprint, run["rows"], run["cols"])
    if not cells:
        return None  # no DEM data at all under this footprint

    slope_pct = run["slope_pct"]
    cell_slopes = [float(slope_pct[r, c]) for r, c in cells if not math.isnan(slope_pct[r, c])]
    if not cell_slopes:
        return None  # Horn's method needs a full 3x3 neighborhood -- an edge/nodata-adjacent footprint can end up empty here
    avg_slope_pct = float(np.mean(cell_slopes))

    water_exclusion = run["water_exclusion"]
    tree_zone_exclusion_polygon_utm = run["tree_zone_exclusion_polygon_utm"]
    hydric_union_utm = run["hydric_union_utm"]
    floodplain_union_utm = run["floodplain_union_utm"]
    canopy_mask_utm = run["canopy_mask_utm"]
    road_union = run["road_union"]
    max_solar_slope_pct = run["max_solar_slope_pct"]

    # THE HARD CONSTRAINTS, in the generator's own order, each an outcome
    # rather than an exit. See the docstring for why an unapplied one has
    # no key.
    constraints = {
        # a structure can't sit on/against pond-siting ground
        "outside_water_candidate_zone": not (
            water_exclusion is not None and footprint.intersects(water_exclusion)
        ),
    }
    # THE TWO DRAINAGE GATES, SEPARATELY -- drainage under a foundation and
    # flood risk around a building are different problems and a site can
    # break either or both, so each is its own named outcome and the
    # violated one reaches the wire by name. Tested against the PAD, not
    # the point: what a gate asks is what ground the BUILDING occupies,
    # which is the pad, while a reported DISTANCE asks where the building
    # is, which is the point. A None union is a gate the run did not apply
    # -- no key at all rather than a trivially-True one -- so a site is
    # never reported clear of a check that never ran. See THE TWO DRAINAGE
    # GATES at the top of this module.
    if hydric_union_utm is not None:
        constraints["outside_hydric_soil"] = not footprint.intersects(hydric_union_utm)
    if floodplain_union_utm is not None:
        constraints["outside_floodplain"] = not footprint.intersects(floodplain_union_utm)
    if tree_zone_exclusion_polygon_utm is not None:
        # a structure shouldn't sit on/against planned tree-zone ground
        constraints["outside_tree_zone_candidate_buffer"] = not footprint.intersects(
            tree_zone_exclusion_polygon_utm
        )
    # before any scoring -- a footprint touching real, existing tree canopy
    constraints["outside_existing_canopy"] = not (
        canopy_mask_utm is not None and any(canopy_mask_utm[r, c] for r, c in cells)
    )
    # too steep to build on -- a real buildability ceiling, independent of production-zone proximity
    constraints[f"max_slope<={max_solar_slope_pct:.0f}pct"] = avg_slope_pct <= max_solar_slope_pct
    if run["apply_road_constraint"]:
        # FROM THE POINT, like the distance it gates -- one definition of
        # "how far is this site from a road", used by the gate and by the
        # reported figure alike. Measuring the gate off the pad while
        # reporting the point's distance would let a candidate whose pad
        # clips the road report 60 ft and still pass a 15 m gate it never
        # actually cleared.
        constraints["within_road_proximity_buffer"] = (
            road_union is not None and point.distance(road_union) <= run["road_proximity_buffer_meters"]
        )

    aspect_deg = run["aspect_deg"]
    cell_aspects = [float(aspect_deg[r, c]) for r, c in cells if not math.isnan(aspect_deg[r, c])]
    mean_aspect = _circular_mean_aspect_deg(cell_aspects)
    a_score = aspect_score(mean_aspect if mean_aspect is not None else float("nan"))

    shading = run["shading"]
    cell_shading = [float(shading[r, c]) for r, c in cells if not math.isnan(shading[r, c])]
    sh_score = float(np.mean(cell_shading)) if cell_shading else 0.5

    s_score = _slope_score(avg_slope_pct, max_solar_slope_pct)

    # DISTANCE TO THE NEAREST BLOCK'S EDGE, FROM THE POINT. Still to the
    # BOUNDARY LINE rather than the filled area (see PRODUCTION_PROXIMITY_
    # SCORE_WEIGHT for why the score peaks at an edge and falls off in both
    # directions), and still the scoring input unchanged -- only the
    # geometry it is measured from moved from the pad to the point.
    production_boundary_geom = run["production_boundary_geom"]
    distance_to_production_edge_m = (
        float(point.distance(production_boundary_geom)) if production_boundary_geom is not None else None
    )
    p_score = _production_proximity_score(
        distance_to_production_edge_m, run["production_proximity_reference_meters"]
    )
    # AND WHICH SIDE OF THAT EDGE THE SITE IS ON, as the distance's SIGN.
    # "0 ft to production" and "inside the block" read differently, and an
    # unsigned distance to an edge says neither: a site buried 200 ft
    # inside a block reports 200 ft and reads exactly like one 200 ft
    # outside. Negative is inside, positive is outside, 0.0 is on the
    # edge, None is "no blocks on this parcel" -- see the signed_distance_
    # to_production_ft entry in _SCALES. The sign is the POINT's
    # containment, which is also what production_zone_relationship reports,
    # so the two can never disagree.
    inside_production = (
        run["raw_production_union"] is not None and run["raw_production_union"].contains(point)
    )
    signed_distance_to_production_m = (
        None
        if distance_to_production_edge_m is None
        else (-distance_to_production_edge_m if inside_production else distance_to_production_edge_m)
    )

    combined = (
        SLOPE_SCORE_WEIGHT * s_score
        + ASPECT_SCORE_WEIGHT * a_score
        + SHADING_SCORE_WEIGHT * sh_score
        + PRODUCTION_PROXIMITY_SCORE_WEIGHT * p_score
    )
    min_suitability_score = run["min_suitability_score"]
    constraints[f"suitability_score>={min_suitability_score * 100:.0f}"] = combined >= min_suitability_score

    relationship = _classify_production_zone_relationship(
        point,
        run["raw_production_union"],
        distance_to_production_edge_m,
        run["production_edge_adjacency_meters"],
    )

    raw_water_union = run["raw_water_union"]
    distance_to_water_zone_m = (
        float(point.distance(raw_water_union)) if raw_water_union is not None else None
    )
    # THE PANEL'S HEADLINE FIGURE, AND THE BRANCH'S REASON TO EXIST. From
    # the POINT. Measured off the pad it was 0.0 on essentially every
    # candidate: the road-proximity constraint tunes candidates to sit
    # close to a road, the 0.1-acre pad therefore INTERSECTS the road, and
    # shapely's .distance() returns 0.0 for intersecting geometry -- so
    # "how far is this site from a road" answered zero every time, for nine
    # of eleven clearing footprints on the reference parcel.
    distance_to_road_m = float(point.distance(road_union)) if road_union is not None else None

    # WHERE THIS SITE SITS IN THE PARCEL'S ELEVATION RANGE, 0 = the
    # parcel's lowest ground, 100 = its highest -- the same linear position
    # production_area_ceiling.py and tree_zone_candidates.py both publish
    # under this same name, off this pad's own mean elevation. None on a
    # parcel with no relief. Clamped to [0, 100] because a pad can cover a
    # cell the on-parcel mask excluded (a cell centre just outside the
    # boundary whose square still intersects it).
    parcel_elevation_range = run["parcel_elevation_range"]
    if parcel_elevation_range is None:
        elevation_percentile = None
    else:
        low, high = parcel_elevation_range
        mean_elevation = float(np.mean([float(run["array"][r, c]) for r, c in cells]))
        elevation_percentile = round(
            max(0.0, min(100.0, (mean_elevation - low) / (high - low) * 100.0)), 1
        )

    # THE COMBINED SOLAR VALUE AND ITS WORD -- the two solar factors
    # together on the 0-100 axis, and SOLAR_RATING_BANDS' band for it.
    # Stored rather than derived downstream, for the same reason the four
    # factor scores are: the narrative and the wire both read it off the
    # candidate instead of re-deriving it, and the bands live in exactly
    # one place.
    solar_value = round(_combined_solar_value(a_score, sh_score), 1)

    geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(footprint))

    return {
        "suitability_score": round(combined * 100, 1),
        # The composite's own four components, stored (0-1, same
        # native scale as every other *_factor field in this
        # pipeline) rather than discarded -- purely additive to
        # the candidate shape, so "why did this site score what
        # it did" can be answered downstream (narrative_data,
        # tests) without re-running any scoring.
        "slope_score": round(s_score, 3),
        "aspect_score": round(a_score, 3),
        "shading_score": round(sh_score, 3),
        "production_proximity_score": round(p_score, 3),
        "avg_slope_pct": round(avg_slope_pct, 1),
        "aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
        "aspect_label": aspect_to_compass_label(mean_aspect) if mean_aspect is not None else "flat",
        "solar_value": solar_value,
        "solar_rating": _solar_rating(solar_value),
        "elevation_percentile_of_parcel": elevation_percentile,
        "elevation_position": _elevation_position(elevation_percentile),
        "distance_to_road_m": round(distance_to_road_m, 1) if distance_to_road_m is not None else None,
        "distance_to_production_zone_m": (
            round(distance_to_production_edge_m, 1) if distance_to_production_edge_m is not None else None
        ),
        "signed_distance_to_production_m": (
            round(signed_distance_to_production_m, 1)
            if signed_distance_to_production_m is not None
            else None
        ),
        "production_zone_relationship": relationship,
        "distance_to_water_zone_m": (
            round(distance_to_water_zone_m, 1) if distance_to_water_zone_m is not None else None
        ),
        "footprint_area_acres": round(footprint.area / SQUARE_METERS_PER_ACRE, 3),
        "polygon_utm": footprint,
        "geometry_wgs84": geometry_wgs84,
        "constraints": constraints,
    }


# What a PLACED site is tagged with, on its internal dict and on the wire,
# so a reader holding a mixed list can tell it from a generated candidate
# without consulting the document's provenance. A generated candidate
# carries no site_origin key at all -- its dict is unchanged by placed
# sites existing -- and wire_translation stamps "generated" on its
# feature for it.
SITE_ORIGIN_USER_PLACED = "user_placed"
SITE_ORIGIN_GENERATED = "generated"


def measure_structure_site(
    x_utm: float,
    y_utm: float,
    dem: dict,
    production_areas: list[dict],
    water_zones: list[dict],
    road_geometries_utm: Optional[list],
    boundary_polygon_utm: Polygon,
    canopy_mask_utm: Optional[np.ndarray] = None,
    tree_zone_exclusion_polygon_utm: Optional[object] = None,
    hydric_union_utm: Optional[object] = None,
    floodplain_union_utm: Optional[object] = None,
    **thresholds,
) -> Optional[dict]:
    """
    THE SECOND ENTRY INTO THE SCORER: one footprint, centred on a point the
    caller chose, measured against the same inputs and the same code as a
    sampled candidate. Takes find_candidate_solar_zones()'s own parameters
    (minus the sampling-only ones -- spacing and max_candidates mean
    nothing for a single point) and returns _measure_footprint()'s dict
    WITH its `constraints` key, or None when the spot is not measurable at
    all (see that function).

    NOT DROPPED FOR A FAILED GATE, and that is the whole difference from
    the generator. A sampled point that fails the slope ceiling is simply
    not a candidate; a point the user placed and asked about IS the
    question, and "this spot averages 27% slope, above the 20% ceiling" is
    the answer -- so every gate's outcome comes back beside the score.

    hydric_union_utm/floodplain_union_utm are the run's own two drainage
    gates, forwarded like every other exclusion: a placed site on either
    ground is MEASURED AND KEPT, with that gate named in its
    `constraints` -- never dropped, which is the whole point of this
    entry.

    PURE AND LOCAL: one Horn pass and one shading pass over the cached
    DEM, a handful of shapely predicates against geometry already in hand,
    one reprojection out. No network, asserted in test_structures_step.py
    under a socket guard.
    """
    run = _prepare_scoring_run(
        dem,
        production_areas,
        water_zones,
        road_geometries_utm,
        boundary_polygon_utm,
        canopy_mask_utm=canopy_mask_utm,
        tree_zone_exclusion_polygon_utm=tree_zone_exclusion_polygon_utm,
        hydric_union_utm=hydric_union_utm,
        floodplain_union_utm=floodplain_union_utm,
        **thresholds,
    )
    return _measure_footprint(x_utm, y_utm, run)


def score_placed_structure_site(lon_lat, result: dict) -> dict:
    """
    A site the USER PLACED, scored against the run that produced `result`
    -- identify_solar_candidate_zones()'s own return dict -- so it carries
    the FULL CANDIDATE MEASUREMENT SET, composite score included, and is
    directly comparable to the generated candidates beside it.

    THIS IS A DELIBERATE DIVERGENCE FROM TREES, AND THE REASONING IS
    DIFFERENT, so the next reader does not "fix" it into consistency. Trees
    refuses to score a drawn zone (wire_translation.rehydrate_tree_zone():
    a zone scoring below the floor would read as scored BADLY rather than
    as UNSCORED, and a drawn tree zone competes for a ranking slot it never
    entered). A placed structure site is the opposite case: the user is not
    proposing a candidate for the ranking, they are saying "I want the
    building HERE -- tell me about this spot." An honest score, with every
    gate's outcome beside it, IS the answer they asked for, and withholding
    it would waste a measurement this module can make for free. So a
    placed site is measured by the same code as a sampled candidate, kept
    whether or not it clears the gates, and told which ones it failed.

    `lon_lat` is the placed point, WGS84. `result` must carry `run_inputs`
    (every input the run scored against, natively) and
    `all_scored_candidates` (the generated shortlist), both of which
    identify_solar_candidate_zones() puts there -- the placed site is
    scored against the SAME production ground, water exclusion, road tier,
    canopy mask and tree-zone exclusion the generated candidates were,
    which is the only thing that makes the comparison honest.

    Raises ValueError, naming the reason, for a point off the parcel (the
    one hard gate) or a point with no measurable DEM under it. Never
    returns a half-measured site.

    WHAT IT ADDS TO THE CANDIDATE SHAPE, and nothing else:

        site_origin      "user_placed" -- the tag that distinguishes it
                         from a generated candidate in any mixed list;
                         the document's provenance ("user_added") is the
                         same fact recorded by the commit
        placed_lon_lat   the point as placed, [lon, lat]
        point_utm        the same point in the DEM's CRS
        constraints      {wire name -> bool}, every hard gate the run
                         applied, from _measure_footprint() -- including
                         the two DRAINAGE gates, so a site on hydric soil
                         or in a floodplain is told which one it broke
                         rather than refused. A generated candidate can
                         never land on either; a placed one commits with
                         the crossing recorded. Landform's rule exactly,
                         applied to a point rather than a polygon
        rank             WHERE THIS SPOT WOULD SIT in the generated
                         shortlist: 1 + the number of generated candidates
                         that score strictly higher. Rank 1 means better
                         than every generated candidate; a tie goes to the
                         generated one. The generated candidates' own ranks
                         are NOT re-numbered -- they are the run's ranking
                         and stay stable -- so two placed sites may share
                         a rank with each other or with a generated
                         candidate. `rank` is therefore a comparison on a
                         placed site and an identity on a generated one;
                         site_origin says which reading applies.
        prime_farmland_conflict / prime_farmland_note
                         INHERITED from the run, when the run checked it.
                         The SSURGO flag is parcel-level ("prime soil was
                         found somewhere in this boundary" -- see
                         flag_prime_farmland_conflicts()), so every site on
                         the parcel carries the same answer, and re-asking
                         SSURGO for a placed point would be a network call
                         for a value already in hand.
    """
    run_inputs = result.get("run_inputs")
    if not run_inputs:
        raise ValueError(
            "score_placed_structure_site() needs the run's own inputs (result['run_inputs']); the result "
            "handed in does not carry them, so a placed site could not be scored against the same run"
        )
    dem = run_inputs["dem"]
    boundary_polygon_utm = run_inputs["boundary_polygon_utm"]

    lon, lat = float(lon_lat[0]), float(lon_lat[1])
    xs, ys = warp_transform("EPSG:4326", dem["crs"], [lon], [lat])
    point_utm = Point(xs[0], ys[0])
    if not boundary_polygon_utm.contains(point_utm):
        raise ValueError(
            f"placed site [{lon:.6f}, {lat:.6f}] lies outside the parcel boundary. The parcel is the one "
            "hard limit on where a structure can be placed; everything else this step checks is measured "
            "and reported, never refused."
        )

    measured = measure_structure_site(
        point_utm.x,
        point_utm.y,
        dem,
        run_inputs["production_areas"],
        run_inputs["water_zones"],
        run_inputs["road_geometries_utm"],
        boundary_polygon_utm,
        canopy_mask_utm=run_inputs["canopy_mask_utm"],
        tree_zone_exclusion_polygon_utm=run_inputs["tree_zone_exclusion_polygon_utm"],
        # THE TWO DRAINAGE GATES, from the run the generated candidates
        # were scored against -- which is what makes "this spot is on
        # hydric soil" a statement about the same data the shortlist was
        # built from. A placed site is NOT dropped for failing one: it is
        # scored, committable, and told which gate it broke.
        hydric_union_utm=run_inputs["hydric_union_utm"],
        floodplain_union_utm=run_inputs["floodplain_union_utm"],
        **run_inputs["thresholds"],
    )
    if measured is None:
        raise ValueError(
            f"placed site [{lon:.6f}, {lat:.6f}] cannot be measured: its footprint either keeps less than "
            f"{MIN_STRUCTURE_FOOTPRINT_FRACTION:.0%} of a building pad inside the parcel or covers no DEM "
            "cell with a defined slope. Place it a little further inside the boundary."
        )

    generated = result.get("all_scored_candidates") or []
    measured["rank"] = 1 + sum(
        1 for candidate in generated if candidate["suitability_score"] > measured["suitability_score"]
    )
    measured["site_origin"] = SITE_ORIGIN_USER_PLACED
    measured["placed_lon_lat"] = [lon, lat]
    measured["point_utm"] = point_utm
    if generated and "prime_farmland_conflict" in generated[0]:
        measured["prime_farmland_conflict"] = generated[0]["prime_farmland_conflict"]
        measured["prime_farmland_note"] = generated[0]["prime_farmland_note"]
    return measured


def flag_prime_farmland_conflicts(
    candidates: list[dict], farmland_classifications: list[dict]
) -> list[dict]:
    """
    Pure post-processing step: checks each candidate's polygon against
    SSURGO farmland classification and adds 'prime_farmland_conflict'
    (bool) and 'prime_farmland_note' (str) to each candidate dict, in
    place, and returns it. Does NOT exclude or re-rank anything — a
    technically great solar site on prime farmland is still flagged, not
    dropped, per the Scale of Permanence tension between competing land
    uses this feature is explicitly meant to surface, not resolve.
    UNCHANGED by the point-candidate redesign.

    farmland_classifications is soil_data.get_farmland_classification_for_
    polygon()'s output — this function takes it pre-fetched (no network
    here) so it's unit-testable with a synthetic list.

    This function only ever sets prime_farmland_conflict based on whether
    ANY prime-farmland map unit was found intersecting the boundary this
    classification data was fetched for — it doesn't have per-candidate
    soil geometry to check individually (SSURGO map unit polygons, not
    just their farmland-class attribute, would be needed for that), so a
    "yes" here means "somewhere in the area this data covers," not
    necessarily "exactly under this polygon." That's stated in the note
    added to each flagged candidate, not left implicit.
    """
    any_prime = any(is_prime_farmland(c.get("farmland_classification")) for c in farmland_classifications)

    for candidate in candidates:
        candidate["prime_farmland_conflict"] = any_prime
        if any_prime:
            candidate["prime_farmland_note"] = (
                "Prime (or conditionally prime) farmland soil was found in this area per "
                "SSURGO — this candidate may sit on or near land better suited to production "
                "than solar infrastructure. This is a tradeoff to weigh, not an exclusion."
            )
        else:
            candidate["prime_farmland_note"] = "No prime farmland classification found in this area per SSURGO."

    return candidates


def select_optimal_structure_site(scored_candidates: list[dict]) -> Optional[dict]:
    """
    Explicit selection step on top of find_candidate_solar_zones()'s own
    ranking: returns the single candidate with rank == 1 (highest
    suitability_score) -- no logic beyond that. Same pattern as
    water_suitability.select_optimal_water_zone() and
    road_corridors.select_optimal_road_corridor(); per product decision,
    this app targets small farms only, where one well-suited structure
    site is sufficient -- no multi-candidate coexistence logic is needed
    here.

    Deliberately does NOT attempt to reconcile this selection with
    road_corridors.select_optimal_road_corridor() beyond the two-tier
    road-proximity constraint identify_solar_candidate_zones() already
    applies (selected corridor primary, real mapped roads as fallback --
    see module docstring). That interplay is deliberately deferred, same
    as the fencing/roads-and-structures interplay already deferred
    elsewhere in this pipeline, until real results from both selections
    independently are available to look at.

    Returns None if scored_candidates is empty -- a real, reportable "no
    candidates at all" outcome, not an error.
    """
    if not scored_candidates:
        return None
    return max(scored_candidates, key=lambda c: c["suitability_score"])


# =====================================================================
# NARRATIVE DATA -- report-facing, FINAL values only
# =====================================================================
# Everything below exists to answer TWO report questions about this
# module's deliverable, and nothing else:
#
#   1. WHERE is it on the map?
#   2. WHAT ARE THE BENEFITS of placing a permanent building here?
#
# "It" is the SELECTED structure site (select_optimal_structure_site()'s
# single rank-1 answer) -- per the narrative_data convention's
# winner-only nuance, this block describes the winner's own package;
# candidate_count is the one light comparison-level value, and it is
# available here precisely because this module's own entry point returns
# the full ranked list alongside the winner.
#
# The same two hard rules production_area_ceiling.py's narrative block
# established govern every value here:
#
#   1. FINAL. Imperial at this boundary (acres, feet); factors rescaled
#      from their native 0-1 to a 0-100 higher-is-better scale so they
#      are directly comparable to the composite score; position as a
#      compass word; everything rounded to 1 decimal place.
#   2. DERIVED, NEVER RECOMPUTED. Every figure is read off the candidate
#      dict find_candidate_solar_zones() already returns (including the
#      four stored factor scores) -- no scoring, distance, or exclusion
#      test is re-run to report on itself. The centroid bearing behind
#      position_in_parcel is trivial arithmetic on the footprint already
#      in hand (water_candidate_zones._position_in_parcel(), reused
#      rather than reimplemented).
#
# The output is plain JSON -- json.dumps() must work with no custom
# encoder.
#
# UNAVAILABLE IS None, NEVER 0.0. A distance whose reference geometry
# doesn't exist (no production zones, no road source, no water zone) is
# None; prime_farmland_conflict is None -- not False -- when the SSURGO
# check never ran, so "no conflict" is never claimed off an outage.
#
# NO REASON STRINGS. This block emits values; the report writes prose --
# the "benefits" are the site's own measured qualities (gentle slope,
# orientation, low terrain shading, road access, production-edge
# position), which are the data that prose is written FROM.

# 8-point compass words for the selected site's facing direction --
# whole words, not aspect_to_compass_label()'s abbreviations ("SE"),
# because this feeds narrative prose directly. Deliberately a separate
# tuple from water_candidate_zones.py's/production_area_ceiling.py's own
# private equivalents (same "constants stay separate even when
# identical" convention this pipeline already applies).
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


def _round1(value):
    """1 decimal place, or None passed straight through -- the single
    rounding boundary for this whole block. None means 'not known', and
    must never be silently rounded into a 0.0 that reads as a
    measurement."""
    return None if value is None else round(float(value), 1)


def _feet(meters):
    """Metres to feet at this block's own rounding boundary, None passed
    straight through -- the metric-to-imperial conversion happens HERE,
    in the module, never downstream in the report."""
    return None if meters is None else round(float(meters) / METERS_PER_FOOT, 1)


def _compass_word(aspect_deg) -> Optional[str]:
    """8-point compass word for a bearing in degrees, or None for an
    undefined aspect (an essentially-flat site faces no direction --
    None, never a fabricated word)."""
    if aspect_deg is None or math.isnan(float(aspect_deg)):
        return None
    return _COMPASS_WORDS[int(round((float(aspect_deg) % 360.0) / 45.0)) % 8]


def _no_candidates_block(rejection_tally: Optional[dict]) -> Optional[dict]:
    """
    WHY A RUN RETURNED NOTHING, off find_candidate_solar_zones()'s own
    rejection tally -- the block narrative_data carries in place of a
    selected site.

    THIS EXISTS BECAUSE A FULLY GATED PARCEL IS A REAL OUTCOME, not a
    failure. Trees deliberately targets hydric ground, so on a wet parcel
    trees and structures want opposite qualities -- and hydric plus
    floodplain plus the existing canopy, road-proximity and score-floor
    gates can together leave ZERO ground a building may stand on. The step
    has to SAY that, naming the grounds that did it, rather than hand back
    an empty list that reads as a broken generate. `blocking_gates` is
    sorted most-rejections-first, which is the order a reader wants: the
    gate at the top is the one to argue with.

    None when no tally was collected (a caller that passed none) -- absent,
    not a fabricated "no reason", the same null-not-zero rule the rest of
    this block obeys.
    """
    if not rejection_tally:
        return None
    sampled = int(rejection_tally.get("sampled", 0))
    not_measurable = int(rejection_tally.get("not_measurable", 0))
    measurable = sampled - not_measurable
    gates = rejection_tally.get("gates") or {}
    return {
        "pads_sampled": sampled,
        "pads_measurable": measurable,
        "blocking_gates": [
            {"gate": name, "pads_rejected": int(count)}
            for name, count in sorted(gates.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        # "nothing on this parcel could hold a building pad at all" and
        # "every pad that could be measured failed a gate" are different
        # answers and a reader needs to be told which.
        "reason": "no_pad_was_measurable" if measurable <= 0 else "every_pad_failed_a_gate",
    }


def build_narrative_data(
    candidates: list[dict],
    boundary_polygon_utm: Polygon,
    road_proximity_source: str,
    tree_zone_exclusion_available: bool,
    water_zone_excluded: bool,
    existing_canopy_excluded: bool,
    drainage_gates_checked: tuple = (),
    rejection_tally: Optional[dict] = None,
) -> dict:
    """
    The 'narrative_data' block identify_solar_candidate_zones() attaches
    to its result -- pre-computed, FINAL, JSON-serialisable values
    answering the two report questions in this section's header comment.
    Data only: no prose, no interpretation. candidates is the same
    ranked list the result dict already carries (read, never modified);
    the selected site is select_optimal_structure_site()'s own answer --
    the ONE definition of "selected", reused rather than re-derived.

    road_proximity_source / tree_zone_exclusion_available /
    water_zone_excluded / existing_canopy_excluded say what the run that
    produced candidates ACTUALLY applied -- which access source
    proximity was measured against ('selected_road_corridor' |
    'real_mapped_road' | 'unavailable'), whether the tree-zone exclusion
    could be checked at all, whether a selected water zone existed to
    exclude, and whether the mandatory canopy gate ran (True always on
    the identify path, whose canopy fetch is fetch-or-raise -- any
    result returned at all was canopy-gated). The identify entry point
    passes its own real outcomes; without these a narrative could claim
    clearances off a run whose checks never ran.

    drainage_gates_checked names the drainage gates the run APPLIED
    ('outside_hydric_soil', 'outside_floodplain', either, both or
    neither), so a narrative can never report a site clear of hydric soil
    on a run where SSURGO never answered. rejection_tally is find_
    candidate_solar_zones()'s own count of what the gates dropped; it is
    what makes the ZERO-CANDIDATE case reportable rather than blank (see
    'no_candidates' below).

    Shape:

        {
          'site_found': bool,
          'candidate_count': int,       # how many ranked candidates the winner
                                        #   was selected from (capped at
                                        #   MAX_CANDIDATES)
          'road_proximity_source',      # STEP-LEVEL: which access source every
                                        #   candidate's ft-to-road was measured
                                        #   against -- 'selected_road_corridor' |
                                        #   'real_mapped_road' | 'unavailable'.
                                        #   True of the whole run, which is why it
                                        #   is promoted here beside candidate_count
                                        #   rather than left only inside 'gates':
                                        #   ft-to-road is the panel's headline
                                        #   figure and its MEANING depends entirely
                                        #   on this -- a distance to a road that
                                        #   does not exist yet is a different fact
                                        #   from one to today's driveway -- so the
                                        #   panel renders it as a run-level notice.
                                        #   The same value stays in 'gates' (the
                                        #   report reads it there); this is one
                                        #   value in two places by design, not two
                                        #   values.
          'gates': {
            'existing_canopy_excluded', # mandatory on the identify path -- any
                                        #   result at all was canopy-gated
            'water_zone_excluded',
            'tree_zone_exclusion_checked',
            'road_proximity_source',
            'prime_farmland_checked',
            'hydric_gate_checked',      # SSURGO hydric soil was gated against
            'floodplain_gate_checked',  # NHD floodplain was gated against
            'drainage_gates_checked',   # the two above as the gate NAMES applied,
                                        #   which are the names constraints_
                                        #   violated uses on a placed site
          },
          'scales': {...},              # HOW TO READ EVERY VALUE HERE -- see
                                        #   _SCALES. Carries SOLAR_RATING_BANDS
                                        #   and production's imported ELEVATION_
                                        #   POSITION_BANDS, so the frontend holds
                                        #   no threshold of its own
          'no_candidates': None when site_found is True, else {
                                        # WHY THE RUN RETURNED NOTHING, so a fully
                                        #   gated parcel says so instead of looking
                                        #   broken. On a wet parcel hydric plus
                                        #   floodplain plus canopy, road proximity
                                        #   and the score floor can genuinely leave
                                        #   zero ground, and that is an answer
            'pads_sampled',             #   grid points tested
            'pads_measurable',          #   of those, pads with a real measurement
            'blocking_gates': [         #   every gate that rejected a measurable
                                        #     pad, most-rejections first
              {'gate', 'pads_rejected'},
            ],
            'reason',                   #   'no_pad_was_measurable' (nothing on this
                                        #     parcel could hold a building pad) or
                                        #     'every_pad_failed_a_gate'
          },
          'selected_site': None when site_found is False, else {
            'score',                    # 0-100
            'footprint_acres',
            'solar_rating',             # SOLAR_RATING_BANDS' word for the two
                                        #   SOLAR factors together
            'solar_value',              # 0-100, the number that word bands
            'location': {               # question 1 -- WHERE on the map
              'position_in_parcel',     #   "center" or an 8-point compass word
              'elevation_percentile_of_parcel',  # 0 = the parcel's lowest ground,
                                        #     100 = its highest; None on a parcel
                                        #     with no relief at all
              'elevation_position',     #   the line above as words -- production's
                                        #     own ELEVATION_POSITION_BANDS, IMPORTED
              'production_zone_relationship',   # 'inside' | 'adjacent' | 'outside'
              'distance_to_production_edge_ft', # UNSIGNED, to the nearest block's
                                        #     edge; None if no blocks exist
              'signed_distance_to_production_ft',  # the same distance SIGNED --
                                        #     NEGATIVE inside a block, positive
                                        #     outside, 0.0 on the edge. This is what
                                        #     lets a narrative say "142 ft inside the
                                        #     block" rather than "142 ft to
                                        #     production" from inside one
              'distance_to_road_ft',            # FROM THE SITE'S POINT; None if no
                                        #     road source was available
              'distance_to_water_zone_ft',      # None if no water zone exists
            },
            'benefits': {               # question 2 -- the measured qualities
              'avg_slope_pct',          #   prose reasons FROM
              'facing',                 #   compass word; None = essentially flat
              'factors': {              #   each rescaled 0-100, higher is better,
                                        #   directly comparable to 'score'
                'slope', 'aspect', 'shading', 'production_proximity',
              },
              'prime_farmland_conflict',  # None -- not False -- when never checked
            },
          },
        }
    """
    selected = select_optimal_structure_site(candidates)
    prime_farmland_checked = selected is not None and "prime_farmland_conflict" in selected
    drainage_gates_checked = tuple(drainage_gates_checked or ())

    data = {
        "site_found": selected is not None,
        "candidate_count": len(candidates),
        # STEP-LEVEL, promoted beside candidate_count -- see the docstring.
        "road_proximity_source": str(road_proximity_source),
        "gates": {
            "existing_canopy_excluded": bool(existing_canopy_excluded),
            "water_zone_excluded": bool(water_zone_excluded),
            "tree_zone_exclusion_checked": bool(tree_zone_exclusion_available),
            "road_proximity_source": str(road_proximity_source),
            "prime_farmland_checked": prime_farmland_checked,
            "hydric_gate_checked": "outside_hydric_soil" in drainage_gates_checked,
            "floodplain_gate_checked": "outside_floodplain" in drainage_gates_checked,
            "drainage_gates_checked": list(drainage_gates_checked),
        },
        "scales": _SCALES,
        "no_candidates": None,
        "selected_site": None,
    }
    if selected is None:
        data["no_candidates"] = _no_candidates_block(rejection_tally)
        return data

    data["selected_site"] = {
        "score": _round1(selected["suitability_score"]),
        "footprint_acres": _round1(selected["footprint_area_acres"]),
        # THE WORD AND THE NUMBER IT BANDS, both read off the candidate --
        # SOLAR_RATING_BANDS is applied once, in the scorer, and nothing
        # here re-derives it.
        "solar_rating": selected["solar_rating"],
        "solar_value": _round1(selected["solar_value"]),
        "location": {
            "position_in_parcel": _position_in_parcel(selected["polygon_utm"], boundary_polygon_utm),
            "elevation_percentile_of_parcel": _round1(selected["elevation_percentile_of_parcel"]),
            "elevation_position": selected["elevation_position"],
            "production_zone_relationship": str(selected["production_zone_relationship"]),
            "distance_to_production_edge_ft": _feet(selected["distance_to_production_zone_m"]),
            "signed_distance_to_production_ft": _feet(selected["signed_distance_to_production_m"]),
            "distance_to_road_ft": _feet(selected["distance_to_road_m"]),
            "distance_to_water_zone_ft": _feet(selected["distance_to_water_zone_m"]),
        },
        "benefits": {
            "avg_slope_pct": _round1(selected["avg_slope_pct"]),
            "facing": _compass_word(selected["aspect_deg"]),
            "factors": {
                "slope": _round1(selected["slope_score"] * 100.0),
                "aspect": _round1(selected["aspect_score"] * 100.0),
                "shading": _round1(selected["shading_score"] * 100.0),
                "production_proximity": _round1(selected["production_proximity_score"] * 100.0),
            },
            "prime_farmland_conflict": (
                bool(selected["prime_farmland_conflict"]) if prime_farmland_checked else None
            ),
        },
    }
    return data


def candidates_to_geojson(
    candidates: list[dict],
    shading_is_rough_proxy: bool = True,
    road_proximity_source: str = "unavailable",
    tree_zone_exclusion_available: bool = True,
    drainage_gates_checked: tuple = (),
    spacing_meters: float = CANDIDATE_POINT_SPACING_METERS,
    max_structure_footprint_acres: float = MAX_STRUCTURE_FOOTPRINT_ACRES,
) -> dict:
    """Wraps find_candidate_solar_zones() (+ optionally
    flag_prime_farmland_conflicts()) output as the schema-conformant
    GeoJSON FeatureCollection this feature delivers
    (layer="solar_infrastructure"). road_proximity_source reports which
    tier actually produced these candidates ("selected_road_corridor" |
    "real_mapped_road" | "unavailable" — see module docstring);
    tree_zone_exclusion_available flags whether the tree-zone-candidate
    exclusion could be checked this run at all (a graceful-degradation
    outcome, see identify_solar_candidate_zones()).

    CONSOLIDATED into wire_translation.py (as structure_sites_to_feature_
    collection) -- this name stays as the module's own entry point,
    forwarding to the single implementation kept there. The four run-level
    flags in this signature reach the wire baked into confidence_notes and
    are not recorded on any candidate dict -- but they ARE now recorded on
    identify_solar_candidate_zones()'s return, under 'run_flags', as
    exactly this keyword set, so a caller holding that result can call
    this function and reproduce its zones_geojson byte for byte. See that
    function's docstring."""
    from wire_translation import structure_sites_to_feature_collection

    return structure_sites_to_feature_collection(
        candidates,
        shading_is_rough_proxy=shading_is_rough_proxy,
        road_proximity_source=road_proximity_source,
        tree_zone_exclusion_available=tree_zone_exclusion_available,
        drainage_gates_checked=drainage_gates_checked,
        spacing_meters=spacing_meters,
        max_structure_footprint_acres=max_structure_footprint_acres,
    )


def identify_solar_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    production_areas: Optional[list[dict]] = None,
    valleys: Optional[list[dict]] = None,
    selected_water_zone: Optional[dict] = None,
    selected_road_corridor: Optional[dict] = None,
    hydric_floodplain_union=None,
    floodplain_data_is_fallback: Optional[bool] = None,
    hydric_union=None,
    floodplain_union=None,
    check_prime_farmland: bool = True,
    canopy_height: Optional[dict] = None,
    tree_zone_patches: Optional[list[dict]] = None,
    farm_roads: Optional[list[dict]] = None,
    farmland_classifications: Optional[list[dict]] = None,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    the mandatory canopy exclusion mask, optimized production areas, the
    single selected water zone, every ranked tree-zone candidate, and the
    two-tier road-proximity source; runs the point-candidate scoring
    (once, or twice if Tier 1 produces nothing — see below); checks the
    SSURGO prime-farmland conflict; and returns the "solar_infrastructure"
    GeoJSON FeatureCollection. See module docstring for the full
    constraint-stack rationale; this docstring covers wiring/ordering.

    dem, boundary_polygon_utm, production_areas, selected_water_zone, and
    selected_road_corridor are all optional overrides, independently of
    one another -- each falls back to being self-computed exactly as
    before if not supplied, same "reuse what an upstream orchestrator
    already computed" pattern water_suitability.identify_water_
    suitability() and road_corridors.identify_road_corridor_candidates()
    already established for these same values. valleys is a pure
    pass-through convenience: it is forwarded as-is (including None) to
    the identify_water_suitability()/identify_road_corridor_candidates()
    calls below, which already have their own correct None-falls-back-
    to-self-compute handling for it -- there is no third copy of that
    fallback logic here. hydric_floodplain_union/floodplain_data_is_
    fallback are forwarded the same way to identify_road_corridor_
    candidates() alone (see that function's own docstring for what they
    mean); they only take effect when selected_road_corridor is not
    itself supplied, same as production_areas/valleys/boundary_polygon_
    utm below.

    canopy_mask_utm is NOT among these overrides -- it is always
    self-computed here (see module docstring); that's a deliberate,
    separate scope decision, not an oversight. tree_zone_exclusion_
    polygon_utm is not one either, but its SOURCE now is: see
    tree_zone_patches below.

    tree_zone_patches is the override for the tree-zone exclusion's
    source -- the list every `tree_zone_patches=` consumer takes
    (identify_tree_zone_candidates()'s own 'patches', or the trees step's
    committed selection rehydrated by wire_translation.rehydrate_tree_
    zones()). None (the default) self-computes exactly as before, by the
    nested identify_tree_zone_candidates() call below. A LIST is used as
    given, and an EMPTY list is an answer: "checked, no planned tree
    ground" -- no exclusion polygon is built, tree_zone_exclusion_
    available stays True, and the nested call does not run. That is what
    a trees step committed EMPTY means to this module: not "unavailable",
    but "nothing to stay clear of", which is the user's own decision and
    the reason the override exists at all. Without it a structures
    generate on the interactive path would REGENERATE tree candidates
    and silently exclude ground the user never committed -- and, because
    the nested call's own soil/stream fetches have no cache override here,
    do it over the network. Measured, not assumed: test_structures_step.py
    compares the regenerated set against the committed one on the
    reference parcel and reports whether the candidates move.

    hydric_union and floodplain_union are THE TWO HARD DRAINAGE GATES,
    and they are NOT in the override family every other parameter here
    belongs to: a None does not self-compute, it means the gate is simply
    not applied, and nothing here fetches either one. That is deliberate.
    A gate is a claim about the ground under a building, and a fetch that
    did not land is not the same answer as ground that came back clean --
    so a run that was handed neither reports `drainage_gates_checked`
    empty rather than gating on invented geometry or, worse, reporting
    sites clear of an unrun check. Both are derived upstream, once, from
    ParcelData's own rows (road_corridors._fetch_floodplain_hydric_
    unions(), through the terrain warm-up or build_pipeline_context()) and
    reach this step as its own two registry cache edges.
      hydric_floodplain_union above is the COMBINATION of these two and is
      a different parameter for a different purpose: it is forwarded,
      untouched, into the nested road and tree self-computes, which read
      wet ground as ONE soft cost penalty. It is not taken apart here, and
      these two are not built from it -- the union cannot be split back.

    farm_roads and farmland_classifications are cache closures in the same
    None-falls-back-to-self-fetch family, each the rows the corresponding
    fetch returns (farm_roads_data.get_farm_roads_for_boundary() and
    soil_data.get_farmland_classification_for_polygon(), which are exactly
    parcel_data.ParcelData.farm_roads and .farmland_classification). They
    exist because Tier 2's road fetch and the SSURGO prime-farmland check
    were this function's last two network calls on a run whose every other
    input was supplied, and a step registry generate is network-free BY
    CONTRACT (step_orchestrator.py's IDEMPOTENT AND REPEATABLE). Neither
    changes what is computed: the rows go through the same code the fetch
    path runs on what it fetched. They are additions beyond the two solar
    changes the structures branch named, and are reported as such.

    Returns:
        {
            'zones_geojson': dict,                   # every scored candidate, ranked
            'all_scored_candidates': list[dict],     # find_candidate_solar_zones()'s own raw
                                                        # scored list (post-farmland-flagging)
            'selected_structure_site': Optional[dict],  # select_optimal_structure_site()'s
                                                           # single rank-1 answer, or None if no
                                                           # candidates exist
            'narrative_data': dict,                  # report-facing, FINAL, JSON-serialisable
                                                        # values -- see build_narrative_data()
            'run_flags': dict,                       # THE RUN-LEVEL FLAGS -- see below
            'run_inputs': dict,                      # what the run scored against, natively
        }

    'run_flags' SURFACES WHAT USED TO LIVE ONLY IN THIS FUNCTION'S LOCAL
    SCOPE. candidates_to_geojson() bakes four run-level values into every
    feature's confidence_notes -- shading_is_rough_proxy, road_proximity_
    source, tree_zone_exclusion_available, and the footprint/spacing
    thresholds (spacing_meters, max_structure_footprint_acres) -- and until
    this key existed none of them was recorded anywhere a caller could
    read: not on a candidate dict, not on PipelineContext, not on this
    return. render_layout_map.fetch_layout_layers() therefore read its
    structure_site Feature off THIS call's zones_geojson, the only artifact
    that knew which run produced the notes. 'run_flags' is exactly
    candidates_to_geojson()'s keyword set, so

        candidates_to_geojson(result["all_scored_candidates"], **result["run_flags"])

    reproduces result["zones_geojson"] byte for byte (asserted in
    test_structures_step.py) and a caller holding the context can now
    rebuild the wire form of any candidate under the notes of the run that
    produced it. fetch_layout_layers() is NOT changed on this branch; it
    could now read from context, and the structures branch report says so.

    'run_inputs' is the native counterpart: the DEM, the parcel polygon,
    the production patches, the water zones, the road geometry and buffer
    of the tier that ACTUALLY produced the candidates, the canopy mask,
    the tree-zone exclusion polygon, THE TWO DRAINAGE GATE GEOMETRIES, and
    the thresholds. It is what score_placed_structure_site() scores a
    user-placed site against, so a placed site and a generated candidate
    are measured against one run -- which is what makes "this spot is on
    hydric soil" a statement about the same data the shortlist was built
    from.
    Native objects (numpy, shapely), like all_scored_candidates -- not
    JSON, and not for the wire.

    'narrative_data' is PURELY ADDITIVE at this level: every other key
    above is byte-identical to what this function returned before it
    existed (each candidate dict additionally carries the four stored
    factor scores -- see find_candidate_solar_zones()). It answers two
    report questions (where is the selected site on the map / what are
    the benefits of placing a permanent building here) with
    pre-computed, imperial, rounded values a narrative can quote
    directly -- derived entirely from the candidate dict this function
    already computed, so adding it re-runs no scoring, distance, or
    exclusion test. See build_narrative_data()'s own docstring for the
    field contract.

    CANOPY (mandatory, non-degrading — see module docstring): fetched
    directly via production_area.get_required_tree_root_zone_mask_utm(),
    NOT wrapped in try/except — a fetch failure is left to propagate
    UNCAUGHT and fails this whole call, same as production_area.py's/
    production_area_ceiling.py's/tree_zone_candidates.py's own mandatory
    canopy gates. identify_optimized_production_areas() below pulls in a
    SECOND, fully independent copy of this same mandatory gate internally
    — both are expected to hard-fail independently on a canopy outage;
    neither is caught here.
      canopy_height is an optional pre-fetched override in the same family
      as the dem/boundary/production/water/valleys overrides: the SAME dict
      canopy_height_data.get_canopy_height_for_boundary() returns (e.g.
      parcel_data.ParcelData.canopy_height). When supplied it is forwarded
      to EVERY canopy consumer this function reaches -- its own direct gate
      above plus the nested identify_optimized_production_areas(),
      identify_water_suitability(), and identify_tree_zone_candidates()
      calls below -- so none of those redundant, independent canopy fetches
      hit the network; when None (the default) each fetches as before,
      leaving every gate's hard-fail semantics unchanged.

    PRODUCTION is production_area_ceiling.identify_optimized_production_
    areas()'s own OPTIMIZED, ceiling-trimmed 'scored_patches' (not
    production_area.identify_production_areas()'s raw candidates), passed
    through to find_candidate_solar_zones() for its scoring-only (not
    exclusion) role — see that function's docstring. Skipped entirely when
    this function's own production_areas override is supplied.

    WATER exclusion is scoped to only the SINGLE selected water zone
    (water_suitability.select_optimal_water_zone(), same selection
    function tree_zone_candidates.py's own rewiring in this same pipeline
    already reuses), using its 'render_fill_polygon_utm' — confirmed live
    that excluding all of a real property's several legitimate,
    separately-buffered water zones can together cover enough of a small
    parcel to zero out every solar candidate, even though each zone's own
    geometry is individually normal. water_suitability.
    identify_water_suitability()'s own real per-zone SSURGO/NHD fetches
    degrade independently and gracefully. Skipped entirely when this
    function's own selected_water_zone override is supplied; when it is
    NOT but boundary_polygon_utm/production_areas/valleys ARE, those three
    are passed through as kwargs so identify_water_suitability() doesn't
    re-derive its own independent copies. selected_water_zone ALSO accepts
    water_suitability.NO_WATER_ZONE -- the explicit "the water pipeline
    already ran and selected nothing" answer (see that constant's own
    docstring): it is normalized back to None here and the self-compute is
    SKIPPED, unlike a bare None (indistinguishable from "not supplied"
    under this pipeline's standard override convention, still
    self-computes as before). Once resolved, this function's own nested
    identify_road_corridor_candidates()/identify_tree_zone_candidates()
    calls below forward the same explicit form (re-wrapping a resolved
    None as NO_WATER_ZONE), so neither nested call re-runs the water
    pipeline either.

    ROAD PROXIMITY is two-tier (see module docstring for the full
    rationale). Tier 1's own selected_road_corridor is resolved BEFORE
    TREE-ZONE-CANDIDATE exclusion below (not after, despite Tier 1's own
    SCORING step running after it) specifically so that step can forward
    this function's own real selected_road_corridor into its own nested
    identify_tree_zone_candidates() call -- see that section's own
    docstring paragraph below for why.
      Tier 1 (primary) resolution: road_corridors.identify_road_corridor_
        candidates() is called directly here, given this function's own
        anchor_lon_lat parameter (the real, user-picked access point,
        threaded down from generate_full_report.py — None degrades to no
        corridor, same as identify_road_corridor_candidates() itself
        already handles), and its own 'selected_road_corridor' (None if no
        corridor exists) is used as the road source at ROAD_CORRIDOR_
        PROXIMITY_METERS. Not wrapped in try/except: this call's own
        internal production-zone fetch is ALSO a mandatory canopy gate (a
        THIRD independent one), same "expected to hard-fail independently"
        reasoning as above. Skipped entirely when this function's own
        selected_road_corridor override is supplied as a real (non-None)
        value; a caller-supplied None is indistinguishable from "not
        supplied" (same None-as-sentinel convention every other override
        in this function uses) and still self-computes -- the Tier 1/
        Tier 2 scoring branching further below is unaffected either way,
        since it already treats a self-computed None (no corridor exists)
        and any other None identically. When selected_road_corridor is NOT
        overridden but boundary_polygon_utm/production_areas/valleys ARE,
        those three (plus hydric_floodplain_union/floodplain_data_is_
        fallback, forwarded as-is) are passed through as kwargs so
        identify_road_corridor_candidates() doesn't re-derive its own
        independent copies.
      Tier 1 (primary) scoring / Tier 2 (fallback, only if Tier 1 produced
        zero candidates — whether because no corridor exists at all, or
        one exists but nothing survives near it): real mapped roads
        (farm_roads_data.get_farm_roads_for_boundary(), unchanged fetch
        logic) at ROAD_PROXIMITY_BUFFER_METERS — this IS today's
        pre-this-pass road-proximity logic, demoted from primary to
        fallback. If this fetch fails outright (a real network error, not
        "zero roads found") and Tier 1 also produced nothing, the road
        constraint is disabled entirely for a final find_candidate_solar_
        zones() call (road_geometries_utm=None) — same terminal fallback
        behavior this module has always had.
    road_proximity_source ("selected_road_corridor" | "real_mapped_road" |
    "unavailable") records which tier actually produced the result and is
    threaded into candidates_to_geojson()'s confidence_notes/per-feature
    properties.

    TREE-ZONE-CANDIDATE exclusion (identify_tree_zone_candidates(), the
    full ranked 'patches' list -- SKIPPED ENTIRELY when this function's own
    tree_zone_patches override is supplied, see above) degrades GRACEFULLY
    on an ordinary fetch failure — noted in confidence_notes via
    candidates_to_geojson()'s own tree_zone_exclusion_available flag —
    UNLESS the failure is
    specifically canopy_height_data.CanopyCoverageIncompleteError
    bubbling up from that call's own internal mandatory canopy gate, in
    which case it propagates uncaught here too, same reasoning as the
    module's own primary canopy gate above. This call forwards this
    function's own already-resolved boundary_polygon_utm/production_
    areas/valleys/selected_water_zone/selected_road_corridor/hydric_
    floodplain_union/floodplain_data_is_fallback, so it reuses them rather
    than self-computing independent copies of production_areas/selected_
    water_zone/selected_road_corridor all over again — previously this
    call forwarded none of those (only boundary_coordinates/dem/
    anchor_lon_lat), a real, measured redundancy found once pipeline_
    context.py's own build_pipeline_context() started supplying overrides
    to THIS function and fixed here.
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

    # MANDATORY, non-degrading -- see module docstring and this
    # function's own docstring. Deliberately NOT wrapped in try/except.
    canopy_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=TREE_ROOT_ZONE_BUFFER_METERS, canopy_height=canopy_height
    )

    if production_areas is None:
        # Optimized/ceiling-trimmed production geometry -- pulls in its
        # own SECOND, independent mandatory canopy gate internally; also
        # not caught here (see this function's own docstring).
        production_result = identify_optimized_production_areas(
            boundary_coordinates, dem=dem, canopy_height=canopy_height
        )
        production_areas = production_result["scored_patches"]

    # water_suitability.NO_WATER_ZONE is the EXPLICIT "already ran the
    # selection, nothing qualified" answer (see that constant's own
    # docstring): reuse it (normalized back to None -- everything below
    # keeps None's existing "no zone" meaning) rather than treating it as
    # "not supplied" and re-running the whole water pipeline. A bare None
    # still self-computes exactly as before.
    if selected_water_zone is NO_WATER_ZONE:
        selected_water_zone = None
    elif selected_water_zone is None:
        water_result = identify_water_suitability(
            boundary_coordinates,
            dem=dem,
            boundary_polygon_utm=boundary_polygon_utm,
            valleys=valleys,
            production_areas=production_areas,
            canopy_height=canopy_height,
        )
        selected_water_zone = water_result["selected_water_zone"]
    # From here on selected_water_zone is RESOLVED: None now genuinely means
    # "no zone on this property," so every nested identify_*() call below
    # forwards it re-wrapped as NO_WATER_ZONE when None -- forwarding the
    # bare None would make the nested call's own self-compute fallback
    # re-run the water pipeline all over again (the exact redundancy the
    # explicit answer exists to prevent).
    resolved_water_zone_answer = selected_water_zone if selected_water_zone is not None else NO_WATER_ZONE
    water_zones = [selected_water_zone] if selected_water_zone else []

    # --- Tier 1 (primary) road source: the property's own single selected
    # road corridor, within ROAD_CORRIDOR_PROXIMITY_METERS. Resolved HERE
    # (moved up from directly above the Tier 1 scoring call below) SPECIFICALLY
    # so the tree-zone-candidate exclusion block right after this can forward
    # this function's own real selected_road_corridor into its own nested
    # identify_tree_zone_candidates() call, instead of leaving that call to
    # self-compute an independent, redundant copy -- the same reasoning
    # already applies to production_areas/selected_water_zone above, both
    # resolved before this point for the same purpose. Not wrapped in
    # try/except: this call's own internal production-zone fetch is ALSO a
    # mandatory canopy gate (a THIRD independent one), same "expected to
    # hard-fail independently" reasoning as the primary canopy gate above.
    # Skipped entirely when this function's own selected_road_corridor
    # override is supplied as a real (non-None) value; a caller-supplied
    # None is indistinguishable from "not supplied" (same None-as-sentinel
    # convention every other override in this function uses) and still
    # self-computes -- the Tier 1/Tier 2 scoring branching below is
    # unaffected either way, since it already treats a self-computed None
    # (no corridor exists) and any other None identically.
    #
    # road_corridors.NO_ROAD_CORRIDOR is the EXPLICIT "the roads step
    # already ran and selected no road" answer (see that constant's own
    # docstring) -- the value an EMPTY roads commit arrives as on the
    # interactive path. Normalized back to None here (everything below,
    # Tier 1's skip and Tier 2's fallback included, keeps None's existing
    # "no corridor exists" meaning) and the self-compute is SKIPPED, exactly
    # as NO_WATER_ZONE is handled above. A bare None still self-computes. ---
    if selected_road_corridor is NO_ROAD_CORRIDOR:
        selected_road_corridor = None
    elif selected_road_corridor is None:
        corridor_result = identify_road_corridor_candidates(
            boundary_coordinates,
            anchor_lon_lat=anchor_lon_lat,
            dem=dem,
            boundary_polygon_utm=boundary_polygon_utm,
            production_areas=production_areas,
            valleys=valleys,
            selected_water_zone=resolved_water_zone_answer,
            hydric_floodplain_union=hydric_floodplain_union,
            floodplain_data_is_fallback=floodplain_data_is_fallback,
            canopy_height=canopy_height,
        )
        selected_road_corridor = corridor_result["selected_road_corridor"]

    # Tree-zone-candidate exclusion: graceful degradation on an ordinary
    # fetch failure, EXCEPT for CanopyCoverageIncompleteError bubbling up
    # from this call's own internal mandatory canopy gate -- see this
    # function's own docstring. Forwards this function's own already-
    # resolved boundary_polygon_utm/production_areas/valleys/selected_
    # water_zone/selected_road_corridor/hydric_floodplain_union/
    # floodplain_data_is_fallback so this nested call reuses them instead
    # of self-computing independent copies of production_areas/selected_
    # water_zone/selected_road_corridor all over again -- previously this
    # call forwarded none of them (only boundary_coordinates/dem/
    # anchor_lon_lat), a real, measured redundancy found and fixed after
    # pipeline_context.py's own build_pipeline_context() started supplying
    # these overrides (see that module's own KNOWN LIMITATIONS #4, prior
    # to this fix).
    #
    # tree_zone_patches= CLOSES THE SELF-COMPUTE, the same way every other
    # override in this function does: a supplied list is the answer, None
    # regenerates. See this function's docstring for what an EMPTY list
    # means and why the override exists.
    tree_zone_exclusion_polygon_utm = None
    tree_zone_exclusion_available = True
    if tree_zone_patches is None:
        try:
            tree_zone_result = identify_tree_zone_candidates(
                boundary_coordinates,
                dem=dem,
                anchor_lon_lat=anchor_lon_lat,
                boundary_polygon_utm=boundary_polygon_utm,
                production_areas=production_areas,
                valleys=valleys,
                selected_water_zone=resolved_water_zone_answer,
                selected_road_corridor=selected_road_corridor,
                hydric_floodplain_union=hydric_floodplain_union,
                floodplain_data_is_fallback=floodplain_data_is_fallback,
                canopy_height=canopy_height,
            )
            tree_zone_patches = tree_zone_result["patches"]
        except CanopyCoverageIncompleteError:
            raise
        except Exception:
            tree_zone_exclusion_available = False
            tree_zone_patches = None
    if tree_zone_patches:
        tree_zone_union = unary_union([p["render_fill_polygon_utm"] for p in tree_zone_patches])
        tree_zone_exclusion_polygon_utm = tree_zone_union.buffer(TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS)

    # THE TWO DRAINAGE GATES, in the DEM's own CRS, exactly as supplied.
    # NOT derived from hydric_floodplain_union above: that union is the
    # COMBINATION roads and trees read as one soft cost penalty, and it
    # cannot be taken apart again. When these are not supplied the gates
    # are simply not applied -- no self-compute, no fetch (see this
    # function's docstring for why that is deliberate, and how it is
    # reported).
    common_zone_kwargs = dict(
        canopy_mask_utm=canopy_mask_utm,
        tree_zone_exclusion_polygon_utm=tree_zone_exclusion_polygon_utm,
        hydric_union_utm=hydric_union,
        floodplain_union_utm=floodplain_union,
    )
    common_zone_kwargs.update(zone_kwargs)

    # WHICH DRAINAGE GATES THIS RUN ACTUALLY APPLIED, by the names
    # constraints_violated uses -- the run-level answer to "was this
    # checked at all", which a None union makes different from "checked
    # and clear". Empty means neither was applied.
    drainage_gates_checked = tuple(
        name
        for name, union in (
            ("outside_hydric_soil", common_zone_kwargs["hydric_union_utm"]),
            ("outside_floodplain", common_zone_kwargs["floodplain_union_utm"]),
        )
        if union is not None
    )

    # THE REJECTION TALLY OF WHICHEVER TIER ANSWERED. Each scoring call
    # below fills it (clearing it first), so after the tier branching it
    # describes the run whose candidates were kept -- which is the run a
    # zero-candidate report has to explain.
    rejection_tally = _empty_rejection_tally()

    # --- Tier 1 (primary) scoring: within ROAD_CORRIDOR_PROXIMITY_METERS
    # of the selected_road_corridor resolved above (self-compute moved
    # earlier -- see that block's own comment). ---
    candidates = []
    road_proximity_source = "unavailable"
    # THE ROAD SOURCE THE RESULT WAS SCORED AGAINST -- (geometries, buffer)
    # of whichever tier answered -- recorded so run_inputs can say it and a
    # placed site can be measured against the same one.
    road_source = (None, ROAD_PROXIMITY_BUFFER_METERS)

    if selected_road_corridor is not None:
        road_source = ([selected_road_corridor["cell_footprint_polygon_utm"]], ROAD_CORRIDOR_PROXIMITY_METERS)
        candidates = find_candidate_solar_zones(
            dem,
            production_areas,
            water_zones,
            road_source[0],
            boundary_polygon_utm,
            road_proximity_buffer_meters=road_source[1],
            rejection_tally=rejection_tally,
            **common_zone_kwargs,
        )
        if candidates:
            road_proximity_source = "selected_road_corridor"

    # --- Tier 2 (fallback): only reached if Tier 1 produced zero
    # candidates -- either no corridor existed at all, or one existed but
    # nothing survived the rest of the constraint stack near it. ---
    if not candidates:
        try:
            roads = farm_roads if farm_roads is not None else get_farm_roads_for_boundary(boundary_coordinates)
            road_lines_wgs84 = [g["geometry"] for g in roads]
        except Exception:
            road_lines_wgs84 = None  # real fetch failure, not "zero roads found"

        if road_lines_wgs84 is not None:
            road_geometries_utm = []
            for geometry in road_lines_wgs84:
                coords = geometry["coordinates"]
                line_lists = coords if geometry["type"] == "MultiLineString" else [coords]
                for line in line_lists:
                    xs, ys = warp_transform("EPSG:4326", dem["crs"], [p[0] for p in line], [p[1] for p in line])
                    road_geometries_utm.append(LineString(zip(xs, ys)))

            road_source = (road_geometries_utm, ROAD_PROXIMITY_BUFFER_METERS)
            candidates = find_candidate_solar_zones(
                dem,
                production_areas,
                water_zones,
                road_source[0],
                boundary_polygon_utm,
                road_proximity_buffer_meters=road_source[1],
                rejection_tally=rejection_tally,
                **common_zone_kwargs,
            )
            road_proximity_source = "real_mapped_road"
        else:
            # Tier 2's own fetch failed outright, AND Tier 1 produced
            # nothing -- fall through to the terminal behavior: disable
            # the road constraint entirely for a final scoring pass.
            road_source = (None, ROAD_PROXIMITY_BUFFER_METERS)
            candidates = find_candidate_solar_zones(
                dem,
                production_areas,
                water_zones,
                None,
                boundary_polygon_utm,
                rejection_tally=rejection_tally,
                **common_zone_kwargs,
            )
            road_proximity_source = "unavailable"

    if check_prime_farmland and candidates:
        try:
            if farmland_classifications is None:
                wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
                farmland_classifications = get_farmland_classification_for_polygon(wkt_polygon)
            candidates = flag_prime_farmland_conflicts(candidates, farmland_classifications)
        except Exception:
            pass  # SSURGO outage -- candidates just won't carry a prime_farmland_conflict flag this run

    # THE FOUR RUN-LEVEL FLAGS, as candidates_to_geojson()'s own keyword
    # set -- see this function's docstring. shading_is_rough_proxy is True
    # on every run this module can make today: the shading signal is
    # terrain_metrics' DEM-only horizon proxy, and no canopy height model
    # or NDVI reaches the scorer.
    run_flags = {
        "shading_is_rough_proxy": True,
        "road_proximity_source": road_proximity_source,
        "tree_zone_exclusion_available": tree_zone_exclusion_available,
        # FIVE NOW, not four: which drainage gates the run applied, by the
        # names constraints_violated uses. It belongs with the other
        # run-level flags for exactly their reason -- a GENERATED
        # candidate's constraints_satisfied list is reconstructed on the
        # wire from the gates the RUN applied (its own outcomes are all
        # True by construction and are not stored), so without this flag
        # the wire could not tell "gated and clear" from "never checked",
        # and would have to claim one of them.
        # A LIST, not a tuple: run_flags must be json.dumps()-clean (the
        # step payload carries it under summary.run_flags), and a tuple
        # round-trips as a list -- so it IS a list from the start rather
        # than a shape that only survives one direction.
        "drainage_gates_checked": list(drainage_gates_checked),
        "spacing_meters": zone_kwargs.get("candidate_point_spacing_meters", CANDIDATE_POINT_SPACING_METERS),
        "max_structure_footprint_acres": zone_kwargs.get(
            "max_structure_footprint_acres", MAX_STRUCTURE_FOOTPRINT_ACRES
        ),
    }
    # The thresholds a placed site must be measured under: whatever
    # zone_kwargs overrode, else the scorer's own defaults (the sampling-
    # only parameters mean nothing for one point and are left out).
    thresholds = {
        key: value
        for key, value in zone_kwargs.items()
        if key not in ("candidate_point_spacing_meters", "max_candidates", "canopy_mask_utm",
                       "tree_zone_exclusion_polygon_utm", "road_proximity_buffer_meters",
                       "hydric_union_utm", "floodplain_union_utm", "rejection_tally")
    }
    thresholds["road_proximity_buffer_meters"] = road_source[1]
    run_inputs = {
        "dem": dem,
        "boundary_polygon_utm": boundary_polygon_utm,
        "production_areas": production_areas,
        "water_zones": water_zones,
        "road_geometries_utm": road_source[0],
        "canopy_mask_utm": common_zone_kwargs["canopy_mask_utm"],
        "tree_zone_exclusion_polygon_utm": common_zone_kwargs["tree_zone_exclusion_polygon_utm"],
        # THE TWO DRAINAGE GATES THE GENERATED CANDIDATES WERE GATED BY,
        # so score_placed_structure_site() measures a placed site against
        # the same two -- which is the whole basis of the comparison.
        "hydric_union_utm": common_zone_kwargs["hydric_union_utm"],
        "floodplain_union_utm": common_zone_kwargs["floodplain_union_utm"],
        "thresholds": thresholds,
    }

    return {
        "zones_geojson": candidates_to_geojson(candidates, **run_flags),
        "all_scored_candidates": candidates,
        "selected_structure_site": select_optimal_structure_site(candidates),
        "narrative_data": build_narrative_data(
            candidates,
            boundary_polygon_utm,
            road_proximity_source=road_proximity_source,
            tree_zone_exclusion_available=tree_zone_exclusion_available,
            water_zone_excluded=bool(water_zones),
            # The canopy gate above is fetch-or-raise, so any result this
            # function returns at all was canopy-gated.
            existing_canopy_excluded=True,
            drainage_gates_checked=drainage_gates_checked,
            # WHAT THE GATES DROPPED, from whichever tier answered -- so a
            # run that found nothing explains itself. Read only when
            # `candidates` is empty; carried unconditionally because which
            # of the two it is is build_narrative_data()'s call, not this
            # function's.
            rejection_tally=rejection_tally,
        ),
        "run_flags": run_flags,
        "run_inputs": run_inputs,
    }


def fetch_and_select_optimal_structure_site(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    **zone_kwargs,
) -> Optional[dict]:
    """
    Convenience wrapper for callers (e.g. render_layout_map.py) that want
    a single best solar structure site candidate rather than the full
    ranked FeatureCollection -- identify_solar_candidate_zones() already
    returns candidates rank-ordered best-first (feature 0 = rank 1), so
    this just returns that top GeoJSON Feature (or None if nothing
    cleared the constraint stack). Selects nothing new -- it picks the #1
    entry an existing, unchanged ranking already produced.
    """
    result = identify_solar_candidate_zones(boundary_coordinates, dem=dem, anchor_lon_lat=anchor_lon_lat, **zone_kwargs)
    features = result["zones_geojson"]["features"]
    return features[0] if features else None


def summarize_solar_candidate_zones(result: dict) -> str:
    features = result["zones_geojson"]["features"]
    if not features:
        return "No solar structure candidates identified (nothing cleared the constraint stack)."

    lines = [f"Solar structure candidates: {len(features)}"]
    for feature in features:
        props = feature["properties"]
        conflict = " [prime farmland conflict]" if props.get("prime_farmland_conflict") else ""
        lines.append(
            f"  - Rank {props['rank']}: score {props['suitability_score']}/100, "
            f"{props['footprint_area_acres']}ac, {props['avg_slope_pct']}% slope, {props['aspect']}-facing, "
            f"{props['distance_to_road_ft']}ft to road, {props['production_zone_relationship']} production zone "
            f"({props['distance_to_production_zone_ft']}ft to edge){conflict}"
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

    # Manual-testing-only reference anchor -- imported here, not at module
    # level, so this stays a __main__-only test fixture rather than a
    # production dependency (see render_layout_map.py's own module
    # docstring for this constant).
    from render_layout_map import _PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT

    print("Identifying solar structure candidates for property boundary...\n")

    try:
        result = identify_solar_candidate_zones(
            property_boundary, anchor_lon_lat=_PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT
        )
        print(summarize_solar_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
