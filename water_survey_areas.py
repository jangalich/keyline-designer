"""
water_survey_areas.py

The water step's deliverable, redefined: TYPED SURVEY AREAS from
weighted-overlay suitability surfaces -- general areas worth surveying for
a farm water system, anchored to NRCS Agriculture Handbook 590's two pond
types:

    EMBANKMENT-type -- a small dam across a drainageway. Wants real (but
        bounded) catchment, a moderate valley grade, water-holding soil,
        and locally wet ground.
    EXCAVATED-type -- a dugout in wet, flat ground. Wants wetness (high
        topographic wetness, real natural depressions), water-holding
        soil, flat ground, and only a mild preference for run-on.

THE TWO TYPES DIFFER IN GENERATION MECHANISM NOW, not just criteria
(this branch's design change). An embankment pond is a VALLEY
COMPARTMENT -- a dam site at a narrows, a storage reach above it,
flanking ridges either side -- and a dugout is not, so one extraction
mechanism could never describe both:

    EXCAVATED keeps the full existing pipeline: threshold extraction
        into 8-connected member regions, closing aggregation, convex-
        hull envelope (now clipped at the road exclusion union exactly
        as at the parcel boundary -- truncated_by_road).
    EMBANKMENT keeps stages 1-2 only (criteria scoring, the weighted
        blend) as a NOMINATION SURFACE; the threshold/components/
        members/closing/hull machinery has left that path entirely,
        replaced by SEED-BASED VALLEY COMPARTMENTS: iterative highest-
        blend seeding (EMBANKMENT_SEED_MIN_SCORE, uncapped), a
        downstream D8 pinch walk to the valley's crest-to-crest width
        minimum (the embankment cell), and a compartment assembled from
        the pinch cell's watershed clipped to the band between two
        baseline-perpendicular crest transects -- the lateral boundary
        of that clip IS the ridge line (hydrology handles branching
        crests; no crest-tracing). A seed with no on-parcel pinch
        produces NOTHING, with a reason code naming the terminator
        (no_pinch_within_bound / pinch_off_parcel /
        pinch_blocked_by_road) -- the hull does not exist on this path
        and there is nothing to fall back to. See the compartment
        constants/section for the full construction, the reporting
        honesty split (seed anchor claim vs compartment means), and
        the dedupe rules.

EXISTING FARM ROADS ARE A GEOMETRIC EXCLUSION for both types, exactly
like the parcel boundary: zone geometry clips at the road exclusion
union (truncated_by_road), and the embankment walks additionally treat
road cells as hard terminators. road_overlap_pct survives as a REPORTED
property by measuring the PRE-clip geometry -- the share of the
walkable claim the clip removed -- because measuring the clipped
geometry would be a guaranteed zero.

This REPLACES pool/wall simulation as the pipeline's water step. The
level-pool arc (water_candidate_zones.py + valley_level_pool.py, both now
DEMOTED to diagnostic-consumed modules) proved the reference property
lacks keyline-dam geometry; survey areas from standard-practice
suitability surfaces are the replacement deliverable. PRECISION IS THE
ENEMY OF THIS DELIVERABLE: nothing in this module computes a pool, a
wall, a volume, or a station. A survey area is "ground worth walking with
a survey rod", not a design.

TUNED POSTURE (the exploration phase is OVER): the module shipped under
a first-run flags-not-filters posture -- no cap, no drops, every
tunable provisional -- and three measured tuning passes then decided
every open number from evidence: the threshold (0.5, judged against the
parcel's ATTAINABLE ~0.82 ceiling -- see SUITABILITY_THRESHOLD), the
excavated slope taper (seep-widened per the FINDING + soil rider
evidence -- see EXCAVATED_SLOPE_FULL_CREDIT_PCT), the floor (a FILTER
on the walkable zone acres, drops visible and attributed -- see
MIN_SURVEY_REGION_AREA_ACRES's history note covering its two prior
bases), and presentation (a TOP_N cap was tried for one pass and
DELETED -- every surviving zone is listed; see the history note at the
old constant's site). The pre-merge pass added the convex-hull
envelope (the surveyable claim), the sparse_anchor honesty guard, and
the cross-type agreement report. The diagnostic's instruments
(threshold comparison, isobands, the excavated interrogation) keep
printing every run so each decision remains evidence-checked rather
than trusted forward.

THE TWO SURFACES ARE KEPT SEPARATE END TO END. Each is a per-cell 0-1
score, a weighted blend of classed criteria (weights sum to 1.0 per type,
asserted at import). They share the gate mask, the soil scorer, and the
derived screens, but are never averaged into one composite: an
embankment-type and an excavated-type answer are different survey
instructions, and blending them would produce ground that is mediocre for
both.

THE TWO DERIVED SCREENS (both free from arrays already in hand):

    TWI = ln(a / tan(beta)) -- topographic wetness index. Specific
        catchment area a from the existing D8 flow accumulation
        (valley_delineation.py), slope from the existing slope machinery
        (production_area.compute_slope_percent()); the flat/zero-slope
        singularity is guarded explicitly (TWI_MIN_SLOPE_TAN below).
        Scored PARCEL-RELATIVE -- the percentile rank of each on-parcel
        cell's TWI within the parcel boundary -- because ABSOLUTE TWI is
        resolution-dependent (a is measured per unit contour width, so
        the same terrain resampled to a different cell size shifts every
        absolute value). A TWI score of 0.9 therefore reads "among the
        wettest ground on THIS parcel", never "wet by a universal
        standard" -- stated again in narrative_data
        (TWI_PARCEL_RELATIVE_NOTE) so no narrative can overclaim it.

    DEPRESSION DEPTH = filled DEM - raw DEM. Both arrays were already
        computed every run (priority-flood fill feeds flow direction);
        the difference was simply discarded. Keeping it converts the
        flat-tie problem into signal: where priority-flood raised the
        raw surface is exactly where water would pond naturally, so
        natural storage basins map directly. A noise floor
        (DEPRESSION_NOISE_FLOOR_METERS) zeroes sub-threshold fill
        artifacts. NOTE: this also reduces the urgency of an
        epsilon-fill rewrite for the WATER layer specifically -- the
        fill's flat ties stop being a lost signal here because the fill
        depth itself is now consumed -- flagged, not fixed, in this
        branch.

GATES (unchanged trio, ported from water_candidate_zones.py): on-parcel
AND contributing-area ceiling AND boundary setback (the setback constant
is ported WITH its full zeroed-history docstring -- premise kept, value
zeroed). The mask is applied to BOTH surfaces before region extraction.
Everything else that used to gate is a reported measurement here: canopy
and road overlap, production overlap, gravity relationship, boundary
adjacency -- all context for the site visit, none of them drops a region.

EXTRACTION AND AGGREGATION -- EXCAVATED ONLY since the compartment
change (embankment generation is described above and at its own
section) -- scoring stays sharp, grouping makes the survey areas:

  1. MEMBER REGIONS: cells at/above SUITABILITY_THRESHOLD on the RAW
     blended surface (0.5, decided from the raw-surface comparison
     against the parcel's attainable ceiling -- see the constant;
     re-verified every run by the diagnostic's threshold comparison; a
     parameter of the extraction function -- the constant only supplies
     the default) form
     8-connected components (WATER_REGION_CONNECTIVITY -- water's own
     constant, deliberately NOT production's 4: flow-concentrated highs
     run diagonally along stems, and 4-connectivity shreds a diagonal
     chain into beads by definition). Extraction runs on the RAW
     surface: a tuning pass tried pre-threshold smoothing and the
     networked run measured why it cannot work here (raw max 0.820 ->
     0.524 -- a one-cell drainageway ribbon never survives a
     neighborhood average; see masked_focal_mean(), retired).
  2. SURVEY ZONES: per type, member footprints are closed at
     SURVEY_ZONE_GROUPING_DISTANCE_METERS (buffer out half, union,
     buffer back -- a vector closing, gaps up to the full distance
     bridge) and each connected result, clipped to the parcel, is ONE
     SURVEY ZONE whose members are the regions it absorbed. A lone
     region closes back to approximately itself -- one code path for
     clusters and singletons. The zone is the deliverable and the
     downstream selected_water_zone; members ride along as
     sub-features, footprints INTACT. Score statistics come from
     MEMBER CELLS ONLY (the envelope never launders sub-threshold
     ground into a score); dual acreage tells the story -- member_acres
     (the anchoring signal) beside zone_acres (the ground to walk).
     See SURVEY_ZONE_GROUPING_DISTANCE_METERS's docstring for the two
     rule-reconciliations (aggregation vs no-morphology; buffer-UNION
     vs the buffer-difference sliver lesson).

NO MORPHOLOGY ON MEASURED FOOTPRINTS: member polygons are exact cell
unions, never redrawn; the zone's render_fill_polygon_utm IS its clipped
envelope, the identity -- no further reduction downstream of the
aggregation that defines the object (the exclusion_zones precedent).

THE FLOOR (the one place the output narrows, visibly): a zone whose
walkable envelope -- zone_acres, the clipped hull -- sits under the
MIN_SURVEY_REGION_AREA_ACRES floor is DROPPED from the pipeline output:
status: dropped + drop_reason on the zone, both acreages on the record,
carried in the diagnostic table and the export's survey_zone_dropped
layer, never silent (see the constant's history note for the two prior
bases). EVERY SURVIVING ZONE IS PRESENTED: a presentation cap was
tried for one pass and deleted -- all survivors are listed, ranked per
type, with the total count, and the user decides what to walk. Two
honesty reports ride each survivor: sparse_anchor (walkable claim
vastly exceeding its anchor -- SPARSE_ANCHOR_MEMBER_FRACTION) and
cross_type_overlaps (the two surfaces agreeing about the same ground
-- CROSS_TYPE_OVERLAP_NOTE_FRACTION drives the either-type narrative
line).

SELECTION (the pooled rule, still provisional, documented): each type
ranks on its own instrument -- embankment by SEED blend score (the
anchor claim; the compartment's walked-ground mean deliberately
includes low-scoring side slopes and would punish a compartment for
doing its job), excavated by member-mean suitability as always -- and
the two are POOLED on those scores (acreage tiebreak); the pooled
rank-1 SURVIVING zone becomes `selected_water_zone` for downstream
consumers (tree search-space subtraction, fencing, solar exclusion,
road exclusion, the map's ripple clip, keypoint relationships).
Pooling embankment against excavated compares two different
instruments on one scale -- kept because downstream needs ONE
unambiguous answer; revisit from the tuned run. The selected zone
carries every field on the established selected_water_zone consumer
contract (render_fill_polygon_utm = the clipped geometry, identity;
representative_elevation_m; id -- plus rank and
served_production_area_ids read by pipeline tests), so rank-1 slots in
unchanged whichever type wins.

identify_water_survey_areas() is the fetch-and-compute entry point,
following the established conventions: independent optional overrides
that each self-compute when not supplied; canopy fetch-or-raise; road
fetch-or-degrade (an outage yields road_overlap_pct=None, "never
checked", not a fabricated 0.0); soil fetch-or-degrade the same way.
compute_water_survey_areas() is the pure core -- no network I/O, unit-
testable against synthetic DEMs.
"""

import logging
import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely import contains_xy
from shapely.geometry import Point, Polygon, mapping
from shapely.geometry import shape as shapely_shape
from shapely.ops import unary_union
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from feature_schema import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
)
from production_area import METERS_PER_FOOT, compute_slope_percent, get_required_tree_root_zone_mask_utm, _fetch_road_exclusion_union_utm
from production_area_ceiling import identify_optimized_production_areas
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    build_disc_kernel_offsets,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    pixel_center_xy,
)
from soil_data import (
    coordinates_to_wkt_polygon,
    get_saturated_hydraulic_conductivity_for_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
    is_hydric,
)
from valley_delineation import compute_flow_accumulation, compute_flow_direction, fill_depressions

# The demoted level-pool arc stays the home of the shared gate/measurement
# machinery this module reuses -- ONE definition each of the contributing-
# area ceiling, the service-distance reference, the canopy buffer, the
# overlap measurement (with its None-means-never-checked semantics), the
# representative-point gravity relationships, and the unchecked/not-
# supplied sentinels. Importing them keeps this module's numbers identical
# to the ones every existing test and docstring already pins, rather than
# forking a second copy that can drift.
from water_candidate_zones import (
    MAX_SERVICE_DISTANCE_METERS,
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    WATER_ZONE_CANOPY_BUFFER_METERS,
    _CANOPY_CHECK_UNCHECKED,
    _ROAD_CHECK_UNCHECKED,
    _ROAD_UNION_NOT_SUPPLIED,
    _overlap_fraction_pct,
    _zone_production_area_relationships,
)

logger = logging.getLogger(__name__)


# --- the two survey types -------------------------------------------------

SURVEY_TYPE_EMBANKMENT = "embankment"
SURVEY_TYPE_EXCAVATED = "excavated"
SURVEY_TYPES = (SURVEY_TYPE_EMBANKMENT, SURVEY_TYPE_EXCAVATED)
"""NRCS Agriculture Handbook 590's two pond types, and the stable TYPE
IDENTIFIERS every region, layer name, and narrative block keys on (same
type-vs-label split exclusion_zones.LAYER_ORDER documents: these strings
are an API, the human wording lives in labels and prose)."""


# --- gates (unchanged trio, ported) --------------------------------------

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
# compute_gate_mask()) -- zeroing this setback does not weaken off-parcel
# exclusion. Ported verbatim from water_candidate_zones.py (premise kept,
# value zeroed) when this module replaced it as the water step.
# CONFIGURABLE.
MIN_BOUNDARY_SETBACK_METERS = 0.0

# The contributing-area ceiling is IMPORTED (MAX_VALLEY_CONTRIBUTING_AREA_
# ACRES = 20.0, water_candidate_zones.py), not redeclared -- one shared
# definition of "above this a pond needs engineered spillways" (NRCS CPS
# 378's 20-drainage-acre freeboard threshold; see the constant's own
# docstring there for the full reasoning and the Pennsylvania cross-check).
# In THIS module the ceiling is both a GATE (cells above it are masked out
# of both surfaces) and the embankment drainage band's CLIFF (the band
# hard-zeros at the same value) -- deliberately the same number twice, so
# the gate and the score can never disagree about where "too much
# catchment" begins.


# --- derived screens ------------------------------------------------------

# Guard for TWI's flat/zero-slope singularity: tan(beta) is floored at
# this value before dividing, so a dead-flat cell (slope 0, tan 0 -- TWI
# would be +inf) instead reads as an extremely flat, extremely wet cell
# with a large-but-finite TWI. 0.001 is a 0.1% grade -- below anything the
# 5 m DEM can genuinely resolve as "sloped", so the floor only ever binds
# where the terrain is flat beyond the data's own precision. v1 prior,
# literature-informed (the standard ln(a/tan(beta)) formulation offers no
# guidance of its own for tan(beta)=0; a small positive floor is the
# common practical guard). TUNE FROM FIRST RUN. CONFIGURABLE.
TWI_MIN_SLOPE_TAN = 0.001

# Depression depths below this read 0 -- priority-flood fill raises tiny
# sub-noise amounts across much of any real DEM (pit-filling artifacts at
# the data's own vertical precision, not real basins). 0.1 m tracks
# keypoint_detection.KEYPOINT_FILL_ARTIFACT_THRESHOLD_M's neighborhood
# (0.15 m, the existing "marsh gate" precedent for separating real fill
# signal from artifact) while sitting slightly below it, because HERE a
# modest real basin is the SIGNAL being hunted, not an artifact to
# discard. v1 prior, PROVISIONAL -- TUNE FROM FIRST RUN against the
# eastern marsh complex's real fill depths. CONFIGURABLE.
DEPRESSION_NOISE_FLOOR_METERS = 0.1

# Depression depth at/above which the depression component of the wetness
# criterion saturates at 1.0. A natural basin that would pond half a meter
# deep on a 5 m public DEM is unambiguously a storage feature; deeper
# readings add no siting information at this instrument's resolution.
# v1 prior, PROVISIONAL -- TUNE FROM FIRST RUN. CONFIGURABLE.
DEPRESSION_FULL_CREDIT_METERS = 0.5


# --- criteria classification tables ---------------------------------------
# Every table below is an independent constant with the same standing:
# v1 prior, literature-informed (NRCS Agriculture Handbook 590's siting
# guidance for the respective pond type), TUNE FROM FIRST RUN. None of
# them has been validated against a ground-truthed pond site yet; the
# first networked run's isoband export is the instrument they get tuned
# from. All CONFIGURABLE.

# Embankment drainage-area band (acres of contributing area):
#   0 below EMBANKMENT_DRAINAGE_MIN_ACRES (a pond needs catchment to
#   fill -- AH590 rules out drainageways too small to refill an
#   embankment pond), ramping to 1.0 at EMBANKMENT_DRAINAGE_FULL_CREDIT_
#   ACRES, plateauing to the existing MAX_VALLEY_CONTRIBUTING_AREA_ACRES
#   ceiling, then a HARD ZERO above it (the ceiling is both a gate and
#   this band's cliff -- a pond must not demand engineered spillways).
# v1 prior, literature-informed, TUNE FROM FIRST RUN.
EMBANKMENT_DRAINAGE_MIN_ACRES = 0.5
EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES = 2.0

# Embankment slope class (percent grade): 1.0 in the ~3-8% sweet spot
# (enough valley grade to form a basin behind a modest dam, not so much
# that the pool runs thin), tapering to 0 at 0.5% (too flat -- poor
# storage-to-earthwork ratio) and at 15% (too steep -- little storage
# behind any real dam height). Bounds inherited from the retired
# water_suitability.py gradient table's floor/sweet-spot reasoning,
# re-centered on AH590's embankment guidance. v1 prior, TUNE FROM FIRST
# RUN.
EMBANKMENT_SLOPE_FLOOR_PCT = 0.5
EMBANKMENT_SLOPE_SWEET_LOW_PCT = 3.0
EMBANKMENT_SLOPE_SWEET_HIGH_PCT = 8.0
EMBANKMENT_SLOPE_CEILING_PCT = 15.0

# Excavated slope class (percent grade), WIDENED FOR THE SEEP CASE:
# 1.0 through EXCAVATED_SLOPE_FULL_CREDIT_PCT, tapering to 0 at
# EXCAVATED_SLOPE_CEILING_PCT. AH-590's excavated class covers dugout
# AND seep-fed excavated ponds -- on small dissected parcels, wet ground
# is typically hillside SEEP at moderate grade, not a flat basin, and a
# seep-fed excavated pond is dug INTO that grade. The original v1 prior
# (1.0 flat, gone by 8% -- the flat-dugout reading of "relatively level
# areas") was indicted by measured evidence: the reference marsh's
# wettest cells sit at real 5-10% grades with wetness 0.44-0.71 and
# scored 0.00-0.35 under the flat taper, the excavated FINDING named the
# slope classes as the failing suspect, and the soil sub-signal rider
# cleared the soil scorer (that ground's map unit is simply mapped
# non-hydric -- a data surprise, nothing to fix). Embankment slope
# classes are UNTOUCHED. CONFIGURABLE.
EXCAVATED_SLOPE_FULL_CREDIT_PCT = 5.0
EXCAVATED_SLOPE_CEILING_PCT = 15.0

# Excavated run-on preference (acres of contributing area): a dugout
# fills from groundwater and local run-on, so contributing area is a MILD
# preference, not a requirement -- 0 at no run-on, saturating at
# RUNON_FULL_CREDIT_ACRES, plateau to the shared ceiling, hard zero above
# it (same cliff as the embankment band: no pond type gets to demand an
# engineered spillway). v1 prior, TUNE FROM FIRST RUN.
RUNON_FULL_CREDIT_ACRES = 2.0

# Wetness criterion (excavated): TWI percentile blended 50/50 with the
# depression-depth score. Equal split is the honest v1 prior -- TWI says
# "water converges here", depression depth says "water would STAY here";
# neither signal has earned dominance until the first run shows which one
# actually picks out the marsh complex. TUNE FROM FIRST RUN.
WETNESS_TWI_SUBWEIGHT = 0.5
WETNESS_DEPRESSION_SUBWEIGHT = 0.5
assert math.isclose(WETNESS_TWI_SUBWEIGHT + WETNESS_DEPRESSION_SUBWEIGHT, 1.0, abs_tol=1e-9), (
    "wetness subweights must sum to 1.0"
)


# --- soil water-holding scorer (salvaged + extended) ----------------------

# NRCS's own standard Ksat class breakpoints (Soil Survey Manual;
# micrometers/second) -- see soil_data.get_saturated_hydraulic_
# conductivity_for_polygon()'s own comment for the full verification and
# reasoning (ksat_r vs. awc_r, chorizon vs. component). WATER_HOLDING_GOOD
# (0.1) is the top of the "low" class -- comfortably slow/water-holding
# for a pond. WATER_HOLDING_POOR (100.0) is the bottom of the "very
# high"/rapid class -- comfortably too permeable without a liner.
# Salvaged from the water-suitability-basin-scoring branch's soil scorer
# (read, not merged). CONFIGURABLE.
WATER_HOLDING_GOOD_KSAT_UM_PER_S = 0.1
WATER_HOLDING_POOR_KSAT_UM_PER_S = 100.0

# 0-1 score per NRCS hydrologic soil group: group A (high infiltration --
# deep sands/gravels) is the leakiest ground a pond can sit on; group D
# (very slow infiltration -- clays, high water tables) is the most
# water-holding. C/D score high per AH590's soil guidance for both pond
# types. Dual groups ('A/D' etc.) score by their UNDRAINED (second)
# letter -- the natural condition is what a pond floor experiences, not
# the artificially-drained one (NRCS NEH Part 630 Ch. 7's own dual-group
# definition). v1 prior, literature-informed, TUNE FROM FIRST RUN.
# CONFIGURABLE.
HYDROLOGIC_GROUP_SCORES = {"A": 0.0, "B": 0.35, "C": 0.8, "D": 1.0}

# Sub-weights of the composite soil water-holding score (must sum to 1.0;
# renormalized over whichever sub-signals are actually available for a
# map unit -- same renormalize-around-the-gap pattern as the basin-shape
# scorer this branch's exploration pass used, so a missing hydgrp never
# silently reads as a neutral 0.5 vote). Ksat carries half the weight
# because it is the direct seepage measurement; group and hydric are
# corroborating classifications. v1 prior, TUNE FROM FIRST RUN.
# CONFIGURABLE.
SOIL_KSAT_SUBWEIGHT = 0.5
SOIL_HYDROLOGIC_GROUP_SUBWEIGHT = 0.3
SOIL_HYDRIC_SUBWEIGHT = 0.2
assert math.isclose(
    SOIL_KSAT_SUBWEIGHT + SOIL_HYDROLOGIC_GROUP_SUBWEIGHT + SOIL_HYDRIC_SUBWEIGHT, 1.0, abs_tol=1e-9
), "soil sub-weights must sum to 1.0"

# Neutral per-cell soil score where no real reading is available (soil
# never fetched, fetch failed, or the cell sits outside every returned
# map-unit polygon) -- same "unknown defaults to neutral, not penalized"
# convention the retired water_suitability.py scorer and
# solar_suitability.py both use. Cells at this default are NOT counted as
# soil coverage (see MIN_SOIL_COVERAGE_FRACTION below).
SOIL_UNAVAILABLE_SCORE = 0.5

# A region whose soil coverage (the fraction of its cells actually inside
# a map unit with a usable score) falls below this fraction is treated as
# too thin a sample to trust for the confidence signal -- same "a
# component covering just 5% of an area doesn't tell you what's really
# there" reasoning as soil_data._component_confidence(). Salvaged
# unchanged from the basin-scoring branch. CONFIGURABLE.
MIN_SOIL_COVERAGE_FRACTION = 0.3


# --- surface weights (must sum to 1.0 per type; asserted at import) -------
# v1 priors, literature-informed (AH590's relative emphasis per pond
# type), TUNE FROM FIRST RUN. All CONFIGURABLE.

# Embankment-type: catchment is the defining requirement of a dam across
# a drainageway (0.30); valley grade decides whether a dam stores
# anything (0.25); soil decides whether the pool holds (0.25); TWI
# corroborates that water actually converges there (0.20).
EMBANKMENT_WEIGHTS = {
    "drainage_area": 0.30,
    "slope": 0.25,
    "soil": 0.25,
    "twi": 0.20,
}

# Excavated-type: wetness IS the siting question for a dugout (0.35);
# soil decides whether it holds without a liner (0.30); flatness decides
# whether excavation is economical (0.25); run-on is a mild bonus (0.10).
EXCAVATED_WEIGHTS = {
    "wetness": 0.35,
    "soil": 0.30,
    "slope": 0.25,
    "drainage_runon": 0.10,
}

for _survey_type, _weights in ((SURVEY_TYPE_EMBANKMENT, EMBANKMENT_WEIGHTS), (SURVEY_TYPE_EXCAVATED, EXCAVATED_WEIGHTS)):
    _weight_sum = sum(_weights.values())
    assert math.isclose(_weight_sum, 1.0, abs_tol=1e-9), (
        f"{_survey_type} criteria weights must sum to 1.0, got {_weight_sum}"
    )


# --- focal smoothing: RETIRED FROM THE EXTRACTION PATH --------------------

# Radius the retired masked focal mean ran at (~3 cells at the 5 m DEM),
# kept as masked_focal_mean()'s default. WHY IT LEFT THE PATH, with the
# networked tuning run's measured numbers: pre-threshold smoothing
# DILUTED THE INTRINSICALLY LINEAR EMBANKMENT SIGNAL below every
# threshold -- the raw embankment surface peaked at 0.820 on the
# reference property and the smoothed surface at 0.524 under the ~7x7
# masked mean, because a one-cell drainageway ribbon can NEVER survive a
# neighborhood average (a diagonal channel contributes ~5 of a 29-cell
# window; the other 24 cells are its valley sides, and no threshold
# both keeps the diluted ribbon and rejects ordinary ground). The
# neighborhood claim was real but mis-placed: it now lives AFTER
# extraction, as the survey-zone aggregation
# (SURVEY_ZONE_GROUPING_DISTANCE_METERS below) -- scoring stays sharp,
# grouping makes the survey areas. Retired, not deleted, per house
# convention; see masked_focal_mean()'s own docstring.
SURVEY_SMOOTHING_RADIUS_METERS = 15.0


# --- region extraction (EXCAVATED ONLY since the compartment change) ------

# Cells at/above this RAW blended suitability score are extracted into
# member regions -- ON THE EXCAVATED SURFACE ONLY. The embankment type
# no longer extracts regions at all: its surface is a NOMINATION SURFACE
# for seed-based valley compartments (see EMBANKMENT_SEED_MIN_SCORE,
# which carries this constant's 0.5 value over with a recorded semantic
# shift). 0.5, DECIDED FROM MEASURED EVIDENCE (the final tuning
# pass): 0.6 had been chosen from the first run's PRE-smoothing
# isobands; with smoothing retired, the raw-surface threshold comparison
# on the reference property read 16 member regions / 0.51 ac of
# anchoring ground at 0.5 versus 5 sub-floor slivers / 0.08 ac at 0.6.
# The deciding insight: the parcel's ACHIEVABLE maximum blend is ~0.82,
# not 1.0 -- the soil criterion's parcel range caps the arithmetic -- so
# a threshold judges against attainable scores, and 0.6 was demanding
# ~3/4 of the attainable ceiling while 0.5 sits at the coherence line
# the isobands actually show. The diagnostic keeps printing the
# THRESHOLD COMPARISON (0.5 / 0.6 / 0.7, 8-connected) every run for the
# EXCAVATED surface, so the choice remains evidence-checked; the
# embankment threshold-comparison lines are retired WITH extraction
# (the number that instrument tuned no longer exists on that path).
# This constant only supplies the extraction function's DEFAULT; it is
# not baked into the math anywhere. CONFIGURABLE.
SUITABILITY_THRESHOLD = 0.5


# --- embankment seed-based compartments ------------------------------------
# THE EMBANKMENT GENERATION MECHANISM (this branch's design change): the
# two survey types now differ in GENERATION, not just criteria. An
# embankment pond is a VALLEY COMPARTMENT -- a dam site at a narrows, a
# storage reach above it, flanking ridges either side -- and a dugout is
# not. Stages 1-2 (criteria scoring, the weighted blend) survive
# unchanged as the embankment NOMINATION SURFACE; stage 3 and after
# (threshold, 8-connected components, members, closing, hull) leave the
# embankment path entirely. From each seed (an iteratively-claimed
# highest-blend cell) the machinery walks DOWNSTREAM along D8 flow to
# find the valley's width minimum (the pinch -- the embankment cell),
# then assembles the compartment: baseline seed->pinch, perpendicular
# crest transects at both ends, and the pinch cell's watershed clipped
# to the band between them. The hull does not exist on this path; a seed
# with no on-parcel pinch produces NOTHING, honestly, with a reason
# code. The excavated type keeps the full existing pipeline.

# Minimum blend score for a cell to qualify as a compartment seed.
# CARRIES THE RETIRED EMBANKMENT EXTRACTION THRESHOLD'S VALUE (0.5 --
# see SUITABILITY_THRESHOLD's evidence note), with a RECORDED SEMANTIC
# SHIFT: under extraction, 0.5 meant "this cell is part of a survey
# area"; here it means "this cell is worth NOMINATING a compartment
# from" -- a weaker claim, because the compartment that results is
# defined by valley geometry (pinch + watershed band), not by which
# cells cleared the number. The evidence basis (judged against the
# parcel's ~0.82 attainable ceiling) carries over with the value.
# CONFIGURABLE.
EMBANKMENT_SEED_MIN_SCORE = 0.5

# Claim radius of the iterative seeding: each accepted seed claims every
# qualifying cell within this real-ground distance, and seeding repeats
# on the highest remaining blend until no qualifying cell is left --
# UNCAPPED, per the standing no-cap rule (every seed is walked; failures
# report their reason). The VALUE is the old grouping distance's 30 m
# ("two high-suitability patches within 30 m are one site visit, not
# two" -- the same judgement, re-aimed: two seeds within 30 m are one
# candidate compartment, not two), but this is deliberately its OWN
# constant, never aliased to SURVEY_ZONE_GROUPING_DISTANCE_METERS: that
# one is excavated closing machinery and the two numbers must be able to
# move independently. CONFIGURABLE.
EMBANKMENT_SEED_SEPARATION_METERS = 30.0

# How far downstream of its seed the pinch walk will look for the
# embankment cell before giving up. A dam more than ~100 m below the
# storage ground it anchors stops being "this compartment's dam" at
# parcel scale -- and the walk's honesty depends on a bound: without
# one, every seed eventually finds SOME narrows somewhere downstream.
# v1 prior, TUNE FROM FIRST RUN. CONFIGURABLE.
EMBANKMENT_PINCH_WALK_MAX_METERS = 100.0

# The false-crest prominence guard for every outward crest walk (valley
# width at pinch stations, and the compartment transects): a bump along
# the outward-and-up walk only counts as THE crest once elevation has
# fallen at least this far below the highest point seen -- a 0.5 m
# knoll mid-slope dips and the walk keeps climbing past it; a real
# ridge falls away and the walk declares the crest at the high point.
# 1.0 m sits well above the DEM's per-cell vertical noise while staying
# below any real flanking-ridge relief. v1 prior, TUNE FROM FIRST RUN.
# CONFIGURABLE.
RIDGE_PROMINENCE_METERS = 1.0

# Bound on each outward crest walk's half-width. A "valley" wider than
# ~200 m crest-to-crest is not a pond narrows at this pipeline's parcel
# scale, and an unbounded walk on a plain would march to the grid edge.
# Hitting the bound is FLAGGED (half_width_bound_hit), never silent:
# the width recorded at a bounded station is a floor on the truth, not
# a measurement of it. v1 prior, TUNE FROM FIRST RUN. CONFIGURABLE.
RIDGE_WALK_MAX_HALF_WIDTH_METERS = 100.0

# When two compartments overlap by more than this fraction of the
# SMALLER one's area, they are duplicates -- two seeds describing one
# valley compartment -- and collapse to the higher-blend seed's
# compartment; the loser is dropped with a duplicate_of_zone_<id>
# reason code (seeds that walk to the SAME embankment cell collapse
# earlier, before assembly, on the same keep-the-higher-blend rule).
# CONFIGURABLE.
COMPARTMENT_DUPLICATE_OVERLAP_FRACTION = 0.5


# --- survey-zone grouping (the closing over extracted regions) ------------

# Member regions closer than this fuse into one SURVEY ZONE: each
# member's footprint is buffered outward by HALF this distance, the
# buffers are unioned, and the union is buffered back inward by the same
# amount -- a morphological CLOSING in vector space, so gaps up to the
# FULL distance bridge. 30 m is the scale at which two high-suitability
# patches are one site visit, not two. THE CLOSING DECIDES GROUPING
# ONLY (pre-merge change): the drawn zone envelope is the CONVEX HULL
# of the grouped members, clipped to the parcel -- see
# build_survey_zones()'s grouping-vs-drawing split for why the hull is
# the surveyable claim the closing's waisted shape never was.
# CONFIGURABLE.
#
# TWO RULE-RECONCILIATIONS, both principled and both load-bearing:
#
# (a) THE NO-MORPHOLOGY RULE forbids REDRAWING MEASURED FOOTPRINTS --
#     a footprint, once computed, is never smoothed, opened, or shrunk
#     for display (the exclusion_zones precedent). Member footprints
#     survive INTACT here, carried as sub-features with their exact
#     cell-union geometry; the zone envelope is a NEW AGGREGATION OBJECT
#     defined over them ("the ground one survey visit walks"), not a
#     mutation of any of them. The zone's own render_fill is the
#     identity of its envelope -- no further morphology downstream of
#     the aggregation that DEFINES the object.
#
# (b) THE NEVER-VECTORIZE-RASTER-MASKS lesson targeted buffer-DIFFERENCE
#     exclusions, whose subtractions spawn sliver geometry at raster
#     staircases. This is buffer-UNION grouping: outward buffers union
#     (union of round-capped shapes is well-conditioned), and the single
#     inward buffer of that union cannot self-intersect. No differencing
#     happens anywhere in the aggregation.
SURVEY_ZONE_GROUPING_DISTANCE_METERS = 30.0

# THE FLOOR IS A FILTER, judged on ZONE ACRES (the clipped hull): a
# survey zone whose walkable envelope falls below this is DROPPED from
# the pipeline output -- "is this a surveyable area" is measured on the
# ground you'd walk. Noise still dies here: a lone cell's hull IS the
# cell (0.006 ac), far under the floor. Dropped zones are never silent:
# they ride the diagnostic terminal table and the GeoJSON export with
# the established status/reason pattern (status: dropped, drop_reason:
# below_min_area, BOTH acreages on the record) -- visible and
# attributed, excluded from downstream planning. The value matches
# water_candidate_zones.MIN_WATER_ZONE_AREA_ACRES (0.1 ac = 17 cells at
# the pipeline's 5 m DEM resolution), the "smaller than this is
# probably raster noise" line.
#
# HISTORY, kept on purpose. Two prior bases, each honest in its era:
# (1) through the tuning passes this was a FLAG, not a filter
# (first-run posture -- every sliver visible while the open numbers
# were decided from runs that needed to show everything); (2) when the
# filter landed, its basis was MEMBER acres, because under the old
# waisted closing envelope the member cells were the only honest size
# -- the envelope hugged them and measured nothing extra. The pre-merge
# hull change made zone acres the walkable claim (a real, drawn survey
# boundary), so the floor moved to the number the question is actually
# about. The honesty cost of that move is covered by the
# sparse_anchor guard below: an envelope can now exceed its anchoring
# ground, and a zone doing so by more than the guard's ratio says so.
# Individual member REGIONS below the floor still only carry the
# below_min_area flag. CONFIGURABLE.
MIN_SURVEY_REGION_AREA_ACRES = 0.1

# The sparse-anchor honesty guard: a surviving zone whose member_acres /
# zone_acres ratio falls below this fraction carries the sparse_anchor
# flag -- its walkable claim (the hull) vastly exceeds the
# high-suitability ground anchoring it, and the zone announces that
# rather than reading as solid candidate area. Reported, never a gate;
# dual acreage remains mandatory in every zone sentence regardless.
# CONFIGURABLE.
SPARSE_ANCHOR_MEMBER_FRACTION = 0.2

# When a surviving zone's envelope overlaps a surviving zone of the
# OTHER type by more than this fraction of its own envelope, the
# narrative adds the consultant line: this ground scores as a candidate
# for either pond type -- evaluate both approaches during the survey.
# The two surfaces stay structurally separate (no merged-zone
# machinery); this is the two instruments independently AGREEING about
# the same ground -- a finding, reported, never restructured.
# CONFIGURABLE.
CROSS_TYPE_OVERLAP_NOTE_FRACTION = 0.5

# PRESENTATION CAP: DELETED (pre-merge decision). A TOP_N of 3 with a
# per-type guarantee shipped for one pass and was removed entirely --
# constant, guarantee, swap logic, and the presented/unpresented
# distinction (its absence is AST-asserted in the tests; this note is
# the history). With the floor already pruning noise on the walkable
# claim, every surviving zone IS presentable: all survivors are listed,
# ranked per type, with the total count -- the user decides what to
# walk. Selection (the pooled rank-1 -> selected_water_zone) was always
# independent of presentation and is unchanged by the deletion.

# Zone lifecycle status values (the established status/reason export
# pattern): every zone is one or the other, and a dropped zone always
# carries a drop_reason from the flag enumeration -- visible and
# attributed, never silent.
ZONE_STATUS_NOMINATED = "nominated"
ZONE_STATUS_DROPPED = "dropped"

# 8-connected component labeling for WATER survey regions -- deliberately
# NOT production's convention, and never to be aliased with it:
# production_area.cluster_and_gate() uses 4-connectivity ON PURPOSE (its
# waist-detection/single-Polygon-footprint reasoning, documented there),
# while water survey regions use 8 because flow-concentrated suitability
# highs run DIAGONALLY along drainage stems, and 4-connectivity shreds a
# diagonal chain into one-cell beads BY DEFINITION (a diagonal step
# shares no edge). The first reference run demonstrated exactly that:
# regions 2/5/6/7/8/9 were beads along one diagonal ribbon. An
# 8-connected footprint CAN come back as a corner-touch MultiPolygon --
# a shape every water-zone consumer has always accepted (the retired
# scorer documented water geometry as legitimately MultiPolygon).
# CONFIGURABLE, but see the bead history before changing it back.
WATER_REGION_CONNECTIVITY = 8

# Tolerance for the boundary-adjacency measurement: the fraction of a
# region's perimeter within this distance of the parcel boundary line.
# Region footprints are clipped with .intersection(boundary_polygon_utm),
# so coincident edges lie ON the boundary up to float error; 0.01 m is
# far below the 5 m cell size and only absorbs that float error, never a
# real gap. CONFIGURABLE.
BOUNDARY_ADJACENCY_TOLERANCE_METERS = 0.01


# --- flags and reason codes -----------------------------------------------

FLAG_BELOW_MIN_AREA = "below_min_area"
FLAG_NO_SERVICE_RELATIONSHIP = "no_service_relationship"
FLAG_SPARSE_ANCHOR = "sparse_anchor"
"""Module-level flag constants, same convention as water_candidate_zones'
reason/flag enumeration: a caller or test that reacts to a flag compares
against a name, never a re-typed string. no_service_relationship and
sparse_anchor are pure FLAGS (informational, never an outcome);
below_min_area doubles as the drop_reason code when a zone's walkable
envelope (excavated) or compartment polygon (embankment) falls under the
tuned floor (member REGIONS still only ever carry it as a flag).
sparse_anchor is EXCAVATED-ONLY: a compartment has no members, so the
member/envelope ratio it guards does not exist on the embankment path."""

# Compartment truncation flags -- the established truncated_by_* naming
# from the demoted water_candidate_zones arc, carried into this module
# now that zone geometry clips at real ground constraints: fired only
# when a clip actually removed area, reported as both a flags entry and
# a boolean property, never a rejection.
FLAG_TRUNCATED_BY_BOUNDARY = "truncated_by_boundary"
FLAG_TRUNCATED_BY_ROAD = "truncated_by_road"

# An outward crest walk that ran out its RIDGE_WALK_MAX_HALF_WIDTH_METERS
# bound (or left the grid) before elevation fell away: the recorded
# width/transect is a FLOOR on the truth, not a measurement of it.
FLAG_HALF_WIDTH_BOUND_HIT = "half_width_bound_hit"

# Pinch-walk failure reason codes -- a seed that finds no embankment cell
# produces NOTHING (there is no hull on this path; nothing to fall back
# to) and is logged/exported with the reason that names its terminator:
REASON_NO_PINCH_WITHIN_BOUND = "no_pinch_within_bound"
"""The walked reach held no interior width minimum on its own merits:
the valley widened monotonically downstream of the seed, or the distance
bound (or the flow field itself) ended the walk while no confirmed pinch
existed behind it."""
REASON_PINCH_OFF_PARCEL = "pinch_off_parcel"
"""The walk hit the parcel boundary while the valley was still
narrowing -- the width minimum sits at the terminal on-parcel station,
so the true pinch plausibly lies off-parcel, where this pipeline does
not site dams."""
REASON_PINCH_BLOCKED_BY_ROAD = "pinch_blocked_by_road"
"""The walk hit a road-exclusion cell while the valley was still
narrowing -- the reach's pinch sits at or beyond an existing farm road,
which is a hard terminator (roads are a geometric exclusion, like the
boundary), so the seed honestly yields nothing."""
REASON_COMPARTMENT_EMPTY_AFTER_CLIP = "compartment_empty_after_clip"
"""Defensive only -- cannot occur by construction (the baseline's own
cells are on-parcel and never road cells, so the clipped watershed band
always keeps ground): guarded so a geometry-library edge case degrades
to an attributed failed seed, never a crash."""

# The dedupe reason code is dynamic (it names the surviving zone):
# duplicate_of_zone_<id>. The prefix is the constant so tests and
# consumers match against a name, never a re-typed string.
DUPLICATE_OF_ZONE_REASON_PREFIX = "duplicate_of_zone_"


def duplicate_of_zone_reason(zone_id: int) -> str:
    """The dedupe drop_reason/reason_code for a seed or compartment that
    collapsed into surviving zone `zone_id` -- two seeds walking to the
    same embankment cell, or two compartments overlapping beyond
    COMPARTMENT_DUPLICATE_OVERLAP_FRACTION of the smaller."""
    return f"{DUPLICATE_OF_ZONE_REASON_PREFIX}{zone_id}"


PROVENANCE_SUITABILITY_SURFACE = "suitability_surface"
PROVENANCE_SEED_COMPARTMENT = "seed_compartment"
"""Per-object provenance: excavated zones still come from threshold
extraction over the suitability surface; an embankment zone is a valley
compartment generated from a seed (the surface is its NOMINATION
instrument, not its extraction instrument)."""

# Seed lifecycle status values, mirroring the zone status pattern: every
# seed either produced a compartment or failed with an attributed reason.
SEED_STATUS_COMPARTMENT = "compartment"
SEED_STATUS_FAILED = "failed"


# --- narrative note constants ---------------------------------------------

TWI_PARCEL_RELATIVE_NOTE = (
    "Topographic wetness scores here are PARCEL-RELATIVE percentile ranks: a score of 0.9 means "
    "'among the wettest ground on THIS parcel', not wet by any universal standard -- absolute TWI is "
    "resolution-dependent, so only the within-parcel ordering is trustworthy."
)

WATER_SURVEY_AREAS_INTRO_NOTE = (
    "This is a SURVEY AREA from a weighted-overlay suitability surface (NRCS Agriculture Handbook "
    "590's two pond types: embankment-type dams across drainageways, excavated-type dugouts in wet "
    "flat ground) -- general ground worth walking with a survey rod, NOT a designed pond. Nothing "
    "here computes a pool, a wall, a volume, or a station; every classification table and weight "
    "behind this score is a provisional v1 prior awaiting tuning against the first real run. "
    "Ground-truth before committing to anything."
)


# ==========================================================================
# Derived screens
# ==========================================================================

def compute_depression_depth(
    raw_array: np.ndarray,
    filled_array: np.ndarray,
    noise_floor_meters: float = DEPRESSION_NOISE_FLOOR_METERS,
) -> np.ndarray:
    """
    Depression depth per cell: filled DEM minus raw DEM (meters), with
    depths below noise_floor_meters read as 0.0 (fill artifacts at the
    data's vertical precision, not real basins -- see the constant's own
    docstring). Both inputs were already computed every pipeline run; the
    difference was previously discarded. NaN where the raw DEM is nodata.
    """
    depth = filled_array.astype(np.float64) - raw_array.astype(np.float64)
    with np.errstate(invalid="ignore"):
        depth[depth < noise_floor_meters] = 0.0
    depth[np.isnan(raw_array)] = np.nan
    return depth


def compute_topographic_wetness_index(dem: dict, flow_accumulation: np.ndarray, slope_pct: np.ndarray) -> np.ndarray:
    """
    Raw TWI = ln(a / tan(beta)) per cell.

    a is the SPECIFIC catchment area: contributing area per unit contour
    width -- flow_accumulation (upstream cell COUNT, each cell counting
    itself, valley_delineation.compute_flow_accumulation()'s own
    convention) times the cell's ground area, divided by the mean cell
    width ((px+py)/2, the same cell-size convention raster_grid's
    metres<->cells converters use). tan(beta) comes from the existing
    slope machinery (percent grade / 100) and is floored at
    TWI_MIN_SLOPE_TAN to guard the flat/zero-slope singularity explicitly
    (tan 0 would send TWI to +inf; the floor turns dead-flat into
    "extremely flat, finitely wet" instead).

    Returns raw TWI values. THESE ARE NOT SCORES: absolute TWI is
    resolution-dependent, so scoring happens parcel-relative in
    parcel_relative_percentile() -- see the module docstring. NaN where
    slope is NaN (unmeasured cell: grid edge or nodata-adjacent -- Horn's
    kernel needs a full neighborhood) or the DEM is nodata.
    """
    px, py = dem["resolution_meters"]
    cell_width_m = (px + py) / 2.0
    cell_area_m2 = px * py

    specific_catchment = flow_accumulation.astype(np.float64) * cell_area_m2 / cell_width_m
    with np.errstate(invalid="ignore"):
        tan_beta = np.maximum(slope_pct.astype(np.float64) / 100.0, TWI_MIN_SLOPE_TAN)
        twi = np.log(specific_catchment / tan_beta)
    twi[np.isnan(slope_pct)] = np.nan
    twi[np.isnan(dem["array"])] = np.nan
    return twi


def parcel_relative_percentile(values: np.ndarray, parcel_mask: np.ndarray) -> np.ndarray:
    """
    Percentile rank (0.0-1.0) of each cell's value among the VALID
    (non-NaN) on-parcel cells -- the parcel-relative scoring the module
    docstring commits to. MEAN-RANK tie convention: rank = (count
    strictly below + half the OTHER equal values) / (n - 1), so with all
    distinct values the parcel's minimum reads 0.0 and its maximum 1.0,
    while equal ground shares one percentile and a dead-flat parcel
    reads a neutral 0.5 everywhere (every cell is equally its wettest
    and its driest -- 0.0 across the board would falsely read "driest").
    A single-cell parcel reads 1.0 (its only ground is trivially its
    wettest). NaN outside the mask and at invalid cells.
    """
    result = np.full(values.shape, np.nan, dtype=np.float64)
    valid = parcel_mask & ~np.isnan(values)
    n = int(np.count_nonzero(valid))
    if n == 0:
        return result
    valid_values = values[valid]
    if n == 1:
        result[valid] = 1.0
        return result
    sorted_values = np.sort(valid_values)
    # searchsorted: left = count strictly below, right - left = tie count.
    below_counts = np.searchsorted(sorted_values, valid_values, side="left")
    tie_counts = np.searchsorted(sorted_values, valid_values, side="right") - below_counts
    result[valid] = (below_counts + 0.5 * (tie_counts - 1)) / (n - 1)
    return result


# ==========================================================================
# Gates
# ==========================================================================

def compute_gate_mask(
    dem: dict,
    boundary_polygon_utm: Polygon,
    flow_accumulation: np.ndarray,
    max_contributing_area_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
) -> tuple[np.ndarray, dict]:
    """
    The unchanged gate trio as one boolean mask: a cell is in play iff it
    is (1) on-parcel (cell-CENTER containment, the pipeline-wide
    convention), (2) at/below the contributing-area ceiling (the NRCS CPS
    378 spillway line -- see MAX_VALLEY_CONTRIBUTING_AREA_ACRES's own
    docstring in water_candidate_zones.py), and (3) at least
    min_boundary_setback_meters from the boundary line (INERT at the
    ported 0.0 value -- see MIN_BOUNDARY_SETBACK_METERS's docstring
    above; the code path is kept so a setback can return without a schema
    change). NaN-elevation cells are excluded (nodata is not surveyable
    ground).

    Returns (mask, stats) where stats is the flag-not-filter accounting:
    how many cells each gate removed, so the diagnostic can print what
    the mask did rather than leaving it invisible.
    """
    array = dem["array"]
    rows, cols = array.shape
    px, py = dem["resolution_meters"]
    area_per_cell = cell_area_acres(dem)

    col_x = dem["origin_x"] + (np.arange(cols) + 0.5) * px
    row_y = dem["origin_y"] - (np.arange(rows) + 0.5) * py
    xs, ys = np.meshgrid(col_x, row_y)
    on_parcel = contains_xy(boundary_polygon_utm, xs, ys)

    valid_elevation = ~np.isnan(array)
    max_contributing_cells = max_contributing_area_acres / area_per_cell
    under_ceiling = flow_accumulation <= max_contributing_cells

    mask = on_parcel & valid_elevation & under_ceiling

    setback_removed = 0
    if min_boundary_setback_meters > 0:
        boundary_line = boundary_polygon_utm.boundary
        for r, c in np.argwhere(mask):
            x, y = pixel_center_xy(dem, int(r), int(c))
            if Point(x, y).distance(boundary_line) < min_boundary_setback_meters:
                mask[r, c] = False
                setback_removed += 1

    on_parcel_valid = int(np.count_nonzero(on_parcel & valid_elevation))
    stats = {
        "grid_cells": int(rows * cols),
        "on_parcel_cells": on_parcel_valid,
        "ceiling_removed_cells": int(np.count_nonzero(on_parcel & valid_elevation & ~under_ceiling)),
        "setback_removed_cells": setback_removed,
        "gated_cells": int(np.count_nonzero(mask)),
        "max_contributing_area_acres": float(max_contributing_area_acres),
        "min_boundary_setback_meters": float(min_boundary_setback_meters),
    }
    return mask, stats


# ==========================================================================
# Criterion scorers (each classification table gets its own unit)
# ==========================================================================

def drainage_band_score(
    contributing_acres: np.ndarray,
    ceiling_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
) -> np.ndarray:
    """
    Embankment drainage-area band: 0 below EMBANKMENT_DRAINAGE_MIN_ACRES,
    linear ramp to 1.0 at EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES, plateau
    at 1.0 to ceiling_acres, HARD ZERO above it -- the ceiling is both
    the gate and this band's cliff (a pond needs catchment to fill and
    must not demand engineered spillways). Vectorized over an acres
    array; scalar-in, scalar-out also works via numpy broadcasting.
    """
    acres = np.asarray(contributing_acres, dtype=np.float64)
    ramp = (acres - EMBANKMENT_DRAINAGE_MIN_ACRES) / (
        EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES - EMBANKMENT_DRAINAGE_MIN_ACRES
    )
    score = np.clip(ramp, 0.0, 1.0)
    score = np.where(acres > ceiling_acres, 0.0, score)
    return score


def runon_score(
    contributing_acres: np.ndarray,
    ceiling_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
) -> np.ndarray:
    """
    Excavated run-on preference: 0 at no contributing area, linear to 1.0
    at RUNON_FULL_CREDIT_ACRES, plateau, hard zero above the shared
    ceiling. MILD preference by weight (0.10), not by shape -- a dugout
    with no run-on is still a dugout; one demanding a spillway is not.
    """
    acres = np.asarray(contributing_acres, dtype=np.float64)
    score = np.clip(acres / RUNON_FULL_CREDIT_ACRES, 0.0, 1.0)
    score = np.where(acres > ceiling_acres, 0.0, score)
    return score


def embankment_slope_score(slope_pct: np.ndarray) -> np.ndarray:
    """
    Embankment slope class: 0 at/below EMBANKMENT_SLOPE_FLOOR_PCT, linear
    up to 1.0 at EMBANKMENT_SLOPE_SWEET_LOW_PCT, 1.0 through the sweet
    spot, linear down to 0 at EMBANKMENT_SLOPE_CEILING_PCT, 0 above. NaN
    slope (unmeasured cell -- grid edge/nodata-adjacent) scores 0.0
    rather than poisoning the blend with NaN; the criteria-completeness
    confidence signal reports the gap instead of hiding it.
    """
    slope = np.asarray(slope_pct, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        rising = (slope - EMBANKMENT_SLOPE_FLOOR_PCT) / (EMBANKMENT_SLOPE_SWEET_LOW_PCT - EMBANKMENT_SLOPE_FLOOR_PCT)
        falling = (EMBANKMENT_SLOPE_CEILING_PCT - slope) / (
            EMBANKMENT_SLOPE_CEILING_PCT - EMBANKMENT_SLOPE_SWEET_HIGH_PCT
        )
        score = np.minimum(np.clip(rising, 0.0, 1.0), np.clip(falling, 0.0, 1.0))
    return np.where(np.isnan(slope), 0.0, score)


def excavated_slope_score(slope_pct: np.ndarray) -> np.ndarray:
    """
    Excavated slope class, seep-widened: 1.0 through
    EXCAVATED_SLOPE_FULL_CREDIT_PCT (a seep-fed excavated pond is dug
    into moderate grade -- see the constants' evidence note), linear to
    0 at EXCAVATED_SLOPE_CEILING_PCT, 0 above. NaN slope scores 0.0 --
    same reasoning as embankment_slope_score().
    """
    slope = np.asarray(slope_pct, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        falling = (EXCAVATED_SLOPE_CEILING_PCT - slope) / (
            EXCAVATED_SLOPE_CEILING_PCT - EXCAVATED_SLOPE_FULL_CREDIT_PCT
        )
        score = np.clip(falling, 0.0, 1.0)
    return np.where(np.isnan(slope), 0.0, score)


def depression_score(depth_m: np.ndarray) -> np.ndarray:
    """
    0-1 depression component of the wetness criterion: 0 at zero depth
    (which includes everything under the noise floor -- see
    compute_depression_depth()), linear to 1.0 at
    DEPRESSION_FULL_CREDIT_METERS, saturated above. NaN depth (nodata)
    scores 0.0.
    """
    depth = np.asarray(depth_m, dtype=np.float64)
    with np.errstate(invalid="ignore"):
        score = np.clip(depth / DEPRESSION_FULL_CREDIT_METERS, 0.0, 1.0)
    return np.where(np.isnan(depth), 0.0, score)


# --- soil sub-scorers -----------------------------------------------------

def ksat_water_holding_score(ksat_r_um_per_s: Optional[float]) -> Optional[float]:
    """
    0-1 score from real SSURGO saturated hydraulic conductivity: 1.0 at/
    below WATER_HOLDING_GOOD_KSAT_UM_PER_S (comfortably slow -- holds
    water well), 0.0 at/above WATER_HOLDING_POOR_KSAT_UM_PER_S
    (comfortably rapid -- leaks badly), on a LOG scale between them (Ksat
    spans several orders of magnitude in practice, so a linear scale
    would barely differentiate the moderately-low/moderately-high range
    where most real soils actually fall). Salvaged from the
    basin-scoring branch's _water_holding_factor(), with one deliberate
    change: an unavailable reading returns None here (the composite
    renormalizes around the gap) instead of a neutral constant.
    """
    if ksat_r_um_per_s is None:
        return None
    try:
        ksat = max(float(ksat_r_um_per_s), 1e-6)
    except (TypeError, ValueError):
        return None
    log_ksat = math.log10(ksat)
    log_good = math.log10(WATER_HOLDING_GOOD_KSAT_UM_PER_S)
    log_poor = math.log10(WATER_HOLDING_POOR_KSAT_UM_PER_S)
    fraction = (log_ksat - log_good) / (log_poor - log_good)
    return max(0.0, min(1.0, 1.0 - fraction))


def hydrologic_group_score(hydgrp: Optional[str]) -> Optional[float]:
    """
    0-1 score from the NRCS hydrologic soil group (see
    HYDROLOGIC_GROUP_SCORES): C/D high, A low. A dual group ('A/D')
    scores by its UNDRAINED second letter (the natural condition a pond
    floor experiences). None/empty/unrecognized returns None -- unknown
    is renormalized around, never scored.
    """
    if not hydgrp or not isinstance(hydgrp, str):
        return None
    letter = hydgrp.strip().upper()
    if "/" in letter:
        letter = letter.split("/")[-1].strip()
    return HYDROLOGIC_GROUP_SCORES.get(letter)


def hydric_share_for_mukey(component_rows: list[dict]) -> Optional[float]:
    """
    0-1 hydric share of one map unit: the summed comppct_r of its hydric
    components (soil_data.is_hydric()'s definition) / 100, clamped to
    [0, 1]. Hydric presence is a POSITIVE signal for pond siting (already
    wet ground) -- the mirror of production_area's use of the same field
    as an exclusion. Unparseable comppct_r on a hydric row counts as 0
    (the house SSURGO-numeric convention: never act on a value that
    couldn't be read). Returns None only when component_rows is empty
    (nothing known about this map unit's composition at all).
    """
    if not component_rows:
        return None
    share = 0.0
    for row in component_rows:
        if not is_hydric(row.get("hydricrating")):
            continue
        try:
            share += float(row.get("comppct_r"))
        except (TypeError, ValueError):
            continue
    return max(0.0, min(1.0, share / 100.0))


def soil_water_score_for_mukey(
    ksat_r_um_per_s: Optional[float],
    hydgrp: Optional[str],
    component_rows: Optional[list[dict]],
) -> Optional[dict]:
    """
    Composite 0-1 soil water-holding score for one map unit: ksat (log
    ramp), hydrologic group (C/D high), and hydric share (positive),
    weighted by the SOIL_*_SUBWEIGHT constants and RENORMALIZED over
    whichever sub-signals are actually available -- a missing hydgrp
    shrinks the denominator instead of casting a fabricated neutral
    vote. Returns {'score', 'ksat_score', 'hydrologic_group_score',
    'hydric_score'} (sub-scores None where unavailable), or None when NO
    sub-signal is available (the map unit contributes nothing and its
    cells fall back to SOIL_UNAVAILABLE_SCORE, uncounted as coverage).
    """
    ksat_score = ksat_water_holding_score(ksat_r_um_per_s)
    group_score = hydrologic_group_score(hydgrp)
    hydric_score = hydric_share_for_mukey(component_rows) if component_rows is not None else None

    components = []
    if ksat_score is not None:
        components.append((SOIL_KSAT_SUBWEIGHT, ksat_score))
    if group_score is not None:
        components.append((SOIL_HYDROLOGIC_GROUP_SUBWEIGHT, group_score))
    if hydric_score is not None:
        components.append((SOIL_HYDRIC_SUBWEIGHT, hydric_score))

    if not components:
        return None

    weight_total = sum(w for w, _ in components)
    score = sum(w * v for w, v in components) / weight_total
    return {
        "score": score,
        "ksat_score": ksat_score,
        "hydrologic_group_score": group_score,
        "hydric_score": hydric_score,
    }


def build_soil_score_grid(dem: dict, gate_mask: np.ndarray, soil_inputs: Optional[dict]) -> dict:
    """
    Rasterizes per-map-unit soil water-holding scores onto the DEM grid
    by cell-CENTER containment (the pipeline-wide raster<->vector
    convention), over gated cells only (the surfaces are zero elsewhere,
    so scoring off-mask soil would be wasted work).

    soil_inputs is either None -- soil NEVER CHECKED (fetch failed or
    skipped): every cell reads the neutral SOIL_UNAVAILABLE_SCORE,
    coverage is None, availability all False -- or a dict of the three
    pre-fetched pieces, ALL of which ParcelData already carries
    (Layer 1, fetched once, hard-fail governed -- the pipeline path
    forwards them through build_pipeline_context()):

        {
            'ksat_rows':           soil_data.get_saturated_hydraulic_conductivity_for_polygon()
                                     (= ParcelData.saturated_hydraulic_conductivity),
            'components':          soil_data.get_soil_data_for_polygon()
                                     (= ParcelData.soil_components; carries hydgrp
                                      since the hydrologic-group query change),
            'geometries_by_mukey': soil_data.get_soil_geometries_for_polygon()
                                     (= ParcelData.soil_geometries),
        }

    where any piece may itself be an empty list/dict (checked, genuinely
    nothing -- a real answer, distinct from never-checked, per the house
    None-vs-absent doctrine). Hydrologic group is read off each map
    unit's DOMINANT component (components arrive comppct_r DESC, so the
    first row per mukey is the dominant one -- the shared soil_data
    convention); there is no separate hydrologic-group fetch anymore.

    Returns {'score_grid', 'covered_mask', 'availability',
    'scores_by_mukey', 'mukey_by_cell'}: score_grid holds
    SOIL_UNAVAILABLE_SCORE wherever no scoreable map unit contains the
    cell center; covered_mask marks the cells that got a REAL score
    (region soil coverage and the confidence signal count only these);
    mukey_by_cell maps each covered (row, col) to the map unit that
    scored it -- the excavated instrumentation's soil-oddity rider
    reads it to put a map unit and its three sub-signals beside every
    deepest-fill cell.
    """
    shape = dem["array"].shape
    score_grid = np.full(shape, SOIL_UNAVAILABLE_SCORE, dtype=np.float64)
    covered_mask = np.zeros(shape, dtype=bool)

    if soil_inputs is None:
        return {
            "score_grid": score_grid,
            "covered_mask": covered_mask,
            "availability": {"checked": False, "ksat": False, "hydrologic_group": False, "hydric": False},
            "scores_by_mukey": {},
            "mukey_by_cell": {},
        }

    ksat_rows = soil_inputs.get("ksat_rows") or []
    component_rows = soil_inputs.get("components") or []
    geometries_by_mukey = soil_inputs.get("geometries_by_mukey") or {}

    ksat_by_mukey: dict = {}
    for row in ksat_rows:
        ksat_by_mukey.setdefault(row.get("mukey"), row.get("ksat_r"))
    components_by_mukey: dict = {}
    for row in component_rows:
        components_by_mukey.setdefault(row.get("mukey"), []).append(row)
    # Hydrologic group off the DOMINANT component: component rows arrive
    # comppct_r DESC, so each mukey's first row is its dominant one --
    # the same positional convention soil_data's own dominant-per-mukey
    # fetchers use. No separate hydrologic-group fetch exists.
    group_by_mukey: dict = {
        mukey: rows[0].get("hydgrp") for mukey, rows in components_by_mukey.items() if rows
    }

    scores_by_mukey: dict = {}
    prepared_by_mukey: dict = {}
    for mukey, geometry in geometries_by_mukey.items():
        scored = soil_water_score_for_mukey(
            ksat_by_mukey.get(mukey),
            group_by_mukey.get(mukey),
            components_by_mukey.get(mukey) if component_rows else None,
        )
        if scored is None:
            continue
        scores_by_mukey[mukey] = scored
        geometry_utm = transform_geom("EPSG:4326", dem["crs"], geometry)
        prepared_by_mukey[mukey] = (prep(shapely_shape(geometry_utm)), scored["score"])

    mukey_by_cell: dict = {}
    if prepared_by_mukey:
        for r, c in np.argwhere(gate_mask):
            x, y = pixel_center_xy(dem, int(r), int(c))
            point = Point(x, y)
            for mukey, (prepared, score) in prepared_by_mukey.items():
                if prepared.contains(point):
                    score_grid[r, c] = score
                    covered_mask[r, c] = True
                    mukey_by_cell[(int(r), int(c))] = mukey
                    break

    return {
        "score_grid": score_grid,
        "covered_mask": covered_mask,
        "availability": {
            "checked": True,
            "ksat": any(s["ksat_score"] is not None for s in scores_by_mukey.values()),
            "hydrologic_group": any(s["hydrologic_group_score"] is not None for s in scores_by_mukey.values()),
            "hydric": any(s["hydric_score"] is not None for s in scores_by_mukey.values()),
        },
        "scores_by_mukey": scores_by_mukey,
        "mukey_by_cell": mukey_by_cell,
    }


# ==========================================================================
# Focal smoothing
# ==========================================================================

def masked_focal_mean(
    values: np.ndarray,
    mask: np.ndarray,
    resolution_meters: tuple[float, float],
    radius_meters: float = SURVEY_SMOOTHING_RADIUS_METERS,
) -> np.ndarray:
    """
    RETIRED FROM THE EXTRACTION PATH, kept as a utility (retired, not
    deleted, per house convention). One tuning pass ran this over each
    blended surface before thresholding; the networked run then measured
    the failure that removed it: the embankment surface's raw maximum of
    0.820 fell to 0.524 under this ~7x7 masked mean, because the
    embankment signal is INTRINSICALLY LINEAR -- a one-cell drainageway
    ribbon contributes ~5 cells of a 29-cell window and its valley sides
    contribute the rest, so a neighborhood average dilutes the ribbon
    below every workable threshold BY CONSTRUCTION, not by mis-tuning.
    Extraction and thresholding run on the RAW surfaces again; the
    neighborhood claim lives after extraction instead, as the
    survey-zone closing (SURVEY_ZONE_GROUPING_DISTANCE_METERS). Nothing
    on the extraction path calls this function (grep-asserted in
    test_water_survey_areas.py); it remains for any future consumer
    with a genuinely areal signal to smooth.

    Disc-window focal mean over IN-MASK cells only: each masked cell's
    output is the mean of `values` across the mask cells within
    radius_meters of it (real-ground disc via raster_grid.build_disc_
    kernel_offsets(), elliptical in cell terms when px != py). Cells
    outside `mask` -- off-parcel, gate-excluded, off-grid -- are absent
    from BOTH numerator and denominator, so a boundary-adjacent cell's
    neighborhood mean is taken over the ground that actually exists for
    this purpose, never dragged toward zero by cells that don't. Output
    is 0.0 outside the mask (matching the gated surfaces' own
    convention). Pure numpy shift-and-accumulate, no scipy -- the same
    implementation doctrine as raster_grid's morphology helpers.
    """
    rows, cols = values.shape
    contribution = np.where(mask, values, 0.0).astype(np.float64)
    mask_float = mask.astype(np.float64)
    numerator = np.zeros((rows, cols), dtype=np.float64)
    denominator = np.zeros((rows, cols), dtype=np.float64)
    for dr, dc in build_disc_kernel_offsets(resolution_meters, radius_meters):
        dst_r0, dst_r1 = max(0, -dr), rows - max(0, dr)
        dst_c0, dst_c1 = max(0, -dc), cols - max(0, dc)
        if dst_r0 >= dst_r1 or dst_c0 >= dst_c1:
            continue
        src_r0, src_r1 = dst_r0 + dr, dst_r1 + dr
        src_c0, src_c1 = dst_c0 + dc, dst_c1 + dc
        numerator[dst_r0:dst_r1, dst_c0:dst_c1] += contribution[src_r0:src_r1, src_c0:src_c1]
        denominator[dst_r0:dst_r1, dst_c0:dst_c1] += mask_float[src_r0:src_r1, src_c0:src_c1]
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(mask & (denominator > 0), numerator / np.maximum(denominator, 1e-12), 0.0)
    return result


# ==========================================================================
# The two surfaces
# ==========================================================================

def compute_suitability_surfaces(
    dem: dict,
    gate_mask: np.ndarray,
    flow_accumulation: np.ndarray,
    slope_pct: np.ndarray,
    twi_percentile: np.ndarray,
    depression_depth: np.ndarray,
    soil_score_grid: np.ndarray,
) -> dict:
    """
    The two per-cell 0-1 suitability surfaces, kept separate end to end.
    Each is the weighted blend of its type's classed criteria
    (EMBANKMENT_WEIGHTS / EXCAVATED_WEIGHTS); the gate mask zeroes both
    surfaces outside the gated cells BEFORE any extraction or export
    reads them, so a masked cell can never clear a threshold or draw an
    isoband. A NaN TWI percentile (unmeasured slope) contributes 0.0 --
    same flag-not-poison handling as the slope scorers; the
    criteria-completeness confidence signal reports it.

    Returns {'embankment', 'excavated', 'criteria': {type: {criterion:
    array}}} -- the criteria grids ride along so per-region
    per-criterion mean contributions (the narrative-honesty mechanism)
    and the diagnostic's criteria-layer summaries read the SAME arrays
    the blend actually used, never a recomputation.
    """
    contributing_acres = flow_accumulation.astype(np.float64) * cell_area_acres(dem)
    twi_score = np.where(np.isnan(twi_percentile), 0.0, twi_percentile)

    embankment_criteria = {
        "drainage_area": drainage_band_score(contributing_acres),
        "slope": embankment_slope_score(slope_pct),
        "soil": soil_score_grid,
        "twi": twi_score,
    }
    wetness = WETNESS_TWI_SUBWEIGHT * twi_score + WETNESS_DEPRESSION_SUBWEIGHT * depression_score(depression_depth)
    excavated_criteria = {
        "wetness": wetness,
        "soil": soil_score_grid,
        "slope": excavated_slope_score(slope_pct),
        "drainage_runon": runon_score(contributing_acres),
    }

    embankment = np.zeros(dem["array"].shape, dtype=np.float64)
    for name, weight in EMBANKMENT_WEIGHTS.items():
        embankment += weight * embankment_criteria[name]
    excavated = np.zeros(dem["array"].shape, dtype=np.float64)
    for name, weight in EXCAVATED_WEIGHTS.items():
        excavated += weight * excavated_criteria[name]

    embankment[~gate_mask] = 0.0
    excavated[~gate_mask] = 0.0

    return {
        SURVEY_TYPE_EMBANKMENT: embankment,
        SURVEY_TYPE_EXCAVATED: excavated,
        "criteria": {
            SURVEY_TYPE_EMBANKMENT: embankment_criteria,
            SURVEY_TYPE_EXCAVATED: excavated_criteria,
        },
    }


# ==========================================================================
# Region extraction
# ==========================================================================

def _region_footprint(dem: dict, cells: list, boundary_polygon_utm: Polygon):
    """The real cell-union footprint of `cells`, clipped to the parcel
    boundary -- raster_grid.cell_union_footprint() then .intersection(),
    the same construction every other zone footprint in this pipeline
    uses."""
    mask = np.zeros(dem["array"].shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return cell_union_footprint(dem, mask).intersection(boundary_polygon_utm)


def _boundary_adjacency_fraction(polygon_utm, boundary_polygon_utm: Polygon) -> float:
    """
    Fraction of the region's perimeter coincident with the parcel
    boundary -- reported CONTEXT for the site visit ("abuts your northern
    line"), never a gate. Measured as the length of the region perimeter
    within BOUNDARY_ADJACENCY_TOLERANCE_METERS of the boundary line,
    over the region's total perimeter.
    """
    region_boundary = polygon_utm.boundary
    total = region_boundary.length
    if total <= 0:
        return 0.0
    tolerance_zone = boundary_polygon_utm.boundary.buffer(BOUNDARY_ADJACENCY_TOLERANCE_METERS)
    shared = region_boundary.intersection(tolerance_zone)
    return max(0.0, min(1.0, shared.length / total))


def _measure_member_cells(
    dem: dict,
    cells: list,
    surface: np.ndarray,
    criteria: dict,
    weights: dict,
    twi_percentile: np.ndarray,
    depression_depth: np.ndarray,
    flow_accumulation: np.ndarray,
    slope_pct: np.ndarray,
    soil_covered_mask: np.ndarray,
    soil_checked: bool,
) -> dict:
    """
    The full measurement set over one set of MEMBER cells -- shared
    verbatim between region extraction and survey-zone aggregation, so a
    zone's score statistics are byte-identical to what its members'
    cells measure directly: MEMBER CELLS ONLY, on the RAW surface and
    RAW criteria grids. The envelope has no cells and never launders
    sub-threshold ground into any number returned here.
    """
    area_per_cell = cell_area_acres(dem)
    raw_array = dem["array"]

    cell_values = np.array([surface[r, c] for r, c in cells], dtype=np.float64)

    criterion_contributions = {}
    for name, weight in weights.items():
        criterion_grid = criteria[name]
        mean_score = float(np.mean([criterion_grid[r, c] for r, c in cells]))
        criterion_contributions[name] = {
            "weight": weight,
            "mean_score": round(mean_score, 3),
            "weighted_contribution": round(weight * mean_score, 3),
        }

    twi_values = [twi_percentile[r, c] for r, c in cells]
    twi_valid = [v for v in twi_values if not math.isnan(v)]
    depth_values = [depression_depth[r, c] for r, c in cells]
    depth_valid = [v for v in depth_values if not math.isnan(v)]

    # "Wettest cell" = the member cell with the highest TWI percentile
    # (falling back to highest accumulation if no TWI is valid) -- its
    # contributing area answers "how much catchment does the wet heart
    # of this ground actually tap".
    if twi_valid:
        wettest_index = int(np.nanargmax(np.array(twi_values, dtype=np.float64)))
    else:
        wettest_index = int(np.argmax([flow_accumulation[r, c] for r, c in cells]))
    wettest_cell = cells[wettest_index]
    wettest_contributing_acres = float(flow_accumulation[wettest_cell[0], wettest_cell[1]]) * area_per_cell

    slope_values = [slope_pct[r, c] for r, c in cells]
    slope_valid = [v for v in slope_values if not math.isnan(v)]

    soil_covered = sum(1 for r, c in cells if soil_covered_mask[r, c])
    soil_coverage_fraction = (soil_covered / len(cells)) if soil_checked else None

    # Criteria completeness: every member cell fully measured (valid
    # slope, hence valid TWI -- soil availability is the OTHER
    # confidence signal). One unmeasured cell means part of the blend
    # scored 0.0 for lack of data rather than for lack of merit.
    complete_cells = sum(
        1 for r, c in cells if not math.isnan(slope_pct[r, c]) and not math.isnan(twi_percentile[r, c])
    )

    return {
        "mean_suitability": round(float(np.mean(cell_values)), 4),
        "max_suitability": round(float(np.max(cell_values)), 4),
        "criterion_contributions": criterion_contributions,
        "twi_percentile_mean": round(float(np.mean(twi_valid)), 3) if twi_valid else None,
        "twi_percentile_max": round(float(np.max(twi_valid)), 3) if twi_valid else None,
        "depression_depth_mean_m": round(float(np.mean(depth_valid)), 3) if depth_valid else None,
        "depression_depth_max_m": round(float(np.max(depth_valid)), 3) if depth_valid else None,
        "wettest_cell_rowcol": wettest_cell,
        "contributing_area_acres_at_wettest_cell": round(wettest_contributing_acres, 2),
        "slope_median_pct": round(float(np.median(slope_valid)), 2) if slope_valid else None,
        "soil_coverage_fraction": (
            round(soil_coverage_fraction, 3) if soil_coverage_fraction is not None else None
        ),
        "criteria_complete": complete_cells == len(cells),
        # Median raw elevation over member cells -- the SAME definition
        # the demoted water zones used, because pipeline_context's
        # keypoint relationship pass reads this field by direct index
        # (consumer contract).
        "representative_elevation_m": float(np.median([raw_array[r, c] for r, c in cells])),
    }


def extract_survey_regions(
    dem: dict,
    surface: np.ndarray,
    criteria: dict,
    survey_type: str,
    gate_mask: np.ndarray,
    boundary_polygon_utm: Polygon,
    twi_percentile: np.ndarray,
    depression_depth: np.ndarray,
    flow_accumulation: np.ndarray,
    slope_pct: np.ndarray,
    soil_covered_mask: np.ndarray,
    soil_checked: bool,
    threshold: float = SUITABILITY_THRESHOLD,
) -> list[dict]:
    """
    Extracts every 8-connected (WATER_REGION_CONNECTIVITY -- see that
    constant for the deliberate contrast with production's 4) component
    of gated cells at/above `threshold` on one type's RAW blended
    surface into a member-region dict -- geometry, the full measurement
    set, and flags. Extraction runs on the RAW surface: the retired
    pre-threshold smoothing diluted the linear embankment signal below
    every threshold (see masked_focal_mean()); the neighborhood claim
    is build_survey_zones()'s job now, downstream of this function.
    NOTHING IS DROPPED: a region below MIN_SURVEY_REGION_AREA_ACRES
    carries FLAG_BELOW_MIN_AREA and its exact acreage. `threshold` is a
    parameter deliberately -- SUITABILITY_THRESHOLD only supplies the
    default (it is the first number the isoband export exists to tune).

    Gravity relationships, overlaps, ranking, confidence, and ids are
    attached at the ZONE level (build_survey_zones() and the compute
    core); this function owns only what one surface plus the screens can
    say about a component.
    """
    weights = EMBANKMENT_WEIGHTS if survey_type == SURVEY_TYPE_EMBANKMENT else EXCAVATED_WEIGHTS
    area_per_cell = cell_area_acres(dem)

    region_mask = gate_mask & (surface >= threshold)
    labels, count = connected_components(region_mask, connectivity=WATER_REGION_CONNECTIVITY)

    regions: list[dict] = []
    for label in range(count):
        cell_indices = np.argwhere(labels == label)
        cells = [(int(r), int(c)) for r, c in cell_indices]
        polygon_utm = _region_footprint(dem, cells, boundary_polygon_utm)
        if polygon_utm.is_empty:
            # Cannot occur by construction (every member cell's CENTER is
            # inside the boundary, so the clipped footprint keeps at least
            # that cell's inside portion) -- guarded anyway so a geometry
            # library edge case degrades to a logged skip, never a crash.
            logger.warning(
                "extract_survey_regions: %s component of %d cell(s) clipped to empty -- skipped",
                survey_type,
                len(cells),
            )
            continue

        area_acres = len(cells) * area_per_cell
        flags = []
        if area_acres < MIN_SURVEY_REGION_AREA_ACRES:
            flags.append(FLAG_BELOW_MIN_AREA)

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

        measurements = _measure_member_cells(
            dem,
            cells,
            surface,
            criteria,
            weights,
            twi_percentile,
            depression_depth,
            flow_accumulation,
            slope_pct,
            soil_covered_mask,
            soil_checked,
        )

        regions.append(
            {
                "survey_type": survey_type,
                "nominated_by": PROVENANCE_SUITABILITY_SURFACE,
                "cells": cells,
                "cell_count": len(cells),
                "area_acres": round(area_acres, 4),
                **measurements,
                "flags": flags,
                "below_min_area": FLAG_BELOW_MIN_AREA in flags,
                "boundary_adjacency_fraction": round(
                    _boundary_adjacency_fraction(polygon_utm, boundary_polygon_utm), 3
                ),
                "polygon_utm": polygon_utm,
                "geometry_wgs84": geometry_wgs84,
                # IDENTITY, not a copy and not a reduction -- member
                # footprints are exact cell unions, never redrawn (the
                # exclusion_zones precedent). The keys exist for shape
                # parity; the DOWNSTREAM render_fill contract lands on
                # the survey ZONE's envelope now.
                "render_fill_polygon_utm": polygon_utm,
                "render_fill_geometry_wgs84": geometry_wgs84,
            }
        )

    return regions


# ==========================================================================
# Survey zones: the closing over extracted regions
# ==========================================================================

def _close_member_footprints(member_polygons: list, grouping_distance_meters: float) -> list:
    """
    The vector closing that defines survey zones: buffer every member
    footprint outward by HALF the grouping distance, unary_union, buffer
    the union back inward by the same amount. Gaps up to the FULL
    distance bridge; a lone member closes back to approximately itself
    (dilation then erosion of a convex shape is exact; a raster
    staircase rounds by a sliver). Returns the connected result
    polygons, each of which becomes one zone envelope. Buffer-UNION
    only -- no differencing anywhere (see SURVEY_ZONE_GROUPING_DISTANCE_
    METERS's rule-reconciliation (b)).
    """
    if not member_polygons:
        return []
    half = grouping_distance_meters / 2.0
    closed = unary_union([polygon.buffer(half) for polygon in member_polygons]).buffer(-half)
    if closed.is_empty:
        # Cannot occur (a closing contains its input union) -- guarded so
        # a geometry-library edge case degrades to per-member envelopes,
        # never a crash or a dropped member.
        logger.warning("_close_member_footprints: closing came back empty -- falling back to member footprints")
        return list(member_polygons)
    if closed.geom_type == "MultiPolygon":
        return list(closed.geoms)
    return [closed]


def build_survey_zones(
    dem: dict,
    regions: list[dict],
    surfaces: dict,
    gate_context: dict,
    boundary_polygon_utm: Polygon,
    grouping_distance_meters: float = SURVEY_ZONE_GROUPING_DISTANCE_METERS,
    road_union_utm=None,
) -> list[dict]:
    """
    The aggregation step: per type, member regions whose footprints sit
    within grouping_distance_meters of each other fuse into one SURVEY
    ZONE -- one code path for clusters and singletons. The zone is the
    deliverable object downstream consumers receive; members ride along
    intact as sub-features with zone-id linkage both ways.

    Since the compartment change this builder serves the EXCAVATED type
    alone in practice -- the embankment type generates no member regions
    (see generate_embankment_compartments()), so its loop iteration
    naturally yields nothing. `road_union_utm`, when not None, clips the
    drawn envelope at the road exclusion union exactly as the boundary
    clip below does (roads are a geometric exclusion now), flagged
    truncated_by_road; the pre-clip envelope is kept for the
    road_overlap_pct measurement.

    GROUPING AND DRAWING ARE TWO DECISIONS (pre-merge change): the 30 m
    vector closing still decides WHICH members belong together --
    unchanged -- but the drawn zone is now the CONVEX HULL of the union
    of its member polygons, CLIPPED TO THE PARCEL BOUNDARY. The
    envelope is the SURVEYABLE CLAIM -- the ground a surveyor would
    rope off -- and the closing's waisted, member-hugging shape was
    never that object; the hull is. Concavity introduced by the
    boundary clip is acceptable (the boundary is real ground truth); a
    singleton's hull is approximately itself (exactly itself for a
    convex member footprint). The rule-reconciliation restates for the
    stronger shape: the hull is an AGGREGATION OBJECT defined over
    intact member footprints -- a new boundary drawn AROUND measured
    geometry, never a redrawing of it; members ride inside untouched.
    Boundary adjacency computes on the clipped hull.

    Score statistics come from MEMBER CELLS ONLY via the same
    _measure_member_cells() the members themselves used -- the envelope
    never launders sub-threshold ground into a score. Dual acreage
    carries both truths, mandatory in every zone sentence: member_acres
    (cell-count acreage of ground that actually cleared the threshold
    -- the anchoring signal) and zone_acres (the clipped HULL's polygon
    acreage -- the ground to walk; polygon-area acreage is correct here
    exactly because a zone envelope is a drawn boundary, not a cell
    population -- the same cell-vs-polygon acreage split
    exclusion_zones.py documents). A zone whose walkable claim vastly
    exceeds its anchor announces itself via the sparse_anchor flag
    (SPARSE_ANCHOR_MEMBER_FRACTION).

    gate_context bundles the screen arrays _measure_member_cells()
    needs: twi_percentile / depression_depth / flow_accumulation /
    slope_pct / soil_covered_mask / soil_checked.

    Overlaps, gravity, confidence, ids, and ranking are attached by the
    compute core (they need production areas / canopy / roads / the
    cross-type pool).
    """
    zones: list[dict] = []
    for survey_type in SURVEY_TYPES:
        weights = EMBANKMENT_WEIGHTS if survey_type == SURVEY_TYPE_EMBANKMENT else EXCAVATED_WEIGHTS
        members = [region for region in regions if region["survey_type"] == survey_type]
        if not members:
            continue
        envelopes = _close_member_footprints(
            [member["polygon_utm"] for member in members], grouping_distance_meters
        )
        claimed: set = set()
        for envelope in envelopes:
            # A member lies inside exactly one connected component of the
            # closing (the closing contains the member union, and its
            # parts are disjoint); intersects() finds it. The claimed
            # guard is belt-and-braces against a degenerate
            # point-touching tie ever double-assigning one.
            zone_member_indices = [
                index
                for index, member in enumerate(members)
                if index not in claimed and envelope.intersects(member["polygon_utm"])
            ]
            if not zone_member_indices:
                continue
            claimed.update(zone_member_indices)
            zone_members = [members[index] for index in zone_member_indices]

            # The closing `envelope` decided membership above; the DRAWN
            # zone is the convex hull of the member union, clipped to
            # the parcel -- see the docstring's grouping-vs-drawing
            # split.
            member_union = unary_union([member["polygon_utm"] for member in zone_members])
            clipped_envelope = member_union.convex_hull.intersection(boundary_polygon_utm)
            if clipped_envelope.is_empty:
                # Members are on-parcel by construction, so their hull
                # always keeps on-parcel area -- guarded anyway.
                logger.warning("build_survey_zones: clipped hull empty -- using member footprints")
                clipped_envelope = member_union

            # ROADS ARE A GEOMETRIC EXCLUSION now, for both types: the
            # hull clips at the road exclusion union exactly as it
            # clips at the parcel boundary, flagged truncated_by_road
            # when the clip actually removed area (the established
            # truncated_by_* convention). road_union_utm None -- road
            # never checked, or checked and genuinely no mapped road --
            # takes none of these branches, which is what keeps the
            # roadless excavated output byte-identical to the
            # pre-road-clip behavior. The PRE-clip envelope is kept so
            # road_overlap_pct can measure the ground the clip removed
            # (measuring the clipped envelope would be guaranteed
            # zero -- see the compute core's overlap pass).
            truncated_by_road = False
            pre_road_clip_envelope = clipped_envelope
            if road_union_utm is not None:
                after_road = _polygonal(clipped_envelope.difference(road_union_utm))
                if not after_road.is_empty and after_road.area < clipped_envelope.area - 1e-6:
                    truncated_by_road = True
                    clipped_envelope = after_road
                elif after_road.is_empty:
                    # A hull entirely inside the road union: keep the
                    # unclipped envelope with the flag rather than
                    # emitting empty geometry (the overlap measurement
                    # then reads ~100% -- the honest picture).
                    truncated_by_road = True

            member_cells = [cell for member in zone_members for cell in member["cells"]]
            member_acres = len(member_cells) * cell_area_acres(dem)
            zone_acres = clipped_envelope.area / SQUARE_METERS_PER_ACRE

            measurements = _measure_member_cells(
                dem,
                member_cells,
                surfaces[survey_type],
                surfaces["criteria"][survey_type],
                weights,
                gate_context["twi_percentile"],
                gate_context["depression_depth"],
                gate_context["flow_accumulation"],
                gate_context["slope_pct"],
                gate_context["soil_covered_mask"],
                gate_context["soil_checked"],
            )

            flags = []
            if truncated_by_road:
                flags.append(FLAG_TRUNCATED_BY_ROAD)
            # The floor's basis is ZONE acres now (the walkable hull,
            # post-clip -- see MIN_SURVEY_REGION_AREA_ACRES's history
            # note); the flag here mirrors the drop decision the
            # compute core makes on the same number.
            if zone_acres < MIN_SURVEY_REGION_AREA_ACRES:
                flags.append(FLAG_BELOW_MIN_AREA)
            # The honesty guard: a walkable claim vastly exceeding its
            # anchoring ground announces itself rather than reading as
            # solid high-suitability area.
            sparse_anchor = zone_acres > 0 and (member_acres / zone_acres) < SPARSE_ANCHOR_MEMBER_FRACTION
            if sparse_anchor:
                flags.append(FLAG_SPARSE_ANCHOR)

            geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(clipped_envelope))

            zones.append(
                {
                    "survey_type": survey_type,
                    "nominated_by": PROVENANCE_SUITABILITY_SURFACE,
                    "members": zone_members,
                    "member_count": len(zone_members),
                    "cells": member_cells,
                    "cell_count": len(member_cells),
                    # DUAL ACREAGE, both labeled -- the narrative
                    # sentence is "zone_acres to survey, anchored by
                    # member_acres of high-suitability ground".
                    "member_acres": round(member_acres, 4),
                    "zone_acres": round(zone_acres, 4),
                    **measurements,
                    "flags": flags,
                    "below_min_area": FLAG_BELOW_MIN_AREA in flags,
                    "sparse_anchor": FLAG_SPARSE_ANCHOR in flags,
                    "truncated_by_road": truncated_by_road,
                    "pre_road_clip_polygon_utm": pre_road_clip_envelope,
                    "boundary_adjacency_fraction": round(
                        _boundary_adjacency_fraction(clipped_envelope, boundary_polygon_utm), 3
                    ),
                    "polygon_utm": clipped_envelope,
                    "geometry_wgs84": geometry_wgs84,
                    # IDENTITY of the clipped envelope -- the aggregation
                    # DEFINES this object's geometry; no further
                    # morphology is applied to it, ever (rule-
                    # reconciliation (a) at SURVEY_ZONE_GROUPING_
                    # DISTANCE_METERS). This is the downstream
                    # render_fill contract (map ripple clip, fencing,
                    # road/solar exclusion, keypoint distances).
                    "render_fill_polygon_utm": clipped_envelope,
                    "render_fill_geometry_wgs84": geometry_wgs84,
                }
            )
    return zones


def _cells_in_polygon_utm(dem: dict, polygon_utm) -> list:
    """Grid cells whose CENTERS fall inside polygon_utm (the pipeline-
    wide raster<->vector convention) -- the envelope's cell population,
    used so canopy/road overlap on a zone reuses the established
    cell-fraction machinery and its sentinel semantics."""
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    col_x = dem["origin_x"] + (np.arange(cols) + 0.5) * px
    row_y = dem["origin_y"] - (np.arange(rows) + 0.5) * py
    xs, ys = np.meshgrid(col_x, row_y)
    inside = contains_xy(polygon_utm, xs, ys)
    return [(int(r), int(c)) for r, c in np.argwhere(inside)]


# ==========================================================================
# Embankment compartments: seed -> pinch -> compartment
# ==========================================================================
# The embankment generation mechanism (see the constants section above
# for the design statement). The excavated path never touches any of
# this; the shared machinery reused here is the existing D8 flow field
# (valley_delineation), keypoint_detection.build_upstream_map() (the
# watershed inversion -- reused, never reimplemented), and the
# measurement/overlap/gravity helpers both types already share.

def _polygonal(geometry):
    """Normalizes a clip result to polygonal geometry: a shapely
    intersection/difference can legitimately come back as a
    GeometryCollection carrying line/point fragments beside the real
    polygons (a transect band edge grazing a cell corner does it), and
    downstream measurement (perimeter adjacency, area, the wire form)
    is defined over the polygonal part only. Polygon/MultiPolygon pass
    through untouched; a collection keeps its polygonal parts; anything
    else (all-linear, empty) reads as the empty Polygon."""
    if geometry.is_empty:
        return geometry if geometry.geom_type in ("Polygon", "MultiPolygon") else Polygon()
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        return geometry
    if geometry.geom_type == "GeometryCollection":
        parts = [part for part in geometry.geoms if part.geom_type in ("Polygon", "MultiPolygon")]
        if not parts:
            return Polygon()
        return unary_union(parts)
    return Polygon()


def _road_cell_mask(dem: dict, road_union_utm) -> np.ndarray:
    """Boolean grid marking cells whose CENTERS fall inside the road
    exclusion union (the pipeline-wide raster<->vector convention).
    All-False when no union is in play -- an unchecked or clean-None road
    answer leaves every walk and seed ungated, which is what makes the
    roadless path byte-identical to the pre-road-exclusion behavior."""
    shape = dem["array"].shape
    if road_union_utm is None:
        return np.zeros(shape, dtype=bool)
    rows, cols = shape
    px, py = dem["resolution_meters"]
    col_x = dem["origin_x"] + (np.arange(cols) + 0.5) * px
    row_y = dem["origin_y"] - (np.arange(rows) + 0.5) * py
    xs, ys = np.meshgrid(col_x, row_y)
    return contains_xy(road_union_utm, xs, ys)


def select_embankment_seeds(
    dem: dict,
    surface: np.ndarray,
    gate_mask: np.ndarray,
    road_cell_mask: np.ndarray,
    criteria: dict,
    min_score: float = EMBANKMENT_SEED_MIN_SCORE,
    separation_meters: float = EMBANKMENT_SEED_SEPARATION_METERS,
) -> list[dict]:
    """
    Iterative highest-blend seeding over the embankment NOMINATION
    surface: a seed candidate is a gated cell (on-parcel, under the
    contributing-area ceiling, outside the inert setback) NOT inside the
    road exclusion union, with blend >= min_score. The highest-blend
    qualifying cell becomes a seed and claims every qualifying cell
    within separation_meters (real-ground disc); seeding repeats on the
    highest remaining blend until no qualifying cell is left. UNCAPPED,
    per the standing no-cap rule -- every seed is walked downstream and
    the failures report their reasons rather than being pre-pruned here.

    Each seed dict carries the ANCHOR CLAIM the reporting honesty split
    keys on: the seed's own blend score and its per-criterion signature
    (the raw criterion scores AT the seed cell), kept separate from
    whatever the eventual compartment's walked ground averages to.
    Deterministic: argmax ties resolve to the first cell in row-major
    order, so the same surface always seeds identically.
    """
    eligible = gate_mask & ~road_cell_mask & (surface >= min_score)
    working = np.where(eligible, surface, -np.inf)
    rows, cols = surface.shape
    offsets = build_disc_kernel_offsets(dem["resolution_meters"], separation_meters)

    seeds: list[dict] = []
    while True:
        flat_index = int(np.argmax(working))
        r, c = divmod(flat_index, cols)
        if not np.isfinite(working[r, c]):
            break
        x, y = pixel_center_xy(dem, r, c)
        lon, lat = warp_transform(dem["crs"], "EPSG:4326", [x], [y])
        seeds.append(
            {
                "rowcol": (r, c),
                "xy": (x, y),
                "geometry_wgs84": {"type": "Point", "coordinates": (lon[0], lat[0])},
                "blend_score": round(float(surface[r, c]), 4),
                "criteria_signature": {
                    name: round(float(criteria[name][r, c]), 3) for name in EMBANKMENT_WEIGHTS
                },
            }
        )
        for dr, dc in offsets:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                working[nr, nc] = -np.inf
    return seeds


def _flow_direction_unit(dem: dict, rowcol: tuple, flow_to_row: np.ndarray, flow_to_col: np.ndarray):
    """Unit ground-space (dx, dy) vector of the D8 flow step out of
    `rowcol`, or None at the -1 sentinel (outlet / flat-plateau tie --
    compute_flow_direction()'s own convention). Row increases downward
    while y increases upward, hence the dy sign flip."""
    r, c = rowcol
    tr, tc = int(flow_to_row[r, c]), int(flow_to_col[r, c])
    if tr < 0:
        return None
    px, py = dem["resolution_meters"]
    dx = (tc - c) * px
    dy = -(tr - r) * py
    length = math.hypot(dx, dy)
    return (dx / length, dy / length)


def ridge_crest_walk(
    dem: dict,
    start_xy: tuple,
    direction_unit: tuple,
    prominence_meters: float = RIDGE_PROMINENCE_METERS,
    max_half_width_meters: float = RIDGE_WALK_MAX_HALF_WIDTH_METERS,
) -> dict:
    """
    One outward-and-up crest walk: from start_xy, march along
    direction_unit sampling the RAW DEM at half-cell steps, tracking the
    highest elevation seen and where it was. The crest is declared AT
    THAT HIGH POINT once elevation has fallen at least prominence_meters
    below it -- the false-crest guard: a sub-prominence knoll dips and
    the walk keeps climbing past it; only a drop that big means the
    ground has genuinely fallen away behind a crest.

    Bound behavior, honest and flagged: if the walk runs out its
    max_half_width_meters bound -- or leaves the grid / hits nodata --
    before the prominence drop confirms a crest, the returned point is
    the walk's END (not the unconfirmed running high), half_width_m is
    the distance actually walked (the bound value when the bound itself
    ended it), and bound_hit is True: the number is a FLOOR on the
    valley's true half-width, never presented as a measurement of it.

    Returns {'half_width_m', 'crest_xy', 'crest_rowcol',
    'crest_elevation_m', 'bound_hit'}.
    """
    array = dem["array"]
    rows, cols = array.shape
    px, py = dem["resolution_meters"]
    step = min(px, py) / 2.0
    x0, y0 = start_xy
    ux, uy = direction_unit

    def _sample(x: float, y: float):
        col = int(math.floor((x - dem["origin_x"]) / px))
        row = int(math.floor((dem["origin_y"] - y) / py))
        if 0 <= row < rows and 0 <= col < cols:
            value = float(array[row, col])
            if not math.isnan(value):
                return row, col, value
        return None

    best_elevation = -math.inf
    best_distance = 0.0
    best_xy = (x0, y0)
    best_rowcol = None
    start_sample = _sample(x0, y0)
    if start_sample is not None:
        best_rowcol = (start_sample[0], start_sample[1])
        best_elevation = start_sample[2]

    distance = 0.0
    last_xy = (x0, y0)
    last_rowcol = best_rowcol
    last_elevation = best_elevation
    while distance < max_half_width_meters:
        distance = min(distance + step, max_half_width_meters)
        x = x0 + ux * distance
        y = y0 + uy * distance
        sample = _sample(x, y)
        if sample is None:
            # Off-grid or nodata before a confirmed crest: the walk ends
            # at its last measurable point, flagged.
            return {
                "half_width_m": round(distance, 1),
                "crest_xy": last_xy,
                "crest_rowcol": last_rowcol,
                "crest_elevation_m": (
                    round(last_elevation, 2) if math.isfinite(last_elevation) else None
                ),
                "bound_hit": True,
            }
        row, col, elevation = sample
        last_xy, last_rowcol, last_elevation = (x, y), (row, col), elevation
        if elevation > best_elevation:
            best_elevation = elevation
            best_distance = distance
            best_xy = (x, y)
            best_rowcol = (row, col)
        elif best_elevation - elevation >= prominence_meters:
            return {
                "half_width_m": round(best_distance, 1),
                "crest_xy": best_xy,
                "crest_rowcol": best_rowcol,
                "crest_elevation_m": round(best_elevation, 2),
                "bound_hit": False,
            }

    return {
        "half_width_m": round(max_half_width_meters, 1),
        "crest_xy": last_xy,
        "crest_rowcol": last_rowcol,
        "crest_elevation_m": (
            round(last_elevation, 2) if math.isfinite(last_elevation) else None
        ),
        "bound_hit": True,
    }


def measure_valley_width(
    dem: dict,
    rowcol: tuple,
    direction_unit: tuple,
    prominence_meters: float = RIDGE_PROMINENCE_METERS,
    max_half_width_meters: float = RIDGE_WALK_MAX_HALF_WIDTH_METERS,
) -> dict:
    """
    Crest-to-crest valley width at one channel cell: two ridge_crest_
    walk()s outward perpendicular to direction_unit (the LOCAL flow
    direction at a pinch station; the BASELINE direction at a
    compartment transect -- the caller decides, this function only takes
    the vector). width_m is the sum of the two half-widths; bound_hit is
    True when EITHER side's walk was bounded (the width is then a floor
    on the truth -- see ridge_crest_walk()).
    """
    x, y = pixel_center_xy(dem, rowcol[0], rowcol[1])
    perpendicular = (-direction_unit[1], direction_unit[0])
    left = ridge_crest_walk(dem, (x, y), perpendicular, prominence_meters, max_half_width_meters)
    right = ridge_crest_walk(
        dem, (x, y), (-perpendicular[0], -perpendicular[1]), prominence_meters, max_half_width_meters
    )
    return {
        "width_m": round(left["half_width_m"] + right["half_width_m"], 1),
        "left": left,
        "right": right,
        "bound_hit": left["bound_hit"] or right["bound_hit"],
    }


def walk_embankment_pinch(
    dem: dict,
    seed_rowcol: tuple,
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
    on_parcel_mask: np.ndarray,
    road_cell_mask: np.ndarray,
    max_walk_meters: float = EMBANKMENT_PINCH_WALK_MAX_METERS,
    prominence_meters: float = RIDGE_PROMINENCE_METERS,
    max_half_width_meters: float = RIDGE_WALK_MAX_HALF_WIDTH_METERS,
) -> dict:
    """
    The pinch walk: from the seed, downstream along the D8 flow
    direction, measuring crest-to-crest valley width (perpendicular to
    the LOCAL flow direction) at every channel cell visited, bounded by
    max_walk_meters of along-channel ground distance. The walk
    TERMINATES before stepping onto an off-parcel cell, onto a
    road-exclusion cell, past the distance bound, or off the flow field
    (the -1 outlet/flat sentinel) -- so every measured station is
    on-parcel, pre-road, within bound by construction.

    THE EMBANKMENT CELL is the along-channel width minimum among the
    measured stations, and it must be an INTERIOR minimum -- at least
    one station before it (the valley demonstrably narrowed to it) and
    at least one after it (the valley demonstrably widened beyond it).
    FAILURE IS HONEST, NO FALLBACK -- the hull does not exist on this
    path and there is nothing to fall back to:

      * minimum at the LAST station -> the valley was still narrowing
        when the walk was cut off; the reason NAMES THE TERMINATOR:
        pinch_off_parcel (boundary), pinch_blocked_by_road (road), or
        no_pinch_within_bound (distance bound / flow field ran out).
      * minimum at the FIRST station (the seed itself) -> the valley
        widens monotonically downstream: no_pinch_within_bound.

    Returns {'found', 'stations', 'terminator', and either
    {'pinch_index', 'pinch_rowcol', 'pinch_width_m', 'walk_distance_m',
    'half_width_bound_hit'} or {'reason_code'}}. Stations carry each
    cell's width measurement for the diagnostic/export instruments.
    """
    px, py = dem["resolution_meters"]
    stations: list[dict] = []
    current = seed_rowcol
    distance = 0.0
    terminator = "flow_end"
    previous_direction = None

    while True:
        direction = _flow_direction_unit(dem, current, flow_to_row, flow_to_col)
        if direction is None and previous_direction is not None:
            # Terminal station on a flat tie/outlet: measure with the
            # incoming direction rather than skipping the cell.
            direction = previous_direction
        if direction is None:
            # The seed itself has no flow direction: nothing measurable.
            terminator = "flow_end"
            break

        measurement = measure_valley_width(
            dem, current, direction, prominence_meters, max_half_width_meters
        )
        stations.append(
            {
                "rowcol": current,
                "distance_m": round(distance, 1),
                "width_m": measurement["width_m"],
                "measurement": measurement,
            }
        )

        r, c = current
        tr, tc = int(flow_to_row[r, c]), int(flow_to_col[r, c])
        if tr < 0:
            terminator = "flow_end"
            break
        step_meters = math.hypot((tc - c) * px, (tr - r) * py)
        if distance + step_meters > max_walk_meters:
            terminator = "distance_bound"
            break
        if not on_parcel_mask[tr, tc]:
            terminator = "boundary"
            break
        if road_cell_mask[tr, tc]:
            terminator = "road"
            break
        previous_direction = direction
        current = (tr, tc)
        distance += step_meters

    if not stations:
        return {
            "found": False,
            "reason_code": REASON_NO_PINCH_WITHIN_BOUND,
            "terminator": terminator,
            "stations": stations,
        }

    widths = [station["width_m"] for station in stations]
    minimum_index = int(np.argmin(widths))
    if minimum_index == len(stations) - 1:
        reason = {
            "boundary": REASON_PINCH_OFF_PARCEL,
            "road": REASON_PINCH_BLOCKED_BY_ROAD,
        }.get(terminator, REASON_NO_PINCH_WITHIN_BOUND)
        return {"found": False, "reason_code": reason, "terminator": terminator, "stations": stations}
    if minimum_index == 0:
        return {
            "found": False,
            "reason_code": REASON_NO_PINCH_WITHIN_BOUND,
            "terminator": terminator,
            "stations": stations,
        }

    pinch_station = stations[minimum_index]
    return {
        "found": True,
        "terminator": terminator,
        "stations": stations,
        "pinch_index": minimum_index,
        "pinch_rowcol": pinch_station["rowcol"],
        "pinch_width_m": pinch_station["width_m"],
        "walk_distance_m": pinch_station["distance_m"],
        "half_width_bound_hit": pinch_station["measurement"]["bound_hit"],
    }


def watershed_cells(pinch_rowcol: tuple, upstream_map: dict) -> set:
    """
    The full watershed (every cell draining through pinch_rowcol,
    itself included): transitive closure of keypoint_detection.
    build_upstream_map()'s one-step feeder adjacency -- the EXISTING
    upstream-map machinery, reused rather than reimplemented (the same
    fan-out valley_level_pool's backwater delineation runs, minus its
    elevation/distance predicates). The flow field is a DAG (flow only
    ever points strictly downhill), so this terminates; the seen set
    guards ties defensively regardless.
    """
    from collections import deque

    seen = {pinch_rowcol}
    frontier = deque([pinch_rowcol])
    while frontier:
        current = frontier.popleft()
        for feeder in upstream_map.get(current, ()):
            if feeder not in seen:
                seen.add(feeder)
                frontier.append(feeder)
    return seen


def _line_geometry_wgs84(dem: dict, points_utm: list) -> dict:
    """LineString WGS84 wire form for a list of (x, y) UTM points, built
    at the object's birth (stored wire forms -- no serialization-time
    reprojection anywhere in this module)."""
    lons, lats = warp_transform(
        dem["crs"], "EPSG:4326", [p[0] for p in points_utm], [p[1] for p in points_utm]
    )
    return {"type": "LineString", "coordinates": list(zip(lons, lats))}


def build_embankment_compartment(
    dem: dict,
    seed: dict,
    walk: dict,
    upstream_map: dict,
    boundary_polygon_utm: Polygon,
    road_union_utm,
    surfaces: dict,
    gate_context: dict,
    prominence_meters: float = RIDGE_PROMINENCE_METERS,
    max_half_width_meters: float = RIDGE_WALK_MAX_HALF_WIDTH_METERS,
) -> Optional[dict]:
    """
    The compartment (the survey area) for one seed whose pinch walk
    found an embankment cell:

      * BASELINE = seed -> embankment cell (the storage reach's spine).
      * TRANSECTS at BOTH endpoints: outward-and-up crest walks
        perpendicular to the BASELINE direction (not local flow), both
        sides, same prominence guard and half-width bound -- four crest
        points, two transect lines.
      * RIDGE CONNECTION: the embankment cell's watershed (the existing
        upstream-map machinery -- watershed_cells()), clipped to the
        band between the two transects. The lateral boundary of that
        clip IS the ridge line between the transect ends -- hydrology
        handles branching crests and spurs; no crest-tracing exists
        anywhere on this path.
      * The compartment polygon = that bounded watershed band, clipped
        to the parcel boundary AND the road exclusion union, each clip
        flagged (truncated_by_boundary / truncated_by_road) when it
        actually removed area. render_fill_polygon_utm IS the clipped
        compartment, identity; WGS84 stored beside UTM for the
        compartment, the baseline, and both transects, all at birth.

    The band is padded half a cell beyond each endpoint along the
    baseline so the seed and embankment CELLS belong to their own
    compartment whole -- a cut exactly through an endpoint's center
    would exclude the two defining cells from the compartment's own
    cell population.

    Score statistics over the compartment's own cells deliberately
    include low-scoring side slopes and the wall reach -- THAT IS THEIR
    JOB (the compartment is the ground one survey walks, ridge to
    ridge); the seed's blend score and criteria signature ride the
    `seed` block SEPARATELY as the anchor claim, per the reporting
    honesty split. Returns None only on the defensive
    empty-after-clip guard (see REASON_COMPARTMENT_EMPTY_AFTER_CLIP).
    """
    seed_rowcol = seed["rowcol"]
    pinch_rowcol = walk["pinch_rowcol"]
    seed_xy = pixel_center_xy(dem, seed_rowcol[0], seed_rowcol[1])
    pinch_xy = pixel_center_xy(dem, pinch_rowcol[0], pinch_rowcol[1])

    baseline_dx = pinch_xy[0] - seed_xy[0]
    baseline_dy = pinch_xy[1] - seed_xy[1]
    baseline_length = math.hypot(baseline_dx, baseline_dy)
    baseline_unit = (baseline_dx / baseline_length, baseline_dy / baseline_length)
    perpendicular = (-baseline_unit[1], baseline_unit[0])

    transects = []
    for end_name, end_xy in (("seed", seed_xy), ("pinch", pinch_xy)):
        left = ridge_crest_walk(dem, end_xy, perpendicular, prominence_meters, max_half_width_meters)
        right = ridge_crest_walk(
            dem, end_xy, (-perpendicular[0], -perpendicular[1]), prominence_meters, max_half_width_meters
        )
        points_utm = [left["crest_xy"], end_xy, right["crest_xy"]]
        transects.append(
            {
                "end": end_name,
                "left": left,
                "right": right,
                "width_m": round(left["half_width_m"] + right["half_width_m"], 1),
                "bound_hit": left["bound_hit"] or right["bound_hit"],
                "points_utm": points_utm,
                "geometry_wgs84": _line_geometry_wgs84(dem, points_utm),
            }
        )

    # The watershed band: every cell draining through the embankment
    # cell, cut to the strip between the two transects. The band
    # rectangle spans the baseline (padded half a cell each end -- see
    # docstring) and extends laterally far beyond any possible watershed
    # (the grid diagonal), so its only real cuts are the two transect-
    # perpendicular ends; the lateral boundary of the clip is the
    # watershed's own divide -- the ridge line.
    px, py = dem["resolution_meters"]
    end_pad = (px + py) / 4.0
    rows, cols = dem["array"].shape
    lateral_reach = math.hypot(rows * py, cols * px)
    a0 = (seed_xy[0] - baseline_unit[0] * end_pad, seed_xy[1] - baseline_unit[1] * end_pad)
    a1 = (pinch_xy[0] + baseline_unit[0] * end_pad, pinch_xy[1] + baseline_unit[1] * end_pad)
    band = Polygon(
        [
            (a0[0] + perpendicular[0] * lateral_reach, a0[1] + perpendicular[1] * lateral_reach),
            (a0[0] - perpendicular[0] * lateral_reach, a0[1] - perpendicular[1] * lateral_reach),
            (a1[0] - perpendicular[0] * lateral_reach, a1[1] - perpendicular[1] * lateral_reach),
            (a1[0] + perpendicular[0] * lateral_reach, a1[1] + perpendicular[1] * lateral_reach),
        ]
    )

    shed = watershed_cells(pinch_rowcol, upstream_map)
    shed_mask = np.zeros(dem["array"].shape, dtype=bool)
    for r, c in shed:
        shed_mask[r, c] = True
    shed_footprint = cell_union_footprint(dem, shed_mask)

    banded = _polygonal(shed_footprint.intersection(band))

    flags: list[str] = []
    area_epsilon = 1e-6
    clipped = _polygonal(banded.intersection(boundary_polygon_utm))
    if clipped.area < banded.area - area_epsilon:
        flags.append(FLAG_TRUNCATED_BY_BOUNDARY)
    pre_road_clip = clipped
    if road_union_utm is not None and not clipped.is_empty:
        after_road = _polygonal(clipped.difference(road_union_utm))
        if after_road.area < clipped.area - area_epsilon:
            flags.append(FLAG_TRUNCATED_BY_ROAD)
        clipped = after_road
    if clipped.is_empty:
        # Cannot occur by construction (the baseline's own on-parcel,
        # non-road cells always survive both clips) -- guarded so a
        # geometry-library edge case degrades to an attributed failed
        # seed, never a crash.
        logger.warning(
            "build_embankment_compartment: compartment clipped to empty for seed %s -- skipped",
            seed_rowcol,
        )
        return None

    if walk["half_width_bound_hit"] or any(t["bound_hit"] for t in transects):
        flags.append(FLAG_HALF_WIDTH_BOUND_HIT)

    compartment_acres = clipped.area / SQUARE_METERS_PER_ACRE
    cells = _cells_in_polygon_utm(dem, clipped)
    # A compartment too small to hold a single cell center is headed for
    # the acreage floor regardless; its stats honestly come from the
    # anchor cell rather than nothing.
    measurement_cells = cells if cells else [seed_rowcol]

    measurements = _measure_member_cells(
        dem,
        measurement_cells,
        surfaces[SURVEY_TYPE_EMBANKMENT],
        surfaces["criteria"][SURVEY_TYPE_EMBANKMENT],
        EMBANKMENT_WEIGHTS,
        gate_context["twi_percentile"],
        gate_context["depression_depth"],
        gate_context["flow_accumulation"],
        gate_context["slope_pct"],
        gate_context["soil_covered_mask"],
        gate_context["soil_checked"],
    )

    geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(clipped))
    seed_lon, seed_lat = seed["geometry_wgs84"]["coordinates"]
    pinch_lons, pinch_lats = warp_transform(dem["crs"], "EPSG:4326", [pinch_xy[0]], [pinch_xy[1]])
    baseline_points_utm = [seed_xy, pinch_xy]

    return {
        "survey_type": SURVEY_TYPE_EMBANKMENT,
        "nominated_by": PROVENANCE_SEED_COMPARTMENT,
        "cells": cells,
        "cell_count": len(cells),
        # THE compartment acreage: the drawn polygon's own area (a
        # compartment is a drawn boundary, not a cell population -- the
        # same cell-vs-polygon acreage split the excavated hull uses).
        # Carried under the shared zone_acres key so the floor, the
        # drop accounting, and every shape-generic consumer read one
        # name; there is deliberately NO member_acres here -- a
        # compartment has no members.
        "zone_acres": round(compartment_acres, 4),
        **measurements,
        # THE ANCHOR CLAIM, separate from the walked ground's means
        # (the reporting honesty split): the seed's own blend score and
        # per-criterion signature, plus the walk records.
        "seed": {
            "rowcol": seed_rowcol,
            "xy": seed_xy,
            "geometry_wgs84": {"type": "Point", "coordinates": (seed_lon, seed_lat)},
            "blend_score": seed["blend_score"],
            "criteria_signature": dict(seed["criteria_signature"]),
        },
        "seed_blend_score": seed["blend_score"],
        "pinch": {
            "rowcol": pinch_rowcol,
            "xy": pinch_xy,
            "geometry_wgs84": {"type": "Point", "coordinates": (pinch_lons[0], pinch_lats[0])},
            "width_m": walk["pinch_width_m"],
            "walk_distance_m": walk["walk_distance_m"],
            "half_width_bound_hit": walk["half_width_bound_hit"],
        },
        "baseline": {
            "points_utm": baseline_points_utm,
            "length_m": round(baseline_length, 1),
            "geometry_wgs84": _line_geometry_wgs84(dem, baseline_points_utm),
        },
        "transects": transects,
        "walk_stations": walk["stations"],
        "flags": flags,
        "below_min_area": False,  # decided at the floor, over zone_acres
        "truncated_by_boundary": FLAG_TRUNCATED_BY_BOUNDARY in flags,
        "truncated_by_road": FLAG_TRUNCATED_BY_ROAD in flags,
        "half_width_bound_hit": FLAG_HALF_WIDTH_BOUND_HIT in flags,
        "boundary_adjacency_fraction": round(
            _boundary_adjacency_fraction(clipped, boundary_polygon_utm), 3
        ),
        "polygon_utm": clipped,
        "geometry_wgs84": geometry_wgs84,
        # IDENTITY of the clipped compartment -- the clip at the parcel
        # boundary and the road union is part of the object's own
        # definition; no further morphology downstream, ever (the same
        # render_fill contract every water zone has carried).
        "render_fill_polygon_utm": clipped,
        "render_fill_geometry_wgs84": geometry_wgs84,
        # PRE-road-clip geometry, kept so road_overlap_pct can measure
        # the ground the road clip REMOVED from the walkable claim --
        # the meaningful number now that the drawn geometry is clipped
        # at roads (measuring the clipped envelope would be a
        # guaranteed zero).
        "pre_road_clip_polygon_utm": pre_road_clip,
    }


def generate_embankment_compartments(
    dem: dict,
    surfaces: dict,
    gate_mask: np.ndarray,
    on_parcel_mask: np.ndarray,
    road_cell_mask: np.ndarray,
    road_union_utm,
    boundary_polygon_utm: Polygon,
    flow_to_row: np.ndarray,
    flow_to_col: np.ndarray,
    gate_context: dict,
) -> tuple[list[dict], list[dict]]:
    """
    The full embankment generation pass: seed, walk, assemble, and
    PINCH-LEVEL dedupe. Returns (compartment_zones, seed_records).

    seed_records carries EVERY seed with its outcome, per the
    dropped-feature pattern -- status 'compartment' with the eventual
    zone linkage, or status 'failed' with the walk's reason code. Two
    seeds walking to the SAME embankment cell collapse here to the
    higher-blend seed (seeding order is blend-descending, so the first
    claimant wins); the loser's reason code is patched to
    duplicate_of_zone_<id> by the compute core once zone ids exist (via
    the private _duplicate_of_zone reference), or falls back to naming
    the winner seed if the winner itself is later deduped away.
    COMPARTMENT-level overlap dedupe happens in the compute core, after
    every compartment exists (it needs the assembled polygons).
    """
    seeds = select_embankment_seeds(
        dem,
        surfaces[SURVEY_TYPE_EMBANKMENT],
        gate_mask,
        road_cell_mask,
        surfaces["criteria"][SURVEY_TYPE_EMBANKMENT],
    )

    upstream_map = None
    compartments: list[dict] = []
    seed_records: list[dict] = []
    zone_by_pinch: dict = {}

    for seed in seeds:
        record = {
            "rowcol": seed["rowcol"],
            "xy": seed["xy"],
            "geometry_wgs84": seed["geometry_wgs84"],
            "blend_score": seed["blend_score"],
            "criteria_signature": seed["criteria_signature"],
        }
        walk = walk_embankment_pinch(
            dem, seed["rowcol"], flow_to_row, flow_to_col, on_parcel_mask, road_cell_mask
        )
        if not walk["found"]:
            record["status"] = SEED_STATUS_FAILED
            record["reason_code"] = walk["reason_code"]
            record["terminator"] = walk["terminator"]
            record["stations_measured"] = len(walk["stations"])
            logger.info(
                "embankment seed %s produced nothing: %s (terminator=%s, %d station(s))",
                seed["rowcol"],
                walk["reason_code"],
                walk["terminator"],
                len(walk["stations"]),
            )
            seed_records.append(record)
            continue

        pinch_rowcol = walk["pinch_rowcol"]
        if pinch_rowcol in zone_by_pinch:
            # Same embankment cell as an earlier (higher-blend -- seeds
            # come out blend-descending) seed: one compartment, the
            # higher-blend seed keeps it, this one is its duplicate.
            record["status"] = SEED_STATUS_FAILED
            record["_duplicate_of_zone"] = zone_by_pinch[pinch_rowcol]
            record["terminator"] = walk["terminator"]
            record["stations_measured"] = len(walk["stations"])
            seed_records.append(record)
            continue

        if upstream_map is None:
            # Built lazily, once, from the SAME flow arrays the walks
            # used -- the existing keypoint machinery, reused.
            from keypoint_detection import build_upstream_map

            upstream_map = build_upstream_map(flow_to_row, flow_to_col)

        compartment = build_embankment_compartment(
            dem,
            seed,
            walk,
            upstream_map,
            boundary_polygon_utm,
            road_union_utm,
            surfaces,
            gate_context,
        )
        if compartment is None:
            record["status"] = SEED_STATUS_FAILED
            record["reason_code"] = REASON_COMPARTMENT_EMPTY_AFTER_CLIP
            record["terminator"] = walk["terminator"]
            record["stations_measured"] = len(walk["stations"])
            seed_records.append(record)
            continue

        compartments.append(compartment)
        zone_by_pinch[pinch_rowcol] = compartment
        record["status"] = SEED_STATUS_COMPARTMENT
        record["_zone"] = compartment
        seed_records.append(record)

    return compartments, seed_records


def dedupe_compartments_by_overlap(
    compartments: list[dict],
    overlap_fraction: float = COMPARTMENT_DUPLICATE_OVERLAP_FRACTION,
) -> tuple[list[dict], list[dict]]:
    """
    COMPARTMENT-level dedupe: two compartments overlapping beyond
    overlap_fraction of the SMALLER one's area are two seeds describing
    one valley compartment -- the higher-blend seed's compartment is
    kept, the loser is returned in the duplicates list carrying a
    _duplicate_of_zone reference (the compute core writes the
    duplicate_of_zone_<id> reason once ids exist). Kept compartments
    are compared in blend-descending order so a chain of overlaps
    collapses onto the single best seed.
    """
    ordered = sorted(compartments, key=lambda z: -z["seed_blend_score"])
    kept: list[dict] = []
    duplicates: list[dict] = []
    for compartment in ordered:
        winner = None
        for existing in kept:
            intersection = compartment["polygon_utm"].intersection(existing["polygon_utm"]).area
            smaller = min(compartment["polygon_utm"].area, existing["polygon_utm"].area)
            if smaller > 0 and intersection > overlap_fraction * smaller:
                winner = existing
                break
        if winner is None:
            kept.append(compartment)
        else:
            compartment["_duplicate_of_zone"] = winner
            duplicates.append(compartment)
    return kept, duplicates


# ==========================================================================
# Relationships, overlaps, confidence, ranking, selection
# ==========================================================================

def _production_overlap_pct(polygon_utm, production_areas: Optional[list[dict]]) -> Optional[float]:
    """
    Percent of this region's own footprint sitting on ground the
    production layer selected -- the third overlap measurement, beside
    canopy_overlap_pct and road_overlap_pct, with the SAME sentinel
    semantics those two established:

        None -> never checked (no production geometry supplied at all)
        0.0  -> checked, and the region genuinely overlaps none

    Measured against render_fill_polygon_utm, the geometry the map
    actually draws. REPORTED, NOT SCORED: conceding production ground to
    a pond is a land-use tradeoff of the same standing as clearing
    canopy for one -- the survey measures it and leaves the trade to the
    farmer. (Salvaged from the basin-scoring branch's pattern, read not
    merged.)
    """
    if production_areas is None:
        return None
    if not production_areas:
        return 0.0
    geometries = [
        pa["render_fill_polygon_utm"]
        for pa in production_areas
        if pa.get("render_fill_polygon_utm") is not None and not pa["render_fill_polygon_utm"].is_empty
    ]
    if not geometries:
        return 0.0
    zone_area = polygon_utm.area
    if zone_area <= 0:
        return 0.0
    union = unary_union(geometries)
    return round(polygon_utm.intersection(union).area / zone_area * 100, 1)


def _confidence_for_region(region: dict, soil_checked: bool) -> str:
    """
    Real, per-region confidence from two checkable quality signals --
      1. soil coverage: the region's own footprint matched to scoreable
         SSURGO map units across at least MIN_SOIL_COVERAGE_FRACTION of
         its cells (a fetch that succeeded but covers 2% of the region
         isn't a trustworthy read -- same reasoning as the retired
         scorer and soil_data._component_confidence())
      2. criteria completeness: every member cell fully measured (valid
         slope and TWI) -- no part of the blend scored 0.0 for lack of
         DATA rather than lack of merit
    2 signals -> CONFIDENCE_HIGH, 1 -> CONFIDENCE_MEDIUM, 0 ->
    CONFIDENCE_LOW. Genuinely varies region-to-region within one run.
    """
    coverage = region["soil_coverage_fraction"]
    soil_signal = soil_checked and coverage is not None and coverage >= MIN_SOIL_COVERAGE_FRACTION
    completeness_signal = bool(region["criteria_complete"])
    signal_count = int(soil_signal) + int(completeness_signal)
    if signal_count >= 2:
        return CONFIDENCE_HIGH
    if signal_count == 1:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _gravity_note(region: dict) -> str:
    primary = region["primary_production_area_relationship"]
    if primary is None:
        return (
            "No production area sits within service range of this region -- ranking context only "
            "(flagged, not dropped): a water feature here would serve future or off-plan uses."
        )
    if primary["above_production_area"]:
        return (
            f"Sits {primary['elevation_differential_m']}m above production area "
            f"{primary['production_area_id']} over {primary['distance_m']}m -- a real gravity-feed "
            "relationship, reported as ranking context and narrative, never a gate."
        )
    return (
        f"Sits {abs(primary['elevation_differential_m'])}m BELOW production area "
        f"{primary['production_area_id']} over {primary['distance_m']}m -- delivering water there "
        "would need a pump (PUMP-REQUIRED). A real cost/maintenance tradeoff, not a defect: this "
        "region survives with the note, exactly as the retired scorer's pump-required candidates did."
    )


def _soil_note(region: dict, soil_checked: bool) -> str:
    if not soil_checked:
        return (
            "Soil was NEVER CHECKED for this run (fetch failed or skipped) -- the soil criterion "
            "defaulted to a neutral value for every cell, NOT measured."
        )
    coverage = region["soil_coverage_fraction"]
    return (
        f"SSURGO soil (ksat water-holding, hydrologic group, hydric share) covered "
        f"{round((coverage or 0.0) * 100, 1)}% of this region's own cells; uncovered cells scored "
        "the neutral default and do not count toward confidence."
    )


def _confidence_notes_for_region(region: dict, soil_checked: bool) -> str:
    type_note = (
        "Embankment-type: a small dam across a drainageway (AH590), generated as a VALLEY "
        "COMPARTMENT -- dam site at the walked width minimum, storage reach above it, ridge-bounded "
        "by the dam site's own watershed; the compartment's score statistics deliberately include "
        "its low-scoring side slopes and wall reach (the seed's own blend score is reported "
        "separately as the anchor claim)."
        if region["survey_type"] == SURVEY_TYPE_EMBANKMENT
        else "Excavated-type: a dugout in wet, flat ground (AH590)."
    )
    return " ".join(
        [
            WATER_SURVEY_AREAS_INTRO_NOTE,
            type_note,
            TWI_PARCEL_RELATIVE_NOTE,
            _gravity_note(region),
            _soil_note(region, soil_checked),
        ]
    )


def _selection_score(zone: dict) -> float:
    """The one number each type ranks and pools on: the SEED's blend
    score for an embankment compartment (the anchor claim -- the
    compartment's walked-ground mean deliberately includes low-scoring
    side slopes and the wall reach, so ranking on it would punish a
    compartment for doing its job), and the MEMBER-mean suitability for
    an excavated zone (as today)."""
    if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
        return zone["seed_blend_score"]
    return zone["mean_suitability"]


def _selection_tiebreak_acres(zone: dict) -> float:
    """Acreage tiebreak between equally-scored zones: the anchoring
    member acres for excavated (the envelope's own acreage never ranks
    anything there); the compartment's own acreage for embankment (the
    only acreage a compartment has)."""
    if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
        return zone["zone_acres"]
    return zone["member_acres"]


def rank_survey_zones_per_type(zones: list[dict]) -> None:
    """
    Assigns `rank` per type IN PLACE: 1 = highest score within that
    type, acreage as the tiebreak (see _selection_score() /
    _selection_tiebreak_acres() for the per-type definitions --
    embankment ranks by SEED blend score since the compartment change;
    excavated by member-mean suitability with member acreage, as
    always). Every zone is ranked; flags never affect rank.
    """
    for survey_type in SURVEY_TYPES:
        typed = [zone for zone in zones if zone["survey_type"] == survey_type]
        typed.sort(key=lambda zone: (-_selection_score(zone), -_selection_tiebreak_acres(zone)))
        for rank, zone in enumerate(typed, start=1):
            zone["rank"] = rank


def attach_cross_type_overlaps(zones: list[dict]) -> None:
    """
    THE AGREEMENT REPORT, attached IN PLACE to every surviving zone: for
    each surviving zone of the OTHER type whose envelope intersects this
    one's, a {zone_id, fraction} entry (fraction = intersected share of
    THIS zone's own envelope area), carried whenever nonzero. The two
    surfaces stay structurally separate -- no merged-zone machinery,
    ever -- this is the two instruments independently AGREEING about
    the same ground, a finding to report, not a structure to build.
    Above CROSS_TYPE_OVERLAP_NOTE_FRACTION the narrative adds the
    consultant line (this area is a candidate for either pond type --
    evaluate both approaches during the survey).
    """
    for zone in zones:
        overlaps = []
        zone_area = zone["polygon_utm"].area
        if zone_area > 0:
            for other in zones:
                if other["survey_type"] == zone["survey_type"]:
                    continue
                intersection_area = zone["polygon_utm"].intersection(other["polygon_utm"]).area
                if intersection_area > 0:
                    overlaps.append(
                        {"zone_id": other["id"], "fraction": round(intersection_area / zone_area, 3)}
                    )
        zone["cross_type_overlaps"] = overlaps


def select_survey_zone(zones: list[dict]) -> Optional[dict]:
    """
    The single selected_water_zone answer for downstream consumers:
    embankment and excavated POOLED on each type's own selection score
    (acreage tiebreak), rank-1 of the pool wins. Since the compartment
    change the pooled scale compares an embankment SEED's blend score
    against an excavated zone's MEMBER-mean suitability -- two
    different instruments' anchor numbers on one 0-1 scale.
    PROVISIONAL AND DOCUMENTED AS SUCH, deliberately simple: pooling
    two instruments is defensible only because downstream needs ONE
    unambiguous answer; revisit from the tuned run (the winner's type
    is itself a finding). Flags never affect selection. Returns None
    when no zone exists at all -- the real, reportable "nothing
    survived" outcome, not an error.
    """
    if not zones:
        return None
    return max(zones, key=lambda zone: (_selection_score(zone), _selection_tiebreak_acres(zone)))


# ==========================================================================
# Pure core
# ==========================================================================

def compute_water_survey_areas(
    dem: dict,
    boundary_polygon_utm: Polygon,
    production_areas: Optional[list[dict]] = None,
    canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
    road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    soil_inputs: Optional[dict] = None,
    threshold: float = SUITABILITY_THRESHOLD,
    filled: Optional[np.ndarray] = None,
    flow_accumulation: Optional[np.ndarray] = None,
    slope_pct: Optional[np.ndarray] = None,
    flow_to_row: Optional[np.ndarray] = None,
    flow_to_col: Optional[np.ndarray] = None,
) -> dict:
    """
    Pure computation over already-fetched inputs -- no network I/O
    anywhere below here (the identify_* entry point owns the fetches).

    production_areas is None for "never checked" (production overlap and
    gravity report their None/empty answers accordingly) or the
    optimized scored_patches list. canopy_root_zone_mask_utm /
    road_exclusion_union_utm carry the shared unchecked sentinels from
    water_candidate_zones (None on the road union is the CLEAN "checked,
    genuinely no mapped road" answer, per the established semantics) --
    since the roads-as-exclusion change the union is no longer only a
    measurement: it gates embankment seeds, terminates embankment pinch
    walks, and clips BOTH types' zone geometry (truncated_by_road).
    soil_inputs: see build_soil_score_grid(). filled /
    flow_accumulation / slope_pct / flow_to_row / flow_to_col are
    optional precomputed overrides so an orchestrator (or a test spying
    on call counts) can guarantee each derivation runs EXACTLY ONCE;
    each self-computes when absent (the flow-direction pair is needed
    unconditionally now -- the embankment pinch walks follow it).

    Returns a dict of survey ZONES (the deliverable, per-type lists,
    pooled selection): EXCAVATED zones are closing-aggregated hull
    envelopes over threshold-extracted member REGIONS (all members
    carried, footprints intact) exactly as before; EMBANKMENT zones are
    SEED-BASED VALLEY COMPARTMENTS (see the compartment section) with
    NO members -- extraction, closing, and the hull no longer exist on
    that path. Also returned: embankment_seeds (every seed with its
    outcome -- the dropped-feature pattern), the surfaces dict
    (surfaces[type] = the RAW blends; the embankment one is a
    NOMINATION surface now -- nothing thresholds it; surfaces
    ["criteria"] = the raw criterion grids), the screens (including the
    unfloored depression_depth_raw for the excavated instrumentation),
    the gate mask, and its stats (numpy/shapely -- NOT
    JSON-serializable; the geojson/narrative builders produce the wire
    forms).
    """
    array = dem["array"]

    if filled is None:
        filled = fill_depressions(array)
    if flow_to_row is None or flow_to_col is None:
        flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    if flow_accumulation is None:
        flow_accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)
    if slope_pct is None:
        # RAW array, not filled -- the same choice the demoted water arc
        # made: slope should describe the real ground, not the
        # hydrologically-conditioned surface.
        slope_pct = compute_slope_percent(array, dem["resolution_meters"])

    gate_mask, gate_stats = compute_gate_mask(dem, boundary_polygon_utm, flow_accumulation)

    depression_depth = compute_depression_depth(array, filled)
    # The UNFLOORED fill depth rides along for the excavated-class
    # instrumentation (diagnose_water_survey_areas.py's before/after-
    # noise-floor distribution and deepest-cell table) -- computed here,
    # once, so the diagnostic interrogates the same arrays the blend
    # used rather than re-deriving its own.
    depression_depth_raw = compute_depression_depth(array, filled, noise_floor_meters=0.0)
    twi_raw = compute_topographic_wetness_index(dem, flow_accumulation, slope_pct)
    # Parcel-relative over the ON-PARCEL population (not just gated
    # cells): the ceiling gate removes high-accumulation cells from PLAY,
    # but they are still parcel ground -- excluding them from the
    # percentile population would silently inflate every surviving
    # cell's wetness rank.
    px, py = dem["resolution_meters"]
    col_x = dem["origin_x"] + (np.arange(array.shape[1]) + 0.5) * px
    row_y = dem["origin_y"] - (np.arange(array.shape[0]) + 0.5) * py
    xs, ys = np.meshgrid(col_x, row_y)
    on_parcel = contains_xy(boundary_polygon_utm, xs, ys) & ~np.isnan(array)
    twi_percentile = parcel_relative_percentile(twi_raw, on_parcel)

    soil_checked = soil_inputs is not None
    soil = build_soil_score_grid(dem, gate_mask, soil_inputs)

    # RAW surfaces all the way through: extraction, thresholding, the
    # isobands, and the threshold comparison. Pre-threshold smoothing is
    # RETIRED from this path (the measured dilution failure -- see
    # masked_focal_mean()); the neighborhood claim happens after
    # extraction, in build_survey_zones() below.
    surfaces = compute_suitability_surfaces(
        dem, gate_mask, flow_accumulation, slope_pct, twi_percentile, depression_depth, soil["score_grid"]
    )

    # The road union resolves EARLY now: it gates embankment seeds,
    # terminates embankment walks, and clips both types' geometry, in
    # addition to the overlap measurement it always fed. The sentinel
    # semantics are unchanged: unchecked and clean-None both mean "no
    # road constraint in play" for the geometry (and the roadless path
    # is byte-identical to the pre-road-exclusion behavior); they still
    # differ for the reported percentage (None vs 0.0).
    canopy_checked = canopy_root_zone_mask_utm is not _CANOPY_CHECK_UNCHECKED
    canopy_mask = canopy_root_zone_mask_utm if canopy_checked else None
    road_checked = road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED
    road_union = road_exclusion_union_utm if road_checked and road_exclusion_union_utm is not None else None
    road_prepared = prep(road_union) if road_union is not None else None
    road_cells = _road_cell_mask(dem, road_union)

    gate_context = {
        "twi_percentile": twi_percentile,
        "depression_depth": depression_depth,
        "flow_accumulation": flow_accumulation,
        "slope_pct": slope_pct,
        "soil_covered_mask": soil["covered_mask"],
        "soil_checked": soil_checked,
    }

    # EXCAVATED: the full existing pipeline -- threshold extraction into
    # member regions, closing aggregation, hull envelope (now clipped at
    # the road union as at the boundary).
    regions = extract_survey_regions(
        dem,
        surfaces[SURVEY_TYPE_EXCAVATED],
        surfaces["criteria"][SURVEY_TYPE_EXCAVATED],
        SURVEY_TYPE_EXCAVATED,
        gate_mask,
        boundary_polygon_utm,
        twi_percentile,
        depression_depth,
        flow_accumulation,
        slope_pct,
        soil["covered_mask"],
        soil_checked,
        threshold=threshold,
    )
    excavated_zones = build_survey_zones(
        dem,
        regions,
        surfaces,
        gate_context,
        boundary_polygon_utm,
        road_union_utm=road_union,
    )

    # EMBANKMENT: seed-based valley compartments. The blend surface is
    # the NOMINATION instrument; generation is seeding + the pinch walk
    # + the watershed-band compartment. Pinch-level dedupe happens
    # inside; compartment-overlap dedupe here, where every polygon
    # exists.
    compartments, embankment_seeds = generate_embankment_compartments(
        dem,
        surfaces,
        gate_mask,
        on_parcel,
        road_cells,
        road_union,
        boundary_polygon_utm,
        flow_to_row,
        flow_to_col,
        gate_context,
    )
    kept_compartments, duplicate_compartments = dedupe_compartments_by_overlap(compartments)

    # One cross-type zone list: kept compartments and excavated zones
    # are the live candidates; overlap-duplicate compartments ride along
    # for identity/attribution and are force-dropped below.
    zones = kept_compartments + excavated_zones + duplicate_compartments

    # Overlaps + gravity on the ZONE, both pure measurements over inputs
    # in hand. Canopy overlap runs on the (clipped) envelope's cell
    # population and production overlap on the envelope polygon, as
    # always. ROAD overlap now measures the PRE-road-clip geometry --
    # the ground the road clip REMOVED from the walkable claim -- since
    # the drawn geometry clips at the union and measuring it there
    # would be a guaranteed zero (None still means never checked; 0.0
    # means checked and nothing removed). Gravity and representative
    # elevation via the existing representative-point machinery -- from
    # member cells for excavated, from the compartment for embankment.
    for zone in zones:
        envelope_cells = _cells_in_polygon_utm(dem, zone["polygon_utm"])
        zone["canopy_overlap_pct"] = _overlap_fraction_pct(envelope_cells, dem, canopy_checked, mask_utm=canopy_mask)
        road_measurement_polygon = zone.get("pre_road_clip_polygon_utm") or zone["polygon_utm"]
        road_measurement_cells = (
            envelope_cells
            if road_measurement_polygon is zone["polygon_utm"]
            else _cells_in_polygon_utm(dem, road_measurement_polygon)
        )
        zone["road_overlap_pct"] = _overlap_fraction_pct(
            road_measurement_cells, dem, road_checked, prepared_union=road_prepared
        )
        zone["production_overlap_pct"] = _production_overlap_pct(zone["polygon_utm"], production_areas)

        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
            representative_point = zone["polygon_utm"].centroid
        else:
            representative_point = unary_union(
                [member["polygon_utm"] for member in zone["members"]]
            ).centroid
        relationships = (
            _zone_production_area_relationships(
                representative_point,
                zone["representative_elevation_m"],
                production_areas,
                MAX_SERVICE_DISTANCE_METERS,
            )
            if production_areas
            else []
        )
        zone["production_area_relationships"] = relationships
        # None where nothing is in range -- an honest empty answer, not a
        # fabricated relationship, and a FLAG rather than a drop: gravity
        # is ranking context and narrative, never a gate. PUMP-REQUIRED
        # (below-elevation) relationships survive with their note.
        zone["primary_production_area_relationship"] = relationships[0] if relationships else None
        zone["has_service_relationship"] = bool(relationships)
        zone["served_production_area_ids"] = sorted({r["production_area_id"] for r in relationships})
        if not relationships:
            zone["flags"].append(FLAG_NO_SERVICE_RELATIONSHIP)

    # Ids, member linkage, confidence -- over ALL zones (a dropped zone
    # keeps its full property set so the diagnostic and export can show
    # it attributed, never silent). Zone ids are assigned over the full
    # cross-type list -- overlap-duplicates included -- so every zone
    # has one unambiguous identity and every duplicate_of_zone_<id>
    # reason can name a real id; member regions get their own ids and
    # each carries its parent's zone_id (excavated only -- a compartment
    # has no members).
    for zone_id, zone in enumerate(zones):
        zone["id"] = zone_id
    for region_id, region in enumerate(regions):
        region["id"] = region_id
    for zone in excavated_zones:
        zone["member_ids"] = [member["id"] for member in zone["members"]]
        for member in zone["members"]:
            member["zone_id"] = zone["id"]
    for zone in zones:
        zone["confidence"] = _confidence_for_region(zone, soil_checked)
        zone["confidence_notes"] = _confidence_notes_for_region(zone, soil_checked)
    for region in regions:
        region["confidence"] = _confidence_for_region(region, soil_checked)

    # Seed-record linkage, now that ids exist: a seed that built a
    # compartment carries its zone_id; a seed that lost a pinch-level or
    # overlap-level dedupe carries duplicate_of_zone_<winner id>. The
    # private object references never leave this function.
    for record in embankment_seeds:
        winner = record.pop("_duplicate_of_zone", None)
        if winner is not None:
            record["reason_code"] = duplicate_of_zone_reason(winner["id"])
        zone_ref = record.pop("_zone", None)
        if zone_ref is not None:
            record["zone_id"] = zone_ref["id"]

    # THE FLOOR FILTERS on ZONE acres -- the walkable hull for
    # excavated, the compartment's own polygon acreage for embankment
    # (see MIN_SURVEY_REGION_AREA_ACRES's history note): a zone under
    # the floor is dropped from the pipeline output -- status/reason
    # attached, rank None -- and survives only in the diagnostic table
    # and the export's dropped layer. An overlap-duplicate compartment
    # drops FIRST, with its duplicate_of_zone_<id> reason (dedupe
    # decides existence; the floor only ever judges survivors).
    surviving_zones: list[dict] = []
    dropped_zones: list[dict] = []
    duplicate_set = {id(zone) for zone in duplicate_compartments}
    for zone in zones:
        if id(zone) in duplicate_set:
            winner = zone.pop("_duplicate_of_zone")
            zone["status"] = ZONE_STATUS_DROPPED
            zone["drop_reason"] = duplicate_of_zone_reason(winner["id"])
            zone["rank"] = None
            zone["cross_type_overlaps"] = []
            dropped_zones.append(zone)
        elif zone["zone_acres"] < MIN_SURVEY_REGION_AREA_ACRES:
            zone["status"] = ZONE_STATUS_DROPPED
            zone["drop_reason"] = FLAG_BELOW_MIN_AREA
            zone["rank"] = None
            zone["cross_type_overlaps"] = []
            zone["below_min_area"] = True
            if FLAG_BELOW_MIN_AREA not in zone["flags"]:
                zone["flags"].append(FLAG_BELOW_MIN_AREA)
            dropped_zones.append(zone)
        else:
            zone["status"] = ZONE_STATUS_NOMINATED
            zone["drop_reason"] = None
            surviving_zones.append(zone)

    rank_survey_zones_per_type(surviving_zones)
    attach_cross_type_overlaps(surviving_zones)
    selected = select_survey_zone(surviving_zones)

    return {
        "zones": surviving_zones,
        "zones_by_type": {
            survey_type: sorted(
                [zone for zone in surviving_zones if zone["survey_type"] == survey_type],
                key=lambda zone: zone["rank"],
            )
            for survey_type in SURVEY_TYPES
        },
        "dropped_zones": dropped_zones,
        "regions": regions,
        "regions_by_type": {
            survey_type: [region for region in regions if region["survey_type"] == survey_type]
            for survey_type in SURVEY_TYPES
        },
        "embankment_seeds": embankment_seeds,
        "selected_water_zone": selected,
        "surfaces": surfaces,
        "screens": {
            "twi_raw": twi_raw,
            "twi_percentile": twi_percentile,
            "depression_depth": depression_depth,
            "depression_depth_raw": depression_depth_raw,
            "flow_accumulation": flow_accumulation,
            "slope_pct": slope_pct,
            "filled": filled,
        },
        "gate_mask": gate_mask,
        "gate_mask_stats": gate_stats,
        "soil": soil,
        "soil_checked": soil_checked,
        "threshold": threshold,
    }


# ==========================================================================
# Wire forms: GeoJSON + narrative_data
# ==========================================================================

def _zone_feature_properties(zone: dict) -> dict:
    """The JSON-serializable property set every survey-zone feature
    carries -- the full measurement contract, flagged not filtered.
    TYPE-DISPATCHED since the compartment change (the honesty split made
    wire shape): an EXCAVATED zone carries dual acreage (member_acres =
    the anchoring signal; zone_acres = the clipped hull, the ground to
    walk), member linkage and the sparse_anchor guard; an EMBANKMENT
    zone is a valley compartment with NO members -- it carries the
    SEED's blend score and criteria signature (the anchor claim,
    separate from the compartment's own criterion means, which
    deliberately average in side slopes and the wall reach), the pinch
    record (crest-to-crest width, walk distance), the baseline length,
    and the truncation/bound flags."""
    properties = {
        "zone_id": zone["id"],
        "survey_type": zone["survey_type"],
        "nominated_by": zone["nominated_by"],
        # Lifecycle: status/drop_reason are the established
        # dropped-not-silent pattern. (A `presented` property existed
        # for one pass and was deleted with the presentation cap --
        # every surviving zone is presented.)
        "status": zone["status"],
        "drop_reason": zone["drop_reason"],
        "rank": zone["rank"],
        # The cross-type agreement report (fractions of THIS zone's
        # envelope overlapped by surviving zones of the other type).
        "cross_type_overlaps": list(zone["cross_type_overlaps"]),
        "zone_acres": zone["zone_acres"],
        "cell_count": zone["cell_count"],
        "mean_suitability": zone["mean_suitability"],
        "max_suitability": zone["max_suitability"],
        "criterion_contributions": zone["criterion_contributions"],
        "twi_percentile_mean": zone["twi_percentile_mean"],
        "twi_percentile_max": zone["twi_percentile_max"],
        "depression_depth_mean_m": zone["depression_depth_mean_m"],
        "depression_depth_max_m": zone["depression_depth_max_m"],
        "contributing_area_acres_at_wettest_cell": zone["contributing_area_acres_at_wettest_cell"],
        "slope_median_pct": zone["slope_median_pct"],
        "boundary_adjacency_fraction": zone["boundary_adjacency_fraction"],
        "canopy_overlap_pct": zone["canopy_overlap_pct"],
        # Since roads became a geometric exclusion this measures the
        # PRE-road-clip geometry -- the share of the walkable claim the
        # road clip removed (the drawn geometry already excludes it);
        # None still means the road layer was never checked.
        "road_overlap_pct": zone["road_overlap_pct"],
        "production_overlap_pct": zone["production_overlap_pct"],
        "primary_production_area_relationship": zone["primary_production_area_relationship"],
        "production_area_relationships": zone["production_area_relationships"],
        "has_service_relationship": zone["has_service_relationship"],
        "served_production_area_ids": zone["served_production_area_ids"],
        "soil_coverage_fraction": zone["soil_coverage_fraction"],
        "criteria_complete": zone["criteria_complete"],
        "flags": list(zone["flags"]),
        "below_min_area": zone["below_min_area"],
        "truncated_by_road": zone["truncated_by_road"],
        "representative_elevation_m": round(zone["representative_elevation_m"], 2),
    }
    if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
        properties.update(
            {
                # THE ANCHOR CLAIM, kept separate from the compartment's
                # criterion_contributions above (the walked ground).
                "seed_blend_score": zone["seed_blend_score"],
                "seed_criteria_signature": dict(zone["seed"]["criteria_signature"]),
                "seed_rowcol": list(zone["seed"]["rowcol"]),
                "pinch_rowcol": list(zone["pinch"]["rowcol"]),
                "pinch_width_m": zone["pinch"]["width_m"],
                "pinch_walk_distance_m": zone["pinch"]["walk_distance_m"],
                "baseline_length_m": zone["baseline"]["length_m"],
                "truncated_by_boundary": zone["truncated_by_boundary"],
                "half_width_bound_hit": zone["half_width_bound_hit"],
            }
        )
    else:
        properties.update(
            {
                # The excavated honesty reports: dual acreage, member
                # linkage, the sparse-anchor guard.
                "sparse_anchor": zone["sparse_anchor"],
                "member_ids": list(zone["member_ids"]),
                "member_count": zone["member_count"],
                "member_acres": zone["member_acres"],
            }
        )
    return properties


def _member_feature_properties(region: dict) -> dict:
    """The JSON-serializable property set every MEMBER feature carries:
    its own measurements plus zone linkage (zone-level properties --
    gravity, overlaps, selection -- live on the parent survey_zone
    feature, deliberately not duplicated here)."""
    return {
        "region_id": region["id"],
        "zone_id": region["zone_id"],
        "survey_type": region["survey_type"],
        "area_acres": region["area_acres"],
        "cell_count": region["cell_count"],
        "mean_suitability": region["mean_suitability"],
        "max_suitability": region["max_suitability"],
        "criterion_contributions": region["criterion_contributions"],
        "twi_percentile_mean": region["twi_percentile_mean"],
        "depression_depth_max_m": region["depression_depth_max_m"],
        "contributing_area_acres_at_wettest_cell": region["contributing_area_acres_at_wettest_cell"],
        "slope_median_pct": region["slope_median_pct"],
        "boundary_adjacency_fraction": region["boundary_adjacency_fraction"],
        "soil_coverage_fraction": region["soil_coverage_fraction"],
        "criteria_complete": region["criteria_complete"],
        "flags": list(region["flags"]),
        "below_min_area": region["below_min_area"],
        "representative_elevation_m": round(region["representative_elevation_m"], 2),
    }


_MEMBER_FEATURE_NOTE = (
    "MEMBER region of a survey zone: the exact cell-union footprint of ground that cleared the "
    "suitability threshold -- the anchoring signal, intact and unredrawn. Zone-level properties "
    "(gravity, overlaps, dual acreage, selection) live on the parent survey_zone feature this "
    "member's zone_id points to."
)


_DROPPED_ZONE_NOTE = (
    "DROPPED from the pipeline output (status: dropped; drop_reason names why): below_min_area means "
    "the judged acreage -- the walkable hull envelope for an excavated zone, the compartment's own "
    "polygon for an embankment zone -- measures under the 0.1 ac floor; duplicate_of_zone_<id> means "
    "this compartment collapsed into a better-seeded one describing the same valley ground. Carried "
    "here visible and attributed, never silently, with the judged acreage on the record (zone_acres; "
    "excavated records also keep member_acres, the anchoring signal inside the envelope)."
)


def survey_areas_to_geojson(zones: list[dict], dropped_zones: Optional[list[dict]] = None) -> dict:
    """
    Every SURVIVING survey zone and every member region as one
    schema-conformant FeatureCollection: zone envelopes on
    survey_zone_embankment / survey_zone_excavated (full properties,
    dual acreage, member linkage, status lifecycle) and member
    footprints on survey_zone_member_<type> (zone linkage both ways).
    When `dropped_zones` is supplied (the diagnostic export path), each
    floor-dropped zone additionally rides the survey_zone_dropped layer
    with the established status/reason pattern -- the pipeline's own
    zones_geojson omits them (they are out of the output), the export
    shows them attributed. STORED WIRE FORMS ONLY: geometry is each
    object's geometry_wgs84 built at its birth -- no serialization-time
    reprojection anywhere in this module.

    CONSOLIDATED into wire_translation.py (as water_survey_zones_to_
    feature_collection) -- this name stays as the module's own entry
    point, forwarding to the single implementation kept there. The
    property builders and note constants below stay HERE: they are this
    module's own measurement vocabulary, not wire shape.
    """
    from wire_translation import water_survey_zones_to_feature_collection

    return water_survey_zones_to_feature_collection(zones, dropped_zones)


def _feet(meters: Optional[float]) -> Optional[float]:
    if meters is None:
        return None
    return round(meters / METERS_PER_FOOT, 1)


def build_narrative_data(result: dict) -> dict:
    """
    Pre-digested, FINAL, JSON-serializable narrative values -- imperial
    at this boundary (acres, feet, percent), None (never 0.0) for
    unavailable, no reason strings beyond the flag enumeration, per the
    established narrative_data doctrine. EVERY SURVIVING ZONE is listed
    with the total count (the presentation cap was deleted -- the user
    decides what to walk), beside the dropped count so the narrative
    can state what the floor pruned; per-criterion mean scores (MEMBER
    cells only) are the narrative-honesty mechanism -- prose may only
    claim what a criterion actually scored, and each zone's block
    carries those scores directly. Dual acreage carries the narrative
    sentence's two numbers ("zone_acres to survey, anchored by
    member_acres of high-suitability ground"), with sparse_anchor and
    the cross-type either_type_candidate finding riding each block.
    twi_is_parcel_relative + twi_note surface the parcel-relative
    caveat so the report layer cannot overclaim wetness.
    """
    surviving = result["zones"]
    dropped = result["dropped_zones"]
    selected = result["selected_water_zone"]
    stats = result["gate_mask_stats"]

    zone_blocks = []
    for zone in sorted(surviving, key=lambda z: (z["survey_type"], z["rank"])):
        primary = zone["primary_production_area_relationship"]
        if primary is None:
            gravity = {"has_service_relationship": False, "can_gravity_feed": None}
        else:
            gravity = {
                "has_service_relationship": True,
                "can_gravity_feed": bool(primary["above_production_area"]),
                "production_area_id": primary["production_area_id"],
                "elevation_differential_ft": _feet(primary["elevation_differential_m"]),
                "distance_ft": _feet(primary["distance_m"]),
            }
        block = {
            "id": zone["id"],
            "survey_type": zone["survey_type"],
            "rank": zone["rank"],
            "zone_acres": round(zone["zone_acres"], 1),
            "mean_suitability": zone["mean_suitability"],
            "max_suitability": zone["max_suitability"],
            "criteria": {
                name: {"weight": entry["weight"], "mean_score": entry["mean_score"]}
                for name, entry in zone["criterion_contributions"].items()
            },
            "twi_percentile_mean": zone["twi_percentile_mean"],
            "depression_depth_max_ft": _feet(zone["depression_depth_max_m"]),
            "contributing_area_acres_at_wettest_cell": zone["contributing_area_acres_at_wettest_cell"],
            "boundary_adjacency_pct": round(zone["boundary_adjacency_fraction"] * 100, 1),
            "overlaps": {
                # All three REPORTED, none scored. Canopy/production are
                # measured on the (clipped) envelope -- the ground being
                # surveyed; road_pct measures the PRE-road-clip
                # geometry, i.e. the share of the walkable claim the
                # road clip removed (the drawn geometry already
                # excludes it). None means never checked; 0.0 means
                # checked and genuinely none.
                "canopy_pct": zone["canopy_overlap_pct"],
                "road_pct": zone["road_overlap_pct"],
                "production_pct": zone["production_overlap_pct"],
            },
            "gravity": gravity,
            "flags": list(zone["flags"]),
            "below_min_area": zone["below_min_area"],
            "truncated_by_road": zone["truncated_by_road"],
            # The agreement report: percent of THIS zone's envelope
            # overlapped by each surviving zone of the other type,
            # plus the constant-driven either-type finding the
            # report's consultant line keys on.
            "cross_type_overlaps": [
                {"zone_id": entry["zone_id"], "overlap_pct": round(entry["fraction"] * 100, 1)}
                for entry in zone["cross_type_overlaps"]
            ],
            "either_type_candidate": any(
                entry["fraction"] >= CROSS_TYPE_OVERLAP_NOTE_FRACTION
                for entry in zone["cross_type_overlaps"]
            ),
            "confidence": zone["confidence"],
        }
        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
            # THE HONESTY SPLIT, translated to narrative: the SEED's
            # blend score and criteria signature (the anchor claim)
            # ride SEPARATELY from `criteria` above, which holds the
            # COMPARTMENT's means over the walked ground -- ground
            # that deliberately includes low-scoring side slopes and
            # the wall reach, because that is the compartment's job.
            # The report's sentence is "zone_acres acres to survey --
            # a valley compartment anchored by a
            # seed_blend_score-scoring storage cell, dam reach at the
            # downstream end."
            block.update(
                {
                    "seed_blend_score": zone["seed_blend_score"],
                    "seed_criteria_signature": dict(zone["seed"]["criteria_signature"]),
                    "pinch_width_ft": _feet(zone["pinch"]["width_m"]),
                    "pinch_walk_distance_ft": _feet(zone["pinch"]["walk_distance_m"]),
                    "baseline_length_ft": _feet(zone["baseline"]["length_m"]),
                    "truncated_by_boundary": zone["truncated_by_boundary"],
                    "half_width_bound_hit": zone["half_width_bound_hit"],
                }
            )
        else:
            # DUAL ACREAGE, both labeled -- the excavated sentence
            # stays "zone_acres to survey, anchored by member_acres of
            # high-suitability ground".
            block.update(
                {
                    "member_count": zone["member_count"],
                    "member_acres": round(zone["member_acres"], 1),
                    "sparse_anchor": zone["sparse_anchor"],
                }
            )
        zone_blocks.append(block)

    seeds = result.get("embankment_seeds", [])
    failed_seeds = [record for record in seeds if record["status"] == SEED_STATUS_FAILED]

    return {
        "zone_found": bool(surviving),
        "zone_count": len(surviving),
        "dropped_count": len(dropped),
        "member_region_count": len(result["regions"]),
        "embankment_zone_count": len(result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]),
        "excavated_zone_count": len(result["zones_by_type"][SURVEY_TYPE_EXCAVATED]),
        # The embankment generation accounting (the dropped-feature
        # pattern, seed edition): every seed either built a compartment
        # or failed with its reason named -- a reach with no on-parcel
        # pinch reports honestly as nothing.
        "embankment_generation": PROVENANCE_SEED_COMPARTMENT,
        "embankment_seed_count": len(seeds),
        "embankment_failed_seed_count": len(failed_seeds),
        "embankment_failed_seeds": [
            {
                "rowcol": list(record["rowcol"]),
                "blend_score": record["blend_score"],
                "reason_code": record.get("reason_code"),
            }
            for record in failed_seeds
        ],
        "suitability_threshold": result["threshold"],
        "grouping_distance_meters": SURVEY_ZONE_GROUPING_DISTANCE_METERS,
        "twi_is_parcel_relative": True,
        "twi_note": TWI_PARCEL_RELATIVE_NOTE,
        "gates": {
            "on_parcel_cells": stats["on_parcel_cells"],
            "ceiling_removed_cells": stats["ceiling_removed_cells"],
            "setback_removed_cells": stats["setback_removed_cells"],
            "gated_cells": stats["gated_cells"],
            "max_contributing_area_acres": stats["max_contributing_area_acres"],
        },
        "soil_checked": result["soil_checked"],
        "selection": {
            "selected_zone_id": selected["id"] if selected is not None else None,
            "selected_survey_type": selected["survey_type"] if selected is not None else None,
            # PROVISIONAL pooling rule, restated where the report reads it
            # -- see select_survey_zone().
            "selection_rule": "pooled_member_mean_suitability_member_acreage_tiebreak",
        },
        "zones": zone_blocks,
    }


def summarize_water_survey_areas(result: dict) -> str:
    zones = result["zones"]
    dropped = result["dropped_zones"]
    seeds = result.get("embankment_seeds", [])
    failed_seeds = [record for record in seeds if record["status"] == SEED_STATUS_FAILED]
    if not zones and not dropped and not failed_seeds:
        return (
            "No water survey zones: no embankment seed qualified and nothing cleared the excavated "
            "suitability threshold."
        )
    lines = [
        f"Water survey zones ({len(zones)} surviving -- all listed, {len(dropped)} dropped; "
        f"{len(result['regions'])} excavated member region(s); "
        f"{len(seeds)} embankment seed(s), {len(failed_seeds)} failed):"
    ]
    for zone in sorted(zones, key=lambda z: (z["survey_type"], z["rank"])):
        top_two = sorted(
            zone["criterion_contributions"].items(),
            key=lambda item: -item[1]["weighted_contribution"],
        )[:2]
        criteria_text = ", ".join(f"{name}={entry['mean_score']}" for name, entry in top_two)
        flag_text = f" [{', '.join(zone['flags'])}]" if zone["flags"] else ""
        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
            lines.append(
                f"  - embankment rank {zone['rank']}: zone {zone['id']}, "
                f"{zone['zone_acres']} ac valley compartment anchored by a "
                f"{zone['seed_blend_score']}-scoring seed (pinch width {zone['pinch']['width_m']} m at "
                f"{zone['pinch']['walk_distance_m']} m downstream), compartment mean "
                f"{zone['mean_suitability']}, compartment criteria: {criteria_text}{flag_text}"
            )
        else:
            lines.append(
                f"  - excavated rank {zone['rank']}: zone {zone['id']}, "
                f"{zone['zone_acres']} ac to survey anchored by {zone['member_acres']} ac "
                f"({zone['member_count']} member(s)), mean {zone['mean_suitability']}, top criteria: "
                f"{criteria_text}{flag_text}"
            )
    for zone in dropped:
        if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT:
            lines.append(
                f"  - DROPPED ({zone['drop_reason']}): embankment zone {zone['id']}, compartment "
                f"{zone['zone_acres']} ac (seed blend {zone['seed_blend_score']})"
            )
        else:
            lines.append(
                f"  - DROPPED ({zone['drop_reason']}): excavated zone {zone['id']}, envelope "
                f"{zone['zone_acres']} ac (anchored by {zone['member_acres']} ac) under the "
                f"{MIN_SURVEY_REGION_AREA_ACRES} ac floor"
            )
    for record in failed_seeds:
        lines.append(
            f"  - FAILED SEED ({record.get('reason_code')}): blend {record['blend_score']} at "
            f"{tuple(record['rowcol'])} -- no compartment, honestly (no fallback exists on this path)"
        )
    return "\n".join(lines)


# ==========================================================================
# Fetch-and-compute entry point
# ==========================================================================

# Parameter sentinel for identify_water_survey_areas()'s soil_inputs:
# distinguishes "the caller supplied nothing, self-fetch" from a caller
# supplying a real None ("soil was already attempted and is genuinely
# unavailable" -- reuse that answer, no second fetch). Same reasoning as
# water_candidate_zones._ROAD_UNION_NOT_SUPPLIED.
_SOIL_INPUTS_NOT_SUPPLIED = object()


def soil_inputs_for_parcel_data(parcel_data) -> Optional[dict]:
    """
    A ParcelData -> this step's `soil_inputs` override, or None.

    ALL THREE PIECES OR NONE, which is the water scorer's own posture stated
    once. build_soil_score_grid() renormalizes over sub-signals that are
    absent WITHIN a successful fetch; a partial FETCH is a different fact and
    must not be dressed up as one -- so a ParcelData missing any of the three
    yields None ("never checked"), and the whole soil criterion degrades to
    neutral with the confidence signal lost and the narrative saying so.

    HERE RATHER THAN IN ITS TWO CALLERS. build_pipeline_context() assembled
    this inline, and the step registry's water entry now needs the same
    assembly (as its soil_inputs edge's `combine`) because a registry
    cache_path names ONE attribute and this override is three. Two copies of
    an all-or-nothing rule is two places for it to become an
    any-two-of-three rule. It reads a ParcelData and builds a dict -- no
    fetch, no computation, nothing this module's suitability path can see.
    """
    ksat_rows = getattr(parcel_data, "saturated_hydraulic_conductivity", None)
    components = getattr(parcel_data, "soil_components", None)
    geometries_by_mukey = getattr(parcel_data, "soil_geometries", None)
    if ksat_rows is None or components is None or geometries_by_mukey is None:
        return None
    return {
        "ksat_rows": ksat_rows,
        "components": components,
        "geometries_by_mukey": geometries_by_mukey,
    }


def _fetch_soil_inputs(boundary_coordinates: list[tuple[float, float]]) -> dict:
    """
    STANDALONE-CALLER FALLBACK ONLY -- the pipeline path never reaches
    this. On the pipeline path all three pieces come from ParcelData
    (Layer 1: fetched once, hard-fail governed) and ride through
    build_pipeline_context() into the soil_inputs override; this
    self-fetch exists so identify_water_survey_areas() invoked standalone
    (a diagnostic, a one-off script) still works, with the documented
    fetch-or-degrade posture.

    One whole-boundary fetch of the three soil pieces the scorer reads:
    ksat (dominant component, shallowest mineral horizon), full component
    rows (hydric share AND hydrologic group -- hydgrp rides
    get_soil_data_for_polygon() since the hydrologic-group query change),
    and clipped map-unit geometry. Whole-boundary rather than per-region
    because the surfaces need per-CELL soil before any region exists.
    Raises on failure; the caller degrades to the never-checked posture.
    """
    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    return {
        "ksat_rows": get_saturated_hydraulic_conductivity_for_polygon(wkt_polygon),
        "components": get_soil_data_for_polygon(wkt_polygon),
        "geometries_by_mukey": get_soil_geometries_for_polygon(wkt_polygon),
    }


def identify_water_survey_areas(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    production_areas: Optional[list[dict]] = None,
    canopy_height: Optional[dict] = None,
    road_exclusion_union_utm=_ROAD_UNION_NOT_SUPPLIED,
    soil_inputs=_SOIL_INPUTS_NOT_SUPPLIED,
    check_soil: bool = True,
    threshold: float = SUITABILITY_THRESHOLD,
) -> dict:
    """
    Full water-step entry point: fetches whatever wasn't supplied,
    computes both suitability surfaces, extracts + measures every survey
    region, and returns wire forms plus the raw result.

    Overrides follow the established conventions, each independent:
      dem / boundary_polygon_utm / production_areas -- None falls back
        to self-compute (production defaults to production_area_ceiling.
        identify_optimized_production_areas()'s scored_patches, the SAME
        optimized geometry road_corridors scores against -- the
        corrected default the retired water_suitability.py documented).
      canopy -- FETCH-OR-RAISE (get_required_tree_root_zone_mask_utm at
        WATER_ZONE_CANOPY_BUFFER_METERS): a silently missing measurement
        is worse than a loud failure, and the pipeline path supplies
        canopy_height from ParcelData so the fetch is free there.
      roads -- fetch-or-degrade: an outage yields road_overlap_pct=None
        ("never checked"), never a fabricated 0.0; a caller-supplied
        real None is the CLEAN "checked, genuinely no mapped road"
        answer and is reused, not re-fetched. THE UNION IS A GEOMETRIC
        EXCLUSION now, not just a measurement: it gates embankment
        seeds, hard-terminates embankment pinch walks, and clips both
        types' zone geometry (truncated_by_road) -- an unavailable or
        clean-None union simply leaves no road constraint in play.
      soil -- LAYER 1 ON THE PIPELINE PATH: all three pieces (ksat rows,
        component rows carrying hydgrp, clipped map-unit geometry) are
        ParcelData fields, fetched once behind its hard-fail contract
        and forwarded here through build_pipeline_context()'s
        soil_inputs override, so the pipeline never fetches soil inside
        this step. STANDALONE callers get fetch-or-degrade: one
        whole-boundary fetch of the three pieces (_fetch_soil_inputs());
        any failure degrades the WHOLE soil criterion to
        never-checked -- neutral scores, coverage None, the confidence
        signal lost, and the narrative saying NEVER CHECKED --
        deliberately all-or-nothing, so a partial outage can't
        masquerade as measured soil, and NEVER silently swallowed into
        the renormalization (renormalizing over available sub-signals
        is for absent data WITHIN a successful fetch; a failed fetch is
        a different fact and reports as one).

    Returns:
        {
            'zones_geojson': FeatureCollection (zone envelopes on the
                             survey_zone_* layers plus member footprints
                             on survey_zone_member_*, every one, flagged
                             not filtered),
            'zones': list[dict] (the survey zones -- the deliverable),
            'zones_by_type': {type: ranked zone list},
            'regions': list[dict] (member regions, footprints intact),
            'regions_by_type': {type: member list},
            'selected_water_zone': pooled rank-1 ZONE dict or None (the
                                   downstream consumer contract lands
                                   here: id / render_fill_polygon_utm =
                                   the clipped envelope, identity /
                                   representative_elevation_m from
                                   member cells),
            'narrative_data': build_narrative_data() block,
            'gate_mask_stats': the gate accounting,
            'result': the full compute_water_survey_areas() dict
                      (surfaces/screens/masks -- diagnostic consumers),
        }
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

    if production_areas is None:
        production_areas = identify_optimized_production_areas(
            boundary_coordinates, dem=dem, canopy_height=canopy_height
        )["scored_patches"]

    canopy_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=WATER_ZONE_CANOPY_BUFFER_METERS, canopy_height=canopy_height
    )

    if road_exclusion_union_utm is _ROAD_UNION_NOT_SUPPLIED:
        try:
            road_exclusion_union_utm = _fetch_road_exclusion_union_utm(boundary_coordinates, dem)
        except Exception:
            road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    if soil_inputs is _SOIL_INPUTS_NOT_SUPPLIED:
        if check_soil:
            try:
                soil_inputs = _fetch_soil_inputs(boundary_coordinates)
            except Exception:
                logger.warning("identify_water_survey_areas: soil fetch failed -- soil criterion never checked")
                soil_inputs = None
        else:
            soil_inputs = None

    result = compute_water_survey_areas(
        dem,
        boundary_polygon_utm,
        production_areas=production_areas,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
        soil_inputs=soil_inputs,
        threshold=threshold,
    )

    return {
        # SURVIVING zones only: the floor's drops are out of the
        # pipeline output by decision (they ride the diagnostic export's
        # dropped layer instead -- visible, attributed, not planned on).
        "zones_geojson": survey_areas_to_geojson(result["zones"]),
        "zones": result["zones"],
        "zones_by_type": result["zones_by_type"],
        "dropped_zones": result["dropped_zones"],
        "regions": result["regions"],
        "regions_by_type": result["regions_by_type"],
        "embankment_seeds": result["embankment_seeds"],
        "selected_water_zone": result["selected_water_zone"],
        "narrative_data": build_narrative_data(result),
        "gate_mask_stats": result["gate_mask_stats"],
        "result": result,
    }


if __name__ == "__main__":
    # Quick offline eyeball check against a synthetic bowl-in-a-slope DEM
    # -- see test_water_survey_areas.py for the real (assertion-based)
    # version, and diagnose_water_survey_areas.py for the networked run.
    rows = cols = 40
    resolution = 5.0
    array = np.zeros((rows, cols), dtype=np.float64)
    for r in range(rows):
        for c in range(cols):
            array[r, c] = 110.0 - r * 0.25 + abs(c - cols // 2) * 0.3
    # A shallow closed basin near the bottom of the slope.
    for r in range(28, 33):
        for c in range(17, 23):
            array[r, c] -= 1.0

    origin_x, origin_y = 500000.0, 4500000.0
    dem = {
        "array": array,
        "resolution_meters": (resolution, resolution),
        "origin_x": origin_x,
        "origin_y": origin_y,
        "crs": "EPSG:32617",
    }
    boundary = Polygon(
        [
            (origin_x + 2 * resolution, origin_y - 2 * resolution),
            (origin_x + (cols - 2) * resolution, origin_y - 2 * resolution),
            (origin_x + (cols - 2) * resolution, origin_y - (rows - 2) * resolution),
            (origin_x + 2 * resolution, origin_y - (rows - 2) * resolution),
        ]
    )

    result = compute_water_survey_areas(dem, boundary)
    print(summarize_water_survey_areas(result))
