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
evidence -- see EXCAVATED_SLOPE_FULL_CREDIT_PCT), the member-acreage
floor (a FILTER now, drops visible and attributed -- see
MIN_SURVEY_REGION_AREA_ACRES's history note), and the presented set
(WATER_ZONE_PRESENTATION_TOP_N = 3 with the per-type consultant
guarantee). The diagnostic's instruments (threshold comparison,
isobands, the excavated interrogation) keep printing every run so each
decision remains evidence-checked rather than trusted forward.

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

EXTRACTION AND AGGREGATION, per type -- scoring stays sharp, grouping
makes the survey areas:

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

THE FLOOR AND THE PRESENTED SET (tuned decisions -- the two places the
output narrows, both visible): a zone whose summed MEMBER acreage sits
under the MIN_SURVEY_REGION_AREA_ACRES floor is DROPPED from the
pipeline output -- status: dropped + drop_reason on the zone, carried
in the diagnostic table and the export's survey_zone_dropped layer,
never silent (see the constant's history note for the flag-only
posture it replaced). The narrated set is the top
WATER_ZONE_PRESENTATION_TOP_N surviving zones with the per-type
consultant guarantee; unpresented survivors keep full properties and
ride the export with `presented: false`.

SELECTION (the pooled rule, unchanged and INDEPENDENT of
presentation): the two types are POOLED by member-mean suitability
(member-acreage tiebreak) and the pooled rank-1 SURVIVING zone becomes
`selected_water_zone` for downstream consumers (tree search-space
subtraction, fencing, solar exclusion, road exclusion, the map's ripple
clip, keypoint relationships). Pooling embankment against excavated
compares two different instruments on one scale -- kept because
downstream needs ONE unambiguous answer, and the presentation
guarantee never touches rank 1 (see apply_presentation()). The
selected zone carries every field on the established
selected_water_zone consumer contract (render_fill_polygon_utm,
representative_elevation_m, id -- plus rank and
served_production_area_ids read by pipeline tests), so rank-1 slots in
unchanged.

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
    make_feature,
    make_feature_collection,
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


# --- region extraction ----------------------------------------------------

# Cells at/above this RAW blended suitability score are extracted into
# member regions. 0.5, DECIDED FROM MEASURED EVIDENCE (the final tuning
# pass): 0.6 had been chosen from the first run's PRE-smoothing
# isobands; with smoothing retired, the raw-surface threshold comparison
# on the reference property read 16 member regions / 0.51 ac of
# anchoring ground at 0.5 versus 5 sub-floor slivers / 0.08 ac at 0.6.
# The deciding insight: the parcel's ACHIEVABLE maximum blend is ~0.82,
# not 1.0 -- the soil criterion's parcel range caps the arithmetic -- so
# a threshold judges against attainable scores, and 0.6 was demanding
# ~3/4 of the attainable ceiling while 0.5 sits at the coherence line
# the isobands actually show. The diagnostic keeps printing the
# THRESHOLD COMPARISON (0.5 / 0.6 / 0.7 on the raw surfaces,
# 8-connected) every run, so the choice remains evidence-checked. This
# constant only supplies the extraction function's DEFAULT; it is not
# baked into the math anywhere. CONFIGURABLE.
SUITABILITY_THRESHOLD = 0.5


# --- survey-zone grouping (the closing over extracted regions) ------------

# Member regions closer than this fuse into one SURVEY ZONE: each
# member's footprint is buffered outward by HALF this distance, the
# buffers are unioned, and the union is buffered back inward by the same
# amount -- a morphological CLOSING in vector space, so gaps up to the
# FULL distance bridge. 30 m is the scale at which two high-suitability
# patches are one site visit, not two -- v1 prior, TUNE FROM RUN.
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

# THE FLOOR IS A FILTER NOW: a survey zone whose MEMBER acreage falls
# below this is DROPPED from the pipeline output (and the presented
# set) -- member acres are the anchoring signal, and the envelope never
# rescues a zone whose actual high-suitability ground is a sliver.
# Dropped zones are never silent: they ride the diagnostic terminal
# table and the GeoJSON export with the established status/reason
# pattern (status: dropped, drop_reason: below_min_area) -- visible and
# attributed, excluded from downstream planning. The value matches
# water_candidate_zones.MIN_WATER_ZONE_AREA_ACRES (0.1 ac = 17 cells at
# the pipeline's 5 m DEM resolution), the "smaller than this is
# probably raster noise" line.
#
# HISTORY, kept on purpose: through the tuning passes this constant was
# deliberately a FLAG, not a filter (`below_min_area`, first-run
# posture) -- every sliver stayed visible while the threshold, the
# smoothing question, and the grouping distance were being decided from
# runs that needed to show everything. Tuning is done; the exploration
# posture ends here, and the flag semantics were retired WITH their
# rationale rather than silently. Individual member REGIONS below the
# floor still only carry the flag (a sub-floor member can belong to an
# above-floor zone -- the ZONE's summed member acreage is what the
# filter judges). CONFIGURABLE.
MIN_SURVEY_REGION_AREA_ACRES = 0.1

# The presented set: the top N SURVIVING zones pooled by member-mean
# suitability -- with the PER-TYPE GUARANTEE (the consultant rule): if a
# type produced at least one surviving zone and none lands in the pooled
# top N, the lowest-ranked presented zone is swapped for that type's
# best, keeping the presented count at N. "Your best dam area and your
# best dugout area both appear whenever both exist" -- a farmer weighing
# the two instruments should always see one of each when the parcel
# offers one of each. Presentation is INDEPENDENT of selection:
# select_survey_zone()'s pooled rank-1 answer is unchanged by any swap
# (the swap only ever replaces the LOWEST presented zone, never rank 1).
# Every surviving zone still carries full properties and rides the
# GeoJSON with its `presented` property, so an unpresented survivor is
# inspectable, just not narrated. Decided in the final tuning pass,
# ending the no-presentation-cap exploration posture. CONFIGURABLE.
WATER_ZONE_PRESENTATION_TOP_N = 3

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


# --- flags (flag, don't filter -- the module's whole posture) -------------

FLAG_BELOW_MIN_AREA = "below_min_area"
FLAG_NO_SERVICE_RELATIONSHIP = "no_service_relationship"
"""Module-level flag constants, same convention as water_candidate_zones'
reason/flag enumeration: a caller or test that reacts to a flag compares
against a name, never a re-typed string. Both are FLAGS (informational),
not outcomes -- no region is dropped for carrying either."""

PROVENANCE_SUITABILITY_SURFACE = "suitability_surface"


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
) -> list[dict]:
    """
    The aggregation step: per type, member regions whose footprints sit
    within grouping_distance_meters of each other fuse into one SURVEY
    ZONE -- one code path for clusters and singletons (a lone region's
    zone is approximately its own footprint). The zone is the
    deliverable object downstream consumers receive; members ride along
    intact as sub-features with zone-id linkage both ways.

    Geometry: the closing envelope, CLIPPED TO THE PARCEL BOUNDARY (the
    survey happens on the user's land); boundary adjacency computes on
    the clipped envelope. Score statistics come from MEMBER CELLS ONLY
    via the same _measure_member_cells() the members themselves used --
    the envelope never launders sub-threshold ground into a score. Dual
    acreage carries both truths: member_acres (cell-count acreage of
    ground that actually cleared the threshold -- the anchoring signal)
    and zone_acres (the clipped envelope's polygon acreage -- the
    ground to walk; polygon-area acreage is correct here exactly
    because a zone envelope is a drawn boundary, not a cell population
    -- the same cell-vs-polygon acreage split exclusion_zones.py
    documents).

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

            clipped_envelope = envelope.intersection(boundary_polygon_utm)
            if clipped_envelope.is_empty:
                # Members are on-parcel by construction, so their
                # envelope always keeps on-parcel area -- guarded anyway.
                logger.warning("build_survey_zones: clipped envelope empty -- using member footprints")
                clipped_envelope = unary_union([member["polygon_utm"] for member in zone_members])

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
            # Flag on MEMBER acreage: the anchoring signal is what the
            # floor was ever about -- an envelope can be arbitrarily
            # larger than the ground that earned it.
            if member_acres < MIN_SURVEY_REGION_AREA_ACRES:
                flags.append(FLAG_BELOW_MIN_AREA)

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
        "Embankment-type: a small dam across a drainageway (AH590)."
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


def rank_survey_zones_per_type(zones: list[dict]) -> None:
    """
    Assigns `rank` per type IN PLACE: 1 = highest MEMBER-mean
    suitability within that type, member acreage as the tiebreak
    (larger anchoring signal first -- between two equally-scored zones,
    the one anchored by more high-suitability ground ranks first; the
    envelope's own acreage never ranks anything). Every zone is ranked;
    flags never affect rank (first-run posture).
    """
    for survey_type in SURVEY_TYPES:
        typed = [zone for zone in zones if zone["survey_type"] == survey_type]
        typed.sort(key=lambda zone: (-zone["mean_suitability"], -zone["member_acres"]))
        for rank, zone in enumerate(typed, start=1):
            zone["rank"] = rank


def apply_presentation(zones: list[dict], top_n: int = WATER_ZONE_PRESENTATION_TOP_N) -> list[dict]:
    """
    Marks each SURVIVING zone's `presented` flag IN PLACE and returns
    the presented list: the top `top_n` zones pooled by member-mean
    suitability (member-acreage tiebreak), adjusted by the PER-TYPE
    GUARANTEE -- if a type has at least one surviving zone and none made
    the pooled top N, the LOWEST-ranked presented zone is swapped out
    for that type's best, keeping the count at N (the consultant rule:
    "your best dam area and your best dugout area both appear whenever
    both exist"). With survivors <= top_n everything is presented and
    the guarantee is trivially satisfied.

    INDEPENDENT OF SELECTION by construction: the swap only ever
    replaces the last (lowest) presented zone, so the pooled rank-1 zone
    -- select_survey_zone()'s answer -- is presented and unchanged
    under every input (asserted with a swap fixture in
    test_water_survey_areas.py).
    """
    pooled = sorted(zones, key=lambda zone: (-zone["mean_suitability"], -zone["member_acres"]))
    presented = pooled[:top_n]

    for survey_type in SURVEY_TYPES:
        typed_survivors = [zone for zone in pooled if zone["survey_type"] == survey_type]
        if not typed_survivors:
            continue
        if any(zone["survey_type"] == survey_type for zone in presented):
            continue
        # The guarantee swap: this type survived but missed the pooled
        # top N -- its best replaces the lowest presented zone. Never
        # the first slot: rank 1 is selection's answer and is presented
        # unconditionally (a degenerate top_n=1 keeps rank 1 rather
        # than honoring the guarantee -- the invariant outranks it).
        if len(presented) > 1:
            presented[-1] = typed_survivors[0]

    for zone in zones:
        zone["presented"] = False
    for zone in presented:
        zone["presented"] = True
    return presented


def select_survey_zone(zones: list[dict]) -> Optional[dict]:
    """
    The single selected_water_zone answer for downstream consumers:
    embankment and excavated POOLED by member-mean suitability (member
    acreage tiebreak), rank-1 of the pool wins. PROVISIONAL,
    deliberately simple -- pooling compares two different survey
    instruments on one scale, which is defensible only because
    downstream needs ONE unambiguous answer; revisit from the tuned run
    (the winner's type is itself a finding). Flags never affect
    selection (a below-min-area zone CAN win -- first-run posture; the
    flag rides along for the reader). Returns None when no zone exists
    at all -- the real, reportable "nothing cleared the threshold"
    outcome, not an error.
    """
    if not zones:
        return None
    return max(zones, key=lambda zone: (zone["mean_suitability"], zone["member_acres"]))


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
) -> dict:
    """
    Pure computation over already-fetched inputs -- no network I/O
    anywhere below here (the identify_* entry point owns the fetches).

    production_areas is None for "never checked" (production overlap and
    gravity report their None/empty answers accordingly) or the
    optimized scored_patches list. canopy_root_zone_mask_utm /
    road_exclusion_union_utm carry the shared unchecked sentinels from
    water_candidate_zones (None on the road union is the CLEAN "checked,
    genuinely no mapped road" answer, per the established semantics).
    soil_inputs: see build_soil_score_grid(). filled /
    flow_accumulation / slope_pct are optional precomputed overrides so
    an orchestrator (or a test spying on call counts) can guarantee each
    derivation runs EXACTLY ONCE; each self-computes when absent.

    Returns a dict of survey ZONES (the deliverable: closing-aggregated,
    flagged not filtered, per-type lists, pooled selection), their
    member REGIONS (all of them, footprints intact), the surfaces dict
    (surfaces[type] = the RAW blend extraction thresholds -- smoothing
    is retired from this path, see masked_focal_mean(); surfaces
    ["criteria"] = the raw criterion grids), the screens (including the
    unfloored depression_depth_raw for the excavated instrumentation),
    the gate mask, and its stats (numpy/shapely -- NOT
    JSON-serializable; the geojson/narrative builders produce the wire
    forms).
    """
    array = dem["array"]

    if filled is None:
        filled = fill_depressions(array)
    if flow_accumulation is None:
        flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
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

    regions: list[dict] = []
    for survey_type in SURVEY_TYPES:
        regions.extend(
            extract_survey_regions(
                dem,
                surfaces[survey_type],
                surfaces["criteria"][survey_type],
                survey_type,
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
        )

    # The aggregation: member regions close into survey zones -- the
    # deliverable objects everything below attaches to.
    zones = build_survey_zones(
        dem,
        regions,
        surfaces,
        {
            "twi_percentile": twi_percentile,
            "depression_depth": depression_depth,
            "flow_accumulation": flow_accumulation,
            "slope_pct": slope_pct,
            "soil_covered_mask": soil["covered_mask"],
            "soil_checked": soil_checked,
        },
        boundary_polygon_utm,
    )

    # Overlaps + gravity on the ZONE, both pure measurements over inputs
    # in hand. Canopy/road overlap runs on the ENVELOPE's cell
    # population (the ground being surveyed -- the established
    # cell-fraction machinery and its None/0.0 sentinel semantics,
    # unchanged); production overlap on the envelope polygon; gravity
    # and representative elevation from MEMBER cells via the existing
    # representative-point machinery.
    canopy_checked = canopy_root_zone_mask_utm is not _CANOPY_CHECK_UNCHECKED
    canopy_mask = canopy_root_zone_mask_utm if canopy_checked else None
    road_checked = road_exclusion_union_utm is not _ROAD_CHECK_UNCHECKED
    road_union = road_exclusion_union_utm if road_checked and road_exclusion_union_utm is not None else None
    road_prepared = prep(road_union) if road_union is not None else None

    for zone in zones:
        envelope_cells = _cells_in_polygon_utm(dem, zone["polygon_utm"])
        zone["canopy_overlap_pct"] = _overlap_fraction_pct(envelope_cells, dem, canopy_checked, mask_utm=canopy_mask)
        zone["road_overlap_pct"] = _overlap_fraction_pct(
            envelope_cells, dem, road_checked, prepared_union=road_prepared
        )
        zone["production_overlap_pct"] = _production_overlap_pct(zone["polygon_utm"], production_areas)

        member_union = unary_union([member["polygon_utm"] for member in zone["members"]])
        relationships = (
            _zone_production_area_relationships(
                member_union.centroid,
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
    # cross-type list so every zone -- dropped included -- has one
    # unambiguous identity; member regions get their own ids and each
    # carries its parent's zone_id.
    for zone_id, zone in enumerate(zones):
        zone["id"] = zone_id
    for region_id, region in enumerate(regions):
        region["id"] = region_id
    for zone in zones:
        zone["member_ids"] = [member["id"] for member in zone["members"]]
        for member in zone["members"]:
            member["zone_id"] = zone["id"]
    for zone in zones:
        zone["confidence"] = _confidence_for_region(zone, soil_checked)
        zone["confidence_notes"] = _confidence_notes_for_region(zone, soil_checked)
    for region in regions:
        region["confidence"] = _confidence_for_region(region, soil_checked)

    # THE FLOOR FILTERS NOW (see MIN_SURVEY_REGION_AREA_ACRES's history
    # note): a zone whose summed MEMBER acreage sits under the floor is
    # dropped from the pipeline output -- status/reason attached, rank
    # None, never presented -- and survives only in the diagnostic table
    # and the export's dropped layer.
    surviving_zones: list[dict] = []
    dropped_zones: list[dict] = []
    for zone in zones:
        if zone["member_acres"] < MIN_SURVEY_REGION_AREA_ACRES:
            zone["status"] = ZONE_STATUS_DROPPED
            zone["drop_reason"] = FLAG_BELOW_MIN_AREA
            zone["rank"] = None
            zone["presented"] = False
            dropped_zones.append(zone)
        else:
            zone["status"] = ZONE_STATUS_NOMINATED
            zone["drop_reason"] = None
            surviving_zones.append(zone)

    rank_survey_zones_per_type(surviving_zones)
    presented_zones = apply_presentation(surviving_zones)
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
        "presented_zones": presented_zones,
        "regions": regions,
        "regions_by_type": {
            survey_type: [region for region in regions if region["survey_type"] == survey_type]
            for survey_type in SURVEY_TYPES
        },
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
    carries -- the full measurement contract, flagged not filtered, with
    DUAL ACREAGE (member_acres = the anchoring signal; zone_acres = the
    clipped envelope, the ground to walk) and member linkage."""
    return {
        "zone_id": zone["id"],
        "survey_type": zone["survey_type"],
        "nominated_by": zone["nominated_by"],
        # Lifecycle + presentation: status/drop_reason are the
        # established dropped-not-silent pattern; `presented`
        # distinguishes the narrated top-N (per-type guarantee applied)
        # from surviving-but-unpresented zones, which remain fully
        # inspectable here.
        "status": zone["status"],
        "drop_reason": zone["drop_reason"],
        "presented": zone["presented"],
        "rank": zone["rank"],
        "member_ids": list(zone["member_ids"]),
        "member_count": zone["member_count"],
        "member_acres": zone["member_acres"],
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
        "representative_elevation_m": round(zone["representative_elevation_m"], 2),
    }


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
    "DROPPED at the member-acreage floor (status: dropped, drop_reason: below_min_area): this zone's "
    "actual high-suitability ground sums under the 0.1 ac floor, so it is excluded from the pipeline "
    "output and the presented set -- carried here visible and attributed, never silently. Member "
    "acres are the anchoring signal; the envelope never rescues a sliver."
)


def survey_areas_to_geojson(zones: list[dict], dropped_zones: Optional[list[dict]] = None) -> dict:
    """
    Every SURVIVING survey zone and every member region as one
    schema-conformant FeatureCollection: zone envelopes on
    survey_zone_embankment / survey_zone_excavated (full properties,
    dual acreage, member linkage, status/presented) and member
    footprints on survey_zone_member_<type> (zone linkage both ways).
    When `dropped_zones` is supplied (the diagnostic export path), each
    floor-dropped zone additionally rides the survey_zone_dropped layer
    with the established status/reason pattern -- the pipeline's own
    zones_geojson omits them (they are out of the output), the export
    shows them attributed. STORED WIRE FORMS ONLY: geometry is each
    object's geometry_wgs84 built at its birth -- no serialization-time
    reprojection anywhere in this module.
    """
    features = []
    for zone in zones:
        features.append(
            make_feature(
                feature_id=f"water-survey-zone-{zone['id']}",
                geometry=zone["geometry_wgs84"],
                layer=f"survey_zone_{zone['survey_type']}",
                label=(
                    f"Survey zone {zone['id']} ({zone['survey_type']}-type, rank {zone['rank']}): "
                    f"{zone['zone_acres']} ac to survey, anchored by {zone['member_acres']} ac of "
                    f"high-suitability ground ({zone['member_count']} member(s))"
                ),
                confidence=zone["confidence"],
                confidence_notes=zone["confidence_notes"],
                extra_properties=_zone_feature_properties(zone),
            )
        )
        for member in zone["members"]:
            features.append(
                make_feature(
                    feature_id=f"water-survey-zone-member-{member['id']}",
                    geometry=member["geometry_wgs84"],
                    layer=f"survey_zone_member_{member['survey_type']}",
                    label=(
                        f"Member region {member['id']} of survey zone {zone['id']} "
                        f"({member['area_acres']} ac)"
                    ),
                    confidence=member["confidence"],
                    confidence_notes=_MEMBER_FEATURE_NOTE,
                    extra_properties=_member_feature_properties(member),
                )
            )
    for zone in dropped_zones or []:
        features.append(
            make_feature(
                feature_id=f"water-survey-zone-dropped-{zone['id']}",
                geometry=zone["geometry_wgs84"],
                layer="survey_zone_dropped",
                label=(
                    f"DROPPED survey zone {zone['id']} ({zone['survey_type']}-type): member ground "
                    f"{zone['member_acres']} ac under the {MIN_SURVEY_REGION_AREA_ACRES} ac floor"
                ),
                confidence=zone["confidence"],
                confidence_notes=_DROPPED_ZONE_NOTE,
                extra_properties=_zone_feature_properties(zone),
            )
        )
    return make_feature_collection(features)


def _feet(meters: Optional[float]) -> Optional[float]:
    if meters is None:
        return None
    return round(meters / METERS_PER_FOOT, 1)


def build_narrative_data(result: dict) -> dict:
    """
    Pre-digested, FINAL, JSON-serializable narrative values -- imperial
    at this boundary (acres, feet, percent), None (never 0.0) for
    unavailable, no reason strings beyond the flag enumeration, per the
    established narrative_data doctrine. The PRESENTED zones are listed
    (WATER_ZONE_PRESENTATION_TOP_N pooled, with the per-type guarantee),
    beside the survivor and dropped counts so the narrative can state
    what it is not showing; per-criterion mean scores (MEMBER cells
    only) are the narrative-honesty mechanism -- prose may only claim
    what a criterion actually scored, and each zone's block carries
    those scores directly. Dual acreage carries the narrative sentence's
    two numbers ("zone_acres to survey, anchored by member_acres of
    high-suitability ground"). twi_is_parcel_relative + twi_note surface
    the parcel-relative caveat so the report layer cannot overclaim
    wetness.
    """
    surviving = result["zones"]
    presented = result["presented_zones"]
    dropped = result["dropped_zones"]
    selected = result["selected_water_zone"]
    stats = result["gate_mask_stats"]

    pooled = sorted(surviving, key=lambda z: (-z["mean_suitability"], -z["member_acres"]))
    guarantee_applied = [z["id"] for z in presented] != [z["id"] for z in pooled[: len(presented)]]

    zone_blocks = []
    for zone in sorted(presented, key=lambda z: (z["survey_type"], z["rank"])):
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
        zone_blocks.append(
            {
                "id": zone["id"],
                "survey_type": zone["survey_type"],
                "rank": zone["rank"],
                "member_count": zone["member_count"],
                # DUAL ACREAGE, both labeled -- the report's sentence is
                # "zone_acres to survey, anchored by member_acres of
                # high-suitability ground".
                "member_acres": round(zone["member_acres"], 1),
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
                    # All three REPORTED, none scored, measured on the
                    # ENVELOPE (the ground being surveyed). None means
                    # never checked; 0.0 means checked and genuinely none.
                    "canopy_pct": zone["canopy_overlap_pct"],
                    "road_pct": zone["road_overlap_pct"],
                    "production_pct": zone["production_overlap_pct"],
                },
                "gravity": gravity,
                "flags": list(zone["flags"]),
                "below_min_area": zone["below_min_area"],
                "confidence": zone["confidence"],
            }
        )

    return {
        "zone_found": bool(surviving),
        "zone_count": len(surviving),
        "presented_count": len(presented),
        "dropped_count": len(dropped),
        "presentation_top_n": WATER_ZONE_PRESENTATION_TOP_N,
        "presentation_guarantee_applied": guarantee_applied,
        "member_region_count": len(result["regions"]),
        "embankment_zone_count": len(result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]),
        "excavated_zone_count": len(result["zones_by_type"][SURVEY_TYPE_EXCAVATED]),
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
    if not zones and not dropped:
        return "No water survey zones: nothing cleared the suitability threshold."
    lines = [
        f"Water survey zones ({len(zones)} surviving, {len(result['presented_zones'])} presented, "
        f"{len(dropped)} dropped at the member floor; {len(result['regions'])} member region(s)):"
    ]
    for zone in sorted(zones, key=lambda z: (z["survey_type"], z["rank"])):
        top_two = sorted(
            zone["criterion_contributions"].items(),
            key=lambda item: -item[1]["weighted_contribution"],
        )[:2]
        criteria_text = ", ".join(f"{name}={entry['mean_score']}" for name, entry in top_two)
        flag_text = f" [{', '.join(zone['flags'])}]" if zone["flags"] else ""
        presented_text = " PRESENTED" if zone["presented"] else ""
        lines.append(
            f"  - {zone['survey_type']} rank {zone['rank']}{presented_text}: zone {zone['id']}, "
            f"{zone['zone_acres']} ac to survey anchored by {zone['member_acres']} ac "
            f"({zone['member_count']} member(s)), mean {zone['mean_suitability']}, top criteria: "
            f"{criteria_text}{flag_text}"
        )
    for zone in dropped:
        lines.append(
            f"  - DROPPED ({zone['drop_reason']}): {zone['survey_type']} zone {zone['id']}, member "
            f"ground {zone['member_acres']} ac under the {MIN_SURVEY_REGION_AREA_ACRES} ac floor"
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
        answer and is reused, not re-fetched.
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
        "presented_zones": result["presented_zones"],
        "regions": result["regions"],
        "regions_by_type": result["regions_by_type"],
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
